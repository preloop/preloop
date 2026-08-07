import type {
  FlowGatewayConversationPreviewMessage,
  FlowGatewayEvent,
  GatewayTokenUsage,
  GatewayUsageBySession,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
} from '../types';

export type SessionObserverScope =
  | 'account'
  | 'runtime_session'
  | 'managed_agent'
  | 'api_key'
  | 'ai_model'
  | 'audit';

// 'conversation' is the chat-style transcript (<session-chat-view>): only
// top-level prompts/responses expanded, everything else collapsed.
// 'timeline' is the original turn/delta transcript, 'chat' its talk-dialog
// variant; both keep rendering through <session-replay-panel>.
export type SessionReplayMode =
  'conversation' | 'timeline' | 'chat' | 'replay' | 'optimize';

export interface ObservedSession {
  id: string;
  sourceId: string | null;
  sourceType: string | null;
  title: string;
  subtitle: string | null;
  sessionReference: string | null;
  runtimePrincipalName: string | null;
  flowName: string | null;
  flowExecutionId: string | null;
  status: string;
  startedAt: string | null;
  lastActivityAt: string | null;
  endedAt: string | null;
  totalRequests: number;
  successfulRequests: number;
  failedRequests: number;
  tokenUsage: GatewayTokenUsage;
  estimatedCost: number;
  latestModelAlias: string | null;
  latestProviderName: string | null;
  canLoadEvents: boolean;
  optimizationWasteScore: number | null;
  optimizationPotentialSavingsTokens: number | null;
  optimizationPotentialSavingsUsd: number | null;
  raw: unknown;
}

export interface SessionObserverFeatures {
  summaries?: boolean;
  optimization?: boolean;
  auditLinks?: boolean;
  liveFollow?: boolean;
  replayModes?: boolean;
  rawPayloads?: boolean;
  endSession?: boolean;
}

export interface SessionSummaryInsight {
  title: string;
  description: string;
  riskLevel: 'low' | 'medium' | 'high';
  highlights: string[];
  nextAction: string | null;
  generatedBy: 'local' | 'model';
  estimatedSummaryCost: number;
}

export interface SessionOptimizationSuggestion {
  id: string;
  title: string;
  description: string;
  expectedSavingsTokens: number;
  expectedSavingsUsd: number;
  confidence: 'low' | 'medium' | 'high';
  actionLabel: string;
  evidence: string[];
  evidenceEventIds?: string[];
  action?: {
    type: string;
    params: Record<string, unknown>;
  } | null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object'
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function asNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function getTokenUsage(value: unknown): GatewayTokenUsage {
  const tokenUsage = asRecord(value);
  return {
    prompt_tokens: asNumber(tokenUsage.prompt_tokens),
    completion_tokens: asNumber(tokenUsage.completion_tokens),
    total_tokens: asNumber(tokenUsage.total_tokens),
  };
}

export function formatSessionSourceLabel(sourceType: string | null): string {
  if (!sourceType) return 'Session';
  if (sourceType === 'flow_execution') return 'Flow execution';
  if (sourceType === 'claude_code') return 'Claude Code';
  if (sourceType === 'claude_desktop') return 'Claude Desktop';
  if (sourceType === 'gemini_cli') return 'Gemini CLI';
  if (sourceType === 'opencode') return 'OpenCode';
  return sourceType
    .split(/[_-]+/g)
    .filter(Boolean)
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(' ');
}

function getStatus(row: Record<string, unknown>): string {
  const explicit = asString(row.activity_status);
  if (explicit) return explicit;
  if (row.ended_at) return 'ended';
  if (row.is_active_now === true) return 'active_now';
  return 'idle';
}

export function looksLikeFilePath(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) {
    return false;
  }
  if (trimmed.startsWith('/') || trimmed.startsWith('~')) {
    return true;
  }
  if (/^[A-Za-z]:[\\/]/.test(trimmed)) {
    return true;
  }
  return (
    (trimmed.includes('/') || trimmed.includes('\\')) &&
    /\.(json|ya?ml|toml|config|ini|env)$/i.test(trimmed)
  );
}

export function formatSessionIdLabel(id: string | null | undefined): string {
  if (!id) {
    return 'Session';
  }
  return id.length > 8 ? id.substring(0, 8) : id;
}

function getMeaningfulSessionReference(value: unknown): string | null {
  const reference = asString(value);
  if (!reference || looksLikeFilePath(reference)) {
    return null;
  }
  return reference;
}

function buildSessionTitleFallback(row: Record<string, unknown>): string {
  const sourceType = asString(row.session_source_type);
  const sourceLabel = formatSessionSourceLabel(sourceType);
  const sessionId = asString(row.id) || asString(row.runtime_session_id);
  const sourceId = asString(row.session_source_id);
  if (sessionId) {
    return `${sourceLabel} ${formatSessionIdLabel(sessionId)}`.trim();
  }
  if (sourceId) {
    return `${sourceLabel} ${sourceId}`.trim();
  }
  return 'Standalone API calls';
}

function buildTitle(row: Record<string, unknown>): string {
  return (
    asString(row.title) ||
    asString(row.summary) ||
    asString(row.session_summary) ||
    asString(row.session_alias) ||
    asString(row.runtime_session_name) ||
    asString(row.runtime_principal_name) ||
    asString(row.flow_name) ||
    getMeaningfulSessionReference(row.session_reference) ||
    asString(row.model_alias) ||
    buildSessionTitleFallback(row)
  );
}

export function normalizeObservedSession(
  session:
    RuntimeSessionSummary | GatewayUsageBySession | Record<string, unknown>
): ObservedSession {
  const row = asRecord(session);
  const runtimeSessionId =
    asString(row.id) || asString(row.runtime_session_id) || null;
  const sourceType = asString(row.session_source_type);
  const sourceId = asString(row.session_source_id);
  const id =
    runtimeSessionId ||
    `standalone:${sourceType || 'unknown'}:${sourceId || 'api'}`;
  const requestCount =
    asNumber(row.total_requests) || asNumber(row.request_count);
  const tokenUsage = getTokenUsage(row.token_usage);
  const title = buildTitle(row);
  const sourceLabel = formatSessionSourceLabel(sourceType);
  const model = asString(row.latest_model_alias) || asString(row.model_alias);
  const provider =
    asString(row.latest_provider_name) || asString(row.provider_name);

  return {
    id,
    sourceId,
    sourceType,
    title,
    subtitle:
      [sourceLabel, model, provider].filter(Boolean).join(' · ') || null,
    sessionReference: asString(row.session_reference),
    runtimePrincipalName: asString(row.runtime_principal_name),
    flowName: asString(row.flow_name),
    flowExecutionId: asString(row.flow_execution_id),
    status: getStatus(row),
    startedAt: asString(row.started_at),
    lastActivityAt:
      asString(row.last_activity_at) || asString(row.last_request_at),
    endedAt: asString(row.ended_at),
    totalRequests: requestCount,
    successfulRequests: asNumber(row.successful_requests),
    failedRequests: asNumber(row.failed_requests),
    tokenUsage,
    estimatedCost: asNumber(row.estimated_cost),
    latestModelAlias: model,
    latestProviderName: provider,
    canLoadEvents: Boolean(runtimeSessionId),
    optimizationWasteScore:
      typeof row.optimization_waste_score === 'number'
        ? row.optimization_waste_score
        : null,
    optimizationPotentialSavingsTokens:
      typeof row.optimization_potential_savings_tokens === 'number'
        ? row.optimization_potential_savings_tokens
        : null,
    optimizationPotentialSavingsUsd:
      typeof row.optimization_potential_savings_usd === 'number'
        ? row.optimization_potential_savings_usd
        : null,
    raw: session,
  };
}

export function normalizeObservedSessions(
  sessions: Array<
    RuntimeSessionSummary | GatewayUsageBySession | Record<string, unknown>
  >
): ObservedSession[] {
  const byId = new Map<string, ObservedSession>();
  for (const row of sessions || []) {
    const session = normalizeObservedSession(row);
    const existing = byId.get(session.id);
    if (!existing) {
      byId.set(session.id, session);
      continue;
    }
    existing.totalRequests += session.totalRequests;
    existing.successfulRequests += session.successfulRequests;
    existing.failedRequests += session.failedRequests;
    existing.estimatedCost += session.estimatedCost;
    existing.tokenUsage = {
      prompt_tokens:
        existing.tokenUsage.prompt_tokens + session.tokenUsage.prompt_tokens,
      completion_tokens:
        existing.tokenUsage.completion_tokens +
        session.tokenUsage.completion_tokens,
      total_tokens:
        existing.tokenUsage.total_tokens + session.tokenUsage.total_tokens,
    };
    if (
      session.lastActivityAt &&
      (!existing.lastActivityAt ||
        new Date(session.lastActivityAt).getTime() >
          new Date(existing.lastActivityAt).getTime())
    ) {
      existing.lastActivityAt = session.lastActivityAt;
    }
  }
  return Array.from(byId.values()).sort((left, right) => {
    const leftTime = new Date(
      left.lastActivityAt || left.startedAt || 0
    ).getTime();
    const rightTime = new Date(
      right.lastActivityAt || right.startedAt || 0
    ).getTime();
    return rightTime - leftTime;
  });
}

export function getGatewayEventPreviewMessages(
  event: FlowGatewayEvent
): FlowGatewayConversationPreviewMessage[] {
  return Array.isArray(event.payload?.conversation_preview?.messages)
    ? event.payload.conversation_preview.messages
    : [];
}

export function getGatewayEventUserRequest(
  event: FlowGatewayEvent
): string | null {
  const messages = getGatewayEventPreviewMessages(event);
  const directUserMessage = [...messages]
    .reverse()
    .find((message) => message.role === 'user' && message.text);
  return directUserMessage?.text?.trim() || null;
}

export function formatCost(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value) || value === 0) {
    return '$0.00';
  }
  return value >= 0.01 ? `$${value.toFixed(2)}` : `$${value.toFixed(4)}`;
}

export function formatNumber(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString() : '0';
}

export function summarizeSessionLocally(
  session: ObservedSession,
  events: FlowGatewayEvent[],
  activity: RuntimeSessionActivityItem[] = []
): SessionSummaryInsight {
  const errors = events.filter(
    (event) =>
      event.payload?.outcome === 'error' ||
      event.payload?.status_code === 429 ||
      Number(event.payload?.status_code || 0) >= 400
  );
  const toolCalls = activity.filter(
    (item) => item.activity_type === 'tool_call'
  );
  const firstRequest = events.map(getGatewayEventUserRequest).find(Boolean);
  const model =
    session.latestModelAlias ||
    events.find((event) => event.payload?.model_alias)?.payload.model_alias ||
    'the configured model';

  const highlights = [
    `${formatNumber(session.totalRequests || events.length)} model request${
      (session.totalRequests || events.length) === 1 ? '' : 's'
    }`,
    `${formatNumber(session.tokenUsage.total_tokens)} tokens`,
    `${formatCost(session.estimatedCost)} estimated spend`,
  ];
  if (toolCalls.length) {
    highlights.push(`${formatNumber(toolCalls.length)} tool call events`);
  }
  if (firstRequest) {
    highlights.push(`User request: ${firstRequest.slice(0, 140)}`);
  }

  return {
    title: `${session.title} used ${model}`,
    description: errors.length
      ? `This session has ${errors.length} failed or denied gateway event${
          errors.length === 1 ? '' : 's'
        }. Start with the failed request details before optimizing spend.`
      : `This session completed without captured gateway errors. Expand requests to inspect prompts, context, tool usage, and raw payloads.`,
    riskLevel: errors.length
      ? 'high'
      : session.estimatedCost > 1
        ? 'medium'
        : 'low',
    highlights,
    nextAction: errors.length
      ? 'Review failed requests and related audit events.'
      : session.tokenUsage.total_tokens > 100_000
        ? 'Inspect large prompt segments for context that can be removed.'
        : null,
    generatedBy: 'local',
    estimatedSummaryCost: 0,
  };
}

/**
 * Minimal offline fallback used only when no backend-generated
 * suggestions are available. The backend response (grounded in the
 * session context profile) is the source of truth; this fallback makes
 * no savings claims of its own.
 */
export function suggestSessionOptimizations(
  session: ObservedSession
): SessionOptimizationSuggestion[] {
  const promptTokens = session.tokenUsage.prompt_tokens;
  const totalTokens = session.tokenUsage.total_tokens;

  if (promptTokens > 0 && promptTokens / Math.max(totalTokens, 1) > 0.75) {
    return [
      {
        id: 'trim-context',
        title: 'Trim prompt context',
        description:
          'Most tokens are prompt tokens. Generate suggestions for a measured breakdown of where they went.',
        expectedSavingsTokens: 0,
        expectedSavingsUsd: 0,
        confidence: 'medium',
        actionLabel: 'Review context segments',
        evidence: [
          `${formatNumber(promptTokens)} prompt tokens`,
          `${Math.round((promptTokens / Math.max(totalTokens, 1)) * 100)}% of session tokens were prompt-side`,
        ],
      },
    ];
  }

  return [
    {
      id: 'budget-guardrail',
      title: 'Add a scoped budget guardrail',
      description:
        'No obvious waste pattern was detected. A scoped budget still protects future sessions from unexpected spend spikes.',
      expectedSavingsTokens: 0,
      expectedSavingsUsd: 0,
      confidence: 'medium',
      actionLabel: 'Review budget policy',
      evidence: [`Current spend: ${formatCost(session.estimatedCost)}`],
    },
  ];
}
