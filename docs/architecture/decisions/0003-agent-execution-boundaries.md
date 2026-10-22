# ADR 0003 — Bounded Agent Workflows, Epistemic Governance, and Citation Verification Gates

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0003` |
| **Date** | `2026-10-22` |
| **Status** | `Proposed` |
| **Related Docs** | [`agent-workflows.md`](../agent-workflows.md), [`data-model.md`](../data-model.md) |

---

## 1. Context and Problem Statement

An AI research and learning system faces three acute reliability and governance risks when employing Large Language Models (LLMs):
1. **Unbounded Autonomous Loops**: Multi-agent systems that dynamically spawn sub-agents, browse arbitrary external URLs, or mutate canonical database records without deterministic bounds can cause runaway token costs, policy violations, and corrupted curricula.
2. **Hallucinated Citations**: LLMs frequently fabricate paper titles, section numbers, timestamps, or quotes when answering technical questions.
3. **Epistemic Flattening**: LLMs often present unpeer-reviewed preprints, personal conjectures, or AI-generated pedagogical analogies with the same tone of certainty as proven mathematical theorems.

---

## 2. Decision

1. **Bounded State-Machine Orchestration (Not Autonomous Microservices)**:
   - The five logical agent roles (`WF-01-DISCOVERY`, `WF-02-CURATION`, `WF-03-ANALYSIS`, `WF-04-LEARNING-PATH`, `WF-05-EVALUATION`) are implemented as **deterministic, typed Python state machines** within `ingestion-worker` (offline batch) and `core-api` (online bounded request-response).
   - Each workflow has a strict tool allowlist, hard token budget (`max_input_tokens`, `max_output_tokens`), maximum retry count (`<= 2`), and wall-clock timeout.
   - Peer-to-peer autonomous agent negotiation and arbitrary external URL fetching during learner Q&A are strictly prohibited.
2. **Mandatory Epistemic Status Classification**:
   - Every generated `Claim` (`clm_<ulid>`) must be classified into one of four mutually exclusive epistemic categories:
     - `ESTABLISHED_EVIDENCE`
     - `AUTHOR_OPINION`
     - `PRELIMINARY_RESEARCH`
     - `GENERATED_INTERPRETATION`
   - Automatic downgrade rules enforce that claims relying solely on unrefereed preprints cannot be labeled `ESTABLISHED_EVIDENCE`, and AI-synthesized analogies/bridges must be labeled `GENERATED_INTERPRETATION`.
3. **Deterministic Post-Generation Verification Gates (`G-01` through `G-05`)**:
   - Before any agent output is persisted or returned to a learner, deterministic Python validators (not an LLM judge alone) verify:
     - **`G-01` Citation Resolution**: Every `citation_id` resolves to an existing, active `ResourceVersion` and `KnowledgeChunk` retrieved during that specific `AgentRun` (`run_...`).
     - **`G-02` Quote-Span & Excerpt Permission Verification**: Every non-null `quoted_span` is permitted **only** when the source `ResourceVersion` has `permits_verbatim_excerpts == true` and `legal_uncertainty_status != 'UNVERIFIED_REQUIRES_HUMAN_REVIEW'`, and matches the normalized substring in the source chunk (`>= 0.95` token overlap) with valid page/timestamp locator bounds. There is no automatic character-length "fair use" exemption; when `permits_verbatim_excerpts == false`, `quoted_span` must be `null` and the citation relies on metadata, page/timestamp locators, and outbound source links.
     - **`G-03` License, Redistribution & Legal Uncertainty Gate**: Blocks storage or display of full text, media, or verbatim excerpts unless an open license or explicit permission has been verified, and flags any `UNVERIFIED_REQUIRES_HUMAN_REVIEW` resource for human review rather than guessing copyright status automatically.
     - **`G-04` Epistemic Consistency Gate**: Enforces epistemic downgrade rules.
     - **`G-05` DAG Cycle & Prerequisite Gate**: Rejects any concept relation or learning path with cycle or topological order violations.
4. **Human-in-the-Loop (HITL) Approval for Canonical Curriculum Changes**:
   - Additions of new source domains, new `Concept` nodes (`status: 'draft' -> 'verified'`), and `REQUIRES` prerequisite graph mutations require explicit `curator` or `admin` approval.

---

## 3. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. Fully Autonomous Multi-Agent Mesh (e.g., Open-Ended Conversation Between Agents)** | Flexible exploration for open-ended tasks. | Unpredictable latency (`30s+`), unbounded token spend, high failure rate on strict schema/citation invariants, and difficult auditability. |
| **B. Pure Prompt-Only Citation Instructions (No Deterministic Post-Check)** | Zero validation code overhead. | LLMs still hallucinate citation IDs or misquote passages under distribution shift; unacceptable for a source-grounded scientific learning system. |
| **C. LLM-as-a-Judge Only for Citation Verification** | Can evaluate semantic paraphrases. | Doubles inference latency and cost on every request and remains probabilistic; we use deterministic code for `G-01`/`G-02` and reserve semantic scoring for `WF-05-EVALUATION` benchmarks. |

---

## 4. Consequences and Trade-Offs

### Positive
- **Zero Fabricated Citations**: Any response that fails `G-01` or `G-02` is caught deterministically before reaching the learner.
- **Transparent Scientific Pedagogy**: Learners see whether a statement is an established mathematical result, a pioneer's perspective (e.g., Hinton on mortal computation), a recent arXiv preprint, or an AI-generated teaching analogy.
- **Predictable Cost & Latency**: Every workflow execution has bounded retries and token caps logged in `agent_runs`.

### Negative / Mitigations
- **Higher Abstention Rate on Sparse Topics**: If the retrieved chunks do not contain verifiable evidence to pass `G-01`/`G-02`, the tutor abstains rather than guessing. *Mitigation*: When abstaining, the API returns relevant catalog search results and clearly states which concepts lack indexed primary coverage.
