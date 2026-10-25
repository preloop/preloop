import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../../../api';
import './user-detail-view';
import {
  RECENT_SESSION_LIMIT,
  teamsForUser,
  type UserDetailView,
} from './user-detail-view';

const USER = {
  id: 'user-1',
  account_id: 'account-1',
  username: 'jane',
  email: 'jane@example.com',
  email_verified: true,
  full_name: 'Jane Doe',
  avatar_url: null,
  is_active: true,
  user_source: 'local',
  oauth_provider: null,
  last_login: '2026-10-17T12:00:00Z',
  created_at: '2026-01-02T12:00:00Z',
  updated_at: '2026-01-02T12:00:00Z',
  roles: [{ id: 'role-1', name: 'editor', team_name: null }],
  inherited_roles: [{ id: 'role-2', name: 'admin', team_name: 'Platform' }],
};

const AGENT = {
  id: 'agent-1',
  display_name: 'Research assistant',
  owner_user_id: 'user-1',
  session_source_type: 'managed_agent',
  session_source_id: 'custom_research',
  runtime_session_id: null,
  lifecycle_state: 'active',
  activity_status: 'idle',
  is_active_now: false,
  onboarding_state: 'fully_onboarded',
  live_validation_status: 'not_run',
  last_seen_at: '2026-10-17T10:00:00Z',
  last_activity_at: '2026-10-17T10:00:00Z',
};

const DEVICE = {
  device_key: 'fp:' + 'a'.repeat(64),
  hostname: 'research-box',
  os: 'linux',
  runner_state: 'online',
  last_seen_at: '2026-10-17T11:00:00Z',
  users: [{ id: 'user-1', label: 'Jane Doe', source: 'runner_registrant' }],
};

interface Options {
  empty?: boolean;
  fail?: string[];
  fleet?: boolean;
  features?: Record<string, unknown>;
  hang?: boolean;
  /** Holds the features answer until this settles. */
  holdFeatures?: Promise<void>;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Which section a request feeds, so a test can fail exactly that one. */
function sectionOf(url: string): string {
  if (url.includes('/fleet/')) return 'devices';
  if (url.includes('/teams')) return 'teams';
  if (url.includes('/runners')) return 'runners';
  if (url.includes('/runtime-sessions')) return 'sessions';
  if (url.includes('/gateway-usage/summary')) return 'usage';
  if (url.includes('/api/v1/agents')) return 'agents';
  if (url.includes('/api/v1/users/')) return 'user';
  return 'other';
}

function stubFetch(options: Options = {}): sinon.SinonStub {
  const failing = new Set(options.fail || []);
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/api/v1/features') && options.holdFeatures)
        await options.holdFeatures;
      if (url.includes('/api/v1/features'))
        return json({
          plugins: [],
          features: options.features ?? {
            user_management: true,
            fleet_coverage: options.fleet !== false,
          },
        });
      if (url.includes('/auth/users/me'))
        return json({
          id: 'user-1',
          permissions: null,
          is_account_owner: true,
        });
      const section = sectionOf(url);
      if (options.hang && section !== 'user') return new Promise(() => {});
      if (failing.has(section)) return json({ detail: 'boom' }, 500);
      const empty = options.empty;
      switch (section) {
        case 'user':
          return json(
            empty ? { ...USER, roles: [], inherited_roles: [] } : USER
          );
        case 'teams':
          if (url.includes('/members'))
            return json(
              url.includes('team-1') && !empty
                ? [{ id: 'm-1', team_id: 'team-1', user_id: 'user-1' }]
                : [{ id: 'm-2', team_id: 'team-2', user_id: 'user-9' }]
            );
          return json({
            teams: [
              { id: 'team-1', name: 'Platform', description: 'Runs the fleet' },
              { id: 'team-2', name: 'Research', description: null },
            ],
            total: 2,
          });
        case 'devices':
          return json({
            items: empty
              ? []
              : [DEVICE, { ...DEVICE, device_key: 'other', users: [] }],
            total: 1,
          });
        case 'runners':
          return json([
            {
              id: 'runner-1',
              name: 'research-box runner',
              status: 'online',
              presence: 'online',
              registered_by_user_id: empty ? 'user-9' : 'user-1',
              last_heartbeat: '2026-10-17T11:00:00Z',
            },
          ]);
        case 'agents':
          return json({
            items: empty
              ? []
              : [AGENT, { ...AGENT, id: 'agent-2', owner_user_id: 'user-9' }],
            total: 2,
          });
        case 'sessions':
          return json({
            items: empty
              ? []
              : [
                  {
                    id: 'session-1',
                    title: 'Summarize the example.com report',
                    session_source_id: 'custom_research',
                    session_reference: null,
                    agent_name: 'Research assistant',
                    started_at: '2026-10-17T09:00:00Z',
                    last_activity_at: '2026-10-17T10:00:00Z',
                    token_usage: {
                      prompt_tokens: 900,
                      completion_tokens: 300,
                      total_tokens: 1200,
                    },
                    estimated_cost: 0.0042,
                  },
                ],
            total: 1,
          });
        case 'usage':
          // B-02: the page reads the per-agent section. The session list
          // is capped at the most recent sessions and is a decoy here.
          return json({
            usage_by_session: [
              {
                agent_id: 'agent-1',
                token_usage: {
                  prompt_tokens: 10,
                  completion_tokens: 5,
                  total_tokens: 15,
                },
                estimated_cost: 0.1,
              },
            ],
            usage_by_agent: [
              {
                agent_id: 'agent-1',
                session_count: 3,
                request_count: 30,
                successful_requests: 30,
                failed_requests: 0,
                token_usage: {
                  prompt_tokens: 1000,
                  completion_tokens: 500,
                  total_tokens: 1500,
                },
                estimated_cost: 1.25,
                subscription_usage: {
                  request_count: 2,
                  prompt_tokens: 100,
                  completion_tokens: 10,
                  total_tokens: 110,
                  api_equivalent_cost: 1,
                  api_equivalent_cost_is_estimate: true,
                  covered_requests: 1,
                  coverage: 0.5,
                  billed: null,
                  billed_available: false,
                },
              },
              {
                agent_id: 'agent-2',
                session_count: 1,
                request_count: 1,
                successful_requests: 1,
                failed_requests: 0,
                token_usage: {
                  prompt_tokens: 9,
                  completion_tokens: 9,
                  total_tokens: 18,
                },
                estimated_cost: 9,
              },
            ],
          });
      }
      return json({ detail: `Unhandled ${url}` }, 500);
    });
}

async function mount(): Promise<UserDetailView> {
  const el = await fixture<UserDetailView>(
    html`<user-detail-view
      .location=${{ params: { userId: 'user-1' } }}
    ></user-detail-view>`
  );
  return el;
}

const root = (el: UserDetailView) => el.shadowRoot!;
const card = (el: UserDetailView, name: string) =>
  root(el).querySelector(`[data-testid="user-${name}"]`) as HTMLElement | null;

async function settled(el: UserDetailView): Promise<void> {
  await waitUntil(
    () =>
      root(el).querySelector('[data-testid="user-facts"]') &&
      !root(el).querySelector('sl-skeleton'),
    'sections did not settle'
  );
}

describe('user-detail-view', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('shows skeletons, not spinners, while sections load', async () => {
    fetchStub = stubFetch({ hang: true });
    const el = await mount();
    await waitUntil(() => root(el).querySelector('[data-testid="user-facts"]'));
    const skeletons = [...root(el).querySelectorAll('sl-skeleton')];
    expect(skeletons.length).to.be.greaterThan(5);
    expect(skeletons.every((s) => s.getAttribute('effect') === 'none')).to.be
      .true;
    expect(root(el).querySelector('sl-spinner')).to.not.exist;
  });

  describe('recent sessions, read last', () => {
    // An observer the test drives: `reveal()` says the card scrolled in.
    let observed: Element[];
    let reveal: () => void;
    const Original = window.IntersectionObserver;

    beforeEach(() => {
      observed = [];
      reveal = () => {};
      window.IntersectionObserver = class {
        constructor(callback: IntersectionObserverCallback) {
          reveal = () =>
            callback(
              observed.map(
                (target) =>
                  ({
                    isIntersecting: true,
                    target,
                  }) as IntersectionObserverEntry
              ),
              this as unknown as IntersectionObserver
            );
        }
        observe(target: Element) {
          observed.push(target);
        }
        unobserve() {}
        disconnect() {}
        takeRecords() {
          return [];
        }
      } as unknown as typeof IntersectionObserver;
    });

    afterEach(() => {
      window.IntersectionObserver = Original;
    });

    const sessionCalls = () =>
      fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/runtime-sessions')).length;

    it('waits for the rest of the page before asking for sessions', async () => {
      fetchStub = stubFetch({ hang: true });
      const el = await mount();
      await waitUntil(() =>
        root(el).querySelector('[data-testid="user-facts"]')
      );
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(sessionCalls()).to.equal(0);
      // Still a skeleton, so the card holds its place.
      expect(card(el, 'sessions')!.querySelector('sl-skeleton')).to.exist;
    });

    it('asks as soon as the card scrolls into view', async () => {
      fetchStub = stubFetch({ hang: true });
      const el = await mount();
      await waitUntil(() => observed.length > 0, 'the card was not observed');
      expect(observed[0]).to.equal(card(el, 'sessions'));
      reveal();
      await waitUntil(() => sessionCalls() === 1);
      reveal();
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(sessionCalls()).to.equal(1);
    });

    it('does not watch the card before user management is confirmed', async () => {
      let release!: () => void;
      const holdFeatures = new Promise<void>((resolve) => (release = resolve));
      fetchStub = stubFetch({ hang: true, holdFeatures });
      const el = await mount();
      await new Promise((resolve) => setTimeout(resolve, 50));
      reveal();
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(sessionCalls()).to.equal(0);
      release();
      await waitUntil(() => observed.length > 0, 'the card was not observed');
      expect(observed.at(-1)).to.equal(card(el, 'sessions'));
      reveal();
      await waitUntil(() => sessionCalls() === 1);
    });

    it('asks once the other sections have answered', async () => {
      fetchStub = stubFetch();
      const el = await mount();
      await settled(el);
      expect(sessionCalls()).to.equal(1);
    });
  });

  it('renders the person with every section linked', async () => {
    fetchStub = stubFetch();
    const el = await mount();
    await settled(el);

    const header = root(el).querySelector('view-header')!;
    expect(header.getAttribute('headertext')).to.equal('Jane Doe');
    expect(root(el).querySelector('user-avatar')).to.exist;
    expect(root(el).textContent).to.contain('jane@example.com');
    expect(root(el).textContent).to.contain('@jane');

    const facts = card(el, 'facts')!;
    expect(facts.textContent).to.contain('Active');
    expect(facts.textContent).to.contain('Last login');
    expect(facts.textContent).to.contain('Member since');
    expect(facts.querySelector('time')!.getAttribute('title')).to.be.ok;

    const roles = card(el, 'roles')!.textContent!;
    expect(roles).to.contain('Editor');
    expect(roles).to.contain('Direct');
    expect(roles).to.contain('From team Platform');

    const teams = card(el, 'teams')!;
    expect(teams.textContent).to.contain('Platform');
    expect(teams.textContent).to.not.contain('Research');
    expect(teams.querySelector('a')!.getAttribute('href')).to.equal(
      '/console/settings/teams'
    );

    const devices = card(el, 'devices')!.querySelectorAll('a');
    expect(devices).to.have.length(1);
    expect(devices[0].getAttribute('href')).to.contain(
      encodeURIComponent(DEVICE.device_key)
    );

    expect(
      card(el, 'runners')!.querySelector('a')!.getAttribute('href')
    ).to.equal('/console/settings/runners?runner=runner-1');

    const agentLinks = card(el, 'agents')!.querySelectorAll('a');
    expect(agentLinks).to.have.length(1);
    expect(agentLinks[0].getAttribute('href')).to.equal(
      '/console/agents/agent-1'
    );

    const session = card(el, 'sessions')!.querySelector('a')!;
    expect(session.getAttribute('href')).to.equal(
      '/console/runtime-sessions?sessionId=session-1'
    );
    // Sub-cent spend reads like the Inventory's, exact value in the title.
    expect(card(el, 'sessions')!.querySelector('td.num span[title="$0.0042"]'))
      .to.exist;

    // Spend reaches a person through the agents they own, and only those.
    const spend = root(el).querySelector('[data-testid="stat-spend"]')!;
    expect(spend.textContent).to.contain('$1.25');
    expect(spend.textContent).to.contain('$ est.');
    // D-05: agent-1 has subscription traffic, partly priced.
    const line = spend.querySelector('[data-testid="without-subscriptions"]');
    expect(line?.querySelector('summary')?.textContent?.trim()).to.equal(
      '≥ $2.25 w/o subs.'
    );
    expect(line?.textContent).to.contain('1 of 2 subscription requests');
    expect(line?.getAttribute('title')).to.contain('1 of 2');
    expect(
      root(el).querySelector('[data-testid="stat-agents"]')!.textContent
    ).to.contain('1');
  });

  it('reads the owner spend from the per-agent section of the summary', async () => {
    fetchStub = stubFetch();
    const el = await mount();
    await settled(el);
    const call = fetchStub
      .getCalls()
      .map((c) => String(c.args[0]))
      .find((url) => url.includes('/gateway-usage/summary'))!;
    const params = new URL(call, location.origin).searchParams;
    expect(params.getAll('breakdown')).to.deep.equal(['agents']);
  });

  it('asks the server for this owner’s sessions only', async () => {
    fetchStub = stubFetch();
    const el = await mount();
    await settled(el);
    const call = fetchStub
      .getCalls()
      .map((c) => String(c.args[0]))
      .find((url) => url.includes('/runtime-sessions'))!;
    const params = new URL(call, location.origin).searchParams;
    expect(params.get('owner_user_id')).to.equal('user-1');
    expect(params.get('limit')).to.equal(String(RECENT_SESSION_LIMIT));
  });

  it('says one line per empty section', async () => {
    fetchStub = stubFetch({ empty: true });
    const el = await mount();
    await settled(el);
    for (const key of [
      'roles',
      'teams',
      'devices',
      'runners',
      'agents',
      'sessions',
    ]) {
      const line = root(el).querySelector(`[data-testid="empty-${key}"]`);
      expect(line, key).to.exist;
    }
    expect(
      root(el).querySelector('[data-testid="stat-spend"]')!.textContent
    ).to.contain('$0.00');
  });

  for (const key of [
    'teams',
    'devices',
    'runners',
    'agents',
    'sessions',
    'usage',
  ]) {
    it(`keeps the page when ${key} fails, and Retry reloads it`, async () => {
      fetchStub = stubFetch({ fail: [key] });
      const el = await mount();
      await waitUntil(() =>
        root(el).querySelector(`[data-testid="error-${key}"]`)
      );
      expect(
        root(el).querySelector(`[data-testid="error-${key}"]`)!.textContent
      ).to.contain('could not be loaded');
      // The other sections still render.
      expect(card(el, 'roles')!.textContent).to.contain('Editor');
      // Spend is read through the owned agents, so it shares their failure.
      expect(root(el).querySelectorAll('[role="alert"]')).to.have.length(
        key === 'agents' ? 2 : 1
      );

      fetchStub.restore();
      fetchStub = stubFetch();
      const retry = root(el).querySelector(
        `[data-testid="error-${key}"] sl-button`
      ) as HTMLElement;
      retry.click();
      await waitUntil(
        () => !root(el).querySelector(`[data-testid="error-${key}"]`)
      );
      await settled(el);
    });
  }

  it('shows the user error with Retry when the person cannot be read', async () => {
    fetchStub = stubFetch({ fail: ['user'] });
    const el = await mount();
    await waitUntil(() => root(el).querySelector('[data-testid="user-error"]'));
    fetchStub.restore();
    fetchStub = stubFetch();
    (
      root(el).querySelector(
        '[data-testid="user-error"] sl-button'
      ) as HTMLElement
    ).click();
    await settled(el);
    expect(card(el, 'facts')).to.exist;
  });

  it('leaves Devices out for a reader without the device fleet', async () => {
    fetchStub = stubFetch({ fleet: false });
    const el = await mount();
    await settled(el);
    expect(card(el, 'devices')).to.not.exist;
    expect(card(el, 'runners')).to.exist;
  });

  it('never draws a Devices skeleton for a reader without the fleet', async () => {
    fetchStub = stubFetch({ fleet: false });
    const el = await mount();
    let sawDevices = false;
    await waitUntil(() => {
      if (card(el, 'devices')) sawDevices = true;
      return (
        root(el).querySelector('[data-testid="user-facts"]') &&
        !root(el).querySelector('sl-skeleton')
      );
    }, 'sections did not settle');
    expect(sawDevices).to.be.false;
  });

  it('finds teams past the first page, a few member reads at a time', async () => {
    const teams = Array.from({ length: 150 }, (_, i) => ({
      id: `team-${i}`,
      name: `Team ${String(i).padStart(3, '0')}`,
    }));
    let inFlight = 0;
    let peak = 0;
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      const respond = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      const page = url.match(/\/api\/v1\/teams\?skip=(\d+)&limit=(\d+)/);
      if (page) {
        const skip = Number(page[1]);
        const limit = Number(page[2]);
        return respond({
          teams: teams.slice(skip, skip + limit),
          total: teams.length,
          skip,
          limit,
        });
      }
      const members = url.match(/\/api\/v1\/teams\/(team-\d+)\/members/);
      if (members) {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((resolve) => setTimeout(resolve, 1));
        inFlight -= 1;
        return respond(
          members[1] === 'team-140' ? [{ user_id: 'user-1' }] : []
        );
      }
      return respond({});
    });

    const mine = await teamsForUser('user-1');

    expect(mine.map((team) => team.id)).to.eql(['team-140']);
    expect(peak).to.be.at.most(6);
  });

  it('says so where user management is not available', async () => {
    fetchStub = stubFetch({ features: { user_management: false } });
    const el = await mount();
    await waitUntil(() => root(el).textContent!.includes('not available'));
    expect(card(el, 'facts')).to.not.exist;
  });
});
