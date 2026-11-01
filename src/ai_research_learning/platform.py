"""Agent-First AI Learning Platform: Controlled Orchestration, Source Ecosystem, and Curriculum Planning Slice.

Implements Task 034:
1. Controlled Agent Orchestration (`PlatformOrchestrator`, `AgentCapability`, `ToolRegistry`,
   `ExecutionPolicy`, `WorkflowRunState`) for the six logical agent capabilities:
   - RESEARCH
   - KNOWLEDGE_CURATOR
   - LEARNING_PLANNER
   - TEACHING
   - ASSESSMENT
   - LEARNING_PROGRESS
2. Five-Tier Source Ecosystem Classification (`EvidenceTier`, `SourceEcosystemService`):
   - DISCOVERED_LINK
   - APPROVED_UNCOLLECTED_RESOURCE
   - COLLECTED_DOCUMENT
   - EXTRACTED_KNOWLEDGE
   - CURRICULUM_READY_EVIDENCE
   Explicitly enforces governance boundaries for `SRC-THE-BATCH` (`CANDIDATE_PENDING_REVIEW`).
3. Concept Prerequisite DAG (`ConceptNode`, `ConceptGraph`) and Deterministic Validation
   (`CurriculumValidator`): topological sort, cycle detection, citation integrity,
   mastery-check honesty gate, and evidence gap reporting.
4. First Working Vertical Slice (`LearningPlannerAgent`, `CurriculumManagementService`):
   Learner goal -> Learning Planner Agent -> Evidence retrieval -> Curriculum generation ->
   Deterministic validation -> Validated curriculum output (JSON & Markdown).
"""

from __future__ import annotations

import argparse
import atexit
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .rag import (
    ChromaPersistentVectorStore,
    DEFAULT_CHROMA_COLLECTION,
    DEFAULT_CHROMA_DIR,
    DEFAULT_RUNTIME_DB_PATH,
    DeterministicHashEmbeddingProvider,
    LocalRAGPipeline,
    ReadOnlySQLiteStore,
    RetrievalOnlyFallbackGenerationProvider,
    RetrievedChunk,
    _sanitize_answer_citations,
)


# ---------------------------------------------------------------------------
# Constants & Enumerations
# ---------------------------------------------------------------------------

CURRICULUM_SCHEMA_VERSION = "1.0.0"
MASTERY_ASSESSMENT_DISCLAIMER = (
    "Design-only mastery check specification: interactive learner mastery evaluation "
    "and grading are planned for the Assessment Agent milestone and are not claimed as "
    "active verification in this release."
)
TEACHING_DEPTH_DISCLAIMER = (
    "Explanation depth preference controls how the Teaching Agent presents material "
    "(foundational_intuition, beginner, intermediate, advanced, or research_level) "
    "and is not a claim or certification of learner mastery."
)
SINGLE_EXERCISE_MASTERY_DISCLAIMER = (
    "A single correct exercise answer demonstrates progress on the targeted objective "
    "but is never equated with complete lesson or concept mastery; full lesson completion "
    "requires demonstrating all lesson objectives across conceptual, mathematical, and "
    "practical exercises."
)
DEFAULT_LEARNER_ID = "learner_default"
THE_BATCH_SOURCE_ID = "SRC-THE-BATCH"
THE_BATCH_CANONICAL_URL = "https://www.deeplearning.ai/the-batch"


class AgentCapability(str, Enum):
    """Logical agent capabilities coordinated by PlatformOrchestrator."""

    RESEARCH = "RESEARCH"
    KNOWLEDGE_CURATOR = "KNOWLEDGE_CURATOR"
    LEARNING_PLANNER = "LEARNING_PLANNER"
    TEACHING = "TEACHING"
    ASSESSMENT = "ASSESSMENT"
    LEARNING_PROGRESS = "LEARNING_PROGRESS"


class EvidenceTier(str, Enum):
    """Five epistemic tiers of the source ecosystem."""

    DISCOVERED_LINK = "DISCOVERED_LINK"
    APPROVED_UNCOLLECTED_RESOURCE = "APPROVED_UNCOLLECTED_RESOURCE"
    COLLECTED_DOCUMENT = "COLLECTED_DOCUMENT"
    EXTRACTED_KNOWLEDGE = "EXTRACTED_KNOWLEDGE"
    CURRICULUM_READY_EVIDENCE = "CURRICULUM_READY_EVIDENCE"


class WorkflowStatus(str, Enum):
    """Lifecycle states for orchestrated agent workflow runs."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABSTAINED = "ABSTAINED"
    FAILED = "FAILED"


class CurriculumStatus(str, Enum):
    """Validation and readiness status of a generated curriculum."""

    VALIDATED = "VALIDATED"
    ABSTAINED_INSUFFICIENT_EVIDENCE = "ABSTAINED_INSUFFICIENT_EVIDENCE"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"


class ExplanationDepth(str, Enum):
    """Teaching depth preference for the Teaching Agent (not a claim of learner mastery)."""

    FOUNDATIONAL_INTUITION = "foundational_intuition"
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    RESEARCH_LEVEL = "research_level"


class LessonProgressStatus(str, Enum):
    """Persistent learner progress status for a lesson."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    PRACTICING = "PRACTICING"
    COMPLETED = "COMPLETED"


class AssessmentQuestionCategory(str, Enum):
    """Categories of assessment questions generated from lesson objectives."""

    CONCEPTUAL = "conceptual"
    MATHEMATICAL = "mathematical"
    PRACTICAL = "practical"


# ---------------------------------------------------------------------------
# Controlled Agent Runtime Contracts
# ---------------------------------------------------------------------------


class PlatformValidationError(ValueError):
    """Raised when a learner goal, tool invocation, or curriculum fails validation."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ExecutionPolicy:
    """Bounded execution policy for controlled agent runs."""

    max_tool_calls: int = 16
    max_retrieval_queries: int = 8
    top_k_per_query: int = 6
    require_deterministic_validation: bool = True
    require_citation_verification: bool = True
    allow_uncollected_syllabus_references: bool = True
    forbid_news_recency_prerequisite_override: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentCapabilitySpec:
    """Contract describing one of the six logical platform agent capabilities."""

    capability: AgentCapability
    display_name: str
    purpose: str
    allowed_tools: Tuple[str, ...]
    input_contract: str
    output_contract: str
    deterministic_guardrails: Tuple[str, ...]
    milestone_status: str

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["capability"] = self.capability.value
        data["allowed_tools"] = list(self.allowed_tools)
        data["deterministic_guardrails"] = list(self.deterministic_guardrails)
        return data


@dataclass(frozen=True)
class ToolDefinition:
    """Registered platform tool with capability-based access control."""

    name: str
    description: str
    allowed_capabilities: Tuple[AgentCapability, ...]
    read_only: bool
    handler: Callable[..., Any]


@dataclass
class WorkflowStepRecord:
    """Audit record for a single step in an orchestrated workflow."""

    step_index: int
    step_name: str
    actor: str
    status: str
    started_at: str
    completed_at: str
    duration_ms: float
    summary: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowRunState:
    """Persistent/in-memory state of an orchestrated agent workflow execution."""

    run_id: str
    capability: AgentCapability
    status: WorkflowStatus
    learner_goal: str
    created_at: str
    updated_at: str
    execution_policy: ExecutionPolicy
    tool_calls_count: int = 0
    curriculum_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    validation_summary: Dict[str, Any] = field(default_factory=dict)
    steps: List[WorkflowStepRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "capability": self.capability.value,
            "status": self.status.value,
            "learner_goal": self.learner_goal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "execution_policy": self.execution_policy.to_dict(),
            "tool_calls_count": self.tool_calls_count,
            "curriculum_id": self.curriculum_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "validation_summary": dict(self.validation_summary),
            "steps": [step.to_dict() for step in self.steps],
        }


class ToolRegistry:
    """Enforces capability-scoped tool permissions and execution budgets."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def list_tools_for(self, capability: AgentCapability) -> List[str]:
        return sorted(
            name
            for name, tool in self._tools.items()
            if capability in tool.allowed_capabilities
        )

    def invoke(
        self,
        *,
        run_state: WorkflowRunState,
        capability: AgentCapability,
        tool_name: str,
        **kwargs: Any,
    ) -> Any:
        if tool_name not in self._tools:
            raise PlatformValidationError(
                "UNKNOWN_TOOL",
                f"Tool '{tool_name}' is not registered in ToolRegistry.",
                {"tool_name": tool_name},
            )
        tool = self._tools[tool_name]
        if capability not in tool.allowed_capabilities:
            raise PlatformValidationError(
                "TOOL_PERMISSION_DENIED",
                f"Capability '{capability.value}' is not permitted to invoke tool '{tool_name}'.",
                {
                    "capability": capability.value,
                    "tool_name": tool_name,
                    "allowed_capabilities": [c.value for c in tool.allowed_capabilities],
                },
            )
        if run_state.tool_calls_count >= run_state.execution_policy.max_tool_calls:
            raise PlatformValidationError(
                "TOOL_BUDGET_EXCEEDED",
                f"Workflow run '{run_state.run_id}' exceeded max_tool_calls={run_state.execution_policy.max_tool_calls}.",
                {
                    "run_id": run_state.run_id,
                    "tool_calls_count": run_state.tool_calls_count,
                    "max_tool_calls": run_state.execution_policy.max_tool_calls,
                },
            )
        run_state.tool_calls_count += 1
        return tool.handler(**kwargs)


def get_logical_agent_catalog() -> Tuple[AgentCapabilitySpec, ...]:
    """Return the canonical contract specifications for all six logical agents."""
    return (
        AgentCapabilitySpec(
            capability=AgentCapability.RESEARCH,
            display_name="Research Agent",
            purpose="Answer technical AI research questions grounded strictly in retrieved knowledge chunks.",
            allowed_tools=("inspect_source_ecosystem", "search_knowledge_chunks"),
            input_contract="ResearchQuestionRequest(question, top_k, provider)",
            output_contract="RAGAnswerResult(status, answer_text, citations, abstention_reason)",
            deterministic_guardrails=(
                "Citation sanitization (_sanitize_answer_citations)",
                "Minimum similarity & token overlap abstention gates",
                "Read-only SQLite and Chroma access",
            ),
            milestone_status="ACTIVE_VIA_RAG_PIPELINE",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.KNOWLEDGE_CURATOR,
            display_name="Knowledge Curator Agent",
            purpose="Inspect source ecosystem tiers, prioritize approved uncollected seeds, and flag evidence gaps.",
            allowed_tools=(
                "inspect_source_ecosystem",
                "list_approved_resources",
                "list_discovered_candidates",
            ),
            input_contract="CurationAuditRequest(topic_filter, include_candidates)",
            output_contract="SourceEcosystemSummary(tier_counts, the_batch_policy, coverage_gaps)",
            deterministic_guardrails=(
                "Candidate links (DISCOVERED_LINK) cannot bypass human/policy approval",
                "SRC-THE-BATCH (CANDIDATE_PENDING_REVIEW) restricted to discovery radar only",
            ),
            milestone_status="ACTIVE_ECOSYSTEM_INSPECTION",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.LEARNING_PLANNER,
            display_name="Learning Planner Agent",
            purpose="Transform a validated learner goal into a prerequisite-ordered, citation-backed curriculum plan.",
            allowed_tools=(
                "inspect_source_ecosystem",
                "query_concept_graph",
                "search_knowledge_chunks",
                "list_approved_resources",
            ),
            input_contract="LearningGoalRequest(learner_goal, learner_background, target_track)",
            output_contract="CurriculumPlan(stages, lessons, prerequisites, source_citations, evidence_gaps)",
            deterministic_guardrails=(
                "Prerequisite DAG cycle & topological order validation",
                "Citation verification against retrieved chunks & approved SQLite resources",
                "Mastery check non-assessment honesty gate",
                "News recency forbidden from overriding foundational prerequisite ordering",
            ),
            milestone_status="ACTIVE_VERTICAL_SLICE_TASK_034",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.TEACHING,
            display_name="Teaching Agent",
            purpose="Deliver interactive lesson walkthroughs, intuitive explanations, prerequisite bridges, examples/analogies, and Socratic questions grounded in verified evidence.",
            allowed_tools=("read_curriculum_plan", "search_knowledge_chunks", "query_concept_graph"),
            input_contract="TeachingRequest(curriculum_id, lesson_id, learner_question, explanation_depth)",
            output_contract="TeachingResponse(core_explanation, prerequisite_explanations, examples_and_analogies, socratic_questions, uncertainty_and_limits, source_citations)",
            deterministic_guardrails=(
                "Lesson scope locked to validated CurriculumLesson objectives",
                "Strict chunk citation sanitization and SQLite verification",
                "Explanation depth preference treated strictly as presentation preference, never mastery certification",
                "Explicit uncertainty and insufficient-evidence disclosure",
            ),
            milestone_status="ACTIVE_INTERACTIVE_SLICE_TASK_035",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.ASSESSMENT,
            display_name="Assessment Agent",
            purpose="Generate conceptual, mathematical, and practical exercises from lesson objectives and evaluate submissions against explicit deterministic rubrics.",
            allowed_tools=("read_curriculum_plan", "search_knowledge_chunks"),
            input_contract="AssessmentSubmission(curriculum_id, lesson_id, exercise_id, submitted_answer)",
            output_contract="AssessmentEvaluation(score, score_band, criterion_evaluations, detected_misconceptions, demonstrated_objectives, objectives_needing_practice)",
            deterministic_guardrails=(
                "Deterministic rubric evaluation explicitly separated from optional LLM feedback",
                "Single correct answer never equated with complete lesson or concept mastery",
                "Explicit misconception pattern detection and remediation mapping",
            ),
            milestone_status="ACTIVE_INTERACTIVE_SLICE_TASK_035",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.LEARNING_PROGRESS,
            display_name="Learning Progress Agent",
            purpose="Persist learner progress, assessment attempts, demonstrated objectives, and knowledge gaps across SQLite and compute prerequisite-aware next lesson recommendations.",
            allowed_tools=("read_curriculum_plan", "query_concept_graph"),
            input_contract="ProgressUpdateRequest(learner_id, curriculum_id, lesson_id, assessment_evaluation)",
            output_contract="CurriculumProgressSummary(lesson_progress, next_recommendation, overall_completion_ratio)",
            deterministic_guardrails=(
                "Prerequisite unlock state computed deterministically from concept DAG and actual progress",
                "Lesson COMPLETED status requires demonstrating all lesson objectives across multiple exercises",
                "Forward-only SQLite schema migration preserving read-only knowledge store integrity",
            ),
            milestone_status="ACTIVE_INTERACTIVE_SLICE_TASK_035",
        ),
    )


# ---------------------------------------------------------------------------
# Five-Tier Source Ecosystem Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEcosystemSummary:
    """Snapshot of the five epistemic tiers in the local knowledge store."""

    total_registered_sources: int
    approved_operational_sources: int
    candidate_pending_sources: int
    tier_counts: Dict[str, int]
    curriculum_ready_chunk_count: int
    curriculum_ready_resource_count: int
    extracted_resource_count: int
    collected_document_count: int
    approved_uncollected_resource_count: int
    discovered_link_candidate_count: int
    the_batch_governance: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CurriculumCitation:
    """Verified source or chunk reference attached to a curriculum lesson or stage."""

    citation_id: str
    evidence_tier: EvidenceTier
    source_id: str
    resource_id: str
    title: str
    canonical_url: str
    chunk_id: Optional[str] = None
    epistemic_layer: str = "VERIFIED_RESOURCE_METADATA"
    locator: str = "resource-metadata"
    excerpt_summary: str = ""
    relevance_score: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["evidence_tier"] = self.evidence_tier.value
        return data


class SourceEcosystemService:
    """Read-only service that classifies knowledge store records into the 5 epistemic tiers."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()

    def _open_readonly(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise PlatformValidationError(
                "DATABASE_NOT_FOUND",
                f"Knowledge store SQLite database not found at '{self.db_path}'.",
            )
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        return conn

    @staticmethod
    def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def summarize_ecosystem(self) -> SourceEcosystemSummary:
        """Compute exact counts across all five epistemic tiers and check SRC-THE-BATCH policy."""
        conn = self._open_readonly()
        try:
            src_rows = conn.execute(
                "SELECT source_id, display_name, origin, status, permits_full_text_storage, max_requests_per_run "
                "FROM sources ORDER BY source_id"
            ).fetchall()
            total_sources = len(src_rows)
            candidate_sources = sum(1 for r in src_rows if r["status"] == "CANDIDATE_PENDING_REVIEW")
            approved_sources = total_sources - candidate_sources

            the_batch_row = next((r for r in src_rows if r["source_id"] == THE_BATCH_SOURCE_ID), None)
            the_batch_governance = {
                "source_id": THE_BATCH_SOURCE_ID,
                "canonical_url": the_batch_row["origin"] if the_batch_row else THE_BATCH_CANONICAL_URL,
                "status": the_batch_row["status"] if the_batch_row else "UNREGISTERED",
                "permits_full_text_storage": bool(the_batch_row["permits_full_text_storage"]) if the_batch_row else False,
                "max_requests_per_run": int(the_batch_row["max_requests_per_run"]) if the_batch_row else 0,
                "role_in_platform": "AI_NEWS_AND_RESEARCH_DISCOVERY_RADAR_ONLY",
                "can_ground_curriculum_citations": False,
                "can_override_foundational_prerequisites": False,
            }

            discovered_count = int(
                conn.execute("SELECT COUNT(*) FROM discovered_link_candidates").fetchone()[0]
            )
            approved_uncollected_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM resources r
                    JOIN sources s ON r.source_id = s.source_id
                    WHERE s.status != 'CANDIDATE_PENDING_REVIEW'
                      AND r.last_verified_at IS NULL
                      AND r.text_storage_status = 'metadata-only-not-yet-collected'
                    """
                ).fetchone()[0]
            )
            collected_doc_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM resources r
                    JOIN sources s ON r.source_id = s.source_id
                    WHERE s.status != 'CANDIDATE_PENDING_REVIEW'
                      AND r.last_verified_at IS NOT NULL
                    """
                ).fetchone()[0]
            )
            has_chunks_table = self._has_table(conn, "knowledge_chunks")
            if has_chunks_table:
                extracted_resource_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT kc.resource_id)
                        FROM knowledge_chunks kc
                        JOIN sources s ON kc.source_id = s.source_id
                        WHERE kc.lifecycle_state = 'ACTIVE'
                          AND s.status != 'CANDIDATE_PENDING_REVIEW'
                        """
                    ).fetchone()[0]
                )
                curriculum_ready_chunk_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM knowledge_chunks kc
                        JOIN sources s ON kc.source_id = s.source_id
                        WHERE kc.lifecycle_state = 'ACTIVE'
                          AND s.status != 'CANDIDATE_PENDING_REVIEW'
                        """
                    ).fetchone()[0]
                )
            else:
                extracted_resource_count = 0
                curriculum_ready_chunk_count = 0

            tier_counts = {
                EvidenceTier.DISCOVERED_LINK.value: discovered_count,
                EvidenceTier.APPROVED_UNCOLLECTED_RESOURCE.value: approved_uncollected_resource_count
                if (approved_uncollected_resource_count := approved_uncollected_count) is not None
                else 0,
                EvidenceTier.COLLECTED_DOCUMENT.value: collected_doc_count,
                EvidenceTier.EXTRACTED_KNOWLEDGE.value: extracted_resource_count,
                EvidenceTier.CURRICULUM_READY_EVIDENCE.value: curriculum_ready_chunk_count,
            }
            return SourceEcosystemSummary(
                total_registered_sources=total_sources,
                approved_operational_sources=approved_sources,
                candidate_pending_sources=candidate_sources,
                tier_counts=tier_counts,
                curriculum_ready_chunk_count=curriculum_ready_chunk_count,
                curriculum_ready_resource_count=collected_doc_count,
                extracted_resource_count=extracted_resource_count,
                collected_document_count=collected_doc_count,
                approved_uncollected_resource_count=approved_uncollected_count,
                discovered_link_candidate_count=discovered_count,
                the_batch_governance=the_batch_governance,
            )
        finally:
            conn.close()

    def list_approved_resources(self, *, only_collected: bool = False) -> List[Dict[str, Any]]:
        """List resources from non-candidate sources with their classified EvidenceTier."""
        conn = self._open_readonly()
        try:
            has_chunks_table = self._has_table(conn, "knowledge_chunks")
            chunk_subquery = (
                "(SELECT COUNT(*) FROM knowledge_chunks kc "
                "WHERE kc.resource_id = r.resource_id AND kc.lifecycle_state = 'ACTIVE')"
                if has_chunks_table
                else "0"
            )
            where_clause = "WHERE s.status != 'CANDIDATE_PENDING_REVIEW'"
            if only_collected:
                where_clause += " AND r.last_verified_at IS NOT NULL"
            rows = conn.execute(
                f"""
                SELECT r.resource_id, r.source_id, s.display_name AS source_name,
                       s.status AS source_status, r.title, r.canonical_url,
                       r.abstract_or_description, r.topic_tags, r.text_storage_status,
                       r.verification_status, r.last_verified_at,
                       {chunk_subquery} AS active_chunk_count
                FROM resources r
                JOIN sources s ON r.source_id = s.source_id
                {where_clause}
                ORDER BY r.source_id, r.resource_id
                """
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                chunk_count = int(row["active_chunk_count"] or 0)
                if chunk_count > 0:
                    tier = EvidenceTier.CURRICULUM_READY_EVIDENCE
                elif row["last_verified_at"] is not None:
                    tier = EvidenceTier.COLLECTED_DOCUMENT
                else:
                    tier = EvidenceTier.APPROVED_UNCOLLECTED_RESOURCE
                results.append(
                    {
                        "resource_id": row["resource_id"],
                        "source_id": row["source_id"],
                        "source_name": row["source_name"],
                        "source_status": row["source_status"],
                        "title": row["title"],
                        "canonical_url": row["canonical_url"],
                        "abstract_or_description": row["abstract_or_description"] or "",
                        "topic_tags": row["topic_tags"] or "",
                        "text_storage_status": row["text_storage_status"],
                        "verification_status": row["verification_status"],
                        "last_verified_at": row["last_verified_at"],
                        "active_chunk_count": chunk_count,
                        "evidence_tier": tier.value,
                    }
                )
            return results
        finally:
            conn.close()

    def list_discovered_candidates(self, *, limit: int = 25) -> List[Dict[str, Any]]:
        """List discovered link candidates (Tier 1: DISCOVERED_LINK, excluded from curriculum citations)."""
        conn = self._open_readonly()
        try:
            rows = conn.execute(
                """
                SELECT candidate_id, referring_source_id, referring_resource_id,
                       normalized_url, link_text, domain_classification, review_status
                FROM discovered_link_candidates
                ORDER BY candidate_id
                LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
            return [
                {
                    "candidate_id": r["candidate_id"],
                    "referring_source_id": r["referring_source_id"],
                    "parent_source_id": r["referring_source_id"],
                    "referring_resource_id": r["referring_resource_id"],
                    "normalized_url": r["normalized_url"],
                    "target_canonical_url": r["normalized_url"],
                    "link_text": r["link_text"] or "",
                    "anchor_text": r["link_text"] or "",
                    "domain_classification": r["domain_classification"],
                    "review_status": r["review_status"],
                    "evidence_tier": EvidenceTier.DISCOVERED_LINK.value,
                }
                for r in rows
            ]
        finally:
            conn.close()

    def verify_citation_records(
        self,
        citations: Sequence[CurriculumCitation],
    ) -> Tuple[bool, List[str]]:
        """Deterministically verify that every citation references real, non-candidate SQLite records."""
        errors: List[str] = []
        conn = self._open_readonly()
        try:
            if self._has_table(conn, "knowledge_chunks"):
                chunk_rows = conn.execute(
                    """
                    SELECT kc.chunk_id, kc.resource_id, kc.source_id, r.canonical_url, s.status AS source_status
                    FROM knowledge_chunks kc
                    JOIN resources r ON kc.resource_id = r.resource_id
                    JOIN sources s ON kc.source_id = s.source_id
                    WHERE kc.lifecycle_state = 'ACTIVE'
                    """
                ).fetchall()
                valid_chunks = {r["chunk_id"]: dict(r) for r in chunk_rows}
            else:
                valid_chunks = {}

            res_rows = conn.execute(
                """
                SELECT r.resource_id, r.source_id, r.canonical_url, r.last_verified_at,
                       r.text_storage_status, s.status AS source_status
                FROM resources r
                JOIN sources s ON r.source_id = s.source_id
                """
            ).fetchall()
            valid_resources = {r["resource_id"]: dict(r) for r in res_rows}

            for cit in citations:
                if cit.evidence_tier == EvidenceTier.DISCOVERED_LINK:
                    errors.append(
                        f"Citation '{cit.citation_id}' uses forbidden tier DISCOVERED_LINK."
                    )
                if cit.source_id == THE_BATCH_SOURCE_ID:
                    errors.append(
                        f"Citation '{cit.citation_id}' references candidate source '{THE_BATCH_SOURCE_ID}'."
                    )
                if cit.resource_id not in valid_resources:
                    errors.append(
                        f"Citation '{cit.citation_id}' references unknown resource_id '{cit.resource_id}'."
                    )
                    continue
                res_meta = valid_resources[cit.resource_id]
                if res_meta["source_status"] == "CANDIDATE_PENDING_REVIEW":
                    errors.append(
                        f"Citation '{cit.citation_id}' references resource '{cit.resource_id}' from unapproved source '{res_meta['source_id']}'."
                    )
                if res_meta["source_id"] != cit.source_id:
                    errors.append(
                        f"Citation '{cit.citation_id}' source_id '{cit.source_id}' mismatches resource source_id '{res_meta['source_id']}'."
                    )
                if cit.chunk_id is not None:
                    if cit.chunk_id not in valid_chunks:
                        errors.append(
                            f"Citation '{cit.citation_id}' references unknown or inactive chunk_id '{cit.chunk_id}'."
                        )
                    else:
                        chk_meta = valid_chunks[cit.chunk_id]
                        if chk_meta["resource_id"] != cit.resource_id:
                            errors.append(
                                f"Citation '{cit.citation_id}' chunk_id '{cit.chunk_id}' belongs to resource '{chk_meta['resource_id']}', not '{cit.resource_id}'."
                            )
                elif cit.evidence_tier == EvidenceTier.CURRICULUM_READY_EVIDENCE:
                    if res_meta["last_verified_at"] is None:
                        errors.append(
                            f"Citation '{cit.citation_id}' claims CURRICULUM_READY_EVIDENCE for uncollected resource '{cit.resource_id}'."
                        )
            return (len(errors) == 0, errors)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Concept Prerequisite DAG & Curriculum Domain Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConceptNode:
    """Canonical concept node in the AI Learning Platform prerequisite graph."""

    concept_id: str
    stage_id: str
    stage_order: int
    stage_title: str
    lesson_id: str
    lesson_order: int
    title: str
    prerequisites: Tuple[str, ...]
    learning_objectives: Tuple[str, ...]
    retrieval_queries: Tuple[str, ...]
    keywords: Tuple[str, ...]
    preferred_resource_ids: Tuple[str, ...]
    explanation_template: str
    exercises: Tuple[Dict[str, str], ...]
    projects: Tuple[Dict[str, str], ...]
    mastery_check_prompts: Tuple[str, ...]
    mastery_pass_criteria: Tuple[str, ...]
    ordering_rationale: str
    known_gap_description: Optional[str] = None
    known_gap_remediation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEFAULT_CONCEPT_NODES: Tuple[ConceptNode, ...] = (
    ConceptNode(
        concept_id="math_linear_algebra_calculus",
        stage_id="stage_01_mathematical_and_programming_foundations",
        stage_order=1,
        stage_title="Stage 1: Mathematical, Numerical, and Optimization Foundations",
        lesson_id="lesson_01_linear_algebra_and_multivariable_calculus",
        lesson_order=1,
        title="Linear Algebra, Vector Spaces, and Multivariable Calculus for Machine Learning",
        prerequisites=(),
        learning_objectives=(
            "Represent inputs, parameters, and activations as vectors, matrices, and tensors.",
            "Compute Jacobians, directional derivatives, and multivariable chain-rule expansions.",
            "Connect geometric linear transformations (3Blue1Brown visual intuition) to matrix multiplication.",
        ),
        retrieval_queries=(
            "chain rule partial derivatives gradient vector matrix multiplication",
            "3Blue1Brown linear algebra neural networks visual intuition",
        ),
        keywords=("linear", "algebra", "calculus", "vector", "matrix", "gradient", "derivative", "math", "foundations"),
        preferred_resource_ids=("res_63c0a0d121a09f38", "res_b6377e0dd4279dae", "res_24d64fe06f34c722"),
        explanation_template=(
            "Every neural architecture is a composition of parameterized linear maps and nonlinear activations. "
            "Before studying multilayer networks, learners must master vector spaces, matrix multiplication, "
            "eigenvalues/singular values, and multivariable calculus. In our knowledge store, visual geometric "
            "intuition is anchored by verified 3Blue1Brown educational resources, while the exact chain-rule "
            "formulation is grounded in the primary Backpropagation text."
        ),
        exercises=(
            {
                "exercise_id": "ex_01_jacobian_chain_rule",
                "title": "Analytical & Numerical Jacobian Verification",
                "description": "Derive the analytical Jacobian of f(W, x, b) = tanh(Wx + b) with respect to W, x, and b, and verify it against finite-difference approximations in Python/NumPy.",
                "deliverable": "Pure-Python/NumPy script with < 1e-6 maximum relative gradient error.",
            },
        ),
        projects=(
            {
                "project_id": "proj_01_numerical_tensor_microkernel",
                "title": "Vectorized Matrix & Autodiff Primitives Workbench",
                "description": "Implement batched matrix multiplication, broadcasting rules, and finite-difference gradient checking without deep learning frameworks.",
                "deliverable": "Tested Python module + reproducible benchmark notebook.",
            },
        ),
        mastery_check_prompts=(
            "Derive the gradient of L = 1/2 ||Wx - y||_2^2 with respect to W and verify dimensional consistency.",
            "Explain why non-commutative matrix multiplication order determines forward vs. backward transpose operations.",
        ),
        mastery_pass_criteria=(
            "Correctly derives matrix gradients with matching shapes (m x n vs. n x m).",
            "Numeric finite-difference check agrees with analytical expression within 1e-6.",
        ),
        ordering_rationale=(
            "Placed first (root of the prerequisite DAG) because backpropagation, stochastic optimization, "
            "and attention mechanisms all require matrix algebra and multivariable chain-rule fluency."
        ),
        known_gap_description=(
            "3Blue1Brown resources (res_63c0a0d121a09f38, res_b6377e0dd4279dae) are verified metadata-only "
            "records in SQLite (permits_full_text_storage=0); full lesson transcripts for linear algebra and "
            "Python programming foundations are not stored in Chroma."
        ),
        known_gap_remediation=(
            "Approve and ingest open-licensed linear algebra/calculus textbook chunks (e.g., CC-BY math notes) "
            "or authorized caption tracks via the transcript ingestion pipeline."
        ),
    ),
    ConceptNode(
        concept_id="backpropagation_and_gradient_descent",
        stage_id="stage_01_mathematical_and_programming_foundations",
        stage_order=1,
        stage_title="Stage 1: Mathematical, Numerical, and Optimization Foundations",
        lesson_id="lesson_02_backpropagation_and_automatic_differentiation",
        lesson_order=2,
        title="Backpropagation, Reverse-Mode Automatic Differentiation, and Gradient Descent",
        prerequisites=("math_linear_algebra_calculus",),
        learning_objectives=(
            "Formulate reverse-mode automatic differentiation over a computational graph using the chain rule.",
            "Explain how backpropagation computes layer-wise error sensitivities efficiently in a single backward pass.",
            "Implement forward and backward passes for a multilayer perceptron trained via stochastic gradient descent.",
        ),
        retrieval_queries=(
            "backpropagation gradient computation multilayer neural networks chain rule gradient descent",
            "reverse mode automatic differentiation weight update loss function",
        ),
        keywords=("backpropagation", "gradient", "descent", "optimization", "chain", "differentiation", "mlp"),
        preferred_resource_ids=("res_24d64fe06f34c722", "res_0098016d9acf1f99", "res_a630f9fe47ef1eb5"),
        explanation_template=(
            "Backpropagation applies the multivariable chain rule recursively from the output loss backward through "
            "intermediate layers to compute exact gradients with respect to every weight. Rather than recomputing "
            "shared subexpressions, reverse-mode differentiation caches forward activations and propagates local "
            "sensitivities in O(W) time, enabling gradient descent optimization across multilayer networks."
        ),
        exercises=(
            {
                "exercise_id": "ex_02_reverse_mode_mlp",
                "title": "Two-Layer MLP Backward Pass from Scratch",
                "description": "Implement forward caching and backward gradient propagation for a 2-layer MLP with cross-entropy loss and compare against numerical gradients.",
                "deliverable": "Unit-tested backpropagation module passing gradient checks on synthetic classification data.",
            },
        ),
        projects=(
            {
                "project_id": "proj_02_scalar_and_tensor_autograd_engine",
                "title": "Minimal Reverse-Mode Autograd Engine",
                "description": "Build a computational graph engine supporting topological sorting, reverse-mode backprop, and SGD/Momentum optimizers.",
                "deliverable": "End-to-end training run demonstrating loss convergence on a non-linear decision boundary.",
            },
        ),
        mastery_check_prompts=(
            "Explain why reverse-mode differentiation scales vastly better than forward-mode differentiation when mapping millions of parameters to a scalar loss.",
            "Trace the exact cached quantities needed from the forward pass to compute dL/dW^(l) during the backward pass.",
        ),
        mastery_pass_criteria=(
            "Identifies upstream gradient delta^(l) and previous activation a^(l-1) as the required outer-product terms.",
            "Correctly contrasts O(1) backward sweeps for scalar losses with O(N) forward-mode sweeps.",
        ),
        ordering_rationale=(
            "Immediately follows Linear Algebra & Multivariable Calculus (`math_linear_algebra_calculus`) and precedes "
            "all deep neural architectures because every subsequent model relies on backpropagation for credit assignment."
        ),
        known_gap_description=None,
        known_gap_remediation=None,
    ),
    ConceptNode(
        concept_id="regularization_and_dropout",
        stage_id="stage_02_core_deep_learning_and_regularization",
        stage_order=2,
        stage_title="Stage 2: Core Deep Learning, Generalization, and Energy-Based Models",
        lesson_id="lesson_03_generalization_and_dropout_regularization",
        lesson_order=3,
        title="Overfitting, Feature Co-Adaptation, and Dropout Regularization",
        prerequisites=("backpropagation_and_gradient_descent",),
        learning_objectives=(
            "Diagnose overfitting and complex feature co-adaptation in overparameterized feedforward networks.",
            "Implement inverted dropout during training and deterministic weight scaling at inference time.",
            "Analyze dropout as approximate geometric model averaging across exponentially many thinned sub-networks.",
        ),
        retrieval_queries=(
            "dropout preventing co-adaptation of feature detectors overfitting regularization neural networks",
            "Hinton dropout随机 omitting hidden units test time scaling",
        ),
        keywords=("dropout", "regularization", "overfitting", "co-adaptation", "generalization", "neural"),
        preferred_resource_ids=("res_ab5dba0c12050d69", "res_e344b54452f01c96"),
        explanation_template=(
            "When a large feedforward neural network is trained on a finite dataset, hidden units can co-adapt "
            "to fix errors made by other specific units, leading to brittle generalization on unseen test data. "
            "Randomly omitting a fraction of hidden feature detectors on each training case prevents complex "
            "co-adaptations and acts as an efficient ensemble over thinned networks."
        ),
        exercises=(
            {
                "exercise_id": "ex_03_inverted_dropout_layer",
                "title": "Train vs. Eval Mode Inverted Dropout",
                "description": "Implement an inverted dropout layer with configurable drop probability p, verifying that expected activation magnitude remains invariant between training and evaluation modes.",
                "deliverable": "Python test suite validating expectation invariance and overfitting reduction.",
            },
        ),
        projects=(
            {
                "project_id": "proj_03_generalization_ablation_study",
                "title": "Regularization Ablation Study (No Regularization vs. L2 vs. Dropout)",
                "description": "Train deep MLPs on a small-sample benchmark across varying dropout rates (p in {0.0, 0.2, 0.5}) and log train/validation generalization gaps.",
                "deliverable": "Structured experiment report with learning curves and co-adaptation diagnostics.",
            },
        ),
        mastery_check_prompts=(
            "Why does randomly zeroing hidden units with probability p = 0.5 prevent feature co-adaptation?",
            "How must outgoing weights or training activations be scaled so that test-time forward passes approximate the ensemble geometric mean without sampling masks?",
        ),
        mastery_pass_criteria=(
            "Explains that each hidden unit must learn to be useful across many random subsets of collaborating units.",
            "Accurately derives the 1/(1-p) inverted training scaling or (1-p) test-time weight multiplication.",
        ),
        ordering_rationale=(
            "Requires `backpropagation_and_gradient_descent` to train multilayer networks first, and precedes deeper "
            "architectural scaling (`deep_residual_networks`) where optimization and generalization must be separated."
        ),
        known_gap_description=None,
        known_gap_remediation=None,
    ),
    ConceptNode(
        concept_id="energy_based_and_boltzmann_models",
        stage_id="stage_02_core_deep_learning_and_regularization",
        stage_order=2,
        stage_title="Stage 2: Core Deep Learning, Generalization, and Energy-Based Models",
        lesson_id="lesson_04_boltzmann_machines_and_stochastic_ representations",
        lesson_order=4,
        title="Energy-Based Models, Boltzmann Machines, and Stochastic Representation Learning",
        prerequisites=("backpropagation_and_gradient_descent",),
        learning_objectives=(
            "Define the global energy function E(s) of a Boltzmann machine over symmetric binary stochastic units.",
            "Relate the Boltzmann-Gibbs equilibrium probability distribution to thermal temperature T and energy gaps.",
            "Explain why unrestricted partition function intractability motivates Restricted Boltzmann Machines (RBMs) and Contrastive Divergence.",
        ),
        retrieval_queries=(
            "Boltzmann machine stochastic recurrent neural network energy function Restricted Boltzmann Machine",
            "Hinton Sejnowski simulated annealing Markov random field equilibrium distribution",
        ),
        keywords=("boltzmann", "energy", "stochastic", "rbm", "generative", "representation", "hinton"),
        preferred_resource_ids=("res_dd13dd0076cd4a6e", "res_0098016d9acf1f99", "res_e344b54452f01c96"),
        explanation_template=(
            "Beyond deterministic feedforward mappings, stochastic energy-based models assign a scalar energy "
            "to every joint configuration of visible and hidden units. Introduced by Geoffrey Hinton and Terry "
            "Sejnowski, Boltzmann machines bridge statistical mechanics and representation learning, while "
            "Restricted Boltzmann Machines (RBMs) remove intra-layer connections to make conditional inference tractable."
        ),
        exercises=(
            {
                "exercise_id": "ex_04_gibbs_sampling_rbm",
                "title": "Energy Function & Block Gibbs Sampler for a Toy RBM",
                "description": "Compute exact configuration energies E(v, h) on a small binary system and compare exact marginal probabilities against block Gibbs sampling.",
                "deliverable": "Python script verifying KL divergence convergence toward the true Boltzmann distribution.",
            },
        ),
        projects=(
            {
                "project_id": "proj_04_rbm_feature_discovery",
                "title": "Unsupervised Latent Feature Learning with an RBM",
                "description": "Train a Restricted Boltzmann Machine using k-step Contrastive Divergence (CD-k) on binary patterns and inspect learned receptive fields.",
                "deliverable": "Reproducible experiment log + visualization of hidden unit weights and reconstructions.",
            },
        ),
        mastery_check_prompts=(
            "Write the global energy function E(s) of a Boltzmann machine and explain the role of symmetric weights w_ij = w_ji with zero self-connections.",
            "Why does removing within-layer connections in an RBM make P(h | v) and P(v | h) conditionally independent?",
        ),
        mastery_pass_criteria=(
            "Correctly states E(s) = -sum_{i<j} w_ij s_i s_j - sum_i theta_i s_i.",
            "Demonstrates that bipartite graph factorization eliminates couplings among hidden units given visible units.",
        ),
        ordering_rationale=(
            "Builds on optimization and probability foundations from Stage 1 to introduce unsupervised/generative "
            "representation learning before modern deep architectures and frontier research."
        ),
        known_gap_description=None,
        known_gap_remediation=None,
    ),
    ConceptNode(
        concept_id="deep_residual_networks",
        stage_id="stage_03_deep_architectures_and_sequence_attention",
        stage_order=3,
        stage_title="Stage 3: Deep Residual Architectures, Sequence Alignment, and Transformers",
        lesson_id="lesson_05_residual_learning_and_identity_shortcuts",
        lesson_order=5,
        title="Deep Residual Learning (ResNet), Degradation vs. Overfitting, and Identity Shortcut Connections",
        prerequisites=("backpropagation_and_gradient_descent", "regularization_and_dropout"),
        learning_objectives=(
            "Distinguish the deep network training degradation problem from classical overfitting.",
            "Reformulate stacked layers to learn residual mappings F(x) := H(x) - x with parameter-free identity shortcuts F(x) + x.",
            "Analyze why identity skip connections preserve gradient flow across 100+ layer networks and Transformer blocks.",
        ),
        retrieval_queries=(
            "Deep Residual Learning for Image Recognition residual mapping shortcut connections degradation",
            "ResNet easier to optimize identity mapping ImageNet",
        ),
        keywords=("residual", "resnet", "shortcut", "skip", "depth", "vision", "cnn", "architecture"),
        preferred_resource_ids=("res_200d1687405c7db9",),
        explanation_template=(
            "As neural networks grow deeper, training accuracy can saturate and degrade rapidly—not from overfitting, "
            "but because stacked nonlinear layers struggle to approximate identity mappings. By explicitly reformulating "
            "layers to learn residual functions F(x) = H(x) - x added to an identity shortcut x, Deep Residual Networks "
            "(ResNets) enable stable optimization at extreme depth without adding extra parameters or computational complexity."
        ),
        exercises=(
            {
                "exercise_id": "ex_05_residual_gradient_flow",
                "title": "Plain Network vs. Residual Block Gradient Norm Diagnostic",
                "description": "Implement a ResidualBlock(x) = x + F(x) and measure backward gradient norms dL/dx across a 32-block stack compared to a plain stack.",
                "deliverable": "Diagnostic script demonstrating non-vanishing gradient propagation through identity shortcuts.",
            },
        ),
        projects=(
            {
                "project_id": "proj_05_resnet_ablation_benchmark",
                "title": "Plain vs. Residual Network Degradation Reproduction",
                "description": "Compare shallow vs. deep plain networks against residual counterparts, logging training error to verify resolution of the degradation anomaly.",
                "deliverable": "Benchmark script and quantitative training/validation loss comparison table.",
            },
        ),
        mastery_check_prompts=(
            "Why do the authors of ResNet argue that the degradation of training accuracy in deeper plain networks is NOT caused by overfitting?",
            "Using the backward chain rule on y = x + F(x, W), show why gradients can flow unimpeded to earlier layers even when dF/dx is small.",
        ),
        mastery_pass_criteria=(
            "Points out that overfitting lowers test accuracy while training error stays low, whereas degradation raises training error itself.",
            "Derives dy/dx = I + dF/dx, showing the additive identity term I prevents multiplicative gradient collapse.",
        ),
        ordering_rationale=(
            "Requires both `backpropagation_and_gradient_descent` and `regularization_and_dropout` (to contrast degradation "
            "against overfitting) and provides the residual skip-connection primitive used inside every Transformer sub-layer."
        ),
        known_gap_description=None,
        known_gap_remediation=None,
    ),
    ConceptNode(
        concept_id="sequence_alignment_and_bahdanau_attention",
        stage_id="stage_03_deep_architectures_and_sequence_attention",
        stage_order=3,
        stage_title="Stage 3: Deep Residual Architectures, Sequence Alignment, and Transformers",
        lesson_id="lesson_06_encoder_decoder_bottlenecks_and_bahdanau_attention",
        lesson_order=6,
        title="Sequence-to-Sequence Bottlenecks and Joint Alignment & Translation (Bahdanau Attention)",
        prerequisites=("backpropagation_and_gradient_descent", "regularization_and_dropout"),
        learning_objectives=(
            "Identify why compressing variable-length input sequences into a single fixed-length vector bottlenecks encoder-decoder performance.",
            "Formulate additive (Bahdanau) soft alignment scores e_{ij}, attention weights alpha_{ij}, and dynamic context vectors c_i.",
            "Implement a Differentiable Attention Alignment module that searches over source annotations during decoding.",
        ),
        retrieval_queries=(
            "Neural Machine Translation by Jointly Learning to Align and Translate fixed-length vector bottleneck",
            "encoder-decoder soft search source sentence alignment attention",
        ),
        keywords=("attention", "bahdanau", "alignment", "translation", "sequence", "encoder", "decoder", "nlp"),
        preferred_resource_ids=("res_0b4b872c018ccdcc", "res_5fb3d618c7ed17ec"),
        explanation_template=(
            "Classical encoder-decoder sequence models compress an entire input sentence into a single fixed-length vector, "
            "causing severe information loss on long sequences. Bahdanau, Cho, and Bengio (2014) resolved this bottleneck "
            "by allowing the decoder to softly search over all encoder hidden annotations at each generation step, "
            "computing differentiable alignment weights jointly with translation."
        ),
        exercises=(
            {
                "exercise_id": "ex_06_additive_attention_alignment",
                "title": "Additive (Bahdanau) Attention & Alignment Matrix Inspector",
                "description": "Implement score(s_{i-1}, h_j) = v_a^T tanh(W_a s_{i-1} + U_a h_j), normalize with softmax, and compute step-wise context vectors.",
                "deliverable": "Python module outputting normalized alignment weights (summing to 1.0 per decoding step).",
            },
        ),
        projects=(
            {
                "project_id": "proj_06_seq2seq_length_bottleneck_study",
                "title": "Fixed-Vector Encoder-Decoder vs. Soft-Attention Sequence Benchmark",
                "description": "Evaluate sequence reconstruction/lookup accuracy as sequence length grows from 8 to 64 tokens with and without soft attention.",
                "deliverable": "Accuracy-vs-sequence-length evaluation report and attention heatmap artifact.",
            },
        ),
        mastery_check_prompts=(
            "What specific structural limitation of standard encoder-decoder architectures motivated Bahdanau et al. (2014)?",
            "How does soft alignment remain end-to-end differentiable with standard backpropagation?",
        ),
        mastery_pass_criteria=(
            "Identifies the fixed-length context vector compression bottleneck on long sentences.",
            "Explains that continuous softmax-weighted sums over encoder annotations replace discrete hard alignments.",
        ),
        ordering_rationale=(
            "Builds on Stage 1-2 neural training foundations and must precede `transformer_self_attention_architecture`, "
            "which generalizes encoder-decoder alignment into purely attention-driven sequence transduction."
        ),
        known_gap_description=None,
        known_gap_remediation=None,
    ),
    ConceptNode(
        concept_id="transformer_self_attention_architecture",
        stage_id="stage_03_deep_architectures_and_sequence_attention",
        stage_order=3,
        stage_title="Stage 3: Deep Residual Architectures, Sequence Alignment, and Transformers",
        lesson_id="lesson_07_transformer_multi_head_self_attention",
        lesson_order=7,
        title="The Transformer Architecture: Scaled Dot-Product and Multi-Head Self-Attention",
        prerequisites=("deep_residual_networks", "sequence_alignment_and_bahdanau_attention"),
        learning_objectives=(
            "Contrast recurrent/convolutional sequence transduction with the recurrence-free Transformer architecture.",
            "Implement Scaled Dot-Product Attention, Multi-Head Attention projections, and residual layer-normalized sub-layers.",
            "Explain how eliminating sequential recurrence unlocks O(1) sequential operations and massive training parallelization.",
        ),
        retrieval_queries=(
            "Attention Is All You Need Transformer dispensing with recurrence and convolutions parallelizable",
            "multi-head self-attention encoder decoder sequence transduction WMT translation",
        ),
        keywords=("transformer", "self-attention", "multi-head", "parallelization", "recurrence", "vaswani"),
        preferred_resource_ids=("res_a475a00509c48269",),
        explanation_template=(
            "Vaswani et al. (2017) introduced the Transformer, a sequence transduction architecture based solely on "
            "attention mechanisms, dispensing with recurrence and convolutions entirely. By combining multi-head "
            "self-attention with residual connections (from ResNet) and position-wise feed-forward networks, "
            "Transformers achieve superior quality while allowing significantly greater parallelization during training."
        ),
        exercises=(
            {
                "exercise_id": "ex_07_scaled_dot_product_multihead",
                "title": "Scaled Dot-Product & Causal Multi-Head Attention Implementation",
                "description": "Implement Attention(Q, K, V) = softmax(QK^T / sqrt(d_k))V with causal masking and multi-head concatenation.",
                "deliverable": "Unit-tested Python module verifying causal masking (zero leakage from future tokens) and shape invariants.",
            },
        ),
        projects=(
            {
                "project_id": "proj_07_mini_transformer_transducer",
                "title": "End-to-End Transformer Block with Residual Connections",
                "description": "Assemble multi-head self-attention, residual skip connections, layer normalization, and position-wise MLPs into a complete trainable Transformer encoder-decoder.",
                "deliverable": "Trainable Transformer implementation with attention head inspection and throughput comparison.",
            },
        ),
        mastery_check_prompts=(
            "How does the Transformer differ from prior dominant sequence transduction models that paired RNNs or CNNs with attention?",
            "Why do residual connections (`deep_residual_networks`) and attention alignment (`sequence_alignment_and_bahdanau_attention`) both serve as direct prerequisites for the Transformer?",
        ),
        mastery_pass_criteria=(
            "States that the Transformer dispenses with recurrence and convolutions entirely, relying solely on attention mechanisms.",
            "Explains that self-attention generalizes alignment while residual shortcuts enable deep stacking of attention/MLP sublayers.",
        ),
        ordering_rationale=(
            "Requires both `deep_residual_networks` (for residual sub-layer connections) and "
            "`sequence_alignment_and_bahdanau_attention` (for attention mechanisms) before synthesizing the full Transformer."
        ),
        known_gap_description=None,
        known_gap_remediation=None,
    ),
    ConceptNode(
        concept_id="ai_research_methodology_and_safety_frontiers",
        stage_id="stage_04_research_methodology_and_frontier_safety",
        stage_order=4,
        stage_title="Stage 4: AI Research Methodology, Generative Frontiers, and AI Safety",
        lesson_id="lesson_08_research_formulation_ablation_and_ai_safety",
        lesson_order=8,
        title="Transitioning from AI Engineer to AI Researcher: Hypothesis Design, GFlowNets, and AI Safety",
        prerequisites=("energy_based_and_boltzmann_models", "transformer_self_attention_architecture"),
        learning_objectives=(
            "Design controlled ablation experiments, baselines, and reproducible evaluation harnesses (exemplified by Dropout, ResNet, and Transformer papers).",
            "Survey frontier research directions in our curated catalog, including Generative Flow Networks (GFlowNets) and honest/cautious scientist AI safety frameworks.",
            "Maintain strict epistemic hygiene: separate primary empirical evidence from metadata-only research seeds and candidate news feeds (`SRC-THE-BATCH`).",
        ),
        retrieval_queries=(
            "Yoshua Bengio research deep learning AI safety governance Generative Flow Networks",
            "Geoffrey Hinton publications representation learning Boltzmann machines neural networks",
        ),
        keywords=("research", "researcher", "safety", "alignment", "gflownets", "bengio", "hinton", "ablation", "frontier"),
        preferred_resource_ids=(
            "res_5fb3d618c7ed17ec",
            "res_73e51a454ee1f4eb",
            "res_e344b54452f01c96",
            "res_17e0fc05c2897a85",
            "res_99569ba920d30adb",
            "res_c6cd5d1200d9419a",
        ),
        explanation_template=(
            "Transitioning from an AI engineer who implements architectures to an AI researcher who advances the field "
            "requires rigorous hypothesis formulation, controlled ablations, and awareness of long-horizon safety and "
            "governance challenges. In our curated repository, verified research portals from Geoffrey Hinton and Yoshua "
            "Bengio define key themes in representation learning, Generative Flow Networks, and AI safety, while also "
            "highlighting where full-text primary papers remain to be collected."
        ),
        exercises=(
            {
                "exercise_id": "ex_08_research_ablation_protocol",
                "title": "Pre-Registered Ablation & Citation Integrity Audit",
                "description": "Write a structured research ablation protocol comparing two architectural hypotheses, specifying primary metrics, negative controls, and verified source citations.",
                "deliverable": "Peer-review-ready experimental protocol document with explicit epistemic tier annotations.",
            },
        ),
        projects=(
            {
                "project_id": "proj_08_reproducible_research_replication",
                "title": "Mini-Paper Replication & Evidence Gap Analysis",
                "description": "Replicate a core claim from one indexed primary paper (Dropout, Bahdanau Attention, ResNet, or Transformer) and produce an evaluation report with abstention/uncertainty tracking.",
                "deliverable": "Reproducible benchmark script + structured replication report.",
            },
        ),
        mastery_check_prompts=(
            "Why must an AI researcher separate verified primary-source chunks (`CURRICULUM_READY_EVIDENCE`) from uncollected seed metadata (`APPROVED_UNCOLLECTED_RESOURCE`) and candidate news feeds (`SRC-THE-BATCH`)?",
            "What makes an architectural ablation study scientifically valid when claiming a new mechanism improves training or generalization?",
        ),
        mastery_pass_criteria=(
            "Accurately distinguishes the five epistemic source tiers and explains why news recency never dictates foundational truth.",
            "Identifies controlled parameter budgets, isolated causal variables, and reproducible baseline comparisons.",
        ),
        ordering_rationale=(
            "Placed as the capstone stage (`stage_04`) after mastering probabilistic/energy models (`energy_based_and_boltzmann_models`) "
            "and modern deep sequence/residual architectures (`transformer_self_attention_architecture`)."
        ),
        known_gap_description=(
            "Frontier AI safety and GFlowNet entries in SRC-BENGIO (e.g., res_17e0fc05c2897a85, res_99569ba920d30adb, "
            "res_c6cd5d1200d9419a) are currently registered as approved uncollected seed records (`metadata-only-not-yet-collected`), "
            "and `SRC-THE-BATCH` remains `CANDIDATE_PENDING_REVIEW`."
        ),
        known_gap_remediation=(
            "Collect and chunk full open-access arXiv/technical report texts for approved AI safety and GFlowNet publications "
            "before generating deep mathematical derivations for those frontier subtopics."
        ),
    ),
)


class ConceptGraph:
    """Directed Acyclic Graph (DAG) of learning concepts and prerequisite relationships."""

    def __init__(self, nodes: Sequence[ConceptNode] = DEFAULT_CONCEPT_NODES) -> None:
        self._nodes: Dict[str, ConceptNode] = {}
        for node in nodes:
            if node.concept_id in self._nodes:
                raise PlatformValidationError(
                    "DUPLICATE_CONCEPT_ID",
                    f"Duplicate concept_id '{node.concept_id}' in ConceptGraph.",
                )
            self._nodes[node.concept_id] = node
        self.validate_dag()

    @property
    def nodes(self) -> Dict[str, ConceptNode]:
        return dict(self._nodes)

    def get_node(self, concept_id: str) -> ConceptNode:
        if concept_id not in self._nodes:
            raise PlatformValidationError(
                "CONCEPT_NOT_FOUND",
                f"Concept '{concept_id}' was not found in ConceptGraph.",
                {"concept_id": concept_id},
            )
        return self._nodes[concept_id]

    def validate_dag(self) -> List[str]:
        """Validate that all prerequisites exist, the graph is cycle-free, and return topological order."""
        in_degree: Dict[str, int] = {cid: 0 for cid in self._nodes}
        dependents: Dict[str, List[str]] = {cid: [] for cid in self._nodes}

        for cid, node in self._nodes.items():
            for prereq in node.prerequisites:
                if prereq not in self._nodes:
                    raise PlatformValidationError(
                        "MISSING_PREREQUISITE_NODE",
                        f"Concept '{cid}' declares unknown prerequisite '{prereq}'.",
                        {"concept_id": cid, "missing_prerequisite": prereq},
                    )
                in_degree[cid] += 1
                dependents[prereq].append(cid)

        # Deterministic Kahn's algorithm ordered by (stage_order, lesson_order, concept_id)
        ready = sorted(
            [cid for cid, deg in in_degree.items() if deg == 0],
            key=lambda c: (self._nodes[c].stage_order, self._nodes[c].lesson_order, c),
        )
        topo_order: List[str] = []
        while ready:
            current = ready.pop(0)
            topo_order.append(current)
            for dep in dependents[current]:
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    ready.append(dep)
                    ready.sort(
                        key=lambda c: (self._nodes[c].stage_order, self._nodes[c].lesson_order, c)
                    )

        if len(topo_order) != len(self._nodes):
            cycling = sorted(set(self._nodes.keys()) - set(topo_order))
            raise PlatformValidationError(
                "PREREQUISITE_CYCLE_DETECTED",
                f"Cycle detected in ConceptGraph involving concepts: {cycling}",
                {"cycling_concepts": cycling},
            )
        return topo_order

    def select_concepts_for_goal(self, learner_goal: str) -> List[ConceptNode]:
        """Select relevant concepts and close under transitive prerequisites in topological order."""
        goal_lower = learner_goal.lower()
        topo_ids = self.validate_dag()

        # Check if goal asks for a full foundational-to-researcher/engineer trajectory
        broad_triggers = (
            "ai engineer",
            "ai researcher",
            "machine learning engineer",
            "deep learning",
            "from mathematical",
            "foundations",
            "end-to-end",
            "complete curriculum",
            "full path",
        )
        if any(trigger in goal_lower for trigger in broad_triggers):
            selected_set: Set[str] = set(topo_ids)
        else:
            selected_set = set()
            goal_tokens = set(re.findall(r"[a-z0-9_]+", goal_lower))
            for cid, node in self._nodes.items():
                if any(kw in goal_lower or kw in goal_tokens for kw in node.keywords):
                    selected_set.add(cid)

        # Always include transitive prerequisites so learning path is self-contained
        queue = list(selected_set)
        while queue:
            cid = queue.pop(0)
            for prereq in self._nodes[cid].prerequisites:
                if prereq not in selected_set:
                    selected_set.add(prereq)
                    queue.append(prereq)

        return [self._nodes[cid] for cid in topo_ids if cid in selected_set]


def build_default_concept_graph() -> ConceptGraph:
    return ConceptGraph(DEFAULT_CONCEPT_NODES)


# ---------------------------------------------------------------------------
# Curriculum Output Schema & Deterministic Validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MasteryCheckDesign:
    """Design-time mastery check specification (explicitly not claiming live evaluation)."""

    check_id: str
    lesson_id: str
    assessment_status: str
    disclaimer: str
    prompts: Tuple[str, ...]
    pass_criteria: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "lesson_id": self.lesson_id,
            "assessment_status": self.assessment_status,
            "disclaimer": self.disclaimer,
            "prompts": list(self.prompts),
            "pass_criteria": list(self.pass_criteria),
        }


@dataclass(frozen=True)
class CurriculumLesson:
    """Single prerequisite-ordered lesson within a CurriculumStage."""

    lesson_id: str
    concept_id: str
    lesson_order: int
    title: str
    learning_objectives: Tuple[str, ...]
    prerequisites: Tuple[str, ...]
    prerequisite_lesson_ids: Tuple[str, ...]
    explanation: str
    reference_materials: Tuple[Dict[str, str], ...]
    exercises: Tuple[Dict[str, str], ...]
    projects: Tuple[Dict[str, str], ...]
    mastery_check: MasteryCheckDesign
    source_citations: Tuple[CurriculumCitation, ...]
    rationale_for_ordering: str
    evidence_gaps: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "concept_id": self.concept_id,
            "lesson_order": self.lesson_order,
            "title": self.title,
            "learning_objectives": list(self.learning_objectives),
            "prerequisites": list(self.prerequisites),
            "prerequisite_lesson_ids": list(self.prerequisite_lesson_ids),
            "explanation": self.explanation,
            "reference_materials": [dict(m) for m in self.reference_materials],
            "exercises": [dict(e) for e in self.exercises],
            "projects": [dict(p) for p in self.projects],
            "mastery_check": self.mastery_check.to_dict(),
            "source_citations": [c.to_dict() for c in self.source_citations],
            "rationale_for_ordering": self.rationale_for_ordering,
            "evidence_gaps": list(self.evidence_gaps),
        }


@dataclass(frozen=True)
class CurriculumStage:
    """Cohesive learning stage containing ordered lessons."""

    stage_id: str
    stage_order: int
    title: str
    stage_objectives: Tuple[str, ...]
    prerequisite_stage_ids: Tuple[str, ...]
    rationale_for_ordering: str
    lessons: Tuple[CurriculumLesson, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_order": self.stage_order,
            "title": self.title,
            "stage_objectives": list(self.stage_objectives),
            "prerequisite_stage_ids": list(self.prerequisite_stage_ids),
            "rationale_for_ordering": self.rationale_for_ordering,
            "lessons": [lesson.to_dict() for lesson in self.lessons],
        }


@dataclass(frozen=True)
class EvidenceGap:
    """Explicit report of missing full-text evidence or uncollected source coverage."""

    gap_id: str
    scope: str
    related_lesson_id: Optional[str]
    related_source_ids: Tuple[str, ...]
    current_evidence_tier: str
    description: str
    recommended_curation_action: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "scope": self.scope,
            "related_lesson_id": self.related_lesson_id,
            "related_source_ids": list(self.related_source_ids),
            "current_evidence_tier": self.current_evidence_tier,
            "description": self.description,
            "recommended_curation_action": self.recommended_curation_action,
        }


@dataclass(frozen=True)
class CurriculumValidationReport:
    """Deterministic validation result for a CurriculumPlan."""

    is_valid: bool
    schema_valid: bool
    prerequisites_consistent: bool
    citations_verified: bool
    mastery_disclaimer_present: bool
    the_batch_policy_respected: bool
    checked_lesson_count: int
    checked_citation_count: int
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "schema_valid": self.schema_valid,
            "prerequisites_consistent": self.prerequisites_consistent,
            "citations_verified": self.citations_verified,
            "mastery_disclaimer_present": self.mastery_disclaimer_present,
            "the_batch_policy_respected": self.the_batch_policy_respected,
            "checked_lesson_count": self.checked_lesson_count,
            "checked_citation_count": self.checked_citation_count,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class CurriculumPlan:
    """Complete structured output of the Learning Planner vertical slice."""

    curriculum_id: str
    run_id: str
    schema_version: str
    status: CurriculumStatus
    created_at: str
    learner_goal: str
    title: str
    executive_summary: str
    rationale_for_ordering: str
    mastery_assessment_disclaimer: str
    abstention_reason: Optional[str]
    source_ecosystem_summary: SourceEcosystemSummary
    stages: Tuple[CurriculumStage, ...]
    source_citations: Tuple[CurriculumCitation, ...]
    evidence_gaps: Tuple[EvidenceGap, ...]
    validation_report: CurriculumValidationReport

    def to_dict(self) -> Dict[str, Any]:
        return {
            "curriculum_id": self.curriculum_id,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "created_at": self.created_at,
            "learner_goal": self.learner_goal,
            "title": self.title,
            "executive_summary": self.executive_summary,
            "rationale_for_ordering": self.rationale_for_ordering,
            "mastery_assessment_disclaimer": self.mastery_assessment_disclaimer,
            "abstention_reason": self.abstention_reason,
            "source_ecosystem_summary": self.source_ecosystem_summary.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "source_citations": [cit.to_dict() for cit in self.source_citations],
            "evidence_gaps": [gap.to_dict() for gap in self.evidence_gaps],
            "validation_report": self.validation_report.to_dict(),
        }


class CurriculumValidator:
    """Deterministic validator enforcing schema, prerequisite DAG order, and citation integrity."""

    def __init__(self, ecosystem_service: SourceEcosystemService) -> None:
        self.ecosystem_service = ecosystem_service

    def validate(self, plan: CurriculumPlan) -> CurriculumValidationReport:
        errors: List[str] = []
        schema_valid = True
        prerequisites_consistent = True
        citations_verified = True
        mastery_disclaimer_present = True
        the_batch_policy_respected = True

        if not plan.curriculum_id or not plan.learner_goal.strip():
            schema_valid = False
            errors.append("CurriculumPlan must include non-empty curriculum_id and learner_goal.")

        if not plan.mastery_assessment_disclaimer or "not claimed" not in plan.mastery_assessment_disclaimer.lower():
            mastery_disclaimer_present = False
            errors.append("CurriculumPlan is missing the mandatory mastery assessment non-execution disclaimer.")

        if plan.status == CurriculumStatus.ABSTAINED_INSUFFICIENT_EVIDENCE:
            if not plan.abstention_reason:
                schema_valid = False
                errors.append("Abstained curriculum plan must provide a non-empty abstention_reason.")
            is_valid = len(errors) == 0
            return CurriculumValidationReport(
                is_valid=is_valid,
                schema_valid=schema_valid,
                prerequisites_consistent=prerequisites_consistent,
                citations_verified=citations_verified,
                mastery_disclaimer_present=mastery_disclaimer_present,
                the_batch_policy_respected=the_batch_policy_respected,
                checked_lesson_count=0,
                checked_citation_count=0,
                errors=tuple(errors),
            )

        if not plan.stages:
            schema_valid = False
            errors.append("Validated CurriculumPlan must contain at least one stage.")

        seen_stage_ids: Set[str] = set()
        seen_lesson_ids: Set[str] = set()
        seen_concept_ids: Set[str] = set()
        all_lesson_citations: List[CurriculumCitation] = []
        lesson_count = 0
        prev_stage_order = 0

        for stage in plan.stages:
            if not stage.stage_id or stage.stage_id in seen_stage_ids:
                schema_valid = False
                errors.append(f"Invalid or duplicate stage_id '{stage.stage_id}'.")
            if stage.stage_order <= prev_stage_order:
                prerequisites_consistent = False
                errors.append(
                    f"Stage '{stage.stage_id}' has non-increasing stage_order={stage.stage_order} after {prev_stage_order}."
                )
            prev_stage_order = stage.stage_order

            for prereq_stage_id in stage.prerequisite_stage_ids:
                if prereq_stage_id not in seen_stage_ids:
                    prerequisites_consistent = False
                    errors.append(
                        f"Stage '{stage.stage_id}' lists prerequisite stage '{prereq_stage_id}' that does not precede it."
                    )

            seen_stage_ids.add(stage.stage_id)
            if not stage.lessons:
                schema_valid = False
                errors.append(f"Stage '{stage.stage_id}' contains no lessons.")

            prev_lesson_order = 0
            for lesson in stage.lessons:
                lesson_count += 1
                if not lesson.lesson_id or lesson.lesson_id in seen_lesson_ids:
                    schema_valid = False
                    errors.append(f"Invalid or duplicate lesson_id '{lesson.lesson_id}'.")
                if lesson.lesson_order <= prev_lesson_order:
                    prerequisites_consistent = False
                    errors.append(
                        f"Lesson '{lesson.lesson_id}' has non-increasing lesson_order={lesson.lesson_order}."
                    )
                prev_lesson_order = lesson.lesson_order

                # Prerequisite topological order check: every prerequisite concept and lesson must already be seen!
                for prereq_cid in lesson.prerequisites:
                    if prereq_cid not in seen_concept_ids:
                        prerequisites_consistent = False
                        errors.append(
                            f"Lesson '{lesson.lesson_id}' (concept '{lesson.concept_id}') depends on prerequisite concept '{prereq_cid}' which appears later or is missing."
                        )
                for prereq_lid in lesson.prerequisite_lesson_ids:
                    if prereq_lid not in seen_lesson_ids:
                        prerequisites_consistent = False
                        errors.append(
                            f"Lesson '{lesson.lesson_id}' depends on prerequisite lesson '{prereq_lid}' which appears later or is missing."
                        )

                if not lesson.learning_objectives:
                    schema_valid = False
                    errors.append(f"Lesson '{lesson.lesson_id}' must declare learning_objectives.")
                if not lesson.explanation.strip():
                    schema_valid = False
                    errors.append(f"Lesson '{lesson.lesson_id}' must include an explanation.")
                if not lesson.exercises or not lesson.projects:
                    schema_valid = False
                    errors.append(f"Lesson '{lesson.lesson_id}' must include exercises and projects.")
                if not lesson.rationale_for_ordering.strip():
                    schema_valid = False
                    errors.append(f"Lesson '{lesson.lesson_id}' must include rationale_for_ordering.")
                if not lesson.source_citations:
                    citations_verified = False
                    errors.append(f"Lesson '{lesson.lesson_id}' has zero source_citations.")

                mc = lesson.mastery_check
                if (
                    mc.assessment_status != "DESIGN_ONLY_NOT_YET_ASSESSED"
                    or "not claimed" not in mc.disclaimer.lower()
                    or not mc.prompts
                    or not mc.pass_criteria
                ):
                    mastery_disclaimer_present = False
                    errors.append(
                        f"Lesson '{lesson.lesson_id}' mastery_check must be DESIGN_ONLY_NOT_YET_ASSESSED with prompts, pass_criteria, and disclaimer."
                    )

                for cit in lesson.source_citations:
                    all_lesson_citations.append(cit)
                    if cit.source_id == THE_BATCH_SOURCE_ID:
                        the_batch_policy_respected = False
                        errors.append(
                            f"Lesson '{lesson.lesson_id}' cites candidate discovery source '{THE_BATCH_SOURCE_ID}'."
                        )

                seen_lesson_ids.add(lesson.lesson_id)
                seen_concept_ids.add(lesson.concept_id)

        # Verify all citations against SQLite knowledge store
        combined_citations = list(all_lesson_citations) + list(plan.source_citations)
        cit_ok, cit_errors = self.ecosystem_service.verify_citation_records(combined_citations)
        if not cit_ok:
            citations_verified = False
            errors.extend(cit_errors)
            if any(THE_BATCH_SOURCE_ID in err for err in cit_errors):
                the_batch_policy_respected = False

        # Ensure inline [chk_...] tokens in explanations match lesson's verified chunk citations
        for stage in plan.stages:
            for lesson in stage.lessons:
                valid_lesson_chunks = {
                    c.chunk_id for c in lesson.source_citations if c.chunk_id is not None
                }
                inline_chunks = set(re.findall(r"chk_[0-9a-fA-F]{4,64}", lesson.explanation))
                for inline_cid in sorted(inline_chunks):
                    if inline_cid not in valid_lesson_chunks:
                        citations_verified = False
                        errors.append(
                            f"Lesson '{lesson.lesson_id}' explanation contains unverified inline chunk token '{inline_cid}'."
                        )

        is_valid = (
            schema_valid
            and prerequisites_consistent
            and citations_verified
            and mastery_disclaimer_present
            and the_batch_policy_respected
            and len(errors) == 0
        )
        return CurriculumValidationReport(
            is_valid=is_valid,
            schema_valid=schema_valid,
            prerequisites_consistent=prerequisites_consistent,
            citations_verified=citations_verified,
            mastery_disclaimer_present=mastery_disclaimer_present,
            the_batch_policy_respected=the_batch_policy_respected,
            checked_lesson_count=lesson_count,
            checked_citation_count=len(all_lesson_citations),
            errors=tuple(errors),
        )


# ---------------------------------------------------------------------------
# Learning Planner Agent & Platform Orchestrator
# ---------------------------------------------------------------------------


_OFF_DOMAIN_KEYWORDS = (
    "sourdough",
    "pastry",
    "knitting",
    "woodworking",
    "plumbing",
    "astrology",
    "gardening",
    "pottery",
)

_AI_DOMAIN_KEYWORDS = (
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural",
    "backpropagation",
    "transformer",
    "attention",
    "resnet",
    "residual",
    "dropout",
    "boltzmann",
    "rbm",
    "linear algebra",
    "calculus",
    "gradient",
    "optimization",
    "researcher",
    "engineer",
    "nlp",
    "vision",
    "representation",
    "safety",
    "gflownet",
)


def validate_learning_goal(learner_goal: Any) -> str:
    """Deterministically validate a learner goal string before agent execution."""
    if not isinstance(learner_goal, str):
        raise PlatformValidationError(
            "INVALID_GOAL_TYPE",
            "learner_goal must be a string.",
        )
    cleaned = " ".join(learner_goal.strip().split())
    if len(cleaned) < 12:
        raise PlatformValidationError(
            "GOAL_TOO_SHORT",
            "learner_goal must be at least 12 characters long and describe a concrete learning objective.",
            {"min_length": 12, "actual_length": len(cleaned)},
        )
    if len(cleaned) > 1000:
        raise PlatformValidationError(
            "GOAL_TOO_LONG",
            "learner_goal must not exceed 1000 characters.",
            {"max_length": 1000, "actual_length": len(cleaned)},
        )
    return cleaned


class LearningPlannerAgent:
    """First vertical slice agent: turns a learner goal into a grounded, validated curriculum."""

    capability = AgentCapability.LEARNING_PLANNER

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        validator: CurriculumValidator,
        concept_graph: Optional[ConceptGraph] = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.validator = validator
        self.concept_graph = concept_graph or build_default_concept_graph()

    def _is_goal_in_domain(self, goal: str) -> Tuple[bool, Optional[str]]:
        goal_lower = goal.lower()
        has_ai_signal = any(kw in goal_lower for kw in _AI_DOMAIN_KEYWORDS)
        has_off_domain = any(kw in goal_lower for kw in _OFF_DOMAIN_KEYWORDS)
        if has_off_domain and not has_ai_signal:
            return (
                False,
                "Learner goal falls outside the AI/ML research and engineering domain covered by the approved knowledge store.",
            )
        if not has_ai_signal:
            return (
                False,
                "Insufficient domain overlap with indexed AI/ML concepts (math foundations, neural networks, optimization, sequence attention, or AI research).",
            )
        return (True, None)

    def execute(
        self,
        *,
        run_state: WorkflowRunState,
        record_step: Callable[[str, str, Dict[str, Any], Callable[[], Any]], Any],
    ) -> CurriculumPlan:
        """Execute the bounded Learning Planner workflow using registered read-only tools."""
        # Step 1: Validate learner goal & domain fit
        cleaned_goal = record_step(
            "validate_learner_goal",
            "DeterministicGoalValidator",
            {"raw_goal_length": len(run_state.learner_goal or "")},
            lambda: validate_learning_goal(run_state.learner_goal),
        )
        in_domain, domain_abstention_reason = self._is_goal_in_domain(cleaned_goal)

        # Step 2: Inspect 5-tier source ecosystem via ToolRegistry
        ecosystem_summary: SourceEcosystemSummary = record_step(
            "inspect_source_ecosystem",
            self.capability.value,
            {"tool": "inspect_source_ecosystem"},
            lambda: self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="inspect_source_ecosystem",
            ),
        )

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        goal_hash = hashlib.sha256(cleaned_goal.encode("utf-8")).hexdigest()[:12]
        curriculum_id = f"curr_{goal_hash}"

        # Abstain if off-domain or if the knowledge base has zero curriculum-ready evidence
        if not in_domain or (
            ecosystem_summary.curriculum_ready_chunk_count == 0
            and ecosystem_summary.collected_document_count == 0
        ):
            reason = domain_abstention_reason or (
                "Knowledge store contains zero curriculum-ready chunks and zero verified collected documents; "
                "abstaining rather than fabricating ungrounded curriculum content."
            )
            abstained_plan = CurriculumPlan(
                curriculum_id=curriculum_id,
                run_id=run_state.run_id,
                schema_version=CURRICULUM_SCHEMA_VERSION,
                status=CurriculumStatus.ABSTAINED_INSUFFICIENT_EVIDENCE,
                created_at=now_iso,
                learner_goal=cleaned_goal,
                title="Abstained Curriculum Plan — Insufficient Grounded Evidence",
                executive_summary=reason,
                rationale_for_ordering="No stages ordered because curriculum generation abstained due to insufficient domain evidence.",
                mastery_assessment_disclaimer=MASTERY_ASSESSMENT_DISCLAIMER,
                abstention_reason=reason,
                source_ecosystem_summary=ecosystem_summary,
                stages=(),
                source_citations=(),
                evidence_gaps=(
                    EvidenceGap(
                        gap_id="gap_abstention_01",
                        scope="CURRICULUM_WIDE",
                        related_lesson_id=None,
                        related_source_ids=(),
                        current_evidence_tier=EvidenceTier.APPROVED_UNCOLLECTED_RESOURCE.value,
                        description=reason,
                        recommended_curation_action="Approve and ingest relevant primary sources before requesting a curriculum on this topic.",
                    ),
                ),
                validation_report=CurriculumValidationReport(
                    is_valid=True,
                    schema_valid=True,
                    prerequisites_consistent=True,
                    citations_verified=True,
                    mastery_disclaimer_present=True,
                    the_batch_policy_respected=True,
                    checked_lesson_count=0,
                    checked_citation_count=0,
                    errors=(),
                ),
            )
            val_report = record_step(
                "validate_curriculum_output",
                "CurriculumValidator",
                {"status": abstained_plan.status.value},
                lambda: self.validator.validate(abstained_plan),
            )
            return CurriculumPlan(**{**abstained_plan.__dict__, "validation_report": val_report})

        # Step 3: Select prerequisite-closed concept subgraph in topological order
        selected_concepts: List[ConceptNode] = record_step(
            "select_and_order_concepts",
            self.capability.value,
            {"tool": "query_concept_graph"},
            lambda: self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="query_concept_graph",
                learner_goal=cleaned_goal,
            ),
        )

        # Step 4: Load approved resources catalog via ToolRegistry
        approved_resources_list: List[Dict[str, Any]] = record_step(
            "load_approved_resources_catalog",
            self.capability.value,
            {"tool": "list_approved_resources"},
            lambda: self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="list_approved_resources",
                only_collected=False,
            ),
        )
        resources_by_id: Dict[str, Dict[str, Any]] = {
            r["resource_id"]: r for r in approved_resources_list
        }

        # Step 5: Retrieve vector-indexed knowledge chunks for each selected concept
        concept_chunks: Dict[str, List[RetrievedChunk]] = {}

        def _retrieve_all_concept_evidence() -> Dict[str, List[RetrievedChunk]]:
            retrieved_map: Dict[str, List[RetrievedChunk]] = {}
            for node in selected_concepts:
                primary_query = " ".join(node.retrieval_queries)
                chunks: List[RetrievedChunk] = self.tool_registry.invoke(
                    run_state=run_state,
                    capability=self.capability,
                    tool_name="search_knowledge_chunks",
                    query_text=primary_query,
                    top_k=run_state.execution_policy.top_k_per_query,
                )
                retrieved_map[node.concept_id] = chunks
            return retrieved_map

        concept_chunks = record_step(
            "retrieve_curriculum_evidence",
            self.capability.value,
            {
                "tool": "search_knowledge_chunks",
                "concept_count": len(selected_concepts),
            },
            _retrieve_all_concept_evidence,
        )

        # Step 6: Synthesize stages, lessons, citations, and evidence gaps
        def _synthesize_curriculum() -> CurriculumPlan:
            concept_to_lesson_id = {n.concept_id: n.lesson_id for n in selected_concepts}
            stages_grouped: Dict[str, List[ConceptNode]] = {}
            stage_meta: Dict[str, Tuple[int, str]] = {}
            for node in selected_concepts:
                stages_grouped.setdefault(node.stage_id, []).append(node)
                stage_meta[node.stage_id] = (node.stage_order, node.stage_title)

            all_citations_by_id: Dict[str, CurriculumCitation] = {}
            evidence_gaps: List[EvidenceGap] = []
            built_stages: List[CurriculumStage] = []
            ordered_stage_ids = sorted(stages_grouped.keys(), key=lambda sid: stage_meta[sid][0])
            valid_chunk_ids_global: Set[str] = set()
            for ch_list in concept_chunks.values():
                for ch in ch_list:
                    valid_chunk_ids_global.add(ch.chunk_id)

            for idx, stage_id in enumerate(ordered_stage_ids):
                stage_order, stage_title = stage_meta[stage_id]
                stage_nodes = sorted(stages_grouped[stage_id], key=lambda n: n.lesson_order)
                prereq_stage_ids = tuple(ordered_stage_ids[:idx])
                built_lessons: List[CurriculumLesson] = []

                for node in stage_nodes:
                    raw_chunks = concept_chunks.get(node.concept_id, [])
                    # Prefer chunks belonging to node.preferred_resource_ids, then top-ranked chunks
                    preferred_set = set(node.preferred_resource_ids)
                    matched_chunks = [c for c in raw_chunks if c.resource_id in preferred_set]
                    if not matched_chunks:
                        matched_chunks = raw_chunks[:2]
                    else:
                        matched_chunks = matched_chunks[:3]

                    lesson_citations: List[CurriculumCitation] = []
                    seen_cit_keys: Set[str] = set()

                    for ch in matched_chunks:
                        cit_id = f"cit_{ch.chunk_id}"
                        excerpt = " ".join(ch.chunk_text.strip().split())
                        if len(excerpt) > 200:
                            excerpt = excerpt[:197].rstrip() + "..."
                        cit = CurriculumCitation(
                            citation_id=cit_id,
                            evidence_tier=EvidenceTier.CURRICULUM_READY_EVIDENCE,
                            source_id=ch.source_id,
                            resource_id=ch.resource_id,
                            title=ch.title,
                            canonical_url=ch.canonical_url,
                            chunk_id=ch.chunk_id,
                            epistemic_layer=ch.epistemic_layer,
                            locator=f"chunk_index={ch.chunk_index} ({ch.char_start}..{ch.char_end})",
                            excerpt_summary=excerpt,
                            relevance_score=round(float(ch.similarity_score), 4),
                        )
                        lesson_citations.append(cit)
                        seen_cit_keys.add(cit_id)
                        all_citations_by_id[cit_id] = cit

                    # Also attach preferred SQLite resources (collected metadata or approved syllabus seeds)
                    lesson_gap_notes: List[str] = []
                    for pref_res_id in node.preferred_resource_ids:
                        res_info = resources_by_id.get(pref_res_id)
                        if not res_info:
                            continue
                        already_covered_by_chunk = any(
                            c.resource_id == pref_res_id for c in lesson_citations
                        )
                        if already_covered_by_chunk:
                            continue
                        tier_enum = EvidenceTier(res_info["evidence_tier"])
                        cit_id = f"cit_{pref_res_id}"
                        if cit_id not in seen_cit_keys:
                            cit = CurriculumCitation(
                                citation_id=cit_id,
                                evidence_tier=tier_enum,
                                source_id=res_info["source_id"],
                                resource_id=pref_res_id,
                                title=res_info["title"],
                                canonical_url=res_info["canonical_url"],
                                chunk_id=None,
                                epistemic_layer=(
                                    "VERIFIED_RESOURCE_METADATA"
                                    if res_info["last_verified_at"]
                                    else "APPROVED_SYLLABUS_SEED_METADATA"
                                ),
                                locator=f"resource_id={pref_res_id} ({res_info['text_storage_status']})",
                                excerpt_summary=(
                                    res_info["abstract_or_description"]
                                    or f"Approved catalog entry from {res_info['source_name']} ({res_info['text_storage_status']})."
                                ),
                                relevance_score=0.85 if res_info["last_verified_at"] else 0.65,
                            )
                            lesson_citations.append(cit)
                            seen_cit_keys.add(cit_id)
                            all_citations_by_id[cit_id] = cit

                        if tier_enum != EvidenceTier.CURRICULUM_READY_EVIDENCE:
                            lesson_gap_notes.append(
                                f"Resource '{res_info['title']}' ({pref_res_id}, {res_info['source_id']}) is currently at tier "
                                f"{tier_enum.value} ({res_info['text_storage_status']}); full-text chunks are not yet indexed."
                            )

                    if node.known_gap_description:
                        lesson_gap_notes.append(node.known_gap_description)
                        evidence_gaps.append(
                            EvidenceGap(
                                gap_id=f"gap_{node.lesson_id}",
                                scope="LESSON_LEVEL",
                                related_lesson_id=node.lesson_id,
                                related_source_ids=tuple(
                                    sorted(
                                        {
                                            resources_by_id[rid]["source_id"]
                                            for rid in node.preferred_resource_ids
                                            if rid in resources_by_id
                                        }
                                    )
                                ),
                                current_evidence_tier=(
                                    EvidenceTier.COLLECTED_DOCUMENT.value
                                    if any(
                                        resources_by_id.get(rid, {}).get("last_verified_at")
                                        for rid in node.preferred_resource_ids
                                    )
                                    else EvidenceTier.APPROVED_UNCOLLECTED_RESOURCE.value
                                ),
                                description=node.known_gap_description,
                                recommended_curation_action=node.known_gap_remediation
                                or "Collect and chunk full-text primary material for this lesson.",
                            )
                        )

                    # Ground explanation with verified chunk citations and sanitize via Task 033 sanitizer
                    chunk_cit_markers = [
                        f"[{c.chunk_id}]" for c in lesson_citations if c.chunk_id is not None
                    ]
                    grounded_snippet = ""
                    if matched_chunks:
                        top_chunk = matched_chunks[0]
                        snippet_clean = " ".join(top_chunk.chunk_text.strip().split())
                        if len(snippet_clean) > 240:
                            snippet_clean = snippet_clean[:237].rstrip() + "..."
                        grounded_snippet = (
                            f" Retrieved primary evidence ({top_chunk.title}, {top_chunk.source_id}): "
                            f"\"{snippet_clean}\" {' '.join(chunk_cit_markers)}."
                        )
                    raw_explanation = f"{node.explanation_template}{grounded_snippet}"
                    sanitized_explanation, _ = _sanitize_answer_citations(
                        raw_explanation,
                        {c.chunk_id for c in lesson_citations if c.chunk_id is not None},
                    )

                    ref_materials = tuple(
                        {
                            "citation_id": c.citation_id,
                            "title": c.title,
                            "source_id": c.source_id,
                            "resource_id": c.resource_id,
                            "chunk_id": c.chunk_id or "",
                            "canonical_url": c.canonical_url,
                            "evidence_tier": c.evidence_tier.value,
                        }
                        for c in lesson_citations
                    )

                    prereq_lesson_ids = tuple(
                        concept_to_lesson_id[p]
                        for p in node.prerequisites
                        if p in concept_to_lesson_id
                    )

                    built_lessons.append(
                        CurriculumLesson(
                            lesson_id=node.lesson_id,
                            concept_id=node.concept_id,
                            lesson_order=node.lesson_order,
                            title=node.title,
                            learning_objectives=node.learning_objectives,
                            prerequisites=node.prerequisites,
                            prerequisite_lesson_ids=prereq_lesson_ids,
                            explanation=sanitized_explanation,
                            reference_materials=ref_materials,
                            exercises=node.exercises,
                            projects=node.projects,
                            mastery_check=MasteryCheckDesign(
                                check_id=f"mc_{node.lesson_id}",
                                lesson_id=node.lesson_id,
                                assessment_status="DESIGN_ONLY_NOT_YET_ASSESSED",
                                disclaimer=MASTERY_ASSESSMENT_DISCLAIMER,
                                prompts=node.mastery_check_prompts,
                                pass_criteria=node.mastery_pass_criteria,
                            ),
                            source_citations=tuple(lesson_citations),
                            rationale_for_ordering=node.ordering_rationale,
                            evidence_gaps=tuple(lesson_gap_notes),
                        )
                    )

                stage_objectives = tuple(
                    obj for les in built_lessons for obj in les.learning_objectives[:2]
                )
                stage_rationale = (
                    f"{stage_title} is positioned at stage_order={stage_order} because all constituent lessons "
                    f"({', '.join(l.lesson_id for l in built_lessons)}) have their prerequisite concepts satisfied "
                    f"by earlier stages ({', '.join(prereq_stage_ids) if prereq_stage_ids else 'foundational entry point'})."
                )
                built_stages.append(
                    CurriculumStage(
                        stage_id=stage_id,
                        stage_order=stage_order,
                        title=stage_title,
                        stage_objectives=stage_objectives,
                        prerequisite_stage_ids=prereq_stage_ids,
                        rationale_for_ordering=stage_rationale,
                        lessons=tuple(built_lessons),
                    )
                )

            # Add ecosystem-wide gap entries for uncollected approved resources and SRC-THE-BATCH discovery status
            evidence_gaps.append(
                EvidenceGap(
                    gap_id="gap_ecosystem_uncollected_seeds",
                    scope="ECOSYSTEM_CATALOG",
                    related_lesson_id=None,
                    related_source_ids=("SRC-HINTON", "SRC-BENGIO", "SRC-ARXIV"),
                    current_evidence_tier=EvidenceTier.APPROVED_UNCOLLECTED_RESOURCE.value,
                    description=(
                        f"{ecosystem_summary.approved_uncollected_resource_count} approved seed resources remain at "
                        f"'metadata-only-not-yet-collected', and {ecosystem_summary.discovered_link_candidate_count} "
                        f"discovered outbound links remain in 'PENDING_REVIEW' (`DISCOVERED_LINK`)."
                    ),
                    recommended_curation_action=(
                        "Run policy-governed collection batches for approved seed resources and review high-priority "
                        "discovered link candidates via KnowledgeCuratorAgent."
                    ),
                )
            )
            evidence_gaps.append(
                EvidenceGap(
                    gap_id="gap_the_batch_discovery_governance",
                    scope="SOURCE_GOVERNANCE",
                    related_lesson_id=None,
                    related_source_ids=(THE_BATCH_SOURCE_ID,),
                    current_evidence_tier="CANDIDATE_PENDING_REVIEW",
                    description=(
                        f"{THE_BATCH_SOURCE_ID} ({THE_BATCH_CANONICAL_URL}) is registered with status='CANDIDATE_PENDING_REVIEW' "
                        "and permits_full_text_storage=0. It serves strictly as an AI news and research-discovery source; "
                        "no news items are cited as curriculum evidence or allowed to alter foundational prerequisite order."
                    ),
                    recommended_curation_action=(
                        "Use The Batch solely to identify newly published primary papers (e.g., on arXiv), then ingest "
                        "those primary papers through approved source connectors."
                    ),
                )
            )

            overall_rationale = (
                "The curriculum follows a strict topological sort over the concept prerequisite DAG: "
                "(1) Stage 1 establishes linear algebra, multivariable calculus, and reverse-mode backpropagation; "
                "(2) Stage 2 builds core generalization (Dropout regularization) and stochastic energy-based representation "
                "learning (Boltzmann machines & RBMs) atop gradient optimization; "
                "(3) Stage 3 scales depth via Deep Residual Learning (ResNet), resolves fixed-vector sequence bottlenecks via "
                "Bahdanau soft alignment, and synthesizes both into the recurrence-free Transformer architecture; "
                "(4) Stage 4 bridges engineering implementation to AI research methodology, ablation design, GFlowNets, and "
                "AI safety. Per platform governance, foundational prerequisites always precede recent architectures regardless "
                "of news recency in SRC-THE-BATCH."
            )

            draft_plan = CurriculumPlan(
                curriculum_id=curriculum_id,
                run_id=run_state.run_id,
                schema_version=CURRICULUM_SCHEMA_VERSION,
                status=CurriculumStatus.VALIDATED,
                created_at=now_iso,
                learner_goal=cleaned_goal,
                title="Foundational-to-Research AI Engineering & Research Curriculum",
                executive_summary=(
                    f"Prerequisite-ordered, citation-verified learning path across {len(built_stages)} stages and "
                    f"{sum(len(s.lessons) for s in built_stages)} lessons generated for goal: \"{cleaned_goal}\". "
                    f"Grounded in {ecosystem_summary.curriculum_ready_chunk_count} active knowledge chunks and "
                    f"{ecosystem_summary.collected_document_count} verified collected documents, with explicit reporting "
                    f"of {len(evidence_gaps)} evidence and curation gaps."
                ),
                rationale_for_ordering=overall_rationale,
                mastery_assessment_disclaimer=MASTERY_ASSESSMENT_DISCLAIMER,
                abstention_reason=None,
                source_ecosystem_summary=ecosystem_summary,
                stages=tuple(built_stages),
                source_citations=tuple(all_citations_by_id.values()),
                evidence_gaps=tuple(evidence_gaps),
                validation_report=CurriculumValidationReport(
                    is_valid=True,
                    schema_valid=True,
                    prerequisites_consistent=True,
                    citations_verified=True,
                    mastery_disclaimer_present=True,
                    the_batch_policy_respected=True,
                    checked_lesson_count=0,
                    checked_citation_count=0,
                    errors=(),
                ),
            )
            return draft_plan

        draft_plan: CurriculumPlan = record_step(
            "synthesize_curriculum_plan",
            self.capability.value,
            {"selected_concepts": [n.concept_id for n in selected_concepts]},
            _synthesize_curriculum,
        )

        # Step 7: Deterministic Curriculum Validation Gate
        validation_report: CurriculumValidationReport = record_step(
            "validate_curriculum_output",
            "CurriculumValidator",
            {"curriculum_id": draft_plan.curriculum_id},
            lambda: self.validator.validate(draft_plan),
        )
        if not validation_report.is_valid:
            raise PlatformValidationError(
                "CURRICULUM_VALIDATION_FAILED",
                f"Generated curriculum failed deterministic validation: {'; '.join(validation_report.errors)}",
                {"validation_report": validation_report.to_dict()},
            )

        return CurriculumPlan(
            **{
                **draft_plan.__dict__,
                "validation_report": validation_report,
            }
        )


# ---------------------------------------------------------------------------
# Task 035: Interactive Learning Models, Progress Persistence, and Agents
# ---------------------------------------------------------------------------


def _find_lesson_in_plan(plan: CurriculumPlan, lesson_id: str) -> Optional[CurriculumLesson]:
    """Locate a CurriculumLesson by lesson_id (also matching normalized whitespace)."""
    target = lesson_id.strip()
    target_norm = re.sub(r"\s+", "_", target)
    for stage in plan.stages:
        for lesson in stage.lessons:
            if (
                lesson.lesson_id == lesson_id
                or lesson.lesson_id.strip() == target
                or re.sub(r"\s+", "_", lesson.lesson_id.strip()) == target_norm
            ):
                return lesson
    return None


def _curriculum_plan_from_dict(data: Mapping[str, Any]) -> CurriculumPlan:
    """Reconstruct a CurriculumPlan dataclass from its serialized dictionary representation."""
    def _cit(c: Mapping[str, Any]) -> CurriculumCitation:
        return CurriculumCitation(
            citation_id=str(c["citation_id"]),
            evidence_tier=EvidenceTier(str(c["evidence_tier"])),
            source_id=str(c["source_id"]),
            resource_id=str(c["resource_id"]),
            title=str(c["title"]),
            canonical_url=str(c["canonical_url"]),
            chunk_id=c.get("chunk_id"),
            epistemic_layer=str(c.get("epistemic_layer", "VERIFIED_RESOURCE_METADATA")),
            locator=str(c.get("locator", "resource-metadata")),
            excerpt_summary=str(c.get("excerpt_summary", "")),
            relevance_score=float(c.get("relevance_score", 1.0)),
        )

    eco_raw = data["source_ecosystem_summary"]
    eco_summary = SourceEcosystemSummary(
        total_registered_sources=int(eco_raw["total_registered_sources"]),
        approved_operational_sources=int(eco_raw["approved_operational_sources"]),
        candidate_pending_sources=int(eco_raw["candidate_pending_sources"]),
        tier_counts=dict(eco_raw["tier_counts"]),
        curriculum_ready_chunk_count=int(eco_raw["curriculum_ready_chunk_count"]),
        curriculum_ready_resource_count=int(eco_raw["curriculum_ready_resource_count"]),
        extracted_resource_count=int(eco_raw["extracted_resource_count"]),
        collected_document_count=int(eco_raw["collected_document_count"]),
        approved_uncollected_resource_count=int(eco_raw["approved_uncollected_resource_count"]),
        discovered_link_candidate_count=int(eco_raw["discovered_link_candidate_count"]),
        the_batch_governance=dict(eco_raw["the_batch_governance"]),
    )

    stages: List[CurriculumStage] = []
    for s_raw in data.get("stages", []):
        lessons: List[CurriculumLesson] = []
        for l_raw in s_raw.get("lessons", []):
            mc_raw = l_raw["mastery_check"]
            mc = MasteryCheckDesign(
                check_id=str(mc_raw["check_id"]),
                lesson_id=str(mc_raw["lesson_id"]),
                assessment_status=str(mc_raw["assessment_status"]),
                disclaimer=str(mc_raw["disclaimer"]),
                prompts=tuple(str(p) for p in mc_raw.get("prompts", [])),
                pass_criteria=tuple(str(p) for p in mc_raw.get("pass_criteria", [])),
            )
            lessons.append(
                CurriculumLesson(
                    lesson_id=str(l_raw["lesson_id"]),
                    concept_id=str(l_raw["concept_id"]),
                    lesson_order=int(l_raw["lesson_order"]),
                    title=str(l_raw["title"]),
                    learning_objectives=tuple(str(x) for x in l_raw.get("learning_objectives", [])),
                    prerequisites=tuple(str(x) for x in l_raw.get("prerequisites", [])),
                    prerequisite_lesson_ids=tuple(str(x) for x in l_raw.get("prerequisite_lesson_ids", [])),
                    explanation=str(l_raw["explanation"]),
                    reference_materials=tuple(dict(m) for m in l_raw.get("reference_materials", [])),
                    exercises=tuple(dict(e) for e in l_raw.get("exercises", [])),
                    projects=tuple(dict(p) for p in l_raw.get("projects", [])),
                    mastery_check=mc,
                    source_citations=tuple(_cit(c) for c in l_raw.get("source_citations", [])),
                    rationale_for_ordering=str(l_raw["rationale_for_ordering"]),
                    evidence_gaps=tuple(str(g) for g in l_raw.get("evidence_gaps", [])),
                )
            )
        stages.append(
            CurriculumStage(
                stage_id=str(s_raw["stage_id"]),
                stage_order=int(s_raw["stage_order"]),
                title=str(s_raw["title"]),
                stage_objectives=tuple(str(x) for x in s_raw.get("stage_objectives", [])),
                prerequisite_stage_ids=tuple(str(x) for x in s_raw.get("prerequisite_stage_ids", [])),
                rationale_for_ordering=str(s_raw["rationale_for_ordering"]),
                lessons=tuple(lessons),
            )
        )

    gaps = tuple(
        EvidenceGap(
            gap_id=str(g["gap_id"]),
            scope=str(g["scope"]),
            related_lesson_id=g.get("related_lesson_id"),
            related_source_ids=tuple(str(s) for s in g.get("related_source_ids", [])),
            current_evidence_tier=str(g["current_evidence_tier"]),
            description=str(g["description"]),
            recommended_curation_action=str(g["recommended_curation_action"]),
        )
        for g in data.get("evidence_gaps", [])
    )

    vr_raw = data["validation_report"]
    val_report = CurriculumValidationReport(
        is_valid=bool(vr_raw["is_valid"]),
        schema_valid=bool(vr_raw["schema_valid"]),
        prerequisites_consistent=bool(vr_raw["prerequisites_consistent"]),
        citations_verified=bool(vr_raw["citations_verified"]),
        mastery_disclaimer_present=bool(vr_raw["mastery_disclaimer_present"]),
        the_batch_policy_respected=bool(vr_raw["the_batch_policy_respected"]),
        checked_lesson_count=int(vr_raw["checked_lesson_count"]),
        checked_citation_count=int(vr_raw["checked_citation_count"]),
        errors=tuple(str(e) for e in vr_raw.get("errors", [])),
    )

    return CurriculumPlan(
        curriculum_id=str(data["curriculum_id"]),
        run_id=str(data["run_id"]),
        schema_version=str(data.get("schema_version", CURRICULUM_SCHEMA_VERSION)),
        status=CurriculumStatus(str(data["status"])),
        created_at=str(data["created_at"]),
        learner_goal=str(data["learner_goal"]),
        title=str(data["title"]),
        executive_summary=str(data["executive_summary"]),
        rationale_for_ordering=str(data["rationale_for_ordering"]),
        mastery_assessment_disclaimer=str(data["mastery_assessment_disclaimer"]),
        abstention_reason=data.get("abstention_reason"),
        source_ecosystem_summary=eco_summary,
        stages=tuple(stages),
        source_citations=tuple(_cit(c) for c in data.get("source_citations", [])),
        evidence_gaps=gaps,
        validation_report=val_report,
    )


@dataclass(frozen=True)
class PrerequisiteExplanation:
    """Prerequisite concept bridge generated by the Teaching Agent."""

    concept_id: str
    lesson_id: str
    title: str
    why_needed: str
    intuitive_summary: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeachingRequest:
    """Structured input contract for the Teaching Agent."""

    curriculum_id: str
    lesson_id: str
    learner_question: str
    explanation_depth: ExplanationDepth = ExplanationDepth.INTERMEDIATE
    learner_id: str = DEFAULT_LEARNER_ID

    def to_dict(self) -> Dict[str, Any]:
        return {
            "curriculum_id": self.curriculum_id,
            "lesson_id": self.lesson_id,
            "learner_question": self.learner_question,
            "explanation_depth": self.explanation_depth.value,
            "learner_id": self.learner_id,
        }


@dataclass(frozen=True)
class TeachingResponse:
    """Structured output contract for the Teaching Agent."""

    interaction_id: str
    run_id: str
    curriculum_id: str
    lesson_id: str
    concept_id: str
    lesson_title: str
    learner_id: str
    learner_question: str
    explanation_depth: ExplanationDepth
    depth_disclaimer: str
    status: str  # "GROUNDED_EXPLANATION" | "PEDAGOGICAL_SYNTHESIS_WITH_METADATA" | "INSUFFICIENT_EVIDENCE"
    core_explanation: str
    prerequisite_explanations: Tuple[PrerequisiteExplanation, ...]
    examples_and_analogies: Tuple[str, ...]
    socratic_questions: Tuple[str, ...]
    uncertainty_and_limits: str
    source_citations: Tuple[CurriculumCitation, ...]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "run_id": self.run_id,
            "curriculum_id": self.curriculum_id,
            "lesson_id": self.lesson_id,
            "concept_id": self.concept_id,
            "lesson_title": self.lesson_title,
            "learner_id": self.learner_id,
            "learner_question": self.learner_question,
            "explanation_depth": self.explanation_depth.value,
            "depth_disclaimer": self.depth_disclaimer,
            "status": self.status,
            "core_explanation": self.core_explanation,
            "prerequisite_explanations": [p.to_dict() for p in self.prerequisite_explanations],
            "examples_and_analogies": list(self.examples_and_analogies),
            "socratic_questions": list(self.socratic_questions),
            "uncertainty_and_limits": self.uncertainty_and_limits,
            "source_citations": [c.to_dict() for c in self.source_citations],
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RubricCriterion:
    """Explicit deterministic grading criterion for an AssessmentExercise."""

    criterion_id: str
    description: str
    target_objective: str
    required_concepts: Tuple[str, ...]
    weight: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "description": self.description,
            "target_objective": self.target_objective,
            "required_concepts": list(self.required_concepts),
            "weight": self.weight,
        }


@dataclass(frozen=True)
class MisconceptionPattern:
    """Deterministic detector for common conceptual or mathematical misconceptions."""

    pattern_id: str
    trigger_phrases: Tuple[str, ...]
    explanation: str
    remediation_objective: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "trigger_phrases": list(self.trigger_phrases),
            "explanation": self.explanation,
            "remediation_objective": self.remediation_objective,
        }


@dataclass(frozen=True)
class AssessmentExercise:
    """Structured exercise generated from lesson objectives with an explicit rubric."""

    exercise_id: str
    curriculum_id: str
    lesson_id: str
    concept_id: str
    category: AssessmentQuestionCategory
    title: str
    prompt: str
    targeted_objectives: Tuple[str, ...]
    starter_hint: str
    rubric: Tuple[RubricCriterion, ...]
    misconception_patterns: Tuple[MisconceptionPattern, ...]
    alias_exercise_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exercise_id": self.exercise_id,
            "curriculum_id": self.curriculum_id,
            "lesson_id": self.lesson_id,
            "concept_id": self.concept_id,
            "category": self.category.value,
            "title": self.title,
            "prompt": self.prompt,
            "targeted_objectives": list(self.targeted_objectives),
            "starter_hint": self.starter_hint,
            "rubric": [r.to_dict() for r in self.rubric],
            "misconception_patterns": [m.to_dict() for m in self.misconception_patterns],
            "alias_exercise_ids": list(self.alias_exercise_ids),
        }


@dataclass(frozen=True)
class CriterionEvaluation:
    """Deterministic grading outcome for a single RubricCriterion."""

    criterion_id: str
    description: str
    target_objective: str
    passed: bool
    score_awarded: float
    max_weight: float
    matched_concepts: Tuple[str, ...]
    missing_concepts: Tuple[str, ...]
    feedback: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "description": self.description,
            "target_objective": self.target_objective,
            "passed": self.passed,
            "score_awarded": self.score_awarded,
            "max_weight": self.max_weight,
            "matched_concepts": list(self.matched_concepts),
            "missing_concepts": list(self.missing_concepts),
            "feedback": self.feedback,
        }


@dataclass(frozen=True)
class AssessmentEvaluation:
    """Structured assessment result returned by AssessmentAgent."""

    attempt_id: str
    run_id: str
    learner_id: str
    curriculum_id: str
    lesson_id: str
    concept_id: str
    exercise_id: str
    category: AssessmentQuestionCategory
    submitted_answer: str
    score: float
    score_band: str  # "DEMONSTRATED" | "PARTIAL" | "NEEDS_PRACTICE"
    passed: bool
    complete_lesson_mastery_claimed: bool
    mastery_disclaimer: str
    evaluation_mode: str  # "DETERMINISTIC_RUBRIC_ONLY" | "DETERMINISTIC_PLUS_OPTIONAL_LLM"
    deterministic_feedback: str
    optional_llm_feedback: Optional[str]
    criterion_evaluations: Tuple[CriterionEvaluation, ...]
    detected_misconceptions: Tuple[Dict[str, str], ...]
    demonstrated_objectives: Tuple[str, ...]
    objectives_needing_practice: Tuple[str, ...]
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "learner_id": self.learner_id,
            "curriculum_id": self.curriculum_id,
            "lesson_id": self.lesson_id,
            "concept_id": self.concept_id,
            "exercise_id": self.exercise_id,
            "category": self.category.value,
            "submitted_answer": self.submitted_answer,
            "score": self.score,
            "score_band": self.score_band,
            "passed": self.passed,
            "complete_lesson_mastery_claimed": self.complete_lesson_mastery_claimed,
            "mastery_disclaimer": self.mastery_disclaimer,
            "evaluation_mode": self.evaluation_mode,
            "deterministic_feedback": self.deterministic_feedback,
            "optional_llm_feedback": self.optional_llm_feedback,
            "criterion_evaluations": [c.to_dict() for c in self.criterion_evaluations],
            "detected_misconceptions": [dict(m) for m in self.detected_misconceptions],
            "demonstrated_objectives": list(self.demonstrated_objectives),
            "objectives_needing_practice": list(self.objectives_needing_practice),
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class LearnerLessonProgress:
    """Persistent progress record for a learner on a single lesson."""

    learner_id: str
    curriculum_id: str
    lesson_id: str
    concept_id: str
    lesson_order: int
    title: str
    status: LessonProgressStatus
    prerequisites_met: bool
    unmet_prerequisite_lesson_ids: Tuple[str, ...]
    teaching_questions_count: int
    assessment_attempts_count: int
    best_score: float
    latest_score: Optional[float]
    completed_exercise_ids: Tuple[str, ...]
    total_exercises_count: int
    demonstrated_objectives: Tuple[str, ...]
    total_objectives_count: int
    knowledge_gaps: Tuple[str, ...]
    last_activity_at: Optional[str]
    recent_attempts: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "learner_id": self.learner_id,
            "curriculum_id": self.curriculum_id,
            "lesson_id": self.lesson_id,
            "concept_id": self.concept_id,
            "lesson_order": self.lesson_order,
            "title": self.title,
            "status": self.status.value,
            "prerequisites_met": self.prerequisites_met,
            "unmet_prerequisite_lesson_ids": list(self.unmet_prerequisite_lesson_ids),
            "teaching_questions_count": self.teaching_questions_count,
            "assessment_attempts_count": self.assessment_attempts_count,
            "best_score": self.best_score,
            "latest_score": self.latest_score,
            "completed_exercise_ids": list(self.completed_exercise_ids),
            "total_exercises_count": self.total_exercises_count,
            "demonstrated_objectives": list(self.demonstrated_objectives),
            "total_objectives_count": self.total_objectives_count,
            "knowledge_gaps": list(self.knowledge_gaps),
            "last_activity_at": self.last_activity_at,
            "recent_attempts": [dict(a) for a in self.recent_attempts],
        }


@dataclass(frozen=True)
class NextLessonRecommendation:
    """Prerequisite-aware next lesson recommendation computed by LearningProgressAgent."""

    learner_id: str
    curriculum_id: str
    recommendation_type: str  # "CONTINUE_ACTIVE_LESSON" | "REMEDIATE_KNOWLEDGE_GAPS" | "START_NEXT_UNLOCKED_LESSON" | "COMPLETE_PREREQUISITE_FIRST" | "CURRICULUM_COMPLETED"
    recommended_lesson_id: Optional[str]
    recommended_concept_id: Optional[str]
    recommended_lesson_title: Optional[str]
    rationale: str
    unmet_prerequisite_lesson_ids: Tuple[str, ...]
    focus_objectives: Tuple[str, ...]
    recommended_action: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "learner_id": self.learner_id,
            "curriculum_id": self.curriculum_id,
            "recommendation_type": self.recommendation_type,
            "recommended_lesson_id": self.recommended_lesson_id,
            "recommended_concept_id": self.recommended_concept_id,
            "recommended_lesson_title": self.recommended_lesson_title,
            "rationale": self.rationale,
            "unmet_prerequisite_lesson_ids": list(self.unmet_prerequisite_lesson_ids),
            "focus_objectives": list(self.focus_objectives),
            "recommended_action": self.recommended_action,
        }


@dataclass(frozen=True)
class CurriculumProgressSummary:
    """Aggregate persistent learning progress across an entire curriculum."""

    learner_id: str
    curriculum_id: str
    curriculum_title: str
    total_lessons: int
    not_started_count: int
    in_progress_count: int
    practicing_count: int
    completed_count: int
    overall_completion_ratio: float
    total_demonstrated_objectives: int
    total_curriculum_objectives: int
    active_knowledge_gaps: Tuple[str, ...]
    lesson_progress: Tuple[LearnerLessonProgress, ...]
    next_recommendation: NextLessonRecommendation
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "learner_id": self.learner_id,
            "curriculum_id": self.curriculum_id,
            "curriculum_title": self.curriculum_title,
            "total_lessons": self.total_lessons,
            "not_started_count": self.not_started_count,
            "in_progress_count": self.in_progress_count,
            "practicing_count": self.practicing_count,
            "completed_count": self.completed_count,
            "overall_completion_ratio": self.overall_completion_ratio,
            "total_demonstrated_objectives": self.total_demonstrated_objectives,
            "total_curriculum_objectives": self.total_curriculum_objectives,
            "active_knowledge_gaps": list(self.active_knowledge_gaps),
            "lesson_progress": [lp.to_dict() for lp in self.lesson_progress],
            "next_recommendation": self.next_recommendation.to_dict(),
            "updated_at": self.updated_at,
        }


LEARNING_PROGRESS_LATEST_SCHEMA_VERSION = 2

LEARNING_PROGRESS_MIGRATIONS: Tuple[Tuple[int, str, str], ...] = (
    (
        1,
        "0001_learning_progress_core",
        """
        CREATE TABLE IF NOT EXISTS persisted_curricula (
            curriculum_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            learner_goal TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            plan_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS learner_lesson_progress (
            learner_id TEXT NOT NULL,
            curriculum_id TEXT NOT NULL,
            lesson_id TEXT NOT NULL,
            concept_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('NOT_STARTED', 'IN_PROGRESS', 'PRACTICING', 'COMPLETED')),
            teaching_questions_count INTEGER NOT NULL DEFAULT 0,
            assessment_attempts_count INTEGER NOT NULL DEFAULT 0,
            best_score REAL NOT NULL DEFAULT 0.0,
            latest_score REAL,
            demonstrated_objectives_json TEXT NOT NULL DEFAULT '[]',
            knowledge_gaps_json TEXT NOT NULL DEFAULT '[]',
            completed_exercise_ids_json TEXT NOT NULL DEFAULT '[]',
            last_activity_at TEXT NOT NULL,
            PRIMARY KEY (learner_id, curriculum_id, lesson_id)
        );

        CREATE INDEX IF NOT EXISTS idx_learner_progress_lookup
            ON learner_lesson_progress(learner_id, curriculum_id);
        """,
    ),
    (
        2,
        "0002_teaching_and_assessment_audit",
        """
        CREATE TABLE IF NOT EXISTS learner_assessment_attempts (
            attempt_id TEXT PRIMARY KEY,
            learner_id TEXT NOT NULL,
            curriculum_id TEXT NOT NULL,
            lesson_id TEXT NOT NULL,
            exercise_id TEXT NOT NULL,
            category TEXT NOT NULL,
            submitted_answer TEXT NOT NULL,
            score REAL NOT NULL,
            score_band TEXT NOT NULL,
            passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
            rubric_feedback_json TEXT NOT NULL,
            misconceptions_json TEXT NOT NULL,
            demonstrated_objectives_json TEXT NOT NULL,
            objectives_needing_practice_json TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_assessment_attempts_lookup
            ON learner_assessment_attempts(learner_id, curriculum_id, lesson_id, attempted_at DESC);

        CREATE TABLE IF NOT EXISTS learner_teaching_interactions (
            interaction_id TEXT PRIMARY KEY,
            learner_id TEXT NOT NULL,
            curriculum_id TEXT NOT NULL,
            lesson_id TEXT NOT NULL,
            learner_question TEXT NOT NULL,
            explanation_depth TEXT NOT NULL,
            response_status TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_teaching_interactions_lookup
            ON learner_teaching_interactions(learner_id, curriculum_id, lesson_id, created_at DESC);
        """,
    ),
)


class LearningProgressStore:
    """SQLite persistence layer for curricula, learner progress, teaching sessions, and assessments.

    Uses a forward-only migration system (`learning_schema_migrations`) with SHA-256 checksum
    verification while keeping the primary knowledge store (`task012_pilot.sqlite`) strictly read-only.
    """

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self._temp_dir_path: Optional[Path] = None
        if db_path is None:
            temp_dir_str = tempfile.mkdtemp(prefix="ai_learning_progress_")
            self._temp_dir_path = Path(temp_dir_str)
            atexit.register(shutil.rmtree, temp_dir_str, True)
            self.db_path = (self._temp_dir_path / "learning_progress.sqlite").resolve()
        else:
            self.db_path = Path(db_path).resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.apply_migrations()

    def close(self) -> None:
        if self._temp_dir_path is not None and self._temp_dir_path.exists():
            shutil.rmtree(str(self._temp_dir_path), ignore_errors=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    @staticmethod
    def _checksum(sql: str) -> str:
        normalized = "\n".join(line.strip() for line in sql.strip().splitlines() if line.strip())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def apply_migrations(self) -> List[Dict[str, Any]]:
        """Apply forward-only migrations and verify checksums of already-applied migrations."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS learning_schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );
                    """
                )
                existing_rows = conn.execute(
                    "SELECT version, name, checksum, applied_at FROM learning_schema_migrations ORDER BY version"
                ).fetchall()
                existing_by_version = {int(r["version"]): dict(r) for r in existing_rows}

                for version, name, sql in LEARNING_PROGRESS_MIGRATIONS:
                    expected_checksum = self._checksum(sql)
                    if version in existing_by_version:
                        recorded = existing_by_version[version]
                        if recorded["checksum"] != expected_checksum or recorded["name"] != name:
                            raise PlatformValidationError(
                                "MIGRATION_CHECKSUM_MISMATCH",
                                f"Learning progress migration v{version} ({name}) checksum mismatch.",
                                {"version": version, "name": name},
                            )
                        continue
                    conn.executescript(sql)
                    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "INSERT INTO learning_schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (version, name, expected_checksum, now_iso),
                    )
            return self.list_migrations()
        finally:
            conn.close()

    def list_migrations(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT version, name, checksum, applied_at FROM learning_schema_migrations ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def save_curriculum(self, plan: CurriculumPlan) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO persisted_curricula (
                        curriculum_id, run_id, status, learner_goal, title, created_at, plan_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(curriculum_id) DO UPDATE SET
                        run_id = excluded.run_id,
                        status = excluded.status,
                        learner_goal = excluded.learner_goal,
                        title = excluded.title,
                        plan_json = excluded.plan_json
                    """,
                    (
                        plan.curriculum_id,
                        plan.run_id,
                        plan.status.value,
                        plan.learner_goal,
                        plan.title,
                        plan.created_at,
                        json.dumps(plan.to_dict(), ensure_ascii=False),
                    ),
                )
        finally:
            conn.close()

    def load_curriculum(self, curriculum_id: str) -> Optional[CurriculumPlan]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT plan_json FROM persisted_curricula WHERE curriculum_id = ?",
                (curriculum_id,),
            ).fetchone()
            if row is None:
                return None
            return _curriculum_plan_from_dict(json.loads(str(row["plan_json"])))
        finally:
            conn.close()

    def list_persisted_curricula(self) -> List[CurriculumPlan]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT plan_json FROM persisted_curricula ORDER BY created_at ASC, curriculum_id ASC"
            ).fetchall()
            return [_curriculum_plan_from_dict(json.loads(str(r["plan_json"]))) for r in rows]
        finally:
            conn.close()

    def get_lesson_row(
        self,
        *,
        learner_id: str,
        curriculum_id: str,
        lesson_id: str,
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM learner_lesson_progress
                WHERE learner_id = ? AND curriculum_id = ? AND lesson_id = ?
                """,
                (learner_id, curriculum_id, lesson_id),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def upsert_lesson_progress(
        self,
        *,
        learner_id: str,
        curriculum_id: str,
        lesson_id: str,
        concept_id: str,
        status: LessonProgressStatus,
        teaching_questions_count: int,
        assessment_attempts_count: int,
        best_score: float,
        latest_score: Optional[float],
        demonstrated_objectives: Sequence[str],
        knowledge_gaps: Sequence[str],
        completed_exercise_ids: Sequence[str],
        last_activity_at: str,
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO learner_lesson_progress (
                        learner_id, curriculum_id, lesson_id, concept_id, status,
                        teaching_questions_count, assessment_attempts_count,
                        best_score, latest_score,
                        demonstrated_objectives_json, knowledge_gaps_json,
                        completed_exercise_ids_json, last_activity_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(learner_id, curriculum_id, lesson_id) DO UPDATE SET
                        concept_id = excluded.concept_id,
                        status = excluded.status,
                        teaching_questions_count = excluded.teaching_questions_count,
                        assessment_attempts_count = excluded.assessment_attempts_count,
                        best_score = excluded.best_score,
                        latest_score = excluded.latest_score,
                        demonstrated_objectives_json = excluded.demonstrated_objectives_json,
                        knowledge_gaps_json = excluded.knowledge_gaps_json,
                        completed_exercise_ids_json = excluded.completed_exercise_ids_json,
                        last_activity_at = excluded.last_activity_at
                    """,
                    (
                        learner_id,
                        curriculum_id,
                        lesson_id,
                        concept_id,
                        status.value,
                        teaching_questions_count,
                        assessment_attempts_count,
                        round(float(best_score), 4),
                        round(float(latest_score), 4) if latest_score is not None else None,
                        json.dumps(list(demonstrated_objectives), ensure_ascii=False),
                        json.dumps(list(knowledge_gaps), ensure_ascii=False),
                        json.dumps(list(completed_exercise_ids), ensure_ascii=False),
                        last_activity_at,
                    ),
                )
        finally:
            conn.close()

    def record_teaching_interaction(self, response: TeachingResponse) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO learner_teaching_interactions (
                        interaction_id, learner_id, curriculum_id, lesson_id,
                        learner_question, explanation_depth, response_status,
                        response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        response.interaction_id,
                        response.learner_id,
                        response.curriculum_id,
                        response.lesson_id,
                        response.learner_question,
                        response.explanation_depth.value,
                        response.status,
                        json.dumps(response.to_dict(), ensure_ascii=False),
                        response.created_at,
                    ),
                )
        finally:
            conn.close()

    def list_teaching_interactions(
        self,
        *,
        learner_id: str,
        curriculum_id: str,
        lesson_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if lesson_id is not None:
                rows = conn.execute(
                    """
                    SELECT response_json FROM learner_teaching_interactions
                    WHERE learner_id = ? AND curriculum_id = ? AND lesson_id = ?
                    ORDER BY created_at ASC
                    """,
                    (learner_id, curriculum_id, lesson_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT response_json FROM learner_teaching_interactions
                    WHERE learner_id = ? AND curriculum_id = ?
                    ORDER BY created_at ASC
                    """,
                    (learner_id, curriculum_id),
                ).fetchall()
            return [json.loads(str(r["response_json"])) for r in rows]
        finally:
            conn.close()

    def record_assessment_attempt(self, evaluation: AssessmentEvaluation) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO learner_assessment_attempts (
                        attempt_id, learner_id, curriculum_id, lesson_id, exercise_id,
                        category, submitted_answer, score, score_band, passed,
                        rubric_feedback_json, misconceptions_json,
                        demonstrated_objectives_json, objectives_needing_practice_json,
                        attempted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation.attempt_id,
                        evaluation.learner_id,
                        evaluation.curriculum_id,
                        evaluation.lesson_id,
                        evaluation.exercise_id,
                        evaluation.category.value,
                        evaluation.submitted_answer,
                        evaluation.score,
                        evaluation.score_band,
                        1 if evaluation.passed else 0,
                        json.dumps([c.to_dict() for c in evaluation.criterion_evaluations], ensure_ascii=False),
                        json.dumps(list(evaluation.detected_misconceptions), ensure_ascii=False),
                        json.dumps(list(evaluation.demonstrated_objectives), ensure_ascii=False),
                        json.dumps(list(evaluation.objectives_needing_practice), ensure_ascii=False),
                        evaluation.evaluated_at,
                    ),
                )
        finally:
            conn.close()

    def list_assessment_attempts(
        self,
        *,
        learner_id: str,
        curriculum_id: str,
        lesson_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if lesson_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM learner_assessment_attempts
                    WHERE learner_id = ? AND curriculum_id = ? AND lesson_id = ?
                    ORDER BY attempted_at DESC
                    """,
                    (learner_id, curriculum_id, lesson_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM learner_assessment_attempts
                    WHERE learner_id = ? AND curriculum_id = ?
                    ORDER BY attempted_at DESC
                    """,
                    (learner_id, curriculum_id),
                ).fetchall()
            results: List[Dict[str, Any]] = []
            for r in rows:
                results.append(
                    {
                        "attempt_id": r["attempt_id"],
                        "learner_id": r["learner_id"],
                        "curriculum_id": r["curriculum_id"],
                        "lesson_id": r["lesson_id"],
                        "exercise_id": r["exercise_id"],
                        "category": r["category"],
                        "submitted_answer": r["submitted_answer"],
                        "score": float(r["score"]),
                        "score_band": r["score_band"],
                        "passed": bool(r["passed"]),
                        "criterion_evaluations": json.loads(str(r["rubric_feedback_json"])),
                        "detected_misconceptions": json.loads(str(r["misconceptions_json"])),
                        "demonstrated_objectives": json.loads(str(r["demonstrated_objectives_json"])),
                        "objectives_needing_practice": json.loads(str(r["objectives_needing_practice_json"])),
                        "attempted_at": r["attempted_at"],
                    }
                )
            return results
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Teaching Agent
# ---------------------------------------------------------------------------


_CONCEPT_EXAMPLES_AND_ANALOGIES: Dict[str, Tuple[str, str]] = {
    "math_linear_algebra_calculus": (
        "Analogy (Geometric Transformations & Altimeter): Think of a weight matrix W as a lens that rotates and stretches input coordinates x into a new feature space, while the gradient nabla_theta L is a 3D compass needle on a mountain terrain pointing uphill—stepping in -nabla_theta L walks down into the valley of minimum loss.",
        "Concrete Example: For a 2-dim input x = [1.0, 2.0]^T and weight matrix W, the Jacobian of f(W, x, b) = tanh(Wx + b) scales upstream sensitivities by (1 - tanh^2(Wx + b)) and matches finite-difference approximations within 1e-6.",
    ),
    "backpropagation_and_gradient_descent": (
        "Analogy (Assembly-Line Credit Assignment): Imagine a multi-stage manufacturing pipeline where a final quality defect E is measured at the output; backpropagation works backward station-by-station using the chain rule so each hidden station receives its exact responsibility delta_j for the final error.",
        "Concrete Example: In the classic XOR problem ([0,0]->0, [0,1]->1, [1,0]->1, [1,1]->0), a single linear layer cannot separate the classes, but a 2-unit hidden layer trained via delta_j = (dE/do_j) f'(net_j) learns one hidden feature for OR and one for NAND, making XOR linearly separable.",
    ),
    "regularization_and_dropout": (
        "Analogy (Cross-Trained Engineering Team): If an engineering team always relies on one specific pair of specialists working together, the team fails when one is absent (co-adaptation). Dropout randomly benches half the units on every training step (p = 0.5), forcing every hidden unit to become independently useful with random teammates.",
        "Concrete Example: With inverted dropout at drop rate p = 0.5, an active hidden activation h_i = 0.8 is scaled by 1/(1 - 0.5) = 2.0 to 1.6 during training so its expected value E[h_i] = 0.5 * 1.6 + 0.5 * 0 = 0.8 matches evaluation mode with no test-time sampling needed.",
    ),
    "energy_based_and_boltzmann_models": (
        "Analogy (Magnetic Spin Crystallization): Like physical atoms settling into low-energy magnetic alignments as metal cools, a Boltzmann machine assigns lower energy E(s) to plausible data patterns and uses thermal noise T to escape shallow local traps.",
        "Concrete Example: In a Restricted Boltzmann Machine (RBM) with 3 visible binary pixels v and 2 hidden feature detectors h, removing hidden-to-hidden edges means P(h_1=1, h_2=1 | v) factors cleanly into P(h_1=1 | v) * P(h_2=1 | v), turning an exponential sum into parallel sigmoid evaluations.",
    ),
    "deep_residual_networks": (
        "Analogy (Delta Editing vs. Rewriting from Scratch): Asking a stack of nonlinear layers to learn H(x) = x from scratch is like retyping an entire manuscript just to fix one sentence; a residual block y = x + F(x) passes the original manuscript x along an identity highway so the layers only learn the diff F(x) = H(x) - x.",
        "Concrete Example: If a deep layer is already near-optimal, driving residual weights toward zero sets F(x) = 0 and preserves y = x exactly, while the backward Jacobian dy/dx = I + dF/dx guarantees the identity term I carries gradients back to layer 1 without vanishing.",
    ),
    "sequence_alignment_and_bahdanau_attention": (
        "Analogy (Open-Book Translation vs. Closed-Book Memorization): A fixed-vector encoder-decoder forces a translator to read a 50-word paragraph once, close the book, and translate from memory; Bahdanau attention keeps the source sentence open so the decoder points a soft spotlight alpha_{ij} at the relevant source words for each target word.",
        "Concrete Example: When translating 'The agreement on the European Economic Area' into French ('L'accord sur la zone économique européenne'), decoding 'européenne' assigns high alignment weight alpha_{i,j} (e.g. 0.78) to 'European' and near-zero weight to 'agreement', forming context vector c_i = sum_j alpha_{ij} h_j.",
    ),
    "transformer_self_attention_architecture": (
        "Analogy (Simultaneous Round-Table Broadcast): Whereas an RNN passes a message sequentially down a whisper line one token at a time (O(n) steps), Transformer Multi-Head Self-Attention lets every token broadcast a Query to all other tokens' Keys simultaneously in O(1) sequential steps.",
        "Concrete Example: For query/key dimension d_k = 64, unscaled dot products Q K^T have variance 64, pushing softmax into flat saturation where gradients vanish; dividing by sqrt(64) = 8 restores unit variance before multiplying by Values V.",
    ),
    "ai_research_methodology_and_safety_frontiers": (
        "Analogy (Watershed River Network vs. Single Fastest Canal): Standard reward-maximizing RL builds a single canal to the highest-reward peak, whereas a GFlowNet routes probability flow through a branching river delta so terminal states x receive water proportional to their reward R(x), discovering diverse scientific candidates.",
        "Concrete Example: In an ablation study of a new attention variant, holding parameter count, optimizer schedule, and random seeds fixed while toggling only the positional encoding isolates whether accuracy gains come from the mechanism itself or extra capacity.",
    ),
}


def validate_teaching_request(
    *,
    curriculum_id: Any,
    lesson_id: Any,
    learner_question: Any,
    explanation_depth: Any = "intermediate",
    learner_id: Any = DEFAULT_LEARNER_ID,
) -> TeachingRequest:
    """Deterministically validate inputs for the Teaching Agent."""
    if not isinstance(curriculum_id, str) or not curriculum_id.strip():
        raise PlatformValidationError("INVALID_CURRICULUM_ID", "curriculum_id must be a non-empty string.")
    if not isinstance(lesson_id, str) or not lesson_id.strip():
        raise PlatformValidationError("INVALID_LESSON_ID", "lesson_id must be a non-empty string.")
    if not isinstance(learner_question, str):
        raise PlatformValidationError("INVALID_QUESTION_TYPE", "learner_question must be a string.")
    cleaned_q = " ".join(learner_question.strip().split())
    if len(cleaned_q) < 4:
        raise PlatformValidationError(
            "QUESTION_TOO_SHORT",
            "learner_question must be at least 4 characters long.",
            {"min_length": 4, "actual_length": len(cleaned_q)},
        )
    if len(cleaned_q) > 2000:
        raise PlatformValidationError(
            "QUESTION_TOO_LONG",
            "learner_question must not exceed 2000 characters.",
            {"max_length": 2000, "actual_length": len(cleaned_q)},
        )
    depth_str = str(explanation_depth or "intermediate").strip().lower()
    try:
        depth_enum = ExplanationDepth(depth_str)
    except ValueError as exc:
        valid_depths = [d.value for d in ExplanationDepth]
        raise PlatformValidationError(
            "INVALID_EXPLANATION_DEPTH",
            f"explanation_depth must be one of {valid_depths}, got '{explanation_depth}'.",
            {"valid_explanation_depths": valid_depths},
        ) from exc
    cleaned_learner = str(learner_id or DEFAULT_LEARNER_ID).strip() or DEFAULT_LEARNER_ID
    return TeachingRequest(
        curriculum_id=curriculum_id.strip(),
        lesson_id=lesson_id.strip(),
        learner_question=cleaned_q,
        explanation_depth=depth_enum,
        learner_id=cleaned_learner,
    )


class TeachingAgent:
    """Interactive Teaching Agent delivering evidence-grounded, depth-adapted explanations."""

    capability = AgentCapability.TEACHING

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        ecosystem_service: SourceEcosystemService,
        concept_graph: Optional[ConceptGraph] = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.ecosystem_service = ecosystem_service
        self.concept_graph = concept_graph or build_default_concept_graph()

    def _build_prerequisite_bridges(
        self,
        plan: CurriculumPlan,
        lesson: CurriculumLesson,
    ) -> Tuple[PrerequisiteExplanation, ...]:
        bridges: List[PrerequisiteExplanation] = []
        for prereq_cid in lesson.prerequisites:
            node = self.concept_graph.get_node(prereq_cid)
            prereq_lesson: Optional[CurriculumLesson] = None
            for stage in plan.stages:
                for cand in stage.lessons:
                    if cand.concept_id == prereq_cid:
                        prereq_lesson = cand
                        break
            title = prereq_lesson.title if prereq_lesson else node.title
            lid = prereq_lesson.lesson_id if prereq_lesson else node.lesson_id
            bridges.append(
                PrerequisiteExplanation(
                    concept_id=prereq_cid,
                    lesson_id=lid,
                    title=title,
                    why_needed=(
                        f"`{lesson.title}` builds directly on `{title}` (`{prereq_cid}`). "
                        f"Rationale: {lesson.rationale_for_ordering}"
                    ),
                    intuitive_summary=node.explanation_template,
                )
            )
        return tuple(bridges)

    def _format_depth_framing(
        self,
        depth: ExplanationDepth,
        lesson: CurriculumLesson,
        learner_question: str,
        grounded_snippets: List[str],
    ) -> str:
        depth_intros: Dict[ExplanationDepth, str] = {
            ExplanationDepth.FOUNDATIONAL_INTUITION: (
                f"**[Foundational Intuition View]** Before diving into heavy notation, let's build the core mental model for "
                f"**{lesson.title}** in response to your question (*\"{learner_question}\"*): "
                f"{lesson.explanation}"
            ),
            ExplanationDepth.BEGINNER: (
                f"**[Beginner Step-by-Step View]** Let's break **{lesson.title}** down step by step for your question "
                f"(*\"{learner_question}\"*). The primary goal is to understand: {lesson.learning_objectives[0]} "
                f"{lesson.explanation}"
            ),
            ExplanationDepth.INTERMEDIATE: (
                f"**[Intermediate Technical View]** Connecting the theory and implementation of **{lesson.title}** "
                f"for your question (*\"{learner_question}\"*): {lesson.explanation} "
                f"Key mechanism to master: {lesson.learning_objectives[min(1, len(lesson.learning_objectives) - 1)]}"
            ),
            ExplanationDepth.ADVANCED: (
                f"**[Advanced Mathematical & Architectural View]** Analyzing the formal structure and optimization dynamics "
                f"of **{lesson.title}** regarding *\"{learner_question}\"*: {lesson.explanation} "
                f"Specifically, we enforce: {'; '.join(lesson.learning_objectives)}"
            ),
            ExplanationDepth.RESEARCH_LEVEL: (
                f"**[Research-Level Epistemic & Frontier View]** Examining **{lesson.title}** from a primary-literature "
                f"and experimental-methodology perspective (*\"{learner_question}\"*): {lesson.explanation} "
                f"Research design focus: {lesson.learning_objectives[-1]}"
            ),
        }
        parts = [depth_intros[depth]]
        if grounded_snippets:
            parts.append(
                "**Directly Retrieved Primary Evidence:** " + " ".join(grounded_snippets)
            )
        else:
            parts.append(
                "**Epistemic Provenance Note:** No additional full-text chunk excerpt matched this specific query beyond the lesson's approved resource metadata; the conceptual walkthrough above is pedagogical synthesis aligned with the lesson syllabus rather than a verbatim primary-chunk quotation."
            )
        return "\n\n".join(parts)

    def execute(
        self,
        *,
        run_state: WorkflowRunState,
        request: TeachingRequest,
        record_step: Callable[[str, str, Dict[str, Any], Callable[[], Any]], Any],
    ) -> TeachingResponse:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Step 1: Load curriculum and target lesson via ToolRegistry
        def _step_load_lesson() -> Tuple[CurriculumPlan, CurriculumLesson]:
            plan = self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="read_curriculum_plan",
                curriculum_id=request.curriculum_id,
            )
            if plan is None:
                raise PlatformValidationError(
                    "CURRICULUM_NOT_FOUND",
                    f"Curriculum '{request.curriculum_id}' was not found.",
                    {"curriculum_id": request.curriculum_id},
                )
            if plan.status != CurriculumStatus.VALIDATED:
                raise PlatformValidationError(
                    "CURRICULUM_NOT_VALIDATED",
                    f"Curriculum '{request.curriculum_id}' has status '{plan.status.value}' and cannot be used for teaching.",
                    {"curriculum_id": request.curriculum_id, "status": plan.status.value},
                )
            lesson = _find_lesson_in_plan(plan, request.lesson_id)
            if lesson is None:
                raise PlatformValidationError(
                    "LESSON_NOT_FOUND",
                    f"Lesson '{request.lesson_id}' does not exist in curriculum '{request.curriculum_id}'.",
                    {"curriculum_id": request.curriculum_id, "lesson_id": request.lesson_id},
                )
            return plan, lesson

        plan, lesson = record_step(
            "load_curriculum_and_lesson",
            "TeachingAgent",
            {"curriculum_id": request.curriculum_id, "lesson_id": request.lesson_id},
            _step_load_lesson,
        )

        # Step 2: Inspect concept graph & prerequisite bridges
        def _step_prereq_bridges() -> Tuple[PrerequisiteExplanation, ...]:
            self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="query_concept_graph",
                learner_goal=lesson.title,
            )
            return self._build_prerequisite_bridges(plan, lesson)

        prereq_bridges = record_step(
            "resolve_prerequisite_bridges",
            "TeachingAgent",
            {"lesson_id": lesson.lesson_id, "prerequisites": list(lesson.prerequisites)},
            _step_prereq_bridges,
        )

        # Step 3: Check domain relevance & retrieve lesson-specific evidence chunks
        q_lower = request.learner_question.lower()
        is_off_domain = any(kw in q_lower for kw in _OFF_DOMAIN_KEYWORDS) and not any(
            akw in q_lower for akw in _AI_DOMAIN_KEYWORDS
        )
        asks_about_the_batch = "the batch" in q_lower or "src-the-batch" in q_lower.replace("_", "-")

        def _step_retrieve_evidence() -> List[RetrievedChunk]:
            if is_off_domain:
                return []
            search_query = f"{lesson.title} {lesson.concept_id.replace('_', ' ')} {request.learner_question}"
            raw_chunks: List[RetrievedChunk] = self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="search_knowledge_chunks",
                query_text=search_query,
                top_k=run_state.execution_policy.top_k_per_query,
            )
            return [c for c in raw_chunks if c.source_id != THE_BATCH_SOURCE_ID]

        retrieved_chunks = record_step(
            "retrieve_lesson_evidence",
            "TeachingAgent",
            {"lesson_id": lesson.lesson_id, "is_off_domain": is_off_domain},
            _step_retrieve_evidence,
        )

        # Step 4: Synthesize grounded explanation, examples, Socratic prompts, and verify citations
        def _step_synthesize_and_verify() -> TeachingResponse:
            ihash = hashlib.sha256(
                f"{run_state.run_id}:{request.curriculum_id}:{lesson.lesson_id}:{request.explanation_depth.value}:{request.learner_question}:{now_iso}".encode("utf-8")
            ).hexdigest()[:12]
            interaction_id = f"teach_{ihash}"

            if is_off_domain:
                return TeachingResponse(
                    interaction_id=interaction_id,
                    run_id=run_state.run_id,
                    curriculum_id=plan.curriculum_id,
                    lesson_id=lesson.lesson_id,
                    concept_id=lesson.concept_id,
                    lesson_title=lesson.title,
                    learner_id=request.learner_id,
                    learner_question=request.learner_question,
                    explanation_depth=request.explanation_depth,
                    depth_disclaimer=TEACHING_DEPTH_DISCLAIMER,
                    status="INSUFFICIENT_EVIDENCE",
                    core_explanation=(
                        "I must abstain from answering this question because it falls outside the AI/ML domain "
                        f"and the scope of `{lesson.title}`. No verified evidence exists in the local knowledge store "
                        "for this off-domain topic, and I will not fabricate unsupported claims or citations."
                    ),
                    prerequisite_explanations=prereq_bridges,
                    examples_and_analogies=(),
                    socratic_questions=(
                        f"Would you like to refocus on `{lesson.title}`—for instance: {lesson.mastery_check.prompts[0]}",
                    ),
                    uncertainty_and_limits=(
                        "Insufficient evidence: query is unrelated to the indexed AI research library and lesson objectives."
                    ),
                    source_citations=(),
                    created_at=now_iso,
                )

            # Filter chunks that are relevant to the lesson or question
            node = self.concept_graph.get_node(lesson.concept_id)
            preferred_set = set(node.preferred_resource_ids)
            prioritized_chunks = [c for c in retrieved_chunks if c.resource_id in preferred_set]
            if not prioritized_chunks:
                prioritized_chunks = retrieved_chunks[:2]
            else:
                prioritized_chunks = prioritized_chunks[:2]

            citations: List[CurriculumCitation] = list(lesson.source_citations)
            seen_chunk_ids = {c.chunk_id for c in citations if c.chunk_id}
            grounded_snippets: List[str] = []

            for idx, ch in enumerate(prioritized_chunks, start=1):
                snippet = " ".join(ch.chunk_text.strip().split())
                if len(snippet) > 220:
                    snippet = snippet[:217].rstrip() + "..."
                grounded_snippets.append(f"From *{ch.title}*: \"{snippet}\" [{ch.chunk_id}].")
                if ch.chunk_id not in seen_chunk_ids:
                    citations.append(
                        CurriculumCitation(
                            citation_id=f"cit_teach_{lesson.lesson_id}_{idx:02d}",
                            evidence_tier=EvidenceTier.CURRICULUM_READY_EVIDENCE,
                            source_id=ch.source_id,
                            resource_id=ch.resource_id,
                            title=ch.title,
                            canonical_url=ch.canonical_url,
                            chunk_id=ch.chunk_id,
                            epistemic_layer=ch.epistemic_layer,
                            locator=f"chunk_index={ch.chunk_index} ({ch.char_start}..{ch.char_end})",
                            excerpt_summary=snippet,
                            relevance_score=round(float(ch.similarity_score), 4),
                        )
                    )
                    seen_chunk_ids.add(ch.chunk_id)

            valid_chunk_ids = {c.chunk_id for c in citations if c.chunk_id is not None}
            raw_explanation = self._format_depth_framing(
                request.explanation_depth,
                lesson,
                request.learner_question,
                grounded_snippets,
            )
            sanitized_explanation = _sanitize_answer_citations(raw_explanation, valid_chunk_ids)

            # Verify all citations against SQLite knowledge store
            cit_ok, cit_errors = self.ecosystem_service.verify_citation_records(citations)
            if not cit_ok:
                raise PlatformValidationError(
                    "TEACHING_CITATION_VERIFICATION_FAILED",
                    f"Teaching Agent citation verification failed: {'; '.join(cit_errors)}",
                    {"errors": cit_errors},
                )

            examples_pair = _CONCEPT_EXAMPLES_AND_ANALOGIES.get(
                lesson.concept_id,
                (
                    f"Analogy: Think of `{lesson.title}` as a modular building block that transforms representations while preserving prerequisite invariants.",
                    f"Concrete Example: Apply `{lesson.learning_objectives[0]}` on a small synthetic tensor to inspect inputs, outputs, and gradients.",
                ),
            )
            socratic_prompts = list(lesson.mastery_check.prompts)
            socratic_prompts.append(
                f"How does mastering '{lesson.learning_objectives[0]}' prepare you for the next stage of the curriculum?"
            )

            uncertainty_notes: List[str] = []
            if asks_about_the_batch:
                uncertainty_notes.append(
                    f"Governance boundary: `{THE_BATCH_SOURCE_ID}` (`CANDIDATE_PENDING_REVIEW`) is a weekly AI news radar only and is never cited as authoritative lesson evidence."
                )
            if lesson.evidence_gaps:
                uncertainty_notes.extend(lesson.evidence_gaps)
            has_chunk_evidence = any(
                c.evidence_tier == EvidenceTier.CURRICULUM_READY_EVIDENCE and c.chunk_id is not None
                for c in citations
            )
            if not has_chunk_evidence:
                status = "PEDAGOGICAL_SYNTHESIS_WITH_METADATA"
                uncertainty_notes.append(
                    "This lesson currently references approved seed resource metadata (`APPROVED_UNCOLLECTED_RESOURCE`) without full-text extracted chunks in Chroma; mathematical derivations are presented as explicit pedagogical synthesis."
                )
            else:
                status = "GROUNDED_EXPLANATION"
                if not uncertainty_notes:
                    uncertainty_notes.append(
                        "Primary chunk citations (`[chk_...]`) verify the core architectural/theoretical claims; analogies and Socratic prompts are pedagogical scaffolding built on top of those cited sources."
                    )

            return TeachingResponse(
                interaction_id=interaction_id,
                run_id=run_state.run_id,
                curriculum_id=plan.curriculum_id,
                lesson_id=lesson.lesson_id,
                concept_id=lesson.concept_id,
                lesson_title=lesson.title,
                learner_id=request.learner_id,
                learner_question=request.learner_question,
                explanation_depth=request.explanation_depth,
                depth_disclaimer=TEACHING_DEPTH_DISCLAIMER,
                status=status,
                core_explanation=sanitized_explanation,
                prerequisite_explanations=prereq_bridges,
                examples_and_analogies=examples_pair,
                socratic_questions=tuple(socratic_prompts),
                uncertainty_and_limits=" ".join(uncertainty_notes),
                source_citations=tuple(citations),
                created_at=now_iso,
            )

        return record_step(
            "synthesize_and_verify_teaching_response",
            "TeachingAgent",
            {"lesson_id": lesson.lesson_id, "explanation_depth": request.explanation_depth.value},
            _step_synthesize_and_verify,
        )


# ---------------------------------------------------------------------------
# Assessment Agent
# ---------------------------------------------------------------------------


_CONCEPT_ASSESSMENT_BLUEPRINTS: Dict[str, Dict[str, Any]] = {
    "math_linear_algebra_calculus": {
        "conceptual_terms": (("vector", "matrix", "tensor", "linear"), ("transformation", "shape", "transpose", "geometric")),
        "mathematical_terms": (("gradient", "jacobian", "partial derivative"), ("chain rule", "directional", "transpose")),
        "practical_terms": (("jacobian", "tanh", "matrix"), ("numerical", "gradient check", "finite difference")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_math_commutative_matrices",
                trigger_phrases=("matrix multiplication is always commutative", "ab always equals ba", "a*b == b*a for all matrices"),
                explanation="Matrix multiplication is generally non-commutative (AB != BA), and inner dimensions must match.",
                remediation_objective="Represent inputs, parameters, and activations as vectors, matrices, and tensors.",
            ),
            MisconceptionPattern(
                pattern_id="misc_math_gradient_direction",
                trigger_phrases=("gradient points toward steepest descent", "add the gradient to minimize loss"),
                explanation="The gradient nabla_theta L points in the direction of steepest ascent; minimizing loss requires stepping in the negative gradient direction (-nabla_theta L).",
                remediation_objective="Compute Jacobians, directional derivatives, and multivariable chain-rule expansions.",
            ),
        ),
    },
    "backpropagation_and_gradient_descent": {
        "conceptual_terms": (("reverse-mode", "forward-mode", "chain rule", "graph"), ("scalar", "sensitivity", "backward", "layer")),
        "mathematical_terms": (("chain rule", "reverse-mode", "delta"), ("derivative", "weight", "activation", "gradient")),
        "practical_terms": (("forward", "backward", "activation"), ("update", "learning rate", "weight", "gradient")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_backprop_linear_depth",
                trigger_phrases=("stacking linear layers without nonlinearities increases expressivity", "no activation function is needed for deep representations"),
                explanation="Without nonlinear activations, any stack of linear layers W_2(W_1 x) collapses algebraically into a single linear transformation W' x.",
                remediation_objective="Formulate reverse-mode automatic differentiation over a computational graph using the chain rule.",
            ),
            MisconceptionPattern(
                pattern_id="misc_backprop_zero_init",
                trigger_phrases=("initialize all weights to zero", "zero initialization breaks symmetry"),
                explanation="Initializing all weights to zero causes all hidden units in a layer to compute identical activations and receive identical gradients (symmetry lock).",
                remediation_objective="Implement forward and backward passes for a multilayer perceptron trained via stochastic gradient descent.",
            ),
        ),
    },
    "regularization_and_dropout": {
        "conceptual_terms": (("co-adaptation", "overfitting", "random", "subset"), ("ensemble", "sub-network", "generalization")),
        "mathematical_terms": (("1/(1-p)", "1-p", "expected", "expectation"), ("scale", "scaling", "probability", "mask")),
        "practical_terms": (("train", "eval", "inference", "test"), ("mask", "bernoulli", "inverted", "scale")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_dropout_active_at_eval",
                trigger_phrases=("keep dropping units at test time", "apply random dropout masks during standard evaluation", "dropout is only used at inference"),
                explanation="Standard evaluation disables random dropout masking and relies on training-time inverted scaling 1/(1-p) (or test-time (1-p) weight scaling) so predictions are deterministic.",
                remediation_objective="Implement inverted dropout during training and deterministic weight scaling at inference time.",
            ),
            MisconceptionPattern(
                pattern_id="misc_dropout_increases_coadaptation",
                trigger_phrases=("dropout encourages units to co-adapt", "increases co-adaptation between hidden units"),
                explanation="Dropout prevents complex co-adaptation by randomly removing units so no feature detector can rely on fixed partner units.",
                remediation_objective="Diagnose overfitting and complex feature co-adaptation in overparameterized feedforward networks.",
            ),
        ),
    },
    "energy_based_and_boltzmann_models": {
        "conceptual_terms": (("energy", "boltzmann", "stochastic", "symmetric"), ("equilibrium", "temperature", "partition")),
        "mathematical_terms": (("bipartite", "conditional", "independent", "factor"), ("visible", "hidden", "within-layer", "intra-layer")),
        "practical_terms": (("gibbs", "contrastive divergence", "sample", "sampling"), ("energy", "probability", "rbm", "hidden")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_rbm_intralayer_edges",
                trigger_phrases=("rbm has hidden-to-hidden connections", "within-layer connections in an rbm", "intra-layer connections are kept in rbm"),
                explanation="A Restricted Boltzmann Machine (RBM) explicitly removes all intra-layer (visible-visible and hidden-to-hidden) connections to make P(h|v) and P(v|h) conditionally independent.",
                remediation_objective="Explain why unrestricted partition function intractability motivates Restricted Boltzmann Machines (RBMs) and Contrastive Divergence.",
            ),
            MisconceptionPattern(
                pattern_id="misc_boltzmann_high_energy_prob",
                trigger_phrases=("higher energy means higher probability", "states with maximum energy are most probable"),
                explanation="In the Boltzmann-Gibbs distribution P(s) proportional to exp(-E(s)/T), lower energy E(s) corresponds to exponentially higher probability.",
                remediation_objective="Relate the Boltzmann-Gibbs equilibrium probability distribution to thermal temperature T and energy gaps.",
            ),
        ),
    },
    "deep_residual_networks": {
        "conceptual_terms": (("degradation", "training error", "training accuracy"), ("overfitting", "identity", "optimize", "optimization")),
        "mathematical_terms": (("f(x) + x", "h(x) - x", "residual"), ("identity", "jacobian", "gradient", "i +")),
        "practical_terms": (("shortcut", "skip", "residual"), ("gradient", "norm", "vanishing", "layer")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_resnet_degradation_is_overfitting",
                trigger_phrases=("degradation is caused by overfitting", "degradation problem is just overfitting", "training error increases because of overfitting"),
                explanation="He et al. (2015) demonstrate that degradation is NOT caused by overfitting: overfitting yields lower training error with higher test error, whereas degradation causes higher training error itself as plain networks grow deeper.",
                remediation_objective="Distinguish the deep network training degradation problem from classical overfitting.",
            ),
        ),
    },
    "sequence_alignment_and_bahdanau_attention": {
        "conceptual_terms": (("fixed-length", "bottleneck", "compress", "long"), ("encoder", "decoder", "search", "annotation")),
        "mathematical_terms": (("softmax", "alignment", "alpha", "score"), ("context vector", "weighted sum", "differentiable")),
        "practical_terms": (("attention", "softmax", "weights", "sum to 1"), ("context", "encoder", "decoder", "step")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_bahdanau_nondifferentiable",
                trigger_phrases=("soft attention is not differentiable", "requires reinforcement learning because softmax is discrete"),
                explanation="Soft alignment computes a continuous softmax-weighted sum over encoder annotations, which is fully differentiable with standard backpropagation.",
                remediation_objective="Formulate additive (Bahdanau) soft alignment scores e_{ij}, attention weights alpha_{ij}, and dynamic context vectors c_i.",
            ),
        ),
    },
    "transformer_self_attention_architecture": {
        "conceptual_terms": (("recurrence", "parallel", "sequential", "o(1)"), ("self-attention", "long-range", "path length")),
        "mathematical_terms": (("sqrt(d_k)", "dot-product", "softmax", "variance"), ("query", "key", "value", "multi-head")),
        "practical_terms": (("mask", "causal", "multi-head", "attention"), ("residual", "layer norm", "projection", "head")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_transformer_uses_rnn",
                trigger_phrases=("transformer relies on recurrent hidden states", "processes tokens sequentially with an rnn"),
                explanation="Vaswani et al. (2017) dispense entirely with recurrence and convolutions, computing sequence representations via self-attention in O(1) sequential operations.",
                remediation_objective="Contrast recurrent/convolutional sequence transduction with the recurrence-free Transformer architecture.",
            ),
            MisconceptionPattern(
                pattern_id="misc_transformer_scaling_reason",
                trigger_phrases=("scale by sqrt(d_k) because dot products become too small", "divide by sqrt(d_k) to increase softmax saturation"),
                explanation="For large d_k, dot products grow large in magnitude (variance d_k), pushing softmax into regions with tiny gradients; scaling by 1/sqrt(d_k) prevents saturation.",
                remediation_objective="Implement Scaled Dot-Product Attention, Multi-Head Attention projections, and residual layer-normalized sub-layers.",
            ),
        ),
    },
    "ai_research_methodology_and_safety_frontiers": {
        "conceptual_terms": (("ablation", "baseline", "control", "hypothesis"), ("reproducible", "confound", "seed", "isolate")),
        "mathematical_terms": (("gflownet", "proportional", "reward", "r(x)"), ("flow", "diversity", "mode", "policy")),
        "practical_terms": (("governance", "the batch", "candidate", "evidence"), ("safety", "alignment", "ablation", "verification")),
        "misconceptions": (
            MisconceptionPattern(
                pattern_id="misc_gflownet_greedy_rl",
                trigger_phrases=("gflownet only maximizes cumulative return on a single mode", "identical objective to greedy q-learning"),
                explanation="Unlike standard reward-maximizing RL that concentrates on a single highest-reward mode, GFlowNets train a policy so terminal state sampling probability P(x) is proportional to R(x).",
                remediation_objective="Survey frontier research directions in our curated catalog, including Generative Flow Networks (GFlowNets) and honest/cautious scientist AI safety frameworks.",
            ),
            MisconceptionPattern(
                pattern_id="misc_the_batch_primary_proof",
                trigger_phrases=("the batch is an approved primary peer-reviewed source", "cite src-the-batch as ground-truth chunk evidence"),
                explanation="SRC-THE-BATCH has status CANDIDATE_PENDING_REVIEW and is restricted to discovery radar only; it must never be cited as authoritative lesson evidence.",
                remediation_objective="Maintain strict epistemic hygiene: separate primary empirical evidence from metadata-only research seeds and candidate news feeds (`SRC-THE-BATCH`).",
            ),
        ),
    },
}


class AssessmentAgent:
    """Interactive Assessment Agent generating objective-aligned exercises and evaluating against deterministic rubrics."""

    capability = AgentCapability.ASSESSMENT

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        concept_graph: Optional[ConceptGraph] = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.concept_graph = concept_graph or build_default_concept_graph()

    def build_exercises_for_lesson(
        self,
        plan: CurriculumPlan,
        lesson: CurriculumLesson,
    ) -> Tuple[AssessmentExercise, ...]:
        """Deterministically construct conceptual, mathematical, and practical exercises for a lesson."""
        objs = list(lesson.learning_objectives)
        obj_conceptual = objs[0]
        obj_math = objs[1] if len(objs) > 1 else objs[0]
        obj_practical = objs[2] if len(objs) > 2 else objs[-1]

        bp = _CONCEPT_ASSESSMENT_BLUEPRINTS.get(lesson.concept_id, {})
        c_terms_1, c_terms_2 = bp.get(
            "conceptual_terms",
            (("concept", "architecture", "representation"), ("objective", "mechanism", "model")),
        )
        m_terms_1, m_terms_2 = bp.get(
            "mathematical_terms",
            (("equation", "derivative", "function"), ("parameter", "scale", "distribution")),
        )
        p_terms_1, p_terms_2 = bp.get(
            "practical_terms",
            (("implement", "verify", "test"), ("training", "evaluation", "diagnostic")),
        )
        misconceptions: Tuple[MisconceptionPattern, ...] = tuple(bp.get("misconceptions", ()))

        mc_prompt_0 = lesson.mastery_check.prompts[0] if lesson.mastery_check.prompts else f"Explain the core mechanism of {lesson.title}."
        mc_prompt_1 = (
            lesson.mastery_check.prompts[1]
            if len(lesson.mastery_check.prompts) > 1
            else f"Formulate the mathematical structure underlying: {obj_math}"
        )
        prac_spec = lesson.exercises[0] if lesson.exercises else {
            "exercise_id": f"{lesson.lesson_id}_ex_01",
            "title": f"Practical Implementation: {lesson.title}",
            "description": obj_practical,
            "deliverable": "Structured verification summary.",
        }
        native_ex_id = str(prac_spec.get("exercise_id", f"{lesson.lesson_id}_ex_01"))

        ex_conceptual = AssessmentExercise(
            exercise_id=f"{lesson.lesson_id}_ex_conceptual",
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
            concept_id=lesson.concept_id,
            category=AssessmentQuestionCategory.CONCEPTUAL,
            title=f"Conceptual Analysis: {lesson.title}",
            prompt=f"{mc_prompt_0} Address the lesson objective: {obj_conceptual}",
            targeted_objectives=(obj_conceptual,),
            starter_hint=(
                f"Include key concepts ({', '.join(c_terms_1[:3])}) and explain the causal mechanism ({', '.join(c_terms_2[:3])})."
            ),
            rubric=(
                RubricCriterion(
                    criterion_id=f"{lesson.lesson_id}_crit_conc_01",
                    description="Identifies core domain primitives and structural mechanism.",
                    target_objective=obj_conceptual,
                    required_concepts=tuple(c_terms_1),
                    weight=0.5,
                ),
                RubricCriterion(
                    criterion_id=f"{lesson.lesson_id}_crit_conc_02",
                    description="Explains causal role and contrast against naive alternatives.",
                    target_objective=obj_conceptual,
                    required_concepts=tuple(c_terms_2),
                    weight=0.5,
                ),
            ),
            misconception_patterns=misconceptions,
            alias_exercise_ids=("conceptual",),
        )

        ex_mathematical = AssessmentExercise(
            exercise_id=f"{lesson.lesson_id}_ex_mathematical",
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
            concept_id=lesson.concept_id,
            category=AssessmentQuestionCategory.MATHEMATICAL,
            title=f"Mathematical Formulation & Derivation: {lesson.title}",
            prompt=f"{mc_prompt_1} Target objective: {obj_math}",
            targeted_objectives=(obj_math,),
            starter_hint=(
                f"State the formal terms ({', '.join(m_terms_1[:3])}) and explain how they interact ({', '.join(m_terms_2[:3])})."
            ),
            rubric=(
                RubricCriterion(
                    criterion_id=f"{lesson.lesson_id}_crit_math_01",
                    description="States accurate mathematical operators, variables, or equations.",
                    target_objective=obj_math,
                    required_concepts=tuple(m_terms_1),
                    weight=0.5,
                ),
                RubricCriterion(
                    criterion_id=f"{lesson.lesson_id}_crit_math_02",
                    description="Connects the mathematical derivation to optimization or probabilistic behavior.",
                    target_objective=obj_math,
                    required_concepts=tuple(m_terms_2),
                    weight=0.5,
                ),
            ),
            misconception_patterns=misconceptions,
            alias_exercise_ids=("mathematical",),
        )

        ex_practical = AssessmentExercise(
            exercise_id=f"{lesson.lesson_id}_ex_practical",
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
            concept_id=lesson.concept_id,
            category=AssessmentQuestionCategory.PRACTICAL,
            title=f"Practical Engineering Exercise: {prac_spec.get('title', lesson.title)}",
            prompt=(
                f"{prac_spec.get('description', obj_practical)} "
                f"Describe your implementation design and verification checks for deliverable: {prac_spec.get('deliverable', '')}"
            ),
            targeted_objectives=(obj_practical,),
            starter_hint=(
                f"Describe the implementation steps ({', '.join(p_terms_1[:3])}) and verification diagnostics ({', '.join(p_terms_2[:3])})."
            ),
            rubric=(
                RubricCriterion(
                    criterion_id=f"{lesson.lesson_id}_crit_prac_01",
                    description="Specifies concrete implementation mechanics and tensor/algorithmic flow.",
                    target_objective=obj_practical,
                    required_concepts=tuple(p_terms_1),
                    weight=0.5,
                ),
                RubricCriterion(
                    criterion_id=f"{lesson.lesson_id}_crit_prac_02",
                    description="Defines verification tests, invariants, or ablation diagnostics.",
                    target_objective=obj_practical,
                    required_concepts=tuple(p_terms_2),
                    weight=0.5,
                ),
            ),
            misconception_patterns=misconceptions,
            alias_exercise_ids=(native_ex_id, "practical"),
        )

        return (ex_conceptual, ex_mathematical, ex_practical)

    def generate_exercises(
        self,
        *,
        run_state: WorkflowRunState,
        curriculum_id: str,
        lesson_id: str,
        record_step: Callable[[str, str, Dict[str, Any], Callable[[], Any]], Any],
    ) -> Tuple[AssessmentExercise, ...]:
        def _step_generate() -> Tuple[AssessmentExercise, ...]:
            plan = self.tool_registry.invoke(
                run_state=run_state,
                capability=self.capability,
                tool_name="read_curriculum_plan",
                curriculum_id=curriculum_id,
            )
            if plan is None:
                raise PlatformValidationError(
                    "CURRICULUM_NOT_FOUND",
                    f"Curriculum '{curriculum_id}' was not found.",
                    {"curriculum_id": curriculum_id},
                )
            if plan.status != CurriculumStatus.VALIDATED:
                raise PlatformValidationError(
                    "CURRICULUM_NOT_VALIDATED",
                    f"Curriculum '{curriculum_id}' is not validated.",
                    {"curriculum_id": curriculum_id, "status": plan.status.value},
                )
            lesson = _find_lesson_in_plan(plan, lesson_id)
            if lesson is None:
                raise PlatformValidationError(
                    "LESSON_NOT_FOUND",
                    f"Lesson '{lesson_id}' does not exist in curriculum '{curriculum_id}'.",
                    {"curriculum_id": curriculum_id, "lesson_id": lesson_id},
                )
            return self.build_exercises_for_lesson(plan, lesson)

        return record_step(
            "generate_lesson_exercises",
            "AssessmentAgent",
            {"curriculum_id": curriculum_id, "lesson_id": lesson_id},
            _step_generate,
        )

    @staticmethod
    def _term_matches(term: str, answer_lower: str) -> bool:
        t = term.lower().strip()
        if t in answer_lower:
            return True
        # Match root stems for longer words (e.g. "nonlinear" / "non-linear", "differentiable" / "differentiation")
        t_alnum = re.sub(r"[^a-z0-9]+", "", t)
        ans_alnum = re.sub(r"[^a-z0-9]+", "", answer_lower)
        if len(t_alnum) >= 5 and t_alnum[:6] in ans_alnum:
            return True
        return False

    def evaluate_submission(
        self,
        *,
        run_state: WorkflowRunState,
        curriculum_id: str,
        lesson_id: str,
        exercise_id: str,
        submitted_answer: Any,
        learner_id: str = DEFAULT_LEARNER_ID,
        record_step: Callable[[str, str, Dict[str, Any], Callable[[], Any]], Any],
    ) -> AssessmentEvaluation:
        if not isinstance(curriculum_id, str) or not curriculum_id.strip():
            raise PlatformValidationError("INVALID_CURRICULUM_ID", "curriculum_id must be a non-empty string.")
        if not isinstance(lesson_id, str) or not lesson_id.strip():
            raise PlatformValidationError("INVALID_LESSON_ID", "lesson_id must be a non-empty string.")
        if not isinstance(exercise_id, str) or not exercise_id.strip():
            raise PlatformValidationError("INVALID_EXERCISE_ID", "exercise_id must be a non-empty string.")
        if not isinstance(submitted_answer, str):
            raise PlatformValidationError("INVALID_SUBMISSION_TYPE", "submitted_answer must be a string.")
        cleaned_answer = " ".join(submitted_answer.strip().split())
        if not cleaned_answer:
            raise PlatformValidationError("EMPTY_SUBMISSION", "submitted_answer must not be empty.")
        if len(cleaned_answer) < 12:
            raise PlatformValidationError(
                "SUBMISSION_TOO_SHORT",
                "submitted_answer must be at least 12 characters long to allow rubric evaluation.",
                {"min_length": 12, "actual_length": len(cleaned_answer)},
            )
        if len(cleaned_answer) > 8000:
            raise PlatformValidationError(
                "SUBMISSION_TOO_LONG",
                "submitted_answer must not exceed 8000 characters.",
                {"max_length": 8000, "actual_length": len(cleaned_answer)},
            )

        exercises = self.generate_exercises(
            run_state=run_state,
            curriculum_id=curriculum_id.strip(),
            lesson_id=lesson_id.strip(),
            record_step=record_step,
        )

        target_ex: Optional[AssessmentExercise] = None
        ex_id_clean = exercise_id.strip()
        for ex in exercises:
            if ex.exercise_id == ex_id_clean or ex_id_clean in ex.alias_exercise_ids:
                target_ex = ex
                break
        if target_ex is None:
            raise PlatformValidationError(
                "EXERCISE_NOT_FOUND",
                f"Exercise '{exercise_id}' was not found for lesson '{lesson_id}'.",
                {
                    "lesson_id": lesson_id,
                    "exercise_id": exercise_id,
                    "available_exercise_ids": [e.exercise_id for e in exercises],
                },
            )

        def _step_evaluate() -> AssessmentEvaluation:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            ans_lower = cleaned_answer.lower()
            word_count = len(cleaned_answer.split())

            # 1. Check misconception patterns deterministically
            detected_misconceptions: List[Dict[str, str]] = []
            for mp in target_ex.misconception_patterns:
                for phrase in mp.trigger_phrases:
                    if phrase.lower() in ans_lower:
                        detected_misconceptions.append(
                            {
                                "pattern_id": mp.pattern_id,
                                "triggered_by": phrase,
                                "explanation": mp.explanation,
                                "remediation_objective": mp.remediation_objective,
                            }
                        )
                        break

            # 2. Evaluate each rubric criterion deterministically
            crit_evals: List[CriterionEvaluation] = []
            raw_score = 0.0
            for crit in target_ex.rubric:
                matched: List[str] = []
                missing: List[str] = []
                for req in crit.required_concepts:
                    if self._term_matches(req, ans_lower):
                        matched.append(req)
                    else:
                        missing.append(req)

                if len(matched) >= 2 and word_count >= 8:
                    crit_passed = True
                    awarded = crit.weight
                    fb = f"Criterion '{crit.criterion_id}' satisfied: matched concepts {matched}."
                elif len(matched) == 1 and word_count >= 5:
                    crit_passed = False
                    awarded = round(crit.weight * 0.5, 4)
                    fb = (
                        f"Criterion '{crit.criterion_id}' partially satisfied: matched {matched}, "
                        f"but needs deeper discussion of {missing[:2]}."
                    )
                else:
                    crit_passed = False
                    awarded = 0.0
                    fb = (
                        f"Criterion '{crit.criterion_id}' not yet demonstrated: address key concepts "
                        f"such as {missing[:3]}."
                    )

                raw_score += awarded
                crit_evals.append(
                    CriterionEvaluation(
                        criterion_id=crit.criterion_id,
                        description=crit.description,
                        target_objective=crit.target_objective,
                        passed=crit_passed,
                        score_awarded=awarded,
                        max_weight=crit.weight,
                        matched_concepts=tuple(matched),
                        missing_concepts=tuple(missing),
                        feedback=fb,
                    )
                )

            # Penalize if misconceptions were detected so a flawed answer cannot pass as DEMONSTRATED
            if detected_misconceptions:
                final_score = round(min(raw_score, 0.55), 4)
            else:
                final_score = round(min(1.0, raw_score), 4)

            if final_score >= 0.75 and not detected_misconceptions:
                score_band = "DEMONSTRATED"
                passed = True
            elif final_score >= 0.40:
                score_band = "PARTIAL"
                passed = False
            else:
                score_band = "NEEDS_PRACTICE"
                passed = False

            demonstrated_objs: List[str] = []
            needs_practice_objs: List[str] = []
            misconception_objs = {m["remediation_objective"] for m in detected_misconceptions}

            for obj in target_ex.targeted_objectives:
                obj_crits = [c for c in crit_evals if c.target_objective == obj]
                if obj not in misconception_objs and obj_crits and all(c.passed for c in obj_crits) and passed:
                    demonstrated_objs.append(obj)
                else:
                    needs_practice_objs.append(obj)

            for m_obj in misconception_objs:
                if m_obj not in needs_practice_objs:
                    needs_practice_objs.append(m_obj)

            feedback_lines: List[str] = [
                f"Deterministic Rubric Score: {final_score:.2f} ({score_band}) on `{target_ex.title}` [{target_ex.category.value}]."
            ]
            for ce in crit_evals:
                feedback_lines.append(f"- {ce.feedback}")
            if detected_misconceptions:
                feedback_lines.append(
                    "Misconceptions Detected: "
                    + " | ".join(f"[{m['pattern_id']}] {m['explanation']}" for m in detected_misconceptions)
                )
            feedback_lines.append(f"Mastery Boundary Note: {SINGLE_EXERCISE_MASTERY_DISCLAIMER}")

            ahash = hashlib.sha256(
                f"{run_state.run_id}:{learner_id}:{curriculum_id}:{target_ex.exercise_id}:{cleaned_answer}:{now_iso}".encode("utf-8")
            ).hexdigest()[:12]

            return AssessmentEvaluation(
                attempt_id=f"att_{ahash}",
                run_id=run_state.run_id,
                learner_id=learner_id,
                curriculum_id=curriculum_id.strip(),
                lesson_id=target_ex.lesson_id,
                concept_id=target_ex.concept_id,
                exercise_id=target_ex.exercise_id,
                category=target_ex.category,
                submitted_answer=cleaned_answer,
                score=final_score,
                score_band=score_band,
                passed=passed,
                complete_lesson_mastery_claimed=False,
                mastery_disclaimer=SINGLE_EXERCISE_MASTERY_DISCLAIMER,
                evaluation_mode="DETERMINISTIC_RUBRIC_ONLY",
                deterministic_feedback="\n".join(feedback_lines),
                optional_llm_feedback=None,
                criterion_evaluations=tuple(crit_evals),
                detected_misconceptions=tuple(detected_misconceptions),
                demonstrated_objectives=tuple(demonstrated_objs),
                objectives_needing_practice=tuple(needs_practice_objs),
                evaluated_at=now_iso,
            )

        return record_step(
            "evaluate_exercise_submission",
            "AssessmentAgent",
            {
                "curriculum_id": curriculum_id,
                "lesson_id": lesson_id,
                "exercise_id": target_ex.exercise_id,
                "category": target_ex.category.value,
            },
            _step_evaluate,
        )


# ---------------------------------------------------------------------------
# Learning Progress Agent
# ---------------------------------------------------------------------------


class LearningProgressAgent:
    """Manages persistent learner progress states and prerequisite-aware next lesson recommendations."""

    capability = AgentCapability.LEARNING_PROGRESS

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        progress_store: LearningProgressStore,
        assessment_agent: AssessmentAgent,
        concept_graph: Optional[ConceptGraph] = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.progress_store = progress_store
        self.assessment_agent = assessment_agent
        self.concept_graph = concept_graph or build_default_concept_graph()

    def record_lesson_opened(
        self,
        *,
        learner_id: str,
        plan: CurriculumPlan,
        lesson: CurriculumLesson,
    ) -> LearnerLessonProgress:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = self.progress_store.get_lesson_row(
            learner_id=learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
        )
        if existing is None:
            self.progress_store.upsert_lesson_progress(
                learner_id=learner_id,
                curriculum_id=plan.curriculum_id,
                lesson_id=lesson.lesson_id,
                concept_id=lesson.concept_id,
                status=LessonProgressStatus.IN_PROGRESS,
                teaching_questions_count=0,
                assessment_attempts_count=0,
                best_score=0.0,
                latest_score=None,
                demonstrated_objectives=(),
                knowledge_gaps=(),
                completed_exercise_ids=(),
                last_activity_at=now_iso,
            )
        else:
            current_status = LessonProgressStatus(str(existing["status"]))
            new_status = (
                LessonProgressStatus.IN_PROGRESS
                if current_status == LessonProgressStatus.NOT_STARTED
                else current_status
            )
            self.progress_store.upsert_lesson_progress(
                learner_id=learner_id,
                curriculum_id=plan.curriculum_id,
                lesson_id=lesson.lesson_id,
                concept_id=lesson.concept_id,
                status=new_status,
                teaching_questions_count=int(existing["teaching_questions_count"]),
                assessment_attempts_count=int(existing["assessment_attempts_count"]),
                best_score=float(existing["best_score"]),
                latest_score=float(existing["latest_score"]) if existing["latest_score"] is not None else None,
                demonstrated_objectives=json.loads(str(existing["demonstrated_objectives_json"])),
                knowledge_gaps=json.loads(str(existing["knowledge_gaps_json"])),
                completed_exercise_ids=json.loads(str(existing["completed_exercise_ids_json"])),
                last_activity_at=now_iso,
            )
        return self.get_lesson_progress(learner_id=learner_id, plan=plan, lesson=lesson)

    def record_teaching_turn(
        self,
        *,
        plan: CurriculumPlan,
        lesson: CurriculumLesson,
        response: TeachingResponse,
    ) -> LearnerLessonProgress:
        self.progress_store.record_teaching_interaction(response)
        existing = self.progress_store.get_lesson_row(
            learner_id=response.learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
        )
        if existing is None:
            self.progress_store.upsert_lesson_progress(
                learner_id=response.learner_id,
                curriculum_id=plan.curriculum_id,
                lesson_id=lesson.lesson_id,
                concept_id=lesson.concept_id,
                status=LessonProgressStatus.IN_PROGRESS,
                teaching_questions_count=1,
                assessment_attempts_count=0,
                best_score=0.0,
                latest_score=None,
                demonstrated_objectives=(),
                knowledge_gaps=(),
                completed_exercise_ids=(),
                last_activity_at=response.created_at,
            )
        else:
            current_status = LessonProgressStatus(str(existing["status"]))
            new_status = (
                LessonProgressStatus.IN_PROGRESS
                if current_status == LessonProgressStatus.NOT_STARTED
                else current_status
            )
            self.progress_store.upsert_lesson_progress(
                learner_id=response.learner_id,
                curriculum_id=plan.curriculum_id,
                lesson_id=lesson.lesson_id,
                concept_id=lesson.concept_id,
                status=new_status,
                teaching_questions_count=int(existing["teaching_questions_count"]) + 1,
                assessment_attempts_count=int(existing["assessment_attempts_count"]),
                best_score=float(existing["best_score"]),
                latest_score=float(existing["latest_score"]) if existing["latest_score"] is not None else None,
                demonstrated_objectives=json.loads(str(existing["demonstrated_objectives_json"])),
                knowledge_gaps=json.loads(str(existing["knowledge_gaps_json"])),
                completed_exercise_ids=json.loads(str(existing["completed_exercise_ids_json"])),
                last_activity_at=response.created_at,
            )
        return self.get_lesson_progress(learner_id=response.learner_id, plan=plan, lesson=lesson)

    def record_assessment_evaluation(
        self,
        *,
        plan: CurriculumPlan,
        lesson: CurriculumLesson,
        evaluation: AssessmentEvaluation,
    ) -> LearnerLessonProgress:
        """Update persistent lesson progress from an AssessmentEvaluation.

        Enforces the mastery rule: a single correct answer never equates to complete
        lesson mastery (`COMPLETED`). Transitioning to `COMPLETED` requires demonstrating
        all lesson objectives across multiple exercises (all 3 exercise categories).
        """
        self.progress_store.record_assessment_attempt(evaluation)
        existing = self.progress_store.get_lesson_row(
            learner_id=evaluation.learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
        )
        exercises = self.assessment_agent.build_exercises_for_lesson(plan, lesson)
        required_exercise_count = len(exercises)

        if existing is None:
            q_count = 0
            att_count = 1
            best_score = evaluation.score
            dem_objs: List[str] = []
            gaps: List[str] = []
            comp_ex_ids: List[str] = []
        else:
            q_count = int(existing["teaching_questions_count"])
            att_count = int(existing["assessment_attempts_count"]) + 1
            best_score = max(float(existing["best_score"]), evaluation.score)
            dem_objs = list(json.loads(str(existing["demonstrated_objectives_json"])))
            gaps = list(json.loads(str(existing["knowledge_gaps_json"])))
            comp_ex_ids = list(json.loads(str(existing["completed_exercise_ids_json"])))

        if evaluation.passed:
            if evaluation.exercise_id not in comp_ex_ids:
                comp_ex_ids.append(evaluation.exercise_id)
            for obj in evaluation.demonstrated_objectives:
                if obj not in dem_objs:
                    dem_objs.append(obj)
                if obj in gaps:
                    gaps.remove(obj)
            # Clear misconception notes associated with demonstrated objectives
            gaps = [
                g for g in gaps
                if not any(obj in g for obj in evaluation.demonstrated_objectives)
            ]
        else:
            for obj in evaluation.objectives_needing_practice:
                if obj not in gaps:
                    gaps.append(obj)
                if obj in dem_objs and evaluation.detected_misconceptions:
                    dem_objs.remove(obj)
            for m in evaluation.detected_misconceptions:
                m_note = f"Misconception ({m['pattern_id']}): {m['explanation']}"
                if m_note not in gaps:
                    gaps.append(m_note)

        # Mastery gate: never equate a single correct answer with complete mastery!
        all_lesson_objs_demonstrated = all(
            obj in dem_objs for obj in lesson.learning_objectives
        )
        all_exercises_completed = len(comp_ex_ids) >= max(2, required_exercise_count)

        if all_lesson_objs_demonstrated and all_exercises_completed and len(gaps) == 0:
            new_status = LessonProgressStatus.COMPLETED
        else:
            new_status = LessonProgressStatus.PRACTICING

        self.progress_store.upsert_lesson_progress(
            learner_id=evaluation.learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
            concept_id=lesson.concept_id,
            status=new_status,
            teaching_questions_count=q_count,
            assessment_attempts_count=att_count,
            best_score=best_score,
            latest_score=evaluation.score,
            demonstrated_objectives=dem_objs,
            knowledge_gaps=gaps,
            completed_exercise_ids=comp_ex_ids,
            last_activity_at=evaluation.evaluated_at,
        )
        return self.get_lesson_progress(learner_id=evaluation.learner_id, plan=plan, lesson=lesson)

    def _completed_lesson_ids(self, learner_id: str, plan: CurriculumPlan) -> Set[str]:
        completed: Set[str] = set()
        for stage in plan.stages:
            for lesson in stage.lessons:
                row = self.progress_store.get_lesson_row(
                    learner_id=learner_id,
                    curriculum_id=plan.curriculum_id,
                    lesson_id=lesson.lesson_id,
                )
                if row and str(row["status"]) == LessonProgressStatus.COMPLETED.value:
                    completed.add(lesson.lesson_id)
        return completed

    def get_lesson_progress(
        self,
        *,
        learner_id: str,
        plan: CurriculumPlan,
        lesson: CurriculumLesson,
    ) -> LearnerLessonProgress:
        completed_ids = self._completed_lesson_ids(learner_id, plan)
        unmet = tuple(
            prereq_lid for prereq_lid in lesson.prerequisite_lesson_ids if prereq_lid not in completed_ids
        )
        row = self.progress_store.get_lesson_row(
            learner_id=learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
        )
        exercises = self.assessment_agent.build_exercises_for_lesson(plan, lesson)
        attempts = self.progress_store.list_assessment_attempts(
            learner_id=learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
        )
        if row is None:
            return LearnerLessonProgress(
                learner_id=learner_id,
                curriculum_id=plan.curriculum_id,
                lesson_id=lesson.lesson_id,
                concept_id=lesson.concept_id,
                lesson_order=lesson.lesson_order,
                title=lesson.title,
                status=LessonProgressStatus.NOT_STARTED,
                prerequisites_met=len(unmet) == 0,
                unmet_prerequisite_lesson_ids=unmet,
                teaching_questions_count=0,
                assessment_attempts_count=0,
                best_score=0.0,
                latest_score=None,
                completed_exercise_ids=(),
                total_exercises_count=len(exercises),
                demonstrated_objectives=(),
                total_objectives_count=len(lesson.learning_objectives),
                knowledge_gaps=(),
                last_activity_at=None,
                recent_attempts=tuple(attempts[:5]),
            )
        return LearnerLessonProgress(
            learner_id=learner_id,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
            concept_id=lesson.concept_id,
            lesson_order=lesson.lesson_order,
            title=lesson.title,
            status=LessonProgressStatus(str(row["status"])),
            prerequisites_met=len(unmet) == 0,
            unmet_prerequisite_lesson_ids=unmet,
            teaching_questions_count=int(row["teaching_questions_count"]),
            assessment_attempts_count=int(row["assessment_attempts_count"]),
            best_score=float(row["best_score"]),
            latest_score=float(row["latest_score"]) if row["latest_score"] is not None else None,
            completed_exercise_ids=tuple(json.loads(str(row["completed_exercise_ids_json"]))),
            total_exercises_count=len(exercises),
            demonstrated_objectives=tuple(json.loads(str(row["demonstrated_objectives_json"]))),
            total_objectives_count=len(lesson.learning_objectives),
            knowledge_gaps=tuple(json.loads(str(row["knowledge_gaps_json"]))),
            last_activity_at=str(row["last_activity_at"]),
            recent_attempts=tuple(attempts[:5]),
        )

    def compute_curriculum_progress(
        self,
        *,
        learner_id: str,
        plan: CurriculumPlan,
        target_lesson_id: Optional[str] = None,
    ) -> CurriculumProgressSummary:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_lessons: List[CurriculumLesson] = [
            lesson for stage in plan.stages for lesson in stage.lessons
        ]
        progress_list = [
            self.get_lesson_progress(learner_id=learner_id, plan=plan, lesson=lesson)
            for lesson in all_lessons
        ]
        by_id = {lp.lesson_id: lp for lp in progress_list}
        lesson_by_id = {l.lesson_id: l for l in all_lessons}

        not_started = sum(1 for lp in progress_list if lp.status == LessonProgressStatus.NOT_STARTED)
        in_progress = sum(1 for lp in progress_list if lp.status == LessonProgressStatus.IN_PROGRESS)
        practicing = sum(1 for lp in progress_list if lp.status == LessonProgressStatus.PRACTICING)
        completed = sum(1 for lp in progress_list if lp.status == LessonProgressStatus.COMPLETED)
        total = len(progress_list)
        ratio = round(completed / total, 4) if total > 0 else 0.0

        total_dem_objs = sum(len(lp.demonstrated_objectives) for lp in progress_list)
        total_curr_objs = sum(lp.total_objectives_count for lp in progress_list)
        all_gaps: List[str] = []
        for lp in progress_list:
            for g in lp.knowledge_gaps:
                if g not in all_gaps:
                    all_gaps.append(g)

        recommendation = self._compute_next_recommendation(
            learner_id=learner_id,
            plan=plan,
            progress_list=progress_list,
            by_id=by_id,
            lesson_by_id=lesson_by_id,
            target_lesson_id=target_lesson_id,
        )

        return CurriculumProgressSummary(
            learner_id=learner_id,
            curriculum_id=plan.curriculum_id,
            curriculum_title=plan.title,
            total_lessons=total,
            not_started_count=not_started,
            in_progress_count=in_progress,
            practicing_count=practicing,
            completed_count=completed,
            overall_completion_ratio=ratio,
            total_demonstrated_objectives=total_dem_objs,
            total_curriculum_objectives=total_curr_objs,
            active_knowledge_gaps=tuple(all_gaps),
            lesson_progress=tuple(progress_list),
            next_recommendation=recommendation,
            updated_at=now_iso,
        )

    def _compute_next_recommendation(
        self,
        *,
        learner_id: str,
        plan: CurriculumPlan,
        progress_list: Sequence[LearnerLessonProgress],
        by_id: Mapping[str, LearnerLessonProgress],
        lesson_by_id: Mapping[str, CurriculumLesson],
        target_lesson_id: Optional[str] = None,
    ) -> NextLessonRecommendation:
        # Case 0: If the caller asked about a specific target lesson with unmet prerequisites,
        # guide them to the earliest unmet prerequisite first.
        if target_lesson_id is not None:
            target_lesson = _find_lesson_in_plan(plan, target_lesson_id)
            if target_lesson is not None:
                target_lp = by_id[target_lesson.lesson_id]
                if not target_lp.prerequisites_met and target_lp.unmet_prerequisite_lesson_ids:
                    first_unmet_id = target_lp.unmet_prerequisite_lesson_ids[0]
                    first_unmet_lesson = lesson_by_id[first_unmet_id]
                    return NextLessonRecommendation(
                        learner_id=learner_id,
                        curriculum_id=plan.curriculum_id,
                        recommendation_type="COMPLETE_PREREQUISITE_FIRST",
                        recommended_lesson_id=first_unmet_lesson.lesson_id,
                        recommended_concept_id=first_unmet_lesson.concept_id,
                        recommended_lesson_title=first_unmet_lesson.title,
                        rationale=(
                            f"Lesson `{target_lesson.title}` (`{target_lesson.lesson_id}`) has unmet prerequisites "
                            f"{list(target_lp.unmet_prerequisite_lesson_ids)}. Complete `{first_unmet_lesson.title}` first."
                        ),
                        unmet_prerequisite_lesson_ids=target_lp.unmet_prerequisite_lesson_ids,
                        focus_objectives=first_unmet_lesson.learning_objectives,
                        recommended_action=f"Complete prerequisite lesson `{first_unmet_lesson.lesson_id}` before starting `{target_lesson.lesson_id}`.",
                    )

        # Case 1: Active lesson with knowledge gaps that needs remediation
        for lp in progress_list:
            if lp.status in (LessonProgressStatus.PRACTICING, LessonProgressStatus.IN_PROGRESS) and lp.knowledge_gaps:
                if not lp.prerequisites_met and lp.unmet_prerequisite_lesson_ids:
                    prereq_id = lp.unmet_prerequisite_lesson_ids[0]
                    prereq_lesson = lesson_by_id[prereq_id]
                    return NextLessonRecommendation(
                        learner_id=learner_id,
                        curriculum_id=plan.curriculum_id,
                        recommendation_type="COMPLETE_PREREQUISITE_FIRST",
                        recommended_lesson_id=prereq_lesson.lesson_id,
                        recommended_concept_id=prereq_lesson.concept_id,
                        recommended_lesson_title=prereq_lesson.title,
                        rationale=(
                            f"You encountered knowledge gaps in `{lp.title}` while prerequisite `{prereq_lesson.title}` "
                            "is still incomplete. Solidify the prerequisite foundation first."
                        ),
                        unmet_prerequisite_lesson_ids=lp.unmet_prerequisite_lesson_ids,
                        focus_objectives=prereq_lesson.learning_objectives,
                        recommended_action=f"Master prerequisite `{prereq_lesson.lesson_id}` and then retry `{lp.lesson_id}`.",
                    )
                lesson = lesson_by_id[lp.lesson_id]
                remaining_objs = tuple(
                    obj for obj in lesson.learning_objectives if obj not in lp.demonstrated_objectives
                ) or lesson.learning_objectives
                return NextLessonRecommendation(
                    learner_id=learner_id,
                    curriculum_id=plan.curriculum_id,
                    recommendation_type="REMEDIATE_KNOWLEDGE_GAPS",
                    recommended_lesson_id=lp.lesson_id,
                    recommended_concept_id=lp.concept_id,
                    recommended_lesson_title=lp.title,
                    rationale=(
                        f"Lesson `{lp.title}` is in `{lp.status.value}` with {len(lp.knowledge_gaps)} active knowledge gap(s). "
                        "Use the Teaching Agent to review the flagged concepts and re-submit the exercise."
                    ),
                    unmet_prerequisite_lesson_ids=lp.unmet_prerequisite_lesson_ids,
                    focus_objectives=remaining_objs,
                    recommended_action=f"Review `{lp.lesson_id}` with the Teaching Agent and resolve remaining exercises.",
                )

        # Case 2: Active lesson in PRACTICING or IN_PROGRESS with prerequisites met
        for lp in progress_list:
            if lp.status in (LessonProgressStatus.PRACTICING, LessonProgressStatus.IN_PROGRESS) and lp.prerequisites_met:
                lesson = lesson_by_id[lp.lesson_id]
                remaining_objs = tuple(
                    obj for obj in lesson.learning_objectives if obj not in lp.demonstrated_objectives
                ) or lesson.learning_objectives
                return NextLessonRecommendation(
                    learner_id=learner_id,
                    curriculum_id=plan.curriculum_id,
                    recommendation_type="CONTINUE_ACTIVE_LESSON",
                    recommended_lesson_id=lp.lesson_id,
                    recommended_concept_id=lp.concept_id,
                    recommended_lesson_title=lp.title,
                    rationale=(
                        f"Lesson `{lp.title}` is currently `{lp.status.value}` ({len(lp.completed_exercise_ids)}/{lp.total_exercises_count} "
                        f"exercises passed, {len(lp.demonstrated_objectives)}/{lp.total_objectives_count} objectives demonstrated). "
                        "Complete the remaining exercises to demonstrate full lesson mastery."
                    ),
                    unmet_prerequisite_lesson_ids=(),
                    focus_objectives=remaining_objs,
                    recommended_action=f"Complete remaining exercises in `{lp.lesson_id}`.",
                )

        # Case 3: Next unlocked NOT_STARTED lesson in topological prerequisite order
        for lp in progress_list:
            if lp.status == LessonProgressStatus.NOT_STARTED and lp.prerequisites_met:
                lesson = lesson_by_id[lp.lesson_id]
                return NextLessonRecommendation(
                    learner_id=learner_id,
                    curriculum_id=plan.curriculum_id,
                    recommendation_type="START_NEXT_UNLOCKED_LESSON",
                    recommended_lesson_id=lp.lesson_id,
                    recommended_concept_id=lp.concept_id,
                    recommended_lesson_title=lp.title,
                    rationale=(
                        f"All prerequisites for `{lp.title}` are satisfied. "
                        f"Ordering rationale: {lesson.rationale_for_ordering}"
                    ),
                    unmet_prerequisite_lesson_ids=(),
                    focus_objectives=lesson.learning_objectives,
                    recommended_action=f"Open `{lp.lesson_id}`, ask the Teaching Agent for an overview, and complete its exercises.",
                )

        # Case 4: All lessons COMPLETED
        return NextLessonRecommendation(
            learner_id=learner_id,
            curriculum_id=plan.curriculum_id,
            recommendation_type="CURRICULUM_COMPLETED",
            recommended_lesson_id=None,
            recommended_concept_id=None,
            recommended_lesson_title=None,
            rationale="Congratulations—all lessons and learning objectives across this curriculum have been demonstrated and completed.",
            unmet_prerequisite_lesson_ids=(),
            focus_objectives=(),
            recommended_action="Review completed projects or generate a specialized frontier curriculum.",
        )


class PlatformOrchestrator:
    """Controlled orchestrator managing workflow state, step tracing, and tool permissions."""

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        chroma_dir: Optional[Path] = None,
        progress_db_path: Optional[Path | str] = None,
        execution_policy: Optional[ExecutionPolicy] = None,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_RUNTIME_DB_PATH).resolve()
        self.chroma_dir = Path(chroma_dir or DEFAULT_CHROMA_DIR).resolve()
        self.execution_policy = execution_policy or ExecutionPolicy()

        self.ecosystem_service = SourceEcosystemService(self.db_path)
        self.concept_graph = build_default_concept_graph()
        self.validator = CurriculumValidator(self.ecosystem_service)
        self.progress_store = LearningProgressStore(progress_db_path)
        self.tool_registry = ToolRegistry()
        self._runs: Dict[str, WorkflowRunState] = {}
        self._curricula: Dict[str, CurriculumPlan] = {}
        for persisted_plan in self.progress_store.list_persisted_curricula():
            self._curricula[persisted_plan.curriculum_id] = persisted_plan
        self._register_default_tools()

        self.learning_planner_agent = LearningPlannerAgent(
            tool_registry=self.tool_registry,
            validator=self.validator,
            concept_graph=self.concept_graph,
        )
        self.teaching_agent = TeachingAgent(
            tool_registry=self.tool_registry,
            ecosystem_service=self.ecosystem_service,
            concept_graph=self.concept_graph,
        )
        self.assessment_agent = AssessmentAgent(
            tool_registry=self.tool_registry,
            concept_graph=self.concept_graph,
        )
        self.learning_progress_agent = LearningProgressAgent(
            tool_registry=self.tool_registry,
            progress_store=self.progress_store,
            assessment_agent=self.assessment_agent,
            concept_graph=self.concept_graph,
        )

    def _search_knowledge_chunks(self, *, query_text: str, top_k: int = 6) -> List[RetrievedChunk]:
        if not self.chroma_dir.exists():
            return []
        with ReadOnlySQLiteStore(self.db_path) as ro_store:
            vector_store = ChromaPersistentVectorStore(
                self.chroma_dir,
                collection_name=DEFAULT_CHROMA_COLLECTION,
                create_if_missing=False,
            )
            if vector_store.count() == 0:
                return []
            pipeline = LocalRAGPipeline(
                store=ro_store,  # type: ignore[arg-type]
                vector_store=vector_store,
                embedding_provider=DeterministicHashEmbeddingProvider(),
                generation_provider=RetrievalOnlyFallbackGenerationProvider(),
            )
            return pipeline.retrieve(
                query_text,
                top_k=top_k,
                min_similarity_score=0.05,
            )

    def _register_default_tools(self) -> None:
        self.tool_registry.register(
            ToolDefinition(
                name="inspect_source_ecosystem",
                description="Inspect the 5 epistemic tiers of the knowledge store and SRC-THE-BATCH governance state.",
                allowed_capabilities=(
                    AgentCapability.RESEARCH,
                    AgentCapability.KNOWLEDGE_CURATOR,
                    AgentCapability.LEARNING_PLANNER,
                ),
                read_only=True,
                handler=self.ecosystem_service.summarize_ecosystem,
            )
        )
        self.tool_registry.register(
            ToolDefinition(
                name="list_approved_resources",
                description="List approved resources in SQLite with their classified EvidenceTier.",
                allowed_capabilities=(
                    AgentCapability.KNOWLEDGE_CURATOR,
                    AgentCapability.LEARNING_PLANNER,
                ),
                read_only=True,
                handler=self.ecosystem_service.list_approved_resources,
            )
        )
        self.tool_registry.register(
            ToolDefinition(
                name="list_discovered_candidates",
                description="List unverified discovered link candidates (Tier 1: DISCOVERED_LINK).",
                allowed_capabilities=(AgentCapability.KNOWLEDGE_CURATOR,),
                read_only=True,
                handler=self.ecosystem_service.list_discovered_candidates,
            )
        )
        self.tool_registry.register(
            ToolDefinition(
                name="query_concept_graph",
                description="Select prerequisite-ordered concepts from the platform ConceptGraph.",
                allowed_capabilities=(
                    AgentCapability.LEARNING_PLANNER,
                    AgentCapability.TEACHING,
                    AgentCapability.LEARNING_PROGRESS,
                ),
                read_only=True,
                handler=self.concept_graph.select_concepts_for_goal,
            )
        )
        self.tool_registry.register(
            ToolDefinition(
                name="search_knowledge_chunks",
                description="Retrieve hybrid semantic + lexical knowledge chunks from Chroma and SQLite.",
                allowed_capabilities=(
                    AgentCapability.RESEARCH,
                    AgentCapability.LEARNING_PLANNER,
                    AgentCapability.TEACHING,
                    AgentCapability.ASSESSMENT,
                ),
                read_only=True,
                handler=self._search_knowledge_chunks,
            )
        )
        self.tool_registry.register(
            ToolDefinition(
                name="read_curriculum_plan",
                description="Read a validated CurriculumPlan by curriculum_id.",
                allowed_capabilities=(
                    AgentCapability.TEACHING,
                    AgentCapability.ASSESSMENT,
                    AgentCapability.LEARNING_PROGRESS,
                ),
                read_only=True,
                handler=self.get_curriculum,
            )
        )

    def _create_run_state(
        self,
        *,
        capability: AgentCapability,
        goal_or_context: str,
        curriculum_id: Optional[str] = None,
        policy_override: Optional[ExecutionPolicy] = None,
    ) -> Tuple[WorkflowRunState, Callable[[str, str, Dict[str, Any], Callable[[], Any]], Any]]:
        policy = policy_override or self.execution_policy
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_hash = hashlib.sha256(
            f"{capability.value}:{goal_or_context}:{len(self._runs)}:{now_iso}".encode("utf-8")
        ).hexdigest()[:12]
        run_id = f"run_{run_hash}"

        run_state = WorkflowRunState(
            run_id=run_id,
            capability=capability,
            status=WorkflowStatus.RUNNING,
            learner_goal=goal_or_context,
            created_at=now_iso,
            updated_at=now_iso,
            execution_policy=policy,
            curriculum_id=curriculum_id,
        )
        self._runs[run_id] = run_state

        def _record_step(
            step_name: str,
            actor: str,
            metadata: Dict[str, Any],
            fn: Callable[[], Any],
        ) -> Any:
            started_ts = time.perf_counter()
            started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            step_idx = len(run_state.steps) + 1
            try:
                result = fn()
                elapsed_ms = round((time.perf_counter() - started_ts) * 1000.0, 3)
                completed_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                run_state.steps.append(
                    WorkflowStepRecord(
                        step_index=step_idx,
                        step_name=step_name,
                        actor=actor,
                        status="COMPLETED",
                        started_at=started_iso,
                        completed_at=completed_iso,
                        duration_ms=elapsed_ms,
                        summary=f"Step '{step_name}' completed by {actor}.",
                        metadata=metadata,
                    )
                )
                run_state.updated_at = completed_iso
                return result
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started_ts) * 1000.0, 3)
                completed_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                run_state.steps.append(
                    WorkflowStepRecord(
                        step_index=step_idx,
                        step_name=step_name,
                        actor=actor,
                        status="FAILED",
                        started_at=started_iso,
                        completed_at=completed_iso,
                        duration_ms=elapsed_ms,
                        summary=f"Step '{step_name}' failed: {exc}",
                        metadata=metadata,
                    )
                )
                run_state.updated_at = completed_iso
                raise

        return run_state, _record_step

    def run_learning_planner(
        self,
        learner_goal: str,
        *,
        policy_override: Optional[ExecutionPolicy] = None,
    ) -> Tuple[WorkflowRunState, Optional[CurriculumPlan]]:
        """Execute a controlled Learning Planner workflow run and store its state."""
        goal_preview = str(learner_goal) if isinstance(learner_goal, str) else ""
        run_state, _record_step = self._create_run_state(
            capability=AgentCapability.LEARNING_PLANNER,
            goal_or_context=goal_preview,
            policy_override=policy_override,
        )

        try:
            plan = self.learning_planner_agent.execute(
                run_state=run_state,
                record_step=_record_step,
            )
            self._curricula[plan.curriculum_id] = plan
            self.progress_store.save_curriculum(plan)
            run_state.curriculum_id = plan.curriculum_id
            run_state.validation_summary = (
                plan.validation_summary if hasattr(plan, "validation_summary") else plan.validation_report.to_dict()
            )
            if plan.status == CurriculumStatus.ABSTAINED_INSUFFICIENT_EVIDENCE:
                run_state.status = WorkflowStatus.ABSTAINED
            else:
                run_state.status = WorkflowStatus.COMPLETED
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return run_state, plan
        except PlatformValidationError as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = exc.code
            run_state.error_message = exc.message
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise
        except Exception as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = "WORKFLOW_EXECUTION_ERROR"
            run_state.error_message = str(exc)
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise

    def open_lesson(
        self,
        *,
        curriculum_id: str,
        lesson_id: str,
        learner_id: str = DEFAULT_LEARNER_ID,
    ) -> Dict[str, Any]:
        """Open a lesson in the interactive workspace and transition NOT_STARTED -> IN_PROGRESS."""
        plan = self.get_curriculum(curriculum_id)
        if plan is None:
            raise PlatformValidationError(
                "CURRICULUM_NOT_FOUND",
                f"Curriculum '{curriculum_id}' was not found.",
                {"curriculum_id": curriculum_id},
            )
        if plan.status != CurriculumStatus.VALIDATED:
            raise PlatformValidationError(
                "CURRICULUM_NOT_VALIDATED",
                f"Curriculum '{curriculum_id}' has status '{plan.status.value}' and cannot be opened.",
                {"curriculum_id": curriculum_id, "status": plan.status.value},
            )
        lesson = _find_lesson_in_plan(plan, lesson_id)
        if lesson is None:
            raise PlatformValidationError(
                "LESSON_NOT_FOUND",
                f"Lesson '{lesson_id}' does not exist in curriculum '{curriculum_id}'.",
                {"curriculum_id": curriculum_id, "lesson_id": lesson_id},
            )
        cleaned_learner = str(learner_id or DEFAULT_LEARNER_ID).strip() or DEFAULT_LEARNER_ID
        lesson_progress = self.learning_progress_agent.record_lesson_opened(
            learner_id=cleaned_learner,
            plan=plan,
            lesson=lesson,
        )
        exercises = self.assessment_agent.build_exercises_for_lesson(plan, lesson)
        summary = self.learning_progress_agent.compute_curriculum_progress(
            learner_id=cleaned_learner,
            plan=plan,
            target_lesson_id=lesson.lesson_id,
        )
        return {
            "curriculum_id": curriculum_id,
            "lesson": lesson.to_dict(),
            "exercises": [e.to_dict() for e in exercises],
            "progress": lesson_progress.to_dict(),
            "next_recommendation": summary.next_recommendation.to_dict(),
        }

    def run_teaching_session(
        self,
        *,
        curriculum_id: Any,
        lesson_id: Any,
        learner_question: Any,
        explanation_depth: Any = "intermediate",
        learner_id: Any = DEFAULT_LEARNER_ID,
    ) -> Tuple[WorkflowRunState, TeachingResponse]:
        """Execute a controlled Teaching Agent interaction and update persistent lesson progress."""
        request = validate_teaching_request(
            curriculum_id=curriculum_id,
            lesson_id=lesson_id,
            learner_question=learner_question,
            explanation_depth=explanation_depth,
            learner_id=learner_id,
        )
        run_state, _record_step = self._create_run_state(
            capability=AgentCapability.TEACHING,
            goal_or_context=f"{request.lesson_id}: {request.learner_question}",
            curriculum_id=request.curriculum_id,
        )
        try:
            response = self.teaching_agent.execute(
                run_state=run_state,
                request=request,
                record_step=_record_step,
            )
            plan = self.get_curriculum(request.curriculum_id)
            assert plan is not None
            lesson = _find_lesson_in_plan(plan, request.lesson_id)
            assert lesson is not None
            self.learning_progress_agent.record_teaching_turn(
                plan=plan,
                lesson=lesson,
                response=response,
            )
            run_state.status = (
                WorkflowStatus.ABSTAINED
                if response.status == "INSUFFICIENT_EVIDENCE"
                else WorkflowStatus.COMPLETED
            )
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return run_state, response
        except PlatformValidationError as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = exc.code
            run_state.error_message = exc.message
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise
        except Exception as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = "WORKFLOW_EXECUTION_ERROR"
            run_state.error_message = str(exc)
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise

    def get_lesson_exercises(
        self,
        *,
        curriculum_id: str,
        lesson_id: str,
    ) -> Tuple[AssessmentExercise, ...]:
        run_state, _record_step = self._create_run_state(
            capability=AgentCapability.ASSESSMENT,
            goal_or_context=f"generate_exercises:{lesson_id}",
            curriculum_id=curriculum_id,
        )
        try:
            exercises = self.assessment_agent.generate_exercises(
                run_state=run_state,
                curriculum_id=curriculum_id,
                lesson_id=lesson_id,
                record_step=_record_step,
            )
            run_state.status = WorkflowStatus.COMPLETED
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return exercises
        except PlatformValidationError as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = exc.code
            run_state.error_message = exc.message
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise

    def run_assessment_evaluation(
        self,
        *,
        curriculum_id: Any,
        lesson_id: Any,
        exercise_id: Any,
        submitted_answer: Any,
        learner_id: Any = DEFAULT_LEARNER_ID,
    ) -> Tuple[WorkflowRunState, AssessmentEvaluation, CurriculumProgressSummary]:
        """Evaluate an exercise submission with AssessmentAgent and update LearningProgressAgent."""
        cleaned_learner = str(learner_id or DEFAULT_LEARNER_ID).strip() or DEFAULT_LEARNER_ID
        run_state, _record_step = self._create_run_state(
            capability=AgentCapability.ASSESSMENT,
            goal_or_context=f"assess:{lesson_id}:{exercise_id}",
            curriculum_id=str(curriculum_id) if isinstance(curriculum_id, str) else None,
        )
        try:
            evaluation = self.assessment_agent.evaluate_submission(
                run_state=run_state,
                curriculum_id=curriculum_id,
                lesson_id=lesson_id,
                exercise_id=exercise_id,
                submitted_answer=submitted_answer,
                learner_id=cleaned_learner,
                record_step=_record_step,
            )
            plan = self.get_curriculum(evaluation.curriculum_id)
            assert plan is not None
            lesson = _find_lesson_in_plan(plan, evaluation.lesson_id)
            assert lesson is not None
            self.learning_progress_agent.record_assessment_evaluation(
                plan=plan,
                lesson=lesson,
                evaluation=evaluation,
            )
            summary = self.learning_progress_agent.compute_curriculum_progress(
                learner_id=cleaned_learner,
                plan=plan,
            )
            run_state.status = WorkflowStatus.COMPLETED
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return run_state, evaluation, summary
        except PlatformValidationError as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = exc.code
            run_state.error_message = exc.message
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise
        except Exception as exc:
            run_state.status = WorkflowStatus.FAILED
            run_state.error_code = "WORKFLOW_EXECUTION_ERROR"
            run_state.error_message = str(exc)
            run_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raise

    def get_curriculum_progress(
        self,
        *,
        curriculum_id: str,
        learner_id: str = DEFAULT_LEARNER_ID,
        target_lesson_id: Optional[str] = None,
    ) -> CurriculumProgressSummary:
        plan = self.get_curriculum(curriculum_id)
        if plan is None:
            raise PlatformValidationError(
                "CURRICULUM_NOT_FOUND",
                f"Curriculum '{curriculum_id}' was not found.",
                {"curriculum_id": curriculum_id},
            )
        cleaned_learner = str(learner_id or DEFAULT_LEARNER_ID).strip() or DEFAULT_LEARNER_ID
        return self.learning_progress_agent.compute_curriculum_progress(
            learner_id=cleaned_learner,
            plan=plan,
            target_lesson_id=target_lesson_id,
        )

    def get_run(self, run_id: str) -> Optional[WorkflowRunState]:
        return self._runs.get(run_id)

    def get_curriculum(self, curriculum_id: str) -> Optional[CurriculumPlan]:
        if curriculum_id in self._curricula:
            return self._curricula[curriculum_id]
        loaded = self.progress_store.load_curriculum(curriculum_id)
        if loaded is not None:
            self._curricula[curriculum_id] = loaded
        return loaded

    def list_curricula(self) -> List[CurriculumPlan]:
        for persisted_plan in self.progress_store.list_persisted_curricula():
            if persisted_plan.curriculum_id not in self._curricula:
                self._curricula[persisted_plan.curriculum_id] = persisted_plan
        return list(self._curricula.values())


class CurriculumManagementService:
    """Application service used by the Web Application, HTTP API (`api.py`), and CLI (`platform.py`)."""

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        chroma_dir: Optional[Path] = None,
        progress_db_path: Optional[Path | str] = None,
        orchestrator: Optional[PlatformOrchestrator] = None,
    ) -> None:
        self.orchestrator = orchestrator or PlatformOrchestrator(
            db_path=db_path,
            chroma_dir=chroma_dir,
            progress_db_path=progress_db_path,
        )

    def list_agents(self) -> Dict[str, Any]:
        catalog = get_logical_agent_catalog()
        return {
            "total": len(catalog),
            "agents": [spec.to_dict() for spec in catalog],
        }

    def generate_curriculum(self, learner_goal: str) -> Dict[str, Any]:
        run_state, plan = self.orchestrator.run_learning_planner(learner_goal)
        assert plan is not None
        return {
            "run": run_state.to_dict(),
            "curriculum": plan.to_dict(),
        }

    def get_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        run_state = self.orchestrator.get_run(run_id)
        return run_state.to_dict() if run_state is not None else None

    def list_curricula(self, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        plans = self.orchestrator.list_curricula()
        sliced = plans[offset : offset + limit]
        items = [
            {
                "curriculum_id": p.curriculum_id,
                "run_id": p.run_id,
                "status": p.status.value,
                "created_at": p.created_at,
                "learner_goal": p.learner_goal,
                "title": p.title,
                "stage_count": len(p.stages),
                "lesson_count": sum(len(s.lessons) for s in p.stages),
                "citation_count": len(p.source_citations),
                "evidence_gap_count": len(p.evidence_gaps),
            }
            for p in sliced
        ]
        return {
            "total": len(plans),
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    def get_curriculum(self, curriculum_id: str) -> Optional[Dict[str, Any]]:
        plan = self.orchestrator.get_curriculum(curriculum_id)
        return plan.to_dict() if plan is not None else None

    def get_curriculum_sources(self, curriculum_id: str) -> Optional[Dict[str, Any]]:
        plan = self.orchestrator.get_curriculum(curriculum_id)
        if plan is None:
            return None
        citations_by_tier: Dict[str, List[Dict[str, Any]]] = {}
        for cit in plan.source_citations:
            citations_by_tier.setdefault(cit.evidence_tier.value, []).append(cit.to_dict())
        return {
            "curriculum_id": plan.curriculum_id,
            "status": plan.status.value,
            "total_citations": len(plan.source_citations),
            "source_ecosystem_summary": plan.source_ecosystem_summary.to_dict(),
            "citations_by_tier": citations_by_tier,
            "source_citations": [c.to_dict() for c in plan.source_citations],
            "evidence_gaps": [g.to_dict() for g in plan.evidence_gaps],
        }

    def get_lesson_detail(
        self,
        curriculum_id: str,
        lesson_id: str,
        *,
        learner_id: str = DEFAULT_LEARNER_ID,
    ) -> Dict[str, Any]:
        plan = self.orchestrator.get_curriculum(curriculum_id)
        if plan is None:
            raise PlatformValidationError(
                "CURRICULUM_NOT_FOUND",
                f"Curriculum '{curriculum_id}' was not found.",
                {"curriculum_id": curriculum_id},
            )
        lesson = _find_lesson_in_plan(plan, lesson_id)
        if lesson is None:
            raise PlatformValidationError(
                "LESSON_NOT_FOUND",
                f"Lesson '{lesson_id}' does not exist in curriculum '{curriculum_id}'.",
                {"curriculum_id": curriculum_id, "lesson_id": lesson_id},
            )
        cleaned_learner = str(learner_id or DEFAULT_LEARNER_ID).strip() or DEFAULT_LEARNER_ID
        exercises = self.orchestrator.assessment_agent.build_exercises_for_lesson(plan, lesson)
        progress = self.orchestrator.learning_progress_agent.get_lesson_progress(
            learner_id=cleaned_learner,
            plan=plan,
            lesson=lesson,
        )
        interactions = self.orchestrator.progress_store.list_teaching_interactions(
            learner_id=cleaned_learner,
            curriculum_id=plan.curriculum_id,
            lesson_id=lesson.lesson_id,
        )
        summary = self.orchestrator.learning_progress_agent.compute_curriculum_progress(
            learner_id=cleaned_learner,
            plan=plan,
            target_lesson_id=lesson.lesson_id,
        )
        return {
            "curriculum_id": plan.curriculum_id,
            "curriculum_title": plan.title,
            "lesson": lesson.to_dict(),
            "exercises": [e.to_dict() for e in exercises],
            "progress": progress.to_dict(),
            "teaching_history": interactions,
            "next_recommendation": summary.next_recommendation.to_dict(),
        }

    def open_lesson(
        self,
        curriculum_id: str,
        lesson_id: str,
        *,
        learner_id: str = DEFAULT_LEARNER_ID,
    ) -> Dict[str, Any]:
        return self.orchestrator.open_lesson(
            curriculum_id=curriculum_id,
            lesson_id=lesson_id,
            learner_id=learner_id,
        )

    def teach_lesson(
        self,
        curriculum_id: str,
        lesson_id: str,
        *,
        learner_question: Any,
        explanation_depth: Any = "intermediate",
        learner_id: str = DEFAULT_LEARNER_ID,
    ) -> Dict[str, Any]:
        run_state, response = self.orchestrator.run_teaching_session(
            curriculum_id=curriculum_id,
            lesson_id=lesson_id,
            learner_question=learner_question,
            explanation_depth=explanation_depth,
            learner_id=learner_id,
        )
        summary = self.orchestrator.get_curriculum_progress(
            curriculum_id=curriculum_id,
            learner_id=response.learner_id,
            target_lesson_id=response.lesson_id,
        )
        lesson_prog = next(
            lp for lp in summary.lesson_progress if lp.lesson_id == response.lesson_id
        )
        return {
            "run": run_state.to_dict(),
            "teaching_response": response.to_dict(),
            "lesson_progress": lesson_prog.to_dict(),
            "progress": summary.to_dict(),
            "next_recommendation": summary.next_recommendation.to_dict(),
        }

    def list_lesson_exercises(self, curriculum_id: str, lesson_id: str) -> Dict[str, Any]:
        exercises = self.orchestrator.get_lesson_exercises(
            curriculum_id=curriculum_id,
            lesson_id=lesson_id,
        )
        return {
            "curriculum_id": curriculum_id,
            "lesson_id": exercises[0].lesson_id if exercises else lesson_id,
            "total": len(exercises),
            "exercises": [e.to_dict() for e in exercises],
        }

    def assess_lesson_exercise(
        self,
        curriculum_id: str,
        lesson_id: str,
        *,
        exercise_id: Any,
        submitted_answer: Any,
        learner_id: str = DEFAULT_LEARNER_ID,
    ) -> Dict[str, Any]:
        run_state, evaluation, summary = self.orchestrator.run_assessment_evaluation(
            curriculum_id=curriculum_id,
            lesson_id=lesson_id,
            exercise_id=exercise_id,
            submitted_answer=submitted_answer,
            learner_id=learner_id,
        )
        lesson_prog = next(
            lp for lp in summary.lesson_progress if lp.lesson_id == evaluation.lesson_id
        )
        return {
            "run": run_state.to_dict(),
            "evaluation": evaluation.to_dict(),
            "lesson_progress": lesson_prog.to_dict(),
            "progress": summary.to_dict(),
            "next_recommendation": summary.next_recommendation.to_dict(),
        }

    def get_curriculum_progress(
        self,
        curriculum_id: str,
        *,
        learner_id: str = DEFAULT_LEARNER_ID,
        target_lesson_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary = self.orchestrator.get_curriculum_progress(
            curriculum_id=curriculum_id,
            learner_id=learner_id,
            target_lesson_id=target_lesson_id,
        )
        d = summary.to_dict()
        return {"progress": dict(d), **d}

    def get_next_recommendation(
        self,
        curriculum_id: str,
        *,
        learner_id: str = DEFAULT_LEARNER_ID,
        target_lesson_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary = self.orchestrator.get_curriculum_progress(
            curriculum_id=curriculum_id,
            learner_id=learner_id,
            target_lesson_id=target_lesson_id,
        )
        rec = summary.next_recommendation.to_dict()
        return {"recommendation": dict(rec), **rec}


# ---------------------------------------------------------------------------
# Formatting & CLI Entrypoint
# ---------------------------------------------------------------------------


def format_curriculum_plan(plan: CurriculumPlan, output_format: str = "markdown") -> str:
    """Format a CurriculumPlan as structured JSON or readable Markdown."""
    fmt = output_format.strip().lower()
    if fmt == "json":
        return json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)
    if fmt not in ("markdown", "md", "text"):
        raise ValueError(f"Unsupported curriculum output format: '{output_format}'")

    lines: List[str] = [
        f"# {plan.title}",
        "",
        f"- **Curriculum ID:** `{plan.curriculum_id}`",
        f"- **Workflow Run ID:** `{plan.run_id}`",
        f"- **Schema Version:** `{plan.schema_version}`",
        f"- **Status:** `{plan.status.value}`",
        f"- **Created At:** `{plan.created_at}`",
        f"- **Learner Goal:** {plan.learner_goal}",
        "",
        "## Executive Summary",
        "",
        plan.executive_summary,
        "",
        "> **Mastery Assessment Disclaimer:** " + plan.mastery_assessment_disclaimer,
        "",
        "## Rationale for Stage & Prerequisite Ordering",
        "",
        plan.rationale_for_ordering,
        "",
        "## Source Ecosystem & Epistemic Tier Summary",
        "",
        f"- **Curriculum-Ready Knowledge Chunks (`CURRICULUM_READY_EVIDENCE`):** {plan.source_ecosystem_summary.curriculum_ready_chunk_count} active chunks across {plan.source_ecosystem_summary.extracted_resource_count} extracted primary documents",
        f"- **Verified Collected Documents (`COLLECTED_DOCUMENT`):** {plan.source_ecosystem_summary.collected_document_count} resources",
        f"- **Approved Uncollected Seed Resources (`APPROVED_UNCOLLECTED_RESOURCE`):** {plan.source_ecosystem_summary.approved_uncollected_resource_count} resources",
        f"- **Discovered Link Candidates (`DISCOVERED_LINK`):** {plan.source_ecosystem_summary.discovered_link_candidate_count} candidates (`PENDING_REVIEW`, excluded from citations)",
        f"- **The Batch (`{THE_BATCH_SOURCE_ID}`):** status=`{plan.source_ecosystem_summary.the_batch_governance.get('status')}`, role=`{plan.source_ecosystem_summary.the_batch_governance.get('role_in_platform')}`",
        "",
    ]

    if plan.abstention_reason:
        lines.extend(
            [
                "## Abstention Notice",
                "",
                plan.abstention_reason,
                "",
            ]
        )

    if plan.stages:
        lines.extend(["## Ordered Learning Path", ""])
        for stage in plan.stages:
            prereq_stages = (
                ", ".join(f"`{s}`" for s in stage.prerequisite_stage_ids)
                if stage.prerequisite_stage_ids
                else "None (Entry Stage)"
            )
            lines.extend(
                [
                    f"### {stage.title} (`{stage.stage_id}`)",
                    "",
                    f"- **Stage Order:** {stage.stage_order}",
                    f"- **Prerequisite Stages:** {prereq_stages}",
                    f"- **Ordering Rationale:** {stage.rationale_for_ordering}",
                    "",
                ]
            )
            for lesson in stage.lessons:
                prereqs = (
                    ", ".join(f"`{p}`" for p in lesson.prerequisites)
                    if lesson.prerequisites
                    else "None (Foundational Entry Lesson)"
                )
                prereq_lessons = (
                    ", ".join(f"`{p}`" for p in lesson.prerequisite_lesson_ids)
                    if lesson.prerequisite_lesson_ids
                    else "None"
                )
                lines.extend(
                    [
                        f"#### Lesson {lesson.lesson_order}: {lesson.title} (`{lesson.lesson_id}`)",
                        "",
                        f"- **Concept ID:** `{lesson.concept_id}`",
                        f"- **Prerequisite Concepts:** {prereqs}",
                        f"- **Prerequisite Lessons:** {prereq_lessons}",
                        f"- **Ordering Rationale:** {lesson.rationale_for_ordering}",
                        "",
                        "**Learning Objectives:**",
                    ]
                )
                for obj in lesson.learning_objectives:
                    lines.append(f"- {obj}")
                lines.extend(
                    [
                        "",
                        "**Explanation & Grounded Evidence:**",
                        "",
                        lesson.explanation,
                        "",
                        "**Exercises:**",
                    ]
                )
                for ex in lesson.exercises:
                    lines.append(
                        f"- `{ex['exercise_id']}` — **{ex['title']}**: {ex['description']} (*Deliverable:* {ex['deliverable']})"
                    )
                lines.extend(["", "**Projects:**"])
                for proj in lesson.projects:
                    lines.append(
                        f"- `{proj['project_id']}` — **{proj['title']}**: {proj['description']} (*Deliverable:* {proj['deliverable']})"
                    )
                lines.extend(
                    [
                        "",
                        f"**Mastery Check Design (`{lesson.mastery_check.check_id}`, status: `{lesson.mastery_check.assessment_status}`):**",
                        f"- *Disclaimer:* {lesson.mastery_check.disclaimer}",
                        "- *Prompts:*",
                    ]
                )
                for prompt in lesson.mastery_check.prompts:
                    lines.append(f"  - {prompt}")
                lines.append("- *Pass Criteria:*")
                for crit in lesson.mastery_check.pass_criteria:
                    lines.append(f"  - {crit}")
                lines.extend(["", "**Source Citations:**"])
                for cit in lesson.source_citations:
                    chunk_label = f", chunk=`{cit.chunk_id}`" if cit.chunk_id else ""
                    lines.append(
                        f"- `{cit.citation_id}` [{cit.evidence_tier.value}] **{cit.title}** (`{cit.source_id}` / `{cit.resource_id}`{chunk_label}) — <{cit.canonical_url}>"
                    )
                if lesson.evidence_gaps:
                    lines.extend(["", "**Lesson Evidence Gaps:**"])
                    for lgap in lesson.evidence_gaps:
                        lines.append(f"- {lgap}")
                lines.append("")

    lines.extend(["## Evidence Gaps & Curation Backlog", ""])
    for gap in plan.evidence_gaps:
        src_str = ", ".join(f"`{s}`" for s in gap.related_source_ids) if gap.related_source_ids else "N/A"
        lines.extend(
            [
                f"- **`{gap.gap_id}`** (Scope: `{gap.scope}`, Tier: `{gap.current_evidence_tier}`, Sources: {src_str}): {gap.description}",
                f"  - *Recommended Action:* {gap.recommended_curation_action}",
            ]
        )

    lines.extend(
        [
            "",
            "## Deterministic Validation Report",
            "",
            f"- **Overall Valid:** `{plan.validation_report.is_valid}`",
            f"- **Schema Valid:** `{plan.validation_report.schema_valid}`",
            f"- **Prerequisites Consistent (DAG Topological Order):** `{plan.validation_report.prerequisites_consistent}`",
            f"- **Citations Verified Against SQLite & Chroma:** `{plan.validation_report.citations_verified}`",
            f"- **Mastery Disclaimer Present:** `{plan.validation_report.mastery_disclaimer_present}`",
            f"- **The Batch Governance Respected:** `{plan.validation_report.the_batch_policy_respected}`",
            f"- **Checked Lessons:** {plan.validation_report.checked_lesson_count}",
            f"- **Checked Lesson Citations:** {plan.validation_report.checked_citation_count}",
            "",
        ]
    )
    return "\n".join(lines)


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-learning-platform",
        description="Agent-First AI Learning Platform CLI (Curriculum Planner, Teaching, Assessment & Progress).",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default="I want to become an AI engineer and eventually an AI researcher, starting from mathematical and programming foundations.",
        help="Learner goal to plan a curriculum for.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (markdown or json).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to SQLite knowledge store (defaults to data/runtime/task012_pilot.sqlite).",
    )
    parser.add_argument(
        "--chroma-dir",
        type=Path,
        default=None,
        help="Path to Chroma vector store directory (defaults to data/runtime/chroma).",
    )
    parser.add_argument(
        "--progress-db",
        type=Path,
        default=None,
        help="Optional SQLite path for persistent learner progress and curricula.",
    )
    parser.add_argument(
        "--learner-id",
        type=str,
        default=DEFAULT_LEARNER_ID,
        help="Learner identifier for progress tracking.",
    )
    parser.add_argument(
        "--curriculum-id",
        type=str,
        default=None,
        help="Existing curriculum ID for teaching, assessment, or progress commands.",
    )
    parser.add_argument(
        "--lesson-id",
        type=str,
        default=None,
        help="Lesson ID for teaching or assessment commands.",
    )
    parser.add_argument(
        "--teach-question",
        type=str,
        default=None,
        help="Ask the Teaching Agent a question about the specified lesson.",
    )
    parser.add_argument(
        "--explanation-depth",
        type=str,
        default=ExplanationDepth.INTERMEDIATE.value,
        choices=[d.value for d in ExplanationDepth],
        help="Teaching explanation depth preference (does not imply learner mastery).",
    )
    parser.add_argument(
        "--exercise-id",
        type=str,
        default=None,
        help="Exercise ID to submit an answer for.",
    )
    parser.add_argument(
        "--submit-answer",
        type=str,
        default=None,
        help="Submitted answer text for the specified lesson exercise.",
    )
    parser.add_argument(
        "--show-progress",
        action="store_true",
        help="Display curriculum learning progress and next lesson recommendation.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to write structured JSON curriculum output.",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="Optional path to write Markdown curriculum output.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    try:
        orchestrator = PlatformOrchestrator(
            db_path=args.db_path,
            chroma_dir=args.chroma_dir,
            progress_db_path=args.progress_db,
        )
        curriculum_id = args.curriculum_id
        plan: Optional[CurriculumPlan] = None
        if not curriculum_id:
            _, plan = orchestrator.run_learning_planner(args.goal)
            assert plan is not None
            curriculum_id = plan.curriculum_id

        if args.teach_question is not None:
            if not args.lesson_id:
                raise PlatformValidationError(
                    "missing_lesson_id",
                    "--lesson-id is required when using --teach-question.",
                )
            _, teach_resp = orchestrator.run_teaching_session(
                curriculum_id=curriculum_id,
                lesson_id=args.lesson_id,
                learner_question=args.teach_question,
                explanation_depth=args.explanation_depth,
                learner_id=args.learner_id,
            )
            assert teach_resp is not None
            sys.stdout.write(json.dumps(teach_resp.to_dict(), indent=2, ensure_ascii=False) + "\n")
            return 0

        if args.submit_answer is not None:
            if not args.lesson_id or not args.exercise_id:
                raise PlatformValidationError(
                    "missing_assessment_args",
                    "--lesson-id and --exercise-id are required when using --submit-answer.",
                )
            _, eval_resp, progress_summary = orchestrator.run_assessment_evaluation(
                curriculum_id=curriculum_id,
                lesson_id=args.lesson_id,
                exercise_id=args.exercise_id,
                submitted_answer=args.submit_answer,
                learner_id=args.learner_id,
            )
            assert eval_resp is not None and progress_summary is not None
            payload = {
                "evaluation": eval_resp.to_dict(),
                "progress": progress_summary.to_dict(),
            }
            sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            return 0

        if args.show_progress:
            progress_summary = orchestrator.get_curriculum_progress(
                curriculum_id=curriculum_id,
                learner_id=args.learner_id,
            )
            sys.stdout.write(json.dumps(progress_summary.to_dict(), indent=2, ensure_ascii=False) + "\n")
            return 0

        if plan is None:
            plan = orchestrator.get_curriculum(curriculum_id)
            if plan is None:
                raise PlatformValidationError(
                    "curriculum_not_found",
                    f"Curriculum '{curriculum_id}' was not found.",
                )
        if args.output_json:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(
                format_curriculum_plan(plan, "json") + "\n",
                encoding="utf-8",
            )
        if args.output_md:
            args.output_md.parent.mkdir(parents=True, exist_ok=True)
            args.output_md.write_text(
                format_curriculum_plan(plan, "markdown") + "\n",
                encoding="utf-8",
            )
        sys.stdout.write(format_curriculum_plan(plan, args.format) + "\n")
        return 0
    except PlatformValidationError as exc:
        sys.stderr.write(f"Validation error [{exc.code}]: {exc.message}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"Execution error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
