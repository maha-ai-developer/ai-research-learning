# AI Research & Learning Explorer

An AI-engineering project for building a source-grounded learning environment from the public research and teaching material of Geoffrey Hinton and Yoshua Bengio. The first milestone is a **metadata-only inventory collector**; later milestones can add Wikipedia and arXiv connectors, search/RAG, a learning UI, and an isolated notebook lab.

> **Current status:** source inventory and metadata collector scaffold are included. The collector defaults to an offline dry run. No live pages were collected in this build. Verify each site's current crawling policy and reuse terms before enabling network collection.

## Project principles

1. **Source before synthesis:** retain original URLs, dates, source identity, and evidence.
2. **Metadata first:** catalogue titles, dates, authors, topic labels, canonical URLs, and identifiers before retaining page text or full papers.
3. **Traceability:** generated explanations should link back to the supporting source material.
4. **Add sources through connectors:** Hinton, Bengio, Wikipedia, and arXiv should map into a shared document model.
5. **Keep code execution isolated:** notebooks and user code run in a separate sandboxed service, not in the web API process.
6. **No hidden collection:** obey access controls and crawling policies; do not treat `robots.txt` as a copyright licence.

## Repository layout

```text
ai-research-learning/
├── collect.py                  # metadata-only collector; dry-run by default
├── knowledge_store.py          # local SQLite persistence store, migrations, and manifest importer
├── src/
│   └── ai_research_learning/
│       ├── api.py              # local read-only Research Library HTTP API (v1)
│       └── sources/            # modular source adapters (Hinton, Bengio, 3B1B, arXiv, Wikipedia) & pilot coordinator
├── seed_urls.json              # 52-record source manifest
├── collection_plan.csv         # initial collection plan
├── inventory/                  # previous inventory deliverables
├── reports/
│   ├── repository-bootstrap-report.md
│   └── task-012-metadata-pilot-report.md
├── notebooks/
│   └── 00_inventory_exploration.ipynb
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_AND_REUSE_POLICY.md
│   └── JUPYTER_LAB_INTEGRATION.md
├── tests/                      # offline unit tests
├── .github/workflows/tests.yml # CI test workflow
├── requirements.txt            # collector runtime dependencies
└── requirements-notebooks.txt  # optional notebook dependencies
```

The collector creates `data/runtime/inventory.sqlite` when first run. Runtime database files and export outputs are excluded from Git by `.gitignore`. The older SQLite inventories in `inventory/` are archived snapshots, not the live runtime database.

## 1. Setup on Linux / Debian / Raspberry Pi

```bash
python3 --version
# Python 3.10+ recommended

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 2. Inspect the inventory without network access

```bash
python collect.py
```

This is a **dry run** and sends no web requests. It should show the selected URLs and skipped/excluded records. The collector creates its local database only when running.

Initialize the local SQLite knowledge store (`data/runtime/inventory.sqlite`), import the 52-record `seed_urls.json` manifest offline, and inspect store status:

```bash
python knowledge_store.py init
python knowledge_store.py import-manifest
python knowledge_store.py status
```

You can also run initialization or manifest import via `collect.py`:

```bash
python collect.py --init-db
python collect.py --import-manifest
```

Run the offline unit tests and syntax verification:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q collect.py curation.py knowledge_store.py src tests
```

## 2.1 Offline Transcript Data Model, Authorization Attestations, and Local Import Workflow (Schema v6, v7 & v8)

Schema migrations `0006_transcripts_and_segments`, `0007_transcript_authorization_attestations`, and `0008_harden_transcript_authorization_triggers` (`LATEST_SCHEMA_VERSION = 8`) extend the local SQLite store with six transcript persistence and authorization tables defined in `docs/architecture/transcript-ingestion.md` (`ARCH-TRANSCRIPT-007`) and `docs/architecture/decisions/0006-transcript-acquisition-and-storage.md` (`ADR-0006`):

- `transcripts` — canonical transcript registry, license/rights status, authorization basis, redistribution permission (`permits_redistribution`), and 8-state ingestion machine (`DISCOVERED`, `RIGHTS_PENDING`, `READY_FOR_INGESTION`, `INGESTED`, `NEEDS_REVIEW`, `BLOCKED_BY_POLICY`, `METADATA_ONLY_LINKED`, `WITHDRAWN_OR_TOMBSTONED`).
- `transcript_authorization_attestations` — auditable authorization records (`tatt_*`, added in Migration 7) capturing who verified permission to store/quote/synthesize/redistribute a transcript (`reviewed_by`, `reviewed_at`), the authorization basis (`OPEN_LICENSE_VERIFIED`, `PUBLIC_DOMAIN_VERIFIED`, `FIRST_PARTY_OWNED`, `WRITTEN_PERMISSION`, `NONE`), the evidence reference/artifact (`evidence_type`, `evidence_reference`, `evidence_url`, `evidence_sha256`), granular scope flags (`permits_metadata_indexing`, `permits_transcript_storage`, `permits_verbatim_excerpts`, `permits_ai_synthesis`, `permits_redistribution`), verification status (`VERIFIED`, `PENDING_REVIEW`, `CONTRADICTORY`, `REJECTED`, `WITHDRAWN`, `EXPIRED`, `UNKNOWN`), expiry (`expires_at`), and withdrawal tracking.
- `transcript_versions` — immutable version ledger (`tver_*`) tracking SHA-256 digests (`raw_content_sha256`, `normalized_text_sha256`), normalizer version (`norm-v1.0`), segment/word/character counts, and confidence statistics.
- `transcript_segments` — normalized, timestamped segment cues (`tseg_*`) with monotonic `start_ms` / `end_ms`, preserved `original_segment_text`, `normalized_segment_text`, speaker attribution, quality flags, and timestamped deep links (`#t=<seconds>s` or `?t=<seconds>`).
- `transcript_syntheses` — transformative study notes/summaries (`tsyn_*`) enforced to exclude verbatim transcript dumps and gated by `permits_ai_synthesis`.
- `transcript_segment_links` — cross-modal links (`tslnk_*`) connecting transcript segments to concepts, paper resources, modules, equations, or chunks.

### Offline Transcript CLI Usage

All transcript commands in `knowledge_store.py` are strictly offline and make zero network requests. **Caller-supplied flags (`--permits-storage`, `--permits-excerpts`, `--permit-redistribution`) are never trusted on their own**: storing transcript text requires a `VERIFIED`, non-withdrawn, non-expired record in `transcript_authorization_attestations` matching the resource (and transcript/language/track scope) whose verified scope covers every requested permission. Note that this is an auditable engineering control and human-verifiable evidence ledger, not an automated legal determination.

```bash
# Step 1: Record a verified authorization attestation with concrete evidence and explicit scope:
python3 knowledge_store.py attest-transcript \
  --db data/runtime/inventory.sqlite \
  --resource-id <res_id> \
  --language en \
  --track-kind PRIMARY \
  --authorization-basis OPEN_LICENSE_VERIFIED \
  --license-status CC_BY_4_0 \
  --license-spdx CC-BY-4.0 \
  --evidence-type SPDX_HEADER_OR_PAGE_FOOTER \
  --evidence-reference "Lecture page footer declares CC BY 4.0 license" \
  --evidence-url "https://example.org/lectures/1" \
  --reviewed-by "curator:alice" \
  --verification-status VERIFIED \
  --permit-storage \
  --permit-verbatim-excerpts \
  --permit-ai-synthesis

# Step 2: Import a local WebVTT (.vtt), SubRip (.srt), or plain-text (.txt) transcript referencing the verified attestation:
python3 knowledge_store.py import-transcript \
  --db data/runtime/inventory.sqlite \
  --resource-id <res_id> \
  --attestation-id <tatt_id> \
  --file path/to/transcript.vtt \
  --language en \
  --track-kind PRIMARY \
  --origin-type MANUAL_CURATED_IMPORT \
  --acquisition-method FIRST_PARTY_CURATED_IMPORT \
  --license-status CC_BY_4_0 \
  --license-spdx CC-BY-4.0 \
  --authorization-basis CREATIVE_COMMONS_LICENSE \
  --attribution "Author Name — Lecture Title (CC BY 4.0)" \
  --permits-storage \
  --permits-excerpts

# Record a metadata-only or policy-blocked transcript entry (stores 0 rows in transcript_versions/transcript_segments):
python3 knowledge_store.py import-transcript \
  --db data/runtime/inventory.sqlite \
  --resource-id <res_id> \
  --language en \
  --acquisition-method METADATA_AND_LINK_ONLY \
  --license-status ALL_RIGHTS_RESERVED \
  --authorization-basis NONE \
  --attribution "External Lecture (All Rights Reserved — Metadata & Deep-Link Only)"

# Inspect attestations, list transcripts, inspect segments, or withdraw an attestation / transcript on takedown:
python3 knowledge_store.py list-transcript-attestations --db data/runtime/inventory.sqlite --resource-id <res_id>
python3 knowledge_store.py list-transcripts --db data/runtime/inventory.sqlite
python3 knowledge_store.py show-transcript <tr_id> --db data/runtime/inventory.sqlite --show-segments
python3 knowledge_store.py withdraw-transcript-attestation <tatt_id> --db data/runtime/inventory.sqlite --withdrawn-by "curator:alice" --reason "Rights withdrawal request"
python3 knowledge_store.py withdraw-transcript <tr_id> --db data/runtime/inventory.sqlite --reason "Rights withdrawal request"
python3 knowledge_store.py expire-transcripts --db data/runtime/inventory.sqlite
```

### Transcript Subsystem Status: Implemented vs. Deferred

| Capability | Status | Notes |
| --- | --- | --- |
| Forward-only SQLite migrations v6, v7 & v8 (`0006_transcripts_and_segments`, `0007_transcript_authorization_attestations`, `0008_harden_transcript_authorization_triggers`) | **Implemented** | Preserves all v1–v7 tables, resources, versions, candidates, and provenance |
| Deterministic IDs (`tr_*`, `tatt_*`, `tver_*`, `tseg_*`, `tsyn_*`, `tslnk_*`) | **Implemented** | Content-addressed and sequence-addressed SHA-256 identifiers |
| Auditable authorization attestations & scope enforcement (storage, quoting, AI synthesis, redistribution) | **Implemented** | Enforces operation-specific permissions (`permits_transcript_storage`, `permits_verbatim_excerpts`, `permits_ai_synthesis`, `permits_redistribution`) across all read/write methods and redacts segment text by default in `get_transcript_segments(..., include_text=False)` unless `permits_redistribution=1` |
| Offline parser & normalizer (`WEBVTT`, `SRT`, `PLAIN_TEXT`) + validation (`TV-01`..`TV-08`) | **Implemented** | Strips markup, extracts speaker labels, flags low-confidence cues, validates intervals |
| Fail-closed policy gate & hardened SQLite triggers blocking direct-SQL bypasses | **Implemented** | SQLite write triggers enforce verified non-expired attestation scope on `transcripts`, `transcript_versions`, `transcript_segments`, `transcript_syntheses`, and `provenance_events`, plus automatic revocation cascades |
| Idempotent re-imports, version superseding (`ACTIVE` -> `SUPERSEDED`), takedown/attestation withdrawal cascading, and natural expiration enforcement | **Implemented** | Purges segments/syntheses and tombstones transcripts on takedown, attestation withdrawal, or natural `expires_at` expiration across all initialization, read, write, and `expire-transcripts` maintenance paths |
| Live remote caption downloading (`OFFICIAL_PLATFORM_API`, YouTube/Vimeo HTTP fetching) | **Deferred** | Requires future task & explicit platform API credentials/terms review |
| Automated Speech Recognition (`LOCAL_ASR_WHISPER` audio download & model execution) | **Deferred** | Schema fields exist, but no audio downloading or ASR execution is implemented |
| Vector embeddings, semantic transcript search, learning UI, and cloud synchronization | **Deferred** | Local SQLite only; no Firebase/GCP provisioning or vector indexing |

## 2.2 Local Research Library HTTP API (Read-Only `v1`)

`src/ai_research_learning/api.py` provides a local, zero-dependency, strictly read-only HTTP API (`v1`) over the SQLite `KnowledgeStore`.

### Safety & Architecture Boundaries

- **Strictly read-only:** Every database connection is opened with SQLite URI `mode=ro` and enforces `PRAGMA query_only = ON`. No API route can mutate database state, and only `GET` requests are accepted (`405 Method Not Allowed` with `Allow: GET` on other HTTP methods).
- **Zero network requests:** All endpoints read exclusively from local SQLite tables (`sources`, `site_policies`, `resources`, `resource_versions`, `transcripts`, `transcript_versions`, `transcript_authorization_attestations`).
- **Loopback binding by default:** The server binds to `127.0.0.1:8000` by default.
- **Transcript content protection boundary:** `GET /api/v1/transcripts` and `GET /api/v1/transcripts/{transcript_id}` expose registry and `transcript_versions` status metadata only (`content_disclosure_policy: "METADATA_ONLY_NO_SEGMENTS_OR_SYNTHESES"`). They never query or return `transcript_segments`, verbatim excerpts, protected transcript text, or `transcript_syntheses` content. Naturally expired or withdrawn attestations are projected in-memory as `WITHDRAWN_OR_TOMBSTONED` / `TOMBSTONED_ON_TAKEDOWN` without mutating the database.
- **Current schema search scope (`metadata_lexical`):** Because `knowledge_chunks`, FTS5 virtual tables, and vector embeddings are deferred in the current schema (`v8`), `GET /api/v1/search?q=...` performs deterministic parameterized lexical search with relevance scoring over stored `resources` metadata columns (`title`, `abstract_or_description`, `authors`, `topic_tags`, `external_identifier`, `canonical_url`, `manifest_id`, `domain_scope`).

### Starting the Local API Server

```bash
# Ensure the local SQLite database is initialized and seeded:
python3 knowledge_store.py init --db data/runtime/inventory.sqlite
python3 knowledge_store.py import-manifest --db data/runtime/inventory.sqlite

# Start the local read-only HTTP server on 127.0.0.1:8000:
python3 -m src.ai_research_learning.api --db data/runtime/inventory.sqlite --host 127.0.0.1 --port 8000
```

### Endpoint Reference & Example `curl` Requests

| Endpoint | Description | Supported Query Parameters |
| --- | --- | --- |
| `GET /healthz` | Service health and SQLite schema readiness | *(none)* |
| `GET /api/v1/sources` | Registered sources, `site_policies` status, and resource/transcript counts | `status`, `source_category`, `adapter_type`, `only_complete` |
| `GET /api/v1/resources` | Paginated resource inventory | `source_id`, `resource_type`, `media_modality`, `lifecycle_state`, `collection_decision`, `limit` (`1..100`), `offset` (`>=0`) |
| `GET /api/v1/resources/{resource_id}` | Single resource metadata, `resource_versions` history, and metadata-only `transcripts_summary` | *(none)* |
| `GET /api/v1/search?q=...` | Deterministic lexical search over resource metadata | `q` *(required)*, `source_id`, `resource_type`, `media_modality`, `lifecycle_state`, `limit` (`1..100`), `offset` (`>=0`) |
| `GET /api/v1/transcripts` | Paginated transcript registry metadata (no segment text) | `resource_id`, `source_id`, `language_bcp47`, `track_kind`, `ingestion_state`, `license_status`, `transcript_storage_status`, `limit` (`1..100`), `offset` (`>=0`) |
| `GET /api/v1/transcripts/{transcript_id}` | Single transcript metadata, attestation summary, and `transcript_versions` status (no segment or synthesis text) | *(none)* |

```bash
# 1. Check service and database health:
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool

# 2. List registered sources and crawl-policy status:
curl -s "http://127.0.0.1:8000/api/v1/sources?only_complete=true" | python3 -m json.tool

# 3. List resources with pagination and source filter:
curl -s "http://127.0.0.1:8000/api/v1/resources?source_id=SRC-HINTON&limit=5&offset=0" | python3 -m json.tool

# 4. Inspect a single resource (metadata, version history, and transcript summary):
curl -s "http://127.0.0.1:8000/api/v1/resources/res_0123456789abcdef" | python3 -m json.tool

# 5. Search resource metadata:
curl -s "http://127.0.0.1:8000/api/v1/search?q=backpropagation&limit=10" | python3 -m json.tool

# 6. List transcript registry metadata:
curl -s "http://127.0.0.1:8000/api/v1/transcripts?limit=10&offset=0" | python3 -m json.tool

# 7. Inspect a single transcript's metadata and version-status ledger:
curl -s "http://127.0.0.1:8000/api/v1/transcripts/tr_0123456789abcdef" | python3 -m json.tool
```

## 2.3 Research Question CLI & Evidence-First Learning Workflow (Task 032)

`src/ai_research_learning/rag.py` provides a read-only, evidence-first command-line research assistant over the local SQLite knowledge store (`data/runtime/task012_pilot.sqlite`) and persistent Chroma vector index (`data/runtime/chroma`, collection `ai_research_knowledge_chunks_v1`).

### 1. Prerequisites and Setup

1. Ensure the project virtual environment is created and has `chromadb` and `requests` installed:
   ```bash
   .venv/bin/python -m pip install -r requirements-notebooks.txt chromadb requests
   ```
2. Verify that the local runtime SQLite database (`data/runtime/task012_pilot.sqlite`) and Chroma vector store (`data/runtime/chroma`) exist.
3. Optional `.env` configuration (at repository root with mode `600`, ignored by Git):
   - The CLI automatically loads `.env` without overriding explicitly exported shell variables.
   - See `.env.example` for all supported `AI_LEARNING_*` environment variables.

### 2. Configuring Vertex AI (`kerala-lottery-intel-dev`) and ADC

When `gemini` is used for embeddings or answer generation, the RAG pipeline connects to Google Cloud Vertex AI using Application Default Credentials (ADC) on project `kerala-lottery-intel-dev` (default location `global`). It never uses or sends `GEMINI_API_KEY` on the Vertex AI path:

```bash
gcloud config set project kerala-lottery-intel-dev
gcloud auth application-default login
gcloud auth application-default set-quota-project kerala-lottery-intel-dev
```

- Default Vertex AI generation model: `gemini-3.8-flash` (at `global` endpoint)
- Default Vertex AI embedding model (for new `gemini` collections): `gemini-embedding-001` (`768` dimensions, `us-central1`)
- The existing `ai_research_knowledge_chunks_v1` collection (`18` vectors) is locked to `local_deterministic/token-hash-bow-v1@128d` for embeddings and can be queried immediately with `--generation-provider gemini` or `--generation-provider retrieval_only`.

### 3. Running the Research Question CLI

```bash
# Display CLI usage and examples:
.venv/bin/python -m src.ai_research_learning.rag --help

# Ask a research question using Vertex AI Gemini (ADC on kerala-lottery-intel-dev):
.venv/bin/python -m src.ai_research_learning.rag \
  "How does backpropagation compute gradients in a neural network?" \
  --generation-provider gemini

# Ask a research question in offline retrieval-only mode (no external LLM calls):
.venv/bin/python -m src.ai_research_learning.rag \
  "How does backpropagation compute gradients in a neural network?" \
  --generation-provider retrieval_only

# Filter by canonical source_id and emit plain text or structured JSON:
.venv/bin/python -m src.ai_research_learning.rag \
  --question "How does residual learning help train deeper neural networks?" \
  --source-id SRC-ARXIV \
  --generation-provider gemini \
  --format text

.venv/bin/python -m src.ai_research_learning.rag \
  --question "What is a Boltzmann machine?" \
  --generation-provider retrieval_only \
  --format json
```

### 4. Example Grounded Answer and Abstention Outputs

**Grounded Answer Example (`status: "answered"`, exit code `0`):**

```markdown
# Research Question & Evidence-Grounded Response

- **Question:** How does backpropagation compute gradients in a neural network?
- **Pipeline Status:** `answered`
- **Sufficient Evidence:** `True`
- **Embedding Configuration:** `local_deterministic/token-hash-bow-v1@128d` (collection: `ai_research_knowledge_chunks_v1`)
- **Generation Configuration:** `gemini/gemini-3.8-flash`

## 1. Generated Interpretation (`GENERATED_INTERPRETATION`)

*Epistemic Layer: `GENERATED_INTERPRETATION` | Synthesized by `gemini/gemini-3.8-flash` | Cited Chunks: `chk_0a6023ab662bc6b4`, `chk_258896475e232c3d`*

Based on the provided sources, backpropagation computes the gradient of the loss with respect to the network weights for a single input–output example through the following process:
* **Application of the Chain Rule:** It serves as an efficient application of the chain rule to neural networks [chk_0a6023ab662bc6b4].
* **Backward Layer-by-Layer Propagation:** It computes the gradients by propagating derivatives backward, one layer at a time, starting from the output layer and moving toward the input layer [chk_258896475e232c3d].

## 2. Verified Source Citations (`SourceCitation`)

1. **`[chk_0a6023ab662bc6b4]` Backpropagation**
   - **Canonical URL:** https://en.wikipedia.org/wiki/Backpropagation
   - **Identifiers:** `chunk_id=chk_0a6023ab662bc6b4` | `resource_id=res_24d64fe06f34c722` | `version_id=ver_20261024_842a8401_v1` | `source_id=SRC-WIKIPEDIA`
   - **Provenance & Offsets:** `epistemic_layer=PRIMARY_SOURCE_TEXT` | `content_origin=RESOURCE_FULL_TEXT` | `chars=0..364`
   - **License:** `CC_BY_SA_4_0 (CC-BY-SA-4.0)`
```

**Explicit Abstention Example (`status: "insufficient_evidence"`, exit code `0`):**

```bash
.venv/bin/python -m src.ai_research_learning.rag \
  "What fermentation temperature is best for sourdough rye bread starter?" \
  --generation-provider gemini
```

```markdown
# Research Question & Evidence-Grounded Response

- **Question:** What fermentation temperature is best for sourdough rye bread starter?
- **Pipeline Status:** `insufficient_evidence`
- **Sufficient Evidence:** `False`

## 1. Generated Interpretation (`GENERATED_INTERPRETATION`)

> **ABSTAINED — INSUFFICIENT EVIDENCE**
> No indexed source chunks met the minimum relevance threshold (0.22) for this question.

No answer was generated and no citations were fabricated.
```

### 5. Citation and Provenance Semantics

- **Read-Only Query Execution:** The CLI opens SQLite via `ReadOnlySQLiteStore` (`mode=ro`, `PRAGMA query_only = ON`) and Chroma with `create_if_missing=False`. Querying cannot modify `data/runtime/task012_pilot.sqlite` or `data/runtime/chroma`.
- **Epistemic Layer Separation:**
  - Section 1 (`GENERATED_INTERPRETATION`) contains only the model-synthesized explanation (or explicit abstention/retrieval-only banner).
  - Section 2 (`SourceCitation`) lists every cited chunk with `chunk_id`, `resource_id`, `version_id`, `source_id`, `canonical_url`, `char_start..char_end`, and license status.
  - Section 3 (`PRIMARY_SOURCE_TEXT` / `AUTHORIZED_ABSTRACT_OR_DESCRIPTION`) displays the verbatim retrieved source chunks separately from generated interpretation.
- **Exit Code Semantics:**
  - `0`: Successful execution, **including legitimate abstentions (`insufficient_evidence`)**.
  - `1`: Operational/runtime errors (`FileNotFoundError` for missing database/collection, `sqlite3.Error`, or upstream HTTP/provider `RuntimeError`).
  - `2`: CLI usage, configuration, missing credential (`ProviderNotConfiguredError`), or embedding compatibility (`EmbeddingIncompatibilityError`) errors.

### 6. Troubleshooting and Safe Credential Handling

- **`EmbeddingIncompatibilityError` (Exit Code `2`):** Raised if `--embedding-provider`, `--embedding-model`, or `--embedding-dimensions` does not match the metadata stored in the target Chroma collection (`local_deterministic/token-hash-bow-v1@128d` on `ai_research_knowledge_chunks_v1`). Omit `--embedding-provider` when querying the default collection, or specify a separate `--collection` when testing a different embedding space.
- **`ProviderNotConfiguredError` (Exit Code `2`):** Raised when an explicitly requested generation provider (`gemini`, `openai`, or `anthropic`) lacks valid ADC or API key credentials. Pass `--allow-retrieval-fallback` if you want the CLI to degrade gracefully to `retrieval_only_fallback` instead of exiting with code `2`.
- **Credential Redaction:** All CLI error messages pass through `redact_secrets(...)`, which masks `AIza...`, `sk-...`, `ya29...`, `Bearer ...`, and any configured secret values as `[REDACTED]` before writing to `stderr`. Never pass raw API keys on the command line; store them in `.env` (`chmod 600 .env`) or use Vertex AI ADC.

## 3. Before enabling network collection

First read `docs/DATA_AND_REUSE_POLICY.md` and `docs/JUPYTER_LAB_INTEGRATION.md`.

Configure a real project contact in your user-agent and audit policies across the five canonical source adapters (`SRC-HINTON`, `SRC-BENGIO`, `SRC-3B1B`, `SRC-ARXIV`, `SRC-WIKIPEDIA`):

```bash
python collect.py \
  --policy-audit \
  --user-agent "AIResearchLearningExplorer/0.1 (+https://github.com/maha-ai-developer/ai-research-learning; metadata-only-pilot)"
```

Review the recorded `robots.txt` and API policy evidence in `site_policies`, `crawl_audit`, and `provenance_events`. Any source whose policy cannot be verified or disallows target routes fails closed automatically. To run the bounded metadata-only pilot (`<= 2` items per source, `<= 10` items total):

```bash
python collect.py \
  --metadata-pilot \
  --pilot-max-per-source 2 \
  --pilot-max-total 10 \
  --user-agent "AIResearchLearningExplorer/0.1 (+https://github.com/maha-ai-developer/ai-research-learning; metadata-only-pilot)"
```

You can also run the legacy HTML-only policy check or batch:

```bash
python collect.py \
  --fetch \
  --policy-only \
  --user-agent "AIResearchLearningBot/0.1 (https://your-domain.example/contact)"
```

The collector restricts URLs to approved first-party site/path scopes, honours published crawl delays and enforces a minimum one-second delay between requests to the same origin. It collects HTML metadata only, does not save full page bodies, and records discovered links for review instead of automatically fetching them. Verify these safeguards and tests against the current code before broader collection.

## 4. Use the notebook for exploratory work

```bash
python -m pip install -r requirements-notebooks.txt
jupyter lab
```

Open `notebooks/00_inventory_exploration.ipynb`. It analyses the local seed manifest; it does not need web access and does not execute arbitrary user-submitted code.

## 5. Publish this local Git repository to GitHub

The ZIP supplied with this project contains an initialized local Git repository and an initial commit. In the extracted project directory, review the files first:

```bash
git status
git log --oneline -3
```

Install and authenticate GitHub CLI if needed. GitHub's `gh repo create` supports pushing an existing local repository to a new remote; the following creates a **private** repo by default:

```bash
gh auth login
gh repo create ai-research-learning \
  --private \
  --source=. \
  --remote=origin \
  --push
```

Use `--public` only after reviewing all contents, licensing notes, and what data should be public. Do not add tokens, `.env` files, credentials, private documents, or unlicensed full-text materials. If the repository name already exists, choose another name or create the remote repo first and add its URL as `origin`.

Official instructions: <https://cli.github.com/manual/gh_repo_create> and <https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github>.

## Roadmap

- [x] Initial source inventory and seed manifest
- [x] Metadata collector scaffold and offline tests
- [x] Notebook for inventory exploration
- [x] Architecture notes for a secure JupyterLab-backed learning lab
- [ ] Verify robots/sitemap policy live from a network-enabled environment
- [ ] Collect and review the first small metadata batch
- [ ] Add source-specific parsers for publication lists and blog pagination
- [ ] Add Wikipedia and arXiv metadata connectors
- [ ] Add search and retrieval with source citations
- [ ] Add a user-facing learning interface
- [ ] Add an isolated notebook execution service only after authentication and resource limits are designed

## Source sites

- Geoffrey Hinton: <https://www.cs.toronto.edu/~hinton/>
- Yoshua Bengio: <https://yoshuabengio.org/en>
- MediaWiki REST API: <https://www.mediawiki.org/wiki/API:REST_API>
- arXiv API: <https://info.arxiv.org/help/api/index.html>
