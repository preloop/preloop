import { fetchWithAuth } from './api';

/**
 * The Fleet Map graph: who owns which machine, what runs on it, and which
 * models, provider accounts and providers those agents reach. One request,
 * already scoped to the account and filtered by the caller's permissions.
 */
export type FleetMapResourceType =
  | 'user'
  | 'device'
  | 'runner'
  | 'agent'
  | 'flow'
  | 'work'
  | 'model'
  | 'provider_account'
  | 'provider';
export type FleetMapNodeType = FleetMapResourceType | 'cluster';
export type FleetMapEdgeType =
  | 'owns'
  | 'hosts'
  | 'runs_on'
  | 'uses_model'
  | 'uses_account'
  | 'account_of_provider'
  | 'provided_by'
  | 'caused'
  | FleetWorkRelationKind;
export type FleetMapGapCode =
  | 'discovered_agent'
  | 'unknown_device'
  | 'offline_runner'
  | 'not_via_gateway'
  | 'open_finding';
export interface FleetMapGap {
  code: FleetMapGapCode;
  label: string;
  severity: 'warning' | 'danger';
}
export interface FleetMapAction {
  label: string;
  href: string;
}
export interface FleetMapNode {
  id: string;
  type: FleetMapNodeType;
  resource_id: string;
  label: string;
  detail: string | null;
  href: string;
  status: string | null;
  owner_ids: string[];
  provider_ids: string[];
  gaps: FleetMapGapCode[];
  finding_count: number;
  actions: FleetMapAction[];
  cluster_of?: FleetMapResourceType;
  count?: number;
  /** Containing node, e.g. the provider of a provider account. */
  parent_id?: string | null;
  /** Agents only. Missing on older servers, read as onboarded. */
  agent_state?: FleetMapAgentState | null;
  last_seen_at?: string | null;
  /** Work object kind and the observed review state, never inferred. */
  work_kind?: 'issue' | 'pull_request';
  review_state?: string | null;
  /** Tracker labels on a work object, as reported by its events. */
  labels?: string[];
  /** Optional pass 3 metadata. Missing activity evidence stays unknown. */
  avatar_url?: string | null;
  idle?: boolean;
  last_active_at?: string | null;
  work_title?: string | null;
  work_reference?: string;
}
export type FleetMapAgentState = 'onboarded' | 'discovered' | 'inactive';
export interface FleetMapEdge {
  id: string;
  source: string;
  target: string;
  kind: FleetMapEdgeType;
  provenance: 'configured' | 'observed';
  impact_source: string;
  impact_target: string;
  gaps: FleetMapGapCode[];
  /** uses_model only: the provider account node the agent goes through. */
  account_id?: string | null;
  actions?: string[];
  count?: number;
  last_active_at?: string;
}
export interface FleetMapGraph {
  schema_version: 1;
  nodes: FleetMapNode[];
  edges: FleetMapEdge[];
  truncated: boolean;
  limit: number;
  clusters: { type: FleetMapResourceType; count: number }[];
  unavailable: string[];
  decommissioned_agents?: number;
  /** Activity window the server applied; absent on servers that ignore it. */
  active_within_minutes?: number | null;
}

/** Every resource type, in reading order; each can be shown or hidden. */
export const FLEET_MAP_TYPES: readonly FleetMapResourceType[] = [
  'user',
  'device',
  'runner',
  'agent',
  'flow',
  'model',
  'provider_account',
  'provider',
  'work',
];

export type FleetMapColumn =
  'user' | 'device' | 'agent' | 'model' | 'provider' | 'flow' | 'work';

/**
 * Columns left to right: a person, the machines they own (runners sit inside
 * their machine), the agents on them, the models those agents call, and the
 * providers (provider accounts sit inside their provider).
 */
export const FLEET_MAP_COLUMNS: readonly FleetMapColumn[] = [
  'user',
  'device',
  'agent',
  'model',
  'provider',
  'flow',
  'work',
];

export const FLEET_MAP_COLUMN_LABEL: Record<
  Exclude<FleetMapNodeType, 'cluster'>,
  string
> = {
  user: 'People',
  device: 'Devices',
  runner: 'Runners',
  agent: 'Agents',
  flow: 'Flows',
  work: 'Work',
  model: 'Models',
  provider_account: 'Provider accounts',
  provider: 'Providers',
};

/** Singular names, for the side panel and screen readers. */
export const FLEET_MAP_TYPE_LABEL: Record<FleetMapNodeType, string> = {
  user: 'Person',
  device: 'Device',
  runner: 'Runner',
  agent: 'Agent',
  flow: 'Flow',
  work: 'Work',
  model: 'Model',
  provider_account: 'Provider account',
  provider: 'Provider',
  cluster: 'Group',
};

export const FLEET_MAP_EDGE_LABEL: Record<FleetMapEdgeType, string> = {
  owns: 'owns',
  hosts: 'hosts',
  runs_on: 'runs on',
  uses_model: 'uses model',
  uses_account: 'uses account',
  account_of_provider: 'account of provider',
  provided_by: 'provided by',
  caused: 'observed activity with',
  resolves: 'resolves',
  mentions: 'mentions',
  triggered: 'triggered by',
  produced: 'produced',
  touched: 'touched',
};

const COLUMN_OF: Record<FleetMapResourceType, FleetMapColumn> = {
  user: 'user',
  device: 'device',
  runner: 'device',
  agent: 'agent',
  flow: 'flow',
  work: 'work',
  model: 'model',
  provider_account: 'provider',
  provider: 'provider',
};

/** The resource type a node stands for; summary groups stand for theirs. */
export const nodeType = (node: FleetMapNode): FleetMapResourceType =>
  node.type === 'cluster' ? node.cluster_of || 'agent' : node.type;

export const nodeColumn = (node: FleetMapNode): FleetMapColumn =>
  COLUMN_OF[nodeType(node)];

/** Agents without a server state predate it and were all onboarded. */
export const agentState = (node: FleetMapNode): FleetMapAgentState =>
  node.agent_state ??
  (node.gaps.includes('discovered_agent') ? 'discovered' : 'onboarded');

const GAP_LABEL: Record<FleetMapGapCode, string> = {
  discovered_agent: 'Not onboarded',
  unknown_device: 'Device association unknown',
  offline_runner: 'Runner offline',
  not_via_gateway: 'Not via the gateway',
  open_finding: 'Open fleet finding',
};

/** Only explicit server evidence creates a governance gap. */
export function nodeGaps(node: FleetMapNode): FleetMapGap[] {
  return [...new Set(node.gaps)].map((code) => ({
    code,
    label:
      code === 'open_finding' && node.finding_count > 1
        ? `${node.finding_count} open fleet findings`
        : GAP_LABEL[code],
    severity: 'warning',
  }));
}

/** Follow the server's dependency direction, independent of visual layout. */
export function blastRadius(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  selectedId: string
): { nodes: Set<string> } {
  const ids = new Set(graph.nodes.map((node) => node.id));
  const nodes = new Set<string>();
  if (!ids.has(selectedId)) return { nodes };
  const neighbours = new Map<string, string[]>();
  graph.edges.forEach((edge) => {
    if (!ids.has(edge.impact_source) || !ids.has(edge.impact_target)) return;
    const list = neighbours.get(edge.impact_source) || [];
    list.push(edge.impact_target);
    neighbours.set(edge.impact_source, list);
  });
  const queue = [selectedId];
  nodes.add(selectedId);
  for (let i = 0; i < queue.length; i++) {
    for (const next of neighbours.get(queue[i]) || []) {
      if (!nodes.has(next)) {
        nodes.add(next);
        queue.push(next);
      }
    }
  }
  return { nodes };
}

/** What the selected node itself relies on: the reverse of blast radius. */
export function dependencies(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  selectedId: string
): Set<string> {
  const reversed = graph.edges.map((edge) => ({
    ...edge,
    impact_source: edge.impact_target,
    impact_target: edge.impact_source,
  }));
  return blastRadius({ nodes: graph.nodes, edges: reversed }, selectedId).nodes;
}

export interface FleetMapFilters {
  types: ReadonlySet<FleetMapNodeType>;
  owner: string;
  provider: string;
  /** Agent states to show; omitted means all. */
  states?: ReadonlySet<FleetMapAgentState>;
  /** Keep only nodes with a gap and their direct neighbours. */
  gapsOnly?: boolean;
}

/**
 * Facets use permission-filtered server associations, not an undirected walk.
 * When agents are narrowed (by state or gaps), models, accounts and providers
 * that no remaining agent reaches drop out too, so a filter shrinks the whole
 * picture instead of leaving a column of orphans.
 */
export function filterGraph(
  graph: FleetMapGraph,
  filters: FleetMapFilters
): FleetMapGraph {
  let nodes = graph.nodes.filter(
    (node) =>
      filters.types.has(nodeType(node)) &&
      (node.type !== 'agent' ||
        !filters.states ||
        filters.states.has(agentState(node))) &&
      (!filters.owner ||
        node.id === filters.owner ||
        node.owner_ids.includes(filters.owner)) &&
      (!filters.provider ||
        node.id === filters.provider ||
        node.provider_ids.includes(filters.provider))
  );
  const edgesOf = (ids: Set<string>) =>
    graph.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  if (filters.gapsOnly) {
    const ids = new Set(nodes.map((node) => node.id));
    const gapIds = new Set(
      nodes.filter((node) => node.gaps.length).map((node) => node.id)
    );
    // One hop only, from the gap nodes themselves, whatever the edge order.
    const keep = new Set(gapIds);
    for (const edge of edgesOf(ids)) {
      if (gapIds.has(edge.source)) keep.add(edge.target);
      if (gapIds.has(edge.target)) keep.add(edge.source);
    }
    // Keep the provider around a kept account so containment still reads.
    for (const node of nodes)
      if (keep.has(node.id) && node.parent_id && ids.has(node.parent_id))
        keep.add(node.parent_id);
    nodes = nodes.filter((node) => keep.has(node.id));
  }
  const narrowed =
    filters.gapsOnly ||
    (filters.states && filters.states.size < 3) ||
    nodes.length < graph.nodes.length;
  if (narrowed && nodes.some((node) => node.type === 'agent')) {
    const reached = new Set(
      nodes
        .filter((n) => n.type === 'agent' || n.type === 'flow')
        .map((n) => n.id)
    );
    // Hidden accounts still connect their models to providers. Filtered-out
    // agents do not contribute reachability, even when their account is shared.
    const downstream = new Set([
      'uses_model',
      'uses_account',
      'provided_by',
      'account_of_provider',
    ]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const edge of graph.edges)
        if (
          downstream.has(edge.kind) &&
          reached.has(edge.source) &&
          !reached.has(edge.target)
        ) {
          reached.add(edge.target);
          changed = true;
        }
      for (const node of graph.nodes)
        if (reached.has(node.id))
          for (const id of node.provider_ids)
            if (!reached.has(id)) {
              reached.add(id);
              changed = true;
            }
    }
    nodes = nodes.filter(
      (node) =>
        !['model', 'provider_account', 'provider'].includes(node.type) ||
        // New servers supply activity evidence, including direct person
        // requests and configured idle resources explicitly opted into.
        node.idle !== undefined ||
        reached.has(node.id)
    );
  }
  const ids = new Set(nodes.map((node) => node.id));
  return { ...graph, nodes, edges: edgesOf(ids) };
}

/**
 * Which node visually contains which: a provider account sits inside its
 * provider, a runner inside the device that hosts it.
 */
export function containers(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>
): Map<string, string> {
  const ids = new Map(graph.nodes.map((node) => [node.id, node]));
  const parent = new Map<string, string>();
  for (const node of graph.nodes) {
    if (
      node.type === 'work' &&
      node.parent_id &&
      ids.get(node.parent_id)?.type === 'work'
    ) {
      parent.set(node.id, node.parent_id);
      continue;
    }
    if (node.type !== 'provider_account') continue;
    const candidate =
      node.parent_id ??
      graph.edges.find(
        (edge) => edge.kind === 'account_of_provider' && edge.source === node.id
      )?.target ??
      node.provider_ids[0];
    if (candidate && ids.has(candidate)) parent.set(node.id, candidate);
  }
  for (const edge of graph.edges)
    if (
      edge.kind === 'hosts' &&
      ids.get(edge.source)?.type === 'device' &&
      ids.get(edge.target)?.type === 'runner' &&
      !parent.has(edge.target)
    )
      parent.set(edge.target, edge.source);
  return parent;
}

/** Summary nodes represent omitted resources, never expanded by client filters. */
export function withClusters(graph: FleetMapGraph): FleetMapGraph {
  return {
    ...graph,
    nodes: [
      ...graph.nodes,
      ...graph.clusters.map((cluster): FleetMapNode => ({
        id: `cluster:${cluster.type}`,
        type: 'cluster',
        resource_id: '',
        label: `${cluster.count} more ${FLEET_MAP_COLUMN_LABEL[cluster.type].toLowerCase()}`,
        detail: 'Outside this map',
        href: '',
        status: null,
        owner_ids: [],
        provider_ids: [],
        gaps: [],
        finding_count: 0,
        actions: [],
        cluster_of: cluster.type,
        count: cluster.count,
      })),
    ],
  };
}

export interface FleetMapPosition {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface FleetMapLayoutOptions {
  /** Rows shown per column before a "+N more" row. */
  cap?: number;
  /** Columns showing every row. */
  expanded?: ReadonlySet<FleetMapColumn>;
  /** Containers whose children are folded into the container row. */
  collapsed?: ReadonlySet<string>;
  /** Nodes that must stay visible past the cap (selection, search hits). */
  pinned?: ReadonlySet<string>;
  /**
   * The stage the layout is fitted into. With it the row pitch stretches so
   * the tallest column uses the height instead of the top third.
   */
  room?: { width: number; height: number; minScale?: number };
}

export interface FleetMapLayout {
  positions: Map<string, FleetMapPosition>;
  /** Top-level nodes that hold children, with their child ids. */
  groups: Map<string, string[]>;
  columns: { type: FleetMapColumn; x: number; count: number }[];
  more: { column: FleetMapColumn; hidden: number; x: number; y: number }[];
  /** Where a node is drawn: itself, its folded container, or nowhere. */
  anchor: (id: string) => string | undefined;
  width: number;
  height: number;
}

export const FLEET_MAP_NODE_WIDTH = 216;
export const FLEET_MAP_NODE_HEIGHT = 48;
export const FLEET_MAP_CHILD_HEIGHT = 36;
export const FLEET_MAP_COLUMN_GAP = 88;
/** Seven columns (flows present) still fit 1920 px at 100%. */
export const FLEET_MAP_DENSE_COLUMN_GAP = 52;
export const FLEET_MAP_ROW_GAP = 10;
/** The widest row pitch: room for a 26px bubble under its node. */
export const FLEET_MAP_MAX_ROW_GAP = 64;
/** Room under the plane for the legend and the zoom controls. */
const FLEET_MAP_FOOTER = 64;
export const FLEET_MAP_HEADER = 36;
export const FLEET_MAP_CAP = 12;
const CHILD_GAP = 6;
const CHILD_INSET = 10;

/**
 * A layered, top-aligned layout: one column per present kind, each column
 * starting directly under its header. Within a column rows are ordered by the
 * mean height of their neighbours in the columns already placed, so edges
 * cross as little as a single sweep manages. Containers stack their children
 * inside them. Past `cap` rows a column ends in a "+N more" row unless it is
 * expanded; pinned nodes always stay. Deterministic, so screenshots repeat.
 */
function layoutColumns(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  options: FleetMapLayoutOptions,
  rowGap: number,
  columnGap: number
): FleetMapLayout {
  const cap = options.cap ?? Number.POSITIVE_INFINITY;
  const parentOf = containers(graph);
  const children = new Map<string, FleetMapNode[]>();
  for (const node of graph.nodes) {
    const parent = parentOf.get(node.id);
    if (!parent) continue;
    children.set(parent, [...(children.get(parent) || []), node]);
  }
  const top = (id: string) => parentOf.get(id) ?? id;
  const adjacent = new Map<string, string[]>();
  for (const edge of graph.edges) {
    const a = top(edge.source);
    const b = top(edge.target);
    if (a === b) continue;
    adjacent.set(a, [...(adjacent.get(a) || []), edge.target]);
    adjacent.set(b, [...(adjacent.get(b) || []), edge.source]);
  }
  const centre = new Map<string, number>();
  const positions = new Map<string, FleetMapPosition>();
  const groups = new Map<string, string[]>();
  const columns: FleetMapLayout['columns'] = [];
  const more: FleetMapLayout['more'] = [];
  let height = FLEET_MAP_HEADER;
  for (const type of FLEET_MAP_COLUMNS) {
    const list = graph.nodes.filter(
      (node) => nodeColumn(node) === type && !parentOf.has(node.id)
    );
    if (!list.length) continue;
    const score = (node: FleetMapNode): number => {
      const ys = (adjacent.get(node.id) || [])
        .map((id) => centre.get(id))
        .filter((value): value is number => value !== undefined);
      return ys.length
        ? ys.reduce((sum, value) => sum + value, 0) / ys.length
        : Number.POSITIVE_INFINITY;
    };
    const scores = new Map(list.map((node) => [node.id, score(node)]));
    const ordered = [...list].sort(
      (a, b) =>
        Number(a.type === 'cluster') - Number(b.type === 'cluster') ||
        scores.get(a.id)! - scores.get(b.id)! ||
        Number(b.gaps.length > 0) - Number(a.gaps.length > 0) ||
        a.label.localeCompare(b.label)
    );
    const limit = options.expanded?.has(type) ? Number.POSITIVE_INFINITY : cap;
    const shown = ordered.filter(
      (node, index) =>
        index < limit ||
        node.type === 'cluster' ||
        options.pinned?.has(node.id) ||
        (children.get(node.id) || []).some((child) =>
          options.pinned?.has(child.id)
        )
    );
    const x = columns.length * (FLEET_MAP_NODE_WIDTH + columnGap);
    columns.push({ type, x, count: ordered.length });
    let y = FLEET_MAP_HEADER;
    for (const node of shown) {
      const kids = (children.get(node.id) || []).sort((a, b) =>
        a.label.localeCompare(b.label)
      );
      const open = kids.length && !options.collapsed?.has(node.id);
      const h = open
        ? FLEET_MAP_NODE_HEIGHT +
          kids.length * (FLEET_MAP_CHILD_HEIGHT + CHILD_GAP) +
          CHILD_INSET -
          CHILD_GAP
        : FLEET_MAP_NODE_HEIGHT;
      positions.set(node.id, { x, y, w: FLEET_MAP_NODE_WIDTH, h });
      centre.set(node.id, y + FLEET_MAP_NODE_HEIGHT / 2);
      if (kids.length)
        groups.set(
          node.id,
          kids.map((kid) => kid.id)
        );
      if (open)
        kids.forEach((kid, index) => {
          const ky =
            y +
            FLEET_MAP_NODE_HEIGHT +
            index * (FLEET_MAP_CHILD_HEIGHT + CHILD_GAP);
          positions.set(kid.id, {
            x: x + CHILD_INSET,
            y: ky,
            w: FLEET_MAP_NODE_WIDTH - 2 * CHILD_INSET,
            h: FLEET_MAP_CHILD_HEIGHT,
          });
          centre.set(kid.id, ky + FLEET_MAP_CHILD_HEIGHT / 2);
        });
      else
        for (const kid of kids)
          centre.set(kid.id, y + FLEET_MAP_NODE_HEIGHT / 2);
      y += h + rowGap;
    }
    const hidden = ordered.length - shown.length;
    if (hidden > 0) {
      more.push({ column: type, hidden, x, y });
      y += FLEET_MAP_CHILD_HEIGHT + rowGap;
    }
    height = Math.max(height, y - rowGap);
  }
  const anchor = (id: string): string | undefined =>
    positions.has(id)
      ? id
      : positions.has(parentOf.get(id) ?? '')
        ? parentOf.get(id)
        : undefined;
  const width = columns.length
    ? columns.length * (FLEET_MAP_NODE_WIDTH + columnGap) - columnGap
    : 0;
  return { positions, groups, columns, more, anchor, width, height };
}

/**
 * Columns, top-aligned under their headers (#1880). Given the stage, the
 * row pitch stretches (10px to 64px, the same in every column so rows stay
 * level) until the tallest column reaches the bottom of the fitted view: a
 * small fleet uses the height instead of the top third, and every node gets
 * a lane under it for its bubble. With a Flows column the column gap tightens
 * so seven columns still fit a 1920px screen at 100%.
 */
export function layoutGraph(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  options: FleetMapLayoutOptions = {}
): FleetMapLayout {
  const dense = graph.nodes.some((node) => nodeColumn(node) === 'flow');
  const columnGap = dense ? FLEET_MAP_DENSE_COLUMN_GAP : FLEET_MAP_COLUMN_GAP;
  const natural = layoutColumns(graph, options, FLEET_MAP_ROW_GAP, columnGap);
  const room = options.room;
  if (!room || !natural.width) return natural;
  const scale = Math.max(
    room.minScale ?? 0.6,
    Math.min(1, (room.width - 48) / natural.width)
  );
  const usable = (room.height - 16 - FLEET_MAP_FOOTER) / scale;
  // Rows per column (top-level boxes and "+N more" rows) and their height.
  const rows = new Map<number, { count: number; bottom: number }>();
  for (const column of natural.columns)
    rows.set(column.x, { count: 0, bottom: 0 });
  for (const [id, p] of natural.positions) {
    const row = rows.get(p.x);
    if (!row || natural.anchor(id) !== id || p.w !== FLEET_MAP_NODE_WIDTH)
      continue;
    row.count += 1;
    row.bottom = Math.max(row.bottom, p.y + p.h);
  }
  for (const more of natural.more) {
    const row = rows.get(more.x);
    if (!row) continue;
    row.count += 1;
    row.bottom = Math.max(row.bottom, more.y + FLEET_MAP_CHILD_HEIGHT);
  }
  let gap = FLEET_MAP_MAX_ROW_GAP;
  for (const { count, bottom } of rows.values())
    if (count > 1)
      gap = Math.min(gap, FLEET_MAP_ROW_GAP + (usable - bottom) / (count - 1));
  gap = Math.floor(gap);
  return gap > FLEET_MAP_ROW_GAP
    ? layoutColumns(graph, options, gap, columnGap)
    : natural;
}

export async function getFleetMap(
  signal?: AbortSignal,
  activeWithinMinutes: number | null = null,
  includeIdle = false
): Promise<FleetMapGraph> {
  const params = new URLSearchParams();
  if (activeWithinMinutes)
    params.set('active_within_minutes', String(activeWithinMinutes));
  if (includeIdle) params.set('include_idle', 'true');
  const url = `/api/v1/fleet/map${params.size ? `?${params}` : ''}`;
  const response = await fetchWithAuth(url, { signal });
  if (!response.ok)
    throw new Error(
      response.status === 403
        ? 'You do not have permission to view the fleet map.'
        : 'The fleet map could not be loaded. Retry to refresh it.'
    );
  return withClusters(await response.json());
}

export type FleetCodeAction = 'opened' | 'commented' | 'reviewed' | 'merged';
export type FleetCodeCounts = Record<FleetCodeAction, number> & {
  issues_opened?: number;
  issues_updated?: number;
  issues_commented?: number;
};

/** A flow with code activity in the window; the map places it. */
export interface FleetCodeFlow {
  id: string;
  resource_id: string;
  label: string;
  href: string;
  model_node_id: string | null;
  last_active_at: string | null;
  model_node_ids?: string[];
  run_count?: number;
}

export interface FleetCodeEvent {
  id: string;
  at: string;
  action: FleetCodeAction;
  node_id: string;
  owner_id?: string | null;
  pr?: string | null;
  url?: string | null;
  work_id?: string | null;
  source?: 'builtin' | 'cli' | 'github_mcp' | 'gitlab_mcp' | 'webhook';
}

/**
 * One issue or pull request touched in the window, newest first (bounded).
 * Contract: factory brief usage/contract-work.md; optional until the server
 * sends it.
 */
export interface FleetWorkItem {
  id?: string;
  href?: string | null;
  key: string;
  kind: 'issue' | 'pull_request';
  reference: string;
  repo: string | null;
  title: string | null;
  url: string | null;
  state: 'open' | 'closed' | 'merged' | null;
  last_action: string;
  last_at: string;
  review_state: string | null;
  labels: string[];
  related_issue: string | null;
  node_ids: string[];
}

/** Pull and merge requests per map node in the window (`/fleet/code-activity`). */
export interface FleetCodeActivity {
  active_within_minutes: number | null;
  since: string;
  truncated: boolean;
  counts: Record<string, FleetCodeCounts>;
  flows: FleetCodeFlow[];
  events: FleetCodeEvent[];
  /** Distinct work items in the window, so history shows on load. */
  work?: FleetWorkItem[];
  work_truncated?: boolean;
  relations?: FleetWorkRelation[];
  relations_truncated?: boolean;
}

export type FleetWorkRelationKind =
  'resolves' | 'mentions' | 'triggered' | 'produced' | 'touched';
export interface FleetWorkRelation {
  id: string;
  kind: FleetWorkRelationKind;
  source: string;
  target: string;
  actions?: string[];
  count?: number;
  at: string;
  provenance: 'tracker' | 'flow_run' | 'tool_call' | 'cli' | 'mcp';
}

/** Gateway totals for one model or provider node in the window. */
export interface FleetUsageTotals {
  request_count: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  estimated_cost: number | null;
  cost_is_estimate: true;
  currency: string | null;
  partial: boolean;
}

/** Optional so a partial response never turns an unmeasured metric into zero. */
export interface FleetNodeTotals extends FleetUsageTotals {
  tool_calls?: number;
  approvals_requested?: number;
  approvals_approved?: number;
  approvals_declined?: number;
  prs_opened?: number;
  prs_reviewed?: number;
  prs_merged?: number;
  review_comments?: number;
  issues_touched?: number;
  prs_touched?: number;
}

/** `GET /fleet/usage`: totals keyed by the fleet graph's own node ids. */
export interface FleetUsageResponse {
  active_within_minutes: number | null;
  since: string;
  until: string;
  truncated: boolean;
  partial: boolean;
  models: Record<string, FleetUsageTotals>;
  providers: Record<string, FleetUsageTotals>;
  agents?: Record<string, FleetNodeTotals>;
  people?: Record<string, FleetNodeTotals>;
  flows?: Record<string, FleetNodeTotals>;
  devices?: Record<string, FleetNodeTotals>;
  work?: Record<string, FleetNodeTotals>;
}

export async function getFleetUsage(
  signal?: AbortSignal,
  activeWithinMinutes: number | null = null
): Promise<FleetUsageResponse> {
  const url = activeWithinMinutes
    ? `/api/v1/fleet/usage?active_within_minutes=${activeWithinMinutes}`
    : '/api/v1/fleet/usage';
  const response = await fetchWithAuth(url, { signal });
  if (!response.ok) throw new Error('Usage totals could not be loaded.');
  return response.json();
}

export async function getFleetCodeActivity(
  signal?: AbortSignal,
  activeWithinMinutes: number | null = null
): Promise<FleetCodeActivity> {
  const url = activeWithinMinutes
    ? `/api/v1/fleet/code-activity?active_within_minutes=${activeWithinMinutes}`
    : '/api/v1/fleet/code-activity';
  const response = await fetchWithAuth(url, { signal });
  if (!response.ok) throw new Error('Code activity could not be loaded.');
  return response.json();
}

/**
 * Place the flows that did code work on the map: one node per flow, with a
 * line to its model when that model is on the map. Flows already on the map
 * are left as they are.
 */
export function withCodeFlows(
  graph: FleetMapGraph,
  activity: Pick<FleetCodeActivity, 'flows'> | null
): FleetMapGraph {
  if (!activity?.flows.length) return graph;
  const ids = new Set(graph.nodes.map((node) => node.id));
  const nodes = [...graph.nodes];
  const edges = [...graph.edges];
  for (const flow of activity.flows) {
    if (!ids.has(flow.id))
      nodes.push({
        id: flow.id,
        type: 'flow',
        resource_id: flow.resource_id,
        label: flow.label,
        detail: 'Flow',
        href: flow.href,
        status: null,
        owner_ids: [],
        provider_ids: [],
        gaps: [],
        finding_count: 0,
        actions: [],
        last_seen_at: flow.last_active_at,
        last_active_at: flow.last_active_at,
      });
    ids.add(flow.id);
    const models =
      flow.model_node_ids ?? (flow.model_node_id ? [flow.model_node_id] : []);
    for (const model of new Set(models)) {
      if (
        !ids.has(model) ||
        edges.some(
          (edge) =>
            edge.kind === 'uses_model' &&
            edge.source === flow.id &&
            edge.target === model
        )
      )
        continue;
      edges.push({
        id: `${flow.id}>uses_model>${model}`,
        source: flow.id,
        target: model,
        kind: 'uses_model',
        provenance: 'observed',
        impact_source: model,
        impact_target: flow.id,
        gaps: [],
      });
    }
  }
  return { ...graph, nodes, edges };
}
