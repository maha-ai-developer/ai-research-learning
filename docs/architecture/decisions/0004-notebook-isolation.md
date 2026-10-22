# ADR 0004 — Separation of Notebook Authoring/Cataloging from Isolated Code Execution

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0004` |
| **Date** | `2026-10-22` |
| **Status** | `Proposed` |
| **Related Docs** | [`client-and-lab.md`](../client-and-lab.md), [`implementation-roadmap.md`](../implementation-roadmap.md) |

---

## 1. Context and Problem Statement

Interactive Jupyter notebooks (`.ipynb`) are central to learning mathematical foundations, backpropagation, and machine learning experimentation. However, Jupyter notebooks contain arbitrary executable Python and shell code. Executing notebook cells inside `core-api` (or any process with access to service account credentials, Firestore, Cloud Storage, or internal VPC networks) introduces critical vulnerabilities:
1. **Remote Code Execution (RCE) & Credential Theft**: User code could read environment variables or query the GCE metadata server (`http://169.254.169.254`) to exfiltrate the `sa-core-api` IAM token and tamper with Firestore/Cloud Storage.
2. **Denial of Service (DoS)**: Memory allocation bombs or infinite loops in a single notebook cell would starve all concurrent web and mobile API users on `core-api`.
3. **Abuse & Crypto-Mining**: Open outbound network access and unthrottled compute in notebook kernels invite abuse.

---

## 2. Decision

1. **Strict Separation of Notebook Authoring/Viewing (Stages 6–8) from Interactive Code Execution (Stage 9), Deferring Client-Side `Pyodide`**:
   - **Never Execute Notebook Code in `core-api`**: `core-api` treats `.ipynb` files strictly as inert JSON documents parsed and validated via `nbformat`.
   - **Defer Client-Side `Pyodide` Execution**: Client-side in-browser WebAssembly execution (`Pyodide` / `JupyterLite`) is explicitly **deferred**. Stages 6–8 focus strictly on **static rendered notebooks and downloadable `.ipynb` notebook artifacts** (plus pre-recorded `ExperimentResult` telemetry), avoiding premature browser runtime complexity and keeping all interactive code execution consolidated in the Stage 9 isolated lab milestone.
   - **Stages 6–8 Delivery Mode**: Curated notebooks (`nb_<slug>_v<N>`) and pre-recorded `ExperimentResult` runs (`exp_<ulid>`) are stored in Cloud Storage (`notebooks/` and `experiments/`), rendered as static read-only cells with pre-computed plot previews in the Next.js and Flutter clients, and accompanied by downloadable `.ipynb` artifacts.
2. **Deferred Stage 9 Isolated Execution Architecture (`lab-execution-gateway`)**:
   - Interactive code execution belongs to the **Stage 9 isolated lab milestone** and is implemented behind a separate service (`lab-execution-gateway`) that dispatches code to **ephemeral, single-tenant gVisor-sandboxed containers** enforcing six mandatory security controls:
     1. **Authentication & Quota Gate**: Requires a verified Firebase ID Token (`email_verified == true`) and enforces a strict per-user daily execution quota (e.g., max `30 minutes` compute/day).
     2. **System Call Sandboxing**: Runs under Cloud Run Gen2 / gVisor user-space kernel interception as an unprivileged `nobody` (`UID 65534`) user with a read-only root filesystem and size-bounded `tmpfs` (`/tmp`, max `256 MiB`).
     3. **Zero Cloud IAM Identity**: The sandbox container runs with **no GCP service account permissions** and blocks all traffic to the GCE metadata server (`169.254.169.254`).
     4. **Default-Deny Network Egress**: Outbound internet access (`0.0.0.0/0`) is blocked at the VPC/egress firewall level. All required Python libraries (`numpy`, `scipy`, `matplotlib`, CPU `torch`) and approved datasets are pre-mounted read-only in the container image.
     5. **Hard Resource & Time Limits**: Maximum `1 vCPU`, `2 GiB RAM`, `60 seconds` wall-clock timeout per cell execution, `15 minutes` maximum session lifetime, and `10 MiB` maximum output payload.
     6. **Ephemeral Teardown**: The container and its `/tmp` scratch memory are destroyed immediately upon session completion or timeout; only validated stdout/stderr/plot artifacts and structured metrics are persisted as an `ExperimentResult` (`exp_<ulid>`) by `lab-execution-gateway`.

---

## 3. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. In-Process `exec()` / Subprocess Kernel Inside `core-api`** | Zero extra infrastructure. | **Catastrophic security risk**: exposes `sa-core-api` service account credentials, Firestore write access, and shared container memory to arbitrary user code. Explicitly forbidden. |
| **B. Multi-Tenant Shared JupyterHub Server Without Network Isolation** | Familiar JupyterLab interface. | Shared kernel hosts or unisolated outbound network access allow cross-user side-channel attacks, SSRF, and crypto-mining abuse. |
| **C. Browser-Only WebAssembly Execution (`Pyodide` / `JupyterLite`) in Stages 6–8** | Runs on the learner's device; zero server compute cost. | **Deferred**: Adds heavy client bundle size (`> 20 MiB` WASM/wheel downloads), mobile memory pressure, and divergent package compatibility (`torch`/C-extensions) before the core static notebook catalog is proven. We start with static notebooks and downloadable `.ipynb` artifacts in Stages 6–8; interactive code execution belongs to the Stage 9 isolated lab milestone. |

---

## 4. Consequences and Trade-Offs

### Positive
- **Complete Blast-Radius Isolation**: Even if a learner writes malicious code in a notebook cell in Stage 9, the sandbox has zero IAM credentials, zero network egress, and cannot affect `core-api`, Firestore, or other learners.
- **Early Delivery of Learning Value (Stages 6–8)**: Learners can inspect curated notebooks, study pre-computed `ExperimentResult` outputs, download `.ipynb` files, or launch them in Google Colab immediately in Stage 7 without waiting for Stage 9 sandbox infrastructure.

### Negative / Mitigations
- **No Arbitrary `pip install` from the Internet Inside Stage 9 Sandbox**: Because outbound internet egress is blocked, learners cannot install arbitrary third-party PyPI packages at runtime. *Mitigation*: Pre-bake a curated, version-pinned scientific Python environment (`numpy`, `scipy`, `pandas`, `matplotlib`, `scikit-learn`, `torch` CPU) into the sandbox image and update it via CI/CD.

---

## 5. Official References Consulted

- Jupyter Notebook Security Best Practices: https://jupyter-notebook.readthedocs.io/en/stable/security.html (Checked `2026-10-21`)
- Jupyter Server Security Documentation: https://jupyter-server.readthedocs.io/en/latest/operators/security.html (Checked `2026-10-21`)
- Cloud Run Execution Environments & gVisor Sandboxing: https://cloud.google.com/run/docs/about-execution-environments (Checked `2026-10-21`)
