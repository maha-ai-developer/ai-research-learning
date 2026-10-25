import contextlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import knowledge_store
from knowledge_store import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    KnowledgeStore,
    TranscriptPolicyViolationError,
    TranscriptSegmentCue,
    compute_resource_id,
    compute_segment_deep_link_url,
    compute_transcript_attestation_id,
    compute_transcript_id,
    compute_transcript_segment_id,
    compute_transcript_segment_link_id,
    compute_transcript_synthesis_id,
    compute_transcript_version_id,
    init_db,
    normalize_segment_text,
    parse_plain_text_transcript,
    parse_srt_transcript,
    parse_timestamp_to_ms,
    parse_webvtt_transcript,
    sha256_hex,
    validate_transcript_cues,
    validate_transcript_state_transition,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "transcripts"
VTT_FIXTURE = FIXTURES_DIR / "synthetic_lecture_en.vtt"
SRT_FIXTURE = FIXTURES_DIR / "synthetic_interview_fr.srt"
TXT_FIXTURE = FIXTURES_DIR / "synthetic_notes_en.txt"


def _forbid_network(*args, **kwargs):
    raise AssertionError("Network access is strictly forbidden during offline transcript operations!")


class TranscriptDataModelAndWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._socket_patcher = patch.object(socket, "socket", side_effect=_forbid_network)
        self._create_conn_patcher = patch.object(
            socket, "create_connection", side_effect=_forbid_network
        )
        self._socket_patcher.start()
        self._create_conn_patcher.start()

    def tearDown(self):
        self._create_conn_patcher.stop()
        self._socket_patcher.stop()

    def _seed_sample_resource(
        self,
        store: KnowledgeStore,
        *,
        canonical_url: str = "https://www.3blue1brown.com/lessons/synthetic-test-lecture",
        title: str = "Synthetic Lecture on Backpropagation (Test Fixture)",
    ) -> str:
        upserted = store.upsert_resource(
            source_id="SRC-3B1B",
            manifest_id=None,
            canonical_url=canonical_url,
            title=title,
            authors="Synthetic Test Author",
            published_at="2026-01-15",
            resource_type="Educational video lesson",
            media_modality="VIDEO_WITH_TRANSCRIPT",
            domain_scope="www.3blue1brown.com",
            collection_decision="COLLECT",
            license_status="VERIFIED_PERMISSIVE",
            license_spdx="CC-BY-4.0",
            permitted_storage_scope="FULL_TEXT_AND_EXCERPTS",
            permits_full_text_storage=1,
            permits_verbatim_excerpts=1,
            legal_uncertainty_status="CLEARED_PERMISSIVE",
            text_storage_status="metadata-only-not-yet-collected",
            acquisition_method="TEST_FIXTURE_SETUP",
            actor="unit_test",
        )
        return str(upserted["resource_id"])

    def _create_open_license_attestation(
        self,
        store: KnowledgeStore,
        resource_id: str,
        *,
        license_spdx: str = "CC-BY-4.0",
        authorization_basis: str = "OPEN_LICENSE_VERIFIED",
        permits_transcript_storage: bool = True,
        permits_verbatim_excerpts: bool = True,
        permits_ai_synthesis: bool = True,
        permits_redistribution: bool = False,
        language_bcp47: str | None = None,
        track_kind: str | None = None,
        transcript_id: str | None = None,
        verification_status: str = "VERIFIED",
        expires_at: str | None = None,
    ) -> dict:
        return store.record_transcript_attestation(
            resource_id=resource_id,
            transcript_id=transcript_id,
            language_bcp47=language_bcp47,
            track_kind=track_kind,
            authorization_basis=authorization_basis,
            license_status="VERIFIED_PERMISSIVE",
            license_spdx=license_spdx,
            evidence_type="CREATOR_LICENSE_PAGE",
            evidence_reference=f"Verified {license_spdx} caption license page for {resource_id}",
            source_evidence_url="https://www.3blue1brown.com/lessons/synthetic-test-lecture",
            evidence_sha256=sha256_hex(f"evidence:{resource_id}:{license_spdx}"),
            attribution_text=f"Synthetic Test Fixture ({license_spdx})",
            permits_metadata_indexing=True,
            permits_transcript_storage=permits_transcript_storage,
            permits_verbatim_excerpts=permits_verbatim_excerpts,
            permits_ai_synthesis=permits_ai_synthesis,
            permits_redistribution=permits_redistribution,
            reviewed_by="unit_test_curator",
            review_notes="Verified open license in unit test fixture",
            verification_status=verification_status,
            expires_at=expires_at,
        )

    def test_01_forward_only_migration_from_v5_and_v6_to_v7_preserves_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Part A: Migrate from v5 -> v7
            db_v5_path = Path(tmp) / "v5_to_v7.sqlite"
            conn = sqlite3.connect(str(db_v5_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            for version, name, sql_marker, migration_fn in MIGRATIONS[:5]:
                checksum = sha256_hex(f"{version}:{name}:{sql_marker}")
                with conn:
                    migration_fn(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (version, name, checksum, "2026-01-01T00:00:00+00:00"),
                    )
                    conn.execute(f"PRAGMA user_version = {version}")
            conn.close()

            with KnowledgeStore(db_path=db_v5_path, auto_initialize=False) as store:
                newly_applied = store.initialize()
                self.assertEqual(newly_applied, [6, 7, 8])
                self.assertEqual(store.get_schema_version(), 8)
                self.assertEqual(LATEST_SCHEMA_VERSION, 8)

                sources = store.list_sources(only_complete=True)
                self.assertEqual(len(sources), 5)

                tables = {
                    r[0]
                    for r in store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for expected_table in (
                    "transcripts",
                    "transcript_versions",
                    "transcript_segments",
                    "transcript_syntheses",
                    "transcript_segment_links",
                    "transcript_authorization_attestations",
                ):
                    self.assertIn(expected_table, tables)

                self.assertEqual(store.initialize(), [])

            # Part B: Migrate from v6 (containing a pre-existing authorized transcript) -> v7
            db_v6_path = Path(tmp) / "v6_to_v7.sqlite"
            conn6 = sqlite3.connect(str(db_v6_path))
            conn6.row_factory = sqlite3.Row
            conn6.execute("PRAGMA foreign_keys = ON")
            conn6.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            for version, name, sql_marker, migration_fn in MIGRATIONS[:6]:
                checksum = sha256_hex(f"{version}:{name}:{sql_marker}")
                with conn6:
                    migration_fn(conn6)
                    conn6.execute(
                        "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (version, name, checksum, "2026-01-01T00:00:00+00:00"),
                    )
                    conn6.execute(f"PRAGMA user_version = {version}")

            # Insert a resource and a v6 authorized transcript prior to applying migration 7
            store_v6 = KnowledgeStore(conn=conn6, auto_initialize=False)
            res_id = self._seed_sample_resource(
                store_v6,
                canonical_url="https://www.3blue1brown.com/lessons/v6-legacy",
                title="Legacy v6 Resource",
            )
            tr_id = compute_transcript_id(res_id, "en", "PRIMARY")
            with conn6:
                conn6.execute(
                    """
                    INSERT INTO transcripts (
                        transcript_id, resource_id, source_id, media_canonical_url,
                        language_bcp47, track_kind, origin_type, acquisition_method,
                        caption_format, speaker_attribution_quality, license_status,
                        license_spdx, authorization_basis,
                        permits_transcript_storage, permits_verbatim_excerpts, permits_ai_synthesis,
                        attribution_text, ingestion_state, transcript_storage_status,
                        segment_count, created_at, updated_at
                    ) VALUES (
                        ?, ?, 'SRC-3B1B', 'https://www.3blue1brown.com/lessons/v6-legacy',
                        'en', 'PRIMARY', 'AUTHOR_PROVIDED', 'OPEN_LICENSED_DIRECT_CAPTION',
                        'WEBVTT', 'VERIFIED_SINGLE_SPEAKER', 'VERIFIED_PERMISSIVE',
                        'CC-BY-4.0', 'OPEN_LICENSE_VERIFIED',
                        1, 1, 1,
                        'Legacy v6 Attribution', 'INGESTED', 'STORED_TIMED_SEGMENTS',
                        0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                    )
                    """,
                    (tr_id, res_id),
                )
            conn6.close()

            with KnowledgeStore(db_path=db_v6_path, auto_initialize=False) as store6:
                newly_applied_6 = store6.initialize()
                self.assertEqual(newly_applied_6, [7, 8])
                self.assertEqual(store6.get_schema_version(), 8)
                migrated_tr = store6.get_transcript(tr_id)
                self.assertIsNotNone(migrated_tr["authorization_attestation_id"])
                backfilled_att = store6.get_transcript_attestation(
                    migrated_tr["authorization_attestation_id"]
                )
                self.assertIsNotNone(backfilled_att)
                self.assertEqual(backfilled_att["verification_status"], "VERIFIED")
                self.assertEqual(backfilled_att["permits_transcript_storage"], 1)

    def test_02_deterministic_transcript_attestation_version_and_segment_ids(self):
        resource_id = compute_resource_id(
            "SRC-3B1B", "https://www.3blue1brown.com/lessons/synthetic-test-lecture"
        )
        t_id_1 = compute_transcript_id(resource_id, "en", "PRIMARY")
        t_id_2 = compute_transcript_id(resource_id, "EN", "primary")
        self.assertEqual(t_id_1, t_id_2)
        self.assertTrue(t_id_1.startswith("tr_"))
        self.assertEqual(len(t_id_1), 19)

        att_id_1 = compute_transcript_attestation_id(
            resource_id,
            "OPEN_LICENSE_VERIFIED",
            "CREATOR_LICENSE_PAGE",
            "https://www.3blue1brown.com/lessons/synthetic-test-lecture",
            language_bcp47="en",
            track_kind="PRIMARY",
        )
        att_id_2 = compute_transcript_attestation_id(
            resource_id,
            "open_license_verified",
            "creator_license_page",
            "https://www.3blue1brown.com/lessons/synthetic-test-lecture",
            language_bcp47="EN",
            track_kind="primary",
        )
        self.assertEqual(att_id_1, att_id_2)
        self.assertTrue(att_id_1.startswith("tatt_"))
        self.assertEqual(len(att_id_1), 21)

        t_id_fr = compute_transcript_id(resource_id, "fr", "TRANSLATION")
        self.assertNotEqual(t_id_1, t_id_fr)

        raw_sha = sha256_hex("raw content")
        norm_sha = sha256_hex("normalized content")
        v_id_1 = compute_transcript_version_id(
            t_id_1, 1, raw_sha, norm_sha, recorded_date="2026-10-25T00:00:00+00:00"
        )
        v_id_2 = compute_transcript_version_id(
            t_id_1, 1, raw_sha, norm_sha, recorded_date="2026-10-25"
        )
        self.assertEqual(v_id_1, v_id_2)
        self.assertRegex(v_id_1, r"^tver_20261025_[0-9a-f]{8}_v1$")

        seg_v1 = compute_transcript_segment_id(t_id_1, 0, version_number=1)
        self.assertEqual(seg_v1, f"seg_{t_id_1}_00000")
        seg_v2 = compute_transcript_segment_id(t_id_1, 0, version_number=2)
        self.assertEqual(seg_v2, f"seg_{t_id_1}_v2_00000")

        deep_link = compute_segment_deep_link_url(
            "https://www.3blue1brown.com/lessons/synthetic-test-lecture", 9250
        )
        self.assertEqual(
            deep_link, "https://www.3blue1brown.com/lessons/synthetic-test-lecture#t=9s"
        )

    def test_03_valid_import_of_webvtt_srt_and_plain_text_under_verified_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "store.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                att_cc_by = self._create_open_license_attestation(
                    store,
                    res_id,
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                )

                # 1. WebVTT import (PRIMARY, en)
                vtt_res = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    origin_type="AUTHOR_PROVIDED",
                    acquisition_method="OPEN_LICENSED_DIRECT_CAPTION",
                    speaker_attribution_quality="VERIFIED_MULTI_SPEAKER",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic Test Fixture (CC-BY-4.0)",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att_cc_by["attestation_id"],
                    duration_ms=20000,
                )
                self.assertEqual(vtt_res.status, "imported")
                self.assertEqual(vtt_res.ingestion_state, "INGESTED")
                self.assertEqual(vtt_res.transcript_storage_status, "STORED_TIMED_SEGMENTS")
                self.assertTrue(vtt_res.permits_transcript_storage)
                self.assertTrue(vtt_res.permits_verbatim_excerpts)
                self.assertEqual(vtt_res.authorization_attestation_id, att_cc_by["attestation_id"])
                self.assertEqual(vtt_res.stored_segments, 3)

                segments = store.get_transcript_segments(vtt_res.transcript_id)
                self.assertEqual(len(segments), 3)
                self.assertEqual(segments[0]["speaker_label"], "Instructor")
                self.assertIn("<v Instructor>", segments[0]["original_segment_text"])
                self.assertEqual(
                    segments[0]["normalized_segment_text"],
                    "Welcome to this synthetic test lecture on backpropagation & chain rule derivatives.",
                )
                self.assertEqual(
                    segments[2]["normalized_segment_text"],
                    "How does gradient clipping affect exploding gradients when ||g||_2 > tau?",
                )
                self.assertTrue(segments[2]["deep_link_url"].endswith("#t=9s"))

                # 2. SRT import (TRANSLATION, fr)
                srt_res = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=SRT_FIXTURE,
                    language_bcp47="fr",
                    track_kind="TRANSLATION",
                    origin_type="COMMUNITY_CONTRIBUTED",
                    acquisition_method="OPEN_LICENSED_DIRECT_CAPTION",
                    speaker_attribution_quality="VERIFIED_MULTI_SPEAKER",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic French Translation Fixture (CC-BY-4.0)",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att_cc_by["attestation_id"],
                )
                self.assertEqual(srt_res.status, "imported")
                self.assertEqual(srt_res.ingestion_state, "INGESTED")
                self.assertEqual(srt_res.transcript_storage_status, "STORED_TIMED_SEGMENTS")
                self.assertEqual(srt_res.stored_segments, 2)
                fr_segments = store.get_transcript_segments(srt_res.transcript_id)
                self.assertEqual(fr_segments[0]["speaker_label"], "Moderator")
                self.assertEqual(fr_segments[1]["start_ms"], 3800)
                self.assertEqual(fr_segments[1]["end_ms"], 8400)

                # 3. Plain text import (DESCRIPTIVE_AUDIO_TEXT, en) under PUBLIC_DOMAIN_VERIFIED / CC0-1.0
                att_pd = self._create_open_license_attestation(
                    store,
                    res_id,
                    license_spdx="CC0-1.0",
                    authorization_basis="PUBLIC_DOMAIN_VERIFIED",
                    language_bcp47="en",
                    track_kind="DESCRIPTIVE_AUDIO_TEXT",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    permits_redistribution=True,
                )
                txt_res = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=TXT_FIXTURE,
                    language_bcp47="en",
                    track_kind="DESCRIPTIVE_AUDIO_TEXT",
                    origin_type="MANUAL_CURATED_IMPORT",
                    acquisition_method="FIRST_PARTY_CURATED_IMPORT",
                    speaker_attribution_quality="VERIFIED_SINGLE_SPEAKER",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC0-1.0",
                    authorization_basis="PUBLIC_DOMAIN_VERIFIED",
                    attribution_text="Synthetic Plain Text Notes (CC0-1.0)",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    permits_redistribution=True,
                    attestation_id=att_pd["attestation_id"],
                )
                self.assertEqual(txt_res.status, "imported")
                self.assertEqual(txt_res.ingestion_state, "INGESTED")
                self.assertEqual(txt_res.transcript_storage_status, "STORED_PLAIN_TEXT")
                self.assertTrue(txt_res.permits_redistribution)
                self.assertEqual(txt_res.stored_segments, 2)

                # Verify synthesis and segment link persistence
                syn = store.record_transcript_synthesis(
                    transcript_id=vtt_res.transcript_id,
                    synthesis_type="STRUCTURED_SUMMARY",
                    generator_type="HUMAN_CURATED",
                    content_markdown="## Summary\nCovers scalar loss formulation and gradient clipping.",
                    referenced_time_ranges=[{"start_ms": 0, "end_ms": 14000}],
                    review_status="APPROVED",
                )
                self.assertTrue(syn["synthesis_id"].startswith("tsyn_"))
                self.assertEqual(len(store.get_transcript_syntheses(vtt_res.transcript_id)), 1)

                lnk = store.record_transcript_segment_link(
                    segment_id=segments[1]["segment_id"],
                    target_type="EQUATION",
                    target_id="eq_mse_loss",
                    relation_type="INTRODUCES",
                    confidence=0.95,
                    created_by="unit_test_curator",
                )
                self.assertTrue(lnk["link_id"].startswith("tslnk_"))
                self.assertEqual(
                    len(store.get_transcript_segment_links(segments[1]["segment_id"])), 1
                )

    def test_04_policy_gate_blocks_caller_flags_without_attestation_and_invalid_attestations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "policy_gate.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

                # Scenario 1: Caller passes permits_transcript_storage=True + VERIFIED_PERMISSIVE + CC-BY-4.0
                # WITHOUT an attestation -> MUST fail closed and store 0 segments
                blocked_no_att = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    license_spdx="CC-BY-4.0",
                    attribution_text="Caller-Claimed License Without Attestation",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                )
                self.assertEqual(blocked_no_att.status, "blocked")
                self.assertEqual(blocked_no_att.ingestion_state, "BLOCKED_BY_POLICY")
                self.assertEqual(
                    blocked_no_att.transcript_storage_status, "BLOCKED_NO_AUTHORIZATION"
                )
                self.assertFalse(blocked_no_att.permits_transcript_storage)
                self.assertFalse(blocked_no_att.permits_verbatim_excerpts)
                self.assertIsNone(blocked_no_att.authorization_attestation_id)
                self.assertIsNone(blocked_no_att.transcript_version_id)
                self.assertEqual(blocked_no_att.stored_segments, 0)
                self.assertIn("Missing auditable authorization attestation", blocked_no_att.reason)

                # Scenario 2a: Non-existent attestation_id fails closed
                blocked_missing_id = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    license_spdx="CC-BY-4.0",
                    attribution_text="Missing Attestation ID",
                    permits_transcript_storage=True,
                    attestation_id="tatt_0000000000000000",
                )
                self.assertEqual(blocked_missing_id.status, "blocked")
                self.assertEqual(blocked_missing_id.stored_segments, 0)

                # Scenario 2b: UNKNOWN, PENDING_REVIEW, REJECTED, and CONTRADICTORY attestations all fail closed
                for status_label in ("UNKNOWN", "PENDING_REVIEW", "REJECTED"):
                    att_non_verified = store.record_transcript_attestation(
                        resource_id=res_id,
                        language_bcp47="fr",
                        track_kind="TRANSLATION",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        evidence_type="CREATOR_LICENSE_PAGE",
                        evidence_reference=f"Non-verified test ({status_label})",
                        source_evidence_url="https://www.3blue1brown.com/lessons/synthetic-test-lecture",
                        permits_transcript_storage=True,
                        verification_status=status_label,
                    )
                    res_blocked = store.import_local_transcript_file(
                        resource_id=res_id,
                        transcript_path=SRT_FIXTURE,
                        language_bcp47="fr",
                        track_kind="TRANSLATION",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        attribution_text="Non-verified attestation import",
                        permits_transcript_storage=True,
                        attestation_id=att_non_verified["attestation_id"],
                    )
                    self.assertEqual(res_blocked.status, "blocked")
                    self.assertEqual(res_blocked.stored_segments, 0)

                # Contradictory attestation (e.g., OPEN_LICENSE_VERIFIED with non-permissive SPDX or ALL_RIGHTS_RESERVED with permits_transcript_storage=True)
                att_contradictory = store.record_transcript_attestation(
                    resource_id=res_id,
                    language_bcp47="es",
                    track_kind="TRANSLATION",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    license_status="ALL_RIGHTS_RESERVED",
                    license_spdx="CC-BY-NC-ND-4.0",
                    evidence_type="CREATOR_LICENSE_PAGE",
                    evidence_reference="Contradictory license claim",
                    source_evidence_url="https://www.3blue1brown.com/lessons/synthetic-test-lecture",
                    permits_transcript_storage=True,
                    verification_status="VERIFIED",
                )
                self.assertEqual(att_contradictory["verification_status"], "CONTRADICTORY")
                self.assertEqual(att_contradictory["permits_transcript_storage"], 0)
                blocked_contradictory = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=SRT_FIXTURE,
                    language_bcp47="es",
                    track_kind="TRANSLATION",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Contradictory attestation import",
                    permits_transcript_storage=True,
                    attestation_id=att_contradictory["attestation_id"],
                )
                self.assertEqual(blocked_contradictory.status, "blocked")
                self.assertEqual(blocked_contradictory.stored_segments, 0)

                # Scenario 2c: METADATA_AND_LINK_ONLY
                meta_only = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=TXT_FIXTURE,
                    language_bcp47="de",
                    track_kind="TRANSLATION",
                    acquisition_method="METADATA_AND_LINK_ONLY",
                    license_status="ALL_RIGHTS_RESERVED",
                    authorization_basis="NONE",
                    attribution_text="External Lecture Metadata Only",
                    permits_transcript_storage=False,
                )
                self.assertEqual(meta_only.status, "metadata_only")
                self.assertEqual(meta_only.ingestion_state, "METADATA_ONLY_LINKED")
                self.assertEqual(meta_only.transcript_storage_status, "METADATA_AND_LINK_ONLY")
                self.assertEqual(meta_only.stored_segments, 0)

                # Verify ZERO rows exist in transcript_versions and transcript_segments across the entire DB
                ver_count = store.conn.execute(
                    "SELECT COUNT(*) FROM transcript_versions"
                ).fetchone()[0]
                seg_count = store.conn.execute(
                    "SELECT COUNT(*) FROM transcript_segments"
                ).fetchone()[0]
                self.assertEqual(ver_count, 0)
                self.assertEqual(seg_count, 0)

                # Verify provenance_events never contain raw transcript text
                prov_rows = store.conn.execute(
                    "SELECT details_json FROM provenance_events"
                ).fetchall()
                for row in prov_rows:
                    meta_str = row[0] or ""
                    self.assertNotIn("backpropagation", meta_str)
                    self.assertNotIn("entropie", meta_str)

                # Verify SQLite triggers directly reject inserting into transcript_versions for a blocked transcript
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcript_versions (
                            transcript_version_id, transcript_id, version_number,
                            raw_content_sha256, normalized_text_sha256, normalizer_version,
                            segment_count, word_count, char_count, status, created_at
                        ) VALUES ('tver_20261025_deadbeef_v1', ?, 1, 'a', 'b', 'norm-v1.0', 1, 1, 1, 'ACTIVE', '2026-10-25T00:00:00+00:00')
                        """,
                        (blocked_no_att.transcript_id,),
                    )

    def test_05_validation_failures_for_corrupt_cues_and_missing_parent_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "validation.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                att = self._create_open_license_attestation(store, res_id)

                # Missing parent resource
                with self.assertRaisesRegex(ValueError, "Parent resource_id does not exist"):
                    store.import_local_transcript_file(
                        resource_id="res_0000000000000000",
                        transcript_path=VTT_FIXTURE,
                        attribution_text="Missing resource test",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        permits_transcript_storage=True,
                        attestation_id=att["attestation_id"],
                    )

                # Empty transcript content (TV-01)
                with self.assertRaisesRegex(ValueError, "TV-01"):
                    store.import_local_transcript(
                        resource_id=res_id,
                        raw_content="WEBVTT\n\n",
                        caption_format="WEBVTT",
                        attribution_text="Empty WebVTT",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        permits_transcript_storage=True,
                        attestation_id=att["attestation_id"],
                    )

                # end_ms < start_ms (TV-02)
                bad_interval_vtt = (
                    "WEBVTT\n\n00:00:10.000 --> 00:00:05.000\nInvalid interval segment.\n"
                )
                with self.assertRaisesRegex(ValueError, "TV-02"):
                    store.import_local_transcript(
                        resource_id=res_id,
                        raw_content=bad_interval_vtt,
                        caption_format="WEBVTT",
                        attribution_text="Bad interval WebVTT",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        permits_transcript_storage=True,
                        attestation_id=att["attestation_id"],
                    )

                # Non-monotonic start_ms across segments (TV-02)
                non_monotonic_vtt = (
                    "WEBVTT\n\n"
                    "00:00:08.000 --> 00:00:12.000\nFirst segment.\n\n"
                    "00:00:03.000 --> 00:00:06.000\nOut of order second segment.\n"
                )
                with self.assertRaisesRegex(ValueError, "TV-02"):
                    store.import_local_transcript(
                        resource_id=res_id,
                        raw_content=non_monotonic_vtt,
                        caption_format="WEBVTT",
                        attribution_text="Non-monotonic WebVTT",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        permits_transcript_storage=True,
                        attestation_id=att["attestation_id"],
                    )

                # Negative timestamp / empty segment text via validate_transcript_cues (TV-02, TV-04)
                with self.assertRaisesRegex(ValueError, "TV-02"):
                    validate_transcript_cues(
                        [
                            TranscriptSegmentCue(
                                sequence_index=0,
                                start_ms=-100,
                                end_ms=1000,
                                original_segment_text="Negative start",
                                normalized_segment_text="Negative start",
                            )
                        ],
                        language_bcp47="en",
                        attribution_text="Test",
                        license_status="VERIFIED_PERMISSIVE",
                    )

                with self.assertRaisesRegex(ValueError, "TV-04"):
                    validate_transcript_cues(
                        [
                            TranscriptSegmentCue(
                                sequence_index=0,
                                start_ms=0,
                                end_ms=1000,
                                original_segment_text="<v Speaker></v>",
                                normalized_segment_text="   ",
                            )
                        ],
                        language_bcp47="en",
                        attribution_text="Test",
                        license_status="VERIFIED_PERMISSIVE",
                    )

                # Duration sanity check (TV-03)
                with self.assertRaisesRegex(ValueError, "TV-03"):
                    store.import_local_transcript_file(
                        resource_id=res_id,
                        transcript_path=VTT_FIXTURE,
                        duration_ms=2000,  # VTT ends at 14000ms > 2000 + 5000ms
                        attribution_text="Duration exceeded test",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        permits_transcript_storage=True,
                        attestation_id=att["attestation_id"],
                    )

    def test_06_idempotent_reimport_and_version_superseding_on_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "versioning.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                att = self._create_open_license_attestation(store, res_id)

                first = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic Lecture Attribution",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att["attestation_id"],
                )
                self.assertEqual(first.status, "imported")
                self.assertEqual(first.version_number, 1)

                # Re-import identical content -> must be skipped_unchanged without creating v2
                second = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic Lecture Attribution",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att["attestation_id"],
                )
                self.assertEqual(second.status, "skipped_unchanged")
                self.assertEqual(second.transcript_id, first.transcript_id)
                self.assertEqual(second.transcript_version_id, first.transcript_version_id)
                self.assertEqual(second.version_number, 1)
                self.assertEqual(len(store.get_transcript_versions(first.transcript_id)), 1)

                # Modify transcript content -> creates version 2 and marks version 1 SUPERSEDED
                updated_vtt = (
                    VTT_FIXTURE.read_text(encoding="utf-8")
                    + "\n00:00:14.000 --> 00:00:18.500\n<v Instructor>Fourth segment added in revised caption release.</v>\n"
                )
                third = store.import_local_transcript(
                    resource_id=res_id,
                    raw_content=updated_vtt,
                    caption_format="WEBVTT",
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic Lecture Attribution",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att["attestation_id"],
                )
                self.assertEqual(third.status, "updated")
                self.assertEqual(third.version_number, 2)
                self.assertEqual(third.stored_segments, 4)
                self.assertNotEqual(third.transcript_version_id, first.transcript_version_id)

                versions = store.get_transcript_versions(first.transcript_id)
                self.assertEqual(len(versions), 2)
                self.assertEqual(versions[0]["version_number"], 1)
                self.assertEqual(versions[0]["status"], "SUPERSEDED")
                self.assertEqual(versions[1]["version_number"], 2)
                self.assertEqual(versions[1]["status"], "ACTIVE")

                # Verify immutability trigger on transcript_versions
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcript_versions SET word_count = 999 WHERE transcript_version_id = ?",
                        (first.transcript_version_id,),
                    )

    def test_07_attestation_withdrawal_and_expiry_block_storage_and_cascade_takedown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "takedown.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                att = self._create_open_license_attestation(store, res_id)
                imported = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic Lecture Attribution",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att["attestation_id"],
                )
                store.record_transcript_synthesis(
                    transcript_id=imported.transcript_id,
                    synthesis_type="STRUCTURED_SUMMARY",
                    generator_type="HUMAN_CURATED",
                    content_markdown="## Notes\nTransformative summary.",
                )
                self.assertEqual(len(store.get_transcript_segments(imported.transcript_id)), 3)
                self.assertEqual(len(store.get_transcript_syntheses(imported.transcript_id)), 1)

                # Withdrawing the authorization attestation cascades takedown to linked transcripts
                withdrawn_att = store.withdraw_transcript_attestation(
                    att["attestation_id"],
                    reason="Rights holder revoked caption storage authorization",
                    actor="rights_governance_officer",
                )
                self.assertEqual(withdrawn_att["verification_status"], "WITHDRAWN")
                self.assertEqual(withdrawn_att["permits_transcript_storage"], 0)
                self.assertEqual(withdrawn_att["permits_verbatim_excerpts"], 0)
                self.assertEqual(withdrawn_att["permits_redistribution"], 0)

                tombstoned = store.get_transcript(imported.transcript_id)
                self.assertEqual(tombstoned["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
                self.assertEqual(
                    tombstoned["transcript_storage_status"], "TOMBSTONED_ON_TAKEDOWN"
                )
                self.assertEqual(tombstoned["permits_transcript_storage"], 0)
                self.assertEqual(tombstoned["permits_verbatim_excerpts"], 0)
                self.assertEqual(tombstoned["permits_ai_synthesis"], 0)
                self.assertEqual(tombstoned["permits_redistribution"], 0)
                self.assertEqual(tombstoned["segment_count"], 0)
                self.assertIsNone(tombstoned["latest_transcript_version_id"])

                # Confirm all segments and syntheses were purged
                seg_left = store.conn.execute(
                    "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?",
                    (imported.transcript_id,),
                ).fetchone()[0]
                syn_left = store.conn.execute(
                    "SELECT COUNT(*) FROM transcript_syntheses WHERE transcript_id = ?",
                    (imported.transcript_id,),
                ).fetchone()[0]
                self.assertEqual(seg_left, 0)
                self.assertEqual(syn_left, 0)

                # Confirm version row is marked TOMBSTONED and provenance events were recorded
                versions = store.get_transcript_versions(imported.transcript_id)
                self.assertEqual(versions[0]["status"], "TOMBSTONED")
                prov = store.conn.execute(
                    "SELECT * FROM provenance_events WHERE event_type = 'TRANSCRIPT_ATTESTATION_WITHDRAWN'"
                ).fetchone()
                self.assertIsNotNone(prov)

                # Confirm a withdrawn attestation blocks any new transcript storage attempt
                blocked_after_withdrawal = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=SRT_FIXTURE,
                    language_bcp47="fr",
                    track_kind="TRANSLATION",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Attempt with withdrawn attestation",
                    permits_transcript_storage=True,
                    attestation_id=att["attestation_id"],
                )
                self.assertEqual(blocked_after_withdrawal.status, "blocked")
                self.assertEqual(blocked_after_withdrawal.stored_segments, 0)

                # Confirm an expired attestation blocks transcript storage
                expired_att = self._create_open_license_attestation(
                    store,
                    res_id,
                    language_bcp47="de",
                    track_kind="TRANSLATION",
                    expires_at="2020-01-01T00:00:00+00:00",
                )
                blocked_expired = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=SRT_FIXTURE,
                    language_bcp47="de",
                    track_kind="TRANSLATION",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Attempt with expired attestation",
                    permits_transcript_storage=True,
                    attestation_id=expired_att["attestation_id"],
                )
                self.assertEqual(blocked_expired.status, "blocked")
                self.assertEqual(blocked_expired.stored_segments, 0)
                self.assertIn("expired", blocked_expired.reason.lower())

    def test_08_state_machine_and_cli_commands_work_offline(self):
        validate_transcript_state_transition("DISCOVERED", "RIGHTS_PENDING")
        validate_transcript_state_transition("RIGHTS_PENDING", "READY_FOR_INGESTION")
        with self.assertRaises(ValueError):
            validate_transcript_state_transition("BLOCKED_BY_POLICY", "INGESTED")
        with self.assertRaises(ValueError):
            validate_transcript_state_transition("WITHDRAWN_OR_TOMBSTONED", "INGESTED")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cli.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

            # 1. Create attestation via CLI
            att_buf = io.StringIO()
            with contextlib.redirect_stdout(att_buf):
                rc_att = knowledge_store.main(
                    [
                        "attest-transcript",
                        "--db",
                        str(db_path),
                        "--resource-id",
                        res_id,
                        "--language",
                        "en",
                        "--track-kind",
                        "PRIMARY",
                        "--authorization-basis",
                        "OPEN_LICENSE_VERIFIED",
                        "--license-status",
                        "VERIFIED_PERMISSIVE",
                        "--license-spdx",
                        "CC-BY-4.0",
                        "--evidence-type",
                        "CREATOR_LICENSE_PAGE",
                        "--evidence-reference",
                        "Verified 3B1B lesson page CC-BY-4.0 notice",
                        "--evidence-url",
                        "https://www.3blue1brown.com/lessons/synthetic-test-lecture",
                        "--attribution",
                        "CLI Test Attribution (CC-BY-4.0)",
                        "--permit-storage",
                        "--permit-verbatim-excerpts",
                        "--permit-ai-synthesis",
                        "--reviewed-by",
                        "cli_test_curator",
                        "--verification-status",
                        "VERIFIED",
                    ]
                )
            self.assertEqual(rc_att, 0)
            att_out = att_buf.getvalue()
            self.assertIn("Verification status:       VERIFIED", att_out)

            att_id = compute_transcript_attestation_id(
                res_id,
                "OPEN_LICENSE_VERIFIED",
                "CREATOR_LICENSE_PAGE",
                "Verified 3B1B lesson page CC-BY-4.0 notice",
                language_bcp47="en",
                track_kind="PRIMARY",
            )

            # 2. List attestations via CLI
            list_att_buf = io.StringIO()
            with contextlib.redirect_stdout(list_att_buf):
                rc_list_att = knowledge_store.main(
                    ["list-transcript-attestations", "--db", str(db_path)]
                )
            self.assertEqual(rc_list_att, 0)
            self.assertIn(att_id, list_att_buf.getvalue())

            # 3. Import transcript via CLI referencing attestation
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = knowledge_store.main(
                    [
                        "import-transcript",
                        "--db",
                        str(db_path),
                        "--resource-id",
                        res_id,
                        "--file",
                        str(VTT_FIXTURE),
                        "--language",
                        "en",
                        "--track-kind",
                        "PRIMARY",
                        "--origin-type",
                        "AUTHOR_PROVIDED",
                        "--acquisition-method",
                        "OPEN_LICENSED_DIRECT_CAPTION",
                        "--license-status",
                        "VERIFIED_PERMISSIVE",
                        "--license-spdx",
                        "CC-BY-4.0",
                        "--authorization-basis",
                        "OPEN_LICENSE_VERIFIED",
                        "--attribution",
                        "CLI Test Attribution (CC-BY-4.0)",
                        "--attestation-id",
                        att_id,
                        "--permit-storage",
                        "--permit-verbatim-excerpts",
                    ]
                )
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("Status:                    imported", output)
            self.assertIn(f"Attestation ID:            {att_id}", output)
            self.assertIn("Stored segments:           3", output)
            self.assertNotIn("backpropagation", output)

            t_id = compute_transcript_id(res_id, "en", "PRIMARY")

            list_buf = io.StringIO()
            with contextlib.redirect_stdout(list_buf):
                rc_list = knowledge_store.main(["list-transcripts", "--db", str(db_path)])
            self.assertEqual(rc_list, 0)
            self.assertIn(t_id, list_buf.getvalue())

            show_buf = io.StringIO()
            with contextlib.redirect_stdout(show_buf):
                rc_show = knowledge_store.main(
                    ["show-transcript", "--db", str(db_path), "--transcript-id", t_id]
                )
            self.assertEqual(rc_show, 0)
            self.assertIn("Stored Segments:      3", show_buf.getvalue())
            self.assertIn(att_id, show_buf.getvalue())

            withdraw_buf = io.StringIO()
            with contextlib.redirect_stdout(withdraw_buf):
                rc_withdraw = knowledge_store.main(
                    [
                        "withdraw-transcript-attestation",
                        "--db",
                        str(db_path),
                        "--attestation-id",
                        att_id,
                        "--reason",
                        "CLI attestation withdrawal test",
                    ]
                )
            self.assertEqual(rc_withdraw, 0)
            self.assertIn("status=WITHDRAWN", withdraw_buf.getvalue())

            status_buf = io.StringIO()
            with contextlib.redirect_stdout(status_buf):
                rc_status = knowledge_store.main(["status", "--db", str(db_path)])
            self.assertEqual(rc_status, 0)
            self.assertIn("Schema version: 8", status_buf.getvalue())
            self.assertIn("transcripts", status_buf.getvalue())
            self.assertIn("transcript_authorization_attestations", status_buf.getvalue())

    def test_09_low_confidence_inaudible_markers_route_to_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "needs_review.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                att = self._create_open_license_attestation(store, res_id)
                vtt_with_inaudible = (
                    "WEBVTT\n\n"
                    "00:00:00.000 --> 00:00:05.000\n"
                    "<v Speaker>First segment has [inaudible] audio dropout.</v>\n\n"
                    "00:00:05.000 --> 00:00:10.000\n"
                    "<v Speaker>Second segment is clear.</v>\n"
                )
                result = store.import_local_transcript(
                    resource_id=res_id,
                    raw_content=vtt_with_inaudible,
                    caption_format="WEBVTT",
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Inaudible Marker Test",
                    permits_transcript_storage=True,
                    attestation_id=att["attestation_id"],
                )
                self.assertEqual(result.status, "imported")
                self.assertEqual(result.ingestion_state, "NEEDS_REVIEW")
                self.assertIn("TV-07", result.reason or "")
                segments = store.get_transcript_segments(result.transcript_id)
                self.assertEqual(segments[0]["is_low_confidence"], 1)
                self.assertIn("CONTAINS_INAUDIBLE_MARKER", segments[0]["quality_flags"])

    def test_10_discovery_registration_and_state_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "discovery.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                disc = store.register_transcript_discovery(
                    resource_id=res_id,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    attribution_text="Discovered Lecture Track",
                    ingestion_state="DISCOVERED",
                )
                self.assertEqual(disc["ingestion_state"], "DISCOVERED")
                self.assertEqual(disc["transcript_storage_status"], "NOT_STORED")
                self.assertEqual(disc["permits_transcript_storage"], 0)

                pending = store.transition_transcript_state(
                    disc["transcript_id"],
                    "RIGHTS_PENDING",
                    reason="Awaiting license verification",
                )
                self.assertEqual(pending["ingestion_state"], "RIGHTS_PENDING")

                blocked = store.transition_transcript_state(
                    disc["transcript_id"],
                    "BLOCKED_BY_POLICY",
                    reason="No open license found",
                )
                self.assertEqual(blocked["ingestion_state"], "BLOCKED_BY_POLICY")
                self.assertEqual(blocked["transcript_storage_status"], "BLOCKED_NO_AUTHORIZATION")

    def test_11_written_permission_attestation_and_granular_scope_enforcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "written_permission.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

                # Create a WRITTEN_PERMISSION attestation that grants storage + AI synthesis
                # but forbids verbatim excerpts and redistribution, without needing a public license URL.
                written_att = store.record_transcript_attestation(
                    resource_id=res_id,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    authorization_basis="WRITTEN_PERMISSION",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx=None,
                    evidence_type="WRITTEN_PERMISSION_RECORD",
                    evidence_reference="2026-10-15 Signed Lecturer Email Permission #LP-2026-09",
                    source_evidence_url=None,
                    evidence_sha256=sha256_hex("signed-permission-email-digest"),
                    attribution_text="Used by written permission of the lecturer (internal study only)",
                    permits_metadata_indexing=True,
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=False,
                    permits_ai_synthesis=True,
                    permits_redistribution=False,
                    reviewed_by="rights_reviewer",
                    review_notes="Internal storage & synthesis permitted; verbatim quoting and redistribution prohibited.",
                    verification_status="VERIFIED",
                )
                self.assertEqual(written_att["verification_status"], "VERIFIED")
                self.assertIsNone(written_att["source_evidence_url"])
                self.assertEqual(written_att["permits_transcript_storage"], 1)
                self.assertEqual(written_att["permits_verbatim_excerpts"], 0)
                self.assertEqual(written_att["permits_redistribution"], 0)

                # Attempting to import with permits_verbatim_excerpts=True or permits_redistribution=True
                # beyond the attestation scope MUST fail closed.
                exceeded_verbatim = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    authorization_basis="WRITTEN_PERMISSION",
                    attribution_text="Used by written permission",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=written_att["attestation_id"],
                )
                self.assertEqual(exceeded_verbatim.status, "blocked")
                self.assertEqual(exceeded_verbatim.stored_segments, 0)
                self.assertIn("permits_verbatim_excerpts", exceeded_verbatim.reason)

                exceeded_redist = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    authorization_basis="WRITTEN_PERMISSION",
                    attribution_text="Used by written permission",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=False,
                    permits_redistribution=True,
                    attestation_id=written_att["attestation_id"],
                )
                self.assertEqual(exceeded_redist.status, "blocked")
                self.assertEqual(exceeded_redist.stored_segments, 0)
                self.assertIn("permits_redistribution", exceeded_redist.reason)

                # Importing within the exact attested scope succeeds and preserves scope flags
                valid_written = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    authorization_basis="WRITTEN_PERMISSION",
                    attribution_text="Used by written permission of the lecturer (internal study only)",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=False,
                    permits_ai_synthesis=True,
                    permits_redistribution=False,
                    attestation_id=written_att["attestation_id"],
                )
                self.assertEqual(valid_written.status, "imported")
                self.assertTrue(valid_written.permits_transcript_storage)
                self.assertFalse(valid_written.permits_verbatim_excerpts)
                self.assertFalse(valid_written.permits_redistribution)
                self.assertEqual(valid_written.stored_segments, 3)

                # Attempting downstream verbatim excerpt quoting or redistribution fails closed
                segments = store.get_transcript_segments(valid_written.transcript_id)
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.get_verbatim_transcript_excerpt(segments[0]["segment_id"])

                with self.assertRaises(TranscriptPolicyViolationError):
                    store.export_transcript_for_redistribution(valid_written.transcript_id)

                # Transformative AI synthesis IS permitted by the attestation
                syn = store.record_transcript_synthesis(
                    transcript_id=valid_written.transcript_id,
                    synthesis_type="STRUCTURED_SUMMARY",
                    generator_type="LLM_ASSISTED_CURATED",
                    content_markdown="## Concept Notes\nOwn-words explanation of gradient clipping.",
                )
                self.assertTrue(syn["synthesis_id"].startswith("tsyn_"))

    def test_12_cross_resource_and_track_mismatch_and_provenance_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cross_resource.sqlite"
            with init_db(db_path) as store:
                res_a = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/resource-a",
                    title="Resource A",
                )
                res_b = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/resource-b",
                    title="Resource B",
                )

                att_a = self._create_open_license_attestation(
                    store,
                    res_a,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                )

                # 1. Attestation for Resource A cannot authorize transcript storage for Resource B
                mismatch_res = store.import_local_transcript_file(
                    resource_id=res_b,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Cross-resource attempt",
                    permits_transcript_storage=True,
                    attestation_id=att_a["attestation_id"],
                )
                self.assertEqual(mismatch_res.status, "blocked")
                self.assertEqual(mismatch_res.stored_segments, 0)
                self.assertIn("resource_id mismatch", mismatch_res.reason)

                # 2. Attestation scoped to language 'en' cannot authorize language 'fr'
                mismatch_lang = store.import_local_transcript_file(
                    resource_id=res_a,
                    transcript_path=SRT_FIXTURE,
                    language_bcp47="fr",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Language mismatch attempt",
                    permits_transcript_storage=True,
                    attestation_id=att_a["attestation_id"],
                )
                self.assertEqual(mismatch_lang.status, "blocked")
                self.assertEqual(mismatch_lang.stored_segments, 0)
                self.assertIn("language_bcp47 mismatch", mismatch_lang.reason)

                # 3. Valid import on Resource A records authorization_attestation_id and policy outcome
                # in provenance_events without storing full transcript text.
                valid_a = store.import_local_transcript_file(
                    resource_id=res_a,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Valid Resource A Import",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    attestation_id=att_a["attestation_id"],
                )
                self.assertEqual(valid_a.status, "imported")

                events = store.list_provenance_events(resource_id=res_a)
                imported_events = [
                    e for e in events if e["event_type"] == "TRANSCRIPT_IMPORTED"
                ]
                self.assertEqual(len(imported_events), 1)
                details = json.loads(imported_events[0]["details_json"])
                self.assertEqual(
                    details["authorization_attestation_id"], att_a["attestation_id"]
                )
                self.assertEqual(details["ingestion_state"], "INGESTED")
                self.assertEqual(
                    details["transcript_storage_status"], "STORED_TIMED_SEGMENTS"
                )
                self.assertNotIn("backpropagation", imported_events[0]["details_json"])

    def test_13_direct_sql_authorization_bypass_hardening_and_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "direct_sql_hardening.sqlite"
            with init_db(db_path) as store:
                res_a = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/sql-hardening-a",
                    title="SQL Hardening Resource A",
                )
                res_b = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/sql-hardening-b",
                    title="SQL Hardening Resource B",
                )

                # Create a valid attestation for Resource A (en, PRIMARY, storage=1, verbatim=0, synthesis=1, redist=0)
                att_a = store.record_transcript_attestation(
                    resource_id=res_a,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    authorization_basis="WRITTEN_PERMISSION",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx=None,
                    evidence_type="WRITTEN_PERMISSION_RECORD",
                    evidence_reference="Signed Permission #SQL-01",
                    evidence_sha256=sha256_hex("signed-permission-sql-01"),
                    attribution_text="Resource A Written Permission",
                    permits_metadata_indexing=True,
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=False,
                    permits_ai_synthesis=True,
                    permits_redistribution=False,
                    reviewed_by="security_auditor",
                    verification_status="VERIFIED",
                )

                # Create an expired attestation for Resource A (de, TRANSLATION)
                att_expired = self._create_open_license_attestation(
                    store,
                    res_a,
                    language_bcp47="de",
                    track_kind="TRANSLATION",
                    expires_at="2020-01-01T00:00:00+00:00",
                )

                # ------------------------------------------------------------------
                # Question 1: Can direct SQL set permits_transcript_storage = 1
                # without a valid, matching attestation?
                # ------------------------------------------------------------------
                tr_b_id = compute_transcript_id(res_b, "en", "PRIMARY")
                # 1a. No attestation (NULL)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcripts (
                            transcript_id, resource_id, source_id, media_canonical_url,
                            language_bcp47, track_kind, origin_type, acquisition_method,
                            caption_format, speaker_attribution_quality, license_status,
                            license_spdx, authorization_basis, authorization_attestation_id,
                            permits_transcript_storage, permits_verbatim_excerpts, permits_ai_synthesis,
                            permits_redistribution, attribution_text, ingestion_state,
                            transcript_storage_status, segment_count, created_at, updated_at
                        ) VALUES (
                            ?, ?, 'SRC-3B1B', 'https://www.3blue1brown.com/lessons/sql-hardening-b',
                            'en', 'PRIMARY', 'AUTHOR_PROVIDED', 'OPEN_LICENSED_DIRECT_CAPTION',
                            'WEBVTT', 'VERIFIED_SINGLE_SPEAKER', 'VERIFIED_PERMISSIVE',
                            'CC-BY-4.0', 'OPEN_LICENSE_VERIFIED', NULL,
                            1, 0, 1, 0, 'Bypass attempt', 'INGESTED', 'STORED_TIMED_SEGMENTS',
                            0, '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (tr_b_id, res_b),
                    )

                # 1b. Cross-resource attestation (att_a belongs to res_a, used on res_b)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcripts (
                            transcript_id, resource_id, source_id, media_canonical_url,
                            language_bcp47, track_kind, origin_type, acquisition_method,
                            caption_format, speaker_attribution_quality, license_status,
                            license_spdx, authorization_basis, authorization_attestation_id,
                            permits_transcript_storage, permits_verbatim_excerpts, permits_ai_synthesis,
                            permits_redistribution, attribution_text, ingestion_state,
                            transcript_storage_status, segment_count, created_at, updated_at
                        ) VALUES (
                            ?, ?, 'SRC-3B1B', 'https://www.3blue1brown.com/lessons/sql-hardening-b',
                            'en', 'PRIMARY', 'AUTHOR_PROVIDED', 'FIRST_PARTY_CURATED_IMPORT',
                            'WEBVTT', 'VERIFIED_SINGLE_SPEAKER', 'VERIFIED_PERMISSIVE',
                            NULL, 'WRITTEN_PERMISSION', ?,
                            1, 0, 1, 0, 'Cross-resource SQL bypass', 'INGESTED', 'STORED_TIMED_SEGMENTS',
                            0, '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (tr_b_id, res_b, att_a["attestation_id"]),
                    )

                # 1c. Language mismatch via direct SQL (att_a is scoped to 'en', used for 'fr' on res_a)
                tr_a_fr_id = compute_transcript_id(res_a, "fr", "PRIMARY")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcripts (
                            transcript_id, resource_id, source_id, media_canonical_url,
                            language_bcp47, track_kind, origin_type, acquisition_method,
                            caption_format, speaker_attribution_quality, license_status,
                            license_spdx, authorization_basis, authorization_attestation_id,
                            permits_transcript_storage, permits_verbatim_excerpts, permits_ai_synthesis,
                            permits_redistribution, attribution_text, ingestion_state,
                            transcript_storage_status, segment_count, created_at, updated_at
                        ) VALUES (
                            ?, ?, 'SRC-3B1B', 'https://www.3blue1brown.com/lessons/sql-hardening-a',
                            'fr', 'PRIMARY', 'AUTHOR_PROVIDED', 'FIRST_PARTY_CURATED_IMPORT',
                            'WEBVTT', 'VERIFIED_SINGLE_SPEAKER', 'VERIFIED_PERMISSIVE',
                            NULL, 'WRITTEN_PERMISSION', ?,
                            1, 0, 1, 0, 'Lang mismatch SQL bypass', 'INGESTED', 'STORED_TIMED_SEGMENTS',
                            0, '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (tr_a_fr_id, res_a, att_a["attestation_id"]),
                    )

                # 1d. Expired attestation via direct SQL (att_expired is expired, used on res_a 'de' 'TRANSLATION')
                tr_a_de_id = compute_transcript_id(res_a, "de", "TRANSLATION")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcripts (
                            transcript_id, resource_id, source_id, media_canonical_url,
                            language_bcp47, track_kind, origin_type, acquisition_method,
                            caption_format, speaker_attribution_quality, license_status,
                            license_spdx, authorization_basis, authorization_attestation_id,
                            permits_transcript_storage, permits_verbatim_excerpts, permits_ai_synthesis,
                            permits_redistribution, attribution_text, ingestion_state,
                            transcript_storage_status, segment_count, created_at, updated_at
                        ) VALUES (
                            ?, ?, 'SRC-3B1B', 'https://www.3blue1brown.com/lessons/sql-hardening-a',
                            'de', 'TRANSLATION', 'AUTHOR_PROVIDED', 'OPEN_LICENSED_DIRECT_CAPTION',
                            'WEBVTT', 'VERIFIED_SINGLE_SPEAKER', 'VERIFIED_PERMISSIVE',
                            'CC-BY-4.0', 'OPEN_LICENSE_VERIFIED', ?,
                            1, 0, 1, 0, 'Expired attestation SQL bypass', 'INGESTED', 'STORED_TIMED_SEGMENTS',
                            0, '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (tr_a_de_id, res_a, att_expired["attestation_id"]),
                    )

                # 1e. Malformed non-ISO expires_at on attestation via direct SQL is rejected
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcript_authorization_attestations SET expires_at = 'not-a-valid-date' WHERE attestation_id = ?",
                        (att_a["attestation_id"],),
                    )

                # 1f. Broadening or retargeting attestation scope via direct SQL UPDATE is rejected
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcript_authorization_attestations SET resource_id = ? WHERE attestation_id = ?",
                        (res_b, att_a["attestation_id"]),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcript_authorization_attestations SET language_bcp47 = NULL WHERE attestation_id = ?",
                        (att_a["attestation_id"],),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcript_authorization_attestations SET track_kind = 'TRANSLATION' WHERE attestation_id = ?",
                        (att_a["attestation_id"],),
                    )

                # ------------------------------------------------------------------
                # Question 2 & 3: Direct SQL bypass attempts on versions, segments,
                # verbatim excerpts, redistribution, and syntheses
                # ------------------------------------------------------------------
                # Import valid transcript for Resource A under att_a (storage=1, verbatim=0, synthesis=1, redist=0)
                imported_a = store.import_local_transcript_file(
                    resource_id=res_a,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="VERIFIED_PERMISSIVE",
                    authorization_basis="WRITTEN_PERMISSION",
                    attribution_text="Resource A Written Permission",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=False,
                    permits_ai_synthesis=True,
                    permits_redistribution=False,
                    attestation_id=att_a["attestation_id"],
                )
                self.assertEqual(imported_a.status, "imported")

                # 3a. Direct SQL cannot escalate permits_verbatim_excerpts = 1 or permits_redistribution = 1
                # when the attestation has permits_verbatim_excerpts = 0 and permits_redistribution = 0
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcripts SET permits_verbatim_excerpts = 1 WHERE transcript_id = ?",
                        (imported_a.transcript_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcripts SET permits_redistribution = 1 WHERE transcript_id = ?",
                        (imported_a.transcript_id,),
                    )

                # 3b. Direct SQL cannot insert TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED or TRANSCRIPT_REDISTRIBUTION_EXPORTED
                # audit records into provenance_events when permission is not granted
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO provenance_events (
                            event_id, resource_id, source_id, event_type, acquisition_method,
                            actor, status, details_json, recorded_at
                        ) VALUES (
                            'evt_forged_verbatim_01', ?, 'SRC-3B1B', 'TRANSCRIPT_VERBATIM_EXCERPT_ACCESSED',
                            'FIRST_PARTY_CURATED_IMPORT', 'attacker', 'allowed', ?, '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (
                            res_a,
                            json.dumps(
                                {
                                    "transcript_id": imported_a.transcript_id,
                                    "authorization_attestation_id": att_a["attestation_id"],
                                }
                            ),
                        ),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO provenance_events (
                            event_id, resource_id, source_id, event_type, acquisition_method,
                            actor, status, details_json, recorded_at
                        ) VALUES (
                            'evt_forged_redist_01', ?, 'SRC-3B1B', 'TRANSCRIPT_REDISTRIBUTION_EXPORTED',
                            'FIRST_PARTY_CURATED_IMPORT', 'attacker', 'exported', ?, '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (
                            res_a,
                            json.dumps(
                                {
                                    "transcript_id": imported_a.transcript_id,
                                    "authorization_attestation_id": att_a["attestation_id"],
                                }
                            ),
                        ),
                    )

                # 3c. Narrowing permits_ai_synthesis = 0 via direct SQL on the attestation cascades to transcripts
                # and blocks inserting transcript_syntheses via direct SQL or application methods
                syn_before = store.record_transcript_synthesis(
                    transcript_id=imported_a.transcript_id,
                    synthesis_type="STRUCTURED_SUMMARY",
                    generator_type="HUMAN_CURATED",
                    content_markdown="## Summary\nValid transformative summary while permits_ai_synthesis=1.",
                )
                self.assertEqual(len(store.get_transcript_syntheses(imported_a.transcript_id)), 1)

                store.conn.execute(
                    "UPDATE transcript_authorization_attestations SET permits_ai_synthesis = 0 WHERE attestation_id = ?",
                    (att_a["attestation_id"],),
                )
                # Verify cascade purged existing synthesis and set transcripts.permits_ai_synthesis = 0
                self.assertEqual(len(store.get_transcript_syntheses(imported_a.transcript_id)), 0)
                self.assertEqual(store.get_transcript(imported_a.transcript_id)["permits_ai_synthesis"], 0)

                # Direct SQL cannot re-enable transcripts.permits_ai_synthesis = 1 or insert transcript_syntheses
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcripts SET permits_ai_synthesis = 1 WHERE transcript_id = ?",
                        (imported_a.transcript_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcript_syntheses (
                            synthesis_id, transcript_id, transcript_version_id, synthesis_type,
                            generator_type, content_markdown, is_original_transformative,
                            contains_verbatim_transcript, review_status, created_at
                        ) VALUES (
                            'tsyn_bypass_01', ?, ?, 'STRUCTURED_SUMMARY',
                            'HUMAN_CURATED', 'Bypass synthesis attempt', 1, 0, 'PENDING_REVIEW',
                            '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (imported_a.transcript_id, imported_a.transcript_version_id),
                    )
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.record_transcript_synthesis(
                        transcript_id=imported_a.transcript_id,
                        synthesis_type="STRUCTURED_SUMMARY",
                        generator_type="HUMAN_CURATED",
                        content_markdown="## Blocked\nSynthesis disallowed.",
                    )

                # ------------------------------------------------------------------
                # Question 4: Direct SQL expiry/withdrawal cascades takedown and
                # prevents any continued import or segment/version insertion
                # ------------------------------------------------------------------
                # 4a. Deleting an attestation row via direct SQL is prohibited
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "DELETE FROM transcript_authorization_attestations WHERE attestation_id = ?",
                        (att_a["attestation_id"],),
                    )

                # 4b. Expiring att_a via direct SQL UPDATE cascades takedown to transcripts, versions, and segments
                store.conn.execute(
                    "UPDATE transcript_authorization_attestations SET expires_at = '2020-01-01T00:00:00+00:00' WHERE attestation_id = ?",
                    (att_a["attestation_id"],),
                )
                tr_after_expiry = store.get_transcript(imported_a.transcript_id)
                self.assertEqual(tr_after_expiry["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
                self.assertEqual(tr_after_expiry["transcript_storage_status"], "TOMBSTONED_ON_TAKEDOWN")
                self.assertEqual(tr_after_expiry["permits_transcript_storage"], 0)
                self.assertEqual(len(store.get_transcript_segments(imported_a.transcript_id)), 0)
                self.assertEqual(
                    store.get_transcript_versions(imported_a.transcript_id)[0]["status"],
                    "TOMBSTONED",
                )

                # 4c. Direct SQL cannot reactivate a tombstoned version or tombstoned transcript,
                # nor insert versions or segments after expiry/tombstone
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcript_versions SET status = 'ACTIVE' WHERE transcript_version_id = ?",
                        (imported_a.transcript_version_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE transcripts SET ingestion_state = 'INGESTED' WHERE transcript_id = ?",
                        (imported_a.transcript_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcript_versions (
                            transcript_version_id, transcript_id, version_number,
                            raw_content_sha256, normalized_text_sha256, normalizer_version,
                            segment_count, word_count, char_count, status, created_at
                        ) VALUES (
                            'tver_after_expiry_v2', ?, 2, 'h1', 'h2', 'norm-v1.0',
                            1, 1, 1, 'ACTIVE', '2026-10-25T00:00:00+00:00'
                        )
                        """,
                        (imported_a.transcript_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        INSERT INTO transcript_segments (
                            segment_id, transcript_id, transcript_version_id, sequence_index,
                            start_ms, end_ms, original_segment_text, normalized_segment_text,
                            deep_link_url, segment_sha256
                        ) VALUES (
                            'tseg_after_expiry_0', ?, ?, 0, 0, 1000, 'Bypass text', 'Bypass text',
                            'https://www.3blue1brown.com/lessons/sql-hardening-a#t=0s', 'sha'
                        )
                        """,
                        (imported_a.transcript_id, imported_a.transcript_version_id),
                    )

                # 4d. Withdrawn attestation cannot be reactivated via direct SQL UPDATE
                att_b = self._create_open_license_attestation(store, res_b)
                store.withdraw_transcript_attestation(
                    att_b["attestation_id"],
                    reason="Revoked for test",
                    actor="security_auditor",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        """
                        UPDATE transcript_authorization_attestations
                           SET verification_status = 'VERIFIED',
                               withdrawn_at = NULL,
                               permits_transcript_storage = 1
                         WHERE attestation_id = ?
                        """,
                        (att_b["attestation_id"],),
                    )

    def test_14_natural_expiry_without_manual_update_blocks_reads_and_tombstones_idempotently(self) -> None:
        """Verify natural attestation expiration (clock advancing past expires_at without a manual DB UPDATE) across all read/write/maintenance paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_natural_expiry.db"
            current_time = ["2026-10-25T12:00:00+00:00"]

            with KnowledgeStore(db_path, clock=lambda: current_time[0]) as store:
                res_id = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/natural-expiry-lecture",
                    title="Natural Expiry Verification Lecture",
                )
                future_expiry = "2026-10-26T00:00:00+00:00"

                # 1. Create a valid, verified attestation with a future expires_at
                att = self._create_open_license_attestation(
                    store,
                    res_id,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    permits_ai_synthesis=True,
                    permits_redistribution=True,
                    expires_at=future_expiry,
                )
                att_id = str(att["attestation_id"])
                self.assertEqual(att["verification_status"], "VERIFIED")
                self.assertEqual(att["expires_at"], future_expiry)

                # 2. Import a synthetic transcript and verify authorized access works before expires_at
                imported = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    origin_type="AUTHOR_PROVIDED",
                    acquisition_method="OPEN_LICENSED_DIRECT_CAPTION",
                    speaker_attribution_quality="VERIFIED_SINGLE_SPEAKER",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="3Blue1Brown CC-BY-4.0 Temporary Grant",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                    permits_ai_synthesis=True,
                    permits_redistribution=True,
                    attestation_id=att_id,
                )
                self.assertEqual(imported.status, "imported")
                tr_id = imported.transcript_id

                syn = store.record_transcript_synthesis(
                    transcript_id=tr_id,
                    synthesis_type="STRUCTURED_SUMMARY",
                    generator_type="HUMAN_CURATED",
                    content_markdown="## Summary\nAuthorized synthesis before expiry.",
                )
                self.assertTrue(syn["synthesis_id"].startswith("tsyn_"))

                segments_before = store.get_transcript_segments(tr_id)
                self.assertEqual(len(segments_before), 3)
                first_seg_id = str(segments_before[0]["segment_id"])

                link = store.record_transcript_segment_link(
                    segment_id=first_seg_id,
                    target_type="CONCEPT",
                    target_id="concept_backprop_chain_rule",
                    relation_type="EXPLAINS",
                )
                self.assertTrue(link["link_id"].startswith("tslnk_"))
                self.assertEqual(len(store.get_transcript_segment_links(first_seg_id)), 1)

                excerpts_before = store.get_verbatim_transcript_excerpt(tr_id, actor="tester")
                self.assertEqual(len(excerpts_before), 3)
                single_excerpt_before = store.get_verbatim_transcript_excerpt(first_seg_id, actor="tester")
                self.assertEqual(single_excerpt_before["segment_id"], first_seg_id)

                export_before = store.export_transcript_for_redistribution(tr_id, actor="tester")
                self.assertEqual(len(export_before["segments"]), 3)
                self.assertEqual(len(store.get_transcript_syntheses(tr_id)), 1)
                self.assertEqual(store.get_transcript(tr_id)["ingestion_state"], "INGESTED")
                self.assertEqual(len(store.list_transcripts(ingestion_state="INGESTED")), 1)
                self.assertEqual(store.get_transcript_versions(tr_id)[0]["status"], "ACTIVE")

                # 3. Advance the controllable test clock beyond expires_at WITHOUT updating the attestation row
                current_time[0] = "2026-10-26T00:00:05+00:00"

                # Confirm raw SQLite state before any application call is still VERIFIED / INGESTED
                raw_att_before_call = store.conn.execute(
                    "SELECT verification_status FROM transcript_authorization_attestations WHERE attestation_id = ?",
                    (att_id,),
                ).fetchone()
                self.assertEqual(raw_att_before_call["verification_status"], "VERIFIED")

                # 4a. Calling get_verbatim_transcript_excerpt by segment_id as the VERY FIRST call after natural expiry
                # must fail closed with TranscriptPolicyViolationError (not return stale text or KeyError)
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.get_verbatim_transcript_excerpt(first_seg_id, actor="tester_after_expiry")

                # 4b. Calling get_verbatim_transcript_excerpt by transcript_id also raises TranscriptPolicyViolationError
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.get_verbatim_transcript_excerpt(tr_id, actor="tester_after_expiry")

                # 4c. Calling export_transcript_for_redistribution raises TranscriptPolicyViolationError
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.export_transcript_for_redistribution(tr_id, actor="tester_after_expiry")

                # 4d. Calling get_transcript_segments returns [] and raw segments table has 0 rows
                self.assertEqual(store.get_transcript_segments(tr_id), [])
                raw_seg_count = store.conn.execute(
                    "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?",
                    (tr_id,),
                ).fetchone()[0]
                self.assertEqual(raw_seg_count, 0)

                # 4e. Calling get_transcript_syntheses and get_transcript_segment_links returns []
                self.assertEqual(store.get_transcript_syntheses(tr_id), [])
                self.assertEqual(store.get_transcript_segment_links(first_seg_id), [])
                raw_syn_count = store.conn.execute(
                    "SELECT COUNT(*) FROM transcript_syntheses WHERE transcript_id = ?",
                    (tr_id,),
                ).fetchone()[0]
                self.assertEqual(raw_syn_count, 0)

                # 4f. get_transcript, list_transcripts, and get_transcript_versions reflect tombstoned/revoked state
                tr_after = store.get_transcript(tr_id)
                self.assertIsNotNone(tr_after)
                self.assertEqual(tr_after["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
                self.assertEqual(tr_after["transcript_storage_status"], "TOMBSTONED_ON_TAKEDOWN")
                self.assertEqual(tr_after["permits_transcript_storage"], 0)
                self.assertEqual(tr_after["permits_verbatim_excerpts"], 0)
                self.assertEqual(tr_after["permits_ai_synthesis"], 0)
                self.assertEqual(tr_after["permits_redistribution"], 0)
                self.assertEqual(tr_after["segment_count"], 0)
                self.assertIsNone(tr_after["latest_transcript_version_id"])

                self.assertEqual(store.list_transcripts(ingestion_state="INGESTED"), [])
                tombstoned_list = store.list_transcripts(ingestion_state="WITHDRAWN_OR_TOMBSTONED")
                self.assertEqual(len(tombstoned_list), 1)
                self.assertEqual(tombstoned_list[0]["transcript_id"], tr_id)

                versions_after = store.get_transcript_versions(tr_id)
                self.assertEqual(len(versions_after), 1)
                self.assertEqual(versions_after[0]["status"], "TOMBSTONED")

                # 4g. get_transcript_attestation and list_transcript_attestations reflect EXPIRED status
                att_after = store.get_transcript_attestation(att_id)
                self.assertIsNotNone(att_after)
                self.assertEqual(att_after["verification_status"], "EXPIRED")
                self.assertEqual(att_after["permits_transcript_storage"], 0)
                self.assertEqual(att_after["permits_verbatim_excerpts"], 0)
                self.assertEqual(att_after["permits_ai_synthesis"], 0)
                self.assertEqual(att_after["permits_redistribution"], 0)

                self.assertEqual(store.list_transcript_attestations(verification_status="VERIFIED"), [])
                expired_atts = store.list_transcript_attestations(verification_status="EXPIRED")
                self.assertEqual(len(expired_atts), 1)
                self.assertEqual(expired_atts[0]["attestation_id"], att_id)

                # 4h. Write operations (import, synthesis, segment link) are blocked after natural expiry
                with self.assertRaises(ValueError):
                    store.import_local_transcript_file(
                        resource_id=res_id,
                        transcript_path=VTT_FIXTURE,
                        language_bcp47="en",
                        track_kind="PRIMARY",
                        license_status="VERIFIED_PERMISSIVE",
                        license_spdx="CC-BY-4.0",
                        authorization_basis="OPEN_LICENSE_VERIFIED",
                        attribution_text="Re-import attempt after natural expiry",
                        permits_transcript_storage=True,
                        attestation_id=att_id,
                    )
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.record_transcript_synthesis(
                        transcript_id=tr_id,
                        synthesis_type="STRUCTURED_SUMMARY",
                        generator_type="HUMAN_CURATED",
                        content_markdown="## Blocked\nCannot synthesize expired transcript.",
                    )
                with self.assertRaises(TranscriptPolicyViolationError):
                    store.record_transcript_segment_link(
                        segment_id=first_seg_id,
                        target_type="CONCEPT",
                        target_id="concept_blocked",
                        relation_type="EXPLAINS",
                    )

                # 4i. Provenance events recorded once and repeated expiry maintenance is idempotent
                exp_events = store.conn.execute(
                    "SELECT * FROM provenance_events WHERE event_type = 'TRANSCRIPT_ATTESTATION_EXPIRED'"
                ).fetchall()
                tomb_events = store.conn.execute(
                    "SELECT * FROM provenance_events WHERE event_type = 'TRANSCRIPT_TOMBSTONED_ON_TAKEDOWN'"
                ).fetchall()
                self.assertEqual(len(exp_events), 1)
                self.assertEqual(len(tomb_events), 1)

                # Repeated maintenance calls must be a no-op returning [] and adding 0 duplicate events
                self.assertEqual(store.expire_due_transcript_attestations(), [])
                self.assertEqual(store.run_transcript_expiry_maintenance(), [])
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM provenance_events WHERE event_type = 'TRANSCRIPT_ATTESTATION_EXPIRED'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store.conn.execute(
                        "SELECT COUNT(*) FROM provenance_events WHERE event_type = 'TRANSCRIPT_TOMBSTONED_ON_TAKEDOWN'"
                    ).fetchone()[0],
                    1,
                )

    def test_15_natural_expiry_triggered_by_first_read_on_segments_or_store_init_or_cli(self) -> None:
        """Verify natural expiry when first triggered by get_transcript_segments, store re-initialization, or CLI expire-transcripts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_natural_expiry_paths.db"
            current_time = ["2026-10-25T12:00:00+00:00"]

            with KnowledgeStore(db_path, clock=lambda: current_time[0]) as store:
                res_1 = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/expiry-path-1",
                    title="Expiry Path 1",
                )
                res_2 = self._seed_sample_resource(
                    store,
                    canonical_url="https://www.3blue1brown.com/lessons/expiry-path-2",
                    title="Expiry Path 2",
                )
                att_1 = self._create_open_license_attestation(
                    store,
                    res_1,
                    expires_at="2026-10-26T00:00:00+00:00",
                )
                att_2 = self._create_open_license_attestation(
                    store,
                    res_2,
                    expires_at="2026-10-27T00:00:00+00:00",
                )
                imp_1 = store.import_local_transcript_file(
                    resource_id=res_1,
                    transcript_path=VTT_FIXTURE,
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Path 1 CC-BY-4.0",
                    permits_transcript_storage=True,
                    attestation_id=att_1["attestation_id"],
                )
                imp_2 = store.import_local_transcript_file(
                    resource_id=res_2,
                    transcript_path=SRT_FIXTURE,
                    caption_format="SRT",
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Path 2 CC-BY-4.0",
                    permits_transcript_storage=True,
                    attestation_id=att_2["attestation_id"],
                )
                self.assertEqual(len(store.get_transcript_segments(imp_1.transcript_id)), 3)
                self.assertEqual(len(store.get_transcript_segments(imp_2.transcript_id)), 2)

                # Advance clock past att_1's expires_at (but before att_2's expires_at)
                current_time[0] = "2026-10-26T01:00:00+00:00"

                # Calling get_transcript_segments directly as the first read after expiry:
                self.assertEqual(store.get_transcript_segments(imp_1.transcript_id), [])
                self.assertEqual(store.get_transcript(imp_1.transcript_id)["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
                # Meanwhile imp_2 (whose expires_at is 2026-10-27) remains active and accessible!
                self.assertEqual(len(store.get_transcript_segments(imp_2.transcript_id)), 2)
                self.assertEqual(store.get_transcript(imp_2.transcript_id)["ingestion_state"], "INGESTED")

            # Re-open/initialize the store with clock advanced past att_2's expires_at
            with KnowledgeStore(db_path, clock=lambda: "2026-10-27T01:00:00+00:00") as store2:
                # KnowledgeStore.__init__(auto_initialize=True) -> initialize() -> expire_due_transcript_attestations()
                raw_seg_2 = store2.conn.execute(
                    "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?",
                    (imp_2.transcript_id,),
                ).fetchone()[0]
                self.assertEqual(raw_seg_2, 0)
                self.assertEqual(store2.get_transcript(imp_2.transcript_id)["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
                self.assertEqual(store2.get_transcript_attestation(att_2["attestation_id"])["verification_status"], "EXPIRED")

            # Verify CLI expire-transcripts subcommand works offline and is idempotent
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = knowledge_store.main(
                    [
                        "expire-transcripts",
                        "--db",
                        str(db_path),
                        "--now",
                        "2026-10-28T00:00:00+00:00",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn("Expired transcript attestations processed (0):", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
