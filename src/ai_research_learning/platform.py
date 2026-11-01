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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
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
            purpose="Deliver interactive lesson walkthroughs, intuitive explanations, and guided derivations.",
            allowed_tools=("read_curriculum_plan", "search_knowledge_chunks"),
            input_contract="LessonSessionRequest(curriculum_id, lesson_id, learner_prompt)",
            output_contract="LessonTurnResponse(explanation, citations, follow_up_prompts)",
            deterministic_guardrails=(
                "Lesson scope locked to validated CurriculumLesson objectives",
                "Strict chunk citation verification",
            ),
            milestone_status="CONTRACT_DEFINED_STAGE_2",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.ASSESSMENT,
            display_name="Assessment Agent",
            purpose="Evaluate learner exercise, project, and mastery check submissions against explicit rubrics.",
            allowed_tools=("read_curriculum_plan", "search_knowledge_chunks"),
            input_contract="AssessmentSubmission(curriculum_id, lesson_id, check_id, learner_submission)",
            output_contract="AssessmentEvaluation(score_band, rubric_breakdown, remediation_links)",
            deterministic_guardrails=(
                "Rubric criteria deterministically checked before LLM feedback",
                "Explicitly separated from curriculum design in Task 034",
            ),
            milestone_status="CONTRACT_DEFINED_STAGE_2",
        ),
        AgentCapabilitySpec(
            capability=AgentCapability.LEARNING_PROGRESS,
            display_name="Learning Progress Agent",
            purpose="Track learner mastery progression across the concept DAG and schedule spaced reviews.",
            allowed_tools=("read_curriculum_plan", "query_concept_graph"),
            input_contract="ProgressUpdateRequest(learner_id, curriculum_id, completed_lesson_ids)",
            output_contract="LearnerProgressState(unlocked_stage_ids, next_recommended_lesson_id)",
            deterministic_guardrails=(
                "Prerequisite unlock state computed deterministically from concept DAG",
            ),
            milestone_status="CONTRACT_DEFINED_STAGE_2",
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


class PlatformOrchestrator:
    """Controlled orchestrator managing workflow state, step tracing, and tool permissions."""

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        chroma_dir: Optional[Path] = None,
        execution_policy: Optional[ExecutionPolicy] = None,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_RUNTIME_DB_PATH).resolve()
        self.chroma_dir = Path(chroma_dir or DEFAULT_CHROMA_DIR).resolve()
        self.execution_policy = execution_policy or ExecutionPolicy()

        self.ecosystem_service = SourceEcosystemService(self.db_path)
        self.concept_graph = build_default_concept_graph()
        self.validator = CurriculumValidator(self.ecosystem_service)
        self.tool_registry = ToolRegistry()
        self._runs: Dict[str, WorkflowRunState] = {}
        self._curricula: Dict[str, CurriculumPlan] = {}
        self._register_default_tools()

        self.learning_planner_agent = LearningPlannerAgent(
            tool_registry=self.tool_registry,
            validator=self.validator,
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
                handler=lambda curriculum_id: self._curricula.get(curriculum_id),
            )
        )

    def run_learning_planner(
        self,
        learner_goal: str,
        *,
        policy_override: Optional[ExecutionPolicy] = None,
    ) -> Tuple[WorkflowRunState, Optional[CurriculumPlan]]:
        """Execute a controlled Learning Planner workflow run and store its state."""
        policy = policy_override or self.execution_policy
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        goal_preview = str(learner_goal) if isinstance(learner_goal, str) else ""
        run_hash = hashlib.sha256(
            f"{goal_preview}:{len(self._runs)}:{now_iso}".encode("utf-8")
        ).hexdigest()[:12]
        run_id = f"run_{run_hash}"

        run_state = WorkflowRunState(
            run_id=run_id,
            capability=AgentCapability.LEARNING_PLANNER,
            status=WorkflowStatus.RUNNING,
            learner_goal=goal_preview,
            created_at=now_iso,
            updated_at=now_iso,
            execution_policy=policy,
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

        try:
            plan = self.learning_planner_agent.execute(
                run_state=run_state,
                record_step=_record_step,
            )
            self._curricula[plan.curriculum_id] = plan
            run_state.curriculum_id = plan.curriculum_id
            run_state.validation_summary = plan.validation_summary if hasattr(plan, "validation_summary") else plan.validation_report.to_dict()
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

    def get_run(self, run_id: str) -> Optional[WorkflowRunState]:
        return self._runs.get(run_id)

    def get_curriculum(self, curriculum_id: str) -> Optional[CurriculumPlan]:
        return self._curricula.get(curriculum_id)

    def list_curricula(self) -> List[CurriculumPlan]:
        return list(self._curricula.values())


class CurriculumManagementService:
    """Application service used by both the HTTP API (`api.py`) and CLI (`platform.py`)."""

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        chroma_dir: Optional[Path] = None,
        orchestrator: Optional[PlatformOrchestrator] = None,
    ) -> None:
        self.orchestrator = orchestrator or PlatformOrchestrator(
            db_path=db_path,
            chroma_dir=chroma_dir,
        )

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
        description="Agent-First AI Learning Platform CLI (Curriculum Planner & Ecosystem Inspector).",
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
        )
        _, plan = orchestrator.run_learning_planner(args.goal)
        assert plan is not None
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
