import { expect, fixture, html } from '@open-wc/testing';
import './fleet-map';
import type { FleetMap } from './fleet-map';
import type { FleetCodeActivity } from '../fleet-map-api';
import {
  denseFleetActivity,
  denseFleetGraph,
} from '../test-helpers/fleet-map-dense';

/**
 * B-08: the Overview Inventory map with a dense fleet, at the two window
 * sizes the review used. The host sizes are the ones the Overview gives the
 * map at those windows (`--fleet-map-height` is the Inventory card's
 * clamp(420px, 100dvh - 360px, 720px)). Nothing may be drawn off the canvas, and no note
 * (a toast, a bubble or a count badge) may cover another node.
 */
const WINDOWS = [
  {
    name: '1440x900',
    viewport: { width: 1440, height: 900 },
    host: [1123, 540],
  },
  {
    name: '1920x1080',
    viewport: { width: 1920, height: 1080 },
    host: [1278, 720],
  },
] as const;

type Box = { left: number; top: number; right: number; bottom: number };

const box = (el: Element): Box => {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
};

const intersects = (a: Box, b: Box) =>
  a.left < b.right - 0.5 &&
  b.left < a.right - 0.5 &&
  a.top < b.bottom - 0.5 &&
  b.top < a.bottom - 0.5;

const inside = (inner: Box, outer: Box) =>
  inner.left >= outer.left - 1 &&
  inner.top >= outer.top - 1 &&
  inner.right <= outer.right + 1 &&
  inner.bottom <= outer.bottom + 1;

async function settle(el: FleetMap) {
  for (let i = 0; i < 4; i++) {
    await el.updateComplete;
    await new Promise((resolve) => requestAnimationFrame(resolve));
  }
}

async function overviewMap(
  arrangement: 'spatial' | 'columns',
  host: readonly [number, number],
  scope: string,
  activity: FleetCodeActivity | null = denseFleetActivity()
) {
  localStorage.setItem(
    `preloop:fleet-map:arrangement:${scope}:geometry-user`,
    arrangement
  );
  const el = await fixture<FleetMap>(
    html`<fleet-map
      class="inventory-map"
      wheel-mode="page"
      .graph=${denseFleetGraph()}
      instance-admin
      reduced-motion
      default-arrangement="spatial"
      positions-scope=${scope}
      user-id="geometry-user"
      style="width:${host[0]}px;--fleet-map-height:${host[1]}px"
    ></fleet-map>`
  );
  await settle(el);
  if (activity) {
    el.codeActivity = activity;
    await settle(el);
  }
  return el;
}

function offCanvas(el: FleetMap): string[] {
  const root = el.shadowRoot!;
  const stage = box(root.querySelector('.stage')!);
  return [
    ...root.querySelectorAll<HTMLElement>(
      '[data-node], .code-badges, .toast, .bubble'
    ),
  ]
    .filter((node) => node.getClientRects().length)
    .filter((node) => getComputedStyle(node).visibility !== 'hidden')
    .filter((node) => !inside(box(node), stage))
    .map((node) => node.dataset.node || node.className);
}

/** Notes over a node they do not belong to. */
function notesOnNodes(el: FleetMap): string[] {
  const root = el.shadowRoot!;
  const nodes = [
    ...root.querySelectorAll<HTMLElement>('.node:not(.child), .column-title'),
  ].filter((node) => node.dataset.node || node.matches('.column-title'));
  const notes = [
    ...root.querySelectorAll<HTMLElement>('.toast, .bubble, .code-badges'),
  ].filter((note) => getComputedStyle(note).visibility !== 'hidden');
  const hits: string[] = [];
  for (const note of notes) {
    const own = note.closest<HTMLElement>('[data-node]')?.dataset.node;
    const noteBox = box(note);
    for (const node of nodes) {
      if (node.dataset.node === own) continue;
      if (intersects(noteBox, box(node)))
        hits.push(
          `${note.className} on ${node.dataset.node ?? node.textContent}`
        );
    }
  }
  return hits;
}

describe('fleet-map on the Overview: a dense fleet fits its canvas (B-08)', () => {
  afterEach(async () => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
    const { setViewport } = await import('@web/test-runner-commands');
    await setViewport({ width: 1280, height: 800 });
  });

  for (const window of WINDOWS)
    for (const arrangement of ['spatial', 'columns'] as const)
      it(`draws every node and note inside the canvas, ${arrangement} at ${window.name}`, async () => {
        const { setViewport } = await import('@web/test-runner-commands');
        await setViewport(window.viewport);
        const el = await overviewMap(
          arrangement,
          window.host,
          `geometry-${arrangement}-${window.name}`
        );
        expect(
          el.shadowRoot!.querySelectorAll('[data-node]').length
        ).to.be.greaterThan(40);
        expect(offCanvas(el)).to.eql([]);
        expect(notesOnNodes(el)).to.eql([]);
      });

  for (const window of WINDOWS)
    for (const arrangement of ['spatial', 'columns'] as const)
      it(`keeps code activity toasts off every node, ${arrangement} at ${window.name}`, async () => {
        const { setViewport } = await import('@web/test-runner-commands');
        await setViewport(window.viewport);
        // The events as they arrive on load: the newest three are fresh, so
        // the map announces them, as it did on the reviewed account.
        const activity = denseFleetActivity();
        const now = Date.now();
        activity.events = activity.events.map((event, index) => ({
          ...event,
          at: new Date(now - (index + 1) * 5_000).toISOString(),
        }));
        const el = await overviewMap(
          arrangement,
          window.host,
          `geometry-toasts-${arrangement}-${window.name}`,
          null
        );
        el.codeActivity = activity;
        el.seedToasts(activity, now);
        await settle(el);
        const toasts = [
          ...el.shadowRoot!.querySelectorAll<HTMLElement>('.toast'),
        ];
        expect(toasts.length).to.equal(3);
        // Placed, not hidden: the corner had room for them off the nodes.
        expect(
          toasts.filter(
            (toast) => getComputedStyle(toast).visibility === 'visible'
          ).length
        ).to.be.at.least(1);
        expect(notesOnNodes(el)).to.eql([]);
        expect(offCanvas(el)).to.eql([]);
      });

  it('fits again when the code activity lands after the graph', async () => {
    const { setViewport } = await import('@web/test-runner-commands');
    await setViewport(WINDOWS[0].viewport);
    const el = await overviewMap('spatial', WINDOWS[0].host, 'late-code', null);
    expect(offCanvas(el)).to.eql([]);
    const before = el.shadowRoot!.querySelectorAll('[data-node]').length;
    el.codeActivity = denseFleetActivity();
    await settle(el);
    // The flows and work the activity adds are drawn, and drawn in view.
    expect(
      el.shadowRoot!.querySelectorAll('[data-node]').length
    ).to.be.greaterThan(before);
    expect(offCanvas(el)).to.eql([]);
  });

  it('fits again after the Show menu hides and restores a node type', async () => {
    const { setViewport } = await import('@web/test-runner-commands');
    await setViewport(WINDOWS[0].viewport);
    const el = await overviewMap('columns', WINDOWS[0].host, 'show-menu');
    const toggle = async (type: string) => {
      const box = el.shadowRoot!.querySelector<
        HTMLElement & { checked: boolean }
      >(`sl-menu-item[data-type="${type}"]`);
      expect(box, type).to.exist;
      box!.click();
      await settle(el);
    };
    await toggle('work');
    expect(offCanvas(el)).to.eql([]);
    await toggle('work');
    expect(offCanvas(el)).to.eql([]);
  });
});
