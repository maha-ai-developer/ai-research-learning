# ADR 0001 — Minimal Serverless Cloud Architecture on Firebase & Google Cloud

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0001` |
| **Date** | `2026-10-22` |
| **Status** | `Proposed` |
| **Related Docs** | [`system-overview.md`](../system-overview.md), [`implementation-roadmap.md`](../implementation-roadmap.md), [`cloud-provisioning-plan.md`](../../operations/cloud-provisioning-plan.md) |

---

## 1. Context, Unprovisioned Infrastructure Status, and Problem Statement

> [!IMPORTANT]
> **Explicit Unprovisioned Cloud Infrastructure Status & Local-First Rollout**:
> - **No Firebase or Google Cloud projects have been created for this product.**
> - **Project IDs (`maha-airl-dev-01` and `maha-airl-prod-01` recorded as unprovisioned candidates only), regions, billing configuration, APIs, and `DEV`/`PROD` environments are not yet established.** Global namespace availability, account access, and billing permissions must be verified before project creation.
> - **This architecture package and Pull Requests #1 and #2 do not—and must not—create projects, enable billing, enable services, deploy resources, or commit environment credentials.**
> - **Local-First Milestone & `DEV`-First Rollout**: Initial adapter, schema, and security-rule development (Stages 1–6) uses the **Firebase Local Emulator Suite** and **local SQLite** (`data/research_inventory.sqlite`) with zero cloud dependency. Cloud provisioning and billing authorization are deferred to a separate approval gate, followed by a **`DEV`-first rollout** (`PROD` remains unprovisioned and unavailable until release readiness is demonstrated in `DEV`). See [`cloud-provisioning-plan.md`](../../operations/cloud-provisioning-plan.md).
> - **Blaze Plan & Cost Controls**: Because **Firebase App Hosting requires the Google Cloud Blaze (pay-as-you-go) billing plan** and may incur charges across multiple underlying services (Cloud Build, Cloud Run SSR in the primary region, Artifact Registry, Cloud CDN/bandwidth, and Secret Manager), and because Cloud Billing budgets (`$5/month DEV`, `$15/month PROD`) are **notification thresholds only, not maximum spending guarantees**, explicit service-specific caps (`max-instances`), API quotas, and application-level LLM token circuit breakers must be reviewed and approved before enabling Firebase App Hosting or provisioning cloud resources.

The AI Research & Learning System requires a target cloud architecture capable of:
1. Serving a responsive Next.js web application and a Flutter mobile application (iOS/Android) over a shared API with clear access boundaries (unauthenticated read-only access allowed only if the catalog is deliberately published; authentication required for user-specific progress and protected endpoints; identified principals, abuse controls, and strict quotas required for AI tutor endpoints).
2. Executing scheduled, polite metadata discovery and curation pipelines across five initial sources (`SRC-HINTON`, `SRC-BENGIO`, `SRC-3B1B`, `SRC-ARXIV`, `SRC-WIKIPEDIA`).
3. Storing canonical metadata, provenance records, concept prerequisite graphs, learner progress, and permitted raw/derived artifacts with strict `DEV` and `PROD` separation once provisioned.
4. Operating with minimal baseline cost (scale-to-zero when idle) and low operational overhead while avoiding premature microservice proliferation.

---

## 2. Decision

We adopt a **local-first development workflow** (Firebase Local Emulator Suite + local SQLite `data/research_inventory.sqlite` for Stages 1–6) followed by a **minimal serverless Google Cloud + Firebase target architecture** isolated across separate future `DEV` (`${GCP_PROJECT_ID_DEV}`, candidate `maha-airl-dev-01`) and `PROD` (`${GCP_PROJECT_ID_PROD}`, candidate `maha-airl-prod-01`) projects (to be created only during the separate approval-gated cloud provisioning milestone, using a **`DEV`-first rollout** where `PROD` remains unavailable until release readiness is demonstrated in `DEV`; see [`cloud-provisioning-plan.md`](../../operations/cloud-provisioning-plan.md)):

1. **Firebase App Hosting (Provisional Option A) or Direct Cloud Run Web Hosting (Fallback Option C)**:
   - **Provisional Recommendation (Option A)**: Deploy data and Python compute (`Firestore`, `Cloud Storage`, `Cloud Run` `core-api` and `ingestion-worker`, and `Artifact Registry`) in `asia-south1` (Mumbai) and **Firebase App Hosting** (`apphosting.yaml`) in `asia-southeast1` (Singapore, nearest supported App Hosting region), gated on Blaze plan billing approval. Because App Hosting's primary-region server (`asia-southeast1`) handles uncached SSR requests even when Cloud CDN is enabled, dynamic web and Flutter operations (`search`, `tutor`, `learner progress`) use a **direct client-to-Mumbai-API (`core-api` in `asia-south1`) request path**, avoiding unnecessary cross-region server-side proxies through `asia-southeast1`.
   - **Co-Located Fallback (Option C)**: If single-region simplicity and strict co-location in `asia-south1` outweigh App Hosting's git-driven convenience, deploy the Next.js web container directly on **Cloud Run** in `asia-south1` alongside `core-api` and `Firestore`. No region has been finalized for provisioning.
2. **Firebase Authentication** for unified identity management and JWT ID Token issuance across Web and Flutter clients:
   - Read-only public catalog endpoints may be accessed without authentication **only if the catalog is deliberately published**.
   - User-specific progress and protected endpoints require an authenticated user.
   - AI tutor endpoints (`/api/v1/tutor/ask`) require an **identified principal, abuse controls (App Check + rate limiting), and strict token/request quotas**. Firebase Anonymous Auth may be used for low-friction testing after infrastructure exists, but it must remain bound to strict per-UID/per-IP quotas and project budget caps so that it never eliminates cost controls.
3. **Cloud Run Service (`core-api`)** as a single modular monolith exposing `/api/v1/*` for search, concept graph queries, transcript timestamp navigation, source-cited AI tutoring, learning paths, and learner progress.
4. **Cloud Run Jobs (`ingestion-worker`)** triggered by Cloud Scheduler for batch discovery, metadata synchronization, and curation (`WF-01-DISCOVERY`, `WF-02-CURATION`).
5. **Cloud Firestore (Native Mode) + Google Cloud Storage (GCS)** as the canonical metadata/vector store and immutable blob/artifact store respectively.

---

## 3. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. Google Kubernetes Engine (GKE) + Microservices per Workflow** | High customization; fine-grained per-agent scaling. | Always-on control plane and node costs; excessive operational complexity for an early-stage learning platform; violates the "prefer simple designs over premature microservices" principle. |
| **B. Next.js API Routes Only (No Dedicated Cloud Run Backend)** | Single deployment artifact for web. | Tightly couples Flutter mobile API traffic and long-running Python scientific/ingestion libraries (`nbformat`, `pydantic`, PDF/HTML parsers) to Node.js frontend serverless functions. |
| **C. Always-On Compute Engine VMs** | Predictable fixed monthly cost. | No scale-to-zero; manual OS patching, TLS management, and container orchestration required. |

---

## 4. Consequences and Trade-Offs

### Positive
- **Scale-to-Zero Cost Efficiency**: Both `core-api` (`min-instances = 0`) and `ingestion-worker` (on-demand Job) incur zero compute charges when idle.
- **Clean Client Parity**: Next.js and Flutter consume the exact same `/api/v1/*` endpoints and Firebase Auth tokens.
- **Fault Isolation**: Running polite source discovery inside `ingestion-worker` (Cloud Run Job) prevents slow external HTTP fetches (e.g., arXiv's 3-second crawl delay) from consuming request concurrency or memory on `core-api`.

### Negative / Mitigations
- **Cold-Start Latency on `core-api`**: Mitigated by keeping container images lean, lazy-loading heavy clients, and optionally setting `min-instances = 1` in `PROD` only if p95 cold-start latency exceeds `2.5s`.
- **Cross-Region Split Under Provisional Option A (`asia-southeast1` App Hosting + `asia-south1` Data/Compute)**: Because App Hosting's primary-region server (`asia-southeast1`) processes uncached requests even with Cloud CDN enabled, routing dynamic API requests through Next.js SSR would add cross-region latency. Mitigated by routing all dynamic web and Flutter requests directly from the client to `core-api` in `asia-south1`, and preserving **Option C** (direct Cloud Run web hosting in `asia-south1`) as a single-region fallback.
- **Multi-Service Blaze Billing Exposure**: Firebase App Hosting requires the Blaze plan and can incur costs across Cloud Build, Cloud Run, Artifact Registry, Cloud CDN/bandwidth, and Secret Manager, while Cloud Billing budgets (`$5 DEV` / `$15 PROD`) are notification thresholds only (with reporting delays) rather than hard total-bill ceilings. Mitigated by local-first development (Stages 1–6), `DEV`-first rollout, strict `max-instances` caps, API consumer quota overrides, and application-level token/request circuit breakers ([`cloud-provisioning-plan.md`](../../operations/cloud-provisioning-plan.md)).
- **Vendor Coupling to GCP IAM/Firebase Auth**: Mitigated by isolating storage and AI model interactions behind Python repository and `ModelProvider` interfaces inside `core-api`.

---

## 5. Official References Consulted

- Firebase App Hosting Architecture: https://firebase.google.com/docs/app-hosting/about-app-hosting (Checked `2026-10-21`)
- Cloud Run Services vs. Jobs: https://cloud.google.com/run/docs/overview/what-is-cloud-run (Checked `2026-10-21`)
- Firebase Authentication Admin SDK: https://firebase.google.com/docs/auth/admin (Checked `2026-10-21`)
