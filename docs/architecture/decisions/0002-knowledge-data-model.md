# ADR 0002 — Canonical Knowledge Data Model, Provenance Separation, and Firestore Vector Indexing

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0002` |
| **Date** | `2026-10-21` |
| **Status** | `Proposed` |
| **Related Docs** | [`data-model.md`](../data-model.md), [`source-adapter-contract.md`](../source-adapter-contract.md) |

---

## 1. Context and Problem Statement

The system must represent heterogeneous learning resources (foundational papers from Hinton/Bengio/arXiv, interactive video lessons from 3Blue1Brown, reference encyclopedic articles from Wikipedia, timestamped transcripts, prerequisite concept graphs, source-grounded claims/citations, learning paths, and notebook experiments) while satisfying four strict requirements:
1. **Immutable Provenance**: Every resource snapshot, transcript, and generated claim must trace back to an immutable version (`ver_<yyyymmdd>_<hash8>`) and provenance audit log (`prov_<ulid>`).
2. **Separation of Source vs. Generated Content**: Original source metadata and verbatim quotes must never be overwritten or conflated with AI-generated explanations.
3. **License & Redistribution Compliance**: Full-text or media binaries may only be stored in Cloud Storage when `redistribution_allowed == true`.
4. **Minimal Database Footprint**: Avoid introducing additional database engines (e.g., PostgreSQL, Pinecone, Neo4j) in the first release unless justified by clear quantitative evidence.

---

## 2. Decision

1. **Hybrid Firestore + Cloud Storage Split**:
   - **Cloud Firestore (Native Mode)** serves as the single operational metadata, concept graph (`concepts`, `concept_relations`), learner state (`user_progress`), and vector search (`knowledge_chunks` with 768-dimension `VectorValue` embeddings queried via native `find_nearest`) database for Release 1.
   - **Google Cloud Storage (GCS)** stores immutable content-addressed blobs exceeding Firestore's 1 MiB document limit or requiring raw byte archival (`audit/`, `artifacts/`, `notebooks/`, `experiments/`).
2. **Versioned Immutability & Soft-Delete Tombstoning**:
   - Resources are split into a stable logical identity (`resources/{resource_id}`) and immutable content snapshots (`resources/{resource_id}/versions/{version_id}`).
   - All `Citation` (`cit_<ulid>`) and `KnowledgeChunk` (`chk_<version_id>_<seq>`) documents bind to `version_id` so upstream edits never silently invalidate historical citations.
   - Retracted or removed upstream resources transition to `status = 'retracted' | 'tombstoned'` (soft-delete), filtering their chunks from active retrieval while preserving historical citation integrity.
3. **Explicit Epistemic & License Fields**:
   - Every `Claim` (`clm_<ulid>`) requires an explicit `epistemic_status` (`ESTABLISHED_EVIDENCE`, `AUTHOR_OPINION`, `PRELIMINARY_RESEARCH`, `GENERATED_INTERPRETATION`) and a non-empty `citation_ids[]` array.
   - Every `ResourceVersion` carries a `RightsAndLicensing` block (`license_spdx`, `redistribution_allowed`, `attribution_text`).

---

## 3. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. Cloud SQL for PostgreSQL (`pgvector`) in Release 1** | Rich relational joins, recursive CTEs for concept DAGs, native full-text + vector search. | Requires always-on instance billing (or VPC connector management), schema migration tooling, and separate client auth plumbing compared to Firebase Auth + Firestore. Deferred until quantitative triggers are met. |
| **B. Dedicated Vector DB (Vertex AI Vector Search / Pinecone) + Dedicated Graph DB (Neo4j)** | Specialized ANN tuning and Cypher graph queries. | Multiplies operational complexity, synchronization failure modes, and fixed monthly costs for a corpus initially under `100,000` chunks and `< 2,500` concepts. |
| **C. Mutable In-Place Resource Overwrites** | Simpler single-document schema. | Destroys citation reproducibility: if a Wikipedia revision or arXiv v2 preprint changes section numbering, existing citations break or misattribute quotes. |

---

## 4. Quantitative Triggers for Future Database Migration

We establish explicit, measurable thresholds that must be crossed before introducing additional databases:

1. **Trigger for Dedicated Vector Search (`Vertex AI Vector Search` or `pgvector`)**:
   - Active `knowledge_chunks` count exceeds **`500,000` chunks**, **OR**
   - `find_nearest` p95 query latency with metadata pre-filters exceeds **`250 ms`** over a 7-day rolling window, **OR**
   - Composite index limits (`200` per Firestore database) block required multi-attribute search combinations.
2. **Trigger for Relational / Graph Store (`Cloud SQL PostgreSQL` via Firebase Data Connect)**:
   - Concept graph exceeds **`10,000` nodes** and **`50,000` edges** where in-memory topological sorting exceeds **`50 ms`**, **OR**
   - Complex cross-entity analytical reporting requires multi-way SQL joins across `> 100,000` learner assessment records.

---

## 5. Official References Consulted

- Firestore Vector Search (`find_nearest`, `<= 2048` dimensions): https://firebase.google.com/docs/firestore/vector-search (Checked `2026-10-21`)
- Firestore Quotas and Document Limits (`1 MiB` max document size): https://firebase.google.com/docs/firestore/quotas (Checked `2026-10-21`)
