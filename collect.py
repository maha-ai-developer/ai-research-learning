#!/usr/bin/env python3
"""Conservative metadata-only collector for the Hinton/Bengio source inventory.

Default mode is a dry run. Use --fetch only after reviewing the manifest and policy.
The collector does not save full page text, download PDFs, or follow external links.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from knowledge_store import (
    KnowledgeStore,
    apply_migrations,
    format_import_summary,
    import_seed_manifest,
)

DEFAULT_USER_AGENT = "AIResearchLearningMetadataBot/0.1 (metadata-only; configure a contact URL before production)"
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3

# Scope is intentionally explicit. Do not broaden this to every page on either host.
APPROVED_HOST_PATHS: dict[str, tuple[str, ...]] = {
    "www.cs.toronto.edu": ("/~hinton",),
    "yoshuabengio.org": ("/en",),
}
NON_HTML_EXTENSIONS = {
    ".pdf", ".zip", ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", ".mp4", ".mp3", ".wav", ".ppt", ".pptx", ".doc", ".docx",
}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def canonicalize_url(url: str) -> str:
    """Normalize only transport-level differences; preserve path and query semantics."""
    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError(f"Not an absolute HTTP(S) URL: {url!r}")
    port = p.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = p.path or "/"
    return urlunsplit((scheme, netloc, path, p.query, ""))


def is_approved_site_url(url: str, allowed_hosts: set[str] | None = None) -> bool:
    try:
        u = canonicalize_url(url)
    except ValueError:
        return False
    p = urlsplit(u)
    host = (p.hostname or "").lower()
    if allowed_hosts is not None and host not in allowed_hosts:
        return False
    allowed_prefixes = APPROVED_HOST_PATHS.get(host)
    if not allowed_prefixes:
        return False
    path = p.path.rstrip("/") or "/"
    return any(path == prefix or path.startswith(prefix + "/") for prefix in allowed_prefixes)


def _meta_content(soup: BeautifulSoup, keys: Iterable[str]) -> str | None:
    wanted = {key.casefold() for key in keys}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").strip().casefold()
        if key in wanted:
            content = " ".join((tag.get("content") or "").split())
            if content:
                return content
    return None


def _jsonld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text()
        try:
            visit(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def _first_jsonld(objects: list[dict[str, Any]], key: str) -> Any:
    for obj in objects:
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
    return None


def _flatten_author(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name") or value.get("givenName")
        return str(name).strip() if name else None
    if isinstance(value, list):
        names = [name for item in value if (name := _flatten_author(item))]
        return "; ".join(dict.fromkeys(names)) if names else None
    return None


def _split_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    # Use semicolons for field values where possible; accept comma-separated HTML keywords.
    parts = re.split(r"\s*[;,]\s*", value)
    return list(dict.fromkeys(part.strip() for part in parts if part.strip()))[:30]


def _same_host_canonical(candidate: str | None, fetched_url: str) -> str:
    if not candidate:
        return canonicalize_url(fetched_url)
    try:
        normalized = canonicalize_url(urljoin(fetched_url, candidate))
    except ValueError:
        return canonicalize_url(fetched_url)
    # Only accept canonical tags on the same host. Never canonicalize across owners.
    if host_of(normalized) != host_of(fetched_url):
        return canonicalize_url(fetched_url)
    return normalized


def _external_identifier(url: str, soup: BeautifulSoup) -> str | None:
    doi = _meta_content(soup, ["citation_doi", "dc.identifier", "DC.identifier", "doi"])
    if doi:
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        return f"doi:{doi}"
    for candidate in [url, _meta_content(soup, ["citation_arxiv_id", "arxiv"] ) or ""]:
        match = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)(\d{4}\.\d{4,5}(?:v\d+)?)", candidate, re.I)
        if match:
            return f"arxiv:{match.group(1)}"
    return None


def parse_metadata(html: str, fetched_url: str, manifest_entry: dict[str, Any]) -> dict[str, Any]:
    """Extract metadata only; does not store article body or full page text."""
    soup = BeautifulSoup(html, "html.parser")
    jsonld = _jsonld_objects(soup)

    title = (
        _meta_content(soup, ["og:title", "twitter:title", "citation_title", "DC.title", "dc.title"])
        or _first_jsonld(jsonld, "headline")
        or _first_jsonld(jsonld, "name")
        or (soup.title.get_text(" ", strip=True) if soup.title else None)
    )
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    title = " ".join(title.split()) if title else str(manifest_entry.get("Page / resource name", "Untitled resource"))

    author = (
        _meta_content(soup, ["citation_author", "author", "article:author", "dc.creator", "DC.creator"])
        or _flatten_author(_first_jsonld(jsonld, "author"))
    )
    published = (
        _meta_content(soup, ["article:published_time", "datePublished", "citation_publication_date", "publication_date", "dc.date", "DC.date", "date"])
        or _first_jsonld(jsonld, "datePublished")
        or _first_jsonld(jsonld, "dateCreated")
    )
    if isinstance(published, dict):
        published = None
    if published:
        published = str(published).strip()

    description = (
        _meta_content(soup, ["og:description", "description", "twitter:description", "citation_abstract", "dc.description", "DC.description"])
        or _first_jsonld(jsonld, "description")
    )
    if description:
        description = " ".join(str(description).split())[:1200]

    keywords_value = _meta_content(soup, ["keywords", "citation_keywords", "article:tag"])
    keywords = _split_keywords(keywords_value)
    canonical_tag = soup.find("link", rel=lambda x: x and "canonical" in [s.casefold() for s in (x if isinstance(x, list) else str(x).split())])
    canonical = _same_host_canonical(canonical_tag.get("href") if canonical_tag else None, fetched_url)
    media_type = _first_jsonld(jsonld, "@type")
    if isinstance(media_type, list):
        media_type = "; ".join(map(str, media_type))
    content_type = str(manifest_entry.get("Resource type", "web_page"))
    if media_type:
        content_type = f"{content_type} | schema:{media_type}"

    metadata_for_hash = {
        "canonical_url": canonical,
        "title": title,
        "authors": author,
        "published_at": published,
        "content_type": content_type,
        "topic_tags": keywords,
        "description": description,
        "external_identifier": _external_identifier(fetched_url, soup),
    }
    metadata_hash = hashlib.sha256(json.dumps(metadata_for_hash, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        **metadata_for_hash,
        "abstract_or_description": description,
        "topic_tags": "; ".join(keywords) if keywords else None,
        "metadata_hash": metadata_hash,
    }


def extract_links(html: str, base_url: str) -> list[dict[str, str | None]]:
    """Enumerate anchors only. This function never fetches linked URLs."""
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            absolute = canonicalize_url(urljoin(base_url, href))
        except ValueError:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        label = " ".join(anchor.get_text(" ", strip=True).split())[:300] or None
        rel_val = anchor.get("rel")
        rel = " ".join(rel_val) if isinstance(rel_val, list) else (str(rel_val) if rel_val else None)
        host = host_of(absolute)
        if is_approved_site_url(absolute):
            scope, decision = "first-party-in-scope", "candidate-not-fetched"
        elif host in APPROVED_HOST_PATHS:
            scope, decision = "same-host-out-of-scope", "record-only-review"
        else:
            scope, decision = "external", "record-only-review"
        result.append({"link_url": absolute, "link_text": label, "rel": rel, "target_scope": scope, "decision": decision})
        if len(result) >= 1000:
            break
    return result


@dataclass
class Policy:
    origin: str
    status: str
    parser: urllib.robotparser.RobotFileParser | None
    delay_seconds: float
    robots_url: str
    sitemap_urls: list[str]
    http_status: int | None = None
    evidence: str = ""

    def can_fetch(self, user_agent: str, url: str) -> bool:
        return self.status == "ok" and bool(self.parser and self.parser.can_fetch(user_agent, url))


class ConservativeCollector:
    def __init__(
        self,
        db_path: Path,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        min_delay: float = DEFAULT_DELAY_SECONDS,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.min_delay = max(1.0, float(min_delay))
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self.last_request_at: dict[str, float] = {}
        self.policies: dict[str, Policy] = {}
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()
        self.store = KnowledgeStore(conn=self.db, auto_initialize=False)

    def close(self) -> None:
        self.db.close()

    def _ensure_schema(self) -> None:
        apply_migrations(self.db)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.commit()

    def _wait(self, origin: str, delay: float | None = None) -> None:
        now = time.monotonic()
        wait_for = (self.last_request_at.get(origin, now) + max(self.min_delay, delay or 0.0)) - now
        if wait_for > 0:
            self.sleep_fn(wait_for)
        self.last_request_at[origin] = time.monotonic()

    def _raw_request(self, url: str, *, stream: bool = True) -> requests.Response:
        origin = origin_of(url)
        self._wait(origin, self.min_delay)
        return self.session.get(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.2"},
            timeout=self.timeout,
            allow_redirects=False,
            stream=stream,
        )

    def load_policy(self, origin: str) -> Policy:
        if origin in self.policies:
            return self.policies[origin]
        host = host_of(origin)
        robots_url = origin.rstrip("/") + "/robots.txt"
        parser = urllib.robotparser.RobotFileParser(robots_url)
        now = utc_now()
        try:
            response = self._raw_request(robots_url, stream=True)
            status_code = response.status_code
            if status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("robots.txt redirected without a Location header")
                redirected = urljoin(robots_url, location)
                if host_of(redirected) != host:
                    raise RuntimeError("robots.txt redirect changed host; refusing to infer permission")
                response = self._raw_request(redirected, stream=True)
                status_code = response.status_code
            if 200 <= status_code < 300:
                raw = self._read_limited(response)
                text = raw.decode(response.encoding or "utf-8", errors="replace")
                parser.set_url(robots_url)
                parser.parse(text.splitlines())
                delay = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*") or 0
                effective_delay = max(self.min_delay, float(delay or 0))
                sitemaps = [
                    line.split(":", 1)[1].strip()
                    for line in text.splitlines()
                    if line.lower().startswith("sitemap:") and ":" in line
                ]
                policy = Policy(origin, "ok", parser, effective_delay, robots_url, sitemaps, status_code, "robots.txt fetched and parsed")
            elif status_code in {404, 410}:
                # RFC 9309's unavailable 4xx policy permits access; still use conservative delay.
                parser.parse([])
                policy = Policy(origin, "ok", parser, self.min_delay, robots_url, [], status_code, f"robots.txt returned HTTP {status_code}; conservative delay retained")
            else:
                policy = Policy(origin, "blocked", None, self.min_delay, robots_url, [], status_code, f"robots.txt returned HTTP {status_code}; fail-closed")
            response.close()
        except Exception as exc:
            policy = Policy(origin, "unverified", None, self.min_delay, robots_url, [], None, f"robots check failed: {type(exc).__name__}: {exc}")

        self.policies[origin] = policy
        self.db.execute(
            "INSERT INTO site_policies(origin,robots_url,status,http_status,crawl_delay_seconds,sitemap_urls,evidence,checked_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(origin) DO UPDATE SET "
            "robots_url=excluded.robots_url,status=excluded.status,http_status=excluded.http_status,"
            "crawl_delay_seconds=excluded.crawl_delay_seconds,sitemap_urls=excluded.sitemap_urls,"
            "evidence=excluded.evidence,checked_at=excluded.checked_at",
            (origin, robots_url, policy.status, policy.http_status, policy.delay_seconds,
             json.dumps(policy.sitemap_urls), policy.evidence, now),
        )
        try:
            self.db.execute(
                "INSERT INTO crawl_audit(endpoint_url,check_name,status,evidence,next_action,checked_at) VALUES(?,?,?,?,?,?)",
                (robots_url, "robots.txt live policy check", policy.status, policy.evidence,
                 "Review crawl rules before collecting; fail closed when status is unverified or blocked", now),
            )
        except sqlite3.OperationalError:
            # The collector can still work with a database that has not yet got crawl_audit.
            pass
        self.db.commit()
        return policy

    def _read_limited(self, response: requests.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        if getattr(response, "headers", {}).get("Content-Length"):
            try:
                if int(response.headers["Content-Length"]) > self.max_bytes:
                    raise ValueError(f"response exceeds {self.max_bytes} byte limit")
            except ValueError as exc:
                if "response exceeds" in str(exc):
                    raise
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > self.max_bytes:
                raise ValueError(f"response exceeds {self.max_bytes} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _get_html(self, requested_url: str) -> tuple[str, str, str, int]:
        """Return (final URL, HTML, content type, status); redirects are host/scope checked."""
        current = canonicalize_url(requested_url)
        for _ in range(MAX_REDIRECTS + 1):
            if not is_approved_site_url(current):
                raise PermissionError(f"URL outside approved site/path scope: {current}")
            policy = self.load_policy(origin_of(current))
            if not policy.can_fetch(self.user_agent, current):
                raise PermissionError(f"robots policy does not permit fetch: {current} ({policy.status})")
            self._wait(origin_of(current), policy.delay_seconds)
            response = self.session.get(
                current,
                headers={"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            status = response.status_code
            if status in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("redirect without Location header")
                nxt = canonicalize_url(urljoin(current, location))
                if not is_approved_site_url(nxt):
                    raise PermissionError(f"redirect leaves approved first-party scope: {nxt}")
                current = nxt
                continue
            if status < 200 or status >= 300:
                response.close()
                raise RuntimeError(f"HTTP {status}")
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                response.close()
                raise TypeError(f"non-HTML response ({content_type or 'unknown content type'}); not downloaded")
            raw = self._read_limited(response)
            charset = response.encoding or "utf-8"
            html = raw.decode(charset, errors="replace")
            response.close()
            return current, html, content_type, status
        raise RuntimeError("too many redirects")

    def _record_fetch_event(self, run_id: str, manifest_id: str, url: str, final_url: str | None,
                            status: str, http_status: int | None, error: str | None) -> None:
        self.db.execute(
            "INSERT INTO fetch_events(run_id,manifest_id,requested_url,final_url,status,http_status,error,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, manifest_id, url, final_url, status, http_status, error, utc_now()),
        )
        self.db.execute(
            "UPDATE source_urls SET last_fetch_status=?, last_fetch_error=?, last_fetch_at=? WHERE manifest_id=?",
            (status, error, utc_now(), manifest_id),
        )

    def _save_document(self, manifest_id: str, metadata: dict[str, Any]) -> None:
        fetched_at = utc_now()
        source_id = self.store.resolve_or_register_source(
            person_source=None,
            canonical_url=metadata["canonical_url"],
            manifest_id=manifest_id,
        )
        res = self.store.upsert_resource(
            source_id=source_id,
            manifest_id=manifest_id,
            canonical_url=metadata["canonical_url"],
            title=metadata.get("title") or metadata["canonical_url"],
            resource_type=metadata.get("content_type") or "web_page",
            authors=metadata.get("authors"),
            published_at=metadata.get("published_at"),
            abstract_or_description=metadata.get("abstract_or_description"),
            topic_tags=metadata.get("topic_tags"),
            external_identifier=metadata.get("external_identifier"),
            text_storage_status="metadata-only",
            discovered_at=fetched_at,
            acquisition_method="CONSERVATIVE_HTML_COLLECTOR",
            actor=self.user_agent,
            change_reason="html_metadata_collected",
        )
        self.db.execute(
            "INSERT INTO documents(manifest_id,canonical_url,title,authors,published_at,content_type,topic_tags,"
            "abstract_or_description,external_identifier,license_status,text_storage_status,fetched_at,content_hash,metadata_hash,resource_id,version_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(canonical_url) DO UPDATE SET "
            "title=COALESCE(excluded.title,documents.title),authors=COALESCE(excluded.authors,documents.authors),"
            "published_at=COALESCE(excluded.published_at,documents.published_at),"
            "content_type=COALESCE(excluded.content_type,documents.content_type),"
            "topic_tags=COALESCE(excluded.topic_tags,documents.topic_tags),"
            "abstract_or_description=COALESCE(excluded.abstract_or_description,documents.abstract_or_description),"
            "external_identifier=COALESCE(excluded.external_identifier,documents.external_identifier),"
            "text_storage_status='metadata-only',fetched_at=excluded.fetched_at,metadata_hash=excluded.metadata_hash,"
            "resource_id=excluded.resource_id,version_id=excluded.version_id",
            (manifest_id, metadata["canonical_url"], metadata.get("title"), metadata.get("authors"),
             metadata.get("published_at"), metadata.get("content_type"), metadata.get("topic_tags"),
             metadata.get("abstract_or_description"), metadata.get("external_identifier"),
             "not_checked", "metadata-only", fetched_at, None, metadata.get("metadata_hash"),
             res["resource_id"], res["current_version_id"]),
        )

    def _save_discovered_links(self, manifest_id: str, links: list[dict[str, str | None]]) -> int:
        count_before = self.db.total_changes
        stamp = utc_now()
        for link in links:
            self.db.execute(
                "INSERT OR IGNORE INTO discovered_links(from_manifest_id,link_url,link_text,rel,target_scope,decision,discovered_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (manifest_id, link["link_url"], link["link_text"], link["rel"], link["target_scope"], link["decision"], stamp),
            )
        return self.db.total_changes - count_before

    def audit_policy_only(self, urls: list[str]) -> dict[str, Any]:
        origins = sorted({origin_of(u) for u in urls if host_of(u) in APPROVED_HOST_PATHS})
        checked: list[dict[str, Any]] = []
        for origin in origins:
            policy = self.load_policy(origin)
            sitemap_candidates = list(dict.fromkeys(policy.sitemap_urls + [
                origin + "/sitemap.xml", origin + "/wp-sitemap.xml", origin + "/sitemap_index.xml"
            ]))
            sitemap_results = []
            for sitemap_url in sitemap_candidates:
                try:
                    if host_of(sitemap_url) != host_of(origin):
                        sitemap_results.append({"url": sitemap_url, "status": "skipped-cross-host"})
                        continue
                    if policy.status != "ok" or not policy.can_fetch(self.user_agent, sitemap_url):
                        sitemap_results.append({"url": sitemap_url, "status": "not-checked-policy-blocked"})
                        continue
                    self._wait(origin, policy.delay_seconds)
                    response = self.session.get(
                        sitemap_url,
                        headers={"User-Agent": self.user_agent, "Accept": "application/xml,text/xml,*/*;q=0.1"},
                        timeout=self.timeout,
                        allow_redirects=False,
                        stream=True,
                    )
                    code = response.status_code
                    ct = response.headers.get("Content-Type", "")
                    response.close()
                    outcome = "available" if 200 <= code < 300 else ("not-found" if code in {404, 410} else "http-error")
                    sitemap_results.append({"url": sitemap_url, "status": outcome, "http_status": code, "content_type": ct})
                except Exception as exc:
                    sitemap_results.append({"url": sitemap_url, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
                self.db.execute(
                    "INSERT INTO crawl_audit(endpoint_url,check_name,status,evidence,next_action,checked_at) VALUES(?,?,?,?,?,?)",
                    (sitemap_url, "sitemap endpoint check", sitemap_results[-1]["status"],
                     json.dumps(sitemap_results[-1], ensure_ascii=False),
                     "Use only sitemap URLs confirmed available and allowed by robots rules", utc_now()),
                )
            checked.append({"origin": origin, "robots_status": policy.status, "robots_evidence": policy.evidence,
                            "crawl_delay_seconds": policy.delay_seconds, "sitemaps": sitemap_results})
        self.db.commit()
        return {"checked_origins": checked}


def normalize_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_id": row.get("Manifest ID") or row.get("manifest_id"),
        "person_source": row.get("Person / source") or row.get("person_source") or "Unknown source",
        "url": row.get("URL") or row.get("url"),
        "page_resource_name": row.get("Page / resource name") or row.get("page_resource_name") or "Untitled resource",
        "resource_type": row.get("Resource type") or row.get("resource_type") or "web_page",
        "domain_scope": row.get("Domain scope") or row.get("domain_scope") or "",
        "verification_status": row.get("Verification status") or row.get("verification_status") or "Unverified",
        "priority": row.get("Priority") or row.get("priority") or "P3",
        "collection_decision": row.get("Collection decision") or row.get("collection_decision") or "Review required",
    }


def is_binary_url(url: str) -> bool:
    return Path(urlsplit(url).path.lower()).suffix in NON_HTML_EXTENSIONS


def eligible_rows(raw_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    eligible: list[dict[str, Any]] = []
    excluded: list[tuple[dict[str, Any], str]] = []
    for raw in raw_rows:
        row = normalize_manifest_row(raw)
        url = row["url"]
        if not url:
            excluded.append((row, "missing URL"))
            continue
        scope = row["domain_scope"].casefold()
        decision = row["collection_decision"].casefold()
        if "control endpoint" in scope or "compliance endpoint" in row["resource_type"].casefold():
            excluded.append((row, "policy endpoint; checked separately"))
        elif "external" in scope or "record only" in decision or "cross-owner" in scope:
            excluded.append((row, "external or record-only reference"))
        elif not is_approved_site_url(url):
            excluded.append((row, "outside exact approved host/path scope"))
        elif is_binary_url(url):
            excluded.append((row, "binary resource; no PDF/media download in metadata phase"))
        else:
            eligible.append(row)
    return eligible, excluded


def balanced_select(rows: list[dict[str, Any]], total_limit: int, per_source_limit: int) -> list[dict[str, Any]]:
    def group(row: dict[str, Any]) -> str:
        return "Geoffrey Hinton" if "hinton" in row["person_source"].casefold() or "university of toronto" in row["person_source"].casefold() else "Yoshua Bengio"
    def priority(row: dict[str, Any]) -> tuple[int, str]:
        p = row["priority"].upper()
        return (int(p[1:]) if p.startswith("P") and p[1:].isdigit() else 9, row["manifest_id"] or "")
    groups: dict[str, list[dict[str, Any]]] = {"Geoffrey Hinton": [], "Yoshua Bengio": []}
    for row in rows:
        groups.setdefault(group(row), []).append(row)
    for values in groups.values():
        values.sort(key=priority)
    chosen: list[dict[str, Any]] = []
    per_source = {key: 0 for key in groups}
    while len(chosen) < max(total_limit, 0):
        advanced = False
        for key in ("Geoffrey Hinton", "Yoshua Bengio"):
            if per_source.get(key, 0) >= per_source_limit:
                continue
            if groups.get(key):
                chosen.append(groups[key].pop(0))
                per_source[key] = per_source.get(key, 0) + 1
                advanced = True
                if len(chosen) >= total_limit:
                    break
        if not advanced:
            break
    return chosen


def sync_manifest(db: sqlite3.Connection, raw_rows: list[dict[str, Any]]) -> None:
    for raw in raw_rows:
        row = normalize_manifest_row(raw)
        if not row["manifest_id"] or not row["url"]:
            continue
        try:
            url = canonicalize_url(row["url"])
        except ValueError:
            continue
        values = (
            row["manifest_id"], row["person_source"], url, row["page_resource_name"], row["resource_type"],
            row["domain_scope"], row["verification_status"], raw.get("Observed from") or raw.get("observed_from"),
            raw.get("Information to inventory") or raw.get("information_to_inventory"), row["priority"],
            row["collection_decision"], raw.get("Robots / sitemap status") or raw.get("robots_sitemap_status"),
            raw.get("Rights status") or raw.get("rights_status"), raw.get("Last checked") or raw.get("last_checked"),
        )
        db.execute(
            "INSERT INTO source_urls(manifest_id,person_source,url,page_resource_name,resource_type,domain_scope,verification_status,"
            "observed_from,information_to_inventory,priority,collection_decision,robots_sitemap_status,rights_status,last_checked) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(manifest_id) DO UPDATE SET "
            "person_source=excluded.person_source,url=excluded.url,page_resource_name=excluded.page_resource_name,"
            "resource_type=excluded.resource_type,domain_scope=excluded.domain_scope,verification_status=excluded.verification_status,"
            "observed_from=excluded.observed_from,information_to_inventory=excluded.information_to_inventory,priority=excluded.priority,"
            "collection_decision=excluded.collection_decision,robots_sitemap_status=excluded.robots_sitemap_status,"
            "rights_status=excluded.rights_status,last_checked=excluded.last_checked",
            values,
        )
    db.commit()


def export_discoveries(db_path: Path, out_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT from_manifest_id,link_url,link_text,rel,target_scope,decision,discovered_at FROM discovered_links ORDER BY from_manifest_id,link_url"
    ).fetchall()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["from_manifest_id", "link_url", "link_text", "rel", "target_scope", "decision", "discovered_at"])
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    con.close()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metadata-only source collector. Default mode is a no-network dry run.")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("seed_urls.json"), help="JSON URL manifest")
    parser.add_argument("--db", type=Path, default=Path(__file__).parent / "data" / "runtime" / "inventory.sqlite", help="SQLite runtime inventory database (created if absent)")
    parser.add_argument("--limit", type=int, default=10, help="Maximum HTML pages to select in one run (default: 10)")
    parser.add_argument("--per-source-limit", type=int, default=5, help="Maximum pages per researcher in one run (default: 5)")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Identify the collector; set a real contact URL before production")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="Maximum downloaded response size per HTML page")
    parser.add_argument("--min-delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Minimum per-origin delay in seconds; never below 1 second")
    parser.add_argument("--fetch", action="store_true", help="Enable network collection; without this flag the command is a dry run")
    parser.add_argument("--policy-only", action="store_true", help="Check robots.txt and sitemap endpoint availability, then exit")
    parser.add_argument("--init-db", action="store_true", help="Initialize or migrate the local SQLite database offline and exit")
    parser.add_argument("--import-manifest", action="store_true", help="Import seed_urls.json into the local SQLite database offline and exit")
    parser.add_argument("--discoveries-csv", type=Path, default=None, help="Path for the discovered-link review CSV")
    args = parser.parse_args(argv)

    if args.init_db:
        with KnowledgeStore(db_path=args.db, auto_initialize=True) as store:
            print(f"Initialized local knowledge store at {args.db} (schema version {store.get_schema_version()}).")
        if not args.import_manifest and not args.fetch:
            return 0

    if args.import_manifest:
        if not args.manifest.exists():
            print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
            return 2
        summary = import_seed_manifest(db_path=args.db, manifest_path=args.manifest)
        print(format_import_summary(summary))
        if not args.fetch:
            return 0 if summary.rejected_count == 0 else 1

    if not args.manifest.exists():
        print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    try:
        raw_rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: failed to parse manifest: {exc}", file=sys.stderr)
        return 2
    if not isinstance(raw_rows, list):
        print("ERROR: manifest must contain a JSON list of records", file=sys.stderr)
        return 2

    eligible, excluded = eligible_rows(raw_rows)
    chosen = balanced_select(eligible, args.limit, args.per_source_limit)
    print("AI Research Metadata Collector")
    print(f"Manifest records: {len(raw_rows)}")
    print(f"Eligible HTML seed URLs: {len(eligible)}")
    print(f"Excluded from fetch: {len(excluded)}")
    print(f"Selected this run: {len(chosen)} (max {args.limit}; max {args.per_source_limit} per researcher)")
    for row in chosen:
        print(f"  PLAN  {row['manifest_id']:<12} {row['person_source']:<18} {row['url']}")
    if excluded:
        print("Excluded examples:")
        for row, reason in excluded[:8]:
            print(f"  SKIP  {row['manifest_id'] or 'NO-ID':<12} {reason}: {row['url']}")

    if not args.fetch:
        print("\nDRY RUN ONLY: no network requests made and no document records collected.")
        print("After reviewing the plan, use --policy-only first, then --fetch for collection.")
        return 0
    if "configure a contact url before production" in args.user_agent.casefold():
        print("ERROR: before any live request, supply --user-agent with your project name and a real contact URL.", file=sys.stderr)
        return 2

    collector = ConservativeCollector(args.db, user_agent=args.user_agent, timeout=args.timeout,
                                      max_bytes=args.max_bytes, min_delay=args.min_delay)
    try:
        sync_manifest(collector.db, raw_rows)
        selected_urls = [row["url"] for row in chosen]
        if args.policy_only:
            policy_report = collector.audit_policy_only([row["url"] for row in eligible])
            print(json.dumps(policy_report, indent=2, ensure_ascii=False))
            print("Policy-only run complete; no content pages were fetched.")
            return 0

        run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:6]
        started = utc_now()
        collector.db.execute(
            "INSERT INTO crawl_runs(run_id,started_at,manifest_path,selected_count,status,notes) VALUES(?,?,?,?,?,?)",
            (run_id, started, str(args.manifest.resolve()), len(chosen), "running", "Metadata-only; HTML only; robots enforced; one-second minimum per origin"),
        )
        collector.db.commit()
        fetched = discovered_total = skipped = errors = 0
        for row in chosen:
            manifest_id, url = row["manifest_id"], row["url"]
            try:
                final_url, html, content_type, status_code = collector._get_html(url)
                metadata = parse_metadata(html, final_url, row)
                collector._save_document(manifest_id, metadata)
                links = extract_links(html, final_url)
                discovered_total += collector._save_discovered_links(manifest_id, links)
                collector._record_fetch_event(run_id, manifest_id, url, final_url, "fetched-metadata-only", status_code, None)
                fetched += 1
                print(f"FETCHED {manifest_id}: {metadata.get('title')}")
            except TypeError as exc:
                skipped += 1
                collector._record_fetch_event(run_id, manifest_id, url, None, "skipped-non-html", None, str(exc))
                print(f"SKIPPED {manifest_id}: {exc}")
            except PermissionError as exc:
                skipped += 1
                collector._record_fetch_event(run_id, manifest_id, url, None, "blocked-by-policy-or-scope", None, str(exc))
                print(f"BLOCKED {manifest_id}: {exc}")
            except Exception as exc:
                errors += 1
                collector._record_fetch_event(run_id, manifest_id, url, None, "error", None, f"{type(exc).__name__}: {exc}")
                print(f"ERROR {manifest_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            collector.db.commit()
        final_status = "completed" if errors == 0 else "completed-with-errors"
        collector.db.execute(
            "UPDATE crawl_runs SET finished_at=?,fetched_count=?,discovered_count=?,skipped_count=?,error_count=?,status=? WHERE run_id=?",
            (utc_now(), fetched, discovered_total, skipped, errors, final_status, run_id),
        )
        collector.db.commit()
        out_csv = args.discoveries_csv or args.db.with_name("discovered_links_pending_review.csv")
        exported = export_discoveries(args.db, out_csv)
        print("\nRun summary:")
        print(f"  Run ID: {run_id}")
        print(f"  Metadata records fetched: {fetched}")
        print(f"  Newly discovered links recorded: {discovered_total}")
        print(f"  Skipped: {skipped}; errors: {errors}")
        print(f"  Discovery review CSV: {out_csv} ({exported} total rows)")
        print("  Full page text and binary files were not stored or downloaded.")
        return 1 if errors else 0
    finally:
        collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
