"""English Wikipedia official MediaWiki API metadata adapter (SRC-WIKIPEDIA).

Follows docs/architecture/source-adapter-contract.md Section 4.5:
- Uses https://en.wikipedia.org/w/api.php (never HTML scraping of /wiki/*)
- Sends mandatory descriptive User-Agent and Api-User-Agent headers with project URL
- Enforces sequential requests, in-memory response caching, and Retry-After handling
- Records pageid, revision_id, revision timestamp, canonical URL, permanent revision URL,
  Wikidata QID, and CC BY-SA 4.0 attribution metadata (metadata-only pilot storage)
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import requests

from .base import (
    NormalizedResourceEnvelope,
    NotModifiedResponse,
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


class WikipediaSourceAdapter(SourceAdapter):
    """Official English Wikipedia MediaWiki Action API metadata adapter."""

    source_id = "SRC-WIKIPEDIA"
    adapter_id = "wikipedia-mediawiki-api-adapter/0.1"
    display_name = "English Wikipedia — MediaWiki API"
    origin = "https://en.wikipedia.org"
    api_endpoint = "https://en.wikipedia.org/w/api.php"
    robots_url = "https://en.wikipedia.org/robots.txt"
    terms_of_use_url = "https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy"
    min_delay_seconds = 1.0

    default_wikipedia_titles: tuple[dict[str, str], ...] = (
        {
            "title": "Backpropagation",
            "priority": "P0",
            "observed_from": "Curated foundational neural network concept",
        },
        {
            "title": "Boltzmann_machine",
            "priority": "P0",
            "observed_from": "Curated foundational Hinton/Sejnowski concept",
        },
    )

    def build_query_url(self, page_title: str) -> str:
        clean_title = page_title.strip().replace(" ", "_")
        if not clean_title:
            raise ValueError("Wikipedia page title must not be empty")
        params = {
            "action": "query",
            "prop": "info|revisions|pageprops|categories",
            "inprop": "url",
            "rvprop": "ids|timestamp",
            "rvslots": "main",
            "cllimit": "10",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
            "titles": clean_title,
        }
        return f"{self.api_endpoint}?{urlencode(params)}"

    def is_url_in_scope(self, url: str) -> bool:
        try:
            normalized = canonicalize_url(url)
        except ValueError:
            return False
        if is_binary_asset_url(normalized):
            return False
        parsed = urlsplit(normalized)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == "en.wikipedia.org"
            and parsed.path == "/w/api.php"
            and "action=query" in parsed.query
        )

    def audit_policy(self, *, force_refresh: bool = False) -> PolicyAuditResult:
        """Audit Wikimedia User-Agent policy and en.wikipedia.org robots.txt."""
        if self._cached_policy is not None and not force_refresh:
            return self._cached_policy

        # Probe /wiki/Backpropagation in robots.txt because Wikimedia's robots.txt
        # disallows /w/index.php HTML crawling while directing automated clients to /w/api.php.
        policy = self._fetch_robots_policy(
            robots_url=self.robots_url,
            terms_of_use_url=self.terms_of_use_url,
            probe_urls=["https://en.wikipedia.org/wiki/Backpropagation"],
            allowed_scope=["https://en.wikipedia.org/w/api.php?action=query*"],
            required_identification=(
                "Mandatory descriptive User-Agent and Api-User-Agent header with project URL "
                "per Wikimedia Foundation User-Agent Policy"
            ),
            rate_limit_behavior=(
                "Sequential single-worker API requests (no concurrent batching); "
                "in-memory caching; honor HTTP 429/503 Retry-After header"
            ),
            unresolved_questions=[
                "Wikipedia article text is CC BY-SA 4.0 (ShareAlike); pilot stores metadata and revision identifiers only, without article body text."
            ],
            base_restrictions=[
                "Use MediaWiki API (/w/api.php) rather than HTML scraping of /wiki/* pages",
                "Retain canonical article URL, exact revision ID (oldid), and CC BY-SA 4.0 license metadata",
            ],
        )
        return policy

    def discover_candidates(
        self,
        *,
        seed_manifest: list[dict[str, Any]] | None = None,
        limit: int = 2,
    ) -> list[SourceCandidate]:
        """Return up to `limit` curated Wikipedia MediaWiki API candidates."""
        max_items = max(0, int(limit))
        if max_items == 0:
            return []

        candidates: list[SourceCandidate] = []
        for item in self.default_wikipedia_titles[:max_items]:
            title = item["title"]
            candidates.append(
                SourceCandidate(
                    source_id=self.source_id,
                    url=self.build_query_url(title),
                    title_hint=title.replace("_", " "),
                    resource_type_hint="encyclopedia_concept_metadata",
                    manifest_id=None,
                    external_id=f"wikipedia:en:{title}",
                    priority=item.get("priority", "P0"),
                    observed_from=item.get("observed_from", "Curated Wikipedia concept seed"),
                    extra={
                        "page_title": title,
                        "domain_scope": "First-party API",
                        "information_to_inventory": "Page ID, Title, Revision ID, Timestamp, Canonical URL, Wikidata QID, License",
                        "collection_decision": "Fetch MediaWiki API Metadata Only",
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
        """Query the English Wikipedia MediaWiki API for page/revision metadata."""
        if not is_identified_user_agent(self.user_agent):
            raise PolicyBlockedError(
                "Wikimedia User-Agent Policy requires a descriptive User-Agent with contact/project URL",
                source_id=self.source_id,
                url=candidate.url,
            )

        query_url = candidate.url
        if not self.is_url_in_scope(query_url):
            raise ScopeViolationError(
                f"Wikipedia adapter only permits https://en.wikipedia.org/w/api.php?action=query URLs (got {query_url})",
                source_id=self.source_id,
                url=query_url,
            )

        active_policy = policy or self._cached_policy or self.audit_policy()
        if not active_policy.is_metadata_collection_permitted:
            raise PolicyBlockedError(
                f"Wikipedia policy audit did not permit access (status={active_policy.status})",
                source_id=self.source_id,
                url=query_url,
                http_status=active_policy.http_status,
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

        effective_delay = max(self.min_delay_seconds, active_policy.effective_delay_seconds)
        self.rate_limiter.wait(origin_of(query_url), effective_delay)

        req_headers = {
            "User-Agent": self.user_agent,
            "Api-User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if conditional_headers:
            for key in ("If-None-Match", "If-Modified-Since"):
                if conditional_headers.get(key):
                    req_headers[key] = conditional_headers[key]

        response: requests.Response | None = None
        try:
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
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=query_url,
                    http_status=304,
                    outcome="not-modified-304",
                )
                raise NotModifiedResponse(query_url, headers_dict)

            if status_code in {429, 503}:
                retry_after = parse_retry_after(headers_dict.get("Retry-After"))
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=query_url,
                    http_status=status_code,
                    outcome="rate-limited",
                )
                raise RateLimitError(
                    f"Wikipedia API returned HTTP {status_code} rate limit",
                    source_id=self.source_id,
                    url=query_url,
                    http_status=status_code,
                    retry_after_seconds=retry_after,
                )

            if status_code in {401, 403, 451}:
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=query_url,
                    http_status=status_code,
                    outcome="policy-or-access-blocked",
                )
                raise PolicyBlockedError(
                    f"Wikipedia API blocked request with HTTP {status_code}",
                    source_id=self.source_id,
                    url=query_url,
                    http_status=status_code,
                )

            if status_code < 200 or status_code >= 300:
                response.close()
                self._record_http_event(
                    purpose="METADATA_FETCH",
                    url=query_url,
                    http_status=status_code,
                    outcome=f"http-error-{status_code}",
                )
                raise TransientFetchError(
                    f"Wikipedia API returned HTTP {status_code}",
                    source_id=self.source_id,
                    url=query_url,
                    http_status=status_code,
                    is_retryable=status_code >= 500,
                )

            raw_bytes = self._read_limited(response)
            encoding = getattr(response, "encoding", None) or "utf-8"
            body_text = raw_bytes.decode(encoding, errors="replace")
            response.close()

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
                content_type=(headers_dict.get("Content-Type") or "application/json"),
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
                f"Network error querying Wikipedia API: {exc}",
                source_id=self.source_id,
                url=query_url,
                is_retryable=True,
            ) from exc

    def normalize(
        self,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> NormalizedResourceEnvelope:
        """Parse a MediaWiki action=query JSON payload into a NormalizedResourceEnvelope."""
        try:
            data = json.loads(raw.body_text)
        except Exception as exc:
            raise ParseError(
                f"Invalid JSON from Wikipedia API: {exc}",
                source_id=self.source_id,
                url=raw.final_url,
            ) from exc

        if not isinstance(data, dict) or "query" not in data or not isinstance(data["query"], dict):
            raise ParseError(
                f"Wikipedia API response missing 'query' object for {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

        pages_raw = data["query"].get("pages")
        if isinstance(pages_raw, dict):
            pages = list(pages_raw.values())
        elif isinstance(pages_raw, list):
            pages = pages_raw
        else:
            pages = []

        if not pages or not isinstance(pages[0], dict):
            raise ParseError(
                f"Wikipedia API returned no page entries for {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

        page = pages[0]
        if page.get("missing") is True or "missing" in page and page.get("missing") is not False:
            raise ParseError(
                f"Wikipedia page is marked missing in API response for {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

        page_id = page.get("pageid")
        title = str(page.get("title") or "").strip()
        if not title or page_id is None:
            raise ParseError(
                f"Wikipedia API page missing 'title' or 'pageid' for {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

        revisions = page.get("revisions") or []
        latest_rev = revisions[0] if (isinstance(revisions, list) and revisions) else {}
        rev_id = latest_rev.get("revid") or page.get("lastrevid")
        parent_rev_id = latest_rev.get("parentid")
        rev_timestamp = latest_rev.get("timestamp") or page.get("touched")

        slug = quote(title.replace(" ", "_"), safe=":_()-,.!")
        canonical_page_url = canonicalize_url(
            str(page.get("canonicalurl") or page.get("fullurl") or f"https://en.wikipedia.org/wiki/{slug}")
        )
        permanent_revision_url = (
            f"https://en.wikipedia.org/w/index.php?title={slug}&oldid={rev_id}"
            if rev_id is not None
            else canonical_page_url
        )

        pageprops = page.get("pageprops") if isinstance(page.get("pageprops"), dict) else {}
        wikidata_qid = pageprops.get("wikibase_item")
        short_desc = pageprops.get("wikibase-shortdesc")

        raw_categories = page.get("categories") if isinstance(page.get("categories"), list) else []
        categories = [
            str(cat.get("title")).replace("Category:", "").strip()
            for cat in raw_categories
            if isinstance(cat, dict) and cat.get("title")
        ]

        candidate = raw.candidate
        active_policy = policy or self._cached_policy

        completeness = {
            "has_title": bool(title),
            "has_page_id": page_id is not None,
            "has_revision_id": rev_id is not None,
            "has_revision_timestamp": bool(rev_timestamp),
            "has_wikidata_qid": bool(wikidata_qid),
            "has_license": True,
        }

        return NormalizedResourceEnvelope(
            source_id=self.source_id,
            adapter_id=self.adapter_id,
            canonical_url=canonical_page_url,
            fetched_url=raw.final_url,
            title=title,
            resource_type="encyclopedia_concept_metadata",
            manifest_id=(candidate.manifest_id if candidate else None),
            authors="Wikipedia contributors",
            published_at=None,
            updated_at_source=rev_timestamp,
            media_modality="TEXT_METADATA",
            domain_scope="First-party API",
            abstract_or_description=short_desc,
            topic_tags=", ".join(categories[:8]) if categories else "artificial intelligence, machine learning",
            external_identifier=f"wikipedia:en:pageid={page_id};oldid={rev_id}",
            language=str(page.get("pagelanguage") or "en"),
            transcript_reference=None,
            official_links=[canonical_page_url, permanent_revision_url],
            observed_from=(candidate.observed_from if candidate else "MediaWiki Action API"),
            information_to_inventory="Page ID, Title, Revision ID, Timestamp, Canonical URL, Wikidata QID, License",
            priority=(candidate.priority if candidate else "P0"),
            collection_decision="Fetch MediaWiki API Metadata Only",
            verification_status=f"Verified MediaWiki API HTTP {raw.http_status} via {self.adapter_id}",
            robots_sitemap_status=(
                f"{active_policy.status}: {active_policy.evidence_summary}"
                if active_policy
                else "Verified Wikimedia API policy"
            ),
            rights_status=(
                "Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0); "
                f"permanent revision URL: {permanent_revision_url}"
            ),
            license_status="CC_BY_SA_4_0",
            license_spdx="CC-BY-SA-4.0",
            permitted_storage_scope="METADATA_ONLY",
            permits_full_text_storage=True,
            permits_verbatim_excerpts=True,
            legal_uncertainty_status="CLEARED_CC_BY_SA_4_0_ATTRIBUTION_REQUIRED",
            text_storage_status="metadata-only",
            collected_at_utc=raw.fetched_at_utc,
            http_status=raw.http_status,
            etag=raw.headers.get("ETag"),
            last_modified_header=raw.headers.get("Last-Modified"),
            metadata_completeness=completeness,
            source_specific_metadata={
                "page_id": page_id,
                "revision_id": rev_id,
                "parent_revision_id": parent_rev_id,
                "revision_timestamp": rev_timestamp,
                "permanent_revision_url": permanent_revision_url,
                "wikidata_qid": wikidata_qid,
                "categories": categories,
                "attribution_license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            discovered_links=[],
        )
