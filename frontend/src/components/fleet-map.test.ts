import { expect, fixture, html } from '@open-wc/testing';
import { spy, stub, useFakeTimers } from 'sinon';
import './fleet-map';
import type { FleetMap } from './fleet-map';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';

const node = (el: FleetMap, id: string) =>
  el.shadowRoot!.querySelector<HTMLButtonElement>(`[data-node="${id}"]`)!;

/** The default view is onboarded agents; include discovered and inactive. */
async function allAgents(el: FleetMap): Promise<void> {
  el.shadowRoot!.querySelector<HTMLElement>(
    '[data-state="discovered"]'
  )!.click();
  el.shadowRoot!.querySelector<HTMLElement>(
    '[data-type="provider_account"]'
  )!.click();
  await el.updateComplete;
}

// The Show choice is remembered per user and map: no test inherits another's.
afterEach(() => {
  for (const key of Object.keys(localStorage))
    if (key.startsWith('preloop:fleet-map:types')) localStorage.removeItem(key);
});

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
    await allAgents(el);
    expect(el.shadowRoot!.querySelectorAll('.node').length).to.equal(15);
    expect(node(el, 'agent:cursor').getAttribute('aria-label')).to.contain(
      'Not onboarded'
    );
    expect(node(el, 'account:dev').querySelector('.gap-dot')).to.exist;
    expect(
      el.shadowRoot!.querySelectorAll('path.edge.configured').length
    ).to.equal(2);
    // Accounts sit inside their provider, so that relationship is not a line.
    expect(
      el.shadowRoot!.querySelector('path[data-edge="account_of_provider"]')
    ).to.not.exist;
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
    await allAgents(el);
    el.shadowRoot!.querySelector<HTMLElement>(
      '[data-owner="user:sam"]'
    )!.click();
    await el.updateComplete;
    expect(node(el, 'user:maya')).to.not.exist;
    expect(
      el
        .shadowRoot!.querySelector('.count')!
        .textContent!.replace(/\s+/g, ' ')
        .trim()
    ).to.equal('6 active in this window');
  });
});

describe('fleet-map gap filter', () => {
  it('counts each gap kind and lights the nodes that carry it', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    await allAgents(el);
    const chip = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="fleet-map-gap-select"]'
    )!;
    expect(chip.parentElement!.textContent).to.contain('1 Not onboarded');
    el.shadowRoot!.querySelector<HTMLElement>(
      '[data-gap="discovered_agent"]'
    )!.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector<HTMLElement & { checked: boolean }>(
        '[data-gap="discovered_agent"]'
      )!.checked
    ).to.equal(true);
    expect(node(el, 'agent:cursor').classList.contains('focus')).to.equal(true);
    expect(node(el, 'agent:claude').classList.contains('dim')).to.equal(true);
    expect(
      node(el, 'agent:cursor').querySelector('.sub')!.textContent
    ).to.equal('Not onboarded');
  });

  it('draws no gap control when the map has no gaps', async () => {
    const graph = sampleFleetMap();
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{
          ...graph,
          nodes: graph.nodes.map((n) => ({ ...n, gaps: [] })),
        }}
      ></fleet-map>`
    );
    expect(el.shadowRoot!.querySelector('[data-testid="fleet-map-gap-select"]'))
      .not.to.exist;
  });
});

describe('fleet-map completeness and navigation', () => {
  it('keeps the full impact list when a type filter hides intermediate nodes', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    await allAgents(el);
    el.shadowRoot!.querySelector('[data-type="model"]')!.dispatchEvent(
      new MouseEvent('click')
    );
    await el.updateComplete;
    if (!node(el, 'account:prod')) {
      el.shadowRoot!.querySelector(
        '[data-type="provider_account"]'
      )!.dispatchEvent(new MouseEvent('click'));
      await el.updateComplete;
    }
    node(el, 'account:prod').click();
    await el.updateComplete;
    const panel = el.shadowRoot!.querySelector(
      '[data-testid="fleet-map-panel"]'
    )!;
    expect(panel.textContent).to.contain('OpenAI');
    expect(panel.textContent).to.contain('Codex');
    expect(panel.textContent).not.to.contain('OpenAI dev');
  });

  it('warns about incomplete impact from capped or unavailable sources', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{ ...sampleFleetMap(), truncated: true, unavailable: ['devices'] }}
      ></fleet-map>`
    );
    if (!node(el, 'account:prod')) {
      el.shadowRoot!.querySelector(
        '[data-type="provider_account"]'
      )!.dispatchEvent(new MouseEvent('click'));
      await el.updateComplete;
    }
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
    await allAgents(el);
    const count = el
      .shadowRoot!.querySelector('.count')!
      .textContent!.replace(/\s+/g, ' ')
      .trim();
    expect(count).to.equal('15 active in this window');
    expect(
      el.shadowRoot!.querySelector('.count')!.getAttribute('title')
    ).to.contain('1 summary groups');
  });

  it('keeps zoom controls and selection on screen when the panel is below the map', async () => {
    const { setViewport } = await import('@web/test-runner-commands');
    await setViewport({ width: 390, height: 844 });
    try {
      const el = await fixture<FleetMap>(
        html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
      );
      if (!node(el, 'account:prod')) {
        el.shadowRoot!.querySelector(
          '[data-type="provider_account"]'
        )!.dispatchEvent(new MouseEvent('click'));
        await el.updateComplete;
      }
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
    await allAgents(el);
    el.shadowRoot!.querySelector<HTMLElement>(
      '[data-gap="discovered_agent"]'
    )!.click();
    el.shadowRoot!.querySelector<HTMLElement>(
      '[data-owner="user:maya"]'
    )!.click();
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

  it('exposes its selection so a host can clear it', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    expect(el.hasSelection()).to.be.false;
    (el.shadowRoot!.querySelector('button[data-node]') as HTMLElement).click();
    await el.updateComplete;
    expect(el.hasSelection()).to.be.true;
    el.clearSelection();
    await el.updateComplete;
    expect(el.hasSelection()).to.be.false;
  });
});

describe('fleet-map v2 readability', () => {
  it('shows onboarded agents by default and counts the rest behind filters', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{ ...sampleFleetMap(), decommissioned_agents: 4 }}
      ></fleet-map>`
    );
    expect(node(el, 'agent:cursor')).to.not.exist;
    // Only the hidden agent used this account; it leaves with the agent.
    expect(node(el, 'account:dev')).to.not.exist;
    const discovered = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-state="discovered"]'
    )!;
    expect(discovered.textContent).to.contain('Discovered 1');
    expect(
      el.shadowRoot!.querySelector('.count')!.getAttribute('title')
    ).to.contain('4 decommissioned agents not shown');
    discovered.click();
    await el.updateComplete;
    expect(node(el, 'agent:cursor')).to.exist;
  });

  it('nests accounts in their provider and folds them', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    el.shadowRoot!.querySelector(
      '[data-type="provider_account"]'
    )!.dispatchEvent(new MouseEvent('click'));
    await el.updateComplete;
    const provider = node(el, 'provider:openai').getBoundingClientRect();
    const account = node(el, 'account:prod').getBoundingClientRect();
    expect(account.top).to.be.greaterThan(provider.top);
    expect(account.left).to.be.greaterThan(provider.left);
    // Inside its provider an account drops the repeated provider name.
    expect(node(el, 'account:prod').textContent!.trim()).to.equal('Prod');
    expect(node(el, 'account:prod').getAttribute('aria-label')).to.contain(
      'OpenAI prod'
    );
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-fold="provider:openai"]'
    )!.click();
    await el.updateComplete;
    expect(node(el, 'account:prod')).to.not.exist;
    expect(node(el, 'provider:openai')).to.exist;
  });

  it('puts each column directly under its header', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const titles = [...el.shadowRoot!.querySelectorAll('.column-title')];
    expect(titles[0].textContent).to.contain('People');
    const header = titles[0].getBoundingClientRect();
    const first = node(el, 'user:maya').getBoundingClientRect();
    const people = node(el, 'user:sam').getBoundingClientRect();
    expect(Math.min(first.top, people.top) - header.bottom).to.be.below(40);
  });

  it('collapses long columns into a +N more row that expands', async () => {
    const graph = sampleFleetMap();
    const extra = Array.from({ length: 20 }, (_, i) => ({
      ...graph.nodes.find((n) => n.id === 'agent:codex')!,
      id: `agent:x${i}`,
      label: `Extra ${i}`,
    }));
    const el = await fixture<FleetMap>(
      html`<fleet-map
        .graph=${{ ...graph, nodes: [...graph.nodes, ...extra] }}
      ></fleet-map>`
    );
    const more = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-more="agent"]'
    )!;
    expect(more.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
      '+10 more agents'
    );
    more.click();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-more="agent"]')).to.not.exist;
    expect(node(el, 'agent:x19')).to.exist;
  });

  it('search highlights matches and Enter selects the first', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const toolbar = el.shadowRoot!.querySelector('list-toolbar')!;
    const input = toolbar
      .shadowRoot!.querySelector('sl-input')!
      .shadowRoot!.querySelector('input')!;
    input.value = 'codex';
    input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    await el.updateComplete;
    expect(node(el, 'agent:codex').classList.contains('match')).to.equal(true);
    expect(node(el, 'user:maya').classList.contains('dim')).to.equal(true);
    input.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        composed: true,
      })
    );
    await el.updateComplete;
    expect(el.hasSelection()).to.be.true;
    expect(
      el.shadowRoot!.querySelector('[data-testid="fleet-map-panel"] h3')!
        .textContent
    ).to.contain('Codex');
  });

  it('focus mode hides everything unrelated to the selection', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    node(el, 'agent:claude').click();
    await el.updateComplete;
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="focus-only"]'
    )!.click();
    await el.updateComplete;
    expect(node(el, 'agent:codex')).to.not.exist;
    expect(node(el, 'model:sonnet')).to.exist;
    expect(node(el, 'device:mbp')).to.exist;
  });

  it('gaps only keeps gap nodes and their neighbours', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    el.shadowRoot!.querySelector<HTMLElement>('[data-gap="all"]')!.click();
    await el.updateComplete;
    expect(node(el, 'runner:r2')).to.exist;
    expect(node(el, 'agent:codex')).to.not.exist;
  });

  it('toggles full screen from its own button and leaves it on Esc', async () => {
    const el = await fixture<FleetMap>(
      html`<fleet-map .graph=${sampleFleetMap()}></fleet-map>`
    );
    const events: boolean[] = [];
    el.addEventListener('fleet-map-fullscreen', (e) =>
      events.push((e as CustomEvent).detail.fullscreen)
    );
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="fleet-map-fullscreen"]'
    )!.click();
    await el.updateComplete;
    expect(el.hasAttribute('fullscreen')).to.be.true;
    expect(getComputedStyle(el).position).to.equal('fixed');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await el.updateComplete;
    expect(el.fullscreen).to.be.false;
    expect(events).to.deep.equal([true, false]);
    expect(document.body.style.overflow).to.equal('');
  });
});

describe('fleet-map labels', () => {
  it('names discovered agents by product, not raw kind', async () => {
    const { agentKindLabel } = await import('./fleet-map');
    expect(agentKindLabel('claude_code')).to.equal('Claude Code');
    expect(agentKindLabel('cursor_cli')).to.equal('Cursor CLI');
  });
  describe('activity window', () => {
    afterEach(() => localStorage.removeItem('preloop:fleet-map:window'));

    it('hides resources inactive in the window when the server did not', async () => {
      const graph = sampleFleetMap();
      const recent = new Date().toISOString();
      const old = new Date(Date.now() - 40 * 86_400_000).toISOString();
      for (const n of graph.nodes) {
        if (n.id === 'agent:codex' || n.id === 'device:mbp')
          n.last_seen_at = recent;
        if (n.id === 'agent:claude') n.last_seen_at = old;
      }
      const el = await fixture<FleetMap>(
        html`<fleet-map .graph=${graph} .activityWindow=${'30d'}></fleet-map>`
      );
      expect(node(el, 'agent:codex')).to.exist;
      expect(node(el, 'model:gpt')).to.exist;
      expect(node(el, 'provider:openai')).to.exist;
      expect(node(el, 'user:maya')).to.exist;
      expect(node(el, 'agent:claude')).to.not.exist;
      expect(node(el, 'model:sonnet')).to.not.exist;
      expect(node(el, 'user:sam')).to.not.exist;
      const changed = spy();
      el.addEventListener('fleet-map-window-change', changed);
      el.shadowRoot!.querySelector<HTMLElement>('[data-window="any"]')!.click();
      await el.updateComplete;
      expect(changed.firstCall.args[0].detail).to.deep.equal({
        window: 'any',
        minutes: null,
      });
      expect(localStorage.getItem('preloop:fleet-map:window')).to.equal('any');
      expect(node(el, 'agent:claude')).to.exist;
    });

    it('keeps the window reachable when nothing is active in it', async () => {
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${{ ...sampleFleetMap(), nodes: [], edges: [], active_within_minutes: 1440 }}
          .activityWindow=${'24h'}
        ></fleet-map>`
      );
      const empty = el.shadowRoot!.querySelector(
        '[data-testid="fleet-map-empty"]'
      )!;
      expect(empty.textContent).to.contain(
        'Nothing active in the last 24 hours'
      );
      const changed = spy();
      el.addEventListener('fleet-map-window-change', changed);
      el.shadowRoot!.querySelector<HTMLElement>('[data-window="any"]')!.click();
      expect(changed.firstCall.args[0].detail.minutes).to.equal(null);
    });

    it('reads naive server timestamps as UTC, as the server does', async () => {
      const { activeGraph } = await import('../utils/fleet-map-activity');
      const graph = sampleFleetMap();
      const codex = graph.nodes.find((n) => n.id === 'agent:codex')!;
      codex.last_seen_at = '2026-10-01T12:00:00';
      const since = Date.UTC(2026, 9, 1, 11, 59);
      expect(
        activeGraph(graph, since).nodes.some((n) => n.id === 'agent:codex')
      ).to.equal(true);
      expect(
        activeGraph(graph, since + 2 * 60_000).nodes.some(
          (n) => n.id === 'agent:codex'
        )
      ).to.equal(false);
    });

    it('trusts a graph the server already filtered', async () => {
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${{ ...sampleFleetMap(), active_within_minutes: 1440 }}
          .activityWindow=${'24h'}
        ></fleet-map>`
      );
      expect(node(el, 'agent:claude')).to.exist;
    });
  });

  describe('live activity', () => {
    it('lights each line by what travelled it, with the tool or model it names', async () => {
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${sampleFleetMap()}
          .activityWindow=${'any'}
        ></fleet-map>`
      );
      const call = {
        type: 'model_gateway_call',
        payload: {
          managed_agent_id: 'codex',
          model_alias: 'gpt-6.1',
          prompt_tokens: 4100,
          completion_tokens: 380,
        },
      };
      el.recordActivity('gateway_activity', call);
      el.recordActivity('gateway_activity', call);
      // An MCP tool runs on its server, which the map does not draw: it
      // lights no device line.
      el.recordActivity('gateway_activity', {
        type: 'mcp_call',
        payload: { managed_agent_id: 'codex', tool_name: 'search' },
      });
      // A native tool runs on the agent's machine and is named on that line.
      el.recordActivity('runtime_sessions', {
        type: 'runtime_session_updated',
        payload: {
          activity_type: 'tool_call',
          managed_agent_id: 'codex',
          tool_name: 'Edit',
          status: 'allowed',
        },
      });
      el.recordActivity('approvals', { type: 'approval_created' });
      expect(el.shadowRoot!.querySelector('path.edge.active')).to.not.exist;
      el.flushActivity();
      await el.updateComplete;
      const line = el.shadowRoot!.querySelector<SVGPathElement>(
        'path[data-edge-id="uses_model:agent:codex:model:gpt:configured"]'
      )!;
      expect(line.classList.contains('active')).to.equal(true);
      expect(line.getAttribute('data-activity')).to.equal(
        'gpt-6.1 · 4.1k in / 380 out ×2'
      );
      const hosted = el.shadowRoot!.querySelector<SVGPathElement>(
        'path[data-edge-id="hosts:device:mbp:agent:codex:observed"]'
      )!;
      expect(hosted.getAttribute('data-activity')).to.equal('Edit allowed');
      const labels = [
        ...el.shadowRoot!.querySelectorAll('.activity-label'),
      ].map((label) => label.textContent);
      // Text is drawn only where it covers no node; the line keeps it.
      expect(labels.join(' ')).not.to.contain('tool call');
      for (const text of el.shadowRoot!.querySelectorAll('.activity-label')) {
        const t = text.getBoundingClientRect();
        for (const node of el.shadowRoot!.querySelectorAll('.node')) {
          const r = node.getBoundingClientRect();
          const hit =
            t.left < r.right &&
            r.left < t.right &&
            t.top < r.bottom &&
            r.top < t.bottom;
          expect(hit, text.textContent ?? '').to.equal(false);
        }
      }
      expect(labels.join(' ')).not.to.contain('model request');
      expect(
        el.shadowRoot!.querySelectorAll('path.edge.active').length
      ).to.equal(2);
    });
  });

  describe('live refresh', () => {
    it('asks the host to refetch when an unmapped agent becomes active', async () => {
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${{ ...sampleFleetMap(), active_within_minutes: 43200 }}
          .activityWindow=${'30d'}
        ></fleet-map>`
      );
      el.live = true;
      const stale = spy();
      el.addEventListener('fleet-map-stale', stale);
      const clock = useFakeTimers();
      try {
        const call = {
          type: 'model_gateway_call',
          payload: { managed_agent_id: 'not-on-the-map', model_alias: 'x' },
        };
        el.recordActivity('gateway_activity', call);
        el.recordActivity('gateway_activity', call);
        clock.tick(2_999);
        expect(stale.callCount).to.equal(0);
        clock.tick(1);
        expect(stale.callCount).to.equal(1);
      } finally {
        clock.restore();
      }
    });

    it('does not refetch for a mapped agent without a matching line, or under Any time', async () => {
      // Codex stays on the map but loses every line, so activity from it
      // yields no edge and only the mapped-agent guard keeps it quiet.
      const sample = sampleFleetMap();
      const lineless = {
        ...sample,
        active_within_minutes: 43200,
        edges: sample.edges.filter(
          (e) => e.source !== 'agent:codex' && e.target !== 'agent:codex'
        ),
      };
      for (const window of ['30d', 'any'] as const) {
        const el = await fixture<FleetMap>(
          html`<fleet-map
            .graph=${lineless}
            .activityWindow=${window}
            live
          ></fleet-map>`
        );
        const stale = spy();
        el.addEventListener('fleet-map-stale', stale);
        const clock = useFakeTimers();
        try {
          el.recordActivity('gateway_activity', {
            type: 'model_gateway_call',
            payload: {
              managed_agent_id: 'codex',
              model_alias: 'ambient-model',
            },
          });
          if (window === 'any')
            el.recordActivity('gateway_activity', {
              type: 'model_gateway_call',
              payload: { managed_agent_id: 'not-on-the-map', model_alias: 'x' },
            });
          clock.tick(10_000);
          expect(stale.callCount, window).to.equal(0);
        } finally {
          clock.restore();
          el.remove();
        }
      }
    });

    it('fits the graph that arrives for a new window, not the old one', async () => {
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${sampleFleetMap()}
          .activityWindow=${'30d'}
        ></fleet-map>`
      );
      const fit = spy(el as unknown as { fit: () => void }, 'fit');
      el.activityWindow = '1h';
      await el.updateComplete;
      fit.resetHistory();
      el.graph = { ...sampleFleetMap() };
      await el.updateComplete;
      expect(fit.callCount).to.equal(1);
      el.graph = { ...sampleFleetMap() };
      await el.updateComplete;
      expect(fit.callCount, 'a background refresh keeps the zoom').to.equal(1);
    });

    it('polls on the window cadence so quiet resources drop out', async () => {
      const clock = useFakeTimers();
      try {
        const el = await fixture<FleetMap>(
          html`<fleet-map
            .graph=${sampleFleetMap()}
            .activityWindow=${'5m'}
            live
          ></fleet-map>`
        );
        const stale = spy();
        el.addEventListener('fleet-map-stale', stale);
        clock.tick(30_001);
        expect(stale.callCount).to.equal(1);
        el.activityWindow = 'any';
        await el.updateComplete;
        clock.tick(600_000);
        expect(stale.callCount).to.equal(1);
        el.remove();
      } finally {
        clock.restore();
      }
    });
  });

  describe('spatial arrangement', () => {
    afterEach(() => {
      for (const key of Object.keys(localStorage))
        if (key.startsWith('preloop:fleet-map:')) localStorage.removeItem(key);
    });

    it('is not offered to anyone but an instance administrator', async () => {
      localStorage.setItem('preloop:fleet-map:arrangement:fleet:u1', 'spatial');
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${sampleFleetMap()}
          .activityWindow=${'any'}
          user-id="u1"
        ></fleet-map>`
      );
      expect(el.shadowRoot!.querySelector('[data-arrangement]')).to.not.exist;
      // A stored choice cannot turn it on either: columns still render.
      expect(el.shadowRoot!.querySelector('.column-title')).to.exist;
    });

    it('lays nodes out in space and keeps a dragged node where it was dropped', async () => {
      const el = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${sampleFleetMap()}
          .activityWindow=${'any'}
          instance-admin
          user-id="u1"
        ></fleet-map>`
      );
      el.shadowRoot!.querySelector<HTMLButtonElement>(
        '[data-arrangement="spatial"]'
      )!.click();
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('.column-title')).to.not.exist;
      expect(
        localStorage.getItem('preloop:fleet-map:arrangement:fleet:u1')
      ).to.equal('spatial');
      const target = node(el, 'agent:codex');
      const machineX = parseFloat(node(el, 'device:mbp').style.left);
      const before = {
        x: parseFloat(target.style.left),
        y: parseFloat(target.style.top),
      };
      const stage = el.shadowRoot!.querySelector<HTMLElement>('.stage')!;
      const scale = 1;
      (el as unknown as { scale: number }).scale = scale;
      const at = (x: number, y: number) => ({
        bubbles: true,
        composed: true,
        pointerId: 7,
        button: 0,
        clientX: x,
        clientY: y,
      });
      target.dispatchEvent(new PointerEvent('pointerdown', at(100, 100)));
      stage.dispatchEvent(new PointerEvent('pointermove', at(160, 140)));
      stage.dispatchEvent(new PointerEvent('pointerup', at(160, 140)));
      await el.updateComplete;
      const moved = node(el, 'agent:codex');
      expect(parseFloat(moved.style.left)).to.equal(before.x + 60);
      expect(parseFloat(moved.style.top)).to.equal(before.y + 40);
      const stored = JSON.parse(
        localStorage.getItem('preloop:fleet-map:positions:fleet:u1')!
      );
      // Codex sits inside maya-laptop, so the machine moved and is stored.
      expect(Object.keys(stored)).to.have.length(
        el.shadowRoot!.querySelectorAll('[data-node]').length
      );
      expect(stored['device:mbp'].x).to.equal(machineX + 60);
      // The drop click lands on the stage, not the node; the next real
      // node click still selects.
      stage.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      node(el, 'agent:codex').click();
      await el.updateComplete;
      expect(el.hasSelection()).to.equal(true);
      el.clearSelection();
      await el.updateComplete;
      // A click delivered to the node straight after a drop is swallowed.
      node(el, 'user:maya').dispatchEvent(
        new PointerEvent('pointerdown', at(160, 140))
      );
      stage.dispatchEvent(new PointerEvent('pointermove', at(200, 160)));
      stage.dispatchEvent(new PointerEvent('pointerup', at(200, 160)));
      node(el, 'user:maya').click();
      await el.updateComplete;
      expect(el.hasSelection()).to.equal(false);

      const again = await fixture<FleetMap>(
        html`<fleet-map
          .graph=${sampleFleetMap()}
          .activityWindow=${'any'}
          instance-admin
          user-id="u1"
        ></fleet-map>`
      );
      const restored = node(again, 'agent:codex');
      expect(parseFloat(restored.style.left)).to.equal(before.x + 60);
      expect(parseFloat(restored.style.top)).to.equal(before.y + 40);
    });
  });
});
