"""Offline test suite for Task 035 — Interactive Agentic Learning Experience.

Covers:
1. Teaching Agent workflow across all 5 explanation depth preferences, prerequisite bridges,
   examples/analogies, Socratic questions, and teaching-depth honesty disclaimer.
2. Citation integrity and insufficient-evidence / abstention behavior (including The Batch governance).
3. Assessment Agent exercise generation (conceptual, mathematical, practical) and deterministic
   rubric evaluation.
4. Invalid, incomplete, and misconception-triggering answer handling.
5. Learning Progress state transitions (NOT_STARTED -> IN_PROGRESS -> PRACTICING -> COMPLETED)
   and single-exercise mastery disclaimer enforcement.
6. Forward-only SQLite migrations, checksum tamper detection, and persistence across restarts
   while preserving runtime `task012_pilot.sqlite` untouched.
7. Curriculum, lesson, and exercise access validation.
8. End-to-end interactive learning workflow:
   Learner selects curriculum -> Opens lesson -> Teaching Agent explains -> Learner asks questions ->
   Learner completes exercise -> Assessment Agent evaluates -> Learning Progress updates ->
   Learning Planner recommends the next lesson.
9. HTTP API & Web Application contracts (`GET /`, `GET /app`, `GET /api/v1/agents`,
   lesson detail/open/teach/exercises/assess, progress, and recommendations) + CLI flags.
"""

from __future__ import annotations

import hashlib
from http import HTTPStatus
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_research_learning.api import LocalResearchLibraryAPI
from ai_research_learning.platform import (
    AgentCapability,
    AssessmentQuestionCategory,
    CurriculumStatus,
    ExplanationDepth,
    LearningProgressStore,
    LessonProgressStatus,
    PlatformOrchestrator,
    PlatformValidationError,
    SINGLE_EXERCISE_MASTERY_DISCLAIMER,
    TEACHING_DEPTH_DISCLAIMER,
    _sanitize_answer_citations,
    get_logical_agent_catalog,
    main as platform_cli_main,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class InteractiveLearningPlatformTests(unittest.TestCase):
    """Comprehensive offline verification for Task 035 interactive learning capabilities."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.runtime_db_path = cls.repo_root / "data" / "runtime" / "task012_pilot.sqlite"
        cls.runtime_chroma_dir = cls.repo_root / "data" / "runtime" / "chroma"
        cls.initial_runtime_db_sha256 = _sha256_file(cls.runtime_db_path)

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="test_task035_")
        self.progress_db_path = Path(self._tmp_dir.name) / "progress.sqlite"
        self.orchestrator = PlatformOrchestrator(
            db_path=self.runtime_db_path,
            chroma_dir=self.runtime_chroma_dir,
            progress_db_path=self.progress_db_path,
        )
        _, self.plan = self.orchestrator.run_learning_planner(
            "I want to become an AI engineer and eventually an AI researcher, starting from mathematical and programming foundations."
        )
        assert self.plan is not None
        self.curriculum_id = self.plan.curriculum_id
        self.first_lesson_id = "lesson_01_linear_algebra_and_multivariable_calculus"
        self.second_lesson_id = "lesson_02_backpropagation_and_automatic_differentiation"

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()
        self.assertEqual(
            _sha256_file(self.runtime_db_path),
            self.initial_runtime_db_sha256,
            "Runtime knowledge store SQLite database must never be mutated.",
        )

    def test_01_logical_agent_catalog_marks_interactive_agents_active(self) -> None:
        catalog = {spec.capability: spec for spec in get_logical_agent_catalog()}
        self.assertEqual(
            catalog[AgentCapability.LEARNING_PLANNER].milestone_status,
            "ACTIVE_VERTICAL_SLICE_TASK_034",
        )
        for cap in (
            AgentCapability.TEACHING,
            AgentCapability.ASSESSMENT,
            AgentCapability.LEARNING_PROGRESS,
        ):
            self.assertEqual(catalog[cap].milestone_status, "ACTIVE_INTERACTIVE_SLICE_TASK_035")

    def test_02_teaching_agent_depths_prerequisites_socratic_and_citation_integrity(self) -> None:
        # Test across all 5 explanation depth preferences on a lesson that has prerequisites
        for depth in ExplanationDepth:
            run_state, response = self.orchestrator.run_teaching_session(
                curriculum_id=self.curriculum_id,
                lesson_id=self.second_lesson_id,
                learner_question="How does backpropagation train distributed representations across hidden layers?",
                explanation_depth=depth.value,
                learner_id="learner_alpha",
            )
            self.assertEqual(run_state.status.value, "COMPLETED")
            self.assertIsNotNone(response)
            assert response is not None
            self.assertEqual(response.explanation_depth, depth)
            self.assertEqual(response.depth_disclaimer, TEACHING_DEPTH_DISCLAIMER)
            self.assertGreaterEqual(len(response.prerequisite_explanations), 1)
            self.assertEqual(
                response.prerequisite_explanations[0].lesson_id,
                self.first_lesson_id,
            )
            self.assertGreaterEqual(len(response.examples_and_analogies), 2)
            self.assertGreaterEqual(len(response.socratic_questions), 2)
            self.assertTrue(bool(response.uncertainty_and_limits))
            self.assertGreaterEqual(len(response.source_citations), 1)

            # Verify every citation references a real source/resource in the knowledge store
            cit_ok, cit_errors = self.orchestrator.ecosystem_service.verify_citation_records(
                list(response.source_citations)
            )
            self.assertTrue(cit_ok, f"Expected verified citations, got errors: {cit_errors}")
            for cit in response.source_citations:
                self.assertNotEqual(cit.source_id, "SRC-THE-BATCH")

    def test_03_teaching_agent_insufficient_evidence_and_the_batch_governance(self) -> None:
        # 1. Off-domain question triggers honest INSUFFICIENT_EVIDENCE abstention
        _, off_domain_resp = self.orchestrator.run_teaching_session(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            learner_question="What is the best sourdough bread baking recipe and mortgage interest rate?",
            explanation_depth="beginner",
            learner_id="learner_alpha",
        )
        assert off_domain_resp is not None
        self.assertEqual(off_domain_resp.status, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(len(off_domain_resp.source_citations), 0)
        self.assertIn("Insufficient evidence", off_domain_resp.uncertainty_and_limits)

        # 2. Question mentioning The Batch enforces candidate-pending-review governance disclosure
        _, batch_resp = self.orchestrator.run_teaching_session(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            learner_question="Can you cite SRC-THE-BATCH or The Batch newsletter for linear algebra?",
            explanation_depth="intermediate",
            learner_id="learner_alpha",
        )
        assert batch_resp is not None
        self.assertIn("SRC-THE-BATCH", batch_resp.uncertainty_and_limits)
        self.assertIn("CANDIDATE_PENDING_REVIEW", batch_resp.uncertainty_and_limits)
        for cit in batch_resp.source_citations:
            self.assertNotEqual(cit.source_id, "SRC-THE-BATCH")

        # 3. Pseudo-citation sanitizer strips unverified [chk_...] markers
        sanitized = _sanitize_answer_citations(
            "Valid text [chk_deadbeef12345678] kept clean.",
            set(),
        )
        self.assertNotIn("chk_deadbeef12345678", sanitized)

    def test_04_assessment_agent_rubrics_misconceptions_and_mastery_honesty(self) -> None:
        exercises = self.orchestrator.get_lesson_exercises(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
        )
        self.assertEqual(len(exercises), 3)
        categories = [ex.category for ex in exercises]
        self.assertEqual(
            categories,
            [
                AssessmentQuestionCategory.CONCEPTUAL,
                AssessmentQuestionCategory.MATHEMATICAL,
                AssessmentQuestionCategory.PRACTICAL,
            ],
        )

        conceptual_ex = exercises[0]
        # Submit an incomplete answer that triggers a misconception ("matrix multiplication is always commutative")
        _, weak_eval, progress_after_weak = self.orchestrator.run_assessment_evaluation(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            exercise_id=conceptual_ex.exercise_id,
            submitted_answer=(
                "Matrix multiplication is always commutative and ab always equals ba "
                "in neural networks."
            ),
            learner_id="learner_beta",
        )
        assert weak_eval is not None and progress_after_weak is not None
        self.assertFalse(weak_eval.passed)
        self.assertFalse(weak_eval.complete_lesson_mastery_claimed)
        self.assertGreaterEqual(len(weak_eval.detected_misconceptions), 1)
        self.assertGreaterEqual(len(weak_eval.objectives_needing_practice), 1)
        self.assertEqual(weak_eval.mastery_disclaimer, SINGLE_EXERCISE_MASTERY_DISCLAIMER)

        # Progress status must be PRACTICING (not COMPLETED)
        l1_prog = next(
            lp for lp in progress_after_weak.lesson_progress if lp.lesson_id == self.first_lesson_id
        )
        self.assertEqual(l1_prog.status, LessonProgressStatus.PRACTICING)
        self.assertGreaterEqual(len(l1_prog.knowledge_gaps), 1)

        # Now submit a strong answer that satisfies the conceptual rubric criteria
        strong_conceptual_answer = (
            "In neural networks, each layer applies a linear transformation via matrix and vector "
            "operations on tensors, where transpose and shape alignment govern geometric projections."
        )
        _, strong_eval, progress_after_one_pass = self.orchestrator.run_assessment_evaluation(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            exercise_id=conceptual_ex.exercise_id,
            submitted_answer=strong_conceptual_answer,
            learner_id="learner_beta",
        )
        assert strong_eval is not None and progress_after_one_pass is not None
        self.assertTrue(strong_eval.passed)
        self.assertFalse(strong_eval.complete_lesson_mastery_claimed)
        self.assertGreaterEqual(strong_eval.score, 0.70)
        self.assertEqual(strong_eval.mastery_disclaimer, SINGLE_EXERCISE_MASTERY_DISCLAIMER)

        # CRITICAL INVARIANT: Passing a single exercise must NOT mark the lesson COMPLETED!
        l1_prog_after_one = next(
            lp for lp in progress_after_one_pass.lesson_progress if lp.lesson_id == self.first_lesson_id
        )
        self.assertEqual(
            l1_prog_after_one.status,
            LessonProgressStatus.PRACTICING,
            "A single correct exercise must leave the lesson in PRACTICING state, never complete mastery.",
        )

    def test_05_end_to_end_interactive_workflow_and_next_lesson_recommendation(self) -> None:
        learner_id = "learner_e2e"

        # 1. Initial progress: all 8 lessons NOT_STARTED, recommends lesson_01
        initial_progress = self.orchestrator.get_curriculum_progress(
            curriculum_id=self.curriculum_id,
            learner_id=learner_id,
        )
        self.assertEqual(initial_progress.not_started_count, 8)
        self.assertEqual(
            initial_progress.next_recommendation.recommended_lesson_id,
            self.first_lesson_id,
        )
        self.assertEqual(
            initial_progress.next_recommendation.recommendation_type,
            "START_NEXT_UNLOCKED_LESSON",
        )

        # 2. Learner opens lesson_01 -> transitions NOT_STARTED -> IN_PROGRESS
        opened = self.orchestrator.open_lesson(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            learner_id=learner_id,
        )
        self.assertEqual(opened["progress"]["status"], LessonProgressStatus.IN_PROGRESS.value)

        # 3. Teaching Agent explains & learner asks a question
        _, teach_resp = self.orchestrator.run_teaching_session(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            learner_question="Explain vector norms, matrix transformations, and why chain rule derivatives matter for gradients.",
            explanation_depth="foundational_intuition",
            learner_id=learner_id,
        )
        assert teach_resp is not None
        self.assertIn(
            teach_resp.status,
            ("GROUNDED_EXPLANATION", "PEDAGOGICAL_SYNTHESIS_WITH_METADATA"),
        )

        # 4. Learner completes all 3 exercises (conceptual, mathematical, practical) for lesson_01
        exercises = self.orchestrator.get_lesson_exercises(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
        )
        answers = [
            (
                "Linear transformations use vector, matrix, and tensor representations where geometric "
                "shape and transpose properties govern layer projections."
            ),
            (
                "By the chain rule, the Jacobian, gradient, and partial derivative with directional "
                "transpose operations propagate sensitivities through composite functions."
            ),
            (
                "We derive the analytical Jacobian of tanh matrix transformations and verify accuracy "
                "with a numerical finite difference gradient check."
            ),
        ]
        for ex, ans in zip(exercises, answers):
            _, eval_res, prog_summary = self.orchestrator.run_assessment_evaluation(
                curriculum_id=self.curriculum_id,
                lesson_id=self.first_lesson_id,
                exercise_id=ex.exercise_id,
                submitted_answer=ans,
                learner_id=learner_id,
            )
            assert eval_res is not None
            self.assertTrue(eval_res.passed, f"Expected exercise {ex.exercise_id} to pass")

        # 5. After all 3 exercises pass, lesson_01 transitions to COMPLETED and Planner recommends lesson_02!
        final_progress = self.orchestrator.get_curriculum_progress(
            curriculum_id=self.curriculum_id,
            learner_id=learner_id,
        )
        l1_final = next(
            lp for lp in final_progress.lesson_progress if lp.lesson_id == self.first_lesson_id
        )
        self.assertEqual(l1_final.status, LessonProgressStatus.COMPLETED)
        self.assertEqual(final_progress.completed_count, 1)
        self.assertEqual(
            final_progress.next_recommendation.recommended_lesson_id,
            self.second_lesson_id,
        )
        self.assertEqual(
            len(final_progress.next_recommendation.unmet_prerequisite_lesson_ids),
            0,
        )
        self.assertEqual(
            final_progress.next_recommendation.recommendation_type,
            "START_NEXT_UNLOCKED_LESSON",
        )

    def test_06_database_migrations_checksum_guard_and_restart_persistence(self) -> None:
        learner_id = "learner_persist"
        self.orchestrator.open_lesson(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
            learner_id=learner_id,
        )

        # Verify migrations table has versions 1 and 2
        migrations = self.orchestrator.progress_store.list_migrations()
        self.assertEqual([m["version"] for m in migrations], [1, 2])

        # Create a brand-new PlatformOrchestrator pointing to the same progress_db_path
        restarted_orchestrator = PlatformOrchestrator(
            db_path=self.runtime_db_path,
            chroma_dir=self.runtime_chroma_dir,
            progress_db_path=self.progress_db_path,
        )
        loaded_plan = restarted_orchestrator.get_curriculum(self.curriculum_id)
        self.assertIsNotNone(loaded_plan)
        restarted_progress = restarted_orchestrator.get_curriculum_progress(
            curriculum_id=self.curriculum_id,
            learner_id=learner_id,
        )
        l1_restarted = next(
            lp for lp in restarted_progress.lesson_progress if lp.lesson_id == self.first_lesson_id
        )
        self.assertEqual(l1_restarted.status, LessonProgressStatus.IN_PROGRESS)

        # Tamper with migration checksum in a separate SQLite file and verify tamper detection
        tampered_db = Path(self._tmp_dir.name) / "tampered.sqlite"
        store = LearningProgressStore(tampered_db)
        conn = sqlite3.connect(str(tampered_db))
        try:
            with conn:
                conn.execute("UPDATE learning_schema_migrations SET checksum = 'bad_checksum' WHERE version = 1")
        finally:
            conn.close()
        with self.assertRaises(PlatformValidationError) as ctx:
            store.apply_migrations()
        self.assertEqual(ctx.exception.code, "MIGRATION_CHECKSUM_MISMATCH")

    def test_07_validation_errors_for_invalid_inputs_and_unvalidated_curricula(self) -> None:
        # Empty / too short question
        with self.assertRaises(PlatformValidationError) as ctx1:
            self.orchestrator.run_teaching_session(
                curriculum_id=self.curriculum_id,
                lesson_id=self.first_lesson_id,
                learner_question="hi",
            )
        self.assertEqual(ctx1.exception.code, "QUESTION_TOO_SHORT")

        # Invalid explanation depth
        with self.assertRaises(PlatformValidationError) as ctx2:
            self.orchestrator.run_teaching_session(
                curriculum_id=self.curriculum_id,
                lesson_id=self.first_lesson_id,
                learner_question="Explain linear algebra foundations in detail.",
                explanation_depth="omniscient_guru_mode",
            )
        self.assertEqual(ctx2.exception.code, "INVALID_EXPLANATION_DEPTH")

        # Empty / too short submitted assessment answer
        exercises = self.orchestrator.get_lesson_exercises(
            curriculum_id=self.curriculum_id,
            lesson_id=self.first_lesson_id,
        )
        with self.assertRaises(PlatformValidationError) as ctx3:
            self.orchestrator.run_assessment_evaluation(
                curriculum_id=self.curriculum_id,
                lesson_id=self.first_lesson_id,
                exercise_id=exercises[0].exercise_id,
                submitted_answer="too short",
            )
        self.assertEqual(ctx3.exception.code, "SUBMISSION_TOO_SHORT")

        # Non-existent lesson ID
        with self.assertRaises(PlatformValidationError) as ctx4:
            self.orchestrator.open_lesson(
                curriculum_id=self.curriculum_id,
                lesson_id="lesson_99_nonexistent",
            )
        self.assertEqual(ctx4.exception.code, "LESSON_NOT_FOUND")

    def test_08_http_api_and_web_application_contracts(self) -> None:
        api = LocalResearchLibraryAPI(
            db_path=self.runtime_db_path,
            chroma_dir=self.runtime_chroma_dir,
            progress_db_path=self.progress_db_path,
        )

        # 1. Web App UI at GET / and GET /app
        for route in ("/", "/app"):
            web_resp = api.handle_request("GET", route)
            self.assertEqual(web_resp.status, HTTPStatus.OK)
            self.assertEqual(web_resp.content_type, "text/html; charset=utf-8")
            html_text = web_resp.to_payload_bytes().decode("utf-8")
            self.assertIn("1. Learning Dashboard", html_text)
            self.assertIn("2. Curriculum &amp; Prerequisites", html_text)
            self.assertIn("3. Lesson Workspace", html_text)
            self.assertIn("4. Teaching Agent Conversation", html_text)
            self.assertIn("5. Exercise &amp; Rubric Assessment", html_text)
            self.assertIn("6. Learning Progress &amp; Next Lesson Recommendation", html_text)

        # 2. Agent catalog endpoint
        agents_resp = api.handle_request("GET", "/api/v1/agents")
        self.assertEqual(agents_resp.status, HTTPStatus.OK)
        self.assertEqual(len(agents_resp.body["agents"]), 6)

        # 3. Lesson open, detail, exercises, teach, assess, progress, and recommendations
        open_resp = api.handle_request(
            "POST",
            f"/api/v1/curricula/{self.curriculum_id}/lessons/{self.first_lesson_id}/open",
            body={"learner_id": "api_learner"},
        )
        self.assertEqual(open_resp.status, HTTPStatus.OK)
        self.assertEqual(open_resp.body["progress"]["status"], "IN_PROGRESS")

        detail_resp = api.handle_request(
            "GET",
            f"/api/v1/curricula/{self.curriculum_id}/lessons/{self.first_lesson_id}?learner_id=api_learner",
        )
        self.assertEqual(detail_resp.status, HTTPStatus.OK)
        self.assertEqual(detail_resp.body["lesson"]["lesson_id"], self.first_lesson_id)
        self.assertEqual(len(detail_resp.body["exercises"]), 3)

        ex_resp = api.handle_request(
            "GET",
            f"/api/v1/curricula/{self.curriculum_id}/lessons/{self.first_lesson_id}/exercises",
        )
        self.assertEqual(ex_resp.status, HTTPStatus.OK)
        ex_id = ex_resp.body["exercises"][0]["exercise_id"]

        teach_resp = api.handle_request(
            "POST",
            f"/api/v1/curricula/{self.curriculum_id}/lessons/{self.first_lesson_id}/teach",
            body={
                "learner_id": "api_learner",
                "question": "How do matrix conditioning and norms affect gradient descent?",
                "explanation_depth": "intermediate",
            },
        )
        self.assertEqual(teach_resp.status, HTTPStatus.OK)
        self.assertIn("teaching_response", teach_resp.body)
        self.assertEqual(
            teach_resp.body["teaching_response"]["depth_disclaimer"],
            TEACHING_DEPTH_DISCLAIMER,
        )

        assess_resp = api.handle_request(
            "POST",
            f"/api/v1/curricula/{self.curriculum_id}/lessons/{self.first_lesson_id}/assess",
            body={
                "learner_id": "api_learner",
                "exercise_id": ex_id,
                "submitted_answer": (
                    "Matrix, vector, and tensor linear transformations rely on geometric "
                    "shape and transpose alignment while conditioning controls numerical stability."
                ),
            },
        )
        self.assertEqual(assess_resp.status, HTTPStatus.OK)
        self.assertTrue(assess_resp.body["evaluation"]["passed"])
        self.assertEqual(assess_resp.body["progress"]["practicing_count"], 1)

        prog_resp = api.handle_request(
            "GET",
            f"/api/v1/curricula/{self.curriculum_id}/progress?learner_id=api_learner",
        )
        self.assertEqual(prog_resp.status, HTTPStatus.OK)
        self.assertEqual(prog_resp.body["progress"]["practicing_count"], 1)

        rec_resp = api.handle_request(
            "GET",
            f"/api/v1/curricula/{self.curriculum_id}/recommendations?learner_id=api_learner",
        )
        self.assertEqual(rec_resp.status, HTTPStatus.OK)
        self.assertEqual(
            rec_resp.body["recommendation"]["recommended_lesson_id"],
            self.first_lesson_id,
        )

    def test_09_cli_interactive_teaching_assessment_and_progress_flags(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = platform_cli_main(
                [
                    "--db-path",
                    str(self.runtime_db_path),
                    "--chroma-dir",
                    str(self.runtime_chroma_dir),
                    "--progress-db",
                    str(self.progress_db_path),
                    "--curriculum-id",
                    self.curriculum_id,
                    "--lesson-id",
                    self.first_lesson_id,
                    "--teach-question",
                    "Why is numerical stability important in linear algebra for deep learning?",
                    "--explanation-depth",
                    "beginner",
                ]
            )
        self.assertEqual(rc, 0)
        teach_payload = json.loads(buf.getvalue())
        self.assertEqual(teach_payload["lesson_id"], self.first_lesson_id)
        self.assertEqual(teach_payload["explanation_depth"], "beginner")

        buf_prog = io.StringIO()
        with patch("sys.stdout", buf_prog):
            rc_prog = platform_cli_main(
                [
                    "--db-path",
                    str(self.runtime_db_path),
                    "--chroma-dir",
                    str(self.runtime_chroma_dir),
                    "--progress-db",
                    str(self.progress_db_path),
                    "--curriculum-id",
                    self.curriculum_id,
                    "--show-progress",
                ]
            )
        self.assertEqual(rc_prog, 0)
        prog_payload = json.loads(buf_prog.getvalue())
        self.assertEqual(prog_payload["curriculum_id"], self.curriculum_id)


if __name__ == "__main__":
    unittest.main()
