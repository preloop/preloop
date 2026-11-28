import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import { mockApi, type MockApi } from '../../../test-helpers/capability-api';
import { invalidateApiCaches } from '../../../api';
import type {
  DeviceFleetDevice,
  FleetFinding,
} from '../../../device-fleet-api';
import { fleetExtensionFindings } from '../../../test-helpers/fleet-extension-findings';
import {
  buildRegister,
  filterRows,
  registerCsv,
  CSV_COLUMNS,
} from '../../../compliance-api';
import {
  EMPTY_REGISTER,
  REPORTED_NOT_ATTESTED,
  TEAM_EDITION_NOTE,
} from './compliance-view';
import './compliance-view';
import type { ComplianceView } from './compliance-view';

const KEY_A = 'fp:' + 'a'.repeat(64);
const KEY_B = 'fp:' + 'b'.repeat(64);
const KEY_C = 'fp:' + 'c'.repeat(64);

function device(
  key: string,
  hostname: string,
  changes: Partial<DeviceFleetDevice> = {}
): DeviceFleetDevice {
  return {
    device_key: key,
    hostname,
    os: 'macos',
    runner_state: 'online',
    collection_state: 'fresh',
    inventory_state: 'fresh',
    roster_state: 'not_on_roster',
    verification: 'unverified',
    last_seen_at: '2026-11-28T10:00:00Z',
    agents_onboarded: 1,
    agents_not_onboarded: 0,
    users: [],
    runners: [],
    agents: [],
    sources: [],
    possible_same_machine_as: [],
    findings: [],
    ...changes,
  };
}

const DEVICES = [
  device(KEY_A, 'jane-laptop', {
    users: [{ id: 'user-1', label: 'Jane Doe', source: 'agent_owner' }],
  }),
  device(KEY_B, 'build-host', {
    collection_state: 'stale',
    agents_not_onboarded: 2,
  }),
  device(KEY_C, 'new-host', {
    collection_state: 'none',
    last_seen_at: null,
    agents_onboarded: 0,
  }),
];

const FINDINGS: FleetFinding[] = [
  { ...fleetExtensionFindings[0], opened_at: '2026-11-27T10:00:00Z' },
  { ...fleetExtensionFindings[1], opened_at: '2026-11-25T10:00:00Z' },
  {
    ...fleetExtensionFindings[2],
    id: 'f-b',
    device_key: KEY_B,
    opened_at: '2026-11-20T10:00:00Z',
    evidence: { ...fleetExtensionFindings[2].evidence, agent_kind: 'codex' },
  },
  {
    ...fleetExtensionFindings[0],
    id: 'f-resolved',
    state: 'resolved',
  },
];

describe('compliance register', () => {
  it('groups open findings per device, oldest first, with people and counts', () => {
    // protects=the register rows and summary; fails_when=resolved findings
    // count, rows sort newest first, or a device with no person is dropped;
    // no_claim=fixtures stand in for the fleet routes.
    const { rows, summary } = buildRegister({ items: DEVICES }, FINDINGS);
    expect(rows.map((r) => [r.device, r.person])).to.deep.equal([
      ['build-host', 'Unassigned'],
      ['jane-laptop', 'Jane Doe'],
    ]);
    expect(rows[1].since).to.equal('2026-11-25T10:00:00Z');
    expect(rows[1].extensions).to.deep.equal([
      'release-check (hook)',
      'review-helper (skill)',
    ]);
    expect(rows[1].reasons).to.deep.equal(['content_changed', 'unapproved']);
    expect(rows[1].decideHref).to.contain('finding=');
    expect(rows[0].agentKinds).to.deep.equal(['codex']);
    expect(summary).to.deep.equal({
      devicesReporting: 1,
      devicesStale: 1,
      devicesNeverReported: 1,
      agentsOnboarded: 2,
      agentsNotOnboarded: 2,
      openFindings: 3,
      findingsByReason: {
        unapproved: 1,
        content_changed: 1,
        version_outside_constraint: 1,
      },
    });
  });

  it('filters by person, device, agent and reason', () => {
    // protects=the four filters; fails_when=a filter leaves other rows in;
    // no_claim=none beyond protects.
    const { rows } = buildRegister({ items: DEVICES }, FINDINGS);
    expect(filterRows(rows, { person: 'Jane Doe' })).to.have.length(1);
    expect(filterRows(rows, { device: KEY_B })[0].device).to.equal(
      'build-host'
    );
    expect(filterRows(rows, { agent: 'codex' })).to.have.length(1);
    expect(filterRows(rows, { reason: 'unapproved' })[0].person).to.equal(
      'Jane Doe'
    );
    expect(filterRows(rows, { person: 'Jane Doe', agent: 'codex' })).to.be
      .empty;
  });

  it('writes a CSV with the as-of time, columns and neutralised formulas', () => {
    // protects=the export; fails_when=the timestamp or a column is missing or
    // a cell can run as a spreadsheet formula; no_claim=no browser download.
    const { rows } = buildRegister({ items: DEVICES }, FINDINGS);
    rows[0].person = '=HYPERLINK("x")';
    const csv = registerCsv(rows, new Date('2026-11-28T12:00:00Z'));
    const lines = csv.trim().split('\n');
    expect(lines[0]).to.equal(
      '# Compliance register as of 2026-11-28T12:00:00.000Z'
    );
    expect(lines[1]).to.equal(CSV_COLUMNS.join(','));
    expect(lines).to.have.length(2 + rows.length);
    expect(lines[2].startsWith(`"'=HYPERLINK(""x"")"`)).to.equal(true);
  });
});

describe('Compliance view', () => {
  let api: MockApi;
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-token');
  });
  afterEach(() => {
    api?.restore();
    invalidateApiCaches();
    localStorage.removeItem('accessToken');
  });

  const COUNTS = { approved: 40, unapproved: 5, unknown: 15, total: 60 };
  const DECISIONS = {
    items: [
      {
        id: 'd1',
        created_at: '2026-11-28T09:00:00Z',
        agent_kind: 'claude_code',
        extension_kind: 'skill',
        extension_id: 'review-helper',
        action: 'state_changed',
        previous_state: 'approved',
        new_state: 'unapproved',
        content_hash: null,
        note: 'Not reviewed',
        decided_by_user_id: 'user-1',
        decided_by: 'Jane Doe',
      },
    ],
  };

  function setup(team: boolean, findings: FleetFinding[] = FINDINGS) {
    api = mockApi({
      capabilities: team ? ['fleet_coverage'] : [],
      profile: { permissions: ['view_agents', 'view_device_fleet'] },
      routes: [
        {
          path: '/api/v1/agents/discovery-extensions/state-counts',
          body: COUNTS,
        },
        { path: '/api/v1/agents/extension-decisions', body: DECISIONS },
        {
          path: '/api/v1/fleet/device-fleet',
          body: {
            items: DEVICES,
            total: 3,
            truncated: false,
            summary: {},
            unassigned_agents: [],
          },
        },
        { path: '/api/v1/fleet/findings', body: { items: findings } },
      ],
    });
  }

  async function render(): Promise<ComplianceView> {
    const el = await fixture<ComplianceView>(
      html`<compliance-view></compliance-view>`
    );
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="decisions"]'),
      'decisions render'
    );
    return el;
  }

  it('renders the team register, summary and decisions with the fleet capability', async () => {
    // protects=the page on a fleet edition; fails_when=a section is missing,
    // the fixed sentence is dropped or the page claims compliance;
    // no_claim=mocked routes, no visual regression claim.
    setup(true);
    const el = await render();
    const root = el.shadowRoot!;
    expect(root.querySelector('[data-testid="honesty"]')?.textContent).to.equal(
      REPORTED_NOT_ATTESTED
    );
    expect(
      root.querySelectorAll('[data-testid="register"] tbody tr')
    ).to.have.length(2);
    expect(
      root.querySelector('[data-testid="tile-devices"]')?.textContent
    ).to.contain('1 stale · 1 never reported');
    expect(
      root.querySelector('[data-testid="tile-extensions"]')?.textContent
    ).to.contain('5 unapproved · 15 not decided');
    expect(
      root.querySelector('[data-testid="decisions"]')?.textContent
    ).to.contain('Approved → Unapproved');
    expect(root.querySelector('[data-testid="edition-note"]')).to.equal(null);
    expect(root.textContent?.toLowerCase()).not.to.contain('compliant');
    expect(root.querySelector('[data-testid="export-csv"]')).to.exist;
  });

  it('shows the empty state without claiming compliance', async () => {
    // protects=the empty wording; fails_when=no rows read as "compliant";
    // no_claim=none beyond protects.
    setup(true, []);
    const el = await render();
    expect(
      el.shadowRoot!.querySelector('[data-testid="empty"]')?.textContent?.trim()
    ).to.equal(EMPTY_REGISTER);
  });

  it('falls back to the single-account summary without the fleet capability', async () => {
    // protects=the open-source fallback; fails_when=a fleet route is called or
    // the team note is missing; no_claim=none beyond protects.
    setup(false);
    const el = await render();
    const root = el.shadowRoot!;
    expect(
      root.querySelector('[data-testid="edition-note"]')?.textContent?.trim()
    ).to.equal(TEAM_EDITION_NOTE);
    expect(root.querySelector('[data-testid="register"]')).to.equal(null);
    expect(root.querySelector('[data-testid="tile-extensions"]')).to.exist;
    expect(api.callsTo(/^\/api\/v1\/fleet\//)).to.be.empty;
  });
});
