"""Offline test suite for Task 034: Agent-First AI Learning Platform & Curriculum Slice."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
from http import HTTPStatus
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from knowledge_store import KnowledgeStore
from src.ai_research_learning.api import LocalResearchLibraryAPI, create_http_server
from src.ai_research_learning.platform import (
    AgentCapability,
    CURRICULUM_SCHEMA_VERSION,
    ConceptGraph,
    ConceptNode,
    CurriculumCitation,
    CurriculumManagementService,
    CurriculumPlan,
    CurriculumStatus,
    CurriculumValidator,
    DEFAULT_CONCEPT_NODES,
    EvidenceTier,
    ExecutionPolicy,
    MASTERY_ASSESSMENT_DISCLAIMER,
    PlatformOrchestrator,
    PlatformValidationError,
    SourceEcosystemService,
    THE_BATCH_SOURCE_ID,
    WorkflowStatus,
    build_default_concept_graph,
    format_curriculum_plan,
    get_logical_agent_catalog,
    main as platform_cli_main,
    validate_learning_goal,
)
from src.ai_research_learning.rag import DEFAULT_CHROMA_DIR, DEFAULT_RUNTIME_DB_PATH


EXAMPLE_GOAL = (
    "I want to become an AI engineer and eventually an AI researcher, "
    "starting from mathematical and programming foundations."
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class TestAgentFirstLearningPlatform(unittest.TestCase):
    """Comprehensive offline verification of Task 034 architecture and vertical slice."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_path = DEFAULT_RUNTIME_DB_PATH
        cls.chroma_dir = DEFAULT_CHROMA_DIR
        cls.initial_db_sha256 = _sha256_file(cls.db_path)

    def test_01_learning_goal_input_validation(self) -> None:
        """Verify deterministic validation of learner goals before agent invocation."""
        valid = validate_learning_goal("  Learn   backpropagation and   Transformers  ")
        self.assertEqual(valid, "Learn backpropagation and Transformers")

        with self.assertRaises(PlatformValidationError) as ctx_type:
            validate_learning_goal(12345)
        self.assertEqual(ctx_type.exception.code, "INVALID_GOAL_TYPE")

        with self.assertRaises(PlatformValidationError) as ctx_short:
            validate_learning_goal("too short")
        self.assertEqual(ctx_short.exception.code, "GOAL_TOO_SHORT")

        with self.assertRaises(PlatformValidationError) as ctx_long:
            validate_learning_goal("a" * 1005)
        self.assertEqual(ctx_long.exception.code, "GOAL_TOO_LONG")

    def test_02_logical_agent_catalog_and_tool_permission_boundaries(self) -> None:
        """Verify all six logical agent capabilities and ToolRegistry permission/budget guardrails."""
        catalog = get_logical_agent_catalog()
        capabilities = [spec.capability for spec in catalog]
        self.assertEqual(
            capabilities,
            [
                AgentCapability.RESEARCH,
                AgentCapability.KNOWLEDGE_CURATOR,
                AgentCapability.LEARNING_PLANNER,
                AgentCapability.TEACHING,
                AgentCapability.ASSESSMENT,
                AgentCapability.LEARNING_PROGRESS,
            ],
        )
        for spec in catalog:
            d = spec.to_dict()
            self.assertTrue(d["display_name"])
            self.assertTrue(d["allowed_tools"])
            self.assertTrue(d["deterministic_guardrails"])

        orchestrator = PlatformOrchestrator(db_path=self.db_path, chroma_dir=self.chroma_dir)
        dummy_run, _ = orchestrator.run_learning_planner(EXAMPLE_GOAL)

        # TEACHING agent is not permitted to invoke list_discovered_candidates (reserved for KNOWLEDGE_CURATOR)
        with self.assertRaises(PlatformValidationError) as ctx_perm:
            orchestrator.tool_registry.invoke(
                run_state=dummy_run,
                capability=AgentCapability.TEACHING,
                tool_name="list_discovered_candidates",
            )
        self.assertEqual(ctx_perm.exception.code, "TOOL_PERMISSION_DENIED")

        # Unknown tool is rejected
        with self.assertRaises(PlatformValidationError) as ctx_unknown:
            orchestrator.tool_registry.invoke(
                run_state=dummy_run,
                capability=AgentCapability.LEARNING_PLANNER,
                tool_name="nonexistent_tool_xyz",
            )
        self.assertEqual(ctx_unknown.exception.code, "UNKNOWN_TOOL")

        # Exceeding max_tool_calls budget fails deterministically
        tight_policy = ExecutionPolicy(max_tool_calls=2)
        with self.assertRaises(PlatformValidationError) as ctx_budget:
            orchestrator.run_learning_planner(EXAMPLE_GOAL, policy_override=tight_policy)
        self.assertEqual(ctx_budget.exception.code, "TOOL_BUDGET_EXCEEDED")

    def test_03_source_ecosystem_five_tiers_and_the_batch_governance(self) -> None:
        """Verify exact 5-tier epistemic classification and SRC-THE-BATCH discovery-only policy."""
        eco = SourceEcosystemService(self.db_path)
        summary = eco.summarize_ecosystem()

        self.assertEqual(summary.total_registered_sources, 11)
        self.assertEqual(summary.approved_operational_sources, 10)
        self.assertEqual(summary.candidate_pending_sources, 1)
        self.assertEqual(summary.tier_counts[EvidenceTier.DISCOVERED_LINK.value], 181)
        self.assertEqual(summary.tier_counts[EvidenceTier.APPROVED_UNCOLLECTED_RESOURCE.value], 48)
        self.assertEqual(summary.tier_counts[EvidenceTier.COLLECTED_DOCUMENT.value], 12)
        self.assertEqual(summary.tier_counts[EvidenceTier.EXTRACTED_KNOWLEDGE.value], 6)
        self.assertEqual(summary.tier_counts[EvidenceTier.CURRICULUM_READY_EVIDENCE.value], 18)

        batch_gov = summary.the_batch_governance
        self.assertEqual(batch_gov["source_id"], THE_BATCH_SOURCE_ID)
        self.assertEqual(batch_gov["status"], "CANDIDATE_PENDING_REVIEW")
        self.assertFalse(batch_gov["permits_full_text_storage"])
        self.assertFalse(batch_gov["can_ground_curriculum_citations"])
        self.assertFalse(batch_gov["can_override_foundational_prerequisites"])

        approved_resources = eco.list_approved_resources()
        self.assertEqual(len(approved_resources), 60)
        self.assertTrue(all(r["source_id"] != THE_BATCH_SOURCE_ID for r in approved_resources))

        candidates = eco.list_discovered_candidates(limit=10)
        self.assertEqual(len(candidates), 10)
        self.assertTrue(
            all(c["evidence_tier"] == EvidenceTier.DISCOVERED_LINK.value for c in candidates)
        )

    def test_04_concept_graph_dag_validation_and_cycle_detection(self) -> None:
        """Verify topological sorting, transitive prerequisite closure, and cycle/missing node detection."""
        graph = build_default_concept_graph()
        topo_order = graph.validate_dag()
        self.assertEqual(len(topo_order), 8)
        self.assertEqual(topo_order[0], "math_linear_algebra_calculus")
        self.assertLess(
            topo_order.index("math_linear_algebra_calculus"),
            topo_order.index("backpropagation_and_gradient_descent"),
        )
        self.assertLess(
            topo_order.index("backpropagation_and_gradient_descent"),
            topo_order.index("deep_residual_networks"),
        )
        self.assertLess(
            topo_order.index("sequence_alignment_and_bahdanau_attention"),
            topo_order.index("transformer_self_attention_architecture"),
        )
        self.assertLess(
            topo_order.index("deep_residual_networks"),
            topo_order.index("transformer_self_attention_architecture"),
        )

        # Focused goal on Transformers must automatically include transitive prerequisites
        selected = graph.select_concepts_for_goal(
            "I want to understand the Transformer multi-head self-attention architecture."
        )
        selected_ids = [n.concept_id for n in selected]
        self.assertIn("math_linear_algebra_calculus", selected_ids)
        self.assertIn("backpropagation_and_gradient_descent", selected_ids)
        self.assertIn("deep_residual_networks", selected_ids)
        self.assertIn("sequence_alignment_and_bahdanau_attention", selected_ids)
        self.assertIn("transformer_self_attention_architecture", selected_ids)

        # Missing prerequisite node must fail validation
        bad_missing_node = replace(
            DEFAULT_CONCEPT_NODES[0],
            prerequisites=("nonexistent_concept_node",),
        )
        with self.assertRaises(PlatformValidationError) as ctx_missing:
            ConceptGraph((bad_missing_node,))
        self.assertEqual(ctx_missing.exception.code, "MISSING_PREREQUISITE_NODE")

        # Cycle in prerequisite graph must fail validation
        node_a = replace(
            DEFAULT_CONCEPT_NODES[0],
            concept_id="concept_a",
            prerequisites=("concept_b",),
        )
        node_b = replace(
            DEFAULT_CONCEPT_NODES[1],
            concept_id="concept_b",
            prerequisites=("concept_a",),
        )
        with self.assertRaises(PlatformValidationError) as ctx_cycle:
            ConceptGraph((node_a, node_b))
        self.assertEqual(ctx_cycle.exception.code, "PREREQUISITE_CYCLE_DETECTED")

    def test_05_end_to_end_learning_planner_vertical_slice(self) -> None:
        """Verify end-to-end execution of LearningPlannerAgent on the Task 034 example goal."""
        orchestrator = PlatformOrchestrator(db_path=self.db_path, chroma_dir=self.chroma_dir)
        run_state, plan = orchestrator.run_learning_planner(EXAMPLE_GOAL)

        self.assertEqual(run_state.status, WorkflowStatus.COMPLETED)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.status, CurriculumStatus.VALIDATED)
        self.assertEqual(plan.schema_version, CURRICULUM_SCHEMA_VERSION)
        self.assertEqual(plan.mastery_assessment_disclaimer, MASTERY_ASSESSMENT_DISCLAIMER)
        self.assertTrue(plan.validation_report.is_valid)
        self.assertTrue(plan.validation_report.schema_valid)
        self.assertTrue(plan.validation_report.prerequisites_consistent)
        self.assertTrue(plan.validation_report.citations_verified)
        self.assertTrue(plan.validation_report.mastery_disclaimer_present)
        self.assertTrue(plan.validation_report.the_batch_policy_respected)

        # Verify 4 stages and 8 lessons
        self.assertEqual(len(plan.stages), 4)
        lessons = [lesson for stage in plan.stages for lesson in stage.lessons]
        self.assertEqual(len(lessons), 8)

        # Every lesson has required fields and verified citations
        cited_chunk_ids = set()
        for lesson in lessons:
            self.assertTrue(lesson.lesson_id)
            self.assertTrue(lesson.learning_objectives)
            self.assertTrue(lesson.explanation)
            self.assertTrue(lesson.exercises)
            self.assertTrue(lesson.projects)
            self.assertTrue(lesson.rationale_for_ordering)
            self.assertTrue(lesson.source_citations)
            self.assertEqual(
                lesson.mastery_check.assessment_status,
                "DESIGN_ONLY_NOT_YET_ASSESSED",
            )
            for cit in lesson.source_citations:
                self.assertNotEqual(cit.source_id, THE_BATCH_SOURCE_ID)
                self.assertNotEqual(cit.evidence_tier, EvidenceTier.DISCOVERED_LINK)
                if cit.chunk_id:
                    cited_chunk_ids.add(cit.chunk_id)

        self.assertGreaterEqual(len(cited_chunk_ids), 6)
        self.assertGreaterEqual(len(plan.evidence_gaps), 4)

        # Verify Markdown and JSON formatting
        md_out = format_curriculum_plan(plan, "markdown")
        json_out = format_curriculum_plan(plan, "json")
        self.assertIn("Mastery Assessment Disclaimer:", md_out)
        self.assertIn("SRC-THE-BATCH", md_out)
        parsed_json = json.loads(json_out)
        self.assertEqual(parsed_json["curriculum_id"], plan.curriculum_id)

    def test_06_curriculum_validator_catches_prerequisite_and_citation_regressions(self) -> None:
        """Verify CurriculumValidator deterministically catches fabricated citations and prerequisite inversions."""
        orchestrator = PlatformOrchestrator(db_path=self.db_path, chroma_dir=self.chroma_dir)
        _, valid_plan = orchestrator.run_learning_planner(EXAMPLE_GOAL)
        assert valid_plan is not None
        validator = orchestrator.validator

        # 1. Inject a fabricated chunk_id into a lesson citation
        stage0 = valid_plan.stages[0]
        lesson0 = stage0.lessons[0]
        bad_cit = CurriculumCitation(
            citation_id="cit_fabricated",
            evidence_tier=EvidenceTier.CURRICULUM_READY_EVIDENCE,
            source_id="SRC-WIKIPEDIA",
            resource_id="res_24d64fe06f34c722",
            title="Backpropagation",
            canonical_url="https://en.wikipedia.org/wiki/Backpropagation",
            chunk_id="chk_deadbeef99999999",
        )
        bad_lesson = replace(lesson0, source_citations=(*lesson0.source_citations, bad_cit))
        bad_stage = replace(stage0, lessons=(bad_lesson, *stage0.lessons[1:]))
        bad_plan_cit = replace(valid_plan, stages=(bad_stage, *valid_plan.stages[1:]))
        report_cit = validator.validate(bad_plan_cit)
        self.assertFalse(report_cit.is_valid)
        self.assertFalse(report_cit.citations_verified)
        self.assertTrue(any("chk_deadbeef99999999" in e for e in report_cit.errors))

        # 2. Inject an unverified inline [chk_...] token in lesson explanation prose
        bad_prose_lesson = replace(
            lesson0,
            explanation=lesson0.explanation + " Fabricated inline ref [chk_cafebabe12345678].",
        )
        bad_prose_stage = replace(stage0, lessons=(bad_prose_lesson, *stage0.lessons[1:]))
        bad_plan_prose = replace(valid_plan, stages=(bad_prose_stage, *valid_plan.stages[1:]))
        report_prose = validator.validate(bad_plan_prose)
        self.assertFalse(report_prose.is_valid)
        self.assertFalse(report_prose.citations_verified)
        self.assertTrue(any("chk_cafebabe12345678" in e for e in report_prose.errors))

        # 3. Invert prerequisite order (place lesson_01 after lesson_02 while lesson_02 requires lesson_01)
        inverted_lessons = (
            replace(stage0.lessons[1], lesson_order=1),
            replace(stage0.lessons[0], lesson_order=2),
        )
        inverted_stage = replace(stage0, lessons=inverted_lessons)
        bad_plan_order = replace(valid_plan, stages=(inverted_stage, *valid_plan.stages[1:]))
        report_order = validator.validate(bad_plan_order)
        self.assertFalse(report_order.is_valid)
        self.assertFalse(report_order.prerequisites_consistent)

        # 4. Cite SRC-THE-BATCH candidate source
        batch_cit = CurriculumCitation(
            citation_id="cit_the_batch",
            evidence_tier=EvidenceTier.COLLECTED_DOCUMENT,
            source_id=THE_BATCH_SOURCE_ID,
            resource_id="res_24d64fe06f34c722",
            title="The Batch News",
            canonical_url="https://www.deeplearning.ai/the-batch",
        )
        batch_lesson = replace(lesson0, source_citations=(*lesson0.source_citations, batch_cit))
        batch_stage = replace(stage0, lessons=(batch_lesson, *stage0.lessons[1:]))
        bad_plan_batch = replace(valid_plan, stages=(batch_stage, *valid_plan.stages[1:]))
        report_batch = validator.validate(bad_plan_batch)
        self.assertFalse(report_batch.is_valid)
        self.assertFalse(report_batch.the_batch_policy_respected)

    def test_07_insufficient_evidence_and_off_domain_abstention(self) -> None:
        """Verify abstention when the learner goal is off-domain or the knowledge store is empty."""
        orchestrator = PlatformOrchestrator(db_path=self.db_path, chroma_dir=self.chroma_dir)
        run_state, plan = orchestrator.run_learning_planner(
            "Teach me artisan sourdough bread baking and pottery glazing techniques."
        )
        self.assertEqual(run_state.status, WorkflowStatus.ABSTAINED)
        assert plan is not None
        self.assertEqual(plan.status, CurriculumStatus.ABSTAINED_INSUFFICIENT_EVIDENCE)
        self.assertIsNotNone(plan.abstention_reason)
        self.assertEqual(len(plan.stages), 0)
        self.assertEqual(len(plan.source_citations), 0)
        self.assertTrue(plan.validation_report.is_valid)

        # Also test against a fresh, empty migrated SQLite DB with zero chunks/resources
        with tempfile.TemporaryDirectory() as tmp_dir:
            empty_db = Path(tmp_dir) / "empty.sqlite"
            ks = KnowledgeStore(empty_db)
            ks.initialize()
            ks.close()

            empty_orchestrator = PlatformOrchestrator(
                db_path=empty_db,
                chroma_dir=Path(tmp_dir) / "no_chroma",
            )
            empty_run, empty_plan = empty_orchestrator.run_learning_planner(EXAMPLE_GOAL)
            self.assertEqual(empty_run.status, WorkflowStatus.ABSTAINED)
            assert empty_plan is not None
            self.assertEqual(empty_plan.status, CurriculumStatus.ABSTAINED_INSUFFICIENT_EVIDENCE)
            self.assertEqual(len(empty_plan.stages), 0)
            self.assertEqual(len(empty_plan.source_citations), 0)

    def test_08_http_api_and_service_contracts(self) -> None:
        """Verify /api/v1/curricula/* endpoints in-process and over a live loopback HTTP server."""
        api = LocalResearchLibraryAPI(db_path=self.db_path, chroma_dir=self.chroma_dir)

        # Method guards on /api/v1/curricula/generate and /api/v1/curricula
        get_gen = api.handle_request("GET", "/api/v1/curricula/generate")
        self.assertEqual(get_gen.status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(get_gen.headers.get("Allow"), "POST")

        post_list = api.handle_request("POST", "/api/v1/curricula")
        self.assertEqual(post_list.status, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(post_list.headers.get("Allow"), "GET")

        # Invalid body on POST /api/v1/curricula/generate
        bad_body = api.handle_request(
            "POST",
            "/api/v1/curricula/generate",
            body=json.dumps({"learner_goal": "short"}),
        )
        self.assertEqual(bad_body.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(bad_body.body["error_code"], "GOAL_TOO_SHORT")

        # Valid POST /api/v1/curricula/generate
        gen_resp = api.handle_request(
            "POST",
            "/api/v1/curricula/generate",
            body=json.dumps({"learner_goal": EXAMPLE_GOAL}),
        )
        self.assertEqual(gen_resp.status, HTTPStatus.CREATED)
        run_id = gen_resp.body["run"]["run_id"]
        curriculum_id = gen_resp.body["curriculum"]["curriculum_id"]

        # GET /api/v1/curricula/runs/{run_id}
        run_resp = api.handle_request("GET", f"/api/v1/curricula/runs/{run_id}")
        self.assertEqual(run_resp.status, HTTPStatus.OK)
        self.assertEqual(run_resp.body["run"]["status"], "COMPLETED")
        self.assertEqual(run_resp.body["run"]["curriculum_id"], curriculum_id)

        # GET /api/v1/curricula
        list_resp = api.handle_request("GET", "/api/v1/curricula")
        self.assertEqual(list_resp.status, HTTPStatus.OK)
        self.assertEqual(list_resp.body["total_count"], 1)
        self.assertEqual(list_resp.body["items"][0]["curriculum_id"], curriculum_id)

        # GET /api/v1/curricula/{curriculum_id}
        curr_resp = api.handle_request("GET", f"/api/v1/curricula/{curriculum_id}")
        self.assertEqual(curr_resp.status, HTTPStatus.OK)
        self.assertEqual(curr_resp.body["curriculum"]["curriculum_id"], curriculum_id)
        self.assertEqual(len(curr_resp.body["curriculum"]["stages"]), 4)

        # GET /api/v1/curricula/{curriculum_id}/sources
        src_resp = api.handle_request("GET", f"/api/v1/curricula/{curriculum_id}/sources")
        self.assertEqual(src_resp.status, HTTPStatus.OK)
        self.assertEqual(src_resp.body["curriculum_id"], curriculum_id)
        self.assertIn(
            EvidenceTier.CURRICULUM_READY_EVIDENCE.value,
            src_resp.body["citations_by_tier"],
        )

        # Live loopback HTTP server round-trip
        server, _ = create_http_server(db_path=self.db_path, host="127.0.0.1", port=0)
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = Request(
                f"http://{host}:{port}/api/v1/curricula/generate",
                data=json.dumps({"learner_goal": EXAMPLE_GOAL}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 201)
                payload = json.loads(resp.read().decode("utf-8"))
                live_cid = payload["curriculum"]["curriculum_id"]

            with urlopen(
                f"http://{host}:{port}/api/v1/curricula/{live_cid}/sources",
                timeout=10,
            ) as resp2:
                self.assertEqual(resp2.status, 200)
                src_payload = json.loads(resp2.read().decode("utf-8"))
                self.assertEqual(src_payload["curriculum_id"], live_cid)
        finally:
            server.shutdown()
            server.server_close()

    def test_09_reproducibility_cli_and_runtime_db_immutability(self) -> None:
        """Verify deterministic reproducibility across runs, CLI exit codes, and SQLite immutability."""
        service = CurriculumManagementService(db_path=self.db_path, chroma_dir=self.chroma_dir)
        first = service.generate_curriculum(EXAMPLE_GOAL)["curriculum"]
        second = service.generate_curriculum(EXAMPLE_GOAL)["curriculum"]

        self.assertEqual(first["curriculum_id"], second["curriculum_id"])
        self.assertEqual(first["stages"], second["stages"])
        self.assertEqual(first["source_citations"], second["source_citations"])
        self.assertEqual(first["evidence_gaps"], second["evidence_gaps"])

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_json = Path(tmp_dir) / "curr.json"
            out_md = Path(tmp_dir) / "curr.md"
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                exit_ok = platform_cli_main(
                    [
                        "--goal",
                        EXAMPLE_GOAL,
                        "--output-json",
                        str(out_json),
                        "--output-md",
                        str(out_md),
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(exit_ok, 0)
            self.assertTrue(out_json.exists())
            self.assertTrue(out_md.exists())

            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                exit_bad = platform_cli_main(["--goal", "short"])
            self.assertEqual(exit_bad, 2)

        # Ensure runtime SQLite database was never modified
        self.assertEqual(_sha256_file(self.db_path), self.initial_db_sha256)


if __name__ == "__main__":
    unittest.main()
