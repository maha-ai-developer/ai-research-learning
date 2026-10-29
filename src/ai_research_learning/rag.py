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
import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
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

DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"
DEFAULT_RUNTIME_DB_PATH = REPO_ROOT / "data" / "runtime" / "task012_pilot.sqlite"
DEFAULT_CHROMA_DIR = REPO_ROOT / "data" / "runtime" / "chroma"
DEFAULT_VERTEX_PROJECT = "kerala-lottery-intel-dev"
DEFAULT_VERTEX_LOCATION = "global"
DEFAULT_ADC_CREDENTIALS_PATH = (
    Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
)
DEFAULT_CHROMA_COLLECTION = "ai_research_knowledge_chunks_v1"
DEFAULT_CHUNK_SIZE_CHARS = 600
DEFAULT_CHUNK_OVERLAP_CHARS = 100
DEFAULT_MIN_SIMILARITY_SCORE = 0.22
DEFAULT_TOP_K = 4

VALID_EMBEDDING_PROVIDERS = ("local_deterministic", "openai", "gemini")
VALID_GENERATION_PROVIDERS = ("retrieval_only", "openai", "anthropic", "gemini")
VALID_CLI_OUTPUT_FORMATS = ("markdown", "text", "json")


def parse_dotenv_content(content: str) -> dict[str, str]:
    """Parse `.env` file content into a dictionary without mutating `os.environ`."""
    parsed: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        if val and val[0] in ("'", '"'):
            quote_char = val[0]
            end_quote_idx = val.find(quote_char, 1)
            if end_quote_idx != -1:
                val = val[1:end_quote_idx]
        else:
            # Strip inline unquoted comments preceded by whitespace
            comment_idx = val.find(" #")
            if comment_idx != -1:
                val = val[:comment_idx].rstrip()
        parsed[key] = val
    return parsed


def load_dotenv_file(dotenv_path: Path | str | None = DEFAULT_DOTENV_PATH) -> dict[str, str]:
    """Read key-value pairs from a local `.env` file if present without mutating `os.environ`."""
    if dotenv_path is None:
        return {}
    path = Path(dotenv_path)
    if not path.is_file():
        return {}
    return parse_dotenv_content(path.read_text(encoding="utf-8"))


_SECRET_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"ya29\.[A-Za-z0-9._-]+"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
)


def redact_secrets(text: str, secrets: Sequence[str | None]) -> str:
    """Replace any non-empty secret values or common token patterns in `text` with `[REDACTED]`."""
    redacted = text
    for secret in secrets:
        if not secret:
            continue
        candidate = secret.strip()
        if candidate and candidate in redacted:
            redacted = redacted.replace(candidate, "[REDACTED]")
    for pattern in _SECRET_TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


AdcTokenProviderFn = Callable[[], str]
_ADC_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def resolve_adc_credentials_path(
    adc_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the Google Cloud Application Default Credentials (ADC) JSON path."""
    if adc_path is not None:
        return Path(adc_path)
    env = environ if environ is not None else os.environ
    env_override = (env.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if env_override:
        return Path(env_override)
    return DEFAULT_ADC_CREDENTIALS_PATH


def has_vertex_adc_credentials(
    adc_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True if a readable Google Cloud ADC credentials file is present."""
    path = resolve_adc_credentials_path(adc_path=adc_path, environ=environ)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and bool(data.get("type"))
    except Exception:
        return False


def get_vertex_adc_access_token(
    adc_path: Path | str | None = None,
    *,
    force_refresh: bool = False,
) -> str:
    """Obtain an OAuth2 access token from Google Cloud Application Default Credentials (ADC).

    Does not use or require `GEMINI_API_KEY`. Redacts all credential/token material on error.
    """
    path = resolve_adc_credentials_path(adc_path=adc_path)
    cache_key = str(path)
    now = time.monotonic()
    if not force_refresh and cache_key in _ADC_TOKEN_CACHE:
        cached_token, expires_at = _ADC_TOKEN_CACHE[cache_key]
        if cached_token and now < expires_at:
            return cached_token

    secret_values: list[str] = []
    if path.is_file():
        try:
            creds = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(creds, dict) and creds.get("type") == "authorized_user":
                client_id = str(creds.get("client_id") or "").strip()
                client_secret = str(creds.get("client_secret") or "").strip()
                refresh_token = str(creds.get("refresh_token") or "").strip()
                token_uri = str(
                    creds.get("token_uri") or "https://oauth2.googleapis.com/token"
                ).strip()
                secret_values.extend([s for s in (client_secret, refresh_token) if s])
                if client_id and client_secret and refresh_token:
                    import requests

                    resp = requests.post(
                        token_uri,
                        data={
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "refresh_token": refresh_token,
                            "grant_type": "refresh_token",
                        },
                        timeout=15,
                    )
                    resp.raise_for_status()
                    token_data = resp.json()
                    access_token = str(token_data.get("access_token") or "").strip()
                    expires_in = float(token_data.get("expires_in") or 3600.0)
                    if access_token:
                        _ADC_TOKEN_CACHE[cache_key] = (
                            access_token,
                            now + max(60.0, expires_in - 120.0),
                        )
                        return access_token
        except Exception:
            # Fall back to gcloud ADC CLI below if direct token exchange fails
            pass

    try:
        sub_env = dict(os.environ)
        existing_metrics = sub_env.get("CLOUDSDK_METRICS_ENVIRONMENT", "").strip()
        sub_env["CLOUDSDK_METRICS_ENVIRONMENT"] = (
            f"{existing_metrics} datacloud.antigravity".strip()
        )
        proc = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=sub_env,
        )
        if proc.returncode == 0:
            token = (proc.stdout or "").strip()
            if token:
                _ADC_TOKEN_CACHE[cache_key] = (token, now + 1800.0)
                return token
        stderr_msg = redact_secrets((proc.stderr or "").strip(), secret_values)
        raise ProviderNotConfiguredError(
            "Vertex AI ADC authentication failed: unable to obtain an access token "
            f"from Application Default Credentials ({stderr_msg or 'no ADC credentials found'})."
        )
    except ProviderNotConfiguredError:
        raise
    except Exception as exc:
        raise ProviderNotConfiguredError(
            redact_secrets(
                f"Vertex AI ADC authentication failed: {exc}",
                secret_values,
            )
        ) from None


def build_vertex_ai_model_endpoint(
    *,
    project_id: str,
    location: str,
    model_name: str,
    method: str,
) -> str:
    """Build the official Vertex AI REST endpoint URL for a Google publisher model."""
    clean_project = project_id.strip()
    clean_location = (location or DEFAULT_VERTEX_LOCATION).strip()
    clean_model = (
        model_name[len("models/") :]
        if model_name.startswith("models/")
        else model_name.strip()
    )
    if clean_location == "global":
        host = "https://aiplatform.googleapis.com"
    elif clean_location in ("us", "eu"):
        host = f"https://aiplatform.{clean_location}.rep.googleapis.com"
    else:
        host = f"https://{clean_location}-aiplatform.googleapis.com"
    return (
        f"{host}/v1/projects/{clean_project}/locations/{clean_location}"
        f"/publishers/google/models/{clean_model}:{method}"
    )

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


class EmbeddingIncompatibilityError(RuntimeError):
    """Raised when a Chroma collection contains vectors from an incompatible embedding provider, model, or dimension."""


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
    vertex_project: str | None = DEFAULT_VERTEX_PROJECT
    vertex_location: str = DEFAULT_VERTEX_LOCATION
    use_vertex_ai: bool = True
    chroma_collection: str = DEFAULT_CHROMA_COLLECTION

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dotenv_path: Path | str | None = None,
        load_dotenv: bool | None = None,
    ) -> "ProviderConfig":
        """Load provider configuration from `.env` and an environment mapping.

        Precedence rules:
        - Explicit variables in `environ` (or `os.environ` when `environ is None`)
          always override values loaded from `.env`.
        - When `environ is None`, `.env` is loaded by default (unless `load_dotenv=False`)
          from `dotenv_path` or `DEFAULT_DOTENV_PATH` without mutating `os.environ`.
        - When an explicit `environ` mapping is passed (e.g. in isolated unit tests),
          `.env` is only loaded if `dotenv_path is not None` or `load_dotenv=True`.
        """
        if environ is None:
            should_load_dotenv = True if load_dotenv is None else bool(load_dotenv)
            base_env: Mapping[str, str] = os.environ
        else:
            should_load_dotenv = (
                bool(load_dotenv) if load_dotenv is not None else (dotenv_path is not None)
            )
            base_env = environ

        dotenv_values = (
            load_dotenv_file(DEFAULT_DOTENV_PATH if dotenv_path is None else dotenv_path)
            if should_load_dotenv
            else {}
        )
        env: dict[str, str] = {**dotenv_values, **dict(base_env)}

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
            "gemini": "gemini-embedding-2",
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
            "anthropic": "claude-haiku-4-5-20251001",
            "gemini": "gemini-3.8-flash",
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

        vertex_project = (
            _clean_key("AI_LEARNING_VERTEX_PROJECT")
            or _clean_key("GOOGLE_CLOUD_PROJECT")
            or _clean_key("GCLOUD_PROJECT")
            or DEFAULT_VERTEX_PROJECT
        )
        vertex_location = (
            _clean_key("AI_LEARNING_VERTEX_LOCATION")
            or _clean_key("GOOGLE_CLOUD_LOCATION")
            or DEFAULT_VERTEX_LOCATION
        )
        raw_use_vertex = (env.get("AI_LEARNING_GEMINI_USE_VERTEX_AI") or "").strip().lower()
        if raw_use_vertex in ("0", "false", "no", "off"):
            use_vertex_ai = False
        elif raw_use_vertex in ("1", "true", "yes", "on"):
            use_vertex_ai = True
        else:
            use_vertex_ai = True

        return cls(
            embedding_provider=emb_provider,
            embedding_model=emb_model,
            embedding_dimensions=emb_dims,
            generation_provider=gen_provider,
            generation_model=gen_model,
            openai_api_key=_clean_key("OPENAI_API_KEY"),
            anthropic_api_key=_clean_key("ANTHROPIC_API_KEY"),
            gemini_api_key=_clean_key("GEMINI_API_KEY"),
            vertex_project=vertex_project,
            vertex_location=vertex_location,
            use_vertex_ai=use_vertex_ai,
            chroma_collection=collection,
        )

    def credential_status(self) -> dict[str, str]:
        """Return safe `CONFIGURED` / `MISSING` status for each supported provider key."""
        return {
            "GEMINI_API_KEY": "CONFIGURED" if self.gemini_api_key else "MISSING",
            "ANTHROPIC_API_KEY": "CONFIGURED" if self.anthropic_api_key else "MISSING",
            "OPENAI_API_KEY": "CONFIGURED" if self.openai_api_key else "MISSING",
        }

    def vertex_status(self) -> dict[str, Any]:
        """Return safe Vertex AI + ADC status without exposing any credential material."""
        adc_available = has_vertex_adc_credentials()
        return {
            "use_vertex_ai": self.use_vertex_ai,
            "vertex_project": self.vertex_project,
            "vertex_location": self.vertex_location,
            "adc_credentials": "CONFIGURED" if adc_available else "MISSING",
            "requires_gemini_api_key": not self.use_vertex_ai,
        }

    def has_embedding_credentials(self) -> bool:
        """Return True if the selected `embedding_provider` has required credentials."""
        if self.embedding_provider == "local_deterministic":
            return True
        if self.embedding_provider == "openai":
            return bool(self.openai_api_key)
        if self.embedding_provider == "gemini":
            if self.use_vertex_ai:
                return bool(self.vertex_project) and has_vertex_adc_credentials()
            return bool(self.gemini_api_key)
        return False

    def has_generation_credentials(self) -> bool:
        """Return True if the selected `generation_provider` is an LLM with configured credentials."""
        if self.generation_provider == "openai":
            return bool(self.openai_api_key)
        if self.generation_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.generation_provider == "gemini":
            if self.use_vertex_ai:
                return bool(self.vertex_project) and has_vertex_adc_credentials()
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
            "credential_status": self.credential_status(),
            "vertex_ai": self.vertex_status(),
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
            "METADATA_AND_ABSTRACT",
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
            "ARXIV_METADATA_AND_ABSTRACT_PERMITTED",
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
    """Perform an HTTPS JSON POST using `requests` with strict secret redaction on error."""
    import requests

    secret_values: list[str] = []
    for k, v in (headers or {}).items():
        if not v:
            continue
        if k.lower() in ("authorization", "x-api-key", "x-goog-api-key") or "key" in k.lower() or "token" in k.lower():
            secret_values.append(str(v))
            if str(v).lower().startswith("bearer "):
                secret_values.append(str(v)[7:].strip())

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        body_excerpt = ""
        resp_obj = getattr(exc, "response", None)
        if resp_obj is not None:
            try:
                err_json = resp_obj.json()
                if isinstance(err_json, dict) and isinstance(err_json.get("error"), dict):
                    body_excerpt = str(err_json["error"].get("message") or err_json["error"])
                else:
                    body_excerpt = str(err_json)
            except Exception:
                body_excerpt = (resp_obj.text or "")[:300]
        raw_message = (
            f"Provider HTTP request to {url} failed"
            + (f" (status={status_code})" if status_code is not None else "")
            + (f": {body_excerpt}" if body_excerpt else f": {exc}")
        )
        raise RuntimeError(redact_secrets(raw_message, secret_values)) from None


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
        for idx, vec in enumerate(vectors):
            if len(vec) != self._dimensions:
                raise RuntimeError(
                    f"OpenAI embedding vector {idx} has dimension {len(vec)}, expected {self._dimensions}."
                )
        return vectors

    def embed_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return self.embed_texts([query])[0]


def _extract_vertex_embedding_values(data: dict[str, Any]) -> list[list[float]]:
    """Extract embedding float vectors from Vertex AI (`:embedContent` / `:predict`) or Gemini API responses."""
    if "embedding" in data and isinstance(data["embedding"], dict):
        return [[float(x) for x in data["embedding"].get("values", [])]]
    if "embeddings" in data and isinstance(data["embeddings"], list):
        return [[float(x) for x in item.get("values", [])] for item in data["embeddings"]]
    if "predictions" in data and isinstance(data["predictions"], list):
        vectors: list[list[float]] = []
        for pred in data["predictions"]:
            if isinstance(pred, dict) and isinstance(pred.get("embeddings"), dict):
                vectors.append([float(x) for x in pred["embeddings"].get("values", [])])
        return vectors
    return []


class GeminiEmbeddingProvider:
    """Interchangeable Google Gemini embedding provider supporting Vertex AI (ADC) and Developer API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-embedding-2",
        dimensions: int = 128,
        vertex_project: str | None = None,
        vertex_location: str = DEFAULT_VERTEX_LOCATION,
        use_vertex_ai: bool | None = None,
        adc_token_provider: AdcTokenProviderFn | None = None,
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._use_vertex_ai = (
            bool(use_vertex_ai)
            if use_vertex_ai is not None
            else bool(vertex_project and vertex_project.strip())
        )
        self._api_key = (
            None
            if self._use_vertex_ai
            else (api_key.strip() if api_key and api_key.strip() else None)
        )
        self._model_name = model_name
        self._dimensions = int(dimensions)
        self._vertex_project = (
            (vertex_project or DEFAULT_VERTEX_PROJECT).strip()
            if self._use_vertex_ai
            else None
        )
        self._vertex_location = (vertex_location or DEFAULT_VERTEX_LOCATION).strip()
        self._adc_token_provider = adc_token_provider or get_vertex_adc_access_token
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

    @property
    def use_vertex_ai(self) -> bool:
        return self._use_vertex_ai

    @property
    def vertex_project(self) -> str | None:
        return self._vertex_project

    @property
    def vertex_location(self) -> str:
        return self._vertex_location

    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "GeminiEmbeddingProvider requires Vertex AI ADC configuration or GEMINI_API_KEY."
            )
        return self._api_key

    def _require_vertex_token(self) -> str:
        if not self._vertex_project:
            raise ProviderNotConfiguredError(
                "GeminiEmbeddingProvider in Vertex AI mode requires vertex_project to be set."
            )
        token = (self._adc_token_provider() or "").strip()
        if not token:
            raise ProviderNotConfiguredError(
                "GeminiEmbeddingProvider in Vertex AI mode requires a valid ADC access token."
            )
        return token

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._use_vertex_ai:
            access_token = self._require_vertex_token()
            assert self._vertex_project is not None
            clean_model = (
                self._model_name[len("models/") :]
                if self._model_name.startswith("models/")
                else self._model_name.strip()
            )
            headers = {
                "Authorization": f"Bearer {access_token}",
                "x-goog-user-project": self._vertex_project,
                "Content-Type": "application/json",
            }
            vectors: list[list[float]] = []
            use_embed_content = clean_model.startswith("gemini-embedding-2")
            method = "embedContent" if use_embed_content else "predict"
            url = build_vertex_ai_model_endpoint(
                project_id=self._vertex_project,
                location=self._vertex_location,
                model_name=clean_model,
                method=method,
            )
            for t in texts:
                if use_embed_content:
                    payload: dict[str, Any] = {
                        "content": {"role": "user", "parts": [{"text": t}]},
                        "outputDimensionality": self._dimensions,
                    }
                else:
                    payload = {
                        "instances": [{"content": t}],
                        "parameters": {
                            "autoTruncate": True,
                            "outputDimensionality": self._dimensions,
                        },
                    }
                data = self._http_post(url, headers, payload)
                extracted = _extract_vertex_embedding_values(data)
                if not extracted:
                    raise RuntimeError(
                        f"Vertex AI Gemini embedding response for {clean_model!r} returned no vector values."
                    )
                vectors.append(extracted[0])
        else:
            api_key = self._require_key()
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
            vectors = _extract_vertex_embedding_values(data)

        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Gemini embedding response returned {len(vectors)} vectors for {len(texts)} inputs."
            )
        for idx, vec in enumerate(vectors):
            if len(vec) != self._dimensions:
                raise RuntimeError(
                    f"Gemini embedding vector {idx} has dimension {len(vec)}, expected {self._dimensions}."
                )
        return vectors

    def embed_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return self.embed_texts([query])[0]


def create_embedding_provider(
    config: ProviderConfig,
    *,
    adc_token_provider: AdcTokenProviderFn | None = None,
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
        if config.use_vertex_ai:
            return GeminiEmbeddingProvider(
                api_key=None,
                model_name=config.embedding_model,
                dimensions=config.embedding_dimensions,
                vertex_project=config.vertex_project,
                vertex_location=config.vertex_location,
                use_vertex_ai=True,
                adc_token_provider=adc_token_provider,
                http_post_json=http_post_json,
            )
        return GeminiEmbeddingProvider(
            api_key=config.gemini_api_key,
            model_name=config.embedding_model,
            dimensions=config.embedding_dimensions,
            use_vertex_ai=False,
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


_CHUNK_ID_RE = re.compile(r"chk_[0-9a-f]{16}")

_ABSTENTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:the\s+)?(?:retrieved|indexed|provided)\s+"
        r"(?:sources?|chunks?|passages?|context|documents?|excerpts?|abstracts?)\s+"
        r"(?:do\s+not|don't|does\s+not|doesn't|cannot|can't)\s+"
        r"(?:contain|provide|include|specify|mention|state|have|support)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:not\s+enough|insufficient|no\s+sufficient|lack(?:s|ing)?(?:\s+sufficient)?)\s+"
        r"(?:evidence|information|context|details?|data)\s+"
        r"(?:in|from|within|across)\s+(?:the\s+)?(?:retrieved|indexed|provided)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:there\s+is\s+no|there\s+are\s+no)\s+"
        r"(?:sufficient\s+)?(?:evidence|information|mention)\s+"
        r"(?:in|from|within)\s+(?:the\s+)?(?:retrieved|indexed|provided)\s+"
        r"(?:sources?|chunks?|passages?|context|documents?|abstracts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cannot|can't|unable\s+to)\s+"
        r"(?:be\s+answered|answer(?:\s+this|\s+the)?(?:\s+question)?|determine|verify)\s+"
        r"(?:from|using|based\s+on)\s+(?:the\s+)?(?:retrieved|indexed|provided)\s+"
        r"(?:sources?|chunks?|passages?|context|documents?|abstracts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*insufficient[\s_]+evidence\b",
        re.IGNORECASE,
    ),
)


def _extract_cited_chunk_ids_from_text(
    answer_text: str,
    valid_chunk_ids: Sequence[str],
) -> list[str]:
    """Extract cited `chk_...` identifiers from `answer_text` and filter to valid retrieved IDs."""
    valid_set = set(valid_chunk_ids)
    mentioned = _CHUNK_ID_RE.findall(answer_text)
    verified: list[str] = []
    for cid in mentioned:
        if cid in valid_set and cid not in verified:
            verified.append(cid)
    return verified


def _sanitize_answer_citations(
    answer_text: str,
    valid_chunk_ids: Sequence[str],
) -> tuple[str, list[str]]:
    """Strip fabricated `chk_<16-hex>` markers from `answer_text` and return `(cleaned_text, verified_ids)`.

    Only chunk IDs present in `valid_chunk_ids` (the retrieved chunks for the query)
    are preserved in both the rendered answer text and the returned `cited_chunk_ids` list.
    """
    valid_set = set(valid_chunk_ids)
    verified_ids = _extract_cited_chunk_ids_from_text(answer_text, valid_chunk_ids)

    def _replace_group(match: re.Match[str]) -> str:
        open_ch = match.group(1)
        inner = match.group(2)
        close_ch = match.group(3)
        cids = _CHUNK_ID_RE.findall(inner)
        if not cids:
            return match.group(0)
        kept = [cid for cid in cids if cid in valid_set]
        if not kept:
            return ""
        dedup_kept: list[str] = []
        for cid in kept:
            if cid not in dedup_kept:
                dedup_kept.append(cid)
        if open_ch == "[" and close_ch == "]":
            return " ".join(f"[{cid}]" for cid in dedup_kept)
        return f"{open_ch}{', '.join(dedup_kept)}{close_ch}"

    cleaned = re.sub(
        r"(\[|\()([^\]\)\n]*chk_[0-9a-f]{16}[^\]\)\n]*)(\]|\))",
        _replace_group,
        answer_text,
    )

    def _replace_bare(match: re.Match[str]) -> str:
        cid = match.group(0)
        return cid if cid in valid_set else ""

    cleaned = _CHUNK_ID_RE.sub(_replace_bare, cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in cleaned.split("\n")]
    cleaned = "\n".join(lines).strip()
    return cleaned, verified_ids


def _is_generator_abstention(answer_text: str) -> bool:
    """Return True when the generator explicitly states that retrieved sources lack sufficient evidence."""
    if not answer_text or not answer_text.strip():
        return True
    without_cids = re.sub(r"[\[\(]?\bchk_[0-9a-f]{16}\b[\]\)]?", " ", answer_text)
    normalized = " ".join(without_cids.strip().split())
    return any(pattern.search(normalized) for pattern in _ABSTENTION_PATTERNS)


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
        cleaned_text, cited_ids = _sanitize_answer_citations(content, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=cleaned_text,
            cited_chunk_ids=cited_ids,
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


class AnthropicGenerationProvider:
    """Interchangeable Anthropic Messages API provider for source-grounded RAG."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model_name: str = "claude-haiku-4-5-20251001",
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
        cleaned_text, cited_ids = _sanitize_answer_citations(content, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=cleaned_text,
            cited_chunk_ids=cited_ids,
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


class GeminiGenerationProvider:
    """Interchangeable Google Gemini `generateContent` provider supporting Vertex AI (ADC) and Developer API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-3.8-flash",
        vertex_project: str | None = None,
        vertex_location: str = DEFAULT_VERTEX_LOCATION,
        use_vertex_ai: bool | None = None,
        adc_token_provider: AdcTokenProviderFn | None = None,
        http_post_json: HttpPostJsonFn | None = None,
    ) -> None:
        self._use_vertex_ai = (
            bool(use_vertex_ai)
            if use_vertex_ai is not None
            else bool(vertex_project and vertex_project.strip())
        )
        self._api_key = (
            None
            if self._use_vertex_ai
            else (api_key.strip() if api_key and api_key.strip() else None)
        )
        self._model_name = model_name
        self._vertex_project = (
            (vertex_project or DEFAULT_VERTEX_PROJECT).strip()
            if self._use_vertex_ai
            else None
        )
        self._vertex_location = (vertex_location or DEFAULT_VERTEX_LOCATION).strip()
        self._adc_token_provider = adc_token_provider or get_vertex_adc_access_token
        self._http_post = http_post_json or _default_http_post_json

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def use_vertex_ai(self) -> bool:
        return self._use_vertex_ai

    @property
    def vertex_project(self) -> str | None:
        return self._vertex_project

    @property
    def vertex_location(self) -> str:
        return self._vertex_location

    def is_configured(self) -> bool:
        if self._use_vertex_ai:
            if not self._vertex_project:
                return False
            if self._adc_token_provider is not get_vertex_adc_access_token:
                return True
            return has_vertex_adc_credentials()
        return bool(self._api_key)

    def _require_vertex_token(self) -> str:
        if not self._vertex_project:
            raise ProviderNotConfiguredError(
                "GeminiGenerationProvider in Vertex AI mode requires vertex_project to be set."
            )
        token = (self._adc_token_provider() or "").strip()
        if not token:
            raise ProviderNotConfiguredError(
                "GeminiGenerationProvider in Vertex AI mode requires a valid ADC access token."
            )
        return token

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload:
        system_prompt, user_prompt = build_grounded_rag_prompt(prompt_input)
        clean_model = (
            self._model_name[len("models/") :]
            if self._model_name.startswith("models/")
            else self._model_name.strip()
        )
        if self._use_vertex_ai:
            access_token = self._require_vertex_token()
            assert self._vertex_project is not None
            url = build_vertex_ai_model_endpoint(
                project_id=self._vertex_project,
                location=self._vertex_location,
                model_name=clean_model,
                method="generateContent",
            )
            headers = {
                "Authorization": f"Bearer {access_token}",
                "x-goog-user-project": self._vertex_project,
                "Content-Type": "application/json",
            }
            payload: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            }
            if not clean_model.startswith("gemini-3.8"):
                payload["generationConfig"] = {"temperature": 0.0}
        else:
            if not self._api_key:
                raise ProviderNotConfiguredError(
                    "GeminiGenerationProvider requires Vertex AI ADC configuration or GEMINI_API_KEY."
                )
            model_path = f"models/{clean_model}"
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
        non_thought_parts = [
            str(p.get("text", ""))
            for p in parts
            if isinstance(p, dict) and not p.get("thought") and "text" in p
        ]
        if non_thought_parts:
            content = "\n".join(non_thought_parts).strip()
        else:
            content = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
        valid_ids = [c.chunk_id for c in prompt_input.retrieved_chunks]
        cleaned_text, cited_ids = _sanitize_answer_citations(content, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=cleaned_text,
            cited_chunk_ids=cited_ids,
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


def create_generation_provider(
    config: ProviderConfig,
    *,
    adc_token_provider: AdcTokenProviderFn | None = None,
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
        if config.use_vertex_ai:
            return GeminiGenerationProvider(
                api_key=None,
                model_name=config.generation_model,
                vertex_project=config.vertex_project,
                vertex_location=config.vertex_location,
                use_vertex_ai=True,
                adc_token_provider=adc_token_provider,
                http_post_json=http_post_json,
            )
        return GeminiGenerationProvider(
            api_key=config.gemini_api_key,
            model_name=config.generation_model,
            use_vertex_ai=False,
            http_post_json=http_post_json,
        )
    raise ValueError(f"Unsupported generation_provider: {config.generation_provider!r}")


# ---------------------------------------------------------------------------
# Persistent Local Chroma Vector Store & SQLite Linkage
# ---------------------------------------------------------------------------


def ensure_knowledge_chunks_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the `knowledge_chunks` table and indexes in SQLite."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_chunks'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript(KNOWLEDGE_CHUNKS_SCHEMA_SQL)


class ReadOnlySQLiteStore:
    """Read-only SQLite connection wrapper enforcing `mode=ro` and `PRAGMA query_only = ON`."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"SQLite knowledge store database not found at {self.db_path}"
            )
        uri = f"file:{self.db_path.resolve()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON;")
        required_tables = {"sources", "resources", "knowledge_chunks"}
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        present_tables = {str(r["name"]) for r in rows}
        missing = sorted(required_tables - present_tables)
        if missing:
            self.conn.close()
            raise RuntimeError(
                f"SQLite database {self.db_path} is missing required RAG tables: {', '.join(missing)}"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ReadOnlySQLiteStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class ChromaPersistentVectorStore:
    """Local persistent Chroma vector store backed by `chromadb.PersistentClient`."""

    def __init__(
        self,
        persist_directory: Path | str,
        *,
        collection_name: str = DEFAULT_CHROMA_COLLECTION,
        create_if_missing: bool = True,
    ) -> None:
        self.persist_directory = Path(persist_directory)
        self.collection_name = collection_name
        if not create_if_missing:
            if not self.persist_directory.is_dir():
                raise FileNotFoundError(
                    f"Chroma persistence directory not found at {self.persist_directory}"
                )
            self._client = chromadb.PersistentClient(path=str(self.persist_directory))
            try:
                self._collection = self._client.get_collection(name=self.collection_name)
            except Exception as exc:
                raise FileNotFoundError(
                    f"Chroma collection {self.collection_name!r} not found in {self.persist_directory}"
                ) from exc
        else:
            self.persist_directory.mkdir(parents=True, exist_ok=True)
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

    def peek_sample_record(self) -> dict[str, Any] | None:
        """Return metadata and vector dimensionality of one stored item in the collection, if non-empty."""
        if self.count() == 0:
            return None
        res = self._collection.get(limit=1, include=["metadatas", "embeddings"])
        ids = res.get("ids") or []
        if not ids:
            return None
        metas = res.get("metadatas") or []
        embs = res.get("embeddings")
        meta = dict(metas[0] or {}) if metas else {}
        emb_dim: int | None = None
        if embs is not None and len(embs) > 0 and embs[0] is not None:
            emb_dim = len(embs[0])
        return {
            "chunk_id": str(ids[0]),
            "metadata": meta,
            "embedding_dimensions": emb_dim,
        }


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

    def verify_collection_embedding_compatibility(self) -> None:
        """Ensure `self.vector_store.collection_name` never mixes incompatible embedding models or dimensions."""
        ensure_knowledge_chunks_schema(self.store.conn)
        expected_provider = self.embedding_provider.provider_name
        expected_model = self.embedding_provider.model_name
        expected_dims = int(self.embedding_provider.dimensions)
        collection_name = self.vector_store.collection_name

        rows = self.store.conn.execute(
            """
            SELECT DISTINCT embedding_provider, embedding_model, embedding_dimensions
              FROM knowledge_chunks
             WHERE vector_collection = ? AND lifecycle_state = 'ACTIVE'
            """,
            (collection_name,),
        ).fetchall()
        for row in rows:
            actual_provider = str(row["embedding_provider"])
            actual_model = str(row["embedding_model"])
            actual_dims = int(row["embedding_dimensions"])
            if (actual_provider, actual_model, actual_dims) != (
                expected_provider,
                expected_model,
                expected_dims,
            ):
                raise EmbeddingIncompatibilityError(
                    f"Chroma collection {collection_name!r} already contains active vectors "
                    f"indexed with ({actual_provider}/{actual_model}@{actual_dims}d), "
                    f"which is incompatible with the configured embedding provider "
                    f"({expected_provider}/{expected_model}@{expected_dims}d). "
                    f"Use a separate AI_LEARNING_CHROMA_COLLECTION or explicitly rebuild the collection "
                    f"to prevent silently mixing incompatible vector spaces."
                )

        sample = self.vector_store.peek_sample_record()
        if sample is not None:
            sample_dims = sample.get("embedding_dimensions")
            if sample_dims is not None and int(sample_dims) != expected_dims:
                raise EmbeddingIncompatibilityError(
                    f"Chroma collection {collection_name!r} contains {sample_dims}-dimensional vectors, "
                    f"which is incompatible with configured dimension {expected_dims} for "
                    f"({expected_provider}/{expected_model})."
                )
            meta = sample.get("metadata") or {}
            meta_provider = meta.get("embedding_provider")
            meta_model = meta.get("embedding_model")
            meta_dims = meta.get("embedding_dimensions")
            if (
                (meta_provider is not None and str(meta_provider) != expected_provider)
                or (meta_model is not None and str(meta_model) != expected_model)
                or (meta_dims is not None and int(meta_dims) != expected_dims)
            ):
                raise EmbeddingIncompatibilityError(
                    f"Chroma collection {collection_name!r} metadata records embedding signature "
                    f"({meta_provider}/{meta_model}@{meta_dims}d), which is incompatible with "
                    f"({expected_provider}/{expected_model}@{expected_dims}d)."
                )

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
        self.verify_collection_embedding_compatibility()
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
                    "embedding_provider": self.embedding_provider.provider_name,
                    "embedding_model": self.embedding_provider.model_name,
                    "embedding_dimensions": int(self.embedding_provider.dimensions),
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
        self.verify_collection_embedding_compatibility()
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

            # Penalize hits with zero stem/concept overlap unless neural cosine similarity is very high
            if overlap_count == 0 and (
                self.embedding_provider.provider_name == "local_deterministic"
                or cosine_sim < 0.72
            ):
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
        strict_generation: bool = False,
    ) -> RAGResponse:
        """Execute the source-grounded RAG workflow for `question`.

        1. Retrieve relevant chunks from Chroma + SQLite.
        2. If retrieved evidence is empty or below threshold, return `insufficient_evidence`
           without calling the generation model or fabricating citations.
        3. If credentials for the configured `GenerationProvider` are available, generate
           a grounded answer, verify cited `chunk_id`s against retrieved chunks, and return
           `status="answered"` with `generated_answer` separated from `retrieved_chunks`.
        4. If credentials are not configured (`retrieval_only` or missing API key), return
           `status="retrieval_only_fallback"` with the retrieved chunks and citations
           (unless `strict_generation=True` and a non-`retrieval_only` provider was selected,
           in which case `ProviderNotConfiguredError` is raised).
        """
        if (
            strict_generation
            and self.generation_provider.provider_name != "retrieval_only"
            and not self.generation_provider.is_configured()
        ):
            raise ProviderNotConfiguredError(
                f"Generation provider {self.generation_provider.provider_name!r} "
                f"(model {self.generation_provider.model_name!r}) has no configured credentials."
            )

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
            if strict_generation and self.generation_provider.provider_name != "retrieval_only":
                raise
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

        # Sanitize answer text and verify citations against retrieved chunks (never allow fabricated chunk_ids)
        valid_chunk_ids = [c.chunk_id for c in retrieved]
        cleaned_answer_text, text_cited_ids = _sanitize_answer_citations(
            payload.answer_text, valid_chunk_ids
        )
        verified_ids: list[str] = []
        for cid in [*payload.cited_chunk_ids, *text_cited_ids]:
            if cid in by_chunk_id and cid not in verified_ids:
                verified_ids.append(cid)

        # Post-retrieval abstention: generator explicitly states evidence is insufficient,
        # or produced no valid citations for an answer that requires cited evidence.
        if _is_generator_abstention(cleaned_answer_text):
            return RAGResponse(
                question=question,
                status="insufficient_evidence",
                sufficient_evidence=False,
                retrieved_chunks=retrieved,
                citations=[],
                generated_answer=None,
                insufficient_evidence_reason=(
                    "Retrieved source chunks were inspected, but they do not contain "
                    "sufficient evidence to answer the specific question."
                ),
                fallback_reason=None,
                provider_summary=provider_summary,
            )

        if not verified_ids:
            return RAGResponse(
                question=question,
                status="insufficient_evidence",
                sufficient_evidence=False,
                retrieved_chunks=retrieved,
                citations=[],
                generated_answer=None,
                insufficient_evidence_reason=(
                    "Generated response did not include any valid citations to the retrieved "
                    "source chunks; abstaining to preserve citation integrity."
                ),
                fallback_reason=None,
                provider_summary=provider_summary,
            )

        citations = _build_citations(verified_ids)

        generated = GeneratedAnswer(
            answer_text=cleaned_answer_text,
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


# ---------------------------------------------------------------------------
# Research Question CLI & Evidence-First Learning Workflow (Task 032)
# ---------------------------------------------------------------------------


def resolve_cli_provider_config(
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_dimensions: int | None = None,
    generation_provider: str | None = None,
    generation_model: str | None = None,
    chroma_collection: str | None = None,
    vertex_project: str | None = None,
    vertex_location: str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | str | None = None,
    load_dotenv: bool | None = None,
) -> ProviderConfig:
    """Resolve `ProviderConfig` from `.env` + environment with optional CLI flag overrides."""
    base_env = os.environ if environ is None else environ
    should_load_dotenv = (environ is None) if load_dotenv is None else bool(load_dotenv)
    dotenv_values = (
        load_dotenv_file(DEFAULT_DOTENV_PATH if dotenv_path is None else dotenv_path)
        if should_load_dotenv
        else {}
    )
    merged: dict[str, str] = {**dotenv_values, **dict(base_env)}

    if embedding_provider is not None:
        merged["AI_LEARNING_EMBEDDING_PROVIDER"] = embedding_provider
        if embedding_model is None:
            merged.pop("AI_LEARNING_EMBEDDING_MODEL", None)
    if embedding_model is not None:
        merged["AI_LEARNING_EMBEDDING_MODEL"] = embedding_model
    if embedding_dimensions is not None:
        merged["AI_LEARNING_EMBEDDING_DIMENSIONS"] = str(embedding_dimensions)

    if generation_provider is not None:
        merged["AI_LEARNING_GENERATION_PROVIDER"] = generation_provider
        if generation_model is None:
            merged.pop("AI_LEARNING_GENERATION_MODEL", None)
    if generation_model is not None:
        merged["AI_LEARNING_GENERATION_MODEL"] = generation_model

    if chroma_collection is not None:
        merged["AI_LEARNING_CHROMA_COLLECTION"] = chroma_collection
    if vertex_project is not None:
        merged["AI_LEARNING_VERTEX_PROJECT"] = vertex_project
    if vertex_location is not None:
        merged["AI_LEARNING_VERTEX_LOCATION"] = vertex_location

    return ProviderConfig.from_env(merged, load_dotenv=False)


def ask_research_question(
    question: str,
    *,
    db_path: Path | str = DEFAULT_RUNTIME_DB_PATH,
    chroma_dir: Path | str = DEFAULT_CHROMA_DIR,
    config: ProviderConfig | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_similarity_score: float = DEFAULT_MIN_SIMILARITY_SCORE,
    source_ids: Sequence[str] | None = None,
    strict_generation: bool = True,
    adc_token_provider: AdcTokenProviderFn | None = None,
    http_post_json: HttpPostJsonFn | None = None,
) -> RAGResponse:
    """Execute a read-only, evidence-grounded research query against SQLite and Chroma.

    Opens the SQLite knowledge store in strict read-only mode (`mode=ro`, `PRAGMA query_only = ON`)
    and opens the existing Chroma collection without creating or mutating any files or vectors.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Research question must be a non-empty string.")
    if int(top_k) <= 0:
        raise ValueError("top_k must be a positive integer.")
    if not (0.0 <= float(min_similarity_score) <= 1.0):
        raise ValueError("min_similarity_score must be between 0.0 and 1.0.")

    resolved_config = config or ProviderConfig.from_env()
    emb_provider = create_embedding_provider(
        resolved_config,
        adc_token_provider=adc_token_provider,
        http_post_json=http_post_json,
    )
    gen_provider = create_generation_provider(
        resolved_config,
        adc_token_provider=adc_token_provider,
        http_post_json=http_post_json,
    )
    if (
        strict_generation
        and resolved_config.generation_provider != "retrieval_only"
        and not gen_provider.is_configured()
    ):
        raise ProviderNotConfiguredError(
            f"Generation provider {resolved_config.generation_provider!r} "
            f"(model {resolved_config.generation_model!r}) is not configured with required credentials."
        )

    with ReadOnlySQLiteStore(db_path) as ro_store:
        vector_store = ChromaPersistentVectorStore(
            chroma_dir,
            collection_name=resolved_config.chroma_collection,
            create_if_missing=False,
        )
        pipeline = LocalRAGPipeline(
            store=ro_store,  # type: ignore[arg-type]
            vector_store=vector_store,
            embedding_provider=emb_provider,
            generation_provider=gen_provider,
            config=resolved_config,
        )
        return pipeline.answer_question(
            question.strip(),
            top_k=int(top_k),
            min_similarity_score=float(min_similarity_score),
            source_ids=source_ids,
            strict_generation=strict_generation,
        )


def format_rag_response(
    response: RAGResponse,
    *,
    output_format: str = "markdown",
) -> str:
    """Format a `RAGResponse` as readable Markdown, plain text, or JSON.

    Explicitly separates:
    1. Model-generated interpretation (`GENERATED_INTERPRETATION`) or abstention notice
    2. Verified source citations (`SourceCitation` with resource/version/chunk IDs)
    3. Retrieved source material (`PRIMARY_SOURCE_TEXT` / `AUTHORIZED_ABSTRACT_OR_DESCRIPTION`)
    """
    fmt = output_format.strip().lower()
    if fmt not in VALID_CLI_OUTPUT_FORMATS:
        raise ValueError(
            f"Unsupported output_format {output_format!r}; expected one of {VALID_CLI_OUTPUT_FORMATS}"
        )

    if fmt == "json":
        return json.dumps(response.to_dict(), indent=2)

    prov = response.provider_summary or {}
    emb_info = (
        f"{prov.get('embedding_provider', 'unknown')}/"
        f"{prov.get('embedding_model', 'unknown')}@"
        f"{prov.get('embedding_dimensions', '?')}d"
    )
    gen_info = (
        f"{prov.get('generation_provider', 'unknown')}/"
        f"{prov.get('generation_model', 'unknown')}"
    )
    collection_info = str(prov.get("chroma_collection", DEFAULT_CHROMA_COLLECTION))

    if fmt == "markdown":
        lines: list[str] = [
            "# Research Question & Evidence-Grounded Response",
            "",
            f"- **Question:** {response.question}",
            f"- **Pipeline Status:** `{response.status}`",
            f"- **Sufficient Evidence:** `{response.sufficient_evidence}`",
            f"- **Embedding Configuration:** `{emb_info}` (collection: `{collection_info}`)",
            f"- **Generation Configuration:** `{gen_info}`",
            "",
            "## 1. Generated Interpretation (`GENERATED_INTERPRETATION`)",
            "",
        ]
        if response.status == "insufficient_evidence":
            lines.extend(
                [
                    "> **ABSTAINED — INSUFFICIENT EVIDENCE**",
                    f"> {response.insufficient_evidence_reason or 'Insufficient evidence in local knowledge store.'}",
                    "",
                    "No answer was generated and no citations were fabricated.",
                ]
            )
        elif response.generated_answer is not None:
            ga = response.generated_answer
            cited_str = ", ".join(f"`{cid}`" for cid in ga.cited_chunk_ids) or "none"
            lines.extend(
                [
                    f"*Epistemic Layer: `{ga.epistemic_status}` | "
                    f"Synthesized by `{ga.provider_name}/{ga.model_name}` | "
                    f"Cited Chunks: {cited_str}*",
                    "",
                    ga.answer_text,
                ]
            )
        else:
            lines.extend(
                [
                    "> **RETRIEVAL-ONLY MODE (No LLM Synthesis Generated)**",
                    f"> {response.fallback_reason or 'Returning verified source chunks and citations only.'}",
                ]
            )

        lines.extend(
            [
                "",
                "## 2. Verified Source Citations (`SourceCitation`)",
                "",
            ]
        )
        if not response.citations:
            lines.append("None (pipeline abstained due to insufficient evidence).")
        else:
            for idx, cit in enumerate(response.citations, start=1):
                license_label = (
                    f"{cit.license_status} ({cit.license_spdx})"
                    if cit.license_spdx
                    else cit.license_status
                )
                lines.append(f"{idx}. **`[{cit.chunk_id}]` {cit.title}**")
                lines.append(f"   - **Canonical URL:** {cit.canonical_url}")
                lines.append(
                    f"   - **Identifiers:** `chunk_id={cit.chunk_id}` | "
                    f"`resource_id={cit.resource_id}` | `version_id={cit.version_id}` | "
                    f"`source_id={cit.source_id}`"
                )
                lines.append(
                    f"   - **Provenance & Offsets:** `epistemic_layer={cit.epistemic_layer}` | "
                    f"`content_origin={cit.content_origin}` | `chars={cit.char_start}..{cit.char_end}`"
                )
                lines.append(f"   - **License:** `{license_label}`")
                if cit.authors or cit.published_at:
                    meta_parts: list[str] = []
                    if cit.authors:
                        meta_parts.append(f"Authors: {cit.authors}")
                    if cit.published_at:
                        meta_parts.append(f"Published: {cit.published_at}")
                    lines.append(f"   - **Publication Metadata:** {' | '.join(meta_parts)}")

        lines.extend(
            [
                "",
                "## 3. Retrieved Source Material (`PRIMARY_SOURCE_TEXT` / `AUTHORIZED_ABSTRACT_OR_DESCRIPTION`)",
                "",
            ]
        )
        if not response.retrieved_chunks:
            lines.append("None.")
        else:
            lines.append(
                "*Verbatim/abstract source chunks retrieved from the local knowledge store "
                "(kept strictly separate from model-generated interpretation):*"
            )
            lines.append("")
            for idx, chunk in enumerate(response.retrieved_chunks, start=1):
                lines.append(
                    f"### Chunk {idx}: `[{chunk.chunk_id}]` — {chunk.title} "
                    f"(similarity: `{chunk.similarity_score:.4f}`)"
                )
                lines.append(
                    f"- **Source Provenance:** `epistemic_layer={chunk.epistemic_layer}` | "
                    f"`content_origin={chunk.content_origin}` | "
                    f"`resource_id={chunk.resource_id}` | `version_id={chunk.version_id}` | "
                    f"`chars={chunk.char_start}..{chunk.char_end}` | "
                    f"`sha256={chunk.content_hash[:16]}...`"
                )
                lines.append(f"- **Canonical URL:** {chunk.canonical_url}")
                lines.append("")
                for text_line in chunk.chunk_text.splitlines():
                    lines.append(f"> {text_line}")
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    # Plain-text format ("text")
    text_lines: list[str] = [
        "========================================================================",
        "RESEARCH QUESTION & EVIDENCE-GROUNDED RESPONSE",
        "========================================================================",
        f"Question:                {response.question}",
        f"Pipeline Status:         {response.status}",
        f"Sufficient Evidence:     {response.sufficient_evidence}",
        f"Embedding Configuration: {emb_info} (collection: {collection_info})",
        f"Generation Config:       {gen_info}",
        "",
        "------------------------------------------------------------------------",
        "1. GENERATED INTERPRETATION (GENERATED_INTERPRETATION)",
        "------------------------------------------------------------------------",
    ]
    if response.status == "insufficient_evidence":
        text_lines.extend(
            [
                "ABSTAINED — INSUFFICIENT EVIDENCE",
                f"Reason: {response.insufficient_evidence_reason or 'Insufficient evidence in local knowledge store.'}",
                "No answer was generated and no citations were fabricated.",
            ]
        )
    elif response.generated_answer is not None:
        ga = response.generated_answer
        cited_str = ", ".join(ga.cited_chunk_ids) or "none"
        text_lines.extend(
            [
                f"Epistemic Layer: {ga.epistemic_status}",
                f"Provider/Model:  {ga.provider_name}/{ga.model_name}",
                f"Cited Chunks:    {cited_str}",
                "",
                ga.answer_text,
            ]
        )
    else:
        text_lines.extend(
            [
                "RETRIEVAL-ONLY MODE (No LLM Synthesis Generated)",
                f"Reason: {response.fallback_reason or 'Returning verified source chunks and citations only.'}",
            ]
        )

    text_lines.extend(
        [
            "",
            "------------------------------------------------------------------------",
            "2. VERIFIED SOURCE CITATIONS (SourceCitation)",
            "------------------------------------------------------------------------",
        ]
    )
    if not response.citations:
        text_lines.append("None (pipeline abstained due to insufficient evidence).")
    else:
        for idx, cit in enumerate(response.citations, start=1):
            text_lines.append(f"[{idx}] [{cit.chunk_id}] {cit.title}")
            text_lines.append(f"    Canonical URL: {cit.canonical_url}")
            text_lines.append(
                f"    Identifiers:   chunk_id={cit.chunk_id} | resource_id={cit.resource_id} | "
                f"version_id={cit.version_id} | source_id={cit.source_id}"
            )
            text_lines.append(
                f"    Provenance:    epistemic_layer={cit.epistemic_layer} | "
                f"content_origin={cit.content_origin} | chars={cit.char_start}..{cit.char_end} | "
                f"license={cit.license_status}"
            )

    text_lines.extend(
        [
            "",
            "------------------------------------------------------------------------",
            "3. RETRIEVED SOURCE MATERIAL (PRIMARY_SOURCE_TEXT / AUTHORIZED_ABSTRACT)",
            "------------------------------------------------------------------------",
        ]
    )
    if not response.retrieved_chunks:
        text_lines.append("None.")
    else:
        for idx, chunk in enumerate(response.retrieved_chunks, start=1):
            text_lines.append(
                f"[{idx}] [{chunk.chunk_id}] {chunk.title} "
                f"(similarity={chunk.similarity_score:.4f}, layer={chunk.epistemic_layer}, "
                f"chars={chunk.char_start}..{chunk.char_end})"
            )
            text_lines.append(f"    URL:  {chunk.canonical_url}")
            text_lines.append(f"    Text: {chunk.chunk_text}")
            text_lines.append("")

    return "\n".join(text_lines).rstrip() + "\n"


def build_cli_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser for the research question CLI."""
    epilog = (
        "Examples:\n"
        "  # Ask a research question using Vertex AI Gemini (ADC on kerala-lottery-intel-dev):\n"
        "  python3 -m src.ai_research_learning.rag \\\n"
        '    "How does backpropagation compute gradients in a neural network?" \\\n'
        "    --generation-provider gemini\n\n"
        "  # Run in offline retrieval-only mode (returns verified chunks & citations without LLM call):\n"
        "  python3 -m src.ai_research_learning.rag \\\n"
        '    "How does dropout prevent overfitting in neural networks?" \\\n'
        "    --generation-provider retrieval_only\n\n"
        "  # Emit structured JSON or plain text output:\n"
        "  python3 -m src.ai_research_learning.rag \\\n"
        '    --question "What is the Transformer architecture?" \\\n'
        "    --generation-provider gemini --format json\n\n"
        "  # Run the self-contained offline end-to-end RAG proof:\n"
        "  python3 -m src.ai_research_learning.rag --proof\n"
    )
    parser = argparse.ArgumentParser(
        prog="python3 -m src.ai_research_learning.rag",
        description=(
            "Evidence-first AI research question CLI over the local SQLite knowledge store "
            "and persistent Chroma vector index. Returns source-grounded answers with "
            "traceable chunk/resource citations or abstains when evidence is insufficient."
        ),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        help="Research question to ask against the local SQLite + Chroma knowledge store.",
    )
    parser.add_argument(
        "-q",
        "--question",
        dest="question_flag",
        default=None,
        help="Research question to ask (alternative to positional question argument).",
    )
    parser.add_argument(
        "--proof",
        action="store_true",
        help="Run the self-contained local end-to-end RAG proof and print JSON results.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "Path to the SQLite knowledge store database "
            "(default: AI_LEARNING_DB_PATH or data/runtime/task012_pilot.sqlite)."
        ),
    )
    parser.add_argument(
        "--chroma-dir",
        type=Path,
        default=None,
        help=(
            "Path to the persistent Chroma vector directory "
            "(default: AI_LEARNING_CHROMA_DIR or data/runtime/chroma)."
        ),
    )
    parser.add_argument(
        "--collection",
        default=None,
        help=(
            "Chroma collection name override "
            f"(default: AI_LEARNING_CHROMA_COLLECTION or {DEFAULT_CHROMA_COLLECTION})."
        ),
    )
    parser.add_argument(
        "--embedding-provider",
        choices=VALID_EMBEDDING_PROVIDERS,
        default=None,
        help="Embedding provider override (local_deterministic, gemini, openai).",
    )
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model override.",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=None,
        help="Embedding vector dimensionality override.",
    )
    parser.add_argument(
        "--generation-provider",
        choices=VALID_GENERATION_PROVIDERS,
        default=None,
        help="Generation provider override (retrieval_only, gemini, openai, anthropic).",
    )
    parser.add_argument(
        "--generation-model",
        default=None,
        help="Generation model override.",
    )
    parser.add_argument(
        "--vertex-project",
        default=None,
        help=f"Google Cloud Vertex AI project ID override (default: {DEFAULT_VERTEX_PROJECT}).",
    )
    parser.add_argument(
        "--vertex-location",
        default=None,
        help=f"Google Cloud Vertex AI location override (default: {DEFAULT_VERTEX_LOCATION}).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Maximum number of retrieved chunks to consider (default: {DEFAULT_TOP_K}).",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=DEFAULT_MIN_SIMILARITY_SCORE,
        help=(
            "Minimum effective similarity score in [0.0, 1.0] required for retrieval "
            f"(default: {DEFAULT_MIN_SIMILARITY_SCORE})."
        ),
    )
    parser.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        default=None,
        help="Filter retrieval to specific canonical source_id(s) (e.g. SRC-WIKIPEDIA, SRC-ARXIV).",
    )
    parser.add_argument(
        "--format",
        choices=VALID_CLI_OUTPUT_FORMATS,
        default="markdown",
        dest="output_format",
        help="Output display format: markdown (default), text, or json.",
    )
    parser.add_argument(
        "--allow-retrieval-fallback",
        action="store_true",
        help=(
            "Fall back to retrieval-only output instead of returning a non-zero exit code "
            "when an LLM generation provider lacks configured credentials."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | str | None = None,
    load_dotenv: bool | None = None,
    adc_token_provider: AdcTokenProviderFn | None = None,
    http_post_json: HttpPostJsonFn | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """CLI entrypoint for asking evidence-grounded research questions or running `--proof`.

    Exit codes:
    - `0`: Successful answer, retrieval-only result, legitimate abstention (`insufficient_evidence`),
           `--proof`, or `--help`.
    - `1`: Operational/runtime error (missing database/collection, network/HTTP error, SQLite error).
    - `2`: CLI usage error, invalid configuration, missing provider credentials, or
           `EmbeddingIncompatibilityError`.
    """
    out_stream = stdout if stdout is not None else sys.stdout
    err_stream = stderr if stderr is not None else sys.stderr
    parser = build_cli_parser()

    try:
        with contextlib.redirect_stdout(out_stream), contextlib.redirect_stderr(err_stream):
            args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            return 0
        return int(code) if isinstance(code, int) else 2

    if args.proof:
        summary = run_end_to_end_rag_proof()
        out_stream.write(json.dumps(summary, indent=2) + "\n")
        return 0

    pos_q = args.question
    flag_q = args.question_flag
    if pos_q is not None and flag_q is not None and pos_q.strip() != flag_q.strip():
        err_stream.write(
            "ERROR [ValueError]: Specify the research question either as a positional "
            "argument or via --question, not both.\n"
        )
        return 2

    raw_question = flag_q if flag_q is not None else pos_q
    if raw_question is None:
        parser.print_help(file=err_stream)
        err_stream.write(
            "\nERROR: A research question (or --proof) is required.\n"
        )
        return 2

    if not raw_question.strip():
        err_stream.write("ERROR [ValueError]: Research question must be a non-empty string.\n")
        return 2

    known_secrets: list[str | None] = []
    try:
        base_env = os.environ if environ is None else environ
        should_load_dotenv = (environ is None) if load_dotenv is None else bool(load_dotenv)
        dotenv_values = (
            load_dotenv_file(DEFAULT_DOTENV_PATH if dotenv_path is None else dotenv_path)
            if should_load_dotenv
            else {}
        )
        merged_env = {**dotenv_values, **dict(base_env)}
        for secret_key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            if merged_env.get(secret_key):
                known_secrets.append(merged_env[secret_key])

        config = resolve_cli_provider_config(
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_dimensions=args.embedding_dimensions,
            generation_provider=args.generation_provider,
            generation_model=args.generation_model,
            chroma_collection=args.collection,
            vertex_project=args.vertex_project,
            vertex_location=args.vertex_location,
            environ=environ,
            dotenv_path=dotenv_path,
            load_dotenv=load_dotenv,
        )
        known_secrets.extend(
            [config.gemini_api_key, config.anthropic_api_key, config.openai_api_key]
        )

        db_path = (
            args.db
            if args.db is not None
            else Path(
                merged_env.get("AI_LEARNING_DB_PATH", "").strip()
                or str(DEFAULT_RUNTIME_DB_PATH)
            )
        )
        chroma_dir = (
            args.chroma_dir
            if args.chroma_dir is not None
            else Path(
                merged_env.get("AI_LEARNING_CHROMA_DIR", "").strip()
                or str(DEFAULT_CHROMA_DIR)
            )
        )

        response = ask_research_question(
            raw_question,
            db_path=db_path,
            chroma_dir=chroma_dir,
            config=config,
            top_k=args.top_k,
            min_similarity_score=args.min_similarity,
            source_ids=args.source_ids,
            strict_generation=not bool(args.allow_retrieval_fallback),
            adc_token_provider=adc_token_provider,
            http_post_json=http_post_json,
        )
        rendered = format_rag_response(response, output_format=args.output_format)
        out_stream.write(redact_secrets(rendered, known_secrets))
        return 0
    except (
        ValueError,
        ProviderNotConfiguredError,
        EmbeddingIncompatibilityError,
    ) as exc:
        safe_msg = redact_secrets(str(exc), known_secrets)
        err_stream.write(f"ERROR [{type(exc).__name__}]: {safe_msg}\n")
        return 2
    except (FileNotFoundError, sqlite3.Error, RuntimeError, Exception) as exc:
        safe_msg = redact_secrets(str(exc), known_secrets)
        err_stream.write(f"ERROR [{type(exc).__name__}]: {safe_msg}\n")
        return 1


# ---------------------------------------------------------------------------
# Offline RAG Evaluation Harness (Task 033)
# ---------------------------------------------------------------------------

DEFAULT_RAG_EVAL_DATASET_PATH = REPO_ROOT / "tests" / "fixtures" / "rag_eval_dataset_v1.json"


@dataclass(frozen=True)
class RAGEvalCaseResult:
    """Per-case evaluation metrics for the offline RAG benchmark harness."""

    case_id: str
    category: str
    question: str
    expected_status: str
    actual_status: str
    expected_abstain: bool
    actual_abstain: bool
    expected_resource_ids: list[str]
    retrieved_resource_ids: list[str]
    expected_chunk_ids: list[str]
    retrieved_chunk_ids: list[str]
    cited_chunk_ids: list[str]
    resource_hit_at_k: float | None
    chunk_recall_at_k: float | None
    reciprocal_rank_at_k: float | None
    citation_precision: float | None
    citation_recall: float | None
    fabricated_citation_count: int
    citation_metadata_resolved: bool
    required_concept_recall: float | None
    lexical_claim_groundedness_proxy: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "question": self.question,
            "expected_status": self.expected_status,
            "actual_status": self.actual_status,
            "expected_abstain": self.expected_abstain,
            "actual_abstain": self.actual_abstain,
            "expected_resource_ids": list(self.expected_resource_ids),
            "retrieved_resource_ids": list(self.retrieved_resource_ids),
            "expected_chunk_ids": list(self.expected_chunk_ids),
            "retrieved_chunk_ids": list(self.retrieved_chunk_ids),
            "cited_chunk_ids": list(self.cited_chunk_ids),
            "resource_hit_at_k": (
                round(self.resource_hit_at_k, 4) if self.resource_hit_at_k is not None else None
            ),
            "chunk_recall_at_k": (
                round(self.chunk_recall_at_k, 4) if self.chunk_recall_at_k is not None else None
            ),
            "reciprocal_rank_at_k": (
                round(self.reciprocal_rank_at_k, 4)
                if self.reciprocal_rank_at_k is not None
                else None
            ),
            "citation_precision": (
                round(self.citation_precision, 4) if self.citation_precision is not None else None
            ),
            "citation_recall": (
                round(self.citation_recall, 4) if self.citation_recall is not None else None
            ),
            "fabricated_citation_count": self.fabricated_citation_count,
            "citation_metadata_resolved": self.citation_metadata_resolved,
            "required_concept_recall": (
                round(self.required_concept_recall, 4)
                if self.required_concept_recall is not None
                else None
            ),
            "lexical_claim_groundedness_proxy": (
                round(self.lexical_claim_groundedness_proxy, 4)
                if self.lexical_claim_groundedness_proxy is not None
                else None
            ),
        }


@dataclass(frozen=True)
class RAGEvalSummary:
    """Aggregate benchmark summary for offline RAG evaluation."""

    dataset_version: str
    task_id: str
    total_cases: int
    answerable_cases: int
    abstention_cases: int
    indexed_resource_count: int
    indexed_chunk_count: int
    resource_hit_rate_at_k: float
    chunk_recall_at_k: float
    mrr_at_k: float
    citation_precision: float
    citation_recall: float
    fabricated_citation_rate: float
    citation_metadata_resolution_accuracy: float
    required_concept_recall: float
    abstention_precision: float
    abstention_recall: float
    lexical_claim_groundedness_proxy: float
    case_results: list[RAGEvalCaseResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "task_id": self.task_id,
            "total_cases": self.total_cases,
            "answerable_cases": self.answerable_cases,
            "abstention_cases": self.abstention_cases,
            "indexed_resource_count": self.indexed_resource_count,
            "indexed_chunk_count": self.indexed_chunk_count,
            "metrics": {
                "resource_hit_rate_at_k": round(self.resource_hit_rate_at_k, 4),
                "chunk_recall_at_k": round(self.chunk_recall_at_k, 4),
                "mrr_at_k": round(self.mrr_at_k, 4),
                "citation_precision": round(self.citation_precision, 4),
                "citation_recall": round(self.citation_recall, 4),
                "fabricated_citation_rate": round(self.fabricated_citation_rate, 4),
                "citation_metadata_resolution_accuracy": round(
                    self.citation_metadata_resolution_accuracy, 4
                ),
                "required_concept_recall": round(self.required_concept_recall, 4),
                "abstention_precision": round(self.abstention_precision, 4),
                "abstention_recall": round(self.abstention_recall, 4),
                "lexical_claim_groundedness_proxy": round(
                    self.lexical_claim_groundedness_proxy, 4
                ),
            },
            "case_results": [c.to_dict() for c in self.case_results],
        }


def compute_lexical_claim_groundedness_proxy(
    answer_text: str,
    cited_chunks: Sequence[RetrievedChunk],
) -> float:
    """Compute a deterministic Lexical/Concept-Overlap Claim Groundedness Proxy in [0.0, 1.0].

    NOTE: This metric measures the fraction of content word stems in `answer_text`
    (excluding `[chk_...]` markers) that appear in the cited source chunks' titles or text.
    It is a deterministic lexical/concept-overlap proxy and does not claim to prove
    semantic entailment or scientific truth.
    """
    if not answer_text or not answer_text.strip() or not cited_chunks:
        return 0.0
    stripped_answer = _CHUNK_ID_RE.sub("", answer_text)
    answer_stems = extract_stemmed_content_set(stripped_answer)
    if not answer_stems:
        return 0.0
    evidence_stems: set[str] = set()
    for chunk in cited_chunks:
        evidence_stems |= extract_stemmed_content_set(f"{chunk.title} {chunk.chunk_text}")
    overlap = answer_stems & evidence_stems
    return len(overlap) / len(answer_stems)


def seed_eval_corpus_from_fixture(
    store: KnowledgeStore,
    vector_store: ChromaPersistentVectorStore,
    dataset: Mapping[str, Any],
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Deterministically seed the 6 pilot resources and 18 knowledge chunks into an isolated store."""
    embedder = embedding_provider or DeterministicHashEmbeddingProvider(
        model_name=str(dataset.get("embedding_model", "token-hash-bow-v1")),
        dimensions=int(dataset.get("embedding_dimensions", 128)),
    )
    pipeline = LocalRAGPipeline(
        store=store,
        vector_store=vector_store,
        embedding_provider=embedder,
        generation_provider=RetrievalOnlyFallbackGenerationProvider(),
    )

    indexed_resource_ids: list[str] = []
    indexed_chunk_ids: list[str] = []

    for res_spec in dataset.get("corpus_resources", []):
        upserted = store.upsert_resource(
            source_id=str(res_spec["source_id"]),
            canonical_url=str(res_spec["canonical_url"]),
            title=str(res_spec["title"]),
            resource_type=str(res_spec["resource_type"]),
            authors=res_spec.get("authors"),
            published_at=res_spec.get("published_at"),
            abstract_or_description=str(res_spec["abstract_or_description"]),
            license_status=str(res_spec["license_status"]),
            license_spdx=res_spec.get("license_spdx"),
            permitted_storage_scope=str(res_spec["permitted_storage_scope"]),
            permits_full_text_storage=bool(res_spec.get("permits_full_text_storage")),
        )
        rid = str(upserted["resource_id"])
        target_vid = str(res_spec["current_version_id"])
        actual_vid = str(upserted["current_version_id"])
        if actual_vid != target_vid:
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE resources SET current_version_id = NULL WHERE resource_id = ?",
                    (rid,),
                )
                conn.execute(
                    "UPDATE provenance_events SET version_id = NULL WHERE version_id = ?",
                    (actual_vid,),
                )
                conn.execute(
                    "UPDATE resource_versions SET version_id = ? WHERE version_id = ?",
                    (target_vid, actual_vid),
                )
                conn.execute(
                    "UPDATE resources SET current_version_id = ? WHERE resource_id = ?",
                    (target_vid, rid),
                )
                conn.execute(
                    "UPDATE provenance_events SET version_id = ? WHERE resource_id = ? AND version_id IS NULL",
                    (target_vid, rid),
                )
        idx_res = pipeline.index_resource(rid)
        indexed_resource_ids.append(rid)
        indexed_chunk_ids.extend(idx_res["chunk_ids"])

    return {
        "resource_count": len(indexed_resource_ids),
        "chunk_count": len(indexed_chunk_ids),
        "resource_ids": indexed_resource_ids,
        "chunk_ids": indexed_chunk_ids,
    }


class _FixtureEvalGenerationProvider:
    """Deterministic offline generation provider driven by benchmark case specifications."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text

    @property
    def provider_name(self) -> str:
        return "offline_eval_mock"

    @property
    def model_name(self) -> str:
        return "fixture-grounded-v1"

    def is_configured(self) -> bool:
        return True

    def generate_grounded_answer(self, prompt_input: RAGPromptInput) -> GeneratedAnswerPayload:
        valid_ids = [c.chunk_id for c in prompt_input.retrieved_chunks]
        cleaned_text, cited_ids = _sanitize_answer_citations(self._response_text, valid_ids)
        return GeneratedAnswerPayload(
            answer_text=cleaned_text,
            cited_chunk_ids=cited_ids,
            provider_name=self.provider_name,
            model_name=self.model_name,
        )


def _verify_citation_metadata_against_sqlite(
    store: KnowledgeStore,
    citation: SourceCitation,
) -> bool:
    """Verify that a `SourceCitation` resolves to exact SQLite provenance and slice boundaries."""
    row = store.conn.execute(
        """
        SELECT kc.*,
               r.canonical_url,
               r.title,
               r.authors,
               r.published_at,
               r.license_status,
               r.license_spdx,
               r.current_version_id,
               r.lifecycle_state AS resource_lifecycle_state,
               rv.abstract_or_description
          FROM knowledge_chunks kc
          JOIN resources r ON r.resource_id = kc.resource_id
          JOIN resource_versions rv ON rv.version_id = kc.version_id
         WHERE kc.chunk_id = ?
        """,
        (citation.chunk_id,),
    ).fetchone()
    if row is None:
        return False
    if row["lifecycle_state"] != "ACTIVE" or row["resource_lifecycle_state"] != "ACTIVE":
        return False
    if (
        citation.resource_id != str(row["resource_id"])
        or citation.version_id != str(row["version_id"])
        or citation.version_id != str(row["current_version_id"])
        or citation.source_id != str(row["source_id"])
        or citation.canonical_url != str(row["canonical_url"])
        or citation.title != str(row["title"])
        or citation.authors != row["authors"]
        or citation.published_at != row["published_at"]
        or citation.license_status != str(row["license_status"])
        or citation.license_spdx != row["license_spdx"]
        or citation.char_start != int(row["char_start"])
        or citation.char_end != int(row["char_end"])
        or citation.content_origin != str(row["content_origin"])
        or citation.epistemic_layer != str(row["epistemic_layer"])
    ):
        return False

    normalized_doc = normalize_text_for_chunking(
        f"{row['title']}\n\n{row['abstract_or_description'] or ''}"
    )
    expected_slice = normalized_doc[citation.char_start : citation.char_end]
    return expected_slice == str(row["chunk_text"])


def run_offline_rag_evaluation(
    dataset_path: Path | str = DEFAULT_RAG_EVAL_DATASET_PATH,
    *,
    work_dir: Path | str | None = None,
) -> RAGEvalSummary:
    """Run the deterministic offline RAG evaluation benchmark in an isolated SQLite + Chroma store."""
    resolved_dataset_path = Path(dataset_path)
    dataset = json.loads(resolved_dataset_path.read_text(encoding="utf-8"))
    top_k = int(dataset.get("default_top_k", DEFAULT_TOP_K))
    min_sim = float(dataset.get("default_min_similarity_score", DEFAULT_MIN_SIMILARITY_SCORE))

    def _execute_in_dir(base_dir: Path) -> RAGEvalSummary:
        db_path = base_dir / "rag_eval_isolated.sqlite"
        chroma_dir = base_dir / "chroma_eval_isolated"
        store = KnowledgeStore(db_path)
        try:
            ensure_knowledge_chunks_schema(store.conn)
            vector_store = ChromaPersistentVectorStore(
                persist_directory=chroma_dir,
                collection_name="rag_eval_benchmark_v1",
            )
            embedder = DeterministicHashEmbeddingProvider(
                model_name=str(dataset.get("embedding_model", "token-hash-bow-v1")),
                dimensions=int(dataset.get("embedding_dimensions", 128)),
            )
            seeded = seed_eval_corpus_from_fixture(
                store,
                vector_store,
                dataset,
                embedding_provider=embedder,
            )

            case_results: list[RAGEvalCaseResult] = []
            resource_hits: list[float] = []
            chunk_recalls: list[float] = []
            reciprocal_ranks: list[float] = []
            citation_precisions: list[float] = []
            citation_recalls: list[float] = []
            concept_recalls: list[float] = []
            groundedness_proxies: list[float] = []

            total_citations_emitted = 0
            total_fabricated_citations = 0
            total_resolved_citations = 0

            expected_abstain_true = 0
            predicted_abstain_true = 0
            true_positive_abstentions = 0

            for case in dataset.get("cases", []):
                case_id = str(case["case_id"])
                category = str(case["category"])
                question = str(case["question"])
                source_ids = case.get("source_ids")
                expected_abstain = bool(case["expected_abstain"])
                expected_status = str(case["expected_status"])
                expected_res_ids = [str(x) for x in case.get("expected_resource_ids", [])]
                expected_chk_ids = [str(x) for x in case.get("expected_chunk_ids", [])]
                required_concepts = [str(x) for x in case.get("required_concepts", [])]
                mock_text = str(case.get("mock_generator_response", ""))

                gen_provider = _FixtureEvalGenerationProvider(mock_text)
                pipeline = LocalRAGPipeline(
                    store=store,
                    vector_store=vector_store,
                    embedding_provider=embedder,
                    generation_provider=gen_provider,
                )
                resp = pipeline.answer_question(
                    question,
                    top_k=top_k,
                    min_similarity_score=min_sim,
                    source_ids=source_ids,
                )

                retrieved_chk_ids = [c.chunk_id for c in resp.retrieved_chunks]
                retrieved_res_ids: list[str] = []
                for c in resp.retrieved_chunks:
                    if c.resource_id not in retrieved_res_ids:
                        retrieved_res_ids.append(c.resource_id)

                cited_chk_ids = [c.chunk_id for c in resp.citations]
                actual_abstain = resp.status == "insufficient_evidence"

                if expected_abstain:
                    expected_abstain_true += 1
                if actual_abstain:
                    predicted_abstain_true += 1
                if expected_abstain and actual_abstain:
                    true_positive_abstentions += 1

                # Retrieval metrics (for cases with expected retrieval targets)
                res_hit: float | None = None
                if expected_res_ids:
                    res_hit = (
                        1.0
                        if any(rid in retrieved_res_ids for rid in expected_res_ids)
                        else 0.0
                    )
                    resource_hits.append(res_hit)

                chk_recall: float | None = None
                mrr_val: float | None = None
                if expected_chk_ids:
                    matched_chks = [cid for cid in expected_chk_ids if cid in retrieved_chk_ids]
                    chk_recall = len(matched_chks) / len(expected_chk_ids)
                    chunk_recalls.append(chk_recall)

                    rr = 0.0
                    for rank_idx, r_cid in enumerate(retrieved_chk_ids, start=1):
                        if r_cid in expected_chk_ids:
                            rr = 1.0 / rank_idx
                            break
                    mrr_val = rr
                    reciprocal_ranks.append(mrr_val)

                # Fabricated citation check across citations, cited_chunk_ids, and answer_text
                retrieved_set = set(retrieved_chk_ids)
                text_cids = (
                    _CHUNK_ID_RE.findall(resp.generated_answer.answer_text)
                    if resp.generated_answer is not None
                    else []
                )
                ans_cids = (
                    list(resp.generated_answer.cited_chunk_ids)
                    if resp.generated_answer is not None
                    else []
                )
                all_emitted_cids = list(dict.fromkeys([*cited_chk_ids, *ans_cids, *text_cids]))
                fab_count = sum(1 for cid in all_emitted_cids if cid not in retrieved_set)
                total_citations_emitted += len(all_emitted_cids)
                total_fabricated_citations += fab_count

                # Metadata resolution check
                case_meta_ok = True
                for cit in resp.citations:
                    if _verify_citation_metadata_against_sqlite(store, cit):
                        total_resolved_citations += 1
                    else:
                        case_meta_ok = False

                # Answer & citation quality metrics on answerable cases
                cit_prec: float | None = None
                cit_rec: float | None = None
                concept_rec: float | None = None
                ground_proxy: float | None = None

                if not expected_abstain:
                    expected_chk_set = set(expected_chk_ids)
                    if cited_chk_ids:
                        relevant_cited = [
                            cid for cid in cited_chk_ids if cid in expected_chk_set
                        ]
                        cit_prec = len(relevant_cited) / len(cited_chk_ids)
                    else:
                        cit_prec = 0.0
                    citation_precisions.append(cit_prec)

                    if expected_chk_ids:
                        recalled_cited = [
                            cid for cid in expected_chk_ids if cid in cited_chk_ids
                        ]
                        cit_rec = len(recalled_cited) / len(expected_chk_ids)
                        citation_recalls.append(cit_rec)

                    if required_concepts:
                        ans_lower = (
                            resp.generated_answer.answer_text.lower()
                            if resp.generated_answer is not None
                            else ""
                        )
                        found_concepts = [
                            concept
                            for concept in required_concepts
                            if concept.lower() in ans_lower
                        ]
                        concept_rec = len(found_concepts) / len(required_concepts)
                        concept_recalls.append(concept_rec)

                    if resp.generated_answer is not None:
                        cited_chunk_objs = [
                            c for c in resp.retrieved_chunks if c.chunk_id in cited_chk_ids
                        ]
                        ground_proxy = compute_lexical_claim_groundedness_proxy(
                            resp.generated_answer.answer_text,
                            cited_chunk_objs,
                        )
                        groundedness_proxies.append(ground_proxy)

                case_results.append(
                    RAGEvalCaseResult(
                        case_id=case_id,
                        category=category,
                        question=question,
                        expected_status=expected_status,
                        actual_status=resp.status,
                        expected_abstain=expected_abstain,
                        actual_abstain=actual_abstain,
                        expected_resource_ids=expected_res_ids,
                        retrieved_resource_ids=retrieved_res_ids,
                        expected_chunk_ids=expected_chk_ids,
                        retrieved_chunk_ids=retrieved_chk_ids,
                        cited_chunk_ids=cited_chk_ids,
                        resource_hit_at_k=res_hit,
                        chunk_recall_at_k=chk_recall,
                        reciprocal_rank_at_k=mrr_val,
                        citation_precision=cit_prec,
                        citation_recall=cit_rec,
                        fabricated_citation_count=fab_count,
                        citation_metadata_resolved=case_meta_ok,
                        required_concept_recall=concept_rec,
                        lexical_claim_groundedness_proxy=ground_proxy,
                    )
                )

            avg_res_hit = sum(resource_hits) / max(len(resource_hits), 1)
            avg_chk_recall = sum(chunk_recalls) / max(len(chunk_recalls), 1)
            avg_mrr = sum(reciprocal_ranks) / max(len(reciprocal_ranks), 1)
            avg_cit_prec = sum(citation_precisions) / max(len(citation_precisions), 1)
            avg_cit_rec = sum(citation_recalls) / max(len(citation_recalls), 1)
            fab_rate = (
                total_fabricated_citations / total_citations_emitted
                if total_citations_emitted > 0
                else 0.0
            )
            meta_acc = (
                total_resolved_citations / total_citations_emitted
                if total_citations_emitted > 0
                else 1.0
            )
            avg_concept_rec = sum(concept_recalls) / max(len(concept_recalls), 1)
            abst_prec = (
                true_positive_abstentions / predicted_abstain_true
                if predicted_abstain_true > 0
                else 0.0
            )
            abst_rec = (
                true_positive_abstentions / expected_abstain_true
                if expected_abstain_true > 0
                else 0.0
            )
            avg_ground_proxy = sum(groundedness_proxies) / max(len(groundedness_proxies), 1)

            return RAGEvalSummary(
                dataset_version=str(dataset.get("dataset_version", "1.0")),
                task_id=str(dataset.get("task_id", "Task-033")),
                total_cases=len(case_results),
                answerable_cases=len(case_results) - expected_abstain_true,
                abstention_cases=expected_abstain_true,
                indexed_resource_count=int(seeded["resource_count"]),
                indexed_chunk_count=int(seeded["chunk_count"]),
                resource_hit_rate_at_k=avg_res_hit,
                chunk_recall_at_k=avg_chk_recall,
                mrr_at_k=avg_mrr,
                citation_precision=avg_cit_prec,
                citation_recall=avg_cit_rec,
                fabricated_citation_rate=fab_rate,
                citation_metadata_resolution_accuracy=meta_acc,
                required_concept_recall=avg_concept_rec,
                abstention_precision=abst_prec,
                abstention_recall=abst_rec,
                lexical_claim_groundedness_proxy=avg_ground_proxy,
                case_results=case_results,
            )
        finally:
            store.close()

    if work_dir is not None:
        base_path = Path(work_dir)
        base_path.mkdir(parents=True, exist_ok=True)
        return _execute_in_dir(base_path)

    with tempfile.TemporaryDirectory(prefix="task033_rag_eval_") as tmp_dir:
        return _execute_in_dir(Path(tmp_dir))


if __name__ == "__main__":
    raise SystemExit(main())
