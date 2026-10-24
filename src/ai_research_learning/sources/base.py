"""Common source-adapter contract, data models, rate limiting, and error taxonomy.

Follows:
- docs/architecture/source-adapter-contract.md
- docs/architecture/data-model.md

Adapters are decoupled from SQLite/database persistence. They audit source
policies, discover bounded candidates, retrieve metadata-only payloads, and
produce normalized resource envelopes that a coordinator persists through
`KnowledgeStore`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Callable
import urllib.robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

DEFAULT_PROJECT_USER_AGENT = (
    "AIResearchLearningBot/0.1 "
    "(+https://github.com/maha-ai-developer/ai-research-learning; metadata-only)"
)
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

NON_METADATA_BINARY_EXTENSIONS = {
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".bz2",
    ".xz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mp3",
    ".wav",
    ".ogg",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
}

PLACEHOLDER_UA_MARKERS = (
    "configure a contact url before production",
    "python-requests",
    "python-urllib",
)


def utc_now() -> str:
    """Return current UTC timestamp in ISO-8601 format (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def origin_of(url: str) -> str:
    """Return lowercase scheme://netloc origin of a URL."""
    parsed = urlsplit(url.strip())
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def host_of(url: str) -> str:
    """Return lowercase hostname of a URL."""
    return (urlsplit(url.strip()).hostname or "").lower()


def canonicalize_url(url: str) -> str:
    """Normalize transport-level differences while preserving path and query semantics."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"Invalid empty URL: {url!r}")
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError(f"Not an absolute HTTP(S) URL: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"URL must not contain credentials: {url!r}")
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def is_binary_asset_url(url: str) -> bool:
    """Return True if the URL path has a binary/media/PDF extension."""
    suffix = Path(urlsplit(url).path.lower()).suffix
    return suffix in NON_METADATA_BINARY_EXTENSIONS


def is_identified_user_agent(user_agent: str) -> bool:
    """Return True if `user_agent` identifies the project with a contact URL or email."""
    if not isinstance(user_agent, str):
        return False
    cleaned = user_agent.strip()
    if len(cleaned) < 12:
        return False
    lowered = cleaned.casefold()
    if any(marker in lowered for marker in PLACEHOLDER_UA_MARKERS):
        return False
    has_contact = ("https://" in lowered) or ("http://" in lowered) or ("@" in lowered)
    return has_contact


def parse_retry_after(value: str | None) -> float | None:
    """Parse an HTTP Retry-After header (seconds) if numeric."""
    if not value:
        return None
    cleaned = value.strip()
    try:
        seconds = float(cleaned)
        return max(0.0, seconds)
    except ValueError:
        return None


def extract_fallback_crawl_delay(lines: list[str], user_agent: str) -> float | None:
    """Extract Crawl-delay for `*` or matching user-agent when stdlib RobotFileParser drops a block."""
    ua_token = (user_agent.split("/", 1)[0] or "").strip().lower()
    active_agents: list[str] = []
    seen_directive_in_group = False
    matched_delays: list[float] = []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = [part.strip() for part in line.split(":", 1)]
        key_lower = key.lower()
        if key_lower == "user-agent":
            if seen_directive_in_group:
                active_agents = []
                seen_directive_in_group = False
            active_agents.append(val.lower())
        else:
            seen_directive_in_group = True
            if key_lower == "crawl-delay":
                if "*" in active_agents or (ua_token and any(ua_token in a for a in active_agents)):
                    try:
                        matched_delays.append(max(0.0, float(val)))
                    except ValueError:
                        pass
    return max(matched_delays) if matched_delays else None


class AdapterError(Exception):
    """Base exception for classified source-adapter errors."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str,
        url: str | None = None,
        error_class: str = "ADAPTER_ERROR",
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        is_retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.source_id = source_id
        self.url = url
        self.error_class = error_class
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.is_retryable = is_retryable


class PolicyBlockedError(AdapterError):
    """Raised when robots.txt, API policy, or missing identification blocks access."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str,
        url: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(
            message,
            source_id=source_id,
            url=url,
            error_class="POLICY_BLOCKED",
            http_status=http_status,
            is_retryable=False,
        )


class ScopeViolationError(AdapterError):
    """Raised when a URL or redirect is outside the adapter's approved host/path/media scope."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str,
        url: str | None = None,
    ) -> None:
        super().__init__(
            message,
            source_id=source_id,
            url=url,
            error_class="SCOPE_VIOLATION",
            is_retryable=False,
        )


class RateLimitError(AdapterError):
    """Raised when an origin returns HTTP 429 or 503 rate-limiting signals."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str,
        url: str | None = None,
        http_status: int | None = 429,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            source_id=source_id,
            url=url,
            error_class="RATE_LIMITED",
            http_status=http_status,
            retry_after_seconds=retry_after_seconds,
            is_retryable=False,
        )


class TransientFetchError(AdapterError):
    """Raised on network timeouts, connection errors, or non-rate-limit HTTP errors."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str,
        url: str | None = None,
        http_status: int | None = None,
        is_retryable: bool = True,
    ) -> None:
        super().__init__(
            message,
            source_id=source_id,
            url=url,
            error_class="NETWORK_OR_HTTP_ERROR",
            http_status=http_status,
            is_retryable=is_retryable,
        )


class ParseError(AdapterError):
    """Raised when an HTML, Atom XML, or MediaWiki JSON response cannot be parsed safely."""

    def __init__(
        self,
        message: str,
        *,
        source_id: str,
        url: str | None = None,
    ) -> None:
        super().__init__(
            message,
            source_id=source_id,
            url=url,
            error_class="PARSE_ERROR",
            is_retryable=False,
        )


class NotModifiedResponse(Exception):
    """Signal raised internally when a conditional GET returns HTTP 304 Not Modified."""

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        super().__init__(f"HTTP 304 Not Modified: {url}")
        self.url = url
        self.headers = headers


@dataclass
class PolicyAuditResult:
    """Result of auditing a source's robots.txt and/or official API access policy."""

    source_id: str
    adapter_id: str
    display_name: str
    origin: str
    policy_url: str
    terms_of_use_url: str | None
    status: str  # "OK" | "BLOCKED" | "UNVERIFIED"
    metadata_collection_decision: str  # "PERMITTED" | "DENIED" | "REQUIRES_REVIEW"
    http_status: int | None
    observed_crawl_delay_seconds: float | None
    effective_delay_seconds: float
    required_identification: str
    rate_limit_behavior: str
    allowed_scope: list[str]
    observed_restrictions: list[str]
    unresolved_questions: list[str]
    sitemap_urls: list[str] = field(default_factory=list)
    evidence_summary: str = ""
    checked_at_utc: str = field(default_factory=utc_now)
    robot_parser: urllib.robotparser.RobotFileParser | None = field(default=None, repr=False)

    @property
    def is_metadata_collection_permitted(self) -> bool:
        return self.status == "OK" and self.metadata_collection_decision == "PERMITTED"

    def can_fetch_url(self, user_agent: str, url: str) -> bool:
        """Return True only if policy audit succeeded and robots rules permit `url`."""
        if not self.is_metadata_collection_permitted:
            return False
        if self.robot_parser is not None:
            return bool(
                self.robot_parser.can_fetch(user_agent, url)
                and self.robot_parser.can_fetch("*", url)
            )
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("robot_parser", None)
        return payload


@dataclass
class SourceCandidate:
    """A candidate resource URL or API record discovered or selected for metadata retrieval."""

    source_id: str
    url: str
    title_hint: str | None = None
    resource_type_hint: str = "web_page"
    manifest_id: str | None = None
    external_id: str | None = None
    priority: str = "P1"
    observed_from: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveredLinkCandidate:
    """An anchor link discovered on an HTML resource page (recorded, not recursively crawled)."""

    from_url: str
    from_manifest_id: str | None
    link_url: str
    link_text: str | None
    rel: str | None
    target_scope: str
    decision: str


@dataclass
class RawMetadataPayload:
    """Raw HTTP/API payload returned by an adapter's metadata retrieval step."""

    source_id: str
    adapter_id: str
    requested_url: str
    final_url: str
    http_status: int
    content_type: str
    headers: dict[str, str]
    body_text: str
    fetched_at_utc: str = field(default_factory=utc_now)
    from_cache_or_not_modified: bool = False
    candidate: SourceCandidate | None = None


@dataclass
class NormalizedResourceEnvelope:
    """Normalized metadata record ready for persistence in `KnowledgeStore`."""

    source_id: str
    adapter_id: str
    canonical_url: str
    fetched_url: str
    title: str
    resource_type: str
    manifest_id: str | None = None
    authors: str | None = None
    published_at: str | None = None
    updated_at_source: str | None = None
    media_modality: str = "TEXT_HTML"
    domain_scope: str = "First-party"
    abstract_or_description: str | None = None
    topic_tags: str | None = None
    external_identifier: str | None = None
    language: str | None = None
    transcript_reference: str | None = None
    official_links: list[str] = field(default_factory=list)
    observed_from: str | None = None
    information_to_inventory: str | None = None
    priority: str | None = "P1"
    collection_decision: str | None = "Metadata-only pilot collection"
    verification_status: str | None = "Verified metadata via source adapter"
    robots_sitemap_status: str | None = None
    rights_status: str | None = None
    license_status: str = "ALL_RIGHTS_RESERVED_METADATA_ONLY"
    license_spdx: str | None = None
    permitted_storage_scope: str = "METADATA_ONLY"
    permits_full_text_storage: bool = False
    permits_verbatim_excerpts: bool = False
    legal_uncertainty_status: str = "UNVERIFIED_REQUIRES_HUMAN_REVIEW"
    text_storage_status: str = "metadata-only"
    collected_at_utc: str = field(default_factory=utc_now)
    http_status: int | None = 200
    etag: str | None = None
    last_modified_header: str | None = None
    metadata_completeness: dict[str, Any] = field(default_factory=dict)
    source_specific_metadata: dict[str, Any] = field(default_factory=dict)
    discovered_links: list[DiscoveredLinkCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HttpRequestEvent:
    """Record of a single live HTTP request made by an adapter."""

    source_id: str
    adapter_id: str
    purpose: str  # "POLICY_AUDIT" | "METADATA_FETCH" | "DISCOVERY"
    url: str
    http_status: int | None
    outcome: str
    requested_at_utc: str
    error: str | None = None


class OriginRateLimiter:
    """Per-origin sequential rate limiter enforcing source-specific minimum delays."""

    def __init__(
        self,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._last_request_at: dict[str, float] = {}

    def wait(self, origin: str, min_delay_seconds: float) -> float:
        """Wait if needed before issuing a request to `origin`. Returns seconds slept."""
        key = origin.rstrip("/").lower()
        enforced_delay = max(0.0, float(min_delay_seconds))
        now = self._clock_fn()
        slept = 0.0
        if key in self._last_request_at:
            elapsed = now - self._last_request_at[key]
            remaining = enforced_delay - elapsed
            if remaining > 0:
                self._sleep_fn(remaining)
                slept = remaining
        self._last_request_at[key] = self._clock_fn()
        return slept


class SourceAdapter(ABC):
    """Abstract base class for all knowledge source adapters."""

    source_id: str
    adapter_id: str
    display_name: str
    origin: str
    min_delay_seconds: float = 1.0

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_PROJECT_USER_AGENT,
        session: requests.Session | None = None,
        rate_limiter: OriginRateLimiter | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter or OriginRateLimiter(sleep_fn=sleep_fn)
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.request_log: list[HttpRequestEvent] = []
        self._cached_policy: PolicyAuditResult | None = None
        self._response_cache: dict[str, RawMetadataPayload] = {}

    def _record_http_event(
        self,
        *,
        purpose: str,
        url: str,
        http_status: int | None,
        outcome: str,
        error: str | None = None,
    ) -> None:
        self.request_log.append(
            HttpRequestEvent(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                purpose=purpose,
                url=url,
                http_status=http_status,
                outcome=outcome,
                requested_at_utc=utc_now(),
                error=error,
            )
        )

    def _read_limited(self, response: requests.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        headers = getattr(response, "headers", {}) or {}
        if headers.get("Content-Length"):
            try:
                if int(headers["Content-Length"]) > self.max_bytes:
                    raise TransientFetchError(
                        f"Response exceeds {self.max_bytes} byte limit",
                        source_id=self.source_id,
                        is_retryable=False,
                    )
            except ValueError:
                pass
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > self.max_bytes:
                raise TransientFetchError(
                    f"Response exceeds {self.max_bytes} byte limit",
                    source_id=self.source_id,
                    is_retryable=False,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch_robots_policy(
        self,
        *,
        robots_url: str,
        terms_of_use_url: str | None,
        probe_urls: list[str],
        allowed_scope: list[str],
        required_identification: str,
        rate_limit_behavior: str,
        unresolved_questions: list[str],
        base_restrictions: list[str] | None = None,
    ) -> PolicyAuditResult:
        """Fetch and evaluate a robots.txt endpoint with fail-closed semantics."""
        now = utc_now()
        restrictions = list(base_restrictions or [])
        if not is_identified_user_agent(self.user_agent):
            policy = PolicyAuditResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                display_name=self.display_name,
                origin=self.origin,
                policy_url=robots_url,
                terms_of_use_url=terms_of_use_url,
                status="BLOCKED",
                metadata_collection_decision="DENIED",
                http_status=None,
                observed_crawl_delay_seconds=None,
                effective_delay_seconds=self.min_delay_seconds,
                required_identification=required_identification,
                rate_limit_behavior=rate_limit_behavior,
                allowed_scope=allowed_scope,
                observed_restrictions=restrictions + ["User-Agent lacks project identification / contact URL"],
                unresolved_questions=unresolved_questions,
                sitemap_urls=[],
                evidence_summary="Fail-closed: User-Agent is not properly identified with a project/contact URL.",
                checked_at_utc=now,
                robot_parser=None,
            )
            self._cached_policy = policy
            return policy

        parser = urllib.robotparser.RobotFileParser(robots_url)
        current_url = robots_url
        robots_host = host_of(robots_url)
        response: requests.Response | None = None
        try:
            for _ in range(MAX_REDIRECTS + 1):
                self.rate_limiter.wait(origin_of(current_url), self.min_delay_seconds)
                response = self.session.get(
                    current_url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/plain,text/html;q=0.5,*/*;q=0.1",
                    },
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
                status_code = response.status_code
                if status_code in REDIRECT_STATUSES:
                    location = (response.headers or {}).get("Location")
                    response.close()
                    if not location:
                        self._record_http_event(
                            purpose="POLICY_AUDIT",
                            url=current_url,
                            http_status=status_code,
                            outcome="redirect-missing-location",
                        )
                        raise PolicyBlockedError(
                            "robots.txt redirect missing Location header",
                            source_id=self.source_id,
                            url=current_url,
                            http_status=status_code,
                        )
                    next_url = canonicalize_url(urljoin(current_url, location))
                    if host_of(next_url) != robots_host:
                        self._record_http_event(
                            purpose="POLICY_AUDIT",
                            url=current_url,
                            http_status=status_code,
                            outcome="blocked-cross-host-redirect",
                        )
                        raise PolicyBlockedError(
                            f"robots.txt redirected across hosts to {next_url}; fail-closed",
                            source_id=self.source_id,
                            url=current_url,
                            http_status=status_code,
                        )
                    self._record_http_event(
                        purpose="POLICY_AUDIT",
                        url=current_url,
                        http_status=status_code,
                        outcome="redirect-followed",
                    )
                    current_url = next_url
                    continue
                break
            else:
                raise PolicyBlockedError(
                    "Too many redirects while fetching robots.txt",
                    source_id=self.source_id,
                    url=robots_url,
                )

            assert response is not None
            status_code = response.status_code
            if status_code in {429, 503}:
                retry_after = parse_retry_after((response.headers or {}).get("Retry-After"))
                response.close()
                self._record_http_event(
                    purpose="POLICY_AUDIT",
                    url=current_url,
                    http_status=status_code,
                    outcome="rate-limited",
                )
                policy = PolicyAuditResult(
                    source_id=self.source_id,
                    adapter_id=self.adapter_id,
                    display_name=self.display_name,
                    origin=self.origin,
                    policy_url=robots_url,
                    terms_of_use_url=terms_of_use_url,
                    status="BLOCKED",
                    metadata_collection_decision="DENIED",
                    http_status=status_code,
                    observed_crawl_delay_seconds=retry_after,
                    effective_delay_seconds=max(self.min_delay_seconds, retry_after or 0.0),
                    required_identification=required_identification,
                    rate_limit_behavior=rate_limit_behavior,
                    allowed_scope=allowed_scope,
                    observed_restrictions=restrictions + [f"HTTP {status_code} rate limit/unavailable on policy check"],
                    unresolved_questions=unresolved_questions,
                    sitemap_urls=[],
                    evidence_summary=f"Fail-closed: policy endpoint returned HTTP {status_code}.",
                    checked_at_utc=now,
                    robot_parser=None,
                )
                self._cached_policy = policy
                return policy

            if 200 <= status_code < 300:
                raw = self._read_limited(response)
                encoding = getattr(response, "encoding", None) or "utf-8"
                text = raw.decode(encoding, errors="replace")
                response.close()

                # Guard against binary or completely malformed non-text responses
                if "\x00" in text:
                    self._record_http_event(
                        purpose="POLICY_AUDIT",
                        url=current_url,
                        http_status=status_code,
                        outcome="unverified-corrupt-robots",
                    )
                    policy = PolicyAuditResult(
                        source_id=self.source_id,
                        adapter_id=self.adapter_id,
                        display_name=self.display_name,
                        origin=self.origin,
                        policy_url=robots_url,
                        terms_of_use_url=terms_of_use_url,
                        status="UNVERIFIED",
                        metadata_collection_decision="REQUIRES_REVIEW",
                        http_status=status_code,
                        observed_crawl_delay_seconds=None,
                        effective_delay_seconds=self.min_delay_seconds,
                        required_identification=required_identification,
                        rate_limit_behavior=rate_limit_behavior,
                        allowed_scope=allowed_scope,
                        observed_restrictions=restrictions + ["robots.txt contained binary/null bytes"],
                        unresolved_questions=unresolved_questions,
                        sitemap_urls=[],
                        evidence_summary="Fail-closed: robots.txt could not be interpreted reliably.",
                        checked_at_utc=now,
                        robot_parser=None,
                    )
                    self._cached_policy = policy
                    return policy

                parser.set_url(robots_url)
                lines = text.splitlines()
                parser.parse(lines)

                raw_delay = parser.crawl_delay(self.user_agent)
                if raw_delay is None:
                    raw_delay = parser.crawl_delay("*")
                if raw_delay is None:
                    raw_delay = extract_fallback_crawl_delay(lines, self.user_agent)
                observed_delay = float(raw_delay) if raw_delay is not None else None
                effective_delay = max(self.min_delay_seconds, observed_delay or 0.0)

                sitemaps = [
                    line.split(":", 1)[1].strip()
                    for line in lines
                    if line.strip().lower().startswith("sitemap:") and ":" in line
                ]
                disallow_lines = [
                    line.strip()
                    for line in lines
                    if line.strip().lower().startswith("disallow:")
                    and line.split(":", 1)[1].strip()
                ]
                if disallow_lines:
                    restrictions.append(
                        f"Observed {len(disallow_lines)} Disallow rule(s): "
                        + ", ".join(disallow_lines[:8])
                    )
                if observed_delay is not None:
                    restrictions.append(f"Observed Crawl-delay: {observed_delay}s")

                probes_allowed = all(
                    parser.can_fetch(self.user_agent, probe) and parser.can_fetch("*", probe)
                    for probe in probe_urls
                )
                if probes_allowed:
                    status = "OK"
                    decision = "PERMITTED"
                    outcome = "policy-verified-ok"
                    summary = (
                        f"robots.txt HTTP {status_code} verified; target scope permitted; "
                        f"effective delay {effective_delay:.1f}s "
                        f"(observed Crawl-delay: {observed_delay if observed_delay is not None else 'none'})."
                    )
                else:
                    status = "BLOCKED"
                    decision = "DENIED"
                    outcome = "policy-blocked-by-robots"
                    restrictions.append("Target scope disallowed by robots.txt rules")
                    summary = f"Fail-closed: robots.txt HTTP {status_code} disallows one or more target scope URLs."

                self._record_http_event(
                    purpose="POLICY_AUDIT",
                    url=current_url,
                    http_status=status_code,
                    outcome=outcome,
                )
                policy = PolicyAuditResult(
                    source_id=self.source_id,
                    adapter_id=self.adapter_id,
                    display_name=self.display_name,
                    origin=self.origin,
                    policy_url=robots_url,
                    terms_of_use_url=terms_of_use_url,
                    status=status,
                    metadata_collection_decision=decision,
                    http_status=status_code,
                    observed_crawl_delay_seconds=observed_delay,
                    effective_delay_seconds=effective_delay,
                    required_identification=required_identification,
                    rate_limit_behavior=rate_limit_behavior,
                    allowed_scope=allowed_scope,
                    observed_restrictions=restrictions,
                    unresolved_questions=unresolved_questions,
                    sitemap_urls=sitemaps,
                    evidence_summary=summary,
                    checked_at_utc=now,
                    robot_parser=parser,
                )
                self._cached_policy = policy
                return policy

            # Any other HTTP status (e.g. 401, 403, 404, 500) -> fail closed for HTML policy audit
            # unless explicitly handled by RFC 9309, wait: Task 012 says:
            # "If a site policy cannot be fetched or interpreted reliably, fail closed for that source's HTML collection and explain the blocker."
            response.close()
            self._record_http_event(
                purpose="POLICY_AUDIT",
                url=current_url,
                http_status=status_code,
                outcome=f"policy-http-{status_code}-fail-closed",
            )
            policy = PolicyAuditResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                display_name=self.display_name,
                origin=self.origin,
                policy_url=robots_url,
                terms_of_use_url=terms_of_use_url,
                status="BLOCKED" if status_code in {401, 403, 451} else "UNVERIFIED",
                metadata_collection_decision="DENIED" if status_code in {401, 403, 451} else "REQUIRES_REVIEW",
                http_status=status_code,
                observed_crawl_delay_seconds=None,
                effective_delay_seconds=self.min_delay_seconds,
                required_identification=required_identification,
                rate_limit_behavior=rate_limit_behavior,
                allowed_scope=allowed_scope,
                observed_restrictions=restrictions + [f"Policy endpoint returned HTTP {status_code}"],
                unresolved_questions=unresolved_questions,
                sitemap_urls=[],
                evidence_summary=f"Fail-closed: policy endpoint {robots_url} returned HTTP {status_code}; refusing to infer permission.",
                checked_at_utc=now,
                robot_parser=None,
            )
            self._cached_policy = policy
            return policy

        except PolicyBlockedError as exc:
            policy = PolicyAuditResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                display_name=self.display_name,
                origin=self.origin,
                policy_url=robots_url,
                terms_of_use_url=terms_of_use_url,
                status="BLOCKED",
                metadata_collection_decision="DENIED",
                http_status=exc.http_status,
                observed_crawl_delay_seconds=None,
                effective_delay_seconds=self.min_delay_seconds,
                required_identification=required_identification,
                rate_limit_behavior=rate_limit_behavior,
                allowed_scope=allowed_scope,
                observed_restrictions=restrictions + [str(exc)],
                unresolved_questions=unresolved_questions,
                sitemap_urls=[],
                evidence_summary=f"Fail-closed: {exc}",
                checked_at_utc=now,
                robot_parser=None,
            )
            self._cached_policy = policy
            return policy
        except Exception as exc:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self._record_http_event(
                purpose="POLICY_AUDIT",
                url=current_url,
                http_status=None,
                outcome="policy-fetch-error",
                error=f"{type(exc).__name__}: {exc}",
            )
            policy = PolicyAuditResult(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                display_name=self.display_name,
                origin=self.origin,
                policy_url=robots_url,
                terms_of_use_url=terms_of_use_url,
                status="UNVERIFIED",
                metadata_collection_decision="REQUIRES_REVIEW",
                http_status=None,
                observed_crawl_delay_seconds=None,
                effective_delay_seconds=self.min_delay_seconds,
                required_identification=required_identification,
                rate_limit_behavior=rate_limit_behavior,
                allowed_scope=allowed_scope,
                observed_restrictions=restrictions + [f"Policy fetch error: {type(exc).__name__}: {exc}"],
                unresolved_questions=unresolved_questions,
                sitemap_urls=[],
                evidence_summary=f"Fail-closed: policy check failed ({type(exc).__name__}: {exc}).",
                checked_at_utc=now,
                robot_parser=None,
            )
            self._cached_policy = policy
            return policy

    @abstractmethod
    def audit_policy(self, *, force_refresh: bool = False) -> PolicyAuditResult:
        """Audit the source's robots.txt or official API policy before content fetches."""

    @abstractmethod
    def discover_candidates(
        self,
        *,
        seed_manifest: list[dict[str, Any]] | None = None,
        limit: int = 2,
    ) -> list[SourceCandidate]:
        """Return bounded candidate resources for metadata collection."""

    @abstractmethod
    def fetch_metadata(
        self,
        candidate: SourceCandidate,
        *,
        policy: PolicyAuditResult | None = None,
        conditional_headers: dict[str, str] | None = None,
    ) -> RawMetadataPayload:
        """Retrieve raw metadata payload for `candidate` after verifying policy & scope."""

    @abstractmethod
    def normalize(
        self,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> NormalizedResourceEnvelope:
        """Normalize raw metadata payload into the canonical resource envelope."""

    def collect_candidate(
        self,
        candidate: SourceCandidate,
        *,
        policy: PolicyAuditResult | None = None,
        conditional_headers: dict[str, str] | None = None,
    ) -> NormalizedResourceEnvelope:
        """Audit policy (if not provided), fetch metadata, and return normalized envelope."""
        active_policy = policy or self._cached_policy or self.audit_policy()
        raw = self.fetch_metadata(
            candidate,
            policy=active_policy,
            conditional_headers=conditional_headers,
        )
        return self.normalize(raw, policy=active_policy)
