"""arXiv official Atom API metadata adapter (SRC-ARXIV).

Follows docs/architecture/source-adapter-contract.md Section 5.4:
- Uses https://export.arxiv.org/api/query (never HTML scraping or PDF downloading)
- Enforces single-connection sequential requests with >= 3.0s delay
- Parses Atom XML entries into metadata + abstract envelopes
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET

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

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)


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
    min_delay_seconds = 3.0

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

    def build_query_url(self, arxiv_id: str) -> str:
        clean_id = extract_arxiv_id(arxiv_id)
        query = urlencode({"id_list": clean_id, "max_results": 1})
        return f"{self.api_endpoint}?{query}"

    def is_url_in_scope(self, url: str) -> bool:
        try:
            normalized = canonicalize_url(url)
        except ValueError:
            return False
        if is_binary_asset_url(normalized) or "/pdf/" in normalized.lower():
            return False
        parsed = urlsplit(normalized)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == "export.arxiv.org"
            and parsed.path == "/api/query"
        )

    def audit_policy(self, *, force_refresh: bool = False) -> PolicyAuditResult:
        """Audit export.arxiv.org robots.txt and API terms before querying."""
        if self._cached_policy is not None and not force_refresh:
            return self._cached_policy
        probe_url = self.build_query_url("1207.0580")
        return self._fetch_robots_policy(
            robots_url=self.robots_url,
            terms_of_use_url=self.terms_of_use_url,
            probe_urls=[probe_url],
            allowed_scope=["https://export.arxiv.org/api/query?*"],
            required_identification=(
                "Descriptive User-Agent header with project URL; single-connection sequential requests"
            ),
            rate_limit_behavior=(
                "Mandatory >= 3.0s delay between sequential requests per arXiv API Terms of Use; "
                "abort on HTTP 429/503"
            ),
            unresolved_questions=[
                "Full PDF and TeX source bundles have per-submission licenses (CC BY, CC BY-SA, or arXiv non-exclusive license); pilot stores metadata and abstract only, never PDFs."
            ],
            base_restrictions=[
                "Use official Atom API (https://export.arxiv.org/api/query) only; do not scrape HTML or download PDFs",
                "Enforce >= 3.0s minimum delay between requests on a single connection",
            ],
        )

    def discover_candidates(
        self,
        *,
        seed_manifest: list[dict[str, Any]] | None = None,
        limit: int = 2,
    ) -> list[SourceCandidate]:
        """Return up to `limit` curated arXiv API query candidates."""
        max_items = max(0, int(limit))
        if max_items == 0:
            return []

        candidates: list[SourceCandidate] = []
        for item in self.default_arxiv_candidates[:max_items]:
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

        effective_delay = max(3.0, self.min_delay_seconds, active_policy.effective_delay_seconds)
        self.rate_limiter.wait(origin_of(query_url), effective_delay)

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
                    f"arXiv API returned HTTP {status_code} rate limit",
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
                    f"arXiv API blocked request with HTTP {status_code}",
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

    def normalize(
        self,
        raw: RawMetadataPayload,
        *,
        policy: PolicyAuditResult | None = None,
    ) -> NormalizedResourceEnvelope:
        """Parse an arXiv Atom XML payload into a NormalizedResourceEnvelope."""
        try:
            root = ET.fromstring(raw.body_text)
        except ET.ParseError as exc:
            raise ParseError(
                f"Invalid Atom XML from arXiv API: {exc}",
                source_id=self.source_id,
                url=raw.final_url,
            ) from exc

        entry = root.find("atom:entry", ATOM_NS)
        if entry is None:
            raise ParseError(
                f"arXiv Atom feed contained no <entry> elements for {raw.final_url}",
                source_id=self.source_id,
                url=raw.final_url,
            )

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
            observed_from=(candidate.observed_from if candidate else "arXiv Atom API"),
            information_to_inventory="arXiv ID, Title, Authors, Abstract, Categories, Published/Updated Dates",
            priority=(candidate.priority if candidate else "P0"),
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
            },
            discovered_links=[],
        )
