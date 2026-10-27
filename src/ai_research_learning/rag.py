"""Local-first RAG, persistent Chroma vector indexing, and multi-provider abstraction.

Implements the Phase 2 & Phase 3 vertical slice defined in ADR-0007:
- Candidate source registration and policy gate for DeepLearning.AI *The Batch* (`SRC-THE-BATCH`)
- Rights-respecting document extraction, normalization, and deterministic chunking
- Interchangeable `EmbeddingProvider` and `GenerationProvider` interfaces with strict
  configuration/credential boundaries (`local_deterministic`, `openai`, `anthropic`, `gemini`,
  and `retrieval_only` fallback)
- Persistent local vector storage via `chromadb.PersistentClient` linked to authoritative
  SQLite records (`resources`, `resource_versions`, `knowledge_chunks`, `provenance_events`)
- Source-grounded retrieval and question answering with citation verification,
  insufficient-evidence refusal, and separation of source text from generated explanations
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Mapping, Protocol, Sequence

import chromadb

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from knowledge_store import (  # noqa: E402
    KnowledgeStore,
    sha256_hex,
    utc_now,
)

DEFAULT_CHROMA_COLLECTION = "ai_research_knowledge_chunks_v1"
DEFAULT_CHUNK_SIZE_CHARS = 600
DEFAULT_CHUNK_OVERLAP_CHARS = 100
DEFAULT_MIN_SIMILARITY_SCORE = 0.22
DEFAULT_TOP_K = 4

VALID_EMBEDDING_PROVIDERS = ("local_deterministic", "openai", "gemini")
VALID_GENERATION_PROVIDERS = ("retrieval_only", "openai", "anthropic", "gemini")

# Candidate Source #6 (ADR-0007 Section G): DeepLearning.AI The Batch
CANDIDATE_SOURCE_THE_BATCH: dict[str, Any] = {
    "source_id": "SRC-THE-BATCH",
    "display_name": "DeepLearning.AI — The Batch",
    "source_category": "EDUCATIONAL_NEWSLETTER",
    "adapter_type": "THE_BATCH_HTML_CANDIDATE",
    "origin": "https://www.deeplearning.ai",
    "approved_host_paths": ["www.deeplearning.ai/the-batch/"],
    "robots_url": "https://www.deeplearning.ai/robots.txt",
    "crawl_delay_seconds": 1.0,
    "min_enforced_delay_seconds": 1.0,
    "max_requests_per_run": 0,
    "default_license_status": "ALL_RIGHTS_RESERVED_METADATA_ONLY",
    "permits_metadata_indexing": 1,
    "permits_full_text_storage": 0,
    "permits_verbatim_excerpts": 0,
    "permits_media_rehosting": 0,
    "legal_uncertainty_status": "UNVERIFIED_REQUIRES_HUMAN_REVIEW",
    "attribution_template": "{title} — DeepLearning.AI (The Batch) ({canonical_url})",
    "metadata_complete": 1,
    "status": "CANDIDATE_PENDING_REVIEW",
    "notes": (
        "Candidate source evaluated in Task 029 (ADR-0007). robots.txt allows general "
        "user-agents (Allow: /) and publishes sitemap.xml, while disallowing bulk AI crawlers "
        "(GPTBot, ClaudeBot, CCBot). Full newsletter articles remain copyright DeepLearning.AI; "
        "full-text storage and RAG chunk indexing are disabled (0) pending human policy review."
    ),
}

KNOWLEDGE_CHUNKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
    version_id TEXT NOT NULL REFERENCES resource_versions(version_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    content_origin TEXT NOT NULL CHECK (
        content_origin IN (
            'RESOURCE_FULL_TEXT',
            'RESOURCE_ABSTRACT_METADATA',
            'TRANSCRIPT_AUTHORIZED_TEXT'
        )
    ),
    epistemic_layer TEXT NOT NULL CHECK (
        epistemic_layer IN (
            'PRIMARY_SOURCE_TEXT',
            'AUTHORIZED_ABSTRACT_OR_DESCRIPTION'
        )
    ),
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end >= char_start),
    chunk_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
    vector_collection TEXT NOT NULL,
    vector_id TEXT NOT NULL UNIQUE,
    lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        lifecycle_state IN ('ACTIVE', 'SUPERSEDED', 'TOMBSTONED')
    ),
    indexed_at TEXT NOT NULL,
    provenance_event_id TEXT REFERENCES provenance_events(event_id),
    UNIQUE (resource_id, version_id, content_origin, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_resource
    ON knowledge_chunks(resource_id, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_version
    ON knowledge_chunks(version_id, lifecycle_state);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source
    ON knowledge_chunks(source_id, lifecycle_state);
"""


class PolicyBlockedIndexingError(ValueError):
    """Raised when a resource or source fails the rights and licensing gate for RAG indexing."""


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a model provider is invoked without required credentials or configuration."""


@dataclass(frozen=True)
class TextSpan:
    """Normalized text chunk with deterministic character offsets and SHA-256 content hash."""

    chunk_index: int
    char_start: int
    char_end: int
    chunk_text: str
    content_hash: str


@dataclass(frozen=True)
class IndexableDocument:
    """Rights-verified document extracted from a SQLite resource for chunking and indexing."""

    resource_id: str
    version_id: str
    source_id: str
    canonical_url: str
    title: str
    authors: str | None
    published_at: str | None
    license_status: str
    license_spdx: str | None
    content_origin: str
    epistemic_layer: str
    text: str


@dataclass(frozen=True)
class ProviderConfig:
    """Explicit configuration for embedding and generation providers without exposing secrets."""

    embedding_provider: str = "local_deterministic"
    embedding_model: str = "token-hash-bow-v1"
    embedding_dimensions: int = 128
    generation_provider: str = "retrieval_only"
    generation_model: str = "none"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    chroma_collection: str = DEFAULT_CHROMA_COLLECTION

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProviderConfig":
        """Load provider configuration from an environment mapping (defaults to `os.environ`)."""
        env = environ if environ is not None else os.environ
        emb_provider = (
            env.get("AI_LEARNING_EMBEDDING_PROVIDER", "local_deterministic").strip().lower()
            or "local_deterministic"
        )
        if emb_provider not in VALID_EMBEDDING_PROVIDERS:
            raise ValueError(
                f"Unsupported AI_LEARNING_EMBEDDING_PROVIDER {emb_provider!r}; "
                f"expected one of {VALID_EMBEDDING_PROVIDERS}"
            )

        default_emb_model = {
            "local_deterministic": "token-hash-bow-v1",
            "openai": "text-embedding-3-small",
            "gemini": "text-embedding-004",
        }[emb_provider]
        emb_model = env.get("AI_LEARNING_EMBEDDING_MODEL", "").strip() or default_emb_model

        raw_dims = env.get("AI_LEARNING_EMBEDDING_DIMENSIONS", "").strip()
        emb_dims = int(raw_dims) if raw_dims else 128
        if emb_dims <= 0:
            raise ValueError("AI_LEARNING_EMBEDDING_DIMENSIONS must be a positive integer")

        gen_provider = (
            env.get("AI_LEARNING_GENERATION_PROVIDER", "retrieval_only").strip().lower()
            or "retrieval_only"
        )
        if gen_provider not in VALID_GENERATION_PROVIDERS:
            raise ValueError(
                f"Unsupported AI_LEARNING_GENERATION_PROVIDER {gen_provider!r}; "
                f"expected one of {VALID_GENERATION_PROVIDERS}"
            )

        default_gen_model = {
            "retrieval_only": "none",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-haiku-latest",
            "gemini": "gemini-2.5-flash",
        }[gen_provider]
        gen_model = env.get("AI_LEARNING_GENERATION_MODEL", "").strip() or default_gen_model

        collection = (
            env.get("AI_LEARNING_CHROMA_COLLECTION", DEFAULT_CHROMA_COLLECTION).strip()
            or DEFAULT_CHROMA_COLLECTION
        )

        def _clean_key(key_name: str) -> str | None:
            val = env.get(key_name)
            if val is None:
                return None
            stripped = val.strip()
            return stripped if stripped else None

        return cls(
            embedding_provider=emb_provider,
            embedding_model=emb_model,
            embedding_dimensions=emb_dims,
            generation_provider=gen_provider,
            generation_model=gen_model,
            openai_api_key=_clean_key("OPENAI_API_KEY"),
            anthropic_api_key=_clean_key("ANTHROPIC_API_KEY"),
            gemini_api_key=_clean_key("GEMINI_API_KEY"),
            chroma_collection=collection,
        )

    def has_embedding_credentials(self) -> bool:
        """Return True if the selected `embedding_provider` has required credentials."""
        if self.embedding_provider == "local_deterministic":
            return True
        if self.embedding_provider == "openai":
            return bool(self.openai_api_key)
        if self.embedding_provider == "gemini":
            return bool(self.gemini_api_key)
        return False

    def has_generation_credentials(self) -> bool:
        """Return True if the selected `generation_provider` is an LLM with configured credentials."""
        if self.generation_provider == "openai":
            return bool(self.openai_api_key)
        if self.generation_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.generation_provider == "gemini":
            return bool(self.gemini_api_key)
        return False

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a serializable configuration summary with all secret values redacted."""
        return {
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "generation_provider": self.generation_provider,
            "generation_model": self.generation_model,
            "chroma_collection": self.chroma_collection,
            "has_openai_api_key": bool(self.openai_api_key),
            "has_anthropic_api_key": bool(self.anthropic_api_key),
            "has_gemini_api_key": bool(self.gemini_api_key),
            "embedding_ready": self.has_embedding_credentials(),
            "generation_ready": self.has_generation_credentials(),
        }

    def __repr__(self) -> str:
        safe = self.to_safe_dict()
        return f"ProviderConfig({safe!r})"


@dataclass(frozen=True)
class RetrievedChunk:
    """Source-grounded chunk retrieved from Chroma and verified against SQLite metadata."""

    chunk_id: str
    resource_id: str
    version_id: str
    source_id: str
    canonical_url: str
    title: str
    authors: str | None
    published_at: str | None
    license_status: str
    license_spdx: str | None
    chunk_index: int
    char_start: int
    char_end: int
    chunk_text: str
    content_hash: str
    content_origin: str
    epistemic_layer: str
    similarity_score: float
    vector_distance: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "resource_id": self.resource_id,
            "version_id": self.version_id,
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "authors": self.authors,
            "published_at": self.published_at,
            "license_status": self.license_status,
            "license_spdx": self.license_spdx,
            "chunk_index": self.chunk_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "chunk_text": self.chunk_text,
            "content_hash": self.content_hash,
            "content_origin": self.content_origin,
            "epistemic_layer": self.epistemic_layer,
            "similarity_score": round(self.similarity_score, 4),
            "vector_distance": round(self.vector_distance, 4),
        }


@dataclass(frozen=True)
class SourceCitation:
    """Verified citation linking an answer to an indexed SQLite resource and chunk."""

    chunk_id: str
    resource_id: str
    version_id: str
    source_id: str
    title: str
    authors: str | None
    published_at: str | None
    canonical_url: str
    license_status: str
    license_spdx: str | None
    char_start: int
    char_end: int
    content_origin: str
    epistemic_layer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "resource_id": self.resource_id,
            "version_id": self.version_id,
            "source_id": self.source_id,
            "title": self.title,
            "authors": self.authors,
            "published_at": self.published_at,
            "canonical_url": self.canonical_url,
            "license_status": self.license_status,
            "license_spdx": self.license_spdx,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "content_origin": self.content_origin,
            "epistemic_layer": self.epistemic_layer,
        }


@dataclass(frozen=True)
class GeneratedAnswer:
    """Model-generated synthesis kept strictly separate from primary source chunks."""

    answer_text: str
    provider_name: str
    model_name: str
    cited_chunk_ids: list[str]
    epistemic_status: str = "GENERATED_INTERPRETATION"

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_text": self.answer_text,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "cited_chunk_ids": list(self.cited_chunk_ids),
            "epistemic_status": self.epistemic_status,
        }


@dataclass(frozen=True)
class RAGResponse:
    """Structured response from the local-first RAG pipeline."""

    question: str
    status: str  # "answered" | "retrieval_only_fallback" | "insufficient_evidence"
    sufficient_evidence: bool
    retrieved_chunks: list[RetrievedChunk]
    citations: list[SourceCitation]
    generated_answer: GeneratedAnswer | None = None
    insufficient_evidence_reason: str | None = None
    fallback_reason: str | None = None
    provider_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "sufficient_evidence": self.sufficient_evidence,
            "retrieved_chunks": [c.to_dict() for c in self.retrieved_chunks],
            "citations": [c.to_dict() for c in self.citations],
            "generated_answer": (
                self.generated_answer.to_dict() if self.generated_answer is not None else None
            ),
            "insufficient_evidence_reason": self.insufficient_evidence_reason,
            "fallback_reason": self.fallback_reason,
            "provider_summary": dict(self.provider_summary),
        }


# ---------------------------------------------------------------------------
# Candidate Source Policy Evaluation (SRC-THE-BATCH)
# ---------------------------------------------------------------------------


def register_candidate_source_the_batch(store: KnowledgeStore) -> dict[str, Any]:
    """Register `SRC-THE-BATCH` in `sources` with `CANDIDATE_PENDING_REVIEW` status.

    Preserves the separation between candidate discovery/policy evaluation and
    approved collection: `max_requests_per_run=0`, `permits_full_text_storage=0`,
    and `status='CANDIDATE_PENDING_REVIEW'`.
    """
    return store.register_source(**CANDIDATE_SOURCE_THE_BATCH)


def evaluate_the_batch_candidate_policy(
    robots_txt_content: str | None = None,
) -> dict[str, Any]:
    """Evaluate `https://www.deeplearning.ai/the-batch` against collection & licensing rules.

    Separates technical feasibility from source approval:
    - `technical_feasibility`: `"FEASIBLE_METADATA_ONLY"`
    - `source_approval_status`: `"CANDIDATE_PENDING_REVIEW"`
    - `ingestion_enabled`: `False`
    """
    general_agent_allowed = True
    sitemap_advertised = True
    disallowed_ai_crawlers: list[str] = ["GPTBot", "ClaudeBot", "CCBot", "Google-Extended"]

    if robots_txt_content is not None:
        lowered = robots_txt_content.lower()
        sitemap_advertised = "sitemap:" in lowered
        found_bots: list[str] = []
        for bot in ("GPTBot", "ClaudeBot", "CCBot", "Google-Extended", "ChatGPT-User"):
            if bot.lower() in lowered:
                found_bots.append(bot)
        if found_bots:
            disallowed_ai_crawlers = found_bots
        # Check if User-agent: * has Disallow: /the-batch
        if "disallow: /the-batch" in lowered:
            general_agent_allowed = False

    return {
        "source_id": CANDIDATE_SOURCE_THE_BATCH["source_id"],
        "candidate_url": "https://www.deeplearning.ai/the-batch/",
        "robots_url": CANDIDATE_SOURCE_THE_BATCH["robots_url"],
        "general_user_agent_allowed": general_agent_allowed,
        "sitemap_advertised": sitemap_advertised,
        "disallowed_bulk_ai_crawlers": disallowed_ai_crawlers,
        "default_license_status": CANDIDATE_SOURCE_THE_BATCH["default_license_status"],
        "permits_metadata_indexing": bool(CANDIDATE_SOURCE_THE_BATCH["permits_metadata_indexing"]),
        "permits_full_text_storage": bool(CANDIDATE_SOURCE_THE_BATCH["permits_full_text_storage"]),
        "permits_verbatim_excerpts": bool(CANDIDATE_SOURCE_THE_BATCH["permits_verbatim_excerpts"]),
        "technical_feasibility": (
            "FEASIBLE_METADATA_ONLY" if general_agent_allowed else "BLOCKED_BY_ROBOTS"
        ),
        "source_approval_status": "CANDIDATE_PENDING_REVIEW",
        "ingestion_enabled": False,
        "policy_summary": (
            "Metadata-only cataloguing is technically feasible under User-Agent: *, "
            "but automated full-text scraping and RAG chunk indexing are prohibited "
            "because newsletter text is proprietary and robots.txt disallows AI training crawlers. "
            "Source remains CANDIDATE_PENDING_REVIEW with ingestion_enabled=False."
        ),
    }


# ---------------------------------------------------------------------------
# Text Normalization, Deterministic Chunking, and Rights Gate
# ---------------------------------------------------------------------------


def normalize_text_for_chunking(text: str) -> str:
    """Normalize line endings and whitespace while preserving paragraph structure."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse horizontal whitespace within lines
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.split("\n")]
    # Collapse 3+ consecutive newlines into double newlines
    joined = "\n".join(lines)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()


def compute_chunk_id(
    resource_id: str,
    version_id: str,
    content_origin: str,
    chunk_index: int,
) -> str:
    """Compute a deterministic primary key `chk_<16-hex>` for an indexed chunk."""
    payload = f"{resource_id.strip()}|{version_id.strip()}|{content_origin.strip()}|{int(chunk_index)}"
    return "chk_" + sha256_hex(payload)[:16]


def chunk_text(
    text: str,
    *,
    chunk_size_chars: int = DEFAULT_CHUNK_SIZE_CHARS,
    chunk_overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> list[TextSpan]:
    """Deterministically split normalized text into overlapping `TextSpan` chunks."""
    if chunk_size_chars < 64:
        raise ValueError("chunk_size_chars must be >= 64")
    if chunk_overlap_chars < 0 or chunk_overlap_chars >= chunk_size_chars:
        raise ValueError("chunk_overlap_chars must be >= 0 and < chunk_size_chars")

    normalized = normalize_text_for_chunking(text)
    if not normalized:
        return []

    total_len = len(normalized)
    if total_len <= chunk_size_chars:
        return [
            TextSpan(
                chunk_index=0,
                char_start=0,
                char_end=total_len,
                chunk_text=normalized,
                content_hash=sha256_hex(normalized),
            )
        ]

    spans: list[TextSpan] = []
    start = 0
    chunk_index = 0

    while start < total_len:
        target_end = min(start + chunk_size_chars, total_len)
        if target_end < total_len:
            # Prefer breaking on paragraph, sentence, or space boundary in the second half of the window
            search_floor = start + max(chunk_size_chars // 2, 1)
            window = normalized[search_floor:target_end]
            break_offset = -1
            for sep in ("\n\n", ". ", "! ", "? ", "\n", "; ", ", ", " "):
                pos = window.rfind(sep)
                if pos != -1:
                    break_offset = search_floor + pos + len(sep)
                    break
            end = break_offset if break_offset > start else target_end
        else:
            end = total_len

        raw_slice = normalized[start:end]
        leading_trim = len(raw_slice) - len(raw_slice.lstrip())
        trailing_trim = len(raw_slice) - len(raw_slice.rstrip())
        actual_start = start + leading_trim
        actual_end = end - trailing_trim
        cleaned_slice = normalized[actual_start:actual_end]

        if cleaned_slice:
            spans.append(
                TextSpan(
                    chunk_index=chunk_index,
                    char_start=actual_start,
                    char_end=actual_end,
                    chunk_text=cleaned_slice,
                    content_hash=sha256_hex(cleaned_slice),
                )
            )
            chunk_index += 1

        if end >= total_len:
            break

        next_start = max(end - chunk_overlap_chars, start + 1)
        # Align next_start to a word boundary when possible
        if next_start > start + 1 and next_start < total_len and not normalized[next_start - 1].isspace():
            space_pos = normalized.find(" ", next_start, min(next_start + 40, end))
            if space_pos != -1:
                next_start = space_pos + 1
        start = next_start

    return spans


def extract_indexable_document_from_resource(
    store: KnowledgeStore,
    resource_id: str,
    *,
    full_text: str | None = None,
) -> IndexableDocument:
    """Enforce the rights/licensing gate and extract permitted text for RAG chunk indexing."""
    resource = store.get_resource(resource_id=resource_id)
    if resource is None:
        raise KeyError(f"Unknown resource_id: {resource_id!r}")

    if resource.get("lifecycle_state") != "ACTIVE":
        raise PolicyBlockedIndexingError(
            f"Resource {resource_id!r} has lifecycle_state={resource.get('lifecycle_state')!r} "
            "(only ACTIVE resources may be indexed)."
        )

    if resource.get("collection_decision") == "REJECTED":
        raise PolicyBlockedIndexingError(
            f"Resource {resource_id!r} has collection_decision='REJECTED' and cannot be indexed."
        )

    source_id = str(resource["source_id"])
    source = store.get_source(source_id)
    if source is None:
        raise PolicyBlockedIndexingError(
            f"Resource {resource_id!r} references unknown source {source_id!r}."
        )

    source_status = str(source.get("status") or "")
    if source_status in ("CANDIDATE_PENDING_REVIEW", "RECORD_ONLY", "BLOCKED", "DISABLED"):
        raise PolicyBlockedIndexingError(
            f"Source {source_id!r} has status={source_status!r}; "
            "resources from unapproved or record-only sources cannot be indexed into the RAG vector store."
        )

    version_id = str(resource.get("current_version_id") or "")
    if not version_id:
        raise PolicyBlockedIndexingError(
            f"Resource {resource_id!r} is missing current_version_id."
        )

    license_status = str(resource.get("license_status") or "ALL_RIGHTS_RESERVED_METADATA_ONLY")
    license_spdx = resource.get("license_spdx")
    permitted_scope = str(resource.get("permitted_storage_scope") or "METADATA_ONLY")
    res_permits_full = bool(int(resource.get("permits_full_text_storage") or 0))
    src_permits_full = bool(int(source.get("permits_full_text_storage") or 0))

    # Case 1: Caller supplied full article/body text
    if full_text is not None and full_text.strip():
        if not (res_permits_full and src_permits_full):
            raise PolicyBlockedIndexingError(
                f"Resource {resource_id!r} (source={source_id!r}, license_status={license_status!r}) "
                "does not permit full-text storage or full-text RAG indexing."
            )
        normalized_body = normalize_text_for_chunking(
            f"{resource['title']}\n\n{full_text.strip()}"
        )
        return IndexableDocument(
            resource_id=resource_id,
            version_id=version_id,
            source_id=source_id,
            canonical_url=str(resource["canonical_url"]),
            title=str(resource["title"]),
            authors=resource.get("authors"),
            published_at=resource.get("published_at"),
            license_status=license_status,
            license_spdx=license_spdx,
            content_origin="RESOURCE_FULL_TEXT",
            epistemic_layer="PRIMARY_SOURCE_TEXT",
            text=normalized_body,
        )

    # Case 2: Extract stored abstract/description when permitted by source/resource license
    abstract_text = (resource.get("abstract_or_description") or "").strip()
    if not abstract_text:
        raise PolicyBlockedIndexingError(
            f"Resource {resource_id!r} has no permitted full text or abstract_or_description to index."
        )

    allows_abstract_indexing = (
        (res_permits_full and src_permits_full)
        or permitted_scope
        in (
            "ARXIV_METADATA_AND_ABSTRACT_PERMITTED",
            "WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED",
            "OPEN_LICENSE_FULL_TEXT",
        )
        or license_status
        in (
            "CC_BY_SA_4_0",
            "CC_BY_4_0",
            "CC0_1_0",
            "OPEN_LICENSE_VERIFIED",
            "PUBLIC_DOMAIN_VERIFIED",
            "ARXIV_NON_EXCLUSIVE_DISTRIB",
            "ARXIV_AUTHOR_SELECTED_LICENSE",
        )
    )

    if not allows_abstract_indexing:
        raise PolicyBlockedIndexingError(
            f"Resource {resource_id!r} has license_status={license_status!r} and "
            f"permitted_storage_scope={permitted_scope!r}; semantic chunk indexing is blocked by policy."
        )

    epistemic_layer = (
        "PRIMARY_SOURCE_TEXT"
        if (res_permits_full and src_permits_full)
        else "AUTHORIZED_ABSTRACT_OR_DESCRIPTION"
    )
    content_origin = (
        "RESOURCE_FULL_TEXT"
        if (res_permits_full and src_permits_full)
        else "RESOURCE_ABSTRACT_METADATA"
    )
    combined_text = normalize_text_for_chunking(f"{resource['title']}\n\n{abstract_text}")

    return IndexableDocument(
        resource_id=resource_id,
        version_id=version_id,
        source_id=source_id,
        canonical_url=str(resource["canonical_url"]),
        title=str(resource["title"]),
        authors=resource.get("authors"),
        published_at=resource.get("published_at"),
        license_status=license_status,
        license_spdx=license_spdx,
        content_origin=content_origin,
        epistemic_layer=epistemic_layer,
        text=combined_text,
    )


# ---------------------------------------------------------------------------
# Tokenization & Deterministic Local Embedding Provider
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "explain",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)

_SYNONYM_CONCEPT_MAP: dict[str, list[str]] = {
    "backprop": ["backpropagation", "gradient", "chain_rule", "neural"],
    "backpropagation": ["backpropagation", "gradient", "chain_rule", "derivative", "weight"],
    "gradient": ["gradient", "backpropagation", "derivative", "descent"],
    "gradients": ["gradient", "backpropagation", "derivative", "descent"],
    "chain": ["chain_rule", "backpropagation", "derivative"],
    "dropout": ["dropout", "overfitting", "regularization", "coadaptation", "units"],
    "overfitting": ["overfitting", "dropout", "regularization", "generalization"],
    "regularization": ["regularization", "dropout", "overfitting"],
    "boltzmann": ["boltzmann", "stochastic", "energy", "equilibrium", "rbm"],
    "transformer": ["transformer", "attention", "self_attention", "sequence", "encoder"],
    "transformers": ["transformer", "attention", "self_attention", "sequence", "encoder"],
    "attention": ["attention", "transformer", "self_attention", "sequence"],
    "representation": ["representation", "features", "manifold", "unsupervised"],
    "representations": ["representation", "features", "manifold", "unsupervised"],
}


def _stem_token(token: str) -> str:
    """Apply lightweight deterministic suffix normalization for English technical terms."""
    if len(token) <= 4:
        return token
    if token.endswith("ies") and len(token) > 5:
        return token[:-3] + "y"
    for suffix in ("ingly", "edly", "ation", "itions", "ition", "ments", "ment", "ness", "ings", "ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def extract_content_tokens(text: str) -> list[str]:
    """Extract normalized non-stopword alphanumeric tokens from `text`."""
    raw_tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [tok for tok in raw_tokens if tok not in _STOPWORDS and len(tok) > 1]


def extract_stemmed_content_set(text: str) -> set[str]:
    """Return the set of stemmed content tokens and mapped domain concepts for `text`."""
    tokens = extract_content_tokens(text)
    stems: set[str] = set()
    for tok in tokens:
        stems.add(_stem_token(tok))
        for concept in _SYNONYM_CONCEPT_MAP.get(tok, ()):
            stems.add(_stem_token(concept))
    return stems


class EmbeddingProvider(Protocol):
    """Protocol for interchangeable text embedding providers."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class DeterministicHashEmbeddingProvider:
    """Local offline deterministic feature-hashing embedding provider.

    Produces L2-normalized dense vectors in `R^dimensions` using signed SHA-256
    projections of unigrams, stems, domain concept expansions, and bigrams.
    Requires zero network access or external model weights while providing strong
    cosine similarity separation on technical AI/ML text.
    """

    def __init__(
        self,
        *,
        model_name: str = "token-hash-bow-v1",
        dimensions: int = 128,
    ) -> None:
        if dimensions < 16:
            raise ValueError("dimensions must be >= 16")
        self._provider_name = "local_deterministic"
        self._model_name = model_name
        self._dimensions = int(dimensions)

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _embed_single(self, text: str) -> list[float]:
        vec = [0.0] * self._dimensions
        tokens = extract_content_tokens(text)
        if not tokens:
            # Fallback for empty/stopword-only input so vector is still valid
            tokens = re.findall(r"[a-z0-9]+", text.lower()) or ["_empty_"]

        features: list[tuple[str, float]] = []
        for idx, tok in enumerate(tokens):
            stem = _stem_token(tok)
            features.append((f"u:{tok}", 1.0))
            features.append((f"s:{stem}", 1.4))
            for concept in _SYNONYM_CONCEPT_MAP.get(tok, ()):
                features.append((f"c:{concept}", 1.2))
            if idx + 1 < len(tokens):
                next_stem = _stem_token(tokens[idx + 1])
                features.append((f"b:{stem}_{next_stem}", 0.8))

        for feat, weight in features:
            digest = hashlib.sha256(feat.encode("utf-8")).digest()
            # Project each feature into two coordinates to reduce hash collisions
            idx1 = int.from_bytes(digest[0:4], "big") % self._dimensions
            sign1 = 1.0 if (digest[4] & 1) == 0 else -1.0
            idx2 = int.from_bytes(digest[8:12], "big") % self._dimensions
            sign2 = 1.0 if (digest[12] & 1) == 0 else -1.0
            vec[idx1] += sign1 * weight
            vec[idx2] += sign2 * (weight * 0.5)

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        else:
            vec[0] = 1.0
        return vec

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_single(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return self._embed_single(query)


HttpPostJsonFn = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


def _default_http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Perform an HTTPS JSON POST using `requests` when explicitly configured."""
    import requests

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


class OpenAIEmbeddingProvider:
    """Interchangeable OpenAI embedding provider (`/v1/embeddings`)."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str = "text-embedding-3-small",
        dimensions: int = 128,
        base_url: str = "https://api.openai.com/v1/embeddings",
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._model_name = model_name
        self._dimensions = int(dimensions)
        self._base_url = base_url
        self._http_post = http_post_json or _default_http_post_json

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "OpenAIEmbeddingProvider requires OPENAI_API_KEY to be set."
            )
        return self._api_key

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        api_key = self._require_key()
        if not texts:
            return []
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model_name,
            "input": list(texts),
            "dimensions": self._dimensions,
        }
        data = self._http_post(self._base_url, headers, payload)
        items = sorted(data.get("data", []), key=lambda item: int(item.get("index", 0)))
        vectors = [[float(x) for x in item["embedding"]] for item in items]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"OpenAI embedding response returned {len(vectors)} vectors for {len(texts)} inputs."
            )
        return vectors

    def embed_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return self.embed_texts([query])[0]


class GeminiEmbeddingProvider:
    """Interchangeable Google Gemini embedding provider (`batchEmbedContents`)."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str = "text-embedding-004",
        dimensions: int = 128,
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._model_name = model_name
        self._dimensions = int(dimensions)
        self._http_post = http_post_json or _default_http_post_json

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "GeminiEmbeddingProvider requires GEMINI_API_KEY to be set."
            )
        return self._api_key

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        api_key = self._require_key()
        if not texts:
            return []
        model_path = (
            self._model_name
            if self._model_name.startswith("models/")
            else f"models/{self._model_name}"
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:batchEmbedContents"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        requests_list = [
            {
                "model": model_path,
                "content": {"parts": [{"text": t}]},
                "outputDimensionality": self._dimensions,
            }
            for t in texts
        ]
        data = self._http_post(url, headers, {"requests": requests_list})
        embeddings = data.get("embeddings", [])
        vectors = [[float(x) for x in item["values"]] for item in embeddings]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Gemini embedding response returned {len(vectors)} vectors for {len(texts)} inputs."
            )
        return vectors

    def embed_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return self.embed_texts([query])[0]


def create_embedding_provider(
    config: ProviderConfig,
    *,
    http_post_json: HttpPostJsonFn | None = None,
) -> EmbeddingProvider:
    """Instantiate the configured `EmbeddingProvider`."""
    if config.embedding_provider == "local_deterministic":
        return DeterministicHashEmbeddingProvider(
            model_name=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
    if config.embedding_provider == "openai":
        return OpenAIEmbeddingProvider(
            api_key=config.openai_api_key,
            model_name=config.embedding_model,
            dimensions=config.embedding_dimensions,
            http_post_json=http_post_json,
        )
    if config.embedding_provider == "gemini":
        return GeminiEmbeddingProvider(
            api_key=config.gemini_api_key,
            model_name=config.embedding_model,
            dimensions=config.embedding_dimensions,
            http_post_json=http_post_json,
        )
    raise ValueError(f"Unsupported embedding_provider: {config.embedding_provider!r}")


# ---------------------------------------------------------------------------
# Interchangeable Generation Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RAGPromptInput:
    """Structured prompt payload supplied to a `GenerationProvider`."""

    question: str
    retrieved_chunks: list[RetrievedChunk]


@dataclass(frozen=True)
class GeneratedAnswerPayload:
    """Raw output returned by a `GenerationProvider` before citation validation."""

    answer_text: str
    cited_chunk_ids: list[str]
    provider_name: str
    model_name: str


class GenerationProvider(Protocol):
    """Protocol for interchangeable language model generation providers."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def is_configured(self) -> bool: ...

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload: ...


def build_grounded_rag_prompt(prompt_input: RAGPromptInput) -> tuple[str, str]:
    """Build system and user prompts enforcing strict source-grounded RAG rules."""
    system_prompt = (
        "You are a source-grounded AI research assistant. Answer the user's question "
        "ONLY using the provided retrieved source chunks. "
        "Cite the exact chunk_id(s) you used in square brackets, e.g. [chk_...]. "
        "If the retrieved chunks do not contain sufficient evidence to answer the question, "
        "state clearly that the retrieved sources do not contain enough evidence. "
        "Never fabricate citations or URLs."
    )
    context_blocks: list[str] = []
    for chunk in prompt_input.retrieved_chunks:
        context_blocks.append(
            f"[{chunk.chunk_id}] Title: {chunk.title}\n"
            f"Source: {chunk.source_id} | URL: {chunk.canonical_url} | License: {chunk.license_status}\n"
            f"Content ({chunk.epistemic_layer}): {chunk.chunk_text}"
        )
    joined_context = "\n\n---\n\n".join(context_blocks)
    user_prompt = (
        f"Question: {prompt_input.question}\n\n"
        f"Retrieved Source Chunks:\n{joined_context}\n\n"
        "Respond with a clear, source-grounded explanation referencing the relevant [chk_...] IDs."
    )
    return system_prompt, user_prompt


def _extract_cited_chunk_ids_from_text(
    answer_text: str,
    valid_chunk_ids: Sequence[str],
) -> list[str]:
    """Extract cited `chk_...` identifiers from `answer_text` and filter to valid retrieved IDs."""
    valid_set = set(valid_chunk_ids)
    mentioned = re.findall(r"chk_[0-9a-f]{16}", answer_text)
    verified: list[str] = []
    for cid in mentioned:
        if cid in valid_set and cid not in verified:
            verified.append(cid)
    return verified


class RetrievalOnlyFallbackGenerationProvider:
    """Deterministic retrieval-only provider used when no LLM credentials are configured."""

    def __init__(self, *, reason: str | None = None) -> None:
        self._reason = reason or (
            "Generation provider is set to 'retrieval_only' (no external LLM credentials configured). "
            "Returning verified source chunks and provenance citations without a synthesized LLM response."
        )

    @property
    def provider_name(self) -> str:
        return "retrieval_only"

    @property
    def model_name(self) -> str:
        return "none"

    @property
    def fallback_reason(self) -> str:
        return self._reason

    def is_configured(self) -> bool:
        return False

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload:
        raise ProviderNotConfiguredError(self._reason)


class OpenAIGenerationProvider:
    """Interchangeable OpenAI Chat Completions provider for source-grounded RAG."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1/chat/completions",
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._model_name = model_name
        self._base_url = base_url
        self._http_post = http_post_json or _default_http_post_json

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model_name

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "OpenAIGenerationProvider requires OPENAI_API_KEY to be set."
            )
        system_prompt, user_prompt = build_grounded_rag_prompt(prompt_input)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = self._http_post(self._base_url, headers, payload)
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("OpenAI chat completion returned no choices.")
        content = str(choices[0].get("message", {}).get("content", "")).strip()
        valid_ids = [c.chunk_id for c in prompt_input.retrieved_chunks]
        cited_ids = _extract_cited_chunk_ids_from_text(content, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=content,
            cited_chunk_ids=cited_ids or valid_ids[:1],
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


class AnthropicGenerationProvider:
    """Interchangeable Anthropic Messages API provider for source-grounded RAG."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str = "claude-3-5-haiku-latest",
        base_url: str = "https://api.anthropic.com/v1/messages",
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._model_name = model_name
        self._base_url = base_url
        self._http_post = http_post_json or _default_http_post_json

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return self._model_name

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "AnthropicGenerationProvider requires ANTHROPIC_API_KEY to be set."
            )
        system_prompt, user_prompt = build_grounded_rag_prompt(prompt_input)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model_name,
            "max_tokens": 512,
            "temperature": 0.0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        data = self._http_post(self._base_url, headers, payload)
        blocks = data.get("content", [])
        text_parts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
        content = "\n".join(text_parts).strip()
        valid_ids = [c.chunk_id for c in prompt_input.retrieved_chunks]
        cited_ids = _extract_cited_chunk_ids_from_text(content, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=content,
            cited_chunk_ids=cited_ids or valid_ids[:1],
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


class GeminiGenerationProvider:
    """Interchangeable Google Gemini `generateContent` provider for source-grounded RAG."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str = "gemini-2.5-flash",
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._model_name = model_name
        self._http_post = http_post_json or _default_http_post_json

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model_name

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "GeminiGenerationProvider requires GEMINI_API_KEY to be set."
            )
        system_prompt, user_prompt = build_grounded_rag_prompt(prompt_input)
        model_path = (
            self._model_name
            if self._model_name.startswith("models/")
            else f"models/{self._model_name}"
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent"
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.0},
        }
        data = self._http_post(url, headers, payload)
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini generateContent returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        content = "\n".join(str(p.get("text", "")) for p in parts).strip()
        valid_ids = [c.chunk_id for c in prompt_input.retrieved_chunks]
        cited_ids = _extract_cited_chunk_ids_from_text(content, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=content,
            cited_chunk_ids=cited_ids or valid_ids[:1],
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


def create_generation_provider(
    config: ProviderConfig,
    *,
    http_post_json: HttpPostJsonFn | None = None,
) -> GenerationProvider:
    """Instantiate the configured `GenerationProvider`."""
    if config.generation_provider == "retrieval_only":
        return RetrievalOnlyFallbackGenerationProvider()
    if config.generation_provider == "openai":
        return OpenAIGenerationProvider(
            api_key=config.openai_api_key,
            model_name=config.generation_model,
            http_post_json=http_post_json,
        )
    if config.generation_provider == "anthropic":
        return AnthropicGenerationProvider(
            api_key=config.anthropic_api_key,
            model_name=config.generation_model,
            http_post_json=http_post_json,
        )
    if config.generation_provider == "gemini":
        return GeminiGenerationProvider(
            api_key=config.gemini_api_key,
            model_name=config.generation_model,
            http_post_json=http_post_json,
        )
    raise ValueError(f"Unsupported generation_provider: {config.generation_provider!r}")


# ---------------------------------------------------------------------------
# Persistent Local Chroma Vector Store & SQLite Linkage
# ---------------------------------------------------------------------------


def ensure_knowledge_chunks_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the `knowledge_chunks` table and indexes in SQLite."""
    conn.executescript(KNOWLEDGE_CHUNKS_SCHEMA_SQL)


class ChromaPersistentVectorStore:
    """Local persistent Chroma vector store backed by `chromadb.PersistentClient`."""

    def __init__(
        self,
        persist_directory: Path | str,
        *,
        collection_name: str = DEFAULT_CHROMA_COLLECTION,
    ) -> None:
        self.persist_directory = Path(persist_directory)
        self.persist_directory.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection_name
        self._client = chromadb.PersistentClient(path=str(self.persist_directory))
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert_chunks(
        self,
        *,
        chunk_ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[Mapping[str, str | int | float | bool]],
    ) -> None:
        """Idempotently upsert chunk vectors, documents, and metadata into Chroma."""
        if not chunk_ids:
            return
        self._collection.upsert(
            ids=list(chunk_ids),
            embeddings=[list(vec) for vec in embeddings],
            documents=list(documents),
            metadatas=[dict(m) for m in metadatas],
        )

    def query_similar(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        where: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Query the local Chroma collection and return ranked candidates with cosine similarity."""
        total = self._collection.count()
        if total == 0:
            return []
        n_results = min(max(int(top_k), 1), total)
        query_kwargs: dict[str, Any] = {
            "query_embeddings": [list(query_embedding)],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = dict(where)

        result = self._collection.query(**query_kwargs)
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        hits: list[dict[str, Any]] = []
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            distance = float(dist)
            # Chroma cosine space returns cosine distance = 1.0 - cosine_similarity
            cosine_sim = max(-1.0, min(1.0, 1.0 - distance))
            hits.append(
                {
                    "chunk_id": str(cid),
                    "document": str(doc),
                    "metadata": dict(meta or {}),
                    "distance": distance,
                    "cosine_similarity": cosine_sim,
                }
            )
        return hits

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        """Delete the specified `chunk_ids` from the persistent Chroma collection."""
        if not chunk_ids:
            return
        self._collection.delete(ids=list(chunk_ids))

    def get_chunk_ids(self, *, resource_id: str | None = None) -> list[str]:
        """Return stored vector IDs in the collection, optionally filtered by `resource_id`."""
        if resource_id is not None:
            res = self._collection.get(where={"resource_id": resource_id}, include=[])
        else:
            res = self._collection.get(include=[])
        return [str(i) for i in (res.get("ids") or [])]

    def count(self) -> int:
        """Return the total number of vectors stored in the Chroma collection."""
        return int(self._collection.count())


class LocalRAGPipeline:
    """End-to-end local-first RAG pipeline bridging SQLite and persistent Chroma."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        vector_store: ChromaPersistentVectorStore,
        embedding_provider: EmbeddingProvider,
        generation_provider: GenerationProvider,
        config: ProviderConfig | None = None,
    ) -> None:
        self.store = store
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.generation_provider = generation_provider
        self.config = config or ProviderConfig(
            embedding_provider=embedding_provider.provider_name,
            embedding_model=embedding_provider.model_name,
            embedding_dimensions=embedding_provider.dimensions,
            generation_provider=generation_provider.provider_name,
            generation_model=generation_provider.model_name,
            chroma_collection=vector_store.collection_name,
        )
        ensure_knowledge_chunks_schema(self.store.conn)

    def index_resource(
        self,
        resource_id: str,
        *,
        full_text: str | None = None,
        chunk_size_chars: int = DEFAULT_CHUNK_SIZE_CHARS,
        chunk_overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
    ) -> dict[str, Any]:
        """Extract permitted text, chunk, embed, and index a SQLite resource into Chroma.

        - Enforces source and resource licensing policy before indexing.
        - Avoids re-embedding and duplicate rows when re-indexing unchanged content
          under the same `(version_id, content_hash, embedding_provider, embedding_model, dimensions)`.
        - Marks any superseded chunks for `resource_id` as `SUPERSEDED` in SQLite and
          removes their stale vectors from Chroma.
        """
        ensure_knowledge_chunks_schema(self.store.conn)
        doc = extract_indexable_document_from_resource(
            self.store,
            resource_id,
            full_text=full_text,
        )
        spans = chunk_text(
            doc.text,
            chunk_size_chars=chunk_size_chars,
            chunk_overlap_chars=chunk_overlap_chars,
        )
        if not spans:
            raise PolicyBlockedIndexingError(
                f"Resource {resource_id!r} produced zero non-empty text chunks."
            )

        planned_chunks: list[tuple[str, TextSpan]] = [
            (
                compute_chunk_id(
                    doc.resource_id,
                    doc.version_id,
                    doc.content_origin,
                    span.chunk_index,
                ),
                span,
            )
            for span in spans
        ]
        planned_ids = [cid for cid, _ in planned_chunks]

        # Check existing active chunks for this resource
        existing_rows = self.store.conn.execute(
            """
            SELECT * FROM knowledge_chunks
             WHERE resource_id = ? AND lifecycle_state = 'ACTIVE'
             ORDER BY chunk_index ASC
            """,
            (doc.resource_id,),
        ).fetchall()
        existing_by_id = {str(r["chunk_id"]): dict(r) for r in existing_rows}
        chroma_existing_ids = set(self.vector_store.get_chunk_ids(resource_id=doc.resource_id))

        # Check if all planned chunks are already active, identical, and present in Chroma
        all_unchanged = len(existing_by_id) == len(planned_chunks) and all(
            cid in existing_by_id
            and cid in chroma_existing_ids
            and existing_by_id[cid]["version_id"] == doc.version_id
            and existing_by_id[cid]["content_hash"] == span.content_hash
            and existing_by_id[cid]["embedding_provider"] == self.embedding_provider.provider_name
            and existing_by_id[cid]["embedding_model"] == self.embedding_provider.model_name
            and int(existing_by_id[cid]["embedding_dimensions"]) == self.embedding_provider.dimensions
            and existing_by_id[cid]["vector_collection"] == self.vector_store.collection_name
            for cid, span in planned_chunks
        )

        if all_unchanged:
            return {
                "operation": "unchanged",
                "resource_id": doc.resource_id,
                "version_id": doc.version_id,
                "source_id": doc.source_id,
                "content_origin": doc.content_origin,
                "epistemic_layer": doc.epistemic_layer,
                "chunk_count": len(planned_chunks),
                "chunk_ids": planned_ids,
                "superseded_chunk_ids": [],
            }

        # Embed chunks using configured EmbeddingProvider
        chunk_texts = [span.chunk_text for _, span in planned_chunks]
        embeddings = self.embedding_provider.embed_texts(chunk_texts)
        now = utc_now()

        metadatas: list[dict[str, str | int | float | bool]] = []
        for cid, span in planned_chunks:
            metadatas.append(
                {
                    "chunk_id": cid,
                    "resource_id": doc.resource_id,
                    "version_id": doc.version_id,
                    "source_id": doc.source_id,
                    "chunk_index": int(span.chunk_index),
                    "content_origin": doc.content_origin,
                    "epistemic_layer": doc.epistemic_layer,
                    "license_status": doc.license_status,
                    "canonical_url": doc.canonical_url,
                    "title": doc.title,
                    "content_hash": span.content_hash,
                }
            )

        # Upsert into Chroma first
        self.vector_store.upsert_chunks(
            chunk_ids=planned_ids,
            embeddings=embeddings,
            documents=chunk_texts,
            metadatas=metadatas,
        )

        # Identify any stale/superseded chunk IDs from earlier versions or chunkings
        planned_id_set = set(planned_ids)
        stale_active_ids = [cid for cid in existing_by_id if cid not in planned_id_set]
        stale_chroma_ids = [cid for cid in chroma_existing_ids if cid not in planned_id_set]
        ids_to_delete_from_chroma = sorted(set(stale_active_ids) | set(stale_chroma_ids))
        if ids_to_delete_from_chroma:
            self.vector_store.delete_chunks(ids_to_delete_from_chroma)

        with self.store.transaction() as conn:
            prov = self.store.record_provenance(
                event_type="KNOWLEDGE_CHUNKS_INDEXED",
                acquisition_method="LOCAL_RAG_INDEXER",
                actor="local_rag_pipeline",
                status="indexed",
                source_id=doc.source_id,
                resource_id=doc.resource_id,
                version_id=doc.version_id,
                source_url=doc.canonical_url,
                final_url=doc.canonical_url,
                content_hash=sha256_hex(doc.text),
                details={
                    "chunk_count": len(planned_chunks),
                    "chunk_ids": planned_ids,
                    "superseded_chunk_ids": stale_active_ids,
                    "content_origin": doc.content_origin,
                    "epistemic_layer": doc.epistemic_layer,
                    "embedding_provider": self.embedding_provider.provider_name,
                    "embedding_model": self.embedding_provider.model_name,
                    "embedding_dimensions": self.embedding_provider.dimensions,
                    "vector_collection": self.vector_store.collection_name,
                },
                recorded_at=now,
            )
            prov_event_id = prov["event_id"]

            if stale_active_ids:
                placeholders = ",".join("?" for _ in stale_active_ids)
                conn.execute(
                    f"""
                    UPDATE knowledge_chunks
                       SET lifecycle_state = 'SUPERSEDED',
                           indexed_at = ?
                     WHERE chunk_id IN ({placeholders})
                    """,
                    tuple([now, *stale_active_ids]),
                )

            for cid, span in planned_chunks:
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        chunk_id, resource_id, version_id, source_id, chunk_index,
                        content_origin, epistemic_layer, char_start, char_end,
                        chunk_text, content_hash, embedding_provider, embedding_model,
                        embedding_dimensions, vector_collection, vector_id,
                        lifecycle_state, indexed_at, provenance_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        version_id = excluded.version_id,
                        source_id = excluded.source_id,
                        chunk_index = excluded.chunk_index,
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
                        provenance_event_id = excluded.provenance_event_id
                    """,
                    (
                        cid,
                        doc.resource_id,
                        doc.version_id,
                        doc.source_id,
                        span.chunk_index,
                        doc.content_origin,
                        doc.epistemic_layer,
                        span.char_start,
                        span.char_end,
                        span.chunk_text,
                        span.content_hash,
                        self.embedding_provider.provider_name,
                        self.embedding_provider.model_name,
                        self.embedding_provider.dimensions,
                        self.vector_store.collection_name,
                        cid,
                        now,
                        prov_event_id,
                    ),
                )

        operation = "updated" if existing_by_id else "indexed"
        return {
            "operation": operation,
            "resource_id": doc.resource_id,
            "version_id": doc.version_id,
            "source_id": doc.source_id,
            "content_origin": doc.content_origin,
            "epistemic_layer": doc.epistemic_layer,
            "chunk_count": len(planned_chunks),
            "chunk_ids": planned_ids,
            "superseded_chunk_ids": stale_active_ids,
            "provenance_event_id": prov_event_id,
        }

    def remove_resource_from_index(
        self,
        resource_id: str,
        *,
        reason: str = "resource_removed_or_tombstoned",
    ) -> dict[str, Any]:
        """Remove all vectors for `resource_id` from Chroma and mark SQLite chunks `TOMBSTONED`."""
        ensure_knowledge_chunks_schema(self.store.conn)
        rows = self.store.conn.execute(
            "SELECT chunk_id FROM knowledge_chunks WHERE resource_id = ? AND lifecycle_state != 'TOMBSTONED'",
            (resource_id,),
        ).fetchall()
        sqlite_chunk_ids = [str(r["chunk_id"]) for r in rows]
        chroma_chunk_ids = self.vector_store.get_chunk_ids(resource_id=resource_id)
        all_ids = sorted(set(sqlite_chunk_ids) | set(chroma_chunk_ids))

        if all_ids:
            self.vector_store.delete_chunks(all_ids)

        now = utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE knowledge_chunks
                   SET lifecycle_state = 'TOMBSTONED',
                       indexed_at = ?
                 WHERE resource_id = ?
                """,
                (now, resource_id),
            )
        return {
            "resource_id": resource_id,
            "removed_chunk_ids": all_ids,
            "reason": reason,
        }

    def list_indexed_chunks(
        self,
        *,
        resource_id: str | None = None,
        only_active: bool = True,
    ) -> list[dict[str, Any]]:
        """List SQLite `knowledge_chunks` records linked to `resources` and `resource_versions`."""
        ensure_knowledge_chunks_schema(self.store.conn)
        clauses: list[str] = []
        params: list[Any] = []
        if resource_id is not None:
            clauses.append("kc.resource_id = ?")
            params.append(resource_id)
        if only_active:
            clauses.append("kc.lifecycle_state = 'ACTIVE'")

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.store.conn.execute(
            f"""
            SELECT kc.*,
                   r.canonical_url,
                   r.title,
                   r.current_version_id,
                   r.lifecycle_state AS resource_lifecycle_state
              FROM knowledge_chunks kc
              JOIN resources r ON r.resource_id = kc.resource_id
              JOIN resource_versions rv ON rv.version_id = kc.version_id
              {where_sql}
             ORDER BY kc.resource_id ASC, kc.chunk_index ASC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def retrieve(
        self,
        question: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_similarity_score: float = DEFAULT_MIN_SIMILARITY_SCORE,
        source_ids: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Embed `question`, query Chroma, and join against active SQLite records."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")

        ensure_knowledge_chunks_schema(self.store.conn)
        query_vec = self.embedding_provider.embed_query(question)
        where_filter: dict[str, Any] | None = None
        if source_ids:
            clean_sources = [s.strip() for s in source_ids if s and s.strip()]
            if len(clean_sources) == 1:
                where_filter = {"source_id": clean_sources[0]}
            elif len(clean_sources) > 1:
                where_filter = {"source_id": {"$in": clean_sources}}

        # Fetch candidate pool from Chroma
        raw_hits = self.vector_store.query_similar(
            query_vec,
            top_k=max(int(top_k) * 3, 8),
            where=where_filter,
        )
        if not raw_hits:
            return []

        query_stems = extract_stemmed_content_set(question)
        retrieved: list[RetrievedChunk] = []

        for hit in raw_hits:
            chunk_id = hit["chunk_id"]
            row = self.store.conn.execute(
                """
                SELECT kc.*,
                       r.canonical_url,
                       r.title,
                       r.authors,
                       r.published_at,
                       r.license_status,
                       r.license_spdx,
                       r.lifecycle_state AS resource_lifecycle_state,
                       r.collection_decision,
                       r.current_version_id,
                       s.status AS source_status
                  FROM knowledge_chunks kc
                  JOIN resources r ON r.resource_id = kc.resource_id
                  JOIN sources s ON s.source_id = kc.source_id
                 WHERE kc.chunk_id = ?
                """,
                (chunk_id,),
            ).fetchone()

            if row is None:
                continue
            if row["lifecycle_state"] != "ACTIVE":
                continue
            if row["resource_lifecycle_state"] != "ACTIVE":
                continue
            if row["collection_decision"] == "REJECTED":
                continue
            if row["source_status"] in ("CANDIDATE_PENDING_REVIEW", "RECORD_ONLY", "BLOCKED", "DISABLED"):
                continue
            if row["version_id"] != row["current_version_id"]:
                continue

            # Require both vector similarity and at least minimal concept/lexical grounding
            chunk_stems = extract_stemmed_content_set(
                f"{row['title']} {row['chunk_text']}"
            )
            overlap_count = len(query_stems & chunk_stems) if query_stems else 0
            overlap_ratio = (
                overlap_count / max(len(query_stems), 1) if query_stems else 0.0
            )
            cosine_sim = float(hit["cosine_similarity"])

            # If using local_deterministic hashing, penalize pure hash-collision hits that have 0 stem overlap
            if self.embedding_provider.provider_name == "local_deterministic" and overlap_count == 0:
                effective_score = cosine_sim * 0.25
            else:
                effective_score = min(1.0, (0.75 * cosine_sim) + (0.25 * overlap_ratio))

            if effective_score < min_similarity_score:
                continue

            retrieved.append(
                RetrievedChunk(
                    chunk_id=str(row["chunk_id"]),
                    resource_id=str(row["resource_id"]),
                    version_id=str(row["version_id"]),
                    source_id=str(row["source_id"]),
                    canonical_url=str(row["canonical_url"]),
                    title=str(row["title"]),
                    authors=row["authors"],
                    published_at=row["published_at"],
                    license_status=str(row["license_status"]),
                    license_spdx=row["license_spdx"],
                    chunk_index=int(row["chunk_index"]),
                    char_start=int(row["char_start"]),
                    char_end=int(row["char_end"]),
                    chunk_text=str(row["chunk_text"]),
                    content_hash=str(row["content_hash"]),
                    content_origin=str(row["content_origin"]),
                    epistemic_layer=str(row["epistemic_layer"]),
                    similarity_score=effective_score,
                    vector_distance=float(hit["distance"]),
                )
            )

        retrieved.sort(key=lambda item: (-item.similarity_score, item.chunk_id))
        return retrieved[: max(int(top_k), 1)]

    def answer_question(
        self,
        question: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_similarity_score: float = DEFAULT_MIN_SIMILARITY_SCORE,
        source_ids: Sequence[str] | None = None,
    ) -> RAGResponse:
        """Execute the source-grounded RAG workflow for `question`.

        1. Retrieve relevant chunks from Chroma + SQLite.
        2. If retrieved evidence is empty or below threshold, return `insufficient_evidence`
           without calling the generation model or fabricating citations.
        3. If credentials for the configured `GenerationProvider` are available, generate
           a grounded answer, verify cited `chunk_id`s against retrieved chunks, and return
           `status="answered"` with `generated_answer` separated from `retrieved_chunks`.
        4. If credentials are not configured (`retrieval_only` or missing API key), return
           `status="retrieval_only_fallback"` with the retrieved chunks and citations.
        """
        retrieved = self.retrieve(
            question,
            top_k=top_k,
            min_similarity_score=min_similarity_score,
            source_ids=source_ids,
        )
        provider_summary = self.config.to_safe_dict()

        if not retrieved:
            return RAGResponse(
                question=question,
                status="insufficient_evidence",
                sufficient_evidence=False,
                retrieved_chunks=[],
                citations=[],
                generated_answer=None,
                insufficient_evidence_reason=(
                    f"No indexed source chunks met the minimum relevance threshold "
                    f"({min_similarity_score:.2f}) for this question."
                ),
                fallback_reason=None,
                provider_summary=provider_summary,
            )

        by_chunk_id = {c.chunk_id: c for c in retrieved}

        def _build_citations(chunk_ids: Sequence[str]) -> list[SourceCitation]:
            citations: list[SourceCitation] = []
            for cid in chunk_ids:
                chunk = by_chunk_id.get(cid)
                if chunk is None:
                    continue
                citations.append(
                    SourceCitation(
                        chunk_id=chunk.chunk_id,
                        resource_id=chunk.resource_id,
                        version_id=chunk.version_id,
                        source_id=chunk.source_id,
                        title=chunk.title,
                        authors=chunk.authors,
                        published_at=chunk.published_at,
                        canonical_url=chunk.canonical_url,
                        license_status=chunk.license_status,
                        license_spdx=chunk.license_spdx,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        content_origin=chunk.content_origin,
                        epistemic_layer=chunk.epistemic_layer,
                    )
                )
            return citations

        if not self.generation_provider.is_configured():
            fallback_reason = (
                getattr(self.generation_provider, "fallback_reason", None)
                or f"Generation provider {self.generation_provider.provider_name!r} has no configured credentials; "
                "using deterministic retrieval-only fallback."
            )
            all_citations = _build_citations([c.chunk_id for c in retrieved])
            return RAGResponse(
                question=question,
                status="retrieval_only_fallback",
                sufficient_evidence=True,
                retrieved_chunks=retrieved,
                citations=all_citations,
                generated_answer=None,
                insufficient_evidence_reason=None,
                fallback_reason=fallback_reason,
                provider_summary=provider_summary,
            )

        try:
            payload = self.generation_provider.generate_grounded_answer(
                RAGPromptInput(question=question, retrieved_chunks=retrieved)
            )
        except ProviderNotConfiguredError as exc:
            all_citations = _build_citations([c.chunk_id for c in retrieved])
            return RAGResponse(
                question=question,
                status="retrieval_only_fallback",
                sufficient_evidence=True,
                retrieved_chunks=retrieved,
                citations=all_citations,
                generated_answer=None,
                insufficient_evidence_reason=None,
                fallback_reason=str(exc),
                provider_summary=provider_summary,
            )

        # Verify citations against retrieved chunks (never allow fabricated chunk_ids)
        verified_ids = [cid for cid in payload.cited_chunk_ids if cid in by_chunk_id]
        if not verified_ids:
            verified_ids = [retrieved[0].chunk_id]
        citations = _build_citations(verified_ids)

        generated = GeneratedAnswer(
            answer_text=payload.answer_text,
            provider_name=payload.provider_name,
            model_name=payload.model_name,
            cited_chunk_ids=verified_ids,
            epistemic_status="GENERATED_INTERPRETATION",
        )
        return RAGResponse(
            question=question,
            status="answered",
            sufficient_evidence=True,
            retrieved_chunks=retrieved,
            citations=citations,
            generated_answer=generated,
            insufficient_evidence_reason=None,
            fallback_reason=None,
            provider_summary=provider_summary,
        )


# ---------------------------------------------------------------------------
# End-to-End Proof Runner (Phase 3 Verification)
# ---------------------------------------------------------------------------


def run_end_to_end_rag_proof(work_dir: Path | str | None = None) -> dict[str, Any]:
    """Run a self-contained local proof demonstrating all 8 Phase 3 requirements."""
    temp_ctx = tempfile.TemporaryDirectory() if work_dir is None else None
    base_dir = Path(temp_ctx.name) if temp_ctx is not None else Path(work_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        db_path = base_dir / "rag_proof.sqlite"
        chroma_dir = base_dir / "chroma_vectors"

        store = KnowledgeStore(db_path)
        # 1. Candidate source policy evaluation & registration for SRC-THE-BATCH
        the_batch_source = register_candidate_source_the_batch(store)
        the_batch_policy = evaluate_the_batch_candidate_policy()

        # 2. Upsert permitted resources (Wikipedia CC BY-SA 4.0 and arXiv abstract-permitted)
        res_backprop = store.upsert_resource(
            source_id="SRC-WIKIPEDIA",
            canonical_url="https://en.wikipedia.org/wiki/Backpropagation",
            title="Backpropagation",
            authors="Wikipedia contributors",
            published_at="2026-03-01",
            resource_type="Encyclopedia article",
            abstract_or_description=(
                "Backpropagation is a gradient estimation method used to train neural network "
                "models. It computes the gradient of a loss function with respect to the weights "
                "of the network by applying the chain rule backward from the output layer to "
                "earlier hidden layers."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="CC_BY_SA_4_0",
            license_spdx="CC-BY-SA-4.0",
            permitted_storage_scope="WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED",
            permits_full_text_storage=1,
            permits_verbatim_excerpts=1,
            legal_uncertainty_status="CLEARED_OPEN_LICENSE",
        )
        res_dropout = store.upsert_resource(
            source_id="SRC-ARXIV",
            canonical_url="https://arxiv.org/abs/1207.0580",
            title="Improving neural networks by preventing co-adaptation of feature detectors",
            authors="Geoffrey E. Hinton, Nitish Srivastava, Alex Krizhevsky, Ilya Sutskever, Ruslan R. Salakhutdinov",
            published_at="2012-07-03",
            resource_type="Preprint paper",
            abstract_or_description=(
                "When a large feedforward neural network is trained on a small training set, "
                "it typically performs poorly on held-out test data. This overfitting is "
                "greatly reduced by randomly omitting half of the feature detectors on each "
                "training case, a technique called dropout that prevents complex co-adaptations."
            ),
            collection_decision="COLLECTED",
            verification_status="HTTP_200_VERIFIED",
            license_status="ARXIV_NON_EXCLUSIVE_DISTRIB",
            permitted_storage_scope="ARXIV_METADATA_AND_ABSTRACT_PERMITTED",
            permits_full_text_storage=0,
            permits_verbatim_excerpts=0,
            legal_uncertainty_status="CLEARED_METADATA_AND_ABSTRACT",
        )

        # Configure local deterministic embedding + retrieval-only fallback
        config = ProviderConfig.from_env({})
        emb_provider = create_embedding_provider(config)
        gen_provider = create_generation_provider(config)
        vector_store = ChromaPersistentVectorStore(
            chroma_dir,
            collection_name=config.chroma_collection,
        )
        pipeline = LocalRAGPipeline(
            store=store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=gen_provider,
            config=config,
        )

        idx1 = pipeline.index_resource(res_backprop["resource_id"])
        idx2 = pipeline.index_resource(res_dropout["resource_id"])
        # Re-index unchanged resource to verify idempotency (no duplicate chunks)
        idx1_repeat = pipeline.index_resource(res_backprop["resource_id"])

        # Query 1: Retrieval-only fallback when no live LLM credentials are provided
        fallback_resp = pipeline.answer_question(
            "How does backpropagation compute gradients in a neural network?"
        )

        # Query 2: Insufficient evidence refusal for out-of-domain topic
        insufficient_resp = pipeline.answer_question(
            "What fermentation temperature is best for sourdough rye bread starter?"
        )

        # Query 3: Configured generation provider with deterministic mocked transport
        def _mock_openai_chat(
            url: str,
            headers: dict[str, str],
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            top_cid = idx1["chunk_ids"][0]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                f"Backpropagation computes the gradient of the loss function "
                                f"with respect to network weights by applying the chain rule "
                                f"backward from the output layer [{top_cid}]."
                            )
                        }
                    }
                ]
            }

        configured_gen = OpenAIGenerationProvider(
            api_key="sk-test-offline-mock-key",
            model_name="gpt-4o-mini",
            http_post_json=_mock_openai_chat,
        )
        configured_pipeline = LocalRAGPipeline(
            store=store,
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=configured_gen,
            config=ProviderConfig(
                embedding_provider=emb_provider.provider_name,
                embedding_model=emb_provider.model_name,
                embedding_dimensions=emb_provider.dimensions,
                generation_provider="openai",
                generation_model="gpt-4o-mini",
                openai_api_key="sk-test-offline-mock-key",
            ),
        )
        answered_resp = configured_pipeline.answer_question(
            "How does backpropagation compute gradients in a neural network?"
        )

        linked_chunks = pipeline.list_indexed_chunks()
        chroma_count = vector_store.count()
        store.close()

        return {
            "candidate_source_the_batch": {
                "status": the_batch_source["status"],
                "technical_feasibility": the_batch_policy["technical_feasibility"],
                "ingestion_enabled": the_batch_policy["ingestion_enabled"],
            },
            "indexing": {
                "backprop_operation": idx1["operation"],
                "backprop_reindex_operation": idx1_repeat["operation"],
                "dropout_operation": idx2["operation"],
                "sqlite_active_chunks": len(linked_chunks),
                "chroma_vector_count": chroma_count,
            },
            "retrieval_only_fallback": {
                "status": fallback_resp.status,
                "sufficient_evidence": fallback_resp.sufficient_evidence,
                "top_resource_id": fallback_resp.retrieved_chunks[0].resource_id,
                "citation_count": len(fallback_resp.citations),
                "generated_answer": fallback_resp.generated_answer,
            },
            "configured_provider_answer": {
                "status": answered_resp.status,
                "sufficient_evidence": answered_resp.sufficient_evidence,
                "provider_name": (
                    answered_resp.generated_answer.provider_name
                    if answered_resp.generated_answer
                    else None
                ),
                "epistemic_status": (
                    answered_resp.generated_answer.epistemic_status
                    if answered_resp.generated_answer
                    else None
                ),
                "cited_chunk_ids": (
                    answered_resp.generated_answer.cited_chunk_ids
                    if answered_resp.generated_answer
                    else []
                ),
            },
            "insufficient_evidence": {
                "status": insufficient_resp.status,
                "sufficient_evidence": insufficient_resp.sufficient_evidence,
                "citation_count": len(insufficient_resp.citations),
                "generated_answer": insufficient_resp.generated_answer,
            },
        }
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint to run the local RAG pipeline proof."""
    parser = argparse.ArgumentParser(
        description="Local-first AI Research & Learning RAG pipeline proof."
    )
    parser.add_argument(
        "--proof",
        action="store_true",
        help="Run the self-contained local end-to-end RAG proof and print JSON results.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.proof:
        summary = run_end_to_end_rag_proof()
        print(json.dumps(summary, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
