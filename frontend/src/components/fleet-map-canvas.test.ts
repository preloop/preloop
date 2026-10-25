import { expect, fixture, html } from '@open-wc/testing';
import './fleet-map';
import type { FleetMap } from './fleet-map';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';
import type { FleetMapGraph } from '../fleet-map-api';

const call = {
  type: 'model_gateway_request_started',
  payload: { managed_agent_id: 'codex', model_alias: 'gpt-6.1' },
};

async function spatial(graph: FleetMapGraph = sampleFleetMap(), extra = '') {
  const el = await fixture<FleetMap>(
    html`<fleet-map
      .graph=${graph}
      instance-admin
      spatial-only
      user-id="u-canvas${extra}"
      positions-scope="canvas-test"
    ></fleet-map>`
  );
  await el.updateComplete;
  return el;
}

const box = (el: FleetMap, id: string) => {
  const node = el.shadowRoot!.querySelector<HTMLElement>(
    `[data-node="${id}"]`
  )!;
  return {
    x: parseFloat(node.style.left),
    y: parseFloat(node.style.top),
    w: parseFloat(node.style.width),
    h: parseFloat(node.style.height),
    child: node.classList.contains('child'),
  };
};

describe('fleet-map canvas: machines hold their agents', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  it('draws an agent with a known machine inside that machine, without a hosts line', async () => {
    const el = await spatial();
    const machine = el.shadowRoot!.querySelector<HTMLElement>('.box')!;
    expect(machine, 'the machine is a box').to.exist;
    const laptop = box(el, 'device:mbp');
    const codex = box(el, 'agent:codex');
    expect(codex.child).to.equal(true);
    const frame = {
      x: parseFloat(machine.style.left),
      y: parseFloat(machine.style.top),
      h: parseFloat(machine.style.height),
    };
    expect(codex.x).to.be.greaterThan(frame.x);
    expect(codex.y).to.be.greaterThan(laptop.y);
    expect(codex.y + codex.h).to.be.at.most(frame.y + frame.h);
    expect(
      el.shadowRoot!.querySelector(
        'path[data-edge-id="hosts:device:mbp:agent:codex:observed"]'
      )
    ).to.not.exist;
  });

  it('leaves an agent with no known machine free near its owner', async () => {
    const graph = sampleFleetMap();
    const free = {
      ...graph,
      edges: graph.edges.filter(
        (e) => !(e.kind === 'hosts' && e.target === 'agent:codex')
      ),
    };
    const el = await spatial(free, '-free');
    expect(box(el, 'agent:codex').child).to.equal(false);
    expect(box(el, 'agent:claude').child).to.equal(true);
  });

  it('moves the agents with their machine when the machine is dragged', async () => {
    const el = await spatial(sampleFleetMap(), '-drag');
    const before = box(el, 'agent:codex');
    const header = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-node="device:mbp"]'
    )!;
    const stage = el.shadowRoot!.querySelector<HTMLElement>('.stage')!;
    (el as unknown as { scale: number }).scale = 1;
    const at = (x: number, y: number) => ({
      bubbles: true,
      composed: true,
      pointerId: 5,
      button: 0,
      clientX: x,
      clientY: y,
    });
    header.dispatchEvent(new PointerEvent('pointerdown', at(10, 10)));
    stage.dispatchEvent(new PointerEvent('pointermove', at(90, 30)));
    stage.dispatchEvent(new PointerEvent('pointerup', at(90, 30)));
    await el.updateComplete;
    const after = box(el, 'agent:codex');
    expect(after.x - before.x).to.equal(80);
    expect(after.y - before.y).to.equal(20);
    const stored = JSON.parse(
      localStorage.getItem(
        'preloop:fleet-map:positions:canvas-test:u-canvas-drag'
      )!
    );
    expect(Object.keys(stored)).to.have.length(
      el.shadowRoot!.querySelectorAll('[data-node]').length
    );
  });
});

describe('fleet-map canvas: folding a machine', () => {
  it('keeps the fold control on the spatial canvas, so a folded machine opens again', async () => {
    const el = await spatial(sampleFleetMap(), '-fold');
    const fold = () =>
      el.shadowRoot!.querySelector<HTMLButtonElement>(
        '[data-fold="device:mbp"]'
      );
    fold()!.click();
    await el.updateComplete;
    expect(fold(), 'still there when folded').to.exist;
    expect(fold()!.getAttribute('aria-expanded')).to.equal('false');
    expect(el.shadowRoot!.querySelector('[data-node="agent:codex"]')).to.not
      .exist;
    fold()!.click();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-node="agent:codex"]')).to.exist;
  });
});

describe('fleet-map canvas: marks', () => {
  it('shows product logos for known agent kinds and the generic icon otherwise', async () => {
    const graph = sampleFleetMap();
    const kinds: Record<string, string> = {
      'agent:claude': 'claude_code',
      'agent:codex': 'codex',
      'agent:cursor': 'mystery_agent',
    };
    const el = await spatial(
      {
        ...graph,
        nodes: graph.nodes.map((n) =>
          kinds[n.id]
            ? { ...n, detail: kinds[n.id], agent_state: 'onboarded' }
            : n
        ),
      },
      '-marks'
    );
    const mark = (id: string) =>
      el.shadowRoot!.querySelector(`[data-node="${id}"] .glyph`)!;
    expect(
      mark('agent:claude').querySelector('img')!.getAttribute('src')
    ).to.contain('claude.svg');
    expect(
      mark('agent:codex').querySelector('img')!.getAttribute('src')
    ).to.contain('codex.svg');
    expect(mark('agent:cursor').querySelector('img')).to.not.exist;
    expect(
      mark('agent:cursor').querySelector('sl-icon')!.getAttribute('name')
    ).to.equal('robot');
  });

  it('marks a DeepSeek model served by OpenRouter as DeepSeek, and its provider as OpenRouter', async () => {
    const graph = sampleFleetMap();
    const routed = {
      ...graph,
      nodes: [
        ...graph.nodes,
        {
          ...graph.nodes.find((n) => n.id === 'model:gpt')!,
          id: 'model:flash',
          label: 'DeepSeek Flash',
          detail: 'deepseek-flash',
          provider_ids: ['provider:openrouter'],
        },
        {
          ...graph.nodes.find((n) => n.id === 'provider:openai')!,
          id: 'provider:openrouter',
          label: 'OpenRouter',
          provider_ids: ['provider:openrouter'],
        },
      ],
      edges: [
        ...graph.edges,
        {
          ...graph.edges[0],
          id: 'codex-flash',
          source: 'agent:codex',
          target: 'model:flash',
          kind: 'uses_model' as const,
        },
        {
          ...graph.edges[0],
          id: 'flash-or',
          source: 'model:flash',
          target: 'provider:openrouter',
          kind: 'provided_by' as const,
        },
      ],
    };
    const el = await spatial(routed, '-routed');
    const mark = (id: string) =>
      el
        .shadowRoot!.querySelector(`[data-node="${id}"] .glyph`)!
        .getAttribute('data-mark');
    expect(mark('model:flash')).to.equal('DeepSeek');
    expect(mark('provider:openrouter')).to.equal('OpenRouter');
    expect(mark('provider:openai')).to.equal('OpenAI');
    // A fading model keeps its provider's mark.
    const unknown = {
      ...routed,
      nodes: routed.nodes.map((n) =>
        n.id === 'model:flash'
          ? { ...n, label: 'Mystery', detail: 'mystery-1' }
          : n
      ),
    };
    el.graph = unknown;
    await el.updateComplete;
    expect(mark('model:flash')).to.equal('OpenRouter');
    el.graph = {
      ...unknown,
      nodes: unknown.nodes.filter((n) => n.id !== 'model:flash'),
    };
    await el.updateComplete;
    expect(
      el
        .shadowRoot!.querySelector('[data-leaving="model:flash"] .glyph')!
        .getAttribute('data-mark')
    ).to.equal('OpenRouter');
  });

  it('labels each pull request counter', async () => {
    const el = await spatial(sampleFleetMap(), '-badges');
    el.codeActivity = {
      active_within_minutes: 5,
      since: new Date().toISOString(),
      truncated: false,
      counts: {
        'agent:codex': { opened: 2, commented: 0, reviewed: 1, merged: 3 },
      },
      flows: [],
      events: [],
    };
    await el.updateComplete;
    const titles = [
      ...el.shadowRoot!.querySelectorAll<HTMLElement>(
        '[data-code="agent:codex"] .code-badge'
      ),
    ].map((b) => b.title);
    expect(titles).to.deep.equal(['2 opened', '1 reviewed', '3 merged']);
  });
});

describe('fleet-map canvas: bubbles and a bare map', () => {
  afterEach(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map')) localStorage.removeItem(key);
  });

  it('shows no bubbles until turned on, then a short note, remembered', async () => {
    const el = await spatial(sampleFleetMap(), '-bubbles');
    el.recordActivity('gateway_activity', call);
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.bubble')).to.not.exist;
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="fleet-map-bubbles"]'
    )!.click();
    await el.updateComplete;
    const bubble = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-bubble="agent:codex"]'
    )!;
    expect(bubble.textContent!.trim()).to.equal('gpt-6.1  thinking');
    expect(
      localStorage.getItem(
        'preloop:fleet-map:bubbles:canvas-test:u-canvas-bubbles'
      )
    ).to.equal('on');
  });

  it('never shows more than four bubbles or eight lit nodes', async () => {
    const graph = sampleFleetMap();
    const many = Array.from({ length: 10 }, (_, i) => ({
      ...graph.nodes.find((n) => n.id === 'agent:codex')!,
      id: `agent:m${i}`,
      resource_id: `m${i}`,
    }));
    localStorage.setItem(
      'preloop:fleet-map:bubbles:canvas-test:u-canvas-many',
      'on'
    );
    const el = await spatial(
      {
        ...graph,
        nodes: [...graph.nodes, ...many],
        edges: [
          ...graph.edges,
          ...many.map((n) => ({
            ...graph.edges[0],
            id: `m-${n.id}`,
            source: n.id,
            target: 'model:gpt',
            kind: 'uses_model' as const,
          })),
        ],
      },
      '-many'
    );
    for (const n of many)
      el.recordActivity('gateway_activity', {
        type: 'model_gateway_request_started',
        payload: { managed_agent_id: n.resource_id, model_alias: 'gpt-6.1' },
      });
    await el.updateComplete;
    expect(el.litNodes.length).to.equal(8);
    expect(el.shadowRoot!.querySelectorAll('.bubble').length).to.equal(4);
  });

  it('a bare map shows only the canvas, and an empty one is a blank stage', async () => {
    const el = await spatial(sampleFleetMap(), '-bare');
    el.bare = true;
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.toolbar')).to.not.exist;
    expect(
      el.shadowRoot!.querySelector<HTMLElement>('.legend')!.hidden
    ).to.equal(true);
    el.graph = { ...sampleFleetMap(), nodes: [], edges: [] };
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="fleet-map-empty"]')).to
      .not.exist;
    expect(el.shadowRoot!.querySelector('.stage')).to.exist;
  });
});
