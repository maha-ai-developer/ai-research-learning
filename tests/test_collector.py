import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collect import (
    canonicalize_url,
    eligible_rows,
    extract_links,
    is_approved_site_url,
    parse_metadata,
    ConservativeCollector,
    sync_manifest,
)


class CollectorPureFunctionTests(unittest.TestCase):
    def test_canonicalize_strips_fragment_preserves_query(self):
        self.assertEqual(
            canonicalize_url("HTTPS://YoshuaBengio.org/en/blog?page=1#section"),
            "https://yoshuabengio.org/en/blog?page=1",
        )

    def test_scope_is_limited_by_host_and_path(self):
        self.assertTrue(is_approved_site_url("https://www.cs.toronto.edu/~hinton/pages/publications.html"))
        self.assertTrue(is_approved_site_url("https://yoshuabengio.org/en/blog?page=1"))
        self.assertFalse(is_approved_site_url("https://www.cs.toronto.edu/~tijmen/csc321/"))
        self.assertFalse(is_approved_site_url("https://yoshuabengio.org/wp-admin/"))
        self.assertFalse(is_approved_site_url("https://example.com/en"))

    def test_manifest_excludes_controls_external_and_pdf(self):
        rows = [
            {"Manifest ID": "1", "Person / source": "Geoffrey Hinton", "URL": "https://www.cs.toronto.edu/~hinton/index.html", "Domain scope": "First-party", "Collection decision": "metadata-first", "Resource type": "Discovery index"},
            {"Manifest ID": "2", "Person / source": "Yoshua Bengio", "URL": "https://yoshuabengio.org/robots.txt", "Domain scope": "Control endpoint", "Collection decision": "Check policy", "Resource type": "Compliance endpoint"},
            {"Manifest ID": "3", "Person / source": "Yoshua Bengio", "URL": "https://arxiv.org/abs/1234.56789", "Domain scope": "External — record only", "Collection decision": "record only", "Resource type": "External research record"},
            {"Manifest ID": "4", "Person / source": "Geoffrey Hinton", "URL": "https://www.cs.toronto.edu/~hinton/shortcv.pdf", "Domain scope": "First-party", "Collection decision": "metadata-first", "Resource type": "PDF / profile record"},
        ]
        eligible, excluded = eligible_rows(rows)
        self.assertEqual([row["manifest_id"] for row in eligible], ["1"])
        self.assertEqual(len(excluded), 3)

    def test_parse_metadata_uses_tags_and_does_not_capture_body(self):
        html = '''<!doctype html><html><head>
          <title>Fallback title</title>
          <meta property="og:title" content="A Research Paper" />
          <meta name="author" content="Ada Researcher" />
          <meta property="article:published_time" content="2025-03-12T10:00:00Z" />
          <meta name="description" content="A concise abstract summary." />
          <meta name="keywords" content="deep learning, representation learning" />
          <link rel="canonical" href="https://yoshuabengio.org/en/publication/example" />
          </head><body><h1>Different body title</h1><p>Full body should not be saved.</p></body></html>'''
        metadata = parse_metadata(html, "https://yoshuabengio.org/en/publication/example?utm=1", {
            "Page / resource name": "Seed title", "Resource type": "Publication detail page"
        })
        self.assertEqual(metadata["title"], "A Research Paper")
        self.assertEqual(metadata["authors"], "Ada Researcher")
        self.assertEqual(metadata["published_at"], "2025-03-12T10:00:00Z")
        self.assertEqual(metadata["canonical_url"], "https://yoshuabengio.org/en/publication/example")
        self.assertEqual(metadata["topic_tags"], "deep learning; representation learning")
        self.assertNotIn("Full body should not be saved", str(metadata))
        self.assertTrue(metadata["metadata_hash"])

    def test_canonical_cross_host_is_ignored(self):
        html = '<html><head><title>X</title><link rel="canonical" href="https://example.com/copied" /></head><body></body></html>'
        metadata = parse_metadata(html, "https://yoshuabengio.org/en/blog/x", {"Page / resource name": "x", "Resource type": "Blog"})
        self.assertEqual(metadata["canonical_url"], "https://yoshuabengio.org/en/blog/x")

    def test_extract_links_labels_scope_without_fetching(self):
        html = '''<html><body>
          <a href="/en/blog/new-article">New article</a>
          <a href="https://arxiv.org/abs/2401.12345">arXiv paper</a>
          <a href="mailto:test@example.com">Email</a>
          <a href="https://yoshuabengio.org/wp-admin/">Admin</a>
          </body></html>'''
        links = extract_links(html, "https://yoshuabengio.org/en")
        self.assertEqual(len(links), 3)
        scopes = {x["link_text"]: x["target_scope"] for x in links}
        self.assertEqual(scopes["New article"], "first-party-in-scope")
        self.assertEqual(scopes["arXiv paper"], "external")
        self.assertEqual(scopes["Admin"], "same-host-out-of-scope")


class FakeResponse:
    def __init__(self, status_code, text="", headers=None, encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = encoding
        self._body = text.encode(encoding)
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.mapping:
            raise OSError("network disabled in fixture")
        return self.mapping[url]


class CollectorNetworkGuardTests(unittest.TestCase):
    def test_end_to_end_metadata_only_stores_no_body_text(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "inventory.sqlite"
            robots_url = "https://yoshuabengio.org/robots.txt"
            page_url = "https://yoshuabengio.org/en"
            robots = FakeResponse(200, "User-agent: *\nAllow: /en/\nCrawl-delay: 1\n", {"Content-Type": "text/plain"})
            html = """<html><head><title>Fixture Home</title>
                <meta name="description" content="Fixture description" />
                </head><body><a href="/en/blog/hello">Read article</a>
                <p>Private full body is not extracted.</p></body></html>"""
            page = FakeResponse(200, html, {"Content-Type": "text/html; charset=utf-8"})
            session = FakeSession({robots_url: robots, page_url: page})
            collector = ConservativeCollector(db_path, session=session, sleep_fn=lambda _seconds: None)
            try:
                row = {
                    "manifest_id": "YB-URL-001",
                    "person_source": "Yoshua Bengio",
                    "url": page_url,
                    "page_resource_name": "Homepage",
                    "resource_type": "Discovery index",
                    "domain_scope": "First-party",
                    "verification_status": "Opened",
                    "priority": "P1",
                    "collection_decision": "metadata-first",
                }
                sync_manifest(collector.db, [row])
                final_url, result_html, _content_type, _status_code = collector._get_html(page_url)
                meta = parse_metadata(result_html, final_url, row)
                collector._save_document(row["manifest_id"], meta)
                links = extract_links(result_html, final_url)
                collector._save_discovered_links(row["manifest_id"], links)
                collector.db.commit()
                doc = collector.db.execute(
                    "SELECT title,abstract_or_description,text_storage_status,content_hash,metadata_hash FROM documents WHERE canonical_url=?",
                    (page_url,),
                ).fetchone()
                self.assertEqual(doc["title"], "Fixture Home")
                self.assertEqual(doc["abstract_or_description"], "Fixture description")
                self.assertEqual(doc["text_storage_status"], "metadata-only")
                self.assertIsNone(doc["content_hash"])
                self.assertTrue(doc["metadata_hash"])
                self.assertEqual(collector.db.execute("SELECT COUNT(*) FROM discovered_links").fetchone()[0], 1)
                self.assertEqual(len(session.calls), 2)
            finally:
                collector.close()

    def test_robot_disallow_blocks_content_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "inventory.sqlite"
            robots_url = "https://yoshuabengio.org/robots.txt"
            robots = FakeResponse(200, "User-agent: *\nDisallow: /en\n", {"Content-Type": "text/plain"})
            session = FakeSession({robots_url: robots})
            collector = ConservativeCollector(db_path, session=session, sleep_fn=lambda _seconds: None)
            try:
                with self.assertRaises(PermissionError):
                    collector._get_html("https://yoshuabengio.org/en")
                self.assertEqual(len(session.calls), 1)
            finally:
                collector.close()


if __name__ == "__main__":
    unittest.main()
