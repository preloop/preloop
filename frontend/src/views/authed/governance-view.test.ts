import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from '../../test-helpers/capability-api';
import type { GovernanceRun, GovernanceFinding } from '../../governance-api';
import type { GovernanceView } from './governance-view';
import './governance-view';

const run: GovernanceRun = {
  id: 'run-1',
  window_start: '2026-10-05T07:00:00Z',
  window_end: '2026-10-12T07:00:00Z',
  status: 'completed',
  started_at: '2026-10-12T07:00:00Z',
  finished_at: '2026-10-12T07:01:00Z',
  sessions_considered: 4,
  sessions_summarized: 2,
  segments: 3,
  summaries_started_at: '2026-10-05T08:00:00Z',
  platform_cost: 0.012,
  account_cost: 0,
};
const finding: GovernanceFinding = {
  id: 'finding-1',
  run_id: 'run-1',
  kind: 'violation',
  user_id: '00000000-0000-4000-8000-000000000001',
  user_name: 'Jane Doe',
  category: 'unapproved_tool',
  severity: 'high',
  title: 'Review tool use',
  rule_ref: 'must.tools',
  evidence: [
    {
      excerpt: 'A tool was used',
      source: 'metadata',
      audit_event_id: 'event-1',
    },
  ],
  session_ids: ['session-1'],
  suggestion: 'Use an approved tool.',
  created_at: '2026-10-12T07:01:00Z',
};
const BASE = '/api/v1/governance-review';

describe('Governance findings', () => {
  let api: MockApi;
  let runs: GovernanceRun[];
  let rows: GovernanceFinding[];
  beforeEach(() => {
    signInForTest();
    localStorage.removeItem('preloop.governance.view_mode');
    runs = [{ ...run }];
    rows = [{ ...finding }];
    api = mockApi({
      capabilities: ['governance_review'],
      routes: [
        { path: `${BASE}/runs`, body: () => runs },
        {
          path: `${BASE}/findings`,
          body: (call) =>
            rows.filter(
              (row) =>
                Boolean(row.dismissed_at) ===
                (new URLSearchParams(call.search).get('dismissed') === 'true')
            ),
        },
        { path: `${BASE}/findings/finding-1`, body: () => rows[0] },
        {
          path: `${BASE}/findings/finding-1/dismiss`,
          method: 'POST',
          body: () => {
            rows = [
              {
                ...rows[0],
                dismissed_at: run.finished_at,
                dismissed_reason: 'expected',
                dismissed_by_id: 'user-1',
              },
            ];
            return rows[0];
          },
        },
        {
          path: `${BASE}/findings/finding-1/restore`,
          method: 'POST',
          body: () => {
            rows = [{ ...finding }];
            return rows[0];
          },
        },
      ],
    });
  });
  it('shows a completed deterministic report without pending analysis', async () => {
    runs = [
      {
        ...run,
        model_analysis_enabled: false,
        sessions_summarized: 0,
        segments: 0,
        status: 'completed',
      },
    ];
    rows = [];
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Model analysis is not enabled.')
    );
    const text = el.shadowRoot!.textContent!;
    expect(text).to.contain('Model analysis is not enabled.');
    expect(text).to.contain('Completed');
    expect(text).not.to.contain('Not yet reviewed');
    expect(text).not.to.contain('first hourly sweep');
    expect(el.shadowRoot!.querySelector('.partial')).to.equal(null);
  });
  afterEach(() => {
    api.restore();
    window.history.replaceState({}, '', '/');
  });
  async function mount(id?: string) {
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    if (id) el.onBeforeEnter({ params: { findingId: id } });
    await waitUntil(() =>
      el
        .shadowRoot!.textContent!.replace(/\s+/g, ' ')
        .includes(id ? 'Use an approved tool.' : 'Reviewed 2 of 4 sessions')
    );
    await el.updateComplete;
    return el;
  }
  it('renders coverage, run history and a real finding anchor', async () => {
    const el = await mount();
    const text = el.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('Week of Oct 5, 2026 to Oct 12, 2026 (UTC)');
    expect(text).not.to.contain('segments');
    expect(text).to.contain('Weekly review');
    expect(text).to.contain('Broke a requirement in your policy.');
    expect(
      el.shadowRoot!.querySelector(
        'a[href="/console/governance/findings/finding-1"]'
      )
    ).to.exist;
    expect(el.shadowRoot!.textContent).to.contain('Run history');
    expect(el.shadowRoot!.textContent).to.contain('$0.012');
  });
  it('sends kind, severity, developer and week filters to the server', async () => {
    const el = await mount();
    for (const [name, value] of [
      ['kind', 'inefficiency'],
      ['severity', 'low'],
      ['user_id', '00000000-0000-4000-8000-000000000001'],
      ['run_id', 'run-1'],
    ]) {
      const select = el.shadowRoot!.querySelector(
        `[name="${name}"]`
      ) as HTMLInputElement;
      select.value = value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      await waitUntil(() =>
        api
          .callsTo(`${BASE}/findings`)
          .some((c) => new URLSearchParams(c.search).get(name) === value)
      );
      await el.updateComplete;
    }
  });
  it('drills down to session and audit evidence, quotes the rule and restores dismissal', async () => {
    const el = await mount('finding-1');
    expect(
      el.shadowRoot!.querySelector(
        'a[href="/console/runtime-sessions?sessionId=session-1"]'
      )
    ).to.exist;
    expect(
      el.shadowRoot!.querySelector('a[href="/console/audit?event=event-1"]')
    ).to.exist;
    expect(el.shadowRoot!.textContent).to.contain('must.tools');
    expect(el.shadowRoot!.textContent).to.contain('Tools (requirement)');
    expect(el.shadowRoot!.textContent).to.contain('Gateway usage data');
    (
      el.shadowRoot!.querySelector('[data-dismiss="expected"]') as HTMLElement
    ).click();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Dismissed: Expected')
    );
    expect(
      api.callsTo(`${BASE}/findings/finding-1/dismiss`, 'POST')[0].body
    ).to.eql({ reason: 'expected' });
    (el.shadowRoot!.querySelector('[data-restore]') as HTMLElement).click();
    await waitUntil(
      () =>
        api.callsTo(`${BASE}/findings/finding-1/restore`, 'POST').length === 1
    );
    await waitUntil(
      () => !el.shadowRoot!.textContent!.includes('Dismissed: Expected')
    );
  });
  it('keeps dismissed findings collapsed until requested', async () => {
    rows = [{ ...finding, dismissed_at: run.finished_at }];
    const el = await mount();
    expect(
      api
        .callsTo(`${BASE}/findings`)
        .some((c) => new URLSearchParams(c.search).get('dismissed') === 'true')
    ).to.equal(false);
    const details = el.shadowRoot!.querySelector(
      'details[data-dismissed]'
    ) as HTMLDetailsElement;
    expect(details.open).to.equal(false);
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await waitUntil(
      () => el.shadowRoot!.querySelector('details[data-dismissed] a') !== null
    );
  });
  for (const state of ['zero', 'covered', 'budget']) {
    it(`renders a readable ${state} partial-data state without an empty findings table`, async () => {
      rows = [];
      if (state === 'zero')
        runs = [
          {
            ...run,
            sessions_summarized: 0,
            segments: 0,
            summaries_started_at: undefined,
          },
        ];
      if (state === 'budget')
        runs = [
          { ...run, status: 'partial', stop_reason: 'governance_hard_cap' },
        ];
      const el = await fixture<GovernanceView>(
        html`<governance-view></governance-view>`
      );
      await waitUntil(() => el.shadowRoot!.textContent!.includes('Reviewed'));
      const text = el.shadowRoot!.textContent!;
      expect(text).to.contain('Run history');
      expect(el.shadowRoot!.querySelector('table[data-findings]')).to.equal(
        null
      );
      if (state === 'zero') {
        expect(text).to.contain('Not yet reviewed');
        expect(text.replace(/\s+/g, ' ')).to.contain('first hourly sweep');
      }
      if (state === 'covered')
        expect(text).to.contain('No findings in the reviewed data');
      if (state === 'budget')
        expect(text).to.contain('Run spending limit reached');
    });
  }
  it('still shows deterministic findings when no sessions are reviewed', async () => {
    runs = [{ ...run, sessions_summarized: 0, segments: 0 }];
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    await waitUntil(
      () =>
        !!el.shadowRoot!.querySelector(
          'a[href="/console/governance/findings/finding-1"]'
        )
    );
    expect(el.shadowRoot!.textContent).to.contain('Not yet reviewed');
  });
  it('shows names, sorts the page and persists the chosen collection view', async () => {
    rows = [
      { ...finding, id: 'finding-2', title: 'Zebra tool', severity: 'low' },
      { ...finding, title: 'Alpha tool' },
    ];
    const el = await mount();
    expect(el.shadowRoot!.textContent).to.contain('Jane Doe');
    const header = el.shadowRoot!.querySelector(
      'table[data-findings] th button'
    ) as HTMLButtonElement;
    header.click();
    await el.updateComplete;
    expect(
      el
        .shadowRoot!.querySelector('table[data-findings] tbody a')!
        .textContent!.trim()
    ).to.equal('Alpha tool');
    header.click();
    await el.updateComplete;
    expect(
      el
        .shadowRoot!.querySelector('table[data-findings] tbody a')!
        .textContent!.trim()
    ).to.equal('Zebra tool');
    const toolbar = el.shadowRoot!.querySelector('list-toolbar')!;
    toolbar.dispatchEvent(
      new CustomEvent('view-change', { detail: { value: 'cards' } })
    );
    await el.updateComplete;
    expect(localStorage.getItem('preloop.governance.view_mode')).to.equal(
      'cards'
    );
    expect(
      getComputedStyle(el.shadowRoot!.querySelector('.finding-table')!).display
    ).to.equal('none');
  });
  it('keeps each actions button inside its measured table cell', async () => {
    const el = await mount();
    const cell = el.shadowRoot!.querySelector('td.actions') as HTMLElement;
    const actions = cell.querySelector('resource-actions') as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await actions.updateComplete;
    const button = actions.shadowRoot!.querySelector(
      '.actions-container sl-button'
    ) as HTMLElement;
    const bounds = button.getBoundingClientRect();
    const container = cell.getBoundingClientRect();
    expect(bounds.width).to.be.greaterThan(0);
    expect(bounds.left).to.be.at.least(container.left);
    expect(bounds.right).to.be.at.most(container.right);
  });
  it('hides management controls and spend from a policy-only reader', async () => {
    api.restore();
    api = mockApi({
      capabilities: ['governance_review'],
      profile: { permissions: ['view_policies'] },
      routes: [
        { path: `${BASE}/runs`, body: [run] },
        { path: `${BASE}/findings`, body: [finding] },
        { path: `${BASE}/findings/finding-1`, body: finding },
      ],
    });
    const el = await mount('finding-1');
    expect(el.shadowRoot!.querySelector('[data-dismiss]')).to.equal(null);
    expect(el.shadowRoot!.textContent).not.to.contain('Spend est.');
    expect(el.shadowRoot!.textContent).not.to.contain('$0.012');
    expect(api.calls.some((call) => call.method === 'POST')).to.equal(false);
  });
  it('requests a detail finding’s own run even when it is older than the history page', async () => {
    rows = [{ ...finding, run_id: 'older-run' }];
    await mount('finding-1');
    expect(
      api
        .callsTo(`${BASE}/runs`)
        .some(
          (call) =>
            new URLSearchParams(call.search).get('run_id') === 'older-run'
        )
    ).to.equal(true);
  });
  it('escapes stored evidence instead of rendering finding HTML', async () => {
    rows = [
      {
        ...finding,
        evidence: [
          { excerpt: '<img src=x onerror=alert(1)>', source: 'metadata' },
        ],
      },
    ];
    const el = await mount('finding-1');
    expect(el.shadowRoot!.querySelector('blockquote img')).to.equal(null);
    expect(el.shadowRoot!.querySelector('blockquote')!.textContent).to.contain(
      '<img'
    );
  });
  it('shows a failed load as an error, with a retry, rather than a clean empty review', async () => {
    api.restore();
    api = mockApi({
      capabilities: ['governance_review'],
      routes: [
        {
          path: `${BASE}/runs`,
          status: 500,
          body: { detail: 'Review unavailable' },
        },
      ],
    });
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('[role="alert"]'));
    expect(el.shadowRoot!.textContent).to.contain('Review unavailable');
    expect(el.shadowRoot!.textContent).not.to.contain(
      'No findings in the reviewed data'
    );
    expect(el.shadowRoot!.textContent).to.contain('Retry');
  });
  it('hides the surface when the plugin is absent', async () => {
    api.restore();
    api = mockApi();
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    await waitUntil(() => api.callsTo('/api/v1/features').length > 0);
    await new Promise((resolve) => setTimeout(resolve, 50));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('view-header')).to.equal(null);
    expect(api.callsTo(`${BASE}/findings`)).to.have.length(0);
  });
});

describe('Governance findings clarity', () => {
  it('names rules in plain words', async () => {
    const { ruleLabel } = await import('./governance-view');
    expect(ruleLabel('prefer.retry_limit')).to.equal(
      'Retry limit (preference)'
    );
    expect(ruleLabel('must.data_handling')).to.equal(
      'Data handling (requirement)'
    );
    expect(ruleLabel('must.account.2')).to.equal('Your policy (requirement)');
    expect(ruleLabel('policy_notice.no-secrets')).to.equal(
      'Policy rule no-secrets'
    );
    expect(ruleLabel(null)).to.equal('Not a policy rule');
  });
});

describe('Governance week and banner', () => {
  let api: MockApi;
  let firstPage: GovernanceRun[];
  const older: GovernanceRun = {
    ...run,
    id: 'run-0',
    window_start: '2026-09-28T07:00:00Z',
    window_end: '2026-10-05T07:00:00Z',
    mode_after: 'off_over_budget',
  };
  beforeEach(() => {
    signInForTest();
    firstPage = [run, older];
    api = mockApi({
      capabilities: ['governance_review'],
      routes: [
        {
          path: `${BASE}/runs`,
          body: (call) =>
            new URLSearchParams(call.search).get('offset') === '25'
              ? [older]
              : firstPage,
        },
        { path: `${BASE}/findings`, body: () => [] },
      ],
    });
  });
  afterEach(() => api.restore());

  it('shows the chosen week and no stale over-budget banner', async () => {
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    await waitUntil(() => el.shadowRoot!.textContent!.includes('Reviewed'));
    const history = el.shadowRoot!.querySelector(
      '[data-history]'
    ) as HTMLDetailsElement;
    history.open = true;
    const weeks = history.querySelectorAll('tbody button');
    (weeks[1] as HTMLButtonElement).click();
    await waitUntil(
      () =>
        el
          .shadowRoot!.textContent!.replace(/\s+/g, ' ')
          .includes('Week of Sep 28'),
      'chosen week'
    );
    await el.updateComplete;
    const chosen = el.shadowRoot!.querySelector(
      'select[name="run_id"]'
    ) as HTMLSelectElement;
    expect(chosen.value).to.equal('run-0');
    expect(el.shadowRoot!.querySelector('.over-budget')).to.equal(null);
  });

  it('does not show the current budget banner on older run pages', async () => {
    firstPage = Array.from({ length: 25 }, (_, i) => ({
      ...run,
      id: `recent-${i}`,
    }));
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    await waitUntil(() => el.shadowRoot!.textContent!.includes('Reviewed'));
    const history = el.shadowRoot!.querySelector(
      '[data-history]'
    ) as HTMLDetailsElement;
    history.open = true;
    const olderButton = Array.from(history.querySelectorAll('sl-button')).find(
      (button) => button.textContent!.includes('Older runs')
    )!;
    olderButton.click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector(
        'select[name="run_id"] option[value="run-0"]'
      )
    );
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain('Sep 28');
    expect(el.shadowRoot!.querySelector('.over-budget')).to.equal(null);
  });
});
