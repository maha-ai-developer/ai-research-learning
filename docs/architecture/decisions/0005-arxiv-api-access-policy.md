# ADR 0005 — arXiv API-Specific Access Policy Profile vs. General HTML Crawling

| Metadata | Value |
| :--- | :--- |
| **ADR ID** | `ADR-0005` |
| **Date** | `2026-10-24` |
| **Status** | `Accepted` |
| **Policy Profile ID** | `ARXIV_OFFICIAL_ATOM_API_V1` |
| **Policy Version** | `ADR-0005/2026-10-24` |
| **Related Docs** | [`source-adapter-contract.md`](../source-adapter-contract.md), [`data-model.md`](../data-model.md), [`task-012-metadata-pilot-report.md`](../../../reports/task-012-metadata-pilot-report.md) |

---

## 1. Context and Problem Statement

During the Task 012 bounded metadata pilot (`2026-10-24`), our generic fail-closed policy auditor fetched `https://export.arxiv.org/robots.txt`, which returned:

```text
# Welcome to export.arxiv.org
# Notice: Automated harvesting of export.arxiv.org is not allowed.

User-agent: *
Disallow: /
```

Because `PolicyAuditResult.can_fetch_url()` applied standard `urllib.robotparser` (`Disallow: /`) uniformly to all URLs on `export.arxiv.org`, the `SRC-ARXIV` adapter (`ArxivSourceAdapter`) failed closed (`BLOCKED` / `DENIED`), issuing zero queries to `https://export.arxiv.org/api/query`.

However, arXiv explicitly provides and documents `http(s)://export.arxiv.org/api/query` as its official programmatic metadata API so that automated tools fetch structured bibliographic metadata via Atom XML rather than scraping HTML pages (`/abs/`, `/list/`, `/search/`) or bulk-harvesting `/pdf/` and `/e-print/` routes on `arxiv.org` or `export.arxiv.org`.

We need to resolve this operational and architectural distinction **without** weakening the repository's generic `robots.txt` fail-closed policy, **without** introducing an origin-wide `robots.txt` bypass on `export.arxiv.org`, and **without** misreporting that `export.arxiv.org/robots.txt` allows general HTML crawling.

---

## 2. Official Normative References

1. **arXiv API Terms of Use**: <https://info.arxiv.org/help/api/tou.html>
   - Governs programmatic use of the arXiv APIs (`export.arxiv.org/api/...`), licensing of bibliographic metadata (CC0 1.0 Universal dedication for arXiv descriptive metadata), and rate/connection limits.
2. **arXiv API User's Manual**: <https://info.arxiv.org/help/api/user-manual.html>
   - Specifies the canonical Atom query endpoint (`http://export.arxiv.org/api/query` / `https://export.arxiv.org/api/query`), query syntax (`search_query`, `id_list`, `start`, `max_results`), and mandatory client behavior: **a single connection at a time** and **at least a 3-second delay between consecutive requests**.
3. **IETF RFC 9309 — Robots Exclusion Protocol**: <https://www.rfc-editor.org/rfc/rfc9309.html>
   - Defines the Robots Exclusion Protocol (`robots.txt`) for general automated web crawlers, while recognizing that service operators may govern specifically documented programmatic API endpoints through explicit API terms of use and rate-limiting controls distinct from general HTML web crawling.

---

## 3. Decision

### 3.1 Strict Separation Between General HTML Crawling and Documented API Access

1. **General HTML Crawling & Unstructured Harvesting (Governed by `robots.txt` + Fail-Closed Policy)**:
   - All HTML web crawling across `arxiv.org` and `export.arxiv.org` (including `/`, `/abs/*`, `/list/*`, `/find/*`, `/search/*`, `/html/*`) and all full-text/binary routes (`/pdf/*`, `/e-print/*`, `/src/*`) remain strictly governed by `robots.txt` (RFC 9309) and our fail-closed scope rules.
   - Because `https://export.arxiv.org/robots.txt` specifies `User-agent: *` / `Disallow: /`, **general HTML crawling on `export.arxiv.org` is and must remain recorded as `DISALLOWED_BY_ROBOTS_TXT` (`general_html_crawling_permitted = False`)**.
   - Audit reports, `site_policies`, `crawl_audit`, and `provenance_events` must **preserve** the factual `robots.txt` result (`Disallow: /`) and must never claim that `export.arxiv.org`'s general `robots.txt` allows HTML crawling.

2. **Narrowly Scoped arXiv Metadata API Profile (`ARXIV_OFFICIAL_ATOM_API_V1`)**:
   - Calls made by `ArxivSourceAdapter` (`SRC-ARXIV`) to the single documented metadata endpoint are governed by the explicit policy profile `ARXIV_OFFICIAL_ATOM_API_V1` under the official arXiv API Terms of Use (<https://info.arxiv.org/help/api/tou.html>) and User's Manual (<https://info.arxiv.org/help/api/user-manual.html>).
   - This profile is **endpoint-specific, not origin-wide**:
     - **Approved Endpoint**: Only `https://export.arxiv.org/api/query` (HTTPS `GET` only, with a non-empty `id_list` or `search_query` parameter and bounded `max_results <= 50`) is permitted by this profile.
     - **Blocked Paths on Same Origin**: Any other path on `export.arxiv.org` (e.g., `https://export.arxiv.org/`, `https://export.arxiv.org/abs/1207.0580`, `https://export.arxiv.org/pdf/1207.0580.pdf`, `https://export.arxiv.org/api/other`, `https://export.arxiv.org/list/cs.AI/recent`), any non-HTTPS scheme (`http://`), any unapproved host (`arxiv.org/abs/...`, `example.org/...`), and any cross-host or path-changing redirect remain strictly **blocked**.
   - **Rate & Concurrency Limits**:
     - **Single Connection**: All requests execute sequentially through a single `requests.Session` / `OriginRateLimiter` lock (`max_concurrent_requests = 1`).
     - **Minimum Inter-Request Delay**: At least **`3.0` seconds** (`effective_delay_seconds >= 3.0`) between consecutive requests to `export.arxiv.org` across all project traffic.
   - **Required Audit & Provenance Fields**:
     - Every policy audit and metadata fetch under this profile must record in `PolicyAuditResult` and `provenance_events`:
       - `policy_profile_id`: `"ARXIV_OFFICIAL_ATOM_API_V1"`
       - `policy_version`: `"ADR-0005/2026-10-24"`
       - `policy_basis`: `"arXiv API Terms of Use (https://info.arxiv.org/help/api/tou.html) and User Manual (https://info.arxiv.org/help/api/user-manual.html); RFC 9309 (https://www.rfc-editor.org/rfc/rfc9309.html)"`
       - `approved_api_endpoint`: `"https://export.arxiv.org/api/query"`
       - `verified_at_utc`: ISO-8601 UTC timestamp of the policy verification
       - `robots_crawl_status`: `"DISALLOWED_BY_ROBOTS_TXT"` (preserving the exact `export.arxiv.org/robots.txt` finding)
       - `general_html_crawling_permitted`: `False`
   - **Fail-Closed Conditions**:
     - The `ARXIV_OFFICIAL_ATOM_API_V1` profile immediately **fails closed** (`PolicyBlockedError`, `RateLimitError`, or `ScopeViolationError`) if:
       1. The User-Agent is missing or lacks project contact identification (`is_identified_user_agent(ua) == False`).
       2. The audit check (`robots.txt` or endpoint probe) returns an explicit denial (`HTTP 401`, `403`, `451`), rate-limit/overload signal (`HTTP 429`, `503`), server error (`5xx`), or network failure.
       3. `robots.txt` adds an explicit `Disallow: /api` or `Disallow: /api/query` rule specifically targeting the API route, or API terms are revoked.
       4. A request targets any URL other than `https://export.arxiv.org/api/query` with valid query parameters, or encounters a redirect (`301`/`302`/`303`/`307`/`308`).
       5. An API call receives `HTTP 429`, `503`, `401`, `403`, or `451` (no automatic retry loops on denial or rate limiting).

---

## 4. Alternatives Considered

| Alternative | Pros | Cons / Reason for Rejection |
| :--- | :--- | :--- |
| **A. Origin-Wide `ignore_robots=True` Flag on `export.arxiv.org`** | Trivial one-line change. | **Rejected**: Violates fail-closed safety principles by permitting HTML paths (`/abs/`, `/list/`) or `/pdf/` routes on `export.arxiv.org` if a candidate URL is misconfigured, and obscures the `robots.txt` audit trail. |
| **B. Keep `SRC-ARXIV` Permanently Blocked by `export.arxiv.org/robots.txt`** | Zero changes to policy evaluation. | **Rejected**: Conflates general HTML scraping (`Disallow: /`) with arXiv's officially documented metadata API (`https://export.arxiv.org/api/query`), preventing compliant bibliographic metadata verification. |
| **C. Endpoint-Specific Policy Profile (`ARXIV_OFFICIAL_ATOM_API_V1`) with Preserved `robots.txt` Audit (Chosen)** | Allows only `https://export.arxiv.org/api/query` under official API terms (`>= 3.0s` delay, single connection) while keeping `robots.txt` (`Disallow: /`) active and recorded for all HTML/PDF/other paths. | Requires explicit URL/query validation and structured audit metadata fields. |

---

## 5. Consequences

### Positive
- **Full Audit Integrity**: `site_policies`, `crawl_audit`, and `provenance_events` truthfully record both that `export.arxiv.org/robots.txt` disallows general HTML crawling (`Disallow: /`) and that `https://export.arxiv.org/api/query` is authorized via `ARXIV_OFFICIAL_ATOM_API_V1` (`ADR-0005/2026-10-24`).
- **Zero Scope Creep**: HTML pages, PDF links, unapproved hosts, cross-host redirects, and arbitrary URLs remain deterministically blocked and verified by unit tests.
- **Strict Rate Discipline**: Single-connection sequential execution with `>= 3.0s` spacing and single-request batching (`id_list=...&max_results=2`) minimizes load on arXiv's infrastructure.
