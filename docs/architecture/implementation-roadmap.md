# Deliverable F — Staged Implementation Roadmap & Cloud Operations Plan

| Metadata | Value |
| :--- | :--- |
| **Document ID** | `ARCH-ROADMAP-006` |
| **Version** | `1.1.0` |
| **Last Updated** | `2026-10-22` |
| **Status** | `Proposed (For Architectural Review)` |
| **Companion Docs** | [`system-overview.md`](./system-overview.md), [`cloud-provisioning-plan.md`](../operations/cloud-provisioning-plan.md), [`0001-cloud-architecture.md`](./decisions/0001-cloud-architecture.md), [`0002-knowledge-data-model.md`](./decisions/0002-knowledge-data-model.md), [`0003-agent-execution-boundaries.md`](./decisions/0003-agent-execution-boundaries.md), [`0004-notebook-isolation.md`](./decisions/0004-notebook-isolation.md) |

---

## 0. Current Cloud Infrastructure Status, Local-First Milestone & Approval-Gated Provisioning Policy

> [!IMPORTANT]
> **Unprovisioned Cloud Infrastructure Status & Local-First Rollout**:
> - **No Firebase or Google Cloud projects have been created for this product.**
> - **Project IDs (`maha-airl-dev-01` and `maha-airl-prod-01` recorded as unprovisioned candidates only), regions, billing configuration, APIs, and `DEV`/`PROD` environments are not yet established.** Global namespace availability, account access, and billing permissions must be checked before project creation. Placeholders such as `${GCP_PROJECT_ID_DEV}` and `${GCP_PROJECT_ID_PROD}` are parameterized templates for future use, not active cloud resources.
> - **Pull Requests #1 and #2 do not—and must not—create projects, enable billing, enable cloud APIs/services, deploy resources, or commit environment credentials.**
> - **Local-First Milestone (Stages 1–6) & `DEV`-First Cloud Rollout (Stage 7)**: Stages 1 through 6 are developed and verified locally using the **Firebase Local Emulator Suite** and **local SQLite** (`data/research_inventory.sqlite`) with zero cloud billing dependency. Cloud provisioning and billing authorization are deferred until a separate approval gate prior to Stage 7, followed by a **`DEV`-first rollout** (`PROD` remains unprovisioned and unavailable until release readiness has been demonstrated in `DEV`). See [`../operations/cloud-provisioning-plan.md`](../operations/cloud-provisioning-plan.md) for the complete pre-provisioning plan.
> - **Blaze Plan & Cost Controls**: Because **Firebase App Hosting requires the Google Cloud Blaze (pay-as-you-go) billing plan** and may incur charges across multiple underlying services (Cloud Build, Cloud Run SSR in the primary region, Artifact Registry, Cloud CDN/bandwidth, and Secret Manager), and because Cloud Billing budgets (`$5/month DEV`, `$15/month PROD`) are **notification thresholds only, not maximum spending guarantees**, explicit service-specific caps (`max-instances`), API quotas, and per-user/project LLM token circuit breakers must be reviewed and approved before enabling billing or provisioning cloud infrastructure.

---

## 1. Staged Implementation Sequence Overview

The implementation of the AI Research & Learning System is organized into **nine sequential stages**, plus an explicit **Approval-Gated Cloud Provisioning Milestone** prior to Stage 7 cloud deployment. Stages 1 through 6 can be developed and verified locally using offline test suites and the local Firebase Emulator Suite before any cloud project is created. No stage may advance without passing its measurable completion checks and automated CI gates.

```mermaid
flowchart TD
    S1["Stage 1: Architecture & Security Foundations"]
    S2["Stage 2: Source Inventory & Metadata Ingestion"]
    S3["Stage 3: Canonical Knowledge Model & Provenance"]
    S4["Stage 4: Transcript Ingestion"]
    S5["Stage 5: Source-Grounded Search & AI Tutoring"]
    S6["Stage 6: Learning Plans, Exercises & Evaluation"]
    S7["Stage 7: Web Application & Cloud Deployment"]
    S8["Stage 8: Flutter Mobile Application"]
    S9["Stage 9: Isolated Notebook Execution"]

    S1 --> S2
    S2 --> S3
    S3 --> S4
    S3 --> S5
    S4 --> S5
    S5 --> S6
    S6 --> S7
    S7 --> S8
    S7 --> S9
```

---

## 2. Detailed Stage Definitions (Stages 1–9)

### Stage 1 — Architecture and Security Foundations
- **Dependencies**: None (Baseline repository and CI established in Task 001; see [`reports/repository-bootstrap-report.md`](../../reports/repository-bootstrap-report.md)).
- **Scope**:
  - Finalize and approve architectural design package (`ARCH-SYS-001` through `ARCH-ROADMAP-006` and ADRs `0001`–`0004`).
  - Define environment configuration templates (`${GCP_PROJECT_ID_DEV}`, `${GCP_PROJECT_ID_PROD}`), Firestore security rules (`firestore.rules`), Storage security rules (`storage.rules`), and least-privilege IAM service account matrices as code (without hardcoding project IDs).
  - Establish GitHub Actions workflows for linting, type checking, unit testing, and security rule validation against the local Firebase Emulator Suite.
- **Acceptance Criteria**:
  - Architecture PR reviewed and approved by the system architect.
  - Firestore and Cloud Storage security rules reject all unauthenticated client writes and reject learner writes to canonical catalog collections (`sources`, `resources`, `concepts`, `claims`, `citations`) in emulator tests.
- **Key Risks & Mitigations**:
  - *Risk*: Accidental credential leakage or hardcoded project identifiers. *Mitigation*: Enforce secret scanning and parameterized environment variables (`${GCP_PROJECT_ID}`) in CI.
- **Measurable Completion Checks**:
  - `100%` of Firestore security rule unit tests pass in offline emulator mode; zero hardcoded cloud project IDs or secrets in repository.

---

### Stage 2 — Source Inventory and Metadata Ingestion
- **Dependencies**: Stage 1.
- **Scope**:
  - Implement the `SourceAdapter` interface ([`source-adapter-contract.md`](./source-adapter-contract.md)) for the five initial sources (`SRC-HINTON`, `SRC-BENGIO`, `SRC-3B1B`, `SRC-ARXIV`, `SRC-WIKIPEDIA`).
  - Enforce RFC 9309 `robots.txt` validation, source-specific rate limiters (e.g., arXiv 3.0s spacing, single connection), conditional HTTP caching (`ETag` / `If-Modified-Since`), URL canonicalization, and `RightsAndLicensing` tagging.
  - Implement `WF-01-DISCOVERY` with fixture-based offline tests for CI and dry-run inventory validation.
- **Acceptance Criteria**:
  - All five adapters emit valid `CandidateResourceRecord` payloads passing Pydantic schema validation.
  - `redistribution_allowed` is set to `false` by default unless an explicit permissive license (`CC-BY-4.0`, `CC-BY-SA-4.0`, `CC0-1.0`) is verified.
- **Key Risks & Mitigations**:
  - *Risk*: Violating arXiv or Wikipedia API etiquette or crawling disallowed paths. *Mitigation*: Hardcoded token-bucket limiters, descriptive `User-Agent`, and mandatory `robots.txt` pre-check before any HTTP fetch.
- **Measurable Completion Checks**:
  - `100%` pass rate on offline adapter contract tests covering all 5 sources, duplicate URL normalization, HTTP `429`/`503` backoff, and robots/license enforcement.

---

### Stage 3 — Canonical Knowledge Model and Provenance
- **Dependencies**: Stage 2.
- **Scope**:
  - Implement Firestore repositories and Pydantic domain models for `Source`, `Resource`, `ResourceVersion`, `ProvenanceRecord`, `Concept`, `ConceptRelation`, `Claim`, and `Citation` ([`data-model.md`](./data-model.md)).
  - Implement `WF-02-CURATION` including SHA-256 content-hash deduplication, immutable `ResourceVersion` creation (`ver_<yyyymmdd>_<hash8>`), append-only `ProvenanceRecord` logging, and cycle-free `ConceptRelation` DAG validation.
- **Acceptance Criteria**:
  - Every `ResourceVersion` and `Concept` links to an immutable `ProvenanceRecord`.
  - Attempting to insert a prerequisite cycle (`A -> B -> A`) raises `ERR_PREREQUISITE_CYCLE_DETECTED`.
  - Soft-deletion (`status = 'retracted' | 'tombstoned'`) preserves citation history while removing chunks from active retrieval.
- **Key Risks & Mitigations**:
  - *Risk*: Orphaned citations when source documents update. *Mitigation*: Citations bind to immutable `version_id` (`ver_...`), never mutable pointers.
- **Measurable Completion Checks**:
  - `0` cycles in `concept_relations` graph; `100%` of curated resources have verifiable `ProvenanceRecord` entries and SHA-256 hashes in integration tests.

---

### Stage 4 — Transcript Ingestion
- **Dependencies**: Stage 3.
- **Scope**:
  - Implement authorized transcript acquisition workflows (`official_caption_api`, `first_party_webpage`, `curated_upload`) and schema normalization into `Transcript` (`tr_...`) and `TranscriptSegment` (`seg_...`) records with `start_ms` and `end_ms` locators.
  - Link transcript segments to `knowledge_chunks` and `concept_ids` while enforcing `redistribution_allowed` policies (e.g., snippet-only indexing + deep timestamp linking for `SRC-3B1B`).
- **Acceptance Criteria**:
  - Transcript segments accurately resolve to timestamped deep links (`start_ms`, `end_ms`).
  - Unauthorized audio/video downloading or covert scraping is blocked by policy gates (`G-03`).
- **Key Risks & Mitigations**:
  - *Risk*: Storing or redistributing full copyrighted transcripts without authorization. *Mitigation*: Policy gate verifies `acquisition_method` and `redistribution_allowed` before storage.
- **Measurable Completion Checks**:
  - `100%` of ingested `TranscriptSegment` records satisfy `0 <= start_ms < end_ms` and resolve to valid `ResourceVersion` and `ProvenanceRecord` IDs.

---

### Stage 5 — Source-Grounded Search and AI Tutoring
- **Dependencies**: Stages 3 and 4.
- **Scope**:
  - Execute a **benchmark-based embedding model selection step**: evaluate active candidate embedding models on a representative AI/math retrieval benchmark before locking the initial `embedding_space_version`, rather than hardcoding a fixed model or dimension (`text-embedding-004` is excluded as deprecated/shut down).
  - Implement the replaceable `ModelProvider` and `VectorIndex` abstractions in `core-api`, keeping the embedding provider, model identifier, model version, vector dimension (up to Firestore's documented maximum of `2,048` dimensions; not a default), and `embedding_space_version` configurable and recorded on every `KnowledgeChunk` and `AgentRun`.
  - Implement the re-embedding and vector index migration workflow for future embedding model upgrades (`embedding_space_version` bump, dual-version backfill, composite vector index creation, and cutover).
  - Implement hybrid search (`POST /api/v1/search`) and the source-grounded AI Tutor (`POST /api/v1/tutor/ask`) powered by `WF-03-ANALYSIS`, requiring an identified principal, abuse controls, and quotas.
  - Enforce deterministic citation verification (`G-01`), quote-span and license/excerpt permission verification (`G-02` / `G-03`, where `permits_verbatim_excerpts == false` or `legal_uncertainty_status == 'UNVERIFIED_REQUIRES_HUMAN_REVIEW'` blocks verbatim quotes), and epistemic status tagging (`ESTABLISHED_EVIDENCE`, `AUTHOR_OPINION`, `PRELIMINARY_RESEARCH`, `GENERATED_INTERPRETATION`).
- **Acceptance Criteria**:
  - Every `KnowledgeChunk` records `embedding_provider`, `embedding_model_id`, `embedding_model_version`, `embedding_dimension`, and `embedding_space_version`.
  - Every tutor response includes structured `claims[]` and `citations[]` pointing to active `ResourceVersion` locators, and suppresses `quoted_span` whenever `permits_verbatim_excerpts == false`.
  - Any claim with a fabricated citation ID or unverified/unpermitted quote is deterministically rejected (`ERR_GROUNDING_UNVERIFIED_CITATION`).
- **Key Risks & Mitigations**:
  - *Risk*: LLM hallucinating citations, conflating generated analogies with primary theorems, or upstream embedding model deprecation. *Mitigation*: Post-generation deterministic verifier (`G-01`, `G-02`, `G-03`, `G-04`) and configurable `embedding_space_version` re-embedding pipeline.
- **Measurable Completion Checks**:
  - `100%` citation resolution rate (`citation_precision == 1.0`), `0` unpermitted verbatim quotes, and `>= 0.90` groundedness score across the golden evaluation benchmark suite.

---

### Stage 6 — Learning Plans, Exercises, and Evaluation
- **Dependencies**: Stage 5.
- **Scope**:
  - Implement `WF-04-LEARNING-PATH` (topological prerequisite sorting from foundational mathematics to deep learning and research) and `WF-05-EVALUATION` (exercise grading, misconception diagnosis, and source-grounded remediation).
  - Implement `NotebookArtifact` (`nb_...`) and `ExperimentResult` (`exp_...`) metadata cataloging and static `.ipynb` validation (`nbformat`). Client-side `Pyodide` execution is deferred; Stages 6–8 provide static rendered notebooks and downloadable `.ipynb` artifacts.
- **Acceptance Criteria**:
  - Generated learning paths satisfy topological ordering across all `REQUIRES` edges (`prerequisite_violation_count == 0`).
  - Learner assessment evaluations cite specific source passages for every remediation tip.
- **Key Risks & Mitigations**:
  - *Risk*: Prematurely advancing learners before prerequisites are mastered. *Mitigation*: Mastery state transitions require passing rubric-backed assessments in `WF-05-EVALUATION`.
- **Measurable Completion Checks**:
  - `0` topological ordering violations across test curricula; `100%` of assessment feedback items include valid remediation `citation_ids`.

---

### Stage 7 — Web Application and Approval-Gated Cloud Deployment (`DEV`-First Rollout)
- **Dependencies**: Stage 6 (completed and verified against the local Firebase Emulator Suite and local SQLite `data/research_inventory.sqlite`) **AND explicit human approval of the Cloud Provisioning & Cost-Control Milestone** ([`../operations/cloud-provisioning-plan.md`](../operations/cloud-provisioning-plan.md)).
- **Scope**:
  - Build the Next.js responsive web application (`Next.js App Router`) with Concept Graph Explorer, Resource & Transcript Reader (with timestamp synchronization), Source-Cited AI Tutor UI (showing distinct Source Evidence Cards and epistemic badges), and Static Notebook Viewer with downloadable `.ipynb` artifacts (client-side `Pyodide` execution remains deferred).
  - Enforce endpoint authentication policies: read-only catalog access may be unauthenticated only if the catalog is deliberately published; user progress and protected endpoints require authentication; AI tutor endpoints require an identified principal, abuse controls, and quotas (Firebase Anonymous Auth may be used for low-friction testing after infrastructure exists, but must never bypass cost controls).
  - Follow a **`DEV`-first rollout**: after cloud provisioning approval, provision and validate the `DEV` environment first (`${GCP_PROJECT_ID_DEV}`, candidate `maha-airl-dev-01`), keeping `PROD` (`${GCP_PROJECT_ID_PROD}`, candidate `maha-airl-prod-01`) unprovisioned/unavailable until release readiness is demonstrated in `DEV`.
  - Configure containerized deployment under the approved regional topology (**provisional Option A**: data and Python compute `Firestore`, `Cloud Storage`, `Cloud Run` `core-api` and `ingestion-worker`, and `Artifact Registry` in `asia-south1`, with Firebase App Hosting in `asia-southeast1` and a **direct client-to-Mumbai-API request path** for dynamic web/Flutter operations because App Hosting's primary region processes uncached requests even with Cloud CDN enabled; or **fallback Option C**: all services including Next.js hosted directly on Cloud Run in `asia-south1`; no region is finalized yet) using GitHub Actions and Workload Identity Federation (WIF).
- **Acceptance Criteria**:
  - End-to-end user journey works in `DEV` first (and subsequently in `PROD` only after `DEV` release readiness sign-off) with Firebase Auth, rate limiting, structured logging, service quotas, and budget alert notifications active.
  - Original source quotes and attribution banners are visually separated from AI-generated explanations on all screens.
- **Key Risks & Mitigations**:
  - *Risk*: Unexpected cloud or LLM API spend (especially since Cloud Billing budgets are notification thresholds only, not hard spending caps, and Firebase App Hosting requires Blaze across multiple underlying services). *Mitigation*: `DEV`-first rollout, Cloud Run `max-instances` caps, API consumer quota overrides, per-user token-bucket rate limits, and application-level daily token circuit breakers alongside billing budget alerts (`$5/month DEV`, `$15/month PROD` at 50%/80%/100%).
- **Measurable Completion Checks**:
  - Web Core Web Vitals pass in Lighthouse audit; accessibility score `>= 90`; CI/CD deploys cleanly via WIF with zero long-lived service account keys.

---

### Stage 8 — Flutter Mobile Application
- **Dependencies**: Stage 7.
- **Scope**:
  - Build the iOS/Android Flutter client consuming `/api/v1/*` ([`client-and-lab.md`](./client-and-lab.md)).
  - Implement learning path progression, synchronized transcript reader with timestamp jump, source-cited AI tutor chat, flashcards/assessments, and offline progress queueing (`POST /api/v1/me/progress/sync`).
- **Acceptance Criteria**:
  - Feature parity on core study, transcript navigation, and assessment workflows with the web client.
  - Offline study progress and bookmarks synchronize idempotently upon reconnection.
- **Key Risks & Mitigations**:
  - *Risk*: API contract drift breaking deployed mobile binaries. *Mitigation*: Strict `/api/v1` contract tests and backwards-compatible schema evolution policy.
- **Measurable Completion Checks**:
  - `100%` of Flutter widget and integration tests pass in CI; zero API contract mismatches against the OpenAPI `/api/v1` specification.

---

### Stage 9 — Isolated Notebook Execution
- **Dependencies**: Stage 7 (can proceed in parallel with or after Stage 8).
- **Scope**:
  - Implement `lab-execution-gateway` and ephemeral gVisor-sandboxed execution containers ([`0004-notebook-isolation.md`](./decisions/0004-notebook-isolation.md)).
  - Enforce zero cloud IAM credentials inside kernels, blocked GCE metadata server access, default-deny outbound network egress, CPU/RAM cgroups (`1 vCPU`, `2 GiB RAM`), and `60s` per-cell execution timeouts.
  - Record reproducible `ExperimentResult` (`exp_<ulid>`) telemetry from sandbox runs.
- **Acceptance Criteria**:
  - Malicious test payloads attempting network egress, metadata server token theft (`169.254.169.254`), filesystem escape, or fork bombs are deterministically blocked and terminated within resource bounds.
- **Key Risks & Mitigations**:
  - *Risk*: Container escape or crypto-mining abuse. *Mitigation*: Strict authentication + verified-email gate, strict per-user daily execution quota, gVisor sandbox isolation, and zero outbound network access.
- **Measurable Completion Checks**:
  - `100%` of adversarial security red-team tests (metadata access, egress attempt, memory exhaustion, infinite loop) blocked and verified before enabling Stage 9 in `PROD`.

---

## 3. Target Cloud Environment, Identity, CI/CD, Monitoring, Quotas, and Cost Controls (Post-Provisioning)

See [`../operations/cloud-provisioning-plan.md`](../operations/cloud-provisioning-plan.md) for the complete operational specification, candidate project ID checks, regional topology options, and service-specific quota/spend-cap limitations.

### 3.1 Target DEV and PROD Cloud Project Separation & Regional Topology (Unprovisioned Blueprint)
No Firebase or Google Cloud projects exist today, and no cloud project IDs, regions, billing accounts, or APIs have been established. Stages 1–6 run locally against the **Firebase Local Emulator Suite** and **local SQLite** (`data/research_inventory.sqlite`). When the approval-gated cloud provisioning milestone is authorized, rollout proceeds **`DEV`-first** and all deployment manifests parameterize the target project via `${GCP_PROJECT_ID}`:
- **`DEV` Project (`${GCP_PROJECT_ID_DEV}`, candidate ID `maha-airl-dev-01` — unprovisioned candidate only)**: Provisioned first once cloud testing is justified after local emulator verification. Used for integration testing, staging previews, and capped dry-run ingestion (`--limit 25`) with strict instance caps (`max-instances = 2`).
- **`PROD` Project (`${GCP_PROJECT_ID_PROD}`, candidate ID `maha-airl-prod-01` — unprovisioned candidate only)**: Remains unprovisioned and unavailable until release readiness has been demonstrated in `DEV` and a separate production approval gate is passed.
- **Provisional Regional Topology (Unfinalized)**: **Option A** is the provisional recommendation (`asia-south1` Mumbai for Firestore, Cloud Storage, Cloud Run `core-api` and `ingestion-worker`, and Artifact Registry; `asia-southeast1` Singapore for Firebase App Hosting, with a **direct client-to-Mumbai-API request path** for dynamic web/Flutter calls because App Hosting's primary region executes uncached SSR requests). **Option C** (hosting the Next.js container directly on Cloud Run in `asia-south1`) is preserved as a single-region fallback.

### 3.2 Identity Boundaries & Least-Privilege IAM

| Identity / Principal | Target Environment | Permitted Roles / Permissions | Explicit Denials |
| :--- | :--- | :--- | :--- |
| `sa-cicd-deployer` (via Workload Identity Federation) | `DEV` / `PROD` | `roles/run.admin`, `roles/firebaseapphosting.admin`, `roles/artifactregistry.writer`, `roles/firebaserules.admin`, `roles/datastore.indexAdmin`, `roles/iam.serviceAccountUser` (scoped to runtime SAs) | No static JSON keys; branch-restricted to `main` / release tags for `PROD`. |
| `sa-core-api` | Cloud Run `core-api` | `roles/datastore.user` (Firestore), `roles/storage.objectViewer` (`artifacts/`, `notebooks/`), `roles/storage.objectCreator` (`experiments/`), `roles/aiplatform.user`, `roles/secretmanager.secretAccessor` (scoped) | Cannot write to `audit/` bucket prefix; cannot invoke Cloud Run Admin APIs. |
| `sa-ingestion-worker` | Cloud Run Job `ingestion-worker` | `roles/datastore.user`, `roles/storage.objectAdmin` (`audit/`, `artifacts/`, `notebooks/`), `roles/aiplatform.user`, `roles/secretmanager.secretAccessor` (scoped) | No public ingress URL; triggered only by Cloud Scheduler (`sa-scheduler-invoker`) or `admin` workflow. |
| `sa-apphosting-web` | Firebase App Hosting (`web`) | `roles/firebaseapphosting.computeRunner`, `roles/secretmanager.secretAccessor` (scoped to web public/SSR config only) | No direct `roles/datastore.user` or `roles/storage.objectAdmin`; dynamic requests route via `core-api`. |
| `sa-lab-sandbox` (Stage 9) | Ephemeral Sandbox | **None** (`0` GCP IAM permissions; metadata server blocked) | No access to Firestore, GCS, Vertex AI, or internal VPC services. |

### 3.3 CI/CD Pipeline Architecture (GitHub Actions + Firebase App Hosting)
1. **Pull Request Gate (`.github/workflows/ci.yml`)**:
   - Runs offline Python unit tests (`python3 -m unittest discover -s tests -v`), bytecode compilation checks, schema contract tests, and security rule tests.
   - Zero live external scraping or cloud mutations during PR checks.
2. **Staging Deployment (`DEV` — First Cloud Rollout Target)**:
   - Authenticates to `${GCP_PROJECT_ID_DEV}` via OIDC Workload Identity Federation (`google-github-actions/auth`).
   - Builds and deploys `core-api` and `ingestion-worker` images, and triggers Firebase App Hosting rollout (or Cloud Run web rollout under Option C).
3. **Production Promotion (`PROD` — Gated on `DEV` Release Readiness)**:
   - Requires demonstrated release readiness in `DEV`, protected GitHub Environment approval (`production`), green staging smoke tests, and verified provenance audit checks before rolling out to `${GCP_PROJECT_ID_PROD}`.

### 3.4 Monitoring, Quotas, and Cost Controls

> [!WARNING]
> **Budget Notification Limitation & Multi-Service Blaze Costs**: Cloud Billing budgets (`$5/month DEV`, `$15/month PROD`) are **notification thresholds only, not maximum spending guarantees**, and are subject to multi-hour billing telemetry delays. Furthermore, Firebase App Hosting requires the Blaze plan and can incur costs across Cloud Build, Cloud Run SSR, Artifact Registry, Cloud CDN/bandwidth, and Secret Manager, while GCP API consumer quota overrides take 15–30 minutes to propagate and exclude storage/egress. No single control guarantees a strict total-bill ceiling; defense-in-depth technical limits are mandatory (see [`../operations/cloud-provisioning-plan.md`](../operations/cloud-provisioning-plan.md)).

| Control Layer | Configuration (`DEV`) | Configuration (`PROD`) | Purpose |
| :--- | :--- | :--- | :--- |
| **Cloud Run Scaling** | `min-instances: 0`, `max-instances: 2` (`core-api` & App Hosting) | `min-instances: 0`, `max-instances: 5–10` (`core-api`), `3` (App Hosting) | Scale-to-zero idle cost; hard instance ceiling against traffic spikes. |
| **API Rate Limiting & Quotas** | `30 req/min` (Search), `5 req/min` (Tutor) per UID + GCP API consumer quota overrides | `60 req/min` (Search), `15 req/min` (Tutor) per UID + GCP API consumer quota overrides | Prevents automated scraping and bounds LLM/API spend (noting 15–30 min quota override propagation delay). |
| **LLM Daily Token Quota** | `100,000` tokens/day project cap | `500,000–2,000,000` tokens/day project cap + per-UID cap | Application-level hard circuit breaker (`503 ERR_UPSTREAM_MODEL_UNAVAILABLE`) if daily token budget is exhausted; falls back to deterministic search. |
| **Cloud Storage Lifecycle** | Delete temporary staging/audit files after `7–30 days`; abort incomplete multipart uploads after `1 day` | Transition `audit/` raw snapshots after `30–90 days`; abort incomplete multipart uploads after `1 day` | Minimizes long-term storage cost while preserving provenance. |
| **Cloud Billing Budgets (Notification Only)** | Notification thresholds at `50%`, `80%`, `100%` of `$5/month` (`DEV`) | Notification thresholds at `50%`, `80%`, `100%` of `$15/month` (`PROD`) | Email + Pub/Sub notifications to project administrators (**does not automatically cap spend or disable billing**). |
| **Observability Alerts** | Log-based errors in Cloud Logging | P1 alert on `citation_verification_failures_total > 0` or `5xx > 2%` over 5 min | Immediate visibility into grounding regressions or service outages. |

---

## 4. Official References Consulted

| Topic | Official Documentation URL | Date Checked |
| :--- | :--- | :--- |
| **GitHub Actions Workload Identity Federation with GCP** | https://github.com/google-github-actions/auth | `2026-10-21` |
| **Cloud Run Max Instances & Cost Controls** | https://cloud.google.com/run/docs/configuring/max-instances | `2026-10-21` |
| **Firebase Emulator Suite for CI Testing** | https://firebase.google.com/docs/emulator-suite | `2026-10-21` |
| **Cloud Billing Budgets & Cost Alerting** | https://cloud.google.com/billing/docs/how-to/budgets | `2026-10-21` |
