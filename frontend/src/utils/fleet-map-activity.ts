import type { FleetMapEdge, FleetMapGraph } from '../fleet-map-api';
import { parseUTCDate } from './date';

/**
 * Activity window and live traffic for the Fleet Map.
 *
 * The window decides which resources the map shows (the server filters;
 * an older server that ignores the parameter is filtered here with the same
 * rule). Live traffic lights the relationship a request travelled for a few
 * seconds and names what it was. A light is a state change, not an ambient
 * animation (DESIGN.md, Console > Motion).
 */

export type FleetMapWindow = '5m' | '1h' | '24h' | '7d' | '30d' | '90d' | 'any';

export const FLEET_MAP_WINDOWS: readonly {
  value: FleetMapWindow;
  label: string;
  minutes: number | null;
}[] = [
  { value: '5m', label: 'Last 5 minutes', minutes: 5 },
  { value: '1h', label: 'Last hour', minutes: 60 },
  { value: '24h', label: 'Last 24 hours', minutes: 60 * 24 },
  { value: '7d', label: 'Last 7 days', minutes: 60 * 24 * 7 },
  { value: '30d', label: 'Last 30 days', minutes: 60 * 24 * 30 },
  { value: '90d', label: 'Last 90 days', minutes: 60 * 24 * 90 },
  { value: 'any', label: 'Any time', minutes: null },
];

export const DEFAULT_FLEET_MAP_WINDOW: FleetMapWindow = '30d';
export const FLEET_MAP_WINDOW_KEY = 'preloop:fleet-map:window';

export const windowMinutes = (value: FleetMapWindow): number | null =>
  FLEET_MAP_WINDOWS.find((item) => item.value === value)?.minutes ?? null;

/** The stored window, or 30 days. Storage failures are a preference miss. */
export function loadFleetMapWindow(): FleetMapWindow {
  try {
    const saved = localStorage.getItem(FLEET_MAP_WINDOW_KEY);
    if (FLEET_MAP_WINDOWS.some((item) => item.value === saved))
      return saved as FleetMapWindow;
  } catch {
    // Private mode: fall through to the default.
  }
  return DEFAULT_FLEET_MAP_WINDOW;
}

/**
 * How often a host refetches the map so resources that went quiet drop out
 * of a short window. Any time never expires anything, so it never polls.
 */
export function windowRefreshMs(value: FleetMapWindow): number | null {
  const minutes = windowMinutes(value);
  if (minutes === null) return null;
  if (minutes <= 5) return 30_000;
  if (minutes <= 60) return 60_000;
  return 300_000;
}

/** Wait after activity from an unmapped agent before asking for a refetch. */
export const FLEET_MAP_STALE_DEBOUNCE_MS = 3_000;

export function saveFleetMapWindow(value: FleetMapWindow): void {
  try {
    localStorage.setItem(FLEET_MAP_WINDOW_KEY, value);
  } catch {
    // A preference, not a requirement.
  }
}

const DOWNSTREAM: FleetMapEdge['kind'][] = [
  'uses_model',
  'uses_account',
  'account_of_provider',
  'provided_by',
];
const UPSTREAM: FleetMapEdge['kind'][] = ['hosts', 'owns'];

/**
 * Keep what took part in activity since `sinceMs`, mirroring the server:
 * agents, devices and runners carry their own last-seen time; what an active
 * agent reaches and whoever holds an active resource are active with it.
 * Summary groups stay, they already stand for resources outside the map.
 */
export function activeGraph(
  graph: FleetMapGraph,
  sinceMs: number,
  keepIdle = false
): FleetMapGraph {
  const active = new Set(
    graph.nodes
      .filter(
        (node) =>
          node.type === 'cluster' ||
          (node.idle === false && !node.last_active_at) ||
          (node.idle !== true &&
            (node.last_active_at ||
              (['agent', 'device', 'runner'].includes(node.type) &&
                node.last_seen_at)) &&
            parseUTCDate(node.last_active_at || node.last_seen_at!).getTime() >=
              sinceMs)
      )
      .map((node) => node.id)
  );
  const idle = new Set(
    graph.nodes.filter((node) => node.idle === true).map((node) => node.id)
  );
  // When a host is still fetching a newly narrowed window, the previous
  // response's flags describe its old window. Dated model evidence must
  // pass the new bound; an agent binding cannot revive that model.
  const explicit = new Set(
    graph.nodes.filter((node) => node.idle !== undefined).map((node) => node.id)
  );
  for (const kind of DOWNSTREAM)
    for (const edge of graph.edges)
      if (
        edge.kind === kind &&
        active.has(edge.source) &&
        !idle.has(edge.target) &&
        (!explicit.has(edge.target) || active.has(edge.target))
      )
        active.add(edge.target);
  for (const kind of UPSTREAM)
    for (const edge of graph.edges)
      if (
        edge.kind === kind &&
        active.has(edge.target) &&
        !idle.has(edge.source)
      )
        active.add(edge.source);
  // Opted-in configured resources stay visible, but cannot make neighbours
  // active merely through a binding or ownership relationship.
  if (keepIdle) for (const id of idle) active.add(id);
  return {
    ...graph,
    nodes: graph.nodes.filter((node) => active.has(node.id)),
    edges: graph.edges.filter(
      (edge) => active.has(edge.source) && active.has(edge.target)
    ),
  };
}

/** Realtime topics carrying traffic the map can place. */
export const FLEET_ACTIVITY_TOPICS = [
  'gateway_activity',
  'approvals',
  'runtime_sessions',
  'flow_executions',
];

export type FleetActivityKind = 'model request' | 'tool call' | 'approval';

export interface FleetActivityHit {
  kind: FleetActivityKind;
  /** Agent node the traffic came from. */
  agent: string;
  /** Model alias, for model requests. */
  model?: string;
  /** Measured tokens on a completed model request. */
  tokens?: { input: number; output: number };
  /** What the line says while lit: the tool's own name, never a kind. */
  label?: string;
  /** A native tool runs on the agent's machine; MCP tools do not. */
  native?: boolean;
}

const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.round(value)
    : null;

const clip = (value: unknown, max = 60): string =>
  text(value).replace(/\s+/g, ' ').trim().slice(0, max);

const text = (value: unknown): string =>
  typeof value === 'string' ? value : '';

/** Read one realtime message into the agent and kind it concerns. */
export function activityFromRealtime(
  topic: string,
  message: unknown
): FleetActivityHit | null {
  if (!message || typeof message !== 'object') return null;
  const record = message as Record<string, unknown>;
  const type = text(record.type);
  const payload = (record.payload || {}) as Record<string, unknown>;
  if (topic === 'gateway_activity') {
    const flow = text(record.flow_id) || text(payload.flow_id);
    const agentId = text(payload.managed_agent_id);
    const agent = flow ? `flow:${flow}` : agentId ? `agent:${agentId}` : '';
    if (!agent) return null;
    if (
      type === 'model_gateway_call' ||
      type === 'model_gateway_request_started'
    ) {
      const input = count(payload.prompt_tokens);
      const output = count(payload.completion_tokens);
      return {
        kind: 'model request',
        agent,
        model: text(payload.requested_model) || text(payload.model_alias),
        ...(type === 'model_gateway_call' && input !== null && output !== null
          ? { tokens: { input, output } }
          : {}),
      };
    }
    if (type === 'mcp_call') {
      // An MCP tool runs on its server, not on the agent's machine.
      const tool = clip(payload.tool_name);
      const server = clip(payload.server_name);
      const agentOnly = text(payload.managed_agent_id);
      return {
        kind: 'tool call',
        agent: agentOnly ? `agent:${agentOnly}` : agent,
        label: tool ? (server ? `${tool} · ${server}` : tool) : '',
        native: false,
      };
    }
    return null;
  }
  if (
    topic === 'runtime_sessions' &&
    type === 'runtime_session_updated' &&
    payload.activity_type === 'tool_call'
  ) {
    const agent = text(payload.managed_agent_id);
    const flow = text(payload.flow_id) || text(record.flow_id);
    const tool = clip(payload.tool_name);
    const status = clip(payload.status, 20);
    return agent || flow
      ? {
          kind: 'tool call',
          agent: agent ? `agent:${agent}` : `flow:${flow}`,
          // A permission decision on a native tool, by the tool's name.
          label: tool ? (status ? `${tool} ${status}` : tool) : '',
          native: true,
        }
      : null;
  }
  if (topic === 'approvals' && type === 'approval_created') {
    const agent =
      text(record.managed_agent_id) || text(payload.managed_agent_id);
    const tool = clip(record.tool_name) || clip(payload.tool_name);
    return agent
      ? {
          kind: 'approval',
          agent: `agent:${agent}`,
          label: tool ? `approval: ${tool}` : 'approval requested',
        }
      : null;
  }
  return null;
}

/** The canonical key the server gives a model node (`detail`). */
export function modelKey(alias: string): string {
  return (alias.trim().split('/').pop() || '')
    .toLowerCase()
    .replace(/-(?:\d{8}|\d{4}-\d{2}-\d{2})$/, '')
    .replace(/:latest$/, '');
}

/**
 * The relationships a hit travelled. A model request lights the agent's or
 * flow's line to that model (every model line when the alias is unknown); a
 * native tool lights the line from the machine it runs on; an approval
 * lights the line from the person who owns the agent and decides it. An MCP
 * tool runs on a server the map does not draw, so it lights no line: its
 * node and bubble say it.
 */
export function edgesForActivity(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  hit: FleetActivityHit
): string[] {
  if (hit.kind === 'model request') {
    const lines = graph.edges.filter(
      (edge) => edge.kind === 'uses_model' && edge.source === hit.agent
    );
    const key = hit.model ? modelKey(hit.model) : '';
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    const exact = key
      ? lines.filter((edge) => {
          const detail = byId.get(edge.target)?.detail || '';
          return detail === key || detail.startsWith(`${key} `);
        })
      : [];
    return (exact.length ? exact : lines).map((edge) => edge.id);
  }
  if (hit.kind === 'tool call' && !hit.native) return [];
  const via = hit.kind === 'approval' ? 'owns' : 'hosts';
  return graph.edges
    .filter((edge) => edge.target === hit.agent && edge.kind === via)
    .map((edge) => edge.id);
}

export interface FleetActivityMark {
  counts: Map<FleetActivityKind, number>;
  until: number;
  /** The latest specific label ("Edit allowed", "Claude Fable 5.1 · 4.1k in / 380 out"). */
  label?: string;
  /** Labelled hits since the line lit. */
  hits?: number;
}

/**
 * What a lit line says: the latest specific label and how many hits it
 * stands for, or nothing. A kind ("model request", "tool call") is never a
 * label: the line already says that much.
 */
export function activityLabel(mark: FleetActivityMark): string {
  if (!mark.label) return '';
  return (mark.hits ?? 1) > 1 ? `${mark.label} ×${mark.hits}` : mark.label;
}

export interface FleetActivityOptions {
  /** How long a line stays lit after its last hit. */
  holdMs?: number;
  /** At most one repaint per this interval, however busy the fleet is. */
  flushMs?: number;
  /** At most this many lines lit at once; the stalest go dark first. */
  maxLit?: number;
  now?: () => number;
}

/**
 * Collects hits and repaints at most once per `flushMs`, so a busy fleet
 * stays readable: hits on one line add up in its label instead of flashing.
 */
export class FleetActivityTracker {
  readonly marks = new Map<string, FleetActivityMark>();
  private pending: {
    edges: string[];
    kind: FleetActivityKind;
    label: string;
  }[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private due = 0;
  private readonly holdMs: number;
  private readonly flushMs: number;
  private readonly maxLit: number;
  private readonly now: () => number;

  constructor(
    private readonly onChange: () => void,
    options: FleetActivityOptions = {}
  ) {
    this.holdMs = options.holdMs ?? 5000;
    this.flushMs = options.flushMs ?? 1000;
    this.maxLit = options.maxLit ?? 12;
    this.now = options.now ?? Date.now;
  }

  add(edges: string[], kind: FleetActivityKind, label = ''): void {
    if (!edges.length) return;
    this.pending.push({ edges, kind, label });
    this.schedule(this.flushMs);
  }

  /** Apply pending hits and expire old ones now. */
  flush(): void {
    this.timer = null;
    const now = this.now();
    for (const { edges, kind, label } of this.pending)
      for (const id of edges) {
        const mark = this.marks.get(id);
        if (!mark || mark.until <= now) {
          this.marks.set(id, {
            counts: new Map([[kind, 1]]),
            until: now + this.holdMs,
            label,
            hits: label ? 1 : 0,
          });
        } else {
          mark.counts.set(kind, (mark.counts.get(kind) || 0) + 1);
          mark.until = now + this.holdMs;
          // Only hits that say something count: a start and its completion
          // are one call.
          if (label) {
            mark.hits = (mark.hits ?? 0) + 1;
            mark.label = label;
          }
          // Re-insert so the map stays ordered stalest first.
          this.marks.delete(id);
          this.marks.set(id, mark);
        }
      }
    this.pending = [];
    for (const [id, mark] of this.marks)
      if (mark.until <= now) this.marks.delete(id);
    while (this.marks.size > this.maxLit)
      this.marks.delete(this.marks.keys().next().value as string);
    this.onChange();
    if (this.marks.size) {
      const next = Math.min(...[...this.marks.values()].map((m) => m.until));
      this.schedule(Math.max(this.flushMs, next - now));
    }
  }

  stop(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.pending = [];
    this.marks.clear();
  }

  /** Keep the earlier of the running timer and this one. */
  private schedule(delay: number): void {
    const due = this.now() + delay;
    if (this.timer && this.due <= due) return;
    if (this.timer) clearTimeout(this.timer);
    this.due = due;
    this.timer = setTimeout(() => this.flush(), delay);
  }
}
