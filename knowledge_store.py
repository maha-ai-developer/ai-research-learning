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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
DEFAULT_SEED_PATH = ROOT / "seed_urls.json"
DEFAULT_DB_PATH = ROOT / "data" / "runtime" / "inventory.sqlite"

LATEST_SCHEMA_VERSION = 4


def utc_now() -> str:
    """Return current UTC timestamp in ISO-8601 format (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_hex(value: str) -> str:
    """Return full 64-character lowercase SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_and_canonicalize_url(url: Any) -> str:
    """Validate an HTTP/HTTPS URL and return its deterministic canonical form.

    Raises ValueError if the input is not a valid HTTP(S) URL with a host.
    """
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("URL cannot be empty")
    if any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise ValueError(f"URL contains whitespace or control characters: {url!r}")

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"URL must use http or https scheme, got {parsed.scheme!r}: {url!r}")
    if not parsed.netloc:
        raise ValueError(f"URL is missing network host (netloc): {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"URL must not contain embedded credentials: {url!r}")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"URL has invalid port: {url!r}") from exc

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname or "." not in hostname and hostname != "localhost":
        raise ValueError(f"URL has invalid hostname {hostname!r}: {url!r}")

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) or port is None:
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"

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


MIGRATIONS: tuple[tuple[int, str, str, Any], ...] = (
    (1, "0001_legacy_collector_tables", LEGACY_COLLECTOR_TABLES_SQL, _migration_1_legacy_tables),
    (2, "0002_canonical_knowledge_store_v1", CANONICAL_SCHEMA_V2_SQL, _migration_2_canonical_schema),
    (3, "0003_seed_canonical_sources_and_backfill", "SEED_CANONICAL_SOURCES_V3", _migration_3_seed_sources_and_backfill),
    (4, "0004_discovered_link_candidates", DISCOVERED_LINK_CANDIDATES_V4_SQL, _migration_4_discovered_link_candidates),
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
        if review_status not in ("PENDING_REVIEW", "APPROVED_FOR_MANIFEST", "REJECTED", "DEFERRED"):
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

    def list_discovered_link_candidates(
        self,
        *,
        referring_resource_id: str | None = None,
        referring_source_id: str | None = None,
        domain_classification: str | None = None,
        review_status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List persisted discovered link candidates with optional filtering."""
        clauses: list[str] = []
        params: list[Any] = []
        if referring_resource_id is not None:
            clauses.append("referring_resource_id = ?")
            params.append(referring_resource_id)
        if referring_source_id is not None:
            clauses.append("referring_source_id = ?")
            params.append(referring_source_id)
        if domain_classification is not None:
            clauses.append("domain_classification = ?")
            params.append(domain_classification)
        if review_status is not None:
            clauses.append("review_status = ?")
            params.append(review_status)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM discovered_link_candidates {where_sql} ORDER BY first_seen_at ASC, candidate_id ASC",
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
                    "provenance_events",
                    "collection_runs",
                    "documents",
                )
            }
        print(f"Knowledge store status ({db_target}):")
        print(f"  Schema version: {version}")
        for tbl, cnt in counts.items():
            print(f"  {tbl:<20}: {cnt}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
