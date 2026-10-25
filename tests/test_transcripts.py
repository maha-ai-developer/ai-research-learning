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
    TranscriptSegmentCue,
    compute_resource_id,
    compute_segment_deep_link_url,
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

    def _seed_sample_resource(self, store: KnowledgeStore) -> str:
        upserted = store.upsert_resource(
            source_id="SRC-3B1B",
            manifest_id=None,
            canonical_url="https://www.3blue1brown.com/lessons/synthetic-test-lecture",
            title="Synthetic Lecture on Backpropagation (Test Fixture)",
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

    def test_01_forward_only_migration_from_v5_to_v6_preserves_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v5_to_v6.sqlite"
            conn = sqlite3.connect(str(db_path))
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
            # Apply only migrations 1..5 to simulate an existing schema v5 database
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

            # Verify v5 state before upgrading
            conn_check = sqlite3.connect(str(db_path))
            self.assertEqual(conn_check.execute("PRAGMA user_version").fetchone()[0], 5)
            conn_check.close()

            # Open with KnowledgeStore to trigger forward-only migration 5 -> 6
            with KnowledgeStore(db_path=db_path, auto_initialize=False) as store:
                newly_applied = store.initialize()
                self.assertEqual(newly_applied, [6])
                self.assertEqual(store.get_schema_version(), 6)
                self.assertEqual(LATEST_SCHEMA_VERSION, 6)

                # Confirm existing canonical sources survived intact
                sources = store.list_sources(only_complete=True)
                self.assertEqual(len(sources), 5)

                # Confirm all 5 new transcript tables exist
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
                ):
                    self.assertIn(expected_table, tables)

                # Confirm repeated initialization is idempotent
                self.assertEqual(store.initialize(), [])

    def test_02_deterministic_transcript_version_and_segment_ids(self):
        resource_id = compute_resource_id(
            "SRC-3B1B", "https://www.3blue1brown.com/lessons/synthetic-test-lecture"
        )
        t_id_1 = compute_transcript_id(resource_id, "en", "PRIMARY")
        t_id_2 = compute_transcript_id(resource_id, "EN", "primary")
        self.assertEqual(t_id_1, t_id_2)
        self.assertTrue(t_id_1.startswith("tr_"))
        self.assertEqual(len(t_id_1), 19)

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

    def test_03_valid_import_of_webvtt_srt_and_plain_text_under_explicit_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "store.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

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
                    duration_ms=20000,
                )
                self.assertEqual(vtt_res.status, "imported")
                self.assertEqual(vtt_res.ingestion_state, "INGESTED")
                self.assertEqual(vtt_res.transcript_storage_status, "STORED_TIMED_SEGMENTS")
                self.assertTrue(vtt_res.permits_transcript_storage)
                self.assertTrue(vtt_res.permits_verbatim_excerpts)
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
                )
                self.assertEqual(srt_res.status, "imported")
                self.assertEqual(srt_res.ingestion_state, "INGESTED")
                self.assertEqual(srt_res.transcript_storage_status, "STORED_TIMED_SEGMENTS")
                self.assertEqual(srt_res.stored_segments, 2)
                fr_segments = store.get_transcript_segments(srt_res.transcript_id)
                self.assertEqual(fr_segments[0]["speaker_label"], "Moderator")
                self.assertEqual(fr_segments[1]["start_ms"], 3800)
                self.assertEqual(fr_segments[1]["end_ms"], 8400)

                # 3. Plain text import (DESCRIPTIVE_AUDIO_TEXT, en)
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
                    authorization_basis="FIRST_PARTY_OWNED",
                    attribution_text="Synthetic Plain Text Notes (CC0-1.0)",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                )
                self.assertEqual(txt_res.status, "imported")
                self.assertEqual(txt_res.ingestion_state, "INGESTED")
                self.assertEqual(txt_res.transcript_storage_status, "STORED_PLAIN_TEXT")
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

    def test_04_policy_gate_blocks_unauthorized_or_unknown_license_and_stores_zero_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "policy_gate.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

                # Case A: license_status is UNKNOWN (even though caller passes permits_transcript_storage=True)
                blocked_unknown = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    language_bcp47="en",
                    track_kind="PRIMARY",
                    license_status="UNKNOWN",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    license_spdx="CC-BY-4.0",
                    attribution_text="Unverified License Transcript",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                )
                self.assertEqual(blocked_unknown.status, "blocked")
                self.assertEqual(blocked_unknown.ingestion_state, "BLOCKED_BY_POLICY")
                self.assertEqual(
                    blocked_unknown.transcript_storage_status, "BLOCKED_NO_AUTHORIZATION"
                )
                self.assertFalse(blocked_unknown.permits_transcript_storage)
                self.assertFalse(blocked_unknown.permits_verbatim_excerpts)
                self.assertIsNone(blocked_unknown.transcript_version_id)
                self.assertEqual(blocked_unknown.stored_segments, 0)

                # Case B: ALL_RIGHTS_RESERVED
                blocked_arr = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=SRT_FIXTURE,
                    language_bcp47="fr",
                    track_kind="TRANSLATION",
                    license_status="ALL_RIGHTS_RESERVED",
                    authorization_basis="NONE",
                    attribution_text="Proprietary Lecture Transcript",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                )
                self.assertEqual(blocked_arr.status, "blocked")
                self.assertEqual(blocked_arr.ingestion_state, "BLOCKED_BY_POLICY")
                self.assertEqual(blocked_arr.stored_segments, 0)

                # Case C: METADATA_AND_LINK_ONLY
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
                        (blocked_unknown.transcript_id,),
                    )

    def test_05_validation_failures_for_corrupt_cues_and_missing_parent_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "validation.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

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
                    )

    def test_06_idempotent_reimport_and_version_superseding_on_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "versioning.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

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

    def test_07_takedown_withdrawal_purges_segments_and_syntheses_and_retains_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "takedown.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
                imported = store.import_local_transcript_file(
                    resource_id=res_id,
                    transcript_path=VTT_FIXTURE,
                    license_status="VERIFIED_PERMISSIVE",
                    license_spdx="CC-BY-4.0",
                    authorization_basis="OPEN_LICENSE_VERIFIED",
                    attribution_text="Synthetic Lecture Attribution",
                    permits_transcript_storage=True,
                    permits_verbatim_excerpts=True,
                )
                store.record_transcript_synthesis(
                    transcript_id=imported.transcript_id,
                    synthesis_type="STRUCTURED_SUMMARY",
                    generator_type="HUMAN_CURATED",
                    content_markdown="## Notes\nTransformative summary.",
                )
                self.assertEqual(len(store.get_transcript_segments(imported.transcript_id)), 3)
                self.assertEqual(len(store.get_transcript_syntheses(imported.transcript_id)), 1)

                tombstoned = store.tombstone_transcript_on_takedown(
                    imported.transcript_id,
                    reason="Rights holder requested removal of caption track",
                    actor="rights_governance_officer",
                )
                self.assertEqual(tombstoned["ingestion_state"], "WITHDRAWN_OR_TOMBSTONED")
                self.assertEqual(
                    tombstoned["transcript_storage_status"], "TOMBSTONED_ON_TAKEDOWN"
                )
                self.assertEqual(tombstoned["permits_transcript_storage"], 0)
                self.assertEqual(tombstoned["permits_verbatim_excerpts"], 0)
                self.assertEqual(tombstoned["permits_ai_synthesis"], 0)
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

                # Confirm version row is marked TOMBSTONED and provenance event was recorded
                versions = store.get_transcript_versions(imported.transcript_id)
                self.assertEqual(versions[0]["status"], "TOMBSTONED")
                prov = store.conn.execute(
                    "SELECT * FROM provenance_events WHERE event_type = 'TRANSCRIPT_TOMBSTONED_ON_TAKEDOWN'"
                ).fetchone()
                self.assertIsNotNone(prov)

    def test_08_state_machine_and_cli_commands_work_offline(self):
        # Verify state machine rejects invalid transitions
        validate_transcript_state_transition("DISCOVERED", "RIGHTS_PENDING")
        validate_transcript_state_transition("RIGHTS_PENDING", "READY_FOR_INGESTION")
        with self.assertRaises(ValueError):
            validate_transcript_state_transition("BLOCKED_BY_POLICY", "INGESTED")
        with self.assertRaises(ValueError):
            validate_transcript_state_transition("WITHDRAWN_OR_TOMBSTONED", "INGESTED")

        # Verify CLI commands: import-transcript, list-transcripts, show-transcript, withdraw-transcript, status
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cli.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)

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
                        "--permit-storage",
                        "--permit-verbatim-excerpts",
                    ]
                )
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("Status:                    imported", output)
            self.assertIn("Stored segments:           3", output)
            # Ensure CLI output never dumps full transcript text
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

            withdraw_buf = io.StringIO()
            with contextlib.redirect_stdout(withdraw_buf):
                rc_withdraw = knowledge_store.main(
                    [
                        "withdraw-transcript",
                        "--db",
                        str(db_path),
                        "--transcript-id",
                        t_id,
                        "--reason",
                        "CLI takedown test",
                    ]
                )
            self.assertEqual(rc_withdraw, 0)
            self.assertIn("state=WITHDRAWN_OR_TOMBSTONED", withdraw_buf.getvalue())

            status_buf = io.StringIO()
            with contextlib.redirect_stdout(status_buf):
                rc_status = knowledge_store.main(["status", "--db", str(db_path)])
            self.assertEqual(rc_status, 0)
            self.assertIn("Schema version: 6", status_buf.getvalue())
            self.assertIn("transcripts", status_buf.getvalue())

    def test_09_low_confidence_inaudible_markers_route_to_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "needs_review.sqlite"
            with init_db(db_path) as store:
                res_id = self._seed_sample_resource(store)
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


if __name__ == "__main__":
    unittest.main()
