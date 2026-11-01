# ADR-0008: Agent-First AI Learning Platform Architecture and First Curriculum Planning Vertical Slice

* **Status:** Accepted
* **Date:** 2026-10-30
* **Task:** Task 034
* **Deciders:** Engineering Architecture & AI Platform Team
* **Related ADRs:** [ADR-0001](0001-platform-scope-and-target-outcomes.md), [ADR-0002](0002-modular-monolith-and-evolution-path.md), [ADR-0003](0003-agent-execution-boundaries.md), [ADR-0004](0004-data-and-storage-strategy.md), [ADR-0005](0005-source-governance-and-compliance.md), [ADR-0006](0006-evaluation-and-mastery-verification.md), [ADR-0007](0007-local-first-rag-chroma-and-multi-provider-architecture.md)

---

## 1. Context and Problem Statement

Through Task 033, `ai-research-learning` established a verified local-first foundation:
1. **Source Governance & Inventory (`knowledge_store.py`):** 11 registered sources (`3 ACTIVE`, `2 PLANNED_PHASE_2` approved for collection, `5 RECORD_ONLY` external reference buckets, and `1 CANDIDATE_PENDING_REVIEW` for `SRC-THE-BATCH`), 60 seed resources, 181 discovered link candidates (`PENDING_REVIEW`), and immutable `provenance_events` / `collection_runs`.
2. **Knowledge Extraction & Multi-Provider RAG (`src/ai_research_learning/rag.py`):** 18 active `knowledge_chunks` across 6 extracted primary documents in SQLite (`data/runtime/task012_pilot.sqlite`) and persistent Chroma (`data/runtime/chroma`), hybrid lexical + semantic retrieval (`ChromaKnowledgeRetriever`), strict citation sanitization (`_sanitize_answer_citations`), abstention gates, and an offline evaluation harness (`evaluate_rag_pipeline`).
3. **Read-Only Research Library HTTP API (`src/ai_research_learning/api.py`):** Loopback JSON REST API (`/healthz`, `/api/v1/sources`, `/api/v1/resources`, `/api/v1/search`, `/api/v1/transcripts`).

Task 034 evolves this repository from a research library and RAG question-answering pipeline into an **Agent-First AI Learning Platform** that combines:
- **Agentic intelligence** for research, knowledge curation, curriculum planning, teaching, assessment, and learning progress.
- **A traditional web application experience layer** for visual learning, curriculum navigation, structured lessons, learner interaction, and progress tracking.
- **A scalable platform architecture** that starts as a local-first modular monolith and evolves cleanly toward cloud deployment and distributed execution without rewriting core domain contracts.

We must also deliver the **first working end-to-end vertical slice** of this platform:
> **Learner goal → Learning Planner Agent → Evidence retrieval → Curriculum generation → Deterministic validation → Validated curriculum output**

---

## 2. Architectural Gaps Identified in Pre-Implementation Inspection

Inspecting the repository at commit `18f26e72df077e1a35f5470dc0a3cc9e44c4e8b3` revealed five concrete gaps between the existing RAG/library modules and the target learning platform:

1. **No Executable Agent Runtime or Orchestrator:** While `docs/architecture/agent-workflows.md` and `ADR-0003` describe bounded agent workflows conceptually, the Python codebase lacked an executable `PlatformOrchestrator`, capability contracts for the six logical agents, tool permission enforcement (`ToolRegistry`), bounded execution policies (`ExecutionPolicy`), and structured workflow run state (`WorkflowRunState`).
2. **No Explicit Source Ecosystem Tier Classification in Retrieval:** `ChromaKnowledgeRetriever` retrieves `ACTIVE` `knowledge_chunks`, while `LocalResearchLibraryAPI` queries `resources` and `discovered_link_candidates` separately. No service explicitly classifies evidence across the five epistemic tiers (`DISCOVERED_LINK`, `APPROVED_UNCOLLECTED_RESOURCE`, `COLLECTED_DOCUMENT`, `EXTRACTED_KNOWLEDGE`, `CURRICULUM_READY_EVIDENCE`) or enforces that candidate discovery feeds such as `SRC-THE-BATCH` never drive prerequisite ordering or unapproved citations.
3. **No Concept Graph or Prerequisite DAG Validator:** There was no domain representation of foundational-to-research concepts, prerequisite edges, topological ordering checks, or cycle detection.
4. **No Curriculum Domain Model or Deterministic Curriculum Validator:** There was no structured schema or validator for multi-stage learning paths (`CurriculumPlan`, `CurriculumStage`, `CurriculumLesson`, `MasteryCheckDesign`, `EvidenceGap`, `CurriculumCitation`) verifying that every citation resolves to retrieved chunks or verified SQLite resources and that mastery checks do not falsely claim live assessment execution.
5. **No Curriculum Service or Web API Endpoints:** `src/ai_research_learning/api.py` exposed read-only resource/source/transcript endpoints, but lacked endpoints for submitting a learner goal (`POST /api/v1/curricula/generate`), polling/reading workflow run state (`GET /api/v1/curricula/runs/{run_id}`), listing/reading curricula (`GET /api/v1/curricula`, `GET /api/v1/curricula/{curriculum_id}`), and inspecting curriculum source evidence (`GET /api/v1/curricula/{curriculum_id}/sources`).

---

## 3. Decision: Five-Layer Agent-First Platform Architecture

We adopt a strict five-layer platform architecture in which both the **traditional web application** and **agentic workflows** share the same platform services, domain models, and governance invariants.

```
+-----------------------------------------------------------------------------------+
| 1. EXPERIENCE LAYER                                                               |
|    - Web Application UI (Curriculum Explorer, Lesson Reader, Lab & Progress View) |
|    - HTTP REST API (`/healthz`, `/api/v1/sources`, `/api/v1/resources`,           |
|      `/api/v1/search`, `/api/v1/transcripts`, `/api/v1/curricula/*`)              |
|    - CLI Entrypoints (`rag.py`, `api.py`, `platform.py`)                          |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 2. CONTROLLED AGENT RUNTIME                                                       |
|    - PlatformOrchestrator (workflow state machine, bounded steps, audit trail)    |
|    - Logical Agent Capabilities:                                                  |
|      1. RESEARCH            2. KNOWLEDGE_CURATOR     3. LEARNING_PLANNER          |
|      4. TEACHING            5. ASSESSMENT            6. LEARNING_PROGRESS         |
|    - ToolRegistry (capability-scoped read/write permissions)                      |
|    - ExecutionPolicy (step budgets, timeouts, citation & validation gates)        |
|    - Provider & Model Routing (reuse of `rag.py` multi-provider abstraction)      |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 3. PLATFORM SERVICES (Deterministic Domain & Governance Logic)                    |
|    - SourceEcosystemService (5-tier epistemic classification & policy checks)     |
|    - Retrieval & RAG Service (`ChromaKnowledgeRetriever`, `RAGPipeline`)          |
|    - CurriculumManagementService + Deterministic `CurriculumValidator`            |
|      (Prerequisite DAG topological sort, cycle detection, citation verification)  |
|    - LearningSession & Progress Service Contracts (interfaces for Stages 2-4)     |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 4. DATA LAYER                                                                     |
|    - Relational Store (`data/runtime/task012_pilot.sqlite` -> PostgreSQL):        |
|      sources, resources, discovered_link_candidates, collection_runs,             |
|      knowledge_chunks, rag_runs, plus runtime curriculum/workflow registries      |
|    - Vector Store (`data/runtime/chroma` -> Managed Vector Index):                |
|      `ai_research_knowledge_chunks_v1` embeddings & metadata                      |
|    - Artifact & Report Store (`data/raw_transcripts/`, `reports/`)                |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 5. INFRASTRUCTURE LAYER                                                           |
|    - Stage 1: Local-first Python modular monolith (stdlib HTTP + SQLite + Chroma) |
|    - Stages 2-4: Containerized API, background workers, managed DB/Vector/Queues  |
+-----------------------------------------------------------------------------------+
```

---

## 4. Decision: Controlled Agent Orchestration vs. Deterministic Boundaries

### 4.1 Six Logical Agent Capabilities (Single Modular Runtime)
Rather than deploying six autonomous microservice agents or uncontrolled multi-agent chat loops, the platform defines six logical `AgentCapability` roles inside `src/ai_research_learning/platform.py` orchestrated by `PlatformOrchestrator`:

| Capability ID | Agent Role | Primary Responsibility | Permitted Tools | Status in Task 034 |
| :--- | :--- | :--- | :--- | :--- |
| `RESEARCH` | Research Agent | Synthesize grounded answers to research questions with strict chunk citations | `search_knowledge_chunks`, `inspect_source_ecosystem` | Implemented via `RAGPipeline` adapter + contract |
| `KNOWLEDGE_CURATOR` | Knowledge Curator Agent | Evaluate discovered link candidates, extract topic coverage, flag evidence gaps | `inspect_source_ecosystem`, `list_approved_resources`, `list_discovered_candidates` | Contract + ecosystem inspection implemented |
| `LEARNING_PLANNER` | Learning Planner Agent | Transform a validated learner goal into a prerequisite-ordered, citation-backed curriculum plan | `inspect_source_ecosystem`, `search_knowledge_chunks`, `list_approved_resources`, `query_concept_graph` | **Full vertical slice implemented** |
| `TEACHING` | Teaching Agent | Deliver interactive lesson explanations, worked examples, and Socratic hints | `search_knowledge_chunks`, `read_curriculum_plan` | Contract defined for subsequent slice |
| `ASSESSMENT` | Assessment Agent | Evaluate learner exercise/project submissions and mastery check responses against rubrics | `read_curriculum_plan`, `search_knowledge_chunks` | Contract defined (explicitly not claimed as active mastery evaluation in Task 034) |
| `LEARNING_PROGRESS` | Learning Progress Agent | Update learner mastery state, schedule spaced reviews, and recommend curriculum adaptations | `read_curriculum_plan` | Contract defined for subsequent slice |

### 4.2 Strict Separation Between Agent Reasoning and Deterministic Platform Logic
Per `ADR-0003`, deterministic invariants **never** rely on LLM compliance alone:
- **Source Governance & Approval Checks:** Enforced deterministically by SQL queries over `sources.status != 'CANDIDATE_PENDING_REVIEW'` (`ACTIVE`, `PLANNED_PHASE_2`, and `RECORD_ONLY`) and `permits_full_text_storage`.
- **Prerequisite DAG Validation:** Enforced deterministically by `CurriculumValidator` (Kahn's algorithm / DFS cycle detection and stage/lesson topological order verification).
- **Citation Integrity:** Enforced deterministically by verifying every `chunk_id` against retrieved `ACTIVE` `knowledge_chunks` (`lifecycle_state = 'ACTIVE'`) and every `resource_id` / `source_id` against verified SQLite records (`resources` and `sources` where `status != 'CANDIDATE_PENDING_REVIEW'`). Fabricated chunk or resource IDs fail validation or are rejected.
- **Mastery Check Honesty Gate:** Every generated curriculum and lesson mastery check carries an explicit design-time status (`DESIGN_ONLY_NOT_YET_ASSESSED`) and disclaimer confirming that interactive mastery assessment is a later platform capability.
- **Database Mutability Protection:** Existing runtime SQLite (`data/runtime/task012_pilot.sqlite`) and Chroma (`data/runtime/chroma`) are accessed in strict read-only mode during curriculum planning and API requests.

---

## 5. Decision: Five-Tier Source Ecosystem Model and Role of *The Batch*

The platform classifies all records in the knowledge store into five explicit epistemic tiers via `EvidenceTier` and `SourceEcosystemService`:

1. **`DISCOVERED_LINK` (Tier 1 — Candidate Discovery Queue):**
   - Backed by `discovered_link_candidates` (`181` rows in `PENDING_REVIEW`).
   - Unverified outbound links crawled from approved seed pages. **Never** used as curriculum citations or authoritative teaching evidence.
2. **`APPROVED_UNCOLLECTED_RESOURCE` (Tier 2 — Approved Seed Catalog / Pending Collection):**
   - Backed by `resources` joined with `sources (status != 'CANDIDATE_PENDING_REVIEW')` where `text_storage_status = 'metadata-only-not-yet-collected'` and `last_verified_at IS NULL` (`48` rows).
   - Represents curated, governance-approved syllabus topics (e.g., Hinton Coursera neural network lectures & tutorials, Bengio AI safety & GFlowNets posts, CSC321 course archive). Can be referenced as **approved catalog syllabus references** while explicitly flagging that full-text extraction is a known `evidence_gap`.
3. **`COLLECTED_DOCUMENT` (Tier 3 — Verified Collected Resource Metadata):**
   - Backed by `resources` where `last_verified_at IS NOT NULL` (`12` rows: 6 metadata-only collected pages from `SRC-3B1B`, `SRC-HINTON`, `SRC-BENGIO` + 6 full-text/abstract extracted documents from `SRC-WIKIPEDIA` and `SRC-ARXIV`).
   - Proves resource reachability, canonical URL, license, and governance compliance.
4. **`EXTRACTED_KNOWLEDGE` (Tier 4 — Stored Text Extracts & Abstracts):**
   - Backed by `resources` with `text_storage_status IN ('INTRO_EXTRACT_STORED_CC_BY_SA_4_0', 'abstract-and-metadata-only')` and `knowledge_chunks` in SQLite (`6` resources, `18` chunks).
5. **`CURRICULUM_READY_EVIDENCE` (Tier 5 — Vector-Indexed Grounding Chunks):**
   - Backed by `knowledge_chunks` (`lifecycle_state = 'ACTIVE'`, `18` chunks) indexed in Chroma (`ai_research_knowledge_chunks_v1`) plus verified collected resources (`last_verified_at IS NOT NULL`) from approved sources (`SRC-WIKIPEDIA`, `SRC-ARXIV`, `SRC-3B1B`, `SRC-HINTON`, `SRC-BENGIO`).
   - Primary grounding evidence for lesson explanations, mathematical derivations, and chunk/resource citations (`[chk_...]`, `res_...`).

### Governance Rule for *The Batch* (`SRC-THE-BATCH`)
`SRC-THE-BATCH` (`https://www.deeplearning.ai/the-batch`) is registered in `sources` with `status = 'CANDIDATE_PENDING_REVIEW'`, `permits_full_text_storage = 0`, and `max_requests_per_run = 0`.
- **Permitted Role:** AI news and research-discovery radar for surfacing emerging papers and industry trends to the Knowledge Curator backlog once reviewed.
- **Prohibited Role:** `SRC-THE-BATCH` is deterministically excluded from `CURRICULUM_READY_EVIDENCE` and `APPROVED_UNCOLLECTED_RESOURCE`, cannot be cited as curriculum grounding evidence while `CANDIDATE_PENDING_REVIEW`, and **news recency or popularity is strictly forbidden from overriding foundational prerequisite ordering** (e.g., Linear Algebra, Calculus, Probability, and Backpropagation must always precede Sequence Attention and Transformers regardless of current news trends).

---

## 6. Decision: Web Application & HTTP API v1 Contracts

To ensure the traditional web application and CLI tools operate as first-class citizens over the same backend (`src/ai_research_learning/api.py`), `LocalResearchLibraryAPI` is extended with five non-breaking `/api/v1/curricula` endpoints while preserving all existing read-only endpoints and `405` method guards:

1. **`POST /api/v1/curricula/generate`**: Validates a learner goal payload (`{"learner_goal": "...", "provider": "local", ...}`), executes the `LearningPlannerAgent` workflow through `PlatformOrchestrator`, validates the resulting `CurriculumPlan`, stores the run and curriculum in the service registry, and returns `201 Created` with `run` and `curriculum`.
2. **`GET /api/v1/curricula/runs/{run_id}`**: Returns the `WorkflowRunState` (status, step trace, timing, validation summary, and `curriculum_id`).
3. **`GET /api/v1/curricula`**: Lists generated curricula with summary metadata and pagination (`limit`, `offset`).
4. **`GET /api/v1/curricula/{curriculum_id}`**: Returns the complete validated `CurriculumPlan` (stages, lessons, prerequisites, exercises, projects, mastery checks, citations, ordering rationale, and evidence gaps).
5. **`GET /api/v1/curricula/{curriculum_id}/sources`**: Returns the structured source references, chunk citations, evidence tier breakdown, and `SRC-THE-BATCH` governance status for a curriculum.

---

## 7. Decision: Four-Stage Scale Evolution Path

| Stage | Architecture & Deployment Topology | Data & Retrieval Layer | Agent Execution & Orchestration | Trigger to Advance |
| :--- | :--- | :--- | :--- | :--- |
| **Stage 1 (Current — Task 034)** | Local-first Python modular monolith (`api.py`, `rag.py`, `platform.py`), stdlib HTTP server + CLI | SQLite (`task012_pilot.sqlite`, read-only at runtime) + embedded Chroma (`data/runtime/chroma`) + JSON/MD reports | Synchronous in-process `PlatformOrchestrator` with deterministic `ToolRegistry`, `ExecutionPolicy`, and `CurriculumValidator` | Need for multi-user web sessions and persistent learner state |
| **Stage 2 (Web/API Deployment)** | Containerized ASGI/WSGI web service + SPA frontend, user authentication, persistent learner database | PostgreSQL (or read/write application DB separated from immutable knowledge snapshot) + Chroma server | Background job queue for long-running LLM curriculum generation & RAG synthesis (`GET /api/v1/curricula/runs/{run_id}` polling) | Concurrent learner workloads & asynchronous collection/planning jobs |
| **Stage 3 (Distributed Agent Workers)** | Horizontally scaled API tier + dedicated stateless agent worker pool + task queue (Redis/Cloud Tasks) | Managed PostgreSQL + managed vector database + object storage (`GCS`/`S3`) for raw transcripts and artifacts | Distributed worker execution of `WorkflowRunState` steps, centralized OpenTelemetry tracing, cost/token circuit breakers | High-throughput multi-agent workflows and large corpus indexing |
| **Stage 4 (Multi-Tenant Scale)** | Independently scalable domain services (Knowledge, Retrieval, Curriculum, Interactive Lab, Assessment) | Multi-tenant isolated schemas, tiered vector namespaces, read replicas, CDN-cached static curriculum assets | Dynamic model/provider routing by capability & latency SLA, resilient workflow checkpointing and replay | Enterprise/institutional scale and strict tenant isolation |

---

## 8. Consequences

### Positive
- **Zero Regression on Existing Capabilities:** All 89 existing tests across `test_api.py`, `test_rag.py`, `test_task012_collection.py`, and `test_knowledge_store.py` continue to pass without mutating runtime SQLite or Chroma artifacts.
- **Grounded, Honest Curricula:** Curricula generated from the local knowledge base use real retrieved chunks (`[chk_...]`) where full text exists (Backpropagation, Boltzmann Machines, Dropout, Bahdanau Attention, ResNet, Transformer) and explicitly report `evidence_gaps` where the local store only has metadata-only seed records (e.g., foundational Linear Algebra, Multivariable Calculus, Python systems engineering, RL, and Safety/Alignment).
- **Web and Agent Parity:** Both the CLI and the HTTP REST API invoke the exact same `PlatformOrchestrator` -> `LearningPlannerAgent` -> `CurriculumValidator` pipeline.
