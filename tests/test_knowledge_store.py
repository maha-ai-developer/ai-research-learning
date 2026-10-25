import contextlib
import io
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import collect
import knowledge_store
from knowledge_store import (
    DEFAULT_SEED_PATH,
    LATEST_SCHEMA_VERSION,
    KnowledgeStore,
    compute_resource_id,
    import_seed_manifest,
    init_db,
    sha256_hex,
    validate_and_canonicalize_url,
)


class KnowledgeStoreTests(unittest.TestCase):
    def test_database_creation_and_repeated_idempotent_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "store.sqlite"
            with init_db(db_path) as store:
                self.assertEqual(store.get_schema_version(), LATEST_SCHEMA_VERSION)
                migrations = store.list_migrations()
                self.assertEqual([m["version"] for m in migrations], [1, 2, 3, 4, 5, 6, 7, 8])
                user_ver = store.conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(user_ver, LATEST_SCHEMA_VERSION)
                fk_enabled = store.conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(fk_enabled, 1)

                sources = store.list_sources(only_complete=True)
                source_ids = [s["source_id"] for s in sources]
                self.assertEqual(
                    source_ids,
                    ["SRC-3B1B", "SRC-ARXIV", "SRC-BENGIO", "SRC-HINTON", "SRC-WIKIPEDIA"],
                )

                # Repeated initialization must be a no-op without errors or duplicates
                newly_applied = store.initialize()
                self.assertEqual(newly_applied, [])
                self.assertEqual(len(store.list_sources()), 5)

            # Re-opening existing DB again applies 0 migrations
            with init_db(db_path) as store2:
                self.assertEqual(store2.initialize(), [])
                self.assertEqual(store2.get_schema_version(), LATEST_SCHEMA_VERSION)

    def test_migration_checksum_mismatch_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tampered.sqlite"
            with init_db(db_path) as store:
                with store.transaction() as conn:
                    conn.execute(
                        "UPDATE schema_migrations SET checksum = 'bad_checksum' WHERE version = 1"
                    )
                with self.assertRaises(RuntimeError):
                    store.initialize()

    def test_migrates_existing_historical_sqlite_databases_preserving_all_data(self):
        repo_root = Path(__file__).resolve().parent.parent
        historical_files = [
            repo_root / "inventory" / "ai_research_inventory_early.sqlite",
            repo_root / "inventory" / "ai_research_metadata_inventory_prepared.sqlite",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for src_db in historical_files:
                self.assertTrue(src_db.exists(), f"Missing historical snapshot: {src_db}")
                copied_db = Path(tmp) / src_db.name
                shutil.copy2(src_db, copied_db)

                with KnowledgeStore(copied_db) as store:
                    self.assertEqual(store.get_schema_version(), LATEST_SCHEMA_VERSION)
                    # Original 52 source_urls and 6 crawl_audit rows must be preserved
                    su_count = store.conn.execute("SELECT COUNT(*) FROM source_urls").fetchone()[0]
                    audit_count = store.conn.execute("SELECT COUNT(*) FROM crawl_audit").fetchone()[0]
                    self.assertEqual(su_count, 52)
                    self.assertEqual(audit_count, 6)

                    # Backfilled into canonical resources, resource_versions, and provenance_events
                    resources = store.list_resources()
                    self.assertEqual(len(resources), 52)
                    ver_count = store.conn.execute("SELECT COUNT(*) FROM resource_versions").fetchone()[0]
                    self.assertEqual(ver_count, 52)
                    policy_events = store.conn.execute(
                        "SELECT COUNT(*) FROM provenance_events WHERE event_type = 'POLICY_CHECK'"
                    ).fetchone()[0]
                    self.assertEqual(policy_events, 6)

                # Subsequent manifest import on migrated DB is 100% idempotent
                summary = import_seed_manifest(copied_db, DEFAULT_SEED_PATH)
                self.assertEqual(summary.imported_count, 0)
                self.assertEqual(summary.updated_count, 0)
                self.assertEqual(summary.skipped_count, 52)
                self.assertEqual(summary.rejected_count, 0)

    def test_seed_manifest_import_and_repeated_idempotent_import_preserve_incomplete_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "manifest_import.sqlite"

            # First import of the 52-record seed_urls.json
            first = import_seed_manifest(db_path, DEFAULT_SEED_PATH)
            self.assertEqual(first.total_records, 52)
            self.assertEqual(first.imported_count, 52)
            self.assertEqual(first.updated_count, 0)
            self.assertEqual(first.skipped_count, 0)
            self.assertEqual(first.rejected_count, 0)

            # Second import of unchanged seed_urls.json must be idempotent
            second = import_seed_manifest(db_path, DEFAULT_SEED_PATH)
            self.assertEqual(second.total_records, 52)
            self.assertEqual(second.imported_count, 0)
            self.assertEqual(second.updated_count, 0)
            self.assertEqual(second.skipped_count, 52)
            self.assertEqual(second.rejected_count, 0)

            with KnowledgeStore(db_path) as store:
                resources = store.list_resources()
                self.assertEqual(len(resources), 52)

                # Verify no duplicate versions were created on second import
                total_versions = store.conn.execute("SELECT COUNT(*) FROM resource_versions").fetchone()[0]
                self.assertEqual(total_versions, 52)

                # Check known record mapping (HI-URL-001 -> SRC-HINTON, EXT-URL-004 -> SRC-ARXIV)
                hinton_home = store.get_resource(manifest_id="HI-URL-001")
                self.assertIsNotNone(hinton_home)
                self.assertEqual(hinton_home["source_id"], "SRC-HINTON")
                self.assertEqual(hinton_home["canonical_url"], "https://www.cs.toronto.edu/~hinton/index.html")
                # Missing fields in seed_urls.json must remain None (not fabricated)
                self.assertIsNone(hinton_home["authors"])
                self.assertIsNone(hinton_home["published_at"])
                self.assertIsNone(hinton_home["content_hash"])

                arxiv_rec = store.get_resource(manifest_id="EXT-URL-004")
                self.assertIsNotNone(arxiv_rec)
                self.assertEqual(arxiv_rec["source_id"], "SRC-ARXIV")

                # Check incomplete/external source metadata is preserved without fabrication
                ext_pub = store.get_resource(manifest_id="EXT-URL-001")
                self.assertIsNotNone(ext_pub)
                ext_source = store.get_source(ext_pub["source_id"])
                self.assertIsNotNone(ext_source)
                self.assertEqual(ext_source["metadata_complete"], 0)
                self.assertEqual(ext_source["status"], "RECORD_ONLY")
                self.assertIsNone(ext_source["origin"])
                self.assertIsNone(ext_source["robots_url"])
                self.assertIsNone(ext_source["attribution_template"])

    def test_manifest_import_detects_updates_and_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "custom_manifest.sqlite"
            manifest_path = Path(tmp) / "custom_seed.json"

            initial_rows = [
                {
                    "Manifest ID": "HI-URL-001",
                    "Person / source": "Geoffrey Hinton",
                    "URL": "https://www.cs.toronto.edu/~hinton/",
                    "Page / resource name": "Hinton Home v1",
                    "Resource type": "Discovery index",
                    "Domain scope": "First-party",
                    "Verification status": "Opened",
                    "Priority": "P1",
                    "Collection decision": "Metadata-first",
                },
                {
                    "Manifest ID": "BAD-URL-001",
                    "Person / source": "Geoffrey Hinton",
                    "URL": "ftp://www.cs.toronto.edu/~hinton/file.tar",
                    "Page / resource name": "Invalid scheme",
                    "Resource type": "Archive",
                },
                {
                    "Manifest ID": "",
                    "Person / source": "Geoffrey Hinton",
                    "URL": "https://www.cs.toronto.edu/~hinton/other.html",
                },
            ]
            manifest_path.write_text(json.dumps(initial_rows), encoding="utf-8")
            s1 = import_seed_manifest(db_path, manifest_path)
            self.assertEqual(s1.imported_count, 1)
            self.assertEqual(s1.updated_count, 0)
            self.assertEqual(s1.skipped_count, 0)
            self.assertEqual(s1.rejected_count, 2)
            self.assertEqual(len(s1.rejected_records), 2)

            # Update title of HI-URL-001 and add a duplicate URL under a conflicting Manifest ID
            updated_rows = [
                {
                    "Manifest ID": "HI-URL-001",
                    "Person / source": "Geoffrey Hinton",
                    "URL": "https://www.cs.toronto.edu/~hinton/",
                    "Page / resource name": "Hinton Home v2 (Updated Title)",
                    "Resource type": "Discovery index",
                    "Domain scope": "First-party",
                    "Verification status": "Opened",
                    "Priority": "P1",
                    "Collection decision": "Metadata-first",
                },
                {
                    "Manifest ID": "HI-URL-999-DUP",
                    "Person / source": "Geoffrey Hinton",
                    "URL": "https://www.cs.toronto.edu/~hinton/#fragment-stripped",
                    "Page / resource name": "Conflicting duplicate URL",
                    "Resource type": "Discovery index",
                },
            ]
            manifest_path.write_text(json.dumps(updated_rows), encoding="utf-8")
            s2 = import_seed_manifest(db_path, manifest_path)
            self.assertEqual(s2.imported_count, 0)
            self.assertEqual(s2.updated_count, 1)
            self.assertEqual(s2.skipped_count, 0)
            self.assertEqual(s2.rejected_count, 1)

            with KnowledgeStore(db_path) as store:
                res = store.get_resource(manifest_id="HI-URL-001")
                self.assertEqual(res["title"], "Hinton Home v2 (Updated Title)")
                self.assertEqual(res["version_count"], 2)
                versions = store.get_resource_versions(res["resource_id"])
                self.assertEqual([v["version_number"] for v in versions], [1, 2])
                self.assertEqual(versions[0]["title"], "Hinton Home v1")
                self.assertEqual(versions[1]["title"], "Hinton Home v2 (Updated Title)")

    def test_url_and_source_validation(self):
        self.assertEqual(
            validate_and_canonicalize_url("HTTPS://WWW.CS.TORONTO.EDU:443/~hinton/index.html#top"),
            "https://www.cs.toronto.edu/~hinton/index.html",
        )
        invalid_urls = [
            "",
            "   ",
            None,
            123,
            "not-a-url",
            "ftp://example.com/file",
            "javascript:alert(1)",
            "https://",
            "https://user:pass@example.com/path",
            "https://example.com/path with spaces",
            "https://invalid_host_without_tld/path",
        ]
        for bad in invalid_urls:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_and_canonicalize_url(bad)

        with tempfile.TemporaryDirectory() as tmp:
            with KnowledgeStore(Path(tmp) / "val.sqlite") as store:
                with self.assertRaises(ValueError):
                    store.register_source(
                        source_id="INVALID-PREFIX",
                        display_name="Bad ID",
                        source_category="TEST",
                        adapter_type="TEST",
                    )
                with self.assertRaises(ValueError):
                    store.register_source(
                        source_id="SRC-TEST",
                        display_name="   ",
                        source_category="TEST",
                        adapter_type="TEST",
                    )

    def test_resource_version_history_on_metadata_and_content_hash_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with KnowledgeStore(Path(tmp) / "versions.sqlite") as store:
                url = "https://www.cs.toronto.edu/~hinton/absps/backprop.html"
                r1 = store.upsert_resource(
                    source_id="SRC-HINTON",
                    canonical_url=url,
                    title="Learning Representations by Back-Propagating Errors",
                    resource_type="Publication detail page",
                    authors="Rumelhart, Hinton, Williams",
                    published_at="1986-10-09",
                )
                self.assertEqual(r1["operation"], "inserted")
                self.assertEqual(r1["version_count"], 1)
                v1_id = r1["current_version_id"]

                # Identical upsert -> unchanged, no new version
                r1_repeat = store.upsert_resource(
                    source_id="SRC-HINTON",
                    canonical_url=url,
                    title="Learning Representations by Back-Propagating Errors",
                    resource_type="Publication detail page",
                    authors="Rumelhart, Hinton, Williams",
                    published_at="1986-10-09",
                )
                self.assertEqual(r1_repeat["operation"], "unchanged")
                self.assertEqual(r1_repeat["version_count"], 1)
                self.assertEqual(r1_repeat["current_version_id"], v1_id)

                # Metadata change (abstract added) -> version 2 created
                r2 = store.upsert_resource(
                    source_id="SRC-HINTON",
                    canonical_url=url,
                    title="Learning Representations by Back-Propagating Errors",
                    resource_type="Publication detail page",
                    authors="Rumelhart, Hinton, Williams",
                    published_at="1986-10-09",
                    abstract_or_description="We describe a new learning procedure, back-propagation.",
                    change_reason="abstract_enriched",
                )
                self.assertEqual(r2["operation"], "updated")
                self.assertEqual(r2["version_count"], 2)
                v2_id = r2["current_version_id"]
                self.assertNotEqual(v1_id, v2_id)

                # Content hash change -> version 3 created
                content_sha = sha256_hex("normalized-page-content-v1")
                r3 = store.upsert_resource(
                    source_id="SRC-HINTON",
                    canonical_url=url,
                    title="Learning Representations by Back-Propagating Errors",
                    resource_type="Publication detail page",
                    authors="Rumelhart, Hinton, Williams",
                    published_at="1986-10-09",
                    abstract_or_description="We describe a new learning procedure, back-propagation.",
                    content_hash=content_sha,
                    change_reason="content_hash_recorded",
                )
                self.assertEqual(r3["operation"], "updated")
                self.assertEqual(r3["version_count"], 3)
                self.assertEqual(r3["content_hash"], content_sha)

                versions = store.get_resource_versions(r1["resource_id"])
                self.assertEqual(len(versions), 3)
                self.assertEqual([v["version_number"] for v in versions], [1, 2, 3])
                self.assertIsNone(versions[0]["abstract_or_description"])
                self.assertIsNotNone(versions[1]["abstract_or_description"])
                self.assertIsNone(versions[1]["content_hash"])
                self.assertEqual(versions[2]["content_hash"], content_sha)
                for ver in versions:
                    self.assertTrue(ver["provenance_event_id"].startswith("prov_"))

    def test_provenance_and_collection_runs_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            with KnowledgeStore(Path(tmp) / "runs.sqlite") as store:
                run_id = store.start_collection_run(
                    run_type="DRY_RUN",
                    is_dry_run=True,
                    manifest_path="seed_urls.json",
                    selected_count=10,
                    notes="Unit test dry run",
                )
                run_initial = store.get_collection_run(run_id)
                self.assertEqual(run_initial["status"], "running")
                self.assertEqual(run_initial["is_dry_run"], 1)

                res = store.upsert_resource(
                    source_id="SRC-BENGIO",
                    canonical_url="https://yoshuabengio.org/en",
                    title="Yoshua Bengio Home",
                    resource_type="Discovery index",
                    run_id=run_id,
                )
                finished = store.finish_collection_run(
                    run_id,
                    status="completed",
                    imported_count=1,
                    skipped_count=9,
                )
                self.assertEqual(finished["status"], "completed")
                self.assertEqual(finished["imported_count"], 1)
                self.assertEqual(finished["skipped_count"], 9)
                self.assertIsNotNone(finished["finished_at"])

                events = store.list_provenance_events(run_id=run_id)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["resource_id"], res["resource_id"])
                self.assertEqual(events[0]["event_type"], "RESOURCE_CREATED")

    def test_foreign_key_and_uniqueness_constraints_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            with KnowledgeStore(Path(tmp) / "constraints.sqlite") as store:
                # 1. Foreign key violation: unknown source_id
                with self.assertRaises(sqlite3.IntegrityError):
                    store.upsert_resource(
                        source_id="SRC-DOES-NOT-EXIST",
                        canonical_url="https://example.org/item",
                        title="Orphan resource",
                        resource_type="Article",
                    )

                # 2. Foreign key violation: unknown manifest_id not in source_urls
                with self.assertRaises(sqlite3.IntegrityError):
                    store.upsert_resource(
                        source_id="SRC-HINTON",
                        manifest_id="MISSING-MANIFEST-ID",
                        canonical_url="https://www.cs.toronto.edu/~hinton/test.html",
                        title="Test",
                        resource_type="Article",
                    )

                # 3. Valid resource insert, then test ON DELETE RESTRICT on sources
                res = store.upsert_resource(
                    source_id="SRC-HINTON",
                    canonical_url="https://www.cs.toronto.edu/~hinton/valid.html",
                    title="Valid Resource",
                    resource_type="Article",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction() as conn:
                        conn.execute("DELETE FROM sources WHERE source_id = 'SRC-HINTON'")

                # 4. Unique constraint on resource_versions(resource_id, version_number)
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction() as conn:
                        conn.execute(
                            """
                            INSERT INTO resource_versions(
                                version_id, resource_id, version_number, canonical_url,
                                title, resource_type, license_status, text_storage_status,
                                metadata_hash, change_reason, created_at
                            ) VALUES (?, ?, 1, ?, 'Dup Ver', 'Article', 'ALL_RIGHTS_RESERVED_METADATA_ONLY',
                                      'metadata-only', ?, 'dup', '2026-10-24T00:00:00+00:00')
                            """,
                            (
                                "ver_20261024_deadbeef_v1",
                                res["resource_id"],
                                res["canonical_url"],
                                sha256_hex("different-hash"),
                            ),
                        )

    def test_transaction_rollback_on_failure_leaves_no_partial_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with KnowledgeStore(Path(tmp) / "rollback.sqlite") as store:
                initial_count = len(store.list_resources())
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.upsert_resource(
                            source_id="SRC-HINTON",
                            canonical_url="https://www.cs.toronto.edu/~hinton/temp.html",
                            title="Should Roll Back",
                            resource_type="Article",
                        )
                        raise RuntimeError("Simulated mid-transaction failure")

                self.assertEqual(len(store.list_resources()), initial_count)
                self.assertIsNone(
                    store.get_resource(canonical_url="https://www.cs.toronto.edu/~hinton/temp.html")
                )

    def test_offline_operations_perform_zero_network_requests(self):
        def forbid_socket(*_args, **_kwargs):
            raise AssertionError("Network socket access is forbidden during local store and dry-run operations")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "offline.sqlite"
            with patch.object(socket, "socket", side_effect=forbid_socket), patch.object(
                socket, "create_connection", side_effect=forbid_socket
            ), contextlib.redirect_stdout(io.StringIO()):
                # 1. KnowledgeStore CLI init, import-manifest, and status
                self.assertEqual(knowledge_store.main(["init", "--db", str(db_path)]), 0)
                self.assertEqual(
                    knowledge_store.main(
                        ["import-manifest", "--db", str(db_path), "--manifest", str(DEFAULT_SEED_PATH)]
                    ),
                    0,
                )
                self.assertEqual(knowledge_store.main(["status", "--db", str(db_path)]), 0)

                # 2. collect.py dry run and offline flags
                self.assertEqual(collect.main(["--db", str(db_path)]), 0)
                self.assertEqual(collect.main(["--db", str(db_path), "--init-db"]), 0)
                self.assertEqual(collect.main(["--db", str(db_path), "--import-manifest"]), 0)

    def test_discovered_link_candidates_idempotency_domain_classification_and_unsafe_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "candidates.sqlite"
            with KnowledgeStore(db_path) as store:
                parent = store.upsert_resource(
                    source_id="SRC-HINTON",
                    canonical_url="https://www.cs.toronto.edu/~hinton/index.html",
                    title="Geoffrey Hinton Home",
                    resource_type="Discovery index",
                )
                parent_id = parent["resource_id"]
                self.assertEqual(len(store.list_resources()), 1)

                # 1. Insert internal discovered link candidate
                c1 = store.upsert_discovered_link_candidate(
                    referring_resource_id=parent_id,
                    referring_source_id="SRC-HINTON",
                    from_url="https://www.cs.toronto.edu/~hinton/index.html",
                    raw_href="/~hinton/csc321/notes.html#lec1",
                    original_url="https://www.cs.toronto.edu/~hinton/csc321/notes.html#lec1",
                    link_text="CSC321 Notes",
                    is_internal=True,
                    target_scope="Hinton in-scope HTML subpage",
                    recommended_decision="Metadata-first candidate (do not auto-crawl)",
                    discovered_at="2026-10-24T08:00:00+00:00",
                    run_id="run_test_1",
                )
                self.assertEqual(c1["operation"], "inserted")
                self.assertEqual(c1["seen_count"], 1)
                self.assertEqual(c1["is_internal"], 1)
                self.assertEqual(c1["domain_classification"], "INTERNAL")
                self.assertEqual(c1["review_status"], "PENDING_REVIEW")
                self.assertEqual(c1["fetch_permission_granted"], 0)
                self.assertIsNone(c1["promoted_resource_id"])
                self.assertEqual(
                    c1["original_url"],
                    "https://www.cs.toronto.edu/~hinton/csc321/notes.html#lec1",
                )
                self.assertEqual(
                    c1["normalized_url"],
                    "https://www.cs.toronto.edu/~hinton/csc321/notes.html",
                )

                # 2. Repeated discovery of same candidate updates last_seen_at and seen_count without duplicating rows
                c1_repeat = store.upsert_discovered_link_candidate(
                    referring_resource_id=parent_id,
                    referring_source_id="SRC-HINTON",
                    from_url="https://www.cs.toronto.edu/~hinton/index.html",
                    raw_href="/~hinton/csc321/notes.html#lec2",
                    original_url="https://www.cs.toronto.edu/~hinton/csc321/notes.html#lec2",
                    link_text="CSC321 Notes Updated Anchor",
                    is_internal=True,
                    target_scope="Hinton in-scope HTML subpage",
                    recommended_decision="Metadata-first candidate (do not auto-crawl)",
                    discovered_at="2026-10-24T09:00:00+00:00",
                    run_id="run_test_2",
                )
                self.assertEqual(c1_repeat["operation"], "updated")
                self.assertEqual(c1_repeat["candidate_id"], c1["candidate_id"])
                self.assertEqual(c1_repeat["seen_count"], 2)
                self.assertEqual(c1_repeat["first_seen_at"], "2026-10-24T08:00:00+00:00")
                self.assertEqual(c1_repeat["last_seen_at"], "2026-10-24T09:00:00+00:00")
                # Original first-discovered audit URL is preserved
                self.assertEqual(
                    c1_repeat["original_url"],
                    "https://www.cs.toronto.edu/~hinton/csc321/notes.html#lec1",
                )

                # 3. External discovered link is classified EXTERNAL without fetch permission and without promoting into resources
                c_ext = store.upsert_discovered_link_candidate(
                    referring_resource_id=parent_id,
                    referring_source_id="SRC-HINTON",
                    from_url="https://www.cs.toronto.edu/~hinton/index.html",
                    raw_href="https://nature.com/articles/nature14539",
                    original_url="https://nature.com/articles/nature14539",
                    link_text="Deep Learning Nature Review",
                    is_internal=False,
                    target_scope="External reference / citation",
                    recommended_decision="Record citation only (do not crawl external domain)",
                )
                self.assertEqual(c_ext["domain_classification"], "EXTERNAL")
                self.assertEqual(c_ext["is_internal"], 0)
                self.assertEqual(c_ext["fetch_permission_granted"], 0)
                self.assertEqual(c_ext["policy_state"], "EXTERNAL_DOMAIN_RECORD_ONLY_NO_FETCH")
                # Candidates must NEVER be automatically promoted into `resources`
                self.assertEqual(len(store.list_resources()), 1)
                self.assertEqual(len(store.list_discovered_link_candidates()), 2)

                # 4. Unsafe schemes (javascript:, data:, file:, mailto:) are rejected and audited in provenance_events
                for unsafe_url in [
                    "javascript:alert('xss')",
                    "data:text/html,<script>alert(1)</script>",
                    "file:///etc/passwd",
                    "mailto:user@example.com",
                ]:
                    with self.assertRaises(ValueError):
                        store.upsert_discovered_link_candidate(
                            referring_resource_id=parent_id,
                            referring_source_id="SRC-HINTON",
                            from_url="https://www.cs.toronto.edu/~hinton/index.html",
                            raw_href=unsafe_url,
                            original_url=unsafe_url,
                            is_internal=False,
                            target_scope="Unsafe test",
                            recommended_decision="Reject",
                        )

                rejected_events = [
                    e
                    for e in store.list_provenance_events(resource_id=parent_id)
                    if e["event_type"] == "DISCOVERED_LINK_CANDIDATE_REJECTED"
                ]
                self.assertEqual(len(rejected_events), 4)
                recorded_originals = {
                    json.loads(e["details_json"])["original_url"] for e in rejected_events
                }
                self.assertIn("javascript:alert('xss')", recorded_originals)
                self.assertIn("file:///etc/passwd", recorded_originals)


if __name__ == "__main__":
    unittest.main()
