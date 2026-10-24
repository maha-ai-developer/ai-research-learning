"""Policy-audit and bounded metadata-pilot coordinator.

Connects the decoupled `SourceAdapter` implementations to the existing SQLite
`KnowledgeStore` (`knowledge_store.py`) without adding adapter-specific logic
to `knowledge_store.py`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Sequence
import requests

from knowledge_store import DEFAULT_DB_PATH, DEFAULT_SEED_PATH, KnowledgeStore

from .arxiv_adapter import ArxivSourceAdapter
from .base import (
    DEFAULT_PROJECT_USER_AGENT,
    AdapterError,
    HttpRequestEvent,
    NormalizedResourceEnvelope,
    NotModifiedResponse,
    OriginRateLimiter,
    PolicyAuditResult,
    RateLimitError,
    SourceAdapter,
    SourceCandidate,
    utc_now,
)
from .html_adapters import (
    BengioSourceAdapter,
    HintonSourceAdapter,
    ThreeBlueOneBrownAdapter,
)
from .wikipedia_adapter import WikipediaSourceAdapter

MAX_PILOT_PER_SOURCE = 2
MAX_PILOT_TOTAL = 10


@dataclass
class PolicyAuditSummary:
    """Summary of a multi-source policy audit run."""

    run_id: str
    checked_at_utc: str
    policies: list[PolicyAuditResult] = field(default_factory=list)
    http_events: list[HttpRequestEvent] = field(default_factory=list)

    @property
    def permitted_count(self) -> int:
        return sum(1 for p in self.policies if p.is_metadata_collection_permitted)

    @property
    def blocked_or_unverified_count(self) -> int:
        return sum(1 for p in self.policies if not p.is_metadata_collection_permitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "checked_at_utc": self.checked_at_utc,
            "permitted_count": self.permitted_count,
            "blocked_or_unverified_count": self.blocked_or_unverified_count,
            "policies": [p.to_dict() for p in self.policies],
            "http_events": [asdict(e) for e in self.http_events],
        }


@dataclass
class PilotItemOutcome:
    """Outcome of a single candidate metadata collection attempt during a pilot."""

    source_id: str
    adapter_id: str
    requested_url: str
    canonical_url: str | None
    title: str | None
    status: str  # "inserted" | "updated" | "unchanged" | "not-modified-304" | "blocked" | "error"
    resource_id: str | None = None
    version_id: str | None = None
    version_count: int | None = None
    http_status: int | None = None
    error_class: str | None = None
    error_message: str | None = None
    discovered_link_count: int = 0
    metadata_completeness: dict[str, Any] = field(default_factory=dict)


@dataclass
class PilotRunSummary:
    """Summary of a bounded metadata pilot across all configured source adapters."""

    run_id: str
    started_at_utc: str
    finished_at_utc: str
    max_per_source: int
    max_total: int
    policies: list[PolicyAuditResult] = field(default_factory=list)
    outcomes: list[PilotItemOutcome] = field(default_factory=list)
    http_events: list[HttpRequestEvent] = field(default_factory=list)
    inserted_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    blocked_count: int = 0
    error_count: int = 0
    discovered_link_count: int = 0

    @property
    def fetched_count(self) -> int:
        return self.inserted_count + self.updated_count + self.unchanged_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "max_per_source": self.max_per_source,
            "max_total": self.max_total,
            "inserted_count": self.inserted_count,
            "updated_count": self.updated_count,
            "unchanged_count": self.unchanged_count,
            "fetched_count": self.fetched_count,
            "blocked_count": self.blocked_count,
            "error_count": self.error_count,
            "discovered_link_count": self.discovered_link_count,
            "policies": [p.to_dict() for p in self.policies],
            "outcomes": [asdict(o) for o in self.outcomes],
            "http_events": [asdict(e) for e in self.http_events],
        }


def build_default_adapters(
    *,
    user_agent: str = DEFAULT_PROJECT_USER_AGENT,
    session: requests.Session | None = None,
    rate_limiter: OriginRateLimiter | None = None,
) -> list[SourceAdapter]:
    """Instantiate the 5 canonical source adapters sharing a single OriginRateLimiter."""
    shared_session = session or requests.Session()
    shared_limiter = rate_limiter or OriginRateLimiter()
    kwargs = {
        "user_agent": user_agent,
        "session": shared_session,
        "rate_limiter": shared_limiter,
    }
    return [
        HintonSourceAdapter(**kwargs),
        BengioSourceAdapter(**kwargs),
        ThreeBlueOneBrownAdapter(**kwargs),
        ArxivSourceAdapter(**kwargs),
        WikipediaSourceAdapter(**kwargs),
    ]


def load_normalized_seed_manifest(manifest_path: Path | str | None = DEFAULT_SEED_PATH) -> list[dict[str, Any]]:
    """Load `seed_urls.json` into normalized candidate dicts if the file exists."""
    if manifest_path is None:
        return []
    resolved = Path(manifest_path)
    if not resolved.exists():
        return []
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "id": item.get("Manifest ID") or item.get("id"),
                "person_or_source": item.get("Person / source") or item.get("person_or_source"),
                "url": item.get("URL") or item.get("url"),
                "title": item.get("Page / resource name") or item.get("title"),
                "page_type": item.get("Resource type") or item.get("page_type"),
                "domain_scope": item.get("Domain scope") or item.get("domain_scope"),
                "verification_status": item.get("Verification status") or item.get("verification_status"),
                "observed_from": item.get("Observed from") or item.get("observed_from"),
                "information_to_inventory": (
                    item.get("Information to inventory") or item.get("information_to_inventory")
                ),
                "priority": item.get("Priority") or item.get("priority") or "P1",
                "collection_decision": (
                    item.get("Collection decision") or item.get("collection_decision")
                ),
            }
        )
    return normalized


class PilotCoordinator:
    """Coordinates policy audits and bounded metadata pilots against `KnowledgeStore`."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        adapters: Sequence[SourceAdapter] | None = None,
        user_agent: str = DEFAULT_PROJECT_USER_AGENT,
    ) -> None:
        self.store = store
        self.adapters = list(adapters) if adapters is not None else build_default_adapters(user_agent=user_agent)

    def _persist_policy_audit(self, policy: PolicyAuditResult, *, run_id: str) -> None:
        """Persist a `PolicyAuditResult` into `sources`, `site_policies`, `crawl_audit`, and `provenance_events`."""
        existing_source = self.store.get_source(policy.source_id)
        with self.store.transaction() as conn:
            if existing_source is not None:
                notes = (
                    f"[{policy.checked_at_utc}] Policy audit ({policy.adapter_id}): "
                    f"status={policy.status}, decision={policy.metadata_collection_decision}. "
                    f"{policy.evidence_summary}"
                )
                conn.execute(
                    """
                    UPDATE sources
                       SET crawl_delay_seconds = ?,
                           min_enforced_delay_seconds = MAX(min_enforced_delay_seconds, ?),
                           robots_url = COALESCE(?, robots_url),
                           notes = ?,
                           updated_at = ?
                     WHERE source_id = ?
                    """,
                    (
                        float(policy.observed_crawl_delay_seconds or existing_source["crawl_delay_seconds"]),
                        max(1.0, float(policy.effective_delay_seconds)),
                        policy.policy_url,
                        notes,
                        policy.checked_at_utc,
                        policy.source_id,
                    ),
                )

            conn.execute(
                """
                INSERT INTO site_policies(
                    origin, robots_url, status, http_status, crawl_delay_seconds,
                    sitemap_urls, evidence, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin) DO UPDATE SET
                    robots_url=excluded.robots_url,
                    status=excluded.status,
                    http_status=excluded.http_status,
                    crawl_delay_seconds=excluded.crawl_delay_seconds,
                    sitemap_urls=excluded.sitemap_urls,
                    evidence=excluded.evidence,
                    checked_at=excluded.checked_at
                """,
                (
                    policy.origin,
                    policy.policy_url,
                    (policy.robots_crawl_status or policy.status).lower(),
                    policy.http_status,
                    policy.observed_crawl_delay_seconds,
                    json.dumps(policy.sitemap_urls, ensure_ascii=False),
                    policy.evidence_summary,
                    policy.checked_at_utc,
                ),
            )

            conn.execute(
                """
                INSERT INTO crawl_audit(
                    endpoint_url, check_name, status, evidence, next_action, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.policy_url,
                    f"policy_audit:{policy.source_id}",
                    policy.status,
                    policy.evidence_summary,
                    (
                        "Permit bounded metadata-only collection"
                        if policy.is_metadata_collection_permitted
                        else "Fail closed; do not collect HTML/API resources until resolved"
                    ),
                    policy.checked_at_utc,
                ),
            )

            self.store.record_provenance(
                event_type="SOURCE_POLICY_AUDIT",
                acquisition_method=policy.adapter_id,
                actor="pilot_coordinator",
                status=policy.status.lower(),
                source_id=policy.source_id,
                run_id=run_id,
                source_url=policy.policy_url,
                final_url=policy.policy_url,
                http_status=policy.http_status,
                details={
                    "policy_verification_status": policy.status,
                    "metadata_collection_decision": policy.metadata_collection_decision,
                    "policy_profile_id": policy.policy_profile_id,
                    "policy_version": policy.policy_version,
                    "policy_basis": policy.policy_basis,
                    "approved_api_endpoint": policy.approved_api_endpoint,
                    "robots_crawl_status": policy.robots_crawl_status,
                    "general_html_crawling_permitted": policy.general_html_crawling_permitted,
                    "verified_at_utc": policy.checked_at_utc,
                    "observed_crawl_delay_seconds": policy.observed_crawl_delay_seconds,
                    "effective_delay_seconds": policy.effective_delay_seconds,
                    "required_identification": policy.required_identification,
                    "rate_limit_behavior": policy.rate_limit_behavior,
                    "allowed_scope": policy.allowed_scope,
                    "observed_restrictions": policy.observed_restrictions,
                    "unresolved_questions": policy.unresolved_questions,
                    "evidence_summary": policy.evidence_summary,
                },
                recorded_at=policy.checked_at_utc,
            )

    def _ensure_source_url_row(
        self,
        candidate: SourceCandidate,
        envelope: NormalizedResourceEnvelope | None = None,
    ) -> str | None:
        """Ensure `candidate.manifest_id` exists in `source_urls` before FK references."""
        manifest_id = (envelope.manifest_id if envelope else None) or candidate.manifest_id
        if not manifest_id:
            return None
        now = utc_now()
        canonical_url = (envelope.canonical_url if envelope else None) or candidate.url
        title = (envelope.title if envelope else None) or candidate.title_hint or canonical_url
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT manifest_id FROM source_urls WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO source_urls(
                        manifest_id, person_source, url, page_resource_name,
                        resource_type, domain_scope, verification_status,
                        observed_from, information_to_inventory, priority,
                        collection_decision, source_id, canonical_url,
                        imported_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest_id,
                        candidate.source_id,
                        candidate.url,
                        title,
                        candidate.resource_type_hint,
                        candidate.extra.get("domain_scope", "First-party"),
                        "Verified via source adapter pilot",
                        candidate.observed_from,
                        candidate.extra.get("information_to_inventory"),
                        candidate.priority,
                        candidate.extra.get("collection_decision"),
                        candidate.source_id,
                        canonical_url,
                        now,
                        now,
                    ),
                )
        return manifest_id

    def run_policy_audit(self, *, force_refresh: bool = True) -> PolicyAuditSummary:
        """Audit robots.txt and API policies across all configured adapters and persist results."""
        run_id = self.store.start_collection_run(
            run_type="POLICY_AUDIT",
            is_dry_run=False,
            selected_count=len(self.adapters),
            notes="Source policy and robots.txt audit across canonical source adapters.",
        )
        policies: list[PolicyAuditResult] = []
        http_events: list[HttpRequestEvent] = []

        try:
            for adapter in self.adapters:
                before_len = len(adapter.request_log)
                policy = adapter.audit_policy(force_refresh=force_refresh)
                policies.append(policy)
                http_events.extend(adapter.request_log[before_len:])
                self._persist_policy_audit(policy, run_id=run_id)

            blocked_count = sum(1 for p in policies if not p.is_metadata_collection_permitted)
            self.store.finish_collection_run(
                run_id,
                status="completed" if blocked_count == 0 else "completed-with-policy-blocks",
                fetched_count=len(policies),
                skipped_count=blocked_count,
                error_count=0,
                notes=(
                    f"Audited {len(policies)} sources: "
                    f"{len(policies) - blocked_count} permitted, {blocked_count} blocked/unverified."
                ),
            )
        except Exception as exc:
            self.store.finish_collection_run(
                run_id,
                status="failed",
                fetched_count=len(policies),
                error_count=1,
                failure_summary=f"{type(exc).__name__}: {exc}",
            )
            raise

        return PolicyAuditSummary(
            run_id=run_id,
            checked_at_utc=utc_now(),
            policies=policies,
            http_events=http_events,
        )

    def run_metadata_pilot(
        self,
        *,
        seed_manifest: list[dict[str, Any]] | None = None,
        manifest_path: Path | str | None = DEFAULT_SEED_PATH,
        max_per_source: int = MAX_PILOT_PER_SOURCE,
        max_total: int = MAX_PILOT_TOTAL,
    ) -> PilotRunSummary:
        """Execute a bounded, policy-gated metadata pilot across all configured adapters.

        Hard caps enforced per Task 012 Section 5:
        - `max_per_source` <= 2
        - `max_total` <= 10
        """
        if max_per_source < 1 or max_per_source > MAX_PILOT_PER_SOURCE:
            raise ValueError(
                f"Pilot max_per_source must be between 1 and {MAX_PILOT_PER_SOURCE} (got {max_per_source})"
            )
        if max_total < 1 or max_total > MAX_PILOT_TOTAL:
            raise ValueError(
                f"Pilot max_total must be between 1 and {MAX_PILOT_TOTAL} (got {max_total})"
            )

        if seed_manifest is None:
            seed_manifest = load_normalized_seed_manifest(manifest_path)

        started_at = utc_now()
        run_id = self.store.start_collection_run(
            run_type="METADATA_PILOT",
            is_dry_run=False,
            manifest_path=manifest_path,
            selected_count=min(max_total, len(self.adapters) * max_per_source),
            notes=(
                f"Bounded policy-gated metadata pilot "
                f"(max_per_source={max_per_source}, max_total={max_total})."
            ),
        )

        # Ensure legacy crawl_runs has a matching run_id so fetch_events FK succeeds
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO crawl_runs(
                    run_id, started_at, manifest_path, selected_count, status, notes
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    started_at,
                    str(manifest_path or "seed_urls.json"),
                    min(max_total, len(self.adapters) * max_per_source),
                    f"Metadata pilot (max_per_source={max_per_source}, max_total={max_total})",
                ),
            )

        summary = PilotRunSummary(
            run_id=run_id,
            started_at_utc=started_at,
            finished_at_utc=started_at,
            max_per_source=max_per_source,
            max_total=max_total,
        )

        total_attempted_fetches = 0

        try:
            for adapter in self.adapters:
                if total_attempted_fetches >= max_total:
                    break

                before_policy_idx = len(adapter.request_log)
                policy = adapter.audit_policy(force_refresh=False)
                summary.policies.append(policy)
                summary.http_events.extend(adapter.request_log[before_policy_idx:])
                self._persist_policy_audit(policy, run_id=run_id)

                if not policy.is_metadata_collection_permitted:
                    summary.blocked_count += 1
                    self.store.record_provenance(
                        event_type="ADAPTER_SOURCE_BLOCKED_BY_POLICY",
                        acquisition_method=adapter.adapter_id,
                        actor="pilot_coordinator",
                        status="blocked-by-policy",
                        source_id=adapter.source_id,
                        run_id=run_id,
                        source_url=policy.policy_url,
                        http_status=policy.http_status,
                        details={
                            "policy_status": policy.status,
                            "decision": policy.metadata_collection_decision,
                            "evidence_summary": policy.evidence_summary,
                        },
                    )
                    summary.outcomes.append(
                        PilotItemOutcome(
                            source_id=adapter.source_id,
                            adapter_id=adapter.adapter_id,
                            requested_url=policy.policy_url,
                            canonical_url=None,
                            title=None,
                            status="blocked",
                            http_status=policy.http_status,
                            error_class="POLICY_BLOCKED",
                            error_message=policy.evidence_summary,
                        )
                    )
                    continue

                remaining_budget = max_total - total_attempted_fetches
                per_source_limit = min(max_per_source, remaining_budget)
                candidates = adapter.discover_candidates(
                    seed_manifest=seed_manifest,
                    limit=per_source_limit,
                )[:per_source_limit]
                source_processed = 0

                for candidate in candidates:
                    if total_attempted_fetches >= max_total or source_processed >= per_source_limit:
                        break
                    before_fetch_idx = len(adapter.request_log)

                    try:
                        envelopes = adapter.collect_candidate_envelopes(candidate, policy=policy)
                        summary.http_events.extend(adapter.request_log[before_fetch_idx:])
                        allowed_slice = min(
                            per_source_limit - source_processed,
                            max_total - total_attempted_fetches,
                        )
                        for envelope in envelopes[:allowed_slice]:
                            total_attempted_fetches += 1
                            source_processed += 1
                            manifest_id = self._ensure_source_url_row(candidate, envelope)
                            upserted = self.store.upsert_resource(
                                source_id=envelope.source_id,
                                manifest_id=manifest_id,
                                canonical_url=envelope.canonical_url,
                                fetched_url=envelope.fetched_url,
                                title=envelope.title,
                                authors=envelope.authors,
                                published_at=envelope.published_at,
                                resource_type=envelope.resource_type,
                                media_modality=envelope.media_modality,
                                domain_scope=envelope.domain_scope,
                                abstract_or_description=envelope.abstract_or_description,
                                topic_tags=envelope.topic_tags,
                                external_identifier=envelope.external_identifier,
                                observed_from=envelope.observed_from,
                                information_to_inventory=envelope.information_to_inventory,
                                priority=envelope.priority,
                                collection_decision=envelope.collection_decision,
                                verification_status=envelope.verification_status,
                                robots_sitemap_status=envelope.robots_sitemap_status,
                                rights_status=envelope.rights_status,
                                license_status=envelope.license_status,
                                license_spdx=envelope.license_spdx,
                                permitted_storage_scope=envelope.permitted_storage_scope,
                                permits_full_text_storage=envelope.permits_full_text_storage,
                                permits_verbatim_excerpts=envelope.permits_verbatim_excerpts,
                                legal_uncertainty_status=envelope.legal_uncertainty_status,
                                text_storage_status=envelope.text_storage_status,
                                last_checked=envelope.collected_at_utc[:10],
                                last_verified_at=envelope.collected_at_utc,
                                discovered_at=envelope.collected_at_utc,
                                run_id=run_id,
                                acquisition_method=envelope.adapter_id,
                                actor="pilot_coordinator",
                                change_reason="metadata_pilot_collection",
                            )

                            operation = str(upserted["operation"])
                            if operation == "inserted":
                                summary.inserted_count += 1
                            elif operation == "updated":
                                summary.updated_count += 1
                            else:
                                summary.unchanged_count += 1
                                with self.store.transaction() as conn:
                                    conn.execute(
                                        "UPDATE resources SET last_verified_at = ?, last_checked = ? WHERE resource_id = ?",
                                        (
                                            envelope.collected_at_utc,
                                            envelope.collected_at_utc[:10],
                                            upserted["resource_id"],
                                        ),
                                    )

                            # Always record an explicit adapter-level provenance event with rich metadata
                            self.store.record_provenance(
                                event_type=(
                                    "ADAPTER_METADATA_COLLECTED"
                                    if operation != "unchanged"
                                    else "RESOURCE_VERIFIED_UNCHANGED"
                                ),
                                acquisition_method=envelope.adapter_id,
                                actor="pilot_coordinator",
                                status=operation,
                                source_id=envelope.source_id,
                                resource_id=upserted["resource_id"],
                                version_id=upserted["current_version_id"],
                                run_id=run_id,
                                manifest_id=manifest_id,
                                source_url=candidate.url,
                                final_url=envelope.fetched_url,
                                http_status=envelope.http_status,
                                metadata_hash=upserted["metadata_hash"],
                                details={
                                    "policy_verification_status": policy.status,
                                    "policy_profile_id": policy.policy_profile_id,
                                    "policy_version": policy.policy_version,
                                    "policy_basis": policy.policy_basis,
                                    "approved_api_endpoint": policy.approved_api_endpoint,
                                    "metadata_completeness": envelope.metadata_completeness,
                                    "source_specific_metadata": envelope.source_specific_metadata,
                                    "official_links": envelope.official_links,
                                    "etag": envelope.etag,
                                    "last_modified": envelope.last_modified_header,
                                    "discovered_link_count": len(envelope.discovered_links),
                                    "rejected_link_count": len(envelope.rejected_discovered_links),
                                },
                                recorded_at=envelope.collected_at_utc,
                            )

                            # Persist discovered_link_candidates (reviewable, un-fetched candidates)
                            for link in envelope.discovered_links:
                                self.store.upsert_discovered_link_candidate(
                                    referring_resource_id=upserted["resource_id"],
                                    referring_source_id=envelope.source_id,
                                    from_manifest_id=manifest_id,
                                    from_url=envelope.canonical_url,
                                    raw_href=link.raw_href or link.original_url or link.link_url,
                                    original_url=link.original_url or link.link_url,
                                    normalized_url=link.link_url,
                                    link_text=link.link_text,
                                    rel=link.rel,
                                    is_internal=link.is_internal,
                                    target_scope=link.target_scope,
                                    recommended_decision=link.decision,
                                    policy_state=link.policy_state,
                                    review_status=link.review_status or "PENDING_REVIEW",
                                    fetch_permission_granted=link.fetch_permission_granted,
                                    discovered_at=envelope.collected_at_utc,
                                    run_id=run_id,
                                    acquisition_method=envelope.adapter_id,
                                    actor="pilot_coordinator",
                                )

                            for rejected in envelope.rejected_discovered_links:
                                self.store.record_provenance(
                                    event_type="DISCOVERED_LINK_CANDIDATE_REJECTED",
                                    acquisition_method=envelope.adapter_id,
                                    actor="pilot_coordinator",
                                    status="rejected-unsafe-or-unsupported-url",
                                    source_id=envelope.source_id,
                                    resource_id=upserted["resource_id"],
                                    run_id=run_id,
                                    manifest_id=manifest_id,
                                    source_url=envelope.canonical_url,
                                    final_url=str(rejected.get("original_url") or rejected.get("raw_href") or ""),
                                    details=dict(rejected),
                                    recorded_at=envelope.collected_at_utc,
                                )

                            # Persist fetch_events and legacy discovered_links (if manifest_id exists)
                            with self.store.transaction() as conn:
                                conn.execute(
                                    """
                                    INSERT INTO fetch_events(
                                        run_id, manifest_id, requested_url, final_url,
                                        status, http_status, error, fetched_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                                    """,
                                    (
                                        run_id,
                                        manifest_id,
                                        candidate.url,
                                        envelope.fetched_url,
                                        f"pilot-{operation}",
                                        envelope.http_status,
                                        envelope.collected_at_utc,
                                    ),
                                )
                                if manifest_id:
                                    conn.execute(
                                        """
                                        UPDATE source_urls
                                           SET last_fetch_status = ?,
                                               last_fetch_error = NULL,
                                               last_fetch_at = ?,
                                               robots_sitemap_status = COALESCE(?, robots_sitemap_status),
                                               updated_at = ?
                                         WHERE manifest_id = ?
                                        """,
                                        (
                                            f"pilot-{operation}",
                                            envelope.collected_at_utc,
                                            envelope.robots_sitemap_status,
                                            envelope.collected_at_utc,
                                            manifest_id,
                                        ),
                                    )
                                    for link in envelope.discovered_links:
                                        conn.execute(
                                            """
                                            INSERT INTO discovered_links(
                                                from_manifest_id, link_url, link_text, rel,
                                                target_scope, decision, discovered_at
                                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                            ON CONFLICT(from_manifest_id, link_url) DO UPDATE SET
                                                link_text=excluded.link_text,
                                                rel=excluded.rel,
                                                target_scope=excluded.target_scope,
                                                decision=excluded.decision,
                                                discovered_at=excluded.discovered_at
                                            """,
                                            (
                                                manifest_id,
                                                link.link_url,
                                                link.link_text,
                                                link.rel,
                                                link.target_scope,
                                                link.decision,
                                                envelope.collected_at_utc,
                                            ),
                                        )

                            summary.discovered_link_count += len(envelope.discovered_links)
                            summary.outcomes.append(
                                PilotItemOutcome(
                                    source_id=envelope.source_id,
                                    adapter_id=envelope.adapter_id,
                                    requested_url=candidate.url,
                                    canonical_url=envelope.canonical_url,
                                    title=envelope.title,
                                    status=operation,
                                    resource_id=upserted["resource_id"],
                                    version_id=upserted["current_version_id"],
                                    version_count=int(upserted["version_count"]),
                                    http_status=envelope.http_status,
                                    discovered_link_count=len(envelope.discovered_links),
                                    metadata_completeness=envelope.metadata_completeness,
                                )
                            )

                    except NotModifiedResponse as not_mod:
                        total_attempted_fetches += 1
                        source_processed += 1
                        summary.http_events.extend(adapter.request_log[before_fetch_idx:])
                        summary.unchanged_count += 1
                        existing_res = self.store.get_resource(
                            manifest_id=candidate.manifest_id
                        ) if candidate.manifest_id else self.store.get_resource(canonical_url=candidate.url)
                        now = utc_now()
                        if existing_res:
                            with self.store.transaction() as conn:
                                conn.execute(
                                    "UPDATE resources SET last_verified_at = ?, last_checked = ? WHERE resource_id = ?",
                                    (now, now[:10], existing_res["resource_id"]),
                                )
                        self.store.record_provenance(
                            event_type="RESOURCE_NOT_MODIFIED_304",
                            acquisition_method=adapter.adapter_id,
                            actor="pilot_coordinator",
                            status="not-modified-304",
                            source_id=adapter.source_id,
                            resource_id=existing_res["resource_id"] if existing_res else None,
                            version_id=existing_res["current_version_id"] if existing_res else None,
                            run_id=run_id,
                            manifest_id=candidate.manifest_id,
                            source_url=candidate.url,
                            final_url=not_mod.url,
                            http_status=304,
                            details={"headers": not_mod.headers},
                        )
                        summary.outcomes.append(
                            PilotItemOutcome(
                                source_id=adapter.source_id,
                                adapter_id=adapter.adapter_id,
                                requested_url=candidate.url,
                                canonical_url=existing_res["canonical_url"] if existing_res else candidate.url,
                                title=existing_res["title"] if existing_res else candidate.title_hint,
                                status="not-modified-304",
                                resource_id=existing_res["resource_id"] if existing_res else None,
                                version_id=existing_res["current_version_id"] if existing_res else None,
                                version_count=int(existing_res["version_count"]) if existing_res else None,
                                http_status=304,
                            )
                        )

                    except AdapterError as err:
                        total_attempted_fetches += 1
                        source_processed += 1
                        summary.http_events.extend(adapter.request_log[before_fetch_idx:])
                        is_blocked = err.error_class in {"POLICY_BLOCKED", "SCOPE_VIOLATION"}
                        if is_blocked:
                            summary.blocked_count += 1
                            outcome_status = "blocked"
                            event_type = "ADAPTER_FETCH_BLOCKED"
                        elif isinstance(err, RateLimitError):
                            summary.error_count += 1
                            outcome_status = "error"
                            event_type = "ADAPTER_RATE_LIMITED"
                        else:
                            summary.error_count += 1
                            outcome_status = "error"
                            event_type = "ADAPTER_FETCH_ERROR"

                        self.store.record_provenance(
                            event_type=event_type,
                            acquisition_method=adapter.adapter_id,
                            actor="pilot_coordinator",
                            status=outcome_status,
                            source_id=adapter.source_id,
                            run_id=run_id,
                            manifest_id=candidate.manifest_id if self._manifest_exists(candidate.manifest_id) else None,
                            source_url=candidate.url,
                            final_url=err.url or candidate.url,
                            http_status=err.http_status,
                            details={
                                "error_class": err.error_class,
                                "error_message": str(err),
                                "retry_after_seconds": err.retry_after_seconds,
                                "is_retryable": err.is_retryable,
                            },
                        )
                        with self.store.transaction() as conn:
                            conn.execute(
                                """
                                INSERT INTO fetch_events(
                                    run_id, manifest_id, requested_url, final_url,
                                    status, http_status, error, fetched_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    run_id,
                                    candidate.manifest_id if self._manifest_exists(candidate.manifest_id) else None,
                                    candidate.url,
                                    err.url or candidate.url,
                                    err.error_class.lower(),
                                    err.http_status,
                                    str(err),
                                    utc_now(),
                                ),
                            )

                        summary.outcomes.append(
                            PilotItemOutcome(
                                source_id=adapter.source_id,
                                adapter_id=adapter.adapter_id,
                                requested_url=candidate.url,
                                canonical_url=None,
                                title=candidate.title_hint,
                                status=outcome_status,
                                http_status=err.http_status,
                                error_class=err.error_class,
                                error_message=str(err),
                            )
                        )
                        # Fail closed without retry storms for this source on rate-limit or HTTP access block
                        if isinstance(err, RateLimitError) or (
                            err.error_class == "POLICY_BLOCKED" and err.http_status in {401, 403, 451}
                        ):
                            break

                        # Abort remaining candidates for this source on HTTP 429/503 rate limiting
                        if isinstance(err, RateLimitError):
                            break

            summary.finished_at_utc = utc_now()
            final_status = (
                "completed"
                if (summary.error_count == 0 and summary.blocked_count == 0)
                else "completed-with-blocks-or-errors"
            )
            self.store.finish_collection_run(
                run_id,
                status=final_status,
                imported_count=summary.inserted_count,
                updated_count=summary.updated_count,
                skipped_count=summary.unchanged_count + summary.blocked_count,
                rejected_count=summary.blocked_count,
                fetched_count=summary.fetched_count,
                discovered_count=summary.discovered_link_count,
                error_count=summary.error_count,
                notes=(
                    f"Pilot finished: inserted={summary.inserted_count}, "
                    f"updated={summary.updated_count}, unchanged={summary.unchanged_count}, "
                    f"blocked={summary.blocked_count}, errors={summary.error_count}."
                ),
            )
            with self.store.transaction() as conn:
                conn.execute(
                    """
                    UPDATE crawl_runs
                       SET finished_at=?,
                           fetched_count=?,
                           discovered_count=?,
                           skipped_count=?,
                           error_count=?,
                           status=?
                     WHERE run_id=?
                    """,
                    (
                        summary.finished_at_utc,
                        summary.fetched_count,
                        summary.discovered_link_count,
                        summary.unchanged_count + summary.blocked_count,
                        summary.error_count,
                        final_status,
                        run_id,
                    ),
                )
        except Exception as exc:
            self.store.finish_collection_run(
                run_id,
                status="failed",
                imported_count=summary.inserted_count,
                updated_count=summary.updated_count,
                skipped_count=summary.unchanged_count,
                rejected_count=summary.blocked_count,
                fetched_count=summary.fetched_count,
                discovered_count=summary.discovered_link_count,
                error_count=summary.error_count + 1,
                failure_summary=f"{type(exc).__name__}: {exc}",
            )
            raise

        return summary

    def _manifest_exists(self, manifest_id: str | None) -> bool:
        if not manifest_id:
            return False
        row = self.store.conn.execute(
            "SELECT 1 FROM source_urls WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        return row is not None


def format_policy_audit_summary(summary: PolicyAuditSummary) -> str:
    """Format a human-readable summary of `PolicyAuditSummary` for CLI output."""
    lines = [
        "=" * 88,
        f"Source Policy Audit Summary (run_id={summary.run_id})",
        f"Checked at: {summary.checked_at_utc}",
        f"Permitted sources: {summary.permitted_count} | Blocked/Unverified: {summary.blocked_or_unverified_count}",
        "-" * 88,
    ]
    for policy in summary.policies:
        lines.append(
            f"- {policy.source_id} ({policy.adapter_id}): "
            f"status={policy.status}, decision={policy.metadata_collection_decision}, "
            f"http_status={policy.http_status}, effective_delay={policy.effective_delay_seconds:.1f}s"
        )
        lines.append(f"  policy_url: {policy.policy_url}")
        lines.append(f"  evidence  : {policy.evidence_summary}")
    lines.append("=" * 88)
    return "\n".join(lines)


def format_pilot_run_summary(summary: PilotRunSummary) -> str:
    """Format a human-readable summary of `PilotRunSummary` for CLI output."""
    lines = [
        "=" * 88,
        f"Policy-Gated Metadata Pilot Summary (run_id={summary.run_id})",
        f"Window : {summary.started_at_utc} -> {summary.finished_at_utc}",
        f"Caps   : max_per_source={summary.max_per_source}, max_total={summary.max_total}",
        (
            f"Counts : inserted={summary.inserted_count}, updated={summary.updated_count}, "
            f"unchanged={summary.unchanged_count}, blocked={summary.blocked_count}, "
            f"errors={summary.error_count}, discovered_links={summary.discovered_link_count}"
        ),
        "-" * 88,
    ]
    for outcome in summary.outcomes:
        lines.append(
            f"- [{outcome.status.upper()}] {outcome.source_id} ({outcome.adapter_id}) "
            f"HTTP {outcome.http_status}: {outcome.title or outcome.requested_url}"
        )
        if outcome.canonical_url:
            lines.append(
                f"  canonical_url={outcome.canonical_url} | resource_id={outcome.resource_id} "
                f"| version_id={outcome.version_id} (v{outcome.version_count})"
            )
        if outcome.error_message:
            lines.append(f"  error ({outcome.error_class}): {outcome.error_message}")
    lines.append("=" * 88)
    return "\n".join(lines)
