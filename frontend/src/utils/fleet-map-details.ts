import type {
  FleetCodeActivity,
  FleetMapEdge,
  FleetMapGraph,
  FleetMapNode,
  FleetNodeTotals,
  FleetUsageResponse,
  FleetWorkRelationKind,
} from '../fleet-map-api';

const RELATIONS = new Set<FleetWorkRelationKind>([
  'resolves',
  'mentions',
  'triggered',
  'produced',
  'touched',
]);

export const isWorkRelation = (edge: FleetMapEdge): boolean =>
  RELATIONS.has(edge.kind as FleetWorkRelationKind);

/** No third-party avatar lookup. Failed or unsupported images use initials. */
export function personInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return [words[0], words.length > 1 ? words[words.length - 1] : '']
    .map((word) => Array.from(word || '')[0] || '')
    .join('')
    .toUpperCase();
}

export function avatarSource(value?: string | null): string {
  if (!value || value.length > 200 * 1024) return '';
  if (/^data:image\/[a-z0-9.+-]+(?:;[^,]*)?,/i.test(value)) return value;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? value
      : '';
  } catch {
    return '';
  }
}

/** Omitted buckets on older servers are unknown, not empty measurements. */
export function nodeUsage(
  usage: FleetUsageResponse | null,
  node: FleetMapNode,
  activity?: FleetCodeActivity | null
): FleetNodeTotals | undefined {
  // A live event may have first named work by URL, before history supplies
  // the stable server id used by the usage bucket.
  const work =
    node.type === 'work'
      ? activity?.work?.find(
          (item) =>
            item.kind === node.work_kind &&
            (item.key === node.resource_id ||
              (!!item.url && item.url === node.href))
        )
      : undefined;
  const id = work ? work.id || `work:${work.kind}:${work.key}` : node.id;
  const buckets = {
    user: usage?.people,
    agent: usage?.agents,
    flow: usage?.flows,
    device: usage?.devices,
    model: usage?.models,
    provider: usage?.providers,
    work: usage?.work,
    runner: undefined,
    provider_account: undefined,
    cluster: undefined,
  };
  return buckets[node.type]?.[id];
}

/** Join the optional server relations to canonical work identities already drawn. */
export function withWorkRelations(
  graph: FleetMapGraph,
  activity: FleetCodeActivity | null
): FleetMapGraph {
  if (!activity?.relations?.length) return graph;
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const aliases = new Map<string, string>();
  for (const item of activity.work ?? []) {
    const canonical = item.id || `work:${item.kind}:${item.key}`;
    const node = graph.nodes.find(
      (node) =>
        node.type === 'work' &&
        node.work_kind === item.kind &&
        (node.resource_id === item.key ||
          (!!item.url && node.href === item.url))
    );
    if (node) aliases.set(canonical, node.id);
  }
  const edges = new Map(
    graph.edges.map((edge) => [
      `${edge.kind}:${edge.source}:${edge.target}`,
      edge,
    ])
  );
  for (const relation of activity.relations.slice(0, 500)) {
    const source = aliases.get(relation.source) ?? relation.source;
    const target = aliases.get(relation.target) ?? relation.target;
    if (
      !RELATIONS.has(relation.kind) ||
      source === target ||
      !nodes.has(source) ||
      !nodes.has(target)
    )
      continue;
    const key = `${relation.kind}:${source}:${target}`;
    const previous = edges.get(key);
    edges.set(key, {
      id: key,
      source,
      target,
      kind: relation.kind,
      provenance: 'observed',
      impact_source: relation.kind === 'triggered' ? target : source,
      impact_target: relation.kind === 'triggered' ? source : target,
      gaps: [],
      actions: [
        ...new Set([...(previous?.actions ?? []), ...(relation.actions ?? [])]),
      ].slice(0, 6),
      count: Math.max(previous?.count ?? 0, relation.count ?? 0) || undefined,
      last_active_at: relation.at,
    });
  }
  return {
    ...graph,
    edges: [...edges.values()].filter(
      (edge) =>
        edge.kind !== 'mentions' ||
        !edges.has(`resolves:${edge.source}:${edge.target}`)
    ),
  };
}

/** Metadata only, bounded so an active line cannot become a paragraph. */
export function workRelationLabel(
  edge: FleetMapEdge,
  target?: FleetMapNode
): string {
  if (!isWorkRelation(edge)) return '';
  const reference =
    target?.work_reference || target?.resource_id.match(/[#!]\d+$/)?.[0] || '';
  const actions = (edge.actions ?? [])
    .map((action) =>
      action.replace(/^(issue|pull_request)_(opened|updated)$/, '$2')
    )
    .filter((action) =>
      [
        'opened',
        'commented',
        'reviewed',
        'merged',
        'labelled',
        'labeled',
        'updated',
        'closed',
        'reopened',
      ].includes(action)
    )
    .map((action) => (action === 'labeled' ? 'labelled' : action));
  const verb =
    edge.kind === 'touched' && actions.length
      ? actions.slice(0, 3).join(', ')
      : edge.kind === 'triggered'
        ? 'triggered by'
        : edge.kind;
  return `${verb}${reference ? ` ${reference}` : ''}`.slice(0, 72);
}
