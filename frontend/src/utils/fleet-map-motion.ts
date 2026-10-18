import type {
  FleetCodeAction,
  FleetCodeCounts,
  FleetMapGraph,
  FleetMapNode,
} from '../fleet-map-api';
import { modelKey } from './fleet-map-activity';

/**
 * Live state for the Fleet Map: nodes lit where traffic starts, nodes
 * entering and leaving the window, and code events surfacing on a node.
 *
 * Everything is a state change caused by real traffic, never ambient
 * (DESIGN.md, Console > Motion): it starts when an event arrives and ends a
 * second or two later, and is a pure function of the clock, so it is the
 * same at every frame rate and under a fake clock. Nothing travels along a
 * line: the line and the node where traffic started light up. With
 * prefers-reduced-motion nodes also appear and leave without fading.
 */

export type FleetMessageKind =
  'request' | 'response' | 'tool' | 'approval' | 'decision' | 'code';

/** What one realtime message sends where; `delay` staggers a reply. */
export interface FleetMessage {
  from: string;
  to: string;
  kind: FleetMessageKind;
  label: string;
  delay?: number;
}

/** A node lit because traffic just started there. */
export interface FleetLight {
  node: string;
  kind: FleetMessageKind;
  label: string;
  start: number;
  until: number;
}

/**
 * Nodes lit by live traffic. A message lights the node it starts from (the
 * agent or flow that sent a request, the model that answered, the person
 * who decided) for `holdMs`; the line it travels lights through the
 * activity tracker. Nothing moves: a light is a state change. Capped, so a
 * busy fleet keeps the stalest lights dark.
 */
export class FleetLights {
  private items = new Map<string, FleetLight>();
  private shown = '';

  constructor(
    readonly holdMs = 1600,
    readonly max = 16
  ) {}

  /** Light the origin of `message`, after its delay. */
  light(message: FleetMessage, now: number): void {
    const start = now + (message.delay ?? 0);
    const current = this.items.get(message.from);
    this.items.delete(message.from);
    this.items.set(message.from, {
      node: message.from,
      kind: message.kind,
      label: message.label,
      start:
        current && current.start <= start && current.until > start
          ? current.start
          : start,
      until: start + this.holdMs,
    });
    while (this.items.size > this.max)
      this.items.delete(this.items.keys().next().value as string);
  }

  /** The light on `node` at `now`, if any. */
  at(node: string, now: number): FleetLight | undefined {
    const item = this.items.get(node);
    return item && item.start <= now && now < item.until ? item : undefined;
  }

  /** Lit nodes at `now`. */
  active(now: number): FleetLight[] {
    return [...this.items.values()].filter(
      (item) => item.start <= now && now < item.until
    );
  }

  /** Drop expired lights; true when what is lit changed since last asked. */
  changed(now: number): boolean {
    for (const [id, item] of this.items)
      if (item.until <= now) this.items.delete(id);
    const key = this.active(now)
      .map((item) => `${item.node}:${item.kind}`)
      .sort()
      .join('|');
    const moved = key !== this.shown;
    this.shown = key;
    return moved;
  }

  get size(): number {
    return this.items.size;
  }

  clear(): void {
    this.items.clear();
    this.shown = '';
  }
}

const text = (value: unknown): string =>
  typeof value === 'string' ? value : '';

const CODE_LABEL: Record<FleetCodeAction, string> = {
  opened: 'opened a PR',
  commented: 'commented',
  reviewed: 'reviewed',
  merged: 'merged',
};

/** A code event read from the `code_activity` topic. */
export interface FleetCodeHit {
  action: FleetCodeAction;
  /** Agent or flow nodes that acted. */
  nodes: string[];
  pr: string;
}

export function codeHitFromRealtime(message: unknown): FleetCodeHit | null {
  if (!message || typeof message !== 'object') return null;
  const record = message as Record<string, unknown>;
  if (text(record.type) !== 'code_activity') return null;
  const payload = (record.payload || {}) as Record<string, unknown>;
  const action = text(payload.action) as FleetCodeAction;
  if (!(action in CODE_LABEL)) return null;
  const nodes: string[] = [];
  const flow = text(payload.flow_id);
  const agent = text(payload.managed_agent_id);
  // A call inside a flow run belongs to the flow, as on the server.
  if (flow) nodes.push(`flow:${flow}`);
  else if (agent) nodes.push(`agent:${agent}`);
  if (Array.isArray(payload.flow_ids))
    for (const id of payload.flow_ids)
      if (typeof id === 'string' && id) nodes.push(`flow:${id}`);
  return nodes.length ? { action, nodes, pr: text(payload.pr) } : null;
}

export const codeLabel = (hit: Pick<FleetCodeHit, 'action' | 'pr'>): string =>
  hit.pr ? `${CODE_LABEL[hit.action]} ${hit.pr}` : CODE_LABEL[hit.action];

/** Add one event to the counts of the nodes that acted and their owners. */
export function countCode(
  counts: Record<string, FleetCodeCounts>,
  nodes: string[],
  action: FleetCodeAction
): Record<string, FleetCodeCounts> {
  const next = { ...counts };
  for (const id of nodes) {
    const current = next[id] ?? {
      opened: 0,
      commented: 0,
      reviewed: 0,
      merged: 0,
    };
    next[id] = { ...current, [action]: current[action] + 1 };
  }
  return next;
}

/** The person who owns an agent on the map, if any. */
export function ownerOf(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  agent: string
): string | undefined {
  const direct = graph.edges.find(
    (edge) => edge.kind === 'owns' && edge.target === agent
  )?.source;
  if (direct) return direct;
  const node = graph.nodes.find((item) => item.id === agent);
  const owner = node?.owner_ids.find((id) => id.startsWith('user:'));
  if (owner) return owner;
  // An agent on someone's laptop answers to the laptop's owner.
  const device = graph.edges.find(
    (edge) => edge.kind === 'hosts' && edge.target === agent
  )?.source;
  return device
    ? graph.edges.find((edge) => edge.kind === 'owns' && edge.target === device)
        ?.source
    : undefined;
}

/**
 * The messages one realtime event sends. A model request goes agent to model
 * and its response comes back; an approval request goes to the person who
 * owns the agent and the decision comes back from them; a tool call leaves
 * the agent for the device it runs on. Nothing is sent between nodes the map
 * does not show.
 */
export function messagesForRealtime(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  topic: string,
  message: unknown
): FleetMessage[] {
  if (!message || typeof message !== 'object') return [];
  const record = message as Record<string, unknown>;
  const type = text(record.type);
  const payload = (record.payload || {}) as Record<string, unknown>;
  const ids = new Set(graph.nodes.map((node) => node.id));
  const agentId =
    text(payload.managed_agent_id) || text(record.managed_agent_id);
  const flowId = text(record.flow_id) || text(payload.flow_id);
  const flow = flowId && ids.has(`flow:${flowId}`) ? `flow:${flowId}` : '';
  const mappedAgent =
    agentId && ids.has(`agent:${agentId}`) ? `agent:${agentId}` : '';
  // A flow run's model calls belong to the flow, as the server counts them,
  // even when the run also carries an employee agent's id. Tool calls and
  // approvals stay with the agent, which is the one acting.
  const agent =
    type === 'model_gateway_call' ? flow || mappedAgent : mappedAgent || flow;
  if (!agent) return [];
  const out: FleetMessage[] = [];
  if (topic === 'gateway_activity' && type === 'model_gateway_call') {
    const alias = text(payload.requested_model) || text(payload.model_alias);
    const key = alias ? modelKey(alias) : '';
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    const lines = graph.edges.filter(
      (edge) => edge.kind === 'uses_model' && edge.source === agent
    );
    const exact = lines.filter((edge) => {
      const detail = byId.get(edge.target)?.detail || '';
      return key && (detail === key || detail.startsWith(`${key} `));
    });
    const target = (exact[0] ?? (lines.length === 1 ? lines[0] : undefined))
      ?.target;
    if (!target) return [];
    const label = alias.split('/').pop() || 'request';
    out.push({ from: agent, to: target, kind: 'request', label });
    out.push({
      from: target,
      to: agent,
      kind: 'response',
      label: '',
      delay: 900,
    });
    return out;
  }
  if (topic === 'gateway_activity' && type === 'mcp_call') {
    const device = graph.edges.find(
      (edge) =>
        (edge.kind === 'hosts' || edge.kind === 'owns') &&
        edge.target === agent &&
        ids.has(edge.source)
    )?.source;
    if (!device) return [];
    return [
      {
        from: agent,
        to: device,
        kind: 'tool',
        label: text(payload.tool_name) || 'tool call',
      },
    ];
  }
  if (topic === 'approvals' && type.startsWith('approval_')) {
    const person = ownerOf(graph, agent);
    if (!person || !ids.has(person)) return [];
    const tool = text(record.tool_name) || text(payload.tool_name);
    if (type === 'approval_created')
      return [
        {
          from: agent,
          to: person,
          kind: 'approval',
          label: tool ? `approve ${tool}?` : 'approval?',
        },
      ];
    if (type === 'approval_approved' || type === 'approval_declined')
      return [
        {
          from: person,
          to: agent,
          kind: 'decision',
          label: type === 'approval_approved' ? 'approved' : 'declined',
        },
      ];
  }
  return [];
}

/**
 * Group nodes for the spatial layout: a person with their devices and the
 * agents on them, a device with no owner on its own, flows together, and
 * the models and providers they share in the middle.
 */
export function clusterOf(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>
): Map<string, string> {
  const group = new Map<string, string>();
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const shared = (node: FleetMapNode) =>
    ['model', 'provider_account', 'provider'].includes(node.type) ||
    (node.type === 'cluster' &&
      ['model', 'provider_account', 'provider'].includes(
        node.cluster_of || ''
      ));
  for (const node of graph.nodes) {
    if (node.type === 'user') group.set(node.id, node.id);
    else if (shared(node)) group.set(node.id, 'shared');
    else if (node.type === 'flow') group.set(node.id, 'flows');
  }
  const assign = (id: string, to: string) => {
    if (!group.has(id) && byId.has(id)) group.set(id, to);
  };
  for (const edge of graph.edges)
    if (edge.kind === 'owns' && group.has(edge.source))
      assign(edge.target, group.get(edge.source)!);
  for (let pass = 0; pass < 2; pass++)
    for (const edge of graph.edges)
      if (edge.kind === 'hosts' && group.has(edge.source))
        assign(edge.target, group.get(edge.source)!);
  for (const node of graph.nodes)
    if (!group.has(node.id))
      group.set(
        node.id,
        node.type === 'device' || node.type === 'runner' ? node.id : 'other'
      );
  // An agent with no device or owner sits with the flows and runners.
  for (const edge of graph.edges)
    if (edge.kind === 'hosts' && group.get(edge.target) === 'other')
      group.set(edge.target, group.get(edge.source)!);
  return group;
}

/** Node lifecycle for enter and exit fades. */
export interface FleetPresence {
  entered: Map<string, number>;
  leaving: Map<string, { node: FleetMapNode; at: number }>;
}

export const ENTER_MS = 320;
export const EXIT_MS = 260;

/**
 * Compare the nodes on screen with the next set: new ones start entering,
 * missing ones start leaving (kept with their last node so they can fade
 * where they stood). The first set never animates.
 */
export function nextPresence(
  presence: FleetPresence,
  previous: FleetMapNode[] | null,
  next: FleetMapNode[],
  now: number
): FleetPresence {
  const entered = new Map(
    [...presence.entered].filter(([, at]) => now - at < ENTER_MS)
  );
  const leaving = new Map(
    [...presence.leaving].filter(([, item]) => now - item.at < EXIT_MS)
  );
  if (!previous) return { entered, leaving };
  const nextIds = new Set(next.map((node) => node.id));
  const before = new Set(previous.map((node) => node.id));
  for (const node of next)
    if (!before.has(node.id)) {
      entered.set(node.id, now);
      leaving.delete(node.id);
    }
  for (const node of previous)
    if (!nextIds.has(node.id) && !leaving.has(node.id))
      leaving.set(node.id, { node, at: now });
  return { entered, leaving };
}

/** Opacity and scale of a node at `now`; 1 and 1 once settled. */
export function presenceStyle(
  presence: FleetPresence,
  id: string,
  now: number
): { opacity: number; scale: number } | null {
  const leaving = presence.leaving.get(id);
  if (leaving) {
    const t = Math.min(1, (now - leaving.at) / EXIT_MS);
    // Ease in on the way out.
    return { opacity: 1 - t * t, scale: 1 - 0.08 * t * t };
  }
  const entered = presence.entered.get(id);
  if (entered === undefined) return null;
  const t = Math.min(1, Math.max(0, (now - entered) / ENTER_MS));
  if (t >= 1) return null;
  const out = 1 - Math.pow(1 - t, 3);
  return { opacity: out, scale: 0.92 + 0.08 * out };
}

/** A code event surfacing on a node; `toast` also lists it in the corner. */
export interface FleetPip {
  id: number;
  node: string;
  action: FleetCodeAction;
  label: string;
  start: number;
}

export const PIP_MS = 2600;
