# Task 012 & Task 013 — Source Adapters & Policy-Gated Metadata Pilot Report

- **Date:** 2026-10-24
- **Branch:** `feat/task-012-source-adapters-metadata-pilot`
- **Manifest Import Run ID (Offline):** `run-20261024T081349Z-a108e2ba`
- **Initial Metadata Pilot Run ID (Live Bounded, Fail-Closed on arXiv):** `run-20261024T081350Z-9fb2e560` (`2026-10-24T08:13:50+00:00` → `2026-10-24T08:14:30+00:00`)
- **Bounded arXiv API Pilot Run ID (Post-ADR-0005):** `run-20261024T091658Z-a5f7d42f` (`2026-10-24T09:16:58+00:00` → `2026-10-24T09:17:02+00:00`)
- **Configured Pilot Bounds:** `max_per_source = 2`, `max_total = 10`
- **Total Pilot Resources Collected Across All Runs:** `10` (`2` per source across 5 sources; `<= 10` cap strictly preserved)

---

## 1. Executive Summary

This report documents the implementation of the modular source-adapter package (`src/ai_research_learning/sources/`), the initial fail-closed metadata pilot (`run-20261024T081350Z-9fb2e560`), the architectural resolution of the arXiv API access policy in [`docs/architecture/decisions/0005-arxiv-api-access-policy.md`](../docs/architecture/decisions/0005-arxiv-api-access-policy.md), the subsequent bounded arXiv metadata pilot (`run-20261024T091658Z-a5f7d42f`), and the persistence of discovered anchor links as reviewable candidates in `discovered_link_candidates` (schema version `4`).

The five canonical sources defined in [`docs/architecture/source-adapter-contract.md`](../docs/architecture/source-adapter-contract.md) are:

1. **Geoffrey Hinton (`SRC-HINTON`)** — `hinton-html-adapter/0.1` (`HintonSourceAdapter`)
2. **Yoshua Bengio (`SRC-BENGIO`)** — `bengio-html-adapter/0.1` (`BengioSourceAdapter`)
3. **3Blue1Brown (`SRC-3B1B`)** — `threeblueonebrown-html-adapter/0.1` (`ThreeBlueOneBrownAdapter`)
4. **arXiv (`SRC-ARXIV`)** — `arxiv-atom-api-adapter/0.1` (`ArxivSourceAdapter`)
5. **English Wikipedia (`SRC-WIKIPEDIA`)** — `wikipedia-mediawiki-api-adapter/0.1` (`WikipediaSourceAdapter`)

All adapters remain decoupled from SQLite internals and emit normalized envelopes (`NormalizedResourceEnvelope`, `PolicyAuditResult`, `DiscoveredLinkCandidate`) that `PilotCoordinator` persists through the `KnowledgeStore` API in [`knowledge_store.py`](../knowledge_store.py). Default CLI execution (`python3 collect.py`) remains a 100% offline dry run making zero network requests.

---

## 2. Source-Policy Audit Results & Historical Fail-Closed Record

Before any resource metadata request was attempted, each adapter audited its applicable `robots.txt` and official API/site policy endpoint using the project's descriptive `User-Agent` (`AIResearchLearningExplorer/0.1 (+https://github.com/maha-ai-developer/ai-research-learning; metadata-only-pilot)`).

### 2.1 Initial Pilot Policy Audit (`run-20261024T081350Z-9fb2e560`)

| Source ID | Adapter ID | Policy Endpoint Audited | Audit Timestamp (UTC) | HTTP Status | Observed Crawl-Delay / Rate Limit | Required Identification | Allowed Collection Scope | Initial Pilot Decision |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SRC-HINTON` | `hinton-html-adapter/0.1` | `https://www.cs.toronto.edu/robots.txt` | `2026-10-24T08:13:50+00:00` | `200 OK` | `1.0s` published in `robots.txt` (`5.0s` conservative floor enforced by adapter) | Descriptive `User-Agent` with project/repository URL | `https://www.cs.toronto.edu/~hinton/*` (HTML metadata only; no PDF/slide binary download; no cross-owner `/~tijmen/*` crawl) | **PERMITTED** (`status=OK`) |
| `SRC-BENGIO` | `bengio-html-adapter/0.1` | `https://yoshuabengio.org/robots.txt` | `2026-10-24T08:14:01+00:00` | `200 OK` | `5.0s` conservative floor enforced by adapter (33 CMS admin/system `Disallow` rules respected) | Descriptive `User-Agent` with project/repository URL | `https://yoshuabengio.org/en*` (HTML metadata only; no full blog text storage; no external media scraping) | **PERMITTED** (`status=OK`) |
| `SRC-3B1B` | `threeblueonebrown-html-adapter/0.1` | `https://www.3blue1brown.com/robots.txt` (Terms: `https://www.3blue1brown.com/about/`) | `2026-10-24T08:14:11+00:00` | `200 OK` | `5.0s` conservative floor enforced by adapter; sitemap `https://www.3blue1brown.com/sitemap.xml` advertised | Descriptive `User-Agent` with project/repository URL | `https://www.3blue1brown.com/`, `/topics/*`, `/lessons/*` (lesson/topic metadata & official links only; no full lesson text, video, or transcript capture) | **PERMITTED** (`status=OK`) |
| `SRC-ARXIV` | `arxiv-atom-api-adapter/0.1` | `https://export.arxiv.org/robots.txt` (API Terms: `https://info.arxiv.org/help/api/tou.html`) | `2026-10-24T08:14:27+00:00` | `200 OK` | `>= 3.0s` sequential single-connection delay required by arXiv API User Manual | Descriptive `User-Agent` with project URL | `https://export.arxiv.org/api/query?*` per API Terms of Use, **blocked in initial pilot** because `https://export.arxiv.org/robots.txt` serves `User-agent: *` / `Disallow: /` | **DENIED / BLOCKED (Fail-Closed)** (`status=BLOCKED`) |
| `SRC-WIKIPEDIA` | `wikipedia-mediawiki-api-adapter/0.1` | `https://en.wikipedia.org/robots.txt` (API Policy: `https://www.mediawiki.org/wiki/Wikimedia_APIs/Access_policy`) | `2026-10-24T08:14:28+00:00` | `200 OK` | Sequential `1.0s` minimum delay; honors HTTP `429`/`503` `Retry-After` headers; in-memory response caching | Mandatory `User-Agent` and `Api-User-Agent` headers linking to repository | `https://en.wikipedia.org/w/api.php?action=query*` (official MediaWiki API metadata + `oldid` revision tracking) | **PERMITTED** (`status=OK`) |

### 2.2 Task 013 & Task 014 Policy Resolution: Explicit `ARXIV_OFFICIAL_ATOM_API_V1` Profile and Global Rate-Limit Boundary (`run-20261024T091658Z-a5f7d42f`)

Following architectural review of the initial fail-closed audit, [`docs/architecture/decisions/0005-arxiv-api-access-policy.md`](../docs/architecture/decisions/0005-arxiv-api-access-policy.md) (`ADR-0005/2026-10-24`) and [`docs/architecture/source-adapter-contract.md`](../docs/architecture/source-adapter-contract.md) established a narrowly scoped policy profile (`ARXIV_OFFICIAL_ATOM_API_V1`) distinguishing:

1. **General HTML/PDF Web Crawling (`robots.txt` Fail-Closed Preserved):**
   - `https://export.arxiv.org/robots.txt` (`Disallow: /`) remains strictly enforced for all general HTML/PDF crawling on `export.arxiv.org` (`general_html_crawling_permitted = False`).
2. **Official arXiv Atom Metadata API (`https://export.arxiv.org/api/query`):**
   - Governed by the [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html) and [arXiv API User Manual](https://info.arxiv.org/help/api/user-manual.html).
   - Scoped exclusively to `GET https://export.arxiv.org/api/query` with single-connection sequential execution (`ARXIV_MAX_CONCURRENCY = 1`), `>= 3.0s` delay between consecutive requests (`ARXIV_MIN_INTERVAL_SECONDS = 3.0`), batched `id_list` queries (`max_results <= 2` in pilot), descriptive `User-Agent`, and metadata/abstract-only persistence (`permitted_storage_scope = "METADATA_AND_ABSTRACT"`, `permits_full_text_storage = False`, no PDF/e-print downloads).
   - **Global Rate-Limit Boundary Across All Project-Controlled Machines:** Per `https://info.arxiv.org/help/api/tou.html`, the `>= 3.0s` minimum interval and single-connection (`max_concurrency = 1`) limit apply **across all machines controlled by the project**, not merely within a single thread or adapter instance.
   - **Supported Runtime Execution Boundary (`LOCAL_SINGLE_WORKER_SERIALIZED`):** In the current local SQLite runtime, all `export.arxiv.org` requests pass through a process-wide mutex gate (`_ArxivProcessRateLimitGate`) and per-origin connection slot (`OriginRateLimiter.connection_slot`) so concurrent calls across threads or multiple in-process `ArxivSourceAdapter` instances are strictly serialized (`max_in_flight == 1`) with `>= 3.0s` spacing. Parallel arXiv requests and multiple independently scheduled arXiv workers are prohibited.
   - **No Retry Storms (Fail-Closed Circuit Breaker):** Any HTTP `401`/`403`/`451` access block or HTTP `429`/`503` rate-limit response latches a fail-closed circuit breaker on `ArxivSourceAdapter` and halts remaining candidates for `SRC-ARXIV` in `PilotCoordinator` without automatic retry loops.
   - **Future Distributed Deployment Requirement:** Deploying multiple independent `ArxivSourceAdapter` instances across separate OS processes, containers, or cloud machines is **explicitly prohibited** until the project implements either (a) a single dedicated serialized API worker (`max_instances = 1`, `concurrency = 1`) through which all arXiv API calls are routed, or (b) a shared global rate-limit lease coordinator enforcing a single `>= 3.0s` schedule and `max_concurrency = 1` across all project-controlled workers.

| Source ID | Adapter ID | Policy Profile & Version | Audit Timestamp (UTC) | Policy Endpoint Audited (`HTTP Status`) | General HTML Crawl Status | Approved API Endpoint & Execution Boundary | Post-ADR-0005 Decision |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SRC-ARXIV` | `arxiv-atom-api-adapter/0.1` | `ARXIV_OFFICIAL_ATOM_API_V1` (`ADR-0005/2026-10-24`) | `2026-10-24T09:16:59+00:00` | `https://export.arxiv.org/robots.txt` (`200 OK`) + `https://info.arxiv.org/help/api/tou.html` | `DISALLOWED_BY_ROBOTS_TXT` (`general_html_crawling_permitted=False`) | `https://export.arxiv.org/api/query` (`>= 3.0s` delay, `max_concurrency=1`, `LOCAL_SINGLE_WORKER_SERIALIZED`) | **PERMITTED (API-Only Profile)** (`status=OK`, `outcome=policy-verified-arxiv-api-profile-ok`) |

---

## 3. Exact Live HTTP Requests Made (By Source, With Counts)

### 3.1 Initial Bounded Pilot (`run-20261024T081350Z-9fb2e560`) — 13 Live HTTP GET Requests

In the initial Task 012 pilot (`run-20261024T081350Z-9fb2e560`), **13 live HTTP GET requests** were made (`5` policy-audit requests + `8` metadata requests):

| Source ID | Policy Requests | Metadata Requests | Total Live Requests | Exact URLs Requested (`HTTP Status` & Timestamp UTC) |
| :--- | :---: | :---: | :---: | :--- |
| `SRC-HINTON` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://www.cs.toronto.edu/robots.txt` (`HTTP 200`, `08:13:50Z`)<br>2. `[METADATA_FETCH]` `https://www.cs.toronto.edu/~hinton/index.html` (`HTTP 200`, `08:13:56Z`)<br>3. `[METADATA_FETCH]` `https://www.cs.toronto.edu/~hinton/pages/publications.html` (`HTTP 200`, `08:14:01Z`) |
| `SRC-BENGIO` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://yoshuabengio.org/robots.txt` (`HTTP 200`, `08:14:01Z`)<br>2. `[METADATA_FETCH]` `https://yoshuabengio.org/en` (`HTTP 200`, `08:14:06Z`)<br>3. `[METADATA_FETCH]` `https://yoshuabengio.org/en/research` (`HTTP 200`, `08:14:11Z`) |
| `SRC-3B1B` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://www.3blue1brown.com/robots.txt` (`HTTP 200`, `08:14:11Z`)<br>2. `[METADATA_FETCH]` `https://www.3blue1brown.com/` (`HTTP 200`, `08:14:17Z`)<br>3. `[METADATA_FETCH]` `https://www.3blue1brown.com/topics/neural-networks` → `307/308` same-host redirect → `https://www.3blue1brown.com/?topic=neural-networks` (`HTTP 200`, `08:14:27Z`) |
| `SRC-ARXIV` | `1` | `0` | **`1`** | 1. `[POLICY_AUDIT]` `https://export.arxiv.org/robots.txt` (`HTTP 200`, `08:14:27Z`) — returned `Disallow: /`; adapter failed closed and issued **0** requests to `https://export.arxiv.org/api/query`. |
| `SRC-WIKIPEDIA` | `1` | `2` | **`3`** | 1. `[POLICY_AUDIT]` `https://en.wikipedia.org/robots.txt` (`HTTP 200`, `08:14:28Z`)<br>2. `[METADATA_FETCH]` `https://en.wikipedia.org/w/api.php?action=query&prop=info%7Crevisions%7Cpageprops%7Ccategories&inprop=url&rvprop=ids%7Ctimestamp&rvslots=main&cllimit=10&redirects=1&format=json&formatversion=2&titles=Backpropagation` (`HTTP 200`, `08:14:29Z`)<br>3. `[METADATA_FETCH]` `https://en.wikipedia.org/w/api.php?action=query&prop=info%7Crevisions%7Cpageprops%7Ccategories&inprop=url&rvprop=ids%7Ctimestamp&rvslots=main&cllimit=10&redirects=1&format=json&formatversion=2&titles=Boltzmann_machine` (`HTTP 200`, `08:14:30Z`) |
| **Subtotal (Run 1)** | **`5`** | **`8`** | **`13`** | Zero PDFs, media files, full papers, or video transcripts were requested or downloaded. |

### 3.2 Bounded arXiv API Pilot (`run-20261024T091658Z-a5f7d42f`) — 2 Live HTTP GET Requests (1 API Query)

In Task 013 (`run-20261024T091658Z-a5f7d42f`), `ArxivSourceAdapter` executed a single bounded pass under `ARXIV_OFFICIAL_ATOM_API_V1`, issuing **1** policy audit request and **1** batched API query returning **2** metadata records:

| Source ID | Policy Requests | Metadata API Queries | Total Live Requests | Exact URLs Requested (`HTTP Status` & Timestamp UTC) |
| :--- | :---: | :---: | :---: | :--- |
| `SRC-ARXIV` | `1` | `1` | **`2`** | 1. `[POLICY_AUDIT]` `https://export.arxiv.org/robots.txt` (`HTTP 200`, `2026-10-24T09:16:59+00:00`, `outcome="policy-verified-arxiv-api-profile-ok"`)<br>2. `[METADATA_FETCH]` `https://export.arxiv.org/api/query?id_list=1207.0580%2C1409.0473&max_results=2` (`HTTP 200`, `2026-10-24T09:17:02+00:00`, `outcome="fetched-ok"`, executed after `>= 3.0s` delay) |

### 3.3 Cumulative Network Request & Resource Totals Across Task 012 + Task 013

- **Total Live HTTP Requests (Task 012 + Task 013):** `15` (`6` policy-audit `robots.txt` requests + `9` metadata requests across 5 sources)
- **Live arXiv API Queries Executed:** `1` (`id_list=1207.0580,1409.0473&max_results=2`, returning `2` Atom `<entry>` records)
- **Total Pilot-Collected Resources in Store:** `10` (`2` from `SRC-HINTON`, `2` from `SRC-BENGIO`, `2` from `SRC-3B1B`, `2` from `SRC-WIKIPEDIA`, `2` from `SRC-ARXIV` — strictly within `max_per_source = 2` and `max_total = 10`)
- **Total Binary / Full-Text / PDF Requests:** `0`

---

## 4. Resources Inserted, Updated, Skipped, or Blocked

### 4.1 Cumulative Summary Counts

- **Resources Inserted (`v1` created):** `6` (`2` from `SRC-3B1B`, `2` from `SRC-WIKIPEDIA` in `run-20261024T081350Z-9fb2e560`; `2` from `SRC-ARXIV` in `run-20261024T091658Z-a5f7d42f`)
- **Resources Updated (`v1` → `v2` version history created):** `4` (`2` from `SRC-HINTON`, `2` from `SRC-BENGIO` in `run-20261024T081350Z-9fb2e560` — enriching the initial offline manifest records with live titles, authors, `ETag`/`Last-Modified` headers, and metadata hashes)
- **Total Pilot Resources Collected:** **`10` / `10`** (`2` per source across all 5 sources)
- **Historical Policy Blocks Preserved in Audit Log:** `1` (`ADAPTER_SOURCE_BLOCKED_BY_POLICY` from `run-20261024T081350Z-9fb2e560` remains intact in `provenance_events`)
- **Network / Parse Errors:** `0`
- **Discovered Link Candidates Persisted (Not Fetched):** `181` in `discovered_link_candidates` (and `181` in `discovered_links` audit log)

### 4.2 Detailed Resource Records Persisted (All 10 Pilot Resources)

| Run ID | Action | Source ID | Resource ID | Version ID (`version_count`) | External / Manifest ID | Canonical URL | Title & Key Metadata | Rights / Storage Scope |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `run-20261024T081350Z-9fb2e560` | **UPDATED** | `SRC-HINTON` | `res_0098016d9acf1f99` | `ver_20261024_1ae147ff_v2` (`v2`) | `HI-URL-001` | `https://www.cs.toronto.edu/~hinton/index.html` | **Geoffrey Hinton**; author: `Geoffrey E. Hinton`; 25 in-scope HTML subpages and 3 PDF citation URLs catalogued without download | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| `run-20261024T081350Z-9fb2e560` | **UPDATED** | `SRC-HINTON` | `res_e344b54452f01c96` | `ver_20261024_84423c36_v2` (`v2`) | `HI-URL-002` | `https://www.cs.toronto.edu/~hinton/pages/publications.html` | **Publications - Geoffrey Hinton**; author: `Geoffrey E. Hinton` | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| `run-20261024T081350Z-9fb2e560` | **UPDATED** | `SRC-BENGIO` | `res_73e51a454ee1f4eb` | `ver_20261024_2dda1aa9_v2` (`v2`) | `YB-URL-001` | `https://yoshuabengio.org/en` | **Home \| Yoshua Bengio**; `ETag: W/"1792079761"`, `Last-Modified: Thu, 15 Oct 2026 15:56:01 GMT`; meta description & 12 section headings | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| `run-20261024T081350Z-9fb2e560` | **UPDATED** | `SRC-BENGIO` | `res_5fb3d618c7ed17ec` | `ver_20261024_8e10c18d_v2` (`v2`) | `YB-URL-002` | `https://yoshuabengio.org/en/research` | **Research \| Yoshua Bengio**; `ETag: W/"1792080116"`, `Last-Modified: Thu, 15 Oct 2026 16:01:56 GMT`; outbound reference `https://arxiv.org/abs/2606.29657` recorded | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| `run-20261024T081350Z-9fb2e560` | **INSERTED** | `SRC-3B1B` | `res_b6377e0dd4279dae` | `ver_20261024_f4d4c999_v1` (`v1`) | `3B-URL-001` | `https://www.3blue1brown.com/` | **3Blue1Brown**; author: `Grant Sanderson (3Blue1Brown)`; official links: `@3blue1brown`, `@grantsanderson`, `github.com/3b1b` | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| `run-20261024T081350Z-9fb2e560` | **INSERTED** | `SRC-3B1B` | `res_63c0a0d121a09f38` | `ver_20261024_9386e06b_v1` (`v1`) | `3B-URL-002` | `https://www.3blue1brown.com/topics/neural-networks` | **3Blue1Brown** (Neural Networks topic index); 28 in-scope lesson/topic links catalogued without recursive crawl | `ALL_RIGHTS_RESERVED_METADATA_ONLY` (`METADATA_ONLY`) |
| `run-20261024T081350Z-9fb2e560` | **INSERTED** | `SRC-WIKIPEDIA` | `res_24d64fe06f34c722` | `ver_20261024_842a8401_v1` (`v1`) | `None` (API) | `https://en.wikipedia.org/wiki/Backpropagation` | **Backpropagation**; `page_id=1360091`, `revision_id=1381710221` (`2026-10-24T08:06:45Z`), `wikidata_qid=Q798503` | `CC_BY_SA_4_0` (`CC-BY-SA-4.0`, stored as `METADATA_ONLY` in pilot) |
| `run-20261024T081350Z-9fb2e560` | **INSERTED** | `SRC-WIKIPEDIA` | `res_dd13dd0076cd4a6e` | `ver_20261024_d1f8525a_v1` (`v1`) | `None` (API) | `https://en.wikipedia.org/wiki/Boltzmann_machine` | **Boltzmann machine**; `page_id=1166059`, `revision_id=1380453709` (`2026-10-15T20:10:07Z`), `wikidata_qid=Q194706` | `CC_BY_SA_4_0` (`CC-BY-SA-4.0`, stored as `METADATA_ONLY` in pilot) |
| `run-20261024T081350Z-9fb2e560` | **BLOCKED (Historical)** | `SRC-ARXIV` | *(none)* | *(none)* | *(none)* | `https://export.arxiv.org/robots.txt` | Historical fail-closed event `ADAPTER_SOURCE_BLOCKED_BY_POLICY` preserved in `provenance_events`; resolved via ADR-0005 in Task 013 | Fail-closed (`POLICY_BLOCKED`) |
| `run-20261024T091658Z-a5f7d42f` | **INSERTED** | `SRC-ARXIV` | `res_ab5dba0c12050d69` | `ver_20261024_0843175d_v1` (`v1`) | `arXiv:1207.0580` (`1207.0580v1`) | `https://arxiv.org/abs/1207.0580` | **Improving neural networks by preventing co-adaptation of feature detectors**; authors: `Geoffrey E. Hinton, Nitish Srivastava, Alex Krizhevsky, Ilya Sutskever, Ruslan R. Salakhutdinov`; published `2012-07-03T06:35:15Z`; categories `cs.NE, cs.CV, cs.LG`; PDF URL recorded as reference only (`pdf_fetched=False`) | `ARXIV_AUTHOR_LICENSE` (`METADATA_AND_ABSTRACT`, `permits_full_text_storage=0`) |
| `run-20261024T091658Z-a5f7d42f` | **INSERTED** | `SRC-ARXIV` | `res_0b4b872c018ccdcc` | `ver_20261024_17563f17_v1` (`v1`) | `arXiv:1409.0473` (`1409.0473v7`) | `https://arxiv.org/abs/1409.0473` | **Neural Machine Translation by Jointly Learning to Align and Translate**; authors: `Dzmitry Bahdanau, Kyunghyun Cho, Yoshua Bengio`; published `2014-09-01T16:33:02Z` (updated `2016-05-19T17:20:09Z`); categories `cs.CL, cs.LG, cs.NE, stat.ML`; PDF URL recorded as reference only (`pdf_fetched=False`) | `ARXIV_AUTHOR_LICENSE` (`METADATA_AND_ABSTRACT`, `permits_full_text_storage=0`) |

---

## 5. Persisted Discovered Link Candidates (`discovered_link_candidates`, Schema Version 4)

In Task 013, forward-only schema migration `4` (`0004_discovered_link_candidates`) created the dedicated `discovered_link_candidates` table in [`knowledge_store.py`](../knowledge_store.py) and backfilled all `181` candidate links discovered during the HTML metadata pilot. Future pilot runs upsert directly into `discovered_link_candidates` (updating `last_seen_at` and incrementing `seen_count` idempotently while rejecting non-`http(s)` or credential-bearing URLs and recording `DISCOVERED_LINK_CANDIDATE_REJECTED` in `provenance_events`).

### 5.1 Candidate Review & Fetch-Permission State

- **Total Persisted Candidates (`discovered_link_candidates`):** `181`
- **Review Status (`review_status = 'PENDING_REVIEW'`):** `181` (`100%`)
- **Fetch Permission Granted (`fetch_permission_granted = 1`):** `0` (`181` rows have `fetch_permission_granted = 0`)
- **Promoted to `resources` (`promoted_resource_id IS NOT NULL`):** `0` (`181` rows remain unpromoted candidates)

### 5.2 Breakdown by Domain Classification & Referring Source

| Referring Source ID | `INTERNAL` Candidates | `EXTERNAL` Candidates | Total Candidates |
| :--- | :---: | :---: | :---: |
| `SRC-HINTON` | `32` | `15` | **`47`** |
| `SRC-BENGIO` | `17` | `29` | **`46`** |
| `SRC-3B1B` | `56` | `32` | **`88`** |
| **Total** | **`105`** | **`76`** | **`181`** |

### 5.3 Breakdown by Classification, Recommended Decision, and Policy State

| Classification | Recommended Decision | Policy State (`policy_state`) | Domain Classification | Candidate Count |
| :--- | :--- | :--- | :---: | :---: |
| `in-scope-html` | `candidate-only-no-recursive-crawl` | `IN_SCOPE_HTML_CANDIDATE_NO_AUTO_FETCH` | `INTERNAL` | **`99`** (`26` `SRC-HINTON`, `17` `SRC-BENGIO`, `56` `SRC-3B1B`) |
| `external-not-fetched` | `skip-external-host` | `EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH` | `EXTERNAL` | **`60`** (`15` `SRC-HINTON`, `19` `SRC-BENGIO`, `26` `SRC-3B1B`) |
| `external-official-reference` | `record-official-external-reference-only` | `EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH` | `EXTERNAL` | **`16`** (`10` `SRC-BENGIO`, `6` `SRC-3B1B`) |
| `same-host-binary-not-fetched` | `record-citation-url-only-no-download` | `SAME_HOST_BINARY_CITATION_NO_FETCH` | `INTERNAL` | **`5`** (`5` `SRC-HINTON`: `minefield.pdf`, `shortcv.pdf`, `HintonMumbai.pdf`, `screen.gif`, `sound.gif`) |
| `same-host-outside-prefix` | `skip-outside-prefix` | `SAME_HOST_OUTSIDE_PREFIX_NO_FETCH` | `INTERNAL` | **`1`** (`1` `SRC-HINTON`: `https://www.cs.toronto.edu/~tijmen/csc321/`) |

---

## 6. Site Policy Resolution & Content Reuse Boundaries

### 6.1 Resolved Site Policy & Global Rate-Limit Boundary: `SRC-ARXIV` (`ADR-0005`)

- **Historical Conflict (`run-20261024T081350Z-9fb2e560`):** `https://export.arxiv.org/robots.txt` serves `User-agent: *` / `Disallow: /` to prevent general web crawlers from indexing duplicate HTML/PDF mirror pages on `export.arxiv.org`. In the initial Task 012 run, `ArxivSourceAdapter` failed closed (`ADAPTER_SOURCE_BLOCKED_BY_POLICY`) and made zero requests to `/api/query`.
- **Architectural Resolution (`ADR-0005` / `run-20261024T091658Z-a5f7d42f`):** [`docs/architecture/decisions/0005-arxiv-api-access-policy.md`](../docs/architecture/decisions/0005-arxiv-api-access-policy.md) defines the `ARXIV_OFFICIAL_ATOM_API_V1` policy profile based on RFC 9309, the [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html), and the [arXiv API User Manual](https://info.arxiv.org/help/api/user-manual.html). General HTML/PDF crawling on `export.arxiv.org` remains disallowed (`general_html_crawling_permitted = False`), while bounded, single-connection metadata queries to `https://export.arxiv.org/api/query` with `>= 3.0s` delay are permitted and audited.
- **Supported Single-Worker Runtime Execution Model (`LOCAL_SINGLE_WORKER_SERIALIZED`):** Per the official [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html), the `>= 3.0s` minimum request interval and single-connection (`max_concurrency = 1`) limit apply **across all machines controlled by the project**, not merely within a single thread or object instance. In the current local runtime, `ArxivSourceAdapter` enforces a process-wide mutex gate (`_ArxivProcessRateLimitGate`) and per-origin connection slot (`OriginRateLimiter.connection_slot`) so concurrent calls within a process are strictly serialized (`max_in_flight == 1`) with `>= 3.0s` spacing, and any HTTP `401`/`403`/`451`/`429`/`503` response latches a fail-closed circuit breaker without retry storms.
- **Future Distributed Deployment Requirement:** Deploying multiple independent `ArxivSourceAdapter` instances across separate processes, containers, or cloud machines is prohibited until the project implements either (1) a single dedicated serialized API worker (`max_instances = 1`, `concurrency = 1`) through which all arXiv API calls are routed, or (2) a shared global rate-limit coordinator enforcing a single `>= 3.0s` schedule and single active connection across all project-controlled workers.

### 6.2 Distinction Between Verified Access Permission and Content Reuse Rights

1. **Metadata Retrieval vs. Full-Content Redistribution:**
   - Passing a `robots.txt` check (`SRC-HINTON`, `SRC-BENGIO`, `SRC-3B1B`) verifies **crawl/access permission** for conservative metadata indexing only; it does **not** grant a copyright license to store or redistribute full HTML bodies, blog articles, PDF papers, slide decks, or video transcripts.
   - All `SRC-HINTON`, `SRC-BENGIO`, and `SRC-3B1B` records are persisted with `permitted_storage_scope = "METADATA_ONLY"`, `permits_full_text_storage = 0`, and `permits_verbatim_excerpts = 0`.
2. **arXiv (`SRC-ARXIV`) Metadata & Abstract Scope vs. Full E-Print Rights:**
   - Under the [arXiv API Terms of Use](https://info.arxiv.org/help/api/tou.html), arXiv metadata and abstracts retrieved via `/api/query` are released under CC0, whereas the underlying papers/PDFs remain subject to each submission's individual license (`license_uri` or default arXiv non-exclusive distribution license). Accordingly, `SRC-ARXIV` records are stored with `permitted_storage_scope = "METADATA_AND_ABSTRACT"`, `permits_full_text_storage = 0`, and `pdf_fetched = False`.
3. **3Blue1Brown (`SRC-3B1B`) Lesson & Transcript Boundaries:**
   - Per `https://www.3blue1brown.com/about/`, full lessons and visuals are proprietary (`ALL_RIGHTS_RESERVED_METADATA_ONLY`). Also, `https://www.3blue1brown.com/topics/neural-networks` redirects to `https://www.3blue1brown.com/?topic=neural-networks` and declares `<link rel="canonical" href="https://www.3blue1brown.com/">` in its HTML head. Our adapter preserves the requested topic URL (`https://www.3blue1brown.com/topics/neural-networks`) as the resource's `canonical_url` while recording `html_declared_canonical_url` in `source_specific_metadata` so topic index pages do not collide with the root homepage.
4. **Wikipedia (`SRC-WIKIPEDIA`) ShareAlike Attribution:**
   - Although English Wikipedia articles are licensed under `CC-BY-SA-4.0` (`permits_full_text_storage = 1`, `permits_verbatim_excerpts = 1`), this pilot stored **metadata and permanent revision identifiers (`oldid`) only** (`permitted_storage_scope = "METADATA_ONLY"`, `text_storage_status = "metadata-only"`). Any future milestone that ingests Wikipedia article text or excerpts must preserve the exact `permanent_revision_url` (`oldid`) and `CC BY-SA 4.0` ShareAlike notice on all downstream views and derivative summaries.
