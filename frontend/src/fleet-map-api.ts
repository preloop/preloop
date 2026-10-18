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
  | 'provided_by';
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
}
export interface FleetMapEdge {
  id: string;
  source: string;
  target: string;
  kind: FleetMapEdgeType;
  provenance: 'configured' | 'observed';
  impact_source: string;
  impact_target: string;
  gaps: FleetMapGapCode[];
}
export interface FleetMapGraph {
  schema_version: 1;
  nodes: FleetMapNode[];
  edges: FleetMapEdge[];
  truncated: boolean;
  limit: number;
  clusters: { type: FleetMapResourceType; count: number }[];
  unavailable: string[];
}

/** Columns left to right: a person, then what they own, then what it calls. */
export const FLEET_MAP_COLUMNS: readonly Exclude<
  FleetMapNodeType,
  'cluster'
>[] = [
  'user',
  'device',
  'runner',
  'agent',
  'model',
  'provider_account',
  'provider',
];

export const FLEET_MAP_COLUMN_LABEL: Record<
  Exclude<FleetMapNodeType, 'cluster'>,
  string
> = {
  user: 'People',
  device: 'Devices',
  runner: 'Runners',
  agent: 'Agents',
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
};

export const nodeColumn = (
  node: FleetMapNode
): Exclude<FleetMapNodeType, 'cluster'> =>
  node.type === 'cluster' ? node.cluster_of || 'agent' : node.type;

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

export interface FleetMapFilters {
  types: ReadonlySet<FleetMapNodeType>;
  owner: string;
  provider: string;
}

/** Facets use permission-filtered server associations, not an undirected walk. */
export function filterGraph(
  graph: FleetMapGraph,
  filters: FleetMapFilters
): FleetMapGraph {
  const nodes = graph.nodes.filter(
    (node) =>
      filters.types.has(nodeColumn(node)) &&
      (!filters.owner ||
        node.id === filters.owner ||
        node.owner_ids.includes(filters.owner)) &&
      (!filters.provider ||
        node.id === filters.provider ||
        node.provider_ids.includes(filters.provider))
  );
  const ids = new Set(nodes.map((node) => node.id));
  return {
    ...graph,
    nodes,
    edges: graph.edges.filter(
      (edge) => ids.has(edge.source) && ids.has(edge.target)
    ),
  };
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
}

export const FLEET_MAP_NODE_WIDTH = 176;
export const FLEET_MAP_NODE_HEIGHT = 52;
export const FLEET_MAP_COLUMN_GAP = 40;
export const FLEET_MAP_ROW_GAP = 14;
export const FLEET_MAP_HEADER = 40;

/**
 * A layered layout: one column per type, empty columns collapsed. Within a
 * column nodes are ordered by the mean row of their neighbours in the
 * column before, so edges between neighbouring columns cross as little as
 * a single sweep manages. Deterministic, so a screenshot is reproducible.
 */
export function layoutGraph(graph: Pick<FleetMapGraph, 'nodes' | 'edges'>): {
  positions: Map<string, FleetMapPosition>;
  columns: { type: Exclude<FleetMapNodeType, 'cluster'>; x: number }[];
  width: number;
  height: number;
} {
  const groups = new Map<string, FleetMapNode[]>();
  for (const node of graph.nodes) {
    const list = groups.get(nodeColumn(node)) || [];
    list.push(node);
    groups.set(nodeColumn(node), list);
  }
  const adjacent = new Map<string, string[]>();
  for (const edge of graph.edges) {
    adjacent.set(edge.source, [
      ...(adjacent.get(edge.source) || []),
      edge.target,
    ]);
    adjacent.set(edge.target, [
      ...(adjacent.get(edge.target) || []),
      edge.source,
    ]);
  }
  const row = new Map<string, number>();
  const columnSize = new Map<string, number>();
  const positions = new Map<string, FleetMapPosition>();
  const columns: { type: Exclude<FleetMapNodeType, 'cluster'>; x: number }[] =
    [];
  let tallest = 0;
  for (const type of FLEET_MAP_COLUMNS) {
    const list = groups.get(type);
    if (!list?.length) continue;
    const score = (node: FleetMapNode): number => {
      const rows = (adjacent.get(node.id) || [])
        .map((id) => row.get(id))
        .filter((value): value is number => value !== undefined);
      return rows.length
        ? rows.reduce((sum, value) => sum + value, 0) / rows.length
        : Number.POSITIVE_INFINITY;
    };
    const ordered = [...list].sort(
      (a, b) =>
        score(a) - score(b) ||
        Number(a.type === 'cluster') - Number(b.type === 'cluster') ||
        a.label.localeCompare(b.label)
    );
    const x = columns.length * (FLEET_MAP_NODE_WIDTH + FLEET_MAP_COLUMN_GAP);
    columns.push({ type, x });
    ordered.forEach((node, index) => {
      row.set(node.id, index);
      columnSize.set(node.id, ordered.length);
      positions.set(node.id, {
        x,
        y:
          FLEET_MAP_HEADER +
          index * (FLEET_MAP_NODE_HEIGHT + FLEET_MAP_ROW_GAP),
      });
    });
    tallest = Math.max(tallest, ordered.length);
  }
  // Centre each column on the tallest one, so a column of two providers
  // sits beside the middle of the agents it serves, not above them.
  const step = FLEET_MAP_NODE_HEIGHT + FLEET_MAP_ROW_GAP;
  for (const [id, position] of positions) {
    const size = columnSize.get(id) ?? tallest;
    position.y += ((tallest - size) * step) / 2;
  }
  const width = columns.length
    ? columns.length * (FLEET_MAP_NODE_WIDTH + FLEET_MAP_COLUMN_GAP) -
      FLEET_MAP_COLUMN_GAP
    : 0;
  const height =
    FLEET_MAP_HEADER +
    Math.max(
      0,
      tallest * (FLEET_MAP_NODE_HEIGHT + FLEET_MAP_ROW_GAP) - FLEET_MAP_ROW_GAP
    );
  return { positions, columns, width, height };
}

export async function getFleetMap(
  signal?: AbortSignal
): Promise<FleetMapGraph> {
  const response = await fetchWithAuth('/api/v1/fleet/map', { signal });
  if (!response.ok)
    throw new Error(
      response.status === 403
        ? 'You do not have permission to view the fleet map.'
        : 'The fleet map could not be loaded. Retry to refresh it.'
    );
  return withClusters(await response.json());
}
