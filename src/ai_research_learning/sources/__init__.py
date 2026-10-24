"""Source adapter package for the AI Research & Learning Explorer.

Provides a unified, policy-gated connector contract across:
- SRC-HINTON (`HintonSourceAdapter`)
- SRC-BENGIO (`BengioSourceAdapter`)
- SRC-3B1B (`ThreeBlueOneBrownAdapter`)
- SRC-ARXIV (`ArxivSourceAdapter`)
- SRC-WIKIPEDIA (`WikipediaSourceAdapter`)
"""
from .arxiv_adapter import (
    ARXIV_MAX_RESULTS_LIMIT,
    ARXIV_POLICY_BASIS,
    ARXIV_POLICY_PROFILE_ID,
    ARXIV_POLICY_VERSION,
    ArxivSourceAdapter,
    extract_arxiv_id,
)
from .base import (
    DEFAULT_PROJECT_USER_AGENT,
    AdapterError,
    DiscoveredLinkCandidate,
    HttpRequestEvent,
    NormalizedResourceEnvelope,
    NotModifiedResponse,
    OriginRateLimiter,
    ParseError,
    PolicyAuditResult,
    PolicyBlockedError,
    RateLimitError,
    RawMetadataPayload,
    ScopeViolationError,
    SourceAdapter,
    SourceCandidate,
    TransientFetchError,
    canonicalize_url,
    host_of,
    is_binary_asset_url,
    is_identified_user_agent,
    origin_of,
    parse_retry_after,
)
from .html_adapters import (
    BengioSourceAdapter,
    ConservativeHtmlAdapter,
    HintonSourceAdapter,
    ThreeBlueOneBrownAdapter,
)
from .pilot import (
    MAX_PILOT_PER_SOURCE,
    MAX_PILOT_TOTAL,
    PilotCoordinator,
    PilotItemOutcome,
    PilotRunSummary,
    PolicyAuditSummary,
    build_default_adapters,
    format_pilot_run_summary,
    format_policy_audit_summary,
    load_normalized_seed_manifest,
)
from .wikipedia_adapter import WikipediaSourceAdapter

__all__ = [
    "ARXIV_MAX_RESULTS_LIMIT",
    "ARXIV_POLICY_BASIS",
    "ARXIV_POLICY_PROFILE_ID",
    "ARXIV_POLICY_VERSION",
    "DEFAULT_PROJECT_USER_AGENT",
    "MAX_PILOT_PER_SOURCE",
    "MAX_PILOT_TOTAL",
    "AdapterError",
    "ArxivSourceAdapter",
    "BengioSourceAdapter",
    "ConservativeHtmlAdapter",
    "DiscoveredLinkCandidate",
    "HintonSourceAdapter",
    "HttpRequestEvent",
    "NormalizedResourceEnvelope",
    "NotModifiedResponse",
    "OriginRateLimiter",
    "ParseError",
    "PilotCoordinator",
    "PilotItemOutcome",
    "PilotRunSummary",
    "PolicyAuditResult",
    "PolicyAuditSummary",
    "PolicyBlockedError",
    "RateLimitError",
    "RawMetadataPayload",
    "ScopeViolationError",
    "SourceAdapter",
    "SourceCandidate",
    "ThreeBlueOneBrownAdapter",
    "TransientFetchError",
    "WikipediaSourceAdapter",
    "build_default_adapters",
    "canonicalize_url",
    "extract_arxiv_id",
    "format_pilot_run_summary",
    "format_policy_audit_summary",
    "host_of",
    "is_binary_asset_url",
    "is_identified_user_agent",
    "load_normalized_seed_manifest",
    "origin_of",
    "parse_retry_after",
]
