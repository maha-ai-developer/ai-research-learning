"""Read-only Local Research Library HTTP API (`v1`).

Provides a local-first, offline-safe, strictly read-only HTTP API backed by the
canonical SQLite `KnowledgeStore`:

- `GET /healthz`: Report service and database schema health.
- `GET /api/v1/sources`: List registered sources and available status/policy info.
- `GET /api/v1/resources`: List resources with pagination and metadata filters.
- `GET /api/v1/resources/{resource_id}`: Inspect one resource's metadata, version
  history, and transcript registry summary.
- `GET /api/v1/search?q=...`: Deterministic parameterized search over locally
  stored resource metadata (`title`, `abstract_or_description`, `authors`,
  `topic_tags`, `external_identifier`, `canonical_url`, `manifest_id`).
- `GET /api/v1/transcripts`: List transcript registry metadata with pagination and
  optional filters.
- `GET /api/v1/transcripts/{transcript_id}`: Inspect metadata and version-status
  information for one transcript.

Safety & Governance Invariants:
1. Strictly read-only: SQLite connections are opened in read-only mode and enforce
   `PRAGMA query_only = ON`. No API request ever mutates the database.
2. Zero live network access: Every endpoint reads exclusively from local SQLite tables.
3. Transcript protection boundary: Transcript endpoints expose registry and version-status
   metadata only. They NEVER query or return `transcript_segments`, verbatim excerpts,
   protected transcript text, or `transcript_syntheses` content.
4. Loopback binding by default: The local HTTP server binds to `127.0.0.1` by default.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

# Ensure root module imports work whether invoked via `python3 -m src.ai_research_learning.api`
# or with `PYTHONPATH=src`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from knowledge_store import (  # noqa: E402
    DEFAULT_DB_PATH,
    LATEST_SCHEMA_VERSION,
    VALID_AUTHORIZATION_BASES,
    VALID_CAPTION_FORMATS,
    VALID_TRANSCRIPT_ACQUISITION_METHODS,
    VALID_TRANSCRIPT_INGESTION_STATES,
    VALID_TRANSCRIPT_LICENSE_STATUSES,
    VALID_TRANSCRIPT_ORIGIN_TYPES,
    VALID_TRANSCRIPT_STORAGE_STATUSES,
    VALID_TRANSCRIPT_TRACK_KINDS,
    _is_iso_timestamp_expired,
    utc_now,
    validate_bcp47_language_tag,
)

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_SEARCH_QUERY_LENGTH = 500

VALID_MEDIA_MODALITIES: tuple[str, ...] = (
    "TEXT_HTML",
    "PDF_DOCUMENT",
    "VIDEO_LINK",
    "VIDEO_WITH_TRANSCRIPT",
    "TEXT_PDF_HTML",
    "SLIDE_INDEX",
    "COMPLIANCE_ENDPOINT",
    "AUDIO_PODCAST",
    "MULTIMODAL",
)
VALID_RESOURCE_LIFECYCLE_STATES: tuple[str, ...] = (
    "ACTIVE",
    "ARCHIVED",
    "SUPERSEDED",
    "WITHDRAWN",
    "TOMBSTONED",
    "PENDING_REVIEW",
    "DEPRECATED",
)

RESOURCE_ID_RE = re.compile(r"^res_[0-9a-f]{16}$")
TRANSCRIPT_ID_RE = re.compile(r"^tr_[0-9a-f]{16}$")
SOURCE_ID_RE = re.compile(r"^SRC-[A-Z0-9_-]{1,60}$")

# Columns exposed from `transcript_versions` (strictly metadata and hashes; no text).
TRANSCRIPT_VERSION_METADATA_COLUMNS: tuple[str, ...] = (
    "transcript_version_id",
    "transcript_id",
    "version_number",
    "raw_content_sha256",
    "normalized_text_sha256",
    "normalizer_version",
    "asr_model_name",
    "asr_model_version",
    "asr_compute_device",
    "source_caption_url",
    "segment_count",
    "word_count",
    "char_count",
    "mean_confidence",
    "low_confidence_segment_count",
    "status",
    "created_at",
)

# Columns exposed from `transcripts` (strictly registry metadata; no segment or synthesis text).
TRANSCRIPT_METADATA_COLUMNS: tuple[str, ...] = (
    "transcript_id",
    "resource_id",
    "source_id",
    "media_canonical_url",
    "language_bcp47",
    "track_kind",
    "origin_type",
    "acquisition_method",
    "caption_format",
    "speaker_attribution_quality",
    "license_status",
    "license_spdx",
    "authorization_basis",
    "authorization_attestation_id",
    "permits_transcript_storage",
    "permits_verbatim_excerpts",
    "permits_ai_synthesis",
    "permits_redistribution",
    "attribution_text",
    "ingestion_state",
    "transcript_storage_status",
    "block_or_review_reason",
    "latest_transcript_version_id",
    "segment_count",
    "duration_ms",
    "created_at",
    "updated_at",
)


class APIError(Exception):
    """Structured HTTP API exception mapped to an RFC 9457 problem response."""

    def __init__(
        self,
        *,
        status: int,
        error_code: str,
        title: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.error_code = error_code
        self.title = title
        self.detail = detail
        self.headers = dict(headers) if headers else {}

    def to_problem_dict(self, *, instance: str) -> dict[str, Any]:
        slug = self.error_code.lower().replace("_", "-")
        return {
            "type": f"https://ai-research-learning.local/errors/{slug}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "instance": instance,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class APIResponse:
    """In-memory representation of an HTTP response returned by `LocalResearchLibraryAPI`."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str]

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _escape_like_term(value: str) -> str:
    """Escape `\\`, `%`, and `_` for parameterized SQLite `LIKE ... ESCAPE '\\'` clauses."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_delimited_or_json_list(raw_value: Any) -> list[str]:
    """Parse a JSON array or comma/semicolon-delimited string into a normalized list of strings."""
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    text = str(raw_value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[;,]", text)
    return [part.strip() for part in parts if part.strip()]


class LocalResearchLibraryAPI:
    """Read-only query and routing layer over the local SQLite `KnowledgeStore`.

    All SQLite connections opened by this service enforce `PRAGMA query_only = ON`
    (and `mode=ro` URI connections for on-disk files) so that no API endpoint can
    create, update, or delete database rows.
    """

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        *,
        connection: sqlite3.Connection | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self._shared_conn = connection
        self._clock: Callable[[], str] = clock or utc_now
        if self._shared_conn is not None:
            self._shared_conn.row_factory = sqlite3.Row
            self._shared_conn.execute("PRAGMA query_only = ON")

    def set_clock(self, clock: Callable[[], str] | None) -> None:
        """Set or reset the clock used for read-only attestation expiry evaluation."""
        self._clock = clock or utc_now

    def _now_iso(self) -> str:
        return self._clock()

    def _open_readonly_connection(self) -> tuple[sqlite3.Connection, bool]:
        """Return `(conn, should_close)` with `PRAGMA query_only = ON`."""
        if self._shared_conn is not None:
            self._shared_conn.row_factory = sqlite3.Row
            self._shared_conn.execute("PRAGMA query_only = ON")
            return self._shared_conn, False

        if not self.db_path.is_file():
            raise APIError(
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                error_code="DATABASE_NOT_INITIALIZED",
                title="Knowledge Store Not Initialized",
                detail=(
                    f"Local SQLite database was not found at {str(self.db_path)!r}. "
                    "Initialize the store first with 'python3 knowledge_store.py init'."
                ),
            )

        resolved_uri = self.db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(resolved_uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn, True

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @classmethod
    def _require_tables(cls, conn: sqlite3.Connection, required_tables: tuple[str, ...]) -> None:
        missing = [tbl for tbl in required_tables if not cls._table_exists(conn, tbl)]
        if missing:
            raise APIError(
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                error_code="SCHEMA_NOT_READY",
                title="Database Schema Incomplete",
                detail=(
                    f"Required SQLite table(s) {', '.join(missing)} are not present. "
                    "Run 'python3 knowledge_store.py init' to apply schema migrations."
                ),
            )

    @staticmethod
    def _extract_single_params(
        query_string: str,
        *,
        allowed_keys: set[str],
    ) -> dict[str, str]:
        """Parse query string and reject unknown or repeated parameters with 400 Bad Request."""
        if not query_string:
            return {}
        parsed = parse_qs(query_string, keep_blank_values=True, strict_parsing=False)
        unknown = sorted(set(parsed.keys()) - allowed_keys)
        if unknown:
            raise APIError(
                status=HTTPStatus.BAD_REQUEST,
                error_code="INVALID_QUERY_PARAMETER",
                title="Invalid Query Parameter",
                detail=(
                    f"Unsupported query parameter(s): {', '.join(unknown)}. "
                    f"Allowed parameters: {', '.join(sorted(allowed_keys)) if allowed_keys else 'none'}."
                ),
            )
        result: dict[str, str] = {}
        for key, values in parsed.items():
            if len(values) > 1:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="DUPLICATE_QUERY_PARAMETER",
                    title="Duplicate Query Parameter",
                    detail=f"Query parameter {key!r} must not be specified more than once.",
                )
            result[key] = values[0]
        return result

    @staticmethod
    def _parse_pagination(params: dict[str, str]) -> tuple[int, int]:
        """Validate and return `(limit, offset)` pagination values."""
        raw_limit = params.get("limit")
        if raw_limit is None:
            limit = DEFAULT_PAGE_LIMIT
        else:
            cleaned_limit = raw_limit.strip()
            if not re.fullmatch(r"-?\d+", cleaned_limit):
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_PAGINATION",
                    title="Invalid Pagination Parameter",
                    detail=f"Query parameter 'limit' must be an integer between 1 and {MAX_PAGE_LIMIT}, got {raw_limit!r}.",
                )
            limit = int(cleaned_limit)
            if limit < 1 or limit > MAX_PAGE_LIMIT:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_PAGINATION",
                    title="Invalid Pagination Parameter",
                    detail=f"Query parameter 'limit' must be between 1 and {MAX_PAGE_LIMIT}, got {limit}.",
                )

        raw_offset = params.get("offset")
        if raw_offset is None:
            offset = 0
        else:
            cleaned_offset = raw_offset.strip()
            if not re.fullmatch(r"-?\d+", cleaned_offset):
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_PAGINATION",
                    title="Invalid Pagination Parameter",
                    detail=f"Query parameter 'offset' must be a non-negative integer, got {raw_offset!r}.",
                )
            offset = int(cleaned_offset)
            if offset < 0:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_PAGINATION",
                    title="Invalid Pagination Parameter",
                    detail=f"Query parameter 'offset' must be >= 0, got {offset}.",
                )

        return limit, offset

    @staticmethod
    def _validate_source_id(raw_source_id: str, *, param_name: str = "source_id") -> str:
        cleaned = raw_source_id.strip()
        if not cleaned or not SOURCE_ID_RE.fullmatch(cleaned):
            raise APIError(
                status=HTTPStatus.BAD_REQUEST,
                error_code="INVALID_IDENTIFIER",
                title="Invalid Source Identifier",
                detail=f"Parameter {param_name!r} must be a valid source ID starting with 'SRC-', got {raw_source_id!r}.",
            )
        return cleaned

    @staticmethod
    def _validate_resource_id(raw_resource_id: str, *, param_name: str = "resource_id") -> str:
        cleaned = raw_resource_id.strip()
        if not RESOURCE_ID_RE.fullmatch(cleaned):
            raise APIError(
                status=HTTPStatus.BAD_REQUEST,
                error_code="INVALID_IDENTIFIER",
                title="Invalid Resource Identifier",
                detail=(
                    f"Identifier {param_name!r} must match 'res_<16 lowercase hex chars>', "
                    f"got {raw_resource_id!r}."
                ),
            )
        return cleaned

    @staticmethod
    def _validate_transcript_id(raw_transcript_id: str, *, param_name: str = "transcript_id") -> str:
        cleaned = raw_transcript_id.strip()
        if not TRANSCRIPT_ID_RE.fullmatch(cleaned):
            raise APIError(
                status=HTTPStatus.BAD_REQUEST,
                error_code="INVALID_IDENTIFIER",
                title="Invalid Transcript Identifier",
                detail=(
                    f"Identifier {param_name!r} must match 'tr_<16 lowercase hex chars>', "
                    f"got {raw_transcript_id!r}."
                ),
            )
        return cleaned

    # ------------------------------------------------------------------
    # Serialization & read-only authorization projection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_resource_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        return {
            "resource_id": d["resource_id"],
            "source_id": d["source_id"],
            "manifest_id": d.get("manifest_id"),
            "canonical_url": d["canonical_url"],
            "fetched_url": d.get("fetched_url"),
            "title": d["title"],
            "authors": d.get("authors"),
            "authors_list": _parse_delimited_or_json_list(d.get("authors")),
            "published_at": d.get("published_at"),
            "resource_type": d["resource_type"],
            "media_modality": d.get("media_modality"),
            "domain_scope": d.get("domain_scope"),
            "abstract_or_description": d.get("abstract_or_description"),
            "topic_tags": d.get("topic_tags"),
            "topic_tags_list": _parse_delimited_or_json_list(d.get("topic_tags")),
            "external_identifier": d.get("external_identifier"),
            "observed_from": d.get("observed_from"),
            "information_to_inventory": d.get("information_to_inventory"),
            "priority": d.get("priority"),
            "collection_decision": d.get("collection_decision"),
            "verification_status": d.get("verification_status"),
            "robots_sitemap_status": d.get("robots_sitemap_status"),
            "rights_status": d.get("rights_status"),
            "license_status": d["license_status"],
            "license_spdx": d.get("license_spdx"),
            "permitted_storage_scope": d["permitted_storage_scope"],
            "permits_full_text_storage": bool(int(d.get("permits_full_text_storage") or 0)),
            "permits_verbatim_excerpts": bool(int(d.get("permits_verbatim_excerpts") or 0)),
            "legal_uncertainty_status": d["legal_uncertainty_status"],
            "text_storage_status": d["text_storage_status"],
            "current_version_id": d.get("current_version_id"),
            "version_count": int(d.get("version_count") or 0),
            "metadata_hash": d["metadata_hash"],
            "content_hash": d.get("content_hash"),
            "lifecycle_state": d["lifecycle_state"],
            "first_discovered_at": d["first_discovered_at"],
            "last_verified_at": d.get("last_verified_at"),
            "last_checked": d.get("last_checked"),
            "updated_at": d["updated_at"],
        }

    @staticmethod
    def _format_resource_version_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        return {
            "version_id": d["version_id"],
            "resource_id": d["resource_id"],
            "version_number": int(d["version_number"]),
            "canonical_url": d["canonical_url"],
            "title": d["title"],
            "authors": d.get("authors"),
            "published_at": d.get("published_at"),
            "resource_type": d["resource_type"],
            "abstract_or_description": d.get("abstract_or_description"),
            "topic_tags": d.get("topic_tags"),
            "external_identifier": d.get("external_identifier"),
            "collection_decision": d.get("collection_decision"),
            "verification_status": d.get("verification_status"),
            "robots_sitemap_status": d.get("robots_sitemap_status"),
            "rights_status": d.get("rights_status"),
            "license_status": d.get("license_status"),
            "text_storage_status": d.get("text_storage_status"),
            "metadata_hash": d["metadata_hash"],
            "content_hash": d.get("content_hash"),
            "change_reason": d.get("change_reason"),
            "provenance_event_id": d.get("provenance_event_id"),
            "created_at": d["created_at"],
        }

    def _project_transcript_metadata_readonly(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        now_iso: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """Project a `transcripts` row into metadata-only dictionary without mutating SQLite.

        If the linked `transcript_authorization_attestations` record has naturally expired
        (`expires_at <= now_iso`) or is withdrawn/unverified while the stored row still shows
        an active state, projects the effective tombstoned/revoked state in-memory so read-only
        clients never see stale authorization flags.
        """
        raw = dict(row)
        projected = {col: raw.get(col) for col in TRANSCRIPT_METADATA_COLUMNS}

        att_id = projected.get("authorization_attestation_id")
        att_summary: dict[str, Any] | None = None
        revoked_or_expired_in_memory = False

        if att_id and self._table_exists(conn, "transcript_authorization_attestations"):
            att_row = conn.execute(
                """
                SELECT attestation_id, resource_id, transcript_id, language_bcp47, track_kind,
                       authorization_basis, license_status, license_spdx, evidence_type,
                       evidence_reference, evidence_url, evidence_sha256,
                       permits_metadata_indexing, permits_transcript_storage,
                       permits_verbatim_excerpts, permits_ai_synthesis, permits_redistribution,
                       reviewed_by, reviewed_at, verification_status, expires_at,
                       withdrawn_at, withdrawn_by, withdrawal_reason, revision_number,
                       created_at, updated_at
                  FROM transcript_authorization_attestations
                 WHERE attestation_id = ?
                """,
                (str(att_id),),
            ).fetchone()
            if att_row is not None:
                att_dict = dict(att_row)
                is_expired = _is_iso_timestamp_expired(att_dict.get("expires_at"), now_iso=now_iso)
                is_withdrawn = bool(att_dict.get("withdrawn_at")) or (
                    str(att_dict.get("verification_status")) == "WITHDRAWN"
                )
                effective_att_status = (
                    "WITHDRAWN"
                    if is_withdrawn
                    else ("EXPIRED" if is_expired else str(att_dict["verification_status"]))
                )
                att_active = (effective_att_status == "VERIFIED") and not is_expired and not is_withdrawn
                row_claims_active_storage = (
                    projected.get("ingestion_state") in ("INGESTED", "READY_FOR_INGESTION")
                    or int(projected.get("permits_transcript_storage") or 0) == 1
                )
                if is_expired or is_withdrawn or (not att_active and row_claims_active_storage):
                    revoked_or_expired_in_memory = True
                att_summary = {
                    "attestation_id": att_dict["attestation_id"],
                    "verification_status": effective_att_status,
                    "authorization_basis": att_dict["authorization_basis"],
                    "license_status": att_dict["license_status"],
                    "license_spdx": att_dict.get("license_spdx"),
                    "evidence_type": att_dict["evidence_type"],
                    "evidence_reference": att_dict["evidence_reference"],
                    "evidence_url": att_dict.get("evidence_url"),
                    "reviewed_by": att_dict["reviewed_by"],
                    "reviewed_at": att_dict["reviewed_at"],
                    "expires_at": att_dict.get("expires_at"),
                    "is_expired": bool(is_expired),
                    "withdrawn_at": att_dict.get("withdrawn_at"),
                    "permits_metadata_indexing": bool(int(att_dict.get("permits_metadata_indexing") or 0)),
                    "permits_transcript_storage": bool(
                        att_active and int(att_dict.get("permits_transcript_storage") or 0)
                    ),
                    "permits_verbatim_excerpts": bool(
                        att_active and int(att_dict.get("permits_verbatim_excerpts") or 0)
                    ),
                    "permits_ai_synthesis": bool(
                        att_active and int(att_dict.get("permits_ai_synthesis") or 0)
                    ),
                    "permits_redistribution": bool(
                        att_active and int(att_dict.get("permits_redistribution") or 0)
                    ),
                }
            else:
                revoked_or_expired_in_memory = True

        if revoked_or_expired_in_memory:
            projected["permits_transcript_storage"] = False
            projected["permits_verbatim_excerpts"] = False
            projected["permits_ai_synthesis"] = False
            projected["permits_redistribution"] = False
            projected["ingestion_state"] = "WITHDRAWN_OR_TOMBSTONED"
            if projected.get("transcript_storage_status") not in (
                "TOMBSTONED_ON_TAKEDOWN",
                "PURGED_ON_TAKEDOWN",
            ):
                projected["transcript_storage_status"] = "TOMBSTONED_ON_TAKEDOWN"
            projected["latest_transcript_version_id"] = None
            projected["segment_count"] = 0
            if att_summary and att_summary.get("is_expired"):
                exp_ts = att_summary.get("expires_at")
                projected["block_or_review_reason"] = (
                    projected.get("block_or_review_reason")
                    or f"Authorization attestation {att_id} expired at {exp_ts}."
                )
        else:
            projected["permits_transcript_storage"] = bool(int(projected.get("permits_transcript_storage") or 0))
            projected["permits_verbatim_excerpts"] = bool(int(projected.get("permits_verbatim_excerpts") or 0))
            projected["permits_ai_synthesis"] = bool(int(projected.get("permits_ai_synthesis") or 0))
            projected["permits_redistribution"] = bool(int(projected.get("permits_redistribution") or 0))
            projected["segment_count"] = int(projected.get("segment_count") or 0)

        return projected, att_summary, revoked_or_expired_in_memory

    # ------------------------------------------------------------------
    # Endpoint Implementations
    # ------------------------------------------------------------------

    def get_healthz(self, *, instance: str = "/healthz") -> APIResponse:
        """`GET /healthz`: Report service health and local database schema readiness."""
        db_exists = self.db_path.is_file() or (self._shared_conn is not None)
        schema_version = 0
        initialized = False
        if db_exists:
            conn, should_close = self._open_readonly_connection()
            try:
                if self._table_exists(conn, "schema_migrations"):
                    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
                    schema_version = int(row[0]) if row is not None else 0
                    initialized = schema_version >= 1
            finally:
                if should_close:
                    conn.close()

        body = {
            "status": "ok",
            "service": "ai-research-learning-local-api",
            "api_version": "v1",
            "read_only": True,
            "network_access": "disabled",
            "database": {
                "path": str(self.db_path),
                "exists": bool(db_exists),
                "initialized": bool(initialized),
                "schema_version": schema_version,
                "latest_supported_schema_version": LATEST_SCHEMA_VERSION,
            },
        }
        return APIResponse(status=HTTPStatus.OK, body=body, headers={})

    def list_sources_endpoint(self, query_string: str = "") -> APIResponse:
        """`GET /api/v1/sources`: List registered sources and available status/policy information."""
        params = self._extract_single_params(
            query_string,
            allowed_keys={"status", "source_category", "adapter_type", "only_complete"},
        )
        status_filter = params.get("status")
        category_filter = params.get("source_category")
        adapter_filter = params.get("adapter_type")
        only_complete_raw = params.get("only_complete")
        only_complete: bool | None = None
        if only_complete_raw is not None:
            lowered = only_complete_raw.strip().lower()
            if lowered in ("1", "true", "yes"):
                only_complete = True
            elif lowered in ("0", "false", "no"):
                only_complete = False
            else:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Boolean Query Parameter",
                    detail=f"Parameter 'only_complete' must be true/false or 1/0, got {only_complete_raw!r}.",
                )

        conn, should_close = self._open_readonly_connection()
        try:
            self._require_tables(conn, ("sources", "resources"))
            clauses: list[str] = []
            sql_params: list[Any] = []
            if status_filter is not None:
                clean_status = status_filter.strip().upper()
                if not clean_status:
                    raise APIError(
                        status=HTTPStatus.BAD_REQUEST,
                        error_code="INVALID_QUERY_PARAMETER",
                        title="Invalid Status Filter",
                        detail="Parameter 'status' must not be empty.",
                    )
                clauses.append("s.status = ?")
                sql_params.append(clean_status)
            if category_filter is not None:
                clean_cat = category_filter.strip().upper()
                if not clean_cat:
                    raise APIError(
                        status=HTTPStatus.BAD_REQUEST,
                        error_code="INVALID_QUERY_PARAMETER",
                        title="Invalid Source Category Filter",
                        detail="Parameter 'source_category' must not be empty.",
                    )
                clauses.append("s.source_category = ?")
                sql_params.append(clean_cat)
            if adapter_filter is not None:
                clean_adapter = adapter_filter.strip().upper()
                if not clean_adapter:
                    raise APIError(
                        status=HTTPStatus.BAD_REQUEST,
                        error_code="INVALID_QUERY_PARAMETER",
                        title="Invalid Adapter Type Filter",
                        detail="Parameter 'adapter_type' must not be empty.",
                    )
                clauses.append("s.adapter_type = ?")
                sql_params.append(clean_adapter)
            if only_complete is not None:
                clauses.append("s.metadata_complete = ?")
                sql_params.append(1 if only_complete else 0)

            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            has_transcripts = self._table_exists(conn, "transcripts")
            has_site_policies = self._table_exists(conn, "site_policies")

            rows = conn.execute(
                f"""
                SELECT s.*,
                       (SELECT COUNT(*) FROM resources r WHERE r.source_id = s.source_id) AS resource_count
                  FROM sources s
                  {where_sql}
                 ORDER BY s.source_id ASC
                """,
                tuple(sql_params),
            ).fetchall()

            transcript_counts: dict[str, int] = {}
            if has_transcripts:
                for tr_row in conn.execute(
                    "SELECT source_id, COUNT(*) AS cnt FROM transcripts GROUP BY source_id"
                ).fetchall():
                    transcript_counts[str(tr_row["source_id"])] = int(tr_row["cnt"])

            site_policies_by_origin: dict[str, dict[str, Any]] = {}
            if has_site_policies:
                for pol_row in conn.execute(
                    "SELECT origin, robots_url, status, crawl_delay_seconds, checked_at FROM site_policies"
                ).fetchall():
                    site_policies_by_origin[str(pol_row["origin"]).rstrip("/")] = {
                        "origin": pol_row["origin"],
                        "robots_url": pol_row["robots_url"],
                        "status": pol_row["status"],
                        "crawl_delay_seconds": pol_row["crawl_delay_seconds"],
                        "checked_at": pol_row["checked_at"],
                    }

            items: list[dict[str, Any]] = []
            for row in rows:
                d = dict(row)
                sid = str(d["source_id"])
                origin_val = d.get("origin")
                norm_origin = str(origin_val).rstrip("/") if origin_val else None
                items.append(
                    {
                        "source_id": sid,
                        "display_name": d["display_name"],
                        "source_category": d["source_category"],
                        "adapter_type": d["adapter_type"],
                        "origin": origin_val,
                        "approved_host_paths": json.loads(d.get("approved_host_paths") or "[]"),
                        "robots_url": d.get("robots_url"),
                        "crawl_delay_seconds": float(d["crawl_delay_seconds"]),
                        "min_enforced_delay_seconds": float(d["min_enforced_delay_seconds"]),
                        "max_requests_per_run": int(d["max_requests_per_run"]),
                        "default_license_status": d["default_license_status"],
                        "permits_metadata_indexing": bool(int(d.get("permits_metadata_indexing") or 0)),
                        "permits_full_text_storage": bool(int(d.get("permits_full_text_storage") or 0)),
                        "permits_verbatim_excerpts": bool(int(d.get("permits_verbatim_excerpts") or 0)),
                        "permits_media_rehosting": bool(int(d.get("permits_media_rehosting") or 0)),
                        "legal_uncertainty_status": d["legal_uncertainty_status"],
                        "attribution_template": d.get("attribution_template"),
                        "metadata_complete": bool(int(d.get("metadata_complete") or 0)),
                        "status": d["status"],
                        "notes": d.get("notes"),
                        "resource_count": int(d.get("resource_count") or 0),
                        "transcript_count": transcript_counts.get(sid, 0),
                        "site_policy": site_policies_by_origin.get(norm_origin) if norm_origin else None,
                        "created_at": d["created_at"],
                        "updated_at": d["updated_at"],
                    }
                )

            return APIResponse(
                status=HTTPStatus.OK,
                body={
                    "total_count": len(items),
                    "returned_count": len(items),
                    "filters": {
                        "status": status_filter.strip().upper() if status_filter else None,
                        "source_category": category_filter.strip().upper() if category_filter else None,
                        "adapter_type": adapter_filter.strip().upper() if adapter_filter else None,
                        "only_complete": only_complete,
                    },
                    "items": items,
                },
                headers={},
            )
        finally:
            if should_close:
                conn.close()

    def list_resources_endpoint(self, query_string: str = "") -> APIResponse:
        """`GET /api/v1/resources`: List resources with pagination and basic filters."""
        params = self._extract_single_params(
            query_string,
            allowed_keys={
                "source_id",
                "resource_type",
                "media_modality",
                "lifecycle_state",
                "collection_decision",
                "limit",
                "offset",
            },
        )
        limit, offset = self._parse_pagination(params)

        clauses: list[str] = []
        sql_params: list[Any] = []

        source_id_filter: str | None = None
        if "source_id" in params:
            source_id_filter = self._validate_source_id(params["source_id"], param_name="source_id")
            clauses.append("source_id = ?")
            sql_params.append(source_id_filter)

        resource_type_filter: str | None = None
        if "resource_type" in params:
            resource_type_filter = params["resource_type"].strip()
            if not resource_type_filter:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Resource Type Filter",
                    detail="Parameter 'resource_type' must be a non-empty string.",
                )
            clauses.append("LOWER(resource_type) = LOWER(?)")
            sql_params.append(resource_type_filter)

        modality_filter: str | None = None
        if "media_modality" in params:
            modality_filter = params["media_modality"].strip().upper()
            if modality_filter not in VALID_MEDIA_MODALITIES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Media Modality Filter",
                    detail=(
                        f"Parameter 'media_modality' must be one of {VALID_MEDIA_MODALITIES}, "
                        f"got {params['media_modality']!r}."
                    ),
                )
            clauses.append("media_modality = ?")
            sql_params.append(modality_filter)

        lifecycle_filter: str | None = None
        if "lifecycle_state" in params:
            lifecycle_filter = params["lifecycle_state"].strip().upper()
            if lifecycle_filter not in VALID_RESOURCE_LIFECYCLE_STATES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Lifecycle State Filter",
                    detail=(
                        f"Parameter 'lifecycle_state' must be one of {VALID_RESOURCE_LIFECYCLE_STATES}, "
                        f"got {params['lifecycle_state']!r}."
                    ),
                )
            clauses.append("lifecycle_state = ?")
            sql_params.append(lifecycle_filter)

        decision_filter: str | None = None
        if "collection_decision" in params:
            decision_filter = params["collection_decision"].strip()
            if not decision_filter:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Collection Decision Filter",
                    detail="Parameter 'collection_decision' must be a non-empty string.",
                )
            clauses.append("LOWER(collection_decision) = LOWER(?)")
            sql_params.append(decision_filter)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn, should_close = self._open_readonly_connection()
        try:
            self._require_tables(conn, ("resources",))
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM resources {where_sql}",
                tuple(sql_params),
            ).fetchone()
            total_count = int(total_row[0]) if total_row is not None else 0

            rows = conn.execute(
                f"""
                SELECT *
                  FROM resources
                  {where_sql}
                 ORDER BY COALESCE(manifest_id, resource_id) ASC, resource_id ASC
                 LIMIT ? OFFSET ?
                """,
                tuple([*sql_params, limit, offset]),
            ).fetchall()

            items = [self._format_resource_row(r) for r in rows]
            return APIResponse(
                status=HTTPStatus.OK,
                body={
                    "total_count": total_count,
                    "returned_count": len(items),
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + len(items)) < total_count,
                    "filters": {
                        "source_id": source_id_filter,
                        "resource_type": resource_type_filter,
                        "media_modality": modality_filter,
                        "lifecycle_state": lifecycle_filter,
                        "collection_decision": decision_filter,
                    },
                    "items": items,
                },
                headers={},
            )
        finally:
            if should_close:
                conn.close()

    def get_resource_endpoint(self, raw_resource_id: str, query_string: str = "") -> APIResponse:
        """`GET /api/v1/resources/{resource_id}`: Return one resource's metadata, versions, and transcript summary."""
        self._extract_single_params(query_string, allowed_keys=set())
        resource_id = self._validate_resource_id(raw_resource_id)

        conn, should_close = self._open_readonly_connection()
        try:
            self._require_tables(conn, ("resources", "resource_versions"))
            row = conn.execute(
                "SELECT * FROM resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if row is None:
                raise APIError(
                    status=HTTPStatus.NOT_FOUND,
                    error_code="RESOURCE_NOT_FOUND",
                    title="Resource Not Found",
                    detail=f"Resource {resource_id!r} was not found in the local knowledge store.",
                )

            resource_data = self._format_resource_row(row)
            ver_rows = conn.execute(
                """
                SELECT *
                  FROM resource_versions
                 WHERE resource_id = ?
                 ORDER BY version_number ASC
                """,
                (resource_id,),
            ).fetchall()
            versions = [self._format_resource_version_row(vr) for vr in ver_rows]

            transcripts_summary: list[dict[str, Any]] = []
            if self._table_exists(conn, "transcripts"):
                now_iso = self._now_iso()
                tr_rows = conn.execute(
                    """
                    SELECT *
                      FROM transcripts
                     WHERE resource_id = ?
                     ORDER BY created_at ASC, transcript_id ASC
                    """,
                    (resource_id,),
                ).fetchall()
                for tr_row in tr_rows:
                    projected, _, _ = self._project_transcript_metadata_readonly(
                        conn, tr_row, now_iso=now_iso
                    )
                    transcripts_summary.append(projected)

            resource_data["versions"] = versions
            resource_data["transcripts_summary"] = transcripts_summary
            return APIResponse(
                status=HTTPStatus.OK,
                body={"resource": resource_data},
                headers={},
            )
        finally:
            if should_close:
                conn.close()

    def search_resources_endpoint(self, query_string: str = "") -> APIResponse:
        """`GET /api/v1/search?q=...`: Deterministic parameterized search over stored resource metadata."""
        params = self._extract_single_params(
            query_string,
            allowed_keys={
                "q",
                "source_id",
                "resource_type",
                "media_modality",
                "lifecycle_state",
                "limit",
                "offset",
            },
        )
        raw_q = params.get("q")
        if raw_q is None or not raw_q.strip():
            raise APIError(
                status=HTTPStatus.BAD_REQUEST,
                error_code="MISSING_SEARCH_QUERY",
                title="Missing Search Query",
                detail="Query parameter 'q' is required and must be a non-empty string.",
            )
        normalized_q = " ".join(raw_q.strip().split())
        if len(normalized_q) > MAX_SEARCH_QUERY_LENGTH:
            raise APIError(
                status=HTTPStatus.BAD_REQUEST,
                error_code="INVALID_SEARCH_QUERY",
                title="Search Query Too Long",
                detail=f"Query parameter 'q' must not exceed {MAX_SEARCH_QUERY_LENGTH} characters.",
            )

        limit, offset = self._parse_pagination(params)

        filter_clauses: list[str] = []
        filter_params: list[Any] = []

        source_id_filter: str | None = None
        if "source_id" in params:
            source_id_filter = self._validate_source_id(params["source_id"], param_name="source_id")
            filter_clauses.append("source_id = ?")
            filter_params.append(source_id_filter)

        resource_type_filter: str | None = None
        if "resource_type" in params:
            resource_type_filter = params["resource_type"].strip()
            if not resource_type_filter:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Resource Type Filter",
                    detail="Parameter 'resource_type' must be a non-empty string.",
                )
            filter_clauses.append("LOWER(resource_type) = LOWER(?)")
            filter_params.append(resource_type_filter)

        modality_filter: str | None = None
        if "media_modality" in params:
            modality_filter = params["media_modality"].strip().upper()
            if modality_filter not in VALID_MEDIA_MODALITIES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Media Modality Filter",
                    detail=(
                        f"Parameter 'media_modality' must be one of {VALID_MEDIA_MODALITIES}, "
                        f"got {params['media_modality']!r}."
                    ),
                )
            filter_clauses.append("media_modality = ?")
            filter_params.append(modality_filter)

        lifecycle_filter: str | None = None
        if "lifecycle_state" in params:
            lifecycle_filter = params["lifecycle_state"].strip().upper()
            if lifecycle_filter not in VALID_RESOURCE_LIFECYCLE_STATES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Lifecycle State Filter",
                    detail=(
                        f"Parameter 'lifecycle_state' must be one of {VALID_RESOURCE_LIFECYCLE_STATES}, "
                        f"got {params['lifecycle_state']!r}."
                    ),
                )
            filter_clauses.append("lifecycle_state = ?")
            filter_params.append(lifecycle_filter)

        # Tokenize query into distinct case-folded terms (max 10 terms) so multi-word queries match
        # deterministically across searchable metadata columns (`AND` across terms, `OR` across columns).
        raw_terms = [t for t in normalized_q.lower().split(" ") if t][:10]
        searchable_columns = (
            "title",
            "abstract_or_description",
            "authors",
            "topic_tags",
            "external_identifier",
            "canonical_url",
            "manifest_id",
            "domain_scope",
        )

        term_clauses: list[str] = []
        term_params: list[Any] = []
        for term in raw_terms:
            like_pattern = f"%{_escape_like_term(term)}%"
            col_disjunction = " OR ".join(
                f"LOWER(COALESCE({col}, '')) LIKE ? ESCAPE '\\'" for col in searchable_columns
            )
            term_clauses.append(f"({col_disjunction})")
            term_params.extend([like_pattern] * len(searchable_columns))

        all_where_clauses = [*filter_clauses, *term_clauses]
        where_sql = f"WHERE {' AND '.join(all_where_clauses)}"

        phrase_pattern = f"%{_escape_like_term(normalized_q.lower())}%"
        score_sql = """
            (
                CASE WHEN LOWER(COALESCE(title, '')) = LOWER(?) THEN 100 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(external_identifier, '')) = LOWER(?) THEN 90 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(manifest_id, '')) = LOWER(?) THEN 90 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(title, '')) LIKE ? ESCAPE '\\' THEN 50 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(topic_tags, '')) LIKE ? ESCAPE '\\' THEN 35 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(authors, '')) LIKE ? ESCAPE '\\' THEN 30 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(abstract_or_description, '')) LIKE ? ESCAPE '\\' THEN 20 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(domain_scope, '')) LIKE ? ESCAPE '\\' THEN 15 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(canonical_url, '')) LIKE ? ESCAPE '\\' THEN 10 ELSE 0 END +
                1
            )
        """
        score_params: list[Any] = [
            normalized_q,
            normalized_q,
            normalized_q,
            phrase_pattern,
            phrase_pattern,
            phrase_pattern,
            phrase_pattern,
            phrase_pattern,
            phrase_pattern,
        ]

        conn, should_close = self._open_readonly_connection()
        try:
            self._require_tables(conn, ("resources",))
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM resources {where_sql}",
                tuple([*filter_params, *term_params]),
            ).fetchone()
            total_count = int(total_row[0]) if total_row is not None else 0

            rows = conn.execute(
                f"""
                SELECT *, {score_sql} AS relevance_score
                  FROM resources
                  {where_sql}
                 ORDER BY relevance_score DESC,
                          COALESCE(manifest_id, resource_id) ASC,
                          resource_id ASC
                 LIMIT ? OFFSET ?
                """,
                tuple([*score_params, *filter_params, *term_params, limit, offset]),
            ).fetchall()

            items: list[dict[str, Any]] = []
            for row in rows:
                formatted = self._format_resource_row(row)
                matched_fields: list[str] = []
                for col in searchable_columns:
                    col_val = str(row[col] or "").lower()
                    if col_val and any(term in col_val for term in raw_terms):
                        matched_fields.append(col)
                formatted["matched_fields"] = matched_fields
                formatted["relevance_score"] = int(row["relevance_score"])
                items.append(formatted)

            return APIResponse(
                status=HTTPStatus.OK,
                body={
                    "query": normalized_q,
                    "search_mode": "metadata_lexical",
                    "total_count": total_count,
                    "returned_count": len(items),
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + len(items)) < total_count,
                    "filters": {
                        "source_id": source_id_filter,
                        "resource_type": resource_type_filter,
                        "media_modality": modality_filter,
                        "lifecycle_state": lifecycle_filter,
                    },
                    "items": items,
                },
                headers={},
            )
        finally:
            if should_close:
                conn.close()

    def list_transcripts_endpoint(self, query_string: str = "") -> APIResponse:
        """`GET /api/v1/transcripts`: List transcript registry metadata with pagination and optional filters."""
        params = self._extract_single_params(
            query_string,
            allowed_keys={
                "resource_id",
                "source_id",
                "language_bcp47",
                "track_kind",
                "ingestion_state",
                "license_status",
                "transcript_storage_status",
                "limit",
                "offset",
            },
        )
        limit, offset = self._parse_pagination(params)

        resource_id_filter: str | None = None
        if "resource_id" in params:
            resource_id_filter = self._validate_resource_id(params["resource_id"], param_name="resource_id")

        source_id_filter: str | None = None
        if "source_id" in params:
            source_id_filter = self._validate_source_id(params["source_id"], param_name="source_id")

        language_filter: str | None = None
        if "language_bcp47" in params:
            try:
                language_filter = validate_bcp47_language_tag(params["language_bcp47"])
            except ValueError as exc:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid BCP-47 Language Tag",
                    detail=str(exc),
                ) from exc

        track_kind_filter: str | None = None
        if "track_kind" in params:
            track_kind_filter = params["track_kind"].strip().upper()
            if track_kind_filter not in VALID_TRANSCRIPT_TRACK_KINDS:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Track Kind Filter",
                    detail=(
                        f"Parameter 'track_kind' must be one of {VALID_TRANSCRIPT_TRACK_KINDS}, "
                        f"got {params['track_kind']!r}."
                    ),
                )

        state_filter: str | None = None
        if "ingestion_state" in params:
            state_filter = params["ingestion_state"].strip().upper()
            if state_filter not in VALID_TRANSCRIPT_INGESTION_STATES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Ingestion State Filter",
                    detail=(
                        f"Parameter 'ingestion_state' must be one of {VALID_TRANSCRIPT_INGESTION_STATES}, "
                        f"got {params['ingestion_state']!r}."
                    ),
                )

        license_filter: str | None = None
        if "license_status" in params:
            license_filter = params["license_status"].strip().upper()
            if license_filter not in VALID_TRANSCRIPT_LICENSE_STATUSES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid License Status Filter",
                    detail=(
                        f"Parameter 'license_status' must be one of {VALID_TRANSCRIPT_LICENSE_STATUSES}, "
                        f"got {params['license_status']!r}."
                    ),
                )

        storage_status_filter: str | None = None
        if "transcript_storage_status" in params:
            storage_status_filter = params["transcript_storage_status"].strip().upper()
            if storage_status_filter not in VALID_TRANSCRIPT_STORAGE_STATUSES:
                raise APIError(
                    status=HTTPStatus.BAD_REQUEST,
                    error_code="INVALID_QUERY_PARAMETER",
                    title="Invalid Transcript Storage Status Filter",
                    detail=(
                        f"Parameter 'transcript_storage_status' must be one of {VALID_TRANSCRIPT_STORAGE_STATUSES}, "
                        f"got {params['transcript_storage_status']!r}."
                    ),
                )

        clauses: list[str] = []
        sql_params: list[Any] = []
        if resource_id_filter is not None:
            clauses.append("resource_id = ?")
            sql_params.append(resource_id_filter)
        if source_id_filter is not None:
            clauses.append("source_id = ?")
            sql_params.append(source_id_filter)
        if language_filter is not None:
            clauses.append("language_bcp47 = ?")
            sql_params.append(language_filter)
        if track_kind_filter is not None:
            clauses.append("track_kind = ?")
            sql_params.append(track_kind_filter)
        if license_filter is not None:
            clauses.append("license_status = ?")
            sql_params.append(license_filter)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn, should_close = self._open_readonly_connection()
        try:
            self._require_tables(conn, ("transcripts",))
            now_iso = self._now_iso()
            rows = conn.execute(
                f"""
                SELECT {", ".join(TRANSCRIPT_METADATA_COLUMNS)}
                  FROM transcripts
                  {where_sql}
                 ORDER BY created_at ASC, transcript_id ASC
                """,
                tuple(sql_params),
            ).fetchall()

            projected_items: list[dict[str, Any]] = []
            for row in rows:
                projected, _, _ = self._project_transcript_metadata_readonly(
                    conn, row, now_iso=now_iso
                )
                if state_filter is not None and projected["ingestion_state"] != state_filter:
                    continue
                if (
                    storage_status_filter is not None
                    and projected["transcript_storage_status"] != storage_status_filter
                ):
                    continue
                projected_items.append(projected)

            total_count = len(projected_items)
            paged_items = projected_items[offset : offset + limit]

            return APIResponse(
                status=HTTPStatus.OK,
                body={
                    "total_count": total_count,
                    "returned_count": len(paged_items),
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + len(paged_items)) < total_count,
                    "content_disclosure_policy": "METADATA_ONLY_NO_SEGMENTS_OR_SYNTHESES",
                    "filters": {
                        "resource_id": resource_id_filter,
                        "source_id": source_id_filter,
                        "language_bcp47": language_filter,
                        "track_kind": track_kind_filter,
                        "ingestion_state": state_filter,
                        "license_status": license_filter,
                        "transcript_storage_status": storage_status_filter,
                    },
                    "items": paged_items,
                },
                headers={},
            )
        finally:
            if should_close:
                conn.close()

    def get_transcript_endpoint(self, raw_transcript_id: str, query_string: str = "") -> APIResponse:
        """`GET /api/v1/transcripts/{transcript_id}`: Return metadata and version-status info for one transcript."""
        self._extract_single_params(query_string, allowed_keys=set())
        transcript_id = self._validate_transcript_id(raw_transcript_id)

        conn, should_close = self._open_readonly_connection()
        try:
            self._require_tables(conn, ("transcripts", "transcript_versions"))
            row = conn.execute(
                f"""
                SELECT {", ".join(TRANSCRIPT_METADATA_COLUMNS)}
                  FROM transcripts
                 WHERE transcript_id = ?
                """,
                (transcript_id,),
            ).fetchone()
            if row is None:
                raise APIError(
                    status=HTTPStatus.NOT_FOUND,
                    error_code="TRANSCRIPT_NOT_FOUND",
                    title="Transcript Not Found",
                    detail=f"Transcript {transcript_id!r} was not found in the local knowledge store.",
                )

            now_iso = self._now_iso()
            projected, att_summary, revoked_or_expired = self._project_transcript_metadata_readonly(
                conn, row, now_iso=now_iso
            )

            ver_rows = conn.execute(
                f"""
                SELECT {", ".join(TRANSCRIPT_VERSION_METADATA_COLUMNS)}
                  FROM transcript_versions
                 WHERE transcript_id = ?
                 ORDER BY version_number ASC
                """,
                (transcript_id,),
            ).fetchall()

            versions: list[dict[str, Any]] = []
            for vr in ver_rows:
                vd = {col: vr[col] for col in TRANSCRIPT_VERSION_METADATA_COLUMNS}
                vd["version_number"] = int(vd["version_number"])
                vd["segment_count"] = int(vd["segment_count"])
                vd["word_count"] = int(vd["word_count"])
                vd["char_count"] = int(vd["char_count"])
                vd["low_confidence_segment_count"] = int(vd["low_confidence_segment_count"])
                if revoked_or_expired and vd["status"] not in ("TOMBSTONED", "PURGED"):
                    vd["status"] = "TOMBSTONED"
                versions.append(vd)

            projected["authorization_attestation"] = att_summary
            projected["versions"] = versions

            return APIResponse(
                status=HTTPStatus.OK,
                body={
                    "content_disclosure_policy": "METADATA_ONLY_NO_SEGMENTS_OR_SYNTHESES",
                    "transcript": projected,
                },
                headers={},
            )
        finally:
            if should_close:
                conn.close()

    # ------------------------------------------------------------------
    # Request Dispatcher (HTTP & in-process testing)
    # ------------------------------------------------------------------

    def handle_request(self, method: str, raw_target: str) -> APIResponse:
        """Dispatch an HTTP request (`method`, `raw_target`) and return an `APIResponse`.

        Guarantees consistent JSON responses, RFC 9457 error envelopes on failure,
        and strictly read-only `GET` handling.
        """
        parsed = urlsplit(raw_target or "/")
        path = parsed.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query_string = parsed.query or ""

        try:
            upper_method = (method or "").strip().upper()
            if upper_method != "GET":
                raise APIError(
                    status=HTTPStatus.METHOD_NOT_ALLOWED,
                    error_code="METHOD_NOT_ALLOWED",
                    title="Method Not Allowed",
                    detail=f"Method {upper_method!r} is not allowed on this read-only API; only 'GET' is supported.",
                    headers={"Allow": "GET"},
                )

            if path == "/healthz":
                self._extract_single_params(query_string, allowed_keys=set())
                return self.get_healthz(instance=path)

            if path == "/api/v1/sources":
                return self.list_sources_endpoint(query_string)

            if path == "/api/v1/resources":
                return self.list_resources_endpoint(query_string)

            if path.startswith("/api/v1/resources/"):
                remainder = unquote(path[len("/api/v1/resources/") :])
                if "/" in remainder or not remainder:
                    raise APIError(
                        status=HTTPStatus.NOT_FOUND,
                        error_code="ENDPOINT_NOT_FOUND",
                        title="Endpoint Not Found",
                        detail=f"No read-only API route matches {path!r}.",
                    )
                return self.get_resource_endpoint(remainder, query_string)

            if path == "/api/v1/search":
                return self.search_resources_endpoint(query_string)

            if path == "/api/v1/transcripts":
                return self.list_transcripts_endpoint(query_string)

            if path.startswith("/api/v1/transcripts/"):
                remainder = unquote(path[len("/api/v1/transcripts/") :])
                if "/" in remainder or not remainder:
                    raise APIError(
                        status=HTTPStatus.NOT_FOUND,
                        error_code="ENDPOINT_NOT_FOUND",
                        title="Endpoint Not Found",
                        detail=f"No read-only API route matches {path!r}.",
                    )
                return self.get_transcript_endpoint(remainder, query_string)

            raise APIError(
                status=HTTPStatus.NOT_FOUND,
                error_code="ENDPOINT_NOT_FOUND",
                title="Endpoint Not Found",
                detail=f"No read-only API route matches {path!r}.",
            )

        except APIError as exc:
            return APIResponse(
                status=exc.status,
                body=exc.to_problem_dict(instance=path),
                headers=exc.headers,
            )
        except sqlite3.OperationalError as exc:
            err_problem = APIError(
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                error_code="DATABASE_OPERATIONAL_ERROR",
                title="Database Operational Error",
                detail=f"Read-only SQLite query failed: {exc}",
            )
            return APIResponse(
                status=err_problem.status,
                body=err_problem.to_problem_dict(instance=path),
                headers={},
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            err_problem = APIError(
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                error_code="INTERNAL_SERVER_ERROR",
                title="Internal Server Error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            return APIResponse(
                status=err_problem.status,
                body=err_problem.to_problem_dict(instance=path),
                headers={},
            )


def _make_http_handler(api: LocalResearchLibraryAPI) -> type[BaseHTTPRequestHandler]:
    """Create a `BaseHTTPRequestHandler` class bound to `api`."""

    class _LibraryAPIRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            response = api.handle_request(method, self.path)
            payload = response.to_json_bytes()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for header_name, header_val in response.headers.items():
                self.send_header(header_name, header_val)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("HEAD")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch("OPTIONS")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress noisy default stderr logging during automated tests unless requested.
            return

    return _LibraryAPIRequestHandler


def create_http_server(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
    connection: sqlite3.Connection | None = None,
    clock: Callable[[], str] | None = None,
) -> tuple[ThreadingHTTPServer, LocalResearchLibraryAPI]:
    """Create a `ThreadingHTTPServer` and `LocalResearchLibraryAPI` bound to `127.0.0.1` by default."""
    api = LocalResearchLibraryAPI(db_path=db_path, connection=connection, clock=clock)
    handler_cls = _make_http_handler(api)
    server = ThreadingHTTPServer((host, int(port)), handler_cls)
    return server, api


def main(argv: list[str] | None = None) -> int:
    """Run the local read-only Research Library HTTP API server."""
    parser = argparse.ArgumentParser(
        description="Run the local read-only AI Research & Learning Library HTTP API (binds to 127.0.0.1 by default)."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to local SQLite knowledge store (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_API_HOST,
        help=f"Bind address (default: {DEFAULT_API_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"Bind port (default: {DEFAULT_API_PORT})",
    )
    args = parser.parse_args(argv)

    server, _ = create_http_server(db_path=args.db, host=args.host, port=args.port)
    bound_host, bound_port = server.server_address[:2]
    print(
        f"Local Research Library API (read-only) listening on http://{bound_host}:{bound_port} "
        f"(db={args.db})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Local Research Library API server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
