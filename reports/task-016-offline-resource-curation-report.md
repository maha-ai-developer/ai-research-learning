# Task 016 Report — Offline Resource Curation for Discovered-Link Candidates

**Repository:** `maha-ai-developer/ai-research-learning`  
**Branch:** `feat/task-016-offline-resource-curation`  
**Base Commit (`main`):** `e3da6398901d858d94236bc8e6b090e22731c879`  
**Scope:** Local SQLite schema migration (`v5`), offline candidate inspection/curation CLI (`curation.py`), strict URL/fetch safety boundaries, and unit test suite (`tests/test_curation.py`). Zero live network requests or cloud provisioning performed.

---

## 1. Local Pilot Runtime Database Inspection

- **Local Path:** `data/runtime/task012_pilot.sqlite` (gitignored under `data/runtime/`; not committed and not assumed to exist in CI or clean checkouts).
- **Availability in Current Workspace:** Present (`561,152` bytes), containing the 15 canonical resources and **181** `discovered_link_candidates` persisted during the Task 012/013 bounded metadata pilot.

### Candidate Breakdown in `data/runtime/task012_pilot.sqlite`

| Referring Source | `INTERNAL` (Same-Origin) | `EXTERNAL` (Cross-Domain) | Total Candidates | `PENDING_REVIEW` | `fetch_permission_granted = 1` |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `SRC-3B1B` | 56 | 32 | **88** | 88 | 0 |
| `SRC-BENGIO` | 17 | 29 | **46** | 46 | 0 |
| `SRC-HINTON` | 32 | 15 | **47** | 47 | 0 |
| **Total** | **105** | **76** | **181** | **181** | **0** |

Top host domains observed among the 181 candidates:
- `www.3blue1brown.com`: `56` (`INTERNAL`)
- `www.cs.toronto.edu`: `32` (`INTERNAL`)
- `yoshuabengio.org`: `17` (`INTERNAL`)
- `www.youtube.com`: `15` (`EXTERNAL`)
- `youtu.be`: `11` (`EXTERNAL`)
- `mila.quebec`: `6` (`EXTERNAL`)
- `arxiv.org`: `5` (`EXTERNAL`)
- `www.nature.com`: `3` (`EXTERNAL`)

---

## 2. Forward-Only Schema Migration (`version = 5`)

Implemented migration `5` (`0005_discovered_link_curation_reviews`) in `knowledge_store.py`:

1. **Expanded `discovered_link_candidates` Table:**
   - Added `'ACCEPTED_FOR_REFERENCE'` to the `review_status` `CHECK` constraint alongside `'PENDING_REVIEW'`, `'APPROVED_FOR_MANIFEST'`, `'REJECTED'`, and `'DEFERRED'`.
   - Added curation tracking columns:
     - `reviewed_by TEXT`
     - `reviewed_at TEXT`
     - `review_reason TEXT`
     - `review_count INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0)`
     - `last_review_event_id TEXT REFERENCES provenance_events(event_id) ON DELETE SET NULL`
   - Rebuilt the table inside a transaction preserving all 25 existing columns, indexes, and foreign-key constraints without data loss.
2. **New Append-Only `discovered_link_reviews` Audit Table:**
   - Records every human curation state transition (`review_id`, `candidate_id`, `referring_resource_id`, `referring_source_id`, `original_url`, `normalized_url`, `domain_classification`, `previous_review_status`, `new_review_status`, `reviewer`, `review_reason`, `reviewed_at`, `fetch_permission_granted`, and `provenance_event_id`).
   - Enforces `CHECK (fetch_permission_granted = 0)` at the SQLite table level so a curation review event can never grant automated network fetch permission.

---

## 3. Offline Review CLI (`curation.py`) & `KnowledgeStore` API

Added `curation.py` at the repository root and corresponding methods on `KnowledgeStore`:

- **`python3 curation.py summary [--db PATH] [--source SRC] [--domain HOST] [--classification INTERNAL|EXTERNAL] [--format text|json]`**
  - Summarizes total candidates, `PENDING_REVIEW`, `ACCEPTED_FOR_REFERENCE`, `APPROVED_FOR_MANIFEST`, `REJECTED`, `DEFERRED`, `fetch_permission_granted` counts, recorded review events, and breakdowns by source, domain classification, and top host domains.
- **`python3 curation.py list [--db PATH] [--source SRC] [--resource-id RES] [--domain HOST] [--classification INTERNAL|EXTERNAL] [--review-status STATUS] [--limit N --offset M | --page P --page-size S] [--format text|json]`**
  - Lists discovered-link candidates with deterministic ordering (`first_seen_at ASC, candidate_id ASC`), filtering, and pagination metadata (`total_count`, `returned_count`, `limit`, `offset`, `has_more`).
- **`python3 curation.py export [--db PATH] [--format csv|json] [--output FILE] [...]`**
  - Exports filtered/paginated candidates to CSV or JSON with all 30 canonical candidate and review columns (`KnowledgeStore.CANDIDATE_EXPORT_COLUMNS`).
- **`python3 curation.py review [--db PATH] (--candidate-id ID | --resource-id RES --url URL) --decision STATUS --reviewer NAME --reason TEXT [--reviewed-at ISO8601] [--format text|json]`**
  - Records a human curation decision, appends an immutable row to `discovered_link_reviews`, logs a `CANDIDATE_LINK_REVIEWED` event in `provenance_events`, and updates `discovered_link_candidates`.
  - **Idempotent:** Repeating the exact same `(decision, reviewer, reason)` on a candidate already in that state returns `operation = "unchanged"` without duplicating review history or provenance rows.
- **`python3 curation.py history [--db PATH] [--candidate-id ID] [--reviewer NAME] [--format text|json]`**
  - Lists recorded review audit events in chronological order.
- **Missing or Empty Runtime Database Handling:**
  - Read-only commands (`summary`, `list`, `export`, `history`) against a non-existent `--db` path exit cleanly (`0`) with `database_exists = false` and `0` counts without creating stray SQLite files on disk.
  - `review` against a non-existent `--db` path exits with code `1` and an explicit error message on `stderr` without creating stray SQLite files.

---

## 4. Safety Boundaries Enforced

1. **Zero Network Access:** Neither `knowledge_store.py` nor `curation.py` imports HTTP clients or opens network sockets. Verified under mocked `socket.socket` / `socket.create_connection` guards.
2. **Separation of Curation and Fetching:**
   - Discovering a link never triggers a fetch.
   - Reviewing a candidate as `ACCEPTED_FOR_REFERENCE` or `APPROVED_FOR_MANIFEST` **never** sets `fetch_permission_granted = 1` (`fetch_permission_granted` is forced to `0` in both `discovered_link_candidates` and `discovered_link_reviews`) and **never** automatically promotes the candidate into `resources`. Any future metadata collection must independently pass the source-specific policy gate.
3. **Preserved Audit Lineage:** `original_url`, `normalized_url`, `referring_resource_id`, `referring_source_id`, and `from_url` are preserved intact across repeated discoveries and curation reviews.
4. **Strict URL & Destination Validation (`validate_and_canonicalize_url`):**
   - Rejects non-HTTP(S) schemes (`javascript:`, `data:`, `file:`, `mailto:`, `ftp:`, `vbscript:`), embedded credentials (`user:pass@host`, `@host`), backslashes in authority, malformed domain labels, and prohibited destinations (loopback `localhost`/`127.0.0.0/8`/`::1`, RFC1918 private networks `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, link-local / cloud metadata `169.254.0.0/16` / `metadata.google.internal`, and internal TLDs `.localhost`, `.local`, `.internal`, `.home.arpa`, `.localdomain`).
   - Before recording any non-rejection curation decision (`ACCEPTED_FOR_REFERENCE`, `APPROVED_FOR_MANIFEST`, etc.), `review_discovered_link_candidate` re-validates `from_url`, `original_url`, and `normalized_url` so tampered or unsafe URLs cannot be accepted.

---

## 5. Verification & Test Results

- `python3 -m py_compile knowledge_store.py collect.py curation.py` — **Passed**
- `python3 -m unittest discover -s tests -v` — **48 tests ran, 48 passed (0 failures, 0 errors)**
  - `tests/test_curation.py` (6 test methods covering filtering, pagination, CSV/JSON export, review persistence, idempotency, external non-fetchable invariant, unsafe URL rejection, missing/empty runtime DBs, and schema `v4 -> v5` migration including a temporary copy of `data/runtime/task012_pilot.sqlite`).
  - `tests/test_knowledge_store.py` (11 test methods)
  - `tests/test_source_adapters.py` (23 test methods)
  - `tests/test_collector.py` (8 test methods)
- `python3 collect.py --db :memory:` — **Passed** (`DRY RUN ONLY: no network requests made and no document records collected.`)

---

## 6. Remaining Limitations

1. **Local Single-Curator Storage:** Review decisions are persisted in the local SQLite database; multi-user concurrent review and Cloud Firestore synchronization remain deferred until cloud provisioning is explicitly approved.
2. **Manifest Promotion Remains Separate:** Marking a candidate `ACCEPTED_FOR_REFERENCE` or `APPROVED_FOR_MANIFEST` records the human curation status and rationale for reference/planning, but does not automatically mutate `seed_urls.json` or trigger live metadata fetching.
