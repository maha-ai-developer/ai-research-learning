"""Task 037 — Policy-Aware Source-to-Knowledge Ingestion Pipeline.

Implements the end-to-end governed ingestion lifecycle:
SOURCE -> AUTHORIZATION -> COLLECTION -> EXTRACTION -> NORMALIZATION ->
KNOWLEDGE -> CHUNKING -> EMBEDDING -> INDEXING -> LEARNING AGENTS

Preserves all existing source-adapter, curation, transcript, SQLite, and Chroma
contracts while keeping `knowledge_store.LATEST_SCHEMA_VERSION == 8` intact via
an idempotent forward-only migration helper (`ensure_ingestion_schema`).
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Sequence
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from knowledge_store import KnowledgeStore, compute_resource_id, sha256_hex, utc_now
from .rag import (
    DEFAULT_CHROMA_COLLECTION as CHROMA_COLLECTION_NAME,
    DEFAULT_CHROMA_DIR,
    DEFAULT_RUNTIME_DB_PATH as DEFAULT_SQLITE_DB_PATH,
    ChromaPersistentVectorStore as ChromaVectorIndex,
    DeterministicHashEmbeddingProvider,
    EmbeddingProvider,
    GeminiEmbeddingProvider,
    LocalRAGPipeline as RAGPipeline,
    OpenAIEmbeddingProvider,
    ProviderConfig,
    compute_chunk_id,
    ensure_knowledge_chunks_schema,
)
from .sources.arxiv_adapter import (
    ARXIV_MIN_INTERVAL_SECONDS,
    ArxivSourceAdapter,
    extract_arxiv_id,
)
from .sources.base import (
    OriginRateLimiter,
    PolicyBlockedError,
    ScopeViolationError,
    SourceCandidate,
    canonicalize_url,
    host_of,
)
from .sources.html_adapters import (
    BengioSourceAdapter,
    HintonSourceAdapter,
    TheBatchSourceAdapter,
    ThreeBlueOneBrownAdapter,
)
from .sources.wikipedia_adapter import WikipediaSourceAdapter


INGESTION_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def attribution_for_license(
    license_status: str | None,
    *,
    source_id: str | None = None,
    canonical_url: str | None = None,
    authors: str | None = None,
) -> str:
    """Build a human-readable license and attribution requirement string."""
    lic = (license_status or "ALL_RIGHTS_RESERVED_METADATA_ONLY").strip()
    src = (source_id or "UNKNOWN").strip()
    url_part = f" ({canonical_url})" if canonical_url else ""
    author_part = f" by {authors}" if authors else ""
    if lic == "CC_BY_SA_4_0" or src == "SRC-WIKIPEDIA":
        return f"CC BY-SA 4.0 attribution required{author_part}; link to canonical Wikipedia revision{url_part} and preserve ShareAlike terms."
    if src == "SRC-ARXIV":
        return f"arXiv API bibliographic/abstract attribution required{author_part}{url_part} (per paper license: {lic})."
    return f"Source attribution required ({src}, license={lic}){author_part}{url_part}."


def _resolve_resource_row(
    conn: sqlite3.Connection,
    resource_id: str | int | None,
) -> dict[str, Any] | None:
    """Resolve a resource row by `resource_id` (`res_...`), `manifest_id`, or 1-based rowid."""
    if resource_id is None:
        return None
    raw_key = str(resource_id).strip()
    if not raw_key:
        return None
    row = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ? OR manifest_id = ? LIMIT 1",
        (raw_key, raw_key),
    ).fetchone()
    if row is not None:
        return dict(row)
    if raw_key.isdigit():
        row = conn.execute(
            "SELECT * FROM resources WHERE rowid = ? LIMIT 1",
            (int(raw_key),),
        ).fetchone()
        if row is not None:
            return dict(row)
    return None


# ==============================================================================
# PHASE B — CANONICAL RESOURCE TYPES, EPISTEMIC CATEGORIES & ORIGINS
# ==============================================================================


class CanonicalResourceType(str, Enum):
    """Unified 12-type classification across all ingested and catalogued resources."""

    RESEARCH_PAPERS = "Research papers"
    RESEARCH_ABSTRACTS = "Research abstracts"
    PUBLICATIONS_AND_TECHNICAL_ARTICLES = "Publications and technical articles"
    AUTHOR_ESSAYS_AND_COMMENTARY = "Author essays and commentary"
    EDUCATIONAL_LESSONS = "Educational lessons"
    LECTURES = "Lectures"
    VIDEOS = "Videos"
    AUTHORIZED_TRANSCRIPTS = "Authorized transcripts"
    SLIDES_AND_LECTURE_NOTES = "Slides and lecture notes"
    COURSES_AND_TUTORIALS = "Courses and tutorials"
    ENCYCLOPEDIA_CONCEPTS = "Encyclopedia concepts"
    AI_NEWS_AND_REPORTS = "AI news and reports"


class EpistemicCategory(str, Enum):
    """5 epistemic categories governing how learning and research agents treat evidence."""

    PRIMARY_RESEARCH = "PRIMARY_RESEARCH"
    AUTHOR_COMMENTARY = "AUTHOR_COMMENTARY"
    EDUCATIONAL_EXPLANATION = "EDUCATIONAL_EXPLANATION"
    REFERENCE_MATERIAL = "REFERENCE_MATERIAL"
    AI_NEWS_AND_DEVELOPMENTS = "AI_NEWS_AND_DEVELOPMENTS"


class ChunkEpistemicLayer(str, Enum):
    """5 chunk-level epistemic layers tracked in the Source Ingestion Control Plane."""

    PRIMARY_SOURCE_TEXT = "PRIMARY_SOURCE_TEXT"
    AUTHORIZED_ABSTRACT_OR_DESCRIPTION = "AUTHORIZED_ABSTRACT_OR_DESCRIPTION"
    TRANSCRIPT_AUTHORIZED_TEXT = "TRANSCRIPT_AUTHORIZED_TEXT"
    EDUCATIONAL_EXPLANATION = "EDUCATIONAL_EXPLANATION"
    NEWS_AND_COMMENTARY = "NEWS_AND_COMMENTARY"


class NormalizedContentOrigin(str, Enum):
    """5 explicit content-origin classifications preventing silent promotion of summaries/metadata."""

    ORIGINAL_SOURCE_TEXT = "Original source text"
    AUTHORIZED_TRANSCRIPT = "Authorized transcript"
    ABSTRACT_OR_METADATA = "Abstract or metadata"
    THIRD_PARTY_EXPLANATION = "Third-party explanation"
    AI_GENERATED_SUMMARY = "AI-generated summary"


class ExtractionFormat(str, Enum):
    """Supported content extraction input formats."""

    HTML = "HTML"
    PDF = "PDF"
    PLAIN_TEXT = "PLAIN_TEXT"
    STRUCTURED_METADATA = "STRUCTURED_METADATA"
    AUTHORIZED_TRANSCRIPT = "AUTHORIZED_TRANSCRIPT"
    SLIDE_DOCUMENT = "SLIDE_DOCUMENT"


class ChunkingStrategy(str, Enum):
    """Content-type-aware chunking strategies (Phase E)."""

    SECTION_AWARE_RESEARCH_PAPER = "section_aware_research_paper"
    HEADING_PARAGRAPH_ARTICLE = "heading_paragraph_article"
    CONCEPT_AWARE_EDUCATIONAL = "concept_aware_educational"
    TRANSCRIPT_TOPIC_TIME_BOUNDARY = "transcript_topic_time_boundary"
    EQUATION_PRESERVING_MATHEMATICAL = "equation_preserving_mathematical"


STRUCTURED_KNOWLEDGE_ELEMENT_TYPES: tuple[str, ...] = (
    "definitions",
    "concepts",
    "claims",
    "hypotheses",
    "methods",
    "equations",
    "experiments",
    "results",
    "limitations",
    "concept_relationships",
    "citations",
)


def classify_canonical_resource_type(
    raw_resource_type: str | None,
    *,
    source_id: str | None = None,
    title: str | None = None,
    media_modality: str | None = None,
    canonical_url: str | None = None,
    has_authorized_transcript: bool = False,
) -> CanonicalResourceType:
    """Deterministically map any raw DB `resource_type` or seed hint into the 12 unified types."""
    if has_authorized_transcript:
        return CanonicalResourceType.AUTHORIZED_TRANSCRIPTS

    raw = (raw_resource_type or "").strip()
    for member in CanonicalResourceType:
        if raw.lower() == member.value.lower():
            return member

    low = raw.lower()
    src = (source_id or "").upper()
    t_low = (title or "").lower()
    m_low = (media_modality or "").lower()
    u_low = (canonical_url or "").lower()

    if src == "SRC-THE-BATCH" or "/the-batch" in u_low or "newsletter" in low or "ai_news" in low or "news" in low:
        return CanonicalResourceType.AI_NEWS_AND_REPORTS

    if "transcript" in low:
        return CanonicalResourceType.AUTHORIZED_TRANSCRIPTS

    if "slide" in low or "lecture note" in low or "lecture_note" in low:
        return CanonicalResourceType.SLIDES_AND_LECTURE_NOTES

    if src == "SRC-WIKIPEDIA" or "encyclopedia" in low or "wikipedia" in low or "glossary" in low:
        return CanonicalResourceType.ENCYCLOPEDIA_CONCEPTS

    if "preprint_metadata_and_abstract" in low or "abstract" in low:
        return CanonicalResourceType.RESEARCH_ABSTRACTS

    if (
        "paper" in low
        or "preprint" in low
        or "monograph" in low
        or "book" in low
        or (src == "SRC-ARXIV" and "full" in low)
    ):
        return CanonicalResourceType.RESEARCH_PAPERS

    if "course" in low or "tutorial" in low or "specialization" in low or "curriculum" in low:
        return CanonicalResourceType.COURSES_AND_TUTORIALS

    if "lecture" in low or "colloquium" in low or "keynote" in low or "talk" in low:
        return CanonicalResourceType.LECTURES

    if "video" in low or ("video" in m_low and "lesson" not in low and "topic" not in low):
        return CanonicalResourceType.VIDEOS

    if (
        src == "SRC-3B1B"
        or "lesson" in low
        or "educational" in low
        or "topic index" in low
        or "/lessons/" in u_low
        or "/topics/" in u_low
    ):
        return CanonicalResourceType.EDUCATIONAL_LESSONS

    if (
        "blog" in low
        or "essay" in low
        or "commentary" in low
        or "statement" in low
        or "testimony" in low
        or "interview" in low
        or "faq" in low
        or "reflection" in t_low
    ):
        return CanonicalResourceType.AUTHOR_ESSAYS_AND_COMMENTARY

    if src == "SRC-ARXIV":
        return CanonicalResourceType.RESEARCH_ABSTRACTS

    return CanonicalResourceType.PUBLICATIONS_AND_TECHNICAL_ARTICLES


def classify_epistemic_category(
    canonical_resource_type: CanonicalResourceType | str,
    *,
    source_id: str | None = None,
    raw_resource_type: str | None = None,
    title: str | None = None,
    canonical_url: str | None = None,
) -> EpistemicCategory:
    """Assign one of the 5 epistemic categories to a resource."""
    if not isinstance(canonical_resource_type, CanonicalResourceType):
        canonical_resource_type = classify_canonical_resource_type(
            str(canonical_resource_type),
            source_id=source_id,
            title=title,
            canonical_url=canonical_url,
        )

    src = (source_id or "").upper()
    u_low = (canonical_url or "").lower()
    raw_low = (raw_resource_type or "").lower()

    if (
        src == "SRC-THE-BATCH"
        or "/the-batch" in u_low
        or canonical_resource_type == CanonicalResourceType.AI_NEWS_AND_REPORTS
    ):
        return EpistemicCategory.AI_NEWS_AND_DEVELOPMENTS

    if canonical_resource_type in {
        CanonicalResourceType.RESEARCH_PAPERS,
        CanonicalResourceType.RESEARCH_ABSTRACTS,
    }:
        return EpistemicCategory.PRIMARY_RESEARCH

    if canonical_resource_type == CanonicalResourceType.AUTHOR_ESSAYS_AND_COMMENTARY:
        return EpistemicCategory.AUTHOR_COMMENTARY

    if canonical_resource_type in {
        CanonicalResourceType.EDUCATIONAL_LESSONS,
        CanonicalResourceType.LECTURES,
        CanonicalResourceType.VIDEOS,
        CanonicalResourceType.AUTHORIZED_TRANSCRIPTS,
        CanonicalResourceType.SLIDES_AND_LECTURE_NOTES,
        CanonicalResourceType.COURSES_AND_TUTORIALS,
    }:
        return EpistemicCategory.EDUCATIONAL_EXPLANATION

    if canonical_resource_type == CanonicalResourceType.ENCYCLOPEDIA_CONCEPTS:
        return EpistemicCategory.REFERENCE_MATERIAL

    # Publications and technical articles: check source & raw hints
    if src == "SRC-BENGIO" and any(k in raw_low or k in u_low for k in ("blog", "essay", "safety", "policy")):
        return EpistemicCategory.AUTHOR_COMMENTARY
    if src in {"SRC-ARXIV", "SRC-HINTON"} and any(k in raw_low for k in ("publication", "paper", "research")):
        return EpistemicCategory.PRIMARY_RESEARCH
    if src == "SRC-WIKIPEDIA":
        return EpistemicCategory.REFERENCE_MATERIAL

    return EpistemicCategory.REFERENCE_MATERIAL


def determine_chunk_epistemic_layer(
    epistemic_category: EpistemicCategory | str,
    content_origin: NormalizedContentOrigin | str,
    *,
    source_id: str | None = None,
    canonical_resource_type: CanonicalResourceType | str | None = None,
    existing_epistemic_type: str | None = None,
) -> ChunkEpistemicLayer:
    """Map `(epistemic_category, content_origin)` to the 5 control-plane chunk epistemic layers.

    Enforces the strict rule: abstracts, metadata, and summaries are NEVER silently promoted
    to `PRIMARY_SOURCE_TEXT`.
    """
    cat = (
        epistemic_category
        if isinstance(epistemic_category, EpistemicCategory)
        else EpistemicCategory(str(epistemic_category))
    )
    origin_str = (
        content_origin.value
        if isinstance(content_origin, NormalizedContentOrigin)
        else str(content_origin)
    )
    src = (source_id or "").upper()

    if origin_str == NormalizedContentOrigin.AUTHORIZED_TRANSCRIPT.value or (
        existing_epistemic_type == ChunkEpistemicLayer.TRANSCRIPT_AUTHORIZED_TEXT.value
    ):
        return ChunkEpistemicLayer.TRANSCRIPT_AUTHORIZED_TEXT

    if cat == EpistemicCategory.AI_NEWS_AND_DEVELOPMENTS or src == "SRC-THE-BATCH":
        return ChunkEpistemicLayer.NEWS_AND_COMMENTARY

    if origin_str in {
        NormalizedContentOrigin.ABSTRACT_OR_METADATA.value,
        NormalizedContentOrigin.AI_GENERATED_SUMMARY.value,
        "abstract_or_description",
        "metadata_only",
    }:
        if cat == EpistemicCategory.AUTHOR_COMMENTARY:
            return ChunkEpistemicLayer.NEWS_AND_COMMENTARY
        return ChunkEpistemicLayer.AUTHORIZED_ABSTRACT_OR_DESCRIPTION

    if origin_str == NormalizedContentOrigin.THIRD_PARTY_EXPLANATION.value:
        return ChunkEpistemicLayer.EDUCATIONAL_EXPLANATION

    # Original source text:
    if cat == EpistemicCategory.EDUCATIONAL_EXPLANATION:
        return ChunkEpistemicLayer.EDUCATIONAL_EXPLANATION
    if cat == EpistemicCategory.AUTHOR_COMMENTARY:
        return ChunkEpistemicLayer.NEWS_AND_COMMENTARY

    # PRIMARY_RESEARCH or open-license REFERENCE_MATERIAL (e.g. Wikipedia CC BY-SA 4.0 full/extract text)
    return ChunkEpistemicLayer.PRIMARY_SOURCE_TEXT


def is_foundational_evidence_eligible(
    epistemic_category: EpistemicCategory | str,
    chunk_epistemic_layer: ChunkEpistemicLayer | str,
) -> bool:
    """Return True only if a chunk/document may serve as foundational curriculum evidence.

    AI news (`AI_NEWS_AND_DEVELOPMENTS` / `NEWS_AND_COMMENTARY`) is strictly excluded
    from foundational research proof.
    """
    cat_val = (
        epistemic_category.value
        if isinstance(epistemic_category, EpistemicCategory)
        else str(epistemic_category)
    )
    layer_val = (
        chunk_epistemic_layer.value
        if isinstance(chunk_epistemic_layer, ChunkEpistemicLayer)
        else str(chunk_epistemic_layer)
    )
    if cat_val == EpistemicCategory.AI_NEWS_AND_DEVELOPMENTS.value:
        return False
    if layer_val == ChunkEpistemicLayer.NEWS_AND_COMMENTARY.value:
        return False
    return True


# ==============================================================================
# FORWARD-ONLY INGESTION SCHEMA MIGRATION (PRESERVES LATEST_SCHEMA_VERSION == 8)
# ==============================================================================


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    if not _table_exists(conn, table_name):
        return False
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(str(r[1]) == column_name for r in rows)


def ensure_ingestion_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create Task 037 ingestion tables and columns without altering `schema_migrations`."""
    ensure_knowledge_chunks_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingestion_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS normalized_documents (
            document_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL,
            version_id TEXT,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            authors TEXT,
            published_at TEXT,
            canonical_url TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            raw_resource_type TEXT,
            content_origin TEXT NOT NULL,
            epistemic_classification TEXT NOT NULL,
            chunk_epistemic_layer TEXT NOT NULL,
            license_status TEXT NOT NULL,
            permitted_storage_scope TEXT NOT NULL,
            extraction_method TEXT NOT NULL,
            extraction_timestamp TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            extraction_status TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            sections_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (resource_id) REFERENCES resources(resource_id) ON DELETE CASCADE,
            FOREIGN KEY (version_id) REFERENCES resource_versions(version_id) ON DELETE SET NULL,
            FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_norm_docs_resource_state
            ON normalized_documents(resource_id, lifecycle_state);
        CREATE INDEX IF NOT EXISTS idx_norm_docs_source_epistemic
            ON normalized_documents(source_id, epistemic_classification);

        CREATE TABLE IF NOT EXISTS knowledge_elements (
            element_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            version_id TEXT,
            source_id TEXT NOT NULL,
            element_type TEXT NOT NULL,
            element_key TEXT,
            statement TEXT NOT NULL,
            section_title TEXT,
            supporting_excerpt TEXT,
            epistemic_classification TEXT NOT NULL,
            content_origin TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (document_id) REFERENCES normalized_documents(document_id) ON DELETE CASCADE,
            FOREIGN KEY (resource_id) REFERENCES resources(resource_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_knowledge_elements_resource_type
            ON knowledge_elements(resource_id, element_type);

        CREATE TABLE IF NOT EXISTS ingestion_operations (
            operation_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            source_id TEXT,
            resource_id TEXT,
            version_id TEXT,
            status TEXT NOT NULL,
            decision_or_reason TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ingestion_ops_stage_status
            ON ingestion_operations(stage, status);
        CREATE INDEX IF NOT EXISTS idx_ingestion_ops_resource
            ON ingestion_operations(resource_id, recorded_at);
        """
    )

    extra_chunk_columns = (
        ("epistemic_classification", "TEXT"),
        ("chunk_strategy", "TEXT"),
        ("section_title", "TEXT"),
        ("structured_elements_json", "TEXT DEFAULT '[]'"),
        ("is_semantic_embedding", "INTEGER DEFAULT 0"),
    )
    for col_name, col_type in extra_chunk_columns:
        if not _column_exists(conn, "knowledge_chunks", col_name):
            conn.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN {col_name} {col_type}")

    existing_v = conn.execute(
        "SELECT version FROM ingestion_schema_migrations WHERE version = ?",
        (INGESTION_SCHEMA_VERSION,),
    ).fetchone()
    if existing_v is None:
        conn.execute(
            """
            INSERT INTO ingestion_schema_migrations (version, applied_at, description)
            VALUES (?, ?, ?)
            """,
            (
                INGESTION_SCHEMA_VERSION,
                _utc_now_iso(),
                "Task 037 source-to-knowledge ingestion tables and chunk metadata columns",
            ),
        )
    conn.commit()


# ==============================================================================
# PHASE B — SOURCE GOVERNANCE & COLLECTION POLICY VALIDATION
# ==============================================================================


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Explicit policy evaluation decision for a resource or candidate before collection/extraction."""

    source_id: str
    resource_id: str | None
    canonical_url: str
    canonical_resource_type: str
    epistemic_category: str
    requested_action: str  # "COLLECT", "EXTRACT_FULL_TEXT", "EXTRACT_METADATA_OR_ABSTRACT", "EXTRACT_TRANSCRIPT"
    is_permitted: bool
    permitted_content_origin: str | None
    permitted_chunk_layer: str | None
    license_status: str
    permitted_storage_scope: str
    attribution_requirement: str
    min_delay_seconds: float
    robots_respected: bool
    curation_authorized: bool
    decision_code: str
    reason: str
    scope_inconsistencies: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_and_reconcile_wikipedia_scope(
    conn: sqlite3.Connection,
    *,
    apply_fix: bool = False,
) -> dict[str, Any]:
    """Inspect and optionally reconcile the Wikipedia licensing/storage-scope inconsistency.

    Detects `SRC-WIKIPEDIA` resources where `license_status = 'CC_BY_SA_4_0'` and
    `permits_full_text_storage = 1` coexist with `permitted_storage_scope = 'METADATA_ONLY'`.
    """
    rows = conn.execute(
        """
        SELECT r.resource_id, r.canonical_url, r.title,
               r.license_status, r.permitted_storage_scope,
               r.permits_full_text_storage, r.permits_verbatim_excerpts,
               r.text_storage_status,
               (SELECT COUNT(*) FROM knowledge_chunks kc
                WHERE kc.resource_id = r.resource_id AND kc.lifecycle_state = 'ACTIVE'
                  AND kc.epistemic_layer = 'PRIMARY_SOURCE_TEXT') AS primary_chunk_count
        FROM resources r
        WHERE r.source_id = 'SRC-WIKIPEDIA'
        ORDER BY r.resource_id ASC
        """
    ).fetchall()

    inconsistent_records: list[dict[str, Any]] = []
    reconciled_ids: list[str] = []
    for row in rows:
        r_dict = dict(row)
        has_mismatch = (
            r_dict["license_status"] == "CC_BY_SA_4_0"
            and bool(r_dict["permits_full_text_storage"])
            and r_dict["permitted_storage_scope"] == "METADATA_ONLY"
        )
        if has_mismatch:
            target_text_status = (
                "INTRO_EXTRACT_STORED_CC_BY_SA_4_0"
                if int(r_dict["primary_chunk_count"] or 0) > 0
                else r_dict["text_storage_status"]
            )
            inconsistent_records.append(
                {
                    "resource_id": str(r_dict["resource_id"]),
                    "title": r_dict["title"],
                    "canonical_url": r_dict["canonical_url"],
                    "license_status": r_dict["license_status"],
                    "previous_permitted_storage_scope": r_dict["permitted_storage_scope"],
                    "previous_text_storage_status": r_dict["text_storage_status"],
                    "permits_full_text_storage": bool(r_dict["permits_full_text_storage"]),
                    "active_primary_chunk_count": int(r_dict["primary_chunk_count"] or 0),
                    "recommended_permitted_storage_scope": "WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED",
                    "recommended_text_storage_status": target_text_status,
                }
            )
            if apply_fix:
                conn.execute(
                    """
                    UPDATE resources
                    SET permitted_storage_scope = 'WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED',
                        text_storage_status = ?
                    WHERE resource_id = ?
                    """,
                    (target_text_status, str(r_dict["resource_id"])),
                )
                conn.execute(
                    """
                    UPDATE resource_versions
                    SET text_storage_status = ?
                    WHERE resource_id = ?
                    """,
                    (target_text_status, str(r_dict["resource_id"])),
                )
                reconciled_ids.append(str(r_dict["resource_id"]))

    if apply_fix and reconciled_ids:
        conn.commit()

    return {
        "source_id": "SRC-WIKIPEDIA",
        "total_wikipedia_resources": len(rows),
        "inconsistent_count": len(inconsistent_records),
        "inconsistent_records": inconsistent_records,
        "reconciled_resource_ids": reconciled_ids,
        "applied_fix": bool(apply_fix),
    }


def evaluate_resource_ingestion_policy(
    conn: sqlite3.Connection,
    *,
    resource_id: str | int | None = None,
    source_id: str | None = None,
    canonical_url: str | None = None,
    requested_action: str = "COLLECT",
    requested_format: ExtractionFormat | str | None = None,
    curation_approved_override: bool = False,
) -> PolicyEvaluationResult:
    """Validate source and resource governance policy before collection or extraction."""
    res_row: dict[str, Any] | None = None
    if resource_id is not None:
        res_row = _resolve_resource_row(conn, resource_id)
    elif canonical_url is not None:
        row = conn.execute(
            "SELECT * FROM resources WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if row is not None:
            res_row = dict(row)

    resolved_rid = (
        str(res_row["resource_id"])
        if res_row is not None
        else (str(resource_id) if resource_id is not None else None)
    )
    src_id = (
        (res_row.get("source_id") if res_row else None)
        or source_id
        or "UNKNOWN"
    )
    url = (
        (res_row.get("canonical_url") if res_row else None)
        or canonical_url
        or ""
    )
    raw_rtype = (res_row.get("resource_type") if res_row else None) or "web_page"
    title = (res_row.get("title") if res_row else None) or url
    modality = (res_row.get("media_modality") if res_row else None) or "TEXT_HTML"

    src_row_raw = conn.execute(
        "SELECT * FROM sources WHERE source_id = ?",
        (src_id,),
    ).fetchone()
    src_row = dict(src_row_raw) if src_row_raw is not None else {}
    src_curation_status = str(src_row.get("status") or src_row.get("curation_status") or "UNKNOWN")

    canon_type = classify_canonical_resource_type(
        raw_rtype,
        source_id=src_id,
        title=title,
        media_modality=modality,
        canonical_url=url,
        has_authorized_transcript=(requested_action == "EXTRACT_TRANSCRIPT"),
    )
    epistemic_cat = classify_epistemic_category(
        canon_type,
        source_id=src_id,
        raw_resource_type=raw_rtype,
        title=title,
        canonical_url=url,
    )

    license_status = str(
        (res_row.get("license_status") if res_row else None)
        or src_row.get("default_license_status")
        or "ALL_RIGHTS_RESERVED_METADATA_ONLY"
    )
    permitted_scope = str(
        (res_row.get("permitted_storage_scope") if res_row else None)
        or ("WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED" if src_id == "SRC-WIKIPEDIA" else "METADATA_ONLY")
    )
    permits_full_text = bool(
        res_row.get("permits_full_text_storage")
        if res_row is not None
        else src_row.get("permits_full_text_storage", 0)
    )
    permits_excerpts = bool(
        res_row.get("permits_verbatim_excerpts")
        if res_row is not None
        else src_row.get("permits_verbatim_excerpts", 0)
    )
    permits_transcript = False
    if res_row is not None and _table_exists(conn, "transcripts"):
        tr_perm = conn.execute(
            """
            SELECT permits_transcript_storage
            FROM transcripts
            WHERE resource_id = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (resolved_rid,),
        ).fetchone()
        if tr_perm is not None:
            permits_transcript = bool(tr_perm["permits_transcript_storage"])

    min_delay = float(
        src_row.get("min_enforced_delay_seconds")
        or (ARXIV_MIN_INTERVAL_SECONDS if src_id == "SRC-ARXIV" else (1.0 if src_id == "SRC-WIKIPEDIA" else 5.0))
    )
    robots_status = str((res_row.get("robots_sitemap_status") if res_row else None) or "OK")
    robots_blocked = "BLOCKED" in robots_status.upper() or "DENIED" in robots_status.upper()

    inconsistencies: list[str] = []
    warnings: list[str] = []
    if (
        src_id == "SRC-WIKIPEDIA"
        and license_status == "CC_BY_SA_4_0"
        and permits_full_text
        and (res_row or {}).get("permitted_storage_scope") == "METADATA_ONLY"
    ):
        inconsistencies.append(
            "Wikipedia resource has license_status=CC_BY_SA_4_0 and permits_full_text_storage=True "
            "but permitted_storage_scope='METADATA_ONLY'; reconcile to 'WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED'."
        )

    attribution = attribution_for_license(
        license_status,
        source_id=src_id,
        canonical_url=url,
        authors=(res_row.get("authors") if res_row else None),
    )

    # 1. Check robots.txt / policy block
    if robots_blocked:
        return PolicyEvaluationResult(
            source_id=src_id,
            resource_id=resolved_rid,
            canonical_url=url,
            canonical_resource_type=canon_type.value,
            epistemic_category=epistemic_cat.value,
            requested_action=requested_action,
            is_permitted=False,
            permitted_content_origin=None,
            permitted_chunk_layer=None,
            license_status=license_status,
            permitted_storage_scope=permitted_scope,
            attribution_requirement=attribution,
            min_delay_seconds=min_delay,
            robots_respected=False,
            curation_authorized=False,
            decision_code="REJECTED_ROBOTS_TXT_DISALLOWED",
            reason=f"Blocked by robots.txt or source policy ({robots_status}).",
            scope_inconsistencies=inconsistencies,
            warnings=warnings,
        )

    # 2. Check DeepLearning.AI The Batch strict path and curation rules
    if src_id == "SRC-THE-BATCH" or "deeplearning.ai" in url.lower():
        parsed = urlsplit(url) if url else None
        path = (parsed.path if parsed else "") or "/"
        host = (parsed.hostname or "").lower() if parsed else ""
        if host not in {"www.deeplearning.ai", "deeplearning.ai"} or not (
            path == "/the-batch" or path.startswith("/the-batch/")
        ):
            return PolicyEvaluationResult(
                source_id=src_id,
                resource_id=resolved_rid,
                canonical_url=url,
                canonical_resource_type=CanonicalResourceType.AI_NEWS_AND_REPORTS.value,
                epistemic_category=EpistemicCategory.AI_NEWS_AND_DEVELOPMENTS.value,
                requested_action=requested_action,
                is_permitted=False,
                permitted_content_origin=None,
                permitted_chunk_layer=None,
                license_status=license_status,
                permitted_storage_scope=permitted_scope,
                attribution_requirement=attribution,
                min_delay_seconds=min_delay,
                robots_respected=True,
                curation_authorized=False,
                decision_code="REJECTED_THE_BATCH_PATH_SCOPE_VIOLATION",
                reason=(
                    f"SRC-THE-BATCH is strictly scoped to https://www.deeplearning.ai/the-batch; "
                    f"general deeplearning.ai pages are prohibited (got '{url}')."
                ),
                scope_inconsistencies=inconsistencies,
                warnings=warnings,
            )
        if src_curation_status == "CANDIDATE_PENDING_REVIEW" and not curation_approved_override:
            return PolicyEvaluationResult(
                source_id=src_id,
                resource_id=resolved_rid,
                canonical_url=url,
                canonical_resource_type=CanonicalResourceType.AI_NEWS_AND_REPORTS.value,
                epistemic_category=EpistemicCategory.AI_NEWS_AND_DEVELOPMENTS.value,
                requested_action=requested_action,
                is_permitted=False,
                permitted_content_origin=None,
                permitted_chunk_layer=None,
                license_status=license_status,
                permitted_storage_scope=permitted_scope,
                attribution_requirement=attribution,
                min_delay_seconds=min_delay,
                robots_respected=True,
                curation_authorized=False,
                decision_code="REJECTED_CANDIDATE_SOURCE_PENDING_CURATION",
                reason="Source SRC-THE-BATCH is in CANDIDATE_PENDING_REVIEW status and requires explicit curation approval before collection.",
                scope_inconsistencies=inconsistencies,
                warnings=warnings,
            )

    # 3. Check discovered link candidate authorization if URL is only an unreviewed candidate
    if res_row is None and url and _table_exists(conn, "discovered_link_candidates"):
        cand_row = conn.execute(
            """
            SELECT review_status, fetch_permission_granted
            FROM discovered_link_candidates
            WHERE normalized_url = ? OR original_url = ?
            ORDER BY candidate_id DESC LIMIT 1
            """,
            (url, url),
        ).fetchone()
        if cand_row is not None and not bool(cand_row["fetch_permission_granted"]):
            return PolicyEvaluationResult(
                source_id=src_id,
                resource_id=None,
                canonical_url=url,
                canonical_resource_type=canon_type.value,
                epistemic_category=epistemic_cat.value,
                requested_action=requested_action,
                is_permitted=False,
                permitted_content_origin=None,
                permitted_chunk_layer=None,
                license_status=license_status,
                permitted_storage_scope=permitted_scope,
                attribution_requirement=attribution,
                min_delay_seconds=min_delay,
                robots_respected=True,
                curation_authorized=False,
                decision_code="REJECTED_UNAPPROVED_DISCOVERED_CANDIDATE",
                reason=(
                    f"Discovered candidate '{url}' has review_status='{cand_row['review_status']}' "
                    "and fetch_permission_granted=0; human curation approval is required before collection."
                ),
                scope_inconsistencies=inconsistencies,
                warnings=warnings,
            )

    # 4. Check EXTERNAL_REFERENCE_ONLY / RECORD_ONLY sources
    if src_curation_status in {"EXTERNAL_REFERENCE_ONLY", "RECORD_ONLY"} and requested_action in {
        "COLLECT",
        "EXTRACT_FULL_TEXT",
        "EXTRACT_TRANSCRIPT",
    }:
        return PolicyEvaluationResult(
            source_id=src_id,
            resource_id=resolved_rid,
            canonical_url=url,
            canonical_resource_type=canon_type.value,
            epistemic_category=epistemic_cat.value,
            requested_action=requested_action,
            is_permitted=False,
            permitted_content_origin=None,
            permitted_chunk_layer=None,
            license_status=license_status,
            permitted_storage_scope=permitted_scope,
            attribution_requirement=attribution,
            min_delay_seconds=min_delay,
            robots_respected=True,
            curation_authorized=False,
            decision_code="REJECTED_EXTERNAL_REFERENCE_ONLY_SOURCE",
            reason=f"Source '{src_id}' is registered as {src_curation_status}; automated collection and full-text storage are prohibited.",
            scope_inconsistencies=inconsistencies,
            warnings=warnings,
        )

    # 5. Check Transcript extraction rules (especially 3Blue1Brown / video resources)
    if requested_action == "EXTRACT_TRANSCRIPT" or requested_format == ExtractionFormat.AUTHORIZED_TRANSCRIPT:
        has_active_attestation = False
        if resolved_rid is not None and _table_exists(conn, "transcript_authorization_attestations"):
            att = conn.execute(
                """
                SELECT 1 FROM transcript_authorization_attestations
                WHERE resource_id = ?
                  AND verification_status = 'VERIFIED'
                  AND withdrawn_at IS NULL
                  AND permits_transcript_storage = 1
                LIMIT 1
                """,
                (resolved_rid,),
            ).fetchone()
            has_active_attestation = att is not None
        if not (permits_transcript and has_active_attestation):
            return PolicyEvaluationResult(
                source_id=src_id,
                resource_id=resolved_rid,
                canonical_url=url,
                canonical_resource_type=CanonicalResourceType.AUTHORIZED_TRANSCRIPTS.value,
                epistemic_category=epistemic_cat.value,
                requested_action=requested_action,
                is_permitted=False,
                permitted_content_origin=None,
                permitted_chunk_layer=None,
                license_status=license_status,
                permitted_storage_scope=permitted_scope,
                attribution_requirement=attribution,
                min_delay_seconds=min_delay,
                robots_respected=True,
                curation_authorized=True,
                decision_code="REJECTED_UNAUTHORIZED_TRANSCRIPT_STORAGE",
                reason=(
                    "Video or lesson access does not permit transcript extraction or storage. "
                    "Requires both permits_transcript_storage=1 and an active TranscriptAuthorizationAttestation."
                ),
                scope_inconsistencies=inconsistencies,
                warnings=warnings,
            )
        return PolicyEvaluationResult(
            source_id=src_id,
            resource_id=resolved_rid,
            canonical_url=url,
            canonical_resource_type=CanonicalResourceType.AUTHORIZED_TRANSCRIPTS.value,
            epistemic_category=epistemic_cat.value,
            requested_action=requested_action,
            is_permitted=True,
            permitted_content_origin=NormalizedContentOrigin.AUTHORIZED_TRANSCRIPT.value,
            permitted_chunk_layer=ChunkEpistemicLayer.TRANSCRIPT_AUTHORIZED_TEXT.value,
            license_status=license_status,
            permitted_storage_scope=permitted_scope,
            attribution_requirement=attribution,
            min_delay_seconds=min_delay,
            robots_respected=True,
            curation_authorized=True,
            decision_code="PERMITTED_AUTHORIZED_TRANSCRIPT",
            reason="Verified active transcript authorization attestation and permits_transcript_storage=1.",
            scope_inconsistencies=inconsistencies,
            warnings=warnings,
        )

    # 6. Check Full-Text extraction rules
    if requested_action == "EXTRACT_FULL_TEXT" or requested_format in {
        ExtractionFormat.PDF,
        ExtractionFormat.SLIDE_DOCUMENT,
    }:
        if not permits_full_text:
            return PolicyEvaluationResult(
                source_id=src_id,
                resource_id=resolved_rid,
                canonical_url=url,
                canonical_resource_type=canon_type.value,
                epistemic_category=epistemic_cat.value,
                requested_action=requested_action,
                is_permitted=False,
                permitted_content_origin=None,
                permitted_chunk_layer=None,
                license_status=license_status,
                permitted_storage_scope=permitted_scope,
                attribution_requirement=attribution,
                min_delay_seconds=min_delay,
                robots_respected=True,
                curation_authorized=True,
                decision_code="REJECTED_FULL_TEXT_NOT_AUTHORIZED",
                reason=(
                    f"Resource (source={src_id}, license={license_status}, scope={permitted_scope}) "
                    "does not authorize full-text extraction or storage (permits_full_text_storage=0)."
                ),
                scope_inconsistencies=inconsistencies,
                warnings=warnings,
            )
        layer = determine_chunk_epistemic_layer(
            epistemic_cat,
            NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT,
            source_id=src_id,
            canonical_resource_type=canon_type,
        )
        return PolicyEvaluationResult(
            source_id=src_id,
            resource_id=resolved_rid,
            canonical_url=url,
            canonical_resource_type=canon_type.value,
            epistemic_category=epistemic_cat.value,
            requested_action=requested_action,
            is_permitted=True,
            permitted_content_origin=NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT.value,
            permitted_chunk_layer=layer.value,
            license_status=license_status,
            permitted_storage_scope=(
                "WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED"
                if src_id == "SRC-WIKIPEDIA" and permitted_scope == "METADATA_ONLY"
                else permitted_scope
            ),
            attribution_requirement=attribution,
            min_delay_seconds=min_delay,
            robots_respected=True,
            curation_authorized=True,
            decision_code="PERMITTED_AUTHORIZED_FULL_TEXT",
            reason=f"Authorized full-text extraction under license={license_status}.",
            scope_inconsistencies=inconsistencies,
            warnings=warnings,
        )

    # 7. Default: COLLECT or EXTRACT_METADATA_OR_ABSTRACT
    if permits_full_text and src_id == "SRC-WIKIPEDIA":
        origin = NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT
        layer = determine_chunk_epistemic_layer(
            epistemic_cat,
            origin,
            source_id=src_id,
            canonical_resource_type=canon_type,
        )
        effective_scope = "WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED"
        decision_code = "PERMITTED_WIKIPEDIA_CC_BY_SA_4_0_API_EXTRACT"
        reason = "Authorized Wikipedia MediaWiki API collection and CC BY-SA 4.0 text extract with revision oldid attribution."
    else:
        origin = NormalizedContentOrigin.ABSTRACT_OR_METADATA
        layer = determine_chunk_epistemic_layer(
            epistemic_cat,
            origin,
            source_id=src_id,
            canonical_resource_type=canon_type,
        )
        effective_scope = permitted_scope
        decision_code = (
            "PERMITTED_ARXIV_ATOM_API_METADATA_AND_ABSTRACT"
            if src_id == "SRC-ARXIV"
            else "PERMITTED_GOVERNED_METADATA_OR_ABSTRACT"
        )
        reason = (
            f"Authorized under source={src_id}, license={license_status}, scope={effective_scope} "
            f"(permits_full_text={permits_full_text}, permits_excerpts={permits_excerpts})."
        )

    return PolicyEvaluationResult(
        source_id=src_id,
        resource_id=resolved_rid,
        canonical_url=url,
        canonical_resource_type=canon_type.value,
        epistemic_category=epistemic_cat.value,
        requested_action=requested_action,
        is_permitted=True,
        permitted_content_origin=origin.value,
        permitted_chunk_layer=layer.value,
        license_status=license_status,
        permitted_storage_scope=effective_scope,
        attribution_requirement=attribution,
        min_delay_seconds=min_delay,
        robots_respected=True,
        curation_authorized=True,
        decision_code=decision_code,
        reason=reason,
        scope_inconsistencies=inconsistencies,
        warnings=warnings,
    )


# ==============================================================================
# PHASE D — CONTENT EXTRACTION & 15-FIELD NORMALIZATION
# ==============================================================================


class IngestionPolicyViolationError(ValueError):
    """Raised when an extraction or normalization step violates source governance."""


@dataclass(frozen=True)
class NormalizedSection:
    """A logical section or time-bounded segment within a normalized document."""

    section_index: int
    section_title: str
    section_type: str  # "abstract", "introduction", "method", "equations", "experiments", "results", "limitations", "concept", "transcript_segment", "slide", "body"
    text: str
    start_time_seconds: float | None = None
    end_time_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedDocument:
    """Normalized internal document representation preserving all 15 required provenance fields."""

    document_id: str
    # 15 mandatory normalized fields:
    resource_id: str
    version_id: str | None
    source_id: str
    title: str
    authors: str | None
    published_at: str | None
    canonical_url: str
    resource_type: str  # CanonicalResourceType value (12 unified types)
    content_origin: str  # NormalizedContentOrigin value (5 origins)
    epistemic_classification: str  # EpistemicCategory value (5 categories)
    license_status: str
    permitted_storage_scope: str
    extraction_method: str
    extraction_timestamp: str
    content_hash: str
    extraction_status: str
    # Additional structured payload:
    chunk_epistemic_layer: str
    normalized_text: str
    sections: tuple[NormalizedSection, ...] = ()
    raw_resource_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Enforce strict content-origin vs primary-source invariants:
        if self.content_origin in {
            NormalizedContentOrigin.ABSTRACT_OR_METADATA.value,
            NormalizedContentOrigin.THIRD_PARTY_EXPLANATION.value,
            NormalizedContentOrigin.AI_GENERATED_SUMMARY.value,
        } and self.chunk_epistemic_layer == ChunkEpistemicLayer.PRIMARY_SOURCE_TEXT.value:
            raise IngestionPolicyViolationError(
                f"Cannot classify content_origin='{self.content_origin}' as PRIMARY_SOURCE_TEXT "
                f"(resource_id={self.resource_id}, title={self.title!r}). "
                "Summaries, abstracts, and metadata must never be silently promoted to primary-source text."
            )

    def required_15_fields(self) -> dict[str, Any]:
        """Return the exact 15 mandatory normalized fields required by Task 037 Phase D."""
        return {
            "resource_id": self.resource_id,
            "version_id": self.version_id,
            "source_id": self.source_id,
            "title": self.title,
            "authors": self.authors,
            "published_at": self.published_at,
            "canonical_url": self.canonical_url,
            "resource_type": self.resource_type,
            "content_origin": self.content_origin,
            "epistemic_classification": self.epistemic_classification,
            "license_status": self.license_status,
            "permitted_storage_scope": self.permitted_storage_scope,
            "extraction_method": self.extraction_method,
            "extraction_timestamp": self.extraction_timestamp,
            "content_hash": self.content_hash,
            "extraction_status": self.extraction_status,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sections"] = [s.to_dict() for s in self.sections]
        payload["required_15_fields"] = self.required_15_fields()
        return payload


def _infer_section_type(heading: str, default_type: str = "body") -> str:
    low = heading.lower().strip()
    if "abstract" in low or "summary" in low:
        return "abstract"
    if "intro" in low or "background" in low or "motivation" in low:
        return "introduction"
    if "method" in low or "architecture" in low or "algorithm" in low or "formulation" in low or "approach" in low:
        return "method"
    if "equation" in low or "derivation" in low or "theorem" in low or "proof" in low or "mathematics" in low:
        return "equations"
    if "experiment" in low or "evaluation" in low or "setup" in low or "benchmark" in low:
        return "experiments"
    if "result" in low or "finding" in low or "performance" in low:
        return "results"
    if "limit" in low or "risk" in low or "challenge" in low or "caveat" in low or "future" in low:
        return "limitations"
    if "definition" in low or "concept" in low or "intuition" in low:
        return "concept"
    if "slide" in low:
        return "slide"
    return default_type


class ContentExtractor:
    """Reusable multi-format content extraction and 15-field normalization pipeline (Phase D)."""

    def extract_and_normalize(
        self,
        conn: sqlite3.Connection,
        *,
        resource_id: str | int,
        raw_content: str | bytes | None = None,
        extraction_format: ExtractionFormat | str | None = None,
        content_origin_override: NormalizedContentOrigin | str | None = None,
        persist: bool = True,
        dry_run: bool = False,
    ) -> NormalizedDocument:
        ensure_ingestion_schema(conn)
        res = _resolve_resource_row(conn, resource_id)
        if res is None:
            raise ValueError(f"Resource ID {resource_id!r} not found in resources table.")
        resolved_rid = str(res["resource_id"])

        ver_row = conn.execute(
            """
            SELECT * FROM resource_versions
            WHERE resource_id = ?
            ORDER BY version_number DESC LIMIT 1
            """,
            (resolved_rid,),
        ).fetchone()
        ver = dict(ver_row) if ver_row is not None else None
        version_id = (
            str(ver["version_id"])
            if ver is not None and ver.get("version_id")
            else (str(res["current_version_id"]) if res.get("current_version_id") else None)
        )

        src_id = str(res["source_id"])
        title = str(res["title"])
        authors = res.get("authors")
        published_at = res.get("published_at")
        canonical_url = str(res["canonical_url"])
        raw_rtype = str(res.get("resource_type") or "web_page")
        modality = str(res.get("media_modality") or "TEXT_HTML")

        # Determine format if not explicitly supplied
        fmt = self._resolve_format(
            extraction_format=extraction_format,
            res=res,
            raw_content=raw_content,
        )

        requested_action = "EXTRACT_METADATA_OR_ABSTRACT"
        if fmt == ExtractionFormat.AUTHORIZED_TRANSCRIPT:
            requested_action = "EXTRACT_TRANSCRIPT"
        elif fmt in {ExtractionFormat.PDF, ExtractionFormat.SLIDE_DOCUMENT}:
            requested_action = "EXTRACT_FULL_TEXT"
        elif fmt in {ExtractionFormat.HTML, ExtractionFormat.PLAIN_TEXT} and raw_content is not None:
            requested_action = "EXTRACT_FULL_TEXT"

        policy = evaluate_resource_ingestion_policy(
            conn,
            resource_id=resolved_rid,
            source_id=src_id,
            canonical_url=canonical_url,
            requested_action=requested_action,
            requested_format=fmt,
        )
        if not policy.is_permitted:
            self._record_operation(
                conn,
                stage="EXTRACTION",
                source_id=src_id,
                resource_id=resolved_rid,
                version_id=version_id,
                status="REJECTED_BY_POLICY",
                decision_or_reason=f"{policy.decision_code}: {policy.reason}",
                retryable=False,
                details={"requested_format": fmt.value, "requested_action": requested_action},
                dry_run=dry_run,
            )
            raise IngestionPolicyViolationError(f"{policy.decision_code}: {policy.reason}")

        # Parse content according to format
        sections, normalized_text, extraction_method, resolved_origin = self._parse_by_format(
            conn=conn,
            res=res,
            ver=ver,
            fmt=fmt,
            raw_content=raw_content,
            policy=policy,
        )

        if content_origin_override is not None:
            override_val = (
                content_origin_override.value
                if isinstance(content_origin_override, NormalizedContentOrigin)
                else str(content_origin_override)
            )
            # Never allow overriding abstract/metadata/summary into Original source text
            if (
                resolved_origin != NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT
                and override_val == NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT.value
            ):
                raise IngestionPolicyViolationError(
                    f"Cannot promote {resolved_origin.value!r} into 'Original source text' for resource_id={resolved_rid}."
                )
            resolved_origin = NormalizedContentOrigin(override_val)

        canon_type = classify_canonical_resource_type(
            raw_rtype,
            source_id=src_id,
            title=title,
            media_modality=modality,
            canonical_url=canonical_url,
            has_authorized_transcript=(fmt == ExtractionFormat.AUTHORIZED_TRANSCRIPT),
        )
        epistemic_cat = classify_epistemic_category(
            canon_type,
            source_id=src_id,
            raw_resource_type=raw_rtype,
            title=title,
            canonical_url=canonical_url,
        )
        chunk_layer = determine_chunk_epistemic_layer(
            epistemic_cat,
            resolved_origin,
            source_id=src_id,
            canonical_resource_type=canon_type,
        )

        content_hash = _sha256_text(normalized_text)
        extraction_ts = _utc_now_iso()
        doc_id = f"doc:{resolved_rid}:{version_id or 'v0'}:{content_hash[:12]}"

        doc = NormalizedDocument(
            document_id=doc_id,
            resource_id=resolved_rid,
            version_id=version_id,
            source_id=src_id,
            title=title,
            authors=authors,
            published_at=published_at,
            canonical_url=canonical_url,
            resource_type=canon_type.value,
            content_origin=resolved_origin.value,
            epistemic_classification=epistemic_cat.value,
            license_status=policy.license_status,
            permitted_storage_scope=policy.permitted_storage_scope,
            extraction_method=extraction_method,
            extraction_timestamp=extraction_ts,
            content_hash=content_hash,
            extraction_status="EXTRACTED_AND_NORMALIZED",
            chunk_epistemic_layer=chunk_layer.value,
            normalized_text=normalized_text,
            sections=tuple(sections),
            raw_resource_type=raw_rtype,
            metadata={
                "attribution_requirement": policy.attribution_requirement,
                "foundational_evidence_eligible": is_foundational_evidence_eligible(
                    epistemic_cat, chunk_layer
                ),
                "extraction_format": fmt.value,
                "section_count": len(sections),
            },
        )

        if persist and not dry_run:
            self._persist_normalized_document(conn, doc)
            self._record_operation(
                conn,
                stage="NORMALIZATION",
                source_id=src_id,
                resource_id=resolved_rid,
                version_id=version_id,
                status="SUCCEEDED",
                decision_or_reason=f"Normalized document {doc.document_id} ({doc.content_origin} -> {doc.chunk_epistemic_layer})",
                retryable=False,
                details={
                    "document_id": doc.document_id,
                    "content_hash": doc.content_hash,
                    "section_count": len(doc.sections),
                    "resource_type": doc.resource_type,
                    "epistemic_classification": doc.epistemic_classification,
                },
                dry_run=False,
            )
        elif dry_run:
            self._record_operation(
                conn,
                stage="NORMALIZATION",
                source_id=src_id,
                resource_id=resolved_rid,
                version_id=version_id,
                status="DRY_RUN",
                decision_or_reason=f"Dry-run normalization preview for {doc.document_id}",
                retryable=False,
                details={"document_id": doc.document_id},
                dry_run=True,
            )

        return doc

    def _resolve_format(
        self,
        *,
        extraction_format: ExtractionFormat | str | None,
        res: dict[str, Any],
        raw_content: str | bytes | None,
    ) -> ExtractionFormat:
        if extraction_format is not None:
            if isinstance(extraction_format, ExtractionFormat):
                return extraction_format
            return ExtractionFormat(str(extraction_format).upper())

        if isinstance(raw_content, bytes) and raw_content.startswith(b"%PDF"):
            return ExtractionFormat.PDF
        if isinstance(raw_content, str):
            stripped = raw_content.lstrip()
            if stripped.startswith("%PDF"):
                return ExtractionFormat.PDF
            if "<html" in stripped.lower() or "<body" in stripped.lower() or "<article" in stripped.lower() or "<h1" in stripped.lower():
                return ExtractionFormat.HTML
            if "slide " in stripped.lower() and "---" in stripped:
                return ExtractionFormat.SLIDE_DOCUMENT
            return ExtractionFormat.PLAIN_TEXT

        if str(res.get("source_id")) == "SRC-WIKIPEDIA" and bool(res.get("permits_full_text_storage")):
            return ExtractionFormat.PLAIN_TEXT

        return ExtractionFormat.STRUCTURED_METADATA

    def _parse_by_format(
        self,
        *,
        conn: sqlite3.Connection,
        res: dict[str, Any],
        ver: dict[str, Any] | None,
        fmt: ExtractionFormat,
        raw_content: str | bytes | None,
        policy: PolicyEvaluationResult,
    ) -> tuple[list[NormalizedSection], str, str, NormalizedContentOrigin]:
        src_id = str(res["source_id"])
        title = str(res["title"])
        resolved_rid = str(res["resource_id"])

        if fmt == ExtractionFormat.AUTHORIZED_TRANSCRIPT:
            t_row = None
            if _table_exists(conn, "transcripts"):
                t_row = conn.execute(
                    """
                    SELECT * FROM transcripts
                    WHERE resource_id = ? AND permits_transcript_storage = 1
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (resolved_rid,),
                ).fetchone()
            if t_row is None and raw_content is None:
                raise IngestionPolicyViolationError(
                    f"No active authorized transcript record found for resource_id={resolved_rid}."
                )
            transcript_body = ""
            if raw_content is not None:
                transcript_body = (
                    raw_content.decode("utf-8", errors="replace")
                    if isinstance(raw_content, bytes)
                    else str(raw_content)
                ).strip()
            elif t_row is not None and _table_exists(conn, "transcript_segments"):
                seg_rows = conn.execute(
                    """
                    SELECT sequence_index, start_ms, end_ms,
                           COALESCE(normalized_segment_text, original_segment_text, '') AS seg_text
                    FROM transcript_segments
                    WHERE transcript_id = ?
                    ORDER BY sequence_index ASC
                    """,
                    (str(t_row["transcript_id"]),),
                ).fetchall()
                seg_lines: list[str] = []
                for sr in seg_rows:
                    txt = str(sr["seg_text"] or "").strip()
                    if not txt:
                        continue
                    start_ms = sr["start_ms"]
                    if start_ms is not None:
                        total_s = int(start_ms) // 1000
                        mm, ss = divmod(total_s, 60)
                        seg_lines.append(f"[{mm:02d}:{ss:02d}] {txt}")
                    else:
                        seg_lines.append(txt)
                transcript_body = "\n".join(seg_lines).strip()
            if not transcript_body:
                raise ValueError(f"Empty transcript content for resource_id={resolved_rid}.")
            sections = self._split_transcript_sections(transcript_body, default_title=title)
            norm_text = "\n\n".join(f"[{s.section_title}]\n{s.text}" for s in sections)
            return (
                sections,
                norm_text,
                "authorized_transcript_extractor_v1",
                NormalizedContentOrigin.AUTHORIZED_TRANSCRIPT,
            )

        if fmt == ExtractionFormat.HTML:
            html_str = (
                raw_content.decode("utf-8", errors="replace")
                if isinstance(raw_content, bytes)
                else str(raw_content or "")
            )
            soup = BeautifulSoup(html_str, "html.parser")
            for tag in soup.find_all(["script", "style", "nav", "footer", "noscript"]):
                tag.decompose()
            sections = self._extract_html_sections(soup, default_title=title)
            norm_text = "\n\n".join(f"## {s.section_title}\n{s.text}" for s in sections)
            origin = (
                NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT
                if bool(res.get("permits_full_text_storage"))
                else NormalizedContentOrigin.ABSTRACT_OR_METADATA
            )
            return sections, norm_text, "html_dom_section_extractor_v1", origin

        if fmt == ExtractionFormat.PDF:
            pdf_text = self._extract_pdf_text(raw_content)
            sections = self._extract_plain_text_sections(pdf_text, default_title=title)
            norm_text = "\n\n".join(f"## {s.section_title}\n{s.text}" for s in sections)
            return (
                sections,
                norm_text,
                "authorized_pdf_section_extractor_v1",
                NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT,
            )

        if fmt == ExtractionFormat.SLIDE_DOCUMENT:
            slide_str = (
                raw_content.decode("utf-8", errors="replace")
                if isinstance(raw_content, bytes)
                else str(raw_content or "")
            )
            raw_slides = [b.strip() for b in re.split(r"\n---\n|\n===+\n|(?=^Slide\s+\d+:)", slide_str, flags=re.M) if b.strip()]
            sections: list[NormalizedSection] = []
            for idx, block in enumerate(raw_slides):
                lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
                heading = lines[0] if lines else f"Slide {idx + 1}"
                body = "\n".join(lines[1:]) if len(lines) > 1 else block
                sections.append(
                    NormalizedSection(
                        section_index=idx,
                        section_title=heading,
                        section_type="slide",
                        text=body,
                    )
                )
            norm_text = "\n\n".join(f"## {s.section_title}\n{s.text}" for s in sections)
            return (
                sections,
                norm_text,
                "authorized_slide_deck_extractor_v1",
                NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT,
            )

        if fmt == ExtractionFormat.PLAIN_TEXT:
            text_body = (
                raw_content.decode("utf-8", errors="replace")
                if isinstance(raw_content, bytes)
                else str(raw_content or "")
            ).strip()
            if not text_body:
                # Check existing active primary chunks or resource description for Wikipedia
                existing_chunks = conn.execute(
                    """
                    SELECT chunk_text, chunk_index FROM knowledge_chunks
                    WHERE resource_id = ? AND lifecycle_state = 'ACTIVE'
                    ORDER BY chunk_index ASC
                    """,
                    (resolved_rid,),
                ).fetchall()
                if existing_chunks:
                    text_body = "\n\n".join(str(r["chunk_text"]).strip() for r in existing_chunks if str(r["chunk_text"]).strip())
                else:
                    text_body = str(res.get("abstract_or_description") or "").strip()
            if not text_body:
                raise ValueError(f"No plain-text body available to extract for resource_id={resolved_rid}.")
            sections = self._extract_plain_text_sections(text_body, default_title=title)
            norm_text = "\n\n".join(f"## {s.section_title}\n{s.text}" for s in sections)
            is_primary = bool(res.get("permits_full_text_storage")) and (
                src_id == "SRC-WIKIPEDIA" or raw_content is not None or len(text_body) > 220
            )
            origin = (
                NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT
                if is_primary
                else NormalizedContentOrigin.ABSTRACT_OR_METADATA
            )
            method = (
                "mediawiki_cc_by_sa_4_0_text_extractor_v1"
                if src_id == "SRC-WIKIPEDIA"
                else "plain_text_section_extractor_v1"
            )
            return sections, norm_text, method, origin

        # Default: STRUCTURED_METADATA (arXiv Atom API abstract/metadata or curated resource metadata)
        abstract_text = str(res.get("abstract_or_description") or "").strip()
        topic_tags = str(res.get("topic_tags") or "").strip()
        authors = str(res.get("authors") or "Unknown authors").strip()
        ext_id = str(res.get("external_identifier") or "").strip()
        parts = [f"Title: {title}", f"Authors: {authors}"]
        if ext_id:
            parts.append(f"Identifier: {ext_id}")
        if abstract_text:
            parts.append(f"Abstract / Description: {abstract_text}")
        if topic_tags:
            parts.append(f"Topics: {topic_tags}")
        combined = "\n".join(parts)
        sections = [
            NormalizedSection(
                section_index=0,
                section_title="Abstract and Bibliographic Metadata",
                section_type="abstract",
                text=combined,
            )
        ]
        method = (
            "arxiv_atom_api_metadata_abstract_extractor_v1"
            if src_id == "SRC-ARXIV"
            else "structured_metadata_extractor_v1"
        )
        return (
            sections,
            combined,
            method,
            NormalizedContentOrigin.ABSTRACT_OR_METADATA,
        )

    def _extract_html_sections(
        self,
        soup: BeautifulSoup,
        *,
        default_title: str,
    ) -> list[NormalizedSection]:
        sections: list[NormalizedSection] = []
        current_heading = default_title
        current_paragraphs: list[str] = []

        root = soup.find("article") or soup.find("main") or soup.find("body") or soup
        for elem in root.find_all(["h1", "h2", "h3", "p", "li", "pre", "blockquote"]):
            tag_name = elem.name.lower()
            text = " ".join(elem.get_text(" ", strip=True).split())
            if not text:
                continue
            if tag_name in {"h1", "h2", "h3"}:
                if current_paragraphs:
                    joined = "\n".join(current_paragraphs).strip()
                    if joined:
                        sections.append(
                            NormalizedSection(
                                section_index=len(sections),
                                section_title=current_heading,
                                section_type=_infer_section_type(current_heading),
                                text=joined,
                            )
                        )
                    current_paragraphs = []
                current_heading = text
            else:
                if not current_paragraphs or current_paragraphs[-1] != text:
                    current_paragraphs.append(text)

        if current_paragraphs:
            joined = "\n".join(current_paragraphs).strip()
            if joined:
                sections.append(
                    NormalizedSection(
                        section_index=len(sections),
                        section_title=current_heading,
                        section_type=_infer_section_type(current_heading),
                        text=joined,
                    )
                )

        if not sections:
            fallback = " ".join(root.get_text(" ", strip=True).split())
            sections.append(
                NormalizedSection(
                    section_index=0,
                    section_title=default_title,
                    section_type="body",
                    text=fallback or default_title,
                )
            )
        return sections

    def _extract_plain_text_sections(
        self,
        text_body: str,
        *,
        default_title: str,
    ) -> list[NormalizedSection]:
        lines = text_body.splitlines()
        sections: list[NormalizedSection] = []
        current_heading = default_title
        current_lines: list[str] = []

        heading_re = re.compile(
            r"^(?:#{1,4}\s+(.+)|(\d+(?:\.\d+)*\s+[A-Z][A-Za-z0-9\s\-:]{2,70})|"
            r"(Abstract|Introduction|Background|Methods?|Methodology|Experiments?|Results?|Discussion|Limitations?|Conclusion|References)\s*:?)$",
            re.I,
        )

        for raw_line in lines:
            stripped = raw_line.strip()
            m = heading_re.match(stripped)
            if m:
                heading_text = (m.group(1) or m.group(2) or m.group(3) or stripped).strip()
                body_so_far = "\n".join(current_lines).strip()
                if body_so_far:
                    sections.append(
                        NormalizedSection(
                            section_index=len(sections),
                            section_title=current_heading,
                            section_type=_infer_section_type(current_heading),
                            text=body_so_far,
                        )
                    )
                    current_lines = []
                current_heading = heading_text
            else:
                current_lines.append(raw_line)

        remainder = "\n".join(current_lines).strip()
        if remainder:
            sections.append(
                NormalizedSection(
                    section_index=len(sections),
                    section_title=current_heading,
                    section_type=_infer_section_type(current_heading),
                    text=remainder,
                )
            )

        if not sections:
            sections.append(
                NormalizedSection(
                    section_index=0,
                    section_title=default_title,
                    section_type="body",
                    text=text_body.strip() or default_title,
                )
            )
        return sections

    def _extract_pdf_text(self, raw_content: str | bytes | None) -> str:
        if raw_content is None:
            raise ValueError("PDF extraction requires authorized PDF content bytes or text.")
        if isinstance(raw_content, bytes):
            text = raw_content.decode("utf-8", errors="ignore")
        else:
            text = str(raw_content)
        # Extract parenthesized PDF text literals `( ... ) Tj` if raw PDF stream is passed, else treat as extracted text
        if text.lstrip().startswith("%PDF"):
            literals = re.findall(r"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj", text)
            if literals:
                cleaned = "\n".join(lit.replace(r"\(", "(").replace(r"\)", ")") for lit in literals)
                if cleaned.strip():
                    return cleaned.strip()
            # Fallback: strip PDF header/trailer markers
            stripped_lines = [
                ln for ln in text.splitlines()
                if not ln.startswith("%PDF") and not ln.startswith("%%EOF") and ln.strip() not in {"stream", "endstream", "obj", "endobj"}
            ]
            return "\n".join(stripped_lines).strip()
        return text.strip()

    def _split_transcript_sections(
        self,
        transcript_body: str,
        *,
        default_title: str,
    ) -> list[NormalizedSection]:
        """Split authorized transcripts by timestamp cues `[MM:SS]` or topic markers."""
        cue_re = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(?:([^:\n]{2,50}):\s*)?(.*)$")
        sections: list[NormalizedSection] = []
        current_title = f"{default_title} — Segment 1"
        current_start: float | None = 0.0
        current_lines: list[str] = []

        for raw_line in transcript_body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = cue_re.match(line)
            if m:
                mins_or_hrs = int(m.group(1))
                secs_or_mins = int(m.group(2))
                secs_opt = int(m.group(3)) if m.group(3) is not None else None
                if secs_opt is not None:
                    ts_sec = float(mins_or_hrs * 3600 + secs_or_mins * 60 + secs_opt)
                else:
                    ts_sec = float(mins_or_hrs * 60 + secs_or_mins)
                topic_hint = (m.group(4) or "").strip()
                rest_text = (m.group(5) or "").strip()
                if current_lines and (len("\n".join(current_lines)) >= 220 or topic_hint):
                    sections.append(
                        NormalizedSection(
                            section_index=len(sections),
                            section_title=current_title,
                            section_type="transcript_segment",
                            text="\n".join(current_lines).strip(),
                            start_time_seconds=current_start,
                            end_time_seconds=ts_sec,
                        )
                    )
                    current_lines = []
                    current_start = ts_sec
                    current_title = topic_hint or f"{default_title} @ {int(ts_sec)}s"
                elif not current_lines:
                    current_start = ts_sec
                    if topic_hint:
                        current_title = topic_hint
                if rest_text:
                    current_lines.append(rest_text)
            else:
                current_lines.append(line)

        if current_lines:
            sections.append(
                NormalizedSection(
                    section_index=len(sections),
                    section_title=current_title,
                    section_type="transcript_segment",
                    text="\n".join(current_lines).strip(),
                    start_time_seconds=current_start,
                    end_time_seconds=None,
                )
            )
        return sections

    def _persist_normalized_document(
        self,
        conn: sqlite3.Connection,
        doc: NormalizedDocument,
    ) -> None:
        now = _utc_now_iso()
        # Supersede earlier active normalized documents for this resource if content_hash differs
        conn.execute(
            """
            UPDATE normalized_documents
            SET lifecycle_state = 'SUPERSEDED', updated_at = ?
            WHERE resource_id = ? AND lifecycle_state = 'ACTIVE' AND document_id != ?
            """,
            (now, doc.resource_id, doc.document_id),
        )
        conn.execute(
            """
            INSERT INTO normalized_documents (
                document_id, resource_id, version_id, source_id, title, authors,
                published_at, canonical_url, resource_type, raw_resource_type,
                content_origin, epistemic_classification, chunk_epistemic_layer,
                license_status, permitted_storage_scope, extraction_method,
                extraction_timestamp, content_hash, extraction_status,
                normalized_text, sections_json, metadata_json,
                lifecycle_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                version_id = excluded.version_id,
                resource_type = excluded.resource_type,
                content_origin = excluded.content_origin,
                epistemic_classification = excluded.epistemic_classification,
                chunk_epistemic_layer = excluded.chunk_epistemic_layer,
                license_status = excluded.license_status,
                permitted_storage_scope = excluded.permitted_storage_scope,
                extraction_method = excluded.extraction_method,
                extraction_timestamp = excluded.extraction_timestamp,
                extraction_status = excluded.extraction_status,
                normalized_text = excluded.normalized_text,
                sections_json = excluded.sections_json,
                metadata_json = excluded.metadata_json,
                lifecycle_state = 'ACTIVE',
                updated_at = excluded.updated_at
            """,
            (
                doc.document_id,
                doc.resource_id,
                doc.version_id,
                doc.source_id,
                doc.title,
                doc.authors,
                doc.published_at,
                doc.canonical_url,
                doc.resource_type,
                doc.raw_resource_type,
                doc.content_origin,
                doc.epistemic_classification,
                doc.chunk_epistemic_layer,
                doc.license_status,
                doc.permitted_storage_scope,
                doc.extraction_method,
                doc.extraction_timestamp,
                doc.content_hash,
                doc.extraction_status,
                doc.normalized_text,
                json.dumps([s.to_dict() for s in doc.sections], ensure_ascii=False),
                json.dumps(doc.metadata, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()

    def _record_operation(
        self,
        conn: sqlite3.Connection,
        *,
        stage: str,
        source_id: str | None,
        resource_id: str | None,
        version_id: str | None,
        status: str,
        decision_or_reason: str,
        retryable: bool,
        details: dict[str, Any],
        dry_run: bool = False,
    ) -> str:
        ts = _utc_now_iso()
        seed = f"{stage}:{source_id}:{resource_id}:{status}:{ts}:{decision_or_reason[:40]}"
        op_id = f"op:{_sha256_text(seed)[:16]}"
        if not dry_run:
            conn.execute(
                """
                INSERT OR REPLACE INTO ingestion_operations (
                    operation_id, stage, source_id, resource_id, version_id,
                    status, decision_or_reason, retryable, details_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    op_id,
                    stage,
                    source_id,
                    resource_id,
                    version_id,
                    status,
                    decision_or_reason,
                    1 if retryable else 0,
                    json.dumps(details, ensure_ascii=False),
                    ts,
                ),
            )
            conn.commit()
        return op_id


# ==============================================================================
# PHASE E — STRUCTURED KNOWLEDGE EXTRACTION & CONTENT-AWARE CHUNKING
# ==============================================================================


@dataclass(frozen=True)
class StructuredKnowledgeElement:
    """Grounded structured knowledge item extracted from a NormalizedDocument (never invented)."""

    element_id: str
    document_id: str
    resource_id: str
    version_id: str | None
    source_id: str
    element_type: str  # one of STRUCTURED_KNOWLEDGE_ELEMENT_TYPES
    element_key: str
    statement: str
    section_title: str
    supporting_excerpt: str
    epistemic_classification: str
    content_origin: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_structured_knowledge(
    doc: NormalizedDocument,
    *,
    conn: sqlite3.Connection | None = None,
    persist: bool = True,
) -> list[StructuredKnowledgeElement]:
    """Extract grounded structured knowledge elements from `doc` without inventing missing elements."""
    elements: list[StructuredKnowledgeElement] = []

    def _add(
        etype: str,
        key: str,
        stmt: str,
        sec_title: str,
        excerpt: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if etype not in STRUCTURED_KNOWLEDGE_ELEMENT_TYPES:
            return
        clean_stmt = " ".join(stmt.split()).strip()
        if not clean_stmt:
            return
        eid_seed = f"{doc.document_id}:{etype}:{key}:{clean_stmt[:80]}"
        eid = f"ke:{_sha256_text(eid_seed)[:16]}"
        if any(e.element_id == eid for e in elements):
            return
        elements.append(
            StructuredKnowledgeElement(
                element_id=eid,
                document_id=doc.document_id,
                resource_id=doc.resource_id,
                version_id=doc.version_id,
                source_id=doc.source_id,
                element_type=etype,
                element_key=key,
                statement=clean_stmt,
                section_title=sec_title,
                supporting_excerpt=" ".join(excerpt.split())[:280],
                epistemic_classification=doc.epistemic_classification,
                content_origin=doc.content_origin,
                metadata=meta or {},
            )
        )

    for sec in doc.sections:
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+|\n+", sec.text)
            if s.strip()
        ]
        # 1. Equations: explicit mathematical formulas (`$$...$$`, `\begin{equation}`, or explicit symbolic equalities)
        for eq_match in re.finditer(
            r"(\$\$[^$]+\$\$|\\begin\{equation\}.*?\\end\{equation\}|(?:[A-Za-z∇∂θwLσ∑∏_0-9()+/\-]+\s*=\s*[^.\n,;]{3,80}))",
            sec.text,
            flags=re.S,
        ):
            eq_str = eq_match.group(1).strip()
            # Filter out metadata key=value or URL parameters
            if "http" in eq_str.lower() or "pageid=" in eq_str.lower() or "oldid=" in eq_str.lower():
                continue
            if any(sym in eq_str for sym in ("$$", "\\", "∂", "∇", "∑", "∏", "exp(", "log(", "softmax", "w_", "θ", "L(", "E(")):
                _add("equations", sec.section_title, eq_str, sec.section_title, eq_str)

        for sent in sentences:
            low = sent.lower()
            # 2. Definitions
            if re.search(r"\b(is defined as|refers to|is a method|is a type of|is an algorithm|denotes)\b", low):
                _add("definitions", doc.title, sent, sec.section_title, sent)

            # 3. Hypotheses
            if re.search(r"\b(we hypothesize|hypothesis|conjecture that|postulate that)\b", low):
                _add("hypotheses", doc.title, sent, sec.section_title, sent)

            # 4. Claims
            if re.search(r"\b(we show that|we demonstrate that|we argue that|we prove that|this establishes that)\b", low):
                _add("claims", doc.title, sent, sec.section_title, sent)

            # 5. Methods
            if sec.section_type == "method" or re.search(
                r"\b(we propose|our method|our algorithm|architecture consists of|trained using|gradient descent|backpropagation computes)\b",
                low,
            ):
                _add("methods", sec.section_title, sent, sec.section_title, sent)

            # 6. Experiments
            if sec.section_type == "experiments" or re.search(
                r"\b(we evaluate on|benchmark|dataset|experimental setup|trained on|ablation study)\b",
                low,
            ):
                _add("experiments", sec.section_title, sent, sec.section_title, sent)

            # 7. Results
            if sec.section_type == "results" or re.search(
                r"\b(outperforms|achieves state-of-the-art|test error|perplexity|accuracy of|empirically reduces)\b",
                low,
            ):
                _add("results", sec.section_title, sent, sec.section_title, sent)

            # 8. Limitations
            if sec.section_type == "limitations" or re.search(
                r"\b(limitation|drawback|fails when|computational cost|bottleneck|vanishing gradient|intractable)\b",
                low,
            ):
                _add("limitations", sec.section_title, sent, sec.section_title, sent)

            # 9. Concept relationships
            rel_match = re.search(
                r"\b([A-Z][A-Za-z0-9\-\s]{2,35})\s+(depends? on|generalizes?|extends?|requires?|is based on|are based on|builds? upon)\s+([A-Za-z0-9\-\s]{3,40})",
                sent,
            )
            if rel_match:
                src_c = rel_match.group(1).strip()
                rel_verb = rel_match.group(2).strip()
                dst_c = rel_match.group(3).strip()
                _add(
                    "concept_relationships",
                    f"{src_c} -> {dst_c}",
                    sent,
                    sec.section_title,
                    sent,
                    {"from_concept": src_c, "relation": rel_verb, "to_concept": dst_c},
                )

            # 10. Citations (`arXiv:XXXX.XXXXX`, `[1]`, `(Author et al., YYYY)`)
            for cit_match in re.finditer(
                r"(arXiv:\d{4}\.\d{4,5}(?:v\d+)?|\([A-Z][A-Za-z\s&]+et al\.,?\s*\d{4}\)|\[\d+(?:,\s*\d+)*\])",
                sent,
            ):
                cit = cit_match.group(1).strip()
                _add("citations", cit, sent, sec.section_title, sent, {"citation_marker": cit})

    # 11. Core concepts grounded in document title / explicit section headings
    if doc.title:
        _add(
            "concepts",
            doc.title,
            f"Primary resource topic: {doc.title} ({doc.resource_type})",
            doc.sections[0].section_title if doc.sections else doc.title,
            doc.normalized_text[:220],
        )

    if conn is not None and persist:
        ensure_ingestion_schema(conn)
        now = _utc_now_iso()
        conn.execute("DELETE FROM knowledge_elements WHERE document_id = ?", (doc.document_id,))
        for elem in elements:
            conn.execute(
                """
                INSERT INTO knowledge_elements (
                    element_id, document_id, resource_id, version_id, source_id,
                    element_type, element_key, statement, section_title,
                    supporting_excerpt, epistemic_classification, content_origin,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    elem.element_id,
                    elem.document_id,
                    elem.resource_id,
                    elem.version_id,
                    elem.source_id,
                    elem.element_type,
                    elem.element_key,
                    elem.statement,
                    elem.section_title,
                    elem.supporting_excerpt,
                    elem.epistemic_classification,
                    elem.content_origin,
                    json.dumps(elem.metadata, ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()

    return elements


@dataclass(frozen=True)
class ChunkDraft:
    """Generated chunk candidate with strategy, section, and structured-element metadata."""

    chunk_id: str
    resource_id: str
    version_id: str | None
    source_id: str
    chunk_index: int
    chunk_text: str
    token_estimate: int
    char_start: int
    char_end: int
    content_hash: str
    content_origin: str
    epistemic_layer: str  # ChunkEpistemicLayer value
    epistemic_classification: str  # EpistemicCategory value
    chunk_strategy: str  # ChunkingStrategy value
    section_title: str
    canonical_url: str
    title: str
    authors: str | None
    published_at: str | None
    license_status: str
    permitted_storage_scope: str
    attribution_requirement: str
    structured_elements: tuple[dict[str, Any], ...] = ()

    @property
    def epistemic_type(self) -> str:
        return self.epistemic_layer

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["epistemic_type"] = self.epistemic_layer
        payload["structured_elements"] = list(self.structured_elements)
        return payload


class ContentAwareChunker:
    """Configurable content-type-aware chunking engine (Phase E)."""

    def __init__(
        self,
        *,
        max_chars: int = 850,
        overlap_chars: int = 80,
    ) -> None:
        self.max_chars = max(200, int(max_chars))
        self.overlap_chars = max(0, min(int(overlap_chars), self.max_chars // 3))

    def select_strategy(
        self,
        doc: NormalizedDocument,
        *,
        strategy_override: ChunkingStrategy | str | None = None,
    ) -> ChunkingStrategy:
        if strategy_override is not None:
            if isinstance(strategy_override, ChunkingStrategy):
                return strategy_override
            return ChunkingStrategy(str(strategy_override))

        if doc.content_origin == NormalizedContentOrigin.AUTHORIZED_TRANSCRIPT.value or (
            doc.resource_type == CanonicalResourceType.AUTHORIZED_TRANSCRIPTS.value
        ):
            return ChunkingStrategy.TRANSCRIPT_TOPIC_TIME_BOUNDARY

        # Check if mathematical equations are prominent
        if any("$$" in s.text or "\\begin{equation}" in s.text or s.section_type == "equations" for s in doc.sections):
            return ChunkingStrategy.EQUATION_PRESERVING_MATHEMATICAL

        if doc.resource_type in {
            CanonicalResourceType.RESEARCH_PAPERS.value,
            CanonicalResourceType.RESEARCH_ABSTRACTS.value,
        }:
            return ChunkingStrategy.SECTION_AWARE_RESEARCH_PAPER

        if doc.resource_type in {
            CanonicalResourceType.EDUCATIONAL_LESSONS.value,
            CanonicalResourceType.LECTURES.value,
            CanonicalResourceType.COURSES_AND_TUTORIALS.value,
            CanonicalResourceType.SLIDES_AND_LECTURE_NOTES.value,
        }:
            return ChunkingStrategy.CONCEPT_AWARE_EDUCATIONAL

        return ChunkingStrategy.HEADING_PARAGRAPH_ARTICLE

    def chunk_document(
        self,
        doc: NormalizedDocument,
        *,
        elements: Sequence[StructuredKnowledgeElement] | None = None,
        strategy_override: ChunkingStrategy | str | None = None,
    ) -> list[ChunkDraft]:
        strategy = self.select_strategy(doc, strategy_override=strategy_override)
        attribution = str(
            doc.metadata.get("attribution_requirement")
            or attribution_for_license(
                doc.license_status,
                source_id=doc.source_id,
                canonical_url=doc.canonical_url,
                authors=doc.authors,
            )
        )

        raw_segments: list[tuple[str, str]] = []
        if strategy == ChunkingStrategy.EQUATION_PRESERVING_MATHEMATICAL:
            raw_segments = self._split_equation_preserving(doc)
        elif strategy == ChunkingStrategy.TRANSCRIPT_TOPIC_TIME_BOUNDARY:
            raw_segments = self._split_transcript_segments(doc)
        elif strategy == ChunkingStrategy.SECTION_AWARE_RESEARCH_PAPER:
            raw_segments = self._split_section_aware(doc, prefix_section_header=True)
        elif strategy == ChunkingStrategy.CONCEPT_AWARE_EDUCATIONAL:
            raw_segments = self._split_concept_aware(doc)
        else:
            raw_segments = self._split_section_aware(doc, prefix_section_header=False)

        drafts: list[ChunkDraft] = []
        running_char = 0
        for idx, (sec_title, seg_text) in enumerate(raw_segments):
            cleaned = seg_text.strip()
            if not cleaned:
                continue
            char_start = running_char
            char_end = char_start + len(cleaned)
            running_char = char_end + 2

            c_hash = _sha256_text(cleaned)
            ver_tag = str(doc.version_id) if doc.version_id is not None else "v0"
            chunk_id = compute_chunk_id(doc.resource_id, ver_tag, doc.content_origin, idx)
            token_est = max(1, len(cleaned.split()))

            matching_elements: list[dict[str, Any]] = []
            if elements:
                for el in elements:
                    if el.section_title == sec_title or (
                        el.supporting_excerpt and el.supporting_excerpt[:40] in cleaned
                    ):
                        matching_elements.append(
                            {
                                "element_type": el.element_type,
                                "element_key": el.element_key,
                                "statement": el.statement,
                            }
                        )

            drafts.append(
                ChunkDraft(
                    chunk_id=chunk_id,
                    resource_id=doc.resource_id,
                    version_id=doc.version_id,
                    source_id=doc.source_id,
                    chunk_index=idx,
                    chunk_text=cleaned,
                    token_estimate=token_est,
                    char_start=char_start,
                    char_end=char_end,
                    content_hash=c_hash,
                    content_origin=doc.content_origin,
                    epistemic_layer=doc.chunk_epistemic_layer,
                    epistemic_classification=doc.epistemic_classification,
                    chunk_strategy=strategy.value,
                    section_title=sec_title,
                    canonical_url=doc.canonical_url,
                    title=doc.title,
                    authors=doc.authors,
                    published_at=doc.published_at,
                    license_status=doc.license_status,
                    permitted_storage_scope=doc.permitted_storage_scope,
                    attribution_requirement=attribution,
                    structured_elements=tuple(matching_elements),
                )
            )
        return drafts

    def _split_section_aware(
        self,
        doc: NormalizedDocument,
        *,
        prefix_section_header: bool,
    ) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        for sec in doc.sections:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", sec.text) if p.strip()]
            current_buf: list[str] = []
            current_len = 0
            for para in paragraphs:
                header_prefix = f"[{sec.section_title}] " if (prefix_section_header and not current_buf) else ""
                candidate_len = current_len + len(header_prefix) + len(para) + 2
                if current_buf and candidate_len > self.max_chars:
                    segments.append((sec.section_title, "\n\n".join(current_buf)))
                    current_buf = []
                    current_len = 0
                    header_prefix = f"[{sec.section_title}] " if prefix_section_header else ""
                if len(header_prefix) + len(para) > self.max_chars:
                    for sub in self._window_sentences(para):
                        pref = f"[{sec.section_title}] " if prefix_section_header else ""
                        segments.append((sec.section_title, f"{pref}{sub}".strip()))
                else:
                    current_buf.append(f"{header_prefix}{para}".strip())
                    current_len += len(header_prefix) + len(para) + 2
            if current_buf:
                segments.append((sec.section_title, "\n\n".join(current_buf)))
        return segments

    def _split_concept_aware(self, doc: NormalizedDocument) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        for sec in doc.sections:
            blocks = [b.strip() for b in re.split(r"\n\s*\n|(?=^(?:Concept|Intuition|Example|Definition)\s*:)", sec.text, flags=re.M) if b.strip()]
            buf: list[str] = []
            for blk in blocks:
                candidate = "\n\n".join(buf + [blk])
                if buf and len(candidate) > self.max_chars:
                    segments.append((sec.section_title, f"[{sec.section_title}]\n" + "\n\n".join(buf)))
                    buf = [blk]
                else:
                    buf.append(blk)
            if buf:
                segments.append((sec.section_title, f"[{sec.section_title}]\n" + "\n\n".join(buf)))
        return segments

    def _split_transcript_segments(self, doc: NormalizedDocument) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        for sec in doc.sections:
            time_prefix = (
                f"[{int(sec.start_time_seconds)}s-{int(sec.end_time_seconds)}s] "
                if sec.start_time_seconds is not None and sec.end_time_seconds is not None
                else (f"[{int(sec.start_time_seconds)}s+] " if sec.start_time_seconds is not None else "")
            )
            full_sec = f"{time_prefix}[{sec.section_title}] {sec.text}".strip()
            if len(full_sec) <= self.max_chars:
                segments.append((sec.section_title, full_sec))
            else:
                for sub in self._window_sentences(sec.text):
                    segments.append((sec.section_title, f"{time_prefix}[{sec.section_title}] {sub}".strip()))
        return segments

    def _split_equation_preserving(self, doc: NormalizedDocument) -> list[tuple[str, str]]:
        """Keep `$$...$$` and `\\begin{equation}...\\end{equation}` blocks intact with their explanatory context."""
        segments: list[tuple[str, str]] = []
        eq_pattern = re.compile(r"(\$\$[^$]+\$\$|\\begin\{equation\}.*?\\end\{equation\})", flags=re.S)
        for sec in doc.sections:
            tokens = eq_pattern.split(sec.text)
            buf: list[str] = []
            for tok in tokens:
                piece = tok.strip()
                if not piece:
                    continue
                is_eq = bool(eq_pattern.fullmatch(piece))
                candidate = "\n\n".join(buf + [piece])
                if buf and len(candidate) > self.max_chars and not is_eq:
                    segments.append((sec.section_title, f"[{sec.section_title}]\n" + "\n\n".join(buf)))
                    buf = [piece]
                else:
                    buf.append(piece)
            if buf:
                segments.append((sec.section_title, f"[{sec.section_title}]\n" + "\n\n".join(buf)))
        return segments

    def _window_sentences(self, text: str) -> list[str]:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return [text[: self.max_chars]]
        windows: list[str] = []
        current: list[str] = []
        for sent in sentences:
            cand = " ".join(current + [sent])
            if current and len(cand) > self.max_chars:
                windows.append(" ".join(current))
                current = [sent]
            else:
                current.append(sent)
        if current:
            windows.append(" ".join(current))
        return windows


# ==============================================================================
# PHASE F — EXPLICIT 6-ANOMALY SQLITE / CHROMA CONSISTENCY VERIFICATION
# ==============================================================================


@dataclass(frozen=True)
class IndexConsistencyReport:
    """Explicit 6-check verification report between SQLite `knowledge_chunks` and Chroma."""

    is_consistent: bool
    sqlite_active_chunks: int
    chroma_vector_count: int
    collection_name: str
    embedding_providers: list[str]
    embedding_models: list[str]
    embedding_dimensions: list[int]
    is_semantic_embedding: bool
    # 6 required anomaly lists:
    missing_vectors: list[str]
    orphaned_vectors: list[str]
    duplicate_vector_ids: list[str]
    dimension_mismatches: list[dict[str, Any]]
    stale_content_versions: list[dict[str, Any]]
    provider_model_inconsistencies: list[dict[str, Any]]
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_index_consistency(
    *,
    sqlite_db_path: Path | str = DEFAULT_SQLITE_DB_PATH,
    chroma_dir: Path | str = DEFAULT_CHROMA_DIR,
    collection_name: str = CHROMA_COLLECTION_NAME,
) -> IndexConsistencyReport:
    """Perform explicit 6-anomaly consistency verification between SQLite and Chroma (Phase F)."""
    db_path = Path(sqlite_db_path)
    c_dir = Path(chroma_dir)

    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        has_chunks = _table_exists(conn, "knowledge_chunks")
        if not has_chunks:
            return IndexConsistencyReport(
                is_consistent=True,
                sqlite_active_chunks=0,
                chroma_vector_count=0,
                collection_name=collection_name,
                embedding_providers=[],
                embedding_models=[],
                embedding_dimensions=[],
                is_semantic_embedding=False,
                missing_vectors=[],
                orphaned_vectors=[],
                duplicate_vector_ids=[],
                dimension_mismatches=[],
                stale_content_versions=[],
                provider_model_inconsistencies=[],
                checked_at=_utc_now_iso(),
            )

        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM knowledge_chunks
                WHERE lifecycle_state = 'ACTIVE'
                ORDER BY resource_id ASC, chunk_index ASC
                """
            ).fetchall()
        ]
        res_ver_map: dict[str, str] = {}
        if _table_exists(conn, "resources"):
            for rr in conn.execute("SELECT resource_id, current_version_id FROM resources").fetchall():
                if rr["resource_id"] and rr["current_version_id"]:
                    res_ver_map[str(rr["resource_id"])] = str(rr["current_version_id"])
    finally:
        conn.close()

    # Check 3: Duplicate vector IDs in SQLite active chunks
    seen_vec_ids: dict[str, int] = {}
    duplicate_vector_ids: list[str] = []
    for r in rows:
        vid = str(r.get("vector_id") or r["chunk_id"])
        seen_vec_ids[vid] = seen_vec_ids.get(vid, 0) + 1
    for vid, cnt in seen_vec_ids.items():
        if cnt > 1:
            duplicate_vector_ids.append(vid)

    # Check 5a: Stale content versions within SQLite (active chunks across multiple version_ids or mismatched with resources.current_version_id)
    stale_content_versions: list[dict[str, Any]] = []
    versions_by_resource: dict[str, set[str]] = {}
    for r in rows:
        rid = str(r["resource_id"])
        vid = str(r["version_id"] or "")
        versions_by_resource.setdefault(rid, set()).add(vid)
    for rid, vset in versions_by_resource.items():
        current_vid = res_ver_map.get(rid)
        if len(vset) > 1 or (current_vid and any(v and v != current_vid for v in vset)):
            for r in rows:
                if str(r["resource_id"]) == rid:
                    c_vid = str(r.get("version_id") or "")
                    if (current_vid and c_vid and c_vid != current_vid) or (not current_vid and len(vset) > 1 and c_vid == sorted(vset)[0]):
                        stale_content_versions.append(
                            {
                                "chunk_id": r["chunk_id"],
                                "resource_id": rid,
                                "chunk_version_id": c_vid,
                                "latest_active_version_id": current_vid or sorted(vset)[-1],
                                "reason": "Active chunk references an older resource_version while a newer version is active.",
                            }
                        )

    # Inspect Chroma collection
    chroma_index = ChromaVectorIndex(
        persist_directory=c_dir,
        collection_name=collection_name,
        create_if_missing=False,
    )
    collection = chroma_index._collection
    chroma_total = int(collection.count()) if collection is not None else 0
    chroma_payload = (
        collection.get(include=["embeddings", "metadatas"])
        if collection is not None and chroma_total > 0
        else {"ids": [], "embeddings": [], "metadatas": []}
    )
    chroma_ids: list[str] = list(chroma_payload.get("ids") or [])
    raw_embeddings = chroma_payload.get("embeddings")
    chroma_embeddings: list[Any] = list(raw_embeddings) if raw_embeddings is not None else []
    raw_metadatas = chroma_payload.get("metadatas")
    chroma_metadatas: list[dict[str, Any]] = list(raw_metadatas) if raw_metadatas is not None else []

    chroma_by_id: dict[str, tuple[Any, dict[str, Any]]] = {}
    for idx, cid in enumerate(chroma_ids):
        if cid in chroma_by_id and cid not in duplicate_vector_ids:
            duplicate_vector_ids.append(cid)
        emb = chroma_embeddings[idx] if idx < len(chroma_embeddings) else None
        meta = chroma_metadatas[idx] if idx < len(chroma_metadatas) and chroma_metadatas[idx] else {}
        chroma_by_id[cid] = (emb, meta)

    sqlite_by_vec_id = {str(r.get("vector_id") or r["chunk_id"]): r for r in rows}

    # Check 1 & 2: Missing and Orphaned vectors
    missing_vectors = sorted(vid for vid in sqlite_by_vec_id if vid not in chroma_by_id)
    orphaned_vectors = sorted(cid for cid in chroma_by_id if cid not in sqlite_by_vec_id)

    # Check 4 & 6 & 5b: Dimension mismatches, Provider/Model inconsistencies, and Stale hashes in Chroma
    dimension_mismatches: list[dict[str, Any]] = []
    provider_model_inconsistencies: list[dict[str, Any]] = []

    providers = sorted({str(r.get("embedding_provider") or "") for r in rows if r.get("embedding_provider")})
    models = sorted({str(r.get("embedding_model") or "") for r in rows if r.get("embedding_model")})
    dims = sorted({int(r.get("embedding_dimensions") or 0) for r in rows if r.get("embedding_dimensions")})

    if len(providers) > 1 or len(models) > 1:
        provider_model_inconsistencies.append(
            {
                "scope": "collection_wide",
                "providers": providers,
                "models": models,
                "reason": "Multiple embedding providers or models mixed within active SQLite chunks.",
            }
        )
    if len(dims) > 1:
        dimension_mismatches.append(
            {
                "scope": "collection_wide",
                "dimensions": dims,
                "reason": "Multiple embedding dimensions mixed within active SQLite chunks.",
            }
        )

    expected_dim = dims[0] if len(dims) == 1 else None
    for vid, r in sqlite_by_vec_id.items():
        sql_dim = int(r.get("embedding_dimensions") or 0)
        sql_prov = str(r.get("embedding_provider") or "")
        sql_model = str(r.get("embedding_model") or "")
        sql_hash = str(r.get("content_hash") or "")
        if vid in chroma_by_id:
            emb, meta = chroma_by_id[vid]
            vec_len = len(emb) if emb is not None else 0
            if vec_len != sql_dim or (expected_dim is not None and vec_len != expected_dim):
                dimension_mismatches.append(
                    {
                        "vector_id": vid,
                        "sqlite_dimensions": sql_dim,
                        "chroma_vector_length": vec_len,
                        "expected_collection_dimension": expected_dim,
                    }
                )
            c_prov = str(meta.get("embedding_provider") or "")
            c_model = str(meta.get("embedding_model") or "")
            if (c_prov and c_prov != sql_prov) or (c_model and c_model != sql_model):
                provider_model_inconsistencies.append(
                    {
                        "vector_id": vid,
                        "sqlite_provider": sql_prov,
                        "chroma_provider": c_prov,
                        "sqlite_model": sql_model,
                        "chroma_model": c_model,
                    }
                )
            c_hash = str(meta.get("content_hash") or "")
            if c_hash and sql_hash and c_hash != sql_hash:
                stale_content_versions.append(
                    {
                        "vector_id": vid,
                        "resource_id": r["resource_id"],
                        "sqlite_content_hash": sql_hash,
                        "chroma_content_hash": c_hash,
                        "reason": "Chroma vector content_hash does not match active SQLite chunk content_hash.",
                    }
                )

    is_consistent = not (
        missing_vectors
        or orphaned_vectors
        or duplicate_vector_ids
        or dimension_mismatches
        or stale_content_versions
        or provider_model_inconsistencies
    )
    is_semantic = any(
        bool(r.get("is_semantic_embedding"))
        or (str(r.get("embedding_provider") or "") not in {"", "local_deterministic"})
        for r in rows
    )

    return IndexConsistencyReport(
        is_consistent=is_consistent,
        sqlite_active_chunks=len(rows),
        chroma_vector_count=chroma_total,
        collection_name=collection_name,
        embedding_providers=providers,
        embedding_models=models,
        embedding_dimensions=dims,
        is_semantic_embedding=is_semantic,
        missing_vectors=missing_vectors,
        orphaned_vectors=orphaned_vectors,
        duplicate_vector_ids=duplicate_vector_ids,
        dimension_mismatches=dimension_mismatches,
        stale_content_versions=stale_content_versions,
        provider_model_inconsistencies=provider_model_inconsistencies,
        checked_at=_utc_now_iso(),
    )


# ==============================================================================
# PHASE C, H, I & K — SOURCE-TO-KNOWLEDGE INGESTION PIPELINE & CLI WORKFLOW
# ==============================================================================


class SourceToKnowledgeIngestionPipeline:
    """Governed coordinator for the Source-to-Knowledge Ingestion Pipeline."""

    def __init__(
        self,
        *,
        sqlite_db_path: Path | str = DEFAULT_SQLITE_DB_PATH,
        chroma_dir: Path | str = DEFAULT_CHROMA_DIR,
        collection_name: str = CHROMA_COLLECTION_NAME,
        embedding_provider: EmbeddingProvider | None = None,
        max_chars_per_chunk: int = 850,
    ) -> None:
        self.sqlite_db_path = Path(sqlite_db_path)
        self.chroma_dir = Path(chroma_dir)
        self.collection_name = collection_name
        self.embedding_provider: EmbeddingProvider = (
            embedding_provider or DeterministicHashEmbeddingProvider()
        )
        self.extractor = ContentExtractor()
        self.chunker = ContentAwareChunker(max_chars=max_chars_per_chunk)

    def _connect_ro(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.sqlite_db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.sqlite_db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_ingestion_schema(conn)
        return conn

    # ------------------------------------------------------------------
    # 1. Ingestion Status & Control Plane Metrics (Phase H & I)
    # ------------------------------------------------------------------

    def get_ingestion_status(self) -> dict[str, Any]:
        """Return real persisted counts across all 11 lifecycle metrics and 5 epistemic chunk layers."""
        conn = self._connect_ro()
        try:
            registered_sources = int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
            discovered_candidates = (
                int(conn.execute("SELECT COUNT(*) FROM discovered_link_candidates").fetchone()[0])
                if _table_exists(conn, "discovered_link_candidates")
                else 0
            )
            reviewed_candidates = (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM discovered_link_candidates WHERE review_status != 'PENDING_REVIEW'"
                    ).fetchone()[0]
                )
                if _table_exists(conn, "discovered_link_candidates")
                else 0
            )
            approved_resources = int(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0])
            collected_resources = int(
                conn.execute("SELECT COUNT(*) FROM resources WHERE last_verified_at IS NOT NULL").fetchone()[0]
            )
            uncollected_resources = approved_resources - collected_resources
            verified_documents = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM resources
                    WHERE last_verified_at IS NOT NULL
                      AND LOWER(COALESCE(verification_status, '')) LIKE 'verified%'
                    """
                ).fetchone()[0]
            )

            has_chunks = _table_exists(conn, "knowledge_chunks")
            has_norm_docs = _table_exists(conn, "normalized_documents")
            has_ops = _table_exists(conn, "ingestion_operations")

            if has_norm_docs:
                extracted_documents = int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT resource_id) FROM (
                            SELECT resource_id FROM normalized_documents WHERE lifecycle_state = 'ACTIVE'
                            UNION
                            SELECT resource_id FROM knowledge_chunks WHERE lifecycle_state = 'ACTIVE'
                        )
                        """
                    ).fetchone()[0]
                )
            elif has_chunks:
                extracted_documents = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT resource_id) FROM knowledge_chunks WHERE lifecycle_state = 'ACTIVE'"
                    ).fetchone()[0]
                )
            else:
                extracted_documents = 0

            authorized_transcripts = (
                int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM transcripts
                        WHERE permits_transcript_storage = 1
                          AND ingestion_state IN ('READY_FOR_INGESTION', 'INGESTED')
                        """
                    ).fetchone()[0]
                )
                if _table_exists(conn, "transcripts")
                else 0
            )

            active_knowledge_chunks = (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM knowledge_chunks WHERE lifecycle_state = 'ACTIVE'"
                    ).fetchone()[0]
                )
                if has_chunks
                else 0
            )

            # Failed or deferred resources (persisted resource flags + failed/deferred ingestion_operations)
            deferred_res_ids = {
                str(r[0])
                for r in conn.execute(
                    """
                    SELECT resource_id FROM resources
                    WHERE LOWER(COALESCE(collection_decision, '')) LIKE '%defer%'
                       OR (last_verified_at IS NOT NULL AND LOWER(COALESCE(verification_status, '')) NOT LIKE 'verified%')
                    """
                ).fetchall()
                if r[0] is not None
            }
            if has_ops:
                for r in conn.execute(
                    """
                    SELECT DISTINCT resource_id FROM ingestion_operations
                    WHERE resource_id IS NOT NULL AND status IN ('FAILED', 'DEFERRED', 'REJECTED_BY_POLICY')
                    """
                ).fetchall():
                    if r[0] is not None:
                        deferred_res_ids.add(str(r[0]))
            failed_or_deferred_resources = len(deferred_res_ids)

            # Separate chunk counts for the 5 epistemic layers
            layer_counts = {layer.value: 0 for layer in ChunkEpistemicLayer}
            by_source_chunks: dict[str, int] = {}
            if has_chunks:
                has_ep_class_col = _column_exists(conn, "knowledge_chunks", "epistemic_classification")
                select_cols = (
                    "kc.source_id, kc.epistemic_layer, kc.content_origin, kc.epistemic_classification, r.resource_type, r.title, r.canonical_url"
                    if has_ep_class_col
                    else "kc.source_id, kc.epistemic_layer, kc.content_origin, NULL AS epistemic_classification, r.resource_type, r.title, r.canonical_url"
                )
                for crow in conn.execute(
                    f"""
                    SELECT {select_cols}
                    FROM knowledge_chunks kc
                    LEFT JOIN resources r ON r.resource_id = kc.resource_id
                    WHERE kc.lifecycle_state = 'ACTIVE'
                    """
                ).fetchall():
                    src_id = str(crow["source_id"])
                    by_source_chunks[src_id] = by_source_chunks.get(src_id, 0) + 1
                    raw_ep = str(crow["epistemic_layer"] or "")
                    if raw_ep in layer_counts:
                        layer_counts[raw_ep] += 1
                    else:
                        cat = (
                            crow["epistemic_classification"]
                            or classify_epistemic_category(
                                crow["resource_type"] or "web_page",
                                source_id=src_id,
                                title=crow["title"],
                                canonical_url=crow["canonical_url"],
                            ).value
                        )
                        mapped = determine_chunk_epistemic_layer(
                            cat,
                            str(crow["content_origin"] or "abstract_or_description"),
                            source_id=src_id,
                            existing_epistemic_type=raw_ep,
                        )
                        layer_counts[mapped.value] += 1

            wiki_audit = audit_and_reconcile_wikipedia_scope(conn, apply_fix=False)
        finally:
            conn.close()

        consistency = verify_index_consistency(
            sqlite_db_path=self.sqlite_db_path,
            chroma_dir=self.chroma_dir,
            collection_name=self.collection_name,
        )

        return {
            "database_path": str(self.sqlite_db_path),
            "chroma_dir": str(self.chroma_dir),
            "collection_name": self.collection_name,
            "lifecycle_metrics": {
                "registered_sources": registered_sources,
                "discovered_candidates": discovered_candidates,
                "reviewed_candidates": reviewed_candidates,
                "approved_resources": approved_resources,
                "uncollected_resources": uncollected_resources,
                "collected_resources": collected_resources,
                "verified_documents": verified_documents,
                "extracted_documents": extracted_documents,
                "authorized_transcripts": authorized_transcripts,
                "active_knowledge_chunks": active_knowledge_chunks,
                "indexed_vectors": consistency.chroma_vector_count,
                "failed_or_deferred_resources": failed_or_deferred_resources,
            },
            "epistemic_chunk_breakdown": layer_counts,
            "chunks_by_source": by_source_chunks,
            "wikipedia_scope_audit": wiki_audit,
            "index_consistency": consistency.to_dict(),
        }

    # ------------------------------------------------------------------
    # 2. List Eligible & Preview Resource (Phase I)
    # ------------------------------------------------------------------

    def list_eligible_resources(
        self,
        *,
        source_id: str | None = None,
        include_uncollected: bool = True,
        include_collected_unchunked: bool = True,
    ) -> list[dict[str, Any]]:
        conn = self._connect_ro()
        try:
            has_chunks = _table_exists(conn, "knowledge_chunks")
            rows = conn.execute(
                """
                SELECT r.*, s.status AS source_curation_status
                FROM resources r
                JOIN sources s ON s.source_id = r.source_id
                ORDER BY r.priority ASC, r.resource_id ASC
                """
            ).fetchall()

            chunk_counts: dict[str, int] = {}
            if has_chunks:
                for cr in conn.execute(
                    "SELECT resource_id, COUNT(*) AS cnt FROM knowledge_chunks WHERE lifecycle_state = 'ACTIVE' GROUP BY resource_id"
                ).fetchall():
                    chunk_counts[str(cr["resource_id"])] = int(cr["cnt"])

            eligible: list[dict[str, Any]] = []
            for row in rows:
                r = dict(row)
                rid = str(r["resource_id"])
                src = str(r["source_id"])
                if source_id and src != source_id:
                    continue
                is_collected = r.get("last_verified_at") is not None
                active_chunks = chunk_counts.get(rid, 0)

                if not include_uncollected and not is_collected:
                    continue
                if not include_collected_unchunked and is_collected and active_chunks == 0:
                    continue

                policy = evaluate_resource_ingestion_policy(
                    conn,
                    resource_id=rid,
                    source_id=src,
                    canonical_url=str(r["canonical_url"]),
                    requested_action="COLLECT" if not is_collected else "EXTRACT_METADATA_OR_ABSTRACT",
                )
                eligible.append(
                    {
                        "resource_id": rid,
                        "source_id": src,
                        "manifest_id": r.get("manifest_id"),
                        "title": r["title"],
                        "canonical_url": r["canonical_url"],
                        "raw_resource_type": r["resource_type"],
                        "canonical_resource_type": policy.canonical_resource_type,
                        "epistemic_category": policy.epistemic_category,
                        "is_collected": is_collected,
                        "active_chunk_count": active_chunks,
                        "license_status": policy.license_status,
                        "permitted_storage_scope": policy.permitted_storage_scope,
                        "is_policy_permitted": policy.is_permitted,
                        "decision_code": policy.decision_code,
                    }
                )
            return eligible
        finally:
            conn.close()

    def preview_resource(self, resource_id: str | int) -> dict[str, Any]:
        """Preview a resource's metadata, classification, policy decision, and current chunks before collection/extraction."""
        conn = self._connect_ro()
        try:
            r = _resolve_resource_row(conn, resource_id)
            if r is None:
                raise ValueError(f"Resource ID {resource_id!r} not found.")
            resolved_rid = str(r["resource_id"])
            collect_policy = evaluate_resource_ingestion_policy(
                conn,
                resource_id=resolved_rid,
                requested_action="COLLECT",
            )
            full_text_policy = evaluate_resource_ingestion_policy(
                conn,
                resource_id=resolved_rid,
                requested_action="EXTRACT_FULL_TEXT",
            )
            transcript_policy = evaluate_resource_ingestion_policy(
                conn,
                resource_id=resolved_rid,
                requested_action="EXTRACT_TRANSCRIPT",
            )
            chunks: list[dict[str, Any]] = []
            if _table_exists(conn, "knowledge_chunks"):
                chunks = [
                    dict(c)
                    for c in conn.execute(
                        """
                        SELECT chunk_id, chunk_index, epistemic_layer, content_origin, char_start, char_end
                        FROM knowledge_chunks
                        WHERE resource_id = ? AND lifecycle_state = 'ACTIVE'
                        ORDER BY chunk_index ASC
                        """,
                        (resolved_rid,),
                    ).fetchall()
                ]
            return {
                "resource_id": resolved_rid,
                "source_id": r["source_id"],
                "manifest_id": r.get("manifest_id"),
                "title": r["title"],
                "authors": r.get("authors"),
                "canonical_url": r["canonical_url"],
                "raw_resource_type": r["resource_type"],
                "canonical_resource_type": collect_policy.canonical_resource_type,
                "epistemic_category": collect_policy.epistemic_category,
                "is_collected": r.get("last_verified_at") is not None,
                "last_verified_at": r.get("last_verified_at"),
                "license_status": r["license_status"],
                "permitted_storage_scope": r["permitted_storage_scope"],
                "permits_full_text_storage": bool(r["permits_full_text_storage"]),
                "permits_verbatim_excerpts": bool(r["permits_verbatim_excerpts"]),
                "policies": {
                    "collection": collect_policy.to_dict(),
                    "full_text_extraction": full_text_policy.to_dict(),
                    "transcript_extraction": transcript_policy.to_dict(),
                },
                "active_chunks": chunks,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 3. Authorized Collection (Phase C)
    # ------------------------------------------------------------------

    def collect_resource(
        self,
        *,
        resource_id: str | int,
        session: Any | None = None,
        rate_limiter: OriginRateLimiter | None = None,
        include_wikipedia_extract: bool = True,
        curation_approved_override: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute policy-validated collection for a curated resource."""
        conn = self._connect_ro() if dry_run else self._connect_rw()
        try:
            res = _resolve_resource_row(conn, resource_id)
            if res is None:
                raise ValueError(f"Resource ID {resource_id!r} not found.")
            resolved_rid = str(res["resource_id"])
            src_id = str(res["source_id"])
            url = str(res["canonical_url"])

            policy_eval = evaluate_resource_ingestion_policy(
                conn,
                resource_id=resolved_rid,
                source_id=src_id,
                canonical_url=url,
                requested_action="COLLECT",
                curation_approved_override=curation_approved_override,
            )
            if not policy_eval.is_permitted:
                if not dry_run:
                    self.extractor._record_operation(
                        conn,
                        stage="COLLECTION",
                        source_id=src_id,
                        resource_id=resolved_rid,
                        version_id=None,
                        status="REJECTED_BY_POLICY",
                        decision_or_reason=f"{policy_eval.decision_code}: {policy_eval.reason}",
                        retryable=False,
                        details=policy_eval.to_dict(),
                    )
                return {
                    "resource_id": resolved_rid,
                    "source_id": src_id,
                    "status": "REJECTED_BY_POLICY",
                    "policy": policy_eval.to_dict(),
                    "dry_run": dry_run,
                }

            if dry_run:
                return {
                    "resource_id": resolved_rid,
                    "source_id": src_id,
                    "canonical_url": url,
                    "status": "DRY_RUN_READY_TO_COLLECT",
                    "policy": policy_eval.to_dict(),
                    "dry_run": True,
                }

            limiter = rate_limiter or OriginRateLimiter()
            adapter = self._build_adapter_for_source(
                src_id,
                session=session,
                rate_limiter=limiter,
                curation_approved=curation_approved_override,
            )

            # Construct appropriate fetch URL (e.g., MediaWiki API URL or arXiv Atom API URL)
            fetch_url = url
            if src_id == "SRC-WIKIPEDIA" and isinstance(adapter, WikipediaSourceAdapter):
                title_slug = url.rsplit("/wiki/", 1)[-1] if "/wiki/" in url else str(res["title"])
                fetch_url = adapter.build_query_url(title_slug, include_extracts=include_wikipedia_extract)
            elif src_id == "SRC-ARXIV" and isinstance(adapter, ArxivSourceAdapter):
                arxiv_id = extract_arxiv_id(res.get("external_identifier") or url)
                if arxiv_id:
                    fetch_url = adapter.build_query_url([arxiv_id], max_results=1)

            candidate = SourceCandidate(
                source_id=src_id,
                url=fetch_url,
                title_hint=str(res["title"]),
                resource_type_hint=str(res["resource_type"]),
                manifest_id=res.get("manifest_id"),
                external_id=res.get("external_identifier"),
                priority=str(res.get("priority") or "P1"),
                observed_from=res.get("observed_from"),
            )

            try:
                audit_res = adapter.audit_policy()
                envelope = adapter.collect_candidate(candidate, policy=audit_res)
            except Exception as exc:
                is_retryable = not isinstance(exc, (PolicyBlockedError, ScopeViolationError))
                self.extractor._record_operation(
                    conn,
                    stage="COLLECTION",
                    source_id=src_id,
                    resource_id=resolved_rid,
                    version_id=None,
                    status="FAILED",
                    decision_or_reason=f"{type(exc).__name__}: {exc}",
                    retryable=is_retryable,
                    details={"fetch_url": fetch_url, "error_type": type(exc).__name__},
                )
                raise

            # Persist updated resource and new resource_version via KnowledgeStore
            conn.close()
            store = KnowledgeStore(self.sqlite_db_path)
            now = envelope.collected_at_utc or _utc_now_iso()
            upsert_res = store.upsert_resource(
                source_id=src_id,
                manifest_id=res.get("manifest_id"),
                canonical_url=url,
                fetched_url=envelope.fetched_url or fetch_url,
                title=envelope.title or str(res["title"]),
                authors=envelope.authors or res.get("authors"),
                published_at=envelope.published_at or res.get("published_at"),
                resource_type=envelope.resource_type or str(res["resource_type"]),
                media_modality=envelope.media_modality or str(res["media_modality"]),
                domain_scope=envelope.domain_scope or str(res["domain_scope"]),
                abstract_or_description=envelope.abstract_or_description or res.get("abstract_or_description"),
                topic_tags=envelope.topic_tags or res.get("topic_tags"),
                external_identifier=envelope.external_identifier or res.get("external_identifier"),
                observed_from=envelope.observed_from or res.get("observed_from"),
                information_to_inventory=envelope.information_to_inventory or res.get("information_to_inventory"),
                priority=str(res.get("priority") or "P1"),
                collection_decision=envelope.collection_decision,
                verification_status=envelope.verification_status,
                robots_sitemap_status=envelope.robots_sitemap_status,
                rights_status=envelope.rights_status,
                license_status=envelope.license_status,
                license_spdx=envelope.license_spdx,
                permitted_storage_scope=envelope.permitted_storage_scope,
                permits_full_text_storage=bool(envelope.permits_full_text_storage),
                permits_verbatim_excerpts=bool(envelope.permits_verbatim_excerpts),
                legal_uncertainty_status=envelope.legal_uncertainty_status,
                text_storage_status=envelope.text_storage_status,
                content_hash=(
                    _sha256_text(envelope.abstract_or_description)
                    if envelope.abstract_or_description
                    else res.get("content_hash")
                ),
                last_checked=now[:10],
                last_verified_at=now,
                discovered_at=str(res.get("first_discovered_at") or now),
                acquisition_method=f"adapter:{envelope.adapter_id}",
                actor="task037_ingestion_pipeline",
                change_reason=f"Authorized collection via {envelope.adapter_id}",
            )
            ver_id = str(upsert_res.get("current_version_id") or "")
            store.close()

            conn = self._connect_rw()
            self.extractor._record_operation(
                conn,
                stage="COLLECTION",
                source_id=src_id,
                resource_id=resolved_rid,
                version_id=ver_id or None,
                status="SUCCEEDED",
                decision_or_reason=f"Collected resource {resolved_rid} via {envelope.adapter_id} (version_id={ver_id})",
                retryable=False,
                details={
                    "fetched_url": envelope.fetched_url,
                    "http_status": envelope.http_status,
                    "permitted_storage_scope": envelope.permitted_storage_scope,
                    "text_storage_status": envelope.text_storage_status,
                },
            )
            return {
                "resource_id": resolved_rid,
                "version_id": ver_id,
                "source_id": src_id,
                "status": "COLLECTED",
                "envelope": envelope.to_dict(),
                "policy": policy_eval.to_dict(),
                "dry_run": False,
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _build_adapter_for_source(
        self,
        source_id: str,
        *,
        session: Any | None = None,
        rate_limiter: OriginRateLimiter | None = None,
        curation_approved: bool = False,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if session is not None:
            kwargs["session"] = session
        if rate_limiter is not None:
            kwargs["rate_limiter"] = rate_limiter
        if source_id == "SRC-WIKIPEDIA":
            return WikipediaSourceAdapter(**kwargs)
        if source_id == "SRC-ARXIV":
            return ArxivSourceAdapter(**kwargs)
        if source_id == "SRC-HINTON":
            return HintonSourceAdapter(**kwargs)
        if source_id == "SRC-BENGIO":
            return BengioSourceAdapter(**kwargs)
        if source_id == "SRC-3B1B":
            return ThreeBlueOneBrownAdapter(**kwargs)
        if source_id == "SRC-THE-BATCH":
            return TheBatchSourceAdapter(curation_approved=curation_approved, **kwargs)
        raise ValueError(f"No operational adapter configured for source_id={source_id!r}")

    # ------------------------------------------------------------------
    # 4. Extract, Chunk, Embed & Index (Phases D, E, F)
    # ------------------------------------------------------------------

    def ingest_resource_to_knowledge(
        self,
        *,
        resource_id: str | int,
        raw_content: str | bytes | None = None,
        extraction_format: ExtractionFormat | str | None = None,
        chunk_strategy: ChunkingStrategy | str | None = None,
        incremental: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run Extraction -> Normalization -> Structured Knowledge -> Chunking -> Embedding -> Indexing for a resource."""
        conn = self._connect_ro() if dry_run else self._connect_rw()
        try:
            doc = self.extractor.extract_and_normalize(
                conn,
                resource_id=resource_id,
                raw_content=raw_content,
                extraction_format=extraction_format,
                persist=not dry_run,
                dry_run=dry_run,
            )
            resolved_rid = doc.resource_id
            elements = extract_structured_knowledge(
                doc,
                conn=(None if dry_run else conn),
                persist=not dry_run,
            )
            chunk_drafts = self.chunker.chunk_document(
                doc,
                elements=elements,
                strategy_override=chunk_strategy,
            )

            if dry_run:
                return {
                    "status": "DRY_RUN_PREVIEW",
                    "document": doc.to_dict(),
                    "structured_elements": [e.to_dict() for e in elements],
                    "chunk_drafts": [c.to_dict() for c in chunk_drafts],
                    "embedding_provider": {
                        "provider": self.embedding_provider.provider_name,
                        "model": self.embedding_provider.model_name,
                        "dimensions": self.embedding_provider.dimensions,
                        "is_semantic": bool(getattr(self.embedding_provider, "is_semantic", False)),
                    },
                }

            # Check idempotency / incremental skip if active chunks already match exact chunk_ids & hashes
            existing_rows = conn.execute(
                """
                SELECT chunk_id, vector_id, content_hash, version_id,
                       embedding_provider, embedding_model, embedding_dimensions
                FROM knowledge_chunks
                WHERE resource_id = ? AND lifecycle_state = 'ACTIVE'
                ORDER BY chunk_index ASC
                """,
                (resolved_rid,),
            ).fetchall()
            existing_hashes = [str(r["content_hash"]) for r in existing_rows]
            new_hashes = [c.content_hash for c in chunk_drafts]
            same_provider = all(
                str(r["embedding_provider"]) == self.embedding_provider.provider_name
                and str(r["embedding_model"]) == self.embedding_provider.model_name
                and int(r["embedding_dimensions"]) == int(self.embedding_provider.dimensions)
                for r in existing_rows
            )

            chroma_index = ChromaVectorIndex(
                persist_directory=self.chroma_dir,
                collection_name=self.collection_name,
                create_if_missing=True,
            )
            existing_vec_ids = [str(r["vector_id"] or r["chunk_id"]) for r in existing_rows]
            existing_in_chroma = set(chroma_index.get_chunk_ids())

            if (
                incremental
                and existing_rows
                and existing_hashes == new_hashes
                and same_provider
                and all(vid in existing_in_chroma for vid in existing_vec_ids)
            ):
                # Still ensure new metadata columns on existing chunks are populated
                for draft in chunk_drafts:
                    conn.execute(
                        """
                        UPDATE knowledge_chunks
                        SET epistemic_classification = ?,
                            chunk_strategy = ?,
                            section_title = ?,
                            structured_elements_json = ?,
                            is_semantic_embedding = ?
                        WHERE resource_id = ? AND chunk_index = ? AND lifecycle_state = 'ACTIVE'
                        """,
                        (
                            draft.epistemic_classification,
                            draft.chunk_strategy,
                            draft.section_title,
                            json.dumps(list(draft.structured_elements), ensure_ascii=False),
                            1 if bool(getattr(self.embedding_provider, "is_semantic", False)) else 0,
                            resolved_rid,
                            draft.chunk_index,
                        ),
                    )
                conn.commit()
                self.extractor._record_operation(
                    conn,
                    stage="INDEXING",
                    source_id=doc.source_id,
                    resource_id=resolved_rid,
                    version_id=doc.version_id,
                    status="SKIPPED_IDEMPOTENT",
                    decision_or_reason=f"Active chunks and vectors for resource {resolved_rid} are already up to date.",
                    retryable=False,
                    details={"chunk_count": len(existing_rows)},
                )
                return {
                    "status": "SKIPPED_IDEMPOTENT",
                    "document": doc.to_dict(),
                    "structured_elements": [e.to_dict() for e in elements],
                    "chunk_count": len(existing_rows),
                    "indexed_vector_ids": existing_vec_ids,
                }

            # Generate embeddings & validate dimensions before touching index
            texts = [c.chunk_text for c in chunk_drafts]
            vectors = self.embedding_provider.embed_texts(texts) if texts else []
            expected_dim = int(self.embedding_provider.dimensions)
            for idx, vec in enumerate(vectors):
                if len(vec) != expected_dim:
                    raise ValueError(
                        f"Embedding dimension mismatch on chunk {idx}: expected {expected_dim}, got {len(vec)}"
                    )

            # Supersede stale SQLite chunks & remove stale vectors from Chroma
            now = _utc_now_iso()
            stale_vec_ids = [
                vid for vid in existing_vec_ids if vid not in {c.chunk_id for c in chunk_drafts}
            ]
            if existing_rows:
                conn.execute(
                    """
                    UPDATE knowledge_chunks
                    SET lifecycle_state = 'SUPERSEDED'
                    WHERE resource_id = ? AND lifecycle_state = 'ACTIVE'
                    """,
                    (resolved_rid,),
                )
            if stale_vec_ids:
                chroma_index.delete_chunks(stale_vec_ids)

            is_sem = 1 if bool(getattr(self.embedding_provider, "is_semantic", False)) else 0
            for draft in chunk_drafts:
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        chunk_id, resource_id, version_id, source_id, chunk_index,
                        content_origin, epistemic_layer, char_start, char_end,
                        chunk_text, content_hash, embedding_provider, embedding_model,
                        embedding_dimensions, vector_collection, vector_id,
                        lifecycle_state, indexed_at, provenance_event_id,
                        epistemic_classification, chunk_strategy, section_title,
                        structured_elements_json, is_semantic_embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        version_id = excluded.version_id,
                        content_origin = excluded.content_origin,
                        epistemic_layer = excluded.epistemic_layer,
                        char_start = excluded.char_start,
                        char_end = excluded.char_end,
                        chunk_text = excluded.chunk_text,
                        content_hash = excluded.content_hash,
                        embedding_provider = excluded.embedding_provider,
                        embedding_model = excluded.embedding_model,
                        embedding_dimensions = excluded.embedding_dimensions,
                        vector_collection = excluded.vector_collection,
                        vector_id = excluded.vector_id,
                        lifecycle_state = 'ACTIVE',
                        indexed_at = excluded.indexed_at,
                        epistemic_classification = excluded.epistemic_classification,
                        chunk_strategy = excluded.chunk_strategy,
                        section_title = excluded.section_title,
                        structured_elements_json = excluded.structured_elements_json,
                        is_semantic_embedding = excluded.is_semantic_embedding
                    """,
                    (
                        draft.chunk_id,
                        draft.resource_id,
                        draft.version_id or "v0",
                        draft.source_id,
                        draft.chunk_index,
                        draft.content_origin,
                        draft.epistemic_layer,
                        draft.char_start,
                        draft.char_end,
                        draft.chunk_text,
                        draft.content_hash,
                        self.embedding_provider.provider_name,
                        self.embedding_provider.model_name,
                        expected_dim,
                        self.collection_name,
                        draft.chunk_id,
                        now,
                        None,
                        draft.epistemic_classification,
                        draft.chunk_strategy,
                        draft.section_title,
                        json.dumps(list(draft.structured_elements), ensure_ascii=False),
                        is_sem,
                    ),
                )
            conn.commit()

            if chunk_drafts:
                chroma_index.upsert_chunks(
                    chunk_ids=[d.chunk_id for d in chunk_drafts],
                    embeddings=vectors,
                    documents=[d.chunk_text for d in chunk_drafts],
                    metadatas=[
                        {
                            "chunk_id": d.chunk_id,
                            "resource_id": d.resource_id,
                            "version_id": d.version_id or "v0",
                            "source_id": d.source_id,
                            "chunk_index": int(d.chunk_index),
                            "content_origin": d.content_origin,
                            "epistemic_layer": d.epistemic_layer,
                            "epistemic_classification": d.epistemic_classification,
                            "chunk_strategy": d.chunk_strategy,
                            "section_title": d.section_title,
                            "content_hash": d.content_hash,
                            "embedding_provider": self.embedding_provider.provider_name,
                            "embedding_model": self.embedding_provider.model_name,
                            "embedding_dimensions": int(expected_dim),
                            "is_semantic_embedding": bool(is_sem),
                            "canonical_url": d.canonical_url,
                            "title": d.title,
                        }
                        for d in chunk_drafts
                    ],
                )
            upserted_count = len(chunk_drafts)
            self.extractor._record_operation(
                conn,
                stage="INDEXING",
                source_id=doc.source_id,
                resource_id=resolved_rid,
                version_id=doc.version_id,
                status="SUCCEEDED",
                decision_or_reason=f"Indexed {upserted_count} chunks into Chroma ({self.collection_name})",
                retryable=False,
                details={
                    "document_id": doc.document_id,
                    "chunk_count": upserted_count,
                    "superseded_vectors_removed": len(stale_vec_ids),
                    "chunk_strategy": chunk_drafts[0].chunk_strategy if chunk_drafts else None,
                    "embedding_provider": self.embedding_provider.provider_name,
                    "embedding_model": self.embedding_provider.model_name,
                    "embedding_dimensions": expected_dim,
                    "is_semantic_embedding": bool(is_sem),
                },
            )
            return {
                "status": "INDEXED",
                "document": doc.to_dict(),
                "structured_elements": [e.to_dict() for e in elements],
                "chunk_count": upserted_count,
                "indexed_vector_ids": [c.chunk_id for c in chunk_drafts],
                "superseded_vectors_removed": stale_vec_ids,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 5. Failure Reporting & Retry Workflow (Phase I)
    # ------------------------------------------------------------------

    def list_failures_and_deferrals(self) -> dict[str, Any]:
        conn = self._connect_ro()
        try:
            deferred_resources = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT resource_id, source_id, manifest_id, title, canonical_url,
                           collection_decision, verification_status, last_verified_at
                    FROM resources
                    WHERE LOWER(COALESCE(collection_decision, '')) LIKE '%defer%'
                       OR (last_verified_at IS NOT NULL AND LOWER(COALESCE(verification_status, '')) NOT LIKE 'verified%')
                    ORDER BY resource_id ASC
                    """
                ).fetchall()
            ]
            operation_failures: list[dict[str, Any]] = []
            if _table_exists(conn, "ingestion_operations"):
                operation_failures = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT * FROM ingestion_operations
                        WHERE status IN ('FAILED', 'REJECTED_BY_POLICY', 'DEFERRED')
                        ORDER BY recorded_at DESC
                        """
                    ).fetchall()
                ]
            return {
                "deferred_or_unverified_resources": deferred_resources,
                "failed_or_rejected_operations": operation_failures,
                "retryable_operation_count": sum(1 for op in operation_failures if bool(op.get("retryable"))),
            }
        finally:
            conn.close()

    def retry_failed_operations(
        self,
        *,
        resource_id: str | int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        conn = self._connect_ro() if dry_run else self._connect_rw()
        try:
            if not _table_exists(conn, "ingestion_operations"):
                return {"retried_count": 0, "outcomes": [], "dry_run": dry_run}
            query = "SELECT * FROM ingestion_operations WHERE status = 'FAILED' AND retryable = 1"
            params: list[Any] = []
            if resource_id is not None:
                res_row = _resolve_resource_row(conn, resource_id)
                resolved_rid = str(res_row["resource_id"]) if res_row is not None else str(resource_id)
                query += " AND resource_id = ?"
                params.append(resolved_rid)
            query += " ORDER BY recorded_at ASC"
            ops = [dict(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

        outcomes: list[dict[str, Any]] = []
        for op in ops:
            rid = op.get("resource_id")
            if rid is None:
                continue
            if dry_run:
                outcomes.append(
                    {
                        "operation_id": op["operation_id"],
                        "resource_id": str(rid),
                        "stage": op["stage"],
                        "status": "DRY_RUN_ELIGIBLE_FOR_RETRY",
                    }
                )
            else:
                res = self.ingest_resource_to_knowledge(resource_id=str(rid), incremental=False, dry_run=False)
                conn_rw = self._connect_rw()
                try:
                    conn_rw.execute(
                        "UPDATE ingestion_operations SET status = 'RETRIED_SUCCEEDED' WHERE operation_id = ?",
                        (op["operation_id"],),
                    )
                    conn_rw.commit()
                finally:
                    conn_rw.close()
                outcomes.append(
                    {
                        "operation_id": op["operation_id"],
                        "resource_id": str(rid),
                        "stage": op["stage"],
                        "status": res["status"],
                    }
                )
        return {
            "retried_count": len(outcomes),
            "outcomes": outcomes,
            "dry_run": dry_run,
        }


# ==============================================================================
# PHASE K — REPRODUCIBLE END-TO-END INGESTION DEMONSTRATION
# ==============================================================================


def run_reproducible_ingestion_demo(
    *,
    baseline_db_path: Path | str = DEFAULT_SQLITE_DB_PATH,
    baseline_chroma_dir: Path | str = DEFAULT_CHROMA_DIR,
    work_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Run an isolated, deterministic end-to-end demonstration of Task 037 (Phase K).

    Clones `baseline_db_path` and `baseline_chroma_dir` into an isolated working directory
    so `data/runtime/task012_pilot.sqlite` is NEVER modified, then demonstrates all 10 steps:
    1. Source discovery or curated resource selection (curated Wikipedia concept article `Multilayer_perceptron` + uncollected baseline inspection).
    2. Policy validation (`evaluate_resource_ingestion_policy` + Wikipedia scope reconciliation).
    3. Authorized collection via `WikipediaSourceAdapter` (MediaWiki API CC BY-SA 4.0 extract with `oldid`).
    4. Content extraction (`ContentExtractor`).
    5. Normalized document preview (all 15 mandatory fields).
    6. Structured knowledge & content-aware chunk creation (`extract_structured_knowledge` + `ContentAwareChunker`).
    7. Embedding generation (`DeterministicHashEmbeddingProvider`, `is_semantic=False`).
    8. Vector indexing in Chroma (`ChromaVectorIndex`) + explicit 6-check consistency verification.
    9. Retrieval with citation (`LocalRAGPipeline.retrieve`).
    10. Teaching Agent explanation using the newly ingested evidence across learner levels.
    """
    from .platform import PlatformOrchestrator

    created_temp = False
    if work_dir is None:
        tmp_root = Path(tempfile.mkdtemp(prefix="task037_ingestion_demo_"))
        created_temp = True
    else:
        tmp_root = Path(work_dir)
        tmp_root.mkdir(parents=True, exist_ok=True)

    demo_db = tmp_root / "demo_pilot.sqlite"
    demo_chroma = tmp_root / "demo_chroma"
    shutil.copy2(Path(baseline_db_path), demo_db)
    if Path(baseline_chroma_dir).exists():
        if demo_chroma.exists():
            shutil.rmtree(demo_chroma)
        shutil.copytree(Path(baseline_chroma_dir), demo_chroma)

    pipeline = SourceToKnowledgeIngestionPipeline(
        sqlite_db_path=demo_db,
        chroma_dir=demo_chroma,
        collection_name=CHROMA_COLLECTION_NAME,
    )

    before_status = pipeline.get_ingestion_status()

    # Step 1: Curated resource selection — register an uncollected curated Wikipedia concept seed on the isolated demo DB
    store = KnowledgeStore(demo_db)
    seed_url = "https://en.wikipedia.org/wiki/Multilayer_perceptron"
    seed_upsert = store.upsert_resource(
        source_id="SRC-WIKIPEDIA",
        manifest_id=None,
        canonical_url=seed_url,
        title="Multilayer perceptron",
        authors="Wikipedia contributors",
        resource_type="Wikipedia concept article",
        media_modality="TEXT_HTML",
        domain_scope="en.wikipedia.org/wiki/",
        priority="P1",
        collection_decision="Collect via MediaWiki API with CC BY-SA 4.0 attribution and revision ID",
        verification_status="Manifest seed pending collection",
        robots_sitemap_status="Checked - MediaWiki API allowed",
        rights_status="CC BY-SA 4.0",
        license_status="CC_BY_SA_4_0",
        license_spdx="CC-BY-SA-4.0",
        permitted_storage_scope="FULL_TEXT_WITH_ATTRIBUTION_AND_SHARE_ALIKE",
        permits_full_text_storage=True,
        permits_verbatim_excerpts=True,
        legal_uncertainty_status="CLEAR_OPEN_LICENSE",
        text_storage_status="metadata-only-not-yet-collected",
        last_verified_at=None,
        acquisition_method="CURATED_SEED_SELECTION",
        actor="task037_demo",
        change_reason="Select curated Wikipedia concept resource for Task 037 end-to-end ingestion demo",
    )
    store.close()
    target_resource_id = str(seed_upsert["resource_id"])
    resource_preview_before = pipeline.preview_resource(target_resource_id)

    # Step 2: Policy validation & Wikipedia scope audit/reconciliation on isolated DB
    conn_rw = pipeline._connect_rw()
    try:
        wiki_reconcile = audit_and_reconcile_wikipedia_scope(conn_rw, apply_fix=True)
        policy_check = evaluate_resource_ingestion_policy(
            conn_rw,
            resource_id=target_resource_id,
            requested_action="COLLECT",
        )
    finally:
        conn_rw.close()

    # Step 3: Authorized collection using a deterministic MediaWiki API response for Multilayer_perceptron
    sample_mlp_extract = (
        "A multilayer perceptron (MLP) is a feedforward artificial neural network that maps sets of "
        "input data onto a set of appropriate outputs using multiple layers of nodes in a directed graph.\n\n"
        "## Architecture and Mathematical Formulation\n"
        "A multilayer perceptron is defined as a feedforward neural network with an input layer, one or more "
        "nonlinear hidden layers, and an output layer. Each hidden unit computes a pre-activation linear "
        "combination followed by a differentiable nonlinear activation function:\n"
        "$$a^{(l)} = \\sigma(W^{(l)} a^{(l-1)} + b^{(l)})$$\n"
        "Multilayer perceptrons depend on backpropagation and gradient descent to compute partial derivatives "
        "of the loss function with respect to weights.\n\n"
        "## Universal Approximation and Limitations\n"
        "We show that multilayer networks with nonlinear hidden activations can approximate continuous functions "
        "on compact subsets of Euclidean space (Rumelhart et al., 1986; Cybenko, 1989). A key limitation of deep "
        "multilayer perceptrons with saturating sigmoid activations is the vanishing gradient problem."
    )
    mediawiki_payload = {
        "batchcomplete": True,
        "query": {
            "pages": [
                {
                    "pageid": 31842,
                    "ns": 0,
                    "title": "Multilayer perceptron",
                    "pagelanguage": "en",
                    "canonicalurl": seed_url,
                    "revisions": [
                        {
                            "revid": 1250011223,
                            "parentid": 1250011000,
                            "timestamp": "2026-03-15T10:00:00Z",
                        }
                    ],
                    "pageprops": {
                        "wikibase_item": "Q189878",
                        "wikibase-shortdesc": "Class of feedforward artificial neural network",
                    },
                    "categories": [
                        {"title": "Category:Artificial neural networks"},
                        {"title": "Category:Deep learning"},
                    ],
                    "extract": sample_mlp_extract,
                }
            ]
        },
    }

    class _DemoWikiResponse:
        def __init__(self, status_code: int, body: str, headers: dict[str, str]) -> None:
            self.status_code = status_code
            self._body = body.encode("utf-8")
            self.headers = headers
            self.encoding = "utf-8"

        def iter_content(self, chunk_size: int = 8192):
            yield self._body

        def close(self) -> None:
            pass

    class _DemoWikiSession:
        def get(self, url: str, **kwargs: Any) -> _DemoWikiResponse:
            if "robots.txt" in url:
                return _DemoWikiResponse(
                    200,
                    "User-agent: *\nAllow: /wiki/\n",
                    {"Content-Type": "text/plain"},
                )
            return _DemoWikiResponse(
                200,
                json.dumps(mediawiki_payload),
                {"Content-Type": "application/json; charset=utf-8"},
            )

    collection_result = pipeline.collect_resource(
        resource_id=target_resource_id,
        session=_DemoWikiSession(),
        rate_limiter=OriginRateLimiter(sleep_fn=lambda _: None),
        include_wikipedia_extract=True,
        dry_run=False,
    )

    # Steps 4, 5, 6, 7, 8: Content extraction, 15-field normalization, structured knowledge, chunking, embedding, indexing
    ingestion_result = pipeline.ingest_resource_to_knowledge(
        resource_id=target_resource_id,
        raw_content=sample_mlp_extract,
        extraction_format=ExtractionFormat.PLAIN_TEXT,
        incremental=False,
        dry_run=False,
    )

    after_status = pipeline.get_ingestion_status()
    consistency_report = verify_index_consistency(
        sqlite_db_path=demo_db,
        chroma_dir=demo_chroma,
        collection_name=CHROMA_COLLECTION_NAME,
    )

    # Step 9: Retrieval with citation via PlatformOrchestrator / LocalRAGPipeline
    orchestrator = PlatformOrchestrator(
        db_path=demo_db,
        chroma_dir=demo_chroma,
        progress_db_path=tmp_root / "demo_progress.sqlite",
    )
    retrieved_chunks = orchestrator._search_knowledge_chunks(
        query_text="How does a multilayer perceptron compute hidden layer activations with backpropagation and what is its limitation?",
        top_k=4,
    )

    # Step 10: Teaching Agent explanation grounded in newly ingested knowledge across learner levels
    _, curriculum = orchestrator.run_learning_planner("Neural network foundations and backpropagation")
    all_lessons = [l for s in curriculum.stages for l in s.lessons]
    target_lesson = next(
        (l for l in all_lessons if "backpropagation" in l.lesson_id.lower()),
        all_lessons[0],
    )
    lesson_id = target_lesson.lesson_id
    _, teaching_beginner = orchestrator.run_teaching_session(
        curriculum_id=curriculum.curriculum_id,
        lesson_id=lesson_id,
        learner_question="What is a multilayer perceptron and how does backpropagation update hidden layers?",
        explanation_depth="beginner",
    )
    _, teaching_intermediate = orchestrator.run_teaching_session(
        curriculum_id=curriculum.curriculum_id,
        lesson_id=lesson_id,
        learner_question="Explain how multilayer perceptrons compute hidden layer activations using weights, biases, and backpropagation.",
        explanation_depth="intermediate",
    )
    _, teaching_research = orchestrator.run_teaching_session(
        curriculum_id=curriculum.curriculum_id,
        lesson_id=lesson_id,
        learner_question="What are the mathematical formulation and limitations of multilayer perceptrons trained with backpropagation?",
        explanation_depth="research_level",
    )

    top_hit = retrieved_chunks[0] if retrieved_chunks else None
    summary = {
        "demo_workspace": str(tmp_root),
        "isolated_database": str(demo_db),
        "step_1_selected_resource": {
            "resource_id": target_resource_id,
            "title": resource_preview_before["title"],
            "canonical_url": resource_preview_before["canonical_url"],
            "was_collected_before": resource_preview_before["is_collected"],
        },
        "step_2_policy_validation": {
            "policy_decision": policy_check.to_dict(),
            "wikipedia_scope_reconciled_count": len(wiki_reconcile["reconciled_resource_ids"]),
        },
        "step_3_authorized_collection": {
            "status": collection_result["status"],
            "version_id": collection_result["version_id"],
            "permitted_storage_scope": collection_result["envelope"]["permitted_storage_scope"],
            "text_storage_status": collection_result["envelope"]["text_storage_status"],
        },
        "step_4_and_5_normalized_document": {
            "document_id": ingestion_result["document"]["document_id"],
            "required_15_fields": ingestion_result["document"]["required_15_fields"],
            "section_count": len(ingestion_result["document"]["sections"]),
        },
        "step_6_structured_knowledge_and_chunks": {
            "structured_element_count": len(ingestion_result["structured_elements"]),
            "structured_elements": ingestion_result["structured_elements"],
            "chunk_count": ingestion_result["chunk_count"],
            "indexed_vector_ids": ingestion_result["indexed_vector_ids"],
        },
        "step_7_and_8_embedding_and_index_verification": {
            "before_active_chunks": before_status["lifecycle_metrics"]["active_knowledge_chunks"],
            "after_active_chunks": after_status["lifecycle_metrics"]["active_knowledge_chunks"],
            "before_indexed_vectors": before_status["lifecycle_metrics"]["indexed_vectors"],
            "after_indexed_vectors": after_status["lifecycle_metrics"]["indexed_vectors"],
            "index_consistency": consistency_report.to_dict(),
        },
        "step_9_retrieval_with_citation": {
            "retrieved_count": len(retrieved_chunks),
            "top_hit_resource_id": top_hit.resource_id if top_hit else None,
            "top_hit_title": top_hit.title if top_hit else None,
            "top_hit_canonical_url": top_hit.canonical_url if top_hit else None,
            "top_hit_chunk_id": top_hit.chunk_id if top_hit else None,
        },
        "step_10_teaching_agent_explanation": {
            "lesson_id": teaching_intermediate.lesson_id,
            "explanation_depth": teaching_intermediate.explanation_depth.value,
            "grounding_status": teaching_intermediate.status,
            "citation_count": len(teaching_intermediate.source_citations),
            "cited_resource_ids": [c.resource_id for c in teaching_intermediate.source_citations],
            "beginner_excerpt": teaching_beginner.core_explanation[:240],
            "explanation_excerpt": teaching_intermediate.core_explanation[:360],
            "research_level_depth": teaching_research.explanation_depth.value,
            "research_level_excerpt": teaching_research.core_explanation[:360],
        },
    }

    if created_temp:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return summary


# ==============================================================================
# CLI ENTRYPOINT (PHASE I)
# ==============================================================================


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_research_learning.ingestion",
        description="Task 037 — Governed Source-to-Knowledge Ingestion Pipeline CLI",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_SQLITE_DB_PATH),
        help="Path to SQLite knowledge store database (default: data/runtime/task012_pilot.sqlite)",
    )
    parser.add_argument(
        "--chroma-dir",
        default=str(DEFAULT_CHROMA_DIR),
        help="Path to Chroma persistent directory (default: data/runtime/chroma_knowledge_v1)",
    )
    parser.add_argument(
        "--collection",
        default=CHROMA_COLLECTION_NAME,
        help=f"Chroma collection name (default: {CHROMA_COLLECTION_NAME})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Inspect 11-metric ingestion control plane status and epistemic breakdown")

    p_elig = sub.add_parser("list-eligible", help="List curated resources and their ingestion eligibility")
    p_elig.add_argument("--source-id", default=None, help="Filter by source_id (e.g. SRC-WIKIPEDIA)")
    p_elig.add_argument("--only-uncollected", action="store_true", help="Show only uncollected resources")

    p_prev = sub.add_parser("preview-resource", help="Preview a resource before collection or extraction")
    p_prev.add_argument("--resource-id", type=str, required=True, help="Resource ID (res_..., manifest_id, or rowid) to preview")

    p_pol = sub.add_parser("validate-policy", help="Validate collection/extraction policy for a resource or URL")
    p_pol.add_argument("--resource-id", type=str, default=None, help="Resource ID")
    p_pol.add_argument("--source-id", default=None, help="Source ID")
    p_pol.add_argument("--url", default=None, help="Canonical URL")
    p_pol.add_argument(
        "--action",
        default="COLLECT",
        choices=["COLLECT", "EXTRACT_FULL_TEXT", "EXTRACT_METADATA_OR_ABSTRACT", "EXTRACT_TRANSCRIPT"],
        help="Requested ingestion action",
    )

    p_col = sub.add_parser("collect", help="Execute authorized collection for a resource")
    p_col.add_argument("--resource-id", type=str, required=True, help="Resource ID to collect")
    p_col.add_argument("--dry-run", action="store_true", help="Preview collection decision without network/DB writes")

    p_ext = sub.add_parser("extract", help="Extract and normalize content for a resource")
    p_ext.add_argument("--resource-id", type=str, required=True, help="Resource ID")
    p_ext.add_argument("--format", default=None, help="Extraction format override (HTML, PDF, PLAIN_TEXT, STRUCTURED_METADATA, AUTHORIZED_TRANSCRIPT, SLIDE_DOCUMENT)")
    p_ext.add_argument("--dry-run", action="store_true", help="Preview extraction without DB writes")

    p_pnorm = sub.add_parser("preview-normalized", help="Preview the 15-field normalized document for a resource")
    p_pnorm.add_argument("--resource-id", type=str, required=True, help="Resource ID")

    p_chk = sub.add_parser("chunk", help="Generate content-aware chunks for a resource")
    p_chk.add_argument("--resource-id", type=str, required=True, help="Resource ID")
    p_chk.add_argument("--strategy", default=None, help="Optional chunking strategy override")
    p_chk.add_argument("--dry-run", action="store_true", help="Preview generated chunks without indexing")

    p_pchk = sub.add_parser("preview-chunks", help="Preview content-aware chunks for a resource without writing")
    p_pchk.add_argument("--resource-id", type=str, required=True, help="Resource ID")
    p_pchk.add_argument("--strategy", default=None, help="Optional chunking strategy override")

    p_emb = sub.add_parser("embed", help="Generate embeddings for a resource's chunks (supports --dry-run)")
    p_emb.add_argument("--resource-id", type=str, required=True, help="Resource ID")
    p_emb.add_argument("--dry-run", action="store_true", help="Preview embedding metadata and dimensions without indexing")

    p_idx = sub.add_parser("index", help="Run full extract -> chunk -> embed -> index pipeline for a resource")
    p_idx.add_argument("--resource-id", type=str, required=True, help="Resource ID")
    p_idx.add_argument("--no-incremental", action="store_true", help="Force re-indexing even if hashes match")
    p_idx.add_argument("--dry-run", action="store_true", help="Dry-run without modifying SQLite or Chroma")

    sub.add_parser("verify-index", help="Verify 6-check SQLite and Chroma vector consistency")

    sub.add_parser("failures", help="Report failed, rejected, or deferred resources and operations")

    p_ret = sub.add_parser("retry", help="Retry eligible failed ingestion operations")
    p_ret.add_argument("--resource-id", type=str, default=None, help="Optional resource ID filter")
    p_ret.add_argument("--dry-run", action="store_true", help="List retryable operations without executing")

    sub.add_parser("demo", help="Run reproducible end-to-end ingestion demonstration on an isolated DB copy")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    pipeline = SourceToKnowledgeIngestionPipeline(
        sqlite_db_path=Path(args.db),
        chroma_dir=Path(args.chroma_dir),
        collection_name=args.collection,
    )

    cmd = args.command
    if cmd == "status":
        out = pipeline.get_ingestion_status()
    elif cmd == "list-eligible":
        items = pipeline.list_eligible_resources(
            source_id=args.source_id,
            include_uncollected=True,
            include_collected_unchunked=not args.only_uncollected,
        )
        out = {"count": len(items), "resources": items}
    elif cmd == "preview-resource":
        out = pipeline.preview_resource(args.resource_id)
    elif cmd == "validate-policy":
        conn = pipeline._connect_ro()
        try:
            res = evaluate_resource_ingestion_policy(
                conn,
                resource_id=args.resource_id,
                source_id=args.source_id,
                canonical_url=args.url,
                requested_action=args.action,
            )
            out = res.to_dict()
        finally:
            conn.close()
    elif cmd == "collect":
        out = pipeline.collect_resource(resource_id=args.resource_id, dry_run=bool(args.dry_run))
    elif cmd == "extract":
        conn = pipeline._connect_ro() if args.dry_run else pipeline._connect_rw()
        try:
            doc = pipeline.extractor.extract_and_normalize(
                conn,
                resource_id=args.resource_id,
                extraction_format=args.format,
                persist=not args.dry_run,
                dry_run=bool(args.dry_run),
            )
            out = doc.to_dict()
        finally:
            conn.close()
    elif cmd == "preview-normalized":
        conn = pipeline._connect_ro()
        try:
            doc = pipeline.extractor.extract_and_normalize(
                conn,
                resource_id=args.resource_id,
                persist=False,
                dry_run=True,
            )
            out = doc.to_dict()
        finally:
            conn.close()
    elif cmd in {"chunk", "preview-chunks"}:
        is_dry = True if cmd == "preview-chunks" else bool(args.dry_run)
        out = pipeline.ingest_resource_to_knowledge(
            resource_id=args.resource_id,
            chunk_strategy=args.strategy,
            dry_run=is_dry,
        )
    elif cmd == "embed":
        preview = pipeline.ingest_resource_to_knowledge(
            resource_id=args.resource_id,
            dry_run=True,
        )
        texts = [c["chunk_text"] for c in preview["chunk_drafts"]]
        vecs = pipeline.embedding_provider.embed_texts(texts) if texts else []
        out = {
            "resource_id": args.resource_id,
            "chunk_count": len(vecs),
            "embedding_provider": preview["embedding_provider"],
            "vector_dimensions_verified": [len(v) for v in vecs],
            "dry_run": bool(args.dry_run),
        }
        if not args.dry_run:
            out["index_result"] = pipeline.ingest_resource_to_knowledge(
                resource_id=args.resource_id,
                dry_run=False,
            )
    elif cmd == "index":
        out = pipeline.ingest_resource_to_knowledge(
            resource_id=args.resource_id,
            incremental=not bool(args.no_incremental),
            dry_run=bool(args.dry_run),
        )
    elif cmd == "verify-index":
        out = verify_index_consistency(
            sqlite_db_path=Path(args.db),
            chroma_dir=Path(args.chroma_dir),
            collection_name=args.collection,
        ).to_dict()
    elif cmd == "failures":
        out = pipeline.list_failures_and_deferrals()
    elif cmd == "retry":
        out = pipeline.retry_failed_operations(
            resource_id=args.resource_id,
            dry_run=bool(args.dry_run),
        )
    elif cmd == "demo":
        out = run_reproducible_ingestion_demo(
            baseline_db_path=Path(args.db),
            baseline_chroma_dir=Path(args.chroma_dir),
        )
    else:
        parser.error(f"Unknown command: {cmd}")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
