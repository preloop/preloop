export interface GitCloneRepository {
  tracker_id: string;
  repository_url?: string;
  clone_path: string;
  branch?: string;
}

export interface GitCloneConfig {
  enabled: boolean;
  repositories?: GitCloneRepository[];
  git_user_name?: string;
  git_user_email?: string;
  source_branch?: string;
  target_branch?: string;
  create_pull_request?: boolean;
  pull_request_title?: string;
  pull_request_description?: string;
}

export interface FlowCustomCommands {
  enabled: boolean;
  commands?: string[];
}

export interface FlowFailureNotifications {
  comment_on_trigger_issue?: boolean;
  attention_item?: boolean;
}

export interface FlowSuccessNotifications {
  comment_on_trigger_issue?: boolean;
}

/** Per-flow terminal notifications. Null means no comments or attention items. */
export interface FlowNotifications {
  on_failure?: FlowFailureNotifications;
  on_success?: FlowSuccessNotifications;
}

export function defaultFlowNotifications(): FlowNotifications {
  return {
    on_failure: {
      comment_on_trigger_issue: false,
      attention_item: false,
    },
    on_success: {
      comment_on_trigger_issue: false,
    },
  };
}

export interface FlowWebhookConfig {
  webhook_secret: string;
}

/** Server-computed schedule state; read-only for the console. */
export interface FlowScheduleState {
  active: boolean;
  type: string;
  description: string;
  timezone: string;
  next_run_at: string | null;
  cron?: string;
}

export interface FlowExecutionStats {
  total_execs?: number;
  running_execs?: number;
  last_seen_at?: string | null;
  estimated_cost?: number;
  /**
   * Tokens over the same period as the counts beside them: the window when
   * `stats_since` was asked for, all time otherwise. Null means "nothing
   * attributable in that period", which is not zero tokens.
   */
  token_usage?: GatewayTokenUsage | null;
  /**
   * Present only when the flows request named a window (`stats_since`), and
   * then every field below is measured over that same window, so a list that
   * says "in the last 30d" can say one thing.
   */
  since?: string;
  runs?: number;
  failed?: number;
  cost?: number;
  last_run_at?: string | null;
}

/**
 * Canonical console-side shape of a flow. Views must import this rather than
 * redeclaring a local copy, so filter and trigger fields cannot drift.
 *
 * `trigger_config` holds the tracker event filters. `null` means "clear the
 * saved filters" on update; absent means "leave them untouched".
 */
export interface Flow {
  id?: string;
  name?: string;
  description?: string;
  icon?: string;
  account_id?: string;
  prompt_template?: string;
  agent_type?: string;
  agent_config?: Record<string, unknown>;
  ai_model_id?: string;
  trigger_event_source?: string;
  trigger_event_types?: string[];
  trigger_organization_id?: string;
  trigger_project_ids?: string[];
  trigger_config?: Record<string, unknown> | null;
  schedule_config?: Record<string, unknown> | null;
  schedule_state?: FlowScheduleState | null;
  webhook_config?: FlowWebhookConfig;
  allowed_mcp_servers?: string[];
  allowed_mcp_tools?: Array<{ server_name: string; tool_name: string }>;
  git_clone_config?: GitCloneConfig;
  notifications?: FlowNotifications | null;
  custom_commands?: FlowCustomCommands;
  timeout_seconds?: number | null;
  max_iterations?: number | null;
  max_budget?: number | null;
  is_preset?: boolean;
  is_enabled?: boolean;
  runner_pool?: string | null;
  execution_stats?: FlowExecutionStats;
  [key: string]: unknown;
}

export interface AIModel {
  id: string;
  name: string;
  description?: string | null;
  provider_name: string;
  model_kind?: 'llm' | 'stt' | 'tts';
  api_key?: string;
  has_api_key?: boolean;
  /**
   * Logical credential type, e.g. 'api_key', 'ambient_provider',
   * 'oauth_anthropic_claude_code', 'oauth_openai_codex'.
   */
  credential_type?: string | null;
  /**
   * False for principal-bound OAuth (Claude Code / Codex subscription)
   * credentials, which only authorize their owner's interactive traffic.
   * Never auto-select a model where this is false.
   */
  supports_server_side_generation?: boolean;
  credentials_secret_id?: string | null;
  credentials_backend_type?: string | null;
  api_endpoint?: string;
  model_identifier: string;
  meta_data?: Record<string, unknown> | null;
  is_default?: boolean;
  created_at: string;
  updated_at: string;
  account_id?: string;
}

export interface SpeechToTextResponse {
  text: string;
  ai_model_id: string;
  provider_name: string;
  model_identifier: string;
}

export interface TextToSpeechRequest {
  input: string;
  voice?: string;
  response_format?: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm';
  ai_model_id?: string | null;
}

export interface FlowGatewayConversationPreviewMessage {
  source?: string | null;
  role?: string | null;
  text?: string | null;
  redacted?: boolean;
  truncated?: boolean;
  original_length?: number | null;
}

export interface FlowGatewayConversationPreview {
  messages?: FlowGatewayConversationPreviewMessage[];
  metadata?: {
    message_count?: number | null;
    request_message_count?: number | null;
    response_message_count?: number | null;
    has_redacted_content?: boolean;
    has_truncated_content?: boolean;
  } | null;
}

export interface FlowGatewayCapturePolicy {
  content_capture_enabled?: boolean;
  max_preview_chars?: number | null;
  sensitive_fields_redacted?: boolean;
  content_redacted?: boolean;
  content_truncated?: boolean;
  conversation_preview_available?: boolean;
}

export interface FlowGatewayEventPayload {
  api_usage_id?: string | null;
  endpoint?: string | null;
  endpoint_kind?: string | null;
  method?: string | null;
  status_code?: number | null;
  outcome?: string | null;
  duration_ms?: number | null;
  user_id?: string | null;
  auth_subject_type?: string | null;
  api_key_id?: string | null;
  ai_model_id?: string | null;
  model_alias?: string | null;
  provider_name?: string | null;
  gateway_provider?: string | null;
  requested_model?: string | null;
  upstream_request_id?: string | null;
  request_fingerprint?: string | null;
  gateway_attempt?: number | null;
  is_retry?: boolean | null;
  retry_of_api_usage_id?: string | null;
  finish_reason?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
  estimated_cost?: number | null;
  runtime_principal?: {
    type?: string | null;
    id?: string | null;
    name?: string | null;
  } | null;
  budget?: Record<string, unknown> | null;
  error_detail?: string | null;
  capture_policy?: FlowGatewayCapturePolicy | null;
  conversation_preview?: FlowGatewayConversationPreview | null;
  request?: unknown;
  response?: unknown;
  message?: string | null;
  [key: string]: unknown;
}

export interface FlowGatewayEvent {
  id: string;
  execution_id: string;
  timestamp: string | null;
  type: string;
  payload: FlowGatewayEventPayload;
}

export interface FlowGatewayEventsResponse {
  logs: FlowGatewayEvent[];
  source: 'container' | 'database';
  pagination?: {
    limit: number;
    offset: number;
    next_offset: number | null;
    total: number;
    has_more: boolean;
  } | null;
}

/**
 * Token totals, split by direction and by cache participation.
 *
 * `input_tokens` / `output_tokens` are the product-facing names for the
 * provider wire names `prompt_tokens` / `completion_tokens`; the backend
 * always sends both pairs in agreement, so a surface can read either.
 *
 * The cache fields describe the input side only: `cache_read_tokens` was
 * served from a prompt cache, `cache_write_tokens` was written into one and
 * `uncached_input_tokens` is the remainder the provider read afresh.
 * `cache_hit_ratio` is null when no request in the aggregate reported a
 * cache split: unknown, which is not "0% hit".
 */
export interface GatewayTokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  uncached_input_tokens?: number;
  cache_hit_ratio?: number | null;
}

export interface GatewayBudgetSummary {
  monthly_limit_usd: number | null;
  soft_limit_usd: number | null;
  current_spend_usd: number;
  soft_limit_exceeded: boolean;
  hard_limit_exceeded: boolean;
}

export interface GatewayUsageByDay {
  date: string;
  request_count: number;
  estimated_cost: number;
  total_tokens: number;
}

export interface GatewayUsageByModel {
  ai_model_id: string | null;
  model_alias: string | null;
  provider_name: string | null;
  request_count: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  last_request_at?: string | null;
  /**
   * Requests this model served that carry no cost at all, and requests it
   * served at exactly $0. Optional: servers older than wave 8 omit both, and
   * the console falls back to `estimated_cost` alone there.
   */
  unpriced_request_count?: number;
  zero_priced_request_count?: number;
  failed_request_count?: number;
}

export interface GatewayUsageByFlow {
  flow_id: string | null;
  flow_name: string | null;
  request_count: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
}

export interface GatewayUsageByExecution {
  flow_execution_id: string | null;
  request_count: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  last_request_at: string | null;
}

export interface GatewayUsageBySession {
  ai_model_id?: string | null;
  runtime_session_id?: string | null;
  runtime_session_name?: string | null;
  session_source_type?: string | null;
  session_source_id?: string | null;
  title?: string | null;
  session_summary?: string | null;
  session_summary_updated_at?: string | null;
  runtime_principal_type?: string | null;
  runtime_principal_id?: string | null;
  runtime_principal_name?: string | null;
  agent_id?: string | null;
  agent_name?: string | null;
  flow_execution_id: string | null;
  flow_id: string | null;
  flow_name: string | null;
  session_reference: string | null;
  model_alias: string | null;
  provider_name: string | null;
  request_count: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  started_at?: string | null;
  last_activity_at?: string | null;
  last_request_at: string | null;
  ended_at?: string | null;
}

export interface GatewayToolUsageByAgent {
  runtime_principal_type?: string | null;
  runtime_principal_id?: string | null;
  runtime_principal_name?: string | null;
  agent_id?: string | null;
  invocation_count: number;
  estimated_schema_cost: number;
}

export interface GatewayUsageByTool {
  tool_name: string;
  server_name?: string | null;
  invocation_count: number;
  successful_invocations: number;
  failed_invocations: number;
  schema_injections: number;
  schema_tokens_total: number;
  estimated_schema_cost: number;
  avg_cost_per_invocation: number;
  last_activity_at?: string | null;
  usage_by_agent: GatewayToolUsageByAgent[];
}

export interface ToolUsageStatsResponse {
  period_start: string;
  period_end: string;
  tools: GatewayUsageByTool[];
}

export interface GatewayUsageSearchResultItem {
  api_usage_id: string;
  timestamp: string;
  status_code: number;
  outcome: 'success' | 'error';
  endpoint: string;
  method: string;
  provider_name: string | null;
  model_alias: string | null;
  flow_id: string | null;
  flow_name: string | null;
  flow_execution_id: string | null;
  runtime_session_id: string | null;
  session_source_type: string | null;
  session_source_id: string | null;
  session_reference: string | null;
  runtime_principal_type: string | null;
  runtime_principal_id: string | null;
  runtime_principal_name: string | null;
  auth_subject_type: string | null;
  api_key_id: string | null;
  api_key_name: string | null;
  estimated_cost: number;
  token_usage: GatewayTokenUsage;
  excerpt: string;
  meta_data: Record<string, unknown>;
}

export interface AccountGatewayUsageSearchResponse {
  period_start: string;
  period_end: string;
  query: string | null;
  total: number;
  limit: number;
  offset: number;
  items: GatewayUsageSearchResultItem[];
}

export interface RuntimeSessionSummary {
  id: string;
  title?: string | null;
  summary?: string | null;
  session_source_type: string;
  session_source_id: string;
  session_reference: string | null;
  runtime_principal_type: string | null;
  runtime_principal_id: string | null;
  runtime_principal_name: string | null;
  started_at: string;
  last_activity_at: string | null;
  ended_at: string | null;
  flow_id: string | null;
  flow_name: string | null;
  flow_execution_id: string | null;
  latest_model_alias: string | null;
  latest_provider_name: string | null;
  is_active_now: boolean;
  activity_status: 'active_now' | 'idle' | 'ended' | string;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  last_request_at: string | null;
  optimization_waste_score?: number | null;
  optimization_potential_savings_tokens?: number | null;
  optimization_potential_savings_usd?: number | null;
}

export interface AccountRuntimeSessionListResponse {
  period_start: string;
  period_end: string;
  query: string | null;
  session_source_type: string | null;
  status: 'all' | 'active' | 'ended';
  total: number;
  limit: number;
  offset: number;
  items: RuntimeSessionSummary[];
}

export interface AccountRuntimeSessionDetailResponse {
  period_start: string;
  period_end: string;
  session: RuntimeSessionSummary;
  usage_by_model: GatewayUsageByModel[];
  interactions: AccountGatewayUsageSearchResponse;
  activity_timeline: RuntimeSessionActivityItem[];
}

export interface ManagedAgentSummary {
  id: string;
  runtime_session_id: string | null;
  owner_user_id: string | null;
  owner_username: string | null;
  owner_email: string | null;
  agent_kind?: string | null;
  display_name: string;
  session_source_type: string;
  session_source_id: string;
  session_reference: string | null;
  enrolled_via: string;
  tags?: Record<string, string>;
  managed_mcp_servers: string[];
  lifecycle_state: 'active' | 'suspended' | 'decommissioned' | string;
  lifecycle_reason: string | null;
  lifecycle_updated_at: string | null;
  is_active_now: boolean;
  activity_status:
    | 'active_now'
    | 'recently_active'
    | 'idle'
    | 'ended'
    | 'suspended'
    | 'decommissioned'
    | string;
  last_seen_at: string;
  started_at: string | null;
  last_activity_at: string | null;
  ended_at: string | null;
  total_requests: number;
  successful_requests?: number;
  failed_requests?: number;
  /** Tokens the agent spent, stated before cost in the agents list. */
  token_usage?: GatewayTokenUsage;
  estimated_cost: number;
  configured_model_alias: string | null;
  configured_model_id?: string | null;
  configured_models?: ManagedAgentModelBindingSummary[];
  latest_model_alias: string | null;
  latest_provider_name: string | null;
  last_request_at: string | null;
  mcp_proxy_configured: boolean;
  model_gateway_configured: boolean;
  onboarding_state:
    | 'fully_onboarded'
    | 'mcp_proxy_only'
    | 'gateway_only'
    | 'incomplete'
    | string;
  live_validation_supported: boolean;
  live_validation_passed: boolean | null;
  live_validation_status:
    'unsupported' | 'not_run' | 'passed' | 'failed' | string;
  last_validated_at: string | null;
  control_feature_name?: string;
  control_capabilities?: string[];
  control_state?:
    | 'unsupported'
    | 'install_pending'
    | 'plugin_configured'
    | 'plugin_connected'
    | string;
  control_enabled?: boolean;
  control_online?: boolean;
  supports_new_session?: boolean;
  supports_existing_session?: boolean;
  supports_voice?: boolean;
  supports_interrupt?: boolean;
  control_session_mode?: 'local' | 'remote' | 'queued' | 'offline' | string;
  /** Last Agent Control heartbeat, so the age of the presence signal is readable. */
  control_last_heartbeat_at?: string | null;
  supported_input_modes?: string[];
  supported_output_modes?: string[];
}

export interface AgentControlCommandRequest {
  message: string;
  metadata?: Record<string, unknown>;
  target_session_id?: string | null;
  session_mode?: 'new' | 'existing' | string;
  start_new_session?: boolean;
  interrupt?: boolean;
  spawn_worktree?: boolean;
}

export interface AgentControlVoiceTranscriptRequest {
  transcript: string;
  metadata?: Record<string, unknown>;
  voice?: Record<string, unknown>;
  target_session_id?: string | null;
  start_new_session?: boolean;
}

export interface AgentControlCommandResponse {
  id?: string;
  command_id?: string;
  status?: string;
  target_session_id?: string | null;
  runtime_session_id?: string | null;
  session_mode?: string | null;
  message?: string | null;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface ManagedAgentModelBindingSummary {
  id: string;
  ai_model_id: string | null;
  binding_type: string;
  config_key: string;
  gateway_alias: string;
  is_primary: boolean;
  status: string;
  provider_name?: string | null;
  model_identifier?: string | null;
  ai_model_name?: string | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
}

export interface ManagedAgentUsageAggregate {
  session_count: number;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  latest_model_alias: string | null;
  latest_provider_name: string | null;
  last_request_at: string | null;
}

export interface ManagedAgentServerActivitySummary {
  server_name: string | null;
  call_count: number;
  successful_calls: number;
  failed_calls: number;
  last_activity_at: string | null;
}

export interface ManagedAgentToolActivitySummary {
  server_name: string | null;
  tool_name: string | null;
  call_count: number;
  successful_calls: number;
  failed_calls: number;
  last_activity_at: string | null;
}

export interface AccountManagedAgentListResponse {
  query: string | null;
  agent_kind: string | null;
  last_seen_after: string | null;
  status: 'all' | 'active' | 'ended';
  total: number;
  limit: number;
  offset: number;
  items: ManagedAgentSummary[];
}

export interface ManagedAgentDetailResponse {
  agent: ManagedAgentSummary;
  aggregate: ManagedAgentUsageAggregate;
  usage_by_model: GatewayUsageByModel[];
  activity_by_server: ManagedAgentServerActivitySummary[];
  activity_by_tool: ManagedAgentToolActivitySummary[];
  sessions: RuntimeSessionSummary[];
}

export interface ManagedAgentUpdateRequest {
  owner_user_id?: string | null;
  display_name?: string | null;
  tags?: Record<string, string> | null;
  lifecycle_action?: 'suspend' | 'resume' | 'decommission' | 'reenroll';
  reason?: string | null;
}

export interface SubjectGovernanceConfig {
  allowed_models: string[];
  model_budgets: Record<
    string,
    {
      monthly_usd_limit?: number | null;
      soft_limit_usd?: number | null;
    }
  >;
  tool_rules: Record<string, Array<Record<string, unknown>>>;
  tool_enabled_overrides?: Record<string, boolean>;
  /**
   * Approval workflow governing this subject's native tool-call approvals
   * (agents permission-check). Null falls back to the account default.
   */
  approval_workflow_id?: string | null;
  /**
   * Whether native tool-call approvals (agents permission-check) are
   * enforced for this subject. "off" makes the server auto-allow escalated
   * (ask) calls instead of asking a human; absent/null means "enforce".
   */
  native_tool_approvals?: 'enforce' | 'off' | null;
}

export interface SubjectGovernanceResponse {
  subject_type: string;
  subject_id: string;
  config: SubjectGovernanceConfig;
}

/**
 * Account-wide governance defaults inherited by every managed agent that
 * carries no explicit per-agent value. Final fallback is "enforce".
 */
export interface AccountGovernanceDefaults {
  native_tool_approvals?: 'enforce' | 'off' | null;
  approval_workflow_id?: string | null;
}

export interface AccountGovernanceDefaultsResponse {
  defaults: AccountGovernanceDefaults;
  /** Managed agent ids carrying an explicit per-agent override. */
  override_agent_ids: string[];
}

export interface RuntimeSessionUpdateRequest {
  action: 'end';
  reason?: string | null;
}

export interface RuntimeSessionActivityItem {
  activity_type:
    | 'model_interaction'
    | 'tool_call'
    | 'session_started'
    | 'session_ended'
    | 'agent_control_message'
    | string;
  timestamp: string;
  title: string;
  summary: string | null;
  status: string | null;
  api_usage_id: string | null;
  tool_name: string | null;
  server_name: string | null;
  auth_subject_type: string | null;
  api_key_id: string | null;
  api_key_name: string | null;
  estimated_cost: number | null;
  total_tokens: number | null;
  request_fingerprint?: string | null;
  gateway_attempt?: number | null;
  is_retry?: boolean;
  retry_of_api_usage_id?: string | null;
  metadata?: Record<string, unknown>;
}

export interface RuntimeSessionActivityListResponse {
  items: RuntimeSessionActivityItem[];
}

export interface RuntimeSessionRequestTool {
  name: string | null;
  source: string | null;
  schema_tokens_estimate: number;
  stripped: boolean;
}

/**
 * Prompt-cache accounting for one gateway request.
 *
 * `null` on any token field means the provider did NOT report that number.
 * It must be rendered as "not reported", never as zero — a `0` here is a real
 * provider-reported zero and carries the opposite meaning.
 */
export interface RuntimeSessionRequestCache {
  cache_read_tokens: number | null;
  cache_creation_tokens: number | null;
  cache_miss_tokens: number | null;
  /** 'reported' = provider sent a miss count; 'derived' = prompt - read - write. */
  cache_miss_source: 'reported' | 'derived' | null;
  has_cache_data: boolean;
  usage_source: string | null;
}

export interface RuntimeSessionRequestItem {
  id: string;
  timestamp: string | null;
  model_alias: string | null;
  provider_name: string | null;
  status_code: number;
  is_error: boolean;
  finish_reason: string | null;
  is_retry: boolean;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  endpoint: string | null;
  tools: RuntimeSessionRequestTool[];
  tools_total_schema_tokens: number;
  cache?: RuntimeSessionRequestCache;
}

/**
 * Whole-session prompt-cache rollup.
 *
 * The hit ratio covers only requests whose provider reported a cache split;
 * `uncovered_prompt_tokens` is the traffic excluded from that denominator.
 * `cache_write_tokens` is null when no provider in the session has a billable
 * cache-write concept at all. `estimated_cache_savings_usd` is null unless the
 * price catalog supports an exact figure, with `savings_omitted_reason` set.
 */
export interface RuntimeSessionCacheModelGroup {
  model_alias: string | null;
  provider_name: string | null;
  requests: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  prompt_tokens: number;
  /** Rows whose provider reported no prompt total; excluded from
   *  `prompt_tokens` rather than counted as zero. */
  requests_with_unknown_prompt_tokens?: number;
  write_reported: boolean;
}

export interface RuntimeSessionCacheSummary {
  requests_total: number;
  requests_with_cache_data: number;
  requests_without_cache_data: number;
  covered_prompt_tokens: number;
  uncovered_prompt_tokens: number;
  cached_prompt_tokens: number;
  uncached_prompt_tokens: number;
  cache_write_tokens: number | null;
  cache_hit_ratio: number | null;
  estimated_cache_savings_usd: number | null;
  /** 'catalog_exact' | 'catalog_exact_partial' (lower bound) | null. */
  savings_basis: string | null;
  savings_omitted_reason: string | null;
  /** Covered requests whose provider reported no prompt total at all. */
  requests_with_unknown_prompt_tokens?: number;
  models?: RuntimeSessionCacheModelGroup[];
}

export interface RuntimeSessionRequestListResponse {
  items: RuntimeSessionRequestItem[];
  total: number;
  failed_count: number;
  limit: number;
  offset: number;
  next_offset: number | null;
  has_more: boolean;
  cache_summary?: RuntimeSessionCacheSummary;
}

export interface RuntimeSessionSummaryInsight {
  title: string;
  description: string;
  risk_level: 'low' | 'medium' | 'high' | string;
  highlights: string[];
  next_action: string | null;
  generated_by: 'local' | 'model' | string;
  fast_model_name: string | null;
  estimated_summary_cost: number;
}

export interface RuntimeSessionInteractionSummary {
  event_id: string;
  title: string;
  summary: string;
  key_points: string[];
  risk_level: 'low' | 'medium' | 'high' | string;
  next_action: string | null;
  generated_by: 'local' | 'model' | string;
  model_name: string | null;
  estimated_summary_cost: number;
}

export interface RuntimeSessionOptimizationActionSpec {
  type:
    | 'scope_tools'
    | 'set_budget'
    | 'open_events'
    | 'manage_output_filter'
    | string;
  params: Record<string, unknown>;
}

export interface ToolOutputFilter {
  id: string;
  account_id: string;
  server_name: string | null;
  tool_name: string;
  managed_agent_id: string | null;
  dropped_fields: string[];
  enabled: boolean;
  created_at: string;
}

export interface ToolOutputFilterListResponse {
  items: ToolOutputFilter[];
}

export interface ToolOutputFilterCreateRequest {
  server_name: string | null;
  tool_name: string;
  dropped_fields: string[];
  managed_agent_id: string | null;
}

export interface RuntimeSessionOptimizationSuggestion {
  id: string;
  title: string;
  description: string;
  expected_savings_tokens: number;
  expected_savings_usd: number;
  confidence: 'low' | 'medium' | 'high' | string;
  action_label: string;
  evidence: string[];
  evidence_event_ids?: string[];
  action?: RuntimeSessionOptimizationActionSpec | null;
}

export interface SessionContextProfileSegment {
  kind: string;
  estimated_tokens: number;
  share: number;
  event_ids?: string[];
  sample_excerpt?: string | null;
}

/** One measured idle-TTL prompt-cache expiry from context analysis. */
export interface SessionCacheIdleExpiryEvent {
  event_id: string;
  previous_event_id: string;
  api_usage_id?: string | null;
  idle_seconds: number;
  provider_ttl_seconds?: number;
  provider_name?: string | null;
  rewritten_tokens?: number;
  previous_cache_read_tokens?: number;
  current_cache_read_tokens?: number;
  measured_extra_cost_usd?: number | null;
}

export interface SessionCacheProfile {
  avg_repeated_prefix_tokens?: number;
  repeated_prefix_share?: number;
  prefix_stability?: string;
  cache_breaking_events?: Array<Record<string, unknown>>;
  measured_cache_read_tokens?: number;
  idle_expiry_events?: SessionCacheIdleExpiryEvent[];
  measured_idle_expiry_tokens?: number;
  measured_idle_expiry_extra_cost_usd?: number;
}

export interface SessionContextProfileData {
  session_id: string;
  analyzed_event_count: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  segments?: SessionContextProfileSegment[];
  cache_profile?: SessionCacheProfile | null;
  retry_profile?: Record<string, unknown> | null;
  tool_bloat?: Record<string, unknown> | null;
  tool_schema_overhead?: Record<string, unknown> | null;
}

export interface RuntimeSessionOptimizationResponse {
  generated_by: 'local' | 'model' | string;
  fast_model_name: string | null;
  model_id?: string | null;
  model_name?: string | null;
  token_usage?: GatewayTokenUsage;
  estimated_optimization_cost?: number;
  generated_at?: string | null;
  from_cache?: boolean;
  cache_miss?: boolean;
  llm_skipped_reason?: string | null;
  waste_score?: number | null;
  potential_savings_tokens?: number;
  potential_savings_usd?: number;
  analyzed_scope_total_tokens?: number;
  analyzed_scope_estimated_cost?: number;
  context_profile?: SessionContextProfileData | null;
  suggestions: RuntimeSessionOptimizationSuggestion[];
  /**
   * True only for the bundled example session. Never set for a real session.
   * When true the UI MUST label the result as sample data and MUST NOT treat
   * its figures as belonging to the account.
   */
  is_example?: boolean;
  example_notice?: string | null;
  example_provenance?: string | null;
  example_title?: string | null;
  example_pricing_note?: string | null;
}

/** Acknowledgement for an accepted async optimization analysis job. */
export interface RuntimeSessionOptimizationJobSubmitResponse {
  job_id: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | string;
}

/** Poll response for one async optimization analysis job. */
export interface RuntimeSessionOptimizationJobStatusResponse {
  job_id: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | string;
  /** Shaped exactly like the inline response; set only on success. */
  result: RuntimeSessionOptimizationResponse | null;
  /** User-facing failure message; set only on failure. */
  error: string | null;
}

export interface RuntimeSessionReplayResponse {
  id: string;
  runtime_session_id: string;
  status: 'completed' | 'aborted_budget' | 'no_payload' | string;
  input_delta_tokens: number;
  input_pct_saved: number;
  end_to_end_delta_median: number | null;
  end_to_end_delta_low: number | null;
  end_to_end_delta_high: number | null;
  inconclusive: boolean;
  cost_spent: number | null;
  n_runs: number;
}

export interface RuntimeSessionOptimizationAppliedAction {
  id: string;
  runtime_session_id: string;
  suggestion_id: string;
  suggestion_title: string | null;
  action_type: string;
  params: Record<string, unknown>;
  status: string;
  applied_by: string | null;
  applied_at: string;
  result: Record<string, unknown>;
  baseline?: Record<string, unknown> | null;
  outcome?: Record<string, unknown> | null;
}

export interface RuntimeSessionOptimizationActionListResponse {
  items: RuntimeSessionOptimizationAppliedAction[];
}

export interface AccountGatewayUsageSummaryResponse {
  period_start: string;
  period_end: string;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  unpriced_requests?: number;
  unpriced_tokens?: number;
  price_catalog?: PriceCatalogInfo | null;
  budget: GatewayBudgetSummary;
  requests_by_day: GatewayUsageByDay[];
  usage_by_model: GatewayUsageByModel[];
  usage_by_flow: GatewayUsageByFlow[];
  usage_by_session: GatewayUsageBySession[];
  usage_by_tool?: GatewayUsageByTool[];
  /**
   * Set when the server answered from a pre-aggregated rollup rather than the
   * raw request rows, which is how a year of usage comes back in a second.
   * A rollup is coarser than the rows behind it, so the card says where the
   * number came from instead of implying it counted every request.
   *
   * Both spellings are read because neither is universal across deployments;
   * absent on servers that do not roll up, and the label is then not shown.
   */
  from_rollup?: boolean | null;
  source?: string | null;
}

export interface RateLimitTotals {
  rate_limited_requests: number;
  // Sum of provider-advised Retry-After values observed on 429 responses:
  // a lower bound on wall-clock stall, read from real provider headers.
  blocked_ms: number;
  last_rate_limited_at: string | null;
  quota_exhausted_count: number;
  transient_count: number;
}

export interface RateLimitByModel {
  model_alias: string | null;
  provider_name: string | null;
  rate_limited_requests: number;
  blocked_ms: number;
  last_rate_limited_at: string | null;
}

export interface RateLimitBySession {
  runtime_session_id: string | null;
  runtime_principal_name: string | null;
  rate_limited_requests: number;
  blocked_ms: number;
  last_rate_limited_at: string | null;
}

export interface RateLimitSnapshotData {
  retry_after_ms?: number;
  requests_limit?: number;
  requests_remaining?: number;
  requests_reset_at?: string;
  requests_reset_after_ms?: number;
  tokens_limit?: number;
  tokens_remaining?: number;
  tokens_reset_at?: string;
  tokens_reset_after_ms?: number;
  subtype?: string;
  subtype_source?: string;
  headers?: Record<string, string>;
}

export interface RateLimitSnapshotItem {
  provider_name: string | null;
  model_alias: string | null;
  observed_at: string;
  status_code: number;
  upstream_credential_type: string | null;
  rate_limit: RateLimitSnapshotData;
}

export interface AccountRateLimitReportResponse {
  period_start: string;
  period_end: string;
  totals: RateLimitTotals;
  by_model: RateLimitByModel[];
  by_session: RateLimitBySession[];
  latest_snapshots: RateLimitSnapshotItem[];
}

export interface PriceCatalogInfo {
  source_url?: string | null;
  fetched_at?: string | null;
  model_count?: number | null;
}

/** One price in USD per million tokens, as providers publish them. */
export interface AIModelPrice {
  input_per_1m?: number | null;
  output_per_1m?: number | null;
  cached_input_per_1m?: number | null;
  blended_per_1m?: number | null;
  request_price?: number | null;
}

export type AIModelPricingSource =
  'override' | 'model_config' | 'catalog' | 'none';

/** What one model actually costs this account, and where that came from. */
export interface AIModelPricingResponse {
  ai_model_id: string;
  model_alias?: string | null;
  provider_name?: string | null;
  source: AIModelPricingSource;
  price: AIModelPrice;
  currency: string;
  override_id?: string | null;
  effective_from?: string | null;
  effective_until?: string | null;
  catalog_key?: string | null;
  /** True when the provider publishes a price list Preloop can read. */
  fetch_supported: boolean;
  fetch_provider_label?: string | null;
}

/** A price read from the provider for confirmation. Nothing is saved. */
export interface AIModelPriceQuote {
  ai_model_id: string;
  provider_name?: string | null;
  source_url: string;
  model_key: string;
  price: AIModelPrice;
  currency: string;
  fetched_at: string;
}

export interface ModelPriceOverride {
  id: string;
  account_id: string;
  ai_model_id: string | null;
  provider_name: string | null;
  model_alias: string;
  currency: string;
  fx_rate_to_usd?: number | null;
  input_price_per_1k: number | null;
  output_price_per_1k: number | null;
  cache_read_input_price_per_1k: number | null;
  cache_creation_input_price_per_1k: number | null;
  price_per_1k: number | null;
  request_price: number | null;
  discount_percent: number | null;
  prepaid_token_balance: number | null;
  prepaid_credit_balance_usd: number | null;
  effective_from: string | null;
  effective_until: string | null;
  is_active: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export type ModelPriceOverrideCreate = Omit<
  ModelPriceOverride,
  'id' | 'account_id' | 'created_at' | 'updated_at'
>;

export type ModelPriceOverrideUpdate = Partial<ModelPriceOverrideCreate>;

// Usage ingested from outside the gateway (e.g. a Cursor CSV/JSON export).
// Reported as its own block so imported spend is never mixed into
// `estimated_cost` or any budget figure (issue #123).
export interface ImportedUsageByModel {
  model_alias: string | null;
  source: string | null;
  request_count: number;
  total_tokens: number;
  imported_cost: number;
  last_event_at: string | null;
}

// One source-side conversation (thread) of imported usage. `estimated_cost`
// (hook/transcript-derived) and `reconciled_cost` (billing export) are kept
// as SEPARATE fields and must never be summed into one number. `null` means
// the source reported nothing ("not reported") — it is not zero. Entries
// whose parent_conversation_id matches another entry's conversation_id are
// subagent workers of that parent thread.
export interface ImportedUsageByConversation {
  conversation_id: string;
  parent_conversation_id: string | null;
  source: string | null;
  event_count: number;
  total_tokens: number | null;
  estimated_cost: number | null;
  reconciled_cost: number | null;
  last_event_at: string | null;
}

export interface ImportedUsageSummary {
  event_count: number;
  total_tokens: number;
  imported_cost: number;
  usage_by_model: ImportedUsageByModel[];
  // Absent on older servers; the console treats missing as "no rollup".
  usage_by_conversation?: ImportedUsageByConversation[];
}

// One model's share of the window's unpriced gateway usage. Names the models
// behind `unpriced_requests`/`unpriced_tokens` so the banner can point at a
// fix instead of only counting the damage.
export interface UnpricedModelUsage {
  model: string;
  requests: number;
  tokens: number;
}

export interface CostAnalyticsSummaryResponse extends AccountGatewayUsageSummaryResponse {
  // Absent (null) when the window contains no imported usage.
  imported_usage?: ImportedUsageSummary | null;
  // Absent on older servers; the console treats missing as "none named".
  unpriced_models?: UnpricedModelUsage[];
}

export interface ProviderBillingConnection {
  id: string;
  provider: string;
  is_active: boolean;
  last_synced_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface CostReconciliationRow {
  provider: string;
  date: string;
  preloop_cost: number;
  provider_cost: number;
  drift_abs: number;
  drift_pct: number | null;
  preloop_tokens: number;
  provider_tokens: number;
}

export interface CostReconciliationResponse {
  rows: CostReconciliationRow[];
  total_preloop_cost: number;
  total_provider_cost: number;
  total_drift_abs: number;
  total_drift_pct: number | null;
}

export interface RepriceResponse {
  submitted_async: boolean;
  // Null when submitted_async: nothing was scanned in-request, which is
  // different from "the window contained 0 rows".
  rows_examined: number | null;
  rows_updated: number | null;
  rows_skipped: number | null;
  cost_before: number | null;
  cost_after: number | null;
  dry_run: boolean;
}

export interface FlowGatewayUsageSummaryResponse {
  flow_id: string;
  flow_name: string | null;
  period_start: string;
  period_end: string;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  budget: GatewayBudgetSummary;
  usage_by_model: GatewayUsageByModel[];
  usage_by_execution: GatewayUsageByExecution[];
}

export interface AIModelGatewayUsageSummaryResponse {
  ai_model_id: string;
  model_name: string;
  provider_name: string;
  model_identifier: string;
  period_start: string;
  period_end: string;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  requests_by_day: GatewayUsageByDay[];
  usage_by_session: GatewayUsageBySession[];
}

export interface AIModelOverviewItem {
  ai_model_id: string;
  model_name: string;
  provider_name: string;
  model_identifier: string;
  model_alias: string | null;
  is_default: boolean;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  unpriced_request_count: number;
  active_session_count: number;
  last_request_at: string | null;
  pricing_source: 'override' | 'model_config' | 'catalog' | 'none';
}

export interface AIModelsOverviewResponse {
  period_start: string;
  period_end: string;
  models: AIModelOverviewItem[];
}

export interface ApiKeyGatewayUsageSummaryResponse {
  api_key_id: string;
  period_start: string;
  period_end: string;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  token_usage: GatewayTokenUsage;
  estimated_cost: number;
  requests_by_day: GatewayUsageByDay[];
  usage_by_model: GatewayUsageByModel[];
  usage_by_session: GatewayUsageBySession[];
}

export type AIModelRuntimeSessionListResponse =
  AccountRuntimeSessionListResponse;

export type AIModelGatewayUsageSearchResponse =
  AccountGatewayUsageSearchResponse;

export interface FetchIssuesListParams {
  query?: string;
  project_ids?: string[];
  status?: 'opened' | 'closed' | 'all';
  limit?: number;
  skip?: number;
  sort_by?: string;
  sort_order?: string;
}

export interface SearchIssuesParams {
  query: string;
  search_type: 'similarity' | 'fulltext';
  embedding_type: 'issue' | 'comment';
  project_ids?: string[];
  limit?: number;
}

export interface SearchResultItem {
  item_type: 'issue' | 'comment';
  item: any; // Using 'any' for comment for now
  similarity: number;
}

export interface SearchIssuesResponse {
  results: SearchResultItem[];
}

export type IssueStatus = 'opened' | 'closed' | 'all';

export interface ApiKey {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  key?: string;
  managed_agent_id?: string | null;
  runtime_principal_type?: string | null;
  runtime_principal_id?: string | null;
  runtime_principal_name?: string | null;
  last_activity_at?: string | null;
  activity_status?:
    'active_now' | 'recently_active' | 'idle' | 'revoked' | string;
  recent_model_calls?: number;
  recent_tool_calls?: number;
}

export interface Project {
  id: string;
  name: string;
  key?: string;
  identifier?: string;
  description?: string;
  url?: string;
  organization_id: string;
  tracker_id?: string;
}

export interface Organization {
  id: string;
  name: string;
  key?: string;
  identifier?: string;
  tracker_id: string;
}

export interface Issue {
  id: string;
  title: string;
  description: string;
  status: string;
  status_id: string;
  priority: string;
  priority_id: string;
  project_id: string;
  project_name: string;
  organization_id: string;
  organization_name: string;
  created_at: string;
  updated_at: string;
  key: string;
  source: string;
  url: string;
  labels?: string[] | null;
  assignee?: string | null;
}

export interface IssueListItem {
  id: string;
  external_id: string;
  key: string;
  title: string;
  description?: string | null;
  status?: string | null;
  priority?: string | null;
  assignee?: string | null;
  labels?: string[] | null;
  organization: string;
  project: string;
  project_id: string;
  project_identifier?: string | null;
  url: string;
  created_at: string;
  updated_at: string;
}

export interface IssueListResponse {
  items: IssueListItem[];
  total: number;
  skip: number;
  limit: number;
}

export interface PullRequestListItem {
  number: number;
  iid: number;
  title: string;
  description?: string | null;
  url: string;
  author?: string | null;
  source_branch?: string | null;
  target_branch?: string | null;
  state: string;
  draft: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PullRequestListResponse {
  items: PullRequestListItem[];
  page: number;
  limit: number;
  has_more: boolean;
  supported: boolean;
  fetched_at: string;
}

export type VerdictStatus =
  'checking' | 'done' | 'failed' | 'timeout' | 'no_model';

export interface VerdictState {
  state: VerdictStatus;
  verdict?: {
    decision?: string;
    reason?: string;
    suggestion?: string;
    resolution?: string;
  };
}

export interface DuplicatePair {
  issue1: Issue;
  issue2: Issue;
  similarity: number;
  verified_as_duplicate: boolean | null;
}

export interface DuplicatesResponse {
  duplicates: DuplicatePair[];
}

export interface IssueComplianceResult {
  id: string;
  prompt_id: string;
  name: string;
  short_name: string;
  compliance_factor: number;
  reason: string;
  suggestion: string;
  annotated_description?: string;
  issue_id: string;
  created_at: string;
  updated_at: string;
}

export interface CompliancePromptMetadata {
  id: string;
  name: string;
  short_name: string;
}

export interface IssueEmbedding {
  issue_id: string;
  project_id: string;
  issue_key: string;
  issue_title: string;
  issue_created_at: string;
  embedding: number[];
}

export interface ComplianceSuggestion {
  title: string;
  description: string;
  changes: string;
}

export interface DependencyPair {
  source_issue_id: string;
  dependent_issue_id: string;
  reason: string;
  confidence_score: number;
  issue_key?: string;
  dependency_key?: string;
  is_committed: boolean;
  comes_from_tracker: boolean;
}

export interface DependencyResponse {
  dependencies: DependencyPair[];
}

// User Management Types
export interface User {
  id: string;
  account_id: string;
  username: string;
  email: string;
  email_verified: boolean;
  full_name: string | null;
  is_active: boolean;
  user_source: string;
  oauth_provider: string | null;
  last_login: string | null;
  created_at: string;
  updated_at: string;
}

export interface UserCreate {
  username: string;
  email: string;
  full_name?: string;
  password: string;
  user_source?: string;
  is_active?: boolean;
}

export interface UserUpdate {
  email?: string;
  full_name?: string;
  is_active?: boolean;
}

export interface UserListResponse {
  users: User[];
  total: number;
  skip: number;
  limit: number;
}

// Team Management Types
export interface Team {
  id: string;
  account_id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface TeamMember {
  id: string;
  team_id: string;
  user_id: string;
  role_id: string | null;
  joined_at: string;
  user?: User;
}

export interface TeamCreate {
  name: string;
  description?: string;
}

export interface TeamUpdate {
  name?: string;
  description?: string;
}

export interface TeamListResponse {
  teams: Team[];
  total: number;
  skip: number;
  limit: number;
}

// Invitation Management Types
export interface UserInvitation {
  id: string;
  account_id: string;
  email: string;
  invited_by_user_id: string;
  token: string;
  status: 'pending' | 'accepted' | 'expired' | 'cancelled';
  expires_at: string;
  accepted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface InvitationCreate {
  email: string;
  role_ids?: string[];
  team_ids?: string[];
}

export interface InvitationListResponse {
  invitations: UserInvitation[];
  total: number;
  skip: number;
  limit: number;
}

// Role Management Types
export interface Role {
  id: string;
  name: string;
  description: string | null;
  is_system_role: boolean;
  permissions: string[];
}

export interface RoleListResponse {
  roles: Role[];
  total: number;
}

export interface DashboardTelemetryResponse {
  active_agents: number;
  total_tool_calls: number;
  daily_cost: number;
  success_rate: number;
}

export type ApprovalRequestStatus =
  'pending' | 'approved' | 'declined' | 'expired' | 'cancelled';

/**
 * Attribution: who asked for an approval, resolved by the server.
 *
 * The ids alone rendered as truncated UUIDs, or worse, as the generic label
 * "AI agent" when the name column happened to be empty. The server now
 * resolves each id it holds into a name, and omits the part when the row it
 * points at is gone, so the console never has to guess.
 */
export interface ApprovalAgentSummary {
  id: string;
  name: string;
  /** `claude_code`, `cursor`, and so on. Absent on older agents. */
  kind?: string | null;
}

export interface ApprovalApiKeySummary {
  id: string;
  name: string;
}

export interface ApprovalSessionSummary {
  id: string;
  /** What the session is about; null when it has no title or reference. */
  subject?: string | null;
}

export interface ApprovalFlowExecutionSummary {
  id: string;
  flow_id?: string | null;
  flow_name?: string | null;
}

/**
 * A pending human decision surfaced by the approvals API.
 *
 * Two flavours share this shape:
 *  - tool approvals (approve / decline, optional comment)
 *  - agent questions (`is_question`, tool_name `ask_user`): the operator picks
 *    one of `question_options` or types free text when `allow_free_text`.
 *
 * The question fields are optional so older backends that omit them degrade to
 * the plain approve/decline UI.
 */
export interface ApprovalRequest {
  id: string;
  account_id: string;
  tool_configuration_id: string;
  approval_workflow_id: string;
  execution_id: string | null;
  tool_name: string;
  summary?: string | null;
  tool_args: Record<string, any>;
  agent_reasoning: string | null;
  status: ApprovalRequestStatus;
  requested_at: string;
  resolved_at: string | null;
  expires_at: string | null;
  approver_comment: string | null;
  /** Set when an onboarded agent asked; links the request to its agent page. */
  managed_agent_id?: string | null;
  /** The runtime session the call came from, when the agent had one. */
  runtime_session_id?: string | null;
  managed_agent_name?: string | null;
  /** The credential the caller authenticated with. Known for every API call. */
  api_key_id?: string | null;
  /**
   * The same four facts, named. Absent on older servers, in which case the
   * ids above still carry the links.
   */
  agent?: ApprovalAgentSummary | null;
  api_key?: ApprovalApiKeySummary | null;
  session?: ApprovalSessionSummary | null;
  flow_execution?: ApprovalFlowExecutionSummary | null;
  webhook_posted_at?: string | null;
  webhook_error?: string | null;
  is_question?: boolean;
  question?: string | null;
  question_options?: string[] | null;
  allow_free_text?: boolean;
  /** True when an AI judged this request rather than a person. */
  decided_by_ai?: boolean;
  /**
   * Set when a time-boxed bypass auto-approved this request without any human
   * review (currently always `'bypass'`). Surfaces must render these
   * distinctly from human decisions.
   */
  auto_approved_reason?: string | null;
  auto_approval_bypass_id?: string | null;
  /** Convenience mirror of `auto_approved_reason !== null`. */
  was_bypassed?: boolean;
  /**
   * True only when a person actually made this decision. Every approval
   * statistic must filter on this — counting bypassed calls as approvals
   * would overstate how much human oversight actually happened.
   */
  decided_by_human?: boolean;
  /**
   * Why this request exists: the policy rule that gated the call, captured
   * when the request was created. Absent on rows created before this field
   * existed and on approvals raised without rule evaluation (the
   * `request_approval` builtin). Surfaces must omit the explanation rather
   * than guess at one.
   */
  rule_context?: ApprovalRuleContext | null;
}

/**
 * Where an approval's gating came from.
 *
 * `tool_access_rule` and `subject_scoped_rule` name an actual rule with an
 * expression. The rest describe gating that no rule produced, and carry an
 * `explanation` instead.
 */
export type ApprovalRuleContextSource =
  | 'tool_access_rule'
  | 'subject_scoped_rule'
  | 'tool_default_workflow'
  | 'rule_evaluation_error'
  | 'agent_permission_hook'
  | 'model_io_rule';

/**
 * Snapshot of the policy rule that required an approval.
 *
 * Recorded at request creation, not recomputed at read time, so editing or
 * deleting a rule later cannot rewrite the reason a past approval was asked
 * for. It states WHAT matched. It is not a risk assessment: nobody scored
 * this call, an expression simply evaluated true.
 */
export interface ApprovalRuleContext {
  source: ApprovalRuleContextSource;
  /** The policy action taken. In practice always `'require_approval'`. */
  decision: string;
  /** Rule description, falling back to its expression, then a generic label. */
  rule_name: string;
  /** Plain statement used when no named rule fired. */
  explanation?: string;
  rule_id?: string;
  /** The condition as the operator wrote it, e.g. `args.amount > 1000`. */
  expression?: string;
  expression_type?: string;
  /** Evaluation order; lower runs first. */
  priority?: number;
  /** Argument names the expression mentions. Presentational hint only. */
  referenced_args?: string[];
  /** Tool config the rule belongs to, for linking to where it is edited. */
  tool_configuration_id?: string;
  /** Lower-priority rules that would also have matched. Informational. */
  also_matched_rule_ids?: string[];
  /** Detector attributes for model I/O holds (never a full prompt). */
  detector_summary?: Record<string, unknown>;
}

/** Mode of a time-boxed approval bypass. */
export type ApprovalBypassMode = 'mute_notifications' | 'auto_approve';

/**
 * A deliberate, expiring relaxation of approval gating.
 *
 * `mute_notifications` silences alerts while approvals still block the agent;
 * `auto_approve` also approves automatically and is a governance bypass.
 */
export interface ApprovalBypass {
  id: string;
  account_id: string;
  user_id: string;
  managed_agent_id: string | null;
  mode: ApprovalBypassMode;
  reason: string | null;
  created_by_user_id: string;
  created_via: string | null;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  revoked_by_user_id: string | null;
  auto_approved_count: number;
  is_account_wide?: boolean;
  suppresses_approvals?: boolean;
}

/** Aggregate bypass state used to drive the console warning banner. */
export interface ApprovalBypassStatus {
  active: boolean;
  auto_approve_active: boolean;
  bypasses: ApprovalBypass[];
  soonest_expiry?: string | null;
}

/** Traffic classes the account kill switch can halt independently. */
export type KillSwitchScope = 'gateway' | 'tools' | 'flows';

/** One currently-halted scope with its activation audit data. */
export interface KillSwitchScopeState {
  scope: KillSwitchScope;
  activated_by_user_id: string | null;
  activated_by_username: string | null;
  activated_at: string | null;
  reason: string | null;
}

/**
 * Aggregate kill-switch state used to drive the halted-state banner.
 *
 * `active` is true when any scope is halted; each halted scope carries who
 * activated it, when, and why, so the banner can attribute the halt.
 */
export interface KillSwitchStatus {
  active: boolean;
  scopes: KillSwitchScopeState[];
}

/**
 * Optional payload carried on an approve/decline decision.
 *
 * Server-side precedence: `answer_text` > `selected_option` > `comment`.
 */
export interface ApprovalDecisionOptions {
  comment?: string | null;
  selected_option?: string | null;
  answer_text?: string | null;
}
