"""Offline unit and integration tests for Task 037: Source-to-Knowledge Ingestion Pipeline."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from knowledge_store import KnowledgeStore, compute_transcript_id, sha256_hex
from ai_research_learning.api import LocalResearchLibraryAPI
from ai_research_learning.ingestion import (
    CanonicalResourceType,
    ChunkEpistemicLayer,
    ChunkingStrategy,
    ContentAwareChunker,
    ContentExtractor,
    EpistemicCategory,
    ExtractionFormat,
    IngestionPolicyViolationError,
    NormalizedContentOrigin,
    STRUCTURED_KNOWLEDGE_ELEMENT_TYPES,
    SourceToKnowledgeIngestionPipeline,
    audit_and_reconcile_wikipedia_scope,
    classify_canonical_resource_type,
    classify_epistemic_category,
    determine_chunk_epistemic_layer,
    ensure_ingestion_schema,
    evaluate_resource_ingestion_policy,
    extract_structured_knowledge,
    main as ingestion_cli_main,
    run_reproducible_ingestion_demo,
    verify_index_consistency,
)
from ai_research_learning.rag import (
    ChromaPersistentVectorStore,
    DEFAULT_CHROMA_COLLECTION,
    DEFAULT_CHROMA_DIR,
    DEFAULT_RUNTIME_DB_PATH,
    compute_chunk_id,
)
from ai_research_learning.sources import (
    BengioSourceAdapter,
    HintonSourceAdapter,
    OriginRateLimiter,
    PolicyBlockedError,
    ScopeViolationError,
    SourceCandidate,
    TheBatchSourceAdapter,
    ThreeBlueOneBrownAdapter,
    WikipediaSourceAdapter,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _FakeHTTPResponse:
    def __init__(
        self,
        body: str | bytes,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        encoding: str = "utf-8",
    ) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.encoding = encoding
        self._bytes = body.encode(encoding) if isinstance(body, str) else body

    def iter_content(self, chunk_size: int = 65536):
        stream = io.BytesIO(self._bytes)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        pass


class _FakeHTTPSession:
    def __init__(self, responses_by_substring: dict[str, _FakeHTTPResponse]) -> None:
        self._responses = responses_by_substring
        self.headers: dict[str, str] = {}

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        allow_redirects: bool = False,
        stream: bool = True,
    ) -> _FakeHTTPResponse:
        if "robots.txt" in url:
            return _FakeHTTPResponse(
                "User-agent: *\nAllow: /\n",
                status_code=200,
                content_type="text/plain; charset=utf-8",
            )
        for key, resp in self._responses.items():
            if key in url:
                return resp
        return _FakeHTTPResponse("Not Found", status_code=404)


class TestTask037IngestionPipeline(unittest.TestCase):
    """Comprehensive offline test suite for Task 037 (Phases A through K)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime_db_path = Path(DEFAULT_RUNTIME_DB_PATH).resolve()
        cls.runtime_chroma_dir = Path(DEFAULT_CHROMA_DIR).resolve()
        cls.baseline_sha256 = _sha256_file(cls.runtime_db_path)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="test_task037_")
        self.work_dir = Path(self._tmp.name)
        self.db_path = self.work_dir / "pilot_copy.sqlite"
        self.chroma_dir = self.work_dir / "chroma_copy"
        shutil.copy2(self.runtime_db_path, self.db_path)
        if self.runtime_chroma_dir.exists():
            shutil.copytree(self.runtime_chroma_dir, self.chroma_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self.assertEqual(
            _sha256_file(self.runtime_db_path),
            self.baseline_sha256,
            "Runtime database data/runtime/task012_pilot.sqlite must never be modified!",
        )

    # ------------------------------------------------------------------
    # 1. Governance & Policy Validation Tests (Phases A & B)
    # ------------------------------------------------------------------

    def test_01_governance_candidate_links_and_the_batch_boundaries(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            ensure_ingestion_schema(conn)

            # 1. Discovered link candidate (181 pending candidates) cannot be ingested directly
            cand_row = conn.execute(
                """
                SELECT candidate_id, normalized_url, referring_source_id
                FROM discovered_link_candidates
                WHERE normalized_url NOT IN (SELECT canonical_url FROM resources)
                LIMIT 1
                """
            ).fetchone()
            self.assertIsNotNone(cand_row)
            cand_policy = evaluate_resource_ingestion_policy(
                conn,
                source_id=str(cand_row["referring_source_id"]),
                canonical_url=str(cand_row["normalized_url"]),
                requested_action="COLLECT",
            )
            self.assertFalse(cand_policy.is_permitted)
            self.assertEqual(cand_policy.decision_code, "REJECTED_UNAPPROVED_DISCOVERED_CANDIDATE")

            # 2. SRC-THE-BATCH path scope violation outside /the-batch, lookalike paths, and dot-segment traversal
            batch_adapter = TheBatchSourceAdapter(curation_approved=True)
            self.assertTrue(batch_adapter.is_url_in_scope("https://www.deeplearning.ai/the-batch/"))
            self.assertTrue(
                batch_adapter.is_url_in_scope("https://www.deeplearning.ai/the-batch/issue-280/?utm_source=test#top")
            )
            for out_of_scope_url in (
                "https://www.deeplearning.ai/courses/machine-learning-specialization/",
                "https://www.deeplearning.ai/the-batch-extra/article/",
                "https://www.deeplearning.ai/the-batching/issue-1/",
                "https://www.deeplearning.ai/the-batch/../courses/ml/",
                "https://evil.example.com/the-batch/issue-280/",
            ):
                self.assertFalse(batch_adapter.is_url_in_scope(out_of_scope_url))
                batch_scope_violation = evaluate_resource_ingestion_policy(
                    conn,
                    source_id="SRC-THE-BATCH",
                    canonical_url=out_of_scope_url,
                    requested_action="COLLECT",
                    curation_approved_override=True,
                )
                self.assertFalse(batch_scope_violation.is_permitted)
                self.assertEqual(
                    batch_scope_violation.decision_code,
                    "REJECTED_THE_BATCH_PATH_SCOPE_VIOLATION",
                )

            # Verify HTTP 302 redirect out of /the-batch/ fails closed with ScopeViolationError
            redirect_resp = _FakeHTTPResponse("", status_code=302)
            redirect_resp.headers["Location"] = "https://www.deeplearning.ai/courses/generative-ai/"
            redirect_session = _FakeHTTPSession(
                {"deeplearning.ai/the-batch/issue-280": redirect_resp}
            )
            redirect_adapter = TheBatchSourceAdapter(
                session=redirect_session,
                rate_limiter=OriginRateLimiter(sleep_fn=lambda _: None),
                curation_approved=True,
            )
            with self.assertRaises(ScopeViolationError):
                redirect_adapter.collect_candidate(
                    SourceCandidate(
                        source_id="SRC-THE-BATCH",
                        url="https://www.deeplearning.ai/the-batch/issue-280/",
                    )
                )

            # 3. SRC-THE-BATCH blocked without explicit curation approval
            batch_blocked = evaluate_resource_ingestion_policy(
                conn,
                source_id="SRC-THE-BATCH",
                canonical_url="https://www.deeplearning.ai/the-batch/issue-250/",
                requested_action="COLLECT",
                curation_approved_override=False,
            )
            self.assertFalse(batch_blocked.is_permitted)
            self.assertEqual(
                batch_blocked.decision_code,
                "REJECTED_CANDIDATE_SOURCE_PENDING_CURATION",
            )

            # 4. SRC-THE-BATCH with curation approval permits only headline/date/topic summary, never full text
            batch_collect_ok = evaluate_resource_ingestion_policy(
                conn,
                source_id="SRC-THE-BATCH",
                canonical_url="https://www.deeplearning.ai/the-batch/issue-250/",
                requested_action="COLLECT",
                curation_approved_override=True,
            )
            self.assertTrue(batch_collect_ok.is_permitted)
            self.assertEqual(
                batch_collect_ok.permitted_chunk_layer,
                ChunkEpistemicLayer.NEWS_AND_COMMENTARY.value,
            )

            batch_fulltext_blocked = evaluate_resource_ingestion_policy(
                conn,
                source_id="SRC-THE-BATCH",
                canonical_url="https://www.deeplearning.ai/the-batch/issue-250/",
                requested_action="EXTRACT_FULL_TEXT",
                curation_approved_override=True,
            )
            self.assertFalse(batch_fulltext_blocked.is_permitted)
            self.assertEqual(
                batch_fulltext_blocked.decision_code,
                "REJECTED_FULL_TEXT_NOT_AUTHORIZED",
            )
        finally:
            conn.close()

    def test_02_governance_license_fulltext_transcript_and_wikipedia_scope_reconciliation(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            ensure_ingestion_schema(conn)

            # Audit detects the 2 baseline Wikipedia scope inconsistencies without mutating on apply_fix=False
            audit_before = audit_and_reconcile_wikipedia_scope(conn, apply_fix=False)
            self.assertEqual(audit_before["inconsistent_count"], 2)
            self.assertEqual(len(audit_before["reconciled_resource_ids"]), 0)

            # Reconcile on isolated copy and verify audit log entries + non-eligible exclusion
            audit_fixed = audit_and_reconcile_wikipedia_scope(conn, apply_fix=True)
            self.assertEqual(len(audit_fixed["reconciled_resource_ids"]), 2)
            audit_after = audit_and_reconcile_wikipedia_scope(conn, apply_fix=False)
            self.assertEqual(audit_after["inconsistent_count"], 0)
            audit_ops = conn.execute(
                "SELECT * FROM ingestion_operations WHERE stage = 'WIKIPEDIA_LICENSE_RECONCILIATION'"
            ).fetchall()
            self.assertEqual(len(audit_ops), 2)
            non_wiki_reconciled = conn.execute(
                "SELECT COUNT(*) FROM resources WHERE source_id != 'SRC-WIKIPEDIA' AND permitted_storage_scope = 'WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED'"
            ).fetchone()[0]
            self.assertEqual(non_wiki_reconciled, 0)

            # Full-text extraction is blocked for Hinton PDFs and arXiv default non-exclusive license
            hinton_res = conn.execute(
                "SELECT resource_id FROM resources WHERE source_id = 'SRC-HINTON' LIMIT 1"
            ).fetchone()
            hinton_ft = evaluate_resource_ingestion_policy(
                conn,
                resource_id=str(hinton_res["resource_id"]),
                requested_action="EXTRACT_FULL_TEXT",
            )
            self.assertFalse(hinton_ft.is_permitted)
            self.assertEqual(hinton_ft.decision_code, "REJECTED_FULL_TEXT_NOT_AUTHORIZED")

            arxiv_res = conn.execute(
                "SELECT resource_id FROM resources WHERE source_id = 'SRC-ARXIV' LIMIT 1"
            ).fetchone()
            arxiv_ft = evaluate_resource_ingestion_policy(
                conn,
                resource_id=str(arxiv_res["resource_id"]),
                requested_action="EXTRACT_FULL_TEXT",
            )
            self.assertFalse(arxiv_ft.is_permitted)
            self.assertEqual(arxiv_ft.decision_code, "REJECTED_FULL_TEXT_NOT_AUTHORIZED")

            # 3Blue1Brown transcript blocked by default, permitted only with explicit attestation + transcript record
            b1b_res = conn.execute(
                "SELECT resource_id, canonical_url FROM resources WHERE source_id = 'SRC-3B1B' LIMIT 1"
            ).fetchone()
            b1b_rid = str(b1b_res["resource_id"])
            b1b_url = str(b1b_res["canonical_url"])
            tr_blocked = evaluate_resource_ingestion_policy(
                conn,
                resource_id=b1b_rid,
                requested_action="EXTRACT_TRANSCRIPT",
            )
            self.assertFalse(tr_blocked.is_permitted)
            self.assertEqual(tr_blocked.decision_code, "REJECTED_UNAUTHORIZED_TRANSCRIPT_STORAGE")
        finally:
            conn.close()

        # Register an explicit transcript authorization attestation via KnowledgeStore
        with KnowledgeStore(self.db_path) as store:
            att = store.record_transcript_attestation(
                resource_id=b1b_rid,
                authorization_basis="OPEN_LICENSE_VERIFIED",
                license_status="VERIFIED_PERMISSIVE",
                license_spdx="CC-BY-4.0",
                evidence_type="CREATOR_LICENSE_PAGE",
                evidence_reference="Grant Sanderson CC-BY-4.0 caption track license",
                source_evidence_url=b1b_url,
                evidence_sha256=sha256_hex(f"attestation:{b1b_rid}"),
                attribution_text="3Blue1Brown (Grant Sanderson) — CC BY 4.0",
                permits_metadata_indexing=True,
                permits_transcript_storage=True,
                permits_verbatim_excerpts=True,
                permits_ai_synthesis=True,
                permits_redistribution=False,
                reviewed_by="unit_test_curator",
                review_notes="Verified CC-BY-4.0 caption track for unit test",
                verification_status="VERIFIED",
            )
            tr_id = compute_transcript_id(b1b_rid, "en", "PRIMARY")
            store.conn.execute(
                """
                INSERT INTO transcripts (
                    transcript_id, resource_id, source_id, media_canonical_url,
                    language_bcp47, track_kind, origin_type, acquisition_method,
                    caption_format, speaker_attribution_quality, license_status,
                    license_spdx, authorization_basis, authorization_attestation_id,
                    permits_transcript_storage, permits_verbatim_excerpts, permits_ai_synthesis,
                    attribution_text, ingestion_state, transcript_storage_status,
                    segment_count, created_at, updated_at
                ) VALUES (
                    ?, ?, 'SRC-3B1B', ?,
                    'en', 'PRIMARY', 'AUTHOR_PROVIDED', 'OPEN_LICENSED_DIRECT_CAPTION',
                    'WEBVTT', 'VERIFIED_SINGLE_SPEAKER', 'VERIFIED_PERMISSIVE',
                    'CC-BY-4.0', 'OPEN_LICENSE_VERIFIED', ?,
                    1, 1, 1,
                    '3Blue1Brown CC BY 4.0', 'INGESTED', 'STORED_TIMED_SEGMENTS',
                    2, '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z'
                )
                """,
                (tr_id, b1b_rid, b1b_url, att["attestation_id"]),
            )
            store.conn.commit()

        conn2 = sqlite3.connect(str(self.db_path))
        conn2.row_factory = sqlite3.Row
        try:
            tr_permitted = evaluate_resource_ingestion_policy(
                conn2,
                resource_id=b1b_rid,
                requested_action="EXTRACT_TRANSCRIPT",
            )
            self.assertTrue(tr_permitted.is_permitted)
            self.assertEqual(tr_permitted.decision_code, "PERMITTED_AUTHORIZED_TRANSCRIPT")
            self.assertEqual(
                tr_permitted.permitted_chunk_layer,
                ChunkEpistemicLayer.TRANSCRIPT_AUTHORIZED_TEXT.value,
            )
        finally:
            conn2.close()

    # ------------------------------------------------------------------
    # 2. Multi-Source Adapter Collection Tests (Phase C)
    # ------------------------------------------------------------------

    def test_03_multi_source_adapters_wikipedia_hinton_bengio_3b1b_the_batch(self) -> None:
        wiki_json = json.dumps(
            {
                "batchcomplete": True,
                "query": {
                    "pages": [
                        {
                            "pageid": 12345,
                            "ns": 0,
                            "title": "Convolutional neural network",
                            "canonicalurl": "https://en.wikipedia.org/wiki/Convolutional_neural_network",
                            "extract": "In deep learning, a convolutional neural network (CNN) is a regularized type of feed-forward neural network that learns feature engineering via filter optimization.",
                            "revisions": [
                                {
                                    "revid": 99887766,
                                    "parentid": 99887700,
                                    "timestamp": "2026-02-01T10:00:00Z",
                                }
                            ],
                            "categories": [
                                {"title": "Category:Deep learning"},
                                {"title": "Category:Artificial neural networks"},
                            ],
                        }
                    ]
                },
            }
        )
        hinton_html = """
        <html><head><title>Distilling the Knowledge in a Neural Network</title>
        <meta name="description" content="We show that ensemble knowledge can be compressed into a single model using softened target probabilities at temperature T."/>
        </head><body>
        <h1>Distilling the Knowledge in a Neural Network</h1>
        <p>Authors: Geoffrey Hinton, Oriol Vinyals, Jeff Dean (2015)</p>
        <p>Abstract: A cumbersome ensemble model transfers dark knowledge to a small distilled model by raising softmax temperature.</p>
        <a href="https://www.cs.toronto.edu/~hinton/absps/distillation.pdf">PDF</a>
        </body></html>
        """
        bengio_html = """
        <html><head><title>System 2 Deep Learning and Reasoning - Yoshua Bengio</title>
        <meta property="article:published_time" content="2025-11-15"/>
        </head><body>
        <article>
        <h1>System 2 Deep Learning and Reasoning</h1>
        <p>Conscious processing enables out-of-distribution generalization, causal discovery, and sparse factor graphs in high-level cognition.</p>
        <a rel="tag" href="https://yoshuabengio.org/tag/causality/">Causality</a>
        <a href="https://arxiv.org/abs/2106.03762">GFlowNet Foundations</a>
        </article></body></html>
        """
        b1b_html = """
        <html><head><title>But what is a convolution? | 3Blue1Brown</title>
        <meta name="description" content="Visualizing discrete and continuous convolutions from moving averages to image processing kernels and FFTs."/>
        </head><body>
        <main>
        <h1>But what is a convolution?</h1>
        <p>Published: 2022-11-18. Chapter 5 of Neural Networks and Linear Algebra visual series.</p>
        <iframe src="https://www.youtube.com/embed/KuXjwB4LzSA"></iframe>
        </main></body></html>
        """
        batch_html = """
        <html><head><title>The Batch Issue 280: Agentic Workflows Mature</title>
        <meta property="article:published_time" content="2026-01-20"/>
        <meta name="description" content="Weekly AI news summary covering agent evaluation benchmarks, open weights releases, and inference scaling."/>
        </head><body><main><h1>The Batch Issue 280: Agentic Workflows Mature</h1></main></body></html>
        """

        session = _FakeHTTPSession(
            {
                "en.wikipedia.org/w/api.php": _FakeHTTPResponse(
                    wiki_json, content_type="application/json; charset=utf-8"
                ),
                "cs.toronto.edu/~hinton": _FakeHTTPResponse(hinton_html),
                "yoshuabengio.org": _FakeHTTPResponse(bengio_html),
                "3blue1brown.com": _FakeHTTPResponse(b1b_html),
                "deeplearning.ai/the-batch": _FakeHTTPResponse(batch_html),
            }
        )
        limiter = OriginRateLimiter(sleep_fn=lambda _: None)

        # Wikipedia adapter with include_extracts=True
        wiki_adapter = WikipediaSourceAdapter(session=session, rate_limiter=limiter)
        wiki_url = wiki_adapter.build_query_url("Convolutional_neural_network", include_extracts=True)
        self.assertIn("explaintext=1", wiki_url)
        wiki_policy = wiki_adapter.audit_policy()
        wiki_env = wiki_adapter.collect_candidate(
            SourceCandidate(source_id="SRC-WIKIPEDIA", url=wiki_url, title_hint="CNN"),
            policy=wiki_policy,
        )
        self.assertEqual(wiki_env.permitted_storage_scope, "WIKIPEDIA_OPEN_LICENSE_TEXT_PERMITTED")
        self.assertTrue(wiki_env.permits_full_text_storage)
        self.assertEqual(wiki_env.external_identifier, "wikipedia:en:pageid=12345;oldid=99887766")

        # Hinton adapter: extracts abstract/metadata and PDF link, blocks full-text storage
        hinton_adapter = HintonSourceAdapter(session=session, rate_limiter=limiter)
        hinton_policy = hinton_adapter.audit_policy()
        hinton_env = hinton_adapter.collect_candidate(
            SourceCandidate(
                source_id="SRC-HINTON",
                url="https://www.cs.toronto.edu/~hinton/distillation.html",
                title_hint="Distilling the Knowledge in a Neural Network",
            ),
            policy=hinton_policy,
        )
        self.assertFalse(hinton_env.permits_full_text_storage)
        self.assertEqual(hinton_env.permitted_storage_scope, "METADATA_ONLY")
        self.assertIn("temperature", (hinton_env.abstract_or_description or "").lower())

        # Bengio adapter: extracts post metadata, topic tags, outward links, blocks full-text redistribution
        bengio_adapter = BengioSourceAdapter(session=session, rate_limiter=limiter)
        bengio_policy = bengio_adapter.audit_policy()
        bengio_env = bengio_adapter.collect_candidate(
            SourceCandidate(
                source_id="SRC-BENGIO",
                url="https://yoshuabengio.org/2025/11/15/system-2-deep-learning/",
                title_hint="System 2 Deep Learning",
            ),
            policy=bengio_policy,
        )
        self.assertFalse(bengio_env.permits_full_text_storage)
        self.assertEqual(bengio_env.published_at, "2025-11-15")
        self.assertGreaterEqual(len(bengio_env.discovered_links), 1)

        # 3Blue1Brown adapter: separates lesson metadata from transcript availability
        b1b_adapter = ThreeBlueOneBrownAdapter(session=session, rate_limiter=limiter)
        b1b_policy = b1b_adapter.audit_policy()
        b1b_env = b1b_adapter.collect_candidate(
            SourceCandidate(
                source_id="SRC-3B1B",
                url="https://www.3blue1brown.com/lessons/convolutions",
                title_hint="But what is a convolution?",
            ),
            policy=b1b_policy,
        )
        self.assertFalse(b1b_env.permits_full_text_storage)
        self.assertIn("KuXjwB4LzSA", str(b1b_env.external_identifier or "") + str(b1b_env.official_links))
        self.assertEqual(b1b_env.text_storage_status, "metadata-only")
        self.assertFalse(b1b_env.source_specific_metadata.get("video_access_permits_transcript_storage", True))
        self.assertTrue(b1b_env.source_specific_metadata.get("transcript_authorization_required", False))

        # The Batch adapter: blocks when curation_approved=False, collects headline/summary only when True
        batch_unapproved = TheBatchSourceAdapter(
            session=session, rate_limiter=limiter, curation_approved=False
        )
        with self.assertRaises(PolicyBlockedError):
            batch_unapproved.collect_candidate(
                SourceCandidate(
                    source_id="SRC-THE-BATCH",
                    url="https://www.deeplearning.ai/the-batch/issue-280/",
                ),
            )

        batch_approved = TheBatchSourceAdapter(
            session=session, rate_limiter=limiter, curation_approved=True
        )
        batch_policy = batch_approved.audit_policy()
        batch_env = batch_approved.collect_candidate(
            SourceCandidate(
                source_id="SRC-THE-BATCH",
                url="https://www.deeplearning.ai/the-batch/issue-280/",
            ),
            policy=batch_policy,
        )
        self.assertFalse(batch_env.permits_full_text_storage)
        self.assertFalse(batch_env.permits_verbatim_excerpts)
        self.assertEqual(batch_env.permitted_storage_scope, "METADATA_ONLY")

    # ------------------------------------------------------------------
    # 3. Extraction, Normalization, Structured Knowledge & Chunking (Phases D & E)
    # ------------------------------------------------------------------

    def test_04_extraction_normalization_15_fields_and_structured_knowledge(self) -> None:
        raw_html = """
        <html>
        <head><title>Residual Neural Networks</title><script>console.log("ignore");</script></head>
        <body>
        <nav>Top Navigation Menu</nav>
        <main>
        <h1>Deep Residual Learning</h1>
        <p>A residual block is defined as a layer block that learns a residual mapping F(x) := H(x) - x with an identity shortcut connection (He et al., 2016).</p>
        <p>Residual Networks depend on batch normalization and chain rule backpropagation for deep optimization.</p>
        <p>$$y = \\mathcal{F}(x, \\{W_i\\}) + x$$</p>
        <h2>Methodology and Architecture</h2>
        <p>We propose a deep residual learning framework where stacked nonlinear layers fit a residual mapping trained using stochastic gradient descent.</p>
        <h2>Limitations and Failure Modes</h2>
        <p>A key limitation of very deep plain networks without residual shortcuts is optimization degradation and vanishing gradient bottlenecks.</p>
        </main>
        <footer>Copyright Footer</footer>
        </body></html>
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            ensure_ingestion_schema(conn)
            audit_and_reconcile_wikipedia_scope(conn, apply_fix=True)
            wiki_row = conn.execute(
                "SELECT resource_id FROM resources WHERE source_id = 'SRC-WIKIPEDIA' LIMIT 1"
            ).fetchone()
            rid = str(wiki_row["resource_id"])

            extractor = ContentExtractor()
            doc = extractor.extract_and_normalize(
                conn,
                resource_id=rid,
                raw_content=raw_html,
                extraction_format=ExtractionFormat.HTML,
                persist=True,
            )
            # Check all 15 mandatory normalized fields
            req_fields = doc.required_15_fields()
            expected_15_keys = {
                "resource_id",
                "version_id",
                "source_id",
                "title",
                "authors",
                "published_at",
                "canonical_url",
                "resource_type",
                "content_origin",
                "epistemic_classification",
                "license_status",
                "permitted_storage_scope",
                "extraction_method",
                "extraction_timestamp",
                "content_hash",
                "extraction_status",
            }
            self.assertTrue(expected_15_keys.issubset(set(req_fields.keys())))
            self.assertEqual(doc.extraction_status, "EXTRACTED_AND_NORMALIZED")
            self.assertNotIn("Top Navigation Menu", doc.normalized_text)
            self.assertNotIn("console.log", doc.normalized_text)
            self.assertIn("$$y = \\mathcal{F}(x, \\{W_i\\}) + x$$", doc.normalized_text)

            # Verify content-origin invariant prevents promoting abstract/metadata to PRIMARY_SOURCE_TEXT
            arxiv_row = conn.execute(
                "SELECT resource_id FROM resources WHERE source_id = 'SRC-ARXIV' LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(arxiv_row)
            with self.assertRaises(IngestionPolicyViolationError):
                extractor.extract_and_normalize(
                    conn,
                    resource_id=str(arxiv_row["resource_id"]),  # arXiv abstract-only resource
                    content_origin_override=NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT,
                    persist=False,
                    dry_run=True,
                )

            # Extract structured knowledge elements
            elements = extract_structured_knowledge(doc, conn=conn, persist=True)
            element_types = {el.element_type for el in elements}
            self.assertTrue(element_types.issubset(set(STRUCTURED_KNOWLEDGE_ELEMENT_TYPES)))
            self.assertIn("definitions", element_types)
            self.assertIn("equations", element_types)
            self.assertIn("methods", element_types)
            self.assertIn("limitations", element_types)
            self.assertIn("concept_relationships", element_types)
            self.assertIn("citations", element_types)

            # Content-aware chunking preserves equations and computes deterministic chunk IDs
            chunker = ContentAwareChunker(max_chars=360, overlap_chars=40)
            chunks = chunker.chunk_document(doc, elements=elements)
            self.assertGreaterEqual(len(chunks), 2)
            for ch in chunks:
                self.assertEqual(ch.epistemic_layer, ChunkEpistemicLayer.PRIMARY_SOURCE_TEXT.value)
                self.assertEqual(ch.content_origin, NormalizedContentOrigin.ORIGINAL_SOURCE_TEXT.value)
                expected_id = compute_chunk_id(
                    ch.resource_id, ch.version_id or "v0", ch.content_origin, ch.chunk_index
                )
                self.assertEqual(ch.chunk_id, expected_id)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 4. Embeddings & 6-Check Index Consistency Verification (Phase F)
    # ------------------------------------------------------------------

    def test_05_index_consistency_detects_all_six_anomaly_categories(self) -> None:
        # Baseline copy starts 100% consistent
        clean_report = verify_index_consistency(
            sqlite_db_path=self.db_path,
            chroma_dir=self.chroma_dir,
            collection_name=DEFAULT_CHROMA_COLLECTION,
        )
        self.assertTrue(clean_report.is_consistent)
        self.assertEqual(clean_report.sqlite_active_chunks, 18)
        self.assertEqual(clean_report.chroma_vector_count, 18)
        self.assertFalse(clean_report.is_semantic_embedding)

        # Inject controlled anomalies into isolated SQLite & Chroma copies to verify all 6 checks:
        conn = sqlite3.connect(str(self.db_path))
        try:
            sample_row = conn.execute(
                "SELECT * FROM knowledge_chunks WHERE lifecycle_state = 'ACTIVE' LIMIT 1"
            ).fetchone()
            cols = [d[1] for d in conn.execute("PRAGMA table_info(knowledge_chunks)").fetchall()]
            row_dict = dict(zip(cols, sample_row))

            # Insert a SQLite active chunk that has no Chroma vector, wrong dimension (64), stale version, and mismatched provider
            row_dict["chunk_id"] = "chk_missing_and_mismatched_01_a"
            row_dict["vector_id"] = "chk_missing_and_mismatched_01"
            row_dict["chunk_index"] = 99
            row_dict["embedding_dimensions"] = 64
            row_dict["embedding_provider"] = "mismatched_provider"
            row_dict["version_id"] = "ver_stale_000"
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO knowledge_chunks ({', '.join(cols)}) VALUES ({placeholders})",
                [row_dict[c] for c in cols],
            )
            # Also insert a second chunk whose fallback chunk_id collides with the effective vector_id above to trigger duplicate_vector_ids
            dup_dict = dict(row_dict)
            dup_dict["chunk_id"] = "chk_missing_and_mismatched_01"
            dup_dict["vector_id"] = ""
            dup_dict["vector_collection"] = "secondary_collection_dup"
            dup_dict["chunk_index"] = 100
            conn.execute(
                f"INSERT INTO knowledge_chunks ({', '.join(cols)}) VALUES ({placeholders})",
                [dup_dict[c] for c in cols],
            )
            conn.commit()
        finally:
            conn.close()

        # Anomaly 2 (orphaned_vector): add a vector in Chroma that is not in SQLite
        vs = ChromaPersistentVectorStore(
            self.chroma_dir,
            collection_name=DEFAULT_CHROMA_COLLECTION,
            create_if_missing=False,
        )
        vs._collection.upsert(
            ids=["chk_orphan_vector_99"],
            embeddings=[[0.1] * 128],
            documents=["Orphan vector document"],
            metadatas=[
                {
                    "chunk_id": "chk_orphan_vector_99",
                    "resource_id": "res_orphan",
                    "version_id": "ver_orphan",
                    "source_id": "SRC-WIKIPEDIA",
                    "embedding_provider": "local_deterministic",
                    "embedding_model": "token-hash-bow-v1",
                    "embedding_dimensions": 128,
                }
            ],
        )

        anomaly_report = verify_index_consistency(
            sqlite_db_path=self.db_path,
            chroma_dir=self.chroma_dir,
            collection_name=DEFAULT_CHROMA_COLLECTION,
        )
        self.assertFalse(anomaly_report.is_consistent)
        self.assertIn("chk_missing_and_mismatched_01", anomaly_report.missing_vectors)
        self.assertIn("chk_orphan_vector_99", anomaly_report.orphaned_vectors)
        self.assertIn("chk_missing_and_mismatched_01", anomaly_report.duplicate_vector_ids)
        self.assertTrue(len(anomaly_report.dimension_mismatches) >= 1)
        self.assertTrue(len(anomaly_report.stale_content_versions) >= 1)
        self.assertTrue(len(anomaly_report.provider_model_inconsistencies) >= 1)

    # ------------------------------------------------------------------
    # 5. End-to-End Ingestion Demo, Agent Integration, Control Plane & CLI (Phases G, H, I, K)
    # ------------------------------------------------------------------

    def test_06_reproducible_end_to_end_ingestion_demo_and_agent_grounding(self) -> None:
        demo_work_dir = self.work_dir / "e2e_demo"
        summary = run_reproducible_ingestion_demo(
            baseline_db_path=self.db_path,
            baseline_chroma_dir=self.chroma_dir,
            work_dir=demo_work_dir,
        )
        self.assertEqual(summary["step_1_selected_resource"]["title"], "Multilayer perceptron")
        self.assertFalse(summary["step_1_selected_resource"]["was_collected_before"])
        self.assertTrue(summary["step_2_policy_validation"]["policy_decision"]["is_permitted"])
        self.assertEqual(summary["step_2_policy_validation"]["wikipedia_scope_reconciled_count"], 2)
        self.assertEqual(summary["step_3_authorized_collection"]["status"], "COLLECTED")
        self.assertGreaterEqual(
            summary["step_6_structured_knowledge_and_chunks"]["structured_element_count"], 4
        )
        self.assertGreaterEqual(summary["step_6_structured_knowledge_and_chunks"]["chunk_count"], 2)

        idx_ver = summary["step_7_and_8_embedding_and_index_verification"]
        self.assertEqual(idx_ver["before_active_chunks"], 18)
        self.assertGreater(idx_ver["after_active_chunks"], 18)
        self.assertEqual(idx_ver["after_active_chunks"], idx_ver["after_indexed_vectors"])
        self.assertTrue(idx_ver["index_consistency"]["is_consistent"])

        # Step 9 & 10: Retrieval and Teaching Agent citation of newly ingested Multilayer perceptron resource
        new_rid = summary["step_1_selected_resource"]["resource_id"]
        self.assertEqual(summary["step_9_retrieval_with_citation"]["top_hit_resource_id"], new_rid)
        self.assertIn(new_rid, summary["step_10_teaching_agent_explanation"]["cited_resource_ids"])

    def test_07_control_plane_observability_and_cli_commands(self) -> None:
        # 1. Verify API and Control Plane include all 11 lifecycle metrics and 5 epistemic chunk layers
        api = LocalResearchLibraryAPI(db_path=self.db_path)
        cp_resp = api.handle_request("GET", "/api/v1/platform/sources-control-plane")
        self.assertEqual(cp_resp.status.value, 200)
        cp_body = cp_resp.body
        metrics = cp_body["ingestion_pipeline_metrics"]
        for key in (
            "registered_sources",
            "discovered_candidates",
            "reviewed_candidates",
            "approved_resources",
            "collected_resources",
            "verified_documents",
            "extracted_documents",
            "authorized_transcripts",
            "active_knowledge_chunks",
            "indexed_vectors",
            "failed_or_deferred_resources",
        ):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["discovered_candidates"], 181)
        self.assertEqual(metrics["approved_resources"], 60)
        self.assertEqual(metrics["collected_resources"], 12)
        self.assertEqual(metrics["extracted_documents"], 6)
        self.assertEqual(metrics["active_knowledge_chunks"], 18)

        layers = cp_body["epistemic_chunk_layers"]
        self.assertEqual(layers["PRIMARY_SOURCE_TEXT"], 7)
        self.assertEqual(layers["AUTHORIZED_ABSTRACT_OR_DESCRIPTION"], 11)
        self.assertEqual(layers["TRANSCRIPT_AUTHORIZED_TEXT"], 0)
        self.assertEqual(layers["EDUCATIONAL_EXPLANATION"], 0)
        self.assertEqual(layers["NEWS_AND_COMMENTARY"], 0)

        # 2. Verify /api/v1/ingestion/status and /api/v1/ingestion/verify-index endpoints
        st_resp = api.handle_request("GET", "/api/v1/ingestion/status")
        self.assertEqual(st_resp.status.value, 200)
        vi_resp = api.handle_request("GET", "/api/v1/ingestion/verify-index")
        self.assertEqual(vi_resp.status.value, 200)
        self.assertTrue(vi_resp.body["is_consistent"])

        # 3. Verify CLI commands (status, list-eligible, preview-resource, verify-index, failures)
        for argv in (
            ["--db", str(self.db_path), "--chroma-dir", str(self.chroma_dir), "status"],
            ["--db", str(self.db_path), "--chroma-dir", str(self.chroma_dir), "list-eligible", "--source-id", "SRC-WIKIPEDIA"],
            ["--db", str(self.db_path), "--chroma-dir", str(self.chroma_dir), "preview-resource", "--resource-id", "1"],
            ["--db", str(self.db_path), "--chroma-dir", str(self.chroma_dir), "verify-index"],
            ["--db", str(self.db_path), "--chroma-dir", str(self.chroma_dir), "failures"],
        ):
            exit_code = ingestion_cli_main(argv)
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
