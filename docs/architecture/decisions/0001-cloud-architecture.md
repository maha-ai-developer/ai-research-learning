# ADR 0001 — Minimal Serverless Cloud Architecture on Firebase & Google Cloud

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0001` |
| **Date** | `2026-10-21` |
| **Status** | `Proposed` |
| **Related Docs** | [`system-overview.md`](../system-overview.md), [`implementation-roadmap.md`](../implementation-roadmap.md) |

---

## 1. Context and Problem Statement

The AI Research & Learning System requires a cloud architecture capable of:
1. Serving a responsive Next.js web application and a Flutter mobile application (iOS/Android) over a shared, authenticated API.
2. Executing scheduled, polite metadata discovery and curation pipelines across five initial sources (`SRC-HINTON`, `SRC-BENGIO`, `SRC-3B1B`, `SRC-ARXIV`, `SRC-WIKIPEDIA`).
3. Storing canonical metadata, provenance records, concept prerequisite graphs, learner progress, and permitted raw/derived artifacts with strict `DEV` and `PROD` separation.
4. Operating with minimal baseline cost (scale-to-zero when idle) and low operational overhead while avoiding premature microservice proliferation.

---

## 2. Decision

We adopt a **minimal serverless Google Cloud + Firebase architecture** consisting of five core managed services isolated across separate `DEV` (`${GCP_PROJECT_ID_DEV}`) and `PROD` (`${GCP_PROJECT_ID_PROD}`) projects:

1. **Firebase App Hosting** for the Next.js Web Client (`apphosting.yaml`).
2. **Firebase Authentication** for unified identity management and JWT ID Token issuance across Web and Flutter clients.
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
- **Vendor Coupling to GCP IAM/Firebase Auth**: Mitigated by isolating storage and AI model interactions behind Python repository and `ModelProvider` interfaces inside `core-api`.

---

## 5. Official References Consulted

- Firebase App Hosting Architecture: https://firebase.google.com/docs/app-hosting/about-app-hosting (Checked `2026-10-21`)
- Cloud Run Services vs. Jobs: https://cloud.google.com/run/docs/overview/what-is-cloud-run (Checked `2026-10-21`)
- Firebase Authentication Admin SDK: https://firebase.google.com/docs/auth/admin (Checked `2026-10-21`)
