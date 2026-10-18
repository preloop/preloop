import { expect, fixture, html } from '@open-wc/testing';
import { stub, useFakeTimers } from 'sinon';
import './fleet-map';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import type { FleetMap } from './fleet-map';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';
import type { FleetCodeActivity } from '../fleet-map-api';

const call = {
  type: 'model_gateway_call',
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
    columns.recordActivity('gateway_activity', call);
    columns.flushActivity();
    await columns.updateComplete;
    expect(
      columns.shadowRoot!.querySelector('.activity-label')!.textContent
    ).to.contain('model request');
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
    expect(toasts).to.deep.equal(['PR Reviewer merged acme/api#7']);
    expect(el.shadowRoot!.querySelector('[data-testid="code-truncated"]')).to
      .exist;
    // The same events on the next refresh are not listed again.
    el.seedToasts(loaded, now + 1000);
    await el.updateComplete;
    expect(el.shadowRoot!.querySelectorAll('[data-toast]').length).to.equal(1);
  });

  it('lights nodes in the columns arrangement too, for admins only', async () => {
    const columns = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${sampleFleetMap()}
        instance-admin
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
    const viewer = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${sampleFleetMap()}
        user-id="u-viewer"
      ></fleet-map>`
    );
    viewer.recordActivity('gateway_activity', call);
    viewer.codeActivity = activity();
    await viewer.updateComplete;
    expect(viewer.litNodes).to.deep.equal([]);
    expect(viewer.shadowRoot!.querySelector('.speaking')).to.not.exist;
    expect(viewer.shadowRoot!.querySelector('[data-code]')).to.not.exist;
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
      type: 'model_gateway_call',
      payload: { managed_agent_id: hidden.resource_id, model_alias: 'gpt-6.1' },
    });
    expect(el.litNodes).to.deep.equal([]);
  });

  it("listens to code activity only on an administrator's live map", async () => {
    const subscribe = stub(unifiedWebSocketManager, 'subscribe').returns(
      () => {}
    );
    try {
      await fixture<FleetMap>(
        html`<fleet-map .graph=${sampleFleetMap()} live></fleet-map>`
      );
      expect(subscribe.args.map((a) => a[0])).to.not.include('code_activity');
      subscribe.resetHistory();
      const admin = await fixture<FleetMap>(
        html`<fleet-map .graph=${sampleFleetMap()} live></fleet-map>`
      );
      admin.instanceAdmin = true;
      await admin.updateComplete;
      expect(subscribe.args.map((a) => a[0])).to.include('code_activity');
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
    expect(inside.camera, 'the view starts gliding').to.exist;
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

  it('stops following the fleet for a while after a pan by hand', async () => {
    const graph = sampleFleetMap();
    const el = await spatialMap({
      graph: {
        ...graph,
        nodes: graph.nodes.filter((n) => n.id !== 'user:sam'),
      },
    });
    el.shadowRoot!.querySelector('.stage')!.dispatchEvent(
      new WheelEvent('wheel', { deltaY: 40, bubbles: true, cancelable: true })
    );
    el.graph = { ...graph };
    await el.updateComplete;
    expect((el as unknown as { camera?: unknown }).camera).to.equal(undefined);
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

  it('surfaces a live code event on the node and in the corner', async () => {
    const el = await spatialMap({ codeActivity: activity() });
    el.recordActivity('code_activity', {
      type: 'code_activity',
      payload: { action: 'merged', flow_ids: ['reviewer'], pr: 'acme/api#42' },
    });
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[data-pip]')!.textContent!.trim()
    ).to.equal('merged acme/api#42');
    const toast = el.shadowRoot!.querySelector('[data-toast]')!;
    expect(toast.textContent).to.contain('PR Reviewer');
    expect(toast.textContent).to.contain('merged acme/api#42');
    const merged = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-code="flow:reviewer"] [data-action="merged"]'
    )!;
    expect(merged.textContent!.trim()).to.equal('2');
  });
});
