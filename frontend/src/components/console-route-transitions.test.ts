/**
 * The console's route-transition matrix.
 *
 * Every console section is entered by clicking a link, checked for the element
 * and params the route table promises, left through each of its own "Back to"
 * links, and walked with the browser's Back and Forward buttons. It runs the
 * real table from `lit-app` against the real router, in a real browser, with
 * the API stubbed, because #499 broke navigation in a way no view test could
 * see: the URL was right, the params were right, and the wrong element was on
 * screen.
 */
import { expect, fixtureCleanup, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import './lit-app';

/** A route as the table declares it and as the console must render it. */
interface Landing {
  /** Path a link points at. */
  path: string;
  /** Where the router settles, when a redirect moves it. */
  url?: string;
  /** Custom element that must end up under the shell. */
  tag: string;
  /** `location.params` of that element, when the route takes any. */
  params?: Record<string, string>;
  /** Views that render outside <console-shell> (the catch-all). */
  atOutlet?: boolean;
  /** Routes whose component no module defines yet. */
  undefinedElement?: boolean;
}

/** Every console section, in the order the matrix walks them. */
const MATRIX: Landing[] = [
  { path: '/console', tag: 'dashboard-view', params: {} },
  { path: '/console/agents', tag: 'agents-view', params: {} },
  {
    path: '/console/agents/agent-1',
    tag: 'agent-detail-view',
    params: { agentId: 'agent-1' },
  },
  {
    path: '/console/agents/agent-1/talk',
    tag: 'agent-talk-view',
    params: { agentId: 'agent-1' },
  },
  { path: '/console/flows', tag: 'flows-view', params: {} },
  { path: '/console/flows/new', tag: 'flow-view', params: {} },
  {
    path: '/console/flows/flow-1',
    tag: 'flow-view',
    params: { flowId: 'flow-1' },
  },
  {
    path: '/console/flows/executions',
    tag: 'flow-executions-view',
    params: {},
  },
  {
    path: '/console/flows/executions/exec-1',
    tag: 'flow-execution-view',
    params: { executionId: 'exec-1' },
  },
  { path: '/console/approvals', tag: 'approvals-view', params: {} },
  {
    path: '/console/approval/appr-1',
    tag: 'approval-view',
    params: { requestId: 'appr-1' },
  },
  // The bare /console/runners route only exists to redirect.
  {
    path: '/console/runners',
    url: '/console/settings/runners',
    tag: 'runners-view',
  },
  { path: '/console/tools', tag: 'tools-view', params: {} },
  { path: '/console/ai-models', tag: 'ai-models-view', params: {} },
  {
    path: '/console/ai-models/model-1',
    tag: 'ai-model-detail-view',
    params: { modelId: 'model-1' },
  },
  { path: '/console/settings/api-keys', tag: 'api-keys-view', params: {} },
  {
    path: '/console/settings/api-keys/key-1',
    tag: 'api-key-view',
    params: { keyId: 'key-1' },
  },
  { path: '/console/policies', tag: 'policies-view', params: {} },
  { path: '/console/trackers', tag: 'trackers-view', params: {} },
  {
    path: '/console/trackers/tracker-1',
    tag: 'tracker-detail-view',
    params: { trackerId: 'tracker-1' },
  },
  {
    path: '/console/trackers/tracker-1/issues/issue-1',
    tag: 'tracker-issue-view',
    params: { trackerId: 'tracker-1', issueId: 'issue-1' },
  },
  {
    path: '/console/runtime-sessions',
    tag: 'runtime-sessions-view',
    params: {},
  },
  { path: '/console/audit', tag: 'audit-view', params: {} },
  { path: '/console/attention', tag: 'attention-view', params: {} },
  { path: '/console/cost', tag: 'cost-view', params: {} },
  { path: '/console/api-usage', tag: 'api-usage-view', params: {} },
  { path: '/console/issues', tag: 'issues-view', params: {} },
  // Settings pages, including the group's own redirect to profile.
  {
    path: '/console/settings',
    url: '/console/settings/profile',
    tag: 'profile-view',
  },
  { path: '/console/settings/security', tag: 'security-view', params: {} },
  { path: '/console/settings/appearance', tag: 'appearance-view', params: {} },
  { path: '/console/settings/account', tag: 'account-view', params: {} },
  { path: '/console/settings/users', tag: 'user-management-view', params: {} },
  { path: '/console/settings/teams', tag: 'team-management-view', params: {} },
  {
    path: '/console/settings/invitations',
    tag: 'invitation-management-view',
    params: {},
  },
  {
    path: '/console/settings/notification-preferences',
    tag: 'notification-preferences-view',
    params: {},
  },
  // Declared in the table with a component no module defines. The router still
  // has to attach the element the table names; the missing module is tracked
  // separately, and this row is here so the day it lands the route works.
  { path: '/console/pricing', tag: 'pricing-view', undefinedElement: true },
  { path: '/console/authorize', tag: 'oauth-consent-view', params: {} },
  {
    path: '/console/governance',
    url: '/console/policies',
    tag: 'policies-view',
  },
  {
    path: '/console/does-not-exist',
    tag: 'not-found-view',
    atOutlet: true,
  },
];

const BY_PATH = new Map(MATRIX.map((entry) => [entry.path, entry]));

/** The console's route groups, where a parent route owns no component. */
const FLOWS_GROUP = [
  '/console/flows',
  '/console/flows/new',
  '/console/flows/flow-1',
  '/console/flows/executions',
  '/console/flows/executions/exec-1',
];
const TRACKERS_GROUP = [
  '/console/trackers',
  '/console/trackers/tracker-1',
  '/console/trackers/tracker-1/issues/issue-1',
];

type RoutedElement = HTMLElement & {
  location?: { params: Record<string, string> };
};

const FLOW = {
  id: 'flow-1',
  name: 'Nightly triage',
  description: 'A flow',
  enabled: true,
  agent_id: 'agent-1',
  trigger_type: 'schedule',
  trigger_config: {},
  steps: [],
  created_at: '2026-09-01T10:00:00Z',
  updated_at: '2026-09-01T10:00:00Z',
};
const EXECUTION = {
  id: 'exec-1',
  flow_id: 'flow-1',
  flow_name: 'Nightly triage',
  status: 'completed',
  trigger_type: 'schedule',
  created_at: '2026-09-02T10:00:00Z',
  started_at: '2026-09-02T10:00:00Z',
  completed_at: '2026-09-02T10:05:00Z',
  steps: [],
  logs: [],
};
const APPROVAL = {
  id: 'appr-1',
  status: 'pending',
  action_type: 'tool_call',
  tool_name: 'shell',
  agent_id: 'agent-1',
  agent_name: 'Agent One',
  risk_level: 'high',
  requested_at: '2026-09-03T10:00:00Z',
  expires_at: '2026-12-03T10:00:00Z',
  request_data: {},
  arguments: {},
};
const AGENT = {
  id: 'agent-1',
  name: 'Agent One',
  agent_type: 'claude_code',
  status: 'active',
  created_at: '2026-09-01T10:00:00Z',
};
const TRACKER = {
  id: 'tracker-1',
  name: 'Repo',
  tracker_type: 'github',
  status: 'connected',
  created_at: '2026-09-01T10:00:00Z',
};

/** One stub for every view the matrix visits. Shapes, not fidelity. */
function stubbedBody(url: string): unknown {
  const path = new URL(url, window.location.origin).pathname;
  if (path.endsWith('/api/v1/features')) return { features: {}, plugins: [] };
  if (path.endsWith('/api/v1/auth/users/me')) {
    return {
      username: 'founder',
      email: 'founder@example.com',
      email_verified: true,
      permissions: null,
      is_superuser: true,
    };
  }
  if (/\/api\/v1\/flows\/executions\/[^/]+\/logs/u.test(path)) return [];
  if (/\/api\/v1\/flows\/executions\/[^/]+$/u.test(path)) return EXECUTION;
  if (path.includes('/api/v1/flows/executions')) return [EXECUTION];
  if (/\/api\/v1\/flows\/[^/]+\/executions/u.test(path)) return [EXECUTION];
  if (/\/api\/v1\/flows\/[^/]+$/u.test(path)) return FLOW;
  if (path.endsWith('/api/v1/flows')) return [FLOW];
  if (/\/api\/v1\/approval-requests\/[^/]+\/history$/u.test(path)) return [];
  if (/\/api\/v1\/approval-requests\/[^/]+$/u.test(path)) return APPROVAL;
  if (path.includes('/api/v1/approval-requests')) return [APPROVAL];
  if (/\/api\/v1\/agents\/[^/]+$/u.test(path)) return AGENT;
  if (path.includes('/api/v1/agents')) return [AGENT];
  if (/\/api\/v1\/trackers\/[^/]+$/u.test(path)) return TRACKER;
  if (path.includes('/api/v1/trackers')) return [TRACKER];
  // Everything else: a list where a list is plausible, an object otherwise.
  if (
    /(models|keys|policies|tools|issues|projects|sessions|logs|events|users|teams|invitations|rules|servers|bypasses|workflows|providers|plans|budgets)/u.test(
      path
    )
  ) {
    return [];
  }
  // Spend and usage pages read nested totals off their summary payloads.
  const zeroTokens = {
    total_tokens: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_tokens: 0,
    reasoning_tokens: 0,
  };
  if (/(summary|usage|overview|cost)/u.test(path)) {
    return {
      token_usage: zeroTokens,
      ...zeroTokens,
      total_cost: 0,
      total_requests: 0,
      items: [],
      series: [],
    };
  }
  return {};
}

describe('console route transitions', () => {
  let app: HTMLElement;
  let fetchStub: sinon.SinonStub;
  const startUrl = window.location.pathname + window.location.search;

  const outlet = () => app.shadowRoot!.querySelector('main')!;
  const shell = () => outlet().querySelector('console-shell');

  /** The deepest element the router attached, whatever it nested it under. */
  const deepest = (): RoutedElement | null => {
    let node: Element | null = outlet().firstElementChild;
    let last: Element | null = null;
    while (node) {
      last = node;
      node = node.firstElementChild;
    }
    return last as RoutedElement | null;
  };

  /** Click a link, exactly as a view's own anchor would be clicked. */
  const clickLink = (href: string): void => {
    const anchor = document.createElement('a');
    anchor.href = href;
    anchor.textContent = href;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  };

  /** Assert the console is showing what the table promises for `entry`. */
  async function assertLanded(entry: Landing, how: string): Promise<void> {
    const url = entry.url ?? entry.path;
    await waitUntil(
      () => window.location.pathname === url,
      `${how}: expected the URL to be ${url}, got ${window.location.pathname}`,
      { timeout: 10000 }
    );
    const selector = entry.atOutlet
      ? `main > ${entry.tag}`
      : `console-shell > ${entry.tag}`;
    await waitUntil(
      () =>
        Boolean(
          entry.atOutlet
            ? outlet().firstElementChild?.localName === entry.tag
            : shell()?.querySelector(`:scope > ${entry.tag}`)
        ),
      `${how}: expected ${selector} at ${url}, got ` +
        `${outlet().firstElementChild?.localName ?? 'nothing'} > ` +
        `${outlet().firstElementChild?.firstElementChild?.localName ?? '-'}`,
      { timeout: 10000 }
    );

    const view = deepest()!;
    expect(view.localName, `${how}: rendered element at ${url}`).to.equal(
      entry.tag
    );
    // The routed view is the shell's only child, and nothing hangs off it.
    // A view nested inside the view it was reached from is exactly the #499
    // regression: right URL, right params, wrong page on screen.
    if (!entry.atOutlet) {
      expect(
        shell()!.children.length,
        `${how}: views under the shell`
      ).to.equal(1);
    }
    expect(
      view.firstElementChild,
      `${how}: ${entry.tag} must not host another routed view`
    ).to.equal(null);
    if (entry.params) {
      expect(view.location?.params, `${how}: params at ${url}`).to.deep.equal(
        entry.params
      );
    }
    if (entry.undefinedElement) {
      expect(customElements.get(entry.tag)).to.equal(undefined);
    }
  }

  /**
   * Every way out of a view that leads to another section in the matrix: the
   * "Back to ..." buttons, the breadcrumbs, the "All executions" link. Only
   * the view's own shadow root is searched, so the set is bounded by the page
   * under test.
   */
  function exitLinks(view: Element): { href: string; click: () => void }[] {
    const root = (view as HTMLElement & { shadowRoot: ShadowRoot | null })
      .shadowRoot;
    if (!root) return [];
    const found = new Map<string, { href: string; click: () => void }>();
    const keep = (href: string | null, click: () => void) => {
      if (!href || !BY_PATH.has(href) || found.has(href)) return;
      found.set(href, { href, click });
    };
    for (const anchor of root.querySelectorAll('a[href^="/console"]')) {
      keep(anchor.getAttribute('href'), () =>
        (anchor as HTMLAnchorElement).click()
      );
    }
    // Shoelace buttons carry the href on the host and render the anchor in
    // their own shadow root, which is the node a user's click starts on.
    for (const button of root.querySelectorAll('sl-button[href^="/console"]')) {
      const inner = (button as HTMLElement).shadowRoot?.querySelector('a');
      if (!inner) continue;
      keep(button.getAttribute('href'), () => inner.click());
    }
    return [...found.values()];
  }

  before(async () => {
    invalidateApiCaches();
    (window as Window & { BRAND_CONFIG?: unknown }).BRAND_CONFIG = {
      name: 'Preloop',
      domain: 'preloop.ai',
      company: { legal_name: 'Preloop', address: '', city: '' },
      branding: {
        logo_light: '/logo.svg',
        logo_dark: '/logo-dark.svg',
        favicon: '/favicon.ico',
        primary_color: '#000',
        gradient_product: '',
        gradient_ai: '',
      },
      social: { twitter: '', linkedin: '', instagram: '' },
    };
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      return new Response(JSON.stringify(stubbedBody(url)), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    window.history.replaceState({}, '', '/console');
    app = document.createElement('lit-app');
    document.body.append(app);
    await assertLanded(BY_PATH.get('/console')!, 'first load of /console');
  });

  after(() => {
    app.remove();
    fixtureCleanup();
    fetchStub.restore();
    localStorage.clear();
    delete (window as Window & { BRAND_CONFIG?: unknown }).BRAND_CONFIG;
    window.history.replaceState({}, '', startUrl);
  });

  it('renders every section entered by a link click, and back and forward again', async () => {
    let previous = BY_PATH.get('/console')!;
    for (const entry of MATRIX) {
      if (entry.path === '/console') continue;
      clickLink(entry.path);
      await assertLanded(entry, `click ${previous.path} -> ${entry.path}`);

      window.history.back();
      await assertLanded(previous, `back from ${entry.path}`);

      window.history.forward();
      await assertLanded(entry, `forward to ${entry.path}`);
      previous = entry;
    }
  });

  it('renders every ordered pair inside the flows and trackers groups', async () => {
    // The route groups are where #499 went wrong: `flows` and `trackers` have
    // children but no component, so their level in the chain owns no element.
    for (const group of [FLOWS_GROUP, TRACKERS_GROUP]) {
      for (const from of group) {
        for (const to of group) {
          if (from === to) continue;
          clickLink(from);
          await assertLanded(BY_PATH.get(from)!, `setup ${from}`);
          clickLink(to);
          await assertLanded(BY_PATH.get(to)!, `${from} -> ${to}`);
        }
      }
    }
  });

  it('connects a view against the URL it navigated to', async () => {
    clickLink('/console/approvals');
    await assertLanded(BY_PATH.get('/console/approvals')!, 'visit approvals');

    const approval = BY_PATH.get('/console/approval/appr-1')!;
    clickLink(approval.path);
    await assertLanded(approval, 'open an approval');

    // <approval-view> takes its request id out of window.location in
    // connectedCallback. Connecting it before the URL moved left the id empty
    // and turned every approval link into a 404.
    const view = deepest() as HTMLElement & { requestId?: string };
    expect(view.requestId).to.equal('appr-1');
  });

  it('follows every "Back to ..." link and breadcrumb a section renders', async () => {
    const clicked: string[] = [];
    for (const entry of MATRIX) {
      if (entry.atOutlet) continue;
      clickLink(entry.path);
      await assertLanded(entry, `visit ${entry.path}`);
      const links = exitLinks(deepest()!);
      for (let index = 0; index < links.length; index++) {
        // The view is rebuilt on the way back in, so re-read its links.
        const fresh = exitLinks(deepest()!)[index];
        if (!fresh) break;
        const target = BY_PATH.get(fresh.href)!;
        fresh.click();
        await assertLanded(target, `${entry.path}: exit link ${fresh.href}`);
        clicked.push(`${entry.path} -> ${fresh.href}`);
        clickLink(entry.path);
        await assertLanded(entry, `return to ${entry.path}`);
      }
    }
    // The founder's report was that these do nothing. If a view stops
    // rendering one, this test quietly stops covering it, so the count is
    // part of the assertion.
    expect(
      clicked.length,
      `section exit links exercised: ${clicked.join(', ')}`
    ).to.be.greaterThan(14);
  });
});
