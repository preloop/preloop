import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';

import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';

import { fetchWithAuth } from '../api';
import consoleStyles from '../styles/console-styles.css?inline';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../services/unified-websocket-manager';
import { parseUTCDate, formatRelativeTime } from '../utils/date';
import { executionDurationText } from '../utils/execution';
import {
  executionSubjectCss,
  renderExecutionSubject,
  type ExecutionSubjectSource,
} from '../utils/execution-subject';

export type FeedTone = 'success' | 'warning' | 'danger' | 'neutral';

/**
 * One line in the feed.
 *
 * `text` is the sentence, `subject` is the thing the sentence is about when
 * that thing lives outside the console (a pull request, an issue), and
 * `trail` is the one number worth carrying (a duration, a status code). The
 * row goes to `href`, unless it is a budget line, which opens the dialog that
 * can actually change the limit.
 */
export interface FeedEvent {
  id: string;
  at: string;
  tone: FeedTone;
  text: string;
  subject?: ExecutionSubjectSource | null;
  trail?: string;
  href?: string;
  budget?: boolean;
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
export const AUDIT_MAX_PAGES = 4;
const AUDIT_WINDOW_HOURS = 24;

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

const ACRONYMS: Record<string, string> = {
  api: 'API',
  ai: 'AI',
  mcp: 'MCP',
  id: 'ID',
  url: 'URL',
  sso: 'SSO',
  oauth: 'OAuth',
  ip: 'IP',
};

/** `api_key_created` reads as "API key created", not "api key created". */
export function humaniseAction(action: string): string {
  const words = (action || '')
    .replace(/[_-]+/g, ' ')
    .trim()
    .split(/\s+/)
    .map((word) => ACRONYMS[word.toLowerCase()] || word);
  if (words.length === 0) return '';
  const first = words[0];
  words[0] = ACRONYMS[first.toLowerCase()]
    ? first
    : first.charAt(0).toUpperCase() + first.slice(1);
  return words.join(' ');
}

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
  const base = { id: event.id, at: event.timestamp };
  const actor =
    lookups.userName(event.user_id) ||
    (typeof details.username === 'string' ? details.username : null);

  switch (event.action) {
    case 'model_gateway_request': {
      const model =
        details.requested_model || details.model_alias || event.resource_id;
      const status = String(event.status || outcome || '').toLowerCase();
      if (status === 'success' || status === 'executed') {
        return null;
      }
      if (status === 'budget_denied') {
        return {
          ...base,
          tone: 'warning',
          text: `Gateway request denied by budget · ${model || 'model'}`,
          href: '/console/audit?event_type=model_gateway_request',
        };
      }
      const code = details.status_code ? ` · ${details.status_code}` : '';
      return {
        ...base,
        tone: 'danger',
        text: `Gateway request failed · ${model || 'model'}${code}`,
        href: '/console/api-usage',
      };
    }
    case 'tool_call': {
      const tool = event.resource_id || details.tool_name || 'Tool';
      // A busy hour is a wall of "update_pull_request ran", so every tool
      // line names who called it: the agent when the audit knows it, the
      // flow run or session principal otherwise. The row then goes to that
      // session rather than to a filtered audit page, which is as specific
      // as this event gets.
      const caller =
        (typeof details.managed_agent_name === 'string'
          ? details.managed_agent_name
          : null) ||
        (typeof details.runtime_principal_name === 'string'
          ? details.runtime_principal_name
          : null);
      const agent = caller ? ` (${caller})` : '';
      const by = caller ? ` · ${caller}` : '';
      const sessionId =
        typeof details.runtime_session_id === 'string'
          ? details.runtime_session_id
          : null;
      const href = sessionId
        ? `/console/runtime-sessions?sessionId=${sessionId}`
        : '/console/audit?event_type=tool_call';
      const normalized = (outcome || '').toLowerCase();
      if (normalized === 'require_approval') {
        return {
          ...base,
          tone: 'warning',
          text: `Approval requested: ${tool}${agent}`,
          href: '/console/approvals',
        };
      }
      if (normalized === 'deny') {
        return {
          ...base,
          tone: 'warning',
          text: `${tool} blocked${by}`,
          href,
        };
      }
      if (normalized === 'failed') {
        return {
          ...base,
          tone: 'danger',
          text: `${tool} failed${by}`,
          href,
        };
      }
      return {
        ...base,
        tone: 'neutral',
        text: `${tool} ran${by}`,
        href,
      };
    }
    case 'authentication':
      return {
        ...base,
        tone: 'neutral',
        text: `${details.username || actor || 'Someone'} signed in`,
        href: '/console/audit?event_type=authentication',
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
        tone: 'neutral',
        text: `${label} ${action}${by}${about}`,
        href: '/console/audit?event_type=configuration_change',
      };
    }
    case 'runtime_session_created':
      return {
        ...base,
        tone: 'neutral',
        text: `${details.runtime_principal_name || 'An agent'} started a session`,
        href: event.resource_id
          ? `/console/runtime-sessions?sessionId=${event.resource_id}`
          : '/console/runtime-sessions',
      };
    case 'runtime_session_ended':
      return {
        ...base,
        tone: 'neutral',
        text: `${details.runtime_principal_name || 'An agent'} ended a session`,
        href: event.resource_id
          ? `/console/runtime-sessions?sessionId=${event.resource_id}`
          : '/console/runtime-sessions',
      };
    case 'runtime_session_updated':
      // A session that is still going is not an event.
      return null;
    case 'approval_created':
    case 'approval_approved':
    case 'approval_denied':
    case 'approval_expired': {
      const tool = details.tool_name || event.resource_id || 'a tool';
      const requestId = details.approval_request_id || event.resource_id;
      const href = requestId
        ? `/console/approval/${requestId}`
        : '/console/approvals';
      const by = actor ? ` by ${actor}` : '';
      if (event.action === 'approval_created') {
        return {
          ...base,
          tone: 'warning',
          text: `Approval requested: ${tool}`,
          href,
        };
      }
      if (event.action === 'approval_approved') {
        return {
          ...base,
          tone: 'success',
          text: `Approval approved${by}: ${tool}`,
          href,
        };
      }
      if (event.action === 'approval_denied') {
        return {
          ...base,
          tone: 'danger',
          text: `Approval declined${by}: ${tool}`,
          href,
        };
      }
      return {
        ...base,
        tone: 'warning',
        text: `Approval expired: ${tool}`,
        href,
      };
    }
    default: {
      // An event type nobody wrote a recipe for still says who did what.
      const words = event.action.split('_');
      const verb = words[words.length - 1];
      const subject = words.slice(0, -1).join('_');
      if (PAST_TENSE.has(verb) && subject) {
        const by = actor ? ` by ${actor}` : '';
        return {
          ...base,
          tone: toneFromOutcome(outcome),
          text: `${humaniseAction(subject)} ${verb}${by}`,
          href: `/console/audit?event_type=${event.action}`,
        };
      }
      const trail = actor ? ` · ${actor}` : '';
      return {
        ...base,
        tone: toneFromOutcome(outcome),
        text: `${humaniseAction(event.action)}${trail}`,
        href: `/console/audit?event_type=${event.action}`,
      };
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
    const base = {
      id: `execution:${executionId}:${status}`,
      at,
      subject: known || null,
      href,
    };
    if (status === 'SUCCEEDED' || status === 'COMPLETED') {
      const duration =
        executionDurationText({
          status,
          start_time: String(payload.start_time || known?.start_time || ''),
          end_time: payload.end_time || known?.end_time || at,
        }) || undefined;
      return {
        ...base,
        tone: 'success',
        text: `${name} succeeded`,
        trail: duration,
      };
    }
    if (status === 'FAILED' || status === 'TIMEOUT' || status === 'ERROR') {
      return { ...base, tone: 'danger', text: `${name} failed` };
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
    const base = { id: `approval:${requestId}:${type}`, at, href };
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
      tone: hard ? 'danger' : 'warning',
      text: amount ? `${words} · ${amount}` : words,
      budget: true,
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
      tone: 'danger',
      text: `Gateway request failed · ${model || 'model'}${
        status ? ` · ${status}` : ''
      }`,
      href: '/console/api-usage',
    };
  }

  if (topic === 'managed_agents') {
    if (type !== 'managed_agent_created') return null;
    const agentId = String(payload.agent_id || '');
    return {
      id: `agent:${agentId}:created`,
      at,
      tone: 'neutral',
      text: `${payload.display_name || 'An agent'} connected`,
      href: agentId ? `/console/agents/${agentId}` : '/console/agents',
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
    if (type === 'runtime_session_created') {
      return {
        id: `session:${sessionId}:created`,
        at,
        tone: 'neutral',
        text: `${who} started a session`,
        href,
      };
    }
    if (type === 'runtime_session_ended') {
      return {
        id: `session:${sessionId}:ended`,
        at,
        tone: 'neutral',
        text: `${who} ended a session`,
        href,
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

  @state() private events: FeedEvent[] = [];
  @state() private loading = true;
  @state() private connected = false;
  @state() private users: FeedContext['users'] = [];

  private unsubscribes: (() => void)[] = [];
  private seen = new Set<string>();
  /** Resolved (never rejected) once the user lookup has had its turn. */
  private usersReady: Promise<void> = Promise.resolve();

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(executionSubjectCss),
    css`
      :host {
        display: block;
        width: 100%;
      }

      .content-card,
      .content-card::part(base) {
        width: 100%;
      }

      .content-card::part(body) {
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

      .rows {
        display: flex;
        flex-direction: column;
      }

      .row {
        align-items: baseline;
        border-bottom: 1px solid var(--console-hairline);
        display: flex;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        position: relative;
      }

      .row:last-child {
        border-bottom: none;
      }

      .row:hover {
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
      .row-text {
        background: none;
        border: none;
        color: inherit;
        cursor: pointer;
        font: inherit;
        overflow: hidden;
        padding: 0;
        text-align: left;
        text-decoration: none;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .row-text::after {
        content: '';
        inset: 0;
        position: absolute;
      }

      .row:hover .row-text,
      .row-text:focus-visible {
        text-decoration: underline;
      }

      .execution-subject,
      a.execution-subject-link {
        position: relative;
        z-index: 1;
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

      .footer {
        border-top: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
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
      this.usersReady = this.loadUsers();
      void this.loadInitial();
    } else {
      this.loading = false;
    }
  }

  disconnectedCallback(): void {
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
      users: this.users,
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
  private async loadUsers(): Promise<void> {
    try {
      const response = await fetchWithAuth('/api/v1/users');
      if (!response.ok) return;
      const data = await response.json();
      const users = Array.isArray(data) ? data : data?.users;
      this.users = Array.isArray(users) ? users : [];
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
      if (into.length >= FEED_INITIAL_ROWS) return;
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
    // The actor's name belongs on the first paint, not the second, but a
    // lookup that never answers must not hold the timeline hostage either.
    await Promise.race([
      this.usersReady,
      new Promise((resolve) => setTimeout(resolve, 2000)),
    ]);
    try {
      const since = new Date(
        Date.now() - AUDIT_WINDOW_HOURS * 60 * 60 * 1000
      ).toISOString();
      const events: FeedEvent[] = [];
      let emptyWindow = false;
      for (let page = 0; page < AUDIT_MAX_PAGES; page += 1) {
        const groups = await this.fetchAuditPage(page * AUDIT_PAGE_SIZE, since);
        if (groups === null) break;
        if (page === 0 && groups.length === 0) emptyWindow = true;
        this.rowsFrom(groups, events);
        if (events.length >= FEED_INITIAL_ROWS) break;
        if (groups.length < AUDIT_PAGE_SIZE) break;
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
   * Take one realtime message. Public because the socket is not the only
   * caller worth having: tests and previews feed the same door.
   */
  ingest(topic: string, message: unknown): void {
    const event = feedEventFromRealtime(topic, message, this.context);
    if (!event || this.seen.has(event.id)) return;
    this.seen.add(event.id);
    this.events = this.sortAndCap([event, ...this.events]);
  }

  /** Newest first, capped: the feed is a window, not a log. */
  private sortAndCap(events: FeedEvent[]): FeedEvent[] {
    const sorted = [...events].sort(
      (a, b) => this.time(b.at) - this.time(a.at)
    );
    const kept = sorted.slice(0, FEED_CAP);
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

  private renderRow(event: FeedEvent) {
    // The line ellipsises in a narrow column, and the tail of it is often
    // the part that names who acted, so the whole line is also its title.
    const text = event.budget
      ? html`<button
          class="row-text"
          type="button"
          title=${event.text}
          @click=${() => this.openBudgetDialog()}
        >
          ${event.text}
        </button>`
      : html`<a
          class="row-text"
          href=${event.href || '/console/audit'}
          title=${event.text}
          >${event.text}</a
        >`;
    return html`
      <div class="row">
        <span class="dot ${event.tone}"></span>
        <span class="line">
          ${text}
          ${
            event.subject
              ? html`<span class="sep">·</span>
                  ${renderExecutionSubject(event.subject)}`
              : nothing
          }
          ${
            event.trail
              ? html`<span class="sep">·</span
                  ><span class="trail">${event.trail}</span>`
              : nothing
          }
        </span>
        <span class="when" title=${this.absolute(event.at)}
          >${formatRelativeTime(event.at)}</span
        >
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
      <div class="rows">
        ${repeat(
          this.events,
          (event) => event.id,
          (event) => this.renderRow(event)
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
        <div class="footer"><a href="/console/audit">View audit →</a></div>
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'activity-feed': ActivityFeed;
  }
}
