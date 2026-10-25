#!/usr/bin/env python3
"""Local SQLite persistence layer for the AI Research & Learning Explorer.

Implements Phase 1 of the canonical knowledge data model defined in:
- docs/architecture/data-model.md
- docs/architecture/source-adapter-contract.md
- docs/architecture/implementation-roadmap.md
- docs/architecture/decisions/0002-knowledge-data-model.md

Scope of this module:
- Canonical `sources`, `source_urls`, `resources`, `resource_versions`,
  `provenance_events`, `collection_runs`, and `schema_migrations` tables.
- Backward-compatible preservation and non-destructive migration of existing
  collector tables (`source_urls`, `documents`, `crawl_runs`,
  `discovered_links`, `site_policies`, `fetch_events`, `crawl_audit`).
- Idempotent seed manifest (`seed_urls.json`) import with zero network I/O,
  strict URL/source validation, duplicate detection, immutable resource
  version history on content/metadata hash changes, and provenance logging.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import html
import io
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Iterator
import unicodedata
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
DEFAULT_SEED_PATH = ROOT / "seed_urls.json"
DEFAULT_DB_PATH = ROOT / "data" / "runtime" / "inventory.sqlite"

LATEST_SCHEMA_VERSION = 8
TRANSCRIPT_NORMALIZER_VERSION = "norm-v1.0"

VALID_CANDIDATE_REVIEW_STATUSES: tuple[str, ...] = (
    "PENDING_REVIEW",
    "ACCEPTED_FOR_REFERENCE",
    "APPROVED_FOR_MANIFEST",
    "REJECTED",
    "DEFERRED",
)
VALID_DOMAIN_CLASSIFICATIONS: tuple[str, ...] = ("INTERNAL", "EXTERNAL")

VALID_TRANSCRIPT_TRACK_KINDS: tuple[str, ...] = (
    "PRIMARY",
    "TRANSLATION",
    "DESCRIPTIVE_AUDIO_TEXT",
)
VALID_TRANSCRIPT_ORIGIN_TYPES: tuple[str, ...] = (
    "AUTHOR_PROVIDED",
    "PUBLISHER_CLOSED_CAPTION",
    "COMMUNITY_CONTRIBUTED",
    "LOCAL_ASR_GENERATED",
    "MANUAL_CURATED_IMPORT",
)
VALID_TRANSCRIPT_ACQUISITION_METHODS: tuple[str, ...] = (
    "FIRST_PARTY_CURATED_IMPORT",
    "OPEN_LICENSED_DIRECT_CAPTION",
    "WIKIMEDIA_TIMEDTEXT_API",
    "OFFICIAL_PARTNER_CAPTION_API",
    "AUTHORIZED_LOCAL_ASR",
    "METADATA_AND_LINK_ONLY",
)
VALID_CAPTION_FORMATS: tuple[str, ...] = (
    "WEBVTT",
    "SRT",
    "PLAIN_TEXT",
    "TTML",
    "HTML_INLINE",
    "MEDIAWIKI_TIMEDTEXT",
    "ASR_JSON",
    "NONE",
)
VALID_SPEAKER_ATTRIBUTION_QUALITIES: tuple[str, ...] = (
    "VERIFIED_SINGLE_SPEAKER",
    "VERIFIED_MULTI_SPEAKER",
    "DIARIZED_UNVERIFIED",
    "UNATTRIBUTED",
    "UNKNOWN",
)
VALID_TRANSCRIPT_LICENSE_STATUSES: tuple[str, ...] = (
    "VERIFIED_PERMISSIVE",
    "ALL_RIGHTS_RESERVED",
    "UNKNOWN",
    "RESTRICTED_PROPRIETARY",
)
VALID_AUTHORIZATION_BASES: tuple[str, ...] = (
    "OPEN_LICENSE_VERIFIED",
    "PUBLIC_DOMAIN_VERIFIED",
    "FIRST_PARTY_OWNED",
    "FIRST_PARTY_OWNED_MEDIA",
    "WRITTEN_PERMISSION",
    "WRITTEN_RIGHTS_HOLDER_PERMISSION",
    "NONE",
)
AUTHORIZED_STORAGE_BASES: frozenset[str] = frozenset(
    {
        "OPEN_LICENSE_VERIFIED",
        "PUBLIC_DOMAIN_VERIFIED",
        "FIRST_PARTY_OWNED",
        "FIRST_PARTY_OWNED_MEDIA",
        "WRITTEN_PERMISSION",
        "WRITTEN_RIGHTS_HOLDER_PERMISSION",
    }
)
VALID_ATTESTATION_EVIDENCE_TYPES: tuple[str, ...] = (
    "SPDX_LICENSE_URL",
    "OFFICIAL_RIGHTS_STATEMENT",
    "WRITTEN_PERMISSION_REF",
    "FIRST_PARTY_POLICY_REF",
    "PUBLIC_DOMAIN_DEDICATION",
    "OTHER_VERIFIABLE_EVIDENCE",
    "CREATOR_LICENSE_PAGE",
    "WRITTEN_PERMISSION_RECORD",
    "PUBLIC_DOMAIN_DECLARATION",
    "FIRST_PARTY_OWNERSHIP_RECORD",
)
VALID_ATTESTATION_VERIFICATION_STATUSES: tuple[str, ...] = (
    "VERIFIED",
    "PENDING_REVIEW",
    "CONTRADICTORY",
    "REJECTED",
    "WITHDRAWN",
    "EXPIRED",
    "UNKNOWN",
)
PERMISSIVE_SPDX_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-3.0",
        "CC-BY-2.0",
        "CC-BY-SA-4.0",
        "CC-BY-SA-3.0",
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "PUBLIC-DOMAIN",
    }
)
VALID_TRANSCRIPT_INGESTION_STATES: tuple[str, ...] = (
    "DISCOVERED",
    "RIGHTS_PENDING",
    "BLOCKED_BY_POLICY",
    "METADATA_ONLY_LINKED",
    "READY_FOR_INGESTION",
    "INGESTED",
    "NEEDS_REVIEW",
    "SUPERSEDED",
    "WITHDRAWN_OR_TOMBSTONED",
)
VALID_TRANSCRIPT_STORAGE_STATUSES: tuple[str, ...] = (
    "STORED_TIMED_SEGMENTS",
    "STORED_PLAIN_TEXT",
    "METADATA_AND_LINK_ONLY",
    "BLOCKED_NO_AUTHORIZATION",
    "TOMBSTONED_ON_TAKEDOWN",
    "NOT_STORED",
)
VALID_TRANSCRIPT_VERSION_STATUSES: tuple[str, ...] = (
    "ACTIVE",
    "SUPERSEDED",
    "TOMBSTONED",
)
VALID_TRANSCRIPT_SYNTHESIS_TYPES: tuple[str, ...] = (
    "STRUCTURED_SUMMARY",
    "SECTION_OUTLINE",
    "GLOSSARY_TERMS",
    "LEARNING_OBJECTIVES",
    "PREREQUISITES",
)
VALID_TRANSCRIPT_SYNTHESIS_GENERATORS: tuple[str, ...] = (
    "HUMAN_CURATED",
    "RULE_EXTRACTOR",
    "LLM_ASSISTED_CURATED",
)
VALID_TRANSCRIPT_SYNTHESIS_REVIEW_STATUSES: tuple[str, ...] = (
    "PENDING_REVIEW",
    "APPROVED",
    "REJECTED",
)
VALID_SEGMENT_LINK_TARGET_TYPES: tuple[str, ...] = (
    "CONCEPT",
    "PAPER_RESOURCE",
    "LEARNING_PATH_MODULE",
    "EQUATION",
    "KNOWLEDGE_CHUNK",
)
VALID_SEGMENT_LINK_RELATIONS: tuple[str, ...] = (
    "EXPLAINS",
    "INTRODUCES",
    "DERIVES",
    "CITES_PAPER",
    "PREREQUISITE_FOR",
    "EXAMPLE_OF",
)

VALID_TRANSCRIPT_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "DISCOVERED": frozenset({"RIGHTS_PENDING", "WITHDRAWN_OR_TOMBSTONED"}),
    "RIGHTS_PENDING": frozenset(
        {
            "BLOCKED_BY_POLICY",
            "METADATA_ONLY_LINKED",
            "READY_FOR_INGESTION",
            "WITHDRAWN_OR_TOMBSTONED",
        }
    ),
    "BLOCKED_BY_POLICY": frozenset({"RIGHTS_PENDING", "WITHDRAWN_OR_TOMBSTONED"}),
    "METADATA_ONLY_LINKED": frozenset({"RIGHTS_PENDING", "WITHDRAWN_OR_TOMBSTONED"}),
    "READY_FOR_INGESTION": frozenset(
        {"INGESTED", "NEEDS_REVIEW", "BLOCKED_BY_POLICY", "WITHDRAWN_OR_TOMBSTONED"}
    ),
    "INGESTED": frozenset(
        {"READY_FOR_INGESTION", "NEEDS_REVIEW", "SUPERSEDED", "WITHDRAWN_OR_TOMBSTONED"}
    ),
    "NEEDS_REVIEW": frozenset(
        {"INGESTED", "BLOCKED_BY_POLICY", "READY_FOR_INGESTION", "WITHDRAWN_OR_TOMBSTONED"}
    ),
    "SUPERSEDED": frozenset({"WITHDRAWN_OR_TOMBSTONED"}),
    "WITHDRAWN_OR_TOMBSTONED": frozenset(),
}

_BCP47_TAG_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")
_RESIDUAL_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_INAUDIBLE_MARKER_RE = re.compile(
    r"\[(?:inaudible|unintelligible|unclear)\]", re.IGNORECASE
)
_VTT_VOICE_TAG_RE = re.compile(
    r"^\s*<v(?:\.[^>\s]+)?\s+([^>]+)>(.*?)(?:</v>)?\s*$", re.DOTALL | re.IGNORECASE
)
_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:\[([A-Za-z0-9 .,'_-]{2,60})\]|([A-Z][A-Za-z0-9 .,'_-]{1,50})):\s+(.+)$",
    re.DOTALL,
)

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HOST_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")
_PROHIBITED_EXACT_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)
_PROHIBITED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".localdomain",
)


def utc_now() -> str:
    """Return current UTC timestamp in ISO-8601 format (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_hex(value: str) -> str:
    """Return full 64-character lowercase SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_and_canonicalize_url(url: Any) -> str:
    """Validate an HTTP/HTTPS URL and return its deterministic canonical form.

    Rejects unsafe schemes, embedded credentials, malformed URLs/hosts, and
    prohibited destinations (loopback, private networks, link-local / cloud
    metadata endpoints, reserved/multicast/unspecified IPs, and internal TLDs).
    Raises ValueError if the input is not a valid public HTTP(S) URL.
    """
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("URL cannot be empty")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(f"URL contains whitespace or control characters: {url!r}")

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"URL must use http or https scheme, got {parsed.scheme!r}: {url!r}")
    if not parsed.netloc:
        raise ValueError(f"URL is missing network host (netloc): {url!r}")
    if "\\" in parsed.netloc or "\\" in parsed.scheme:
        raise ValueError(f"URL contains malformed backslash in authority: {url!r}")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError(f"URL must not contain embedded credentials: {url!r}")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"URL has invalid port: {url!r}") from exc

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise ValueError(f"URL has invalid hostname {hostname!r}: {url!r}")

    if hostname in _PROHIBITED_EXACT_HOSTS or hostname.endswith(_PROHIBITED_HOST_SUFFIXES):
        raise ValueError(f"URL points to prohibited destination host {hostname!r}: {url!r}")

    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        mapped_v4 = getattr(ip_obj, "ipv4_mapped", None)
        for addr in (ip_obj, mapped_v4) if mapped_v4 is not None else (ip_obj,):
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_multicast
                or addr.is_reserved
                or addr.is_unspecified
            ):
                raise ValueError(
                    f"URL points to prohibited destination IP address {hostname!r}: {url!r}"
                )
        host_for_netloc = f"[{hostname}]" if isinstance(ip_obj, ipaddress.IPv6Address) else hostname
    else:
        if "." not in hostname:
            raise ValueError(f"URL has invalid hostname {hostname!r}: {url!r}")
        labels = hostname.split(".")
        if any(not _HOST_LABEL_RE.match(lbl) for lbl in labels) or not _HOST_TLD_RE.match(labels[-1]):
            raise ValueError(f"URL has invalid or prohibited hostname {hostname!r}: {url!r}")
        host_for_netloc = hostname

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) or port is None:
        netloc = host_for_netloc
    else:
        netloc = f"{host_for_netloc}:{port}"

    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def compute_resource_id(source_id: str, canonical_url: str) -> str:
    """Compute deterministic canonical resource ID: `res_<sha256_16>`."""
    if not source_id or not isinstance(source_id, str):
        raise ValueError("source_id must be a non-empty string")
    digest = sha256_hex(f"{source_id.strip()}:{canonical_url.strip()}")[:16]
    return f"res_{digest}"


def compute_candidate_link_id(referring_resource_id: str, normalized_url: str) -> str:
    """Compute deterministic discovered link candidate ID: `cand_<sha256_16>`."""
    if not referring_resource_id or not isinstance(referring_resource_id, str):
        raise ValueError("referring_resource_id must be a non-empty string")
    if not normalized_url or not isinstance(normalized_url, str):
        raise ValueError("normalized_url must be a non-empty string")
    digest = sha256_hex(f"{referring_resource_id.strip()}:{normalized_url.strip()}")[:16]
    return f"cand_{digest}"


def compute_candidate_review_id(
    candidate_id: str,
    review_number: int,
    new_review_status: str,
    reviewer: str,
    reason: str,
    reviewed_at: str,
) -> str:
    """Compute deterministic candidate review audit ID: `rev_<yyyymmdd>_<sha256_10>_r<n>`."""
    date_part = (reviewed_at or utc_now())[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", date_part):
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    digest = sha256_hex(
        f"{candidate_id}:{review_number}:{new_review_status}:{reviewer}:{reason}:{reviewed_at}"
    )[:10]
    return f"rev_{date_part}_{digest}_r{review_number}"


def compute_version_id(
    resource_id: str,
    version_number: int,
    metadata_hash: str,
    content_hash: str | None = None,
    recorded_date: str | None = None,
) -> str:
    """Compute deterministic version identifier: `ver_<yyyymmdd>_<hash8>_v<n>`."""
    date_part = (recorded_date or utc_now())[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", date_part):
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    combined_hash = sha256_hex(f"{resource_id}:{metadata_hash}:{content_hash or ''}")[:8]
    return f"ver_{date_part}_{combined_hash}_v{version_number}"


def validate_bcp47_language_tag(language_bcp47: str) -> str:
    """Validate a BCP-47 language tag (e.g. 'en', 'en-US', 'fr', 'zh-Hant') and return normalized form."""
    if not isinstance(language_bcp47, str) or not language_bcp47.strip():
        raise ValueError("language_bcp47 must be a non-empty string")
    cleaned = language_bcp47.strip()
    if not _BCP47_TAG_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid BCP-47 language tag: {language_bcp47!r}")
    parts = cleaned.split("-")
    normalized_parts = [parts[0].lower()]
    for sub in parts[1:]:
        if len(sub) == 2 and sub.isalpha():
            normalized_parts.append(sub.upper())
        elif len(sub) == 4 and sub.isalpha():
            normalized_parts.append(sub.capitalize())
        else:
            normalized_parts.append(sub.lower())
    return "-".join(normalized_parts)


def compute_transcript_id(resource_id: str, language_bcp47: str, track_kind: str = "PRIMARY") -> str:
    """Compute deterministic transcript ID: `tr_<sha256_16>` over `(resource_id, language_bcp47, track_kind)`."""
    if not resource_id or not isinstance(resource_id, str):
        raise ValueError("resource_id must be a non-empty string")
    lang = validate_bcp47_language_tag(language_bcp47)
    kind = (track_kind or "").strip().upper()
    if kind not in VALID_TRANSCRIPT_TRACK_KINDS:
        raise ValueError(f"Invalid track_kind {track_kind!r}; expected one of {VALID_TRANSCRIPT_TRACK_KINDS}")
    digest = sha256_hex(f"{resource_id.strip()}:{lang}:{kind}")[:16]
    return f"tr_{digest}"


def compute_transcript_version_id(
    transcript_id: str,
    version_number: int,
    raw_content_sha256: str,
    normalized_text_sha256: str | None = None,
    recorded_date: str | None = None,
) -> str:
    """Compute deterministic transcript version ID: `tver_<yyyymmdd>_<hash8>_v<n>`."""
    if not transcript_id or not isinstance(transcript_id, str):
        raise ValueError("transcript_id must be a non-empty string")
    if int(version_number) < 1:
        raise ValueError("version_number must be >= 1")
    date_part = (recorded_date or utc_now())[:10].replace("-", "")
    if not re.fullmatch(r"\d{8}", date_part):
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    combined_hash = sha256_hex(
        f"{transcript_id.strip()}:{raw_content_sha256.strip()}:{(normalized_text_sha256 or '').strip()}"
    )[:8]
    return f"tver_{date_part}_{combined_hash}_v{int(version_number)}"


def compute_transcript_segment_id(
    transcript_id: str,
    sequence_index: int,
    version_number: int = 1,
) -> str:
    """Compute deterministic segment ID (`seg_<transcript_id>_<sequence_index:05d>` for v1, version-scoped for v2+)."""
    if not transcript_id or not isinstance(transcript_id, str):
        raise ValueError("transcript_id must be a non-empty string")
    if int(sequence_index) < 0:
        raise ValueError("sequence_index must be >= 0")
    if int(version_number) < 1:
        raise ValueError("version_number must be >= 1")
    tid = transcript_id.strip()
    seq = int(sequence_index)
    ver = int(version_number)
    if ver == 1:
        return f"seg_{tid}_{seq:05d}"
    return f"seg_{tid}_v{ver}_{seq:05d}"


def compute_transcript_synthesis_id(
    transcript_id: str,
    transcript_version_id: str | None,
    synthesis_type: str,
    content_markdown: str,
) -> str:
    """Compute deterministic synthesis ID: `tsyn_<sha256_16>`."""
    digest = sha256_hex(
        f"{transcript_id.strip()}:{(transcript_version_id or '').strip()}:{synthesis_type.strip()}:{content_markdown.strip()}"
    )[:16]
    return f"tsyn_{digest}"


def compute_transcript_segment_link_id(
    segment_id: str,
    target_type: str,
    target_id: str,
    relation_type: str,
) -> str:
    """Compute deterministic segment link ID: `tslnk_<sha256_16>`."""
    digest = sha256_hex(
        f"{segment_id.strip()}:{target_type.strip()}:{target_id.strip()}:{relation_type.strip()}"
    )[:16]
    return f"tslnk_{digest}"


class TranscriptPolicyViolationError(PermissionError, ValueError):
    """Raised when an operation attempts to store, quote, synthesize, or redistribute transcript content beyond attested authorization scope."""


def compute_transcript_attestation_id(
    resource_id: str,
    authorization_basis: str,
    evidence_type: str,
    evidence_reference: str,
    reviewed_by: str | None = None,
    *,
    transcript_id: str | None = None,
    language_bcp47: str | None = None,
    track_kind: str | None = None,
) -> str:
    """Compute deterministic transcript authorization attestation ID: `tatt_<sha256_16>`."""
    if not resource_id or not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError("resource_id must be a non-empty string")
    lang_norm = validate_bcp47_language_tag(language_bcp47) if language_bcp47 else ""
    kind_norm = (track_kind or "").strip().upper()
    digest = sha256_hex(
        "|".join(
            [
                resource_id.strip(),
                (transcript_id or "").strip(),
                lang_norm,
                kind_norm,
                (authorization_basis or "").strip().upper(),
                (evidence_type or "").strip().upper(),
                (evidence_reference or "").strip(),
            ]
        )
    )[:16]
    return f"tatt_{digest}"


def compute_segment_deep_link_url(media_canonical_url: str, start_ms: int) -> str:
    """Compute deterministic timestamp deep link (`#t=<seconds>s`) for a media URL."""
    canonical = validate_and_canonicalize_url(media_canonical_url)
    seconds = max(0, int(start_ms) // 1000)
    return f"{canonical}#t={seconds}s"


def validate_transcript_state_transition(previous_state: str, new_state: str) -> None:
    """Validate that `previous_state -> new_state` is an allowed transcript state transition."""
    if previous_state not in VALID_TRANSCRIPT_INGESTION_STATES:
        raise ValueError(f"Unknown current transcript ingestion_state: {previous_state!r}")
    if new_state not in VALID_TRANSCRIPT_INGESTION_STATES:
        raise ValueError(f"Unknown target transcript ingestion_state: {new_state!r}")
    if previous_state == new_state:
        return
    allowed = VALID_TRANSCRIPT_STATE_TRANSITIONS.get(previous_state, frozenset())
    if new_state not in allowed:
        raise ValueError(
            f"Invalid transcript ingestion_state transition: {previous_state!r} -> {new_state!r} "
            f"(allowed targets from {previous_state!r}: {sorted(allowed)})"
        )


@dataclass
class TranscriptAuthorizationAttestation:
    """Structured, auditable authorization attestation for transcript rights and scope."""

    attestation_id: str
    resource_id: str
    authorization_basis: str
    license_status: str
    evidence_type: str
    evidence_reference: str
    reviewed_by: str
    reviewed_at: str
    verification_status: str = "VERIFIED"
    transcript_id: str | None = None
    language_bcp47: str | None = None
    track_kind: str | None = None
    license_spdx: str | None = None
    evidence_url: str | None = None
    evidence_sha256: str | None = None
    permits_metadata_indexing: bool = True
    permits_transcript_storage: bool = False
    permits_verbatim_excerpts: bool = False
    permits_ai_synthesis: bool = True
    permits_redistribution: bool = False
    review_notes: str | None = None
    expires_at: str | None = None
    withdrawn_at: str | None = None
    withdrawn_by: str | None = None
    withdrawal_reason: str | None = None
    revision_number: int = 1
    provenance_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "resource_id": self.resource_id,
            "transcript_id": self.transcript_id,
            "language_bcp47": self.language_bcp47,
            "track_kind": self.track_kind,
            "authorization_basis": self.authorization_basis,
            "license_status": self.license_status,
            "license_spdx": self.license_spdx,
            "evidence_type": self.evidence_type,
            "evidence_reference": self.evidence_reference,
            "evidence_url": self.evidence_url,
            "evidence_sha256": self.evidence_sha256,
            "permits_metadata_indexing": int(bool(self.permits_metadata_indexing)),
            "permits_transcript_storage": int(bool(self.permits_transcript_storage)),
            "permits_verbatim_excerpts": int(bool(self.permits_verbatim_excerpts)),
            "permits_ai_synthesis": int(bool(self.permits_ai_synthesis)),
            "permits_redistribution": int(bool(self.permits_redistribution)),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "verification_status": self.verification_status,
            "review_notes": self.review_notes,
            "expires_at": self.expires_at,
            "withdrawn_at": self.withdrawn_at,
            "withdrawn_by": self.withdrawn_by,
            "withdrawal_reason": self.withdrawal_reason,
            "revision_number": self.revision_number,
            "provenance_event_id": self.provenance_event_id,
        }


@dataclass
class TranscriptSegmentCue:
    """Parsed and normalized transcript segment cue prior to database persistence."""

    sequence_index: int
    start_ms: int
    end_ms: int
    original_segment_text: str
    normalized_segment_text: str
    speaker_label: str | None = None
    speaker_confidence: float | None = None
    token_confidence_mean: float | None = None
    token_confidence_min: float | None = None
    is_low_confidence: bool = False
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class TranscriptPolicyDecision:
    """Result of evaluating the transcript storage & excerpt policy gate."""

    permits_transcript_storage: bool
    permits_verbatim_excerpts: bool
    permits_ai_synthesis: bool
    ingestion_state: str
    transcript_storage_status: str
    block_or_review_reason: str | None = None
    permits_redistribution: bool = False
    permits_metadata_indexing: bool = True
    authorization_attestation_id: str | None = None


@dataclass
class TranscriptImportResult:
    """Summary returned by an offline transcript import or metadata registration call."""

    status: str
    transcript_id: str
    resource_id: str
    source_id: str
    language_bcp47: str
    track_kind: str
    ingestion_state: str
    transcript_storage_status: str
    permits_transcript_storage: bool
    permits_verbatim_excerpts: bool
    transcript_version_id: str | None = None
    version_number: int | None = None
    stored_segments: int = 0
    reason: str | None = None
    provenance_event_id: str | None = None
    authorization_attestation_id: str | None = None
    permits_redistribution: bool = False


def _is_iso_timestamp_expired(expires_at: str | None, now_iso: str | None = None) -> bool:
    """Return True if `expires_at` is non-empty and earlier than or equal to `now_iso`."""
    if not expires_at or not str(expires_at).strip():
        return False
    ref_str = (now_iso or utc_now()).strip()
    exp_str = str(expires_at).strip()
    try:
        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        ref_dt = datetime.fromisoformat(ref_str.replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        return exp_dt <= ref_dt
    except ValueError:
        return exp_str <= ref_str


def _detect_attestation_contradictions(
    *,
    authorization_basis: str,
    license_status: str,
    license_spdx: str | None,
    evidence_type: str,
    evidence_reference: str,
    permits_metadata_indexing: bool,
    permits_transcript_storage: bool,
    permits_verbatim_excerpts: bool,
    permits_redistribution: bool,
) -> list[str]:
    """Return a list of contradiction or missing-evidence reasons for an attestation record."""
    issues: list[str] = []
    if not evidence_reference or not str(evidence_reference).strip():
        issues.append("Attestation evidence_reference is empty.")
    if permits_transcript_storage:
        if not permits_metadata_indexing:
            issues.append(
                "Contradictory attestation scope: permits_transcript_storage=1 requires permits_metadata_indexing=1."
            )
        if license_status != "VERIFIED_PERMISSIVE":
            issues.append(
                f"Contradictory attestation: permits_transcript_storage=1 conflicts with license_status={license_status!r}."
            )
        if authorization_basis not in AUTHORIZED_STORAGE_BASES:
            issues.append(
                f"Contradictory attestation: permits_transcript_storage=1 conflicts with authorization_basis={authorization_basis!r}."
            )
        if authorization_basis == "OPEN_LICENSE_VERIFIED" and not (
            license_spdx and str(license_spdx).strip()
        ):
            issues.append(
                "Contradictory attestation: authorization_basis='OPEN_LICENSE_VERIFIED' requires a non-empty license_spdx."
            )
    else:
        if permits_verbatim_excerpts:
            issues.append(
                "Contradictory attestation scope: permits_verbatim_excerpts=1 cannot be granted when permits_transcript_storage=0."
            )
        if permits_redistribution:
            issues.append(
                "Contradictory attestation scope: permits_redistribution=1 cannot be granted when permits_transcript_storage=0."
            )
    if (
        authorization_basis in ("OPEN_LICENSE_VERIFIED", "PUBLIC_DOMAIN_VERIFIED")
        and license_status in ("ALL_RIGHTS_RESERVED", "RESTRICTED_PROPRIETARY")
    ):
        issues.append(
            f"Contradictory attestation: authorization_basis={authorization_basis!r} conflicts with license_status={license_status!r}."
        )
    if (
        license_spdx
        and str(license_spdx).strip().upper() in {s.upper() for s in PERMISSIVE_SPDX_IDENTIFIERS}
        and license_status in ("ALL_RIGHTS_RESERVED", "RESTRICTED_PROPRIETARY")
    ):
        issues.append(
            f"Contradictory attestation: permissive license_spdx={license_spdx!r} conflicts with license_status={license_status!r}."
        )
    return issues


def evaluate_transcript_storage_policy(
    *,
    resource_row: dict[str, Any],
    acquisition_method: str,
    license_status: str = "UNKNOWN",
    license_spdx: str | None = None,
    authorization_basis: str = "NONE",
    attribution_text: str,
    requested_transcript_storage: bool,
    requested_verbatim_excerpts: bool,
    requested_ai_synthesis: bool = True,
    requested_redistribution: bool = False,
    attestation: dict[str, Any] | TranscriptAuthorizationAttestation | None = None,
    requested_attestation_id: str | None = None,
    expected_transcript_id: str | None = None,
    expected_language_bcp47: str | None = None,
    expected_track_kind: str | None = None,
    now_iso: str | None = None,
) -> TranscriptPolicyDecision:
    """Evaluate the mandatory conjunctive transcript policy gate with auditable authorization attestation (ARCH-TRANSCRIPT-007 §2)."""
    if acquisition_method not in VALID_TRANSCRIPT_ACQUISITION_METHODS:
        raise ValueError(f"Invalid acquisition_method: {acquisition_method!r}")
    if license_status not in VALID_TRANSCRIPT_LICENSE_STATUSES:
        raise ValueError(f"Invalid license_status: {license_status!r}")
    if authorization_basis not in VALID_AUTHORIZATION_BASES:
        raise ValueError(f"Invalid authorization_basis: {authorization_basis!r}")
    if not isinstance(attribution_text, str) or not attribution_text.strip():
        raise ValueError("attribution_text must be a non-empty string")

    att_dict: dict[str, Any] | None = None
    if isinstance(attestation, TranscriptAuthorizationAttestation):
        att_dict = attestation.to_dict()
    elif isinstance(attestation, dict):
        att_dict = dict(attestation)

    if resource_row.get("collection_decision") == "REJECTED":
        return TranscriptPolicyDecision(
            permits_transcript_storage=False,
            permits_verbatim_excerpts=False,
            permits_ai_synthesis=False,
            permits_redistribution=False,
            permits_metadata_indexing=False,
            ingestion_state="BLOCKED_BY_POLICY",
            transcript_storage_status="BLOCKED_NO_AUTHORIZATION",
            block_or_review_reason="Parent resource collection_decision is REJECTED.",
            authorization_attestation_id=att_dict.get("attestation_id") if att_dict else requested_attestation_id,
        )

    target_resource_id = str(resource_row.get("resource_id") or "")

    # If an attestation is supplied, validate resource/transcript/language/track binding and lifecycle first
    att_errors: list[str] = []
    att_id: str | None = requested_attestation_id
    if att_dict is not None:
        att_id = str(att_dict.get("attestation_id") or "") or requested_attestation_id
        att_resource_id = str(att_dict.get("resource_id") or "")
        if att_resource_id != target_resource_id:
            att_errors.append(
                f"Attestation {att_id!r} belongs to resource {att_resource_id!r} and cannot authorize resource {target_resource_id!r} (resource_id mismatch)."
            )
        att_tr_id = att_dict.get("transcript_id")
        if att_tr_id and expected_transcript_id and str(att_tr_id) != str(expected_transcript_id):
            att_errors.append(
                f"Attestation {att_id!r} is scoped to transcript_id {att_tr_id!r}, not {expected_transcript_id!r} (transcript_id mismatch)."
            )
        att_lang = att_dict.get("language_bcp47")
        if att_lang and expected_language_bcp47:
            if validate_bcp47_language_tag(str(att_lang)) != validate_bcp47_language_tag(str(expected_language_bcp47)):
                att_errors.append(
                    f"Attestation {att_id!r} is scoped to language {att_lang!r}, not {expected_language_bcp47!r} (language_bcp47 mismatch)."
                )
        att_kind = att_dict.get("track_kind")
        if att_kind and expected_track_kind:
            if str(att_kind).strip().upper() != str(expected_track_kind).strip().upper():
                att_errors.append(
                    f"Attestation {att_id!r} is scoped to track_kind {att_kind!r}, not {expected_track_kind!r} (track_kind mismatch)."
                )
        ver_status = str(att_dict.get("verification_status") or "UNKNOWN").upper()
        if att_dict.get("withdrawn_at") or ver_status == "WITHDRAWN":
            att_errors.append(
                f"Attestation {att_id!r} has been WITHDRAWN ({att_dict.get('withdrawal_reason') or 'revoked'})."
            )
        if ver_status == "EXPIRED" or _is_iso_timestamp_expired(att_dict.get("expires_at"), now_iso=now_iso):
            att_errors.append(
                f"Attestation {att_id!r} is EXPIRED (expires_at={att_dict.get('expires_at')!r})."
            )
        if ver_status != "VERIFIED" and ver_status not in ("WITHDRAWN", "EXPIRED"):
            att_errors.append(
                f"Attestation {att_id!r} verification_status is {ver_status!r} (requires 'VERIFIED')."
            )
        reviewed_by = str(att_dict.get("reviewed_by") or "").strip()
        reviewed_at = str(att_dict.get("reviewed_at") or "").strip()
        if not reviewed_by or not reviewed_at:
            att_errors.append("Attestation is missing required reviewed_by or reviewed_at metadata.")

        contradictions = _detect_attestation_contradictions(
            authorization_basis=str(att_dict.get("authorization_basis") or "NONE"),
            license_status=str(att_dict.get("license_status") or "UNKNOWN"),
            license_spdx=att_dict.get("license_spdx"),
            evidence_type=str(att_dict.get("evidence_type") or ""),
            evidence_reference=str(att_dict.get("evidence_reference") or ""),
            permits_metadata_indexing=bool(att_dict.get("permits_metadata_indexing", 1)),
            permits_transcript_storage=bool(att_dict.get("permits_transcript_storage", 0)),
            permits_verbatim_excerpts=bool(att_dict.get("permits_verbatim_excerpts", 0)),
            permits_redistribution=bool(att_dict.get("permits_redistribution", 0)),
        )
        att_errors.extend(contradictions)

        # Check for contradictions between explicit caller parameters and the attestation record
        att_basis = str(att_dict.get("authorization_basis") or "NONE")
        if authorization_basis != "NONE" and authorization_basis != att_basis:
            att_errors.append(
                f"Contradictory authorization_basis: caller specified {authorization_basis!r} but attestation {att_id!r} specifies {att_basis!r}."
            )
        att_lic_status = str(att_dict.get("license_status") or "UNKNOWN")
        if license_status != "UNKNOWN" and license_status != att_lic_status:
            att_errors.append(
                f"Contradictory license_status: caller specified {license_status!r} but attestation {att_id!r} specifies {att_lic_status!r}."
            )
        att_spdx = (att_dict.get("license_spdx") or "").strip()
        caller_spdx = (license_spdx or "").strip()
        if caller_spdx and att_spdx and caller_spdx.upper() != att_spdx.upper():
            att_errors.append(
                f"Contradictory license_spdx: caller specified {caller_spdx!r} but attestation {att_id!r} specifies {att_spdx!r}."
            )

    effective_license_status = (
        str(att_dict.get("license_status") or "UNKNOWN")
        if (att_dict is not None and license_status == "UNKNOWN")
        else license_status
    )
    effective_authorization_basis = (
        str(att_dict.get("authorization_basis") or "NONE")
        if (att_dict is not None and authorization_basis == "NONE")
        else authorization_basis
    )
    effective_license_spdx = (
        (att_dict.get("license_spdx") or None)
        if (att_dict is not None and not license_spdx)
        else license_spdx
    )

    if acquisition_method == "METADATA_AND_LINK_ONLY":
        if requested_attestation_id and att_dict is None:
            return TranscriptPolicyDecision(
                permits_transcript_storage=False,
                permits_verbatim_excerpts=False,
                permits_ai_synthesis=False,
                permits_redistribution=False,
                permits_metadata_indexing=False,
                ingestion_state="BLOCKED_BY_POLICY",
                transcript_storage_status="BLOCKED_NO_AUTHORIZATION",
                block_or_review_reason=f"Referenced transcript authorization attestation {requested_attestation_id!r} was not found.",
                authorization_attestation_id=None,
            )
        if att_dict is not None and (att_errors or not bool(att_dict.get("permits_metadata_indexing", 1))):
            err_msg = " ".join(att_errors) if att_errors else "Attestation does not permit metadata indexing."
            return TranscriptPolicyDecision(
                permits_transcript_storage=False,
                permits_verbatim_excerpts=False,
                permits_ai_synthesis=False,
                permits_redistribution=False,
                permits_metadata_indexing=False,
                ingestion_state="BLOCKED_BY_POLICY",
                transcript_storage_status="BLOCKED_NO_AUTHORIZATION",
                block_or_review_reason=err_msg,
                authorization_attestation_id=att_id if att_dict is not None else None,
            )
        return TranscriptPolicyDecision(
            permits_transcript_storage=False,
            permits_verbatim_excerpts=False,
            permits_ai_synthesis=bool(
                requested_ai_synthesis
                and (att_dict is None or bool(att_dict.get("permits_ai_synthesis", 1)))
            ),
            permits_redistribution=False,
            permits_metadata_indexing=True,
            ingestion_state="METADATA_ONLY_LINKED",
            transcript_storage_status="METADATA_AND_LINK_ONLY",
            block_or_review_reason=(
                "Metadata and deep-link only mode; transcript text storage is disabled."
            ),
            authorization_attestation_id=att_id if att_dict is not None else None,
        )

    # If a valid, verified attestation grants ONLY metadata indexing (`permits_metadata_indexing=1`,
    # `permits_transcript_storage=0`) and the caller did NOT demand `requested_transcript_storage=True`
    # (nor verbatim excerpts / redistribution), allow metadata-only registration while blocking text storage.
    if (
        att_dict is not None
        and not att_errors
        and bool(att_dict.get("permits_metadata_indexing", 1))
        and not bool(att_dict.get("permits_transcript_storage", 0))
        and not requested_transcript_storage
        and not requested_verbatim_excerpts
        and not requested_redistribution
    ):
        return TranscriptPolicyDecision(
            permits_transcript_storage=False,
            permits_verbatim_excerpts=False,
            permits_ai_synthesis=bool(
                requested_ai_synthesis and bool(att_dict.get("permits_ai_synthesis", 1))
            ),
            permits_redistribution=False,
            permits_metadata_indexing=True,
            ingestion_state="METADATA_ONLY_LINKED",
            transcript_storage_status="METADATA_AND_LINK_ONLY",
            block_or_review_reason=(
                f"Attestation {att_id!r} permits metadata indexing only; transcript text storage is disabled."
            ),
            authorization_attestation_id=att_id,
        )

    reasons: list[str] = []
    if att_dict is None:
        if requested_attestation_id:
            reasons.append(
                f"Referenced transcript authorization attestation {requested_attestation_id!r} was not found."
            )
        else:
            reasons.append(
                "Missing auditable authorization attestation: caller-supplied flags alone cannot authorize transcript storage."
            )
    else:
        reasons.extend(att_errors)
        if not bool(att_dict.get("permits_transcript_storage", 0)):
            reasons.append(
                f"Attestation {att_id!r} does not grant permits_transcript_storage (metadata-only or storage disallowed)."
            )
        if requested_verbatim_excerpts and not bool(att_dict.get("permits_verbatim_excerpts", 0)):
            reasons.append(
                f"Requested permits_verbatim_excerpts=True exceeds attestation {att_id!r} scope (permits_verbatim_excerpts=0)."
            )
        if requested_redistribution and not bool(att_dict.get("permits_redistribution", 0)):
            reasons.append(
                f"Requested permits_redistribution=True exceeds attestation {att_id!r} scope (permits_redistribution=0)."
            )

    if not requested_transcript_storage:
        reasons.append("Explicit permits_transcript_storage flag was not requested.")
    if effective_license_status != "VERIFIED_PERMISSIVE":
        reasons.append(
            f"Transcript license_status is {effective_license_status!r} (requires 'VERIFIED_PERMISSIVE')."
        )
    if effective_authorization_basis not in AUTHORIZED_STORAGE_BASES:
        reasons.append(
            f"Transcript authorization_basis is {effective_authorization_basis!r} (requires verified open license, public domain, first-party ownership, or written permission)."
        )
    if effective_authorization_basis == "OPEN_LICENSE_VERIFIED" and not (
        effective_license_spdx and str(effective_license_spdx).strip()
    ):
        reasons.append(
            "authorization_basis 'OPEN_LICENSE_VERIFIED' requires a non-empty license_spdx identifier."
        )

    if reasons:
        return TranscriptPolicyDecision(
            permits_transcript_storage=False,
            permits_verbatim_excerpts=False,
            permits_ai_synthesis=bool(
                requested_ai_synthesis
                and (att_dict is not None and not att_errors and bool(att_dict.get("permits_ai_synthesis", 0)))
            ),
            permits_redistribution=False,
            permits_metadata_indexing=bool(
                att_dict is not None and not att_errors and bool(att_dict.get("permits_metadata_indexing", 1))
            ),
            ingestion_state="BLOCKED_BY_POLICY",
            transcript_storage_status="BLOCKED_NO_AUTHORIZATION",
            block_or_review_reason=" ".join(reasons),
            authorization_attestation_id=att_id if att_dict is not None else None,
        )

    assert att_dict is not None
    effective_excerpts = bool(
        requested_verbatim_excerpts and bool(att_dict.get("permits_verbatim_excerpts", 0))
    )
    effective_synthesis = bool(
        requested_ai_synthesis and bool(att_dict.get("permits_ai_synthesis", 1))
    )
    effective_redistribution = bool(
        requested_redistribution and bool(att_dict.get("permits_redistribution", 0))
    )
    return TranscriptPolicyDecision(
        permits_transcript_storage=True,
        permits_verbatim_excerpts=effective_excerpts,
        permits_ai_synthesis=effective_synthesis,
        permits_redistribution=effective_redistribution,
        permits_metadata_indexing=True,
        ingestion_state="READY_FOR_INGESTION",
        transcript_storage_status="NOT_STORED",
        block_or_review_reason=None,
        authorization_attestation_id=att_id,
    )


def parse_timestamp_to_ms(raw_timestamp: str) -> int:
    """Parse a WebVTT, SRT, or plain-text timestamp (`HH:MM:SS.mmm`, `HH:MM:SS,mmm`, `MM:SS.mmm`, `MM:SS`) into integer milliseconds."""
    if not isinstance(raw_timestamp, str):
        raise ValueError("Timestamp must be a string")
    cleaned = raw_timestamp.strip()
    m = re.fullmatch(
        r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?",
        cleaned,
    )
    if not m:
        raise ValueError(f"Invalid transcript timestamp format: {raw_timestamp!r}")
    hours_str, mins_str, secs_str, ms_str = m.groups()
    hours = int(hours_str) if hours_str is not None else 0
    mins = int(mins_str)
    secs = int(secs_str)
    if mins >= 60 or secs >= 60:
        raise ValueError(f"Invalid minute/second value in timestamp: {raw_timestamp!r}")
    millis = int((ms_str or "0").ljust(3, "0"))
    return ((hours * 3600) + (mins * 60) + secs) * 1000 + millis


def _extract_speaker_and_clean_markup(raw_cue_text: str) -> tuple[str | None, str]:
    """Extract speaker label from `<v Speaker>` or `Speaker:` prefix if present, returning `(speaker_label, inner_text)`."""
    speaker: str | None = None
    text = raw_cue_text.strip()
    m_vtt = _VTT_VOICE_TAG_RE.match(text)
    if m_vtt:
        speaker = m_vtt.group(1).strip() or None
        text = m_vtt.group(2).strip()
    else:
        # Strip inline tags first to inspect plain prefix, or check prefix before stripping
        m_pref = _SPEAKER_PREFIX_RE.match(text)
        if m_pref:
            cand = (m_pref.group(1) or m_pref.group(2) or "").strip()
            if cand and not cand.lower().startswith(("http", "note", "chapter", "part", "section")):
                speaker = cand
                text = m_pref.group(3).strip()
    return speaker, text


def normalize_segment_text(
    raw_text: str,
    *,
    token_confidence_mean: float | None = None,
) -> tuple[str, str | None, bool, list[str]]:
    """Deterministic rule normalizer (`norm-v1.0`).

    Preserves technical symbols, variable names, equations, and spoken tokens while:
    - Extracting `<v Speaker>` or `[Speaker]:` speaker prefixes when present
    - Stripping WebVTT/SRT formatting tags (`<c>`, `<i>`, `<b>`, `<u>`, `<ruby>`, `<00:00:00.000>`)
    - Unescaping HTML entities (`&amp;`, `&lt;`, `&gt;`, etc.)
    - Applying Unicode NFC normalization and collapsing whitespace
    - Flagging `[inaudible]` / `[unintelligible]` or low ASR confidence (< 0.75) without deleting words
    """
    if not isinstance(raw_text, str):
        raise ValueError("Segment text must be a string")
    speaker, body = _extract_speaker_and_clean_markup(raw_text)
    # Remove WebVTT inline timestamp tags like <00:01:02.500> and formatting tags
    without_tags = re.sub(r"<[^>]+>", " ", body)
    unescaped = html.unescape(without_tags)
    nfc = unicodedata.normalize("NFC", unescaped)
    normalized = re.sub(r"\s+", " ", nfc).strip()

    quality_flags: list[str] = []
    is_low_conf = False
    if _INAUDIBLE_MARKER_RE.search(normalized):
        quality_flags.append("CONTAINS_INAUDIBLE_MARKER")
        is_low_conf = True
    if token_confidence_mean is not None and token_confidence_mean < 0.75:
        quality_flags.append("LOW_ASR_CONFIDENCE")
        is_low_conf = True

    return normalized, speaker, is_low_conf, quality_flags


def parse_webvtt_transcript(raw_content: str) -> list[TranscriptSegmentCue]:
    """Parse a WebVTT (`.vtt`) transcript into deterministic `TranscriptSegmentCue` items."""
    if not isinstance(raw_content, str):
        raise ValueError("WebVTT content must be a string")
    text = raw_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or not lines[0].strip().startswith("WEBVTT"):
        raise ValueError("Invalid WebVTT transcript: missing required 'WEBVTT' file header")

    raw_blocks = re.split(r"\n\s*\n", text.strip())
    header_lines = [ln.strip() for ln in raw_blocks[0].split("\n") if ln.strip()]
    remaining_header = header_lines[1:]
    while remaining_header and "-->" not in remaining_header[0] and re.match(r"^[A-Za-z0-9_-]+\s*:", remaining_header[0]):
        remaining_header.pop(0)
    blocks = (["\n".join(remaining_header)] if remaining_header else []) + raw_blocks[1:]
    cues: list[TranscriptSegmentCue] = []
    for block in blocks:
        block_lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not block_lines:
            continue
        first = block_lines[0]
        if first.startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_line_idx = 0
        if "-->" not in first:
            if len(block_lines) >= 2 and "-->" in block_lines[1]:
                timing_line_idx = 1
            else:
                raise ValueError(f"Malformed WebVTT cue block (missing '-->' timing line): {first!r}")

        timing_line = block_lines[timing_line_idx]
        payload_lines = block_lines[timing_line_idx + 1 :]
        if not payload_lines:
            raise ValueError(f"Empty WebVTT cue text for timing line: {timing_line!r}")

        parts = timing_line.split("-->")
        if len(parts) != 2:
            raise ValueError(f"Malformed WebVTT timing line: {timing_line!r}")
        start_raw = parts[0].strip()
        end_raw = parts[1].strip().split()[0]
        start_ms = parse_timestamp_to_ms(start_raw)
        end_ms = parse_timestamp_to_ms(end_raw)

        original_text = "\n".join(payload_lines).strip()
        normalized_text, speaker, is_low_conf, flags = normalize_segment_text(original_text)
        cues.append(
            TranscriptSegmentCue(
                sequence_index=len(cues),
                start_ms=start_ms,
                end_ms=end_ms,
                original_segment_text=original_text,
                normalized_segment_text=normalized_text,
                speaker_label=speaker,
                is_low_confidence=is_low_conf,
                quality_flags=flags,
            )
        )
    return cues


def parse_srt_transcript(raw_content: str) -> list[TranscriptSegmentCue]:
    """Parse a SubRip (`.srt`) transcript into deterministic `TranscriptSegmentCue` items."""
    if not isinstance(raw_content, str):
        raise ValueError("SRT content must be a string")
    text = raw_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    blocks = re.split(r"\n\s*\n", text)
    cues: list[TranscriptSegmentCue] = []
    for block in blocks:
        block_lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not block_lines:
            continue
        timing_line_idx = 0
        if "-->" not in block_lines[0]:
            if len(block_lines) >= 2 and "-->" in block_lines[1]:
                timing_line_idx = 1
            else:
                raise ValueError(
                    f"Malformed SRT cue block (missing '-->' timing line): {block_lines[0]!r}"
                )
        timing_line = block_lines[timing_line_idx]
        payload_lines = block_lines[timing_line_idx + 1 :]
        if not payload_lines:
            raise ValueError(f"Empty SRT cue text for timing line: {timing_line!r}")

        parts = timing_line.split("-->")
        if len(parts) != 2:
            raise ValueError(f"Malformed SRT timing line: {timing_line!r}")
        start_raw = parts[0].strip()
        end_raw = parts[1].strip().split()[0]
        start_ms = parse_timestamp_to_ms(start_raw)
        end_ms = parse_timestamp_to_ms(end_raw)

        original_text = "\n".join(payload_lines).strip()
        normalized_text, speaker, is_low_conf, flags = normalize_segment_text(original_text)
        cues.append(
            TranscriptSegmentCue(
                sequence_index=len(cues),
                start_ms=start_ms,
                end_ms=end_ms,
                original_segment_text=original_text,
                normalized_segment_text=normalized_text,
                speaker_label=speaker,
                is_low_confidence=is_low_conf,
                quality_flags=flags,
            )
        )
    return cues


_PLAIN_TIMED_LINE_RE = re.compile(
    r"^\s*\[?(\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
    r"(?:\s*(?:-|-->|–)\s*(\d{1,3}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?))?\]?\s+(.+)$"
)


def parse_plain_text_transcript(raw_content: str) -> list[TranscriptSegmentCue]:
    """Parse a plain-text (`.txt`) transcript (either timestamped lines or untimed paragraphs)."""
    if not isinstance(raw_content, str):
        raise ValueError("Plain text content must be a string")
    text = raw_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    non_empty_lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    timed_matches = [_PLAIN_TIMED_LINE_RE.match(ln) for ln in non_empty_lines]
    if non_empty_lines and all(m is not None for m in timed_matches):
        parsed_rows: list[tuple[int, int | None, str]] = []
        for m in timed_matches:
            assert m is not None
            start_ms = parse_timestamp_to_ms(m.group(1))
            end_ms = parse_timestamp_to_ms(m.group(2)) if m.group(2) else None
            payload = m.group(3).strip()
            parsed_rows.append((start_ms, end_ms, payload))

        cues: list[TranscriptSegmentCue] = []
        for idx, (start_ms, explicit_end_ms, payload) in enumerate(parsed_rows):
            if explicit_end_ms is not None:
                end_ms = explicit_end_ms
            elif idx + 1 < len(parsed_rows) and parsed_rows[idx + 1][0] >= start_ms:
                end_ms = parsed_rows[idx + 1][0]
            else:
                end_ms = start_ms
            normalized_text, speaker, is_low_conf, flags = normalize_segment_text(payload)
            cues.append(
                TranscriptSegmentCue(
                    sequence_index=idx,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    original_segment_text=payload,
                    normalized_segment_text=normalized_text,
                    speaker_label=speaker,
                    is_low_confidence=is_low_conf,
                    quality_flags=flags,
                )
            )
        return cues

    # Paragraph-based plain text fallback
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    cues = []
    for idx, para in enumerate(paragraphs):
        normalized_text, speaker, is_low_conf, flags = normalize_segment_text(para)
        cues.append(
            TranscriptSegmentCue(
                sequence_index=idx,
                start_ms=0,
                end_ms=0,
                original_segment_text=para,
                normalized_segment_text=normalized_text,
                speaker_label=speaker,
                is_low_confidence=is_low_conf,
                quality_flags=flags,
            )
        )
    return cues


def detect_caption_format_from_path(path: Path | str) -> str:
    """Detect canonical `caption_format` from a local file extension."""
    suffix = Path(path).suffix.lower()
    if suffix == ".vtt":
        return "WEBVTT"
    if suffix == ".srt":
        return "SRT"
    if suffix in (".txt", ".text", ".md"):
        return "PLAIN_TEXT"
    raise ValueError(
        f"Cannot infer caption_format from extension {suffix!r}; specify --format explicitly"
    )


def parse_transcript_content(raw_content: str, caption_format: str) -> list[TranscriptSegmentCue]:
    """Dispatch transcript parsing based on `caption_format`."""
    fmt = (caption_format or "").strip().upper()
    if fmt == "WEBVTT":
        return parse_webvtt_transcript(raw_content)
    if fmt == "SRT":
        return parse_srt_transcript(raw_content)
    if fmt in ("PLAIN_TEXT", "HTML_INLINE"):
        return parse_plain_text_transcript(raw_content)
    raise ValueError(
        f"Unsupported offline transcript caption_format {caption_format!r} "
        "(supported local import formats: 'WEBVTT', 'SRT', 'PLAIN_TEXT')"
    )


def validate_transcript_cues(
    cues: list[TranscriptSegmentCue],
    *,
    language_bcp47: str,
    attribution_text: str,
    license_status: str,
    duration_ms: int | None = None,
) -> tuple[bool, str | None]:
    """Execute validation rules TV-01 through TV-08 from ARCH-TRANSCRIPT-007 §5.2.

    Raises `ValueError` if structural integrity checks (TV-01..TV-06, TV-08) fail.
    Returns `(needs_review, review_reason)` for confidence threshold checks (TV-07).
    """
    # TV-06: Language tag validity
    validate_bcp47_language_tag(language_bcp47)

    # TV-08: Attribution completeness
    if not isinstance(attribution_text, str) or not attribution_text.strip():
        raise ValueError("TV-08 validation failed: attribution_text must be non-empty")
    if license_status not in VALID_TRANSCRIPT_LICENSE_STATUSES:
        raise ValueError(f"TV-08 validation failed: invalid license_status {license_status!r}")

    # TV-01: Non-empty cue list
    if not cues:
        raise ValueError("TV-01 validation failed: transcript contains zero segments")

    prev_start_ms = 0
    low_conf_count = 0
    conf_values: list[float] = []

    for expected_idx, cue in enumerate(cues):
        # Contiguous sequence_index check (TV-02)
        if cue.sequence_index != expected_idx:
            raise ValueError(
                f"TV-02 validation failed: non-contiguous sequence_index at position {expected_idx} "
                f"(got {cue.sequence_index})"
            )
        # Monotonic non-negative timestamps (TV-02)
        if cue.start_ms < 0 or cue.end_ms < cue.start_ms:
            raise ValueError(
                f"TV-02 validation failed at segment {expected_idx}: "
                f"invalid interval start_ms={cue.start_ms}, end_ms={cue.end_ms}"
            )
        if expected_idx > 0 and cue.start_ms < prev_start_ms:
            raise ValueError(
                f"TV-02 validation failed at segment {expected_idx}: "
                f"non-monotonic start_ms={cue.start_ms} < previous start_ms={prev_start_ms}"
            )
        prev_start_ms = cue.start_ms

        # TV-03: Duration sanity check
        if duration_ms is not None and cue.end_ms > int(duration_ms) + 5000:
            raise ValueError(
                f"TV-03 validation failed at segment {expected_idx}: "
                f"end_ms={cue.end_ms} exceeds resource duration_ms={duration_ms} + 5000ms tolerance"
            )

        # TV-04: Non-empty segment text
        if not cue.original_segment_text or not cue.original_segment_text.strip():
            raise ValueError(
                f"TV-04 validation failed at segment {expected_idx}: empty original_segment_text"
            )
        if not cue.normalized_segment_text or not cue.normalized_segment_text.strip():
            raise ValueError(
                f"TV-04 validation failed at segment {expected_idx}: empty normalized_segment_text"
            )

        # TV-05: Residual markup removal
        if _RESIDUAL_TAG_RE.search(cue.normalized_segment_text):
            raise ValueError(
                f"TV-05 validation failed at segment {expected_idx}: "
                "normalized_segment_text still contains raw markup tags"
            )

        if cue.is_low_confidence:
            low_conf_count += 1
        if cue.token_confidence_mean is not None:
            conf_values.append(float(cue.token_confidence_mean))

    # TV-07: Confidence threshold check
    mean_conf = (sum(conf_values) / len(conf_values)) if conf_values else None
    low_conf_ratio = low_conf_count / len(cues)
    if (mean_conf is not None and mean_conf < 0.75) or low_conf_ratio > 0.15:
        reason = (
            f"TV-07 confidence review required: low_confidence_segments={low_conf_count}/{len(cues)} "
            f"({low_conf_ratio:.1%}), mean_confidence={mean_conf}"
        )
        return True, reason

    return False, None


def compute_metadata_hash(payload: dict[str, Any]) -> str:
    """Compute deterministic 64-character SHA-256 hash over normalized metadata fields."""
    hash_fields = {
        "source_id": payload.get("source_id"),
        "manifest_id": payload.get("manifest_id"),
        "canonical_url": payload.get("canonical_url"),
        "title": payload.get("title"),
        "authors": payload.get("authors"),
        "published_at": payload.get("published_at"),
        "resource_type": payload.get("resource_type"),
        "media_modality": payload.get("media_modality"),
        "domain_scope": payload.get("domain_scope"),
        "abstract_or_description": payload.get("abstract_or_description"),
        "topic_tags": payload.get("topic_tags"),
        "external_identifier": payload.get("external_identifier"),
        "observed_from": payload.get("observed_from"),
        "information_to_inventory": payload.get("information_to_inventory"),
        "priority": payload.get("priority"),
        "collection_decision": payload.get("collection_decision"),
        "verification_status": payload.get("verification_status"),
        "robots_sitemap_status": payload.get("robots_sitemap_status"),
        "rights_status": payload.get("rights_status"),
        "license_status": payload.get("license_status"),
        "license_spdx": payload.get("license_spdx"),
        "permitted_storage_scope": payload.get("permitted_storage_scope"),
        "permits_full_text_storage": int(bool(payload.get("permits_full_text_storage", 0))),
        "permits_verbatim_excerpts": int(bool(payload.get("permits_verbatim_excerpts", 0))),
        "legal_uncertainty_status": payload.get("legal_uncertainty_status"),
        "text_storage_status": payload.get("text_storage_status"),
    }
    serialized = json.dumps(hash_fields, sort_keys=True, ensure_ascii=False)
    return sha256_hex(serialized)


# Approved canonical Phase-1 sources from docs/architecture/data-model.md & source-adapter-contract.md
CANONICAL_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "source_id": "SRC-HINTON",
        "display_name": "Geoffrey Hinton Official Website (University of Toronto)",
        "source_category": "PIONEER_HOMEPAGE",
        "adapter_type": "CONSERVATIVE_HTML_METADATA",
        "origin": "https://www.cs.toronto.edu",
        "approved_host_paths": ["www.cs.toronto.edu/~hinton/"],
        "robots_url": "https://www.cs.toronto.edu/robots.txt",
        "crawl_delay_seconds": 1.0,
        "min_enforced_delay_seconds": 1.0,
        "max_requests_per_run": 5,
        "default_license_status": "ALL_RIGHTS_RESERVED_METADATA_ONLY",
        "permits_metadata_indexing": 1,
        "permits_full_text_storage": 0,
        "permits_verbatim_excerpts": 0,
        "permits_media_rehosting": 0,
        "legal_uncertainty_status": "CONFIRMED_METADATA_ONLY",
        "attribution_template": "{title} — Geoffrey Hinton ({canonical_url})",
        "metadata_complete": 1,
        "status": "ACTIVE",
        "notes": "First-party pioneer academic website; Crawl-delay: 1s verified on domain.",
    },
    {
        "source_id": "SRC-BENGIO",
        "display_name": "Yoshua Bengio Official Website",
        "source_category": "PIONEER_WEBSITE_BLOG",
        "adapter_type": "CONSERVATIVE_HTML_METADATA",
        "origin": "https://yoshuabengio.org",
        "approved_host_paths": ["yoshuabengio.org/"],
        "robots_url": "https://yoshuabengio.org/robots.txt",
        "crawl_delay_seconds": 1.0,
        "min_enforced_delay_seconds": 1.0,
        "max_requests_per_run": 5,
        "default_license_status": "ALL_RIGHTS_RESERVED_METADATA_ONLY",
        "permits_metadata_indexing": 1,
        "permits_full_text_storage": 0,
        "permits_verbatim_excerpts": 0,
        "permits_media_rehosting": 0,
        "legal_uncertainty_status": "UNVERIFIED_REQUIRES_HUMAN_REVIEW",
        "attribution_template": "{title} — Yoshua Bengio ({canonical_url})",
        "metadata_complete": 1,
        "status": "ACTIVE",
        "notes": "First-party pioneer website and blog; robots.txt must be verified at runtime before live fetch.",
    },
    {
        "source_id": "SRC-3B1B",
        "display_name": "3Blue1Brown Educational Website & Lessons",
        "source_category": "EDUCATIONAL_MATHEMATICS",
        "adapter_type": "CONSERVATIVE_HTML_METADATA",
        "origin": "https://www.3blue1brown.com",
        "approved_host_paths": ["www.3blue1brown.com/"],
        "robots_url": "https://www.3blue1brown.com/robots.txt",
        "crawl_delay_seconds": 1.0,
        "min_enforced_delay_seconds": 1.0,
        "max_requests_per_run": 5,
        "default_license_status": "ALL_RIGHTS_RESERVED_METADATA_ONLY",
        "permits_metadata_indexing": 1,
        "permits_full_text_storage": 0,
        "permits_verbatim_excerpts": 0,
        "permits_media_rehosting": 0,
        "legal_uncertainty_status": "UNVERIFIED_REQUIRES_HUMAN_REVIEW",
        "attribution_template": "{title} — Grant Sanderson / 3Blue1Brown ({canonical_url})",
        "metadata_complete": 1,
        "status": "PLANNED_PHASE_2",
        "notes": "Phase-2 educational math and visual intuition source; metadata-only until terms are verified.",
    },
    {
        "source_id": "SRC-ARXIV",
        "display_name": "arXiv Open-Access Preprint Archive",
        "source_category": "RESEARCH_ARCHIVE_API",
        "adapter_type": "ARXIV_ATOM_API",
        "origin": "https://arxiv.org",
        "approved_host_paths": ["arxiv.org/abs/", "export.arxiv.org/api/"],
        "robots_url": "https://arxiv.org/robots.txt",
        "crawl_delay_seconds": 3.0,
        "min_enforced_delay_seconds": 3.0,
        "max_requests_per_run": 20,
        "default_license_status": "CUSTOM_PERMISSIVE",
        "permits_metadata_indexing": 1,
        "permits_full_text_storage": 0,
        "permits_verbatim_excerpts": 0,
        "permits_media_rehosting": 0,
        "legal_uncertainty_status": "CONFIRMED_PERMISSIVE",
        "attribution_template": "{authors}, \"{title},\" arXiv:{external_identifier} ({canonical_url})",
        "metadata_complete": 1,
        "status": "ACTIVE",
        "notes": "arXiv metadata (titles, authors, abstracts, IDs) is CC0; per-paper full-text license checked per record.",
    },
    {
        "source_id": "SRC-WIKIPEDIA",
        "display_name": "English Wikipedia (MediaWiki REST API)",
        "source_category": "ENCYCLOPEDIA_API",
        "adapter_type": "MEDIAWIKI_REST_API",
        "origin": "https://en.wikipedia.org",
        "approved_host_paths": ["en.wikipedia.org/api/rest_v1/", "en.wikipedia.org/wiki/"],
        "robots_url": "https://en.wikipedia.org/robots.txt",
        "crawl_delay_seconds": 1.0,
        "min_enforced_delay_seconds": 1.0,
        "max_requests_per_run": 25,
        "default_license_status": "CC_BY_SA_4_0",
        "permits_metadata_indexing": 1,
        "permits_full_text_storage": 1,
        "permits_verbatim_excerpts": 1,
        "permits_media_rehosting": 0,
        "legal_uncertainty_status": "CONFIRMED_PERMISSIVE",
        "attribution_template": "\"{title},\" Wikipedia, The Free Encyclopedia ({canonical_url}), CC BY-SA 4.0",
        "metadata_complete": 1,
        "status": "PLANNED_PHASE_2",
        "notes": "Wikipedia text is licensed under CC BY-SA 4.0 with attribution and revision provenance.",
    },
)


# Known mappings from manifest "Person / source" strings to canonical or incomplete record-only sources.
KNOWN_PERSON_SOURCE_MAP: dict[str, str] = {
    "geoffrey hinton": "SRC-HINTON",
    "university of toronto domain": "SRC-HINTON",
    "yoshua bengio": "SRC-BENGIO",
    "yoshua bengio domain": "SRC-BENGIO",
    "3blue1brown": "SRC-3B1B",
    "arxiv": "SRC-ARXIV",
    "yoshua bengio / external research archive": "SRC-ARXIV",
    "wikipedia": "SRC-WIKIPEDIA",
}


def slugify_source_id(person_source: str) -> str:
    """Create a deterministic `SRC-EXT-<SLUG>` identifier for an unmapped source label."""
    tokens = re.findall(r"[A-Za-z0-9]+", person_source.upper())
    if not tokens:
        digest = sha256_hex(person_source)[:8].upper()
        return f"SRC-UNMAPPED-{digest}"
    slug = "-".join(tokens)[:32].strip("-")
    return f"SRC-EXT-{slug}"


def infer_media_modality(resource_type: str | None, canonical_url: str) -> str:
    """Infer canonical media_modality from resource_type and URL without fabricating facts."""
    rt = (resource_type or "").casefold()
    url_lower = canonical_url.casefold()
    if "pdf" in rt or url_lower.endswith(".pdf"):
        return "PDF_DOCUMENT"
    if "video" in rt or "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "VIDEO_LINK"
    if "slide" in rt:
        return "SLIDE_INDEX"
    if "compliance endpoint" in rt or url_lower.endswith(("robots.txt", ".xml")):
        return "COMPLIANCE_ENDPOINT"
    return "TEXT_HTML"


@dataclass
class ManifestImportSummary:
    """Summary of an explicit seed manifest import operation."""

    run_id: str
    manifest_path: str
    total_records: int = 0
    imported_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    rejected_count: int = 0
    rejected_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


LEGACY_COLLECTOR_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS source_urls (
    manifest_id TEXT PRIMARY KEY,
    person_source TEXT,
    url TEXT NOT NULL,
    page_resource_name TEXT,
    resource_type TEXT,
    domain_scope TEXT,
    verification_status TEXT,
    observed_from TEXT,
    information_to_inventory TEXT,
    priority TEXT,
    collection_decision TEXT,
    robots_sitemap_status TEXT,
    rights_status TEXT,
    last_checked TEXT,
    last_fetch_status TEXT,
    last_fetch_error TEXT,
    last_fetch_at TEXT,
    source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
    canonical_url TEXT,
    imported_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id TEXT UNIQUE,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT,
    authors TEXT,
    published_at TEXT,
    content_type TEXT,
    topic_tags TEXT,
    abstract_or_description TEXT,
    external_identifier TEXT,
    license_status TEXT NOT NULL,
    text_storage_status TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT,
    metadata_hash TEXT,
    resource_id TEXT,
    version_id TEXT,
    FOREIGN KEY(manifest_id) REFERENCES source_urls(manifest_id)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    manifest_path TEXT NOT NULL,
    selected_count INTEGER NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS discovered_links (
    discovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_manifest_id TEXT NOT NULL,
    link_url TEXT NOT NULL,
    link_text TEXT,
    rel TEXT,
    target_scope TEXT NOT NULL,
    decision TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    UNIQUE(from_manifest_id, link_url),
    FOREIGN KEY(from_manifest_id) REFERENCES source_urls(manifest_id)
);

CREATE TABLE IF NOT EXISTS site_policies (
    origin TEXT PRIMARY KEY,
    robots_url TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    crawl_delay_seconds REAL,
    sitemap_urls TEXT,
    evidence TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    manifest_id TEXT,
    requested_url TEXT NOT NULL,
    final_url TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    error TEXT,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES crawl_runs(run_id)
);

CREATE TABLE IF NOT EXISTS crawl_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_url TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT,
    next_action TEXT,
    checked_at TEXT
);
"""


CANONICAL_SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY CHECK (source_id LIKE 'SRC-%' AND length(source_id) >= 5),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    source_category TEXT NOT NULL CHECK (length(trim(source_category)) > 0),
    adapter_type TEXT NOT NULL CHECK (length(trim(adapter_type)) > 0),
    origin TEXT CHECK (origin IS NULL OR origin LIKE 'http://%' OR origin LIKE 'https://%'),
    approved_host_paths TEXT NOT NULL DEFAULT '[]',
    robots_url TEXT CHECK (robots_url IS NULL OR robots_url LIKE 'http://%' OR robots_url LIKE 'https://%'),
    crawl_delay_seconds REAL NOT NULL DEFAULT 1.0 CHECK (crawl_delay_seconds >= 0.0),
    min_enforced_delay_seconds REAL NOT NULL DEFAULT 1.0 CHECK (min_enforced_delay_seconds >= 1.0),
    max_requests_per_run INTEGER NOT NULL DEFAULT 10 CHECK (max_requests_per_run >= 0),
    default_license_status TEXT NOT NULL DEFAULT 'ALL_RIGHTS_RESERVED_METADATA_ONLY',
    permits_metadata_indexing INTEGER NOT NULL DEFAULT 1 CHECK (permits_metadata_indexing IN (0, 1)),
    permits_full_text_storage INTEGER NOT NULL DEFAULT 0 CHECK (permits_full_text_storage IN (0, 1)),
    permits_verbatim_excerpts INTEGER NOT NULL DEFAULT 0 CHECK (permits_verbatim_excerpts IN (0, 1)),
    permits_media_rehosting INTEGER NOT NULL DEFAULT 0 CHECK (permits_media_rehosting IN (0, 1)),
    legal_uncertainty_status TEXT NOT NULL DEFAULT 'UNVERIFIED_REQUIRES_HUMAN_REVIEW',
    attribution_template TEXT,
    metadata_complete INTEGER NOT NULL DEFAULT 1 CHECK (metadata_complete IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY CHECK (length(trim(run_id)) > 0),
    run_type TEXT NOT NULL DEFAULT 'MANIFEST_IMPORT',
    is_dry_run INTEGER NOT NULL DEFAULT 1 CHECK (is_dry_run IN (0, 1)),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    manifest_path TEXT,
    selected_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
    imported_count INTEGER NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
    updated_count INTEGER NOT NULL DEFAULT 0 CHECK (updated_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    fetched_count INTEGER NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    status TEXT NOT NULL CHECK (length(trim(status)) > 0),
    failure_summary TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY CHECK (resource_id LIKE 'res_%' AND length(resource_id) = 20),
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    manifest_id TEXT UNIQUE REFERENCES source_urls(manifest_id) ON DELETE SET NULL,
    canonical_url TEXT NOT NULL UNIQUE CHECK (canonical_url LIKE 'http://%' OR canonical_url LIKE 'https://%'),
    fetched_url TEXT CHECK (fetched_url IS NULL OR fetched_url LIKE 'http://%' OR fetched_url LIKE 'https://%'),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    authors TEXT,
    published_at TEXT,
    resource_type TEXT NOT NULL CHECK (length(trim(resource_type)) > 0),
    media_modality TEXT NOT NULL DEFAULT 'TEXT_HTML',
    domain_scope TEXT,
    abstract_or_description TEXT,
    topic_tags TEXT,
    external_identifier TEXT,
    observed_from TEXT,
    information_to_inventory TEXT,
    priority TEXT,
    collection_decision TEXT,
    verification_status TEXT,
    robots_sitemap_status TEXT,
    rights_status TEXT,
    license_status TEXT NOT NULL DEFAULT 'ALL_RIGHTS_RESERVED_METADATA_ONLY',
    license_spdx TEXT,
    permitted_storage_scope TEXT NOT NULL DEFAULT 'METADATA_ONLY',
    permits_full_text_storage INTEGER NOT NULL DEFAULT 0 CHECK (permits_full_text_storage IN (0, 1)),
    permits_verbatim_excerpts INTEGER NOT NULL DEFAULT 0 CHECK (permits_verbatim_excerpts IN (0, 1)),
    legal_uncertainty_status TEXT NOT NULL DEFAULT 'UNVERIFIED_REQUIRES_HUMAN_REVIEW',
    text_storage_status TEXT NOT NULL DEFAULT 'metadata-only-not-yet-collected',
    current_version_id TEXT,
    version_count INTEGER NOT NULL DEFAULT 1 CHECK (version_count >= 1),
    metadata_hash TEXT NOT NULL CHECK (length(metadata_hash) = 64),
    content_hash TEXT CHECK (content_hash IS NULL OR length(content_hash) = 64),
    lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE',
    first_discovered_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_checked TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, canonical_url)
);

CREATE TABLE IF NOT EXISTS resource_versions (
    version_id TEXT PRIMARY KEY CHECK (version_id LIKE 'ver_%'),
    resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    canonical_url TEXT NOT NULL CHECK (canonical_url LIKE 'http://%' OR canonical_url LIKE 'https://%'),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    authors TEXT,
    published_at TEXT,
    resource_type TEXT NOT NULL CHECK (length(trim(resource_type)) > 0),
    abstract_or_description TEXT,
    topic_tags TEXT,
    external_identifier TEXT,
    collection_decision TEXT,
    verification_status TEXT,
    robots_sitemap_status TEXT,
    rights_status TEXT,
    license_status TEXT NOT NULL,
    text_storage_status TEXT NOT NULL,
    metadata_hash TEXT NOT NULL CHECK (length(metadata_hash) = 64),
    content_hash TEXT CHECK (content_hash IS NULL OR length(content_hash) = 64),
    change_reason TEXT NOT NULL,
    provenance_event_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(resource_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_versions_unique_hashes
ON resource_versions(resource_id, metadata_hash, IFNULL(content_hash, ''));

CREATE INDEX IF NOT EXISTS idx_resources_source_id ON resources(source_id);
CREATE INDEX IF NOT EXISTS idx_resources_manifest_id ON resources(manifest_id);
CREATE INDEX IF NOT EXISTS idx_resource_versions_resource_id ON resource_versions(resource_id);

CREATE TABLE IF NOT EXISTS provenance_events (
    event_id TEXT PRIMARY KEY CHECK (event_id LIKE 'prov_%'),
    run_id TEXT REFERENCES collection_runs(run_id) ON DELETE SET NULL,
    source_id TEXT REFERENCES sources(source_id) ON DELETE RESTRICT,
    resource_id TEXT REFERENCES resources(resource_id) ON DELETE SET NULL,
    version_id TEXT REFERENCES resource_versions(version_id) ON DELETE SET NULL,
    manifest_id TEXT REFERENCES source_urls(manifest_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    acquisition_method TEXT NOT NULL CHECK (length(trim(acquisition_method)) > 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    source_url TEXT,
    final_url TEXT,
    status TEXT NOT NULL CHECK (length(trim(status)) > 0),
    http_status INTEGER,
    content_hash TEXT,
    metadata_hash TEXT,
    details_json TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provenance_events_resource_id ON provenance_events(resource_id);
CREATE INDEX IF NOT EXISTS idx_provenance_events_run_id ON provenance_events(run_id);
"""


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {str(row[1]) for row in rows}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_def: str) -> None:
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_def}')


def _migration_1_legacy_tables(conn: sqlite3.Connection) -> None:
    """Ensure legacy collector tables and any newly added columns exist without data loss."""
    conn.executescript(LEGACY_COLLECTOR_TABLES_SQL)
    _ensure_column(conn, "source_urls", "last_fetch_status", "TEXT")
    _ensure_column(conn, "source_urls", "last_fetch_error", "TEXT")
    _ensure_column(conn, "source_urls", "last_fetch_at", "TEXT")
    _ensure_column(conn, "source_urls", "source_id", "TEXT REFERENCES sources(source_id) ON DELETE SET NULL")
    _ensure_column(conn, "source_urls", "canonical_url", "TEXT")
    _ensure_column(conn, "source_urls", "imported_at", "TEXT")
    _ensure_column(conn, "source_urls", "updated_at", "TEXT")

    _ensure_column(conn, "documents", "metadata_hash", "TEXT")
    _ensure_column(conn, "documents", "resource_id", "TEXT")
    _ensure_column(conn, "documents", "version_id", "TEXT")


def _migration_2_canonical_schema(conn: sqlite3.Connection) -> None:
    """Create canonical knowledge store tables, constraints, and indexes."""
    conn.executescript(CANONICAL_SCHEMA_V2_SQL)


def _migration_3_seed_sources_and_backfill(conn: sqlite3.Connection) -> None:
    """Seed the 5 canonical sources and backfill any pre-existing legacy rows."""
    store = KnowledgeStore(conn=conn, auto_initialize=False)
    for src in CANONICAL_SOURCES:
        store.register_source(**src)

    # Backfill existing crawl_runs into collection_runs
    existing_runs = conn.execute(
        "SELECT run_id, started_at, finished_at, manifest_path, selected_count, "
        "fetched_count, discovered_count, skipped_count, error_count, status, notes "
        "FROM crawl_runs"
    ).fetchall()
    for run in existing_runs:
        conn.execute(
            """
            INSERT OR IGNORE INTO collection_runs(
                run_id, run_type, is_dry_run, started_at, finished_at, manifest_path,
                selected_count, fetched_count, discovered_count, skipped_count,
                error_count, status, notes
            ) VALUES (?, 'LIVE_COLLECTION', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run[0],
                run[1],
                run[2],
                run[3],
                run[4],
                run[5],
                run[6],
                run[7],
                run[8],
                run[9],
                run[10],
            ),
        )

    # Backfill existing source_urls rows if any exist in a pre-populated SQLite DB
    legacy_seeds = conn.execute(
        "SELECT manifest_id, person_source, url, page_resource_name, resource_type, "
        "domain_scope, verification_status, observed_from, information_to_inventory, "
        "priority, collection_decision, robots_sitemap_status, rights_status, last_checked "
        "FROM source_urls ORDER BY manifest_id"
    ).fetchall()
    if legacy_seeds:
        migration_run_id = "run-migration-backfill-v3"
        now = utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO collection_runs(
                run_id, run_type, is_dry_run, started_at, finished_at, manifest_path,
                selected_count, status, notes
            ) VALUES (?, 'MIGRATION', 1, ?, ?, 'sqlite:source_urls', ?, 'completed',
                      'Backfilled pre-existing source_urls into canonical resources table')
            """,
            (migration_run_id, now, now, len(legacy_seeds)),
        )
        imported = updated = skipped = rejected = 0
        for srow in legacy_seeds:
            raw_item = {
                "Manifest ID": srow[0],
                "Person / source": srow[1],
                "URL": srow[2],
                "Page / resource name": srow[3],
                "Resource type": srow[4],
                "Domain scope": srow[5],
                "Verification status": srow[6],
                "Observed from": srow[7],
                "Information to inventory": srow[8],
                "Priority": srow[9],
                "Collection decision": srow[10],
                "Robots / sitemap status": srow[11],
                "Rights status": srow[12],
                "Last checked": srow[13],
            }
            outcome, _ = store._ingest_manifest_record(
                raw_item,
                run_id=migration_run_id,
                acquisition_method="SQLITE_MIGRATION",
                actor="schema_migration_v3",
            )
            if outcome == "imported":
                imported += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                rejected += 1
        conn.execute(
            """
            UPDATE collection_runs
               SET imported_count=?, updated_count=?, skipped_count=?, rejected_count=?, finished_at=?
             WHERE run_id=?
            """,
            (imported, updated, skipped, rejected, utc_now(), migration_run_id),
        )

    # Backfill existing documents rows if any exist in a pre-populated SQLite DB
    legacy_docs = conn.execute(
        "SELECT document_id, manifest_id, canonical_url, title, authors, published_at, "
        "content_type, topic_tags, abstract_or_description, external_identifier, "
        "license_status, text_storage_status, fetched_at, content_hash, metadata_hash "
        "FROM documents ORDER BY document_id"
    ).fetchall()
    for doc in legacy_docs:
        (
            doc_id,
            manifest_id,
            canonical_url,
            title,
            authors,
            published_at,
            content_type,
            topic_tags,
            abstract_or_description,
            external_identifier,
            license_status,
            text_storage_status,
            fetched_at,
            content_hash,
             _existing_meta_hash,
        ) = doc
        source_id = store.resolve_or_register_source(
            person_source=None,
            canonical_url=canonical_url,
            manifest_id=manifest_id,
        )
        res = store.upsert_resource(
            source_id=source_id,
            canonical_url=canonical_url,
            title=title or canonical_url,
            resource_type=content_type or "Web page",
            manifest_id=manifest_id,
            authors=authors,
            published_at=published_at,
            abstract_or_description=abstract_or_description,
            topic_tags=topic_tags,
            external_identifier=external_identifier,
            license_status=license_status or "ALL_RIGHTS_RESERVED_METADATA_ONLY",
            text_storage_status=text_storage_status or "metadata-only",
            content_hash=content_hash,
            acquisition_method="SQLITE_MIGRATION",
            actor="schema_migration_v3",
            change_reason="legacy_document_backfill",
            discovered_at=fetched_at,
        )
        conn.execute(
            "UPDATE documents SET resource_id=?, version_id=?, metadata_hash=COALESCE(metadata_hash, ?) WHERE document_id=?",
            (res["resource_id"], res["current_version_id"], res["metadata_hash"], doc_id),
        )

    # Backfill existing crawl_audit rows into provenance_events if not already migrated
    audit_rows = conn.execute(
        "SELECT audit_id, endpoint_url, check_name, status, evidence, next_action, checked_at "
        "FROM crawl_audit ORDER BY audit_id"
    ).fetchall()
    for arow in audit_rows:
        audit_id, endpoint_url, check_name, status, evidence, next_action, checked_at = arow
        event_id = f"prov_audit_{audit_id:04d}"
        source_id = store.resolve_or_register_source(
            person_source=None,
            canonical_url=endpoint_url,
            manifest_id=None,
        )
        details = json.dumps(
            {
                "audit_id": audit_id,
                "check_name": check_name,
                "evidence": evidence,
                "next_action": next_action,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO provenance_events(
                event_id, source_id, event_type, acquisition_method, actor,
                source_url, status, details_json, recorded_at
            ) VALUES (?, ?, 'POLICY_CHECK', 'SQLITE_MIGRATION', 'crawl_audit_backfill', ?, ?, ?, ?)
            """,
            (
                event_id,
                source_id,
                endpoint_url,
                status or "recorded",
                details,
                checked_at or utc_now(),
            ),
        )


DISCOVERED_LINK_CANDIDATES_V4_SQL = """
CREATE TABLE IF NOT EXISTS discovered_link_candidates (
    candidate_id            TEXT PRIMARY KEY,
    referring_resource_id   TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    referring_source_id     TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    from_manifest_id        TEXT,
    from_url                TEXT NOT NULL,
    raw_href                TEXT NOT NULL,
    original_url            TEXT NOT NULL,
    normalized_url          TEXT NOT NULL,
    host_domain             TEXT NOT NULL,
    is_internal             INTEGER NOT NULL DEFAULT 0 CHECK (is_internal IN (0, 1)),
    domain_classification   TEXT NOT NULL CHECK (domain_classification IN ('INTERNAL', 'EXTERNAL')),
    target_scope            TEXT NOT NULL,
    recommended_decision    TEXT NOT NULL,
    policy_state            TEXT NOT NULL DEFAULT 'UNVERIFIED_EXTERNAL_CANDIDATE',
    review_status           TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
                                CHECK (review_status IN ('PENDING_REVIEW', 'APPROVED_FOR_MANIFEST', 'REJECTED', 'DEFERRED')),
    fetch_permission_granted INTEGER NOT NULL DEFAULT 0 CHECK (fetch_permission_granted IN (0, 1)),
    promoted_resource_id    TEXT REFERENCES resources(resource_id) ON DELETE SET NULL,
    link_text               TEXT,
    rel                     TEXT,
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    seen_count              INTEGER NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
    first_run_id            TEXT,
    last_run_id             TEXT,
    provenance_event_id     TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL,
    UNIQUE (referring_resource_id, normalized_url)
);

CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_resource
    ON discovered_link_candidates(referring_resource_id);
CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_source
    ON discovered_link_candidates(referring_source_id);
CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_url
    ON discovered_link_candidates(normalized_url);
CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_host
    ON discovered_link_candidates(host_domain, domain_classification);
CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_review
    ON discovered_link_candidates(review_status, fetch_permission_granted);
"""


def _migration_4_discovered_link_candidates(conn: sqlite3.Connection) -> None:
    """Create canonical `discovered_link_candidates` table and backfill existing `discovered_links`."""
    conn.executescript(DISCOVERED_LINK_CANDIDATES_V4_SQL)
    store = KnowledgeStore(conn=conn, auto_initialize=False)
    legacy_links = conn.execute(
        """
        SELECT d.from_manifest_id, d.link_url, d.link_text, d.rel,
               d.target_scope, d.decision, d.discovered_at,
               r.resource_id, r.source_id, r.canonical_url
          FROM discovered_links d
          JOIN resources r ON r.manifest_id = d.from_manifest_id
         ORDER BY d.discovery_id ASC
        """
    ).fetchall()
    for lrow in legacy_links:
        from_manifest_id = lrow[0]
        link_url = lrow[1]
        link_text = lrow[2]
        rel = lrow[3]
        target_scope = lrow[4] or "external-not-fetched"
        decision = lrow[5] or "skip-external-host"
        discovered_at = lrow[6] or utc_now()
        referring_resource_id = lrow[7]
        referring_source_id = lrow[8]
        from_url = lrow[9]

        scope_lower = str(target_scope).lower()
        from_host = (urlsplit(from_url).hostname or "").lower()
        target_host = (urlsplit(link_url).hostname or "").lower()
        is_internal = scope_lower.startswith(("in-scope", "same-host", "internal")) or (
            bool(from_host) and from_host == target_host
        )
        if is_internal:
            if "binary" in scope_lower:
                policy_state = "SAME_HOST_BINARY_CITATION_NO_FETCH"
            elif "outside-prefix" in scope_lower:
                policy_state = "SAME_HOST_OUTSIDE_PREFIX_NO_FETCH"
            else:
                policy_state = "IN_SCOPE_HTML_CANDIDATE_NO_AUTO_FETCH"
        else:
            policy_state = "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH"

        try:
            store.upsert_discovered_link_candidate(
                referring_resource_id=referring_resource_id,
                referring_source_id=referring_source_id,
                from_manifest_id=from_manifest_id,
                from_url=from_url,
                raw_href=link_url,
                original_url=link_url,
                normalized_url=link_url,
                link_text=link_text,
                rel=rel,
                is_internal=is_internal,
                target_scope=target_scope,
                recommended_decision=decision,
                policy_state=policy_state,
                review_status="PENDING_REVIEW",
                fetch_permission_granted=False,
                discovered_at=discovered_at,
                acquisition_method="SQLITE_MIGRATION_V4_BACKFILL",
                actor="migration_v4",
                record_provenance_event=True,
            )
        except ValueError:
            continue


DISCOVERED_LINK_CURATION_V5_SQL = """
CREATE TABLE discovered_link_candidates_v5 (
    candidate_id            TEXT PRIMARY KEY,
    referring_resource_id   TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    referring_source_id     TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    from_manifest_id        TEXT,
    from_url                TEXT NOT NULL,
    raw_href                TEXT NOT NULL,
    original_url            TEXT NOT NULL,
    normalized_url          TEXT NOT NULL,
    host_domain             TEXT NOT NULL,
    is_internal             INTEGER NOT NULL DEFAULT 0 CHECK (is_internal IN (0, 1)),
    domain_classification   TEXT NOT NULL CHECK (domain_classification IN ('INTERNAL', 'EXTERNAL')),
    target_scope            TEXT NOT NULL,
    recommended_decision    TEXT NOT NULL,
    policy_state            TEXT NOT NULL DEFAULT 'UNVERIFIED_EXTERNAL_CANDIDATE',
    review_status           TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
                                CHECK (review_status IN (
                                    'PENDING_REVIEW',
                                    'ACCEPTED_FOR_REFERENCE',
                                    'APPROVED_FOR_MANIFEST',
                                    'REJECTED',
                                    'DEFERRED'
                                )),
    fetch_permission_granted INTEGER NOT NULL DEFAULT 0 CHECK (fetch_permission_granted IN (0, 1)),
    promoted_resource_id    TEXT REFERENCES resources(resource_id) ON DELETE SET NULL,
    link_text               TEXT,
    rel                     TEXT,
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    seen_count              INTEGER NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
    first_run_id            TEXT,
    last_run_id             TEXT,
    provenance_event_id     TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL,
    reviewed_by             TEXT,
    reviewed_at             TEXT,
    review_reason           TEXT,
    review_count            INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    last_review_event_id    TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL,
    UNIQUE (referring_resource_id, normalized_url)
);

CREATE TABLE IF NOT EXISTS discovered_link_reviews (
    review_id               TEXT PRIMARY KEY,
    candidate_id            TEXT NOT NULL REFERENCES discovered_link_candidates(candidate_id) ON DELETE CASCADE,
    referring_resource_id   TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    referring_source_id     TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    original_url            TEXT NOT NULL,
    normalized_url          TEXT NOT NULL,
    domain_classification   TEXT NOT NULL CHECK (domain_classification IN ('INTERNAL', 'EXTERNAL')),
    previous_review_status  TEXT NOT NULL CHECK (previous_review_status IN (
                                'PENDING_REVIEW',
                                'ACCEPTED_FOR_REFERENCE',
                                'APPROVED_FOR_MANIFEST',
                                'REJECTED',
                                'DEFERRED'
                            )),
    new_review_status       TEXT NOT NULL CHECK (new_review_status IN (
                                'PENDING_REVIEW',
                                'ACCEPTED_FOR_REFERENCE',
                                'APPROVED_FOR_MANIFEST',
                                'REJECTED',
                                'DEFERRED'
                            )),
    reviewer                TEXT NOT NULL,
    review_reason           TEXT NOT NULL,
    reviewed_at             TEXT NOT NULL,
    fetch_permission_granted INTEGER NOT NULL DEFAULT 0 CHECK (fetch_permission_granted = 0),
    provenance_event_id     TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL
);
"""


def _migration_5_discovered_link_curation_reviews(conn: sqlite3.Connection) -> None:
    """Forward-only migration 5: expand `discovered_link_candidates` for curation decisions and create `discovered_link_reviews`."""
    conn.execute("DROP TABLE IF EXISTS discovered_link_candidates_v5")
    conn.execute(
        """
        CREATE TABLE discovered_link_candidates_v5 (
            candidate_id            TEXT PRIMARY KEY,
            referring_resource_id   TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
            referring_source_id     TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
            from_manifest_id        TEXT,
            from_url                TEXT NOT NULL,
            raw_href                TEXT NOT NULL,
            original_url            TEXT NOT NULL,
            normalized_url          TEXT NOT NULL,
            host_domain             TEXT NOT NULL,
            is_internal             INTEGER NOT NULL DEFAULT 0 CHECK (is_internal IN (0, 1)),
            domain_classification   TEXT NOT NULL CHECK (domain_classification IN ('INTERNAL', 'EXTERNAL')),
            target_scope            TEXT NOT NULL,
            recommended_decision    TEXT NOT NULL,
            policy_state            TEXT NOT NULL DEFAULT 'UNVERIFIED_EXTERNAL_CANDIDATE',
            review_status           TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
                                        CHECK (review_status IN (
                                            'PENDING_REVIEW',
                                            'ACCEPTED_FOR_REFERENCE',
                                            'APPROVED_FOR_MANIFEST',
                                            'REJECTED',
                                            'DEFERRED'
                                        )),
            fetch_permission_granted INTEGER NOT NULL DEFAULT 0 CHECK (fetch_permission_granted IN (0, 1)),
            promoted_resource_id    TEXT REFERENCES resources(resource_id) ON DELETE SET NULL,
            link_text               TEXT,
            rel                     TEXT,
            first_seen_at           TEXT NOT NULL,
            last_seen_at            TEXT NOT NULL,
            seen_count              INTEGER NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
            first_run_id            TEXT,
            last_run_id             TEXT,
            provenance_event_id     TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL,
            reviewed_by             TEXT,
            reviewed_at             TEXT,
            review_reason           TEXT,
            review_count            INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
            last_review_event_id    TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL,
            UNIQUE (referring_resource_id, normalized_url)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO discovered_link_candidates_v5 (
            candidate_id, referring_resource_id, referring_source_id,
            from_manifest_id, from_url, raw_href, original_url,
            normalized_url, host_domain, is_internal, domain_classification,
            target_scope, recommended_decision, policy_state, review_status,
            fetch_permission_granted, promoted_resource_id, link_text, rel,
            first_seen_at, last_seen_at, seen_count,
            first_run_id, last_run_id, provenance_event_id,
            reviewed_by, reviewed_at, review_reason, review_count, last_review_event_id
        )
        SELECT
            candidate_id, referring_resource_id, referring_source_id,
            from_manifest_id, from_url, raw_href, original_url,
            normalized_url, host_domain, is_internal, domain_classification,
            target_scope, recommended_decision, policy_state, review_status,
            fetch_permission_granted, promoted_resource_id, link_text, rel,
            first_seen_at, last_seen_at, seen_count,
            first_run_id, last_run_id, provenance_event_id,
            NULL, NULL, NULL, 0, NULL
          FROM discovered_link_candidates
        """
    )
    conn.execute("DROP TABLE discovered_link_candidates")
    conn.execute("ALTER TABLE discovered_link_candidates_v5 RENAME TO discovered_link_candidates")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_resource ON discovered_link_candidates(referring_resource_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_source ON discovered_link_candidates(referring_source_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_url ON discovered_link_candidates(normalized_url)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_host ON discovered_link_candidates(host_domain, domain_classification)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_candidates_review ON discovered_link_candidates(review_status, fetch_permission_granted)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovered_link_reviews (
            review_id               TEXT PRIMARY KEY,
            candidate_id            TEXT NOT NULL REFERENCES discovered_link_candidates(candidate_id) ON DELETE CASCADE,
            referring_resource_id   TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
            referring_source_id     TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
            original_url            TEXT NOT NULL,
            normalized_url          TEXT NOT NULL,
            domain_classification   TEXT NOT NULL CHECK (domain_classification IN ('INTERNAL', 'EXTERNAL')),
            previous_review_status  TEXT NOT NULL CHECK (previous_review_status IN (
                                        'PENDING_REVIEW',
                                        'ACCEPTED_FOR_REFERENCE',
                                        'APPROVED_FOR_MANIFEST',
                                        'REJECTED',
                                        'DEFERRED'
                                    )),
            new_review_status       TEXT NOT NULL CHECK (new_review_status IN (
                                        'PENDING_REVIEW',
                                        'ACCEPTED_FOR_REFERENCE',
                                        'APPROVED_FOR_MANIFEST',
                                        'REJECTED',
                                        'DEFERRED'
                                    )),
            reviewer                TEXT NOT NULL,
            review_reason           TEXT NOT NULL,
            reviewed_at             TEXT NOT NULL,
            fetch_permission_granted INTEGER NOT NULL DEFAULT 0 CHECK (fetch_permission_granted = 0),
            provenance_event_id     TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_reviews_candidate ON discovered_link_reviews(candidate_id, reviewed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discovered_link_reviews_status ON discovered_link_reviews(new_review_status, reviewed_at)"
    )


TRANSCRIPTS_AND_SEGMENTS_V6_SQL = """
CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id               TEXT PRIMARY KEY,
    resource_id                 TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    source_id                   TEXT NOT NULL REFERENCES sources(source_id),
    media_canonical_url         TEXT NOT NULL,
    language_bcp47              TEXT NOT NULL,
    track_kind                  TEXT NOT NULL CHECK (track_kind IN (
                                    'PRIMARY',
                                    'TRANSLATION',
                                    'DESCRIPTIVE_AUDIO_TEXT'
                                )),
    origin_type                 TEXT NOT NULL CHECK (origin_type IN (
                                    'AUTHOR_PROVIDED',
                                    'PUBLISHER_CLOSED_CAPTION',
                                    'COMMUNITY_CONTRIBUTED',
                                    'LOCAL_ASR_GENERATED',
                                    'MANUAL_CURATED_IMPORT'
                                )),
    acquisition_method          TEXT NOT NULL CHECK (acquisition_method IN (
                                    'FIRST_PARTY_CURATED_IMPORT',
                                    'OPEN_LICENSED_DIRECT_CAPTION',
                                    'WIKIMEDIA_TIMEDTEXT_API',
                                    'OFFICIAL_PARTNER_CAPTION_API',
                                    'AUTHORIZED_LOCAL_ASR',
                                    'METADATA_AND_LINK_ONLY'
                                )),
    caption_format              TEXT NOT NULL CHECK (caption_format IN (
                                    'WEBVTT',
                                    'SRT',
                                    'PLAIN_TEXT',
                                    'TTML',
                                    'HTML_INLINE',
                                    'MEDIAWIKI_TIMEDTEXT',
                                    'ASR_JSON',
                                    'NONE'
                                )),
    speaker_attribution_quality TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (speaker_attribution_quality IN (
                                    'VERIFIED_SINGLE_SPEAKER',
                                    'VERIFIED_MULTI_SPEAKER',
                                    'DIARIZED_UNVERIFIED',
                                    'UNATTRIBUTED',
                                    'UNKNOWN'
                                )),
    license_status              TEXT NOT NULL CHECK (license_status IN (
                                    'VERIFIED_PERMISSIVE',
                                    'ALL_RIGHTS_RESERVED',
                                    'UNKNOWN',
                                    'RESTRICTED_PROPRIETARY'
                                )),
    license_spdx                TEXT,
    authorization_basis         TEXT NOT NULL CHECK (authorization_basis IN (
                                    'OPEN_LICENSE_VERIFIED',
                                    'FIRST_PARTY_OWNED',
                                    'FIRST_PARTY_OWNED_MEDIA',
                                    'WRITTEN_PERMISSION',
                                    'WRITTEN_RIGHTS_HOLDER_PERMISSION',
                                    'NONE'
                                )),
    permits_transcript_storage  INTEGER NOT NULL DEFAULT 0 CHECK (permits_transcript_storage IN (0, 1)),
    permits_verbatim_excerpts   INTEGER NOT NULL DEFAULT 0 CHECK (permits_verbatim_excerpts IN (0, 1)),
    permits_ai_synthesis        INTEGER NOT NULL DEFAULT 1 CHECK (permits_ai_synthesis IN (0, 1)),
    attribution_text            TEXT NOT NULL CHECK (length(trim(attribution_text)) > 0),
    ingestion_state             TEXT NOT NULL CHECK (ingestion_state IN (
                                    'DISCOVERED',
                                    'RIGHTS_PENDING',
                                    'BLOCKED_BY_POLICY',
                                    'METADATA_ONLY_LINKED',
                                    'READY_FOR_INGESTION',
                                    'INGESTED',
                                    'NEEDS_REVIEW',
                                    'SUPERSEDED',
                                    'WITHDRAWN_OR_TOMBSTONED'
                                )),
    transcript_storage_status   TEXT NOT NULL DEFAULT 'NOT_STORED' CHECK (transcript_storage_status IN (
                                    'STORED_TIMED_SEGMENTS',
                                    'STORED_PLAIN_TEXT',
                                    'METADATA_AND_LINK_ONLY',
                                    'BLOCKED_NO_AUTHORIZATION',
                                    'TOMBSTONED_ON_TAKEDOWN',
                                    'NOT_STORED'
                                )),
    block_or_review_reason      TEXT,
    latest_transcript_version_id TEXT,
    segment_count               INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
    duration_ms                 INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    UNIQUE (resource_id, language_bcp47, track_kind),
    CHECK (
        permits_transcript_storage = 0 OR (
            license_status = 'VERIFIED_PERMISSIVE'
            AND authorization_basis IN (
                'OPEN_LICENSE_VERIFIED',
                'FIRST_PARTY_OWNED',
                'FIRST_PARTY_OWNED_MEDIA',
                'WRITTEN_PERMISSION',
                'WRITTEN_RIGHTS_HOLDER_PERMISSION'
            )
            AND acquisition_method != 'METADATA_AND_LINK_ONLY'
            AND ingestion_state NOT IN (
                'BLOCKED_BY_POLICY',
                'METADATA_ONLY_LINKED',
                'WITHDRAWN_OR_TOMBSTONED'
            )
        )
    ),
    CHECK (permits_verbatim_excerpts = 0 OR permits_transcript_storage = 1),
    CHECK (
        transcript_storage_status NOT IN ('STORED_TIMED_SEGMENTS', 'STORED_PLAIN_TEXT')
        OR permits_transcript_storage = 1
    )
);

CREATE TABLE IF NOT EXISTS transcript_versions (
    transcript_version_id       TEXT PRIMARY KEY,
    transcript_id               TEXT NOT NULL REFERENCES transcripts(transcript_id) ON DELETE CASCADE,
    version_number              INTEGER NOT NULL CHECK (version_number >= 1),
    raw_content_sha256          TEXT NOT NULL,
    normalized_text_sha256      TEXT NOT NULL,
    normalizer_version          TEXT NOT NULL,
    asr_model_name              TEXT,
    asr_model_version           TEXT,
    asr_compute_device          TEXT,
    source_caption_url          TEXT,
    segment_count               INTEGER NOT NULL CHECK (segment_count >= 0),
    word_count                  INTEGER NOT NULL CHECK (word_count >= 0),
    char_count                  INTEGER NOT NULL CHECK (char_count >= 0),
    mean_confidence             REAL CHECK (mean_confidence IS NULL OR (mean_confidence >= 0.0 AND mean_confidence <= 1.0)),
    low_confidence_segment_count INTEGER NOT NULL DEFAULT 0 CHECK (low_confidence_segment_count >= 0),
    status                      TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'TOMBSTONED')),
    created_at                  TEXT NOT NULL,
    UNIQUE (transcript_id, version_number),
    UNIQUE (transcript_id, normalized_text_sha256)
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    segment_id                  TEXT PRIMARY KEY,
    transcript_id               TEXT NOT NULL REFERENCES transcripts(transcript_id) ON DELETE CASCADE,
    transcript_version_id       TEXT NOT NULL REFERENCES transcript_versions(transcript_version_id) ON DELETE CASCADE,
    sequence_index              INTEGER NOT NULL CHECK (sequence_index >= 0),
    start_ms                    INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms                      INTEGER NOT NULL CHECK (end_ms >= start_ms),
    original_segment_text       TEXT NOT NULL CHECK (length(trim(original_segment_text)) > 0),
    normalized_segment_text     TEXT NOT NULL CHECK (length(trim(normalized_segment_text)) > 0),
    speaker_label               TEXT,
    speaker_confidence          REAL CHECK (speaker_confidence IS NULL OR (speaker_confidence >= 0.0 AND speaker_confidence <= 1.0)),
    token_confidence_mean       REAL CHECK (token_confidence_mean IS NULL OR (token_confidence_mean >= 0.0 AND token_confidence_mean <= 1.0)),
    token_confidence_min        REAL CHECK (token_confidence_min IS NULL OR (token_confidence_min >= 0.0 AND token_confidence_min <= 1.0)),
    is_low_confidence           INTEGER NOT NULL DEFAULT 0 CHECK (is_low_confidence IN (0, 1)),
    quality_flags_json          TEXT NOT NULL DEFAULT '[]',
    deep_link_url               TEXT NOT NULL,
    segment_sha256              TEXT NOT NULL,
    UNIQUE (transcript_version_id, sequence_index)
);

CREATE TABLE IF NOT EXISTS transcript_syntheses (
    synthesis_id                TEXT PRIMARY KEY,
    transcript_id               TEXT NOT NULL REFERENCES transcripts(transcript_id) ON DELETE CASCADE,
    transcript_version_id       TEXT REFERENCES transcript_versions(transcript_version_id) ON DELETE SET NULL,
    synthesis_type              TEXT NOT NULL CHECK (synthesis_type IN (
                                    'STRUCTURED_SUMMARY',
                                    'SECTION_OUTLINE',
                                    'GLOSSARY_TERMS',
                                    'LEARNING_OBJECTIVES',
                                    'PREREQUISITES'
                                )),
    generator_type              TEXT NOT NULL CHECK (generator_type IN (
                                    'HUMAN_CURATED',
                                    'RULE_EXTRACTOR',
                                    'LLM_ASSISTED_CURATED'
                                )),
    generator_model             TEXT,
    content_markdown            TEXT NOT NULL CHECK (length(trim(content_markdown)) > 0),
    referenced_time_ranges_json TEXT NOT NULL DEFAULT '[]',
    is_original_transformative  INTEGER NOT NULL DEFAULT 1 CHECK (is_original_transformative = 1),
    contains_verbatim_transcript INTEGER NOT NULL DEFAULT 0 CHECK (contains_verbatim_transcript = 0),
    review_status               TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK (review_status IN (
                                    'PENDING_REVIEW',
                                    'APPROVED',
                                    'REJECTED'
                                )),
    created_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_segment_links (
    link_id                     TEXT PRIMARY KEY,
    segment_id                  TEXT NOT NULL REFERENCES transcript_segments(segment_id) ON DELETE CASCADE,
    target_type                 TEXT NOT NULL CHECK (target_type IN (
                                    'CONCEPT',
                                    'PAPER_RESOURCE',
                                    'LEARNING_PATH_MODULE',
                                    'EQUATION',
                                    'KNOWLEDGE_CHUNK'
                                )),
    target_id                   TEXT NOT NULL,
    relation_type               TEXT NOT NULL CHECK (relation_type IN (
                                    'EXPLAINS',
                                    'INTRODUCES',
                                    'DERIVES',
                                    'CITES_PAPER',
                                    'PREREQUISITE_FOR',
                                    'EXAMPLE_OF'
                                )),
    confidence                  REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_by                  TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    UNIQUE (segment_id, target_type, target_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_transcripts_resource_lang ON transcripts(resource_id, language_bcp47, track_kind);
CREATE INDEX IF NOT EXISTS idx_transcripts_state_license ON transcripts(ingestion_state, license_status);
CREATE INDEX IF NOT EXISTS idx_transcripts_source_id ON transcripts(source_id);
CREATE INDEX IF NOT EXISTS idx_transcript_versions_transcript ON transcript_versions(transcript_id, version_number);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_version_seq ON transcript_segments(transcript_version_id, sequence_index);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_time ON transcript_segments(transcript_id, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS idx_transcript_syntheses_transcript ON transcript_syntheses(transcript_id, synthesis_type);
CREATE INDEX IF NOT EXISTS idx_transcript_segment_links_target ON transcript_segment_links(target_type, target_id);

CREATE TRIGGER IF NOT EXISTS trg_transcript_versions_require_authorized_parent_insert
BEFORE INSERT ON transcript_versions
BEGIN
    SELECT CASE
        WHEN (
            SELECT permits_transcript_storage
              FROM transcripts
             WHERE transcript_id = NEW.transcript_id
        ) != 1
        OR (
            SELECT ingestion_state
              FROM transcripts
             WHERE transcript_id = NEW.transcript_id
        ) IN (
            'DISCOVERED',
            'RIGHTS_PENDING',
            'BLOCKED_BY_POLICY',
            'METADATA_ONLY_LINKED',
            'WITHDRAWN_OR_TOMBSTONED'
        )
        THEN RAISE(ABORT, 'Policy violation: cannot store transcript version when parent transcript does not permit transcript storage')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_versions_immutable_update
BEFORE UPDATE ON transcript_versions
BEGIN
    SELECT CASE
        WHEN NEW.transcript_version_id != OLD.transcript_version_id
          OR NEW.transcript_id != OLD.transcript_id
          OR NEW.version_number != OLD.version_number
          OR NEW.raw_content_sha256 != OLD.raw_content_sha256
          OR NEW.normalized_text_sha256 != OLD.normalized_text_sha256
          OR NEW.normalizer_version != OLD.normalizer_version
          OR NEW.segment_count != OLD.segment_count
          OR NEW.word_count != OLD.word_count
          OR NEW.char_count != OLD.char_count
          OR NEW.created_at != OLD.created_at
        THEN RAISE(ABORT, 'Transcript versions are immutable; only status transitions are permitted')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_segments_require_authorized_parent_insert
BEFORE INSERT ON transcript_segments
BEGIN
    SELECT CASE
        WHEN (
            SELECT permits_transcript_storage
              FROM transcripts
             WHERE transcript_id = NEW.transcript_id
        ) != 1
        THEN RAISE(ABORT, 'Policy violation: cannot store transcript segments when parent transcript does not permit transcript storage')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_segments_immutable_original
BEFORE UPDATE ON transcript_segments
BEGIN
    SELECT CASE
        WHEN NEW.segment_id != OLD.segment_id
          OR NEW.transcript_id != OLD.transcript_id
          OR NEW.transcript_version_id != OLD.transcript_version_id
          OR NEW.sequence_index != OLD.sequence_index
          OR NEW.start_ms != OLD.start_ms
          OR NEW.end_ms != OLD.end_ms
          OR NEW.original_segment_text != OLD.original_segment_text
        THEN RAISE(ABORT, 'Transcript segment original text and timestamps are immutable')
    END;
END;
"""


def _migration_6_transcripts_and_segments(conn: sqlite3.Connection) -> None:
    """Forward-only migration 6: create transcript, version, segment, synthesis, and segment-link tables."""
    conn.executescript(TRANSCRIPTS_AND_SEGMENTS_V6_SQL)


TRANSCRIPT_AUTHORIZATION_ATTESTATIONS_V7_SQL = """
CREATE TABLE IF NOT EXISTS transcript_authorization_attestations (
    attestation_id              TEXT PRIMARY KEY,
    resource_id                 TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    transcript_id               TEXT REFERENCES transcripts(transcript_id) ON DELETE SET NULL,
    language_bcp47              TEXT,
    track_kind                  TEXT CHECK (
                                    track_kind IS NULL OR track_kind IN (
                                        'PRIMARY',
                                        'TRANSLATION',
                                        'DESCRIPTIVE_AUDIO_TEXT'
                                    )
                                ),
    authorization_basis         TEXT NOT NULL CHECK (
                                    authorization_basis IN (
                                        'OPEN_LICENSE_VERIFIED',
                                        'PUBLIC_DOMAIN_VERIFIED',
                                        'FIRST_PARTY_OWNED',
                                        'FIRST_PARTY_OWNED_MEDIA',
                                        'WRITTEN_PERMISSION',
                                        'WRITTEN_RIGHTS_HOLDER_PERMISSION',
                                        'NONE'
                                    )
                                ),
    license_status              TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (
                                    license_status IN (
                                        'VERIFIED_PERMISSIVE',
                                        'ALL_RIGHTS_RESERVED',
                                        'UNKNOWN',
                                        'RESTRICTED_PROPRIETARY'
                                    )
                                ),
    license_spdx                TEXT,
    evidence_type               TEXT NOT NULL CHECK (
                                    evidence_type IN (
                                        'SPDX_LICENSE_URL',
                                        'OFFICIAL_RIGHTS_STATEMENT',
                                        'WRITTEN_PERMISSION_REF',
                                        'FIRST_PARTY_POLICY_REF',
                                        'PUBLIC_DOMAIN_DEDICATION',
                                        'OTHER_VERIFIABLE_EVIDENCE',
                                        'CREATOR_LICENSE_PAGE',
                                        'WRITTEN_PERMISSION_RECORD',
                                        'PUBLIC_DOMAIN_DECLARATION',
                                        'FIRST_PARTY_OWNERSHIP_RECORD'
                                    )
                                ),
    evidence_reference          TEXT NOT NULL CHECK (TRIM(evidence_reference) != ''),
    evidence_url                TEXT,
    evidence_sha256             TEXT CHECK (evidence_sha256 IS NULL OR length(evidence_sha256) = 64),
    permits_metadata_indexing   INTEGER NOT NULL DEFAULT 1 CHECK (permits_metadata_indexing IN (0, 1)),
    permits_transcript_storage  INTEGER NOT NULL DEFAULT 0 CHECK (permits_transcript_storage IN (0, 1)),
    permits_verbatim_excerpts   INTEGER NOT NULL DEFAULT 0 CHECK (permits_verbatim_excerpts IN (0, 1)),
    permits_ai_synthesis        INTEGER NOT NULL DEFAULT 0 CHECK (permits_ai_synthesis IN (0, 1)),
    permits_redistribution      INTEGER NOT NULL DEFAULT 0 CHECK (permits_redistribution IN (0, 1)),
    reviewed_by                 TEXT NOT NULL CHECK (TRIM(reviewed_by) != ''),
    reviewed_at                 TEXT NOT NULL,
    verification_status         TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK (
                                    verification_status IN (
                                        'VERIFIED',
                                        'PENDING_REVIEW',
                                        'CONTRADICTORY',
                                        'REJECTED',
                                        'WITHDRAWN',
                                        'EXPIRED',
                                        'UNKNOWN'
                                    )
                                ),
    review_notes                TEXT,
    expires_at                  TEXT,
    withdrawn_at                TEXT,
    withdrawn_by                TEXT,
    withdrawal_reason           TEXT,
    revision_number             INTEGER NOT NULL DEFAULT 1 CHECK (revision_number >= 1),
    provenance_event_id         TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_attestations_resource
    ON transcript_authorization_attestations(resource_id, verification_status);
CREATE INDEX IF NOT EXISTS idx_transcript_attestations_transcript
    ON transcript_authorization_attestations(transcript_id);
"""

TRANSCRIPT_AUTHORIZATION_ATTESTATIONS_V7_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_transcript_attestations_prevent_delete
BEFORE DELETE ON transcript_authorization_attestations
BEGIN
    SELECT RAISE(ABORT, 'Audit violation: transcript_authorization_attestations rows cannot be deleted; use WITHDRAWN status instead');
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_attestations_verified_gate_insert
BEFORE INSERT ON transcript_authorization_attestations
BEGIN
    SELECT CASE
        WHEN NEW.verification_status = 'VERIFIED'
          AND NEW.permits_transcript_storage = 1
          AND (
              NEW.license_status != 'VERIFIED_PERMISSIVE'
              OR NEW.authorization_basis = 'NONE'
              OR (NEW.authorization_basis = 'OPEN_LICENSE_VERIFIED' AND (NEW.license_spdx IS NULL OR TRIM(NEW.license_spdx) = ''))
              OR NEW.withdrawn_at IS NOT NULL
          )
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation granting transcript storage requires VERIFIED_PERMISSIVE license, valid authorization_basis, and non-withdrawn status')
        WHEN NEW.verification_status = 'VERIFIED'
          AND NEW.permits_transcript_storage = 0
          AND (NEW.permits_verbatim_excerpts = 1 OR NEW.permits_redistribution = 1)
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation cannot grant verbatim excerpts or redistribution when permits_transcript_storage is 0')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_attestations_verified_gate_update
BEFORE UPDATE ON transcript_authorization_attestations
BEGIN
    SELECT CASE
        WHEN NEW.verification_status = 'VERIFIED'
          AND NEW.permits_transcript_storage = 1
          AND (
              NEW.license_status != 'VERIFIED_PERMISSIVE'
              OR NEW.authorization_basis = 'NONE'
              OR (NEW.authorization_basis = 'OPEN_LICENSE_VERIFIED' AND (NEW.license_spdx IS NULL OR TRIM(NEW.license_spdx) = ''))
              OR NEW.withdrawn_at IS NOT NULL
          )
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation granting transcript storage requires VERIFIED_PERMISSIVE license, valid authorization_basis, and non-withdrawn status')
        WHEN NEW.verification_status = 'VERIFIED'
          AND NEW.permits_transcript_storage = 0
          AND (NEW.permits_verbatim_excerpts = 1 OR NEW.permits_redistribution = 1)
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation cannot grant verbatim excerpts or redistribution when permits_transcript_storage is 0')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcripts_require_attestation_insert
BEFORE INSERT ON transcripts
BEGIN
    SELECT CASE
        WHEN NEW.permits_transcript_storage = 1
          AND (
              NEW.authorization_attestation_id IS NULL
              OR NOT EXISTS (
                  SELECT 1
                    FROM transcript_authorization_attestations a
                   WHERE a.attestation_id = NEW.authorization_attestation_id
                     AND a.resource_id = NEW.resource_id
                     AND (a.transcript_id IS NULL OR a.transcript_id = NEW.transcript_id)
                     AND (a.language_bcp47 IS NULL OR a.language_bcp47 = NEW.language_bcp47)
                     AND (a.track_kind IS NULL OR a.track_kind = NEW.track_kind)
                     AND a.verification_status = 'VERIFIED'
                     AND a.withdrawn_at IS NULL
                     AND a.permits_transcript_storage = 1
                     AND (NEW.permits_verbatim_excerpts = 0 OR a.permits_verbatim_excerpts = 1)
                     AND (NEW.permits_redistribution = 0 OR a.permits_redistribution = 1)
              )
          )
        THEN RAISE(ABORT, 'Policy violation: transcripts.permits_transcript_storage=1 requires a VERIFIED, non-withdrawn transcript_authorization_attestations record matching resource_id and requested scope')
        WHEN NEW.permits_transcript_storage = 0
          AND (NEW.permits_verbatim_excerpts = 1 OR NEW.permits_redistribution = 1)
        THEN RAISE(ABORT, 'Policy violation: cannot enable verbatim excerpts or redistribution when permits_transcript_storage is 0')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcripts_require_attestation_update
BEFORE UPDATE ON transcripts
BEGIN
    SELECT CASE
        WHEN NEW.permits_transcript_storage = 1
          AND (
              NEW.authorization_attestation_id IS NULL
              OR NOT EXISTS (
                  SELECT 1
                    FROM transcript_authorization_attestations a
                   WHERE a.attestation_id = NEW.authorization_attestation_id
                     AND a.resource_id = NEW.resource_id
                     AND (a.transcript_id IS NULL OR a.transcript_id = NEW.transcript_id)
                     AND (a.language_bcp47 IS NULL OR a.language_bcp47 = NEW.language_bcp47)
                     AND (a.track_kind IS NULL OR a.track_kind = NEW.track_kind)
                     AND a.verification_status = 'VERIFIED'
                     AND a.withdrawn_at IS NULL
                     AND a.permits_transcript_storage = 1
                     AND (NEW.permits_verbatim_excerpts = 0 OR a.permits_verbatim_excerpts = 1)
                     AND (NEW.permits_redistribution = 0 OR a.permits_redistribution = 1)
              )
          )
        THEN RAISE(ABORT, 'Policy violation: transcripts.permits_transcript_storage=1 requires a VERIFIED, non-withdrawn transcript_authorization_attestations record matching resource_id and requested scope')
        WHEN NEW.permits_transcript_storage = 0
          AND (NEW.permits_verbatim_excerpts = 1 OR NEW.permits_redistribution = 1)
        THEN RAISE(ABORT, 'Policy violation: cannot enable verbatim excerpts or redistribution when permits_transcript_storage is 0')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_versions_require_verified_attestation_insert
BEFORE INSERT ON transcript_versions
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
              JOIN transcript_authorization_attestations a
                ON a.attestation_id = t.authorization_attestation_id
             WHERE t.transcript_id = NEW.transcript_id
               AND t.permits_transcript_storage = 1
               AND a.resource_id = t.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND a.permits_transcript_storage = 1
        )
        THEN RAISE(ABORT, 'Policy violation: cannot store transcript version without a VERIFIED, non-withdrawn authorization attestation')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_transcript_segments_require_verified_attestation_insert
BEFORE INSERT ON transcript_segments
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
              JOIN transcript_authorization_attestations a
                ON a.attestation_id = t.authorization_attestation_id
             WHERE t.transcript_id = NEW.transcript_id
               AND t.permits_transcript_storage = 1
               AND a.resource_id = t.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND a.permits_transcript_storage = 1
        )
        THEN RAISE(ABORT, 'Policy violation: cannot store transcript segments without a VERIFIED, non-withdrawn authorization attestation')
    END;
END;
"""


def _migration_7_transcript_authorization_attestations(conn: sqlite3.Connection) -> None:
    """Forward-only migration 7: create transcript_authorization_attestations, link transcripts, backfill v6 rows, and install triggers."""
    conn.executescript(TRANSCRIPT_AUTHORIZATION_ATTESTATIONS_V7_SQL)
    _ensure_column(
        conn,
        "transcripts",
        "authorization_attestation_id",
        "TEXT REFERENCES transcript_authorization_attestations(attestation_id) ON DELETE SET NULL",
    )
    _ensure_column(
        conn,
        "transcripts",
        "permits_redistribution",
        "INTEGER NOT NULL DEFAULT 0 CHECK (permits_redistribution IN (0, 1))",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcripts_attestation ON transcripts(authorization_attestation_id)"
    )

    # Backfill migration-generated verified attestations for any pre-existing v6 transcripts that already permitted storage
    existing_authorized = conn.execute(
        """
        SELECT transcript_id, resource_id, language_bcp47, track_kind,
               license_status, license_spdx, authorization_basis,
               permits_transcript_storage, permits_verbatim_excerpts,
               permits_ai_synthesis, attribution_text, media_canonical_url,
               created_at, updated_at
          FROM transcripts
         WHERE permits_transcript_storage = 1
           AND authorization_attestation_id IS NULL
        """
    ).fetchall()
    now = utc_now()
    for row in existing_authorized:
        tr_id = str(row[0])
        res_id = str(row[1])
        lang = str(row[2])
        kind = str(row[3])
        lic_status = str(row[4])
        lic_spdx = row[5]
        auth_basis = str(row[6])
        perm_storage = int(row[7])
        perm_excerpts = int(row[8])
        perm_synth = int(row[9])
        attr_text = str(row[10])
        media_url = str(row[11])
        created = str(row[12] or now)
        ev_type = (
            "SPDX_LICENSE_URL"
            if auth_basis == "OPEN_LICENSE_VERIFIED"
            else (
                "WRITTEN_PERMISSION_REF"
                if auth_basis in ("WRITTEN_PERMISSION", "WRITTEN_RIGHTS_HOLDER_PERMISSION")
                else "FIRST_PARTY_POLICY_REF"
            )
        )
        ev_ref = f"migration_v7_backfill:{tr_id}:{attr_text}"
        att_id = compute_transcript_attestation_id(
            resource_id=res_id,
            authorization_basis=auth_basis,
            evidence_type=ev_type,
            evidence_reference=ev_ref,
            reviewed_by="migration_0007_backfill",
            transcript_id=tr_id,
            language_bcp47=lang,
            track_kind=kind,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO transcript_authorization_attestations (
                attestation_id, resource_id, transcript_id, language_bcp47, track_kind,
                authorization_basis, license_status, license_spdx,
                evidence_type, evidence_reference, evidence_url, evidence_sha256,
                permits_metadata_indexing, permits_transcript_storage,
                permits_verbatim_excerpts, permits_ai_synthesis, permits_redistribution,
                reviewed_by, reviewed_at, verification_status, review_notes,
                revision_number, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, 0, 'migration_0007_backfill', ?, 'VERIFIED', 'Backfilled from schema v6 authorized transcript record', 1, ?, ?)
            """,
            (
                att_id,
                res_id,
                tr_id,
                lang,
                kind,
                auth_basis,
                lic_status,
                lic_spdx,
                ev_type,
                ev_ref,
                media_url,
                perm_storage,
                perm_excerpts,
                perm_synth,
                created,
                created,
                now,
            ),
        )
        conn.execute(
            "UPDATE transcripts SET authorization_attestation_id = ? WHERE transcript_id = ?",
            (att_id, tr_id),
        )

    conn.executescript(TRANSCRIPT_AUTHORIZATION_ATTESTATIONS_V7_TRIGGERS_SQL)


TRANSCRIPT_AUTHORIZATION_HARDENING_V8_SQL = """
-- Migration 8: Harden database-level transcript authorization triggers against direct-SQL bypasses

DROP TRIGGER IF EXISTS trg_transcript_attestations_verified_gate_insert;
CREATE TRIGGER trg_transcript_attestations_verified_gate_insert
BEFORE INSERT ON transcript_authorization_attestations
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.expires_at IS NOT NULL AND datetime(NEW.expires_at) IS NULL
        THEN RAISE(ABORT, 'transcript_authorization_attestations.expires_at must be a valid ISO-8601 datetime')
        WHEN NEW.permits_transcript_storage = 1 AND (
            NEW.verification_status != 'VERIFIED'
            OR NEW.permits_metadata_indexing = 0
            OR NEW.license_status != 'VERIFIED_PERMISSIVE'
            OR NEW.authorization_basis NOT IN (
                'OPEN_LICENSE_VERIFIED',
                'PUBLIC_DOMAIN_VERIFIED',
                'FIRST_PARTY_OWNED',
                'FIRST_PARTY_OWNED_MEDIA',
                'WRITTEN_PERMISSION',
                'WRITTEN_RIGHTS_HOLDER_PERMISSION'
            )
            OR NEW.withdrawn_at IS NOT NULL
            OR (NEW.expires_at IS NOT NULL AND datetime(NEW.expires_at) <= datetime('now'))
        )
        THEN RAISE(ABORT, 'Policy violation: attestation granting permits_transcript_storage=1 requires verification_status=VERIFIED, permits_metadata_indexing=1, VERIFIED_PERMISSIVE license, authorized basis, non-withdrawn status, and non-expired expires_at')
        WHEN NEW.permits_transcript_storage = 0
             AND (NEW.permits_verbatim_excerpts = 1 OR NEW.permits_redistribution = 1)
        THEN RAISE(ABORT, 'Policy violation: attestation cannot grant verbatim excerpts or redistribution when permits_transcript_storage is 0')
        WHEN NEW.verification_status = 'VERIFIED'
             AND (
                 NEW.withdrawn_at IS NOT NULL
                 OR (NEW.expires_at IS NOT NULL AND datetime(NEW.expires_at) <= datetime('now'))
             )
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation cannot be withdrawn or already expired')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.authorization_basis IN ('OPEN_LICENSE_VERIFIED', 'PUBLIC_DOMAIN_VERIFIED')
             AND NEW.license_status IN ('ALL_RIGHTS_RESERVED', 'RESTRICTED_PROPRIETARY')
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation has contradictory authorization_basis and license_status')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.permits_transcript_storage = 1
             AND NEW.authorization_basis = 'OPEN_LICENSE_VERIFIED'
             AND (NEW.license_spdx IS NULL OR length(trim(NEW.license_spdx)) = 0)
        THEN RAISE(ABORT, 'Policy violation: OPEN_LICENSE_VERIFIED attestation requires non-empty license_spdx')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.permits_transcript_storage = 1
             AND NEW.authorization_basis IN ('WRITTEN_PERMISSION', 'WRITTEN_RIGHTS_HOLDER_PERMISSION')
             AND (
                 NEW.evidence_type NOT IN ('WRITTEN_PERMISSION_RECORD', 'WRITTEN_PERMISSION_REF')
                 OR (NEW.evidence_url IS NULL AND NEW.evidence_sha256 IS NULL)
             )
        THEN RAISE(ABORT, 'Policy violation: WRITTEN_PERMISSION attestation requires WRITTEN_PERMISSION_RECORD/REF and evidence_sha256 or evidence_url')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_attestations_verified_gate_update;
CREATE TRIGGER trg_transcript_attestations_verified_gate_update
BEFORE UPDATE ON transcript_authorization_attestations
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.attestation_id != OLD.attestation_id OR NEW.resource_id != OLD.resource_id
        THEN RAISE(ABORT, 'transcript_authorization_attestations identity and resource_id are immutable')
        WHEN OLD.transcript_id IS NOT NULL AND (NEW.transcript_id IS NULL OR NEW.transcript_id != OLD.transcript_id)
        THEN RAISE(ABORT, 'transcript_authorization_attestations.transcript_id scope cannot be broadened or retargeted')
        WHEN OLD.language_bcp47 IS NOT NULL AND (NEW.language_bcp47 IS NULL OR NEW.language_bcp47 != OLD.language_bcp47)
        THEN RAISE(ABORT, 'transcript_authorization_attestations.language_bcp47 scope cannot be broadened or retargeted')
        WHEN OLD.track_kind IS NOT NULL AND (NEW.track_kind IS NULL OR NEW.track_kind != OLD.track_kind)
        THEN RAISE(ABORT, 'transcript_authorization_attestations.track_kind scope cannot be broadened or retargeted')
        WHEN (OLD.withdrawn_at IS NOT NULL OR OLD.verification_status = 'WITHDRAWN')
             AND (NEW.withdrawn_at IS NULL OR NEW.verification_status != 'WITHDRAWN' OR NEW.permits_transcript_storage = 1)
        THEN RAISE(ABORT, 'withdrawn transcript_authorization_attestations rows cannot be reactivated; record a new attestation instead')
        WHEN NEW.expires_at IS NOT NULL AND datetime(NEW.expires_at) IS NULL
        THEN RAISE(ABORT, 'transcript_authorization_attestations.expires_at must be a valid ISO-8601 datetime')
        WHEN NEW.permits_transcript_storage = 1 AND (
            NEW.verification_status != 'VERIFIED'
            OR NEW.permits_metadata_indexing = 0
            OR NEW.license_status != 'VERIFIED_PERMISSIVE'
            OR NEW.authorization_basis NOT IN (
                'OPEN_LICENSE_VERIFIED',
                'PUBLIC_DOMAIN_VERIFIED',
                'FIRST_PARTY_OWNED',
                'FIRST_PARTY_OWNED_MEDIA',
                'WRITTEN_PERMISSION',
                'WRITTEN_RIGHTS_HOLDER_PERMISSION'
            )
            OR NEW.withdrawn_at IS NOT NULL
        )
        THEN RAISE(ABORT, 'Policy violation: attestation granting permits_transcript_storage=1 requires verification_status=VERIFIED, permits_metadata_indexing=1, VERIFIED_PERMISSIVE license, authorized basis, and non-withdrawn status')
        WHEN NEW.permits_transcript_storage = 0
             AND (NEW.permits_verbatim_excerpts = 1 OR NEW.permits_redistribution = 1)
        THEN RAISE(ABORT, 'Policy violation: attestation cannot grant verbatim excerpts or redistribution when permits_transcript_storage is 0')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.withdrawn_at IS NOT NULL
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation cannot have non-null withdrawn_at')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.authorization_basis IN ('OPEN_LICENSE_VERIFIED', 'PUBLIC_DOMAIN_VERIFIED')
             AND NEW.license_status IN ('ALL_RIGHTS_RESERVED', 'RESTRICTED_PROPRIETARY')
        THEN RAISE(ABORT, 'Policy violation: VERIFIED attestation has contradictory authorization_basis and license_status')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.permits_transcript_storage = 1
             AND NEW.authorization_basis = 'OPEN_LICENSE_VERIFIED'
             AND (NEW.license_spdx IS NULL OR length(trim(NEW.license_spdx)) = 0)
        THEN RAISE(ABORT, 'Policy violation: OPEN_LICENSE_VERIFIED attestation requires non-empty license_spdx')
        WHEN NEW.verification_status = 'VERIFIED'
             AND NEW.permits_transcript_storage = 1
             AND NEW.authorization_basis IN ('WRITTEN_PERMISSION', 'WRITTEN_RIGHTS_HOLDER_PERMISSION')
             AND (
                 NEW.evidence_type NOT IN ('WRITTEN_PERMISSION_RECORD', 'WRITTEN_PERMISSION_REF')
                 OR (NEW.evidence_url IS NULL AND NEW.evidence_sha256 IS NULL)
             )
        THEN RAISE(ABORT, 'Policy violation: WRITTEN_PERMISSION attestation requires WRITTEN_PERMISSION_RECORD/REF and evidence_sha256 or evidence_url')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_attestations_prevent_delete;
CREATE TRIGGER trg_transcript_attestations_prevent_delete
BEFORE DELETE ON transcript_authorization_attestations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'transcript_authorization_attestations rows are auditable records and cannot be deleted; withdraw the attestation instead');
END;

DROP TRIGGER IF EXISTS trg_transcript_attestations_cascade_revocation_after_update;
CREATE TRIGGER trg_transcript_attestations_cascade_revocation_after_update
AFTER UPDATE ON transcript_authorization_attestations
FOR EACH ROW
BEGIN
    -- Full storage revocation, withdrawal, rejection, contradiction, or expiry cascades takedown
    DELETE FROM transcript_segments
     WHERE (
         NEW.withdrawn_at IS NOT NULL
         OR NEW.verification_status != 'VERIFIED'
         OR NEW.permits_transcript_storage = 0
         OR (NEW.expires_at IS NOT NULL AND (datetime(NEW.expires_at) IS NULL OR datetime(NEW.expires_at) <= datetime('now')))
     )
       AND transcript_id IN (
           SELECT transcript_id FROM transcripts WHERE authorization_attestation_id = NEW.attestation_id
       );

    DELETE FROM transcript_syntheses
     WHERE (
         NEW.withdrawn_at IS NOT NULL
         OR NEW.verification_status != 'VERIFIED'
         OR NEW.permits_transcript_storage = 0
         OR NEW.permits_ai_synthesis = 0
         OR (NEW.expires_at IS NOT NULL AND (datetime(NEW.expires_at) IS NULL OR datetime(NEW.expires_at) <= datetime('now')))
     )
       AND transcript_id IN (
           SELECT transcript_id FROM transcripts WHERE authorization_attestation_id = NEW.attestation_id
       );

    UPDATE transcript_versions
       SET status = 'TOMBSTONED'
     WHERE (
         NEW.withdrawn_at IS NOT NULL
         OR NEW.verification_status != 'VERIFIED'
         OR NEW.permits_transcript_storage = 0
         OR (NEW.expires_at IS NOT NULL AND (datetime(NEW.expires_at) IS NULL OR datetime(NEW.expires_at) <= datetime('now')))
     )
       AND transcript_id IN (
           SELECT transcript_id FROM transcripts WHERE authorization_attestation_id = NEW.attestation_id
       )
       AND status != 'TOMBSTONED';

    UPDATE transcripts
       SET permits_transcript_storage = 0,
           permits_verbatim_excerpts = 0,
           permits_ai_synthesis = 0,
           permits_redistribution = 0,
           ingestion_state = 'WITHDRAWN_OR_TOMBSTONED',
           transcript_storage_status = 'TOMBSTONED_ON_TAKEDOWN',
           latest_transcript_version_id = NULL,
           segment_count = 0,
           block_or_review_reason = COALESCE(
               NEW.withdrawal_reason,
               block_or_review_reason,
               'Authorization attestation revoked, withdrawn, or expired'
           )
     WHERE (
         NEW.withdrawn_at IS NOT NULL
         OR NEW.verification_status != 'VERIFIED'
         OR NEW.permits_transcript_storage = 0
         OR (NEW.expires_at IS NOT NULL AND (datetime(NEW.expires_at) IS NULL OR datetime(NEW.expires_at) <= datetime('now')))
     )
       AND authorization_attestation_id = NEW.attestation_id
       AND (
           permits_transcript_storage = 1
           OR ingestion_state != 'WITHDRAWN_OR_TOMBSTONED'
           OR transcript_storage_status != 'TOMBSTONED_ON_TAKEDOWN'
       );

    -- Partial permission narrowing (verbatim excerpts, AI synthesis, redistribution) cascades to linked transcripts
    UPDATE transcripts
       SET permits_verbatim_excerpts = CASE WHEN NEW.permits_verbatim_excerpts = 0 THEN 0 ELSE permits_verbatim_excerpts END,
           permits_ai_synthesis = CASE WHEN NEW.permits_ai_synthesis = 0 THEN 0 ELSE permits_ai_synthesis END,
           permits_redistribution = CASE WHEN NEW.permits_redistribution = 0 THEN 0 ELSE permits_redistribution END
     WHERE authorization_attestation_id = NEW.attestation_id
       AND (
           (NEW.permits_verbatim_excerpts = 0 AND permits_verbatim_excerpts = 1)
           OR (NEW.permits_ai_synthesis = 0 AND permits_ai_synthesis = 1)
           OR (NEW.permits_redistribution = 0 AND permits_redistribution = 1)
       );
END;

DROP TRIGGER IF EXISTS trg_transcripts_require_attestation_insert;
CREATE TRIGGER trg_transcripts_require_attestation_insert
BEFORE INSERT ON transcripts
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.permits_transcript_storage = 0 AND (
            NEW.permits_verbatim_excerpts = 1
            OR NEW.permits_redistribution = 1
            OR NEW.ingestion_state = 'INGESTED'
            OR NEW.transcript_storage_status IN ('STORED_TIMED_SEGMENTS', 'STORED_PLAIN_TEXT')
            OR NEW.segment_count > 0
            OR NEW.latest_transcript_version_id IS NOT NULL
        )
        THEN RAISE(ABORT, 'transcripts with permits_transcript_storage = 0 cannot enable verbatim excerpts, redistribution, INGESTED state, or stored segment metadata')
        WHEN NEW.authorization_attestation_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
              FROM transcript_authorization_attestations a
             WHERE a.attestation_id = NEW.authorization_attestation_id
               AND a.resource_id = NEW.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = NEW.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = NEW.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = NEW.track_kind)
        )
        THEN RAISE(ABORT, 'transcripts.authorization_attestation_id must reference an existing attestation matching resource_id, transcript_id, language_bcp47, and track_kind')
        WHEN NEW.authorization_attestation_id IS NOT NULL
             AND NEW.permits_ai_synthesis = 1
             AND NOT EXISTS (
                 SELECT 1
                   FROM transcript_authorization_attestations a
                  WHERE a.attestation_id = NEW.authorization_attestation_id
                    AND a.verification_status = 'VERIFIED'
                    AND a.withdrawn_at IS NULL
                    AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
                    AND a.permits_ai_synthesis = 1
             )
        THEN RAISE(ABORT, 'transcripts.permits_ai_synthesis = 1 requires linked attestation to be VERIFIED, non-withdrawn, non-expired, and permit AI synthesis')
        WHEN NEW.permits_transcript_storage = 1 AND NEW.authorization_attestation_id IS NULL
        THEN RAISE(ABORT, 'transcripts.permits_transcript_storage = 1 requires a non-null authorization_attestation_id')
        WHEN NEW.permits_transcript_storage = 1 AND NOT EXISTS (
            SELECT 1
              FROM transcript_authorization_attestations a
             WHERE a.attestation_id = NEW.authorization_attestation_id
               AND a.resource_id = NEW.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = NEW.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = NEW.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = NEW.track_kind)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
               AND a.permits_transcript_storage = 1
               AND a.license_status = 'VERIFIED_PERMISSIVE'
               AND a.authorization_basis IN (
                   'OPEN_LICENSE_VERIFIED',
                   'PUBLIC_DOMAIN_VERIFIED',
                   'FIRST_PARTY_OWNED',
                   'FIRST_PARTY_OWNED_MEDIA',
                   'WRITTEN_PERMISSION',
                   'WRITTEN_RIGHTS_HOLDER_PERMISSION'
               )
               AND (
                   a.authorization_basis = NEW.authorization_basis
                   OR (a.authorization_basis = 'PUBLIC_DOMAIN_VERIFIED' AND NEW.authorization_basis = 'OPEN_LICENSE_VERIFIED')
               )
               AND (a.license_spdx IS NULL OR NEW.license_spdx = a.license_spdx)
               AND (NEW.permits_verbatim_excerpts = 0 OR a.permits_verbatim_excerpts = 1)
               AND (NEW.permits_ai_synthesis = 0 OR a.permits_ai_synthesis = 1)
               AND (NEW.permits_redistribution = 0 OR a.permits_redistribution = 1)
        )
        THEN RAISE(ABORT, 'transcripts.permits_transcript_storage = 1 requires a VERIFIED, non-withdrawn, non-expired attestation matching resource_id, language_bcp47, track_kind, and requested permission scope')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcripts_require_attestation_update;
CREATE TRIGGER trg_transcripts_require_attestation_update
BEFORE UPDATE ON transcripts
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.transcript_id != OLD.transcript_id
             OR NEW.resource_id != OLD.resource_id
             OR NEW.language_bcp47 != OLD.language_bcp47
             OR NEW.track_kind != OLD.track_kind
        THEN RAISE(ABORT, 'transcripts identity and scope columns (transcript_id, resource_id, language_bcp47, track_kind) are immutable')
        WHEN OLD.ingestion_state = 'WITHDRAWN_OR_TOMBSTONED' AND NEW.ingestion_state != 'WITHDRAWN_OR_TOMBSTONED'
        THEN RAISE(ABORT, 'transcripts in WITHDRAWN_OR_TOMBSTONED state cannot be reactivated')
        WHEN NEW.permits_transcript_storage = 0 AND (
            NEW.permits_verbatim_excerpts = 1
            OR NEW.permits_redistribution = 1
            OR NEW.ingestion_state = 'INGESTED'
            OR NEW.transcript_storage_status IN ('STORED_TIMED_SEGMENTS', 'STORED_PLAIN_TEXT')
            OR NEW.segment_count > 0
            OR NEW.latest_transcript_version_id IS NOT NULL
        )
        THEN RAISE(ABORT, 'transcripts with permits_transcript_storage = 0 cannot enable verbatim excerpts, redistribution, INGESTED state, or stored segment metadata')
        WHEN NEW.authorization_attestation_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
              FROM transcript_authorization_attestations a
             WHERE a.attestation_id = NEW.authorization_attestation_id
               AND a.resource_id = NEW.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = NEW.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = NEW.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = NEW.track_kind)
        )
        THEN RAISE(ABORT, 'transcripts.authorization_attestation_id must reference an existing attestation matching resource_id, transcript_id, language_bcp47, and track_kind')
        WHEN NEW.authorization_attestation_id IS NOT NULL
             AND NEW.permits_ai_synthesis = 1
             AND NOT EXISTS (
                 SELECT 1
                   FROM transcript_authorization_attestations a
                  WHERE a.attestation_id = NEW.authorization_attestation_id
                    AND a.verification_status = 'VERIFIED'
                    AND a.withdrawn_at IS NULL
                    AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
                    AND a.permits_ai_synthesis = 1
             )
        THEN RAISE(ABORT, 'transcripts.permits_ai_synthesis = 1 requires linked attestation to be VERIFIED, non-withdrawn, non-expired, and permit AI synthesis')
        WHEN NEW.permits_transcript_storage = 1 AND NEW.authorization_attestation_id IS NULL
        THEN RAISE(ABORT, 'transcripts.permits_transcript_storage = 1 requires a non-null authorization_attestation_id')
        WHEN NEW.permits_transcript_storage = 1 AND NOT EXISTS (
            SELECT 1
              FROM transcript_authorization_attestations a
             WHERE a.attestation_id = NEW.authorization_attestation_id
               AND a.resource_id = NEW.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = NEW.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = NEW.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = NEW.track_kind)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
               AND a.permits_transcript_storage = 1
               AND a.license_status = 'VERIFIED_PERMISSIVE'
               AND a.authorization_basis IN (
                   'OPEN_LICENSE_VERIFIED',
                   'PUBLIC_DOMAIN_VERIFIED',
                   'FIRST_PARTY_OWNED',
                   'FIRST_PARTY_OWNED_MEDIA',
                   'WRITTEN_PERMISSION',
                   'WRITTEN_RIGHTS_HOLDER_PERMISSION'
               )
               AND (
                   a.authorization_basis = NEW.authorization_basis
                   OR (a.authorization_basis = 'PUBLIC_DOMAIN_VERIFIED' AND NEW.authorization_basis = 'OPEN_LICENSE_VERIFIED')
               )
               AND (a.license_spdx IS NULL OR NEW.license_spdx = a.license_spdx)
               AND (NEW.permits_verbatim_excerpts = 0 OR a.permits_verbatim_excerpts = 1)
               AND (NEW.permits_ai_synthesis = 0 OR a.permits_ai_synthesis = 1)
               AND (NEW.permits_redistribution = 0 OR a.permits_redistribution = 1)
        )
        THEN RAISE(ABORT, 'transcripts.permits_transcript_storage = 1 requires a VERIFIED, non-withdrawn, non-expired attestation matching resource_id, language_bcp47, track_kind, and requested permission scope')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcripts_cascade_revocation_after_update;
CREATE TRIGGER trg_transcripts_cascade_revocation_after_update
AFTER UPDATE ON transcripts
FOR EACH ROW
BEGIN
    DELETE FROM transcript_segments
     WHERE transcript_id = NEW.transcript_id
       AND (
           NEW.permits_transcript_storage = 0
           OR NEW.ingestion_state IN ('BLOCKED_BY_POLICY', 'METADATA_ONLY_LINKED', 'WITHDRAWN_OR_TOMBSTONED', 'RIGHTS_PENDING', 'DISCOVERED')
       );

    UPDATE transcript_versions
       SET status = 'TOMBSTONED'
     WHERE transcript_id = NEW.transcript_id
       AND status != 'TOMBSTONED'
       AND (
           NEW.permits_transcript_storage = 0
           OR NEW.ingestion_state IN ('BLOCKED_BY_POLICY', 'METADATA_ONLY_LINKED', 'WITHDRAWN_OR_TOMBSTONED', 'RIGHTS_PENDING', 'DISCOVERED')
       );

    DELETE FROM transcript_syntheses
     WHERE transcript_id = NEW.transcript_id
       AND (
           NEW.permits_ai_synthesis = 0
           OR NEW.ingestion_state IN ('BLOCKED_BY_POLICY', 'WITHDRAWN_OR_TOMBSTONED', 'RIGHTS_PENDING', 'DISCOVERED')
       );
END;

DROP TRIGGER IF EXISTS trg_transcript_versions_require_authorized_parent;
DROP TRIGGER IF EXISTS trg_transcript_versions_require_verified_attestation_insert;
CREATE TRIGGER trg_transcript_versions_require_verified_attestation_insert
BEFORE INSERT ON transcript_versions
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
              JOIN transcript_authorization_attestations a
                ON a.attestation_id = t.authorization_attestation_id
             WHERE t.transcript_id = NEW.transcript_id
               AND t.permits_transcript_storage = 1
               AND t.license_status = 'VERIFIED_PERMISSIVE'
               AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW')
               AND a.resource_id = t.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
               AND a.permits_transcript_storage = 1
               AND a.license_status = 'VERIFIED_PERMISSIVE'
               AND a.authorization_basis IN (
                   'OPEN_LICENSE_VERIFIED',
                   'PUBLIC_DOMAIN_VERIFIED',
                   'FIRST_PARTY_OWNED',
                   'FIRST_PARTY_OWNED_MEDIA',
                   'WRITTEN_PERMISSION',
                   'WRITTEN_RIGHTS_HOLDER_PERMISSION'
               )
        )
        THEN RAISE(ABORT, 'transcript_versions insert blocked: parent transcript lacks a VERIFIED, non-withdrawn, non-expired authorization attestation matching resource_id, language_bcp47, and track_kind')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_versions_immutable_update;
CREATE TRIGGER trg_transcript_versions_immutable_update
BEFORE UPDATE ON transcript_versions
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.transcript_version_id != OLD.transcript_version_id
          OR NEW.transcript_id != OLD.transcript_id
          OR NEW.version_number != OLD.version_number
          OR NEW.raw_content_sha256 != OLD.raw_content_sha256
          OR NEW.normalized_text_sha256 != OLD.normalized_text_sha256
          OR NEW.normalizer_version != OLD.normalizer_version
          OR NEW.segment_count != OLD.segment_count
          OR NEW.word_count != OLD.word_count
          OR NEW.char_count != OLD.char_count
          OR NEW.created_at != OLD.created_at
        THEN RAISE(ABORT, 'transcript_versions content and hash metadata are immutable once recorded')
        WHEN OLD.status = 'TOMBSTONED' AND NEW.status != 'TOMBSTONED'
        THEN RAISE(ABORT, 'tombstoned transcript_versions rows cannot be reactivated')
        WHEN NEW.status = 'ACTIVE' AND NOT EXISTS (
            SELECT 1
              FROM transcripts t
              JOIN transcript_authorization_attestations a
                ON a.attestation_id = t.authorization_attestation_id
             WHERE t.transcript_id = NEW.transcript_id
               AND t.permits_transcript_storage = 1
               AND t.license_status = 'VERIFIED_PERMISSIVE'
               AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW')
               AND a.resource_id = t.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
               AND a.permits_transcript_storage = 1
               AND a.license_status = 'VERIFIED_PERMISSIVE'
               AND a.authorization_basis IN (
                   'OPEN_LICENSE_VERIFIED',
                   'PUBLIC_DOMAIN_VERIFIED',
                   'FIRST_PARTY_OWNED',
                   'FIRST_PARTY_OWNED_MEDIA',
                   'WRITTEN_PERMISSION',
                   'WRITTEN_RIGHTS_HOLDER_PERMISSION'
               )
        )
        THEN RAISE(ABORT, 'transcript_versions cannot remain ACTIVE without a VERIFIED, non-withdrawn, non-expired authorization attestation')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_segments_require_authorized_parent;
DROP TRIGGER IF EXISTS trg_transcript_segments_require_verified_attestation_insert;
CREATE TRIGGER trg_transcript_segments_require_verified_attestation_insert
BEFORE INSERT ON transcript_segments
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
              JOIN transcript_versions v
                ON v.transcript_version_id = NEW.transcript_version_id
               AND v.transcript_id = t.transcript_id
              JOIN transcript_authorization_attestations a
                ON a.attestation_id = t.authorization_attestation_id
             WHERE t.transcript_id = NEW.transcript_id
               AND v.status = 'ACTIVE'
               AND t.permits_transcript_storage = 1
               AND t.license_status = 'VERIFIED_PERMISSIVE'
               AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW')
               AND a.resource_id = t.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
               AND a.permits_transcript_storage = 1
               AND a.license_status = 'VERIFIED_PERMISSIVE'
               AND a.authorization_basis IN (
                   'OPEN_LICENSE_VERIFIED',
                   'PUBLIC_DOMAIN_VERIFIED',
                   'FIRST_PARTY_OWNED',
                   'FIRST_PARTY_OWNED_MEDIA',
                   'WRITTEN_PERMISSION',
                   'WRITTEN_RIGHTS_HOLDER_PERMISSION'
               )
        )
        THEN RAISE(ABORT, 'transcript_segments insert blocked: parent transcript/version lacks an ACTIVE version and a VERIFIED, non-withdrawn, non-expired authorization attestation matching resource_id, language_bcp47, and track_kind')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_segments_require_verified_attestation_update;
CREATE TRIGGER trg_transcript_segments_require_verified_attestation_update
BEFORE UPDATE ON transcript_segments
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.segment_id != OLD.segment_id
          OR NEW.transcript_id != OLD.transcript_id
          OR NEW.transcript_version_id != OLD.transcript_version_id
          OR NEW.sequence_index != OLD.sequence_index
        THEN RAISE(ABORT, 'transcript_segments identity and parent linkage are immutable')
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
              JOIN transcript_versions v
                ON v.transcript_version_id = NEW.transcript_version_id
               AND v.transcript_id = t.transcript_id
              JOIN transcript_authorization_attestations a
                ON a.attestation_id = t.authorization_attestation_id
             WHERE t.transcript_id = NEW.transcript_id
               AND v.status = 'ACTIVE'
               AND t.permits_transcript_storage = 1
               AND t.license_status = 'VERIFIED_PERMISSIVE'
               AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW')
               AND a.resource_id = t.resource_id
               AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
               AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
               AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
               AND a.verification_status = 'VERIFIED'
               AND a.withdrawn_at IS NULL
               AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
               AND a.permits_transcript_storage = 1
               AND a.license_status = 'VERIFIED_PERMISSIVE'
               AND a.authorization_basis IN (
                   'OPEN_LICENSE_VERIFIED',
                   'PUBLIC_DOMAIN_VERIFIED',
                   'FIRST_PARTY_OWNED',
                   'FIRST_PARTY_OWNED_MEDIA',
                   'WRITTEN_PERMISSION',
                   'WRITTEN_RIGHTS_HOLDER_PERMISSION'
               )
        )
        THEN RAISE(ABORT, 'transcript_segments update blocked: parent transcript/version lacks an ACTIVE version and a VERIFIED, non-withdrawn, non-expired authorization attestation')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_syntheses_require_permission_insert;
CREATE TRIGGER trg_transcript_syntheses_require_permission_insert
BEFORE INSERT ON transcript_syntheses
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
             WHERE t.transcript_id = NEW.transcript_id
               AND t.permits_ai_synthesis = 1
               AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW', 'METADATA_ONLY_LINKED')
               AND (
                   NEW.transcript_version_id IS NULL
                   OR EXISTS (
                       SELECT 1
                         FROM transcript_versions v
                        WHERE v.transcript_version_id = NEW.transcript_version_id
                          AND v.transcript_id = NEW.transcript_id
                          AND v.status != 'TOMBSTONED'
                   )
               )
               AND (
                   (t.permits_transcript_storage = 0 AND t.authorization_attestation_id IS NULL AND t.ingestion_state = 'METADATA_ONLY_LINKED')
                   OR EXISTS (
                       SELECT 1
                         FROM transcript_authorization_attestations a
                        WHERE a.attestation_id = t.authorization_attestation_id
                          AND a.resource_id = t.resource_id
                          AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
                          AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
                          AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
                          AND a.verification_status = 'VERIFIED'
                          AND a.withdrawn_at IS NULL
                          AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
                          AND a.permits_ai_synthesis = 1
                   )
               )
        )
        THEN RAISE(ABORT, 'transcript_syntheses insert blocked: parent transcript or linked authorization attestation does not permit AI synthesis or is withdrawn/expired')
    END;
END;

DROP TRIGGER IF EXISTS trg_transcript_syntheses_require_permission_update;
CREATE TRIGGER trg_transcript_syntheses_require_permission_update
BEFORE UPDATE ON transcript_syntheses
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.synthesis_id != OLD.synthesis_id OR NEW.transcript_id != OLD.transcript_id
        THEN RAISE(ABORT, 'transcript_syntheses identity and transcript_id are immutable')
        WHEN NOT EXISTS (
            SELECT 1
              FROM transcripts t
             WHERE t.transcript_id = NEW.transcript_id
               AND t.permits_ai_synthesis = 1
               AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW', 'METADATA_ONLY_LINKED')
               AND (
                   (t.permits_transcript_storage = 0 AND t.authorization_attestation_id IS NULL AND t.ingestion_state = 'METADATA_ONLY_LINKED')
                   OR EXISTS (
                       SELECT 1
                         FROM transcript_authorization_attestations a
                        WHERE a.attestation_id = t.authorization_attestation_id
                          AND a.resource_id = t.resource_id
                          AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
                          AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
                          AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
                          AND a.verification_status = 'VERIFIED'
                          AND a.withdrawn_at IS NULL
                          AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
                          AND a.permits_ai_synthesis = 1
                   )
               )
        )
        THEN RAISE(ABORT, 'transcript_syntheses update blocked: parent transcript or linked authorization attestation does not permit AI synthesis or is withdrawn/expired')
    END;
END;

DROP TRIGGER IF EXISTS trg_provenance_events_require_transcript_permission_insert;
CREATE TRIGGER trg_provenance_events_require_transcript_permission_insert
BEFORE INSERT ON provenance_events
FOR EACH ROW
WHEN NEW.event_type IN ('TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED', 'TRANSCRIPT_REDISTRIBUTION_EXPORTED')
BEGIN
    SELECT CASE
        WHEN NEW.event_type = 'TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED'
             AND NEW.status = 'allowed'
             AND NOT EXISTS (
                 SELECT 1
                   FROM transcripts t
                   JOIN transcript_authorization_attestations a
                     ON a.attestation_id = t.authorization_attestation_id
                  WHERE t.transcript_id = json_extract(NEW.details_json, '$.transcript_id')
                    AND a.attestation_id = json_extract(NEW.details_json, '$.authorization_attestation_id')
                    AND t.resource_id = NEW.resource_id
                    AND t.permits_transcript_storage = 1
                    AND t.permits_verbatim_excerpts = 1
                    AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW')
                    AND a.resource_id = t.resource_id
                    AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
                    AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
                    AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
                    AND a.verification_status = 'VERIFIED'
                    AND a.withdrawn_at IS NULL
                    AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
                    AND a.permits_transcript_storage = 1
                    AND a.permits_verbatim_excerpts = 1
             )
        THEN RAISE(ABORT, 'TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED audit record blocked: transcript and attestation do not permit verbatim excerpts')
        WHEN NEW.event_type = 'TRANSCRIPT_REDISTRIBUTION_EXPORTED'
             AND NEW.status = 'exported'
             AND NOT EXISTS (
                 SELECT 1
                   FROM transcripts t
                   JOIN transcript_authorization_attestations a
                     ON a.attestation_id = t.authorization_attestation_id
                  WHERE t.transcript_id = json_extract(NEW.details_json, '$.transcript_id')
                    AND a.attestation_id = json_extract(NEW.details_json, '$.authorization_attestation_id')
                    AND t.resource_id = NEW.resource_id
                    AND t.permits_transcript_storage = 1
                    AND t.permits_redistribution = 1
                    AND t.ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED', 'NEEDS_REVIEW')
                    AND a.resource_id = t.resource_id
                    AND (a.transcript_id IS NULL OR a.transcript_id = t.transcript_id)
                    AND (a.language_bcp47 IS NULL OR a.language_bcp47 = t.language_bcp47)
                    AND (a.track_kind IS NULL OR a.track_kind = t.track_kind)
                    AND a.verification_status = 'VERIFIED'
                    AND a.withdrawn_at IS NULL
                    AND (a.expires_at IS NULL OR (datetime(a.expires_at) IS NOT NULL AND datetime(a.expires_at) > datetime('now')))
                    AND a.permits_transcript_storage = 1
                    AND a.permits_redistribution = 1
             )
        THEN RAISE(ABORT, 'TRANSCRIPT_REDISTRIBUTION_EXPORTED audit record blocked: transcript and attestation do not permit redistribution')
    END;
END;
"""


def _migration_8_harden_transcript_authorization_triggers(conn: sqlite3.Connection) -> None:
    """Apply Migration 8: harden SQLite triggers across attestations, transcripts, versions, segments, syntheses, and provenance."""
    # Clear any legacy scope-mismatched attestation references on non-stored transcripts prior to trigger installation
    conn.execute(
        """
        UPDATE transcripts
           SET authorization_attestation_id = NULL,
               permits_ai_synthesis = 0
         WHERE authorization_attestation_id IS NOT NULL
           AND permits_transcript_storage = 0
           AND NOT EXISTS (
               SELECT 1
                 FROM transcript_authorization_attestations a
                WHERE a.attestation_id = transcripts.authorization_attestation_id
                  AND a.resource_id = transcripts.resource_id
                  AND (a.transcript_id IS NULL OR a.transcript_id = transcripts.transcript_id)
                  AND (a.language_bcp47 IS NULL OR a.language_bcp47 = transcripts.language_bcp47)
                  AND (a.track_kind IS NULL OR a.track_kind = transcripts.track_kind)
           )
        """
    )
    conn.executescript(TRANSCRIPT_AUTHORIZATION_HARDENING_V8_SQL)


MIGRATIONS: tuple[tuple[int, str, str, Any], ...] = (
    (1, "0001_legacy_collector_tables", LEGACY_COLLECTOR_TABLES_SQL, _migration_1_legacy_tables),
    (2, "0002_canonical_knowledge_store_v1", CANONICAL_SCHEMA_V2_SQL, _migration_2_canonical_schema),
    (3, "0003_seed_canonical_sources_and_backfill", "SEED_CANONICAL_SOURCES_V3", _migration_3_seed_sources_and_backfill),
    (4, "0004_discovered_link_candidates", DISCOVERED_LINK_CANDIDATES_V4_SQL, _migration_4_discovered_link_candidates),
    (5, "0005_discovered_link_curation_reviews", DISCOVERED_LINK_CURATION_V5_SQL, _migration_5_discovered_link_curation_reviews),
    (6, "0006_transcripts_and_segments", TRANSCRIPTS_AND_SEGMENTS_V6_SQL, _migration_6_transcripts_and_segments),
    (7, "0007_transcript_authorization_attestations", TRANSCRIPT_AUTHORIZATION_ATTESTATIONS_V7_SQL, _migration_7_transcript_authorization_attestations),
    (8, "0008_harden_transcript_authorization_triggers", TRANSCRIPT_AUTHORIZATION_HARDENING_V8_SQL, _migration_8_harden_transcript_authorization_triggers),
)


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending schema migrations safely and idempotently inside transactions.

    Returns the list of newly applied migration version numbers.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied_rows = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()
    applied_by_version = {int(r[0]): (str(r[1]), str(r[2])) for r in applied_rows}

    newly_applied: list[int] = []
    for version, name, sql_marker, migration_fn in MIGRATIONS:
        checksum = sha256_hex(f"{version}:{name}:{sql_marker}")
        if version in applied_by_version:
            recorded_name, recorded_checksum = applied_by_version[version]
            if recorded_name != name or recorded_checksum != checksum:
                raise RuntimeError(
                    f"Schema migration {version} ({name}) checksum mismatch: "
                    f"expected {checksum}, found {recorded_checksum} ({recorded_name})"
                )
            continue

        with conn:
            migration_fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (version, name, checksum, utc_now()),
            )
            conn.execute(f"PRAGMA user_version = {int(version)}")
        newly_applied.append(version)

    # Ensure PRAGMA user_version matches the highest applied migration
    current_user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_user_version < LATEST_SCHEMA_VERSION:
        with conn:
            conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")

    return newly_applied


class KnowledgeStore:
    """Local SQLite persistence store for sources, inventory resources, versions, and provenance."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        auto_initialize: bool = True,
    ) -> None:
        if conn is not None:
            self.db_path: Path | None = Path(db_path) if db_path else None
            self.conn = conn
            self._owns_connection = False
        else:
            resolved_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = resolved_path
            self.conn = sqlite3.connect(str(resolved_path))
            self._owns_connection = True

        self._tx_depth = 0
        self._savepoint_seq = 0
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if auto_initialize:
            self.initialize()

    def close(self) -> None:
        if self._owns_connection and self.conn is not None:
            self.conn.close()

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Execute a block atomically inside a SQLite transaction, rolling back on error."""
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._savepoint_seq += 1
        conn_savepoint = f"sp_{self._savepoint_seq}_{int(time.time_ns())}"
        self._tx_depth += 1
        self.conn.execute(f"SAVEPOINT {conn_savepoint}")
        try:
            yield self.conn
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {conn_savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {conn_savepoint}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {conn_savepoint}")
            if self._tx_depth == 1 and self.conn.in_transaction:
                self.conn.commit()
        finally:
            self._tx_depth -= 1

    def initialize(self) -> list[int]:
        """Initialize the database and apply any pending migrations idempotently."""
        return apply_migrations(self.conn)

    def get_schema_version(self) -> int:
        """Return the current schema version recorded in `schema_migrations`."""
        row = self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0]) if row else 0

    def list_migrations(self) -> list[dict[str, Any]]:
        """Return all applied migrations in ascending version order."""
        rows = self.conn.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Source registry operations
    # ------------------------------------------------------------------

    def register_source(
        self,
        *,
        source_id: str,
        display_name: str,
        source_category: str,
        adapter_type: str,
        origin: str | None = None,
        approved_host_paths: list[str] | str | None = None,
        robots_url: str | None = None,
        crawl_delay_seconds: float = 1.0,
        min_enforced_delay_seconds: float = 1.0,
        max_requests_per_run: int = 10,
        default_license_status: str = "ALL_RIGHTS_RESERVED_METADATA_ONLY",
        permits_metadata_indexing: int | bool = 1,
        permits_full_text_storage: int | bool = 0,
        permits_verbatim_excerpts: int | bool = 0,
        permits_media_rehosting: int | bool = 0,
        legal_uncertainty_status: str = "UNVERIFIED_REQUIRES_HUMAN_REVIEW",
        attribution_template: str | None = None,
        metadata_complete: int | bool = 1,
        status: str = "ACTIVE",
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Register or update a source in `sources` with validation.

        Does not fabricate missing fields: `origin`, `robots_url`, and
        `attribution_template` remain `None` (`NULL`) if not known.
        """
        if not isinstance(source_id, str) or not source_id.startswith("SRC-") or len(source_id.strip()) < 5:
            raise ValueError(f"source_id must start with 'SRC-' and be at least 5 chars: {source_id!r}")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("display_name must be a non-empty string")
        if origin is not None:
            origin = validate_and_canonicalize_url(origin).rstrip("/")
        if robots_url is not None:
            robots_url = validate_and_canonicalize_url(robots_url)

        if approved_host_paths is None:
            host_paths_json = "[]"
        elif isinstance(approved_host_paths, str):
            parsed_paths = json.loads(approved_host_paths)
            if not isinstance(parsed_paths, list):
                raise ValueError("approved_host_paths JSON must be a list")
            host_paths_json = json.dumps(parsed_paths, ensure_ascii=False)
        else:
            host_paths_json = json.dumps(list(approved_host_paths), ensure_ascii=False)

        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sources(
                    source_id, display_name, source_category, adapter_type, origin,
                    approved_host_paths, robots_url, crawl_delay_seconds,
                    min_enforced_delay_seconds, max_requests_per_run,
                    default_license_status, permits_metadata_indexing,
                    permits_full_text_storage, permits_verbatim_excerpts,
                    permits_media_rehosting, legal_uncertainty_status,
                    attribution_template, metadata_complete, status, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    source_category=excluded.source_category,
                    adapter_type=excluded.adapter_type,
                    origin=excluded.origin,
                    approved_host_paths=excluded.approved_host_paths,
                    robots_url=excluded.robots_url,
                    crawl_delay_seconds=excluded.crawl_delay_seconds,
                    min_enforced_delay_seconds=excluded.min_enforced_delay_seconds,
                    max_requests_per_run=excluded.max_requests_per_run,
                    default_license_status=excluded.default_license_status,
                    permits_metadata_indexing=excluded.permits_metadata_indexing,
                    permits_full_text_storage=excluded.permits_full_text_storage,
                    permits_verbatim_excerpts=excluded.permits_verbatim_excerpts,
                    permits_media_rehosting=excluded.permits_media_rehosting,
                    legal_uncertainty_status=excluded.legal_uncertainty_status,
                    attribution_template=excluded.attribution_template,
                    metadata_complete=excluded.metadata_complete,
                    status=excluded.status,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id.strip(),
                    display_name.strip(),
                    source_category.strip(),
                    adapter_type.strip(),
                    origin,
                    host_paths_json,
                    robots_url,
                    float(crawl_delay_seconds),
                    float(min_enforced_delay_seconds),
                    int(max_requests_per_run),
                    default_license_status,
                    int(bool(permits_metadata_indexing)),
                    int(bool(permits_full_text_storage)),
                    int(bool(permits_verbatim_excerpts)),
                    int(bool(permits_media_rehosting)),
                    legal_uncertainty_status,
                    attribution_template,
                    int(bool(metadata_complete)),
                    status,
                    notes,
                    now,
                    now,
                ),
            )
        result = self.get_source(source_id.strip())
        assert result is not None
        return result

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        """Retrieve a single source record by `source_id`, or None if not found."""
        row = self.conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["approved_host_paths"] = json.loads(data.get("approved_host_paths") or "[]")
        return data

    def list_sources(self, *, only_complete: bool = False) -> list[dict[str, Any]]:
        """List registered sources ordered by `source_id`."""
        query = "SELECT * FROM sources"
        if only_complete:
            query += " WHERE metadata_complete = 1"
        query += " ORDER BY source_id"
        rows = self.conn.execute(query).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["approved_host_paths"] = json.loads(item.get("approved_host_paths") or "[]")
            results.append(item)
        return results

    def resolve_or_register_source(
        self,
        *,
        person_source: str | None,
        canonical_url: str | None = None,
        manifest_id: str | None = None,
    ) -> str:
        """Resolve a manifest `Person / source` or URL to a `source_id`.

        If the source is an external/unmapped source from a manifest, registers
        it with `metadata_complete=0` and `NULL` for unknown fields (`origin`,
        `robots_url`, `attribution_template`) rather than fabricating metadata.
        """
        label = (person_source or "").strip()
        normalized_label = label.casefold()

        if normalized_label in KNOWN_PERSON_SOURCE_MAP:
            return KNOWN_PERSON_SOURCE_MAP[normalized_label]

        if canonical_url:
            parsed = urlsplit(canonical_url)
            host_path = f"{parsed.netloc.lower()}{parsed.path}"
            if host_path.startswith("www.cs.toronto.edu/~hinton/"):
                return "SRC-HINTON"
            if parsed.netloc.lower() == "yoshuabengio.org":
                return "SRC-BENGIO"
            if parsed.netloc.lower() in ("arxiv.org", "export.arxiv.org"):
                return "SRC-ARXIV"
            if parsed.netloc.lower() == "www.3blue1brown.com":
                return "SRC-3B1B"
            if parsed.netloc.lower().endswith("wikipedia.org"):
                return "SRC-WIKIPEDIA"
            if not label and parsed.netloc.lower() == "www.cs.toronto.edu":
                return "SRC-HINTON"

        if not label and manifest_id:
            if manifest_id.startswith("HI-URL-"):
                return "SRC-HINTON"
            if manifest_id.startswith("YB-URL-"):
                return "SRC-BENGIO"

        # Unknown or incomplete external/cross-owner source: preserve without fabricating metadata
        display_label = label or "Unspecified External Source"
        source_id = slugify_source_id(display_label)
        existing = self.get_source(source_id)
        if existing is None:
            self.register_source(
                source_id=source_id,
                display_name=display_label,
                source_category="EXTERNAL_REFERENCE",
                adapter_type="RECORD_ONLY_NO_ADAPTER",
                origin=None,
                approved_host_paths=[],
                robots_url=None,
                crawl_delay_seconds=1.0,
                min_enforced_delay_seconds=1.0,
                max_requests_per_run=0,
                default_license_status="ALL_RIGHTS_RESERVED_METADATA_ONLY",
                permits_metadata_indexing=1,
                permits_full_text_storage=0,
                permits_verbatim_excerpts=0,
                permits_media_rehosting=0,
                legal_uncertainty_status="UNVERIFIED_REQUIRES_HUMAN_REVIEW",
                attribution_template=None,
                metadata_complete=0,
                status="RECORD_ONLY",
                notes="Incomplete/external source preserved from manifest without fabricating source metadata.",
            )
        return source_id

    # ------------------------------------------------------------------
    # Collection run & provenance operations
    # ------------------------------------------------------------------

    def start_collection_run(
        self,
        *,
        run_type: str = "MANIFEST_IMPORT",
        is_dry_run: bool = True,
        manifest_path: str | Path | None = None,
        selected_count: int = 0,
        notes: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Create a `collection_runs` record and return its `run_id`."""
        generated_run_id = run_id or (
            "run-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + hashlib.sha1(str(time.time_ns()).encode("utf-8")).hexdigest()[:8]
        )
        started_at = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO collection_runs(
                    run_id, run_type, is_dry_run, started_at, manifest_path,
                    selected_count, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    generated_run_id,
                    run_type,
                    int(bool(is_dry_run)),
                    started_at,
                    str(manifest_path) if manifest_path is not None else None,
                    int(selected_count),
                    notes,
                ),
            )
        return generated_run_id

    def finish_collection_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        imported_count: int = 0,
        updated_count: int = 0,
        skipped_count: int = 0,
        rejected_count: int = 0,
        fetched_count: int = 0,
        discovered_count: int = 0,
        error_count: int = 0,
        failure_summary: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Finalize a `collection_runs` record with outcome counts and timestamps."""
        finished_at = utc_now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE collection_runs
                   SET finished_at=?,
                       imported_count=?,
                       updated_count=?,
                       skipped_count=?,
                       rejected_count=?,
                       fetched_count=?,
                       discovered_count=?,
                       error_count=?,
                       status=?,
                       failure_summary=?,
                       notes=COALESCE(?, notes)
                 WHERE run_id=?
                """,
                (
                    finished_at,
                    int(imported_count),
                    int(updated_count),
                    int(skipped_count),
                    int(rejected_count),
                    int(fetched_count),
                    int(discovered_count),
                    int(error_count),
                    status,
                    failure_summary,
                    notes,
                    run_id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Unknown collection run_id: {run_id!r}")
        result = self.get_collection_run(run_id)
        assert result is not None
        return result

    def get_collection_run(self, run_id: str) -> dict[str, Any] | None:
        """Retrieve a `collection_runs` record by `run_id`."""
        row = self.conn.execute("SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_collection_runs(self) -> list[dict[str, Any]]:
        """List all collection runs ordered by `started_at` ascending."""
        rows = self.conn.execute("SELECT * FROM collection_runs ORDER BY started_at, run_id").fetchall()
        return [dict(r) for r in rows]

    def record_provenance(
        self,
        *,
        event_type: str,
        acquisition_method: str,
        actor: str,
        status: str,
        source_id: str | None = None,
        resource_id: str | None = None,
        version_id: str | None = None,
        run_id: str | None = None,
        manifest_id: str | None = None,
        source_url: str | None = None,
        final_url: str | None = None,
        http_status: int | None = None,
        content_hash: str | None = None,
        metadata_hash: str | None = None,
        details: dict[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Record an immutable provenance event in `provenance_events`."""
        timestamp = recorded_at or utc_now()
        generated_event_id = event_id or (
            "prov_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + sha256_hex(f"{time.time_ns()}:{event_type}:{resource_id}:{source_url}")[:10]
        )
        details_json = (
            json.dumps(details, ensure_ascii=False, sort_keys=True)
            if details is not None
            else None
        )
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO provenance_events(
                    event_id, run_id, source_id, resource_id, version_id, manifest_id,
                    event_type, acquisition_method, actor, source_url, final_url,
                    status, http_status, content_hash, metadata_hash, details_json,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_event_id,
                    run_id,
                    source_id,
                    resource_id,
                    version_id,
                    manifest_id,
                    event_type,
                    acquisition_method,
                    actor,
                    source_url,
                    final_url,
                    status,
                    http_status,
                    content_hash,
                    metadata_hash,
                    details_json,
                    timestamp,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM provenance_events WHERE event_id = ?", (generated_event_id,)
        ).fetchone()
        assert row is not None
        result = dict(row)
        result["details"] = json.loads(result["details_json"]) if result.get("details_json") else None
        return result

    def list_provenance_events(
        self,
        *,
        resource_id: str | None = None,
        run_id: str | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List provenance events with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if resource_id is not None:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)

        query = "SELECT * FROM provenance_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY recorded_at, event_id"
        rows = self.conn.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["details"] = json.loads(item["details_json"]) if item.get("details_json") else None
            results.append(item)
        return results

    # ------------------------------------------------------------------
    # Resource & version history operations
    # ------------------------------------------------------------------

    def upsert_resource(
        self,
        *,
        source_id: str,
        canonical_url: str,
        title: str,
        resource_type: str,
        manifest_id: str | None = None,
        fetched_url: str | None = None,
        authors: str | None = None,
        published_at: str | None = None,
        media_modality: str | None = None,
        domain_scope: str | None = None,
        abstract_or_description: str | None = None,
        topic_tags: str | None = None,
        external_identifier: str | None = None,
        observed_from: str | None = None,
        information_to_inventory: str | None = None,
        priority: str | None = None,
        collection_decision: str | None = None,
        verification_status: str | None = None,
        robots_sitemap_status: str | None = None,
        rights_status: str | None = None,
        license_status: str = "ALL_RIGHTS_RESERVED_METADATA_ONLY",
        license_spdx: str | None = None,
        permitted_storage_scope: str = "METADATA_ONLY",
        permits_full_text_storage: int | bool = 0,
        permits_verbatim_excerpts: int | bool = 0,
        legal_uncertainty_status: str = "UNVERIFIED_REQUIRES_HUMAN_REVIEW",
        text_storage_status: str = "metadata-only-not-yet-collected",
        content_hash: str | None = None,
        lifecycle_state: str = "ACTIVE",
        last_checked: str | None = None,
        last_verified_at: str | None = None,
        discovered_at: str | None = None,
        run_id: str | None = None,
        acquisition_method: str = "DIRECT_UPSERT",
        actor: str = "knowledge_store",
        change_reason: str = "resource_upsert",
    ) -> dict[str, Any]:
        """Insert or update a resource without duplicating records.

        - Validates `canonical_url` (and `fetched_url` if provided).
        - Enforces foreign key to `sources(source_id)` (and `source_urls(manifest_id)` if set).
        - Detects existing resource by `canonical_url`, `resource_id`, or `manifest_id`.
        - Creates a new immutable `resource_versions` snapshot ONLY when the
          resource is newly created or its `metadata_hash` or `content_hash` changes.
        - Records a `provenance_events` entry for creation, version update, or
          idempotent no-op check.

        Returns a dict containing the stored resource record plus `"operation"`
        (`"inserted"`, `"updated"`, or `"unchanged"`).
        """
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a non-empty string")
        source_id = source_id.strip()

        normalized_url = validate_and_canonicalize_url(canonical_url)
        normalized_fetched_url = (
            validate_and_canonicalize_url(fetched_url) if fetched_url is not None else None
        )
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        if not isinstance(resource_type, str) or not resource_type.strip():
            raise ValueError("resource_type must be a non-empty string")
        if content_hash is not None and (not isinstance(content_hash, str) or len(content_hash) != 64):
            raise ValueError("content_hash must be None or a 64-character hex SHA-256 digest")

        resolved_modality = media_modality or infer_media_modality(resource_type, normalized_url)
        resource_id = compute_resource_id(source_id, normalized_url)

        payload_for_hash = {
            "source_id": source_id,
            "manifest_id": manifest_id,
            "canonical_url": normalized_url,
            "title": title.strip(),
            "authors": authors,
            "published_at": published_at,
            "resource_type": resource_type.strip(),
            "media_modality": resolved_modality,
            "domain_scope": domain_scope,
            "abstract_or_description": abstract_or_description,
            "topic_tags": topic_tags,
            "external_identifier": external_identifier,
            "observed_from": observed_from,
            "information_to_inventory": information_to_inventory,
            "priority": priority,
            "collection_decision": collection_decision,
            "verification_status": verification_status,
            "robots_sitemap_status": robots_sitemap_status,
            "rights_status": rights_status,
            "license_status": license_status,
            "license_spdx": license_spdx,
            "permitted_storage_scope": permitted_storage_scope,
            "permits_full_text_storage": int(bool(permits_full_text_storage)),
            "permits_verbatim_excerpts": int(bool(permits_verbatim_excerpts)),
            "legal_uncertainty_status": legal_uncertainty_status,
            "text_storage_status": text_storage_status,
        }
        metadata_hash = compute_metadata_hash(payload_for_hash)
        now = utc_now()

        with self.transaction() as conn:
            # Check for existing resource by canonical_url, resource_id, or manifest_id
            existing_by_url = conn.execute(
                "SELECT * FROM resources WHERE canonical_url = ? OR resource_id = ?",
                (normalized_url, resource_id),
            ).fetchone()

            if manifest_id is not None:
                existing_by_manifest = conn.execute(
                    "SELECT * FROM resources WHERE manifest_id = ?",
                    (manifest_id,),
                ).fetchone()
                if (
                    existing_by_manifest is not None
                    and existing_by_url is not None
                    and existing_by_manifest["resource_id"] != existing_by_url["resource_id"]
                ):
                    raise sqlite3.IntegrityError(
                        f"Conflicting manifest_id {manifest_id!r} and canonical_url {normalized_url!r} "
                        f"refer to different resources ({existing_by_manifest['resource_id']} vs {existing_by_url['resource_id']})"
                    )
                if existing_by_url is not None and existing_by_url["manifest_id"] not in (None, manifest_id):
                    raise sqlite3.IntegrityError(
                        f"Duplicate canonical_url {normalized_url!r} already bound to manifest_id "
                        f"{existing_by_url['manifest_id']!r} (cannot bind to {manifest_id!r})"
                    )
                existing = existing_by_url or existing_by_manifest
            else:
                existing = existing_by_url

            if existing is None:
                version_number = 1
                version_id = compute_version_id(
                    resource_id,
                    version_number,
                    metadata_hash,
                    content_hash,
                    recorded_date=discovered_at or now,
                )
                first_discovered = discovered_at or now
                conn.execute(
                    """
                    INSERT INTO resources(
                        resource_id, source_id, manifest_id, canonical_url, fetched_url,
                        title, authors, published_at, resource_type, media_modality,
                        domain_scope, abstract_or_description, topic_tags,
                        external_identifier, observed_from, information_to_inventory,
                        priority, collection_decision, verification_status,
                        robots_sitemap_status, rights_status, license_status,
                        license_spdx, permitted_storage_scope,
                        permits_full_text_storage, permits_verbatim_excerpts,
                        legal_uncertainty_status, text_storage_status,
                        current_version_id, version_count, metadata_hash, content_hash,
                        lifecycle_state, first_discovered_at, last_verified_at,
                        last_checked, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        resource_id,
                        source_id,
                        manifest_id,
                        normalized_url,
                        normalized_fetched_url,
                        title.strip(),
                        authors,
                        published_at,
                        resource_type.strip(),
                        resolved_modality,
                        domain_scope,
                        abstract_or_description,
                        topic_tags,
                        external_identifier,
                        observed_from,
                        information_to_inventory,
                        priority,
                        collection_decision,
                        verification_status,
                        robots_sitemap_status,
                        rights_status,
                        license_status,
                        license_spdx,
                        permitted_storage_scope,
                        int(bool(permits_full_text_storage)),
                        int(bool(permits_verbatim_excerpts)),
                        legal_uncertainty_status,
                        text_storage_status,
                        version_id,
                        version_number,
                        metadata_hash,
                        content_hash,
                        lifecycle_state,
                        first_discovered,
                        last_verified_at,
                        last_checked,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO resource_versions(
                        version_id, resource_id, version_number, canonical_url,
                        title, authors, published_at, resource_type,
                        abstract_or_description, topic_tags, external_identifier,
                        collection_decision, verification_status,
                        robots_sitemap_status, rights_status, license_status,
                        text_storage_status, metadata_hash, content_hash,
                        change_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        resource_id,
                        version_number,
                        normalized_url,
                        title.strip(),
                        authors,
                        published_at,
                        resource_type.strip(),
                        abstract_or_description,
                        topic_tags,
                        external_identifier,
                        collection_decision,
                        verification_status,
                        robots_sitemap_status,
                        rights_status,
                        license_status,
                        text_storage_status,
                        metadata_hash,
                        content_hash,
                        change_reason,
                        now,
                    ),
                )
                prov = self.record_provenance(
                    event_type="RESOURCE_CREATED",
                    acquisition_method=acquisition_method,
                    actor=actor,
                    status="imported",
                    source_id=source_id,
                    resource_id=resource_id,
                    version_id=version_id,
                    run_id=run_id,
                    manifest_id=manifest_id,
                    source_url=normalized_url,
                    final_url=normalized_fetched_url,
                    content_hash=content_hash,
                    metadata_hash=metadata_hash,
                    details={"change_reason": change_reason, "version_number": 1},
                )
                conn.execute(
                    "UPDATE resource_versions SET provenance_event_id = ? WHERE version_id = ?",
                    (prov["event_id"], version_id),
                )
                operation = "inserted"
                target_resource_id = resource_id
            else:
                target_resource_id = str(existing["resource_id"])
                prev_meta_hash = existing["metadata_hash"]
                prev_content_hash = existing["content_hash"]
                effective_content_hash = content_hash if content_hash is not None else prev_content_hash

                if prev_meta_hash == metadata_hash and (prev_content_hash or None) == (effective_content_hash or None):
                    operation = "unchanged"
                else:
                    # Check if an identical (metadata_hash, content_hash) snapshot already exists in version history
                    matching_ver = conn.execute(
                        """
                        SELECT version_id, version_number
                          FROM resource_versions
                         WHERE resource_id = ?
                           AND metadata_hash = ?
                           AND IFNULL(content_hash, '') = IFNULL(?, '')
                        """,
                        (target_resource_id, metadata_hash, effective_content_hash),
                    ).fetchone()

                    max_ver_row = conn.execute(
                        "SELECT COALESCE(MAX(version_number), 1) FROM resource_versions WHERE resource_id = ?",
                        (target_resource_id,),
                    ).fetchone()
                    next_version_number = int(max_ver_row[0]) + 1

                    if matching_ver is None:
                        version_id = compute_version_id(
                            target_resource_id,
                            next_version_number,
                            metadata_hash,
                            effective_content_hash,
                            recorded_date=now,
                        )
                        conn.execute(
                            """
                            INSERT INTO resource_versions(
                                version_id, resource_id, version_number, canonical_url,
                                title, authors, published_at, resource_type,
                                abstract_or_description, topic_tags, external_identifier,
                                collection_decision, verification_status,
                                robots_sitemap_status, rights_status, license_status,
                                text_storage_status, metadata_hash, content_hash,
                                change_reason, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                version_id,
                                target_resource_id,
                                next_version_number,
                                normalized_url,
                                title.strip(),
                                authors,
                                published_at,
                                resource_type.strip(),
                                abstract_or_description,
                                topic_tags,
                                external_identifier,
                                collection_decision,
                                verification_status,
                                robots_sitemap_status,
                                rights_status,
                                license_status,
                                text_storage_status,
                                metadata_hash,
                                effective_content_hash,
                                change_reason,
                                now,
                            ),
                        )
                        new_version_count = next_version_number
                    else:
                        version_id = str(matching_ver["version_id"])
                        new_version_count = next_version_number - 1

                    conn.execute(
                        """
                        UPDATE resources
                           SET source_id=?,
                               manifest_id=COALESCE(?, manifest_id),
                               canonical_url=?,
                               fetched_url=COALESCE(?, fetched_url),
                               title=?,
                               authors=?,
                               published_at=?,
                               resource_type=?,
                               media_modality=?,
                               domain_scope=?,
                               abstract_or_description=?,
                               topic_tags=?,
                               external_identifier=?,
                               observed_from=?,
                               information_to_inventory=?,
                               priority=?,
                               collection_decision=?,
                               verification_status=?,
                               robots_sitemap_status=?,
                               rights_status=?,
                               license_status=?,
                               license_spdx=?,
                               permitted_storage_scope=?,
                               permits_full_text_storage=?,
                               permits_verbatim_excerpts=?,
                               legal_uncertainty_status=?,
                               text_storage_status=?,
                               current_version_id=?,
                               version_count=?,
                               metadata_hash=?,
                               content_hash=?,
                               lifecycle_state=?,
                               last_verified_at=COALESCE(?, last_verified_at),
                               last_checked=COALESCE(?, last_checked),
                               updated_at=?
                         WHERE resource_id=?
                        """,
                        (
                            source_id,
                            manifest_id,
                            normalized_url,
                            normalized_fetched_url,
                            title.strip(),
                            authors,
                            published_at,
                            resource_type.strip(),
                            resolved_modality,
                            domain_scope,
                            abstract_or_description,
                            topic_tags,
                            external_identifier,
                            observed_from,
                            information_to_inventory,
                            priority,
                            collection_decision,
                            verification_status,
                            robots_sitemap_status,
                            rights_status,
                            license_status,
                            license_spdx,
                            permitted_storage_scope,
                            int(bool(permits_full_text_storage)),
                            int(bool(permits_verbatim_excerpts)),
                            legal_uncertainty_status,
                            text_storage_status,
                            version_id,
                            new_version_count,
                            metadata_hash,
                            effective_content_hash,
                            lifecycle_state,
                            last_verified_at,
                            last_checked,
                            now,
                            target_resource_id,
                        ),
                    )
                    prov = self.record_provenance(
                        event_type="RESOURCE_UPDATED",
                        acquisition_method=acquisition_method,
                        actor=actor,
                        status="updated",
                        source_id=source_id,
                        resource_id=target_resource_id,
                        version_id=version_id,
                        run_id=run_id,
                        manifest_id=manifest_id or existing["manifest_id"],
                        source_url=normalized_url,
                        final_url=normalized_fetched_url,
                        content_hash=effective_content_hash,
                        metadata_hash=metadata_hash,
                        details={
                            "change_reason": change_reason,
                            "previous_metadata_hash": prev_meta_hash,
                            "previous_content_hash": prev_content_hash,
                            "version_count": new_version_count,
                        },
                    )
                    if matching_ver is None:
                        conn.execute(
                            "UPDATE resource_versions SET provenance_event_id = ? WHERE version_id = ?",
                            (prov["event_id"], version_id),
                        )
                    operation = "updated"

        stored = self.get_resource(resource_id=target_resource_id)
        assert stored is not None
        stored["operation"] = operation
        return stored

    def get_resource(
        self,
        *,
        resource_id: str | None = None,
        canonical_url: str | None = None,
        manifest_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a resource by `resource_id`, `canonical_url`, or `manifest_id`."""
        if resource_id is not None:
            row = self.conn.execute("SELECT * FROM resources WHERE resource_id = ?", (resource_id,)).fetchone()
        elif canonical_url is not None:
            norm_url = validate_and_canonicalize_url(canonical_url)
            row = self.conn.execute("SELECT * FROM resources WHERE canonical_url = ?", (norm_url,)).fetchone()
        elif manifest_id is not None:
            row = self.conn.execute("SELECT * FROM resources WHERE manifest_id = ?", (manifest_id,)).fetchone()
        else:
            raise ValueError("Specify at least one of resource_id, canonical_url, or manifest_id")
        return dict(row) if row is not None else None

    def list_resources(self, *, source_id: str | None = None) -> list[dict[str, Any]]:
        """List stored resources ordered by `manifest_id` and `resource_id`."""
        if source_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM resources WHERE source_id = ? ORDER BY COALESCE(manifest_id, resource_id)",
                (source_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM resources ORDER BY COALESCE(manifest_id, resource_id)"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_resource_versions(self, resource_id: str) -> list[dict[str, Any]]:
        """Return all immutable version records for `resource_id` in ascending version order."""
        rows = self.conn.execute(
            "SELECT * FROM resource_versions WHERE resource_id = ? ORDER BY version_number ASC",
            (resource_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Discovered link candidate persistence (review-only, no auto-fetch)
    # ------------------------------------------------------------------

    def upsert_discovered_link_candidate(
        self,
        *,
        referring_resource_id: str,
        referring_source_id: str,
        from_url: str,
        original_url: str,
        raw_href: str | None = None,
        normalized_url: str | None = None,
        host_domain: str | None = None,
        is_internal: int | bool = False,
        target_scope: str = "Out-of-scope external link (not in manifest)",
        recommended_decision: str = "Record as reference only; do not fetch automatically",
        policy_state: str | None = None,
        review_status: str = "PENDING_REVIEW",
        fetch_permission_granted: int | bool = False,
        from_manifest_id: str | None = None,
        link_text: str | None = None,
        rel: str | None = None,
        discovered_at: str | None = None,
        run_id: str | None = None,
        provenance_event_id: str | None = None,
        acquisition_method: str = "CANDIDATE_LINK_DISCOVERY",
        actor: str = "source_adapter",
        record_provenance_event: bool = True,
    ) -> dict[str, Any]:
        """Idempotently persist a discovered link candidate without promoting it to `resources`.

        - Rejects unsafe or non-HTTP(S) URL schemes while preserving `original_url`
          for audit in `provenance_events` if rejected by caller or raised here.
        - Enforces `fetch_permission_granted = 0` and `promoted_resource_id = NULL`
          on candidate discovery.
        - On repeated discovery of `(referring_resource_id, normalized_url)`, updates
          `last_seen_at` and increments `seen_count` without creating duplicate rows.
        """
        if not referring_resource_id or not isinstance(referring_resource_id, str):
            raise ValueError("referring_resource_id must be a non-empty string")
        if not referring_source_id or not isinstance(referring_source_id, str):
            raise ValueError("referring_source_id must be a non-empty string")
        if not isinstance(original_url, str) or not original_url.strip():
            raise ValueError("original_url must be a non-empty string")
        if bool(fetch_permission_granted):
            raise ValueError("Discovered link candidates cannot be persisted with fetch_permission_granted=True")

        effective_raw_href = (raw_href if raw_href is not None else original_url).strip()
        canonical_from_url = validate_and_canonicalize_url(from_url)
        prov_run_id: str | None = None
        if run_id is not None:
            run_row = self.conn.execute(
                "SELECT run_id FROM collection_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is not None:
                prov_run_id = run_id

        try:
            canonical_target_url = validate_and_canonicalize_url(normalized_url or original_url)
        except ValueError as exc:
            if record_provenance_event:
                self.record_provenance(
                    event_type="DISCOVERED_LINK_CANDIDATE_REJECTED",
                    acquisition_method=acquisition_method,
                    actor=actor,
                    status="rejected-unsafe-or-invalid-url",
                    source_id=referring_source_id,
                    resource_id=referring_resource_id,
                    run_id=prov_run_id,
                    manifest_id=from_manifest_id,
                    source_url=canonical_from_url,
                    final_url=original_url,
                    details={
                        "raw_href": effective_raw_href,
                        "original_url": original_url,
                        "rejection_reason": str(exc),
                        "requested_run_id": run_id,
                    },
                    recorded_at=discovered_at or utc_now(),
                )
            raise
        parsed_target = urlsplit(canonical_target_url)
        resolved_host = (host_domain or parsed_target.hostname or "").lower().strip()
        if not resolved_host:
            raise ValueError(f"Unable to determine host_domain for {canonical_target_url!r}")

        internal_flag = 1 if bool(is_internal) else 0
        domain_classification = "INTERNAL" if internal_flag else "EXTERNAL"
        resolved_policy_state = policy_state or (
            "SAME_ORIGIN_CANDIDATE_NOT_AUTO_FETCHED"
            if internal_flag
            else "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH"
        )
        if review_status not in VALID_CANDIDATE_REVIEW_STATUSES:
            raise ValueError(f"Invalid review_status: {review_status!r}")

        candidate_id = compute_candidate_link_id(referring_resource_id, canonical_target_url)
        now = discovered_at or utc_now()

        with self.transaction() as conn:
            res_row = conn.execute(
                "SELECT resource_id, source_id, manifest_id FROM resources WHERE resource_id = ?",
                (referring_resource_id,),
            ).fetchone()
            if res_row is None:
                raise ValueError(f"Unknown referring_resource_id: {referring_resource_id!r}")

            effective_manifest_id = from_manifest_id or res_row["manifest_id"]

            existing = conn.execute(
                "SELECT * FROM discovered_link_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()

            if existing is None:
                eff_prov_id = provenance_event_id
                if eff_prov_id is None and record_provenance_event:
                    prov = self.record_provenance(
                        event_type="DISCOVERED_LINK_RECORDED",
                        acquisition_method=acquisition_method,
                        actor=actor,
                        status="candidate-recorded-not-fetched",
                        source_id=referring_source_id,
                        resource_id=referring_resource_id,
                        run_id=prov_run_id,
                        manifest_id=effective_manifest_id,
                        source_url=canonical_from_url,
                        final_url=canonical_target_url,
                        details={
                            "candidate_id": candidate_id,
                            "raw_href": effective_raw_href,
                            "original_url": original_url,
                            "normalized_url": canonical_target_url,
                            "host_domain": resolved_host,
                            "is_internal": bool(internal_flag),
                            "domain_classification": domain_classification,
                            "target_scope": target_scope,
                            "recommended_decision": recommended_decision,
                            "policy_state": resolved_policy_state,
                            "review_status": review_status,
                            "fetch_permission_granted": False,
                            "link_text": link_text,
                            "rel": rel,
                        },
                        recorded_at=now,
                    )
                    eff_prov_id = prov["event_id"]

                conn.execute(
                    """
                    INSERT INTO discovered_link_candidates(
                        candidate_id, referring_resource_id, referring_source_id,
                        from_manifest_id, from_url, raw_href, original_url,
                        normalized_url, host_domain, is_internal, domain_classification,
                        target_scope, recommended_decision, policy_state, review_status,
                        fetch_permission_granted, promoted_resource_id, link_text, rel,
                        first_seen_at, last_seen_at, seen_count,
                        first_run_id, last_run_id, provenance_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        referring_resource_id,
                        referring_source_id,
                        effective_manifest_id,
                        canonical_from_url,
                        effective_raw_href,
                        original_url,
                        canonical_target_url,
                        resolved_host,
                        internal_flag,
                        domain_classification,
                        target_scope,
                        recommended_decision,
                        resolved_policy_state,
                        review_status,
                        link_text,
                        rel,
                        now,
                        now,
                        run_id,
                        run_id,
                        eff_prov_id,
                    ),
                )
                operation = "inserted"
            else:
                new_seen_count = int(existing["seen_count"]) + 1
                conn.execute(
                    """
                    UPDATE discovered_link_candidates
                       SET last_seen_at = ?,
                           seen_count = ?,
                           last_run_id = COALESCE(?, last_run_id),
                           link_text = COALESCE(?, link_text),
                           rel = COALESCE(?, rel),
                           provenance_event_id = COALESCE(?, provenance_event_id)
                     WHERE candidate_id = ?
                    """,
                    (
                        now,
                        new_seen_count,
                        run_id,
                        link_text,
                        rel,
                        provenance_event_id,
                        candidate_id,
                    ),
                )
                operation = "updated"

        stored = self.get_discovered_link_candidate(candidate_id=candidate_id)
        assert stored is not None
        stored["operation"] = operation
        return stored

    def get_discovered_link_candidate(
        self,
        *,
        candidate_id: str | None = None,
        referring_resource_id: str | None = None,
        normalized_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a discovered link candidate by `candidate_id` or `(referring_resource_id, normalized_url)`."""
        if candidate_id is not None:
            row = self.conn.execute(
                "SELECT * FROM discovered_link_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        elif referring_resource_id is not None and normalized_url is not None:
            norm = validate_and_canonicalize_url(normalized_url)
            row = self.conn.execute(
                "SELECT * FROM discovered_link_candidates WHERE referring_resource_id = ? AND normalized_url = ?",
                (referring_resource_id, norm),
            ).fetchone()
        else:
            raise ValueError("Specify candidate_id or both referring_resource_id and normalized_url")
        return dict(row) if row is not None else None

    CANDIDATE_EXPORT_COLUMNS: tuple[str, ...] = (
        "candidate_id",
        "referring_resource_id",
        "referring_source_id",
        "from_manifest_id",
        "from_url",
        "raw_href",
        "original_url",
        "normalized_url",
        "host_domain",
        "is_internal",
        "domain_classification",
        "target_scope",
        "recommended_decision",
        "policy_state",
        "review_status",
        "fetch_permission_granted",
        "promoted_resource_id",
        "link_text",
        "rel",
        "first_seen_at",
        "last_seen_at",
        "seen_count",
        "first_run_id",
        "last_run_id",
        "provenance_event_id",
        "reviewed_by",
        "reviewed_at",
        "review_reason",
        "review_count",
        "last_review_event_id",
    )

    @staticmethod
    def _build_candidate_filter_clause(
        *,
        referring_resource_id: str | None = None,
        referring_source_id: str | None = None,
        host_domain: str | None = None,
        domain_classification: str | None = None,
        review_status: str | None = None,
    ) -> tuple[str, list[Any], dict[str, str | None]]:
        clauses: list[str] = []
        params: list[Any] = []
        norm_resource_id = referring_resource_id.strip() if referring_resource_id is not None else None
        norm_source_id = referring_source_id.strip() if referring_source_id is not None else None
        norm_domain = host_domain.strip().lower().rstrip(".") if host_domain is not None else None
        norm_classification = (
            domain_classification.strip().upper() if domain_classification is not None else None
        )
        norm_status = review_status.strip().upper() if review_status is not None else None

        if norm_resource_id:
            clauses.append("referring_resource_id = ?")
            params.append(norm_resource_id)
        if norm_source_id:
            clauses.append("referring_source_id = ?")
            params.append(norm_source_id)
        if norm_domain:
            clauses.append("LOWER(host_domain) = ?")
            params.append(norm_domain)
        if norm_classification:
            if norm_classification not in VALID_DOMAIN_CLASSIFICATIONS:
                raise ValueError(
                    f"Invalid domain_classification {domain_classification!r}; "
                    f"expected one of {VALID_DOMAIN_CLASSIFICATIONS}"
                )
            clauses.append("domain_classification = ?")
            params.append(norm_classification)
        if norm_status:
            if norm_status not in VALID_CANDIDATE_REVIEW_STATUSES:
                raise ValueError(
                    f"Invalid review_status {review_status!r}; "
                    f"expected one of {VALID_CANDIDATE_REVIEW_STATUSES}"
                )
            clauses.append("review_status = ?")
            params.append(norm_status)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        filters = {
            "referring_resource_id": norm_resource_id or None,
            "referring_source_id": norm_source_id or None,
            "host_domain": norm_domain or None,
            "domain_classification": norm_classification or None,
            "review_status": norm_status or None,
        }
        return where_sql, params, filters

    def count_discovered_link_candidates(
        self,
        *,
        referring_resource_id: str | None = None,
        referring_source_id: str | None = None,
        host_domain: str | None = None,
        domain_classification: str | None = None,
        review_status: str | None = None,
    ) -> int:
        """Count persisted discovered link candidates matching optional filters."""
        where_sql, params, _ = self._build_candidate_filter_clause(
            referring_resource_id=referring_resource_id,
            referring_source_id=referring_source_id,
            host_domain=host_domain,
            domain_classification=domain_classification,
            review_status=review_status,
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM discovered_link_candidates {where_sql}",
            tuple(params),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_discovered_link_candidates(
        self,
        *,
        referring_resource_id: str | None = None,
        referring_source_id: str | None = None,
        host_domain: str | None = None,
        domain_classification: str | None = None,
        review_status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List persisted discovered link candidates with optional filtering and pagination."""
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")
        if limit is not None and limit <= 0:
            raise ValueError(f"limit must be > 0 when provided, got {limit}")

        where_sql, params, _ = self._build_candidate_filter_clause(
            referring_resource_id=referring_resource_id,
            referring_source_id=referring_source_id,
            host_domain=host_domain,
            domain_classification=domain_classification,
            review_status=review_status,
        )
        sql = f"SELECT * FROM discovered_link_candidates {where_sql} ORDER BY first_seen_at ASC, candidate_id ASC"
        query_params = list(params)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            query_params.extend([limit, offset])
        elif offset > 0:
            sql += " LIMIT -1 OFFSET ?"
            query_params.append(offset)

        rows = self.conn.execute(sql, tuple(query_params)).fetchall()
        return [dict(r) for r in rows]

    def query_discovered_link_candidates(
        self,
        *,
        referring_resource_id: str | None = None,
        referring_source_id: str | None = None,
        host_domain: str | None = None,
        domain_classification: str | None = None,
        review_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Query discovered link candidates with pagination metadata and applied filters."""
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")

        where_sql, params, filters = self._build_candidate_filter_clause(
            referring_resource_id=referring_resource_id,
            referring_source_id=referring_source_id,
            host_domain=host_domain,
            domain_classification=domain_classification,
            review_status=review_status,
        )
        total_row = self.conn.execute(
            f"SELECT COUNT(*) FROM discovered_link_candidates {where_sql}",
            tuple(params),
        ).fetchone()
        total_count = int(total_row[0]) if total_row is not None else 0

        rows = self.conn.execute(
            f"""
            SELECT * FROM discovered_link_candidates
            {where_sql}
            ORDER BY first_seen_at ASC, candidate_id ASC
            LIMIT ? OFFSET ?
            """,
            tuple([*params, limit, offset]),
        ).fetchall()
        items = [dict(r) for r in rows]
        return {
            "total_count": total_count,
            "returned_count": len(items),
            "limit": limit,
            "offset": offset,
            "has_more": (offset + len(items)) < total_count,
            "filters": filters,
            "items": items,
        }

    def summarize_discovered_link_candidates(
        self,
        *,
        referring_source_id: str | None = None,
        host_domain: str | None = None,
        domain_classification: str | None = None,
    ) -> dict[str, Any]:
        """Return a summary of pending, accepted-for-reference, rejected, and other candidate counts."""
        where_sql, params, filters = self._build_candidate_filter_clause(
            referring_source_id=referring_source_id,
            host_domain=host_domain,
            domain_classification=domain_classification,
        )

        by_review_status: dict[str, int] = {status: 0 for status in VALID_CANDIDATE_REVIEW_STATUSES}
        for row in self.conn.execute(
            f"""
            SELECT review_status, COUNT(*) AS cnt
              FROM discovered_link_candidates
              {where_sql}
             GROUP BY review_status
             ORDER BY review_status ASC
            """,
            tuple(params),
        ).fetchall():
            by_review_status[str(row["review_status"])] = int(row["cnt"])

        by_classification: dict[str, int] = {cls: 0 for cls in VALID_DOMAIN_CLASSIFICATIONS}
        for row in self.conn.execute(
            f"""
            SELECT domain_classification, COUNT(*) AS cnt
              FROM discovered_link_candidates
              {where_sql}
             GROUP BY domain_classification
             ORDER BY domain_classification ASC
            """,
            tuple(params),
        ).fetchall():
            by_classification[str(row["domain_classification"])] = int(row["cnt"])

        by_source: dict[str, int] = {}
        for row in self.conn.execute(
            f"""
            SELECT referring_source_id, COUNT(*) AS cnt
              FROM discovered_link_candidates
              {where_sql}
             GROUP BY referring_source_id
             ORDER BY referring_source_id ASC
            """,
            tuple(params),
        ).fetchall():
            by_source[str(row["referring_source_id"])] = int(row["cnt"])

        by_domain: dict[str, int] = {}
        for row in self.conn.execute(
            f"""
            SELECT host_domain, COUNT(*) AS cnt
              FROM discovered_link_candidates
              {where_sql}
             GROUP BY host_domain
             ORDER BY cnt DESC, host_domain ASC
            """,
            tuple(params),
        ).fetchall():
            by_domain[str(row["host_domain"])] = int(row["cnt"])

        fetch_perm_clause = (
            f"{where_sql} AND fetch_permission_granted = 1"
            if where_sql
            else "WHERE fetch_permission_granted = 1"
        )
        fetch_perm_row = self.conn.execute(
            f"SELECT COUNT(*) FROM discovered_link_candidates {fetch_perm_clause}",
            tuple(params),
        ).fetchone()
        fetch_permission_granted_count = int(fetch_perm_row[0]) if fetch_perm_row is not None else 0

        review_events_row = self.conn.execute(
            "SELECT COUNT(*) FROM discovered_link_reviews"
        ).fetchone()
        review_events_count = int(review_events_row[0]) if review_events_row is not None else 0

        total_candidates = sum(by_review_status.values())
        return {
            "total_candidates": total_candidates,
            "pending_count": by_review_status["PENDING_REVIEW"],
            "accepted_for_reference_count": by_review_status["ACCEPTED_FOR_REFERENCE"],
            "approved_for_manifest_count": by_review_status["APPROVED_FOR_MANIFEST"],
            "rejected_count": by_review_status["REJECTED"],
            "deferred_count": by_review_status["DEFERRED"],
            "by_review_status": by_review_status,
            "by_domain_classification": by_classification,
            "by_source": by_source,
            "by_host_domain": by_domain,
            "fetch_permission_granted_count": fetch_permission_granted_count,
            "review_events_count": review_events_count,
            "filters": {
                "referring_source_id": filters["referring_source_id"],
                "host_domain": filters["host_domain"],
                "domain_classification": filters["domain_classification"],
            },
        }

    def export_discovered_link_candidates(
        self,
        *,
        export_format: str = "json",
        output_path: Path | str | None = None,
        referring_resource_id: str | None = None,
        referring_source_id: str | None = None,
        host_domain: str | None = None,
        domain_classification: str | None = None,
        review_status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """Export filtered discovered link candidates to CSV or JSON without network access."""
        fmt = (export_format or "").strip().lower()
        if fmt not in ("csv", "json"):
            raise ValueError(f"Unsupported export_format {export_format!r}; expected 'csv' or 'json'")

        candidates = self.list_discovered_link_candidates(
            referring_resource_id=referring_resource_id,
            referring_source_id=referring_source_id,
            host_domain=host_domain,
            domain_classification=domain_classification,
            review_status=review_status,
            limit=limit,
            offset=offset,
        )

        ordered_rows = [
            {col: row.get(col) for col in self.CANDIDATE_EXPORT_COLUMNS}
            for row in candidates
        ]

        if fmt == "json":
            serialized = json.dumps(ordered_rows, indent=2, ensure_ascii=False) + "\n"
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(
                buffer,
                fieldnames=list(self.CANDIDATE_EXPORT_COLUMNS),
                lineterminator="\n",
            )
            writer.writeheader()
            for row in ordered_rows:
                writer.writerow(row)
            serialized = buffer.getvalue()

        if output_path is not None:
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(serialized, encoding="utf-8")

        return serialized

    def review_discovered_link_candidate(
        self,
        *,
        decision: str,
        reviewer: str,
        reason: str,
        candidate_id: str | None = None,
        referring_resource_id: str | None = None,
        normalized_url: str | None = None,
        reviewed_at: str | None = None,
        run_id: str | None = None,
        acquisition_method: str = "HUMAN_CURATION_REVIEW",
    ) -> dict[str, Any]:
        """Record an offline human curation review decision for a discovered link candidate.

        Safety invariants:
        - Never performs any network request.
        - Never grants fetch permission (`fetch_permission_granted` remains `0`).
          Accepting a candidate for reference (`ACCEPTED_FOR_REFERENCE`) or approving
          it for manifest consideration (`APPROVED_FOR_MANIFEST`) does NOT authorize
          automated fetching; any future fetch must pass the source-specific policy gate.
        - Preserves `original_url` and `referring_resource_id` / `from_url` for auditability.
        - Validates candidate URLs against unsafe schemes, embedded credentials,
          malformed syntax, and prohibited destinations before allowing non-rejection
          review states.
        - Idempotent when repeating the exact same `(decision, reviewer, reason)` on a
          candidate already in that state.
        """
        clean_decision = (decision or "").strip().upper()
        if clean_decision not in VALID_CANDIDATE_REVIEW_STATUSES:
            raise ValueError(
                f"Invalid review decision {decision!r}; expected one of {VALID_CANDIDATE_REVIEW_STATUSES}"
            )
        clean_reviewer = (reviewer or "").strip()
        if not clean_reviewer:
            raise ValueError("reviewer must be a non-empty string")
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise ValueError("reason must be a non-empty string")

        existing = self.get_discovered_link_candidate(
            candidate_id=candidate_id,
            referring_resource_id=referring_resource_id,
            normalized_url=normalized_url,
        )
        if existing is None:
            lookup_desc = (
                f"candidate_id={candidate_id!r}"
                if candidate_id is not None
                else f"referring_resource_id={referring_resource_id!r}, normalized_url={normalized_url!r}"
            )
            raise KeyError(f"Discovered link candidate not found ({lookup_desc})")

        resolved_candidate_id = str(existing["candidate_id"])
        orig_url = str(existing.get("original_url") or "").strip()
        norm_url = str(existing.get("normalized_url") or "").strip()
        from_url = str(existing.get("from_url") or "").strip()
        ref_res_id = str(existing.get("referring_resource_id") or "").strip()

        if not orig_url or not norm_url or not from_url or not ref_res_id:
            raise ValueError(
                f"Candidate {resolved_candidate_id} is missing required audit fields "
                "(original_url, normalized_url, from_url, or referring_resource_id)"
            )

        url_validation_error: str | None = None
        try:
            canonical_from = validate_and_canonicalize_url(from_url)
            canonical_orig = validate_and_canonicalize_url(orig_url)
            canonical_norm = validate_and_canonicalize_url(norm_url)
            if canonical_orig != canonical_norm or canonical_norm != norm_url:
                url_validation_error = (
                    f"normalized_url {norm_url!r} does not match canonicalized original_url {canonical_orig!r}"
                )
            elif canonical_from != from_url:
                url_validation_error = (
                    f"from_url {from_url!r} does not match canonicalized form {canonical_from!r}"
                )
        except ValueError as exc:
            url_validation_error = str(exc)

        if url_validation_error is not None and clean_decision != "REJECTED":
            raise ValueError(
                f"Cannot set review_status={clean_decision!r} on candidate {resolved_candidate_id} "
                f"with unsafe or malformed URL: {url_validation_error}"
            )

        is_internal = bool(int(existing["is_internal"]))
        if clean_decision == "REJECTED":
            new_policy_state = "CURATION_REJECTED_NO_FETCH"
        elif clean_decision == "ACCEPTED_FOR_REFERENCE":
            new_policy_state = (
                "SAME_ORIGIN_ACCEPTED_REFERENCE_ONLY_NO_FETCH"
                if is_internal
                else "CURATION_ACCEPTED_REFERENCE_ONLY_NO_FETCH"
            )
        elif clean_decision == "APPROVED_FOR_MANIFEST":
            new_policy_state = (
                "SAME_ORIGIN_MANIFEST_CANDIDATE_REQUIRES_POLICY_GATE_NO_AUTO_FETCH"
                if is_internal
                else "EXTERNAL_MANIFEST_CANDIDATE_REQUIRES_POLICY_GATE_NO_AUTO_FETCH"
            )
        elif clean_decision == "DEFERRED":
            new_policy_state = "CURATION_DEFERRED_NO_FETCH"
        else:
            new_policy_state = (
                "SAME_ORIGIN_CANDIDATE_NOT_AUTO_FETCHED"
                if is_internal
                else "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH"
            )

        # Idempotent no-op when repeating the exact same decision, reviewer, and reason
        if (
            existing.get("review_status") == clean_decision
            and existing.get("reviewed_by") == clean_reviewer
            and existing.get("review_reason") == clean_reason
            and int(existing.get("fetch_permission_granted") or 0) == 0
            and existing.get("policy_state") == new_policy_state
        ):
            unchanged = dict(existing)
            unchanged["operation"] = "unchanged"
            unchanged["review_id"] = existing.get("last_review_event_id")
            return unchanged

        now = reviewed_at or utc_now()
        previous_review_status = str(existing["review_status"])
        previous_policy_state = str(existing["policy_state"])
        new_review_count = int(existing.get("review_count") or 0) + 1

        with self.transaction() as conn:
            prov = self.record_provenance(
                event_type="CANDIDATE_LINK_REVIEWED",
                acquisition_method=acquisition_method,
                actor=clean_reviewer,
                status=f"review-{clean_decision.lower()}",
                source_id=str(existing["referring_source_id"]),
                resource_id=ref_res_id,
                run_id=run_id,
                manifest_id=existing.get("from_manifest_id"),
                source_url=from_url,
                final_url=norm_url,
                details={
                    "candidate_id": resolved_candidate_id,
                    "referring_resource_id": ref_res_id,
                    "referring_source_id": existing["referring_source_id"],
                    "from_url": from_url,
                    "original_url": orig_url,
                    "normalized_url": norm_url,
                    "host_domain": existing["host_domain"],
                    "domain_classification": existing["domain_classification"],
                    "previous_review_status": previous_review_status,
                    "new_review_status": clean_decision,
                    "previous_policy_state": previous_policy_state,
                    "new_policy_state": new_policy_state,
                    "fetch_permission_granted": False,
                    "reviewer": clean_reviewer,
                    "reason": clean_reason,
                    "reviewed_at": now,
                    "review_count": new_review_count,
                },
                recorded_at=now,
            )
            prov_event_id = prov["event_id"]
            review_id = compute_candidate_review_id(
                resolved_candidate_id,
                new_review_count,
                clean_decision,
                clean_reviewer,
                clean_reason,
                now,
            )

            conn.execute(
                """
                INSERT INTO discovered_link_reviews(
                    review_id, candidate_id, referring_resource_id, referring_source_id,
                    original_url, normalized_url, domain_classification,
                    previous_review_status, new_review_status,
                    reviewer, review_reason, reviewed_at,
                    fetch_permission_granted, provenance_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    review_id,
                    resolved_candidate_id,
                    ref_res_id,
                    str(existing["referring_source_id"]),
                    orig_url,
                    norm_url,
                    str(existing["domain_classification"]),
                    previous_review_status,
                    clean_decision,
                    clean_reviewer,
                    clean_reason,
                    now,
                    prov_event_id,
                ),
            )

            conn.execute(
                """
                UPDATE discovered_link_candidates
                   SET review_status = ?,
                       policy_state = ?,
                       fetch_permission_granted = 0,
                       reviewed_by = ?,
                       reviewed_at = ?,
                       review_reason = ?,
                       review_count = ?,
                       last_review_event_id = ?
                 WHERE candidate_id = ?
                """,
                (
                    clean_decision,
                    new_policy_state,
                    clean_reviewer,
                    now,
                    clean_reason,
                    new_review_count,
                    prov_event_id,
                    resolved_candidate_id,
                ),
            )

        updated = self.get_discovered_link_candidate(candidate_id=resolved_candidate_id)
        assert updated is not None
        updated["operation"] = "reviewed"
        updated["review_id"] = review_id
        updated["review_provenance_event_id"] = prov_event_id
        return updated

    def list_discovered_link_reviews(
        self,
        *,
        candidate_id: str | None = None,
        reviewer: str | None = None,
    ) -> list[dict[str, Any]]:
        """List immutable curation review history records from `discovered_link_reviews`."""
        clauses: list[str] = []
        params: list[Any] = []
        if candidate_id is not None:
            clauses.append("candidate_id = ?")
            params.append(candidate_id.strip())
        if reviewer is not None:
            clauses.append("reviewer = ?")
            params.append(reviewer.strip())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM discovered_link_reviews {where_sql} ORDER BY reviewed_at ASC, review_id ASC",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Explicit seed manifest import operation (offline / no network)
    # ------------------------------------------------------------------

    def _ingest_manifest_record(
        self,
        raw_row: Any,
        *,
        run_id: str | None,
        acquisition_method: str = "SEED_MANIFEST_IMPORT",
        actor: str = "seed_manifest_importer",
    ) -> tuple[str, dict[str, Any]]:
        """Validate and import a single raw manifest dict inside a savepoint.

        Returns `(outcome, details)` where `outcome` is one of:
        `'imported'`, `'updated'`, `'skipped'`, or `'rejected'`.
        """
        if not isinstance(raw_row, dict):
            reason = "Manifest entry is not a JSON object"
            self.record_provenance(
                event_type="MANIFEST_ROW_REJECTED",
                acquisition_method=acquisition_method,
                actor=actor,
                status="rejected",
                run_id=run_id,
                details={"reason": reason, "raw_row": repr(raw_row)},
            )
            return "rejected", {"manifest_id": None, "url": None, "reason": reason}

        manifest_id = (raw_row.get("Manifest ID") or "").strip()
        raw_url = (raw_row.get("URL") or "").strip()
        person_source = (raw_row.get("Person / source") or "").strip() or None

        if not manifest_id:
            reason = "Missing required 'Manifest ID'"
            self.record_provenance(
                event_type="MANIFEST_ROW_REJECTED",
                acquisition_method=acquisition_method,
                actor=actor,
                status="rejected",
                run_id=run_id,
                source_url=raw_url or None,
                details={"reason": reason},
            )
            return "rejected", {"manifest_id": None, "url": raw_url or None, "reason": reason}

        try:
            canonical_url = validate_and_canonicalize_url(raw_url)
        except ValueError as exc:
            reason = f"Invalid URL: {exc}"
            self.record_provenance(
                event_type="MANIFEST_ROW_REJECTED",
                acquisition_method=acquisition_method,
                actor=actor,
                status="rejected",
                run_id=run_id,
                source_url=raw_url or None,
                details={"manifest_id": manifest_id, "reason": reason},
            )
            return "rejected", {"manifest_id": manifest_id, "url": raw_url or None, "reason": reason}

        # Preserve unknown/incomplete fields as None rather than fabricating values
        def _opt_str(key: str) -> str | None:
            val = raw_row.get(key)
            if val is None:
                return None
            cleaned = str(val).strip()
            return cleaned if cleaned else None

        page_resource_name = _opt_str("Page / resource name")
        resource_type = _opt_str("Resource type")
        domain_scope = _opt_str("Domain scope")
        verification_status = _opt_str("Verification status")
        observed_from = _opt_str("Observed from")
        information_to_inventory = _opt_str("Information to inventory")
        priority = _opt_str("Priority")
        collection_decision = _opt_str("Collection decision")
        robots_sitemap_status = _opt_str("Robots / sitemap status")
        rights_status = _opt_str("Rights status")
        last_checked = _opt_str("Last checked")
        authors = _opt_str("Authors")
        published_at = _opt_str("Published at")
        topic_tags = _opt_str("Topic tags")
        external_identifier = _opt_str("External identifier")

        try:
            with self.transaction() as conn:
                source_id = self.resolve_or_register_source(
                    person_source=person_source,
                    canonical_url=canonical_url,
                    manifest_id=manifest_id,
                )
                source_meta = self.get_source(source_id)
                assert source_meta is not None

                # Check for conflicting duplicate URL already assigned to a different manifest_id
                existing_url_row = conn.execute(
                    "SELECT resource_id, manifest_id FROM resources WHERE canonical_url = ?",
                    (canonical_url,),
                ).fetchone()
                if existing_url_row is not None and existing_url_row["manifest_id"] not in (None, manifest_id):
                    reason = (
                        f"Duplicate canonical URL {canonical_url!r} already registered under "
                        f"manifest_id {existing_url_row['manifest_id']!r}"
                    )
                    raise ValueError(reason)

                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO source_urls(
                        manifest_id, person_source, url, page_resource_name,
                        resource_type, domain_scope, verification_status,
                        observed_from, information_to_inventory, priority,
                        collection_decision, robots_sitemap_status, rights_status,
                        last_checked, source_id, canonical_url, imported_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manifest_id) DO UPDATE SET
                        person_source=excluded.person_source,
                        url=excluded.url,
                        page_resource_name=excluded.page_resource_name,
                        resource_type=excluded.resource_type,
                        domain_scope=excluded.domain_scope,
                        verification_status=excluded.verification_status,
                        observed_from=excluded.observed_from,
                        information_to_inventory=excluded.information_to_inventory,
                        priority=excluded.priority,
                        collection_decision=excluded.collection_decision,
                        robots_sitemap_status=excluded.robots_sitemap_status,
                        rights_status=excluded.rights_status,
                        last_checked=excluded.last_checked,
                        source_id=excluded.source_id,
                        canonical_url=excluded.canonical_url,
                        updated_at=excluded.updated_at
                    """,
                    (
                        manifest_id,
                        person_source,
                        raw_url,
                        page_resource_name,
                        resource_type,
                        domain_scope,
                        verification_status,
                        observed_from,
                        information_to_inventory,
                        priority,
                        collection_decision,
                        robots_sitemap_status,
                        rights_status,
                        last_checked,
                        source_id,
                        canonical_url,
                        now,
                        now,
                    ),
                )

                upserted = self.upsert_resource(
                    source_id=source_id,
                    manifest_id=manifest_id,
                    canonical_url=canonical_url,
                    title=page_resource_name or canonical_url,
                    authors=authors,
                    published_at=published_at,
                    resource_type=resource_type or "Unspecified manifest resource",
                    domain_scope=domain_scope,
                    abstract_or_description=information_to_inventory,
                    topic_tags=topic_tags,
                    external_identifier=external_identifier,
                    observed_from=observed_from,
                    information_to_inventory=information_to_inventory,
                    priority=priority,
                    collection_decision=collection_decision,
                    verification_status=verification_status,
                    robots_sitemap_status=robots_sitemap_status,
                    rights_status=rights_status,
                    license_status=str(source_meta["default_license_status"]),
                    permitted_storage_scope="METADATA_ONLY",
                    permits_full_text_storage=0,
                    permits_verbatim_excerpts=0,
                    legal_uncertainty_status=str(source_meta["legal_uncertainty_status"]),
                    text_storage_status="metadata-only-not-yet-collected",
                    last_checked=last_checked,
                    run_id=run_id,
                    acquisition_method=acquisition_method,
                    actor=actor,
                    change_reason="seed_manifest_import",
                )
        except (ValueError, sqlite3.IntegrityError) as exc:
            reason = str(exc)
            self.record_provenance(
                event_type="MANIFEST_ROW_REJECTED",
                acquisition_method=acquisition_method,
                actor=actor,
                status="rejected",
                run_id=run_id,
                source_url=raw_url or None,
                details={"manifest_id": manifest_id, "reason": reason},
            )
            return "rejected", {"manifest_id": manifest_id, "url": raw_url, "reason": reason}

        op = upserted["operation"]
        if op == "inserted":
            return "imported", {"manifest_id": manifest_id, "resource_id": upserted["resource_id"]}
        if op == "updated":
            return "updated", {"manifest_id": manifest_id, "resource_id": upserted["resource_id"]}
        return "skipped", {"manifest_id": manifest_id, "resource_id": upserted["resource_id"]}

    def import_seed_manifest(
        self,
        manifest_path: Path | str = DEFAULT_SEED_PATH,
    ) -> ManifestImportSummary:
        """Import `seed_urls.json` into the local database with zero network requests.

        - Maps the actual 14-field JSON structure in `seed_urls.json`.
        - Imports known inventory records cleanly.
        - Makes repeated imports idempotent (`skipped_count` incremented when unchanged).
        - Preserves unknown or incomplete source/resource metadata as `NULL` without fabricating values.
        - Records a `collection_runs` audit entry and `provenance_events`.
        """
        resolved_manifest = Path(manifest_path)
        raw_data = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        if not isinstance(raw_data, list):
            raise ValueError(f"Seed manifest must be a JSON list of objects: {resolved_manifest}")

        run_id = self.start_collection_run(
            run_type="MANIFEST_IMPORT",
            is_dry_run=True,
            manifest_path=resolved_manifest,
            selected_count=len(raw_data),
            notes="Offline seed manifest import into local knowledge store (no network requests).",
        )
        summary = ManifestImportSummary(
            run_id=run_id,
            manifest_path=str(resolved_manifest),
            total_records=len(raw_data),
        )

        try:
            for raw_row in raw_data:
                outcome, detail = self._ingest_manifest_record(
                    raw_row,
                    run_id=run_id,
                    acquisition_method="SEED_MANIFEST_IMPORT",
                    actor="seed_manifest_importer",
                )
                if outcome == "imported":
                    summary.imported_count += 1
                elif outcome == "updated":
                    summary.updated_count += 1
                elif outcome == "skipped":
                    summary.skipped_count += 1
                else:
                    summary.rejected_count += 1
                    summary.rejected_records.append(detail)

            final_status = "completed" if summary.rejected_count == 0 else "completed-with-rejections"
            failure_summary = (
                json.dumps(summary.rejected_records, ensure_ascii=False)
                if summary.rejected_records
                else None
            )
            self.finish_collection_run(
                run_id,
                status=final_status,
                imported_count=summary.imported_count,
                updated_count=summary.updated_count,
                skipped_count=summary.skipped_count,
                rejected_count=summary.rejected_count,
                error_count=summary.rejected_count,
                failure_summary=failure_summary,
            )
        except Exception as exc:
            self.finish_collection_run(
                run_id,
                status="failed",
                imported_count=summary.imported_count,
                updated_count=summary.updated_count,
                skipped_count=summary.skipped_count,
                rejected_count=summary.rejected_count,
                error_count=summary.rejected_count + 1,
                failure_summary=f"{type(exc).__name__}: {exc}",
            )
            raise

        return summary

    # ------------------------------------------------------------------
    # Transcript persistence, offline import, versioning & takedowns
    # ------------------------------------------------------------------

    def get_transcript_attestation(self, attestation_id: str) -> dict[str, Any] | None:
        """Retrieve a `transcript_authorization_attestations` row by `attestation_id`, or None if not found."""
        row = self.conn.execute(
            "SELECT * FROM transcript_authorization_attestations WHERE attestation_id = ?",
            (attestation_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d.setdefault("source_evidence_url", d.get("evidence_url"))
        return d

    def list_transcript_attestations(
        self,
        *,
        resource_id: str | None = None,
        transcript_id: str | None = None,
        verification_status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List `transcript_authorization_attestations` rows matching optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if resource_id is not None:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        if transcript_id is not None:
            clauses.append("transcript_id = ?")
            params.append(transcript_id)
        if verification_status is not None:
            clauses.append("verification_status = ?")
            params.append(verification_status)
        query = "SELECT * FROM transcript_authorization_attestations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, attestation_id"
        rows = self.conn.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d.setdefault("source_evidence_url", d.get("evidence_url"))
            results.append(d)
        return results

    def record_transcript_attestation(
        self,
        *,
        resource_id: str,
        authorization_basis: str = "NONE",
        license_status: str = "UNKNOWN",
        license_spdx: str | None = None,
        evidence_type: str = "OTHER_VERIFIABLE_EVIDENCE",
        evidence_reference: str,
        evidence_url: str | None = None,
        source_evidence_url: str | None = None,
        evidence_sha256: str | None = None,
        attribution_text: str | None = None,
        permits_metadata_indexing: bool = True,
        permits_transcript_storage: bool = False,
        permits_verbatim_excerpts: bool = False,
        permits_ai_synthesis: bool = True,
        permits_redistribution: bool = False,
        reviewed_by: str = "rights_reviewer",
        reviewed_at: str | None = None,
        verification_status: str = "VERIFIED",
        review_notes: str | None = None,
        expires_at: str | None = None,
        transcript_id: str | None = None,
        language_bcp47: str | None = None,
        track_kind: str | None = None,
        strict_verification: bool = False,
    ) -> dict[str, Any]:
        """Record or update an auditable transcript authorization attestation in the local store."""
        resource_row = self.get_resource(resource_id=resource_id)
        if resource_row is None:
            raise ValueError(f"Unknown parent resource_id: {resource_id!r}")
        if authorization_basis not in VALID_AUTHORIZATION_BASES:
            raise ValueError(f"Invalid authorization_basis: {authorization_basis!r}")
        if license_status not in VALID_TRANSCRIPT_LICENSE_STATUSES:
            raise ValueError(f"Invalid license_status: {license_status!r}")
        if evidence_type not in VALID_ATTESTATION_EVIDENCE_TYPES:
            raise ValueError(f"Invalid evidence_type: {evidence_type!r}")
        if verification_status not in VALID_ATTESTATION_VERIFICATION_STATUSES:
            raise ValueError(f"Invalid verification_status: {verification_status!r}")
        if not isinstance(evidence_reference, str) or not evidence_reference.strip():
            raise ValueError("evidence_reference must be a non-empty string")
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise ValueError("reviewed_by must be a non-empty string")

        lang_norm = validate_bcp47_language_tag(language_bcp47) if language_bcp47 else None
        kind_norm: str | None = None
        if track_kind is not None:
            kind_norm = track_kind.strip().upper()
            if kind_norm not in VALID_TRANSCRIPT_TRACK_KINDS:
                raise ValueError(f"Invalid track_kind: {track_kind!r}")

        if transcript_id is not None:
            tr_row = self.get_transcript(transcript_id)
            if tr_row is not None and str(tr_row["resource_id"]) != str(resource_id):
                raise ValueError(
                    f"transcript_id {transcript_id!r} belongs to resource {tr_row['resource_id']!r}, not {resource_id!r}"
                )

        effective_ev_url = evidence_url if evidence_url is not None else source_evidence_url
        canonical_ev_url = (
            validate_and_canonicalize_url(effective_ev_url) if effective_ev_url else None
        )
        norm_ev_sha256: str | None = None
        if evidence_sha256 is not None:
            cleaned_sha = evidence_sha256.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", cleaned_sha):
                raise ValueError("evidence_sha256 must be a 64-character hexadecimal SHA-256 digest")
            norm_ev_sha256 = cleaned_sha

        resolved_spdx = license_spdx.strip() if (license_spdx and license_spdx.strip()) else None
        if (
            authorization_basis == "PUBLIC_DOMAIN_VERIFIED"
            and license_status == "VERIFIED_PERMISSIVE"
            and resolved_spdx is None
        ):
            resolved_spdx = "CC0-1.0"

        effective_status = verification_status
        notes_parts: list[str] = [review_notes.strip()] if (review_notes and review_notes.strip()) else []
        if attribution_text and attribution_text.strip():
            notes_parts.append(f"Attribution: {attribution_text.strip()}")

        contradictions = _detect_attestation_contradictions(
            authorization_basis=authorization_basis,
            license_status=license_status,
            license_spdx=resolved_spdx,
            evidence_type=evidence_type,
            evidence_reference=evidence_reference,
            permits_metadata_indexing=bool(permits_metadata_indexing),
            permits_transcript_storage=bool(permits_transcript_storage),
            permits_verbatim_excerpts=bool(permits_verbatim_excerpts),
            permits_redistribution=bool(permits_redistribution),
        )
        eff_storage = bool(permits_transcript_storage)
        eff_verbatim = bool(permits_verbatim_excerpts)
        eff_redist = bool(permits_redistribution)
        if contradictions and effective_status == "VERIFIED":
            if strict_verification:
                raise ValueError(" ".join(contradictions))
            effective_status = "CONTRADICTORY"
            eff_storage = False
            eff_verbatim = False
            eff_redist = False
            notes_parts.append("Auto-flagged CONTRADICTORY: " + " ".join(contradictions))
        elif effective_status != "VERIFIED":
            eff_storage = False
            eff_verbatim = False
            eff_redist = False

        now = utc_now()
        rev_at = (reviewed_at or now).strip()
        if effective_status == "VERIFIED" and _is_iso_timestamp_expired(expires_at, now_iso=now):
            effective_status = "EXPIRED"
            eff_storage = False
            eff_verbatim = False
            eff_redist = False
            notes_parts.append(f"Attestation expired at {expires_at}.")

        final_notes = " | ".join(notes_parts) if notes_parts else None
        attestation_id = compute_transcript_attestation_id(
            resource_id=resource_id,
            authorization_basis=authorization_basis,
            evidence_type=evidence_type,
            evidence_reference=evidence_reference,
            reviewed_by=reviewed_by,
            transcript_id=transcript_id,
            language_bcp47=lang_norm,
            track_kind=kind_norm,
        )
        source_id = str(resource_row["source_id"])

        with self.transaction() as conn:
            existing = self.get_transcript_attestation(attestation_id)
            prov = self.record_provenance(
                event_type="TRANSCRIPT_ATTESTATION_RECORDED",
                acquisition_method="FIRST_PARTY_CURATED_IMPORT",
                actor=reviewed_by.strip(),
                status=effective_status.lower(),
                source_id=source_id,
                resource_id=resource_id,
                source_url=canonical_ev_url or str(resource_row["canonical_url"]),
                content_hash=norm_ev_sha256,
                details={
                    "attestation_id": attestation_id,
                    "transcript_id": transcript_id,
                    "authorization_basis": authorization_basis,
                    "license_status": license_status,
                    "license_spdx": resolved_spdx,
                    "evidence_type": evidence_type,
                    "evidence_reference": evidence_reference.strip(),
                    "verification_status": effective_status,
                    "permits_metadata_indexing": int(bool(permits_metadata_indexing)),
                    "permits_transcript_storage": int(eff_storage),
                    "permits_verbatim_excerpts": int(eff_verbatim),
                    "permits_ai_synthesis": int(bool(permits_ai_synthesis)),
                    "permits_redistribution": int(eff_redist),
                    "expires_at": expires_at,
                },
            )
            prov_id = str(prov["event_id"])
            tr_fk = transcript_id if (transcript_id and self.get_transcript(transcript_id) is not None) else None
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO transcript_authorization_attestations (
                        attestation_id, resource_id, transcript_id, language_bcp47, track_kind,
                        authorization_basis, license_status, license_spdx,
                        evidence_type, evidence_reference, evidence_url, evidence_sha256,
                        permits_metadata_indexing, permits_transcript_storage,
                        permits_verbatim_excerpts, permits_ai_synthesis, permits_redistribution,
                        reviewed_by, reviewed_at, verification_status, review_notes,
                        expires_at, withdrawn_at, withdrawn_by, withdrawal_reason,
                        revision_number, provenance_event_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 1, ?, ?, ?)
                    """,
                    (
                        attestation_id,
                        resource_id,
                        tr_fk,
                        lang_norm,
                        kind_norm,
                        authorization_basis,
                        license_status,
                        resolved_spdx,
                        evidence_type,
                        evidence_reference.strip(),
                        canonical_ev_url,
                        norm_ev_sha256,
                        int(bool(permits_metadata_indexing)),
                        int(eff_storage),
                        int(eff_verbatim),
                        int(bool(permits_ai_synthesis)),
                        int(eff_redist),
                        reviewed_by.strip(),
                        rev_at,
                        effective_status,
                        final_notes,
                        expires_at,
                        prov_id,
                        now,
                        now,
                    ),
                )
            else:
                next_rev = int(existing["revision_number"]) + 1
                conn.execute(
                    """
                    UPDATE transcript_authorization_attestations
                       SET transcript_id = COALESCE(?, transcript_id),
                           language_bcp47 = ?,
                           track_kind = ?,
                           authorization_basis = ?,
                           license_status = ?,
                           license_spdx = ?,
                           evidence_type = ?,
                           evidence_reference = ?,
                           evidence_url = ?,
                           evidence_sha256 = ?,
                           permits_metadata_indexing = ?,
                           permits_transcript_storage = ?,
                           permits_verbatim_excerpts = ?,
                           permits_ai_synthesis = ?,
                           permits_redistribution = ?,
                           reviewed_by = ?,
                           reviewed_at = ?,
                           verification_status = ?,
                           review_notes = ?,
                           expires_at = ?,
                           withdrawn_at = NULL,
                           withdrawn_by = NULL,
                           withdrawal_reason = NULL,
                           revision_number = ?,
                           provenance_event_id = ?,
                           updated_at = ?
                     WHERE attestation_id = ?
                    """,
                    (
                        tr_fk,
                        lang_norm,
                        kind_norm,
                        authorization_basis,
                        license_status,
                        resolved_spdx,
                        evidence_type,
                        evidence_reference.strip(),
                        canonical_ev_url,
                        norm_ev_sha256,
                        int(bool(permits_metadata_indexing)),
                        int(eff_storage),
                        int(eff_verbatim),
                        int(bool(permits_ai_synthesis)),
                        int(eff_redist),
                        reviewed_by.strip(),
                        rev_at,
                        effective_status,
                        final_notes,
                        expires_at,
                        next_rev,
                        prov_id,
                        now,
                        attestation_id,
                    ),
                )
        row = self.get_transcript_attestation(attestation_id)
        assert row is not None
        return row

    def withdraw_transcript_attestation(
        self,
        attestation_id: str,
        *,
        reason: str,
        actor: str = "rights_governance_officer",
    ) -> dict[str, Any]:
        """Withdraw an authorization attestation and cascade revocation/tombstone to all dependent transcripts."""
        if not reason or not reason.strip():
            raise ValueError("Attestation withdrawal reason must be non-empty")
        if not actor or not actor.strip():
            raise ValueError("actor must be non-empty")
        existing = self.get_transcript_attestation(attestation_id)
        if existing is None:
            raise KeyError(f"Unknown attestation_id: {attestation_id!r}")

        now = utc_now()
        resource_id = str(existing["resource_id"])
        resource_row = self.get_resource(resource_id=resource_id)
        source_id = str(resource_row["source_id"]) if resource_row else None

        with self.transaction() as conn:
            # First tombstone any transcripts authorized by this attestation before revoking attestation flags
            linked_transcripts = conn.execute(
                """
                SELECT transcript_id
                  FROM transcripts
                 WHERE authorization_attestation_id = ?
                    OR (transcript_id = ? AND ? IS NOT NULL)
                """,
                (attestation_id, existing.get("transcript_id"), existing.get("transcript_id")),
            ).fetchall()
            tombstoned_ids: list[str] = []
            for r in linked_transcripts:
                tid = str(r[0])
                self.tombstone_transcript_on_takedown(
                    tid,
                    reason=f"Authorization attestation {attestation_id} withdrawn: {reason.strip()}",
                    actor=actor.strip(),
                )
                tombstoned_ids.append(tid)

            next_rev = int(existing["revision_number"]) + 1
            prov = self.record_provenance(
                event_type="TRANSCRIPT_ATTESTATION_WITHDRAWN",
                acquisition_method="FIRST_PARTY_CURATED_IMPORT",
                actor=actor.strip(),
                status="withdrawn",
                source_id=source_id,
                resource_id=resource_id,
                source_url=existing.get("evidence_url")
                or (str(resource_row["canonical_url"]) if resource_row else None),
                details={
                    "attestation_id": attestation_id,
                    "previous_status": str(existing["verification_status"]),
                    "withdrawal_reason": reason.strip(),
                    "tombstoned_transcripts": tombstoned_ids,
                },
            )
            conn.execute(
                """
                UPDATE transcript_authorization_attestations
                   SET verification_status = 'WITHDRAWN',
                       permits_transcript_storage = 0,
                       permits_verbatim_excerpts = 0,
                       permits_ai_synthesis = 0,
                       permits_redistribution = 0,
                       withdrawn_at = ?,
                       withdrawn_by = ?,
                       withdrawal_reason = ?,
                       revision_number = ?,
                       provenance_event_id = ?,
                       updated_at = ?
                 WHERE attestation_id = ?
                """,
                (
                    now,
                    actor.strip(),
                    reason.strip(),
                    next_rev,
                    str(prov["event_id"]),
                    now,
                    attestation_id,
                ),
            )
        updated = self.get_transcript_attestation(attestation_id)
        assert updated is not None
        return updated

    def get_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        """Retrieve a `transcripts` row by `transcript_id`, or None if not found."""
        row = self.conn.execute(
            "SELECT * FROM transcripts WHERE transcript_id = ?",
            (transcript_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_transcripts(
        self,
        *,
        resource_id: str | None = None,
        source_id: str | None = None,
        ingestion_state: str | None = None,
        license_status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List transcripts matching optional filters ordered by `created_at, transcript_id`."""
        clauses: list[str] = []
        params: list[Any] = []
        if resource_id is not None:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if ingestion_state is not None:
            clauses.append("ingestion_state = ?")
            params.append(ingestion_state)
        if license_status is not None:
            clauses.append("license_status = ?")
            params.append(license_status)

        query = "SELECT * FROM transcripts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, transcript_id"
        rows = self.conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_transcript_versions(self, transcript_id: str) -> list[dict[str, Any]]:
        """Return all `transcript_versions` for `transcript_id` ordered by `version_number` ascending."""
        rows = self.conn.execute(
            """
            SELECT * FROM transcript_versions
             WHERE transcript_id = ?
             ORDER BY version_number ASC
            """,
            (transcript_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_transcript_segments(
        self,
        transcript_id: str,
        *,
        transcript_version_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ordered `transcript_segments` for `transcript_id` (defaults to `latest_transcript_version_id`)."""
        target_version_id = transcript_version_id
        if target_version_id is None:
            tr = self.get_transcript(transcript_id)
            if tr is None or not tr.get("latest_transcript_version_id"):
                return []
            target_version_id = str(tr["latest_transcript_version_id"])
        rows = self.conn.execute(
            """
            SELECT * FROM transcript_segments
             WHERE transcript_id = ? AND transcript_version_id = ?
             ORDER BY sequence_index ASC
            """,
            (transcript_id, target_version_id),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["quality_flags"] = json.loads(item.get("quality_flags_json") or "[]")
            results.append(item)
        return results

    def register_transcript_discovery(
        self,
        *,
        resource_id: str,
        language_bcp47: str = "en",
        track_kind: str = "PRIMARY",
        origin_type: str = "AUTHOR_PROVIDED",
        acquisition_method: str = "METADATA_AND_LINK_ONLY",
        caption_format: str = "NONE",
        speaker_attribution_quality: str = "UNKNOWN",
        license_status: str = "UNKNOWN",
        license_spdx: str | None = None,
        authorization_basis: str = "NONE",
        attribution_text: str,
        ingestion_state: str = "DISCOVERED",
        media_canonical_url: str | None = None,
        duration_ms: int | None = None,
        block_or_review_reason: str | None = None,
        actor: str = "transcript_registry",
    ) -> dict[str, Any]:
        """Register or update a transcript discovery/rights-pending record without storing transcript text."""
        resource_row = self.get_resource(resource_id=resource_id)
        if resource_row is None:
            raise ValueError(f"Unknown parent resource_id: {resource_id!r}")
        lang = validate_bcp47_language_tag(language_bcp47)
        kind = track_kind.strip().upper()
        if kind not in VALID_TRANSCRIPT_TRACK_KINDS:
            raise ValueError(f"Invalid track_kind: {track_kind!r}")
        if origin_type not in VALID_TRANSCRIPT_ORIGIN_TYPES:
            raise ValueError(f"Invalid origin_type: {origin_type!r}")
        if acquisition_method not in VALID_TRANSCRIPT_ACQUISITION_METHODS:
            raise ValueError(f"Invalid acquisition_method: {acquisition_method!r}")
        if caption_format not in VALID_CAPTION_FORMATS:
            raise ValueError(f"Invalid caption_format: {caption_format!r}")
        if speaker_attribution_quality not in VALID_SPEAKER_ATTRIBUTION_QUALITIES:
            raise ValueError(f"Invalid speaker_attribution_quality: {speaker_attribution_quality!r}")
        if license_status not in VALID_TRANSCRIPT_LICENSE_STATUSES:
            raise ValueError(f"Invalid license_status: {license_status!r}")
        if authorization_basis not in VALID_AUTHORIZATION_BASES:
            raise ValueError(f"Invalid authorization_basis: {authorization_basis!r}")
        if ingestion_state not in (
            "DISCOVERED",
            "RIGHTS_PENDING",
            "BLOCKED_BY_POLICY",
            "METADATA_ONLY_LINKED",
        ):
            raise ValueError(
                f"register_transcript_discovery only supports non-text states, got {ingestion_state!r}"
            )
        if not attribution_text or not attribution_text.strip():
            raise ValueError("attribution_text must be non-empty")

        media_url = validate_and_canonicalize_url(
            media_canonical_url or str(resource_row["canonical_url"])
        )
        transcript_id = compute_transcript_id(resource_id, lang, kind)
        source_id = str(resource_row["source_id"])
        storage_status = (
            "METADATA_AND_LINK_ONLY"
            if ingestion_state == "METADATA_ONLY_LINKED"
            else (
                "BLOCKED_NO_AUTHORIZATION"
                if ingestion_state == "BLOCKED_BY_POLICY"
                else "NOT_STORED"
            )
        )
        now = utc_now()
        with self.transaction() as conn:
            existing = self.get_transcript(transcript_id)
            if existing is not None:
                validate_transcript_state_transition(
                    str(existing["ingestion_state"]), ingestion_state
                )
                conn.execute(
                    """
                    UPDATE transcripts
                       SET media_canonical_url = ?,
                           origin_type = ?,
                           acquisition_method = ?,
                           caption_format = ?,
                           speaker_attribution_quality = ?,
                           license_status = ?,
                           license_spdx = ?,
                           authorization_basis = ?,
                           permits_transcript_storage = 0,
                           permits_verbatim_excerpts = 0,
                           attribution_text = ?,
                           ingestion_state = ?,
                           transcript_storage_status = ?,
                           block_or_review_reason = ?,
                           duration_ms = COALESCE(?, duration_ms),
                           updated_at = ?
                     WHERE transcript_id = ?
                    """,
                    (
                        media_url,
                        origin_type,
                        acquisition_method,
                        caption_format,
                        speaker_attribution_quality,
                        license_status,
                        license_spdx,
                        authorization_basis,
                        attribution_text.strip(),
                        ingestion_state,
                        storage_status,
                        block_or_review_reason,
                        duration_ms,
                        now,
                        transcript_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO transcripts (
                        transcript_id, resource_id, source_id, media_canonical_url,
                        language_bcp47, track_kind, origin_type, acquisition_method,
                        caption_format, speaker_attribution_quality, license_status,
                        license_spdx, authorization_basis, permits_transcript_storage,
                        permits_verbatim_excerpts, permits_ai_synthesis, attribution_text,
                        ingestion_state, transcript_storage_status, block_or_review_reason,
                        latest_transcript_version_id, segment_count, duration_ms,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?, ?, ?, NULL, 0, ?, ?, ?)
                    """,
                    (
                        transcript_id,
                        resource_id,
                        source_id,
                        media_url,
                        lang,
                        kind,
                        origin_type,
                        acquisition_method,
                        caption_format,
                        speaker_attribution_quality,
                        license_status,
                        license_spdx,
                        authorization_basis,
                        attribution_text.strip(),
                        ingestion_state,
                        storage_status,
                        block_or_review_reason,
                        duration_ms,
                        now,
                        now,
                    ),
                )
            self.record_provenance(
                event_type=f"TRANSCRIPT_{ingestion_state}",
                acquisition_method=acquisition_method,
                actor=actor,
                status="recorded",
                source_id=source_id,
                resource_id=resource_id,
                source_url=media_url,
                details={
                    "transcript_id": transcript_id,
                    "language_bcp47": lang,
                    "track_kind": kind,
                    "license_snapshot": license_spdx or license_status,
                    "ingestion_state": ingestion_state,
                    "transcript_storage_status": storage_status,
                    "reason": block_or_review_reason,
                },
            )
        result = self.get_transcript(transcript_id)
        assert result is not None
        return result

    def transition_transcript_state(
        self,
        transcript_id: str,
        new_state: str,
        *,
        reason: str | None = None,
        actor: str = "transcript_state_machine",
    ) -> dict[str, Any]:
        """Validate and execute an explicit state transition on an existing transcript record."""
        existing = self.get_transcript(transcript_id)
        if existing is None:
            raise KeyError(f"Unknown transcript_id: {transcript_id!r}")
        prev_state = str(existing["ingestion_state"])
        validate_transcript_state_transition(prev_state, new_state)
        if new_state == "WITHDRAWN_OR_TOMBSTONED":
            return self.tombstone_transcript_on_takedown(
                transcript_id, reason=reason or "Transitioned to WITHDRAWN_OR_TOMBSTONED", actor=actor
            )

        now = utc_now()
        permits_storage = int(existing["permits_transcript_storage"])
        permits_excerpts = int(existing["permits_verbatim_excerpts"])
        permits_redist = int(existing.get("permits_redistribution", 0))
        storage_status = str(existing["transcript_storage_status"])
        if new_state in ("BLOCKED_BY_POLICY", "METADATA_ONLY_LINKED", "RIGHTS_PENDING", "DISCOVERED"):
            permits_storage = 0
            permits_excerpts = 0
            permits_redist = 0
            if new_state == "BLOCKED_BY_POLICY":
                storage_status = "BLOCKED_NO_AUTHORIZATION"
            elif new_state == "METADATA_ONLY_LINKED":
                storage_status = "METADATA_AND_LINK_ONLY"
            else:
                storage_status = "NOT_STORED"

        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE transcripts
                   SET ingestion_state = ?,
                       permits_transcript_storage = ?,
                       permits_verbatim_excerpts = ?,
                       permits_redistribution = ?,
                       transcript_storage_status = ?,
                       block_or_review_reason = COALESCE(?, block_or_review_reason),
                       updated_at = ?
                 WHERE transcript_id = ?
                """,
                (
                    new_state,
                    permits_storage,
                    permits_excerpts,
                    permits_redist,
                    storage_status,
                    reason,
                    now,
                    transcript_id,
                ),
            )
            self.record_provenance(
                event_type="TRANSCRIPT_STATE_TRANSITION",
                acquisition_method=str(existing["acquisition_method"]),
                actor=actor,
                status="transitioned",
                source_id=str(existing["source_id"]),
                resource_id=str(existing["resource_id"]),
                source_url=str(existing["media_canonical_url"]),
                details={
                    "transcript_id": transcript_id,
                    "previous_state": prev_state,
                    "new_state": new_state,
                    "reason": reason,
                },
            )
        updated = self.get_transcript(transcript_id)
        assert updated is not None
        return updated

    def import_local_transcript(
        self,
        *,
        resource_id: str,
        raw_content: str | None = None,
        language_bcp47: str = "en",
        track_kind: str = "PRIMARY",
        origin_type: str = "MANUAL_CURATED_IMPORT",
        acquisition_method: str = "FIRST_PARTY_CURATED_IMPORT",
        caption_format: str = "WEBVTT",
        speaker_attribution_quality: str = "UNKNOWN",
        license_status: str = "UNKNOWN",
        license_spdx: str | None = None,
        authorization_basis: str = "NONE",
        attribution_text: str,
        permits_transcript_storage: bool = False,
        permits_verbatim_excerpts: bool = False,
        permits_ai_synthesis: bool = True,
        permits_redistribution: bool = False,
        attestation_id: str | None = None,
        attestation: dict[str, Any] | TranscriptAuthorizationAttestation | None = None,
        media_canonical_url: str | None = None,
        source_caption_url: str | None = None,
        duration_ms: int | None = None,
        asr_model_name: str | None = None,
        asr_model_version: str | None = None,
        asr_compute_device: str | None = None,
        actor: str = "local_transcript_importer",
        run_id: str | None = None,
    ) -> TranscriptImportResult:
        """Import a local transcript for an existing resource with strict offline policy gating and auditable authorization attestation.

        - Performs zero network requests.
        - Evaluates the conjunctive policy gate AND verifies an auditable `transcript_authorization_attestations`
          record before storing any transcript text.
        - Caller-supplied flags (`permits_transcript_storage=True`, `license_status='VERIFIED_PERMISSIVE'`,
          `authorization_basis=...`) NEVER authorize transcript storage on their own without a verified,
          non-withdrawn, non-expired attestation matching `resource_id` and scope.
        - If unauthorized or unresolved, records the blocked or metadata-only state on `transcripts` and
          `provenance_events` and stores ZERO rows in `transcript_versions` or `transcript_segments`.
        - Never logs or persists full transcript text in `provenance_events`.
        """
        resource_row = self.get_resource(resource_id=resource_id)
        if resource_row is None:
            raise ValueError(f"Parent resource_id does not exist in resources table: {resource_id!r}")

        lang = validate_bcp47_language_tag(language_bcp47)
        kind = (track_kind or "").strip().upper()
        if kind not in VALID_TRANSCRIPT_TRACK_KINDS:
            raise ValueError(f"Invalid track_kind {track_kind!r}; expected one of {VALID_TRANSCRIPT_TRACK_KINDS}")
        if origin_type not in VALID_TRANSCRIPT_ORIGIN_TYPES:
            raise ValueError(f"Invalid origin_type {origin_type!r}; expected one of {VALID_TRANSCRIPT_ORIGIN_TYPES}")
        if acquisition_method not in VALID_TRANSCRIPT_ACQUISITION_METHODS:
            raise ValueError(
                f"Invalid acquisition_method {acquisition_method!r}; expected one of {VALID_TRANSCRIPT_ACQUISITION_METHODS}"
            )
        fmt = (caption_format or "").strip().upper()
        if fmt not in VALID_CAPTION_FORMATS:
            raise ValueError(f"Invalid caption_format {caption_format!r}; expected one of {VALID_CAPTION_FORMATS}")
        if speaker_attribution_quality not in VALID_SPEAKER_ATTRIBUTION_QUALITIES:
            raise ValueError(f"Invalid speaker_attribution_quality: {speaker_attribution_quality!r}")

        media_url = validate_and_canonicalize_url(
            media_canonical_url or str(resource_row["canonical_url"])
        )
        canonical_caption_url = (
            validate_and_canonicalize_url(source_caption_url) if source_caption_url else None
        )
        source_id = str(resource_row["source_id"])
        transcript_id = compute_transcript_id(resource_id, lang, kind)

        resolved_attestation: dict[str, Any] | None = None
        requested_att_id = (attestation_id or "").strip() or None
        if attestation is not None:
            att_candidate = (
                attestation.to_dict()
                if isinstance(attestation, TranscriptAuthorizationAttestation)
                else dict(attestation)
            )
            cand_id = str(att_candidate.get("attestation_id") or "").strip()
            if cand_id:
                existing_att = self.get_transcript_attestation(cand_id)
                if existing_att is not None:
                    resolved_attestation = existing_att
                else:
                    resolved_attestation = att_candidate
            else:
                resolved_attestation = att_candidate
            # If the caller supplied an inline attestation dict/object that has not yet been persisted
            # and its target resource exists in `resources`, persist it via `record_transcript_attestation`
            # so the audit trail and foreign key on `transcripts.authorization_attestation_id` are satisfied.
            if (
                resolved_attestation is not None
                and (not cand_id or self.get_transcript_attestation(cand_id) is None)
                and resolved_attestation.get("resource_id")
                and self.get_resource(resource_id=str(resolved_attestation["resource_id"])) is not None
                and resolved_attestation.get("evidence_type") in VALID_ATTESTATION_EVIDENCE_TYPES
                and resolved_attestation.get("evidence_reference")
                and resolved_attestation.get("reviewed_by")
            ):
                persisted = self.record_transcript_attestation(
                    resource_id=str(resolved_attestation["resource_id"]),
                    transcript_id=resolved_attestation.get("transcript_id"),
                    language_bcp47=resolved_attestation.get("language_bcp47"),
                    track_kind=resolved_attestation.get("track_kind"),
                    authorization_basis=str(resolved_attestation.get("authorization_basis") or "NONE"),
                    license_status=str(resolved_attestation.get("license_status") or "UNKNOWN"),
                    license_spdx=resolved_attestation.get("license_spdx"),
                    evidence_type=str(resolved_attestation["evidence_type"]),
                    evidence_reference=str(resolved_attestation["evidence_reference"]),
                    source_evidence_url=resolved_attestation.get("source_evidence_url"),
                    evidence_sha256=resolved_attestation.get("evidence_sha256"),
                    permits_metadata_indexing=bool(resolved_attestation.get("permits_metadata_indexing", 1)),
                    permits_transcript_storage=bool(resolved_attestation.get("permits_transcript_storage", 0)),
                    permits_verbatim_excerpts=bool(resolved_attestation.get("permits_verbatim_excerpts", 0)),
                    permits_ai_synthesis=bool(resolved_attestation.get("permits_ai_synthesis", 1)),
                    permits_redistribution=bool(resolved_attestation.get("permits_redistribution", 0)),
                    reviewed_by=str(resolved_attestation["reviewed_by"]),
                    reviewed_at=resolved_attestation.get("reviewed_at"),
                    verification_status=str(resolved_attestation.get("verification_status") or "VERIFIED"),
                    review_notes=resolved_attestation.get("review_notes"),
                    expires_at=resolved_attestation.get("expires_at"),
                )
                if resolved_attestation.get("withdrawn_at") or str(resolved_attestation.get("verification_status") or "").upper() == "WITHDRAWN":
                    persisted = self.withdraw_transcript_attestation(
                        str(persisted["attestation_id"]),
                        reason=str(resolved_attestation.get("withdrawal_reason") or "Attestation supplied in withdrawn state"),
                        actor=str(resolved_attestation["reviewed_by"]),
                    )
                resolved_attestation = persisted
                requested_att_id = str(persisted["attestation_id"])
        elif requested_att_id is not None:
            resolved_attestation = self.get_transcript_attestation(requested_att_id)

        policy = evaluate_transcript_storage_policy(
            resource_row=resource_row,
            acquisition_method=acquisition_method,
            license_status=license_status,
            license_spdx=license_spdx,
            authorization_basis=authorization_basis,
            attribution_text=attribution_text,
            requested_transcript_storage=bool(permits_transcript_storage),
            requested_verbatim_excerpts=bool(permits_verbatim_excerpts),
            requested_ai_synthesis=bool(permits_ai_synthesis),
            requested_redistribution=bool(permits_redistribution),
            attestation=resolved_attestation,
            requested_attestation_id=requested_att_id,
            expected_transcript_id=transcript_id,
            expected_language_bcp47=lang,
            expected_track_kind=kind,
        )

        effective_license_status = (
            str(resolved_attestation.get("license_status") or "UNKNOWN")
            if (resolved_attestation is not None and license_status == "UNKNOWN")
            else license_status
        )
        effective_basis = (
            str(resolved_attestation.get("authorization_basis") or "NONE")
            if (resolved_attestation is not None and authorization_basis == "NONE")
            else authorization_basis
        )
        effective_spdx = (
            (resolved_attestation.get("license_spdx") or None)
            if (resolved_attestation is not None and not license_spdx)
            else license_spdx
        )
        # Note: Migration 6 defined `transcripts.authorization_basis` CHECK with `'OPEN_LICENSE_VERIFIED'`
        # for open/public-domain licenses, while `transcript_authorization_attestations` stores
        # `'PUBLIC_DOMAIN_VERIFIED'` directly. Map `'PUBLIC_DOMAIN_VERIFIED'` -> `'OPEN_LICENSE_VERIFIED'`
        # on the `transcripts` row (defaulting `license_spdx` to `'CC0-1.0'`) so Migration 6 CHECK constraints hold.
        db_transcript_basis = (
            "OPEN_LICENSE_VERIFIED"
            if effective_basis == "PUBLIC_DOMAIN_VERIFIED"
            else effective_basis
        )
        db_transcript_spdx = (
            (effective_spdx or "CC0-1.0")
            if effective_basis == "PUBLIC_DOMAIN_VERIFIED"
            else effective_spdx
        )
        fk_attestation_id: str | None = None
        if policy.authorization_attestation_id and resolved_attestation is not None:
            att_res_id = str(resolved_attestation.get("resource_id") or "")
            att_tr_id = str(resolved_attestation.get("transcript_id") or "")
            att_lang = str(resolved_attestation.get("language_bcp47") or "").strip().lower()
            att_kind = str(resolved_attestation.get("track_kind") or "").strip().upper()
            if (
                att_res_id == resource_id
                and (not att_tr_id or att_tr_id == transcript_id)
                and (not att_lang or validate_bcp47_language_tag(att_lang) == lang)
                and (not att_kind or att_kind == kind)
            ):
                fk_attestation_id = policy.authorization_attestation_id

        existing = self.get_transcript(transcript_id)
        if existing is not None and existing["ingestion_state"] == "WITHDRAWN_OR_TOMBSTONED":
            raise ValueError(
                f"Transcript {transcript_id} is WITHDRAWN_OR_TOMBSTONED and cannot be re-imported without explicit administrative reset."
            )

        # Policy gate denied or metadata-only mode: record status & provenance without storing text
        if not policy.permits_transcript_storage:
            now = utc_now()
            with self.transaction() as conn:
                if existing is not None:
                    # Purge any previously stored segments/syntheses and tombstone versions
                    conn.execute(
                        "DELETE FROM transcript_segments WHERE transcript_id = ?",
                        (transcript_id,),
                    )
                    conn.execute(
                        "DELETE FROM transcript_syntheses WHERE transcript_id = ?",
                        (transcript_id,),
                    )
                    conn.execute(
                        "UPDATE transcript_versions SET status = 'TOMBSTONED' WHERE transcript_id = ?",
                        (transcript_id,),
                    )
                    conn.execute(
                        """
                        UPDATE transcripts
                           SET media_canonical_url = ?,
                               origin_type = ?,
                               acquisition_method = ?,
                               caption_format = ?,
                               speaker_attribution_quality = ?,
                               license_status = ?,
                               license_spdx = ?,
                               authorization_basis = ?,
                               authorization_attestation_id = ?,
                               permits_transcript_storage = 0,
                               permits_verbatim_excerpts = 0,
                               permits_ai_synthesis = ?,
                               permits_redistribution = 0,
                               attribution_text = ?,
                               ingestion_state = ?,
                               transcript_storage_status = ?,
                               block_or_review_reason = ?,
                               latest_transcript_version_id = NULL,
                               segment_count = 0,
                               duration_ms = COALESCE(?, duration_ms),
                               updated_at = ?
                         WHERE transcript_id = ?
                        """,
                        (
                            media_url,
                            origin_type,
                            acquisition_method,
                            fmt if acquisition_method != "METADATA_AND_LINK_ONLY" else "NONE",
                            speaker_attribution_quality,
                            effective_license_status,
                            db_transcript_spdx,
                            db_transcript_basis,
                            fk_attestation_id,
                            int(policy.permits_ai_synthesis),
                            attribution_text.strip(),
                            policy.ingestion_state,
                            policy.transcript_storage_status,
                            policy.block_or_review_reason,
                            duration_ms,
                            now,
                            transcript_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO transcripts (
                            transcript_id, resource_id, source_id, media_canonical_url,
                            language_bcp47, track_kind, origin_type, acquisition_method,
                            caption_format, speaker_attribution_quality, license_status,
                            license_spdx, authorization_basis, authorization_attestation_id,
                            permits_transcript_storage, permits_verbatim_excerpts,
                            permits_ai_synthesis, permits_redistribution, attribution_text,
                            ingestion_state, transcript_storage_status, block_or_review_reason,
                            latest_transcript_version_id, segment_count, duration_ms,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, ?, ?, ?, NULL, 0, ?, ?, ?)
                        """,
                        (
                            transcript_id,
                            resource_id,
                            source_id,
                            media_url,
                            lang,
                            kind,
                            origin_type,
                            acquisition_method,
                            fmt if acquisition_method != "METADATA_AND_LINK_ONLY" else "NONE",
                            speaker_attribution_quality,
                            effective_license_status,
                            db_transcript_spdx,
                            db_transcript_basis,
                            fk_attestation_id,
                            int(policy.permits_ai_synthesis),
                            attribution_text.strip(),
                            policy.ingestion_state,
                            policy.transcript_storage_status,
                            policy.block_or_review_reason,
                            duration_ms,
                            now,
                            now,
                        ),
                    )
                prov_event_type = (
                    "TRANSCRIPT_METADATA_ONLY_LINKED"
                    if policy.ingestion_state == "METADATA_ONLY_LINKED"
                    else "TRANSCRIPT_POLICY_BLOCKED"
                )
                prov_status = (
                    "metadata_only"
                    if policy.ingestion_state == "METADATA_ONLY_LINKED"
                    else "blocked"
                )
                prov = self.record_provenance(
                    event_type=prov_event_type,
                    acquisition_method=acquisition_method,
                    actor=actor,
                    status=prov_status,
                    source_id=source_id,
                    resource_id=resource_id,
                    run_id=run_id,
                    source_url=media_url,
                    details={
                        "transcript_id": transcript_id,
                        "language_bcp47": lang,
                        "track_kind": kind,
                        "license_status": effective_license_status,
                        "license_snapshot": db_transcript_spdx or effective_license_status,
                        "authorization_basis": effective_basis,
                        "authorization_attestation_id": fk_attestation_id or requested_att_id,
                        "ingestion_state": policy.ingestion_state,
                        "transcript_storage_status": policy.transcript_storage_status,
                        "reason": policy.block_or_review_reason,
                        "stored_segments": 0,
                    },
                )
                event_id = str(prov["event_id"])
            return TranscriptImportResult(
                status=prov_status,
                transcript_id=transcript_id,
                resource_id=resource_id,
                source_id=source_id,
                language_bcp47=lang,
                track_kind=kind,
                ingestion_state=policy.ingestion_state,
                transcript_storage_status=policy.transcript_storage_status,
                permits_transcript_storage=False,
                permits_verbatim_excerpts=False,
                permits_redistribution=False,
                authorization_attestation_id=fk_attestation_id,
                transcript_version_id=None,
                version_number=None,
                stored_segments=0,
                reason=policy.block_or_review_reason,
                provenance_event_id=event_id,
            )

        # Policy permits storage: validate and parse transcript content
        if raw_content is None or not isinstance(raw_content, str) or not raw_content.strip():
            raise ValueError("TV-01 validation failed: raw_content must be a non-empty string when storage is permitted")

        cues = parse_transcript_content(raw_content, fmt)
        needs_review, review_reason = validate_transcript_cues(
            cues,
            language_bcp47=lang,
            attribution_text=attribution_text,
            license_status=effective_license_status,
            duration_ms=duration_ms,
        )

        raw_content_sha256 = sha256_hex(raw_content)
        normalized_join = "\n".join(
            f"{c.sequence_index}:{c.start_ms}:{c.end_ms}:{c.speaker_label or ''}:{c.normalized_segment_text}"
            for c in cues
        )
        normalized_text_sha256 = sha256_hex(normalized_join)

        target_ingestion_state = "NEEDS_REVIEW" if needs_review else "INGESTED"
        target_storage_status = (
            "STORED_PLAIN_TEXT" if fmt == "PLAIN_TEXT" else "STORED_TIMED_SEGMENTS"
        )
        inferred_duration_ms = duration_ms if duration_ms is not None else max(c.end_ms for c in cues)

        with self.transaction() as conn:
            # Check idempotency against existing versions for this transcript_id
            existing_ver = conn.execute(
                """
                SELECT transcript_version_id, version_number, status
                  FROM transcript_versions
                 WHERE transcript_id = ? AND normalized_text_sha256 = ?
                """,
                (transcript_id, normalized_text_sha256),
            ).fetchone()

            if existing_ver is not None and existing is not None:
                prov = self.record_provenance(
                    event_type="TRANSCRIPT_IMPORT_SKIPPED_UNCHANGED",
                    acquisition_method=acquisition_method,
                    actor=actor,
                    status="skipped",
                    source_id=source_id,
                    resource_id=resource_id,
                    version_id=resource_row.get("current_version_id"),
                    run_id=run_id,
                    source_url=media_url,
                    content_hash=normalized_text_sha256,
                    details={
                        "transcript_id": transcript_id,
                        "transcript_version_id": str(existing_ver["transcript_version_id"]),
                        "version_number": int(existing_ver["version_number"]),
                        "authorization_attestation_id": fk_attestation_id,
                        "license_snapshot": db_transcript_spdx or effective_license_status,
                        "segment_count": len(cues),
                    },
                )
                event_id = str(prov["event_id"])
                return TranscriptImportResult(
                    status="skipped_unchanged",
                    transcript_id=transcript_id,
                    resource_id=resource_id,
                    source_id=source_id,
                    language_bcp47=lang,
                    track_kind=kind,
                    ingestion_state=str(existing["ingestion_state"]),
                    transcript_storage_status=str(existing["transcript_storage_status"]),
                    permits_transcript_storage=bool(existing["permits_transcript_storage"]),
                    permits_verbatim_excerpts=bool(existing["permits_verbatim_excerpts"]),
                    permits_redistribution=bool(existing.get("permits_redistribution", 0)),
                    authorization_attestation_id=fk_attestation_id or existing.get("authorization_attestation_id"),
                    transcript_version_id=str(existing_ver["transcript_version_id"]),
                    version_number=int(existing_ver["version_number"]),
                    stored_segments=int(existing["segment_count"]),
                    reason=existing.get("block_or_review_reason"),
                    provenance_event_id=event_id,
                )

            now = utc_now()
            max_ver_row = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) FROM transcript_versions WHERE transcript_id = ?",
                (transcript_id,),
            ).fetchone()
            next_version_number = int(max_ver_row[0]) + 1
            transcript_version_id = compute_transcript_version_id(
                transcript_id=transcript_id,
                version_number=next_version_number,
                raw_content_sha256=raw_content_sha256,
                normalized_text_sha256=normalized_text_sha256,
                recorded_date=now,
            )

            # First ensure parent `transcripts` row is in an authorized state (`READY_FOR_INGESTION`)
            # with a valid `authorization_attestation_id` before inserting `transcript_versions`
            # and `transcript_segments` (enforced by SQLite triggers in Migrations 6 and 7).
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO transcripts (
                        transcript_id, resource_id, source_id, media_canonical_url,
                        language_bcp47, track_kind, origin_type, acquisition_method,
                        caption_format, speaker_attribution_quality, license_status,
                        license_spdx, authorization_basis, authorization_attestation_id,
                        permits_transcript_storage, permits_verbatim_excerpts,
                        permits_ai_synthesis, permits_redistribution, attribution_text,
                        ingestion_state, transcript_storage_status, block_or_review_reason,
                        latest_transcript_version_id, segment_count, duration_ms,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'READY_FOR_INGESTION', 'NOT_STORED', ?, NULL, 0, ?, ?, ?)
                    """,
                    (
                        transcript_id,
                        resource_id,
                        source_id,
                        media_url,
                        lang,
                        kind,
                        origin_type,
                        acquisition_method,
                        fmt,
                        speaker_attribution_quality,
                        effective_license_status,
                        db_transcript_spdx,
                        db_transcript_basis,
                        fk_attestation_id,
                        int(policy.permits_verbatim_excerpts),
                        int(policy.permits_ai_synthesis),
                        int(policy.permits_redistribution),
                        attribution_text.strip(),
                        review_reason,
                        inferred_duration_ms,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE transcripts
                       SET media_canonical_url = ?,
                           origin_type = ?,
                           acquisition_method = ?,
                           caption_format = ?,
                           speaker_attribution_quality = ?,
                           license_status = ?,
                           license_spdx = ?,
                           authorization_basis = ?,
                           authorization_attestation_id = ?,
                           permits_transcript_storage = 1,
                           permits_verbatim_excerpts = ?,
                           permits_ai_synthesis = ?,
                           permits_redistribution = ?,
                           attribution_text = ?,
                           ingestion_state = 'READY_FOR_INGESTION',
                           block_or_review_reason = ?,
                           duration_ms = COALESCE(?, duration_ms),
                           updated_at = ?
                     WHERE transcript_id = ?
                    """,
                    (
                        media_url,
                        origin_type,
                        acquisition_method,
                        fmt,
                        speaker_attribution_quality,
                        effective_license_status,
                        db_transcript_spdx,
                        db_transcript_basis,
                        fk_attestation_id,
                        int(policy.permits_verbatim_excerpts),
                        int(policy.permits_ai_synthesis),
                        int(policy.permits_redistribution),
                        attribution_text.strip(),
                        review_reason,
                        inferred_duration_ms,
                        now,
                        transcript_id,
                    ),
                )

            # Mark any prior active versions as SUPERSEDED
            if next_version_number > 1:
                conn.execute(
                    """
                    UPDATE transcript_versions
                       SET status = 'SUPERSEDED'
                     WHERE transcript_id = ? AND status = 'ACTIVE'
                    """,
                    (transcript_id,),
                )

            word_count = sum(len(c.normalized_segment_text.split()) for c in cues)
            char_count = sum(len(c.normalized_segment_text) for c in cues)
            low_conf_count = sum(1 for c in cues if c.is_low_confidence)
            conf_vals = [
                float(c.token_confidence_mean)
                for c in cues
                if c.token_confidence_mean is not None
            ]
            mean_conf = (sum(conf_vals) / len(conf_vals)) if conf_vals else None

            conn.execute(
                """
                INSERT INTO transcript_versions (
                    transcript_version_id, transcript_id, version_number,
                    raw_content_sha256, normalized_text_sha256, normalizer_version,
                    asr_model_name, asr_model_version, asr_compute_device,
                    source_caption_url, segment_count, word_count, char_count,
                    mean_confidence, low_confidence_segment_count, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
                """,
                (
                    transcript_version_id,
                    transcript_id,
                    next_version_number,
                    raw_content_sha256,
                    normalized_text_sha256,
                    TRANSCRIPT_NORMALIZER_VERSION,
                    asr_model_name,
                    asr_model_version,
                    asr_compute_device,
                    canonical_caption_url,
                    len(cues),
                    word_count,
                    char_count,
                    mean_conf,
                    low_conf_count,
                    now,
                ),
            )

            for cue in cues:
                segment_id = compute_transcript_segment_id(
                    transcript_id=transcript_id,
                    sequence_index=cue.sequence_index,
                    version_number=next_version_number,
                )
                deep_link = compute_segment_deep_link_url(media_url, cue.start_ms)
                seg_sha256 = sha256_hex(
                    f"{cue.sequence_index}:{cue.start_ms}:{cue.end_ms}:{cue.speaker_label or ''}:{cue.normalized_segment_text}"
                )
                conn.execute(
                    """
                    INSERT INTO transcript_segments (
                        segment_id, transcript_id, transcript_version_id,
                        sequence_index, start_ms, end_ms,
                        original_segment_text, normalized_segment_text,
                        speaker_label, speaker_confidence,
                        token_confidence_mean, token_confidence_min,
                        is_low_confidence, quality_flags_json,
                        deep_link_url, segment_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment_id,
                        transcript_id,
                        transcript_version_id,
                        cue.sequence_index,
                        cue.start_ms,
                        cue.end_ms,
                        cue.original_segment_text,
                        cue.normalized_segment_text,
                        cue.speaker_label,
                        cue.speaker_confidence,
                        cue.token_confidence_mean,
                        cue.token_confidence_min,
                        int(bool(cue.is_low_confidence)),
                        json.dumps(cue.quality_flags, ensure_ascii=False),
                        deep_link,
                        seg_sha256,
                    ),
                )

            conn.execute(
                """
                UPDATE transcripts
                   SET ingestion_state = ?,
                       transcript_storage_status = ?,
                       latest_transcript_version_id = ?,
                       segment_count = ?,
                       updated_at = ?
                 WHERE transcript_id = ?
                """,
                (
                    target_ingestion_state,
                    target_storage_status,
                    transcript_version_id,
                    len(cues),
                    now,
                    transcript_id,
                ),
            )

            op_status = "updated" if next_version_number > 1 else "imported"
            event_type = (
                "TRANSCRIPT_VERSION_SUPERSEDED"
                if next_version_number > 1
                else "TRANSCRIPT_IMPORTED"
            )
            prov = self.record_provenance(
                event_type=event_type,
                acquisition_method=acquisition_method,
                actor=actor,
                status=op_status,
                source_id=source_id,
                resource_id=resource_id,
                version_id=resource_row.get("current_version_id"),
                run_id=run_id,
                source_url=media_url,
                content_hash=normalized_text_sha256,
                details={
                    "transcript_id": transcript_id,
                    "transcript_version_id": transcript_version_id,
                    "version_number": next_version_number,
                    "language_bcp47": lang,
                    "track_kind": kind,
                    "caption_format": fmt,
                    "license_snapshot": db_transcript_spdx or effective_license_status,
                    "authorization_basis": effective_basis,
                    "authorization_attestation_id": fk_attestation_id,
                    "permits_verbatim_excerpts": int(policy.permits_verbatim_excerpts),
                    "permits_redistribution": int(policy.permits_redistribution),
                    "segment_count": len(cues),
                    "word_count": word_count,
                    "char_count": char_count,
                    "raw_content_sha256": raw_content_sha256,
                    "normalized_text_sha256": normalized_text_sha256,
                    "ingestion_state": target_ingestion_state,
                    "transcript_storage_status": target_storage_status,
                },
            )
            event_id = str(prov["event_id"])

        return TranscriptImportResult(
            status=op_status,
            transcript_id=transcript_id,
            resource_id=resource_id,
            source_id=source_id,
            language_bcp47=lang,
            track_kind=kind,
            ingestion_state=target_ingestion_state,
            transcript_storage_status=target_storage_status,
            permits_transcript_storage=True,
            permits_verbatim_excerpts=policy.permits_verbatim_excerpts,
            permits_redistribution=policy.permits_redistribution,
            authorization_attestation_id=fk_attestation_id,
            transcript_version_id=transcript_version_id,
            version_number=next_version_number,
            stored_segments=len(cues),
            reason=review_reason,
            provenance_event_id=event_id,
        )

    def import_local_transcript_file(
        self,
        *,
        resource_id: str,
        transcript_path: Path | str | None,
        language_bcp47: str = "en",
        track_kind: str = "PRIMARY",
        origin_type: str = "MANUAL_CURATED_IMPORT",
        acquisition_method: str = "FIRST_PARTY_CURATED_IMPORT",
        caption_format: str | None = None,
        speaker_attribution_quality: str = "UNKNOWN",
        license_status: str = "UNKNOWN",
        license_spdx: str | None = None,
        authorization_basis: str = "NONE",
        attribution_text: str,
        permits_transcript_storage: bool = False,
        permits_verbatim_excerpts: bool = False,
        permits_ai_synthesis: bool = True,
        permits_redistribution: bool = False,
        attestation_id: str | None = None,
        attestation: dict[str, Any] | TranscriptAuthorizationAttestation | None = None,
        media_canonical_url: str | None = None,
        source_caption_url: str | None = None,
        duration_ms: int | None = None,
        actor: str = "local_transcript_cli",
        run_id: str | None = None,
    ) -> TranscriptImportResult:
        """Import a transcript from a local file path (`.vtt`, `.srt`, `.txt`) without network access."""
        raw_content: str | None = None
        resolved_format = caption_format
        if transcript_path is not None:
            path_obj = Path(transcript_path)
            if not path_obj.is_file():
                raise FileNotFoundError(f"Local transcript file not found: {path_obj}")
            raw_content = path_obj.read_text(encoding="utf-8")
            if not resolved_format:
                resolved_format = detect_caption_format_from_path(path_obj)
        else:
            if not resolved_format:
                resolved_format = "NONE"

        return self.import_local_transcript(
            resource_id=resource_id,
            raw_content=raw_content,
            language_bcp47=language_bcp47,
            track_kind=track_kind,
            origin_type=origin_type,
            acquisition_method=acquisition_method,
            caption_format=resolved_format,
            speaker_attribution_quality=speaker_attribution_quality,
            license_status=license_status,
            license_spdx=license_spdx,
            authorization_basis=authorization_basis,
            attribution_text=attribution_text,
            permits_transcript_storage=permits_transcript_storage,
            permits_verbatim_excerpts=permits_verbatim_excerpts,
            permits_ai_synthesis=permits_ai_synthesis,
            permits_redistribution=permits_redistribution,
            attestation_id=attestation_id,
            attestation=attestation,
            media_canonical_url=media_canonical_url,
            source_caption_url=source_caption_url,
            duration_ms=duration_ms,
            actor=actor,
            run_id=run_id,
        )

    def tombstone_transcript_on_takedown(
        self,
        transcript_id: str,
        *,
        reason: str,
        actor: str = "rights_governance_officer",
    ) -> dict[str, Any]:
        """Withdraw/tombstone a transcript, purging stored segments and syntheses while retaining audit metadata."""
        if not reason or not reason.strip():
            raise ValueError("Takedown/withdrawal reason must be non-empty")
        existing = self.get_transcript(transcript_id)
        if existing is None:
            raise KeyError(f"Unknown transcript_id: {transcript_id!r}")

        now = utc_now()
        with self.transaction() as conn:
            deleted_segments = conn.execute(
                "DELETE FROM transcript_segments WHERE transcript_id = ?",
                (transcript_id,),
            ).rowcount
            deleted_syntheses = conn.execute(
                "DELETE FROM transcript_syntheses WHERE transcript_id = ?",
                (transcript_id,),
            ).rowcount
            conn.execute(
                "UPDATE transcript_versions SET status = 'TOMBSTONED' WHERE transcript_id = ?",
                (transcript_id,),
            )
            conn.execute(
                """
                UPDATE transcripts
                   SET permits_transcript_storage = 0,
                       permits_verbatim_excerpts = 0,
                       permits_ai_synthesis = 0,
                       permits_redistribution = 0,
                       ingestion_state = 'WITHDRAWN_OR_TOMBSTONED',
                       transcript_storage_status = 'TOMBSTONED_ON_TAKEDOWN',
                       block_or_review_reason = ?,
                       latest_transcript_version_id = NULL,
                       segment_count = 0,
                       updated_at = ?
                 WHERE transcript_id = ?
                """,
                (reason.strip(), now, transcript_id),
            )
            self.record_provenance(
                event_type="TRANSCRIPT_TOMBSTONED_ON_TAKEDOWN",
                acquisition_method=str(existing["acquisition_method"]),
                actor=actor,
                status="tombstoned",
                source_id=str(existing["source_id"]),
                resource_id=str(existing["resource_id"]),
                source_url=str(existing["media_canonical_url"]),
                details={
                    "transcript_id": transcript_id,
                    "authorization_attestation_id": existing.get("authorization_attestation_id"),
                    "previous_state": str(existing["ingestion_state"]),
                    "reason": reason.strip(),
                    "purged_segments": deleted_segments,
                    "purged_syntheses": deleted_syntheses,
                },
            )
        result = self.get_transcript(transcript_id)
        assert result is not None
        return result

    def get_verbatim_transcript_excerpt(
        self,
        target_id: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        actor: str = "excerpt_reader",
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Return verbatim transcript segment excerpt(s) only when authorized by both `transcripts` and its attestation.

        Enforcement boundary:
        - SQLite triggers (`Migration 8`) enforce write-time authorization on `transcripts.permits_verbatim_excerpts`,
          `transcript_segments` storage, and `TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED` provenance audit records.
        - Because SQLite does not support `BEFORE SELECT` triggers or row-level read ACLs on stored tables,
          read-time payload gating for verbatim excerpt retrieval is enforced here in the repository method
          before returning segment text.
        """
        seg_row: sqlite3.Row | None = None
        if target_id.startswith(("seg_", "tseg_")):
            seg_row = self.conn.execute(
                "SELECT * FROM transcript_segments WHERE segment_id = ?",
                (target_id,),
            ).fetchone()
            if seg_row is None:
                raise KeyError(f"Unknown segment_id: {target_id!r}")
            transcript_id = str(seg_row["transcript_id"])
        else:
            transcript_id = target_id

        tr = self.get_transcript(transcript_id)
        if tr is None:
            raise KeyError(f"Unknown transcript_id: {transcript_id!r}")
        if (
            tr["ingestion_state"] not in ("READY_FOR_INGESTION", "INGESTED", "NEEDS_REVIEW")
            or not int(tr["permits_transcript_storage"])
        ):
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} does not permit storage or verbatim excerpt retrieval (state={tr['ingestion_state']!r})."
            )
        if not int(tr["permits_verbatim_excerpts"]):
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} does not permit verbatim quoting/excerpts (permits_verbatim_excerpts=0)."
            )
        att_id = tr.get("authorization_attestation_id")
        if not att_id:
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} is missing an authorization_attestation_id; verbatim quoting is blocked."
            )
        att = self.get_transcript_attestation(str(att_id))
        if att is None:
            raise TranscriptPolicyViolationError(
                f"Authorization attestation {att_id!r} for transcript {transcript_id} was not found."
            )
        if (
            att.get("withdrawn_at")
            or str(att.get("verification_status")) != "VERIFIED"
            or _is_iso_timestamp_expired(att.get("expires_at"))
            or not int(att.get("permits_transcript_storage", 0))
            or not int(att.get("permits_verbatim_excerpts", 0))
            or str(att.get("resource_id")) != str(tr["resource_id"])
            or (att.get("transcript_id") and str(att.get("transcript_id")) != transcript_id)
            or (att.get("language_bcp47") and str(att.get("language_bcp47")) != str(tr["language_bcp47"]))
            or (att.get("track_kind") and str(att.get("track_kind")) != str(tr["track_kind"]))
        ):
            raise TranscriptPolicyViolationError(
                f"Attestation {att_id!r} does not permit verbatim quoting (status={att.get('verification_status')!r}, "
                f"permits_verbatim_excerpts={att.get('permits_verbatim_excerpts')!r})."
            )

        if seg_row is not None:
            item = dict(seg_row)
            item["quality_flags"] = json.loads(item.get("quality_flags_json") or "[]")
            item["attribution_text"] = tr["attribution_text"]
            item["license_spdx"] = tr["license_spdx"]
            item["authorization_attestation_id"] = str(att_id)
            self.record_provenance(
                event_type="TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED",
                acquisition_method=str(tr["acquisition_method"]),
                actor=actor,
                status="allowed",
                source_id=str(tr["source_id"]),
                resource_id=str(tr["resource_id"]),
                source_url=str(tr["media_canonical_url"]),
                details={
                    "transcript_id": transcript_id,
                    "segment_id": str(seg_row["segment_id"]),
                    "authorization_attestation_id": str(att_id),
                    "excerpt_segment_count": 1,
                },
            )
            return item

        segments = self.get_transcript_segments(transcript_id)
        filtered = [
            seg
            for seg in segments
            if (start_ms is None or int(seg["end_ms"]) >= int(start_ms))
            and (end_ms is None or int(seg["start_ms"]) <= int(end_ms))
        ]
        self.record_provenance(
            event_type="TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED",
            acquisition_method=str(tr["acquisition_method"]),
            actor=actor,
            status="allowed",
            source_id=str(tr["source_id"]),
            resource_id=str(tr["resource_id"]),
            source_url=str(tr["media_canonical_url"]),
            details={
                "transcript_id": transcript_id,
                "authorization_attestation_id": str(att_id),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "excerpt_segment_count": len(filtered),
            },
        )
        return filtered

    def export_transcript_for_redistribution(
        self,
        transcript_id: str,
        *,
        actor: str = "redistribution_exporter",
    ) -> dict[str, Any]:
        """Export full transcript metadata and segments for external redistribution ONLY when authorized by attestation scope.

        Enforcement boundary:
        - SQLite triggers (`Migration 8`) enforce write-time authorization on `transcripts.permits_redistribution`,
          `transcript_segments` storage, and `TRANSCRIPT_REDISTRIBUTION_EXPORTED` provenance audit records.
        - Because SQLite does not support `BEFORE SELECT` triggers, read-time redistribution export gating is
          enforced in this repository method before returning transcript segments.
        """
        tr = self.get_transcript(transcript_id)
        if tr is None:
            raise KeyError(f"Unknown transcript_id: {transcript_id!r}")
        if (
            tr["ingestion_state"] not in ("READY_FOR_INGESTION", "INGESTED", "NEEDS_REVIEW")
            or not int(tr["permits_transcript_storage"])
        ):
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} is not stored or has been withdrawn; redistribution is blocked."
            )
        if not int(tr.get("permits_redistribution", 0)):
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} does not permit redistribution (permits_redistribution=0)."
            )
        att_id = tr.get("authorization_attestation_id")
        if not att_id:
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} has no authorization_attestation_id; redistribution is blocked."
            )
        att = self.get_transcript_attestation(str(att_id))
        if att is None:
            raise TranscriptPolicyViolationError(
                f"Attestation {att_id!r} for transcript {transcript_id} was not found."
            )
        if (
            att.get("withdrawn_at")
            or str(att.get("verification_status")) != "VERIFIED"
            or _is_iso_timestamp_expired(att.get("expires_at"))
            or not int(att.get("permits_transcript_storage", 0))
            or not int(att.get("permits_redistribution", 0))
            or str(att.get("resource_id")) != str(tr["resource_id"])
            or (att.get("transcript_id") and str(att.get("transcript_id")) != transcript_id)
            or (att.get("language_bcp47") and str(att.get("language_bcp47")) != str(tr["language_bcp47"]))
            or (att.get("track_kind") and str(att.get("track_kind")) != str(tr["track_kind"]))
        ):
            raise TranscriptPolicyViolationError(
                f"Attestation {att_id!r} does not permit redistribution (status={att.get('verification_status')!r}, "
                f"permits_redistribution={att.get('permits_redistribution')!r})."
            )
        segments = self.get_transcript_segments(transcript_id)
        self.record_provenance(
            event_type="TRANSCRIPT_REDISTRIBUTION_EXPORTED",
            acquisition_method=str(tr["acquisition_method"]),
            actor=actor,
            status="exported",
            source_id=str(tr["source_id"]),
            resource_id=str(tr["resource_id"]),
            source_url=str(tr["media_canonical_url"]),
            details={
                "transcript_id": transcript_id,
                "authorization_attestation_id": str(att_id),
                "segment_count": len(segments),
            },
        )
        return {
            "transcript": tr,
            "attestation": att,
            "segments": segments,
        }

    def record_transcript_synthesis(
        self,
        *,
        transcript_id: str,
        synthesis_type: str,
        generator_type: str,
        content_markdown: str,
        transcript_version_id: str | None = None,
        generator_model: str | None = None,
        referenced_time_ranges: list[dict[str, Any]] | None = None,
        is_original_transformative: bool = True,
        contains_verbatim_transcript: bool = False,
        review_status: str = "PENDING_REVIEW",
    ) -> dict[str, Any]:
        """Persist a transformative study synthesis artifact linked to a transcript."""
        tr = self.get_transcript(transcript_id)
        if tr is None:
            raise ValueError(f"Unknown transcript_id: {transcript_id!r}")
        if (
            not int(tr["permits_ai_synthesis"])
            or tr["ingestion_state"] not in ("READY_FOR_INGESTION", "INGESTED", "NEEDS_REVIEW", "METADATA_ONLY_LINKED")
        ):
            raise TranscriptPolicyViolationError(f"Transcript {transcript_id} does not permit synthesis storage")
        att_id = tr.get("authorization_attestation_id")
        if int(tr["permits_transcript_storage"]) and not att_id:
            raise TranscriptPolicyViolationError(
                f"Transcript {transcript_id} is missing a required authorization_attestation_id for synthesis storage."
            )
        if att_id:
            att = self.get_transcript_attestation(str(att_id))
            if (
                att is None
                or att.get("withdrawn_at")
                or str(att.get("verification_status")) != "VERIFIED"
                or _is_iso_timestamp_expired(att.get("expires_at"))
                or not int(att.get("permits_ai_synthesis", 0))
                or str(att.get("resource_id")) != str(tr["resource_id"])
                or (att.get("transcript_id") and str(att.get("transcript_id")) != transcript_id)
                or (att.get("language_bcp47") and str(att.get("language_bcp47")) != str(tr["language_bcp47"]))
                or (att.get("track_kind") and str(att.get("track_kind")) != str(tr["track_kind"]))
            ):
                raise TranscriptPolicyViolationError(
                    f"Linked attestation {att_id!r} for transcript {transcript_id} is missing, withdrawn, expired, scope-mismatched, or disallows AI synthesis."
                )
        if synthesis_type not in VALID_TRANSCRIPT_SYNTHESIS_TYPES:
            raise ValueError(f"Invalid synthesis_type: {synthesis_type!r}")
        if generator_type not in VALID_TRANSCRIPT_SYNTHESIS_GENERATORS:
            raise ValueError(f"Invalid generator_type: {generator_type!r}")
        if review_status not in VALID_TRANSCRIPT_SYNTHESIS_REVIEW_STATUSES:
            raise ValueError(f"Invalid review_status: {review_status!r}")
        if not is_original_transformative or contains_verbatim_transcript:
            raise TranscriptPolicyViolationError(
                "Transcript syntheses must be transformative (is_original_transformative=True) "
                "and must not contain verbatim transcript text (contains_verbatim_transcript=False)"
            )
        if not content_markdown or not content_markdown.strip():
            raise ValueError("content_markdown must be non-empty")

        ver_id = transcript_version_id or tr.get("latest_transcript_version_id")
        synthesis_id = compute_transcript_synthesis_id(
            transcript_id=transcript_id,
            transcript_version_id=ver_id,
            synthesis_type=synthesis_type,
            content_markdown=content_markdown,
        )
        now = utc_now()
        ranges_json = json.dumps(referenced_time_ranges or [], ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transcript_syntheses (
                    synthesis_id, transcript_id, transcript_version_id,
                    synthesis_type, generator_type, generator_model,
                    content_markdown, referenced_time_ranges_json,
                    is_original_transformative, contains_verbatim_transcript,
                    review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(synthesis_id) DO UPDATE SET
                    review_status = excluded.review_status
                """,
                (
                    synthesis_id,
                    transcript_id,
                    ver_id,
                    synthesis_type,
                    generator_type,
                    generator_model,
                    content_markdown.strip(),
                    ranges_json,
                    review_status,
                    now,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM transcript_syntheses WHERE synthesis_id = ?",
            (synthesis_id,),
        ).fetchone()
        assert row is not None
        item = dict(row)
        item["referenced_time_ranges"] = json.loads(item.get("referenced_time_ranges_json") or "[]")
        return item

    def get_transcript_syntheses(self, transcript_id: str) -> list[dict[str, Any]]:
        """Return all `transcript_syntheses` for `transcript_id` ordered by `created_at`."""
        rows = self.conn.execute(
            "SELECT * FROM transcript_syntheses WHERE transcript_id = ? ORDER BY created_at, synthesis_id",
            (transcript_id,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["referenced_time_ranges"] = json.loads(item.get("referenced_time_ranges_json") or "[]")
            results.append(item)
        return results

    def record_transcript_segment_link(
        self,
        *,
        segment_id: str,
        target_type: str,
        target_id: str,
        relation_type: str,
        confidence: float = 1.0,
        created_by: str = "curator",
    ) -> dict[str, Any]:
        """Link a `transcript_segments` row to a concept, paper resource, module, equation, or chunk."""
        if target_type not in VALID_SEGMENT_LINK_TARGET_TYPES:
            raise ValueError(f"Invalid target_type: {target_type!r}")
        if relation_type not in VALID_SEGMENT_LINK_RELATIONS:
            raise ValueError(f"Invalid relation_type: {relation_type!r}")
        if not target_id or not target_id.strip():
            raise ValueError("target_id must be non-empty")
        if not (0.0 <= float(confidence) <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")

        link_id = compute_transcript_segment_link_id(
            segment_id=segment_id,
            target_type=target_type,
            target_id=target_id,
            relation_type=relation_type,
        )
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transcript_segment_links (
                    link_id, segment_id, target_type, target_id,
                    relation_type, confidence, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(segment_id, target_type, target_id, relation_type) DO UPDATE SET
                    confidence = excluded.confidence,
                    created_by = excluded.created_by
                """,
                (
                    link_id,
                    segment_id,
                    target_type,
                    target_id.strip(),
                    relation_type,
                    float(confidence),
                    created_by.strip(),
                    now,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM transcript_segment_links WHERE link_id = ?",
            (link_id,),
        ).fetchone()
        assert row is not None
        return dict(row)

    def get_transcript_segment_links(self, segment_id: str) -> list[dict[str, Any]]:
        """Return all `transcript_segment_links` for `segment_id`."""
        rows = self.conn.execute(
            "SELECT * FROM transcript_segment_links WHERE segment_id = ? ORDER BY created_at, link_id",
            (segment_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> KnowledgeStore:
    """Initialize or migrate the SQLite knowledge store at `db_path` and return a `KnowledgeStore`."""
    return KnowledgeStore(db_path=db_path, auto_initialize=True)


def import_seed_manifest(
    db_path: Path | str = DEFAULT_DB_PATH,
    manifest_path: Path | str = DEFAULT_SEED_PATH,
) -> ManifestImportSummary:
    """Convenience function to initialize `db_path` and import `manifest_path` offline."""
    with KnowledgeStore(db_path=db_path, auto_initialize=True) as store:
        return store.import_seed_manifest(manifest_path=manifest_path)


def format_import_summary(summary: ManifestImportSummary) -> str:
    """Format a human-readable summary of a seed manifest import operation."""
    lines = [
        "Seed Manifest Import Summary (offline / no network requests)",
        f"  Run ID:           {summary.run_id}",
        f"  Manifest path:    {summary.manifest_path}",
        f"  Total records:    {summary.total_records}",
        f"  Imported:         {summary.imported_count}",
        f"  Updated:          {summary.updated_count}",
        f"  Skipped:          {summary.skipped_count}",
        f"  Rejected:         {summary.rejected_count}",
    ]
    if summary.rejected_records:
        lines.append("  Rejected details:")
        for item in summary.rejected_records:
            lines.append(f"    - {item.get('manifest_id') or 'NO-ID'}: {item.get('reason')} ({item.get('url')})")
    return "\n".join(lines)


def format_transcript_import_result(result: TranscriptImportResult) -> str:
    """Format a human-readable summary of a local transcript import without logging transcript text."""
    lines = [
        "Local Transcript Import Summary (offline / no network requests)",
        f"  Status:                    {result.status}",
        f"  Transcript ID:             {result.transcript_id}",
        f"  Resource ID:               {result.resource_id}",
        f"  Source ID:                 {result.source_id}",
        f"  Language / Track:          {result.language_bcp47} ({result.track_kind})",
        f"  Attestation ID:            {result.authorization_attestation_id or 'none'}",
        f"  Ingestion state:           {result.ingestion_state}",
        f"  Storage status:            {result.transcript_storage_status}",
        f"  Permits text storage:      {int(result.permits_transcript_storage)}",
        f"  Permits verbatim excerpts: {int(result.permits_verbatim_excerpts)}",
        f"  Permits redistribution:    {int(result.permits_redistribution)}",
        f"  Transcript version ID:     {result.transcript_version_id or 'none'}",
        f"  Version number:            {result.version_number if result.version_number is not None else 'none'}",
        f"  Stored segments:           {result.stored_segments}",
    ]
    if result.reason:
        lines.append(f"  Policy / review note:      {result.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize or migrate the local SQLite database")
    init_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")

    import_parser = subparsers.add_parser(
        "import-manifest",
        help="Import seed_urls.json into the local SQLite database without network requests",
    )
    import_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    import_parser.add_argument("--manifest", type=Path, default=DEFAULT_SEED_PATH, help="Seed manifest JSON path")

    att_parser = subparsers.add_parser(
        "attest-transcript",
        help="Record an auditable authorization attestation for transcript indexing/storage",
    )
    att_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    att_parser.add_argument("--resource-id", required=True, help="Existing parent resource_id")
    att_parser.add_argument("--transcript-id", default=None, help="Optional specific transcript_id scope")
    att_parser.add_argument("--language", default=None, help="Optional BCP-47 language tag scope (e.g. en)")
    att_parser.add_argument(
        "--track-kind",
        default=None,
        choices=VALID_TRANSCRIPT_TRACK_KINDS,
        help="Optional transcript track kind scope",
    )
    att_parser.add_argument(
        "--authorization-basis",
        required=True,
        choices=VALID_AUTHORIZATION_BASES,
        help="Authorization basis (e.g. OPEN_LICENSE_VERIFIED, PUBLIC_DOMAIN_VERIFIED, FIRST_PARTY_OWNED_MEDIA, WRITTEN_RIGHTS_HOLDER_PERMISSION)",
    )
    att_parser.add_argument(
        "--license-status",
        required=True,
        choices=VALID_TRANSCRIPT_LICENSE_STATUSES,
        help="License classification (e.g. VERIFIED_PERMISSIVE, ALL_RIGHTS_RESERVED, UNKNOWN)",
    )
    att_parser.add_argument("--license-spdx", default=None, help="SPDX license identifier (e.g. CC-BY-4.0, CC0-1.0)")
    att_parser.add_argument(
        "--evidence-type",
        required=True,
        choices=VALID_ATTESTATION_EVIDENCE_TYPES,
        help="Category of authorization evidence",
    )
    att_parser.add_argument(
        "--evidence-ref",
        "--evidence-reference",
        dest="evidence_ref",
        required=True,
        help="Auditable reference, agreement ID, or license declaration excerpt",
    )
    att_parser.add_argument(
        "--evidence-url",
        "--source-evidence-url",
        dest="evidence_url",
        default=None,
        help="Optional canonical HTTPS URL to public license/terms",
    )
    att_parser.add_argument("--evidence-sha256", default=None, help="Optional 64-hex SHA-256 digest of evidence artifact")
    att_parser.add_argument("--attribution", default=None, help="Optional human-readable attribution text")
    att_parser.add_argument(
        "--permit-storage",
        action="store_true",
        help="Grant permission to store transcript text locally",
    )
    att_parser.add_argument(
        "--permit-verbatim-excerpts",
        action="store_true",
        help="Grant permission to quote verbatim transcript excerpts",
    )
    att_parser.add_argument(
        "--permit-ai-synthesis",
        action="store_true",
        help="Explicitly grant permission for transformative AI synthesis notes",
    )
    att_parser.add_argument(
        "--permit-redistribution",
        action="store_true",
        help="Grant permission to redistribute transcript text externally",
    )
    att_parser.add_argument(
        "--disallow-ai-synthesis",
        action="store_true",
        help="Disallow transformative AI synthesis notes on this resource/transcript",
    )
    att_parser.add_argument("--reviewed-by", required=True, help="Curator or reviewer recording the attestation")
    att_parser.add_argument(
        "--verification-status",
        default="VERIFIED",
        choices=VALID_ATTESTATION_VERIFICATION_STATUSES,
        help="Verification status (default: VERIFIED)",
    )
    att_parser.add_argument("--notes", default=None, help="Optional human review notes")
    att_parser.add_argument("--expires-at", default=None, help="Optional ISO-8601 UTC expiration timestamp")

    att_list_parser = subparsers.add_parser(
        "list-transcript-attestations",
        help="List transcript authorization attestations in the local store",
    )
    att_list_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    att_list_parser.add_argument("--resource-id", default=None, help="Filter by resource_id")
    att_list_parser.add_argument(
        "--status",
        default=None,
        choices=VALID_ATTESTATION_VERIFICATION_STATUSES,
        help="Filter by verification_status",
    )

    att_withdraw_parser = subparsers.add_parser(
        "withdraw-transcript-attestation",
        help="Withdraw a transcript authorization attestation and cascade takedown to dependent transcripts",
    )
    att_withdraw_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    att_withdraw_parser.add_argument("--attestation-id", required=True, help="Attestation ID to withdraw")
    att_withdraw_parser.add_argument("--reason", required=True, help="Mandatory audit reason for withdrawal")
    att_withdraw_parser.add_argument("--actor", default="rights_governance_officer", help="Actor recording withdrawal")

    tr_import_parser = subparsers.add_parser(
        "import-transcript",
        help="Import a local transcript (.vtt, .srt, .txt) for an existing resource under offline policy gates",
    )
    tr_import_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    tr_import_parser.add_argument("--resource-id", required=True, help="Existing parent resource_id")
    tr_import_parser.add_argument(
        "--attestation-id",
        default=None,
        help="Auditable authorization attestation ID (required to store transcript text)",
    )
    tr_import_parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Path to local transcript file (.vtt, .srt, .txt); omit when using --acquisition-method METADATA_AND_LINK_ONLY",
    )
    tr_import_parser.add_argument("--language", default="en", help="BCP-47 language tag (default: en)")
    tr_import_parser.add_argument(
        "--track-kind",
        default="PRIMARY",
        choices=VALID_TRANSCRIPT_TRACK_KINDS,
        help="Transcript track kind (default: PRIMARY)",
    )
    tr_import_parser.add_argument(
        "--origin-type",
        default="MANUAL_CURATED_IMPORT",
        choices=VALID_TRANSCRIPT_ORIGIN_TYPES,
        help="Transcript origin classification",
    )
    tr_import_parser.add_argument(
        "--acquisition-method",
        default="FIRST_PARTY_CURATED_IMPORT",
        choices=VALID_TRANSCRIPT_ACQUISITION_METHODS,
        help="Approved offline acquisition method classification",
    )
    tr_import_parser.add_argument(
        "--format",
        dest="caption_format",
        default=None,
        choices=VALID_CAPTION_FORMATS,
        help="Transcript format (auto-detected from .vtt/.srt/.txt if omitted)",
    )
    tr_import_parser.add_argument(
        "--speaker-quality",
        default="UNKNOWN",
        choices=VALID_SPEAKER_ATTRIBUTION_QUALITIES,
        help="Speaker attribution quality level",
    )
    tr_import_parser.add_argument(
        "--license-status",
        default="UNKNOWN",
        choices=VALID_TRANSCRIPT_LICENSE_STATUSES,
        help="Transcript license classification (default: UNKNOWN, which fails closed)",
    )
    tr_import_parser.add_argument("--license-spdx", default=None, help="SPDX license identifier (e.g. CC-BY-4.0)")
    tr_import_parser.add_argument(
        "--authorization-basis",
        default="NONE",
        choices=VALID_AUTHORIZATION_BASES,
        help="Authorization basis for storing transcript text (default: NONE, which fails closed)",
    )
    tr_import_parser.add_argument("--attribution", required=True, help="Required human-readable attribution text")
    tr_import_parser.add_argument(
        "--permit-storage",
        action="store_true",
        help="Request full transcript storage (must be backed by --attestation-id)",
    )
    tr_import_parser.add_argument(
        "--permit-verbatim-excerpts",
        action="store_true",
        help="Request verbatim transcript excerpts (must be backed by --attestation-id scope)",
    )
    tr_import_parser.add_argument(
        "--permit-redistribution",
        action="store_true",
        help="Request external transcript redistribution (must be backed by --attestation-id scope)",
    )
    tr_import_parser.add_argument(
        "--duration-ms",
        type=int,
        default=None,
        help="Optional media duration in milliseconds for TV-03 bounds checking",
    )
    tr_import_parser.add_argument("--actor", default="local_transcript_cli", help="Provenance actor identifier")

    tr_list_parser = subparsers.add_parser("list-transcripts", help="List transcript records in the local store")
    tr_list_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    tr_list_parser.add_argument("--resource-id", default=None, help="Filter by resource_id")
    tr_list_parser.add_argument("--source-id", default=None, help="Filter by source_id")
    tr_list_parser.add_argument(
        "--state",
        default=None,
        choices=VALID_TRANSCRIPT_INGESTION_STATES,
        help="Filter by ingestion_state",
    )

    tr_show_parser = subparsers.add_parser(
        "show-transcript",
        help="Show metadata, version history, and segment count for a transcript",
    )
    tr_show_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    tr_show_parser.add_argument("--transcript-id", required=True, help="Transcript ID to inspect")

    tr_withdraw_parser = subparsers.add_parser(
        "withdraw-transcript",
        help="Tombstone a transcript and purge stored segments/syntheses on takedown or withdrawal",
    )
    tr_withdraw_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    tr_withdraw_parser.add_argument("--transcript-id", required=True, help="Transcript ID to withdraw/tombstone")
    tr_withdraw_parser.add_argument("--reason", required=True, help="Mandatory audit reason for withdrawal")
    tr_withdraw_parser.add_argument("--actor", default="rights_governance_officer", help="Actor recording withdrawal")

    status_parser = subparsers.add_parser("status", help="Show local knowledge store schema and table counts")
    status_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")

    args = parser.parse_args(argv)
    cmd = args.command or "status"

    if cmd == "init":
        with KnowledgeStore(db_path=args.db, auto_initialize=False) as store:
            applied = store.initialize()
            version = store.get_schema_version()
            sources_count = len(store.list_sources())
        print(f"Database initialized at {args.db}")
        print(f"  Schema version: {version}")
        print(f"  Newly applied migrations: {applied if applied else 'none (already up to date)'}")
        print(f"  Registered sources: {sources_count}")
        return 0

    if cmd == "import-manifest":
        summary = import_seed_manifest(db_path=args.db, manifest_path=args.manifest)
        print(format_import_summary(summary))
        return 0 if summary.rejected_count == 0 else 1

    if cmd == "attest-transcript":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            att = store.record_transcript_attestation(
                resource_id=args.resource_id,
                transcript_id=args.transcript_id,
                language_bcp47=args.language,
                track_kind=args.track_kind,
                authorization_basis=args.authorization_basis,
                license_status=args.license_status,
                license_spdx=args.license_spdx,
                evidence_type=args.evidence_type,
                evidence_reference=args.evidence_ref,
                source_evidence_url=args.evidence_url,
                evidence_sha256=args.evidence_sha256,
                attribution_text=args.attribution,
                permits_metadata_indexing=True,
                permits_transcript_storage=bool(args.permit_storage),
                permits_verbatim_excerpts=bool(args.permit_verbatim_excerpts),
                permits_ai_synthesis=(not bool(args.disallow_ai_synthesis)) or bool(args.permit_ai_synthesis),
                permits_redistribution=bool(args.permit_redistribution),
                reviewed_by=args.reviewed_by,
                verification_status=args.verification_status,
                review_notes=args.notes,
                expires_at=args.expires_at,
            )
        print("Transcript Authorization Attestation Recorded (offline / no network requests)")
        print(f"  Attestation ID:            {att['attestation_id']}")
        print(f"  Resource ID:               {att['resource_id']}")
        print(f"  Verification status:       {att['verification_status']}")
        print(f"  Authorization basis:       {att['authorization_basis']}")
        print(f"  License status / SPDX:     {att['license_status']} ({att['license_spdx'] or 'no-spdx'})")
        print(f"  Evidence type / ref:       {att['evidence_type']} | {att['evidence_reference']}")
        print(f"  Permits text storage:      {int(att['permits_transcript_storage'])}")
        print(f"  Permits verbatim excerpts: {int(att['permits_verbatim_excerpts'])}")
        print(f"  Permits redistribution:    {int(att['permits_redistribution'])}")
        return 0

    if cmd == "list-transcript-attestations":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            rows = store.list_transcript_attestations(
                resource_id=args.resource_id,
                verification_status=args.status,
            )
        print(f"Transcript Authorization Attestations ({len(rows)}):")
        for row in rows:
            print(
                f"  - {row['attestation_id']} | resource={row['resource_id']} | "
                f"status={row['verification_status']} | basis={row['authorization_basis']} | "
                f"storage={row['permits_transcript_storage']} | excerpts={row['permits_verbatim_excerpts']} | "
                f"redistribution={row['permits_redistribution']}"
            )
        return 0

    if cmd == "withdraw-transcript-attestation":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            updated = store.withdraw_transcript_attestation(
                args.attestation_id,
                reason=args.reason,
                actor=args.actor,
            )
        print(
            f"Attestation {updated['attestation_id']} withdrawn: "
            f"status={updated['verification_status']}, reason={updated['withdrawal_reason']}"
        )
        return 0

    if cmd == "import-transcript":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            result = store.import_local_transcript_file(
                resource_id=args.resource_id,
                transcript_path=args.file,
                language_bcp47=args.language,
                track_kind=args.track_kind,
                origin_type=args.origin_type,
                acquisition_method=args.acquisition_method,
                caption_format=args.caption_format,
                speaker_attribution_quality=args.speaker_quality,
                license_status=args.license_status,
                license_spdx=args.license_spdx,
                authorization_basis=args.authorization_basis,
                attribution_text=args.attribution,
                permits_transcript_storage=bool(args.permit_storage),
                permits_verbatim_excerpts=bool(args.permit_verbatim_excerpts),
                permits_redistribution=bool(args.permit_redistribution),
                attestation_id=args.attestation_id,
                duration_ms=args.duration_ms,
                actor=args.actor,
            )
        print(format_transcript_import_result(result))
        return 0

    if cmd == "list-transcripts":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            rows = store.list_transcripts(
                resource_id=args.resource_id,
                source_id=args.source_id,
                ingestion_state=args.state,
            )
        print(f"Transcripts ({len(rows)}):")
        for row in rows:
            print(
                f"  - {row['transcript_id']} | resource={row['resource_id']} | "
                f"lang={row['language_bcp47']} ({row['track_kind']}) | "
                f"state={row['ingestion_state']} | storage={row['transcript_storage_status']} | "
                f"attestation={row.get('authorization_attestation_id') or 'none'} | "
                f"segments={row['segment_count']}"
            )
        return 0

    if cmd == "show-transcript":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            tr = store.get_transcript(args.transcript_id)
            if tr is None:
                print(f"Transcript not found: {args.transcript_id}", file=sys.stderr)
                return 1
            versions = store.get_transcript_versions(args.transcript_id)
        print(f"Transcript {tr['transcript_id']}:")
        print(f"  Resource ID:          {tr['resource_id']}")
        print(f"  Source ID:            {tr['source_id']}")
        print(f"  Language / Track:     {tr['language_bcp47']} ({tr['track_kind']})")
        print(f"  Origin / Method:      {tr['origin_type']} / {tr['acquisition_method']}")
        print(f"  Format:               {tr['caption_format']}")
        print(f"  License / Basis:      {tr['license_status']} ({tr['license_spdx'] or 'no-spdx'}) / {tr['authorization_basis']}")
        print(f"  Attestation ID:       {tr.get('authorization_attestation_id') or 'none'}")
        print(f"  Ingestion State:      {tr['ingestion_state']}")
        print(f"  Storage Status:       {tr['transcript_storage_status']}")
        print(f"  Latest Version ID:    {tr['latest_transcript_version_id'] or 'none'}")
        print(f"  Stored Segments:      {tr['segment_count']}")
        print(f"  Versions ({len(versions)}):")
        for ver in versions:
            print(
                f"    * v{ver['version_number']} ({ver['transcript_version_id']}): "
                f"status={ver['status']}, segments={ver['segment_count']}, "
                f"words={ver['word_count']}, sha256={ver['normalized_text_sha256'][:16]}..."
            )
        return 0

    if cmd == "withdraw-transcript":
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            updated = store.tombstone_transcript_on_takedown(
                args.transcript_id,
                reason=args.reason,
                actor=args.actor,
            )
        print(
            f"Transcript {updated['transcript_id']} withdrawn: "
            f"state={updated['ingestion_state']}, storage={updated['transcript_storage_status']}, "
            f"segments={updated['segment_count']}"
        )
        return 0

    if cmd == "status":
        db_target = getattr(args, "db", DEFAULT_DB_PATH)
        if not db_target.exists():
            print(f"Database does not exist yet at {db_target} (run 'init' or 'import-manifest' first).")
            return 0
        with KnowledgeStore(db_path=db_target, auto_initialize=True) as store:
            version = store.get_schema_version()
            counts = {
                table: store.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in (
                    "schema_migrations",
                    "sources",
                    "source_urls",
                    "resources",
                    "resource_versions",
                    "discovered_link_candidates",
                    "discovered_link_reviews",
                    "transcript_authorization_attestations",
                    "transcripts",
                    "transcript_versions",
                    "transcript_segments",
                    "transcript_syntheses",
                    "transcript_segment_links",
                    "provenance_events",
                    "collection_runs",
                    "documents",
                )
            }
        print(f"Knowledge store status ({db_target}):")
        print(f"  Schema version: {version}")
        for tbl, cnt in counts.items():
            print(f"  {tbl:<38}: {cnt}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
