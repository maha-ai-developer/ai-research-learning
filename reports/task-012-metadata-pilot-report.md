# Task 012 — Source Adapters & Policy-Gated Metadata Pilot Report

- **Date:** 2026-10-24
- **Branch:** `feat/task-012-source-adapters-metadata-pilot`
- **Manifest Import Run ID (Offline):** `run-20261024T081349Z-a108e2ba`
- **Metadata Pilot Run ID (Live Bounded):** `run-20261024T081350Z-9fb2e560`
- **Pilot Window (UTC):** `2026-10-24T08:13:50+00:00` → `2026-10-24T08:14:30+00:00`
- **Configured Pilot Bounds:** `max_per_source = 2`, `max_total = 10`

---

## 1. Executive Summary

This report documents the implementation of the modular source-adapter package (`src/ai_research_learning/sources/`) and the execution of a single bounded, policy-gated metadata pilot across the five canonical sources defined in [`docs/architecture/source-adapter-contract.md`](../docs/architecture/source-adapter-contract.md):

1. **Geoffrey Hinton (`SRC-HINTON`)** — `hinton-html-adapter/0.1` (`HintonSourceAdapter`)
2. **Yoshua Bengio (`SRC-BENGIO`)** — `bengio-html-adapter/0.1` (`BengioSourceAdapter`)
3. **3Blue1Brown (`SRC-3B1B`)** — `threeblueonebrown-html-adapter/0.1` (`ThreeBlueOneBrownAdapter`)
4. **arXiv (`SRC-ARXIV`)** — `arxiv-atom-api-adapter/0.1` (`ArxivSourceAdapter`)
5. **English Wikipedia (`SRC-WIKIPEDIA`)** — `wikipedia-mediawiki-api-adapter/0.1` (`WikipediaSourceAdapter`)

All adapters remain decoupled from SQLite internals and emit normalized envelopes (`NormalizedResourceEnvelope`, `PolicyAuditResult`, `DiscoveredLinkCandidate`) that `PilotCoordinator` persists through the existing `KnowledgeStore` API in [`knowledge_store.py`](../knowledge_store.py). Default CLI execution (`python3 collect.py`) remains a 100% offline dry run making zero network requests.

---

## 2. Source-Policy Audit Results

Before any resource metadata request was attempted, each adapter audited its applicable `robots.txt` and official API/site policy endpoint using the project's descriptive `User-Agent` (`AIResearchLearningExplorer/0.1 (+https://github.com/maha-ai-developer/ai-research-learning; metadata-only-pilot)`).

| Source ID | Adapter ID | Policy Endpoint Audited | Audit Timestamp (UTC) | HTTP Status | Observed Crawl-Delay / Rate Limit | Required Identification | Allowed Collection Scope | Metadata Collection Decision |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SRC-HINTON` | `hinton-html-adapter/0.1` | `https://www.cs.toronto.edu/robots.txt` | `2026-10-24T08:13:50+00:00` | `200 OK` | `1.0s` published in `robots.txt` (`5.0s` conservative floor enforced by adapter) | Descriptive `User-Agent` with project/repository URL | `https://www.cs.toronto.edu/~hinton/*` (HTML metadata only; no PDF/slide binary download; no cross-owner `/~tijmen/*` crawl) | **PERMITTED** (`status=OK`) |
| `SRC-BENGIO` | `bengio-html-adapter/0.1` | `https://yoshuabengio.org/robots.txt` | `2026-10-24T08:14:01+00:00` | `200 OK` | `5.0s` conservative floor enforced by adapter (33 CMS admin/system `Disallow` rules respected) | Descriptive `User-Agent` with project/repository URL | `https://yoshuabengio.org/en*` (HTML metadata only; no full blog text storage; no external media scraping) | **PERMITTED** (`status=OK`) |
| `SRC-3B1B` | `threeblueonebrown-html-adapter/0.1` | `https://www.3blue1brown.com/robots.txt` (Terms: `https://www.3blue1brown.com/about/`) | `2026-10-24T08:14:11+00:00` | `200 OK` | `5.0s` conservative floor enforced by adapter; sitemap `https://www.3blue1brown.com/sitemap.xml` advertised | Descriptive `User-Agent` with project/repository URL | `https://www.3blue1brown.com/`, `/topics/*`, `/lessons/*` (lesson/topic metadata & official links only; no full lesson text, video, or transcript capture) | **PERMITTED** (`status=OK`) |
| `SRC-ARXIV` | `arxiv-atom-api-adapter/0.1` | `https://export.arxiv.org/robots.txt` (API Terms: `https://info.arxiv.org/help/api/tou.html`) | `2026-10-24T08:14:27+00:00` | `200 OK` | `>= 3.0s` sequential single-connection delay required by arXiv API User Manual | Descriptive `User-Agent` with project URL | `https://export.arxiv.org/api/query?*` per API Terms of Use, **blocked in pilot** because `https://export.arxiv.org/robots.txt` serves `User-agent: *` / `Disallow: /` | **DENIED / BLOCKED (Fail-Closed)** (`status=BLOCKED`) |
| `SRC-WIKIPEDIA` | `wikipedia-mediawiki-api-adapter/0.1` | `https://en.wikipedia.org/robots.txt` (API Policy: `https://www.mediawiki.org/wiki/Wikimedia_APIs/Access_policy`) | `2026-10-24T08:14:28+00:00` | `200 OK` | Sequential `1.0s` minimum delay; honors HTTP `429`/`503` `Retry-After` headers; in-memory response caching | Mandatory `User-Agent` and `Api-User-Agent` headers linking to repository | `https://en.wikipedia.org/w/api.php?action=query*` (official MediaWiki API metadata + `oldid` revision tracking) | **PERMITTED** (`status=OK`) |

---

## 3. Exact Live HTTP Requests Made (By Source, With Counts)

Across the entire bounded pilot (`run-20261024T081350Z-9fb2e560`), exactly **13 live HTTP GET requests** were made (`5` policy-audit requests + `8` metadata requests):

| Source ID | Policy Requests | Metadata Requests | Total Live Requests | Exact URLs Requested (`HTTP Status` & Timestamp UTC) |
| :--- | :---: | :---: | :---: | :--- |
| `SRC-HINTON` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://www.cs.toronto.edu/robots.txt` (`HTTP 200`, `08:13:50Z`)<br>2. `[METADATA_FETCH]` `https://www.cs.toronto.edu/~hinton/index.html` (`HTTP 200`, `08:13:56Z`)<br>3. `[METADATA_FETCH]` `https://www.cs.toronto.edu/~hinton/pages/publications.html` (`HTTP 200`, `08:14:01Z`) |
| `SRC-BENGIO` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://yoshuabengio.org/robots.txt` (`HTTP 200`, `08:14:01Z`)<br>2. `[METADATA_FETCH]` `https://yoshuabengio.org/en` (`HTTP 200`, `08:14:06Z`)<br>3. `[METADATA_FETCH]` `https://yoshuabengio.org/en/research` (`HTTP 200`, `08:14:11Z`) |
| `SRC-3B1B` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://www.3blue1brown.com/robots.txt` (`HTTP 200`, `08:14:11Z`)<br>2. `[METADATA_FETCH]` `https://www.3blue1brown.com/` (`HTTP 200`, `08:14:17Z`)<br>3. `[METADATA_FETCH]` `https://www.3blue1brown.com/topics/neural-networks` → `307/308` same-host redirect → `https://www.3blue1brown.com/?topic=neural-networks` (`HTTP 200`, `08:14:27Z`) |
| `SRC-ARXIV` | `1` | `0` | **`1`** | 1. `[POLICY_AUDIT]` `https://export.arxiv.org/robots.txt` (`HTTP 200`, `08:14:27Z`) — returned `Disallow: /`; adapter failed closed and issued **0** requests to `https://export.arxiv.org/api/query`. |
| `SRC-WIKIPEDIA` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://en.wikipedia.org/robots.txt` (`HTTP 200`, `08:14:28Z`)<br>2. `[METADATA_FETCH]` `https://en.wikipedia.org/w/api.php?action=query&prop=info%7Crevisions%7Cpageprops%7Ccategories&inprop=url&rvprop=ids%7Ctimestamp&rvslots=main&cllimit=10&redirects=1&format=json&formatversion=2&titles=Backpropagation` (`HTTP 200`, `08:14:29Z`)<br>3. `[METADATA_FETCH]` `https://en.wikipedia.org/w/api.php?action=query&prop=info%7Crevisions%7Cpageprops%7Ccategories&inprop=url&rvprop=ids%7Ctimestamp&rvslots=main&cllimit=10&redirects=1&format=json&formatversion=2&titles=Boltzmann_machine` (`HTTP 200`, `08:14:30Z`) |
| **Total** | **`5`** | **`8`** | **`13`** | Zero PDFs, media files, full papers, or video transcripts were requested or downloaded. |

*Note on `SRC-3B1B` redirect:* `https://www.3blue1brown.com/topics/neural-networks` issued a same-host redirect to `https://www.3blue1brown.com/?topic=neural-networks`, which was validated against host/path scope and `robots.txt` before following (`5.0s` per-origin delay enforced on each hop).

---

## 4. Resources Inserted, Updated, Skipped, or Blocked

### 4.1 Summary Counts (`run-20261024T081350Z-9fb2e560`)

- **Resources Inserted (`v1` created):** `4` (`2` from `SRC-3B1B`, `2` from `SRC-WIKIPEDIA`)
- **Resources Updated (`v1` → `v2` version history created):** `4` (`2` from `SRC-HINTON`, `2` from `SRC-BENGIO` — enriching the initial offline manifest records with live titles, authors, `ETag`/`Last-Modified` headers, and metadata hashes)
- **Sources Blocked by Policy Gate:** `1` (`SRC-ARXIV`, skipping its `2` candidate API queries `1207.0580` and `1409.0473` without making content requests)
- **Network / Parse Errors:** `0`
- **Discovered Outbound/Candidate Links Logged (Not Fetched):** `181` across `discovered_links`

### 4.2 Detailed Resource Records Persisted

| Action | Source ID | Resource ID | Version ID (`version_count`) | Manifest ID | Canonical URL | Title & Key Metadata | Rights / Storage Scope |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **UPDATED** | `SRC-HINTON` | `res_0098016d9acf1f99` | `ver_20261024_1ae147ff_v2` (`v2`) | `HI-URL-001` | `https://www.cs.toronto.edu/~hinton/index.html` | **Geoffrey Hinton**; author: `Geoffrey E. Hinton`; 25 in-scope HTML subpages and 3 PDF citation URLs catalogued without download | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| **UPDATED** | `SRC-HINTON` | `res_e344b54452f01c96` | `ver_20261024_84423c36_v2` (`v2`) | `HI-URL-002` | `https://www.cs.toronto.edu/~hinton/pages/publications.html` | **Publications - Geoffrey Hinton**; author: `Geoffrey E. Hinton` | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| **UPDATED** | `SRC-BENGIO` | `res_73e51a454ee1f4eb` | `ver_20261024_2dda1aa9_v2` (`v2`) | `YB-URL-001` | `https://yoshuabengio.org/en` | **Home \| Yoshua Bengio**; `ETag: W/"1792079761"`, `Last-Modified: Thu, 15 Oct 2026 15:56:01 GMT`; meta description & 12 section headings | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| **UPDATED** | `SRC-BENGIO` | `res_5fb3d618c7ed17ec` | `ver_20261024_8e10c18d_v2` (`v2`) | `YB-URL-002` | `https://yoshuabengio.org/en/research` | **Research \| Yoshua Bengio**; `ETag: W/"1792080116"`, `Last-Modified: Thu, 15 Oct 2026 16:01:56 GMT`; outbound reference `https://arxiv.org/abs/2606.29657` recorded | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| **INSERTED** | `SRC-3B1B` | `res_b6377e0dd4279dae` | `ver_20261024_f4d4c999_v1` (`v1`) | `3B-URL-001` | `https://www.3blue1brown.com/` | **3Blue1Brown**; author: `Grant Sanderson (3Blue1Brown)`; official links: `@3blue1brown`, `@grantsanderson`, `github.com/3b1b` | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| **INSERTED** | `SRC-3B1B` | `res_63c0a0d121a09f38` | `ver_20261024_9386e06b_v1` (`v1`) | `3B-URL-002` | `https://www.3blue1brown.com/topics/neural-networks` | **3Blue1Brown** (Neural Networks topic index); 28 in-scope lesson/topic links catalogued without recursive crawl | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| **INSERTED** | `SRC-WIKIPEDIA` | `res_24d64fe06f34c722` | `ver_20261024_842a8401_v1` (`v1`) | `None` (API) | `https://en.wikipedia.org/wiki/Backpropagation` | **Backpropagation**; `page_id=1360091`, `revision_id=1381710221` (`2026-10-24T08:06:45Z`), `wikidata_qid=Q798503` | `CC_BY_SA_4_0` (`CC-BY-SA-4.0`, stored as `METADATA_ONLY` in pilot) |
| **INSERTED** | `SRC-WIKIPEDIA` | `res_dd13dd0076cd4a6e` | `ver_20261024_d1f8525a_v1` (`v1`) | `None` (API) | `https://en.wikipedia.org/wiki/Boltzmann_machine` | **Boltzmann machine**; `page_id=1166059`, `revision_id=1380453709` (`2026-10-15T20:10:07Z`), `wikidata_qid=Q194706` | `CC_BY_SA_4_0` (`CC-BY-SA-4.0`, stored as `METADATA_ONLY` in pilot) |
| **BLOCKED** | `SRC-ARXIV` | *(none)* | *(none)* | *(none)* | `https://export.arxiv.org/robots.txt` | Provenance event `ADAPTER_SOURCE_BLOCKED_BY_POLICY` recorded; 2 candidate queries (`1207.0580`, `1409.0473`) skipped | Fail-closed (`POLICY_BLOCKED`) |

---

## 5. Discovered Links Classification Summary

During the metadata pilot, `181` anchor links were extracted from the 6 HTML pages and recorded in `discovered_links` without being fetched:

- `in-scope-html` (`decision = candidate-only-no-recursive-crawl`): **99 links** (`26` on `SRC-HINTON`, `17` on `SRC-BENGIO`, `56` on `SRC-3B1B`)
- `external-not-fetched` (`decision = skip-external-host`): **60 links** (`15` on `SRC-HINTON`, `19` on `SRC-BENGIO`, `26` on `SRC-3B1B`)
- `external-official-reference` (`decision = record-official-external-reference-only`): **16 links** (`10` on `SRC-BENGIO` including `https://arxiv.org/abs/2606.29657` and `https://mila.quebec/`, `6` on `SRC-3B1B` including `https://www.youtube.com/@3blue1brown` and `https://github.com/3b1b`)
- `same-host-binary-not-fetched` (`decision = record-citation-url-only-no-download`): **5 links** on `SRC-HINTON` (`minefield.pdf`, `shortcv.pdf`, `HintonMumbai.pdf`, `screen.gif`, `sound.gif`)
- `same-host-outside-prefix` (`decision = skip-outside-prefix`): **1 link** on `SRC-HINTON` (`https://www.cs.toronto.edu/~tijmen/csc321/`)

---

## 6. Unresolved Site Policies & Outstanding Technical/Licensing Questions

### 6.1 Unresolved Site Policy: `SRC-ARXIV` (`export.arxiv.org/robots.txt` vs. API Terms of Use)

- **Observed Conflict:** arXiv's official API User Manual (`https://info.arxiv.org/help/api/user-manual.html`) and API Terms of Use (`https://info.arxiv.org/help/api/tou.html`) instruct automated clients to query `http://export.arxiv.org/api/query` (or `https://export.arxiv.org/api/query`) with a minimum 3-second delay between requests. However, the machine-readable `https://export.arxiv.org/robots.txt` endpoint currently serves:
  ```text
  User-agent: *
  Disallow: /
  ```
  (designed to prevent general web search engines from indexing duplicate HTML mirror pages on `export.arxiv.org`).
- **Fail-Closed Pilot Behavior:** In strict compliance with Task 012 Section 3 and Section 5 (*"If a site policy cannot be fetched or interpreted reliably, fail closed... Stop collecting from any source whose policy is unresolved or whose responses indicate blocking"*), `ArxivSourceAdapter` did not override `export.arxiv.org/robots.txt` unilaterally. Instead, it recorded `status = BLOCKED` and `ADAPTER_SOURCE_BLOCKED_BY_POLICY` in `provenance_events` and made zero requests to `/api/query`.
- **Architectural Decision Needed:** Confirm whether `ArxivSourceAdapter` should treat `https://info.arxiv.org/help/api/tou.html` (and `https://arxiv.org/robots.txt`) as the authoritative policy governing `/api/query` on `export.arxiv.org`, rather than applying web-crawler `Disallow: /` from `export.arxiv.org/robots.txt` to the dedicated Atom API endpoint.

### 6.2 Distinction Between Verified Access Permission and Content Reuse Rights

1. **Metadata Retrieval vs. Full-Content Redistribution:**
   - Passing a `robots.txt` check (`SRC-HINTON`, `SRC-BENGIO`, `SRC-3B1B`) verifies **crawl/access permission** for conservative metadata indexing only; it does **not** grant a copyright license to store or redistribute full HTML bodies, blog articles, PDF papers, slide decks, or video transcripts.
   - All `SRC-HINTON`, `SRC-BENGIO`, and `SRC-3B1B` records are persisted with `permitted_storage_scope = "METADATA_ONLY"`, `permits_full_text_storage = 0`, and `permits_verbatim_excerpts = 0`.
2. **3Blue1Brown (`SRC-3B1B`) Lesson & Transcript Boundaries:**
   - Per `https://www.3blue1brown.com/about/`, full lessons and visuals are proprietary (`ALL_RIGHTS_RESERVED_METADATA_ONLY`). Also, `https://www.3blue1brown.com/topics/neural-networks` redirects to `https://www.3blue1brown.com/?topic=neural-networks` and declares `<link rel="canonical" href="https://www.3blue1brown.com/">` in its HTML head. Our adapter preserves the requested topic URL (`https://www.3blue1brown.com/topics/neural-networks`) as the resource's `canonical_url` while recording `html_declared_canonical_url` in `source_specific_metadata` so topic index pages do not collide with the root homepage.
3. **Wikipedia (`SRC-WIKIPEDIA`) ShareAlike Attribution:**
   - Although English Wikipedia articles are licensed under `CC-BY-SA-4.0` (`permits_full_text_storage = 1`, `permits_verbatim_excerpts = 1`), this pilot stored **metadata and permanent revision identifiers (`oldid`) only** (`permitted_storage_scope = "METADATA_ONLY"`, `text_storage_status = "metadata-only"`). Any future milestone that ingests Wikipedia article text or excerpts must preserve the exact `permanent_revision_url` (`oldid`) and `CC BY-SA 4.0` ShareAlike notice on all downstream views and derivative summaries.
