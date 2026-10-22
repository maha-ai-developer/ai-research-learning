# ADR 0002 — Canonical Knowledge Data Model, Provenance Separation, and Firestore Vector Indexing

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0002` |
| **Date** | `2026-10-22` |
| **Status** | `Proposed` |
| **Related Docs** | [`data-model.md`](../data-model.md), [`source-adapter-contract.md`](../source-adapter-contract.md) |

---

## 1. Context and Problem Statement

The system must represent heterogeneous learning resources (foundational papers from Hinton/Bengio/arXiv, interactive video lessons from 3Blue1Brown, reference encyclopedic articles from Wikipedia, timestamped transcripts, prerequisite concept graphs, source-grounded claims/citations, learning paths, and notebook experiments) while satisfying five strict requirements:
1. **Immutable Provenance**: Every resource snapshot, transcript, and generated claim must trace back to an immutable version (`ver_<yyyymmdd>_<hash8>`) and provenance audit log (`prov_<ulid>`).
2. **Separation of Source vs. Generated Content**: Original source metadata and licensed quotes must never be overwritten or conflated with AI-generated explanations.
3. **License, Excerpt & Redistribution Compliance**: Full-text or media binaries may only be stored/redistributed when `redistribution_allowed == true`, and verbatim quotes (`quoted_span`) may only be stored/served when `permits_verbatim_excerpts == true`. Legal uncertainty (`UNVERIFIED_REQUIRES_HUMAN_REVIEW`) must be recorded explicitly and default closed to metadata + links.
4. **Configurable, Versioned Embeddings**: Upstream embedding models evolve and deprecate (for example, `text-embedding-004` has been shut down and must not be selected). The data model must keep the embedding provider and model configurable, record full model/version/dimension/`embedding_space_version` metadata alongside every vector, and support safe re-embedding and index migration.
5. **Minimal Database Footprint**: Avoid introducing additional database engines (e.g., PostgreSQL, Pinecone, Neo4j) in the first release unless justified by empirical measurements against initial scaling hypotheses.

---

## 2. Decision

1. **Hybrid Firestore + Cloud Storage Split**:
   - **Cloud Firestore (Native Mode)** serves as the single operational metadata, concept graph (`concepts`, `concept_relations`), learner state (`user_progress`), and vector search (`knowledge_chunks` queried via native `find_nearest`) database for Release 1.
   - **Google Cloud Storage (GCS)** stores immutable content-addressed blobs exceeding Firestore's 1 MiB document limit or requiring raw byte archival (`audit/`, `artifacts/`, `notebooks/`, `experiments/`).
2. **Configurable Embedding Strategy, Benchmark Selection, and Migration**:
   - **No Hardcoded Deprecated Models**: `text-embedding-004` is excluded because it has been shut down. The embedding provider and model remain configurable via runtime settings behind the `ModelProvider` abstraction.
   - **Mandatory Embedding Metadata**: Every `KnowledgeChunk` (and `AgentRun`) records `embedding_provider`, `embedding_model_id`, `embedding_model_version`, `embedding_dimension`, and `embedding_space_version` alongside `embedding_vector`.
   - **Dimension Ceiling vs. Default**: Firestore's `2,048` dimensions is treated strictly as Firestore's **documented maximum ceiling**, not the default dimension. The concrete dimension ($D \le 2,048$) and model are chosen in a **Stage 5 benchmark-based model-selection step** evaluating retrieval recall (`Recall@10`, `nDCG@10`), latency, and cost on domain queries.
   - **Re-Embedding & Index Migration**: Because embeddings from different models or dimensions occupy incompatible vector spaces, any model/version/dimension change increments `embedding_space_version`, provisions a corresponding composite vector index scoped to `embedding_space_version` and dimension $D'$, backfills `knowledge_chunks` in `ingestion-worker`, and atomically switches `core-api` retrieval before retiring the old index.
3. **Versioned Immutability & Soft-Delete Tombstoning**:
   - Resources are split into a stable logical identity (`resources/{resource_id}`) and immutable content snapshots (`resources/{resource_id}/versions/{version_id}`).
   - All `Citation` (`cit_<ulid>`) and `KnowledgeChunk` (`chk_<version_id>_<seq>`) documents bind to `version_id` so upstream edits never silently invalidate historical citations.
   - Retracted or removed upstream resources (including copyright/takedown requests) transition to `status = 'retracted' | 'tombstoned'` (soft-delete), purging unpermitted cached artifacts and filtering their chunks from active retrieval while preserving historical audit trails.
4. **Explicit Epistemic & License Fields**:
   - Every `Claim` (`clm_<ulid>`) requires an explicit `epistemic_status` (`ESTABLISHED_EVIDENCE`, `AUTHOR_OPINION`, `PRELIMINARY_RESEARCH`, `GENERATED_INTERPRETATION`) and a non-empty `citation_ids[]` array.
   - Every `ResourceVersion` carries a `RightsAndLicensing` block (`license_spdx`, `redistribution_allowed`, `permits_verbatim_excerpts`, `legal_uncertainty_status`, `attribution_text`), defaulting closed (`redistribution_allowed = false`, `permits_verbatim_excerpts = false`, `legal_uncertainty_status = "UNVERIFIED_REQUIRES_HUMAN_REVIEW"`) unless verified.

---

## 3. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. Cloud SQL for PostgreSQL (`pgvector`) in Release 1** | Rich relational joins, recursive CTEs for concept DAGs, native full-text + vector search. | Requires always-on instance billing (or VPC connector management), schema migration tooling, and separate client auth plumbing compared to Firebase Auth + Firestore. Deferred until empirical scaling hypotheses are tested. |
| **B. Dedicated Vector DB (Vertex AI Vector Search / Pinecone) + Dedicated Graph DB (Neo4j)** | Specialized ANN tuning and Cypher graph queries. | Multiplies operational complexity, synchronization failure modes, and fixed monthly costs for an initial corpus under `100,000` chunks and `< 2,500` concepts. |
| **C. Hardcoding a Single Unversioned Embedding Model** | Slightly simpler initial schema. | Breaks retrieval whenever an upstream model is deprecated or upgraded, as vectors from different models cannot be compared without `embedding_space_version` isolation and re-embedding. |
| **D. Mutable In-Place Resource Overwrites** | Simpler single-document schema. | Destroys citation reproducibility: if a Wikipedia revision or arXiv v2 preprint changes section numbering, existing citations break or misattribute quotes. |

---

## 4. Initial Hypotheses for Future Database Migration (To Validate via Empirical Measurements)

The following thresholds are **initial engineering hypotheses—not universal scaling facts—and must be validated using empirical latency, cost, and recall measurements** on real workload telemetry before introducing any additional database engine:

1. **Initial Hypothesis for Dedicated Vector Search (`Vertex AI Vector Search` or `pgvector`)**:
   - Active `knowledge_chunks` count approaches **`100,000–500,000` chunks** with measured degradation in recall or cost, **OR**
   - Measured `find_nearest` p95 query latency with metadata pre-filters exceeds **`250 ms`** (or `400 ms` p99) over a 7-day rolling window, **OR**
   - Firestore composite index limits (`200` per database) block required multi-attribute search combinations.
2. **Initial Hypothesis for Relational / Graph Store (`Cloud SQL PostgreSQL` via Firebase Data Connect)**:
   - Concept graph exceeds **`10,000` nodes** and **`50,000` edges** where measured in-memory/Firestore traversal exceeds **`50–150 ms`**, **OR**
   - Complex cross-entity analytical reporting requires multi-way SQL joins across `> 100,000` learner assessment records.

---

## 5. Official References Consulted

- Firestore Vector Search (`find_nearest`, `<= 2048` dimensions): https://firebase.google.com/docs/firestore/vector-search (Checked `2026-10-21`)
- Firestore Quotas and Document Limits (`1 MiB` max document size): https://firebase.google.com/docs/firestore/quotas (Checked `2026-10-21`)
