from __future__ import annotations

import hashlib
from http import HTTPStatus
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from knowledge_store import (
    LATEST_SCHEMA_VERSION,
    init_db,
    sha256_hex,
)
from src.ai_research_learning import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    LocalResearchLibraryAPI,
    create_http_server,
)


def _forbid_network(*args, **kwargs):
    raise AssertionError("Live network access is strictly forbidden during API operations!")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalResearchLibraryAPITests(unittest.TestCase):
    """Comprehensive tests for the read-only Local Research Library API (`v1`)."""

    SECRET_SEGMENT_MARKER = "SUPER_SECRET_VERBATIM_SEGMENT_MARKER_DO_NOT_LEAK_99182"
    SECRET_SYNTHESIS_MARKER = "SUPER_SECRET_AI_SYNTHESIS_BODY_DO_NOT_LEAK_77410"

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp_dir.name) / "test_library.sqlite"

        store = init_db(self.db_path)
        try:
            # 1. Seed site policy
            with store.transaction() as cur:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO site_policies (
                        origin, robots_url, status, http_status, crawl_delay_seconds,
                        sitemap_urls, evidence, checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "https://arxiv.org",
                        "https://arxiv.org/robots.txt",
                        "ALLOWED",
                        200,
                        15.0,
                        "[]",
                        "User-agent: *\nCrawl-delay: 15",
                        "2026-03-01T00:00:00Z",
                    ),
                )

            # 2. Seed three distinct resources across sources and modalities
            r1 = store.upsert_resource(
                source_id="SRC-ARXIV",
                manifest_id=None,
                canonical_url="https://arxiv.org/abs/1706.03762",
                title="Attention Is All You Need",
                authors="Ashish Vaswani; Noam Shazeer; Niki Parmar",
                published_at="2017-06-12",
                resource_type="Research paper (preprint)",
                media_modality="TEXT_PDF_HTML",
                domain_scope="arxiv.org",
                abstract_or_description=(
                    "The dominant sequence transduction models are based on complex recurrent or "
                    "convolutional neural networks that include an encoder and a decoder."
                ),
                topic_tags="transformers, self-attention, sequence-modeling",
                external_identifier="arXiv:1706.03762",
                collection_decision="COLLECT",
                verification_status="VERIFIED",
                robots_sitemap_status="ALLOWED",
                rights_status="OPEN_ACCESS",
                license_status="VERIFIED_PERMISSIVE",
                license_spdx="CC-BY-4.0",
                permitted_storage_scope="FULL_TEXT_AND_EXCERPTS",
                permits_full_text_storage=1,
                permits_verbatim_excerpts=1,
                legal_uncertainty_status="CLEARED_PERMISSIVE",
                text_storage_status="abstract-and-metadata-stored",
                acquisition_method="ARXIV_ATOM_API",
                actor="unit_test",
            )
            self.res_1_id = str(r1["resource_id"])

            # Create a second version of r1 so version history has multiple entries
            store.upsert_resource(
                source_id="SRC-ARXIV",
                manifest_id=None,
                canonical_url="https://arxiv.org/abs/1706.03762",
                title="Attention Is All You Need",
                authors="Ashish Vaswani; Noam Shazeer; Niki Parmar",
                published_at="2017-06-12",
                resource_type="Research paper (preprint)",
                media_modality="TEXT_PDF_HTML",
                domain_scope="arxiv.org",
                abstract_or_description=(
                    "We propose a new simple network architecture, the Transformer, based solely on "
                    "attention mechanisms, dispensing with recurrence and convolutions entirely."
                ),
                topic_tags="transformers, self-attention, sequence-modeling, deep-learning",
                external_identifier="arXiv:1706.03762",
                collection_decision="COLLECT",
                verification_status="VERIFIED",
                robots_sitemap_status="ALLOWED",
                rights_status="OPEN_ACCESS",
                license_status="VERIFIED_PERMISSIVE",
                license_spdx="CC-BY-4.0",
                permitted_storage_scope="FULL_TEXT_AND_EXCERPTS",
                permits_full_text_storage=1,
                permits_verbatim_excerpts=1,
                legal_uncertainty_status="CLEARED_PERMISSIVE",
                text_storage_status="abstract-and-metadata-stored",
                acquisition_method="ARXIV_ATOM_API",
                actor="unit_test",
            )

            r2 = store.upsert_resource(
                source_id="SRC-3B1B",
                manifest_id=None,
                canonical_url="https://www.3blue1brown.com/lessons/backpropagation-calculus",
                title="Backpropagation Calculus & Gradient Flow 100%_exact",
                authors="Grant Sanderson",
                published_at="2024-11-03",
                resource_type="Educational video lesson",
                media_modality="VIDEO_WITH_TRANSCRIPT",
                domain_scope="www.3blue1brown.com",
                abstract_or_description="Visualizing chain rule derivatives in neural networks.",
                topic_tags="backpropagation, calculus, neural-networks",
                external_identifier="3b1b:backprop-calculus",
                collection_decision="COLLECT",
                verification_status="VERIFIED",
                robots_sitemap_status="ALLOWED",
                rights_status="OPEN_LICENSE",
                license_status="VERIFIED_PERMISSIVE",
                license_spdx="CC-BY-4.0",
                permitted_storage_scope="FULL_TEXT_AND_EXCERPTS",
                permits_full_text_storage=1,
                permits_verbatim_excerpts=1,
                legal_uncertainty_status="CLEARED_PERMISSIVE",
                text_storage_status="metadata-only-not-yet-collected",
                acquisition_method="CURATED_MANIFEST_SEED",
                actor="unit_test",
            )
            self.res_2_id = str(r2["resource_id"])

            r3 = store.upsert_resource(
                source_id="SRC-HINTON",
                manifest_id=None,
                canonical_url="https://www.cs.toronto.edu/~hinton/absps/naturedeepreview.html",
                title="Deep Learning Review Lecture Notes",
                authors="Geoffrey Hinton; Yann LeCun; Yoshua Bengio",
                published_at="2015-05-28",
                resource_type="Course lecture notes",
                media_modality="TEXT_HTML",
                domain_scope="www.cs.toronto.edu",
                abstract_or_description="Supervised learning, generalized linear models, and representation learning.",
                topic_tags="machine-learning, regression, deep-learning",
                external_identifier="HINTON-NOTES-2015",
                collection_decision="SKIP",
                verification_status="PENDING_REVIEW",
                robots_sitemap_status="ALLOWED",
                rights_status="ALL_RIGHTS_RESERVED",
                license_status="ALL_RIGHTS_RESERVED",
                license_spdx=None,
                permitted_storage_scope="METADATA_ONLY",
                permits_full_text_storage=0,
                permits_verbatim_excerpts=0,
                legal_uncertainty_status="UNVERIFIED_REQUIRES_HUMAN_REVIEW",
                text_storage_status="metadata-only-copyright-restricted",
                acquisition_method="CURATED_MANIFEST_SEED",
                actor="unit_test",
            )
            self.res_3_id = str(r3["resource_id"])

            # 3. Record verified open-license attestation and import transcript + segments + synthesis on res_2
            att = store.record_transcript_attestation(
                resource_id=self.res_2_id,
                language_bcp47="en",
                track_kind="PRIMARY",
                authorization_basis="OPEN_LICENSE_VERIFIED",
                license_status="VERIFIED_PERMISSIVE",
                license_spdx="CC-BY-4.0",
                evidence_type="CREATOR_LICENSE_PAGE",
                evidence_reference="Verified CC-BY-4.0 license statement on 3Blue1Brown lesson page",
                source_evidence_url="https://www.3blue1brown.com/lessons/backpropagation-calculus",
                evidence_sha256=sha256_hex("evidence:res2:cc-by-4.0"),
                attribution_text="Grant Sanderson / 3Blue1Brown (CC-BY-4.0)",
                permits_metadata_indexing=True,
                permits_transcript_storage=True,
                permits_verbatim_excerpts=True,
                permits_ai_synthesis=True,
                permits_redistribution=False,
                reviewed_by="unit_test_curator",
                review_notes="Approved for local transcript storage in unit test",
                verification_status="VERIFIED",
                expires_at="2030-01-01T00:00:00Z",
            )
            self.attestation_id = str(att["attestation_id"])

            vtt_content = (
                "WEBVTT\n\n"
                "00:00:00.000 --> 00:00:04.500\n"
                f"<v Grant Sanderson>Welcome to backpropagation calculus. {self.SECRET_SEGMENT_MARKER}</v>\n\n"
                "00:00:04.500 --> 00:00:09.200\n"
                f"<v Grant Sanderson>Here we apply the chain rule layer by layer. {self.SECRET_SEGMENT_MARKER}</v>\n"
            )
            imported = store.import_local_transcript(
                resource_id=self.res_2_id,
                raw_content=vtt_content,
                language_bcp47="en",
                track_kind="PRIMARY",
                origin_type="AUTHOR_PROVIDED",
                acquisition_method="OPEN_LICENSED_DIRECT_CAPTION",
                caption_format="WEBVTT",
                license_status="VERIFIED_PERMISSIVE",
                license_spdx="CC-BY-4.0",
                authorization_basis="OPEN_LICENSE_VERIFIED",
                attribution_text="Grant Sanderson / 3Blue1Brown (CC-BY-4.0)",
                permits_transcript_storage=True,
                permits_verbatim_excerpts=True,
                permits_ai_synthesis=True,
                permits_redistribution=False,
                attestation_id=self.attestation_id,
                actor="unit_test",
            )
            self.transcript_1_id = str(imported.transcript_id)
            self.transcript_1_version_id = str(imported.transcript_version_id)

            # Record an AI synthesis containing a secret marker to prove API never leaks synthesis bodies
            store.record_transcript_synthesis(
                transcript_id=self.transcript_1_id,
                transcript_version_id=self.transcript_1_version_id,
                synthesis_type="STRUCTURED_SUMMARY",
                generator_type="HUMAN_CURATED",
                content_markdown=f"Summary of derivatives: {self.SECRET_SYNTHESIS_MARKER}",
                is_original_transformative=True,
                contains_verbatim_transcript=False,
                review_status="APPROVED",
            )

            # 4. Also create a blocked metadata-only transcript (no attestation, no segments) on res_3
            blocked = store.import_local_transcript(
                resource_id=self.res_3_id,
                raw_content="1\n00:00:00,000 --> 00:00:03,000\nRestricted lecture content.\n",
                language_bcp47="en",
                track_kind="PRIMARY",
                origin_type="PUBLISHER_CLOSED_CAPTION",
                acquisition_method="FIRST_PARTY_CURATED_IMPORT",
                caption_format="SRT",
                license_status="ALL_RIGHTS_RESERVED",
                authorization_basis="NONE",
                attribution_text="Geoffrey Hinton (All Rights Reserved)",
                permits_transcript_storage=False,
                actor="unit_test",
            )
            self.transcript_2_id = str(blocked.transcript_id)
        finally:
            store.close()

        self.api = LocalResearchLibraryAPI(db_path=self.db_path)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    # ------------------------------------------------------------------
    # 1. Health Endpoint (`GET /healthz`)
    # ------------------------------------------------------------------

    def test_01_healthz_reports_ok_and_schema_status(self) -> None:
        resp = self.api.handle_request("GET", "/healthz")
        self.assertEqual(resp.status, HTTPStatus.OK)
        self.assertEqual(resp.body["status"], "ok")
        self.assertEqual(resp.body["api_version"], "v1")
        self.assertTrue(resp.body["read_only"])
        self.assertEqual(resp.body["network_access"], "disabled")
        db_info = resp.body["database"]
        self.assertTrue(db_info["exists"])
        self.assertTrue(db_info["initialized"])
        self.assertEqual(db_info["schema_version"], LATEST_SCHEMA_VERSION)
        self.assertEqual(db_info["latest_supported_schema_version"], LATEST_SCHEMA_VERSION)

        # Also test against an uninitialized path
        missing_api = LocalResearchLibraryAPI(db_path=Path(self._tmp_dir.name) / "nonexistent.sqlite")
        missing_resp = missing_api.handle_request("GET", "/healthz")
        self.assertEqual(missing_resp.status, HTTPStatus.OK)
        self.assertFalse(missing_resp.body["database"]["exists"])
        self.assertFalse(missing_resp.body["database"]["initialized"])
        self.assertEqual(missing_resp.body["database"]["schema_version"], 0)

    # ------------------------------------------------------------------
    # 2. Sources Endpoint (`GET /api/v1/sources`)
    # ------------------------------------------------------------------

    def test_02_sources_returns_registered_sources_and_policy_info(self) -> None:
        resp = self.api.handle_request("GET", "/api/v1/sources")
        self.assertEqual(resp.status, HTTPStatus.OK)
        self.assertGreaterEqual(resp.body["total_count"], 5)
        self.assertEqual(resp.body["total_count"], resp.body["returned_count"])

        by_id = {item["source_id"]: item for item in resp.body["items"]}
        self.assertIn("SRC-ARXIV", by_id)
        self.assertIn("SRC-3B1B", by_id)

        arxiv = by_id["SRC-ARXIV"]
        self.assertEqual(arxiv["source_category"], "RESEARCH_ARCHIVE_API")
        self.assertEqual(arxiv["adapter_type"], "ARXIV_ATOM_API")
        self.assertEqual(arxiv["resource_count"], 1)
        self.assertIsNotNone(arxiv["site_policy"])
        self.assertEqual(arxiv["site_policy"]["status"], "ALLOWED")
        self.assertEqual(arxiv["site_policy"]["crawl_delay_seconds"], 15.0)

        b1b = by_id["SRC-3B1B"]
        self.assertEqual(b1b["resource_count"], 1)
        self.assertEqual(b1b["transcript_count"], 1)

        # Filter by source_category and only_complete
        filtered = self.api.handle_request(
            "GET", "/api/v1/sources?source_category=RESEARCH_ARCHIVE_API&only_complete=true"
        )
        self.assertEqual(filtered.status, HTTPStatus.OK)
        self.assertTrue(
            all(s["source_category"] == "RESEARCH_ARCHIVE_API" for s in filtered.body["items"])
        )
        self.assertTrue(all(s["metadata_complete"] is True for s in filtered.body["items"]))

    # ------------------------------------------------------------------
    # 3. Resources Endpoints (`GET /api/v1/resources` & `GET /api/v1/resources/{id}`)
    # ------------------------------------------------------------------

    def test_03_resources_list_pagination_and_filters(self) -> None:
        resp = self.api.handle_request("GET", "/api/v1/resources")
        self.assertEqual(resp.status, HTTPStatus.OK)
        self.assertEqual(resp.body["total_count"], 3)
        self.assertEqual(resp.body["returned_count"], 3)
        self.assertFalse(resp.body["has_more"])
        ordered_ids = [item["resource_id"] for item in resp.body["items"]]
        self.assertEqual(ordered_ids, sorted([self.res_1_id, self.res_2_id, self.res_3_id]))

        # Pagination: limit=1, offset=1
        page_resp = self.api.handle_request("GET", "/api/v1/resources?limit=1&offset=1")
        self.assertEqual(page_resp.status, HTTPStatus.OK)
        self.assertEqual(page_resp.body["total_count"], 3)
        self.assertEqual(page_resp.body["returned_count"], 1)
        self.assertTrue(page_resp.body["has_more"])
        self.assertEqual(page_resp.body["items"][0]["resource_id"], ordered_ids[1])

        # Filter by source_id
        src_resp = self.api.handle_request("GET", "/api/v1/resources?source_id=SRC-ARXIV")
        self.assertEqual(src_resp.status, HTTPStatus.OK)
        self.assertEqual(src_resp.body["total_count"], 1)
        self.assertEqual(src_resp.body["items"][0]["resource_id"], self.res_1_id)

        # Filter by media_modality and collection_decision
        mod_resp = self.api.handle_request(
            "GET",
            "/api/v1/resources?media_modality=VIDEO_WITH_TRANSCRIPT&collection_decision=COLLECT",
        )
        self.assertEqual(mod_resp.status, HTTPStatus.OK)
        self.assertEqual(mod_resp.body["total_count"], 1)
        self.assertEqual(mod_resp.body["items"][0]["resource_id"], self.res_2_id)

    def test_04_resource_detail_returns_metadata_versions_and_transcript_summary(self) -> None:
        # Check res_1 (has 2 versions)
        r1_resp = self.api.handle_request("GET", f"/api/v1/resources/{self.res_1_id}")
        self.assertEqual(r1_resp.status, HTTPStatus.OK)
        r1_obj = r1_resp.body["resource"]
        self.assertEqual(r1_obj["resource_id"], self.res_1_id)
        self.assertEqual(r1_obj["title"], "Attention Is All You Need")
        self.assertIn("Ashish Vaswani", r1_obj["authors_list"])
        self.assertIn("transformers", r1_obj["topic_tags_list"])
        self.assertEqual(r1_obj["version_count"], 2)
        self.assertEqual(len(r1_obj["versions"]), 2)
        self.assertEqual([v["version_number"] for v in r1_obj["versions"]], [1, 2])
        self.assertEqual(r1_obj["transcripts_summary"], [])

        # Check res_2 (has 1 transcript summary, metadata only)
        r2_resp = self.api.handle_request("GET", f"/api/v1/resources/{self.res_2_id}")
        self.assertEqual(r2_resp.status, HTTPStatus.OK)
        r2_obj = r2_resp.body["resource"]
        self.assertEqual(len(r2_obj["transcripts_summary"]), 1)
        tr_sum = r2_obj["transcripts_summary"][0]
        self.assertEqual(tr_sum["transcript_id"], self.transcript_1_id)
        self.assertEqual(tr_sum["ingestion_state"], "INGESTED")
        self.assertEqual(tr_sum["segment_count"], 2)
        raw_json = json.dumps(r2_resp.body)
        self.assertNotIn(self.SECRET_SEGMENT_MARKER, raw_json)
        self.assertNotIn(self.SECRET_SYNTHESIS_MARKER, raw_json)

    # ------------------------------------------------------------------
    # 4. Search Endpoint (`GET /api/v1/search?q=...`)
    # ------------------------------------------------------------------

    def test_05_search_deterministic_matches_filters_and_empty_results(self) -> None:
        # Match by title/tags ("Transformer")
        s1 = self.api.handle_request("GET", "/api/v1/search?q=Transformer")
        self.assertEqual(s1.status, HTTPStatus.OK)
        self.assertEqual(s1.body["search_mode"], "metadata_lexical")
        self.assertEqual(s1.body["total_count"], 1)
        self.assertEqual(s1.body["items"][0]["resource_id"], self.res_1_id)
        self.assertIn("abstract_or_description", s1.body["items"][0]["matched_fields"])

        # Match by author ("Sanderson")
        s2 = self.api.handle_request("GET", "/api/v1/search?q=Sanderson")
        self.assertEqual(s2.status, HTTPStatus.OK)
        self.assertEqual(s2.body["total_count"], 1)
        self.assertEqual(s2.body["items"][0]["resource_id"], self.res_2_id)
        self.assertIn("authors", s2.body["items"][0]["matched_fields"])

        # Match by external_identifier ("arXiv:1706.03762")
        s3 = self.api.handle_request("GET", "/api/v1/search?q=arXiv%3A1706.03762")
        self.assertEqual(s3.status, HTTPStatus.OK)
        self.assertEqual(s3.body["total_count"], 1)
        self.assertEqual(s3.body["items"][0]["resource_id"], self.res_1_id)

        # Escaped SQL wildcard characters (`100%_exact`) only match the resource with literal `100%_exact`
        s_wild = self.api.handle_request("GET", "/api/v1/search?q=100%25_exact")
        self.assertEqual(s_wild.status, HTTPStatus.OK)
        self.assertEqual(s_wild.body["total_count"], 1)
        self.assertEqual(s_wild.body["items"][0]["resource_id"], self.res_2_id)

        # Search with source_id filter
        s_filtered = self.api.handle_request(
            "GET", "/api/v1/search?q=learning&source_id=SRC-HINTON"
        )
        self.assertEqual(s_filtered.status, HTTPStatus.OK)
        self.assertEqual(s_filtered.body["total_count"], 1)
        self.assertEqual(s_filtered.body["items"][0]["resource_id"], self.res_3_id)

        # Search with no match returns 200 and empty items list
        s_empty = self.api.handle_request("GET", "/api/v1/search?q=nonexistent_quantum_biology_xyz")
        self.assertEqual(s_empty.status, HTTPStatus.OK)
        self.assertEqual(s_empty.body["total_count"], 0)
        self.assertEqual(s_empty.body["returned_count"], 0)
        self.assertEqual(s_empty.body["items"], [])
        self.assertFalse(s_empty.body["has_more"])

    # ------------------------------------------------------------------
    # 5. Transcripts Endpoints & Strict Content Protection
    # ------------------------------------------------------------------

    def test_06_transcripts_endpoints_return_metadata_only_and_never_expose_protected_text(self) -> None:
        list_resp = self.api.handle_request("GET", "/api/v1/transcripts")
        self.assertEqual(list_resp.status, HTTPStatus.OK)
        self.assertEqual(list_resp.body["total_count"], 2)
        self.assertEqual(
            list_resp.body["content_disclosure_policy"],
            "METADATA_ONLY_NO_SEGMENTS_OR_SYNTHESES",
        )
        raw_list_json = json.dumps(list_resp.body)
        self.assertNotIn(self.SECRET_SEGMENT_MARKER, raw_list_json)
        self.assertNotIn(self.SECRET_SYNTHESIS_MARKER, raw_list_json)

        # Filter by ingestion_state=INGESTED
        idx_resp = self.api.handle_request("GET", "/api/v1/transcripts?ingestion_state=INGESTED")
        self.assertEqual(idx_resp.status, HTTPStatus.OK)
        self.assertEqual(idx_resp.body["total_count"], 1)
        self.assertEqual(idx_resp.body["items"][0]["transcript_id"], self.transcript_1_id)

        # Filter by ingestion_state=BLOCKED_BY_POLICY
        blk_resp = self.api.handle_request(
            "GET", "/api/v1/transcripts?ingestion_state=BLOCKED_BY_POLICY"
        )
        self.assertEqual(blk_resp.status, HTTPStatus.OK)
        self.assertEqual(blk_resp.body["total_count"], 1)
        self.assertEqual(blk_resp.body["items"][0]["transcript_id"], self.transcript_2_id)

        # Inspect single transcript detail (`GET /api/v1/transcripts/{id}`)
        detail_resp = self.api.handle_request(
            "GET", f"/api/v1/transcripts/{self.transcript_1_id}"
        )
        self.assertEqual(detail_resp.status, HTTPStatus.OK)
        self.assertEqual(
            detail_resp.body["content_disclosure_policy"],
            "METADATA_ONLY_NO_SEGMENTS_OR_SYNTHESES",
        )
        tr = detail_resp.body["transcript"]
        self.assertEqual(tr["transcript_id"], self.transcript_1_id)
        self.assertEqual(tr["resource_id"], self.res_2_id)
        self.assertEqual(tr["ingestion_state"], "INGESTED")
        self.assertEqual(tr["transcript_storage_status"], "STORED_TIMED_SEGMENTS")
        self.assertEqual(tr["segment_count"], 2)
        self.assertTrue(tr["permits_transcript_storage"])
        self.assertTrue(tr["permits_verbatim_excerpts"])
        self.assertTrue(tr["permits_ai_synthesis"])
        self.assertFalse(tr["permits_redistribution"])
        self.assertEqual(len(tr["versions"]), 1)
        self.assertEqual(tr["versions"][0]["transcript_version_id"], self.transcript_1_version_id)
        self.assertEqual(tr["versions"][0]["status"], "ACTIVE")
        self.assertEqual(tr["versions"][0]["segment_count"], 2)
        self.assertIsNotNone(tr["authorization_attestation"])
        self.assertEqual(
            tr["authorization_attestation"]["attestation_id"], self.attestation_id
        )

        # Verify neither segments nor syntheses nor secret markers appear anywhere in the response
        raw_detail_json = json.dumps(detail_resp.body)
        self.assertNotIn(self.SECRET_SEGMENT_MARKER, raw_detail_json)
        self.assertNotIn(self.SECRET_SYNTHESIS_MARKER, raw_detail_json)
        self.assertNotIn("segments", tr)
        self.assertNotIn("syntheses", tr)

    def test_07_transcript_natural_expiry_projected_in_readonly_mode_without_db_mutation(self) -> None:
        # Advance the API's read-only evaluation clock past `2030-01-01T00:00:00Z`
        expired_api = LocalResearchLibraryAPI(
            db_path=self.db_path,
            clock=lambda: "2031-06-01T00:00:00Z",
        )
        before_hash = _file_sha256(self.db_path)

        detail_resp = expired_api.handle_request(
            "GET", f"/api/v1/transcripts/{self.transcript_1_id}"
        )
        self.assertEqual(detail_resp.status, HTTPStatus.OK)
        tr = detail_resp.body["transcript"]
        self.assertEqual(tr["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
        self.assertEqual(tr["transcript_storage_status"], "TOMBSTONED_ON_TAKEDOWN")
        self.assertFalse(tr["permits_transcript_storage"])
        self.assertFalse(tr["permits_verbatim_excerpts"])
        self.assertFalse(tr["permits_ai_synthesis"])
        self.assertEqual(tr["segment_count"], 0)
        self.assertIsNone(tr["latest_transcript_version_id"])
        self.assertEqual(tr["versions"][0]["status"], "TOMBSTONED")
        self.assertEqual(tr["authorization_attestation"]["verification_status"], "EXPIRED")
        self.assertTrue(tr["authorization_attestation"]["is_expired"])

        # Filter by WITHDRAWN_OR_TOMBSTONED should include the expired transcript
        list_resp = expired_api.handle_request(
            "GET", "/api/v1/transcripts?ingestion_state=WITHDRAWN_OR_TOMBSTONED"
        )
        self.assertEqual(list_resp.status, HTTPStatus.OK)
        self.assertEqual(list_resp.body["total_count"], 1)
        self.assertEqual(list_resp.body["items"][0]["transcript_id"], self.transcript_1_id)

        after_hash = _file_sha256(self.db_path)
        self.assertEqual(before_hash, after_hash)

    # ------------------------------------------------------------------
    # 6. Error Handling (`400`, `404`, `405`, `503`)
    # ------------------------------------------------------------------

    def test_08_error_responses_for_invalid_params_missing_ids_and_disallowed_methods(self) -> None:
        # 405 Method Not Allowed on POST / PUT / DELETE
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            resp = self.api.handle_request(method, "/api/v1/resources")
            self.assertEqual(resp.status, HTTPStatus.METHOD_NOT_ALLOWED)
            self.assertEqual(resp.headers.get("Allow"), "GET")
            self.assertEqual(resp.body["error_code"], "METHOD_NOT_ALLOWED")
            self.assertEqual(resp.body["status"], 405)

        # 404 Unknown route
        r_unknown = self.api.handle_request("GET", "/api/v1/unknown_route")
        self.assertEqual(r_unknown.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(r_unknown.body["error_code"], "ENDPOINT_NOT_FOUND")

        # 404 Non-existent sub-routes such as `/api/v1/transcripts/{id}/segments`
        r_seg_route = self.api.handle_request(
            "GET", f"/api/v1/transcripts/{self.transcript_1_id}/segments"
        )
        self.assertEqual(r_seg_route.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(r_seg_route.body["error_code"], "ENDPOINT_NOT_FOUND")

        # 400 Invalid resource_id format
        r_bad_res = self.api.handle_request("GET", "/api/v1/resources/not-a-valid-id")
        self.assertEqual(r_bad_res.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(r_bad_res.body["error_code"], "INVALID_IDENTIFIER")

        # 404 Valid resource_id format, but not in DB
        r_missing_res = self.api.handle_request("GET", "/api/v1/resources/res_0000000000000000")
        self.assertEqual(r_missing_res.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(r_missing_res.body["error_code"], "RESOURCE_NOT_FOUND")

        # 400 Invalid transcript_id format
        r_bad_tr = self.api.handle_request("GET", "/api/v1/transcripts/tr_invalid")
        self.assertEqual(r_bad_tr.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(r_bad_tr.body["error_code"], "INVALID_IDENTIFIER")

        # 404 Valid transcript_id format, but not in DB
        r_missing_tr = self.api.handle_request("GET", "/api/v1/transcripts/tr_0000000000000000")
        self.assertEqual(r_missing_tr.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(r_missing_tr.body["error_code"], "TRANSCRIPT_NOT_FOUND")

        # 400 Missing or blank search query `q`
        r_no_q = self.api.handle_request("GET", "/api/v1/search")
        self.assertEqual(r_no_q.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(r_no_q.body["error_code"], "MISSING_SEARCH_QUERY")

        r_blank_q = self.api.handle_request("GET", "/api/v1/search?q=+++")
        self.assertEqual(r_blank_q.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(r_blank_q.body["error_code"], "MISSING_SEARCH_QUERY")

        # 400 Invalid pagination (`limit=0`, `limit=999`, `offset=-1`, `limit=abc`)
        for bad_qs in ("limit=0", "limit=999", "offset=-1", "limit=abc", "offset=xyz"):
            r_page = self.api.handle_request("GET", f"/api/v1/resources?{bad_qs}")
            self.assertEqual(r_page.status, HTTPStatus.BAD_REQUEST, msg=bad_qs)
            self.assertEqual(r_page.body["error_code"], "INVALID_PAGINATION", msg=bad_qs)

        # 400 Unknown query parameter & duplicate query parameter
        r_unk_param = self.api.handle_request("GET", "/api/v1/resources?foo=bar")
        self.assertEqual(r_unk_param.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(r_unk_param.body["error_code"], "INVALID_QUERY_PARAMETER")

        r_dup_param = self.api.handle_request("GET", "/api/v1/resources?limit=5&limit=10")
        self.assertEqual(r_dup_param.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(r_dup_param.body["error_code"], "DUPLICATE_QUERY_PARAMETER")

    # ------------------------------------------------------------------
    # 7. Read-Only Guarantee, Zero Live Network Calls, Fresh Seed DB & Loopback Server
    # ------------------------------------------------------------------

    def test_09_api_reads_do_not_mutate_db_and_make_zero_network_calls(self) -> None:
        before_hash = _file_sha256(self.db_path)

        with (
            patch.object(socket, "socket", side_effect=_forbid_network),
            patch.object(socket, "create_connection", side_effect=_forbid_network),
            patch.object(urllib.request, "urlopen", side_effect=_forbid_network),
        ):
            for target in (
                "/healthz",
                "/api/v1/sources",
                "/api/v1/resources?limit=10&offset=0",
                f"/api/v1/resources/{self.res_1_id}",
                f"/api/v1/resources/{self.res_2_id}",
                "/api/v1/search?q=Attention",
                "/api/v1/transcripts",
                f"/api/v1/transcripts/{self.transcript_1_id}",
            ):
                resp = self.api.handle_request("GET", target)
                self.assertEqual(resp.status, HTTPStatus.OK, msg=target)

        after_hash = _file_sha256(self.db_path)
        self.assertEqual(before_hash, after_hash)

        # Also verify SQLite connection enforces read-only query_only mode at the engine level
        conn, should_close = self.api._open_readonly_connection()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM resources")
        finally:
            if should_close:
                conn.close()

    def test_10_fresh_migrated_database_and_live_loopback_http_server(self) -> None:
        # 1. Verify against a brand-new empty migrated database and after seed manifest import
        fresh_db_path = Path(self._tmp_dir.name) / "fresh_empty.sqlite"
        fresh_store = init_db(fresh_db_path)
        try:
            fresh_api = LocalResearchLibraryAPI(db_path=fresh_db_path)
            self.assertEqual(fresh_api.handle_request("GET", "/healthz").status, HTTPStatus.OK)
            self.assertEqual(
                fresh_api.handle_request("GET", "/api/v1/resources").body["total_count"], 0
            )
            self.assertEqual(
                fresh_api.handle_request("GET", "/api/v1/transcripts").body["total_count"], 0
            )
            self.assertEqual(
                fresh_api.handle_request("GET", "/api/v1/search?q=anything").body["total_count"], 0
            )

            # Import the repository's canonical seed_urls.json into the fresh database
            summary = fresh_store.import_seed_manifest()
            self.assertGreater(summary.imported_count, 0)
        finally:
            fresh_store.close()

        seeded_res = fresh_api.handle_request("GET", "/api/v1/resources?limit=5&offset=0")
        self.assertEqual(seeded_res.status, HTTPStatus.OK)
        self.assertGreater(seeded_res.body["total_count"], 0)
        first_seeded_id = seeded_res.body["items"][0]["resource_id"]
        seeded_detail = fresh_api.handle_request("GET", f"/api/v1/resources/{first_seeded_id}")
        self.assertEqual(seeded_detail.status, HTTPStatus.OK)
        self.assertEqual(seeded_detail.body["resource"]["resource_id"], first_seeded_id)
        seeded_search = fresh_api.handle_request("GET", "/api/v1/search?q=Hinton")
        self.assertEqual(seeded_search.status, HTTPStatus.OK)
        self.assertGreater(seeded_search.body["total_count"], 0)

        # 2. Verify live HTTP transport over 127.0.0.1 loopback server
        self.assertEqual(DEFAULT_API_HOST, "127.0.0.1")
        self.assertEqual(DEFAULT_API_PORT, 8000)

        server, _ = create_http_server(db_path=self.db_path, host="127.0.0.1", port=0)
        host, port = server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.request("GET", "/healthz")
                res = conn.getresponse()
                body = json.loads(res.read().decode("utf-8"))
                self.assertEqual(res.status, 200)
                self.assertEqual(body["status"], "ok")

                conn.request("GET", "/api/v1/search?q=Attention")
                res2 = conn.getresponse()
                body2 = json.loads(res2.read().decode("utf-8"))
                self.assertEqual(res2.status, 200)
                self.assertEqual(body2["total_count"], 1)

                conn.request("POST", "/api/v1/resources")
                res3 = conn.getresponse()
                body3 = json.loads(res3.read().decode("utf-8"))
                self.assertEqual(res3.status, 405)
                self.assertEqual(res3.getheader("Allow"), "GET")
                self.assertEqual(body3["error_code"], "METHOD_NOT_ALLOWED")
            finally:
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
