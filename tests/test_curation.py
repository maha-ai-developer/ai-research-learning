import contextlib
import csv
import io
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import curation
from knowledge_store import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    KnowledgeStore,
    validate_and_canonicalize_url,
)


def _forbid_network(*_args, **_kwargs):
    raise AssertionError("Network access is strictly forbidden during offline curation tests")


class OfflineCurationTests(unittest.TestCase):
    def setUp(self):
        self._socket_patch = patch.object(socket, "socket", side_effect=_forbid_network)
        self._conn_patch = patch.object(socket, "create_connection", side_effect=_forbid_network)
        self._socket_patch.start()
        self._conn_patch.start()

    def tearDown(self):
        self._conn_patch.stop()
        self._socket_patch.stop()

    def _seed_sample_candidates(self, store: KnowledgeStore) -> dict[str, str]:
        hinton_res = store.upsert_resource(
            source_id="SRC-HINTON",
            canonical_url="https://www.cs.toronto.edu/~hinton/index.html",
            title="Geoffrey Hinton Home",
            resource_type="Researcher homepage",
        )
        bengio_res = store.upsert_resource(
            source_id="SRC-BENGIO",
            canonical_url="https://yoshuabengio.org/publications/",
            title="Yoshua Bengio Publications",
            resource_type="Publication index",
        )
        b1b_res = store.upsert_resource(
            source_id="SRC-3B1B",
            canonical_url="https://www.3blue1brown.com/topics/neural-networks",
            title="3Blue1Brown Neural Networks",
            resource_type="Topic index",
        )

        c1 = store.upsert_discovered_link_candidate(
            referring_resource_id=hinton_res["resource_id"],
            referring_source_id="SRC-HINTON",
            from_url="https://www.cs.toronto.edu/~hinton/index.html",
            raw_href="csc321/notes.html#lec1",
            original_url="https://www.cs.toronto.edu/~hinton/csc321/notes.html#lec1",
            link_text="CSC321 Lecture Notes",
            is_internal=True,
            target_scope="Hinton in-scope HTML subpage",
            recommended_decision="Metadata-first candidate (do not auto-crawl)",
            discovered_at="2026-10-24T10:00:00+00:00",
        )
        c2 = store.upsert_discovered_link_candidate(
            referring_resource_id=hinton_res["resource_id"],
            referring_source_id="SRC-HINTON",
            from_url="https://www.cs.toronto.edu/~hinton/index.html",
            raw_href="https://www.nature.com/articles/nature14539",
            original_url="https://www.nature.com/articles/nature14539#citeas",
            link_text="Deep Learning Nature 2015",
            is_internal=False,
            target_scope="External publisher reference",
            recommended_decision="Record citation only (do not crawl external domain)",
            discovered_at="2026-10-24T10:01:00+00:00",
        )
        c3 = store.upsert_discovered_link_candidate(
            referring_resource_id=bengio_res["resource_id"],
            referring_source_id="SRC-BENGIO",
            from_url="https://yoshuabengio.org/publications/",
            raw_href="https://arxiv.org/abs/2106.04399",
            original_url="https://arxiv.org/abs/2106.04399",
            link_text="GFlowNet Foundations",
            is_internal=False,
            target_scope="External arXiv reference",
            recommended_decision="Deferred to arXiv API adapter",
            discovered_at="2026-10-24T10:02:00+00:00",
        )
        c4 = store.upsert_discovered_link_candidate(
            referring_resource_id=bengio_res["resource_id"],
            referring_source_id="SRC-BENGIO",
            from_url="https://yoshuabengio.org/publications/",
            raw_href="/profile/",
            original_url="https://yoshuabengio.org/profile/",
            link_text="Yoshua Bengio Bio",
            is_internal=True,
            target_scope="Bengio internal page",
            recommended_decision="Metadata-first candidate (do not auto-crawl)",
            discovered_at="2026-10-24T10:03:00+00:00",
        )
        c5 = store.upsert_discovered_link_candidate(
            referring_resource_id=b1b_res["resource_id"],
            referring_source_id="SRC-3B1B",
            from_url="https://www.3blue1brown.com/topics/neural-networks",
            raw_href="/lessons/backpropagation-calculus",
            original_url="https://www.3blue1brown.com/lessons/backpropagation-calculus",
            link_text="Backpropagation Calculus",
            is_internal=True,
            target_scope="3B1B lesson page",
            recommended_decision="Metadata-first candidate (do not auto-crawl)",
            discovered_at="2026-10-24T10:04:00+00:00",
        )
        return {
            "hinton_internal": c1["candidate_id"],
            "hinton_external": c2["candidate_id"],
            "bengio_external": c3["candidate_id"],
            "bengio_internal": c4["candidate_id"],
            "b1b_internal": c5["candidate_id"],
            "hinton_resource_id": hinton_res["resource_id"],
            "bengio_resource_id": bengio_res["resource_id"],
        }

    def test_filtering_and_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "curation.sqlite"
            with KnowledgeStore(db_path) as store:
                ids = self._seed_sample_candidates(store)

                # Filter by source
                hinton_rows = store.list_discovered_link_candidates(referring_source_id="SRC-HINTON")
                self.assertEqual(len(hinton_rows), 2)
                self.assertEqual(
                    [r["candidate_id"] for r in hinton_rows],
                    [ids["hinton_internal"], ids["hinton_external"]],
                )

                # Filter by domain (case-insensitive)
                arxiv_rows = store.list_discovered_link_candidates(host_domain="ARXIV.ORG")
                self.assertEqual(len(arxiv_rows), 1)
                self.assertEqual(arxiv_rows[0]["candidate_id"], ids["bengio_external"])

                # Filter by domain_classification
                internal_rows = store.list_discovered_link_candidates(domain_classification="INTERNAL")
                external_rows = store.list_discovered_link_candidates(domain_classification="EXTERNAL")
                self.assertEqual(len(internal_rows), 3)
                self.assertEqual(len(external_rows), 2)

                # Pagination via query_discovered_link_candidates
                page1 = store.query_discovered_link_candidates(limit=2, offset=0)
                self.assertEqual(page1["total_count"], 5)
                self.assertEqual(page1["returned_count"], 2)
                self.assertTrue(page1["has_more"])

                page2 = store.query_discovered_link_candidates(limit=2, offset=2)
                self.assertEqual(page2["total_count"], 5)
                self.assertEqual(page2["returned_count"], 2)
                self.assertTrue(page2["has_more"])

                page3 = store.query_discovered_link_candidates(limit=2, offset=4)
                self.assertEqual(page3["total_count"], 5)
                self.assertEqual(page3["returned_count"], 1)
                self.assertFalse(page3["has_more"])

                combined_ids = [
                    r["candidate_id"]
                    for r in (*page1["items"], *page2["items"], *page3["items"])
                ]
                self.assertEqual(
                    combined_ids,
                    [
                        ids["hinton_internal"],
                        ids["hinton_external"],
                        ids["bengio_external"],
                        ids["bengio_internal"],
                        ids["b1b_internal"],
                    ],
                )

            # CLI list with --page and --page-size
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = curation.main(
                    [
                        "list",
                        "--db",
                        str(db_path),
                        "--classification",
                        "INTERNAL",
                        "--page",
                        "2",
                        "--page-size",
                        "2",
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["total_count"], 3)
            self.assertEqual(payload["returned_count"], 1)
            self.assertEqual(payload["offset"], 2)
            self.assertFalse(payload["has_more"])
            self.assertEqual(payload["items"][0]["candidate_id"], ids["b1b_internal"])

    def test_csv_and_json_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "export.sqlite"
            csv_path = Path(tmp) / "candidates.csv"
            json_path = Path(tmp) / "candidates.json"
            with KnowledgeStore(db_path) as store:
                ids = self._seed_sample_candidates(store)
                store.review_discovered_link_candidate(
                    candidate_id=ids["hinton_external"],
                    decision="ACCEPTED_FOR_REFERENCE",
                    reviewer="curator-alice",
                    reason="Landmark 2015 Nature Deep Learning review citation.",
                    reviewed_at="2026-10-24T12:00:00+00:00",
                )

            # CLI JSON export to file
            with contextlib.redirect_stdout(io.StringIO()):
                rc_json = curation.main(
                    [
                        "export",
                        "--db",
                        str(db_path),
                        "--format",
                        "json",
                        "--source",
                        "SRC-HINTON",
                        "--output",
                        str(json_path),
                    ]
                )
            self.assertEqual(rc_json, 0)
            exported_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(exported_json), 2)
            ext_item = next(r for r in exported_json if r["candidate_id"] == ids["hinton_external"])
            self.assertEqual(ext_item["review_status"], "ACCEPTED_FOR_REFERENCE")
            self.assertEqual(ext_item["reviewed_by"], "curator-alice")
            self.assertEqual(ext_item["fetch_permission_granted"], 0)
            self.assertEqual(
                ext_item["original_url"],
                "https://www.nature.com/articles/nature14539#citeas",
            )
            self.assertEqual(ext_item["referring_resource_id"], ids["hinton_resource_id"])

            # CLI CSV export to file
            with contextlib.redirect_stdout(io.StringIO()):
                rc_csv = curation.main(
                    [
                        "export",
                        "--db",
                        str(db_path),
                        "--format",
                        "csv",
                        "--review-status",
                        "ACCEPTED_FOR_REFERENCE",
                        "--output",
                        str(csv_path),
                    ]
                )
            self.assertEqual(rc_csv, 0)
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 1)
            self.assertEqual(reader[0]["candidate_id"], ids["hinton_external"])
            self.assertEqual(reader[0]["review_status"], "ACCEPTED_FOR_REFERENCE")
            self.assertEqual(reader[0]["fetch_permission_granted"], "0")
            self.assertEqual(
                reader[0]["original_url"],
                "https://www.nature.com/articles/nature14539#citeas",
            )
            self.assertEqual(reader[0]["referring_resource_id"], ids["hinton_resource_id"])

    def test_review_decision_persistence_idempotency_and_external_non_fetchable_invariant(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reviews.sqlite"
            with KnowledgeStore(db_path) as store:
                ids = self._seed_sample_candidates(store)
                initial_resources_count = len(store.list_resources())

                # 1. Accept external candidate for reference -> fetch_permission_granted MUST stay 0
                reviewed_ext = store.review_discovered_link_candidate(
                    candidate_id=ids["hinton_external"],
                    decision="ACCEPTED_FOR_REFERENCE",
                    reviewer="curator-alice",
                    reason="Preserve bibliographic citation to Nature review without fetching external publisher.",
                    reviewed_at="2026-10-24T12:10:00+00:00",
                )
                self.assertEqual(reviewed_ext["operation"], "reviewed")
                self.assertEqual(reviewed_ext["review_status"], "ACCEPTED_FOR_REFERENCE")
                self.assertEqual(reviewed_ext["fetch_permission_granted"], 0)
                self.assertEqual(
                    reviewed_ext["policy_state"],
                    "CURATION_ACCEPTED_REFERENCE_ONLY_NO_FETCH",
                )
                self.assertEqual(reviewed_ext["reviewed_by"], "curator-alice")
                self.assertEqual(reviewed_ext["reviewed_at"], "2026-10-24T12:10:00+00:00")
                self.assertEqual(reviewed_ext["review_count"], 1)
                self.assertIsNotNone(reviewed_ext["last_review_event_id"])
                # Audit fields preserved untouched
                self.assertEqual(
                    reviewed_ext["original_url"],
                    "https://www.nature.com/articles/nature14539#citeas",
                )
                self.assertEqual(reviewed_ext["referring_resource_id"], ids["hinton_resource_id"])
                # Candidate was NOT promoted into resources
                self.assertEqual(len(store.list_resources()), initial_resources_count)

                # 2. Idempotent repeat of exact same review decision
                repeat_ext = store.review_discovered_link_candidate(
                    candidate_id=ids["hinton_external"],
                    decision="ACCEPTED_FOR_REFERENCE",
                    reviewer="curator-alice",
                    reason="Preserve bibliographic citation to Nature review without fetching external publisher.",
                    reviewed_at="2026-10-24T12:15:00+00:00",
                )
                self.assertEqual(repeat_ext["operation"], "unchanged")
                self.assertEqual(repeat_ext["review_count"], 1)
                self.assertEqual(repeat_ext["reviewed_at"], "2026-10-24T12:10:00+00:00")
                self.assertEqual(
                    len(store.list_discovered_link_reviews(candidate_id=ids["hinton_external"])),
                    1,
                )

                # 3. Approve internal candidate for manifest consideration -> fetch_permission_granted STILL 0
                reviewed_int = store.review_discovered_link_candidate(
                    candidate_id=ids["hinton_internal"],
                    decision="APPROVED_FOR_MANIFEST",
                    reviewer="curator-bob",
                    reason="In-scope CSC321 course notes; queue for manifest addition and separate policy gate.",
                    reviewed_at="2026-10-24T12:20:00+00:00",
                )
                self.assertEqual(reviewed_int["review_status"], "APPROVED_FOR_MANIFEST")
                self.assertEqual(reviewed_int["fetch_permission_granted"], 0)
                self.assertEqual(
                    reviewed_int["policy_state"],
                    "SAME_ORIGIN_MANIFEST_CANDIDATE_REQUIRES_POLICY_GATE_NO_AUTO_FETCH",
                )

                # 4. Reject a candidate via CLI
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = curation.main(
                        [
                            "review",
                            "--db",
                            str(db_path),
                            "--candidate-id",
                            ids["bengio_internal"],
                            "--decision",
                            "REJECTED",
                            "--reviewer",
                            "curator-alice",
                            "--reason",
                            "General profile navigation link; not a learning or research artifact.",
                            "--reviewed-at",
                            "2026-10-24T12:30:00+00:00",
                            "--format",
                            "json",
                        ]
                    )
                self.assertEqual(rc, 0)
                rejected_payload = json.loads(out.getvalue())
                self.assertEqual(rejected_payload["review_status"], "REJECTED")
                self.assertEqual(rejected_payload["fetch_permission_granted"], 0)
                self.assertEqual(rejected_payload["policy_state"], "CURATION_REJECTED_NO_FETCH")

                # 5. Summary counts reflect pending, accepted-for-reference, approved-for-manifest, and rejected
                summary = store.summarize_discovered_link_candidates()
                self.assertEqual(summary["total_candidates"], 5)
                self.assertEqual(summary["pending_count"], 2)
                self.assertEqual(summary["accepted_for_reference_count"], 1)
                self.assertEqual(summary["approved_for_manifest_count"], 1)
                self.assertEqual(summary["rejected_count"], 1)
                self.assertEqual(summary["deferred_count"], 0)
                self.assertEqual(summary["fetch_permission_granted_count"], 0)
                self.assertEqual(summary["review_events_count"], 3)

                # 6. Provenance events recorded for all 3 distinct reviews
                prov_reviews = [
                    e
                    for e in store.list_provenance_events()
                    if e["event_type"] == "CANDIDATE_LINK_REVIEWED"
                ]
                self.assertEqual(len(prov_reviews), 3)

    def test_unsafe_url_rejection_in_validation_and_curation_review(self):
        unsafe_urls = [
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "file:///etc/hosts",
            "mailto:test@example.com",
            "ftp://ftp.example.com/paper.pdf",
            "https://user:password@example.com/private",
            "https://user@example.com/private",
            "https://@example.com/private",
            "http://localhost:8000/admin",
            "http://127.0.0.1/secret",
            "http://10.1.2.3/internal",
            "http://172.16.0.5/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:8080/local",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://printer.local/status",
            "https://service.internal/api",
            "https://bad..domain.example.com/page",
            "https://example.com\\@evil.com/path",
        ]
        for bad_url in unsafe_urls:
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(ValueError):
                    validate_and_canonicalize_url(bad_url)

        # Verify that if a database row has a tampered unsafe URL, non-rejection reviews are blocked
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tampered_url.sqlite"
            with KnowledgeStore(db_path) as store:
                ids = self._seed_sample_candidates(store)
                cid = ids["hinton_external"]
                with store.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE discovered_link_candidates
                           SET original_url = 'http://169.254.169.254/latest/meta-data/',
                               normalized_url = 'http://169.254.169.254/latest/meta-data/'
                         WHERE candidate_id = ?
                        """,
                        (cid,),
                    )

                with self.assertRaises(ValueError):
                    store.review_discovered_link_candidate(
                        candidate_id=cid,
                        decision="ACCEPTED_FOR_REFERENCE",
                        reviewer="curator-alice",
                        reason="Attempt to accept prohibited metadata IP",
                    )

                # Rejecting the tampered candidate is allowed so curators can quarantine bad records
                rejected = store.review_discovered_link_candidate(
                    candidate_id=cid,
                    decision="REJECTED",
                    reviewer="curator-alice",
                    reason="Quarantine prohibited link-local IP destination.",
                )
                self.assertEqual(rejected["review_status"], "REJECTED")
                self.assertEqual(rejected["fetch_permission_granted"], 0)

    def test_missing_or_empty_runtime_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_db = Path(tmp) / "does_not_exist.sqlite"

            # 1. summary on missing DB returns 0 and does not create the file
            out_sum = io.StringIO()
            with contextlib.redirect_stdout(out_sum):
                rc = curation.main(["summary", "--db", str(missing_db), "--format", "json"])
            self.assertEqual(rc, 0)
            self.assertFalse(missing_db.exists())
            sum_data = json.loads(out_sum.getvalue())
            self.assertFalse(sum_data["database_exists"])
            self.assertEqual(sum_data["total_candidates"], 0)
            self.assertEqual(sum_data["pending_count"], 0)

            # 2. list on missing DB returns 0 items and does not create the file
            out_list = io.StringIO()
            with contextlib.redirect_stdout(out_list):
                rc = curation.main(["list", "--db", str(missing_db), "--format", "json"])
            self.assertEqual(rc, 0)
            self.assertFalse(missing_db.exists())
            list_data = json.loads(out_list.getvalue())
            self.assertFalse(list_data["database_exists"])
            self.assertEqual(list_data["items"], [])

            # 3. export on missing DB returns empty CSV/JSON and does not create the DB file
            out_exp = io.StringIO()
            with contextlib.redirect_stdout(out_exp):
                rc = curation.main(["export", "--db", str(missing_db), "--format", "csv"])
            self.assertEqual(rc, 0)
            self.assertFalse(missing_db.exists())
            self.assertIn("candidate_id,referring_resource_id", out_exp.getvalue())

            # 4. history on missing DB returns empty list and does not create the DB file
            out_hist = io.StringIO()
            with contextlib.redirect_stdout(out_hist):
                rc = curation.main(["history", "--db", str(missing_db), "--format", "json"])
            self.assertEqual(rc, 0)
            self.assertFalse(missing_db.exists())
            self.assertEqual(json.loads(out_hist.getvalue()), [])

            # 5. review on missing DB fails cleanly with exit code 1 without creating DB file
            err_rev = io.StringIO()
            with contextlib.redirect_stderr(err_rev):
                rc = curation.main(
                    [
                        "review",
                        "--db",
                        str(missing_db),
                        "--candidate-id",
                        "CLINK-0000000000000000",
                        "--decision",
                        "REJECTED",
                        "--reviewer",
                        "curator-alice",
                        "--reason",
                        "Missing DB test",
                    ]
                )
            self.assertEqual(rc, 1)
            self.assertFalse(missing_db.exists())

            # 6. Empty initialized database (0 candidates)
            empty_db = Path(tmp) / "empty.sqlite"
            with KnowledgeStore(empty_db):
                pass
            out_empty = io.StringIO()
            with contextlib.redirect_stdout(out_empty):
                rc = curation.main(["summary", "--db", str(empty_db), "--format", "json"])
            self.assertEqual(rc, 0)
            empty_sum = json.loads(out_empty.getvalue())
            self.assertTrue(empty_sum["database_exists"])
            self.assertEqual(empty_sum["total_candidates"], 0)

    def test_v4_to_v5_migration_preserves_candidates_and_pilot_database_if_available(self):
        from knowledge_store import sha256_hex

        with tempfile.TemporaryDirectory() as tmp:
            v4_db = Path(tmp) / "legacy_v4.sqlite"
            conn = sqlite3.connect(str(v4_db))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            # Apply migrations 1..4 only
            for version, name, sql_marker, fn in MIGRATIONS[:4]:
                checksum = sha256_hex(f"{version}:{name}:{sql_marker}")
                with conn:
                    fn(conn)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (version, name, checksum, "2026-10-24T00:00:00+00:00"),
                    )
                    conn.execute(f"PRAGMA user_version = {version};")
            conn.close()

            # Insert a v4 candidate row before running migration 5
            with KnowledgeStore(v4_db, auto_initialize=False) as store:
                store.conn.execute(
                    """
                    INSERT INTO resources(
                        resource_id, source_id, canonical_url, title, resource_type,
                        license_status, permitted_storage_scope, permits_full_text_storage,
                        permits_verbatim_excerpts, legal_uncertainty_status, text_storage_status,
                        lifecycle_state, metadata_hash, first_discovered_at, last_verified_at, updated_at
                    ) VALUES (
                        'RES-0123456789abcdef', 'SRC-HINTON', 'https://www.cs.toronto.edu/~hinton/index.html',
                        'Hinton Home', 'Homepage', 'ALL_RIGHTS_RESERVED_METADATA_ONLY', 'METADATA_ONLY',
                        0, 0, 'REQUIRES_EXPLICIT_LICENSE_OR_PERMISSION', 'metadata-only',
                        'ACTIVE', '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', '2026-10-24T00:00:00+00:00', '2026-10-24T00:00:00+00:00',
                        '2026-10-24T00:00:00+00:00'
                    )
                    """
                )
                store.conn.execute(
                    """
                    INSERT INTO discovered_link_candidates(
                        candidate_id, referring_resource_id, referring_source_id, from_manifest_id,
                        from_url, raw_href, original_url, normalized_url, host_domain,
                        is_internal, domain_classification, target_scope, recommended_decision,
                        policy_state, review_status, fetch_permission_granted, promoted_resource_id,
                        link_text, rel, first_seen_at, last_seen_at, seen_count,
                        first_run_id, last_run_id, provenance_event_id
                    ) VALUES (
                        'CLINK-fedcba9876543210', 'RES-0123456789abcdef', 'SRC-HINTON', NULL,
                        'https://www.cs.toronto.edu/~hinton/index.html', 'https://arxiv.org/abs/1207.0580',
                        'https://arxiv.org/abs/1207.0580', 'https://arxiv.org/abs/1207.0580', 'arxiv.org',
                        0, 'EXTERNAL', 'External paper', 'Record citation only',
                        'EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH', 'PENDING_REVIEW', 0, NULL,
                        'Dropout paper', NULL, '2026-10-24T01:00:00+00:00', '2026-10-24T01:00:00+00:00',
                        1, NULL, NULL, NULL
                    )
                    """
                )
                store.conn.commit()

                applied = store.initialize()
                self.assertEqual(applied, [5])
                self.assertEqual(store.get_schema_version(), LATEST_SCHEMA_VERSION)

                migrated_row = store.get_discovered_link_candidate(
                    candidate_id="CLINK-fedcba9876543210"
                )
                self.assertIsNotNone(migrated_row)
                self.assertEqual(migrated_row["review_status"], "PENDING_REVIEW")
                self.assertEqual(migrated_row["review_count"], 0)
                self.assertIsNone(migrated_row["reviewed_by"])

                reviewed = store.review_discovered_link_candidate(
                    candidate_id="CLINK-fedcba9876543210",
                    decision="ACCEPTED_FOR_REFERENCE",
                    reviewer="curator-test",
                    reason="Migrated v4->v5 candidate accepted for reference.",
                )
                self.assertEqual(reviewed["review_status"], "ACCEPTED_FOR_REFERENCE")
                self.assertEqual(reviewed["fetch_permission_granted"], 0)

            # If local Task 012 pilot database exists, test migrating a temporary copy of it
            pilot_db = curation.DEFAULT_PILOT_DB_PATH
            if pilot_db.exists():
                pilot_copy = Path(tmp) / "task012_pilot_copy.sqlite"
                shutil.copy2(pilot_db, pilot_copy)
                with KnowledgeStore(pilot_copy) as pilot_store:
                    self.assertEqual(pilot_store.get_schema_version(), LATEST_SCHEMA_VERSION)
                    summary = pilot_store.summarize_discovered_link_candidates()
                    self.assertEqual(summary["total_candidates"], 181)
                    self.assertEqual(summary["pending_count"], 181)
                    self.assertEqual(summary["fetch_permission_granted_count"], 0)
                    self.assertEqual(
                        summary["by_domain_classification"],
                        {"INTERNAL": 105, "EXTERNAL": 76},
                    )
                    self.assertEqual(
                        summary["by_source"],
                        {"SRC-3B1B": 88, "SRC-BENGIO": 46, "SRC-HINTON": 47},
                    )


if __name__ == "__main__":
    unittest.main()
