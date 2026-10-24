#!/usr/bin/env python3
"""Offline resource curation CLI for discovered-link candidates.

Provides a safe, local, zero-network workflow for inspecting, filtering,
paginating, exporting, summarizing, and recording human curation decisions on
`discovered_link_candidates` stored in the SQLite knowledge store.

Safety boundaries:
- Zero network requests are ever made by this module.
- Discovering or reviewing a link never triggers fetching.
- Accepting a candidate for reference (`ACCEPTED_FOR_REFERENCE`) or approving
  it for manifest consideration (`APPROVED_FOR_MANIFEST`) never grants automated
  fetch permission (`fetch_permission_granted` remains `0`).
- Preserves `original_url` and `referring_resource_id` / `from_url` for auditability.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import sys
from typing import Any

from knowledge_store import (
    DEFAULT_DB_PATH,
    VALID_CANDIDATE_REVIEW_STATUSES,
    VALID_DOMAIN_CLASSIFICATIONS,
    KnowledgeStore,
)

DEFAULT_PILOT_DB_PATH = Path(__file__).resolve().parent / "data" / "runtime" / "task012_pilot.sqlite"


def resolve_default_db_path() -> Path:
    """Return the local Task 012 pilot runtime DB if present, otherwise `DEFAULT_DB_PATH`."""
    if DEFAULT_PILOT_DB_PATH.exists():
        return DEFAULT_PILOT_DB_PATH
    return DEFAULT_DB_PATH


def _is_missing_db(db_path: Path | str) -> bool:
    raw = str(db_path)
    if raw == ":memory:":
        return False
    return not Path(db_path).exists()


def empty_summary_payload(
    *,
    db_path: Path | str,
    database_exists: bool,
    referring_source_id: str | None = None,
    host_domain: str | None = None,
    domain_classification: str | None = None,
) -> dict[str, Any]:
    """Return a zeroed summary structure when the target SQLite database is missing or empty."""
    by_review_status = {status: 0 for status in VALID_CANDIDATE_REVIEW_STATUSES}
    by_classification = {cls: 0 for cls in VALID_DOMAIN_CLASSIFICATIONS}
    return {
        "database_path": str(db_path),
        "database_exists": database_exists,
        "total_candidates": 0,
        "pending_count": 0,
        "accepted_for_reference_count": 0,
        "approved_for_manifest_count": 0,
        "rejected_count": 0,
        "deferred_count": 0,
        "by_review_status": by_review_status,
        "by_domain_classification": by_classification,
        "by_source": {},
        "by_host_domain": {},
        "fetch_permission_granted_count": 0,
        "review_events_count": 0,
        "filters": {
            "referring_source_id": referring_source_id.strip() if referring_source_id else None,
            "host_domain": host_domain.strip().lower().rstrip(".") if host_domain else None,
            "domain_classification": domain_classification.strip().upper() if domain_classification else None,
        },
    }


def format_summary_text(summary: dict[str, Any]) -> str:
    """Format a human-readable candidate curation summary."""
    db_path = summary.get("database_path", str(DEFAULT_DB_PATH))
    db_exists = summary.get("database_exists", True)
    lines = [
        "Discovered-Link Candidate Curation Summary (offline / zero network requests)",
        f"  Database path:            {db_path}",
        f"  Database exists:          {'yes' if db_exists else 'no (missing database)'}",
        f"  Total candidates:         {summary['total_candidates']}",
        f"  Pending review:           {summary['pending_count']}",
        f"  Accepted for reference:   {summary['accepted_for_reference_count']}",
        f"  Approved for manifest:    {summary['approved_for_manifest_count']}",
        f"  Rejected:                 {summary['rejected_count']}",
        f"  Deferred:                 {summary['deferred_count']}",
        f"  Fetch permission granted: {summary['fetch_permission_granted_count']}",
        f"  Recorded review events:   {summary['review_events_count']}",
    ]

    by_cls = summary.get("by_domain_classification") or {}
    lines.append(
        f"  Domain classification:    INTERNAL={by_cls.get('INTERNAL', 0)}, "
        f"EXTERNAL={by_cls.get('EXTERNAL', 0)}"
    )

    by_source = summary.get("by_source") or {}
    if by_source:
        lines.append("  By referring source:")
        for src_id, cnt in sorted(by_source.items()):
            lines.append(f"    - {src_id}: {cnt}")

    by_domain = summary.get("by_host_domain") or {}
    if by_domain:
        lines.append("  Top host domains:")
        for dom, cnt in list(by_domain.items())[:10]:
            lines.append(f"    - {dom}: {cnt}")

    return "\n".join(lines)


def format_candidate_list_text(result: dict[str, Any]) -> str:
    """Format a paginated candidate query result as human-readable text."""
    db_path = result.get("database_path", str(DEFAULT_DB_PATH))
    db_exists = result.get("database_exists", True)
    lines = [
        f"Discovered-Link Candidates ({db_path})",
        f"  Database exists: {'yes' if db_exists else 'no'}",
        f"  Matching total:  {result['total_count']}",
        f"  Returned count:  {result['returned_count']} (limit={result['limit']}, offset={result['offset']}, has_more={result['has_more']})",
    ]
    active_filters = {k: v for k, v in (result.get("filters") or {}).items() if v is not None}
    if active_filters:
        lines.append(f"  Active filters:  {json.dumps(active_filters, sort_keys=True)}")

    items = result.get("items") or []
    if not items:
        lines.append("  No matching discovered-link candidates.")
        return "\n".join(lines)

    lines.append("  Candidates:")
    for idx, item in enumerate(items, start=int(result["offset"]) + 1):
        lines.append(
            f"    [{idx}] {item['candidate_id']} | {item['review_status']} | "
            f"{item['domain_classification']} | source={item['referring_source_id']} | "
            f"domain={item['host_domain']} | fetch_permitted={bool(item['fetch_permission_granted'])}"
        )
        lines.append(f"        normalized_url: {item['normalized_url']}")
        lines.append(
            f"        from_resource:  {item['referring_resource_id']} ({item['from_url']})"
        )
        if item.get("reviewed_by") or item.get("review_reason"):
            lines.append(
                f"        last_review:    by={item.get('reviewed_by')} at={item.get('reviewed_at')} "
                f"reason={item.get('review_reason')!r}"
            )
    return "\n".join(lines)


def _resolve_pagination(args: argparse.Namespace) -> tuple[int, int]:
    page = getattr(args, "page", None)
    page_size = getattr(args, "page_size", None)
    limit = getattr(args, "limit", None)
    offset = getattr(args, "offset", None)

    if page is not None:
        if page <= 0:
            raise ValueError(f"--page must be >= 1, got {page}")
        eff_limit = page_size if page_size is not None else (limit if limit is not None else 50)
        if eff_limit <= 0:
            raise ValueError(f"Page size / limit must be >= 1, got {eff_limit}")
        eff_offset = (page - 1) * eff_limit
        return eff_limit, eff_offset

    eff_limit = page_size if page_size is not None else (limit if limit is not None else 50)
    eff_offset = offset if offset is not None else 0
    if eff_limit <= 0:
        raise ValueError(f"--limit must be >= 1, got {eff_limit}")
    if eff_offset < 0:
        raise ValueError(f"--offset must be >= 0, got {eff_offset}")
    return eff_limit, eff_offset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline inspection and curation workflow for discovered-link candidates."
    )
    subparsers = parser.add_subparsers(dest="command")

    # summary
    summary_parser = subparsers.add_parser(
        "summary",
        help="Show summary counts of pending, accepted-for-reference, and rejected candidates",
    )
    summary_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (defaults to local pilot DB if present, else knowledge_base.sqlite)",
    )
    summary_parser.add_argument("--source", dest="source_id", default=None, help="Filter by referring_source_id")
    summary_parser.add_argument("--domain", dest="host_domain", default=None, help="Filter by target host_domain")
    summary_parser.add_argument(
        "--classification",
        dest="domain_classification",
        default=None,
        help="Filter by domain_classification (INTERNAL or EXTERNAL)",
    )
    summary_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (text or json)",
    )

    # list
    list_parser = subparsers.add_parser(
        "list",
        help="List discovered-link candidates with filtering and pagination",
    )
    list_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path",
    )
    list_parser.add_argument("--source", dest="source_id", default=None, help="Filter by referring_source_id")
    list_parser.add_argument(
        "--resource-id",
        dest="resource_id",
        default=None,
        help="Filter by referring_resource_id",
    )
    list_parser.add_argument("--domain", dest="host_domain", default=None, help="Filter by target host_domain")
    list_parser.add_argument(
        "--classification",
        dest="domain_classification",
        default=None,
        help="Filter by domain_classification (INTERNAL or EXTERNAL)",
    )
    list_parser.add_argument(
        "--review-status",
        dest="review_status",
        default=None,
        help=f"Filter by review_status ({', '.join(VALID_CANDIDATE_REVIEW_STATUSES)})",
    )
    list_parser.add_argument("--limit", type=int, default=None, help="Maximum number of rows to return (default: 50)")
    list_parser.add_argument("--offset", type=int, default=None, help="Zero-based row offset (default: 0)")
    list_parser.add_argument("--page", type=int, default=None, help="One-based page number (alternative to --offset)")
    list_parser.add_argument("--page-size", type=int, default=None, help="Page size when using --page")
    list_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (text or json)",
    )

    # export
    export_parser = subparsers.add_parser(
        "export",
        help="Export discovered-link candidates to CSV or JSON for manual review",
    )
    export_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path",
    )
    export_parser.add_argument(
        "--format",
        choices=("csv", "json"),
        default="json",
        help="Export format (csv or json)",
    )
    export_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional file path to write exported CSV/JSON (prints to stdout if omitted)",
    )
    export_parser.add_argument("--source", dest="source_id", default=None, help="Filter by referring_source_id")
    export_parser.add_argument(
        "--resource-id",
        dest="resource_id",
        default=None,
        help="Filter by referring_resource_id",
    )
    export_parser.add_argument("--domain", dest="host_domain", default=None, help="Filter by target host_domain")
    export_parser.add_argument(
        "--classification",
        dest="domain_classification",
        default=None,
        help="Filter by domain_classification (INTERNAL or EXTERNAL)",
    )
    export_parser.add_argument(
        "--review-status",
        dest="review_status",
        default=None,
        help=f"Filter by review_status ({', '.join(VALID_CANDIDATE_REVIEW_STATUSES)})",
    )
    export_parser.add_argument("--limit", type=int, default=None, help="Optional maximum rows to export")
    export_parser.add_argument("--offset", type=int, default=0, help="Zero-based row offset (default: 0)")

    # review
    review_parser = subparsers.add_parser(
        "review",
        help="Record an offline curation review decision with reviewer, timestamp, reason, and provenance",
    )
    review_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path",
    )
    review_parser.add_argument("--candidate-id", default=None, help="Target candidate_id (CLINK-...)")
    review_parser.add_argument(
        "--resource-id",
        default=None,
        help="Referring resource_id (when looking up by --resource-id and --url)",
    )
    review_parser.add_argument(
        "--url",
        dest="normalized_url",
        default=None,
        help="Target URL (when looking up by --resource-id and --url)",
    )
    review_parser.add_argument(
        "--decision",
        required=True,
        help=f"Review decision ({', '.join(VALID_CANDIDATE_REVIEW_STATUSES)})",
    )
    review_parser.add_argument("--reviewer", required=True, help="Reviewer identifier or name")
    review_parser.add_argument("--reason", required=True, help="Explicit curation reason for the decision")
    review_parser.add_argument(
        "--reviewed-at",
        default=None,
        help="Optional ISO-8601 UTC timestamp (defaults to current UTC timestamp)",
    )
    review_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (text or json)",
    )

    # history
    history_parser = subparsers.add_parser(
        "history",
        help="List recorded curation review events from discovered_link_reviews",
    )
    history_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path",
    )
    history_parser.add_argument("--candidate-id", default=None, help="Filter by candidate_id")
    history_parser.add_argument("--reviewer", default=None, help="Filter by reviewer")
    history_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (text or json)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command or "summary"
    db_path = getattr(args, "db", None) or resolve_default_db_path()

    try:
        if cmd == "summary":
            source_id = getattr(args, "source_id", None)
            host_domain = getattr(args, "host_domain", None)
            domain_classification = getattr(args, "domain_classification", None)
            out_format = getattr(args, "format", "text")

            if _is_missing_db(db_path):
                # Validate filter values even if database is missing
                KnowledgeStore._build_candidate_filter_clause(
                    referring_source_id=source_id,
                    host_domain=host_domain,
                    domain_classification=domain_classification,
                )
                payload = empty_summary_payload(
                    db_path=db_path,
                    database_exists=False,
                    referring_source_id=source_id,
                    host_domain=host_domain,
                    domain_classification=domain_classification,
                )
            else:
                with KnowledgeStore(db_path=db_path, auto_initialize=True) as store:
                    payload = store.summarize_discovered_link_candidates(
                        referring_source_id=source_id,
                        host_domain=host_domain,
                        domain_classification=domain_classification,
                    )
                    payload["database_path"] = str(db_path)
                    payload["database_exists"] = True

            if out_format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(format_summary_text(payload))
            return 0

        if cmd == "list":
            limit, offset = _resolve_pagination(args)
            if _is_missing_db(db_path):
                _, _, filters = KnowledgeStore._build_candidate_filter_clause(
                    referring_resource_id=args.resource_id,
                    referring_source_id=args.source_id,
                    host_domain=args.host_domain,
                    domain_classification=args.domain_classification,
                    review_status=args.review_status,
                )
                result = {
                    "database_path": str(db_path),
                    "database_exists": False,
                    "total_count": 0,
                    "returned_count": 0,
                    "limit": limit,
                    "offset": offset,
                    "has_more": False,
                    "filters": filters,
                    "items": [],
                }
            else:
                with KnowledgeStore(db_path=db_path, auto_initialize=True) as store:
                    result = store.query_discovered_link_candidates(
                        referring_resource_id=args.resource_id,
                        referring_source_id=args.source_id,
                        host_domain=args.host_domain,
                        domain_classification=args.domain_classification,
                        review_status=args.review_status,
                        limit=limit,
                        offset=offset,
                    )
                    result["database_path"] = str(db_path)
                    result["database_exists"] = True

            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(format_candidate_list_text(result))
            return 0

        if cmd == "export":
            if args.offset < 0:
                raise ValueError(f"--offset must be >= 0, got {args.offset}")
            if args.limit is not None and args.limit <= 0:
                raise ValueError(f"--limit must be >= 1, got {args.limit}")

            if _is_missing_db(db_path):
                KnowledgeStore._build_candidate_filter_clause(
                    referring_resource_id=args.resource_id,
                    referring_source_id=args.source_id,
                    host_domain=args.host_domain,
                    domain_classification=args.domain_classification,
                    review_status=args.review_status,
                )
                if args.format == "json":
                    serialized = "[]\n"
                else:
                    buf = io.StringIO()
                    writer = csv.DictWriter(
                        buf,
                        fieldnames=list(KnowledgeStore.CANDIDATE_EXPORT_COLUMNS),
                        lineterminator="\n",
                    )
                    writer.writeheader()
                    serialized = buf.getvalue()
                if args.output is not None:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(serialized, encoding="utf-8")
            else:
                with KnowledgeStore(db_path=db_path, auto_initialize=True) as store:
                    serialized = store.export_discovered_link_candidates(
                        export_format=args.format,
                        output_path=args.output,
                        referring_resource_id=args.resource_id,
                        referring_source_id=args.source_id,
                        host_domain=args.host_domain,
                        domain_classification=args.domain_classification,
                        review_status=args.review_status,
                        limit=args.limit,
                        offset=args.offset,
                    )

            if args.output is None:
                sys.stdout.write(serialized)
            else:
                print(f"Exported candidates ({args.format.upper()}) to {args.output}")
            return 0

        if cmd == "review":
            if _is_missing_db(db_path):
                print(
                    f"Error: Cannot record review because database does not exist at {db_path}",
                    file=sys.stderr,
                )
                return 1
            with KnowledgeStore(db_path=db_path, auto_initialize=True) as store:
                updated = store.review_discovered_link_candidate(
                    candidate_id=args.candidate_id,
                    referring_resource_id=args.resource_id,
                    normalized_url=args.normalized_url,
                    decision=args.decision,
                    reviewer=args.reviewer,
                    reason=args.reason,
                    reviewed_at=args.reviewed_at,
                )
            if args.format == "json":
                print(json.dumps(updated, indent=2, ensure_ascii=False))
            else:
                print(
                    f"Review decision recorded ({updated['operation']}):\n"
                    f"  Candidate ID:             {updated['candidate_id']}\n"
                    f"  Normalized URL:           {updated['normalized_url']}\n"
                    f"  Original URL:             {updated['original_url']}\n"
                    f"  Referring resource ID:    {updated['referring_resource_id']}\n"
                    f"  Domain classification:    {updated['domain_classification']}\n"
                    f"  Review status:            {updated['review_status']}\n"
                    f"  Policy state:             {updated['policy_state']}\n"
                    f"  Fetch permission granted: {bool(updated['fetch_permission_granted'])}\n"
                    f"  Reviewed by:              {updated['reviewed_by']}\n"
                    f"  Reviewed at:              {updated['reviewed_at']}\n"
                    f"  Review reason:            {updated['review_reason']}\n"
                    f"  Review count:             {updated['review_count']}\n"
                    f"  Review event ID:          {updated.get('review_id')}"
                )
            return 0

        if cmd == "history":
            if _is_missing_db(db_path):
                events: list[dict[str, Any]] = []
            else:
                with KnowledgeStore(db_path=db_path, auto_initialize=True) as store:
                    events = store.list_discovered_link_reviews(
                        candidate_id=args.candidate_id,
                        reviewer=args.reviewer,
                    )
            if args.format == "json":
                print(json.dumps(events, indent=2, ensure_ascii=False))
            else:
                print(f"Discovered-Link Review History ({db_path}): {len(events)} event(s)")
                for ev in events:
                    print(
                        f"  - {ev['review_id']} | {ev['reviewed_at']} | candidate={ev['candidate_id']} | "
                        f"{ev['previous_review_status']} -> {ev['new_review_status']} | "
                        f"reviewer={ev['reviewer']} | fetch_permitted={bool(ev['fetch_permission_granted'])} | "
                        f"reason={ev['review_reason']!r}"
                    )
            return 0

    except (ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
