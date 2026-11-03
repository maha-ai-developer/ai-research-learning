# Architecture: source-aware AI learning

## End-to-end flow

```text
Sources
  ├─ Hinton website
  ├─ Bengio website
  ├─ Wikipedia / MediaWiki API (later)
  └─ arXiv API (later)
       ↓
Source connectors and policy checks
       ↓
Fetch metadata → parse/normalize → deduplicate → record provenance
       ↓
Document metadata + permitted text/chunks + source citations
       ↓
Keyword search + vector retrieval (later)
       ↓
Tutor / concept explorer / research timeline / comparison view
```

## Core modules

- **Source registry:** stable source identifiers, allowed domains/path prefixes, source type, policy review state, collection cadence, and reuse notes.
- **Connectors:** one implementation per source. Web scraping and APIs are separate connector types, but they emit the same normalized document schema.
- **Ingestion:** fetch approved resources, parse only expected content, canonicalize URLs, hash metadata, detect changed records, and retain fetch logs.
- **Storage:** SQLite is fine for a local prototype. For a multi-user service, move operational metadata to PostgreSQL and store only content that can be retained under applicable terms. Use a separate table for chunks and embeddings.
- **Retrieval:** combine keyword search and semantic retrieval; every returned chunk should keep `document_id`, source URL, date, and chunk locator.
- **Answering:** LLM answers must distinguish source statements, research evidence, and generated explanations. Surface citations and limitations.
- **UI:** source explorer, concept explorer, question-answering, research timeline, and comparisons.
- **Lab service (later):** run user experiments outside the app/API process, behind an isolated execution service.

## Suggested canonical document fields

`document_id`, `source_id`, `source_person`, `content_type`, `title`, `authors`, `published_at`, `canonical_url`, `external_identifier`, `topics`, `abstract_or_description`, `license_status`, `fetched_at`, `metadata_hash`, `text_storage_status`.

Store chunks in a related table with `document_id`, `chunk_index`, `text`, `page_or_section`, `embedding_model`, and `embedding`. Never discard the original URL or source context when chunking.

## Engineering sequence

1. Stabilize URL inventory and policy evidence.
2. Collect small batches of metadata and inspect failures.
3. Add source-specific parsers only where generic metadata is insufficient.
4. Add API-based Wikipedia and arXiv connectors.
5. Add search, then retrieval-grounded explanations with citations.
6. Add JupyterLab-backed coding lessons as a separate, security-reviewed subsystem.

## Platform architecture and decision records

The repository implements a five-layer **Agent-First AI Learning Platform** (`src/ai_research_learning/platform.py`, `src/ai_research_learning/rag.py`, `src/ai_research_learning/api.py`, `src/ai_research_learning/webapp.py`, and `knowledge_store.py`):

1. **Experience Layer (Unified 6-Area Web Shell & CLI):**
   - Single-page unified Web application (`GET /`, `GET /app` in `webapp.py`) & REST HTTP API (`/api/v1/*` in `api.py`) alongside CLI interfaces (`platform.py`, `rag.py`, `api.py`).
   - **Visual Design System — "Scholarly Paper & Carbon Studio":** Independently researched for extended mathematical reading and technical workspace ergonomics (avoiding high-glare pure white `#FFFFFF` and generic navy/cobalt palettes). Features a persistent Light/Dark mode toggle (`localStorage` key `ai_learning_theme`, `data-theme="light|dark"`):
     - **Light Mode ("Scholarly Paper"):** Warm ivory paper canvas (`#FAF8F5`), alabaster elevated cards (`#FFFFFF` / `#F3EFEA`), warm ink typography (`#18181B`), scholarly terracotta primary accent (`#C84B31`), and deep forest-pine verification accent (`#1E6F5C`).
     - **Dark Mode ("Carbon Studio"):** Deep carbon obsidian canvas (`#121316`), slate-graphite surfaces (`#1A1C20` / `#22252A`), crisp parchment text (`#F4F2ED`), coral-ember primary accent (`#E26D52`), and luminous jade verification accent (`#34A88E`).
   - **Six Primary Application Areas:** `HOME` (Executive Overview & Next Action), `LEARN` (Syllabus Tree, Lesson Reader, Teaching Agent, Exercises & Progress), `RESEARCH` (9-Stage Source Ingestion Control Plane, Epistemic Tiers & Knowledge Search), `AI AGENTS` (6 Logical Learning Agents + Optional Read-Only Platform Intelligence Agent & Run Trace Inspector), `LAB` (Interactive Concept DAG Explorer, Mathematical Intuition Cards & Future Isolated Sandbox Architecture), and `PLATFORM INTELLIGENCE` (9-Domain x 5-Depth Coverage Matrix, Source Governance & Read-Only Diagnostic Q&A).
2. **Controlled Agent Runtime:** `PlatformOrchestrator`, capability-scoped `ToolRegistry`, `ExecutionPolicy`, `WorkflowRunState`, six core logical learning agent capabilities (`RESEARCH`, `KNOWLEDGE_CURATOR`, `LEARNING_PLANNER`, `TEACHING`, `ASSESSMENT`, `LEARNING_PROGRESS`), plus the optional read-only diagnostic `PLATFORM_INTELLIGENCE` agent (`PlatformIntelligenceAgent`).
   - `PlatformIntelligenceAgent` operates strictly on read-only inspection tools (`inspect_platform_context`, `inspect_source_control_plane`, `inspect_syllabus_coverage`, `inspect_curriculum_catalog`, `inspect_learner_progress`), answers the 10 canonical platform diagnostic questions, and refuses mutating, destructive, policy-bypass, or secret-exfiltration instructions (`PLATFORM_MUTATION_FORBIDDEN`).
3. **Platform Services:**
   - `SourceEcosystemService` (five-tier epistemic classification and `SRC-THE-BATCH` discovery-radar governance).
   - `PlatformContextService` (unified read-only platform telemetry, **9-stage Source Ingestion Control Plane** from `1. Registered Sources` to `9. Curriculum-Ready Evidence`, and **9-domain Syllabus Intelligence** across all 5 pedagogical depth levels: `foundational_intuition`, `beginner`, `intermediate`, `advanced`, `research_level`).
   - `ChromaKnowledgeRetriever` / `RAGPipeline` with **Vertex AI / Provider HTTP 429 Resilience** (`parse_retry_after_seconds`, `compute_truncated_backoff_delay` with bounded jitter, `DEFAULT_HTTP_MAX_RETRIES = 3`, secret redaction, and `ProviderRateLimitError` when capacity remains unavailable).
   - `CurriculumManagementService` with deterministic `CurriculumValidator` (prerequisite DAG topological verification and citation integrity) and `LearningProgressStore` (forward-only schema `v9` persistence for lesson state, rubric attempts, teaching interactions, demonstrated objectives, and knowledge gaps).
   - `SourceToKnowledgeIngestionPipeline` (`src/ai_research_learning/ingestion.py`) implementing the policy-aware multi-source ingestion workflow (`SOURCE → AUTHORIZATION → COLLECTION → EXTRACTION → NORMALIZATION → KNOWLEDGE → CHUNKING → EMBEDDING → INDEXING → LEARNING AGENTS`), including `evaluate_resource_ingestion_policy`, `audit_and_reconcile_wikipedia_scope`, `ContentExtractor`, `extract_structured_knowledge`, `ContentAwareChunker`, and 6-check `verify_index_consistency`.
4. **Data Layer:** Read-only runtime SQLite (`data/runtime/task012_pilot.sqlite`), isolated learner progress SQLite (`data/runtime/learning_progress.sqlite`), Chroma vector store (`data/runtime/chroma`), raw transcripts (`data/raw_transcripts/`), and curriculum artifacts (`reports/`).
5. **Infrastructure Layer:** Stage 1 local-first modular monolith designed to evolve through Stages 2–4 toward containerized API services, distributed agent workers, and multi-tenant cloud storage.

See the detailed architecture specifications and Architecture Decision Records (ADRs):
- [System Overview](architecture/system-overview.md)
- [Agent Workflows](architecture/agent-workflows.md)
- [Client and Lab Architecture](architecture/client-and-lab.md)
- [Implementation Roadmap](architecture/implementation-roadmap.md)
- [ADR-0001: Platform Scope and Target Outcomes](architecture/decisions/0001-platform-scope-and-target-outcomes.md)
- [ADR-0002: Modular Monolith and Evolution Path](architecture/decisions/0002-modular-monolith-and-evolution-path.md)
- [ADR-0003: Agent Execution Boundaries](architecture/decisions/0003-agent-execution-boundaries.md)
- [ADR-0004: Data and Storage Strategy](architecture/decisions/0004-data-and-storage-strategy.md)
- [ADR-0005: Source Governance and Compliance](architecture/decisions/0005-source-governance-and-compliance.md)
- [ADR-0006: Evaluation and Mastery Verification](architecture/decisions/0006-evaluation-and-mastery-verification.md)
- [ADR-0007: Local-First RAG, Chroma, and Multi-Provider Architecture](architecture/decisions/0007-local-first-rag-chroma-and-multi-provider-architecture.md)
- [ADR-0008: Agent-First Learning Platform and Curriculum Slice](architecture/decisions/0008-agent-first-learning-platform-and-curriculum-slice.md)
