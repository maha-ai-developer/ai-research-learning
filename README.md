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

## 2.1 Offline Transcript Data Model, Authorization Attestations, and Local Import Workflow (Schema v6 & v7)

Schema migrations `0006_transcripts_and_segments` and `0007_transcript_authorization_attestations` (`LATEST_SCHEMA_VERSION = 7`) extend the local SQLite store with six transcript persistence and authorization tables defined in `docs/architecture/transcript-ingestion.md` (`ARCH-TRANSCRIPT-007`) and `docs/architecture/decisions/0006-transcript-acquisition-and-storage.md` (`ADR-0006`):

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
```

### Transcript Subsystem Status: Implemented vs. Deferred

| Capability | Status | Notes |
| --- | --- | --- |
| Forward-only SQLite migrations v6 & v7 (`0006_transcripts_and_segments`, `0007_transcript_authorization_attestations`) | **Implemented** | Preserves all v1–v6 tables, resources, versions, candidates, and provenance |
| Deterministic IDs (`tr_*`, `tatt_*`, `tver_*`, `tseg_*`, `tsyn_*`, `tslnk_*`) | **Implemented** | Content-addressed and sequence-addressed SHA-256 identifiers |
| Auditable authorization attestations & scope enforcement (storage, quoting, AI synthesis, redistribution) | **Implemented** | Blocks caller-supplied permission flags unless backed by a `VERIFIED`, non-expired, non-withdrawn attestation |
| Offline parser & normalizer (`WEBVTT`, `SRT`, `PLAIN_TEXT`) + validation (`TV-01`..`TV-08`) | **Implemented** | Strips markup, extracts speaker labels, flags low-confidence cues, validates intervals |
| Fail-closed policy gate & SQLite triggers blocking unauthorized text storage | **Implemented** | Missing/pending/withdrawn/expired attestation or `ALL_RIGHTS_RESERVED` stores 0 segments |
| Idempotent re-imports, version superseding (`ACTIVE` -> `SUPERSEDED`), and takedown/attestation withdrawal cascading | **Implemented** | Purges segments/syntheses on takedown or attestation withdrawal while retaining `TOMBSTONED` audit history |
| Live remote caption downloading (`OFFICIAL_PLATFORM_API`, YouTube/Vimeo HTTP fetching) | **Deferred** | Requires future task & explicit platform API credentials/terms review |
| Automated Speech Recognition (`LOCAL_ASR_WHISPER` audio download & model execution) | **Deferred** | Schema fields exist, but no audio downloading or ASR execution is implemented |
| Vector embeddings, semantic transcript search, learning UI, and cloud synchronization | **Deferred** | Local SQLite only; no Firebase/GCP provisioning or vector indexing |

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
