/**
 * Where the map's event notes go. A note sits beside the node it is about
 * and never on another node's box or title: each note tries its sides in
 * order, steps along a side to clear a note already there, and takes the
 * first spot that stays on the stage and covers no other node. When every
 * side is taken it takes the nearest free spot; when the stage has none it
 * is dropped. Pure, so the render pass and the per-frame pass agree.
 */

export interface BubbleRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type BubbleSide = 'below' | 'above' | 'right' | 'left';
/** Where a note went: a side of its node, or the nearest free spot. */
export type BubbleSpot = BubbleSide | 'free';

export interface BubbleRequest {
  node: string;
  w: number;
  h: number;
}

export interface PlacedBubble extends BubbleRect {
  node: string;
  side: BubbleSpot;
}

export interface BubblePlacement {
  /** Screen rectangle of every drawn node, keyed by node id. */
  nodes: Map<string, BubbleRect>;
  /** The free part of the stage: notes stay inside it. */
  bounds: BubbleRect;
  /** Sides in order of preference. */
  sides: readonly BubbleSide[];
  /** Nodes a note on `node` may overlap (its own container, for example). */
  related?: (node: string, other: string) => boolean;
  /** Notes per node; later notes on the same node are dropped. */
  perNode?: number;
}

const GAP = 6;
const STEPS = 3;
const SCAN = 24;

const overlap = (a: BubbleRect, b: BubbleRect): number =>
  Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) *
  Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));

function candidate(
  side: BubbleSide,
  step: number,
  anchor: BubbleRect,
  w: number,
  h: number
): BubbleRect {
  const shift = step * (h + 4);
  switch (side) {
    case 'right':
      return {
        x: anchor.x + anchor.w + GAP,
        y: anchor.y + anchor.h / 2 - h / 2 + shift,
        w,
        h,
      };
    case 'left':
      return {
        x: anchor.x - w - GAP,
        y: anchor.y + anchor.h / 2 - h / 2 + shift,
        w,
        h,
      };
    case 'below':
      return { x: anchor.x, y: anchor.y + anchor.h + 4 + shift, w, h };
    case 'above':
      return { x: anchor.x, y: anchor.y - h - 4 - shift, w, h };
  }
}

export function placeBubbles(
  requests: readonly BubbleRequest[],
  options: BubblePlacement
): PlacedBubble[] {
  const { bounds, sides } = options;
  const related = options.related ?? (() => false);
  const perNode = options.perNode ?? 2;
  const placed: PlacedBubble[] = [];
  const counts = new Map<string, number>();
  for (const request of requests) {
    const anchor = options.nodes.get(request.node);
    const count = counts.get(request.node) ?? 0;
    if (!anchor || count >= perNode) continue;
    const w = Math.min(request.w, bounds.w);
    /** What a spot covers: other nodes, and notes already placed. */
    const cost = (rect: BubbleRect): { nodes: number; notes: number } => {
      if (
        rect.x < bounds.x ||
        rect.y < bounds.y ||
        rect.x + rect.w > bounds.x + bounds.w ||
        rect.y + rect.h > bounds.y + bounds.h
      )
        return { nodes: Number.POSITIVE_INFINITY, notes: 0 };
      let nodes = 0;
      for (const [id, box] of options.nodes)
        if (id !== request.node && !related(request.node, id))
          nodes += overlap(rect, box);
      let notes = 0;
      for (const other of placed) notes += overlap(rect, other);
      return { nodes, notes };
    };
    let best: { rect: BubbleRect; side: BubbleSpot; cost: number } | null =
      null;
    // A side steps outward only past notes, never past another node.
    search: for (const side of sides)
      for (let step = 0; step < STEPS; step++) {
        const rect = candidate(side, step, anchor, w, request.h);
        const c = cost(rect);
        const total = c.nodes + c.notes * 4;
        if (!best || total < best.cost) best = { rect, side, cost: total };
        if (total === 0) break search;
        if (c.nodes > 0) break;
      }
    if (!best || best.cost > 0) {
      // Every side is taken: the nearest free spot on the stage, scanned on
      // a coarse grid. With none, the note is not drawn; the node still
      // lights and its Recent list keeps the event.
      const cx = anchor.x + anchor.w / 2;
      const cy = anchor.y + anchor.h / 2;
      let near: { rect: BubbleRect; d: number } | null = null;
      for (let y = bounds.y; y + request.h <= bounds.y + bounds.h; y += SCAN)
        for (let x = bounds.x; x + w <= bounds.x + bounds.w; x += SCAN) {
          const rect = { x, y, w, h: request.h };
          const d = Math.hypot(x + w / 2 - cx, y + request.h / 2 - cy);
          if (near && d >= near.d) continue;
          const c = cost(rect);
          if (c.nodes === 0 && c.notes === 0 && !overlap(rect, anchor))
            near = { rect, d };
        }
      if (!near) continue;
      best = { rect: near.rect, side: 'free', cost: 0 };
    }
    counts.set(request.node, count + 1);
    placed.push({ ...best.rect, node: request.node, side: best.side });
  }
  return placed;
}

/** Corner notes look no lower than this share of the stage. */
const CORNER_REACH = 2 / 3;

/**
 * Where the corner notes go (the code activity toasts): stacked down from
 * the top right of the stage, each in the spot nearest that corner that
 * covers no node and no other note. A note with no such spot within the
 * upper two thirds of the stage is not drawn (null); the event stays in the
 * node's Recent list.
 *
 * Bounded, because it runs on every pan frame while a toast is up: rows are
 * scanned from the top and the scan stops at the first row that is already
 * further from the corner than the best spot found.
 */
export function placeCornerNotes(
  sizes: readonly { w: number; h: number }[],
  options: {
    nodes: Iterable<BubbleRect>;
    bounds: BubbleRect;
    /** Test seam: counts the rows the scan examined. */
    probe?: { rows: number };
  }
): (BubbleRect | null)[] {
  const { bounds, probe } = options;
  const nodes = [...options.nodes];
  const placed: BubbleRect[] = [];
  const corner = { x: bounds.x + bounds.w, y: bounds.y };
  const reach = bounds.y + bounds.h * CORNER_REACH;
  return sizes.map(({ w: width, h }) => {
    const w = Math.min(width, bounds.w);
    let best: { rect: BubbleRect; d: number } | null = null;
    for (
      let y = bounds.y;
      y + h <= bounds.y + bounds.h && y <= reach;
      y += GAP
    ) {
      // Every spot on this row and below is at least this far away.
      if (best && y - corner.y >= best.d) break;
      if (probe) probe.rows += 1;
      for (let x = bounds.x + bounds.w - w; x >= bounds.x; x -= SCAN) {
        const rect = { x, y, w, h };
        // Distance from the corner to the note's own top-right corner.
        const d = Math.hypot(corner.x - (x + w), y - corner.y);
        if (best && d >= best.d) break;
        if (nodes.some((box) => overlap(rect, box) > 0)) continue;
        if (placed.some((other) => overlap(rect, other) > 0)) continue;
        best = { rect, d };
        break;
      }
    }
    if (!best) return null;
    placed.push(best.rect);
    return best.rect;
  });
}
