// Capture the runner session before route tests replace the page URL.
import { setViewport } from '@web/test-runner-commands';
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
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
          path: '/api/v1/fleet/roster',
          body: { schema_version: 1, total: 2, items: [] },
        },
        {
          path: '/api/v1/fleet/roster/csv',
          method: 'PUT',
          body: { schema_version: 1, total: 1, items: [] },
        },
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
  async function chooseRoster(
    view: any,
    file = new File(
      ['device_ref,platform\nexample-device,linux\n'],
      'roster.csv'
    )
  ) {
    const input = view.shadowRoot.querySelector(
      '#roster-file'
    ) as HTMLInputElement;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await view.updateComplete;
    return file;
  }
  it('combines governance and roster filters and clears both', async () => {
    setup();
    const view = await mount();
    const root = view.shadowRoot;
    const change = async (id: string, value: string) => {
      const select = root.querySelector(id);
      select.value = value;
      select.dispatchEvent(new Event('change'));
      await view.updateComplete;
    };
    await change('#governance-filter', 'ungoverned');
    expect(root.querySelectorAll('tbody tr')).to.have.length(1);
    await change('#roster-filter', 'expected_missing');
    expect(root.textContent).to.include('No devices match');
    root.querySelector('.empty-state sl-button').click();
    await view.updateComplete;
    expect(root.querySelector('#governance-filter').value).to.equal('');
    expect(root.querySelector('#roster-filter').value).to.equal('');
    expect(root.querySelectorAll('tbody tr')).to.have.length(1);
    await change('#governance-filter', 'none');
    expect(root.querySelectorAll('tbody tr')).to.have.length(0);
  });
  it('gives detail copy controls slotted accessible names', async () => {
    history.replaceState(
      {},
      '',
      '?device=' + encodeURIComponent(device.device_key)
    );
    setup();
    const view = await mount();
    for (const copy of view.shadowRoot.querySelectorAll('sl-copy-button')) {
      await copy.updateComplete;
      expect(
        copy.querySelector('[slot="copy-icon"] .sr-only')?.textContent.trim()
      ).to.match(/^Copy /);
    }
  });
  it('provides a native expandable limits disclosure', async () => {
    setup();
    const view = await mount();
    const details = view.shadowRoot.querySelector(
      '[data-testid="fleet-limits"] details'
    );
    expect(details.open).to.equal(false);
    expect(details.querySelector('summary').textContent).to.include(
      'What this page can and cannot prove'
    );
    details.querySelector('summary').click();
    expect(details.open).to.equal(true);
  });
  it('confirms roster replacement and sends multipart with private defaults', async () => {
    history.replaceState({}, '', '?tab=roster');
    setup();
    const view = await mount();
    expect(api.callsTo('/api/v1/fleet/device-fleet')).to.have.length(0);
    expect(view.shadowRoot.textContent.replace(/\s+/g, ' ')).to.include(
      '2 devices'
    );
    const file = await chooseRoster(view);
    expect(view.shadowRoot.querySelector('#replace-roster').disabled).to.equal(
      true
    );
    expect(view.shadowRoot.textContent).to.include('roster.csv');
    expect(view.shadowRoot.textContent).to.include('omitted');
    expect(api.callsTo('/api/v1/fleet/roster/csv', 'PUT')).to.have.length(0);
    const confirm = view.shadowRoot.querySelector('#roster-confirm');
    confirm.checked = true;
    confirm.dispatchEvent(new Event('change'));
    await view.updateComplete;
    view.shadowRoot.querySelector('#replace-roster').click();
    await waitUntil(
      () => api.callsTo('/api/v1/fleet/roster/csv', 'PUT').length === 1
    );
    await waitUntil(() => !view.rosterSaving);
    const call = api.stub
      .getCalls()
      .find((call: any) => String(call.args[0]).includes('/roster/csv'))!;
    expect(call.args[0]).to.include('retain_display_names=false');
    const init = call.args[1];
    expect(init.body).to.be.instanceOf(FormData);
    expect(init.body.get('file')).to.equal(file);
    expect(new Headers(init.headers).has('Content-Type')).to.equal(false);
    expect(view.shadowRoot.textContent).to.include('Roster replaced: 1 device');
    expect(view.shadowRoot.querySelector('#roster-file').value).to.equal('');
  });
  async function confirmRoster(view: any) {
    const confirm = view.shadowRoot.querySelector('#roster-confirm');
    confirm.checked = true;
    confirm.dispatchEvent(new Event('change'));
    await view.updateComplete;
  }
  it('requires renewed consent after changing the file or display-name privacy', async () => {
    history.replaceState({}, '', '?tab=roster');
    setup();
    const view = await mount();
    await chooseRoster(view);
    await confirmRoster(view);
    await chooseRoster(view, new File(['device_ref,platform\n'], 'clear.csv'));
    expect(view.shadowRoot.querySelector('#replace-roster').disabled).to.equal(
      true
    );
    await confirmRoster(view);
    const privacy = view.shadowRoot.querySelector('#retain-roster-names');
    privacy.checked = true;
    privacy.dispatchEvent(new Event('change'));
    await view.updateComplete;
    expect(view.shadowRoot.querySelector('#replace-roster').disabled).to.equal(
      true
    );
    await confirmRoster(view);
    view.shadowRoot.querySelector('#replace-roster').click();
    await waitUntil(
      () => api.callsTo('/api/v1/fleet/roster/csv', 'PUT').length === 1
    );
    expect(api.callsTo('/api/v1/fleet/roster/csv', 'PUT')[0].search).to.include(
      'retain_display_names=true'
    );
  });
  it('keeps the current roster on validation failure without reflecting private inputs', async () => {
    history.replaceState({}, '', '?tab=roster');
    setup();
    const view = await mount();
    api.stub.callsFake(
      async () =>
        new Response(
          JSON.stringify({ detail: [{ input: 'private-person@example.com' }] }),
          { status: 422 }
        )
    );
    await chooseRoster(view);
    await confirmRoster(view);
    view.shadowRoot.querySelector('#replace-roster').click();
    await waitUntil(() => Boolean(view.rosterError));
    await view.updateComplete;
    expect(view.shadowRoot.textContent).to.include('Roster was not replaced');
    expect(view.shadowRoot.textContent).not.to.include(
      'private-person@example.com'
    );
    expect(view.roster.total).to.equal(2);
    expect(view.rosterFile.name).to.equal('roster.csv');
    expect(view.shadowRoot.querySelector('#replace-roster').disabled).to.equal(
      true
    );
  });
  it('ignores a roster response after navigating away and prevents duplicate replacement', async () => {
    history.replaceState({}, '', '?tab=roster');
    setup();
    const view = await mount();
    let finish!: (response: Response) => void;
    api.stub.callsFake(
      async () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        })
    );
    await chooseRoster(view);
    await confirmRoster(view);
    const event = new Event('submit', { cancelable: true });
    const first = view.replaceRoster(event);
    await waitUntil(() => Boolean(finish));
    await view.replaceRoster(event);
    expect(api.stub.callCount).to.equal(4); // features, profile, roster, one PUT
    view.onBeforeEnter({ search: '' });
    finish(
      new Response(JSON.stringify({ schema_version: 1, items: [], total: 99 }))
    );
    await first;
    expect(view.notice).to.equal('');
    expect(view.rosterFile).to.equal(null);
    expect(view.roster?.total).not.to.equal(99);
  });
  it('rejects oversized CSV files before upload', async () => {
    history.replaceState({}, '', '?tab=roster');
    setup();
    const view = await mount();
    await chooseRoster(
      view,
      new File([new Uint8Array(1048577)], 'too-large.csv')
    );
    expect(view.shadowRoot.textContent).to.include('1 MiB');
    expect(view.shadowRoot.querySelector('#replace-roster').disabled).to.equal(
      true
    );
    expect(api.callsTo('/api/v1/fleet/roster/csv', 'PUT')).to.have.length(0);
  });
  it('does not read or show roster administration without manage permission', async () => {
    history.replaceState({}, '', '?tab=roster');
    setup(true, ['view_device_fleet']);
    const view = await mount();
    expect(view.shadowRoot.querySelector('#roster-file')).not.to.exist;
    expect(api.callsTo('/api/v1/fleet/roster')).to.have.length(0);
    expect(view.shadowRoot.querySelector('a[href$="tab=roster"]')).not.to.exist;
  });
  it('defers observer layout writes and cancels them on disconnect', async () => {
    setup();
    let notify!: ResizeObserverCallback;
    const observer = { observe() {}, unobserve() {}, disconnect() {} };
    const stub = sinon
      .stub(window, 'ResizeObserver')
      .callsFake(function (callback) {
        notify = callback;
        return observer;
      });
    try {
      const view = await mount();
      const content = document.createElement('div');
      content.className = 'map-content';
      view.shadowRoot.append(content);
      notify([], observer);
      notify([], observer);
      expect(content.style.getPropertyValue('--fleet-content-height')).to.equal(
        ''
      );
      await new Promise(requestAnimationFrame);
      expect(content.style.getPropertyValue('--fleet-content-height')).to.match(
        /^\d+(\.\d+)?px$/
      );
      content.style.removeProperty('--fleet-content-height');
      notify([], observer);
      view.remove();
      await new Promise(requestAnimationFrame);
      expect(content.style.getPropertyValue('--fleet-content-height')).to.equal(
        ''
      );
    } finally {
      stub.restore();
    }
  });
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
    // The person in the People column opens their own page.
    expect(
      view.shadowRoot.querySelector(
        'td[data-label="People"] a[href="/console/settings/users/user-1"]'
      )
    ).to.exist;
    expect(text).to.include('1 governed');
    expect(text).to.include('1 not onboarded');
    expect(text).to.include('1 open');
    expect(view.shadowRoot.querySelector('[data-testid="stat-missing"]')).not.to
      .exist;
    expect(text).to.include('not attestation');
    const stat = (id: string) =>
      view.shadowRoot
        .querySelector(`[data-testid="${id}"] .stat-number`)
        .textContent.trim();
    expect(stat('stat-devices')).to.equal('1');
    expect(stat('stat-ungoverned')).to.equal('1');
    expect(stat('stat-runners-offline')).to.equal('0');
    expect(stat('stat-findings')).to.equal('1');
    expect(view.shadowRoot.querySelector('a[href*="user=user-1"]')).to.not
      .exist;
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
      root.querySelector(
        '[data-testid="device-people"] a[href="/console/settings/users/user-1"]'
      ).textContent
    ).to.include('Jane Doe');
    const people = root.querySelector('[data-testid="device-people"]');
    expect(
      people.querySelectorAll('a[href="/console/settings/users/user-1"]')
    ).to.have.length(1);
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
  it('asks for the stored activity window and refetches when it changes', async () => {
    localStorage.setItem('preloop.device_fleet.view_mode', 'canvas');
    localStorage.removeItem('preloop:fleet-map:window');
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [
        {
          path: '/api/v1/fleet/map',
          body: { ...sampleFleetMap(), active_within_minutes: 43200 },
        },
      ],
    });
    const view = await mount();
    await waitUntil(() => view.shadowRoot.querySelector('fleet-map')?.graph);
    expect(api.callsTo('/api/v1/fleet/map')[0].search).to.equal(
      '?active_within_minutes=43200'
    );
    const map = view.shadowRoot.querySelector('fleet-map');
    await map.updateComplete;
    expect(
      map.shadowRoot.querySelector('[data-window="30d"]').checked
    ).to.equal(true);
    map.shadowRoot.querySelector('[data-window="24h"]').click();
    await waitUntil(() => api.callsTo('/api/v1/fleet/map').length === 2);
    expect(api.callsTo('/api/v1/fleet/map')[1].search).to.equal(
      '?active_within_minutes=1440'
    );
    expect(localStorage.getItem('preloop:fleet-map:window')).to.equal('24h');
    map.shadowRoot.querySelector('[data-window="any"]').click();
    await waitUntil(() => api.callsTo('/api/v1/fleet/map').length === 3);
    expect(api.callsTo('/api/v1/fleet/map')[2].search).to.equal('');
    localStorage.removeItem('preloop:fleet-map:window');
    localStorage.removeItem('preloop.device_fleet.view_mode');
  });

  it('passes the users chosen on the map to the server as users=', async () => {
    localStorage.setItem('preloop.device_fleet.view_mode', 'canvas');
    api = mockApi({
      capabilities: ['fleet_coverage'],
      profile: { permissions: ['view_device_fleet'] },
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    const view = await mount();
    await waitUntil(() => view.shadowRoot.querySelector('fleet-map')?.graph);
    const map = view.shadowRoot.querySelector('fleet-map');
    await map.updateComplete;
    map.shadowRoot.querySelector('[data-user="user:maya"]').click();
    await waitUntil(() => api.callsTo('/api/v1/fleet/map').length === 2);
    expect(api.callsTo('/api/v1/fleet/map')[1].search).to.contain('users=maya');
    await view.updateComplete;
    expect(map.users).to.deep.equal(['user:maya']);
    localStorage.removeItem('preloop.device_fleet.view_mode');
  });

  it('keeps the Table/Map switch clear of the subtitle at 390 px', async () => {
    await setViewport({ width: 390, height: 844 });
    try {
      localStorage.setItem('preloop.device_fleet.view_mode', 'canvas');
      api = mockApi({
        capabilities: ['fleet_coverage'],
        profile: { permissions: ['view_device_fleet'] },
        routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
      });
      const view = await mount();
      await view.updateComplete;
      const header = view.shadowRoot.querySelector('view-header');
      await header.updateComplete;
      const sw = view.shadowRoot.querySelector('.view-switch');
      const subtitle = view.shadowRoot.querySelector('[slot="description"]');
      expect(sw.getBoundingClientRect().bottom).to.be.at.most(
        subtitle.getBoundingClientRect().top
      );
    } finally {
      await setViewport({ width: 1280, height: 800 });
      localStorage.removeItem('preloop.device_fleet.view_mode');
    }
  });
});
