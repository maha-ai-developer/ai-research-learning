"""arXiv official Atom API metadata adapter (SRC-ARXIV).

Follows docs/architecture/source-adapter-contract.md Section 4.4 and ADR-0005:
- Uses https://export.arxiv.org/api/query (never HTML scraping or PDF downloading)
- Enforces single-connection sequential requests (max_concurrency = 1) with >= 3.0s delay
  across the entire local process (`LOCAL_SINGLE_WORKER_SERIALIZED` execution boundary)
- Fails closed without retry storms on HTTP 401/403/451/429/503
- Parses Atom XML entries into metadata + abstract envelopes
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import threading
import time
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests

from .base import (
    DEFAULT_MAX_BYTES,
    DEFAULT_PROJECT_USER_AGENT,
    DEFAULT_TIMEOUT_SECONDS,
    REDIRECT_STATUSES,
    NormalizedResourceEnvelope,
    NotModifiedResponse,
    OriginRateLimiter,
    ParseError,
    PolicyAuditResult,
    PolicyBlockedError,
    RateLimitError,
    RawMetadataPayload,
    ScopeViolationError,
    SourceAdapter,
    SourceCandidate,
    TransientFetchError,
    canonicalize_url,
    is_binary_asset_url,
    is_identified_user_agent,
    origin_of,
    parse_retry_after,
    utc_now,
)

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)

ARXIV_POLICY_PROFILE_ID = "ARXIV_OFFICIAL_ATOM_API_V1"
ARXIV_POLICY_VERSION = "ADR-0005/2026-10-24"
ARXIV_EXECUTION_BOUNDARY_MODE = "LOCAL_SINGLE_WORKER_SERIALIZED"
ARXIV_MIN_INTERVAL_SECONDS = 3.0
ARXIV_MAX_CONCURRENCY = 1
ARXIV_POLICY_BASIS = (
    "Official arXiv API Terms of Use (https://info.arxiv.org/help/api/tou.html) and "
    "arXiv API User's Manual (https://info.arxiv.org/help/api/user-manual.html) for "
    "https://export.arxiv.org/api/query (>= 3.0s between consecutive requests, single "
    "active connection across all project-controlled machines); RFC 9309 robots.txt "
    "Disallow: / preserved as blocking general HTML and PDF crawling on export.arxiv.org."
)
ARXIV_MAX_RESULTS_LIMIT = 50
ALLOWED_ARXIV_QUERY_KEYS = {
    "id_list",
    "search_query",
    "start",
    "max_results",
    "sortBy",
    "sortOrder",
}


class _ArxivProcessRateLimitGate:
    """Process-wide single-connection and >= 3.0s interval gate for export.arxiv.org.

    Per https://info.arxiv.org/help/api/tou.html:
    - Make no more than one request every three seconds (>= 3.0s).
    - Limit requests to a single connection at a time (max_concurrency = 1).

    In the current `LOCAL_SINGLE_WORKER_SERIALIZED` execution boundary, this gate ensures
    that even if multiple `ArxivSourceAdapter` or `OriginRateLimiter` instances are created
    or invoked concurrently across threads in the same Python process, all requests to
    `https://export.arxiv.org` are strictly serialized through a single lock with >= 3.0s
    spacing between consecutive calls.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request_at_by_clock: dict[Callable[[], float], float] = {}
        self._in_flight: int = 0
        self._max_in_flight: int = 0

    @contextmanager
    def acquire_slot(
        self,
        *,
        rate_limiter: OriginRateLimiter,
        origin_key: str,
        min_delay_seconds: float,
    ) -> Iterator[float]:
        effective_delay = max(ARXIV_MIN_INTERVAL_SECONDS, float(min_delay_seconds))
        normalized_origin = rate_limiter._normalize_origin_key(origin_key)
        clock_fn = rate_limiter._clock_fn
        with self._lock:
            now = clock_fn()
            gate_prev = self._last_request_at_by_clock.get(clock_fn)
            limiter_prev = rate_limiter.last_request_at(normalized_origin)
            candidates = [t for t in (gate_prev, limiter_prev) if t is not None]
            last_at = max(candidates) if candidates else None

            slept = 0.0
            if last_at is not None:
                elapsed = now - last_at
                if elapsed < effective_delay:
                    slept = effective_delay - elapsed
                    rate_limiter._sleep_fn(slept)
                    now = clock_fn()

            self._last_request_at_by_clock[clock_fn] = now
            with rate_limiter._lock:
                rate_limiter._last_request_at[normalized_origin] = now
                active = rate_limiter._in_flight_by_origin.get(normalized_origin, 0) + 1
                rate_limiter._in_flight_by_origin[normalized_origin] = active
                if active > rate_limiter._max_in_flight_by_origin.get(normalized_origin, 0):
                    rate_limiter._max_in_flight_by_origin[normalized_origin] = active

            self._in_flight += 1
            if self._in_flight > self._max_in_flight:
                self._max_in_flight = self._in_flight

            try:
                yield slept
            finally:
                completed_at = clock_fn()
                self._in_flight = max(0, self._in_flight - 1)
                self._last_request_at_by_clock[clock_fn] = max(
                    self._last_request_at_by_clock.get(clock_fn, completed_at),
                    completed_at,
                )
                with rate_limiter._lock:
                    rate_limiter._in_flight_by_origin[normalized_origin] = max(
                        0, rate_limiter._in_flight_by_origin.get(normalized_origin, 1) - 1
                    )
                    rate_limiter._last_request_at[normalized_origin] = max(
                        rate_limiter._last_request_at.get(normalized_origin, completed_at),
                        completed_at,
                    )

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    def reset_for_tests(self) -> None:
        with self._lock:
            self._last_request_at_by_clock.clear()
            self._in_flight = 0
            self._max_in_flight = 0


_ARXIV_PROCESS_GATE = _ArxivProcessRateLimitGate()


def _collapse_ws(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def extract_arxiv_id(raw_id_or_url: str) -> str:
    """Extract normalized arXiv identifier (without version suffix if desired, or preserving base ID)."""
    cleaned = raw_id_or_url.strip()
    if cleaned.startswith(("http://", "https://")):
        parsed = urlsplit(cleaned)
        if "id_list=" in parsed.query:
            for part in parsed.query.split("&"):
                if part.startswith("id_list="):
                    cleaned = part.split("=", 1)[1].split(",")[0].strip()
                    break
        elif "/abs/" in parsed.path:
            cleaned = parsed.path.split("/abs/", 1)[1].strip("/")
    if not ARXIV_ID_RE.match(cleaned):
        raise ValueError(f"Invalid arXiv identifier: {raw_id_or_url!r}")
    return cleaned


class ArxivSourceAdapter(SourceAdapter):
    """Official arXiv Atom API metadata adapter."""

    source_id = "SRC-ARXIV"
    adapter_id = "arxiv-atom-api-adapter/0.1"
    display_name = "arXiv — Open-Access Preprint Archive (Atom API)"
    origin = "https://export.arxiv.org"
    api_endpoint = "https://export.arxiv.org/api/query"
    robots_url = "https://export.arxiv.org/robots.txt"
    terms_of_use_url = "https://info.arxiv.org/help/api/tou.html"
    min_delay_seconds = ARXIV_MIN_INTERVAL_SECONDS
    max_concurrency = ARXIV_MAX_CONCURRENCY
    execution_boundary_mode = ARXIV_EXECUTION_BOUNDARY_MODE

    default_arxiv_candidates: tuple[dict[str, str], ...] = (
        {
            "external_id": "1207.0580",
            "title_hint": "Improving neural networks by preventing co-adaptation of feature detectors",
            "priority": "P0",
            "observed_from": "Curated foundational Hinton/Srivastava paper",
        },
        {
            "external_id": "1409.0473",
            "title_hint": "Neural Machine Translation by Jointly Learning to Align and Translate",
            "priority": "P0",
            "observed_from": "Curated foundational Bahdanau/Cho/Bengio paper",
        },
    )

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_PROJECT_USER_AGENT,
        session: requests.Session | None = None,
        rate_limiter: OriginRateLimiter | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        sleep_fn: Callable[[float], None] = time.sleep,
        api_terms_verified: bool = True,
        batch_queries: bool = True,
    ) -> None:
        super().__init__(
            user_agent=user_agent,
            session=session,
            rate_limiter=rate_limiter,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            sleep_fn=sleep_fn,
        )
        self.api_terms_verified = api_terms_verified
        self.batch_queries = batch_queries
        self._halted_error: PolicyBlockedError | RateLimitError | None = None

    @contextmanager
    def _acquire_origin_slot(self, url_or_origin: str, min_delay_seconds: float) -> Iterator[float]:
        """Serialize all export.arxiv.org calls through the process-wide single-connection gate."""
        origin_key = (
            origin_of(url_or_origin)
            if "://" in url_or_origin
            else url_or_origin.strip().lower().rstrip("/")
        )
        with _ARXIV_PROCESS_GATE.acquire_slot(
            rate_limiter=self.rate_limiter,
            origin_key=origin_key,
            min_delay_seconds=max(ARXIV_MIN_INTERVAL_SECONDS, self.min_delay_seconds, min_delay_seconds),
        ) as slept:
            yield slept

    def build_query_url(self, arxiv_id: str) -> str:
        clean_id = extract_arxiv_id(arxiv_id)
        query = urlencode({"id_list": clean_id, "max_results": 1})
        return f"{self.api_endpoint}?{query}"

    def build_batch_query_url(
        self,
        arxiv_ids: Sequence[str],
        *,
        max_results: int | None = None,
    ) -> str:
        """Build a single bounded arXiv Atom API query URL for one or more arXiv IDs."""
        clean_ids = [extract_arxiv_id(item) for item in arxiv_ids if str(item).strip()]
        if not clean_ids:
            raise ValueError("arxiv_ids must contain at least one valid arXiv identifier")
        bounded_max = max_results if max_results is not None else len(clean_ids)
        if bounded_max < 1 or bounded_max > ARXIV_MAX_RESULTS_LIMIT:
            raise ValueError(
                f"max_results must be between 1 and {ARXIV_MAX_RESULTS_LIMIT}, got {bounded_max}"
            )
        query = urlencode({"id_list": ",".join(clean_ids), "max_results": bounded_max})
        return f"{self.api_endpoint}?{query}"

    def is_url_in_scope(self, url: str) -> bool:
        """Return True ONLY for valid bounded GET queries to https://export.arxiv.org/api/query."""
        try:
            normalized = canonicalize_url(url)
        except ValueError:
            return False
        if is_binary_asset_url(normalized) or "/pdf/" in normalized.lower():
            return False
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "export.arxiv.org"
            or parsed.port not in (None, 443)
            or parsed.path != "/api/query"
        ):
            return False
        if not parsed.query:
            return False
        params = parse_qs(parsed.query, keep_blank_values=True)
        if not params or any(key not in ALLOWED_ARXIV_QUERY_KEYS for key in params):
            return False

        id_list_vals = [v.strip() for v in params.get("id_list", []) if v.strip()]
        search_query_vals = [v.strip() for v in params.get("search_query", []) if v.strip()]
        if not id_list_vals and not search_query_vals:
            return False

        for raw_list in id_list_vals:
            parts = [p.strip() for p in raw_list.split(",") if p.strip()]
            if not parts or any(not ARXIV_ID_RE.match(p) for p in parts):
                return False

        if "max_results" in params:
            try:
                max_res = int(params["max_results"][0])
            except (ValueError, IndexError):
                return False
            if max_res < 1 or max_res > ARXIV_MAX_RESULTS_LIMIT:
                return False

        return True

    def audit_policy(self, *, force_refresh: bool = False) -> PolicyAuditResult:
        """Audit export.arxiv.org robots.txt for general HTML crawling and apply ADR-0005 API profile."""
        if self._cached_policy is not None and not force_refresh:
            return self._cached_policy

        html_probe_url = "https://export.arxiv.org/abs/1207.0580"
        api_probe_url = self.build_query_url("1207.0580")

        robots_policy = self._fetch_robots_policy(
            robots_url=self.robots_url,
            terms_of_use_url=self.terms_of_use_url,
            probe_urls=[html_probe_url],
            allowed_scope=[f"{self.api_endpoint}?*"],
            required_identification=(
                "Descriptive User-Agent header with project URL; single-connection sequential requests"
            ),
            rate_limit_behavior=(
                "Mandatory >= 3.0s delay between consecutive requests on a single connection "
                "(max_concurrency = 1) across all project-controlled machines per arXiv API Terms of Use "
                f"(runtime mode: {ARXIV_EXECUTION_BOUNDARY_MODE}); abort immediately without retry storm on HTTP 401/403/451/429/503"
            ),
            unresolved_questions=[
                "Full PDF and TeX source bundles have per-submission licenses (CC BY, CC BY-SA, or arXiv non-exclusive license); pilot stores metadata and abstract only, never PDFs.",
                "Multi-process or distributed cloud deployment of SRC-ARXIV is prohibited until either a single dedicated serialized API worker (max_instances=1, concurrency=1) or a shared global rate-limit coordinator is implemented.",
            ],
            base_restrictions=[
                "Use official Atom API (https://export.arxiv.org/api/query) only; do not scrape HTML or download PDFs",
                "Enforce >= 3.0s minimum delay between consecutive requests on a single active connection (max_concurrency = 1) across all project-controlled machines",
                f"Enforce runtime execution boundary {ARXIV_EXECUTION_BOUNDARY_MODE}; do not deploy multiple uncoordinated processes/machines against arXiv API",
            ],
        )

        # Fail closed if API terms verification flag is revoked/changed
        if not self.api_terms_verified:
            blocked_policy = PolicyAuditResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                display_name=self.display_name,
                origin=self.origin,
                policy_url=self.robots_url,
                terms_of_use_url=self.terms_of_use_url,
                status="BLOCKED",
                metadata_collection_decision="DENIED",
                http_status=robots_policy.http_status,
                observed_crawl_delay_seconds=robots_policy.observed_crawl_delay_seconds,
                effective_delay_seconds=max(3.0, self.min_delay_seconds, robots_policy.effective_delay_seconds),
                required_identification=robots_policy.required_identification,
                rate_limit_behavior=robots_policy.rate_limit_behavior,
                allowed_scope=robots_policy.allowed_scope,
                observed_restrictions=robots_policy.observed_restrictions
                + ["arXiv API Terms of Use verification flag is false or terms changed; failing closed"],
                unresolved_questions=robots_policy.unresolved_questions,
                sitemap_urls=robots_policy.sitemap_urls,
                evidence_summary=(
                    "Fail-closed: arXiv API Terms of Use verification is false or terms changed; "
                    "refusing to apply API policy profile."
                ),
                checked_at_utc=robots_policy.checked_at_utc,
                policy_profile_id=ARXIV_POLICY_PROFILE_ID,
                policy_version=ARXIV_POLICY_VERSION,
                policy_basis=ARXIV_POLICY_BASIS,
                approved_api_endpoint=self.api_endpoint,
                robots_crawl_status="DISALLOWED_BY_ROBOTS_TXT",
                general_html_crawling_permitted=False,
                robot_parser=robots_policy.robot_parser,
                allowed_api_url_validator=None,
            )
            self._cached_policy = blocked_policy
            return blocked_policy

        # If robots.txt fetch itself failed (e.g., network error, 403/429/500, corrupt bytes, or bad User-Agent), fail closed
        if (
            robots_policy.http_status is None
            or not (200 <= robots_policy.http_status < 300)
            or robots_policy.robot_parser is None
        ):
            robots_policy.policy_profile_id = ARXIV_POLICY_PROFILE_ID
            robots_policy.policy_version = ARXIV_POLICY_VERSION
            robots_policy.policy_basis = ARXIV_POLICY_BASIS
            robots_policy.approved_api_endpoint = self.api_endpoint
            robots_policy.robots_crawl_status = "UNVERIFIED_OR_HTTP_ERROR"
            robots_policy.general_html_crawling_permitted = False
            self._cached_policy = robots_policy
            return robots_policy

        parser = robots_policy.robot_parser
        general_html_allowed = bool(
            parser.can_fetch(self.user_agent, html_probe_url)
            and parser.can_fetch("*", html_probe_url)
        )
        robots_crawl_status = (
            "ALLOWED_BY_ROBOTS_TXT"
            if general_html_allowed
            else "DISALLOWED_BY_ROBOTS_TXT (Disallow: / blocks general HTML/PDF crawling on export.arxiv.org)"
        )

        # If robots.txt contains an explicit /api disallow rule, fail closed even for the API profile
        robot_entries = list(getattr(parser, "entries", []))
        default_entry = getattr(parser, "default_entry", None)
        if default_entry is not None:
            robot_entries.append(default_entry)
        explicit_api_disallow = any(
            not getattr(rule, "allowance", True)
            and str(getattr(rule, "path", "")).lower().startswith("/api")
            for entry in robot_entries
            for rule in getattr(entry, "rulelines", [])
        ) or any(
            "/api" in r.lower() and "disallow" in r.lower()
            for r in robots_policy.observed_restrictions
        )
        if explicit_api_disallow and not (
            parser.can_fetch(self.user_agent, api_probe_url)
            and parser.can_fetch("*", api_probe_url)
        ):
            robots_policy.status = "BLOCKED"
            robots_policy.metadata_collection_decision = "DENIED"
            robots_policy.policy_profile_id = ARXIV_POLICY_PROFILE_ID
            robots_policy.policy_version = ARXIV_POLICY_VERSION
            robots_policy.policy_basis = ARXIV_POLICY_BASIS
            robots_policy.approved_api_endpoint = self.api_endpoint
            robots_policy.robots_crawl_status = "EXPLICIT_API_DISALLOW_IN_ROBOTS_TXT"
            robots_policy.general_html_crawling_permitted = False
            robots_policy.allowed_api_url_validator = None
            robots_policy.observed_restrictions.append(
                "Explicit Disallow rule targeting /api in export.arxiv.org/robots.txt; failing closed"
            )
            robots_policy.evidence_summary = (
                "Fail-closed: export.arxiv.org/robots.txt contains an explicit Disallow rule targeting /api."
            )
            self._cached_policy = robots_policy
            return robots_policy

        effective_delay = max(3.0, self.min_delay_seconds)
        restrictions = [
            r
            for r in robots_policy.observed_restrictions
            if r != "Target scope disallowed by robots.txt rules"
        ]
        if not general_html_allowed:
            restrictions.append(
                "export.arxiv.org/robots.txt serves Disallow: / for general HTML/PDF crawlers; general HTML crawling is NOT permitted"
            )
        restrictions.append(
            f"Explicit API policy profile {ARXIV_POLICY_PROFILE_ID} ({ARXIV_POLICY_VERSION}) permits ONLY "
            f"GET {self.api_endpoint} with >= {effective_delay:.1f}s delay and single connection"
        )

        if self.request_log and self.request_log[-1].purpose == "POLICY_AUDIT":
            self.request_log[-1].outcome = "policy-verified-arxiv-api-profile-ok"

        evidence_summary = (
            f"export.arxiv.org/robots.txt (HTTP {robots_policy.http_status}) "
            f"{'disallows general HTML crawling (Disallow: /)' if not general_html_allowed else 'verified'}; "
            f"metadata collection is permitted ONLY for {self.api_endpoint} under explicit policy profile "
            f"{ARXIV_POLICY_PROFILE_ID} ({ARXIV_POLICY_VERSION}) per official arXiv API Terms of Use "
            f"(>= {effective_delay:.1f}s delay, single connection)."
        )

        api_policy = PolicyAuditResult(
            source_id=self.source_id,
            adapter_id=self.adapter_id,
            display_name=self.display_name,
            origin=self.origin,
            policy_url=self.robots_url,
            terms_of_use_url=self.terms_of_use_url,
            status="OK",
            metadata_collection_decision="PERMITTED",
            http_status=robots_policy.http_status,
            observed_crawl_delay_seconds=robots_policy.observed_crawl_delay_seconds,
            effective_delay_seconds=effective_delay,
            required_identification=robots_policy.required_identification,
            rate_limit_behavior=robots_policy.rate_limit_behavior,
            allowed_scope=[self.api_endpoint],
            observed_restrictions=restrictions,
            unresolved_questions=robots_policy.unresolved_questions,
            sitemap_urls=robots_policy.sitemap_urls,
            evidence_summary=evidence_summary,
            checked_at_utc=robots_policy.checked_at_utc,
            policy_profile_id=ARXIV_POLICY_PROFILE_ID,
            policy_version=ARXIV_POLICY_VERSION,
            policy_basis=ARXIV_POLICY_BASIS,
            approved_api_endpoint=self.api_endpoint,
            robots_crawl_status=robots_crawl_status,
            general_html_crawling_permitted=general_html_allowed,
            robot_parser=parser,
            allowed_api_url_validator=self.is_url_in_scope,
        )
        self._cached_policy = api_policy
        return api_policy

    def discover_candidates(
        self,
        *,
        seed_manifest: list[dict[str, Any]] | None = None,
        limit: int = 2,
        batch_query: bool | None = None,
    ) -> list[SourceCandidate]:
        """Return up to `limit` curated arXiv API query candidates.

        When `limit > 1` and `batch_query` is True (default), combines the curated IDs
        into a single bounded API query (`id_list=1207.0580,1409.0473&max_results=2`)
        so a single HTTP request retrieves up to `limit` metadata records.
        """
        max_items = max(0, int(limit))
        if max_items == 0:
            return []

        selected_items = list(self.default_arxiv_candidates[:max_items])
        use_batch = self.batch_queries if batch_query is None else batch_query
        if use_batch and len(selected_items) > 1:
            batch_ids = [item["external_id"] for item in selected_items]
            return [
                SourceCandidate(
                    source_id=self.source_id,
                    url=self.build_batch_query_url(batch_ids, max_results=len(batch_ids)),
                    title_hint="; ".join(
                        item.get("title_hint") or item["external_id"] for item in selected_items
                    ),
                    resource_type_hint="preprint_paper_metadata",
                    manifest_id=None,
                    external_id=",".join(batch_ids),
                    priority=selected_items[0].get("priority", "P0"),
                    observed_from="Curated foundational arXiv seed batch",
                    extra={
                        "domain_scope": "First-party API",
                        "information_to_inventory": "arXiv ID, Title, Authors, Abstract, Categories, Published/Updated Dates, DOI",
                        "collection_decision": "Fetch Atom API Metadata & Abstract Only (No PDF Download)",
                        "batch_items": {item["external_id"]: dict(item) for item in selected_items},
                    },
                )
            ]

        candidates: list[SourceCandidate] = []
        for item in selected_items:
            arxiv_id = item["external_id"]
            candidates.append(
                SourceCandidate(
                    source_id=self.source_id,
                    url=self.build_query_url(arxiv_id),
                    title_hint=item.get("title_hint"),
                    resource_type_hint="preprint_paper_metadata",
                    manifest_id=None,
                    external_id=arxiv_id,
                    priority=item.get("priority", "P0"),
                    observed_from=item.get("observed_from", "Curated arXiv seed"),
                    extra={
                        "domain_scope": "First-party API",
                        "information_to_inventory": "arXiv ID, Title, Authors, Abstract, Categories, Published/Updated Dates, DOI",
                        "collection_decision": "Fetch Atom API Metadata & Abstract Only (No PDF Download)",
                    },
                )
            )
        return candidates

    def fetch_metadata(
        self,
        candidate: SourceCandidate,
        *,
        policy: PolicyAuditResult | None = None,
        conditional_headers: dict[str, str] | None = None,
    ) -> RawMetadataPayload:
        """Query the arXiv Atom API for a single candidate record."""
        if not is_identified_user_agent(self.user_agent):
            raise PolicyBlockedError(
                "User-Agent must identify the project before calling the arXiv API",
                source_id=self.source_id,
                url=candidate.url,
            )

        query_url = candidate.url
        if not self.is_url_in_scope(query_url):
            raise ScopeViolationError(
                f"arXiv adapter only permits https://export.arxiv.org/api/query URLs (got {query_url})",
                source_id=self.source_id,
                url=query_url,
            )

        active_policy = policy or self._cached_policy or self.audit_policy()
        if not active_policy.is_metadata_collection_permitted:
            raise PolicyBlockedError(
                f"arXiv policy audit did not permit access (status={active_policy.status})",
                source_id=self.source_id,
                url=query_url,
                http_status=active_policy.http_status,
            )
        if not active_policy.can_fetch_url(self.user_agent, query_url):
            raise PolicyBlockedError(
                f"export.arxiv.org robots.txt disallows {query_url}",
                source_id=self.source_id,
                url=query_url,
            )

        if not conditional_headers and query_url in self._response_cache:
            cached = self._response_cache[query_url]
            return RawMetadataPayload(
                source_id=cached.source_id,
                adapter_id=cached.adapter_id,
                requested_url=query_url,
                final_url=cached.final_url,
                http_status=cached.http_status,
                content_type=cached.content_type,
                headers=dict(cached.headers),
                body_text=cached.body_text,
                fetched_at_utc=cached.fetched_at_utc,
                from_cache_or_not_modified=True,
                candidate=candidate,
            )

        # Fail closed without retry storms if a previous call on this adapter was blocked or rate-limited
        if self._halted_error is not None:
            if isinstance(self._halted_error, RateLimitError):
                raise RateLimitError(
                    f"arXiv adapter circuit breaker active after prior HTTP {self._halted_error.http_status} "
                    f"rate limit; refusing further API requests to prevent retry storm",
                    source_id=self.source_id,
                    url=query_url,
                    http_status=self._halted_error.http_status,
                    retry_after_seconds=self._halted_error.retry_after_seconds,
                )
            raise PolicyBlockedError(
                f"arXiv adapter circuit breaker active after prior HTTP {self._halted_error.http_status} "
                f"block; refusing further API requests to prevent retry storm",
                source_id=self.source_id,
                url=query_url,
                http_status=self._halted_error.http_status,
            )

        effective_delay = max(
            ARXIV_MIN_INTERVAL_SECONDS,
            self.min_delay_seconds,
            active_policy.effective_delay_seconds,
        )

        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.1",
        }
        if conditional_headers:
            for key in ("If-None-Match", "If-Modified-Since"):
                if conditional_headers.get(key):
                    req_headers[key] = conditional_headers[key]

        response: requests.Response | None = None
        try:
            with self._acquire_origin_slot(origin_of(query_url), effective_delay):
                response = self.session.get(
                    query_url,
                    headers=req_headers,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
                status_code = response.status_code
                headers_dict = dict(response.headers or {})

                if status_code == 304:
                    response.close()
                    response = None
                    self._record_http_event(
                        purpose="METADATA_FETCH",
                        url=query_url,
                        http_status=304,
                        outcome="not-modified-304",
                    )
                    raise NotModifiedResponse(query_url, headers_dict)

                if status_code in REDIRECT_STATUSES:
                    location = headers_dict.get("Location") or ""
                    response.close()
                    response = None
                    next_url = urljoin(query_url, location) if location else ""
                    self._record_http_event(
                        purpose="METADATA_FETCH",
                        url=query_url,
                        http_status=status_code,
                        outcome="blocked-redirect-out-of-scope",
                    )
                    raise ScopeViolationError(
                        f"arXiv API endpoint returned redirect (HTTP {status_code} -> {next_url or 'missing Location'}); "
                        f"cross-host and non-API redirects are blocked by {ARXIV_POLICY_PROFILE_ID}",
                        source_id=self.source_id,
                        url=query_url,
                    )

                if status_code in {429, 503}:
                    retry_after = parse_retry_after(headers_dict.get("Retry-After"))
                    response.close()
                    response = None
                    self._record_http_event(
                        purpose="METADATA_FETCH",
                        url=query_url,
                        http_status=status_code,
                        outcome="rate-limited",
                    )
                    rate_err = RateLimitError(
                        f"arXiv API returned HTTP {status_code} rate limit",
                        source_id=self.source_id,
                        url=query_url,
                        http_status=status_code,
                        retry_after_seconds=retry_after,
                    )
                    self._halted_error = rate_err
                    raise rate_err

                if status_code in {401, 403, 451}:
                    response.close()
                    response = None
                    self._record_http_event(
                        purpose="METADATA_FETCH",
                        url=query_url,
                        http_status=status_code,
                        outcome="policy-or-access-blocked",
                    )
                    block_err = PolicyBlockedError(
                        f"arXiv API blocked request with HTTP {status_code}",
                        source_id=self.source_id,
                        url=query_url,
                        http_status=status_code,
                    )
                    self._halted_error = block_err
                    raise block_err

                if status_code < 200 or status_code >= 300:
                    response.close()
                    response = None
                    self._record_http_event(
                        purpose="METADATA_FETCH",
                        url=query_url,
                        http_status=status_code,
                        outcome=f"http-error-{status_code}",
                    )
                    raise TransientFetchError(
                        f"arXiv API returned HTTP {status_code}",
                        source_id=self.source_id,
                        url=query_url,
                        http_status=status_code,
                        is_retryable=status_code >= 500,
                    )

                raw_bytes = self._read_limited(response)
                encoding = getattr(response, "encoding", None) or "utf-8"
                body_text = raw_bytes.decode(encoding, errors="replace")
                response.close()
                response = None

            self._record_http_event(
                purpose="METADATA_FETCH",
                url=query_url,
                http_status=status_code,
                outcome="fetched-ok",
            )
            payload = RawMetadataPayload(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                requested_url=query_url,
                final_url=query_url,
                http_status=status_code,
                content_type=(headers_dict.get("Content-Type") or "application/atom+xml"),
                headers=headers_dict,
                body_text=body_text,
                fetched_at_utc=utc_now(),
                from_cache_or_not_modified=False,
                candidate=candidate,
            )
            self._response_cache[query_url] = payload
            return payload

        except (
            PolicyBlockedError,
            ScopeViolationError,
            RateLimitError,
            TransientFetchError,
            NotModifiedResponse,
        ):
            raise
        except requests.RequestException as exc:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self._record_http_event(
                purpose="METADATA_FETCH",
                url=query_url,
                http_status=None,
                outcome="network-exception",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise TransientFetchError(
                f"Network error querying arXiv API: {exc}",
                source_id=self.source_id,
                url=query_url,
                is_retryable=True,
            ) from exc

    def _normalize_entry(
        self,
        entry: ET.Element,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> NormalizedResourceEnvelope:
        """Normalize a single Atom `<entry>` element into a NormalizedResourceEnvelope."""
        raw_entry_id = _collapse_ws(entry.findtext("atom:id", default="", namespaces=ATOM_NS))
        title = _collapse_ws(entry.findtext("atom:title", default="", namespaces=ATOM_NS))
        summary = _collapse_ws(entry.findtext("atom:summary", default="", namespaces=ATOM_NS))
        published = _collapse_ws(entry.findtext("atom:published", default="", namespaces=ATOM_NS))
        updated = _collapse_ws(entry.findtext("atom:updated", default="", namespaces=ATOM_NS))

        if not raw_entry_id or not title:
            raise ParseError(
                f"arXiv Atom entry missing required <id> or <title> in {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

        authors = [
            _collapse_ws(author.findtext("atom:name", default="", namespaces=ATOM_NS))
            for author in entry.findall("atom:author", ATOM_NS)
        ]
        authors_clean = [a for a in authors if a]
        authors_str = ", ".join(authors_clean) if authors_clean else None

        primary_cat_el = entry.find("arxiv:primary_category", ATOM_NS)
        primary_category = (
            primary_cat_el.attrib.get("term") if primary_cat_el is not None else None
        )
        categories = [
            cat.attrib.get("term")
            for cat in entry.findall("atom:category", ATOM_NS)
            if cat.attrib.get("term")
        ]
        doi = _collapse_ws(entry.findtext("arxiv:doi", default="", namespaces=ATOM_NS))
        journal_ref = _collapse_ws(
            entry.findtext("arxiv:journal_ref", default="", namespaces=ATOM_NS)
        )
        comment = _collapse_ws(entry.findtext("arxiv:comment", default="", namespaces=ATOM_NS))

        abs_url = None
        pdf_reference_url = None
        license_url = _collapse_ws(
            entry.findtext("arxiv:license", default="", namespaces=ATOM_NS)
        )
        for link in entry.findall("atom:link", ATOM_NS):
            href = (link.attrib.get("href") or "").strip()
            rel = (link.attrib.get("rel") or "").strip()
            title_attr = (link.attrib.get("title") or "").strip().lower()
            if rel == "alternate" and href:
                abs_url = href
            elif title_attr == "pdf" and href:
                pdf_reference_url = href
            elif rel == "license" and href and not license_url:
                license_url = href

        # Normalize canonical abstract URL to https://arxiv.org/abs/<base_id>
        versioned_id = raw_entry_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_entry_id else raw_entry_id
        base_arxiv_id = re.sub(r"v\d+$", "", versioned_id)
        canonical_abs_url = canonicalize_url(f"https://arxiv.org/abs/{base_arxiv_id}")

        official_links = [u for u in (abs_url, pdf_reference_url) if u]
        candidate = raw.candidate
        batch_meta = (
            (candidate.extra or {}).get("batch_items", {}).get(base_arxiv_id, {})
            if candidate
            else {}
        )
        observed_from = (
            batch_meta.get("observed_from")
            or (candidate.observed_from if candidate else None)
            or "arXiv Atom API"
        )
        priority = (
            batch_meta.get("priority")
            or (candidate.priority if candidate else None)
            or "P0"
        )
        active_policy = policy or self._cached_policy

        completeness = {
            "has_title": bool(title),
            "has_authors": bool(authors_str),
            "has_published_or_updated_date": bool(published or updated),
            "has_abstract": bool(summary),
            "has_categories": bool(categories),
            "has_external_identifier": bool(base_arxiv_id),
        }

        return NormalizedResourceEnvelope(
            source_id=self.source_id,
            adapter_id=self.adapter_id,
            canonical_url=canonical_abs_url,
            fetched_url=raw.final_url,
            title=title,
            resource_type="preprint_paper_metadata",
            manifest_id=(candidate.manifest_id if candidate else None),
            authors=authors_str,
            published_at=published,
            updated_at_source=updated,
            media_modality="TEXT_METADATA_AND_ABSTRACT",
            domain_scope="First-party API",
            abstract_or_description=summary,
            topic_tags=", ".join(categories) if categories else primary_category,
            external_identifier=f"arXiv:{base_arxiv_id}",
            language="en",
            transcript_reference=None,
            official_links=official_links,
            observed_from=observed_from,
            information_to_inventory="arXiv ID, Title, Authors, Abstract, Categories, Published/Updated Dates",
            priority=priority,
            collection_decision="Fetch Atom API Metadata & Abstract Only (No PDF Download)",
            verification_status=f"Verified Atom API HTTP {raw.http_status} via {self.adapter_id}",
            robots_sitemap_status=(
                f"{active_policy.status}: {active_policy.evidence_summary}"
                if active_policy
                else "Verified arXiv API policy"
            ),
            rights_status=(
                f"arXiv API metadata & abstract reuse permitted; submission license={license_url or 'unspecified-per-submission'}"
            ),
            license_status="ARXIV_METADATA_AND_ABSTRACT_PERMITTED",
            license_spdx=None,
            permitted_storage_scope="METADATA_AND_ABSTRACT",
            permits_full_text_storage=False,
            permits_verbatim_excerpts=True,
            legal_uncertainty_status="CLEARED_FOR_METADATA_AND_ABSTRACT_ONLY",
            text_storage_status="abstract-and-metadata-only",
            collected_at_utc=raw.fetched_at_utc,
            http_status=raw.http_status,
            etag=raw.headers.get("ETag"),
            last_modified_header=raw.headers.get("Last-Modified"),
            metadata_completeness=completeness,
            source_specific_metadata={
                "arxiv_id": base_arxiv_id,
                "arxiv_versioned_id": versioned_id,
                "primary_category": primary_category,
                "categories": categories,
                "doi": doi,
                "journal_ref": journal_ref,
                "comment": comment,
                "submission_license_url": license_url,
                "pdf_reference_url_not_downloaded": pdf_reference_url,
                "policy_profile_id": (active_policy.policy_profile_id if active_policy else ARXIV_POLICY_PROFILE_ID),
                "policy_version": (active_policy.policy_version if active_policy else ARXIV_POLICY_VERSION),
                "approved_api_endpoint": self.api_endpoint,
                "execution_boundary_mode": ARXIV_EXECUTION_BOUNDARY_MODE,
                "min_interval_seconds": ARXIV_MIN_INTERVAL_SECONDS,
                "max_concurrency": ARXIV_MAX_CONCURRENCY,
            },
            discovered_links=[],
        )

    def normalize_all(
        self,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> list[NormalizedResourceEnvelope]:
        """Parse an arXiv Atom XML payload into one or more NormalizedResourceEnvelopes."""
        try:
            root = ET.fromstring(raw.body_text)
        except ET.ParseError as exc:
            raise ParseError(
                f"Invalid Atom XML from arXiv API: {exc}",
                source_id=self.source_id,
                url=raw.final_url,
            ) from exc

        entries = root.findall("atom:entry", ATOM_NS)
        if not entries:
            raise ParseError(
                f"arXiv Atom feed contained no <entry> elements for {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

        return [self._normalize_entry(entry, raw, policy=policy) for entry in entries]

    def normalize(
        self,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> NormalizedResourceEnvelope:
        """Parse an arXiv Atom XML payload into the first NormalizedResourceEnvelope."""
        return self.normalize_all(raw, policy=policy)[0]
