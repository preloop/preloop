"""Schemas for model gateway usage summaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GatewayTokenUsage(BaseModel):
    """Token usage totals."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class GatewayBudgetSummary(BaseModel):
    """Budget snapshot for gateway usage."""

    monthly_limit_usd: Optional[float] = None
    soft_limit_usd: Optional[float] = None
    current_spend_usd: float = 0.0
    soft_limit_exceeded: bool = False
    hard_limit_exceeded: bool = False


class GatewayUsageByDay(BaseModel):
    """Daily usage aggregate."""

    date: str
    request_count: int = 0
    estimated_cost: float = 0.0
    total_tokens: int = 0


class GatewayUsageByModel(BaseModel):
    """Usage aggregate grouped by model."""

    ai_model_id: Optional[str] = None
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    request_count: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    last_request_at: Optional[datetime] = None


class GatewayToolUsageByAgent(BaseModel):
    """Tool activity attributed to one runtime principal (managed agent)."""

    runtime_principal_type: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    runtime_principal_name: Optional[str] = None
    agent_id: Optional[str] = None
    invocation_count: int = 0
    estimated_schema_cost: float = 0.0


class GatewayUsageByTool(BaseModel):
    """Tool usage aggregate combining invocation counts and schema-injection cost."""

    tool_name: str
    server_name: Optional[str] = None
    invocation_count: int = 0
    successful_invocations: int = 0
    failed_invocations: int = 0
    schema_injections: int = 0
    schema_tokens_total: int = 0
    estimated_schema_cost: float = 0.0
    avg_cost_per_invocation: float = 0.0
    last_activity_at: Optional[datetime] = None
    usage_by_agent: List["GatewayToolUsageByAgent"] = Field(default_factory=list)


class ManagedAgentModelBindingSummary(BaseModel):
    """One configured AI model binding for a managed agent."""

    id: str
    ai_model_id: Optional[str] = None
    binding_type: str
    config_key: str
    gateway_alias: str
    is_primary: bool = False
    status: str
    provider_name: Optional[str] = None
    model_identifier: Optional[str] = None
    ai_model_name: Optional[str] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None


class ManagedAgentModelBindingSyncItem(BaseModel):
    """Upsert payload for one managed-agent model binding."""

    ai_model_id: str
    binding_type: str = "configured"
    config_key: str
    gateway_alias: str
    is_primary: bool = False
    status: str = "gateway_ready"


class ManagedAgentModelBindingSyncRequest(BaseModel):
    """Replace request for one managed agent's configured model bindings."""

    bindings: List[ManagedAgentModelBindingSyncItem] = Field(default_factory=list)


class GatewayUsageByFlow(BaseModel):
    """Usage aggregate grouped by flow."""

    flow_id: Optional[str] = None
    flow_name: Optional[str] = None
    request_count: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0


class GatewayUsageByExecution(BaseModel):
    """Usage aggregate grouped by flow execution."""

    flow_execution_id: Optional[str] = None
    request_count: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    last_request_at: Optional[datetime] = None


class GatewayUsageBySession(BaseModel):
    """Recent usage aggregate grouped by execution-backed session slices."""

    ai_model_id: Optional[str] = None
    runtime_session_id: Optional[str] = None
    session_source_type: Optional[str] = None
    session_source_id: Optional[str] = None
    title: Optional[str] = None
    session_summary: Optional[str] = None
    session_summary_updated_at: Optional[datetime] = None
    runtime_principal_type: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    runtime_principal_name: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    flow_execution_id: Optional[str] = None
    flow_id: Optional[str] = None
    flow_name: Optional[str] = None
    session_reference: Optional[str] = None
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    request_count: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    last_request_at: Optional[datetime] = None


class GatewayUsageSearchResultItem(BaseModel):
    """One account-scoped gateway interaction search hit."""

    api_usage_id: str
    ai_model_id: Optional[str] = None
    timestamp: datetime
    status_code: int
    outcome: str
    endpoint: str
    method: str
    provider_name: Optional[str] = None
    model_alias: Optional[str] = None
    flow_id: Optional[str] = None
    flow_name: Optional[str] = None
    flow_execution_id: Optional[str] = None
    runtime_session_id: Optional[str] = None
    session_source_type: Optional[str] = None
    session_source_id: Optional[str] = None
    session_reference: Optional[str] = None
    runtime_principal_type: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    runtime_principal_name: Optional[str] = None
    auth_subject_type: Optional[str] = None
    api_key_id: Optional[str] = None
    api_key_name: Optional[str] = None
    estimated_cost: float = 0.0
    token_usage: GatewayTokenUsage
    excerpt: str
    meta_data: dict = Field(default_factory=dict)


class AccountGatewayUsageSearchResponse(BaseModel):
    """Account-scoped gateway interaction search results."""

    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    query: Optional[str] = None
    total: int = 0
    limit: int = 20
    offset: int = 0
    items: List[GatewayUsageSearchResultItem] = Field(default_factory=list)


class RuntimeSessionSummary(BaseModel):
    """Aggregated runtime session summary for explorer views."""

    id: str
    session_source_type: str
    session_source_id: str
    session_reference: Optional[str] = None
    runtime_principal_type: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    runtime_principal_name: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    summary_updated_at: Optional[datetime] = None
    started_at: datetime
    last_activity_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    flow_id: Optional[str] = None
    flow_name: Optional[str] = None
    flow_execution_id: Optional[str] = None
    latest_model_alias: Optional[str] = None
    latest_provider_name: Optional[str] = None
    is_active_now: bool = False
    activity_status: str = "idle"
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    last_request_at: Optional[datetime] = None
    optimization_waste_score: Optional[int] = None
    optimization_potential_savings_tokens: Optional[int] = None
    optimization_potential_savings_usd: Optional[float] = None


class AccountRuntimeSessionListResponse(BaseModel):
    """Account-scoped runtime session explorer list response."""

    period_start: datetime
    period_end: datetime
    query: Optional[str] = None
    session_source_type: Optional[str] = None
    status: str = "all"
    total: int = 0
    limit: int = 20
    offset: int = 0
    items: List[RuntimeSessionSummary] = Field(default_factory=list)


class ManagedAgentSummary(BaseModel):
    """Read-only summary for one enrolled external agent."""

    id: str
    runtime_session_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    owner_username: Optional[str] = None
    owner_email: Optional[str] = None
    agent_kind: Optional[str] = None
    display_name: str
    session_source_type: str
    session_source_id: str
    session_reference: Optional[str] = None
    enrollment_hostname: Optional[str] = None
    identity_derivation: Optional[str] = None
    enrolled_via: str
    managed_mcp_servers: List[str] = Field(default_factory=list)
    lifecycle_state: str = "active"
    lifecycle_reason: Optional[str] = None
    lifecycle_updated_at: Optional[datetime] = None
    is_active_now: bool = False
    activity_status: str = "idle"
    last_seen_at: datetime
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    estimated_cost: float = 0.0
    configured_model_alias: Optional[str] = None
    configured_model_id: Optional[str] = None
    configured_models: List[ManagedAgentModelBindingSummary] = Field(
        default_factory=list
    )
    latest_model_alias: Optional[str] = None
    latest_provider_name: Optional[str] = None
    last_request_at: Optional[datetime] = None
    mcp_proxy_configured: bool = False
    model_gateway_configured: bool = False
    onboarding_state: str = "incomplete"
    live_validation_supported: bool = False
    live_validation_passed: Optional[bool] = None
    live_validation_status: str = "unsupported"
    last_validated_at: Optional[datetime] = None
    tags: dict[str, str] = Field(default_factory=dict)
    control_feature_name: str = "Agent Control"
    control_capabilities: List[str] = Field(default_factory=list)
    control_state: str = "unsupported"
    control_enabled: bool = False
    control_online: bool = False
    supports_new_session: bool = False
    supports_existing_session: bool = False
    supports_voice: bool = False
    supports_interrupt: bool = False
    supported_input_modes: List[str] = Field(default_factory=list)
    supported_output_modes: List[str] = Field(default_factory=list)


class ManagedAgentUsageAggregate(BaseModel):
    """Historical usage aggregate across all sessions for one managed agent."""

    session_count: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    latest_model_alias: Optional[str] = None
    latest_provider_name: Optional[str] = None
    last_request_at: Optional[datetime] = None


class ManagedAgentServerActivitySummary(BaseModel):
    """Historical tool activity grouped by MCP server for one managed agent."""

    server_name: Optional[str] = None
    call_count: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    last_activity_at: Optional[datetime] = None


class ManagedAgentToolActivitySummary(BaseModel):
    """Historical tool activity grouped by MCP server and tool name."""

    server_name: Optional[str] = None
    tool_name: Optional[str] = None
    call_count: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    last_activity_at: Optional[datetime] = None


class ManagedAgentCredentialSummary(BaseModel):
    """Durable credential metadata for one managed agent."""

    id: str
    api_key_id: str
    created_by_user_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    credential_type: str
    status: str
    scopes: List[str] = Field(default_factory=list)
    key_prefix: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_issued_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None


class ManagedAgentRegisterRequest(BaseModel):
    """Request to register a custom managed agent the CLI cannot discover.

    Used when an operator wants to onboard an agent (for example a customer's
    LangGraph agent) that has never connected through a runtime session, so a
    gateway credential can subsequently be minted for it.
    """

    display_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class ManagedAgentCredentialCreateRequest(BaseModel):
    """Request to create a durable credential for one managed agent."""

    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=365, ge=1, le=3650)
    scopes: List[str] = Field(default_factory=lambda: ["mcp:read", "mcp:write"])


class ManagedAgentCredentialCreateResponse(BaseModel):
    """One-time response payload for a newly created agent credential."""

    credential: ManagedAgentCredentialSummary
    token: str


class ManagedAgentEnrollmentSummary(BaseModel):
    """Durable enrollment state for one managed agent."""

    id: str
    created_by_user_id: Optional[str] = None
    enrollment_type: str
    adapter_key: Optional[str] = None
    status: str
    target_config_path: Optional[str] = None
    discovered_config: dict = Field(default_factory=dict)
    managed_config: dict = Field(default_factory=dict)
    backup_metadata: dict = Field(default_factory=dict)
    validation_result: dict = Field(default_factory=dict)
    restore_available: bool = False
    last_applied_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    last_restored_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ManagedAgentEnrollmentCreateRequest(BaseModel):
    """Request to persist an enrollment record for one managed agent."""

    enrollment_type: str = Field(..., min_length=1, max_length=64)
    adapter_key: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(default="pending", min_length=1, max_length=32)
    target_config_path: Optional[str] = Field(default=None, max_length=512)
    discovered_config: dict = Field(default_factory=dict)
    managed_config: dict = Field(default_factory=dict)
    backup_metadata: dict = Field(default_factory=dict)
    validation_result: dict = Field(default_factory=dict)
    restore_available: bool = False
    last_applied_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    last_restored_at: Optional[datetime] = None


class ManagedAgentEnrollmentValidateRequest(BaseModel):
    """Request to update validation state for one enrollment."""

    status: str = Field(default="validated", min_length=1, max_length=32)
    validation_result: dict = Field(default_factory=dict)


class ManagedAgentEnrollmentRestoreRequest(BaseModel):
    """Request to mark one enrollment as restored."""

    status: str = Field(default="restored", min_length=1, max_length=32)
    backup_metadata: dict = Field(default_factory=dict)
    validation_result: dict = Field(default_factory=dict)


class AccountManagedAgentListResponse(BaseModel):
    """Account-scoped managed-agent registry response."""

    query: Optional[str] = None
    agent_kind: Optional[str] = None
    last_seen_after: Optional[datetime] = None
    status: str = "all"
    total: int = 0
    limit: int = 20
    offset: int = 0
    items: List[ManagedAgentSummary] = Field(default_factory=list)


class ManagedAgentDetailResponse(BaseModel):
    """One managed agent plus its recent runtime session history."""

    agent: ManagedAgentSummary
    aggregate: ManagedAgentUsageAggregate
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)
    activity_by_server: List[ManagedAgentServerActivitySummary] = Field(
        default_factory=list
    )
    activity_by_tool: List[ManagedAgentToolActivitySummary] = Field(
        default_factory=list
    )
    sessions: List[RuntimeSessionSummary] = Field(default_factory=list)
    credentials: List[ManagedAgentCredentialSummary] = Field(default_factory=list)
    enrollments: List[ManagedAgentEnrollmentSummary] = Field(default_factory=list)


class ManagedAgentUpdateRequest(BaseModel):
    """Operator-driven updates for one managed agent."""

    owner_user_id: Optional[str] = None
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    lifecycle_action: Optional[str] = Field(
        default=None, pattern="^(suspend|resume|decommission|reenroll)$"
    )
    reason: Optional[str] = None
    tags: Optional[dict[str, str]] = None


class PrincipalIdentityLite(BaseModel):
    """Identity metadata accepted on rekey/token flows."""

    hostname: Optional[str] = Field(None, max_length=255)
    config_path: Optional[str] = Field(None, max_length=1024)
    source_type: Optional[str] = Field(None, max_length=64)
    derivation: Optional[str] = Field(None, max_length=16)


class ManagedAgentRekeyRequest(BaseModel):
    """Request body for rewriting a managed agent's durable principal id."""

    new_session_source_id: str = Field(..., min_length=1, max_length=255)
    principal_identity: Optional[PrincipalIdentityLite] = None


class ManagedAgentMergeRequest(BaseModel):
    """Request body for merging a duplicate managed agent into a survivor."""

    duplicate_agent_id: str = Field(..., min_length=1)
    dry_run: bool = False


class ManagedAgentIdentityMutationCounts(BaseModel):
    """Row counts returned by rekey/merge dry-run and execute responses."""

    usage_moved: int = 0
    usage_deleted: int = 0
    runtime_sessions_moved: int = 0
    budget_spend_moved: int = 0
    budget_spend_merged: int = 0
    budget_policies_moved: int = 0
    budget_policies_dropped: int = 0
    approvals_moved: int = 0
    keys_deactivated: int = 0
    dropped_budget_policies: List[Dict[str, Any]] = Field(default_factory=list)


class ManagedAgentRekeyResponse(BaseModel):
    """Response for a successful rekey."""

    agent: ManagedAgentSummary
    counts: ManagedAgentIdentityMutationCounts


class ManagedAgentMergeResponse(BaseModel):
    """Response for a merge dry-run or execute."""

    survivor: ManagedAgentSummary
    duplicate: ManagedAgentSummary
    dry_run: bool
    counts: ManagedAgentIdentityMutationCounts


class RuntimeSessionUpdateRequest(BaseModel):
    """Operator-driven updates for one runtime session."""

    action: str = Field(pattern="^(end)$")
    reason: Optional[str] = None


class RuntimeSessionActivityItem(BaseModel):
    """One activity item in a runtime session timeline."""

    activity_type: str
    timestamp: datetime
    title: str
    summary: Optional[str] = None
    status: Optional[str] = None
    api_usage_id: Optional[str] = None
    tool_name: Optional[str] = None
    server_name: Optional[str] = None
    auth_subject_type: Optional[str] = None
    api_key_id: Optional[str] = None
    api_key_name: Optional[str] = None
    estimated_cost: Optional[float] = None
    total_tokens: Optional[int] = None
    request_fingerprint: Optional[str] = None
    gateway_attempt: Optional[int] = None
    is_retry: bool = False
    retry_of_api_usage_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class RuntimeSessionActivityListResponse(BaseModel):
    """List of activity items for a runtime session."""

    items: List[RuntimeSessionActivityItem] = Field(default_factory=list)


class RuntimeSessionRequestTool(BaseModel):
    """One tool carried by a single gateway request."""

    name: Optional[str] = None
    source: Optional[str] = None
    schema_tokens_estimate: int = 0
    stripped: bool = False


class RuntimeSessionRequestItem(BaseModel):
    """One per-request gateway usage row for the unified session timeline."""

    id: str
    timestamp: Optional[datetime] = None
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    status_code: int = 0
    is_error: bool = False
    finish_reason: Optional[str] = None
    is_retry: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    endpoint: Optional[str] = None
    tools: List[RuntimeSessionRequestTool] = Field(default_factory=list)
    tools_total_schema_tokens: int = 0


class RuntimeSessionRequestListResponse(BaseModel):
    """Paginated per-request rows for one runtime session."""

    items: List[RuntimeSessionRequestItem] = Field(default_factory=list)
    total: int = 0
    failed_count: int = 0
    limit: int = 100
    offset: int = 0
    next_offset: Optional[int] = None
    has_more: bool = False


class RuntimeSessionSummaryInsight(BaseModel):
    """Generated or locally derived summary for one runtime session."""

    title: str
    description: str
    risk_level: str = "low"
    highlights: List[str] = Field(default_factory=list)
    next_action: Optional[str] = None
    generated_by: str = "local"
    fast_model_name: Optional[str] = None
    estimated_summary_cost: float = 0.0


class RuntimeSessionInteractionSummary(BaseModel):
    """Generated or locally derived summary for one gateway interaction."""

    event_id: str
    title: str
    summary: str
    key_points: List[str] = Field(default_factory=list)
    risk_level: str = "low"
    next_action: Optional[str] = None
    generated_by: str = "local"
    model_name: Optional[str] = None
    estimated_summary_cost: float = 0.0


class RuntimeSessionOptimizationActionSpec(BaseModel):
    """Machine-applicable action attached to one optimization suggestion.

    Supported types: ``scope_tools`` (disable unused tools via subject-scoped
    governance), ``set_budget`` (create a scoped budget policy),
    ``enable_compression`` / ``cap_tool_results`` (subject-scoped context
    optimization transforms), ``manage_output_filter`` (open the output-filter
    UI to drop unused bulky tool-result fields; ``params``: ``server_name``,
    ``tool_name``, ``suggested_fields``, ``managed_agent_id``), and
    ``open_events`` (client-side replay deep-link to evidence events).
    """

    type: str
    params: Dict[str, Any] = Field(default_factory=dict)


class RuntimeSessionOptimizationSuggestion(BaseModel):
    """One actionable optimization suggestion for a runtime session."""

    id: str
    title: str
    description: str
    expected_savings_tokens: int = 0
    expected_savings_usd: float = 0.0
    confidence: str = "medium"
    action_label: str
    evidence: List[str] = Field(default_factory=list)
    evidence_event_ids: List[str] = Field(default_factory=list)
    action: Optional[RuntimeSessionOptimizationActionSpec] = None


class RuntimeSessionOptimizationApplyRequest(BaseModel):
    """Request payload to apply one suggestion's action server-side."""

    suggestion_id: str
    suggestion_title: Optional[str] = None
    action: RuntimeSessionOptimizationActionSpec


class RuntimeSessionOptimizationAppliedAction(BaseModel):
    """One applied optimization action, with measured outcome when available."""

    id: str
    runtime_session_id: str
    suggestion_id: str
    suggestion_title: Optional[str] = None
    action_type: str
    params: Dict[str, Any] = Field(default_factory=dict)
    status: str = "applied"
    applied_by: Optional[str] = None
    applied_at: datetime
    result: Dict[str, Any] = Field(default_factory=dict)
    baseline: Optional[Dict[str, Any]] = None
    outcome: Optional[Dict[str, Any]] = None


class RuntimeSessionOptimizationActionListResponse(BaseModel):
    """Applied optimization actions for one runtime session."""

    items: List[RuntimeSessionOptimizationAppliedAction] = Field(default_factory=list)


class RuntimeSessionOptimizationRequest(BaseModel):
    """Generation controls for runtime-session optimization suggestions."""

    model_id: Optional[str] = None
    event_ids: List[str] = Field(default_factory=list)
    source_kinds: List[str] = Field(default_factory=list)
    from_index: Optional[int] = None
    to_index: Optional[int] = None
    regenerate: bool = False
    # When true, return the latest cached result for the session without
    # generating anything (used to surface previously generated suggestions on
    # panel open). A miss returns ``cache_miss=True`` instead of generating.
    cache_only: bool = False


class RuntimeSessionOptimizationResponse(BaseModel):
    """Optimization suggestions for a runtime session."""

    generated_by: str = "local"
    # True only for a cache_only request that found no cached result; signals the
    # UI to show the "generate" prompt rather than an empty-suggestions state.
    cache_miss: bool = False
    fast_model_name: Optional[str] = None
    model_id: Optional[str] = None
    model_name: Optional[str] = None
    token_usage: GatewayTokenUsage = Field(default_factory=GatewayTokenUsage)
    estimated_optimization_cost: float = 0.0
    generated_at: Optional[datetime] = None
    from_cache: bool = False
    llm_skipped_reason: Optional[str] = None
    waste_score: Optional[int] = None
    potential_savings_tokens: int = 0
    potential_savings_usd: float = 0.0
    # Totals for the analyzed scope (the events actually fed to the optimizer),
    # so the UI can show savings against a coherent baseline instead of the
    # whole-session summary, which may be unloaded or wider than the scope.
    analyzed_scope_total_tokens: int = 0
    analyzed_scope_estimated_cost: float = 0.0
    context_profile: Optional[Dict[str, Any]] = None
    suggestions: List[RuntimeSessionOptimizationSuggestion] = Field(
        default_factory=list
    )
    # Example-session labelling. ``is_example`` is True ONLY for the bundled
    # sample analyzed by preloop.services.example_optimization; it is never set
    # for a real session. Clients must label such a response unmistakably as
    # sample data and must not fold its figures into the account's own totals.
    is_example: bool = False
    example_notice: Optional[str] = None
    example_provenance: Optional[str] = None
    example_title: Optional[str] = None
    example_pricing_note: Optional[str] = None


class AccountRuntimeSessionDetailResponse(BaseModel):
    """One runtime session detail summary. (Interactions and activity moved to sub-endpoints)."""

    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    session: RuntimeSessionSummary
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)


class RateLimitTotals(BaseModel):
    """Aggregate 429 telemetry for the report window.

    ``blocked_ms`` sums the provider-advised ``Retry-After`` values observed
    on 429 responses: a lower bound on real wall-clock stall, taken directly
    from provider responses (never estimated). Subtype counts come from the
    per-row classification recorded at capture time; rows recorded before
    this feature carry no subtype and appear in neither count.
    """

    rate_limited_requests: int = 0
    blocked_ms: int = 0
    last_rate_limited_at: Optional[datetime] = None
    quota_exhausted_count: int = 0
    transient_count: int = 0


class RateLimitByModel(BaseModel):
    """429 telemetry grouped by model."""

    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    rate_limited_requests: int = 0
    blocked_ms: int = 0
    last_rate_limited_at: Optional[datetime] = None


class RateLimitBySession(BaseModel):
    """429 telemetry grouped by runtime session (agent run)."""

    runtime_session_id: Optional[str] = None
    runtime_principal_name: Optional[str] = None
    rate_limited_requests: int = 0
    blocked_ms: int = 0
    last_rate_limited_at: Optional[datetime] = None


class RateLimitSnapshotItem(BaseModel):
    """Latest observed rate-limit headers for one provider/model pair.

    ``rate_limit`` echoes the snapshot persisted from a real upstream
    response (normalized fields plus the verbatim ``headers`` map);
    ``observed_at`` timestamps that observation so consumers can label
    staleness. Nothing here is inferred.
    """

    provider_name: Optional[str] = None
    model_alias: Optional[str] = None
    observed_at: datetime
    status_code: int
    upstream_credential_type: Optional[str] = None
    rate_limit: Dict[str, Any] = Field(default_factory=dict)


class AccountRateLimitReportResponse(BaseModel):
    """Account-scoped rate-limit telemetry and observed headroom report."""

    period_start: datetime
    period_end: datetime
    totals: RateLimitTotals
    by_model: List[RateLimitByModel] = Field(default_factory=list)
    by_session: List[RateLimitBySession] = Field(default_factory=list)
    latest_snapshots: List[RateLimitSnapshotItem] = Field(default_factory=list)


class AccountGatewayUsageSummaryResponse(BaseModel):
    """Account-scoped gateway usage summary."""

    period_start: datetime
    period_end: datetime
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    unpriced_requests: int = 0
    unpriced_tokens: int = 0
    budget: GatewayBudgetSummary
    requests_by_day: List[GatewayUsageByDay] = Field(default_factory=list)
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)
    usage_by_flow: List[GatewayUsageByFlow] = Field(default_factory=list)
    usage_by_session: List[GatewayUsageBySession] = Field(default_factory=list)
    usage_by_tool: List[GatewayUsageByTool] = Field(default_factory=list)


class ApiKeyGatewayUsageSummaryResponse(BaseModel):
    """API Key-scoped gateway usage summary."""

    api_key_id: str
    period_start: datetime
    period_end: datetime
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    requests_by_day: List[GatewayUsageByDay] = Field(default_factory=list)
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)
    usage_by_session: List[GatewayUsageBySession] = Field(default_factory=list)


class FlowGatewayUsageSummaryResponse(BaseModel):
    """Flow-scoped gateway usage summary."""

    flow_id: str
    flow_name: Optional[str] = None
    period_start: datetime
    period_end: datetime
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    budget: GatewayBudgetSummary
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)
    usage_by_execution: List[GatewayUsageByExecution] = Field(default_factory=list)


class DashboardTelemetryResponse(BaseModel):
    """Aggregate high-level metrics for the global dashboard control plane."""

    active_agents: int = 0
    total_tool_calls: int = 0
    daily_cost: float = 0.0
    success_rate: float = 0.0
