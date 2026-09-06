import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import { fetchWithAuth } from '../api';
import { humaniseAction } from '../utils/outcome-label';
import { showToast } from './confirm-dialog';
import consoleStyles from '../styles/console-styles.css?inline';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../services/unified-websocket-manager';
import { parseUTCDate, formatRelativeTime } from '../utils/date';
import { executionDurationText } from '../utils/execution';
import {
  failureCategoryLabel,
  renderFailureCategoryChip,
} from '../utils/failure-category';
import {
  executionSubjectCss,
  renderExecutionSubject,
  type ExecutionSubjectSource,
} from '../utils/execution-subject';

export type FeedTone = 'success' | 'warning' | 'danger' | 'neutral';

/** What kind of thing happened. Decides which fields the body shows. */
export type FeedKind =
  | 'execution'
  | 'approval'
  | 'gateway'
  | 'tool'
  | 'budget'
  | 'session'
  | 'agent'
  | 'other';

/** One labelled fact in an expanded row. */
export interface FeedField {
  label: string;
  value: string;
  /** A console page this fact points at. */
  href?: string;
  /** Something to select rather than to read: shown monospaced, copyable. */
  copy?: boolean;
  /** Long enough to want the full width of the body (an error line). */
  wide?: boolean;
}

/**
 * One line in the feed, and everything its expanded body needs.
 *
 * `text` is the sentence, `subject` is the thing the sentence is about when
 * that thing lives outside the console (a pull request, an issue), and
 * `trail` is the one number worth carrying (a duration, a status code).
 *
 * The row does not navigate: it opens onto `fields`, which is the same
 * payload the audit page holds, read for this kind of event. The links out
 * live in the body, `href` first (labelled by `openLabel`), then the audit
 * page deep-linked to this event.
 */
export interface FeedEvent {
  id: string;
  at: string;
  kind: FeedKind;
  tone: FeedTone;
  text: string;
  subject?: ExecutionSubjectSource | null;
  trail?: string;
  href?: string;
  /** "Open run", "Open approval": what `href` opens. */
  openLabel?: string;
  budget?: boolean;
  /** The audit event id, for `/console/audit?event=` and for copying. */
  eventId?: string;
  /** The raw event type, humanised in the body. */
  eventType?: string;
  /** Who did it: a person, an agent, a flow run. */
  actor?: string;
  /** What it was done to: the flow, the agent, the model, the tool. */
  entity?: string;
  fields?: FeedField[];
  /**
   * Which layer broke a failed run (#361). Only failed run rows carry it, and
   * only from servers that derive it; the row reads the same without it.
   */
  failureCategory?: string | null;
  /**
   * Rows that say exactly this, one after another, fold into one row with a
   * count. Only tool calls set it: a busy minute is twelve identical
   * `ran update_pull_request` lines, which is one fact, not twelve.
   */
  foldKey?: string;
}

/** A folded row: the newest of a run of identical events, plus the rest. */
export interface FeedRow {
  event: FeedEvent;
  repeats: FeedEvent[];
}

/** What the page already knows, so the feed can name things properly. */
export interface FeedContext {
  flows?: { id: string; name?: string | null }[];
  agents?: { id: string; display_name?: string | null }[];
  executions?: (ExecutionSubjectSource & {
    id: string;
    flow_id?: string | null;
    flow_name?: string | null;
    status?: string;
    start_time?: string;
    end_time?: string | null;
    failure_category?: string | null;
  })[];
  users?: { id: string; username?: string | null; full_name?: string | null }[];
  budgetPolicies?: {
    period?: string | null;
    soft_limit_usd?: number | null;
    hard_limit_usd?: number | null;
  }[];
}

/** Realtime topics the Overview already listens to. */
export const FEED_TOPICS = [
  'approvals',
  'audit',
  'budget_health',
  'flow_executions',
  'gateway_activity',
  'managed_agents',
  'runtime_sessions',
];

/** How many rows the feed keeps in memory, and how many it fetches to start. */
export const FEED_CAP = 30;
export const FEED_INITIAL_ROWS = 12;
/**
 * The audit timeline is mostly traffic, so the fill pages through it.
 *
 * On an account doing 14k gateway calls a day, every one of the newest 40
 * audit groups can be a successful `model_gateway_request`, which is not
 * news and never becomes a row. That is what emptied the feed on staging at
 * 03:00 while `/console/audit` was listing events from a minute before. So
 * the fill asks for a page, keeps what is news, and asks for the next page
 * until it has enough rows or it runs out of patience.
 */
export const AUDIT_PAGE_SIZE = 50;
/**
 * Three pages, and only the first one is on its own: page 0 decides whether
 * more are needed, and pages 1 and 2 are then asked for together rather than
 * one after the other. A fourth page was two more round trips for rows that
 * were already off the bottom of a 12-row rail.
 */
export const AUDIT_MAX_PAGES = 3;
const AUDIT_WINDOW_HOURS = 24;
/** How long the feed waits for a host that says it is fetching the users. */
const HOST_USERS_WAIT_MS = 1500;

/**
 * Consecutive identical lines become one row with a count.
 *
 * An agent working a pull request calls the same tool six times in a minute;
 * six rows of it push everything else off the rail for one fact. Only
 * adjacent events fold, so the timeline stays a timeline: anything that
 * happened in between breaks the run.
 */
export function foldRows(events: FeedEvent[]): FeedRow[] {
  const rows: FeedRow[] = [];
  for (const event of events) {
    const last = rows[rows.length - 1];
    if (last && event.foldKey && last.event.foldKey === event.foldKey) {
      last.repeats.push(event);
      continue;
    }
    rows.push({ event, repeats: [] });
  }
  return rows;
}

interface AuditLogLike {
  id: string;
  user_id?: string | null;
  action: string;
  resource_type?: string | null;
  resource_id?: string | null;
  status?: string;
  details?: Record<string, any> | null;
  timestamp: string;
}

export interface AuditGroupLike {
  correlation_id?: string | null;
  primary_event: AuditLogLike;
  outcome?: string;
}

// Enum wording lives in utils/outcome-label so components that are not the
// feed can use it; re-exported here because the feed is where callers found
// it first.
export { humaniseAction, outcomeLabel } from '../utils/outcome-label';

const PAST_TENSE = new Set([
  'created',
  'updated',
  'deleted',
  'removed',
  'revoked',
  'rotated',
  'enabled',
  'disabled',
  'assigned',
  'invited',
]);

const CONFIG_LABELS: Record<string, string> = {
  mcp_server: 'MCP Server',
  tool_configuration: 'Tool',
  tool_rule: 'Tool Rule',
  approval_workflow: 'Approval Workflow',
  tracker: 'Tracker',
  flow: 'Flow',
  api_key: 'API key',
  budget_policy: 'Budget',
};

function toneFromOutcome(outcome: string | undefined): FeedTone {
  switch ((outcome || '').toLowerCase()) {
    case 'failed':
    case 'error':
    case 'declined':
      return 'danger';
    case 'deny':
    case 'budget_denied':
    case 'require_approval':
    case 'expired':
      return 'warning';
    case 'approved':
      return 'success';
    default:
      return 'neutral';
  }
}

function money(value: unknown): string {
  const amount = Number(value || 0);
  if (!amount) return '';
  return `$${amount.toFixed(2)}`;
}

/** One field, or nothing at all: a body of "Server: -" says less than no row. */
function field(
  label: string,
  value: unknown,
  extra: Partial<FeedField> = {}
): FeedField | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text) return null;
  return { label, value: text, ...extra };
}

function fieldList(...items: (FeedField | null | undefined)[]): FeedField[] {
  return items.filter((item): item is FeedField => Boolean(item));
}

/** The first number that exists, so callers can list the names a payload uses. */
function firstNumber(
  details: Record<string, any>,
  keys: string[]
): number | null {
  for (const key of keys) {
    const value = Number(details[key]);
    if (Number.isFinite(value) && value !== 0) return value;
  }
  return null;
}

function firstString(
  details: Record<string, any>,
  keys: string[]
): string | null {
  for (const key of keys) {
    const value = details[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

/** 12.4k rather than 12403: the body is a glance, not a bill. */
function tokenText(value: number | null): string {
  if (!value) return '';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(Math.round(value));
}

function millisText(value: number | null): string {
  if (!value) return '';
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value / 60_000)}m ${Math.round((value % 60_000) / 1000)}s`;
}

/**
 * The first line of an error, trimmed to something a two-column body can hold.
 *
 * A stack trace in the feed is a wall; the first line is the one that names
 * what broke, and the audit page holds the rest.
 */
function firstErrorLine(value: unknown): string {
  if (typeof value !== 'string') return '';
  const line = value.split('\n').find((part) => part.trim()) || '';
  const trimmed = line.trim();
  return trimmed.length > 160 ? `${trimmed.slice(0, 157)}...` : trimmed;
}

/** `3894e134` reads; the full UUID is only ever worth copying. */
export function shortId(value: string | null | undefined): string {
  if (!value) return '';
  return value.length > 8 ? value.slice(0, 8) : value;
}

class Lookups {
  constructor(private readonly ctx: FeedContext) {}

  flowName(flowId: string | null | undefined): string | null {
    if (!flowId) return null;
    const flow = (this.ctx.flows || []).find((item) => item.id === flowId);
    return flow?.name || null;
  }

  agentName(agentId: string | null | undefined): string | null {
    if (!agentId) return null;
    const agent = (this.ctx.agents || []).find((item) => item.id === agentId);
    return agent?.display_name || null;
  }

  execution(executionId: string | null | undefined) {
    if (!executionId) return null;
    return (
      (this.ctx.executions || []).find((item) => item.id === executionId) ||
      null
    );
  }

  userName(userId: string | null | undefined): string | null {
    if (!userId) return null;
    const user = (this.ctx.users || []).find((item) => item.id === userId);
    return user?.username || user?.full_name || null;
  }

  /**
   * The word for the limit that fired. The event carries the amount, not the
   * period, so the period comes from the policy with that amount, and is
   * left out entirely when no policy matches.
   */
  budgetPeriod(limit: number | null | undefined): string | null {
    if (!limit) return null;
    const policy = (this.ctx.budgetPolicies || []).find(
      (item) =>
        Number(item.soft_limit_usd || 0) === Number(limit) ||
        Number(item.hard_limit_usd || 0) === Number(limit)
    );
    const period = (policy?.period || '').toLowerCase();
    if (period === 'daily') return 'Daily';
    if (period === 'weekly') return 'Weekly';
    if (period === 'monthly') return 'Monthly';
    if (period === 'hourly') return 'Hourly';
    return null;
  }
}

/**
 * One audit row as a feed line, or null when the row is not news.
 *
 * Only successful gateway requests and session heartbeats are dropped: on an
 * account doing 13k requests a day they would be the whole feed. Everything
 * else gets a line, including event types this console has never seen.
 */
export function feedEventFromAuditGroup(
  group: AuditGroupLike,
  context: FeedContext = {}
): FeedEvent | null {
  const event = group.primary_event;
  if (!event) return null;
  const lookups = new Lookups(context);
  const details = event.details || {};
  const outcome = group.outcome || event.status || '';
  const base = {
    id: event.id,
    at: event.timestamp,
    eventId: event.id,
    eventType: event.action,
  };
  const actor =
    lookups.userName(event.user_id) ||
    (typeof details.username === 'string' ? details.username : null);

  switch (event.action) {
    case 'model_gateway_request': {
      const model = String(
        details.requested_model ||
          details.model_alias ||
          event.resource_id ||
          ''
      );
      const status = String(event.status || outcome || '').toLowerCase();
      if (status === 'success' || status === 'executed') {
        return null;
      }
      const gatewayFields = fieldList(
        field('Model', model),
        field('Provider', firstString(details, ['provider_name', 'provider'])),
        field('Status code', details.status_code),
        field(
          'Tokens',
          tokenText(
            firstNumber(details, ['total_tokens', 'tokens']) ||
              (firstNumber(details, ['input_tokens']) || 0) +
                (firstNumber(details, ['output_tokens']) || 0)
          )
        ),
        field(
          'Latency',
          millisText(
            firstNumber(details, ['latency_ms', 'duration_ms', 'elapsed_ms'])
          )
        ),
        field('Cost', money(details.cost_usd || details.total_cost)),
        field('Error', firstErrorLine(details.error || details.message), {
          wide: true,
        })
      );
      if (status === 'budget_denied') {
        return {
          ...base,
          kind: 'gateway',
          tone: 'warning',
          text: `Gateway request denied by budget · ${model || 'model'}`,
          entity: model || undefined,
          actor: actor || undefined,
          fields: gatewayFields,
          href: '/console/api-usage',
          openLabel: 'Open API usage',
        };
      }
      const code = details.status_code ? ` · ${details.status_code}` : '';
      return {
        ...base,
        kind: 'gateway',
        tone: 'danger',
        text: `Gateway request failed · ${model || 'model'}${code}`,
        entity: model || undefined,
        actor: actor || undefined,
        fields: gatewayFields,
        href: '/console/api-usage',
        openLabel: 'Open API usage',
      };
    }
    case 'tool_call': {
      const tool = String(event.resource_id || details.tool_name || 'a tool');
      // A busy hour is a wall of "update_pull_request ran", so every tool
      // line leads with who called it: the agent when the audit knows it,
      // the flow run or session principal otherwise. The row then opens on
      // the session rather than on a filtered audit page, which is as
      // specific as this event gets.
      const caller =
        firstString(details, [
          'managed_agent_name',
          'runtime_principal_name',
          'agent_name',
        ]) || actor;
      const sessionId =
        typeof details.runtime_session_id === 'string'
          ? details.runtime_session_id
          : null;
      const href = sessionId
        ? `/console/runtime-sessions?sessionId=${sessionId}`
        : undefined;
      const normalized = (outcome || '').toLowerCase();
      const toolFields = fieldList(
        field('Tool', tool),
        field(
          'Server',
          firstString(details, [
            'mcp_server_name',
            'server_name',
            'mcp_server',
            'tool_server',
          ])
        ),
        field('Outcome', humaniseAction(normalized || 'executed')),
        field('Caller', caller),
        field(
          'Duration',
          millisText(firstNumber(details, ['duration_ms', 'latency_ms']))
        ),
        sessionId
          ? { label: 'Session', value: shortId(sessionId), href, copy: true }
          : null,
        field('Reason', firstErrorLine(details.reason || details.error), {
          wide: true,
        })
      );
      const who = caller || 'An agent';
      const shape = {
        ...base,
        kind: 'tool' as const,
        actor: caller || undefined,
        entity: tool,
        fields: toolFields,
        href,
        openLabel: href ? 'Open session' : undefined,
      };
      if (normalized === 'require_approval') {
        return {
          ...shape,
          tone: 'warning',
          text: `${who} needs approval for ${tool}`,
          href: '/console/approvals',
          openLabel: 'Open approvals',
          foldKey: `tool:approval:${caller || ''}:${tool}`,
        };
      }
      if (normalized === 'deny') {
        return {
          ...shape,
          tone: 'warning',
          text: `${who} was blocked from ${tool}`,
          foldKey: `tool:deny:${caller || ''}:${tool}`,
        };
      }
      if (normalized === 'failed') {
        return {
          ...shape,
          tone: 'danger',
          text: `${who} failed to run ${tool}`,
          foldKey: `tool:failed:${caller || ''}:${tool}`,
        };
      }
      return {
        ...shape,
        tone: 'neutral',
        text: `${who} ran ${tool}`,
        foldKey: `tool:ran:${caller || ''}:${tool}`,
      };
    }
    case 'authentication':
      return {
        ...base,
        kind: 'other',
        tone: 'neutral',
        text: `${details.username || actor || 'Someone'} signed in`,
        actor: String(details.username || actor || '') || undefined,
        fields: fieldList(
          field('User', details.username || actor),
          field('Method', firstString(details, ['auth_method', 'method'])),
          field('Address', firstString(details, ['ip_address', 'client_ip'])),
          field('Outcome', humaniseAction(String(outcome || 'success')))
        ),
        href: '/console/audit?event_type=authentication',
        openLabel: 'Open sign-ins',
      };
    case 'configuration_change': {
      const kind = String(details.config_type || event.resource_id || '');
      const label = CONFIG_LABELS[kind] || humaniseAction(kind) || 'Setting';
      const action = String(details.action || 'changed');
      const name =
        (details.new_value && typeof details.new_value === 'object'
          ? details.new_value.name
          : null) ||
        (details.old_value && typeof details.old_value === 'object'
          ? details.old_value.name
          : null);
      const by = actor ? ` by ${actor}` : '';
      const about = name ? ` · ${name}` : '';
      return {
        ...base,
        kind: 'other',
        tone: 'neutral',
        text: `${label} ${action}${by}${about}`,
        actor: actor || undefined,
        entity: name ? String(name) : label,
        fields: fieldList(
          field('Type', label),
          field('Change', humaniseAction(action)),
          field('Name', name),
          field('Changed by', actor)
        ),
        href: '/console/audit?event_type=configuration_change',
        openLabel: 'Open changes',
      };
    }
    case 'runtime_session_created':
    case 'runtime_session_ended': {
      const who = firstString(details, [
        'runtime_principal_name',
        'managed_agent_name',
      ]);
      const started = event.action === 'runtime_session_created';
      const sessionHref = event.resource_id
        ? `/console/runtime-sessions?sessionId=${event.resource_id}`
        : '/console/runtime-sessions';
      return {
        ...base,
        kind: 'session',
        tone: 'neutral',
        text: `${who || 'An agent'} ${started ? 'started' : 'ended'} a session`,
        actor: who || undefined,
        entity: who || undefined,
        fields: fieldList(
          field('Agent', who),
          field('Model', firstString(details, ['model_alias', 'model'])),
          field(
            'Requests',
            firstNumber(details, [
              'request_count',
              'requests',
              'total_requests',
            ])
          ),
          field(
            'Duration',
            millisText(firstNumber(details, ['duration_ms', 'elapsed_ms']))
          ),
          event.resource_id
            ? {
                label: 'Session',
                value: shortId(event.resource_id),
                href: sessionHref,
                copy: true,
              }
            : null
        ),
        href: sessionHref,
        openLabel: 'Open session',
      };
    }
    case 'runtime_session_updated':
      // A session that is still going is not an event.
      return null;
    case 'approval_created':
    case 'approval_approved':
    case 'approval_denied':
    case 'approval_expired': {
      const tool = String(details.tool_name || event.resource_id || 'a tool');
      const requestId = details.approval_request_id || event.resource_id;
      const href = requestId
        ? `/console/approval/${requestId}`
        : '/console/approvals';
      const by = actor ? ` by ${actor}` : '';
      const decision =
        event.action === 'approval_approved'
          ? 'Approved'
          : event.action === 'approval_denied'
            ? 'Declined'
            : event.action === 'approval_expired'
              ? 'Expired'
              : 'Pending';
      const shape = {
        ...base,
        kind: 'approval' as const,
        entity: tool,
        actor: actor || undefined,
        fields: fieldList(
          field('Tool', tool),
          field('Decision', decision),
          field('Decided by', actor),
          field(
            'Agent',
            firstString(details, ['managed_agent_name', 'agent_name'])
          ),
          field(
            'Comment',
            firstString(details, ['comment', 'reason', 'decision_reason']),
            { wide: true }
          )
        ),
        href,
        openLabel: 'Open approval',
      };
      if (event.action === 'approval_created') {
        return {
          ...shape,
          tone: 'warning',
          text: `Approval requested: ${tool}`,
        };
      }
      if (event.action === 'approval_approved') {
        return {
          ...shape,
          tone: 'success',
          text: `Approval approved${by}: ${tool}`,
        };
      }
      if (event.action === 'approval_denied') {
        return {
          ...shape,
          tone: 'danger',
          text: `Approval declined${by}: ${tool}`,
        };
      }
      return { ...shape, tone: 'warning', text: `Approval expired: ${tool}` };
    }
    default: {
      // An event type nobody wrote a recipe for still says who did what.
      const words = event.action.split('_');
      const verb = words[words.length - 1];
      const subject = words.slice(0, -1).join('_');
      const rest = {
        ...base,
        kind: 'other' as const,
        tone: toneFromOutcome(outcome),
        actor: actor || undefined,
        entity: event.resource_id || undefined,
        fields: fieldList(
          field('Actor', actor),
          field('Resource', event.resource_type),
          event.resource_id
            ? {
                label: 'Resource id',
                value: shortId(event.resource_id),
                copy: true,
              }
            : null,
          field('Outcome', outcome ? humaniseAction(outcome) : null)
        ),
        href: `/console/audit?event_type=${event.action}`,
        // The body already links to this very event, so the type filter has to
        // say that it is the wider list, not repeat "Open in audit".
        openLabel: 'Filter audit by this type',
      };
      if (PAST_TENSE.has(verb) && subject) {
        const suffix = actor ? ` by ${actor}` : '';
        return { ...rest, text: `${humaniseAction(subject)} ${verb}${suffix}` };
      }
      const trail = actor ? ` · ${actor}` : '';
      return { ...rest, text: `${humaniseAction(event.action)}${trail}` };
    }
  }
}

/**
 * One realtime message as a feed line, or null when the message is not news.
 *
 * Successful gateway calls, agent heartbeats and session updates are dropped
 * for the same reason as above: they are traffic, not events.
 */
export function feedEventFromRealtime(
  topic: string,
  message: any,
  context: FeedContext = {}
): FeedEvent | null {
  if (!message || typeof message !== 'object') return null;
  const lookups = new Lookups(context);
  const type = String(message.type || '');
  const payload = (message.payload || {}) as Record<string, any>;
  const at = String(message.timestamp || new Date().toISOString());

  if (topic === 'flow_executions') {
    if (type !== 'status_update') return null;
    const executionId = String(
      message.execution_id || payload.execution_id || ''
    );
    const status = String(payload.status || '').toUpperCase();
    if (!executionId || !status) return null;
    const known = lookups.execution(executionId);
    const name =
      payload.flow_name ||
      known?.flow_name ||
      lookups.flowName(message.flow_id || payload.flow_id) ||
      'A flow';
    const href = `/console/flows/executions/${executionId}`;
    const duration =
      executionDurationText({
        status,
        start_time: String(payload.start_time || known?.start_time || ''),
        end_time: payload.end_time || known?.end_time || at,
      }) || undefined;
    const model = firstString(payload, [
      'model_alias',
      'model',
      'requested_model',
    ]);
    const base = {
      id: `execution:${executionId}:${status}`,
      at,
      kind: 'execution' as const,
      subject: known || null,
      eventType: 'flow_execution',
      actor: String(name),
      entity: String(name),
      fields: fieldList(
        field('Flow', name),
        field('Status', humaniseAction(status.toLowerCase())),
        field('Duration', duration),
        field('Model', model),
        field('Provider', firstString(payload, ['provider_name'])),
        field(
          'Tokens',
          tokenText(firstNumber(payload, ['total_tokens', 'tokens']))
        ),
        field(
          '$ est.',
          money(
            payload.total_cost_usd || payload.cost_usd || payload.total_cost
          )
        ),
        field(
          'Tool calls',
          firstNumber(payload, ['tool_call_count', 'tool_calls'])
        ),
        { label: 'Execution', value: shortId(executionId), href, copy: true },
        field('Error', firstErrorLine(payload.error_message || payload.error), {
          wide: true,
        })
      ),
      href,
      openLabel: 'Open run',
    };
    if (status === 'SUCCEEDED' || status === 'COMPLETED') {
      return {
        ...base,
        tone: 'success',
        text: `${name} succeeded`,
        trail: duration,
      };
    }
    if (status === 'FAILED' || status === 'TIMEOUT' || status === 'ERROR') {
      // The realtime payload carries the category when the server derives it;
      // the executions the page already holds are the fallback for a socket
      // message that predates the field.
      const failureCategory =
        firstString(payload, ['failure_category']) ||
        known?.failure_category ||
        null;
      // The open row says it next to the status it qualifies, above the error
      // text it summarises.
      const fields = [...(base.fields || [])];
      const failureField = field(
        'Failure',
        failureCategoryLabel(failureCategory)
      );
      if (failureField) {
        const afterStatus = fields.findIndex((item) => item.label === 'Status');
        fields.splice(
          afterStatus === -1 ? fields.length : afterStatus + 1,
          0,
          failureField
        );
      }
      return {
        ...base,
        tone: 'danger',
        text: `${name} failed`,
        failureCategory,
        fields,
      };
    }
    if (status === 'RUNNING') {
      return { ...base, tone: 'neutral', text: `${name} started` };
    }
    // PENDING, STARTING and the rest are the same run getting ready.
    return null;
  }

  if (topic === 'approvals') {
    const requestId = String(message.approval_request_id || '');
    const tool = message.tool_name || 'a tool';
    const agent = message.managed_agent_name
      ? ` (${message.managed_agent_name})`
      : '';
    const href = requestId
      ? `/console/approval/${requestId}`
      : '/console/approvals';
    const by = lookups.userName(message.approver_id) || message.approver_name;
    const base = {
      id: `approval:${requestId}:${type}`,
      at,
      kind: 'approval' as const,
      eventId: requestId || undefined,
      eventType: type,
      entity: String(tool),
      actor: by || undefined,
      fields: fieldList(
        field('Tool', tool),
        field(
          'Decision',
          type === 'approval_approved'
            ? 'Approved'
            : type === 'approval_declined' || type === 'approval_denied'
              ? 'Declined'
              : type === 'approval_expired'
                ? 'Expired'
                : 'Pending'
        ),
        field('Decided by', by),
        field('Agent', message.managed_agent_name),
        field('Comment', message.comment || message.reason, { wide: true }),
        requestId
          ? { label: 'Request', value: shortId(requestId), href, copy: true }
          : null
      ),
      href,
      openLabel: 'Open approval',
    };
    if (type === 'approval_created') {
      return {
        ...base,
        tone: 'warning',
        text: `Approval requested: ${tool}${agent}`,
      };
    }
    if (type === 'approval_approved') {
      return {
        ...base,
        tone: 'success',
        text: `Approval approved${by ? ` by ${by}` : ''}: ${tool}`,
      };
    }
    if (type === 'approval_declined' || type === 'approval_denied') {
      return {
        ...base,
        tone: 'danger',
        text: `Approval declined${by ? ` by ${by}` : ''}: ${tool}`,
      };
    }
    if (type === 'approval_expired') {
      return { ...base, tone: 'warning', text: `Approval expired: ${tool}` };
    }
    return null;
  }

  if (topic === 'budget_health') {
    const budget = (payload.budget || {}) as Record<string, any>;
    const hard = Boolean(budget.hard_limit_exceeded);
    const soft = Boolean(budget.soft_limit_exceeded);
    if (!hard && !soft) return null;
    const limit = hard
      ? budget.account_limit_usd || budget.flow_limit_usd
      : budget.account_soft_limit_usd || budget.flow_soft_limit_usd;
    const period = lookups.budgetPeriod(limit);
    const amount = money(limit);
    const words = `${period ? `${period} budget` : 'Budget'} ${
      hard ? 'hard' : 'soft'
    } limit reached`;
    return {
      // One line per limit: the same limit stays hit for the rest of the
      // period, and a feed that repeats it says nothing new.
      id: `budget:${hard ? 'hard' : 'soft'}:${limit || 'limit'}`,
      at,
      kind: 'budget',
      eventType: 'budget_limit_reached',
      entity: period ? `${period} budget` : 'Budget',
      tone: hard ? 'danger' : 'warning',
      text: amount ? `${words} · ${amount}` : words,
      fields: fieldList(
        field(
          'Policy',
          budget.policy_name || (period ? `${period} budget` : null)
        ),
        field('Period', period),
        field('Limit', amount),
        field(
          'Spend',
          money(
            budget.current_spend_usd ||
              budget.account_spend_usd ||
              budget.flow_spend_usd
          )
        ),
        field('Scope', budget.flow_limit_usd ? 'Flow' : 'Account')
      ),
      budget: true,
      openLabel: 'Change limits',
    };
  }

  if (topic === 'gateway_activity') {
    if (type !== 'model_gateway_call') return null;
    const status = Number(payload.status_code || 0);
    const outcome = String(payload.outcome || '').toLowerCase();
    if (status && status < 400 && outcome !== 'denied') return null;
    const model =
      payload.requested_model || payload.model_alias || payload.provider_name;
    return {
      id: `gateway:${payload.api_usage_id || `${at}:${model}`}`,
      at,
      kind: 'gateway',
      eventId: payload.api_usage_id ? String(payload.api_usage_id) : undefined,
      eventType: 'model_gateway_request',
      entity: model ? String(model) : undefined,
      tone: 'danger',
      text: `Gateway request failed · ${model || 'model'}${
        status ? ` · ${status}` : ''
      }`,
      fields: fieldList(
        field('Model', model),
        field('Provider', payload.provider_name),
        field('Status code', status || null),
        field(
          'Tokens',
          tokenText(firstNumber(payload, ['total_tokens', 'tokens']))
        ),
        field(
          'Latency',
          millisText(firstNumber(payload, ['latency_ms', 'duration_ms']))
        ),
        field('Cost', money(payload.cost_usd || payload.total_cost)),
        field('Error', firstErrorLine(payload.error || payload.message), {
          wide: true,
        })
      ),
      href: '/console/api-usage',
      openLabel: 'Open API usage',
    };
  }

  if (topic === 'managed_agents') {
    if (type !== 'managed_agent_created') return null;
    const agentId = String(payload.agent_id || '');
    const agentHref = agentId
      ? `/console/agents/${agentId}`
      : '/console/agents';
    return {
      id: `agent:${agentId}:created`,
      at,
      kind: 'agent',
      eventType: 'managed_agent_created',
      entity: payload.display_name ? String(payload.display_name) : undefined,
      tone: 'neutral',
      text: `${payload.display_name || 'An agent'} connected`,
      fields: fieldList(
        field('Agent', payload.display_name),
        field('Kind', payload.agent_type || payload.kind),
        field('Version', payload.version),
        agentId
          ? {
              label: 'Agent id',
              value: shortId(agentId),
              href: agentHref,
              copy: true,
            }
          : null
      ),
      href: agentHref,
      openLabel: 'Open agent',
    };
  }

  if (topic === 'runtime_sessions') {
    const sessionId = String(
      payload.runtime_session_id || message.runtime_session_id || ''
    );
    const who =
      payload.runtime_principal_name ||
      lookups.agentName(payload.managed_agent_id) ||
      'An agent';
    const href = sessionId
      ? `/console/runtime-sessions?sessionId=${sessionId}`
      : '/console/runtime-sessions';
    const sessionFields = fieldList(
      field('Agent', who),
      field('Model', firstString(payload, ['model_alias', 'model'])),
      field(
        'Requests',
        firstNumber(payload, ['request_count', 'requests', 'total_requests'])
      ),
      sessionId
        ? { label: 'Session', value: shortId(sessionId), href, copy: true }
        : null
    );
    if (type === 'runtime_session_created') {
      return {
        id: `session:${sessionId}:created`,
        at,
        kind: 'session',
        eventId: sessionId || undefined,
        eventType: type,
        actor: String(who),
        entity: String(who),
        tone: 'neutral',
        text: `${who} started a session`,
        fields: sessionFields,
        href,
        openLabel: 'Open session',
      };
    }
    if (type === 'runtime_session_ended') {
      return {
        id: `session:${sessionId}:ended`,
        at,
        kind: 'session',
        eventId: sessionId || undefined,
        eventType: type,
        actor: String(who),
        entity: String(who),
        tone: 'neutral',
        text: `${who} ended a session`,
        fields: sessionFields,
        href,
        openLabel: 'Open session',
      };
    }
    // `runtime_session_updated` is a heartbeat, not an event.
    return null;
  }

  if (topic === 'audit') {
    if (type !== 'audit_event') return null;
    const action = String(payload.action || '');
    if (!action) return null;
    return feedEventFromAuditGroup(
      {
        outcome: String(payload.outcome || ''),
        primary_event: {
          id: String(
            payload.audit_log_id || payload.api_usage_id || `${action}:${at}`
          ),
          user_id: payload.user_id || null,
          action,
          resource_id: payload.resource_id || null,
          status: payload.outcome || payload.status || '',
          details: payload,
          timestamp: at,
        },
      },
      context
    );
  }

  return null;
}

/**
 * What is happening right now, in one column.
 *
 * The Overview used to answer that question with three cards that each held
 * one kind of event. This holds all of them, in time order, at one depth: the
 * audit page owns filtering and history, the feed owns "the last hour".
 * Nothing here animates beyond the row being there on the next paint.
 */
@customElement('activity-feed')
export class ActivityFeed extends LitElement {
  @property({ type: Array }) flows: FeedContext['flows'] = [];
  @property({ type: Array }) agents: FeedContext['agents'] = [];
  @property({ type: Array }) executions: FeedContext['executions'] = [];
  @property({ type: Array }) budgetPolicies: FeedContext['budgetPolicies'] = [];
  /** Skips the audit fetch in tests and in previews. */
  @property({ type: Boolean }) autoload = true;
  /**
   * The account's people, when the host already has them.
   *
   * `null` means nobody handed them over and the feed looks them up itself,
   * which is what a standalone feed does. The Overview fetches the same list
   * for its Inventory, and two components asking the same endpoint for the
   * same list is one request too many.
   */
  @property({ type: Array }) users: FeedContext['users'] | null = null;
  /**
   * True when the host fetches the people list itself and will set `users`.
   *
   * The feed then waits briefly for that list instead of asking for the same
   * one. It still falls back to its own lookup if the host comes back with
   * nothing (an operator without `view_users`, an older server), so the
   * standalone behaviour is never lost.
   */
  @property({ type: Boolean }) usersFromHost = false;

  @state() private events: FeedEvent[] = [];
  /** The one open row, by row id. One at a time: this is a rail, not a page. */
  @state() private openId: string | null = null;
  @state() private loading = true;
  @state() private connected = false;
  /** The list the feed looked up itself, when the host provided none. */
  @state() private fetchedUsers: FeedContext['users'] = [];
  /** Rows currently below the fold of the scroller; 0 when nothing overflows. */
  @state() private belowFold = 0;

  private sizeObserver: ResizeObserver | null = null;
  private unsubscribes: (() => void)[] = [];
  private seen = new Set<string>();
  /** Resolved (never rejected) once the user lookup has had its turn. */
  private usersReady: Promise<void> = Promise.resolve();
  /** Called when the host sets a non-empty `users`, so the wait can end. */
  private hostUsersArrived: (() => void) | null = null;
  private scrollAnchor: { top: number; height: number } | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    css`
      /* The card is a column that fills whatever height it is given and
         scrolls its list inside, so a busy hour never grows the page past
         the column it lives in. The host is flex rather than block so a
         parent can hand it a height with flex: 1; when nobody does, the
         list's own max-height (below) keeps it civil. */
      :host {
        display: flex;
        flex-direction: column;
        min-height: 0;
        width: 100%;
      }

      .content-card {
        display: flex;
        flex-direction: column;
        flex: 1 1 auto;
        min-height: 0;
        width: 100%;
      }

      .content-card::part(base) {
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 0;
        width: 100%;
      }

      .content-card::part(body) {
        display: flex;
        flex-direction: column;
        flex: 1 1 auto;
        min-height: 0;
        overflow: hidden;
        padding: 0;
      }

      /* Not ".header": the console sheet gives that class a page-header
         min-height and bottom margin, which pads a card header by 40px. */
      .card-head {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: space-between;
      }

      .title {
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }

      /* "live" is a fact, not an alarm: a 6px dot in the meta register, and
         it does not pulse. The console's motion budget is spent elsewhere. */
      .live {
        align-items: center;
        color: var(--console-meta-color);
        display: flex;
        font-size: var(--console-text-meta);
        gap: 6px;
      }

      .live-dot {
        background: var(--sl-color-success-600);
        border-radius: 50%;
        height: 6px;
        width: 6px;
      }

      /* The list is the one thing that scrolls. Wherever nobody hands the
         card a height it stops at 360px, the same floor the rail gives it,
         so the feed is one size off the rail and cannot run away with the
         page. 360px holds about twenty rows against an in-memory cap of
         thirty, so a full feed always overflows and the list really does
         scroll rather than the card quietly growing to the cap.

         The cap is lifted by whoever takes responsibility for the height
         (the Overview rail sets --activity-feed-list-max-height: none),
         not by a viewport width: this card is not only ever on the rail,
         and a wide window is not a promise that something above it is
         bounding the column. */
      .rows {
        display: flex;
        flex: 1 1 auto;
        flex-direction: column;
        max-height: var(--activity-feed-list-max-height, 360px);
        min-height: 0;
        overflow-y: auto;
        scrollbar-width: thin;
        overscroll-behavior: contain;
      }

      .rows::-webkit-scrollbar {
        width: 8px;
      }

      .rows::-webkit-scrollbar-thumb {
        background: var(--console-hairline);
        border-radius: 4px;
      }

      .footer,
      .card-head {
        flex: 0 0 auto;
      }

      .row {
        border-bottom: 1px solid var(--console-hairline);
      }

      .row:last-child {
        border-bottom: none;
      }

      /* The head is the line; the body is what the line was hiding. The head
         is what stretches the toggle's hit area, so an open body stays
         clickable in its own right. */
      .row-head {
        align-items: baseline;
        display: flex;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        position: relative;
      }

      .row-head:hover {
        background-color: var(--console-hover-tint);
      }

      .row.open .row-head {
        background-color: var(--console-hover-tint);
      }

      /* The tone dot is the only colour in the row: the text stays ink. */
      .dot {
        border-radius: 50%;
        flex-shrink: 0;
        height: 6px;
        margin-top: 6px;
        width: 6px;
      }

      .dot.success {
        background: var(--sl-color-success-600);
      }

      .dot.warning {
        background: var(--sl-color-warning-600);
      }

      .dot.danger {
        background: var(--sl-color-danger-600);
      }

      .dot.neutral {
        background: var(--sl-color-neutral-400);
      }

      .line {
        display: flex;
        flex: 1;
        flex-wrap: wrap;
        font-size: var(--console-text-body);
        gap: 4px;
        min-width: 0;
      }

      /* The whole row is the target; the subject link inside it keeps its own
         hit area by sitting above the stretched one. */
      /* Two lines rather than one clipped one: the tool at the end of
         "Pull Request Reviewer ran update_pull_request" is the news, and a
         368px rail cannot hold that sentence on one line. */
      .row-text {
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
        line-clamp: 2;
        background: none;
        border: none;
        color: inherit;
        cursor: pointer;
        display: -webkit-box;
        font: inherit;
        overflow: hidden;
        overflow-wrap: anywhere;
        padding: 0;
        text-align: left;
        text-decoration: none;
      }

      .row-text::after {
        content: '';
        inset: 0;
        position: absolute;
      }

      .row-head:hover .row-text,
      .row-text:focus-visible {
        text-decoration: underline;
      }

      .execution-subject,
      a.execution-subject-link {
        position: relative;
        z-index: 1;
      }

      /* A count is the whole point of a folded row, so it reads as a number,
         not as more sentence. */
      .count {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }

      .chevron {
        color: var(--console-meta-color);
        flex-shrink: 0;
        font-size: 12px;
        transition: transform 0.12s ease;
      }

      .row.open .chevron {
        transform: rotate(180deg);
      }

      /* The body is one register down from the line above it: 13px meta
         labels, ink values, two columns when the card is wide enough. */
      .row-body {
        display: flex;
        flex-direction: column;
        font-size: var(--console-text-meta);
        gap: var(--sl-spacing-small);
        padding: 0 var(--sl-spacing-medium) var(--sl-spacing-small)
          calc(var(--sl-spacing-medium) + 14px);
      }

      .fields {
        display: grid;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        margin: 0;
      }

      .field.wide {
        grid-column: 1 / -1;
      }

      .field dt {
        color: var(--console-meta-color);
      }

      .field dd {
        margin: 0;
        overflow-wrap: anywhere;
      }

      .field dd.mono {
        font-family: var(--sl-font-mono);
      }

      .occurrences {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .occurrence {
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
      }

      .row-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-medium);
      }

      .row-actions a,
      .row-actions button {
        background: none;
        border: none;
        color: var(--console-link-color);
        cursor: pointer;
        font: inherit;
        padding: 0;
        text-decoration: none;
      }

      .row-actions a:hover,
      .row-actions button:hover {
        text-decoration: underline;
      }

      .trail {
        color: var(--console-meta-color);
      }

      .sep {
        color: var(--console-meta-color);
      }

      .when {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
        flex-shrink: 0;
        white-space: nowrap;
      }

      .empty {
        color: var(--console-meta-color);
        padding: var(--sl-spacing-large) var(--sl-spacing-medium);
        text-align: center;
      }

      .skeleton-row {
        border-bottom: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      sl-skeleton {
        --border-radius: var(--sl-border-radius-small);
        height: 0.75rem;
      }

      /* One hairline row: what is still below the fold on the left, the way
         out on the right. */
      .footer {
        align-items: baseline;
        border-top: 1px solid var(--console-hairline);
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: space-between;
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      }

      .footer .more {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }

      .footer a {
        color: var(--console-link-color);
        font-size: var(--console-text-meta);
        text-decoration: none;
      }

      .footer a:hover {
        text-decoration: underline;
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    this.connected =
      unifiedWebSocketManager.getState() === ConnectionState.CONNECTED;
    this.unsubscribes.push(
      unifiedWebSocketManager.onStateChange((state) => {
        this.connected = state === ConnectionState.CONNECTED;
      })
    );
    for (const topic of FEED_TOPICS) {
      this.unsubscribes.push(
        unifiedWebSocketManager.subscribe(topic, (message: unknown) =>
          this.ingest(topic, message)
        )
      );
    }
    if (this.autoload) {
      this.usersReady = this.resolveUsers();
      void this.loadInitial();
    } else {
      this.loading = false;
    }
  }

  disconnectedCallback(): void {
    this.sizeObserver?.disconnect();
    this.sizeObserver = null;
    for (const unsubscribe of this.unsubscribes) {
      try {
        unsubscribe();
      } catch {
        // A subscription that is already gone is not a problem.
      }
    }
    this.unsubscribes = [];
    super.disconnectedCallback();
  }

  private get context(): FeedContext {
    return {
      flows: this.flows,
      agents: this.agents,
      executions: this.executions,
      users: this.users?.length ? this.users : this.fetchedUsers,
      budgetPolicies: this.budgetPolicies,
    };
  }

  /**
   * Names for `... by dimo`. Unavailable to non-admins, and that is fine.
   *
   * Never throws and never stores anything but an array: the actor's name is
   * a nicety, and a lookup that fails, 403s or answers in a shape nobody
   * expected must not cost the operator the whole timeline.
   */
  /**
   * The people list, from the host when it has one and from the API when it
   * does not. Never rejects: a name is a nicety, not a row.
   */
  private async resolveUsers(): Promise<void> {
    if (this.users?.length) return;
    if (this.usersFromHost) {
      await Promise.race([
        new Promise<void>((resolve) => {
          this.hostUsersArrived = resolve;
        }),
        new Promise((resolve) => setTimeout(resolve, HOST_USERS_WAIT_MS)),
      ]);
      if (this.users?.length) return;
    }
    await this.loadUsers();
  }

  private async loadUsers(): Promise<void> {
    try {
      // The same URL the console's other readers use, so a lookup that
      // overlaps one of theirs is coalesced into a single request.
      const response = await fetchWithAuth('/api/v1/users?skip=0&limit=100');
      if (!response.ok) return;
      const data = await response.json();
      const users = Array.isArray(data) ? data : data?.users;
      this.fetchedUsers = Array.isArray(users)
        ? (users as FeedContext['users'])
        : [];
    } catch {
      // The feed degrades to "by" nothing rather than failing.
    }
  }

  /** One page of the audit timeline, or null when it cannot be read. */
  private async fetchAuditPage(
    skip: number,
    since: string | null
  ): Promise<AuditGroupLike[] | null> {
    const params = new URLSearchParams();
    params.set('limit', String(AUDIT_PAGE_SIZE));
    if (skip) params.set('skip', String(skip));
    if (since) params.set('start_date', since);
    const response = await fetchWithAuth(
      `/api/v1/audit-logs/grouped?${params}`
    );
    if (!response.ok) return null;
    const data = await response.json();
    return Array.isArray(data?.groups) ? data.groups : [];
  }

  /**
   * Turn audit groups into rows, one at a time.
   *
   * Reading a group is a pure function over a payload the console does not
   * control, so a single odd row is skipped rather than allowed to throw
   * through the whole fill.
   */
  private rowsFrom(groups: AuditGroupLike[], into: FeedEvent[]): void {
    for (const group of groups) {
      // Rows, not events: twelve tool calls by one agent fold into one row,
      // and a rail with one row in it is not a filled feed.
      if (foldRows(into).length >= FEED_INITIAL_ROWS) return;
      let event: FeedEvent | null = null;
      try {
        event = feedEventFromAuditGroup(group, this.context);
      } catch {
        continue;
      }
      if (event && !this.seen.has(event.id)) {
        this.seen.add(event.id);
        into.push(event);
      }
    }
  }

  private async loadInitial(): Promise<void> {
    this.loading = true;
    try {
      const since = new Date(
        Date.now() - AUDIT_WINDOW_HOURS * 60 * 60 * 1000
      ).toISOString();
      // The timeline and the actor names are asked for at the same time:
      // the names used to be awaited first, so the feed's first row waited
      // on a request it does not need in order to draw a row.
      const firstPage = await this.fetchAuditPage(0, since);
      // The actor's name belongs on the first paint, not the second, but a
      // lookup that never answers must not hold the timeline hostage either.
      await Promise.race([
        this.usersReady,
        new Promise((resolve) => setTimeout(resolve, 2000)),
      ]);
      const events: FeedEvent[] = [];
      let emptyWindow = false;
      if (firstPage !== null) {
        emptyWindow = firstPage.length === 0;
        this.rowsFrom(firstPage, events);
        // Page 0 decides whether the rest are needed. When they are, they
        // are asked for together instead of one after the other, and there
        // are two of them: a fourth page was two more round trips for rows
        // that were already off the bottom of a twelve-row rail.
        if (
          firstPage.length >= AUDIT_PAGE_SIZE &&
          foldRows(events).length < FEED_INITIAL_ROWS
        ) {
          const more = await Promise.all(
            Array.from({ length: AUDIT_MAX_PAGES - 1 }, (_, index) =>
              this.fetchAuditPage((index + 1) * AUDIT_PAGE_SIZE, since)
            )
          );
          for (const groups of more) {
            if (groups === null) break;
            if (foldRows(events).length >= FEED_INITIAL_ROWS) break;
            this.rowsFrom(groups, events);
            if (groups.length < AUDIT_PAGE_SIZE) break;
          }
        }
      }
      // A quiet account has nothing in the last day and still has a history.
      // Rather than say "Nothing yet" to an account that worked yesterday,
      // ask once for the newest events whenever they happened.
      if (emptyWindow && events.length === 0) {
        const groups = await this.fetchAuditPage(0, null);
        if (groups) this.rowsFrom(groups, events);
      }
      this.events = this.sortAndCap([...events, ...this.events]);
    } catch {
      // No audit access, no history: the live rows still arrive.
    } finally {
      this.loading = false;
    }
  }

  /**
   * Keep the read position when a row arrives above it.
   *
   * A feed that scrolls itself is only usable if reading it is not
   * interrupted: rows prepend, so everything below moves down by the height
   * of the new row. Unless the list is already at the top (where the point
   * is to see the newest row arrive), the scroll offset is corrected by
   * exactly the height that was added.
   */
  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('users') && this.users?.length && this.hostUsersArrived) {
      const arrived = this.hostUsersArrived;
      this.hostUsersArrived = null;
      arrived();
    }
    if (!changed.has('events')) return;
    const list = this.renderRoot?.querySelector<HTMLElement>('.rows');
    this.scrollAnchor = list
      ? { top: list.scrollTop, height: list.scrollHeight }
      : null;
  }

  protected firstUpdated(): void {
    // The rail decides the card's height, and the height decides how many
    // rows are out of sight: re-measure whenever either moves.
    if (typeof ResizeObserver === 'function') {
      this.sizeObserver = new ResizeObserver(() => this.measureBelowFold());
      this.sizeObserver.observe(this);
    }
    this.measureBelowFold();
  }

  /**
   * How many rows sit entirely under the fold of the scroller.
   *
   * Without it the card looked broken rather than scrollable: the list has no
   * visible scrollbar, so a row clipped mid-line was the only hint that
   * anything followed. A row that is partly visible is not counted, because
   * it is already saying "there is more".
   */
  private measureBelowFold(): void {
    const list = this.renderRoot?.querySelector<HTMLElement>('.rows');
    if (!list) {
      if (this.belowFold !== 0) this.belowFold = 0;
      return;
    }
    const fold = list.getBoundingClientRect().bottom;
    const hidden = Array.from(
      list.querySelectorAll<HTMLElement>('.row')
    ).filter((row) => row.getBoundingClientRect().top >= fold - 1).length;
    if (hidden !== this.belowFold) this.belowFold = hidden;
  }

  protected updated(changed: Map<string, unknown>): void {
    this.measureBelowFold();
    // An opened row whose body is below the fold of a short rail has told the
    // operator nothing, so the row moves to the top of the list and its body
    // takes the height that is left. The list is scrolled by hand rather than
    // with scrollIntoView, which would drag the page along with it.
    if (changed.has('openId') && this.openId) {
      const list = this.renderRoot?.querySelector<HTMLElement>('.rows');
      const row = this.renderRoot?.querySelector<HTMLElement>('.row.open');
      if (list && row) {
        list.scrollTop +=
          row.getBoundingClientRect().top - list.getBoundingClientRect().top;
      }
    }
    if (!changed.has('events') || !this.scrollAnchor) return;
    const anchor = this.scrollAnchor;
    this.scrollAnchor = null;
    if (anchor.top <= 0) return;
    const list = this.renderRoot?.querySelector<HTMLElement>('.rows');
    if (!list) return;
    const added = list.scrollHeight - anchor.height;
    if (added > 0) list.scrollTop = anchor.top + added;
  }

  /**
   * Take one realtime message. Public because the socket is not the only
   * caller worth having: tests and previews feed the same door.
   */
  ingest(topic: string, message: unknown): void {
    const event = feedEventFromRealtime(topic, message, this.context);
    if (!event || this.seen.has(event.id)) return;
    this.seen.add(event.id);
    this.events = this.sortAndCap([event, ...this.events]);
  }

  /**
   * Newest first, capped at rows: the feed is a window, not a log.
   *
   * The cap counts rows rather than events, because a run of identical tool
   * calls is one row: counting events would leave a rail holding five lines
   * and call it full.
   */
  private sortAndCap(events: FeedEvent[]): FeedEvent[] {
    const sorted = [...events].sort(
      (a, b) => this.time(b.at) - this.time(a.at)
    );
    const kept = foldRows(sorted)
      .slice(0, FEED_CAP)
      .flatMap((row) => [row.event, ...row.repeats]);
    if (kept.length < sorted.length) {
      const keptIds = new Set(kept.map((event) => event.id));
      for (const event of sorted) {
        if (!keptIds.has(event.id)) this.seen.delete(event.id);
      }
    }
    return kept;
  }

  private time(value: string): number {
    const parsed = parseUTCDate(value).getTime();
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  private absolute(value: string): string {
    const date = parseUTCDate(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
  }

  private openBudgetDialog(): void {
    this.dispatchEvent(
      new CustomEvent('open-budget-limits', { bubbles: true, composed: true })
    );
  }

  private toggle(id: string): void {
    this.openId = this.openId === id ? null : id;
  }

  private copy(value: string, message: string): void {
    void navigator.clipboard?.writeText?.(value);
    showToast(message, 'success');
  }

  /**
   * Consecutive identical lines become one row with a count.
   *
   * An agent working a pull request calls the same tool six times in a
   * minute; six rows of it push everything else off the rail for one fact.
   * Only adjacent events fold, so the timeline stays a timeline: anything
   * that happened in between breaks the run.
   */
  private get rows(): FeedRow[] {
    return foldRows(this.events);
  }

  /** Who, what and when, then whatever this kind of event knows. */
  private bodyFields(event: FeedEvent): FeedField[] {
    const head: FeedField[] = [
      { label: 'When', value: this.absolute(event.at) },
    ];
    if (event.eventType) {
      head.push({ label: 'Event', value: humaniseAction(event.eventType) });
    }
    const taken = new Set(head.map((item) => item.label.toLowerCase()));
    const rest = (event.fields || []).filter(
      (item) => !taken.has(item.label.toLowerCase())
    );
    return [...head, ...rest];
  }

  private renderField(item: FeedField) {
    const value = item.href
      ? html`<a href=${item.href}>${item.value}</a>`
      : item.value;
    return html`
      <div class="field ${item.wide ? 'wide' : ''}">
        <dt>${item.label}</dt>
        <dd class=${item.copy ? 'mono' : ''} title=${item.value}>${value}</dd>
      </div>
    `;
  }

  private renderRowBody(row: FeedRow, bodyId: string) {
    const event = row.event;
    const auditHref = event.eventId
      ? `/console/audit?event=${encodeURIComponent(event.eventId)}`
      : '/console/audit';
    return html`
      <div class="row-body" id=${bodyId}>
        <dl class="fields">
          ${this.bodyFields(event).map((item) => this.renderField(item))}
        </dl>
        ${
          row.repeats.length
            ? html`<div class="occurrences">
                ${[event, ...row.repeats].map(
                  (each) =>
                    html`<span class="occurrence"
                      >${this.absolute(each.at)}</span
                    >`
                )}
              </div>`
            : nothing
        }
        <div class="row-actions">
          ${
            event.budget
              ? html`<button
                  type="button"
                  @click=${() => this.openBudgetDialog()}
                >
                  ${event.openLabel || 'Change limits'}
                </button>`
              : event.href
                ? html`<a href=${event.href}
                    >${event.openLabel || 'Open details'}</a
                  >`
                : nothing
          }
          ${
            event.budget
              ? nothing
              : html`<a href=${auditHref}>Open in audit</a>`
          }
          ${
            event.eventId
              ? html`<button
                  type="button"
                  @click=${() =>
                    this.copy(event.eventId as string, 'Event id copied')}
                >
                  Copy id ${shortId(event.eventId)}
                </button>`
              : nothing
          }
        </div>
      </div>
    `;
  }

  private renderRow(row: FeedRow) {
    const event = row.event;
    const open = this.openId === event.id;
    const bodyId = `body-${event.id.replace(/[^A-Za-z0-9_-]/g, '-')}`;
    const count = row.repeats.length + 1;
    // The line ellipsises in a narrow column, and the tail of it is often
    // the part that names who acted, so the whole line is also its title.
    return html`
      <div class="row ${open ? 'open' : ''}">
        <div class="row-head">
          <span class="dot ${event.tone}"></span>
          <span class="line">
            <button
              class="row-text"
              type="button"
              title=${event.text}
              aria-expanded=${open ? 'true' : 'false'}
              aria-controls=${bodyId}
              @click=${() => this.toggle(event.id)}
            >
              ${event.text}${
                count > 1
                  ? html` <span class="count">×${count}</span>`
                  : nothing
              }
            </button>
            ${
              event.subject
                ? html`<span class="sep">·</span>
                    ${renderExecutionSubject(event.subject)}`
                : nothing
            }
            ${
              /* "A flow failed" is the same line whatever broke; the chip is
                 the shortest way to say which layer did. */
              renderFailureCategoryChip(event.failureCategory)
            }
            ${
              event.trail
                ? html`<span class="sep">·</span
                    ><span class="trail">${event.trail}</span>`
                : nothing
            }
          </span>
          <sl-icon class="chevron" name="chevron-down"></sl-icon>
          <span class="when" title=${this.absolute(event.at)}
            >${formatRelativeTime(event.at)}</span
          >
        </div>
        ${open ? this.renderRowBody(row, bodyId) : nothing}
      </div>
    `;
  }

  private renderBody() {
    if (this.loading && this.events.length === 0) {
      return html`
        <div class="rows">
          ${Array.from(
            { length: 6 },
            () =>
              html`<div class="skeleton-row">
                <sl-skeleton effect="none"></sl-skeleton>
              </div>`
          )}
        </div>
      `;
    }
    if (this.events.length === 0) {
      return html`<div class="empty">
        Nothing yet. Events appear here as agents work.
      </div>`;
    }
    return html`
      <div class="rows" @scroll=${() => this.measureBelowFold()}>
        ${repeat(
          this.rows,
          (row) => row.event.id,
          (row) => this.renderRow(row)
        )}
      </div>
    `;
  }

  render() {
    return html`
      <sl-card class="content-card">
        <div slot="header" class="card-head">
          <span class="title">Activity</span>
          ${
            this.connected
              ? html`<span class="live"
                  ><span class="live-dot"></span>live</span
                >`
              : nothing
          }
        </div>
        ${this.renderBody()}
        <div class="footer">
          ${
            this.belowFold > 0
              ? html`<span class="more">${this.belowFold} more</span>`
              : nothing
          }
          <a href="/console/audit">View audit →</a>
        </div>
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'activity-feed': ActivityFeed;
  }
}
