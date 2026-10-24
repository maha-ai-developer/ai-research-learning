"""Deterministic offline unit tests for the modular source-adapter layer and pilot coordinator.

All tests run 100% offline using mocked HTTP responses. No live network calls are made.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
for p in (str(ROOT_DIR), str(SRC_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import collect  # noqa: E402
from knowledge_store import KnowledgeStore  # noqa: E402
from ai_research_learning.sources import (  # noqa: E402
    ARXIV_POLICY_PROFILE_ID,
    ARXIV_POLICY_VERSION,
    DEFAULT_PROJECT_USER_AGENT,
    ArxivSourceAdapter,
    BengioSourceAdapter,
    HintonSourceAdapter,
    NotModifiedResponse,
    OriginRateLimiter,
    ParseError,
    PilotCoordinator,
    PolicyBlockedError,
    RateLimitError,
    ScopeViolationError,
    SourceCandidate,
    ThreeBlueOneBrownAdapter,
    TransientFetchError,
    WikipediaSourceAdapter,
    is_identified_user_agent,
)


class FakeResponse:
    """Deterministic fake `requests.Response` for offline testing."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: str | bytes = "",
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.encoding = encoding
        self._bytes = body.encode(encoding) if isinstance(body, str) else body
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        stream = io.BytesIO(self._bytes)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Deterministic URL router replacing `requests.Session` in offline tests."""

    def __init__(self, routes: dict[str, FakeResponse | list[FakeResponse] | Exception]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        allow_redirects: bool = False,
        stream: bool = True,
    ) -> FakeResponse:
        self.calls.append((url, dict(headers or {})))
        if url not in self.routes:
            raise AssertionError(f"Unexpected HTTP GET to unmocked URL: {url}")
        target = self.routes[url]
        if isinstance(target, list):
            if not target:
                raise AssertionError(f"Exhausted mocked responses for URL: {url}")
            item = target.pop(0)
        else:
            item = target
        if isinstance(item, Exception):
            raise item
        return item


SAMPLE_HINTON_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>Geoffrey E. Hinton - Home Page</title>
  <meta name="description" content="Geoffrey Hinton emeritus home page at the University of Toronto." />
  <link rel="canonical" href="https://www.cs.toronto.edu/~hinton/" />
</head>
<body>
  <h1>Geoffrey E. Hinton</h1>
  <a href="/~hinton/absps/NatureDeepReview.pdf">Deep Learning Review (PDF)</a>
  <a href="/~hinton/csc321/notes.html">Course Notes</a>
  <a href="https://arxiv.org/abs/1207.0580">Dropout Paper on arXiv</a>
  <a href="https://example.org/unrelated">External Unrelated Link</a>
  <a href="javascript:alert('unsafe')">Unsafe JS Link</a>
  <a href="mailto:hinton@cs.toronto.edu">Email Link</a>
</body>
</html>
"""

SAMPLE_BENGIO_HTML = """<!DOCTYPE html>
<html lang="en-US">
<head>
  <title>Yoshua Bengio - Full Professor, Mila</title>
  <meta property="og:description" content="Official website of Yoshua Bengio covering deep learning and AI safety." />
  <meta property="article:published_time" content="2024-05-10T12:00:00Z" />
  <meta property="article:modified_time" content="2025-01-15T08:30:00Z" />
  <link rel="canonical" href="https://yoshuabengio.org/en/" />
</head>
<body>
  <h1>Yoshua Bengio</h1>
  <h2>Research Themes</h2>
  <h3>AI Safety &amp; Governance</h3>
  <a href="https://yoshuabengio.org/en/ai-scientist/">The AI Scientist</a>
  <a href="https://mila.quebec/en/">Mila</a>
</body>
</html>
"""

SAMPLE_3B1B_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>Neural networks - 3Blue1Brown</title>
  <meta name="description" content="Visual introduction to neural networks, backpropagation, and transformers." />
</head>
<body>
  <h1>Neural networks</h1>
  <a href="/lessons/neural-networks">But what is a neural network?</a>
  <a href="/lessons/gradient-descent">Gradient descent, how neural networks learn</a>
  <a href="https://www.youtube.com/watch?v=aircAruvnKk">Official YouTube Video</a>
</body>
</html>
"""

SAMPLE_ARXIV_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1207.0580v1</id>
    <updated>2012-07-03T14:02:11Z</updated>
    <published>2012-07-03T14:02:11Z</published>
    <title>Improving neural networks by preventing co-adaptation of feature detectors</title>
    <summary>When a large feedforward neural network is trained on a small training set, it typically performs poorly on held-out test data.</summary>
    <author><name>Geoffrey E. Hinton</name></author>
    <author><name>Nitish Srivastava</name></author>
    <arxiv:comment>18 pages</arxiv:comment>
    <link href="http://arxiv.org/abs/1207.0580v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1207.0580v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.NE" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.NE" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""

SAMPLE_ARXIV_BATCH_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1207.0580v1</id>
    <updated>2012-07-03T14:02:11Z</updated>
    <published>2012-07-03T14:02:11Z</published>
    <title>Improving neural networks by preventing co-adaptation of feature detectors</title>
    <summary>When a large feedforward neural network is trained on a small training set, it typically performs poorly on held-out test data.</summary>
    <author><name>Geoffrey E. Hinton</name></author>
    <author><name>Nitish Srivastava</name></author>
    <arxiv:comment>18 pages</arxiv:comment>
    <link href="http://arxiv.org/abs/1207.0580v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1207.0580v1" rel="related" type="application/pdf"/>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.NE" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.NE" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1409.0473v7</id>
    <updated>2016-05-19T16:10:00Z</updated>
    <published>2014-09-01T19:15:12Z</published>
    <title>Neural Machine Translation by Jointly Learning to Align and Translate</title>
    <summary>Neural machine translation is a recently proposed approach to machine translation.</summary>
    <author><name>Dzmitry Bahdanau</name></author>
    <author><name>Kyunghyun Cho</name></author>
    <author><name>Yoshua Bengio</name></author>
    <link href="http://arxiv.org/abs/1409.0473v7" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/1409.0473v7" rel="related" type="application/pdf"/>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""

SAMPLE_WIKIPEDIA_JSON = json.dumps(
    {
        "batchcomplete": True,
        "query": {
            "pages": [
                {
                    "pageid": 389217,
                    "ns": 0,
                    "title": "Backpropagation",
                    "pagelanguage": "en",
                    "lastrevid": 1245678901,
                    "canonicalurl": "https://en.wikipedia.org/wiki/Backpropagation",
                    "fullurl": "https://en.wikipedia.org/wiki/Backpropagation",
                    "revisions": [
                        {
                            "revid": 1245678901,
                            "parentid": 1245670000,
                            "timestamp": "2026-10-01T10:20:30Z",
                        }
                    ],
                    "pageprops": {
                        "wikibase_item": "Q193524",
                        "wikibase-shortdesc": "Algorithm for training artificial neural networks",
                    },
                    "categories": [
                        {"ns": 14, "title": "Category:Artificial neural networks"},
                        {"ns": 14, "title": "Category:Machine learning algorithms"},
                    ],
                }
            ]
        },
    }
)


class FakeClock:
    """Deterministic clock and sleep recorder for rate-limiter testing."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class SourceAdaptersNormalizationTests(unittest.TestCase):
    """Test normalization and policy auditing across all 5 source adapters."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.limiter = OriginRateLimiter(sleep_fn=self.clock.sleep, clock_fn=self.clock.clock)

    def test_hinton_adapter_policy_and_normalization(self) -> None:
        session = FakeSession(
            {
                "https://www.cs.toronto.edu/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nDisallow: /private/\n",
                    headers={"Content-Type": "text/plain"},
                ),
                "https://www.cs.toronto.edu/~hinton/": FakeResponse(
                    status_code=200,
                    body=SAMPLE_HINTON_HTML,
                    headers={"Content-Type": "text/html; charset=utf-8", "ETag": '"hinton-v1"'},
                ),
            }
        )
        adapter = HintonSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        self.assertTrue(policy.is_metadata_collection_permitted)
        self.assertEqual(policy.status, "OK")
        self.assertEqual(policy.effective_delay_seconds, 5.0)

        candidates = adapter.discover_candidates(limit=1)
        self.assertEqual(len(candidates), 1)
        envelope = adapter.collect_candidate(candidates[0], policy=policy)

        self.assertEqual(envelope.source_id, "SRC-HINTON")
        self.assertEqual(envelope.canonical_url, "https://www.cs.toronto.edu/~hinton/")
        self.assertEqual(envelope.title, "Geoffrey E. Hinton - Home Page")
        self.assertEqual(envelope.authors, "Geoffrey E. Hinton")
        self.assertEqual(envelope.permitted_storage_scope, "METADATA_ONLY")
        self.assertFalse(envelope.permits_full_text_storage)
        self.assertFalse(envelope.permits_verbatim_excerpts)
        self.assertEqual(envelope.source_specific_metadata["catalogued_pdf_citation_count"], 1)
        self.assertEqual(envelope.source_specific_metadata["in_scope_html_subpage_count"], 1)
        self.assertIn("https://arxiv.org/abs/1207.0580", envelope.official_links)

    def test_bengio_adapter_crawl_delay_and_normalization(self) -> None:
        session = FakeSession(
            {
                "https://yoshuabengio.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nCrawl-delay: 10\nSitemap: https://yoshuabengio.org/wp-sitemap.xml\n",
                    headers={"Content-Type": "text/plain"},
                ),
                "https://yoshuabengio.org/en": FakeResponse(
                    status_code=200,
                    body=SAMPLE_BENGIO_HTML,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                ),
            }
        )
        adapter = BengioSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        self.assertTrue(policy.is_metadata_collection_permitted)
        self.assertEqual(policy.observed_crawl_delay_seconds, 10.0)
        self.assertEqual(policy.effective_delay_seconds, 10.0)
        self.assertEqual(policy.sitemap_urls, ["https://yoshuabengio.org/wp-sitemap.xml"])

        candidates = adapter.discover_candidates(limit=1)
        envelope = adapter.collect_candidate(candidates[0], policy=policy)
        # First request (robots.txt) waited 0s; second request (/en) waited 10s
        self.assertEqual(self.clock.slept, [10.0])
        self.assertEqual(envelope.source_id, "SRC-BENGIO")
        self.assertEqual(envelope.canonical_url, "https://yoshuabengio.org/en")
        self.assertEqual(envelope.source_specific_metadata["html_declared_canonical_url"], "https://yoshuabengio.org/en/")
        self.assertEqual(envelope.published_at, "2024-05-10T12:00:00Z")
        self.assertEqual(envelope.updated_at_source, "2025-01-15T08:30:00Z")

    def test_3b1b_adapter_lesson_index_normalization(self) -> None:
        session = FakeSession(
            {
                "https://www.3blue1brown.com/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nAllow: /\n",
                    headers={"Content-Type": "text/plain"},
                ),
                "https://www.3blue1brown.com/topics/neural-networks": FakeResponse(
                    status_code=200,
                    body=SAMPLE_3B1B_HTML,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                ),
            }
        )
        adapter = ThreeBlueOneBrownAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        candidates = adapter.discover_candidates(limit=2)
        topic_candidate = [c for c in candidates if "topics/neural-networks" in c.url][0]
        envelope = adapter.collect_candidate(topic_candidate, policy=policy)

        self.assertEqual(envelope.source_id, "SRC-3B1B")
        self.assertEqual(envelope.media_modality, "VIDEO_AND_INTERACTIVE_LESSON_INDEX")
        self.assertIn(
            "https://www.3blue1brown.com/lessons/neural-networks",
            envelope.source_specific_metadata["discovered_lesson_or_topic_urls"],
        )
        self.assertIn(
            "https://www.youtube.com/watch?v=aircAruvnKk",
            envelope.source_specific_metadata["official_video_links"],
        )
        self.assertFalse(envelope.permits_full_text_storage)

    def test_arxiv_adapter_atom_xml_normalization_and_3s_rate_limit(self) -> None:
        q1 = "https://export.arxiv.org/api/query?id_list=1207.0580&max_results=1"
        session = FakeSession(
            {
                "https://export.arxiv.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nDisallow: /\nCrawl-delay: 15\n",
                    headers={"Content-Type": "text/plain"},
                ),
                q1: FakeResponse(
                    status_code=200,
                    body=SAMPLE_ARXIV_ATOM_XML,
                    headers={"Content-Type": "application/atom+xml"},
                ),
            }
        )
        adapter = ArxivSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        self.assertTrue(policy.is_metadata_collection_permitted)
        self.assertEqual(policy.status, "OK")
        self.assertEqual(policy.metadata_collection_decision, "PERMITTED")
        self.assertIn("DISALLOWED_BY_ROBOTS_TXT", policy.robots_crawl_status or "")
        self.assertFalse(policy.general_html_crawling_permitted)
        self.assertEqual(policy.policy_profile_id, ARXIV_POLICY_PROFILE_ID)
        self.assertEqual(policy.policy_version, ARXIV_POLICY_VERSION)
        self.assertEqual(policy.approved_api_endpoint, "https://export.arxiv.org/api/query")
        self.assertGreaterEqual(policy.effective_delay_seconds, 3.0)

        candidates = adapter.discover_candidates(limit=1)
        envelope = adapter.collect_candidate(candidates[0], policy=policy)

        self.assertEqual(self.clock.slept, [3.0])
        self.assertEqual(envelope.source_id, "SRC-ARXIV")
        self.assertEqual(envelope.canonical_url, "https://arxiv.org/abs/1207.0580")
        self.assertEqual(envelope.external_identifier, "arXiv:1207.0580")
        self.assertEqual(envelope.authors, "Geoffrey E. Hinton, Nitish Srivastava")
        self.assertEqual(envelope.permitted_storage_scope, "METADATA_AND_ABSTRACT")
        self.assertFalse(envelope.permits_full_text_storage)
        self.assertTrue(envelope.permits_verbatim_excerpts)
        self.assertEqual(envelope.source_specific_metadata["primary_category"], "cs.NE")
        self.assertEqual(envelope.source_specific_metadata["policy_profile_id"], ARXIV_POLICY_PROFILE_ID)
        self.assertEqual(envelope.source_specific_metadata["policy_version"], ARXIV_POLICY_VERSION)
        self.assertEqual(
            envelope.source_specific_metadata["pdf_reference_url_not_downloaded"],
            "http://arxiv.org/pdf/1207.0580v1",
        )

    def test_arxiv_batch_query_returns_multiple_envelopes_in_single_api_call(self) -> None:
        batch_url = "https://export.arxiv.org/api/query?id_list=1207.0580%2C1409.0473&max_results=2"
        session = FakeSession(
            {
                "https://export.arxiv.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nDisallow: /\nCrawl-delay: 15\n",
                    headers={"Content-Type": "text/plain"},
                ),
                batch_url: FakeResponse(
                    status_code=200,
                    body=SAMPLE_ARXIV_BATCH_ATOM_XML,
                    headers={"Content-Type": "application/atom+xml"},
                ),
            }
        )
        adapter = ArxivSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        candidates = adapter.discover_candidates(limit=2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].url, batch_url)

        envelopes = adapter.collect_candidate_envelopes(candidates[0], policy=policy)
        self.assertEqual(len(envelopes), 2)
        self.assertEqual(envelopes[0].canonical_url, "https://arxiv.org/abs/1207.0580")
        self.assertEqual(envelopes[0].external_identifier, "arXiv:1207.0580")
        self.assertEqual(envelopes[1].canonical_url, "https://arxiv.org/abs/1409.0473")
        self.assertEqual(envelopes[1].external_identifier, "arXiv:1409.0473")
        # Exactly 1 robots.txt call + 1 batched API query call
        self.assertEqual(len(session.calls), 2)

    def test_wikipedia_adapter_mediawiki_json_normalization(self) -> None:
        adapter = WikipediaSourceAdapter(rate_limiter=self.limiter)
        candidates = adapter.discover_candidates(limit=1)
        q_url = candidates[0].url
        session = FakeSession(
            {
                "https://en.wikipedia.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nDisallow: /w/\nAllow: /wiki/\n",
                    headers={"Content-Type": "text/plain"},
                ),
                q_url: FakeResponse(
                    status_code=200,
                    body=SAMPLE_WIKIPEDIA_JSON,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                ),
            }
        )
        adapter.session = session
        policy = adapter.audit_policy()
        self.assertTrue(policy.is_metadata_collection_permitted)

        envelope = adapter.collect_candidate(candidates[0], policy=policy)
        self.assertEqual(envelope.source_id, "SRC-WIKIPEDIA")
        self.assertEqual(envelope.canonical_url, "https://en.wikipedia.org/wiki/Backpropagation")
        self.assertEqual(envelope.license_status, "CC_BY_SA_4_0")
        self.assertEqual(envelope.license_spdx, "CC-BY-SA-4.0")
        self.assertEqual(envelope.source_specific_metadata["page_id"], 389217)
        self.assertEqual(envelope.source_specific_metadata["revision_id"], 1245678901)
        self.assertEqual(envelope.source_specific_metadata["wikidata_qid"], "Q193524")
        self.assertIn(
            "https://en.wikipedia.org/w/index.php?title=Backpropagation&oldid=1245678901",
            envelope.official_links,
        )
        # Verify descriptive User-Agent and Api-User-Agent were sent
        _, sent_headers = session.calls[-1]
        self.assertEqual(sent_headers.get("User-Agent"), DEFAULT_PROJECT_USER_AGENT)
        self.assertEqual(sent_headers.get("Api-User-Agent"), DEFAULT_PROJECT_USER_AGENT)


class PolicyAndSafetyBoundaryTests(unittest.TestCase):
    """Test fail-closed robots.txt handling, redirects, PDF rejection, and rate-limit errors."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.limiter = OriginRateLimiter(sleep_fn=self.clock.sleep, clock_fn=self.clock.clock)

    def test_robots_disallow_blocks_adapter_fail_closed(self) -> None:
        session = FakeSession(
            {
                "https://www.cs.toronto.edu/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nDisallow: /~hinton/\n",
                    headers={"Content-Type": "text/plain"},
                ),
            }
        )
        adapter = HintonSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        self.assertFalse(policy.is_metadata_collection_permitted)
        self.assertEqual(policy.status, "BLOCKED")
        self.assertEqual(policy.metadata_collection_decision, "DENIED")

        candidate = adapter.discover_candidates(limit=1)[0]
        with self.assertRaises(PolicyBlockedError):
            adapter.fetch_metadata(candidate, policy=policy)
        # Verify the page URL was never requested after robots.txt blocked it
        self.assertEqual(len(session.calls), 1)

    def test_corrupt_or_unavailable_robots_fails_closed(self) -> None:
        # Case A: HTTP 500 on robots.txt -> UNVERIFIED / REQUIRES_REVIEW
        session_500 = FakeSession(
            {
                "https://yoshuabengio.org/robots.txt": FakeResponse(status_code=500, body="Server error"),
            }
        )
        adapter_500 = BengioSourceAdapter(session=session_500, rate_limiter=self.limiter)
        policy_500 = adapter_500.audit_policy()
        self.assertFalse(policy_500.is_metadata_collection_permitted)
        self.assertEqual(policy_500.status, "UNVERIFIED")
        with self.assertRaises(PolicyBlockedError):
            adapter_500.fetch_metadata(adapter_500.discover_candidates(limit=1)[0], policy=policy_500)

        # Case B: Null bytes / corrupt binary in robots.txt -> UNVERIFIED
        session_corrupt = FakeSession(
            {
                "https://yoshuabengio.org/robots.txt": FakeResponse(
                    status_code=200,
                    body=b"User-agent: *\x00\x00Disallow: /",
                ),
            }
        )
        adapter_corrupt = BengioSourceAdapter(session=session_corrupt, rate_limiter=self.limiter)
        policy_corrupt = adapter_corrupt.audit_policy()
        self.assertFalse(policy_corrupt.is_metadata_collection_permitted)
        self.assertEqual(policy_corrupt.status, "UNVERIFIED")

    def test_unidentified_user_agent_fails_closed(self) -> None:
        self.assertFalse(is_identified_user_agent("python-requests/2.31.0"))
        self.assertFalse(
            is_identified_user_agent(
                "AIResearchLearningMetadataBot/0.1 (metadata-only; configure a contact URL before production)"
            )
        )
        self.assertTrue(is_identified_user_agent(DEFAULT_PROJECT_USER_AGENT))

        session = FakeSession({})
        adapter = WikipediaSourceAdapter(
            user_agent="python-requests/2.31.0",
            session=session,
            rate_limiter=self.limiter,
        )
        policy = adapter.audit_policy()
        self.assertFalse(policy.is_metadata_collection_permitted)
        self.assertEqual(policy.status, "BLOCKED")
        self.assertEqual(len(session.calls), 0)

    def test_cross_host_redirect_is_rejected(self) -> None:
        session = FakeSession(
            {
                "https://www.cs.toronto.edu/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nAllow: /\n",
                ),
                "https://www.cs.toronto.edu/~hinton/": FakeResponse(
                    status_code=302,
                    headers={"Location": "https://malicious.example.com/phish"},
                ),
            }
        )
        adapter = HintonSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        candidate = adapter.discover_candidates(limit=1)[0]
        with self.assertRaises(ScopeViolationError):
            adapter.fetch_metadata(candidate, policy=policy)
        # Only robots.txt and the initial URL were called; malicious host was never called
        self.assertEqual(len(session.calls), 2)

    def test_pdf_and_media_urls_are_rejected(self) -> None:
        session = FakeSession(
            {
                "https://www.cs.toronto.edu/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nAllow: /\n",
                ),
            }
        )
        adapter = HintonSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()

        pdf_candidate = SourceCandidate(
            source_id="SRC-HINTON",
            url="https://www.cs.toronto.edu/~hinton/absps/NatureDeepReview.pdf",
        )
        with self.assertRaises(ScopeViolationError):
            adapter.fetch_metadata(pdf_candidate, policy=policy)

        arxiv_pdf = SourceCandidate(
            source_id="SRC-ARXIV",
            url="https://export.arxiv.org/pdf/1207.0580.pdf",
        )
        arxiv_adapter = ArxivSourceAdapter(session=session, rate_limiter=self.limiter)
        with self.assertRaises(ScopeViolationError):
            arxiv_adapter.fetch_metadata(arxiv_pdf, policy=policy)

    def test_rate_limit_429_and_retry_after_handling(self) -> None:
        adapter = WikipediaSourceAdapter(rate_limiter=self.limiter)
        candidate = adapter.discover_candidates(limit=1)[0]
        session = FakeSession(
            {
                "https://en.wikipedia.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nAllow: /wiki/\n",
                ),
                candidate.url: FakeResponse(
                    status_code=429,
                    body="Too Many Requests",
                    headers={"Retry-After": "45"},
                ),
            }
        )
        adapter.session = session
        policy = adapter.audit_policy()
        with self.assertRaises(RateLimitError) as ctx:
            adapter.fetch_metadata(candidate, policy=policy)
        self.assertEqual(ctx.exception.http_status, 429)
        self.assertEqual(ctx.exception.retry_after_seconds, 45.0)
        self.assertEqual( ctx.exception.error_class, "RATE_LIMITED")

    def test_conditional_get_304_not_modified(self) -> None:
        session = FakeSession(
            {
                "https://www.cs.toronto.edu/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nAllow: /\n",
                ),
                "https://www.cs.toronto.edu/~hinton/": FakeResponse(
                    status_code=304,
                    headers={"ETag": '"hinton-v1"'},
                ),
            }
        )
        adapter = HintonSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        candidate = adapter.discover_candidates(limit=1)[0]
        with self.assertRaises(NotModifiedResponse):
            adapter.fetch_metadata(
                candidate,
                policy=policy,
                conditional_headers={"If-None-Match": '"hinton-v1"'},
            )
        _, sent_headers = session.calls[-1]
        self.assertEqual(sent_headers.get("If-None-Match"), '"hinton-v1"')

    def test_malformed_payload_raises_parse_error(self) -> None:
        arxiv_adapter = ArxivSourceAdapter(rate_limiter=self.limiter)
        q_url = arxiv_adapter.discover_candidates(limit=1)[0].url
        session = FakeSession(
            {
                "https://export.arxiv.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nAllow: /api/\n",
                ),
                q_url: FakeResponse(
                    status_code=200,
                    body="<not-valid-xml",
                    headers={"Content-Type": "application/atom+xml"},
                ),
            }
        )
        arxiv_adapter.session = session
        policy = arxiv_adapter.audit_policy()
        with self.assertRaises(ParseError):
            arxiv_adapter.collect_candidate(arxiv_adapter.discover_candidates(limit=1)[0], policy=policy)

    def test_arxiv_api_policy_profile_strictly_blocks_html_pdf_unapproved_hosts_and_redirects(self) -> None:
        valid_api_url = "https://export.arxiv.org/api/query?id_list=1207.0580&max_results=1"
        session = FakeSession(
            {
                "https://export.arxiv.org/robots.txt": FakeResponse(
                    status_code=200,
                    body="User-agent: *\nDisallow: /\nCrawl-delay: 15\n",
                    headers={"Content-Type": "text/plain"},
                ),
                valid_api_url: FakeResponse(
                    status_code=302,
                    headers={"Location": "https://arxiv.org/abs/1207.0580"},
                ),
            }
        )
        adapter = ArxivSourceAdapter(session=session, rate_limiter=self.limiter)
        policy = adapter.audit_policy()
        self.assertTrue(policy.is_metadata_collection_permitted)
        self.assertFalse(policy.general_html_crawling_permitted)
        self.assertIn("DISALLOWED_BY_ROBOTS_TXT", policy.robots_crawl_status or "")
        self.assertTrue(policy.is_api_url_permitted(valid_api_url))

        # All non-API paths, HTML paths, PDF paths, unapproved hosts, HTTP scheme, or unbounded queries MUST be blocked
        blocked_urls = [
            "https://export.arxiv.org/abs/1207.0580",
            "https://export.arxiv.org/list/cs.AI/recent",
            "https://export.arxiv.org/pdf/1207.0580.pdf",
            "https://export.arxiv.org/api/other?id_list=1207.0580",
            "https://export.arxiv.org/oai2?verb=Identify",
            "https://arxiv.org/abs/1207.0580",
            "https://arxiv.org/api/query?id_list=1207.0580&max_results=1",
            "http://export.arxiv.org/api/query?id_list=1207.0580&max_results=1",
            "https://export.arxiv.org/api/query",
            "https://export.arxiv.org/api/query?id_list=1207.0580&max_results=51",
            "https://export.arxiv.org/api/query?id_list=not_an_id&max_results=1",
            "https://export.arxiv.org/api/query?id_list=1207.0580&unexpected_param=1",
            "https://example.org/api/query?id_list=1207.0580&max_results=1",
        ]
        for bad_url in blocked_urls:
            with self.subTest(bad_url=bad_url):
                self.assertFalse(adapter.is_url_in_scope(bad_url))
                self.assertFalse(policy.is_api_url_permitted(bad_url))
                with self.assertRaises(ScopeViolationError):
                    adapter.fetch_metadata(
                        SourceCandidate(source_id="SRC-ARXIV", url=bad_url),
                        policy=policy,
                    )

        # Redirect from the approved API endpoint must also be rejected fail-closed
        with self.assertRaises(ScopeViolationError):
            adapter.fetch_metadata(
                SourceCandidate(source_id="SRC-ARXIV", url=valid_api_url),
                policy=policy,
            )

        # If API terms verification is revoked/disabled, or robots.txt explicitly disallows /api/query, fail closed
        adapter_unverified = ArxivSourceAdapter(
            session=FakeSession(
                {
                    "https://export.arxiv.org/robots.txt": FakeResponse(
                        status_code=200,
                        body="User-agent: *\nDisallow: /\n",
                    ),
                }
            ),
            rate_limiter=self.limiter,
            api_terms_verified=False,
        )
        policy_unverified = adapter_unverified.audit_policy()
        self.assertFalse(policy_unverified.is_metadata_collection_permitted)
        self.assertEqual(policy_unverified.status, "BLOCKED")
        with self.assertRaises(PolicyBlockedError):
            adapter_unverified.fetch_metadata(
                SourceCandidate(source_id="SRC-ARXIV", url=valid_api_url),
                policy=policy_unverified,
            )

        adapter_api_disallowed = ArxivSourceAdapter(
            session=FakeSession(
                {
                    "https://export.arxiv.org/robots.txt": FakeResponse(
                        status_code=200,
                        body="User-agent: *\nDisallow: /api/query\n",
                    ),
                }
            ),
            rate_limiter=self.limiter,
        )
        policy_api_disallowed = adapter_api_disallowed.audit_policy()
        self.assertFalse(policy_api_disallowed.is_metadata_collection_permitted)
        self.assertEqual(policy_api_disallowed.status, "BLOCKED")


class PilotCoordinatorAndIdempotencyTests(unittest.TestCase):
    """Test PilotCoordinator SQLite persistence, idempotency, caps, and provenance events."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pilot.sqlite"
        self.clock = FakeClock()
        self.limiter = OriginRateLimiter(sleep_fn=self.clock.sleep, clock_fn=self.clock.clock)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _build_mocked_adapters(self, *, block_bengio: bool = False, rate_limit_wiki: bool = False):
        wiki_tmp = WikipediaSourceAdapter(rate_limiter=self.limiter)
        wiki_cands = wiki_tmp.discover_candidates(limit=2)
        arxiv_tmp = ArxivSourceAdapter(rate_limiter=self.limiter)
        arxiv_cand_1 = arxiv_tmp.discover_candidates(limit=1)[0]
        arxiv_cand_2 = arxiv_tmp.discover_candidates(limit=2)[0]

        routes: dict[str, FakeResponse | list[FakeResponse] | Exception] = {
            "https://www.cs.toronto.edu/robots.txt": FakeResponse(
                status_code=200,
                body="User-agent: *\nAllow: /\n",
            ),
            "https://www.cs.toronto.edu/~hinton/": FakeResponse(
                status_code=200,
                body=SAMPLE_HINTON_HTML,
                headers={"Content-Type": "text/html"},
            ),
            "https://yoshuabengio.org/robots.txt": FakeResponse(
                status_code=200,
                body=(
                    "User-agent: *\nDisallow: /en\n"
                    if block_bengio
                    else "User-agent: *\nCrawl-delay: 5\nAllow: /\n"
                ),
            ),
            "https://yoshuabengio.org/en": FakeResponse(
                status_code=200,
                body=SAMPLE_BENGIO_HTML,
                headers={"Content-Type": "text/html"},
            ),
            "https://www.3blue1brown.com/robots.txt": FakeResponse(
                status_code=200,
                body="User-agent: *\nAllow: /\n",
            ),
            "https://www.3blue1brown.com/": FakeResponse(
                status_code=200,
                body=SAMPLE_3B1B_HTML,
                headers={"Content-Type": "text/html"},
            ),
            "https://export.arxiv.org/robots.txt": FakeResponse(
                status_code=200,
                body="User-agent: *\nDisallow: /\nCrawl-delay: 15\n",
            ),
            arxiv_cand_1.url: FakeResponse(
                status_code=200,
                body=SAMPLE_ARXIV_ATOM_XML,
                headers={"Content-Type": "application/atom+xml"},
            ),
            arxiv_cand_2.url: FakeResponse(
                status_code=200,
                body=SAMPLE_ARXIV_BATCH_ATOM_XML,
                headers={"Content-Type": "application/atom+xml"},
            ),
            "https://en.wikipedia.org/robots.txt": FakeResponse(
                status_code=200,
                body="User-agent: *\nAllow: /wiki/\n",
            ),
            wiki_cands[0].url: (
                FakeResponse(status_code=429, body="Rate limited", headers={"Retry-After": "30"})
                if rate_limit_wiki
                else FakeResponse(
                    status_code=200,
                    body=SAMPLE_WIKIPEDIA_JSON,
                    headers={"Content-Type": "application/json"},
                )
            ),
        }
        session = FakeSession(routes)
        kwargs = {"session": session, "rate_limiter": self.limiter}
        return [
            HintonSourceAdapter(**kwargs),
            BengioSourceAdapter(**kwargs),
            ThreeBlueOneBrownAdapter(**kwargs),
            ArxivSourceAdapter(**kwargs),
            WikipediaSourceAdapter(**kwargs),
        ]

    def test_pilot_enforces_hard_caps(self) -> None:
        with KnowledgeStore(db_path=self.db_path) as store:
            coordinator = PilotCoordinator(store, adapters=[])
            with self.assertRaises(ValueError):
                coordinator.run_metadata_pilot(max_per_source=3, max_total=10)
            with self.assertRaises(ValueError):
                coordinator.run_metadata_pilot(max_per_source=2, max_total=11)

    def test_idempotent_repeated_pilot_runs_and_provenance(self) -> None:
        with KnowledgeStore(db_path=self.db_path) as store:
            # First run: inserts 5 resources (1 per source across 5 sources)
            adapters_run1 = self._build_mocked_adapters()
            coord1 = PilotCoordinator(store, adapters=adapters_run1)
            summary1 = coord1.run_metadata_pilot(
                seed_manifest=[],
                max_per_source=1,
                max_total=5,
            )
            self.assertEqual(summary1.inserted_count, 5)
            self.assertEqual(summary1.updated_count, 0)
            self.assertEqual(summary1.unchanged_count, 0)
            self.assertEqual(summary1.error_count, 0)
            self.assertEqual(summary1.blocked_count, 0)

            stored_resources = store.list_resources()
            self.assertEqual(len(stored_resources), 5)
            for res in stored_resources:
                self.assertEqual(res["version_count"], 1)
                versions = store.get_resource_versions(res["resource_id"])
                self.assertEqual(len(versions), 1)

            # Second run with identical responses: 0 inserted, 0 updated, 5 unchanged, no duplicate versions
            adapters_run2 = self._build_mocked_adapters()
            coord2 = PilotCoordinator(store, adapters=adapters_run2)
            summary2 = coord2.run_metadata_pilot(
                seed_manifest=[],
                max_per_source=1,
                max_total=5,
            )
            self.assertEqual(summary2.inserted_count, 0)
            self.assertEqual(summary2.updated_count, 0)
            self.assertEqual(summary2.unchanged_count, 5)

            stored_resources_after = store.list_resources()
            self.assertEqual(len(stored_resources_after), 5)
            for res in stored_resources_after:
                self.assertEqual(res["version_count"], 1)

            # Verify provenance events were recorded for both runs
            run1_events = store.list_provenance_events(run_id=summary1.run_id)
            run2_events = store.list_provenance_events(run_id=summary2.run_id)
            self.assertTrue(any(e["event_type"] == "SOURCE_POLICY_AUDIT" for e in run1_events))
            self.assertTrue(any(e["event_type"] == "ADAPTER_METADATA_COLLECTED" for e in run1_events))
            self.assertTrue(any(e["event_type"] == "RESOURCE_VERIFIED_UNCHANGED" for e in run2_events))

            # Verify discovered_link_candidates were persisted as reviewable candidates without promoting into resources
            candidates_after = store.list_discovered_link_candidates()
            self.assertGreater(len(candidates_after), 0)
            for cand in candidates_after:
                # Seen in both run1 and run2 -> seen_count == 2 without duplicating candidate rows
                self.assertEqual(cand["seen_count"], 2)
                self.assertEqual(cand["review_status"], "PENDING_REVIEW")
                self.assertEqual(cand["fetch_permission_granted"], 0)
                self.assertIsNone(cand["promoted_resource_id"])
                if cand["domain_classification"] == "EXTERNAL":
                    self.assertEqual(cand["is_internal"], 0)
                    self.assertEqual(cand["policy_state"], "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH")
                else:
                    self.assertEqual(cand["is_internal"], 1)

            # Verify unsafe/non-HTTP links (javascript:, mailto:) were rejected and audited in provenance_events
            rejected_events = [
                e for e in run1_events if e["event_type"] == "DISCOVERED_LINK_CANDIDATE_REJECTED"
            ]
            self.assertEqual(len(rejected_events), 2)
            rejected_originals = {json.loads(e["details_json"])["original_url"] for e in rejected_events}
            self.assertIn("javascript:alert('unsafe')", rejected_originals)
            self.assertIn("mailto:hinton@cs.toronto.edu", rejected_originals)

    def test_blocked_and_errored_sources_record_provenance(self) -> None:
        with KnowledgeStore(db_path=self.db_path) as store:
            adapters = self._build_mocked_adapters(block_bengio=True, rate_limit_wiki=True)
            coordinator = PilotCoordinator(store, adapters=adapters)
            summary = coordinator.run_metadata_pilot(
                seed_manifest=[],
                max_per_source=1,
                max_total=5,
            )
            self.assertEqual(summary.inserted_count, 3)  # Hinton, 3B1B, arXiv
            self.assertEqual(summary.blocked_count, 1)   # Bengio blocked by robots.txt
            self.assertEqual(summary.error_count, 1)     # Wikipedia 429 rate-limited

            events = store.list_provenance_events(run_id=summary.run_id)
            event_types = {e["event_type"] for e in events}
            self.assertIn("ADAPTER_SOURCE_BLOCKED_BY_POLICY", event_types)
            self.assertIn("ADAPTER_RATE_LIMITED", event_types)

    def test_cli_default_remains_offline_dry_run(self) -> None:
        with patch("requests.Session.get") as mocked_get:
            exit_code = collect.main(["--db", str(self.db_path)])
            self.assertEqual(exit_code, 0)
            mocked_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
