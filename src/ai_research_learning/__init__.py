"""AI Research & Learning Explorer package."""
from __future__ import annotations

from typing import Any

from .api import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    APIError,
    APIResponse,
    LocalResearchLibraryAPI,
    create_http_server,
)

_RAG_EXPORTS = frozenset(
    {
        "AnthropicGenerationProvider",
        "CANDIDATE_SOURCE_THE_BATCH",
        "ChromaPersistentVectorStore",
        "DEFAULT_CHROMA_COLLECTION",
        "DeterministicHashEmbeddingProvider",
        "EmbeddingProvider",
        "GeminiEmbeddingProvider",
        "GeminiGenerationProvider",
        "GeneratedAnswer",
        "GeneratedAnswerPayload",
        "GenerationProvider",
        "IndexableDocument",
        "LocalRAGPipeline",
        "OpenAIEmbeddingProvider",
        "OpenAIGenerationProvider",
        "PolicyBlockedIndexingError",
        "ProviderConfig",
        "ProviderNotConfiguredError",
        "RAGPromptInput",
        "RAGResponse",
        "RetrievalOnlyFallbackGenerationProvider",
        "RetrievedChunk",
        "SourceCitation",
        "TextSpan",
        "chunk_text",
        "compute_chunk_id",
        "create_embedding_provider",
        "create_generation_provider",
        "ensure_knowledge_chunks_schema",
        "evaluate_the_batch_candidate_policy",
        "extract_indexable_document_from_resource",
        "normalize_text_for_chunking",
        "register_candidate_source_the_batch",
        "run_end_to_end_rag_proof",
    }
)

__all__ = [
    "APIError",
    "APIResponse",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "LocalResearchLibraryAPI",
    "__version__",
    "create_http_server",
    *sorted(_RAG_EXPORTS),
]
__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    if name in _RAG_EXPORTS:
        from . import rag

        return getattr(rag, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
