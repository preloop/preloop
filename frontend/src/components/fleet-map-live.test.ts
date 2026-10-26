import { expect, fixture, html } from '@open-wc/testing';
import { stub, useFakeTimers } from 'sinon';
import './fleet-map';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import type { FleetMap } from './fleet-map';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';
import type { FleetCodeActivity } from '../fleet-map-api';

const call = {
  type: 'model_gateway_request_started',
  payload: { managed_agent_id: 'codex', model_alias: 'gpt-6.1' },
};

const activity = (): FleetCodeActivity => ({
  active_within_minutes: 60,
  since: new Date().toISOString(),
  truncated: false,
  counts: {
    'agent:codex': { opened: 2, commented: 0, reviewed: 1, merged: 0 },
    'user:maya': { opened: 2, commented: 0, reviewed: 1, merged: 0 },
    'flow:reviewer': { opened: 0, commented: 3, reviewed: 4, merged: 1 },
  },
  flows: [
    {
      id: 'flow:reviewer',
      resource_id: 'reviewer',
      label: 'PR Reviewer',
      href: '/console/flows/reviewer',
      model_node_id: 'model:sonnet',
      last_active_at: new Date().toISOString(),
    },
  ],
  events: [],
});

/** The sample fleet plus Ana, her laptop and an onboarded agent: a new island. */
function withAna(): ReturnType<typeof sampleFleetMap> {
  const graph = sampleFleetMap();
  const by = (id: string) => graph.nodes.find((n) => n.id === id)!;
  const edge = graph.edges[0];
  return {
    ...graph,
    nodes: [
      ...graph.nodes,
      { ...by('user:maya'), id: 'user:ana', label: 'Ana' },
      { ...by('device:mbp'), id: 'device:ana', owner_ids: ['user:ana'] },
      { ...by('agent:codex'), id: 'agent:ana', owner_ids: ['user:ana'] },
    ],
    edges: [
      ...graph.edges,
      {
        ...edge,
        id: 'o-ana',
        source: 'user:ana',
        target: 'device:ana',
        kind: 'owns' as const,
      },
      {
        ...edge,
        id: 'h-ana',
        source: 'device:ana',
        target: 'agent:ana',
        kind: 'hosts' as const,
      },
      {
        ...edge,
        id: 'm-ana',
        source: 'agent:ana',
        target: 'model:gpt',
        kind: 'uses_model' as const,
      },
    ],
  };
}

type Glide = {
  camera?: unknown;
  tween?: {
    from: Map<string, { x: number; y: number }>;
    to: { positions: Map<string, { x: number; y: number }> };
  };
  spatialCache?: { layout: unknown };
};

const movedIn = (el: FleetMap): number => {
  const tween = (el as unknown as Glide).tween;
  if (!tween) return 0;
  return [...tween.to.positions].filter(([id, to]) => {
    const from = tween.from.get(id);
    return from && (from.x !== to.x || from.y !== to.y);
  }).length;
};

async function spatialMap(extra: Record<string, unknown> = {}) {
  const el = await fixture<FleetMap>(
    html`<fleet-map
      .graph=${sampleFleetMap()}
      instance-admin
      spatial-only
      user-id="u-live"
      positions-scope="live-test"
    ></fleet-map>`
  );
  Object.assign(el, extra);
  await el.updateComplete;
  return el;
}

describe('fleet-map live canvas', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  it('lights the agent for a gateway call, then the model that answered', async () => {
    const clock = useFakeTimers({ now: Date.now(), toFake: ['Date'] });
    try {
      const el = await spatialMap();
      el.recordActivity('gateway_activity', call);
      await el.updateComplete;
      expect(el.litNodes).to.deep.equal(['agent:codex']);
      const codex = el.shadowRoot!.querySelector('[data-node="agent:codex"]')!;
      expect(codex.classList.contains('speaking')).to.equal(true);
      expect(codex.classList.contains('speak-request')).to.equal(true);
      clock.tick(1000);
      el.recordActivity('gateway_activity', {
        ...call,
        type: 'model_gateway_call',
      });
      expect(el.litNodes).to.include('model:gpt');
      el.requestUpdate();
      await el.updateComplete;
      expect(
        el
          .shadowRoot!.querySelector('[data-node="model:gpt"]')!
          .classList.contains('speak-response')
      ).to.equal(true);
      // Nothing travels along the line.
      expect(el.shadowRoot!.querySelector('[data-particle]')).to.not.exist;
    } finally {
      clock.restore();
    }
  });

  it('lights the line without a label on the spatial canvas, with one in columns', async () => {
    const el = await spatialMap();
    el.recordActivity('gateway_activity', call);
    el.flushActivity();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('path.edge.active')).to.exist;
    expect(el.shadowRoot!.querySelector('.activity-label')).to.not.exist;
    const columns = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${sampleFleetMap()}
        instance-admin
        user-id="u-l"
      ></fleet-map>`
    );
    // A started request has nothing specific to say; a completed one names
    // the model that answered and the tokens it measured.
    columns.recordActivity('gateway_activity', call);
    columns.recordActivity('gateway_activity', {
      type: 'model_gateway_call',
      payload: {
        managed_agent_id: 'codex',
        model_alias: 'gpt-6.1',
        prompt_tokens: 4100,
        completion_tokens: 380,
      },
    });
    columns.flushActivity();
    await columns.updateComplete;
    expect(
      columns
        .shadowRoot!.querySelector('path.edge.active')!
        .getAttribute('data-activity')
    ).to.equal('gpt-6.1 · 4.1k in / 380 out');
  });

  it('lists recent events from the endpoint on load and says when counts are capped', async () => {
    const el = await spatialMap();
    const now = Date.now();
    const loaded = {
      ...activity(),
      truncated: true,
      events: [
        {
          id: 'e1',
          at: new Date(now - 30_000).toISOString(),
          action: 'merged' as const,
          node_id: 'flow:reviewer',
          pr: 'acme/api#7',
        },
        {
          id: 'e2',
          at: new Date(now - 600_000).toISOString(),
          action: 'opened' as const,
          node_id: 'agent:codex',
        },
      ],
    };
    el.codeActivity = loaded;
    el.seedToasts(loaded, now);
    await el.updateComplete;
    const toasts = [...el.shadowRoot!.querySelectorAll('[data-toast]')].map(
      (t) => t.textContent!.replace(/\s+/g, ' ').trim()
    );
    expect(toasts).to.deep.equal([
      'PR Reviewer contributed to merged acme/api#7',
    ]);
    expect(el.shadowRoot!.querySelector('[data-testid="code-truncated"]')).to
      .exist;
    // The same events on the next refresh are not listed again.
    el.seedToasts(loaded, now + 1000);
    await el.updateComplete;
    expect(el.shadowRoot!.querySelectorAll('[data-toast]').length).to.equal(1);
  });

  it('lights nodes in the columns arrangement too, for every viewer', async () => {
    const columns = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${sampleFleetMap()}
        user-id="u-columns"
      ></fleet-map>`
    );
    expect(columns.shadowRoot!.querySelector('.column-title')).to.exist;
    columns.recordActivity('gateway_activity', call);
    await columns.updateComplete;
    expect(
      columns
        .shadowRoot!.querySelector('[data-node="agent:codex"]')!
        .classList.contains('speaking')
    ).to.equal(true);
    columns.codeActivity = activity();
    await columns.updateComplete;
    expect(columns.shadowRoot!.querySelector('[data-code]')).to.exist;
  });

  it("drops a member's realtime event for an agent the map does not hold", async () => {
    const viewer = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${sampleFleetMap()}
        .activityWindow=${'any'}
        user-id="u-viewer"
      ></fleet-map>`
    );
    const hidden = {
      ...call,
      payload: { ...call.payload, managed_agent_id: 'not-shared' },
    };
    viewer.recordActivity('gateway_activity', hidden);
    viewer.recordActivity('code_activity', {
      topic: 'code_activity',
      payload: {
        action: 'opened',
        managed_agent_id: 'not-shared',
        url: 'https://github.com/example/private/pull/1',
      },
    });
    await viewer.updateComplete;
    expect(viewer.litNodes).to.deep.equal([]);
    expect(viewer.shadowRoot!.querySelector('[data-toast]')).to.not.exist;
    expect(viewer.shadowRoot!.textContent).to.not.contain('example/private');
    // An administrator's map still takes account-wide events as they come.
    const admin = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${sampleFleetMap()}
        instance-admin
        user-id="u-admin"
      ></fleet-map>`
    );
    admin.recordActivity('gateway_activity', call);
    expect(admin.litNodes).to.include('agent:codex');
  });

  it('lights nothing for a node folded past the column cap', async () => {
    const base = sampleFleetMap();
    const extra = Array.from({ length: 14 }, (_, i) => ({
      ...base.nodes.find((n) => n.id === 'agent:codex')!,
      id: `agent:x${i}`,
      resource_id: `x${i}`,
      label: `Extra ${String(i).padStart(2, '0')}`,
    }));
    const graph = {
      ...base,
      nodes: [...base.nodes, ...extra],
      edges: [
        ...base.edges,
        ...extra.flatMap((n) => [
          {
            ...base.edges[4],
            id: `h-${n.id}`,
            source: 'device:mbp',
            target: n.id,
          },
          {
            ...base.edges[8],
            id: `m-${n.id}`,
            source: n.id,
            target: 'model:gpt',
            kind: 'uses_model' as const,
          },
        ]),
      ],
    };
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${graph}
        instance-admin
        user-id="u-cap"
      ></fleet-map>`
    );
    const hidden = extra.find(
      (n) => !el.shadowRoot!.querySelector(`[data-node="${n.id}"]`)
    )!;
    expect(hidden, 'the cap folds some agents').to.exist;
    el.recordActivity('gateway_activity', {
      type: 'model_gateway_request_started',
      payload: { managed_agent_id: hidden.resource_id, model_alias: 'gpt-6.1' },
    });
    expect(el.litNodes).to.deep.equal([]);
  });

  it("listens to code activity on every viewer's live map", async () => {
    const subscribe = stub(unifiedWebSocketManager, 'subscribe').returns(
      () => {}
    );
    try {
      await fixture<FleetMap>(
        html`<fleet-map .graph=${sampleFleetMap()} live></fleet-map>`
      );
      expect(subscribe.args.map((a) => a[0])).to.include('code_activity');
      subscribe.resetHistory();
      await fixture<FleetMap>(
        html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
      );
      expect(subscribe.args.map((a) => a[0])).to.not.include('code_activity');
    } finally {
      subscribe.restore();
    }
  });

  it('caps how many nodes are lit at once', async () => {
    const el = await spatialMap();
    for (let i = 0; i < 60; i++)
      el.recordActivity('approvals', {
        type: 'approval_created',
        managed_agent_id: i % 2 ? 'codex' : 'claude',
        tool_name: `T${i}`,
      });
    expect(el.litNodes.sort()).to.deep.equal(['agent:claude', 'agent:codex']);
    expect(el.litNodes.length).to.be.at.most(16);
  });

  it('lights the same under reduced motion, since nothing moves', async () => {
    const el = await spatialMap({ reducedMotion: true });
    el.recordActivity('gateway_activity', call);
    el.flushActivity();
    await el.updateComplete;
    expect(el.litNodes).to.deep.equal(['agent:codex']);
    expect(el.shadowRoot!.querySelector('path.edge.active')).to.exist;
  });

  it('fades in nodes that join the window and out the ones that leave', async () => {
    const graph = sampleFleetMap();
    const el = await spatialMap({
      graph: {
        ...graph,
        nodes: graph.nodes.filter((n) => n.id !== 'agent:claude'),
      },
    });
    el.graph = {
      ...graph,
      nodes: graph.nodes.filter((n) => n.id !== 'agent:codex'),
    };
    await el.updateComplete;
    const entering = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="agent:claude"]'
    )!;
    expect(Number(entering.style.opacity)).to.be.lessThan(1);
    expect(el.shadowRoot!.querySelector('[data-leaving="agent:codex"]')).to
      .exist;
    await new Promise((resolve) => setTimeout(resolve, 450));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-leaving="agent:codex"]')).to.not
      .exist;
    expect(entering.style.opacity).to.equal('');
  });

  it('glides to the new centre of gravity when a person joins or leaves', async () => {
    const graph = sampleFleetMap();
    // Ana joins with a laptop and an onboarded agent: a new island.
    const by = (id: string) => graph.nodes.find((n) => n.id === id)!;
    const edge = graph.edges[0];
    const joined = {
      ...graph,
      nodes: [
        ...graph.nodes,
        { ...by('user:maya'), id: 'user:ana', label: 'Ana' },
        { ...by('device:mbp'), id: 'device:ana', owner_ids: ['user:ana'] },
        { ...by('agent:codex'), id: 'agent:ana', owner_ids: ['user:ana'] },
      ],
      edges: [
        ...graph.edges,
        {
          ...edge,
          id: 'o-ana',
          source: 'user:ana',
          target: 'device:ana',
          kind: 'owns' as const,
        },
        {
          ...edge,
          id: 'h-ana',
          source: 'device:ana',
          target: 'agent:ana',
          kind: 'hosts' as const,
        },
        {
          ...edge,
          id: 'm-ana',
          source: 'agent:ana',
          target: 'model:gpt',
          kind: 'uses_model' as const,
        },
      ],
    };
    const withoutSam = graph;
    const el = await spatialMap({ graph: withoutSam });
    type Internals = {
      camera?: unknown;
      tween?: unknown;
      gravityView(l: unknown): { x: number; y: number; s: number };
      lastLayout: unknown;
    };
    const inside = el as unknown as Internals;
    const plane = () =>
      el.shadowRoot!.querySelector<HTMLElement>('.plane')!.style.transform;
    el.graph = joined;
    await el.updateComplete;
    // Membership can re-pack an unowned layout without moving a camera that already fits.
    expect(inside.lastLayout).to.exist;
    const tween = inside.tween as {
      from: Map<string, { x: number; y: number }>;
      to: { positions: Map<string, { x: number; y: number }> };
    };
    expect(tween, 'nodes glide to their new places').to.exist;
    const moved = [...tween.to.positions].filter(([id, to]) => {
      const from = tween.from.get(id);
      return from && (from.x !== to.x || from.y !== to.y);
    });
    expect(moved.length, 'the islands are dealt round again').to.be.greaterThan(
      0
    );
    await new Promise((resolve) => setTimeout(resolve, 600));
    const target = inside.gravityView(inside.lastLayout);
    const scale = Number(/scale\(([\d.]+)\)/.exec(plane())![1]);
    expect(scale).to.be.closeTo(target.s, 1e-4);
    expect(inside.camera).to.equal(undefined);
  });

  for (const [how, move] of [
    [
      'a scroll',
      (stage: Element) =>
        stage.dispatchEvent(
          new WheelEvent('wheel', {
            deltaY: 40,
            bubbles: true,
            cancelable: true,
          })
        ),
    ],
    [
      'an arrow key',
      (stage: Element) =>
        stage.dispatchEvent(
          new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })
        ),
    ],
  ] as const)
    it(`stops following the fleet for a while after ${how}`, async () => {
      const el = await spatialMap();
      move(el.shadowRoot!.querySelector('.stage')!);
      el.graph = withAna();
      await el.updateComplete;
      expect(movedIn(el), 'the fleet still rearranges').to.be.greaterThan(0);
      expect((el as unknown as Glide).camera).to.equal(undefined);
    });

  it('a press on the stage without moving keeps following', async () => {
    const el = await spatialMap();
    const stage = el.shadowRoot!.querySelector<HTMLElement>('.stage')!;
    const at = {
      bubbles: true,
      composed: true,
      pointerId: 3,
      button: 0,
      clientX: 50,
      clientY: 50,
    };
    stage.dispatchEvent(new PointerEvent('pointerdown', at));
    stage.dispatchEvent(new PointerEvent('pointerup', at));
    el.graph = withAna();
    await el.updateComplete;
    expect(movedIn(el)).to.be.greaterThan(0);
  });

  it('settles in focus mode instead of re-laying out every frame', async () => {
    const el = await spatialMap();
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-node="agent:codex"]'
    )!.click();
    await el.updateComplete;
    const focus = [
      ...el.shadowRoot!.querySelectorAll<HTMLButtonElement>('button'),
    ].find(
      (b) =>
        b.hasAttribute('aria-pressed') &&
        /related|focus/i.test(b.textContent || '')
    )!;
    focus.click();
    await el.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 500));
    const layout = (el as unknown as Glide).spatialCache!.layout;
    el.requestUpdate();
    await el.updateComplete;
    el.requestUpdate();
    await el.updateComplete;
    expect((el as unknown as Glide).spatialCache!.layout).to.equal(layout);
    expect((el as unknown as Glide).tween).to.equal(undefined);
  });

  it('stores the whole visible arrangement when a machine is dragged', async () => {
    const el = await spatialMap();
    const target = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="agent:codex"]'
    )!;
    const stage = el.shadowRoot!.querySelector<HTMLElement>('.stage')!;
    const at = (x: number, y: number) => ({
      bubbles: true,
      composed: true,
      pointerId: 9,
      button: 0,
      clientX: x,
      clientY: y,
    });
    target.dispatchEvent(new PointerEvent('pointerdown', at(100, 100)));
    stage.dispatchEvent(new PointerEvent('pointermove', at(160, 140)));
    stage.dispatchEvent(new PointerEvent('pointerup', at(160, 140)));
    await el.updateComplete;
    const stored = JSON.parse(
      localStorage.getItem('preloop:fleet-map:positions:live-test:u-live')!
    );
    expect(Object.keys(stored)).to.have.length(
      el.shadowRoot!.querySelectorAll('[data-node]').length
    );
    el.graph = withAna();
    await el.updateComplete;
    expect(movedIn(el), 'owned positions stay put').to.equal(0);
    expect((el as unknown as Glide).camera).to.equal(undefined);
  });

  it('keeps nodes where they are when the graph refreshes', async () => {
    const graph = sampleFleetMap();
    const el = await spatialMap({
      graph: {
        ...graph,
        nodes: graph.nodes.filter((n) => n.id !== 'agent:claude'),
      },
    });
    const at = (id: string) =>
      el.shadowRoot!.querySelector<HTMLElement>(`[data-node="${id}"]`)!.style
        .left;
    const before = at('agent:codex');
    el.graph = { ...graph };
    await el.updateComplete;
    expect(at('agent:codex')).to.equal(before);
    // The arrival is placed beside what it links to, not across the map.
    const laptop = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="device:mbp"]'
    )!;
    const claude = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="agent:claude"]'
    )!;
    expect(
      Math.abs(parseFloat(claude.style.left) - parseFloat(laptop.style.left))
    ).to.be.lessThan(400);
  });

  it('enters and leaves instantly under reduced motion', async () => {
    const graph = sampleFleetMap();
    const el = await spatialMap({ reducedMotion: true });
    el.graph = {
      ...graph,
      nodes: graph.nodes.filter((n) => n.id !== 'agent:codex'),
    };
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-leaving]')).to.not.exist;
  });

  it('renders pull request counters on agents, people and flows', async () => {
    const el = await spatialMap({ codeActivity: activity() });
    const badges = (id: string) =>
      [
        ...el.shadowRoot!.querySelectorAll<HTMLElement>(
          `[data-code="${id}"] .code-badge`
        ),
      ].map((b) => `${b.dataset.action}:${b.textContent!.trim()}`);
    expect(badges('agent:codex')).to.deep.equal(['opened:2', 'reviewed:1']);
    expect(badges('user:maya')).to.deep.equal(['opened:2', 'reviewed:1']);
    // The flow is placed on the map from the code activity.
    expect(el.shadowRoot!.querySelector('[data-node="flow:reviewer"]')).to
      .exist;
    expect(badges('flow:reviewer')).to.deep.equal([
      'reviewed:4',
      'commented:3',
      'merged:1',
    ]);
    expect(
      el
        .shadowRoot!.querySelector('[data-code="agent:codex"]')!
        .getAttribute('aria-label')
    ).to.contain('2 opened');
  });

  it('credits merge contributions without claiming the flow was the merger', async () => {
    const el = await spatialMap({ codeActivity: activity() });
    el.recordActivity('code_activity', {
      type: 'code_activity',
      payload: { action: 'merged', flow_ids: ['reviewer'], pr: 'acme/api#42' },
    });
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[data-pip]')!.textContent!.trim()
    ).to.equal('contributed to merged acme/api#42');
    const toast = el.shadowRoot!.querySelector('[data-toast]')!;
    expect(toast.textContent).to.contain('PR Reviewer');
    expect(toast.textContent).to.contain('merged acme/api#42');
    expect(toast.textContent!.replace(/\s+/g, ' ')).to.contain(
      'PR Reviewer contributed to merged acme/api#42'
    );
    const merged = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-code="flow:reviewer"] [data-action="merged"]'
    )!;
    expect(merged.textContent!.trim()).to.equal('2');
  });
});
