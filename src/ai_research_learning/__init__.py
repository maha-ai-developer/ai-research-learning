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
        "DEFAULT_ADC_CREDENTIALS_PATH",
        "DEFAULT_CHROMA_COLLECTION",
        "DEFAULT_CHROMA_DIR",
        "DEFAULT_DOTENV_PATH",
        "DEFAULT_HTTP_BACKOFF_MULTIPLIER",
        "DEFAULT_HTTP_INITIAL_BACKOFF_SECONDS",
        "DEFAULT_HTTP_JITTER_RATIO",
        "DEFAULT_HTTP_MAX_BACKOFF_SECONDS",
        "DEFAULT_HTTP_MAX_RETRIES",
        "DEFAULT_RAG_EVAL_DATASET_PATH",
        "DEFAULT_RUNTIME_DB_PATH",
        "DEFAULT_VERTEX_LOCATION",
        "DEFAULT_VERTEX_PROJECT",
        "DeterministicHashEmbeddingProvider",
        "EmbeddingIncompatibilityError",
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
        "ProviderRateLimitError",
        "RAGEvalCaseResult",
        "RAGEvalSummary",
        "RAGPromptInput",
        "RAGResponse",
        "ReadOnlySQLiteStore",
        "RetrievalOnlyFallbackGenerationProvider",
        "RetrievedChunk",
        "SourceCitation",
        "TextSpan",
        "VALID_CLI_OUTPUT_FORMATS",
        "ask_research_question",
        "build_cli_parser",
        "build_vertex_ai_model_endpoint",
        "chunk_text",
        "compute_chunk_id",
        "compute_lexical_claim_groundedness_proxy",
        "compute_truncated_backoff_delay",
        "create_embedding_provider",
        "create_generation_provider",
        "ensure_knowledge_chunks_schema",
        "evaluate_the_batch_candidate_policy",
        "extract_indexable_document_from_resource",
        "format_rag_response",
        "get_vertex_adc_access_token",
        "has_vertex_adc_credentials",
        "load_dotenv_file",
        "normalize_text_for_chunking",
        "parse_dotenv_content",
        "parse_retry_after_seconds",
        "redact_secrets",
        "register_candidate_source_the_batch",
        "resolve_adc_credentials_path",
        "resolve_cli_provider_config",
        "run_end_to_end_rag_proof",
        "run_offline_rag_evaluation",
        "seed_eval_corpus_from_fixture",
    }
)

_PLATFORM_EXPORTS = frozenset(
    {
        "AgentCapability",
        "AgentCapabilitySpec",
        "AssessmentAgent",
        "AssessmentEvaluation",
        "AssessmentExercise",
        "AssessmentQuestionCategory",
        "CANONICAL_SOURCE_ECOSYSTEM_ROLES",
        "CURRICULUM_SCHEMA_VERSION",
        "ConceptGraph",
        "ConceptNode",
        "CriterionEvaluation",
        "CurriculumCitation",
        "CurriculumLesson",
        "CurriculumManagementService",
        "CurriculumPlan",
        "CurriculumProgressSummary",
        "CurriculumStage",
        "CurriculumStatus",
        "CurriculumValidationReport",
        "CurriculumValidator",
        "DEFAULT_CONCEPT_NODES",
        "DEFAULT_LEARNER_ID",
        "EvidenceGap",
        "EvidenceTier",
        "ExecutionPolicy",
        "ExplanationDepth",
        "INGESTION_CONTROL_PLANE_STAGES",
        "LearnerLessonProgress",
        "LearningPlannerAgent",
        "LearningProgressAgent",
        "LearningProgressStore",
        "LessonProgressStatus",
        "MASTERY_ASSESSMENT_DISCLAIMER",
        "MasteryCheckDesign",
        "MisconceptionPattern",
        "NextLessonRecommendation",
        "PlatformContextService",
        "PlatformIntelligenceAgent",
        "PlatformIntelligenceResponse",
        "PlatformOrchestrator",
        "PlatformValidationError",
        "PrerequisiteExplanation",
        "RubricCriterion",
        "SINGLE_EXERCISE_MASTERY_DISCLAIMER",
        "SYLLABUS_DOMAINS",
        "SourceEcosystemService",
        "SourceEcosystemSummary",
        "TEACHING_DEPTH_DISCLAIMER",
        "THE_BATCH_CANONICAL_URL",
        "THE_BATCH_SOURCE_ID",
        "TeachingAgent",
        "TeachingRequest",
        "TeachingResponse",
        "ToolDefinition",
        "ToolRegistry",
        "WorkflowRunState",
        "WorkflowStatus",
        "WorkflowStepRecord",
        "build_default_concept_graph",
        "format_curriculum_plan",
        "get_logical_agent_catalog",
        "validate_learning_goal",
    }
)

_WEBAPP_EXPORTS = frozenset({"DESIGN_SYSTEM_METADATA", "render_learning_web_app_html"})

_INGESTION_EXPORTS = frozenset(
    {
        "CanonicalResourceType",
        "ChunkDraft",
        "ChunkEpistemicLayer",
        "ChunkingStrategy",
        "ContentAwareChunker",
        "ContentExtractor",
        "EpistemicCategory",
        "ExtractionFormat",
        "INGESTION_SCHEMA_VERSION",
        "IndexConsistencyReport",
        "IngestionPolicyViolationError",
        "NormalizedContentOrigin",
        "NormalizedDocument",
        "NormalizedSection",
        "PolicyEvaluationResult",
        "STRUCTURED_KNOWLEDGE_ELEMENT_TYPES",
        "SourceToKnowledgeIngestionPipeline",
        "StructuredKnowledgeElement",
        "attribution_for_license",
        "audit_and_reconcile_wikipedia_scope",
        "classify_canonical_resource_type",
        "classify_epistemic_category",
        "determine_chunk_epistemic_layer",
        "ensure_ingestion_schema",
        "evaluate_resource_ingestion_policy",
        "extract_structured_knowledge",
        "is_foundational_evidence_eligible",
        "run_reproducible_ingestion_demo",
        "verify_index_consistency",
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
    *sorted(_RAG_EXPORTS | _PLATFORM_EXPORTS | _WEBAPP_EXPORTS | _INGESTION_EXPORTS),
]
__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    if name in _RAG_EXPORTS:
        from . import rag

        return getattr(rag, name)
    if name in _PLATFORM_EXPORTS:
        from . import platform

        return getattr(platform, name)
    if name in _WEBAPP_EXPORTS:
        from . import webapp

        return getattr(webapp, name)
    if name in _INGESTION_EXPORTS:
        from . import ingestion

        return getattr(ingestion, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
