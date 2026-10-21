import {
  FLEET_MAP_CHILD_HEIGHT,
  FLEET_MAP_NODE_HEIGHT,
  FLEET_MAP_NODE_WIDTH,
  containingProvider,
  providerChildOrder,
  type FleetMapGraph,
  type FleetMapNode,
  type FleetMapLayout,
  type FleetMapPosition,
} from '../fleet-map-api';
import { parseArrangement } from './fleet-map-words';

/**
 * Spatial layout for the Fleet Map: every resource is a free node placed by
 * a small force simulation (linked nodes pull together, all nodes push
 * apart), seeded from the column layout so it is deterministic. A position
 * the user dragged wins over the simulation and is stored per user.
 */

export type FleetMapArrangement = 'columns' | 'spatial';
export type FleetMapPoints = Record<
  string,
  { x: number; y: number; w?: number }
>;

const ARRANGEMENT_KEY = 'preloop:fleet-map:arrangement';
const POSITIONS_KEY = 'preloop:fleet-map:positions';

const keyFor = (base: string, user: string, scope: string) =>
  `${base}:${scope}:${user || 'anonymous'}`;

export function loadArrangement(
  user: string,
  scope = 'fleet',
  fallback: FleetMapArrangement = 'columns'
): FleetMapArrangement {
  try {
    const saved = localStorage.getItem(keyFor(ARRANGEMENT_KEY, user, scope));
    return parseArrangement(saved) ?? fallback;
  } catch {
    return fallback;
  }
}

export function saveArrangement(
  user: string,
  value: FleetMapArrangement,
  scope = 'fleet'
): void {
  try {
    localStorage.setItem(keyFor(ARRANGEMENT_KEY, user, scope), value);
  } catch {
    // A preference, not a requirement.
  }
}

export function loadPositions(user: string, scope = 'fleet'): FleetMapPoints {
  try {
    const raw = localStorage.getItem(keyFor(POSITIONS_KEY, user, scope));
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object') return {};
    const points: FleetMapPoints = {};
    const seen = JSON.parse(
      localStorage.getItem(`${keyFor(POSITIONS_KEY, user, scope)}:seen`) ?? '{}'
    );
    for (const [id, value] of Object.entries(parsed)) {
      if (
        typeof seen?.[id] === 'number' &&
        Date.now() - seen[id] > 7 * 86400_000
      )
        continue;
      const point = value as { x?: unknown; y?: unknown; w?: unknown };
      if (Number.isFinite(point?.x) && Number.isFinite(point?.y))
        points[id] = {
          x: Number(point.x),
          y: Number(point.y),
          ...(Number.isFinite(point.w) &&
          Number(point.w) >= 180 &&
          Number(point.w) <= 300
            ? { w: Number(point.w) }
            : {}),
        };
    }
    return points;
  } catch {
    return {};
  }
}

export function savePositions(
  user: string,
  points: FleetMapPoints,
  scope = 'fleet',
  activeIds = Object.keys(points)
): void {
  try {
    localStorage.setItem(
      keyFor(POSITIONS_KEY, user, scope),
      JSON.stringify(points)
    );
    const key = `${keyFor(POSITIONS_KEY, user, scope)}:seen`;
    const stored = JSON.parse(localStorage.getItem(key) ?? '{}');
    const seen: Record<string, number> = {};
    for (const id of Object.keys(points))
      seen[id] = typeof stored?.[id] === 'number' ? stored[id] : Date.now();
    for (const id of activeIds) seen[id] = Date.now();
    localStorage.setItem(key, JSON.stringify(seen));
  } catch {
    // A preference, not a requirement.
  }
}

export interface FleetMapViewport {
  width: number;
  height: number;
}

const GUTTER = 24;
/**
 * The smallest spatial scale that keeps 13px meta text at 11px, the console's
 * smallest size. Fit never goes below it; a narrow stage stacks shelves instead.
 */
export const LEGIBLE_SPATIAL_SCALE = 0.85;
/** The most a packed canvas stretches between blocks and between shelves. */
const MAX_BLOCK_GAP = 48;
const MAX_SHELF_GAP = 96;
const TYPE_ORDER = [
  'user',
  'device',
  'runner',
  'agent',
  'flow',
  'work',
  'cluster',
];

/**
 * Pack the canvas left to right in shelves (pass 4 order): the people
 * islands first (a person, their machines and the agents inside), then the
 * flows, then the shared core (one short column per provider, its accounts
 * and models inside it), then the work items. Every shelf is centred on the
 * stage; gaps stretch to use the height and the width only up to a short
 * line, so the picture stays packed. Stable ordering makes reset repeatable.
 */
export function clusterSeed(
  ids: string[],
  clusters: Map<string, string>,
  graph?: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  heightOf: (id: string) => number = () => FLEET_MAP_NODE_HEIGHT,
  viewport: FleetMapViewport = { width: 1200, height: 720 },
  nodeWidth = FLEET_MAP_NODE_WIDTH
): Map<string, { x: number; y: number }> {
  const byId = new Map((graph?.nodes ?? []).map((node) => [node.id, node]));
  const typeOf = (id: string) => byId.get(id)?.type ?? id.split(':')[0];
  const members = new Map<string, string[]>();
  for (const id of ids) {
    const key = clusters.get(id) ?? 'other';
    members.set(key, [...(members.get(key) ?? []), id]);
  }
  const seed = new Map<string, { x: number; y: number }>();
  const roomHeight = Math.max(320, viewport.height - GUTTER * 2);
  // A shelf is a column of blocks; a block is rows that stay 12px apart
  // (an island), and the space between blocks stretches.
  type Shelf = string[][];
  const shelves: Shelf[] = [];
  const blockHeight = (block: string[]) =>
    block.reduce((sum, id) => sum + heightOf(id), 0) +
    Math.max(0, block.length - 1) * 12;
  const shelfHeight = (shelf: Shelf) =>
    shelf.reduce((sum, block) => sum + blockHeight(block), 0) +
    Math.max(0, shelf.length - 1) * GUTTER;
  /** Which part of the picture a shelf belongs to: core, people or work. */
  const sections: string[] = [];
  /** Blocks in order, a new shelf when the stage height is used up. */
  const fill = (blocks: string[][], section: string) => {
    let shelf: Shelf = [];
    for (const block of blocks) {
      if (
        shelf.length &&
        shelfHeight(shelf) + GUTTER + blockHeight(block) > roomHeight
      ) {
        shelves.push(shelf);
        sections.push(section);
        shelf = [];
      }
      shelf.push(block);
    }
    if (shelf.length) {
      shelves.push(shelf);
      sections.push(section);
    }
  };
  // People islands, then anything without a person.
  const rank = (id: string) => TYPE_ORDER.indexOf(typeOf(id));
  const islandOrder = (key: string, list: string[]) =>
    key === 'other' ? 2 : list.some((id) => typeOf(id) === 'user') ? 0 : 1;
  const islands = [...members]
    .filter(([key]) => !['shared', 'work', 'flows'].includes(key))
    .sort(
      ([a, la], [b, lb]) =>
        islandOrder(a, la) - islandOrder(b, lb) || a.localeCompare(b)
    )
    .map(([, list]) =>
      [...list].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    );
  fill(islands, 'people');
  // Work cluster: the flows beside the issues and pull requests they act on.
  const flows = (members.get('flows') ?? []).sort();
  fill(
    flows.map((id) => [id]),
    'flows'
  );
  // Then the shared core: one column per provider, models inside it.
  const shared = members.get('shared') ?? [];
  const providerIds = new Set(shared.filter((id) => typeOf(id) === 'provider'));
  const providerOf = (id: string, depth = 0): string => {
    for (const edge of graph?.edges ?? [])
      if (
        depth < 3 &&
        edge.source === id &&
        (edge.kind === 'provided_by' || edge.kind === 'account_of_provider')
      )
        return providerIds.has(edge.target)
          ? edge.target
          : providerOf(edge.target, depth + 1);
    const facet = byId.get(id)?.provider_ids.find((p) => providerIds.has(p));
    return facet ?? '';
  };
  const core = new Map<string, string[]>();
  for (const id of [...providerIds].sort()) core.set(id, [id]);
  for (const id of shared.filter((id) => !providerIds.has(id)).sort()) {
    const key = providerOf(id);
    core.set(key, [...(core.get(key) ?? []), id]);
  }
  for (const [, column] of [...core].sort(
    ([a], [b]) => Number(!a) - Number(!b) || a.localeCompare(b)
  ))
    fill(
      column.map((id) => [id]),
      'core'
    );
  // Work items last.
  const work = (members.get('work') ?? []).sort();
  fill(
    work.map((id) => [id]),
    'work'
  );
  // A narrow stage (an Overview card) stacks neighbouring shelves while they
  // fit its height, so the work cluster is not pushed past the right edge at
  // a legible scale. Shelves of one section stack first (flows stay left of
  // their work while they can), the flattest pair first; order is kept.
  const widthOf = (count: number) => count * (nodeWidth + GUTTER) + GUTTER;
  for (const sameSection of [true, false])
    while (
      shelves.length > 1 &&
      widthOf(shelves.length) > viewport.width / LEGIBLE_SPATIAL_SCALE
    ) {
      let best = -1;
      let bestHeight = Infinity;
      for (let i = 0; i + 1 < shelves.length; i++) {
        if (sameSection && sections[i] !== sections[i + 1]) continue;
        const height =
          shelfHeight(shelves[i]) + GUTTER + shelfHeight(shelves[i + 1]);
        if (height <= roomHeight && height < bestHeight) {
          best = i;
          bestHeight = height;
        }
      }
      if (best < 0) break;
      shelves.splice(best, 2, [...shelves[best], ...shelves[best + 1]]);
      sections.splice(best, 2, sections[best]);
    }
  shelves.forEach((shelf, index) => {
    const natural = shelfHeight(shelf) - Math.max(0, shelf.length - 1) * GUTTER;
    const gap =
      shelf.length > 1
        ? Math.max(
            GUTTER,
            Math.min(
              MAX_BLOCK_GAP,
              (roomHeight * 0.92 - natural) / (shelf.length - 1)
            )
          )
        : 0;
    const height = natural + Math.max(0, shelf.length - 1) * gap;
    let y = GUTTER + Math.max(0, (roomHeight - height) / 2);
    const x = index * (nodeWidth + GUTTER);
    for (const block of shelf) {
      for (const id of block) {
        seed.set(id, { x, y });
        y += heightOf(id) + 12;
      }
      y += gap - 12;
    }
  });
  // Spread the shelves through the available width.
  const xs = [...new Set([...seed.values()].map((p) => p.x))].sort(
    (a, b) => a - b
  );
  if (xs.length > 1) {
    const natural = xs[xs.length - 1] - xs[0] + nodeWidth;
    const target = Math.max(natural, viewport.width - 2 * GUTTER);
    // Spread, but never further than a short line apart (pass 4: packed).
    const gap = Math.min(
      (target - nodeWidth) / (xs.length - 1),
      nodeWidth + MAX_SHELF_GAP
    );
    const placed = new Map(xs.map((x, i) => [x, GUTTER + i * gap]));
    for (const p of seed.values()) p.x = placed.get(p.x)!;
  }
  return seed;
}

/** Push overlapping boxes apart; pinned boxes never move. */
function separate(
  xs: Float64Array,
  ys: Float64Array,
  fixed: boolean[],
  hs?: number[],
  passes = 40,
  nodeWidth = FLEET_MAP_NODE_WIDTH
): void {
  const n = xs.length;
  const w = nodeWidth + 16;
  const h = (i: number) => (hs ? hs[i] : FLEET_MAP_NODE_HEIGHT);
  for (let pass = 0; pass < passes; pass++) {
    let moved = false;
    for (let i = 0; i < n; i++)
      for (let j = i + 1; j < n; j++) {
        const ox = w - Math.abs(xs[i] - xs[j]);
        // Boxes may differ in height: the upper one's height decides.
        const oy =
          (ys[i] <= ys[j] ? h(i) : h(j)) + 12 - Math.abs(ys[i] - ys[j]);
        if (ox <= 0 || oy <= 0 || (fixed[i] && fixed[j])) continue;
        moved = true;
        // Resolve along the shallower axis.
        const vertical = oy < ox;
        const sign = vertical
          ? ys[i] <= ys[j]
            ? -1
            : 1
          : xs[i] <= xs[j]
            ? -1
            : 1;
        const amount = (vertical ? oy : ox) / (fixed[i] || fixed[j] ? 1 : 2);
        if (!fixed[i]) {
          if (vertical) ys[i] += sign * amount;
          else xs[i] += sign * amount;
        }
        if (!fixed[j]) {
          if (vertical) ys[j] -= sign * amount;
          else xs[j] -= sign * amount;
        }
      }
    if (!moved) break;
  }
}

/**
 * Where a node that joins an existing picture starts: beside what it links
 * to that is already placed, else beside its group, so a new agent appears
 * next to its laptop instead of on the far side of the map.
 */
function arrivalSeed(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  ids: string[],
  pinned: FleetMapPoints,
  clusters?: Map<string, string>
): Map<string, { x: number; y: number }> {
  const out = new Map<string, { x: number; y: number }>();
  const placed = ids.filter((id) => id in pinned);
  if (!placed.length || placed.length === ids.length) return out;
  const centre = (points: { x: number; y: number }[]) => ({
    x: points.reduce((sum, p) => sum + p.x, 0) / points.length,
    y: points.reduce((sum, p) => sum + p.y, 0) / points.length,
  });
  ids.forEach((id, i) => {
    if (id in pinned) return;
    const type = graph.nodes.find((node) => node.id === id)?.type;
    if (type === 'model') {
      const models = graph.nodes
        .filter((node) => node.type === 'model' && node.id in pinned)
        .map((node) => pinned[node.id]);
      if (models.length) {
        out.set(id, {
          x: models[0].x,
          y:
            Math.max(...models.map((p) => p.y)) +
            FLEET_MAP_NODE_HEIGHT * 1.25 +
            24,
        });
        return;
      }
    }
    const linked = graph.edges
      .filter((edge) => edge.source === id || edge.target === id)
      .map((edge) => (edge.source === id ? edge.target : edge.source))
      .filter((other) => other in pinned)
      .map((other) => pinned[other]);
    const group = clusters?.get(id);
    const mates = group
      ? placed.filter((other) => clusters?.get(other) === group)
      : [];
    const anchor = linked.length
      ? centre(linked)
      : mates.length
        ? centre(mates.map((other) => pinned[other]))
        : null;
    if (!anchor) return;
    // A fixed, per-node offset keeps arrivals deterministic and apart.
    const angle = ((i * 137.5) % 360) * (Math.PI / 180);
    out.set(id, {
      x: anchor.x + Math.cos(angle) * 140,
      y: anchor.y + Math.sin(angle) * 90,
    });
  });
  return out;
}

/**
 * Place every node of `graph`. `seed` gives starting points (the column
 * layout); `pinned` points are fixed and never moved by the simulation.
 * With `clusters` (node id to group key) nodes start in their group and are
 * held near it, so a person, their machines and agents read as one place.
 */
/** Space a contained node takes inside its machine. */
const CHILD_ROW = FLEET_MAP_CHILD_HEIGHT + 6;
const CHILD_INSET = 12;

/**
 * Which agents and runners sit inside which machine: a `hosts` line from a
 * device to something on the map. Anything hosted twice stays with the
 * first machine.
 */
export function hostedBy(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>
): Map<string, string[]> {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const kids = new Map<string, string[]>();
  const taken = new Set<string>();
  for (const edge of graph.edges) {
    if (edge.kind !== 'hosts' || taken.has(edge.target)) continue;
    const host = byId.get(edge.source);
    const kid = byId.get(edge.target);
    if (
      host?.type !== 'device' ||
      !kid ||
      !['agent', 'runner'].includes(kid.type)
    )
      continue;
    taken.add(edge.target);
    kids.set(edge.source, [...(kids.get(edge.source) ?? []), edge.target]);
  }
  for (const list of kids.values()) list.sort();
  return kids;
}

/**
 * What each provider box holds on the canvas: its accounts, then its models
 * (pass 4: models sit inside their provider while Providers are shown).
 */
export function providerContents(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>
): Map<string, string[]> {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const kids = new Map<string, FleetMapNode[]>();
  for (const node of graph.nodes) {
    const provider = containingProvider(node, graph, byId);
    if (provider) kids.set(provider, [...(kids.get(provider) ?? []), node]);
  }
  return new Map(
    [...kids].map(([provider, list]) => [
      provider,
      list.sort(providerChildOrder).map((node) => node.id),
    ])
  );
}

/** Height of a machine holding `count` agents and runners. */
export const containerHeight = (count: number): number =>
  count ? FLEET_MAP_NODE_HEIGHT + count * CHILD_ROW + 6 : FLEET_MAP_NODE_HEIGHT;

export function spatialLayout(
  graph: Pick<FleetMapGraph, 'nodes' | 'edges'>,
  seed: Map<string, FleetMapPosition>,
  pinned: FleetMapPoints = {},
  clusters?: Map<string, string>,
  collapsed: Set<string> = new Set(),
  viewport: FleetMapViewport = { width: 1200, height: 720 }
): FleetMapLayout {
  // On a grouped map a machine is a box holding its agents: only the boxes
  // are placed, and what they hold is laid out inside them.
  const contained = clusters ? hostedBy(graph) : new Map<string, string[]>();
  if (clusters)
    for (const [provider, list] of providerContents(graph))
      contained.set(provider, list);
  for (const node of graph.nodes)
    if (
      node.type === 'work' &&
      node.parent_id &&
      graph.nodes.some((n) => n.id === node.parent_id)
    )
      contained.set(node.parent_id, [
        ...(contained.get(node.parent_id) ?? []),
        node.id,
      ]);
  const kidOf = new Map<string, string>();
  for (const [host, list] of contained)
    for (const kid of list) kidOf.set(kid, host);
  const baseHeightOf = (id: string) =>
    collapsed.has(id)
      ? FLEET_MAP_NODE_HEIGHT
      : containerHeight(contained.get(id)?.length ?? 0);
  const ids = graph.nodes.map((node) => node.id).filter((id) => !kidOf.has(id));
  // Choose the largest cards that fit the measured shelves without shrinking
  // below their design size. Crowded fleets retain the base width and pan.
  let nodeWidth = FLEET_MAP_NODE_WIDTH;
  if (clusters && !Object.keys(pinned).length) {
    for (
      let candidate = FLEET_MAP_NODE_WIDTH;
      candidate <= 300;
      candidate += 12
    ) {
      const factor = Math.min(1.25, candidate / FLEET_MAP_NODE_WIDTH);
      const trial = clusterSeed(
        ids,
        clusters,
        graph,
        (id) => baseHeightOf(id) * factor,
        viewport,
        candidate
      );
      const boxes = ids.map((id) => ({
        ...trial.get(id)!,
        h: baseHeightOf(id) * factor,
      }));
      const w =
        Math.max(...boxes.map((b) => b.x + candidate)) -
        Math.min(...boxes.map((b) => b.x));
      const h =
        Math.max(...boxes.map((b) => b.y + b.h)) -
        Math.min(...boxes.map((b) => b.y));
      if (w <= viewport.width - 48 && h <= viewport.height - 48)
        nodeWidth = candidate;
    }
  } else if (clusters) {
    nodeWidth = Math.max(
      216,
      ...Object.values(pinned).map((point) => point.w ?? 216)
    );
  }
  const factor = Math.min(1.25, nodeWidth / FLEET_MAP_NODE_WIDTH);
  const heightOf = (id: string) => baseHeightOf(id) * factor;
  const index = new Map(ids.map((id, i) => [id, i]));
  const n = ids.length;
  const xs = new Float64Array(n);
  const ys = new Float64Array(n);
  const grouped = clusters
    ? clusterSeed(ids, clusters, graph, heightOf, viewport, nodeWidth)
    : null;
  const hs = ids.map(heightOf);
  const near = arrivalSeed(graph, ids, pinned, clusters);
  ids.forEach((id, i) => {
    const start = pinned[id] ??
      near.get(id) ??
      grouped?.get(id) ??
      seed.get(id) ?? { x: (i % 8) * 260, y: i * 30 };
    xs[i] = start.x;
    ys[i] = start.y;
  });
  // A growing machine may push the immediately following boxes in its own
  // island. Other owned islands and the camera stay exactly where they were.
  for (const [host, kids] of contained) {
    const i = index.get(host);
    if (
      i === undefined ||
      !(host in pinned) ||
      !kids.some((kid) => !(kid in pinned))
    )
      continue;
    let bottom = ys[i] + hs[i];
    const below = ids
      .map((id, j) => ({ id, j }))
      .filter(
        ({ id, j }) =>
          j !== i &&
          id in pinned &&
          clusters?.get(id) === clusters?.get(host) &&
          Math.abs(xs[j] - xs[i]) < nodeWidth &&
          ys[j] > ys[i]
      )
      .sort((a, b) => ys[a.j] - ys[b.j]);
    for (const { j } of below) {
      if (ys[j] >= bottom + 12) break;
      ys[j] = bottom + 12;
      bottom = ys[j] + hs[j];
    }
  }
  const fixed = ids.map((id) => id in pinned);
  const links = graph.edges
    .map((edge) => [index.get(edge.source), index.get(edge.target)])
    .filter(
      (pair): pair is [number, number] =>
        pair[0] !== undefined && pair[1] !== undefined && pair[0] !== pair[1]
    );
  const ideal = 300;
  // Grouped maps are laid out by island; only overlaps are resolved.
  if (clusters) separate(xs, ys, fixed, hs, 40, nodeWidth);
  const iterations =
    fixed.every(Boolean) || clusters ? 0 : n > 250 ? 60 : n > 80 ? 120 : 220;
  let heat = 60;
  const dx = new Float64Array(n);
  const dy = new Float64Array(n);
  for (let step = 0; step < iterations; step++) {
    dx.fill(0);
    dy.fill(0);
    for (let i = 0; i < n; i++)
      for (let j = i + 1; j < n; j++) {
        let ex = xs[i] - xs[j];
        let ey = ys[i] - ys[j];
        let d2 = ex * ex + ey * ey;
        if (d2 < 1) {
          ex = ((i * 7919) % 13) - 6 || 1;
          ey = ((j * 104729) % 11) - 5 || 1;
          d2 = ex * ex + ey * ey;
        }
        const force = (ideal * ideal) / d2;
        dx[i] += ex * force;
        dy[i] += ey * force;
        dx[j] -= ex * force;
        dy[j] -= ey * force;
      }
    for (const [a, b] of links) {
      const ex = xs[a] - xs[b];
      const ey = ys[a] - ys[b];
      const d = Math.sqrt(ex * ex + ey * ey) || 1;
      const pull = d / ideal;
      dx[a] -= ex * pull;
      dy[a] -= ey * pull;
      dx[b] += ex * pull;
      dy[b] += ey * pull;
    }
    for (let i = 0; i < n; i++) {
      if (fixed[i]) continue;
      const d = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1;
      const move = Math.min(d, heat);
      xs[i] += (dx[i] / d) * move;
      ys[i] += (dy[i] / d) * move;
    }
    heat = Math.max(2, heat * 0.97);
  }
  // Shift into positive space with a margin, unless something is pinned:
  // a pinned node must stay exactly where the user dropped it.
  let minX = 0;
  let minY = 0;
  if (!Object.keys(pinned).some((id) => index.has(id))) {
    minX = Math.min(...xs) - 24;
    minY = Math.min(...ys) - 24;
  } else {
    // Arrivals beside a pinned picture never land off its top or left edge,
    // where the stage would clip them.
    for (let i = 0; i < n; i++) {
      if (fixed[i]) continue;
      xs[i] = Math.max(24, xs[i]);
      ys[i] = Math.max(24, ys[i]);
    }
    if (clusters) separate(xs, ys, fixed, hs, 40, nodeWidth);
  }
  const positions = new Map<string, FleetMapPosition>();
  const groups = new Map<string, string[]>();
  let width = 0;
  let height = 0;
  ids.forEach((id, i) => {
    const x = Math.round(xs[i] - minX);
    const y = Math.round(ys[i] - minY);
    const h = hs[i];
    positions.set(id, { x, y, w: nodeWidth, h });
    width = Math.max(width, x + nodeWidth + 24);
    height = Math.max(height, y + h + 24);
    const kids = contained.get(id);
    if (!kids) return;
    // A folded machine still owns its group, so it keeps its fold control.
    groups.set(id, kids);
    if (collapsed.has(id)) return;
    kids.forEach((kid, k) =>
      positions.set(kid, {
        x: x + CHILD_INSET,
        y: y + (FLEET_MAP_NODE_HEIGHT + k * CHILD_ROW) * factor,
        w: nodeWidth - 2 * CHILD_INSET,
        h: FLEET_MAP_CHILD_HEIGHT * factor,
      })
    );
  });
  return {
    positions,
    groups,
    columns: [],
    more: [],
    // A folded machine stands in for what it holds.
    anchor: (id) =>
      positions.has(id)
        ? id
        : kidOf.has(id) && positions.has(kidOf.get(id)!)
          ? kidOf.get(id)
          : undefined,
    width,
    height,
  };
}
