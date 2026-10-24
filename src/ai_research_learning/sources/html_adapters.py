"""Conservative HTML metadata adapters for first-party researcher and educator websites.

Covers:
- HintonSourceAdapter (SRC-HINTON): Geoffrey Hinton official website
- BengioSourceAdapter (SRC-BENGIO): Yoshua Bengio official website
- ThreeBlueOneBrownAdapter (SRC-3B1B): 3Blue1Brown official educational website

All three adapters:
- Audit robots.txt before any content fetch and fail closed when policy is blocked or unverified
- Restrict requests to approved hosts/path prefixes and reject binary/PDF downloads
- Enforce sequential per-origin rate limiting and conditional GET headers
- Extract metadata-only envelopes with discovered link candidates (no recursive crawling)
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .base import (
    MAX_REDIRECTS,
    REDIRECT_STATUSES,
    DiscoveredLinkCandidate,
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
    host_of,
    is_binary_asset_url,
    is_identified_user_agent,
    origin_of,
    parse_retry_after,
    utc_now,
)


def _clean_text(value: str | None, max_chars: int = 400) -> str | None:
    if not value:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    if not collapsed:
        return None
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 1].rstrip() + "…"
    return collapsed


class ConservativeHtmlAdapter(SourceAdapter):
    """Base class for conservative, policy-gated HTML metadata adapters."""

    allowed_hosts: tuple[str, ...] = ()
    allowed_path_prefixes: tuple[str, ...] = ("/",)
    robots_url: str = ""
    terms_of_use_url: str | None = None
    probe_urls: tuple[str, ...] = ()
    default_authors: str | None = None
    default_candidates: tuple[dict[str, str], ...] = ()
    unresolved_policy_questions: tuple[str, ...] = ()
    base_policy_restrictions: tuple[str, ...] = (
        "Metadata-only storage; no full HTML body, article text, transcript, or PDF storage",
        "No recursive link crawling; discovered links are logged as candidates only",
    )

    def is_url_in_scope(self, url: str) -> bool:
        """Return True if `url` is HTTP(S), on an allowed host/path prefix, and not a binary asset."""
        try:
            normalized = canonicalize_url(url)
        except ValueError:
            return False
        if is_binary_asset_url(normalized):
            return False
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").lower()
        if host not in self.allowed_hosts:
            return False
        path = parsed.path or "/"
        return any(path.startswith(prefix) for prefix in self.allowed_path_prefixes)

    def audit_policy(self, *, force_refresh: bool = False) -> PolicyAuditResult:
        """Audit the origin's robots.txt policy with fail-closed behavior."""
        if self._cached_policy is not None and not force_refresh:
            return self._cached_policy
        allowed_scope = [
            f"https://{host}{prefix}*"
            for host in self.allowed_hosts
            for prefix in self.allowed_path_prefixes
        ]
        return self._fetch_robots_policy(
            robots_url=self.robots_url,
            terms_of_use_url=self.terms_of_use_url,
            probe_urls=list(self.probe_urls),
            allowed_scope=allowed_scope,
            required_identification=(
                "Descriptive User-Agent header with project name and repository/contact URL"
            ),
            rate_limit_behavior=(
                f"Sequential single-worker requests; >= {self.min_delay_seconds:.1f}s minimum delay "
                "or robots.txt Crawl-delay if higher; immediate stop on HTTP 429/503"
            ),
            unresolved_questions=list(self.unresolved_policy_questions),
            base_restrictions=list(self.base_policy_restrictions),
        )

    def discover_candidates(
        self,
        *,
        seed_manifest: list[dict[str, Any]] | None = None,
        limit: int = 2,
    ) -> list[SourceCandidate]:
        """Select up to `limit` curated HTML candidates within this adapter's scope."""
        max_items = max(0, int(limit))
        if max_items == 0:
            return []

        records: list[dict[str, Any]] = []
        if seed_manifest:
            for item in seed_manifest:
                url = str(item.get("url") or "").strip()
                if url and self.is_url_in_scope(url):
                    records.append(item)
        if not records:
            records = [dict(item) for item in self.default_candidates]

        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        records.sort(
            key=lambda item: (
                priority_rank.get(str(item.get("priority") or "P1"), 9),
                str(item.get("id") or ""),
            )
        )

        candidates: list[SourceCandidate] = []
        seen_urls: set[str] = set()
        for item in records:
            raw_url = str(item.get("url") or "").strip()
            if not raw_url or not self.is_url_in_scope(raw_url):
                continue
            normalized_url = canonicalize_url(raw_url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            candidates.append(
                SourceCandidate(
                    source_id=self.source_id,
                    url=normalized_url,
                    title_hint=item.get("page_type") or item.get("title"),
                    resource_type_hint=str(item.get("page_type") or "web_page"),
                    manifest_id=item.get("id"),
                    priority=str(item.get("priority") or "P1"),
                    observed_from=item.get("observed_from"),
                    extra={
                        "domain_scope": item.get("domain_scope", "First-party"),
                        "information_to_inventory": item.get("information_to_inventory"),
                        "collection_decision": item.get("collection_decision"),
                    },
                )
            )
            if len(candidates) >= max_items:
                break
        return candidates

    def fetch_metadata(
        self,
        candidate: SourceCandidate,
        *,
        policy: PolicyAuditResult | None = None,
        conditional_headers: dict[str, str] | None = None,
    ) -> RawMetadataPayload:
        """Fetch an HTML page within scope after verifying policy compliance."""
        if not is_identified_user_agent(self.user_agent):
            raise PolicyBlockedError(
                "User-Agent must identify the project and contact URL before fetching HTML resources",
                source_id=self.source_id,
                url=candidate.url,
            )

        normalized_url = canonicalize_url(candidate.url)
        if is_binary_asset_url(normalized_url):
            raise ScopeViolationError(
                f"Binary or PDF asset download is forbidden in metadata-only mode: {normalized_url}",
                source_id=self.source_id,
                url=normalized_url,
            )
        if not self.is_url_in_scope(normalized_url):
            raise ScopeViolationError(
                f"Candidate URL is outside allowed scope for {self.source_id}: {normalized_url}",
                source_id=self.source_id,
                url=normalized_url,
            )

        active_policy = policy or self._cached_policy or self.audit_policy()
        if not active_policy.is_metadata_collection_permitted:
            raise PolicyBlockedError(
                f"Policy audit for {self.source_id} did not permit collection "
                f"(status={active_policy.status}, decision={active_policy.metadata_collection_decision})",
                source_id=self.source_id,
                url=normalized_url,
                http_status=active_policy.http_status,
            )
        if not active_policy.can_fetch_url(self.user_agent, normalized_url):
            raise PolicyBlockedError(
                f"robots.txt disallows fetching {normalized_url}",
                source_id=self.source_id,
                url=normalized_url,
            )

        if not conditional_headers and normalized_url in self._response_cache:
            cached = self._response_cache[normalized_url]
            return RawMetadataPayload(
                source_id=cached.source_id,
                adapter_id=cached.adapter_id,
                requested_url=normalized_url,
                final_url=cached.final_url,
                http_status=cached.http_status,
                content_type=cached.content_type,
                headers=dict(cached.headers),
                body_text=cached.body_text,
                fetched_at_utc=cached.fetched_at_utc,
                from_cache_or_not_modified=True,
                candidate=candidate,
            )

        effective_delay = max(self.min_delay_seconds, active_policy.effective_delay_seconds)
        current_url = normalized_url
        response: requests.Response | None = None

        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        }
        if conditional_headers:
            for key in ("If-None-Match", "If-Modified-Since"):
                if conditional_headers.get(key):
                    req_headers[key] = conditional_headers[key]

        try:
            for _ in range(MAX_REDIRECTS + 1):
                self.rate_limiter.wait(origin_of(current_url), effective_delay)
                response = self.session.get(
                    current_url,
                    headers=req_headers,
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
                            purpose="METADATA_FETCH",
                            url=current_url,
                            http_status=status_code,
                            outcome="redirect-missing-location",
                        )
                        raise TransientFetchError(
                            f"Redirect HTTP {status_code} missing Location header",
                            source_id=self.source_id,
                            url=current_url,
                            http_status=status_code,
                            is_retryable=False,
                        )
                    next_url = canonicalize_url(urljoin(current_url, location))
                    if is_binary_asset_url(next_url) or not self.is_url_in_scope(next_url):
                        self._record_http_event(
                            purpose="METADATA_FETCH",
                            url=current_url,
                            http_status=status_code,
                            outcome="blocked-out-of-scope-redirect",
                        )
                        raise ScopeViolationError(
                            f"Redirect target {next_url} is outside allowed HTML scope for {self.source_id}",
                            source_id=self.source_id,
                            url=next_url,
                        )
                    if not active_policy.can_fetch_url(self.user_agent, next_url):
                        self._record_http_event(
                            purpose="METADATA_FETCH",
                            url=current_url,
                            http_status=status_code,
                            outcome="blocked-robots-redirect",
                        )
                        raise PolicyBlockedError(
                            f"Redirect target {next_url} is disallowed by robots.txt",
                            source_id=self.source_id,
                            url=next_url,
                        )
                    self._record_http_event(
                        purpose="METADATA_FETCH",
                        url=current_url,
                        http_status=status_code,
                        outcome="redirect-followed",
                    )
                    current_url = next_url
                    continue
                break
            else:
                raise ScopeViolationError(
                    f"Exceeded maximum redirects ({MAX_REDIRECTS}) for {normalized_url}",
                    source_id=self.source_id,
                    url=normalized_url,
                )

            assert response is not None
            status_code = response.status_code
            headers_dict = dict(response.headers or {})

            if status_code == 304:
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=current_url,
                    http_status=304,
                    outcome="not-modified-304",
                )
                raise NotModifiedResponse(current_url, headers_dict)

            if status_code in {429, 503}:
                retry_after = parse_retry_after(headers_dict.get("Retry-After"))
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=current_url,
                    http_status=status_code,
                    outcome="rate-limited",
                )
                raise RateLimitError(
                    f"Rate limited (HTTP {status_code}) fetching {current_url}",
                    source_id=self.source_id,
                    url=current_url,
                    http_status=status_code,
                    retry_after_seconds=retry_after,
                )

            if status_code in {401, 403, 451}:
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=current_url,
                    http_status=status_code,
                    outcome="policy-or-access-blocked",
                )
                raise PolicyBlockedError(
                    f"Access blocked with HTTP {status_code} for {current_url}",
                    source_id=self.source_id,
                    url=current_url,
                    http_status=status_code,
                )

            if status_code < 200 or status_code >= 300:
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=current_url,
                    http_status=status_code,
                    outcome=f"http-error-{status_code}",
                )
                raise TransientFetchError(
                    f"Unexpected HTTP {status_code} fetching {current_url}",
                    source_id=self.source_id,
                    url=current_url,
                    http_status=status_code,
                    is_retryable=status_code >= 500,
                )

            content_type = (headers_dict.get("Content-Type") or "").lower()
            if content_type and not (
                "text/html" in content_type or "application/xhtml+xml" in content_type
            ):
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=current_url,
                    http_status=status_code,
                    outcome="rejected-non-html-content-type",
                )
                raise ScopeViolationError(
                    f"Expected HTML content type but received '{content_type}' from {current_url}",
                    source_id=self.source_id,
                    url=current_url,
                )

            raw_bytes = self._read_limited(response)
            encoding = getattr(response, "encoding", None) or "utf-8"
            body_text = raw_bytes.decode(encoding, errors="replace")
            response.close()

            self._record_http_event(
                purpose="METADATA_FETCH",
                url=current_url,
                http_status=status_code,
                outcome="fetched-ok",
            )
            payload = RawMetadataPayload(
                source_id=self.source_id,
                adapter_id=self.adapter_id,
                requested_url=normalized_url,
                final_url=current_url,
                http_status=status_code,
                content_type=content_type or "text/html",
                headers=headers_dict,
                body_text=body_text,
                fetched_at_utc=utc_now(),
                from_cache_or_not_modified=False,
                candidate=candidate,
            )
            self._response_cache[normalized_url] = payload
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
                url=current_url,
                http_status=None,
                outcome="network-exception",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise TransientFetchError(
                f"Network error fetching {current_url}: {exc}",
                source_id=self.source_id,
                url=current_url,
                is_retryable=True,
            ) from exc

    def _extract_json_ld_metadata(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Extract lightweight publication/author metadata from JSON-LD scripts if present."""
        extracted: dict[str, Any] = {}
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw_json = script.string or script.get_text()
            if not raw_json:
                continue
            try:
                data = json.loads(raw_json)
            except Exception:
                continue
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if "@graph" in node and isinstance(node["@graph"], list):
                    nodes.extend(item for item in node["@graph"] if isinstance(item, dict))
                for date_key, target_key in (
                    ("datePublished", "published_at"),
                    ("dateModified", "updated_at_source"),
                ):
                    if node.get(date_key) and not extracted.get(target_key):
                        extracted[target_key] = str(node[date_key]).strip()
                author = node.get("author")
                if author and not extracted.get("authors"):
                    if isinstance(author, dict) and author.get("name"):
                        extracted["authors"] = str(author["name"]).strip()
                    elif isinstance(author, list):
                        names = [
                            str(a.get("name")).strip()
                            for a in author
                            if isinstance(a, dict) and a.get("name")
                        ]
                        if names:
                            extracted["authors"] = ", ".join(names)
        return extracted

    def _extract_common_html_metadata(
        self,
        raw: RawMetadataPayload,
    ) -> tuple[BeautifulSoup, dict[str, Any]]:
        if not raw.body_text or "\x00" in raw.body_text:
            raise ParseError(
                f"Empty or corrupt HTML body from {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )
        soup = BeautifulSoup(raw.body_text, "html.parser")
        if soup.find("html") is None and soup.find("title") is None and soup.find("body") is None:
            raise ParseError(
                f"Response from {raw.final_url} does not contain recognizable HTML structure",
                source_id=self.source_id,
                url=raw.final_url,
            )

        title_tag = soup.title.string.strip() if (soup.title and soup.title.string) else None
        og_title_tag = soup.find("meta", attrs={"property": "og:title"})
        og_title = (
            str(og_title_tag.get("content")).strip()
            if og_title_tag and og_title_tag.get("content")
            else None
        )
        h1_tag = soup.find("h1")
        h1_text = _clean_text(h1_tag.get_text(" ", strip=True), max_chars=200) if h1_tag else None
        title = (
            _clean_text(title_tag, max_chars=240)
            or _clean_text(og_title, max_chars=240)
            or h1_text
            or (raw.candidate.title_hint if raw.candidate else None)
            or raw.final_url
        )

        meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        og_desc_tag = soup.find("meta", attrs={"property": "og:description"})
        description = _clean_text(
            (meta_desc_tag.get("content") if meta_desc_tag else None)
            or (og_desc_tag.get("content") if og_desc_tag else None),
            max_chars=360,
        )

        canonical_link = None
        link_tag = soup.find("link", rel=lambda v: v and "canonical" in str(v).lower())
        if link_tag and link_tag.get("href"):
            try:
                resolved = canonicalize_url(urljoin(raw.final_url, str(link_tag["href"])))
                if host_of(resolved) in self.allowed_hosts:
                    canonical_link = resolved
            except ValueError:
                canonical_link = None

        html_tag = soup.find("html")
        language = (
            str(html_tag.get("lang")).strip()
            if html_tag and html_tag.get("lang")
            else None
        )

        json_ld = self._extract_json_ld_metadata(soup)

        pub_meta = soup.find(
            "meta",
            attrs={"property": re.compile(r"^(article:published_time|og:published_time)$", re.I)},
        ) or soup.find("meta", attrs={"name": re.compile(r"^(date|pubdate|dc\.date)$", re.I)})
        mod_meta = soup.find(
            "meta",
            attrs={"property": re.compile(r"^(article:modified_time|og:updated_time)$", re.I)},
        )
        time_tag = soup.find("time", attrs={"datetime": True})

        published_at = (
            (str(pub_meta.get("content")).strip() if pub_meta and pub_meta.get("content") else None)
            or json_ld.get("published_at")
            or (str(time_tag.get("datetime")).strip() if time_tag and time_tag.get("datetime") else None)
        )
        updated_at_source = (
            (str(mod_meta.get("content")).strip() if mod_meta and mod_meta.get("content") else None)
            or json_ld.get("updated_at_source")
            or raw.headers.get("Last-Modified")
        )

        author_meta = soup.find("meta", attrs={"name": re.compile(r"^author$", re.I)})
        authors = (
            (str(author_meta.get("content")).strip() if author_meta and author_meta.get("content") else None)
            or json_ld.get("authors")
            or self.default_authors
        )

        discovered_links: list[DiscoveredLinkCandidate] = []
        rejected_discovered_links: list[dict[str, Any]] = []
        official_links: list[str] = []
        seen_links: set[str] = set()
        seen_rejected: set[str] = set()
        manifest_id = raw.candidate.manifest_id if raw.candidate else None

        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue

            rel_val = a_tag.get("rel")
            rel_str = " ".join(rel_val) if isinstance(rel_val, list) else (str(rel_val) if rel_val else None)
            link_text = _clean_text(a_tag.get_text(" ", strip=True), max_chars=140)

            parsed_href = urlsplit(href)
            raw_scheme = parsed_href.scheme.lower()
            if raw_scheme and raw_scheme not in ("http", "https"):
                if href not in seen_rejected and len(rejected_discovered_links) < 50:
                    seen_rejected.add(href)
                    rejected_discovered_links.append(
                        {
                            "from_url": raw.final_url,
                            "from_manifest_id": manifest_id,
                            "raw_href": href,
                            "original_url": href,
                            "scheme": raw_scheme,
                            "reason": f"Rejected non-HTTP(S) or unsafe URL scheme '{raw_scheme}:'",
                            "link_text": link_text,
                            "rel": rel_str,
                        }
                    )
                continue

            original_url = urljoin(raw.final_url, href)
            try:
                abs_url = canonicalize_url(original_url)
            except ValueError as exc:
                if original_url not in seen_rejected and len(rejected_discovered_links) < 50:
                    seen_rejected.add(original_url)
                    rejected_discovered_links.append(
                        {
                            "from_url": raw.final_url,
                            "from_manifest_id": manifest_id,
                            "raw_href": href,
                            "original_url": original_url,
                            "scheme": raw_scheme or "invalid",
                            "reason": f"Rejected malformed candidate URL: {exc}",
                            "link_text": link_text,
                            "rel": rel_str,
                        }
                    )
                continue

            if abs_url in seen_links:
                continue
            seen_links.add(abs_url)

            link_host = host_of(abs_url)
            is_internal = link_host in self.allowed_hosts
            domain_classification = "INTERNAL" if is_internal else "EXTERNAL"

            if is_internal:
                if is_binary_asset_url(abs_url):
                    target_scope = "same-host-binary-not-fetched"
                    decision = "record-citation-url-only-no-download"
                    policy_state = "SAME_HOST_BINARY_CITATION_NO_FETCH"
                elif self.is_url_in_scope(abs_url):
                    target_scope = "in-scope-html"
                    decision = "candidate-only-no-recursive-crawl"
                    policy_state = "IN_SCOPE_HTML_CANDIDATE_NO_AUTO_FETCH"
                else:
                    target_scope = "same-host-outside-prefix"
                    decision = "skip-outside-prefix"
                    policy_state = "SAME_HOST_OUTSIDE_PREFIX_NO_FETCH"
            elif any(
                ext_host in link_host
                for ext_host in (
                    "youtube.com",
                    "youtu.be",
                    "arxiv.org",
                    "mila.quebec",
                    "scholar.google",
                    "github.com",
                )
            ):
                target_scope = "external-official-reference"
                decision = "record-official-external-reference-only"
                policy_state = "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH"
                if len(official_links) < 12:
                    official_links.append(abs_url)
            else:
                target_scope = "external-not-fetched"
                decision = "skip-external-host"
                policy_state = "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH"

            if len(discovered_links) < 50:
                discovered_links.append(
                    DiscoveredLinkCandidate(
                        from_url=raw.final_url,
                        from_manifest_id=manifest_id,
                        link_url=abs_url,
                        link_text=link_text,
                        rel=rel_str,
                        target_scope=target_scope,
                        decision=decision,
                        raw_href=href,
                        original_url=original_url,
                        host_domain=link_host,
                        is_internal=is_internal,
                        domain_classification=domain_classification,
                        policy_state=policy_state,
                        review_status="PENDING_REVIEW",
                        fetch_permission_granted=False,
                    )
                )

        common = {
            "title": title,
            "description": description,
            "canonical_url": canonical_link or raw.requested_url,
            "language": language,
            "published_at": published_at,
            "updated_at_source": updated_at_source,
            "authors": authors,
            "h1": h1_text,
            "discovered_links": discovered_links,
            "rejected_discovered_links": rejected_discovered_links,
            "official_links": official_links,
        }
        return soup, common

    def _extract_source_specific_metadata(
        self,
        soup: BeautifulSoup,
        raw: RawMetadataPayload,
        common: dict[str, Any],
    ) -> dict[str, Any]:
        return {}

    def normalize(
        self,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> NormalizedResourceEnvelope:
        """Convert an HTML payload into a metadata-only NormalizedResourceEnvelope."""
        soup, common = self._extract_common_html_metadata(raw)
        source_specific = self._extract_source_specific_metadata(soup, raw, common)
        candidate = raw.candidate
        active_policy = policy or self._cached_policy

        # Keep canonical_url aligned with the candidate URL if a manifest_id is bound to it
        # and store any differing HTML <link rel="canonical"> in source_specific_metadata.
        canonical_url = common["canonical_url"]
        if candidate and candidate.manifest_id and canonical_url != raw.requested_url:
            source_specific["html_declared_canonical_url"] = canonical_url
            canonical_url = raw.requested_url

        completeness = {
            "has_title": bool(common["title"]),
            "has_authors": bool(common["authors"]),
            "has_published_or_updated_date": bool(
                common["published_at"] or common["updated_at_source"]
            ),
            "has_description": bool(common["description"]),
            "has_language": bool(common["language"]),
            "discovered_link_count": len(common["discovered_links"]),
        }

        robots_summary = (
            f"{active_policy.status}: {active_policy.evidence_summary}"
            if active_policy
            else "Policy verified"
        )

        return NormalizedResourceEnvelope(
            source_id=self.source_id,
            adapter_id=self.adapter_id,
            canonical_url=canonical_url,
            fetched_url=raw.final_url,
            title=common["title"],
            resource_type=(candidate.resource_type_hint if candidate else "web_page"),
            manifest_id=(candidate.manifest_id if candidate else None),
            authors=common["authors"],
            published_at=common["published_at"],
            updated_at_source=common["updated_at_source"],
            media_modality=str(source_specific.get("media_modality") or "TEXT_HTML"),
            domain_scope=str(
                (candidate.extra.get("domain_scope") if candidate else None) or "First-party"
            ),
            abstract_or_description=common["description"],
            topic_tags=source_specific.get("topic_tags"),
            external_identifier=source_specific.get("external_identifier"),
            language=common["language"] or "en",
            transcript_reference=source_specific.get("transcript_reference"),
            official_links=common["official_links"],
            observed_from=(candidate.observed_from if candidate else None),
            information_to_inventory=(
                candidate.extra.get("information_to_inventory") if candidate else None
            ),
            priority=(candidate.priority if candidate else "P1"),
            collection_decision=(
                (candidate.extra.get("collection_decision") if candidate else None)
                or "Metadata-only HTML pilot collection"
            ),
            verification_status=f"Verified HTTP {raw.http_status} via {self.adapter_id}",
            robots_sitemap_status=robots_summary,
            rights_status="All rights reserved; metadata-only cataloguing with canonical link-out",
            license_status="ALL_RIGHTS_RESERVED_METADATA_ONLY",
            license_spdx=None,
            permitted_storage_scope="METADATA_ONLY",
            permits_full_text_storage=False,
            permits_verbatim_excerpts=False,
            legal_uncertainty_status="UNVERIFIED_REQUIRES_HUMAN_REVIEW",
            text_storage_status="metadata-only",
            collected_at_utc=raw.fetched_at_utc,
            http_status=raw.http_status,
            etag=raw.headers.get("ETag"),
            last_modified_header=raw.headers.get("Last-Modified"),
            metadata_completeness=completeness,
            source_specific_metadata=source_specific,
            discovered_links=common["discovered_links"],
            rejected_discovered_links=common["rejected_discovered_links"],
        )


class HintonSourceAdapter(ConservativeHtmlAdapter):
    """Conservative HTML metadata adapter for Geoffrey Hinton's official website."""

    source_id = "SRC-HINTON"
    adapter_id = "hinton-html-adapter/0.1"
    display_name = "Geoffrey Hinton — Official Academic Page"
    origin = "https://www.cs.toronto.edu"
    min_delay_seconds = 5.0
    allowed_hosts = ("www.cs.toronto.edu",)
    allowed_path_prefixes = ("/~hinton/",)
    robots_url = "https://www.cs.toronto.edu/robots.txt"
    terms_of_use_url = None
    probe_urls = ("https://www.cs.toronto.edu/~hinton/",)
    default_authors = "Geoffrey E. Hinton"
    default_candidates = (
        {
            "id": "HI-URL-001",
            "url": "https://www.cs.toronto.edu/~hinton/",
            "page_type": "Academic Home Page",
            "priority": "P0",
            "domain_scope": "First-party",
            "observed_from": "Direct Seed",
            "information_to_inventory": "Bio, Publications, Lectures",
            "collection_decision": "Fetch HTML Index Only",
        },
    )
    unresolved_policy_questions = (
        "Legacy static HTML site without structured publication API or per-item machine-readable license metadata.",
        "Linked PDFs under /~hinton/absps/ are copyright-variable; only bibliographic link targets are catalogued, never downloaded.",
    )

    def _extract_source_specific_metadata(
        self,
        soup: BeautifulSoup,
        raw: RawMetadataPayload,
        common: dict[str, Any],
    ) -> dict[str, Any]:
        pdf_citations: list[dict[str, str]] = []
        html_subpages: list[str] = []
        for link in common["discovered_links"]:
            if link.target_scope == "same-host-binary-not-fetched" and link.link_url.lower().endswith(".pdf"):
                pdf_citations.append(
                    {
                        "url": link.link_url,
                        "title_hint": link.link_text or Path_basename(link.link_url),
                    }
                )
            elif link.target_scope == "in-scope-html":
                html_subpages.append(link.link_url)

        return {
            "source_family": "academic_homepage",
            "institution": "University of Toronto",
            "topic_tags": "deep learning, neural networks, Boltzmann machines, representation learning",
            "catalogued_pdf_citation_count": len(pdf_citations),
            "sample_pdf_citations_not_downloaded": pdf_citations[:10],
            "in_scope_html_subpage_count": len(html_subpages),
            "in_scope_html_subpages": html_subpages[:10],
        }


def Path_basename(url: str) -> str:
    path = urlsplit(url).path
    return path.rsplit("/", 1)[-1] if "/" in path else path


class BengioSourceAdapter(ConservativeHtmlAdapter):
    """Conservative HTML metadata adapter for Yoshua Bengio's official website."""

    source_id = "SRC-BENGIO"
    adapter_id = "bengio-html-adapter/0.1"
    display_name = "Yoshua Bengio — Official Website"
    origin = "https://yoshuabengio.org"
    min_delay_seconds = 5.0
    allowed_hosts = ("yoshuabengio.org", "www.yoshuabengio.org")
    allowed_path_prefixes = ("/",)
    robots_url = "https://yoshuabengio.org/robots.txt"
    terms_of_use_url = "https://yoshuabengio.org/en/"
    probe_urls = (
        "https://yoshuabengio.org/en",
        "https://yoshuabengio.org/en/ai-scientist/",
    )
    default_authors = "Yoshua Bengio"
    default_candidates = (
        {
            "id": "YB-URL-001",
            "url": "https://yoshuabengio.org/en",
            "page_type": "Official Home Page",
            "priority": "P0",
            "domain_scope": "First-party",
            "observed_from": "Direct Seed",
            "information_to_inventory": "Site Structure, Recent Posts, Navigation",
            "collection_decision": "Fetch HTML Index Only",
        },
        {
            "id": "YB-URL-002",
            "url": "https://yoshuabengio.org/en/ai-scientist/",
            "page_type": "Section Landing Page",
            "priority": "P1",
            "domain_scope": "First-party",
            "observed_from": "YB-URL-001",
            "information_to_inventory": "Research Themes, Key Papers",
            "collection_decision": "Fetch HTML Index Only",
        },
    )
    unresolved_policy_questions = (
        "robots.txt advertises a WordPress sitemap; pilot remains restricted to curated HTML pages with Crawl-delay respected.",
        "Blog post body text is not licensed for redistribution; only page titles, dates, categories, and canonical URLs are stored.",
    )

    def _extract_source_specific_metadata(
        self,
        soup: BeautifulSoup,
        raw: RawMetadataPayload,
        common: dict[str, Any],
    ) -> dict[str, Any]:
        headings = [
            _clean_text(tag.get_text(" ", strip=True), max_chars=120)
            for tag in soup.find_all(["h2", "h3"])
        ]
        headings = [h for h in headings if h][:12]
        return {
            "source_family": "researcher_website",
            "affiliation": "Université de Montréal / Mila",
            "topic_tags": "deep learning, AI safety,科學/research themes, governance",
            "section_headings": headings,
        }


class ThreeBlueOneBrownAdapter(ConservativeHtmlAdapter):
    """Conservative HTML metadata adapter for 3Blue1Brown's official website and lesson index."""

    source_id = "SRC-3B1B"
    adapter_id = "threeblueonebrown-html-adapter/0.1"
    display_name = "3Blue1Brown — Official Website & Lesson Index"
    origin = "https://www.3blue1brown.com"
    min_delay_seconds = 5.0
    allowed_hosts = ("www.3blue1brown.com", "3blue1brown.com")
    allowed_path_prefixes = ("/",)
    robots_url = "https://www.3blue1brown.com/robots.txt"
    terms_of_use_url = "https://www.3blue1brown.com/faq"
    probe_urls = (
        "https://www.3blue1brown.com/",
        "https://www.3blue1brown.com/topics/neural-networks",
    )
    default_authors = "Grant Sanderson (3Blue1Brown)"
    default_candidates = (
        {
            "id": "3B-URL-001",
            "url": "https://www.3blue1brown.com/",
            "page_type": "Educational Home Page",
            "priority": "P0",
            "domain_scope": "First-party",
            "observed_from": "Direct Seed",
            "information_to_inventory": "Lesson Index, Series Structure",
            "collection_decision": "Fetch HTML Index Only",
        },
        {
            "id": "3B-URL-002",
            "url": "https://www.3blue1brown.com/topics/neural-networks",
            "page_type": "Topic Index Page",
            "priority": "P0",
            "domain_scope": "First-party",
            "observed_from": "3B-URL-001",
            "information_to_inventory": "Neural Network Lesson URLs",
            "collection_decision": "Fetch HTML Index Only",
        },
    )
    unresolved_policy_questions = (
        "Written lesson text, interactive visuals, and video transcripts are proprietary; only lesson/topic metadata and official links are stored.",
    )

    def _extract_source_specific_metadata(
        self,
        soup: BeautifulSoup,
        raw: RawMetadataPayload,
        common: dict[str, Any],
    ) -> dict[str, Any]:
        lesson_links = [
            link.link_url
            for link in common["discovered_links"]
            if "/lessons/" in link.link_url or "/topics/" in link.link_url
        ]
        youtube_links = [
            link
            for link in common["official_links"]
            if "youtube.com" in link or "youtu.be" in link
        ]
        path = urlsplit(raw.final_url).path
        media_modality = "VIDEO_AND_INTERACTIVE_LESSON_INDEX" if "/topics/" in path or "/lessons/" in path else "TEXT_HTML"
        return {
            "source_family": "educational_visual_lessons",
            "media_modality": media_modality,
            "topic_tags": "mathematics, neural networks, linear algebra, visual intuition",
            "discovered_lesson_or_topic_urls": lesson_links[:15],
            "official_video_links": youtube_links[:10],
            "transcript_reference": (
                "Official written lesson available on 3blue1brown.com (link-out only; full text not stored)"
                if "/lessons/" in path or "/topics/" in path
                else None
            ),
        }
