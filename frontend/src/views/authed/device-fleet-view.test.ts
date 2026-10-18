import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import { mockApi, type MockApi } from '../../test-helpers/capability-api';
import { invalidateApiCaches } from '../../api';
import { capabilityRoutesFor } from '../../lazy-routes';
import { hasFleetPermission } from '../../device-fleet-api';
import { sampleFleetMap } from '../../test-helpers/fleet-map-graph';
import './device-fleet-view';

const device = {
  device_key: 'fp:' + 'a'.repeat(64),
  hostname: 'example-workstation',
  os: 'linux',
  runner_state: 'online',
  collection_state: 'none',
  inventory_state: 'fresh',
  roster_state: 'not_on_roster',
  verification: 'unverified',
  last_seen_at: '2026-10-16T12:00:00Z',
  agents_onboarded: 1,
  agents_not_onboarded: 1,
  users: [
    { id: 'user-1', label: 'Jane Doe', source: 'runner_registrant' },
    { id: 'user-1', label: 'Jane Doe', source: 'agent_owner' },
    { id: null, label: 'roster-person@example.com', source: 'roster' },
  ],
  runners: [{ id: 'runner-1', name: 'Example runner', state: 'online' }],
  agents: [
    {
      id: 'candidate-1',
      kind: 'codex',
      name: null,
      status: 'new',
      managed_agent_id: null,
      first_seen_at: '2026-10-15T12:00:00Z',
      last_seen_at: '2026-10-16T12:00:00Z',
      source: 'candidate',
    },
    {
      id: 'inventory-1',
      kind: 'claude-code',
      name: 'Example agent',
      status: 'onboarded',
      managed_agent_id: 'agent-1',
      first_seen_at: '2026-10-15T12:00:00Z',
      last_seen_at: '2026-10-16T12:00:00Z',
      source: 'inventory',
    },
    {
      id: 'managed-1',
      kind: 'claude-code',
      name: 'Example agent',
      status: 'onboarded',
      managed_agent_id: 'agent-1',
      first_seen_at: '2026-10-15T12:00:00Z',
      last_seen_at: '2026-10-16T12:00:00Z',
      source: 'managed_agent',
    },
  ],
  possible_same_machine_as: [],
  findings: [
    {
      id: 'finding-1',
      rule: 'discovered_not_onboarded',
      device_key: 'fp:' + 'a'.repeat(64),
      user_id: null,
      state: 'open',
      opened_at: '2026-10-16T12:00:00Z',
      last_evaluated_at: '2026-10-16T12:00:00Z',
      evidence: {},
      limitation: 'Self-reported.',
      title: 'Discovered agent not onboarded',
      detail: 'codex has not been onboarded.',
      href: '/console/settings/device-fleet',
    },
  ],
};
const settings = {
  schema_version: 1,
  rules: Object.fromEntries(
    ['traffic_without_runner', 'discovered_not_onboarded'].map((rule) => [
      rule,
      {
        enabled: false,
        channels: {
          attention: true,
          email: false,
          push: false,
          webhook: false,
        },
        window_minutes: 60,
        grace_minutes: 30,
        not_onboarded_days: 3,
      },
    ])
  ),
};

describe('Device fleet', () => {
  let api: MockApi;
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-token');
  });
  afterEach(() => {
    api?.restore();
    invalidateApiCaches();
    localStorage.removeItem('accessToken');
    history.replaceState({}, '', '/');
  });
  function setup(
    enabled = true,
    permissions: string[] = ['view_device_fleet', 'manage_device_fleet']
  ) {
    api = mockApi({
      capabilities: enabled ? ['fleet_coverage'] : [],
      profile: { permissions },
      routes: [
        {
          path: '/api/v1/fleet/device-fleet',
          body: {
            schema_version: 1,
            items: [device],
            total: 1,
            truncated: false,
            summary: {
              devices: 1,
              with_runner_online: 1,
              with_ungoverned_agents: 1,
              expected_missing: 0,
              has_roster: false,
            },
            unassigned_agents: [],
          },
        },
        {
          path: '/api/v1/fleet/device-fleet/detail',
          body: { schema_version: 1, device, observations: [] },
        },
        { path: '/api/v1/fleet/settings', body: settings },
        {
          path: '/api/v1/fleet/settings',
          method: 'PUT',
          body: (call) => call.body,
        },
      ],
    });
  }
  async function mount() {
    const view = await fixture<any>(
      html`<device-fleet-view></device-fleet-view>`
    );
    await waitUntil(() => !view.loading);
    await view.updateComplete;
    return view;
  }
  it('registers only with the fleet capability', () => {
    expect(
      capabilityRoutesFor(new Set()).some(
        (r) => r.path === 'settings/device-fleet'
      )
    ).to.equal(false);
    expect(
      capabilityRoutesFor(new Set(['fleet_coverage'])).map((r) => r.path)
    ).to.eql(['settings/device-fleet']);
  });
  it('without RBAC permits only the selected account owner', () => {
    expect(
      hasFleetPermission({ permissions: null }, 'view_device_fleet')
    ).to.equal(false);
    expect(
      hasFleetPermission(
        { permissions: null, is_account_owner: true },
        'view_device_fleet'
      )
    ).to.equal(true);
  });
  it('makes no fleet requests without capability or permission', async () => {
    setup(false);
    let view = await mount();
    expect(view.shadowRoot.textContent).not.to.include('Device fleet');
    expect(api.callsTo(/\/fleet\//)).to.have.length(0);
    view.remove();
    api.restore();
    invalidateApiCaches();
    setup(true, ['view_flows']);
    view = await mount();
    expect(api.callsTo(/\/fleet\//)).to.have.length(0);
  });
  it('shows a runner device with no roster and independent evidence states', async () => {
    setup();
    const view = await mount();
    const text = view.shadowRoot.textContent.replace(/\s+/g, ' ');
    expect(text).to.include('example-workstation');
    expect(text).to.include('Jane Doe');
    expect(text).to.include('1 governed');
    expect(text).to.include('1 not onboarded');
    expect(text).to.include('1 open');
    expect(text).not.to.include('Expected, never seen');
    expect(text).to.include('not attestation');
    const stat = (id: string) =>
      view.shadowRoot
        .querySelector(`[data-testid="${id}"] .stat-number`)
        .textContent.trim();
    expect(stat('stat-devices')).to.equal('1');
    expect(stat('stat-ungoverned')).to.equal('1');
    expect(stat('stat-runners-offline')).to.equal('0');
    expect(stat('stat-findings')).to.equal('1');
    expect(view.shadowRoot.querySelector('a[href*="user=user-1"]')).to.exist;
  });

  it('says how to add the first device when the fleet is empty', async () => {
    setup();
    api.restore();
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [
        {
          path: '/api/v1/fleet/device-fleet',
          body: {
            schema_version: 1,
            items: [],
            total: 0,
            truncated: false,
            summary: {
              devices: 0,
              with_runner_online: 0,
              with_ungoverned_agents: 0,
              expected_missing: 0,
              has_roster: false,
            },
            unassigned_agents: [],
          },
        },
      ],
    });
    const view = await mount();
    const empty = view.shadowRoot.querySelector('[data-testid="fleet-empty"]');
    expect(empty.textContent).to.include('preloop runner fg');
  });

  it('gives first-device guidance, not a filter miss, when only unassigned agents exist', async () => {
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [
        {
          path: '/api/v1/fleet/device-fleet',
          body: {
            schema_version: 1,
            items: [],
            total: 0,
            truncated: false,
            summary: {
              devices: 0,
              with_runner_online: 0,
              with_ungoverned_agents: 0,
              expected_missing: 0,
              has_roster: false,
            },
            unassigned_agents: [
              {
                id: 'managed-9',
                kind: 'codex',
                name: 'Example agent',
                status: 'onboarded',
                managed_agent_id: 'agent-9',
                first_seen_at: null,
                last_seen_at: null,
              },
            ],
          },
        },
      ],
    });
    const view = await mount();
    const root = view.shadowRoot;
    expect(
      root.querySelector('[data-testid="fleet-empty"]').textContent
    ).to.include('preloop runner fg');
    expect(root.textContent).not.to.include('No devices match');
    expect(root.querySelector('a[href="/console/agents/agent-9"]')).to.exist;
  });

  it('shows a retry when the fleet fails to load', async () => {
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [{ path: '/api/v1/fleet/device-fleet', status: 500, body: {} }],
    });
    const view = await mount();
    const alert = view.shadowRoot.querySelector('sl-alert[role="alert"]');
    expect(alert.textContent).to.include('Retry to refresh it');
    expect(alert.querySelector('sl-button')).to.exist;
  });

  it('links people, runners and governed agents on the device page', async () => {
    history.replaceState(
      {},
      '',
      '?device=' + encodeURIComponent(device.device_key)
    );
    setup();
    const view = await mount();
    const root = view.shadowRoot;
    expect(api.callsTo('/api/v1/fleet/device-fleet')).to.have.length(0);
    expect(root.textContent).to.include('Unverified');
    expect(
      root.querySelector('[data-testid="device-people"] a[href*="user=user-1"]')
        .textContent
    ).to.include('Jane Doe');
    const people = root.querySelector('[data-testid="device-people"]');
    expect(people.querySelectorAll('a[href*="user=user-1"]')).to.have.length(1);
    expect(people.textContent.replace(/\s+/g, ' ')).to.include(
      'Registered the runner · Owns an agent here'
    );
    // A roster entry with no account user is named, not linked.
    expect(people.textContent).to.include('roster-person@example.com');
    expect(people.querySelectorAll('a')).to.have.length(1);
    const governed = root.querySelectorAll(
      '[data-testid="device-governed"] a[href="/console/agents/agent-1"]'
    );
    expect(governed).to.have.length(1);
    const ungoverned = root.querySelector('[data-testid="device-ungoverned"]');
    expect(
      ungoverned.querySelector('sl-copy-button.onboard').getAttribute('value')
    ).to.equal('preloop agents onboard codex');
    expect(
      root.querySelector('[data-testid="device-findings"]').textContent
    ).to.include('Discovered agent not onboarded');
  });
  it('reads detail by encoded device key and links its runner', async () => {
    history.replaceState(
      {},
      '',
      '?device=' + encodeURIComponent(device.device_key)
    );
    setup();
    const view = await mount();
    expect(
      api.callsTo('/api/v1/fleet/device-fleet/detail')[0].search
    ).to.include('device_key=fp%3A');
    expect(view.shadowRoot.querySelector('a[href*="runner=runner-1"]')).to
      .exist;
  });
  it('reloads detail and settings on same-route query navigation', async () => {
    setup();
    const view = await mount();
    view.onBeforeEnter({
      search: '?device=' + encodeURIComponent(device.device_key),
    });
    await waitUntil(() => !view.loading);
    await view.updateComplete;
    expect(view.shadowRoot.querySelector('a[href*="runner=runner-1"]')).to
      .exist;
    view.onBeforeEnter({ search: '?tab=settings' });
    await waitUntil(() => !view.loading);
    await view.updateComplete;
    expect(view.shadowRoot.querySelector('form')).to.exist;
  });

  it('falls back to devices for a view-only settings URL', async () => {
    history.replaceState({}, '', '?tab=settings');
    setup(true, ['view_device_fleet']);
    const view = await mount();
    expect(view.shadowRoot.textContent).to.include('example-workstation');
    expect(view.shadowRoot.querySelector('form')).not.to.exist;
    expect(api.callsTo('/api/v1/fleet/settings')).to.have.length(0);
    view.onBeforeEnter({ search: '?tab=settings' });
    await waitUntil(() => !view.loading);
    await view.updateComplete;
    expect(view.shadowRoot.textContent).to.include('example-workstation');
  });

  it('saves complete opt-in settings using the shared wire contract', async () => {
    history.replaceState({}, '', '?tab=settings');
    setup();
    const view = await mount();
    const checkbox = view.shadowRoot.querySelector(
      'input[type=checkbox]'
    ) as HTMLInputElement;
    expect(checkbox.checked).to.equal(false);
    checkbox.click();
    view.shadowRoot
      .querySelector('form')
      .dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(
      () => api.callsTo('/api/v1/fleet/settings', 'PUT').length === 1
    );
    const body = api.callsTo('/api/v1/fleet/settings', 'PUT')[0]
      .body as typeof settings;
    expect(body.schema_version).to.equal(1);
    expect(body.rules.traffic_without_runner.enabled).to.equal(true);
    expect(body.rules.traffic_without_runner.channels.email).to.equal(false);
  });

  it('switches to the map, loads it once, and remembers the choice', async () => {
    localStorage.removeItem('preloop.device_fleet.view_mode');
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [
        {
          path: '/api/v1/fleet/device-fleet',
          body: {
            items: [device],
            total: 1,
            truncated: false,
            summary: {
              devices: 1,
              with_runner_online: 1,
              with_ungoverned_agents: 1,
              expected_missing: 0,
              has_roster: false,
            },
            unassigned_agents: [],
          },
        },
        { path: '/api/v1/fleet/map', body: sampleFleetMap() },
      ],
    });
    const view = await mount();
    expect(view.shadowRoot.querySelector('table')).to.exist;
    expect(api.callsTo('/api/v1/fleet/map').length).to.equal(0);
    const mapButton = [
      ...view.shadowRoot.querySelectorAll('.view-switch button'),
    ].find((b: Element) => b.textContent!.includes('Map')) as HTMLButtonElement;
    mapButton.click();
    await waitUntil(() => view.shadowRoot.querySelector('fleet-map')?.graph);
    expect(view.shadowRoot.querySelector('table')).to.not.exist;
    const pressed = view.shadowRoot.querySelector(
      '.view-switch [aria-pressed="true"]'
    );
    expect(pressed.textContent).to.include('Map');
    expect(localStorage.getItem('preloop.device_fleet.view_mode')).to.equal(
      'canvas'
    );
    expect(api.callsTo('/api/v1/fleet/map').length).to.equal(1);
    localStorage.removeItem('preloop.device_fleet.view_mode');
  });

  it('offers retry and the table when the map fails', async () => {
    localStorage.setItem('preloop.device_fleet.view_mode', 'canvas');
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [
        {
          path: '/api/v1/fleet/device-fleet',
          body: {
            items: [device],
            total: 1,
            truncated: false,
            summary: {
              devices: 1,
              with_runner_online: 1,
              with_ungoverned_agents: 1,
              expected_missing: 0,
              has_roster: false,
            },
            unassigned_agents: [],
          },
        },
        { path: '/api/v1/fleet/map', status: 500, body: {} },
      ],
    });
    const view = await mount();
    await waitUntil(() => !view.mapLoading);
    await view.updateComplete;
    const text = view.shadowRoot.textContent.replace(/\s+/g, ' ');
    expect(text).to.include('Could not load the fleet map');
    expect(text).to.include('Back to the table');
    localStorage.removeItem('preloop.device_fleet.view_mode');
  });
});
