import { afterEach, describe, expect, it, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import '@shoelace-style/shoelace/dist/themes/light.css';
import '../../src/components/fleet-map';
import '../../src/components/inventory-card';
import '../../src/views/authed/dashboard-control-plane-view';
import { sampleFleetMap } from '../../src/test-helpers/fleet-map-graph';
import { getFleetMap, withCodeFlows } from '../../src/fleet-map-api';
import {
  avatarSource,
  nodeUsage,
  workRelationLabel,
} from '../../src/utils/fleet-map-details';
import { FleetStory } from '../../src/utils/fleet-map-story';
import { codeHitFromRealtime } from '../../src/utils/fleet-map-motion';

const mounted: HTMLElement[] = [];
async function mount(extra: Record<string, unknown> = {}) {
  const el: any = document.createElement('fleet-map');
  Object.assign(el, {
    graph: sampleFleetMap(),
    instanceAdmin: true,
    reducedMotion: true,
    userId: 'pass3-user',
    positionsScope: 'pass3',
    ...extra,
  });
  el.style.cssText = 'height:600px;width:100%;min-width:0';
  document.body.style.margin = '0';
  document.body.append(el);
  mounted.push(el);
  await el.updateComplete;
  await new Promise(requestAnimationFrame);
  await el.updateComplete;
  return el;
}
const root = (el: HTMLElement) => el.shadowRoot!;
const node = (el: HTMLElement, id: string) =>
  root(el).querySelector(`[data-node="${id}"]`)!;
afterEach(() => {
  mounted.splice(0).forEach((el) => el.remove());
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('Fleet Map pass 3', () => {
  it('does not carry model activity from the previous window while the host refetches', async () => {
    const graph = sampleFleetMap();
    graph.active_within_minutes = 60;
    graph.nodes = graph.nodes.map((n) => ({
      ...n,
      idle: false,
      last_active_at: new Date(
        Date.now() - (n.type === 'model' ? 30 : 1) * 60_000
      ).toISOString(),
    }));
    const el = await mount({ graph, activityWindow: '1h' });
    expect(root(el).querySelector('[data-node="model:gpt"]')).not.toBeNull();
    el.activityWindow = '5m';
    await el.updateComplete;
    expect(root(el).querySelector('[data-node="model:gpt"]')).toBeNull();
    expect(root(el).querySelector('[data-node="agent:codex"]')).not.toBeNull();
  });

  it('keeps narrow filter pages inside the viewport and closes search before fullscreen', async () => {
    await page.viewport(390, 900);
    const el = await mount();
    await page.getByTestId('fleet-map-filters').click();
    await page.getByTestId('fleet-map-show').click();
    const idle = root(el).querySelector<HTMLElement>(
      '[data-testid="fleet-map-idle"]'
    )!;
    const bounds = idle.getBoundingClientRect();
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(390);
    await userEvent.keyboard('{Escape}');
    el.fullscreen = true;
    await el.updateComplete;
    await page.getByTestId('compact-search').click();
    const toolbar = root(el).querySelector('list-toolbar')!;
    const input: any = root(toolbar).querySelector('sl-input');
    input.focus();
    await userEvent.keyboard('Jane{Escape}');
    expect(el.fullscreen).toBe(true);
    expect((root(toolbar).querySelector('sl-dropdown') as any).open).toBe(
      false
    );
  });

  it('preserves unknown work state and labels normalized issue actions', () => {
    const story = new FleetStory();
    story.seedWork({
      ...activity(),
      work: [work('issue', 7, { state: null })],
    });
    const issue = story
      .graph(sampleFleetMap(), null)
      .nodes.find((n) => n.type === 'work')!;
    expect(issue.status).toBeNull();
    expect(
      workRelationLabel(
        {
          id: 'touch',
          source: 'agent:codex',
          target: issue.id,
          kind: 'touched',
          provenance: 'observed',
          impact_source: 'agent:codex',
          impact_target: issue.id,
          gaps: [],
          actions: ['issue_opened'],
        },
        issue
      )
    ).toBe('opened #7');
  });
  it('retains standalone server-confirmed activity and opted-in idle resources through facets', async () => {
    const graph = sampleFleetMap();
    graph.nodes = graph.nodes.map((n) => ({
      ...n,
      idle: n.id === 'model:sonnet',
    }));
    graph.edges = graph.edges.filter((e) => !e.target.startsWith('model:'));
    const el = await mount({ graph });
    expect(root(el).querySelector('[data-node="model:gpt"]')).not.toBeNull();
    expect(root(el).querySelector('[data-node="model:sonnet"]')).toBeNull();
    el.setIncludeIdle(true);
    await el.updateComplete;
    expect(root(el).querySelector('[data-node="model:sonnet"]')).not.toBeNull();
  });

  it('accepts bounded server image formats and falls back for other URL schemes', () => {
    for (const value of [
      'http://example.com/avatar.png',
      'https://example.com/avatar.png',
      'data:image/svg+xml,%3Csvg%3E%3C/svg%3E',
    ])
      expect(avatarSource(value)).toBe(value);
    for (const value of [
      'javascript:alert(1)',
      'data:text/html,test',
      'data:image/png,' + 'x'.repeat(200 * 1024),
    ])
      expect(avatarSource(value)).toBe('');
  });

  it('treats classified issue comments as issue work without incrementing PR counters', async () => {
    const el = await mount({ codeActivity: activity() });
    const event = {
      type: 'code_activity',
      payload: {
        event_id: 'issue-comment',
        action: 'commented',
        kind: 'issue',
        managed_agent_id: 'codex',
        work_id: 'work:issue:example/widgets#7',
        url: 'https://example.com/example/widgets/issues/7',
      },
    };
    expect(codeHitFromRealtime(event)).toBeNull();
    el.recordActivity('code_activity', event);
    await el.updateComplete;
    expect(el.codeActivity.counts).toEqual({});
    expect(
      root(el).querySelector('[data-node="work:issue:example/widgets#7"]')
    ).not.toBeNull();
    expect(
      root(el).querySelector(
        '[data-node="work:pull_request:example/widgets#7"]'
      )
    ).toBeNull();
  });

  it('keeps up to 200 work items and identifies capped work or relations', async () => {
    const code = {
      ...activity(),
      work: Array.from({ length: 201 }, (_, i) => work('issue', i + 1)),
      work_truncated: true,
    };
    const story = new FleetStory();
    story.seedWork(code);
    expect(
      story.graph(sampleFleetMap(), null).nodes.filter((n) => n.type === 'work')
    ).toHaveLength(200);
    const el = await mount({
      codeActivity: { ...activity(), relations_truncated: true },
    });
    expect(
      normalizedText(root(el).querySelector('[data-testid="code-truncated"]'))
    ).toContain('may be incomplete');
  });

  it('joins work usage after a live URL identity receives its server key', () => {
    const code = activity();
    const item = code.work[2];
    const workNode = {
      ...sampleFleetMap().nodes[0],
      type: 'work' as const,
      id: `work:pull_request:${item.url}`,
      resource_id: item.url,
      href: item.url,
      work_kind: 'pull_request' as const,
    };
    const usage = {
      active_within_minutes: null,
      since: at,
      until: at,
      partial: false,
      truncated: false,
      models: {},
      providers: {},
      work: { [item.id]: totals },
    };
    expect(nodeUsage(usage, workNode, code)).toBe(totals);
  });

  for (const width of [390, 768, 1024, 1280, 1440, 1920]) {
    it(`keeps the toolbar on one bounded line at ${width}px`, async () => {
      await page.viewport(width, 900);
      const el = await mount();
      const toolbar: any = root(el).querySelector('list-toolbar');
      const row = root(toolbar).querySelector('.list-toolbar')!;
      expect(row.getBoundingClientRect().height).toBeLessThanOrEqual(40);
      expect(row.scrollWidth).toBeLessThanOrEqual(row.clientWidth + 1);
      expect(
        root(el).querySelector('[data-testid="fleet-map-arrangement"]')
      ).not.toBeNull();
      expect(
        root(el).querySelector('[data-testid="fleet-map-filters"]') !== null
      ).toBe(width < 1280);
      const search = root(toolbar).querySelector('sl-input')!;
      expect(search.getBoundingClientRect().width).toBeLessThanOrEqual(240);
    });
  }

  it('expands narrow search without growing the bar and keeps typing connected', async () => {
    await page.viewport(390, 900);
    const el = await mount();
    const toolbar = root(el).querySelector('list-toolbar')!;
    const trigger: any = root(toolbar).querySelector(
      '[data-testid="compact-search"]'
    );
    expect(trigger).not.toBeNull();
    trigger.click();
    const input: any = root(toolbar).querySelector('sl-input');
    input.value = 'Codex';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    expect(node(el, 'agent:codex').classList.contains('match')).toBe(true);
    expect(
      root(toolbar).querySelector('.list-toolbar')!.getBoundingClientRect()
        .height
    ).toBeLessThanOrEqual(40);
  });

  it('honours idle flags, requests configured resources only on opt-in, and counts active nodes', async () => {
    const graph = sampleFleetMap();
    graph.active_within_minutes = 60;
    graph.nodes = graph.nodes.map((n) => ({ ...n, idle: n.type === 'model' }));
    const el = await mount({ graph, activityWindow: '1h' });
    expect(root(el).querySelector('[data-node="model:gpt"]')).toBeNull();
    const count = root(el).querySelector('.count')!.textContent;
    expect(count?.replace(/\s+/g, ' ')).toContain('active in this window');
    const changed = vi.fn();
    el.addEventListener('fleet-map-idle-change', changed);
    (
      root(el).querySelector('[data-testid="fleet-map-idle"]') as HTMLElement
    ).click();
    await el.updateComplete;
    expect(root(el).querySelector('[data-node="model:gpt"]')).not.toBeNull();
    expect(root(el).querySelector('.count')!.textContent).toBe(count);
    expect(changed.mock.calls[0][0].detail.includeIdle).toBe(true);
  });

  it('renders lazy round avatars and falls back to initials then the person icon', async () => {
    const graph = sampleFleetMap();
    Object.assign(graph.nodes[0], {
      avatar_url: 'https://example.com/avatar.png',
    });
    const el = await mount({ graph });
    const avatar = node(el, graph.nodes[0].id).querySelector('img')!;
    expect(avatar?.getAttribute('alt')).toBe('');
    expect(avatar.getAttribute('loading')).toBe('lazy');
    expect(avatar.getAttribute('width')).toBe('24');
    avatar.dispatchEvent(new Event('error'));
    await el.updateComplete;
    expect(node(el, graph.nodes[0].id).textContent).toContain('MC');
    graph.nodes = graph.nodes.map((n) =>
      n.id === graph.nodes[0].id ? { ...n, label: '', avatar_url: null } : n
    );
    el.graph = { ...graph };
    await el.updateComplete;
    expect(
      node(el, graph.nodes[0].id).querySelector('sl-icon[name="person"]')
    ).not.toBeNull();
  });

  it('draws each observed flow model once even when the flow already exists', () => {
    const graph = sampleFleetMap();
    const flow = {
      id: 'flow:review',
      resource_id: 'review',
      label: 'Review',
      href: '/console/flows/review',
      model_node_id: 'model:gpt',
      model_node_ids: ['model:gpt', 'model:sonnet', 'model:gpt'],
      last_active_at: new Date().toISOString(),
    };
    const first = withCodeFlows(graph, { flows: [flow] });
    const again = withCodeFlows(first, { flows: [flow] });
    expect(again.nodes.filter((n) => n.id === flow.id)).toHaveLength(1);
    expect(
      again.edges
        .filter((e) => e.source === flow.id)
        .map((e) => e.target)
        .sort()
    ).toEqual(['model:gpt', 'model:sonnet']);
  });

  it('arranges current nodes with Undo while keeping filters and refits Columns', async () => {
    await page.viewport(1440, 900);
    const el = await mount({ defaultArrangement: 'spatial' });
    el.points = { 'user:maya': { x: 1200, y: 900 } };
    el.query = 'Codex';
    await el.updateComplete;
    expect(typeof el.arrange).toBe('function');
    el.arrange();
    await el.updateComplete;
    expect(el.points).toEqual({});
    expect(el.query).toBe('Codex');
    expect(root(el).querySelector('.reset-notice')!.textContent).toContain(
      'Arranged'
    );
    el.undoResetLayout();
    await el.updateComplete;
    expect(el.points['user:maya']).toMatchObject({ x: 1200, y: 900 });
    el.setArrangement('columns');
    await el.updateComplete;
    const fit = vi.spyOn(el, 'fit');
    el.arrange();
    expect(fit).toHaveBeenCalled();
  });
});

const at = new Date().toISOString();
const work = (kind: 'issue' | 'pull_request', number: number, extra = {}) => ({
  id: `work:${kind}:example/widgets#${number}`,
  key: `example/widgets#${number}`,
  kind,
  reference: `#${number}`,
  title:
    kind === 'issue'
      ? 'Improve keyboard navigation'
      : 'Add keyboard navigation',
  repo: 'example/widgets',
  url: `https://example.com/example/widgets/${kind === 'issue' ? 'issues' : 'pull'}/${number}`,
  state: 'open' as const,
  last_action: 'opened',
  last_at: at,
  review_state: null,
  labels: [],
  related_issue: null,
  node_ids: ['agent:codex', 'user:maya'],
  ...extra,
});
function activity() {
  const issue = work('issue', 7),
    mentioned = work('issue', 8),
    pr = work('pull_request', 42);
  const relation = (kind: any, source: string, target: string, extra = {}) => ({
    id: `${kind}:${source}:${target}`,
    kind,
    source,
    target,
    at,
    provenance: 'tool_call' as const,
    ...extra,
  });
  return {
    active_within_minutes: null,
    since: at,
    truncated: false,
    counts: {},
    events: [],
    flows: [
      {
        id: 'flow:review',
        resource_id: 'review',
        label: 'PR reviewer',
        href: '/console/flows/review',
        model_node_id: 'model:gpt',
        model_node_ids: ['model:gpt', 'model:sonnet'],
        last_active_at: at,
      },
    ],
    work: [issue, mentioned, pr],
    relations: [
      relation('resolves', pr.id, issue.id),
      relation('resolves', pr.id, issue.id),
      relation('mentions', pr.id, mentioned.id),
      relation('produced', 'flow:review', pr.id),
      relation('triggered', 'flow:review', issue.id),
      relation('touched', 'agent:codex', pr.id, {
        actions: ['opened', 'commented'],
        count: 3,
      }),
      relation('touched', 'agent:missing', pr.id),
    ],
  };
}
const totals = {
  request_count: 14,
  input_tokens: 12000,
  output_tokens: null,
  cache_read_tokens: null,
  cache_write_tokens: null,
  estimated_cost: 0.0042,
  cost_is_estimate: true as const,
  currency: 'USD',
  partial: true,
  tool_calls: 23,
  approvals_requested: 3,
  approvals_approved: 2,
  approvals_declined: 1,
  prs_opened: 1,
  prs_reviewed: 2,
  prs_merged: 0,
  review_comments: 4,
  issues_touched: 3,
  prs_touched: 2,
};
const normalizedText = (el: Element | null) =>
  (el?.textContent || '').replace(/\s+/g, ' ').trim();

for (const arrangement of ['columns', 'spatial']) {
  describe(`pass 3 work and details in ${arrangement}`, () => {
    it('keeps different work relations between the same endpoints and refreshes relation-only responses', async () => {
      const history = activity();
      const el = await mount({
        codeActivity: history,
        defaultArrangement: arrangement,
      });
      const produced = history.relations.find((r) => r.kind === 'produced')!;
      el.codeActivity = {
        ...history,
        work: undefined,
        relations: [
          ...history.relations,
          {
            ...produced,
            id: 'flow-touch',
            kind: 'touched',
            actions: ['reviewed'],
            count: 2,
          },
        ],
      };
      await el.updateComplete;
      expect(
        root(el).querySelectorAll('path[data-edge="produced"]')
      ).toHaveLength(1);
      expect(
        root(el).querySelectorAll('path[data-edge="touched"]')
      ).toHaveLength(2);
    });

    it('draws deduplicated typed relations, solid resolutions and dashed mentions without lighting history', async () => {
      await page.viewport(1920, 1080);
      const el = await mount({
        codeActivity: activity(),
        defaultArrangement: arrangement,
      });
      for (const kind of [
        'resolves',
        'mentions',
        'produced',
        'triggered',
        'touched',
      ])
        expect(
          root(el).querySelectorAll(`path[data-edge="${kind}"]`),
          kind
        ).toHaveLength(1);
      expect(root(el).querySelectorAll('path.edge.active')).toHaveLength(0);
      expect(
        getComputedStyle(root(el).querySelector('path[data-edge="resolves"]')!)
          .strokeDasharray
      ).toBe('none');
      expect(
        getComputedStyle(root(el).querySelector('path[data-edge="mentions"]')!)
          .strokeDasharray
      ).not.toBe('none');
      node(el, 'work:pull_request:example/widgets#42').dispatchEvent(
        new MouseEvent('click')
      );
      await el.updateComplete;
      expect(
        root(el)
          .querySelector('path[data-edge="resolves"]')!
          .getAttribute('data-activity')
      ).toBe('resolves #7');
      expect(
        root(el)
          .querySelector('path[data-edge="touched"]')!
          .getAttribute('data-activity')
      ).toBe('opened, commented #42');
      expect(
        root(el).querySelectorAll('.activity-label').length
      ).toBeLessThanOrEqual(6);
      expect(
        root(el)
          .querySelector('path[data-edge="mentions"]')!
          .classList.contains('lit')
      ).toBe(true);
    });

    it('lights a touched edge with the live action and preserves the server work history', async () => {
      const history = activity();
      const el = await mount({
        codeActivity: history,
        defaultArrangement: arrangement,
      });
      el.recordActivity('code_activity', {
        type: 'code_activity',
        payload: {
          event_id: 'synthetic-comment',
          action: 'commented',
          managed_agent_id: 'codex',
          subject: {
            kind: 'pull_request',
            key: 'example/widgets#42',
            reference: '#42',
            repo: 'example/widgets',
          },
        },
      });
      await el.updateComplete;
      expect(el.codeActivity.work).toEqual(history.work);
      expect(el.codeActivity.relations).toEqual(history.relations);
      expect(
        root(el)
          .querySelector('path[data-edge="touched"]')!
          .getAttribute('data-activity')
      ).toContain('commented');
      expect(
        root(el).querySelectorAll('path[data-edge="touched"]')
      ).toHaveLength(1);
    });

    it('shows work title, state, repository, actors with counts and linked related objects', async () => {
      const el = await mount({
        codeActivity: activity(),
        defaultArrangement: arrangement,
      });
      node(el, 'work:pull_request:example/widgets#42').dispatchEvent(
        new MouseEvent('click')
      );
      await el.updateComplete;
      const panel = root(el).querySelector('.panel')!;
      expect(normalizedText(panel)).toContain('Add keyboard navigation');
      expect(normalizedText(panel)).toContain('Open');
      expect(normalizedText(panel)).toContain('Repository: example/widgets');
      const details = panel.querySelector(
        '[data-testid="fleet-map-work-details"]'
      )!;
      for (const text of [
        'Codex',
        'Maya Chen',
        'PR reviewer',
        '3 actions',
        'Related issues and PRs',
      ])
        expect(normalizedText(details)).toContain(text);
      expect(
        panel
          .querySelector(
            'a[href="https://example.com/example/widgets/pull/42"]'
          )!
          .getAttribute('target')
      ).toBe('_blank');
      expect(
        details.querySelector(
          'a[href="https://example.com/example/widgets/issues/7"]'
        )
      ).not.toBeNull();
      expect(panel.querySelector('[data-testid="focus-only"]')).not.toBeNull();
    });
  });
}

describe('pass 3 usage and hosts', () => {
  it('requests configured idle resources only when the caller opts in', async () => {
    localStorage.setItem('accessToken', 'synthetic-test-token');
    const fetch = vi.spyOn(window, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(sampleFleetMap()), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
    await getFleetMap(undefined, 60, true);
    expect(String(fetch.mock.calls[0][0])).toContain(
      'active_within_minutes=60&include_idle=true'
    );
    fetch.mockResolvedValue(
      new Response(JSON.stringify(sampleFleetMap()), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
    await getFleetMap(undefined, 60);
    expect(String(fetch.mock.calls[1][0])).not.toContain('include_idle');
  });

  it('keeps the same Inventory instance in a full-width Overview grid row when Map is selected', async () => {
    await page.viewport(1440, 900);
    const dashboard: any = document.createElement('dashboard-view');
    dashboard.fetchDashboardData = async () => {};
    dashboard.loadCachedDashboardData = () => {};
    dashboard.connectRealtime = () => {};
    Object.assign(dashboard, {
      isAdmin: true,
      profileUserId: 'pass3-user',
      profileResolved: true,
      featuresResolved: true,
      permissions: null,
      loading: false,
      welcomeCardDismissed: true,
    });
    document.body.append(dashboard);
    mounted.push(dashboard);
    await dashboard.updateComplete;
    const inventory: any = root(dashboard).querySelector('inventory-card');
    await inventory.updateComplete;
    inventory.loadMap = async () => {};
    inventory.map = sampleFleetMap();
    root(inventory)
      .querySelector<HTMLElement>('[data-inventory-view="map"]')!
      .click();
    await inventory.updateComplete;
    await dashboard.updateComplete;
    expect(root(dashboard).querySelector('inventory-card')).toBe(inventory);
    const grid = root(dashboard)
      .querySelector('.dashboard')!
      .getBoundingClientRect();
    const bounds = inventory.getBoundingClientRect();
    expect(bounds.width).toBeGreaterThan(grid.width - 2);
    expect(bounds.right).toBeLessThanOrEqual(1440);
    const map: any = root(inventory).querySelector('fleet-map');
    map.live = false;
    expect(map.getBoundingClientRect().height).toBeLessThanOrEqual(720);
  });

  it('shows measured usage on people, agents, flows, devices, models, providers and work with unknowns and estimated sub-cent cost', async () => {
    const codeActivity = activity();
    const usage: any = {
      active_within_minutes: null,
      since: at,
      until: at,
      partial: false,
      truncated: false,
      models: {},
      providers: {},
    };
    const nodes = [
      ['user:maya', 'people'],
      ['agent:codex', 'agents'],
      ['flow:review', 'flows'],
      ['device:mbp', 'devices'],
      ['model:gpt', 'models'],
      ['provider:openai', 'providers'],
      ['work:pull_request:example/widgets#42', 'work'],
    ];
    for (const [id, bucket] of nodes) usage[bucket] = { [id]: totals };
    const el = await mount({ codeActivity, usage });
    for (const [id] of nodes) {
      node(el, id).dispatchEvent(new MouseEvent('click'));
      await el.updateComplete;
      const block = root(el).querySelector('[data-testid="fleet-map-usage"]')!;
      const metrics = new Map(
        [...block.querySelectorAll('dt')].map((dt) => [
          dt.textContent?.trim(),
          dt.nextElementSibling!.textContent?.trim(),
        ])
      );
      expect(metrics.get('Requests')).toBe('14');
      expect(metrics.get('Tokens out')).toBe('Unknown');
      expect(metrics.get('Cost est.')).toBe('$0.0042');
      expect(metrics.get('PRs merged')).toBe('0');
      expect(metrics.get('Review comments')).toBe('4');
      expect(metrics.get('Issues touched')).toBe('3');
      expect(metrics.get('Approvals declined')).toBe('1');
      expect(
        getComputedStyle(block.querySelector('dd')!).fontVariantNumeric
      ).toBe('tabular-nums');
      expect(normalizedText(block)).toContain('Partial totals');
      expect(root(el).querySelector('.panel .actions a')).not.toBeNull();
    }
  });

  it('keeps old servers usable and never manufactures absent metrics', async () => {
    const el = await mount({
      usage: {
        active_within_minutes: null,
        since: at,
        until: at,
        partial: false,
        truncated: false,
        models: {
          'model:gpt': {
            ...totals,
            tool_calls: undefined,
            prs_opened: undefined,
          },
        },
        providers: {},
      },
    });
    node(el, 'user:maya').dispatchEvent(new MouseEvent('click'));
    await el.updateComplete;
    expect(
      normalizedText(root(el).querySelector('[data-testid="fleet-map-usage"]'))
    ).toContain('No usage reported');
    expect(root(el).querySelector('.usage-metrics')).toBeNull();
    node(el, 'model:gpt').dispatchEvent(new MouseEvent('click'));
    await el.updateComplete;
    expect(
      normalizedText(root(el).querySelector('[data-testid="fleet-map-usage"]'))
    ).not.toContain('Tool calls');
    el.activityWindow = '5m';
    await el.updateComplete;
    expect(el.usage).toBeNull();
    expect(el.codeActivity).toBeNull();
  });

  it('uses the same avatar and fallback in the panel header', async () => {
    const graph = sampleFleetMap();
    graph.nodes[0].avatar_url =
      'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRFQAAAAASUVORK5CYII=';
    const el = await mount({ graph });
    node(el, graph.nodes[0].id).dispatchEvent(new MouseEvent('click'));
    await el.updateComplete;
    const avatar = root(el).querySelector('.panel-heading img')!;
    expect(avatar.getAttribute('alt')).toBe('');
    expect(avatar.getAttribute('loading')).toBe('lazy');
    avatar.dispatchEvent(new Event('error'));
    await el.updateComplete;
    expect(
      normalizedText(root(el).querySelector('.panel-heading .glyph'))
    ).toBe('MC');
  });

  it('keeps Inventory bounded, drops table controls in Map, and covers the viewport in fullscreen', async () => {
    await page.viewport(390, 900);
    const inv: any = document.createElement('inventory-card');
    Object.assign(inv, {
      instanceAdmin: true,
      userId: 'pass3-user',
      map: sampleFleetMap(),
      mapWindow: 'any',
    });
    document.body.append(inv);
    mounted.push(inv);
    await inv.updateComplete;
    root(inv)
      .querySelector<HTMLElement>('[data-inventory-view="map"]')!
      .click();
    await inv.updateComplete;
    const el: any = root(inv).querySelector('fleet-map');
    el.live = false;
    await el.updateComplete;
    expect(root(inv).querySelector('.sort-select')).toBeNull();
    expect(root(inv).querySelector('time-range-select')).toBeNull();
    expect(inv.scrollWidth).toBeLessThanOrEqual(390);
    expect(inv.getBoundingClientRect().height).toBeLessThan(900);
    document.body.style.overflow = 'clip';
    el.fullscreen = true;
    await el.updateComplete;
    const full = el.getBoundingClientRect();
    expect(full.top).toBe(0);
    expect(full.left).toBe(0);
    expect(full.width).toBe(390);
    expect(full.height).toBe(900);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await el.updateComplete;
    expect(el.fullscreen).toBe(false);
    expect(document.body.style.overflow).toBe('clip');
    document.body.style.overflow = '';
    expect(root(inv).querySelector('fleet-map')).toBe(el);
  });

  it('uses the glide for Arrange and suppresses it under reduced motion with a six-second Undo', async () => {
    await page.viewport(1440, 900);
    const el = await mount({
      reducedMotion: false,
      defaultArrangement: 'spatial',
    });
    el.points = { 'user:maya': { x: 1500, y: 800 } };
    await el.updateComplete;
    el.arrange();
    await el.updateComplete;
    expect(el.tween).toBeDefined();
    el.undoResetLayout();
    el.reducedMotion = true;
    await el.updateComplete;
    el.arrange();
    await el.updateComplete;
    expect(el.tween).toBeUndefined();
    const clock = vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      el.resetLayout();
      await el.updateComplete;
      expect(root(el).querySelector('.reset-notice')).not.toBeNull();
      await clock.advanceTimersByTimeAsync(6000);
      await el.updateComplete;
      expect(root(el).querySelector('.reset-notice')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
