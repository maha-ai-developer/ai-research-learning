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
├── seed_urls.json              # 52-record source manifest
├── collection_plan.csv         # initial collection plan
├── inventory/                  # previous inventory deliverables
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

Run the offline unit tests:

```bash
python -m unittest discover -s tests -v
python -m compileall -q collect.py knowledge_store.py tests
```

## 3. Before enabling network collection

First read `docs/DATA_AND_REUSE_POLICY.md` and `docs/JUPYTER_LAB_INTEGRATION.md`.

Configure a real project contact in your user-agent and check policies from your own network:

```bash
python collect.py \
  --fetch \
  --policy-only \
  --user-agent "AIResearchLearningBot/0.1 (https://your-domain.example/contact)"
```

Review the recorded `robots.txt` evidence and sitemap endpoint checks. If a policy cannot be verified, the collector should fail closed. Then, only after reviewing the policy results and reuse permissions, run a small batch:

```bash
python collect.py \
  --fetch \
  --limit 10 \
  --per-source-limit 5 \
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
