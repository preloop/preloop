import { expect, fixture, html } from '@open-wc/testing';
import { spy, stub } from 'sinon';
import './fleet-map';
import type { FleetMap } from './fleet-map';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';

const node = (el: FleetMap, id: string) =>
  el.shadowRoot!.querySelector<HTMLButtonElement>(`[data-node="${id}"]`)!;

describe('fleet-map', () => {
  it('says what to do when there is nothing to map', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{ ...sampleFleetMap(), nodes: [], edges: [] }}
      ></fleet-map>`
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-empty"]')!
        .textContent
    ).to.contain('preloop runner fg');
  });

  it('renders every node and marks gaps in the accessible name', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    expect(el.shadowRoot!.querySelectorAll('.node').length).to.equal(15);
    expect(node(el, 'agent:cursor').getAttribute('aria-label')).to.contain(
      'Not onboarded'
    );
    expect(node(el, 'account:dev').querySelector('.gap-dot')).to.exist;
    expect(
      el.shadowRoot!.querySelectorAll('path.edge.configured').length
    ).to.equal(7);
  });

  it('selecting a node dims the rest and lists its blast radius with actions', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    node(el, 'provider:openai').click();
    await el.updateComplete;
    expect(node(el, 'agent:claude').classList.contains('dim')).to.equal(true);
    expect(node(el, 'agent:codex').classList.contains('lit')).to.equal(true);
    const panel = el.shadowRoot!.querySelector(
      '[data-testid="fleet-map-panel"]'
    )!;
    expect(panel.textContent).to.contain('Maya Chen');
    expect(panel.textContent).to.contain('Sam Ortiz');
    const rotate = [...panel.querySelectorAll('a')].find(
      (a) => a.getAttribute('href') === '/console/ai-models/example'
    );
    expect(rotate?.textContent).to.contain('Manage credentials');
    expect(panel.querySelector('a[href="/console/settings/users"]')).to.exist;
    el.shadowRoot!.querySelector<HTMLElement>('.stage')!.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape' })
    );
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="fleet-map-panel"]')).to
      .not.exist;
  });

  it('filters by owner and counts honestly', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const select = el.shadowRoot!.querySelectorAll('select')[0];
    select.value = 'user:sam';
    select.dispatchEvent(new Event('change'));
    await el.updateComplete;
    expect(node(el, 'user:maya')).to.not.exist;
    expect(
      el
        .shadowRoot!.querySelector('.count')!
        .textContent!.replace(/\s+/g, ' ')
        .trim()
    ).to.equal('6 of 15 nodes');
  });
});

describe('fleet-map gap strip', () => {
  it('counts each gap kind and lights the nodes that carry it', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const chip = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '.gap-chip[data-gap="discovered_agent"]'
    )!;
    expect(chip.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
      '1not onboarded'
    );
    chip.click();
    await el.updateComplete;
    expect(chip.getAttribute('aria-pressed')).to.equal('true');
    expect(node(el, 'agent:cursor').classList.contains('focus')).to.equal(true);
    expect(node(el, 'agent:claude').classList.contains('dim')).to.equal(true);
    expect(
      node(el, 'agent:cursor').querySelector('.sub')!.textContent
    ).to.equal('Not onboarded');
  });

  it('says so when the map has no gaps', async () => {
    const graph = sampleFleetMap();
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{
          ...graph,
          nodes: graph.nodes.map((n) => ({ ...n, gaps: [] })),
        }}
      ></fleet-map>`
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-gaps"]')!
        .textContent
    ).to.contain('No governance gaps');
  });
});

describe('fleet-map completeness and navigation', () => {
  it('keeps the full impact list when a type filter hides intermediate nodes', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const modelToggle = [
      ...el.shadowRoot!.querySelectorAll<HTMLButtonElement>(
        '.type-toggles button'
      ),
    ].find((button) => button.textContent?.trim() === 'Models')!;
    modelToggle.click();
    await el.updateComplete;
    node(el, 'account:prod').click();
    await el.updateComplete;
    const panel = el.shadowRoot!.querySelector(
      '[data-testid="fleet-map-panel"]'
    )!;
    expect(panel.textContent).to.contain('gpt-6.1');
    expect(panel.textContent).to.contain('Codex');
    expect(panel.textContent).not.to.contain('OpenAI dev');
  });

  it('warns about incomplete impact from capped or unavailable sources', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{ ...sampleFleetMap(), truncated: true, unavailable: ['devices'] }}
      ></fleet-map>`
    );
    node(el, 'account:prod').click();
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain(
      'filters only narrow the loaded map'
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-panel"]')!
        .textContent
    ).to.contain('may be incomplete');
  });

  it('uses solid configured and dashed observed relationships', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const observed = el.shadowRoot!.querySelector('path.observed')!;
    const configured = el.shadowRoot!.querySelector('path.configured')!;
    expect(getComputedStyle(observed).strokeDasharray).not.to.equal('none');
    expect(getComputedStyle(configured).strokeDasharray).to.equal('none');
    const initial =
      el.shadowRoot!.querySelector<HTMLElement>('.plane')!.style.transform;
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      'button[aria-label="Zoom in"]'
    )!.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector<HTMLElement>('.plane')!.style.transform
    ).not.to.equal(initial);
  });
});

describe('fleet-map capped and narrow views', () => {
  it('counts loaded resources separately from omitted summary groups', async () => {
    const { withClusters } = await import('../fleet-map-api');
    const graph = withClusters({
      ...sampleFleetMap(),
      truncated: true,
      clusters: [{ type: 'agent', count: 80 }],
    });
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${graph}></fleet-map>`
    );
    const count = el
      .shadowRoot!.querySelector('.count')!
      .textContent!.replace(/\s+/g, ' ')
      .trim();
    expect(count).to.equal('15 nodes · 1 summary group');
  });

  it('keeps zoom controls and selection on screen when the panel is below the map', async () => {
    const { setViewport } = await import('@web/test-runner-commands');
    await setViewport({ width: 390, height: 844 });
    try {
      const el = await fixture<FleetMap>(
        html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
      );
      node(el, 'account:prod').click();
      await el.updateComplete;
      const stage = el
        .shadowRoot!.querySelector('.stage')!
        .getBoundingClientRect();
      const controls = el
        .shadowRoot!.querySelector('.canvas-tools')!
        .getBoundingClientRect();
      const selection = node(el, 'account:prod').getBoundingClientRect();
      expect(controls.left).to.be.at.least(stage.left);
      expect(controls.right).to.be.at.most(stage.right);
      expect(selection.left).to.be.at.least(stage.left);
      expect(selection.right).to.be.at.most(stage.right);
    } finally {
      await setViewport({ width: 1280, height: 800 });
    }
  });

  it('shows gaps for the active filters and clears a focus that becomes hidden', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-gap="discovered_agent"]'
    )!.click();
    const owner = el.shadowRoot!.querySelector('select')!;
    owner.value = 'user:maya';
    owner.dispatchEvent(new Event('change'));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-gap="discovered_agent"]')).to.not
      .exist;
    expect(node(el, 'agent:claude').classList.contains('dim')).to.equal(false);
  });
});

describe('fleet-map gesture performance', () => {
  it('pans and zooms without rendering the graph again', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const stage = el.shadowRoot!.querySelector<HTMLElement>('.stage')!;
    const plane = el.shadowRoot!.querySelector<HTMLElement>('.plane')!;
    const before = plane.style.transform;
    const render = spy(el, 'render');
    const capture = stub(stage, 'setPointerCapture');
    const release = stub(stage, 'releasePointerCapture');
    try {
      stage.dispatchEvent(
        new PointerEvent('pointerdown', {
          pointerId: 1,
          button: 0,
          clientX: 10,
          clientY: 10,
        })
      );
      for (let i = 0; i < 20; i++) {
        stage.dispatchEvent(
          new PointerEvent('pointermove', {
            pointerId: 1,
            clientX: 20 + i,
            clientY: 30 + i,
          })
        );
        stage.dispatchEvent(
          new WheelEvent('wheel', { deltaY: -1, cancelable: true })
        );
      }
      stage.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1 }));
      stage.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
      await el.updateComplete;
      expect(plane.style.transform).not.to.equal(before);
      expect(render.callCount).to.equal(0);
      expect(el.shadowRoot!.querySelector('.zoom')!.textContent).to.match(
        /\d+%/
      );
      el.graph = { ...sampleFleetMap(), nodes: [], edges: [] };
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('.plane')).not.to.exist;
      expect(el.shadowRoot!.querySelector('[data-testid="fleet-map-empty"]')).to
        .exist;
    } finally {
      render.restore();
      capture.restore();
      release.restore();
    }
  });
});
