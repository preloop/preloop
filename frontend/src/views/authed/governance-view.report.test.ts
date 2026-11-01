import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import {
  mockApi,
  signInForTest,
  type MockApi,
  TEST_PROFILE,
} from '../../test-helpers/capability-api';
import type { GovernanceFinding, GovernanceRun } from '../../governance-api';
import { sessionName, type GovernanceView } from './governance-view';
import './governance-view';

const BASE = '/api/v1/governance-review';
const deliveryId = '12345678-1234-1234-1234-123456789abc';
const exceptionKey = 'a'.repeat(64);
const acknowledgePath = `/api/v1/weekly-report/deliveries/${deliveryId}/acknowledge/${exceptionKey}`;
const run: GovernanceRun = {
  id: 'run-1',
  window_start: '2026-10-10T09:00:00Z',
  window_end: '2026-10-17T09:00:00Z',
  status: 'completed',
  started_at: '2026-10-17T09:00:00Z',
  sessions_considered: 4,
  sessions_summarized: 2,
  segments: 3,
};
const base = {
  run_id: 'run-1',
  severity: 'medium' as const,
  session_ids: [],
  suggestion: 'Look at it.',
  created_at: '2026-10-17T09:01:00Z',
};
const spend: GovernanceFinding = {
  ...base,
  id: 'spend-1',
  kind: 'anomaly',
  category: 'spend_outlier',
  title: 'Spend outlier: daily_spend',
  evidence: [
    {
      source: 'spend_outlier_finding',
      excerpt: 'Daily spend was 8.5x the usual day.',
      spend: {
        day: '2026-10-13',
        cost: 22.39,
        baseline: 2.64,
        ratio: 8.49,
        daily_series: [
          { day: '2026-10-12', cost: 2.71, is_outlier: false },
          { day: '2026-10-13', cost: 22.39, is_outlier: true },
          { day: '2026-10-14', cost: 2.6, is_outlier: false },
        ],
        contributing_sessions: [
          {
            runtime_session_id: 'session-a',
            title: 'Refactor billing digest',
            agent_name: 'Codex CLI',
            started_at: '2026-10-13T08:12:00Z',
            cost: 9.8,
            href: '/console/runtime-sessions?sessionId=session-a',
            agent_href: '/console/agents/agent-1',
          },
          {
            runtime_session_id: 'session-b',
            title: null,
            agent_name: 'Claude Code',
            started_at: '2026-10-13T11:40:00Z',
            cost: 6.15,
            flow_href: '/console/flows/flow-1',
            flow_name: 'Nightly triage',
          },
        ],
      },
    },
  ],
};
const tool: GovernanceFinding = {
  ...base,
  id: 'tool-1',
  kind: 'inefficiency',
  category: 'unused_tools',
  title: 'browser-07 attached in 14 sessions and used in 1.',
  evidence: [
    {
      source: 'tool_hygiene',
      excerpt: 'browser-07 used in 1 of 14 sessions.',
      tool: {
        name: 'browser-07',
        sessions_attached: 14,
        sessions_used: 1,
        schema_tokens_per_request: 610,
        estimated_weekly_tokens: 6_800_000,
        estimated_weekly_savings: 3.4,
        capability: 'driving a browser page',
      },
    },
  ],
};

describe('Weekly review deep links and report evidence', () => {
  let api: MockApi;
  let mode: string;
  let valueEnabled: boolean;
  let costAllowed: boolean;
  let weeks: GovernanceRun[];
  beforeEach(() => {
    signInForTest();
    mode = 'on_platform';
    valueEnabled = false;
    costAllowed = true;
    weeks = [run];
    api = mockApi({
      capabilities: ['governance_review'],
      routes: [
        {
          path: '/api/v1/features',
          body: () => ({
            features: {
              governance_review: true,
              weekly_work_value: valueEnabled,
            },
          }),
        },
        {
          path: '/api/v1/auth/users/me',
          body: () => ({
            ...TEST_PROFILE,
            permissions: costAllowed ? null : ['view_policies'],
          }),
        },
        {
          path: '/api/v1/billing/weekly-value',
          body: () => ({
            window_start: run.window_start,
            window_end: run.window_end,
            items: [],
            method: 'Evidence',
            prs_observed: 0,
            prs_considered: 0,
          }),
        },
        { path: `${BASE}/runs`, body: () => weeks },
        {
          path: `${BASE}/findings`,
          body: (call) =>
            new URLSearchParams(call.search).get('category') === 'tool_hygiene'
              ? [tool]
              : [spend, tool],
        },
        { path: `${BASE}/findings/spend-1`, body: () => spend },
        { path: `${BASE}/findings/tool-1`, body: () => tool },
        {
          path: `${BASE}/settings`,
          body: () => ({ mode, own_model_exempt: false }),
        },
        {
          path: `${BASE}/settings`,
          method: 'PUT',
          body: (call) => {
            mode = (call.body as { mode: string }).mode;
            return { mode, own_model_exempt: false };
          },
        },
        {
          path: acknowledgePath,
          method: 'POST',
          body: () => ({ acknowledged: true }),
        },
        {
          path: '/api/v1/weekly-report/preferences',
          method: 'PUT',
          body: () => ({ account_enabled: true, user_enabled: false }),
        },
      ],
    });
  });
  afterEach(() => {
    api.restore();
    sinon.restore();
    window.history.replaceState({}, '', '/');
  });
  const text = (el: GovernanceView) =>
    el.shadowRoot!.textContent!.replace(/\s+/g, ' ');
  async function mount(search = '', id?: string) {
    window.history.replaceState({}, '', `/console/governance${search}`);
    const el = await fixture<GovernanceView>(
      html`<governance-view></governance-view>`
    );
    if (id) el.onBeforeEnter({ params: { findingId: id } });
    await waitUntil(() => !el.shadowRoot!.textContent!.includes('Loading'));
    await el.updateComplete;
    return el;
  }

  it('opens the run and section an email link names', async () => {
    const el = await mount('?run_id=run-1&section=tool_hygiene');
    await waitUntil(() => text(el).includes('Showing Tool hygiene'));
    const call = api.callsTo(`${BASE}/findings`)[0];
    const params = new URLSearchParams(call.search);
    expect(params.get('run_id')).to.equal('run-1');
    expect(params.get('category')).to.equal('tool_hygiene');
    expect(el.shadowRoot!.querySelector('a[href*="tool-1"]')).to.exist;
    expect(el.shadowRoot!.querySelector('a[href*="spend-1"]')).not.to.exist;
  });

  it('confirms an emailed exception before acknowledging it', async () => {
    const el = await mount(
      `?digest_delivery=${deliveryId}&ack_exception=${exceptionKey}`
    );
    expect(api.callsTo(acknowledgePath, 'POST')).to.have.length(0);
    expect(el.shadowRoot!.querySelector('[data-acknowledge]')).to.exist;
    (
      el.shadowRoot!.querySelector(
        '[data-acknowledge] sl-button[variant="primary"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => el.shadowRoot!.querySelector('[data-acknowledged]'));
    expect(api.callsTo(acknowledgePath, 'POST')).to.have.length(1);
  });

  it('asks before stopping the weekly email, then stops it for this user', async () => {
    const el = await mount('?unsubscribe=weekly-report');
    expect(el.shadowRoot!.querySelector('[data-unsubscribe]')).to.exist;
    const stop = el.shadowRoot!.querySelector(
      '[data-unsubscribe] sl-button[variant="primary"]'
    ) as HTMLElement;
    stop.click();
    await waitUntil(() => el.shadowRoot!.querySelector('[data-unsubscribed]'));
    expect(
      api.callsTo('/api/v1/weekly-report/preferences', 'PUT')[0].body
    ).to.eql({ user_enabled: false });
  });

  it('says plainly when the review is off and turns it on in one click', async () => {
    mode = 'off';
    const el = await mount();
    await waitUntil(() => el.shadowRoot!.querySelector('[data-review-off]'));
    expect(text(el)).to.contain('The AI usage policy review is off.');
    (
      el.shadowRoot!.querySelector('[data-review-off] sl-button') as HTMLElement
    ).click();
    await waitUntil(() => !el.shadowRoot!.querySelector('[data-review-off]'));
    expect(api.callsTo(`${BASE}/settings`, 'PUT')[0].body).to.eql({
      mode: 'on_platform',
    });
  });

  it('charts the outlier day and links the sessions that drove it', async () => {
    const el = await mount('', 'spend-1');
    await waitUntil(() => el.shadowRoot!.querySelector('[data-spend-chart]'));
    expect(text(el)).to.match(/cost \$22\.39, 8\.5x your usual \$2\.64 a day/);
    const chart = el.shadowRoot!.querySelector('[data-spend-chart]')!;
    expect(chart.querySelectorAll('rect.bar')).to.have.length(3);
    expect(chart.querySelectorAll('rect.outlier')).to.have.length(1);
    expect(chart.getAttribute('aria-label')).to.contain('$22.39');
    const titled = el.shadowRoot!.querySelector(
      'a[href="/console/runtime-sessions?sessionId=session-a"]'
    )!;
    expect(titled.textContent).to.equal('Refactor billing digest');
    expect(el.shadowRoot!.querySelector('a[href="/console/agents/agent-1"]')).to
      .exist;
    expect(
      el.shadowRoot!.querySelector('a[href="/console/flows/flow-1"]')!
        .textContent
    ).to.equal('Nightly triage');
    expect(text(el)).not.to.contain('{"');
  });

  it('weighs a tool’s schema cost against the capability it gives', async () => {
    const el = await mount('', 'tool-1');
    await waitUntil(() => el.shadowRoot!.querySelector('[data-tool-tradeoff]'));
    const body = text(el);
    expect(body).to.contain('About 610 tokens on every request');
    expect(body).to.contain('About 6.8M tokens a week');
    expect(body).to.contain('Driving a browser page');
    expect(body).to.contain('a trade-off, not a fix');
  });

  it('names an untitled session by its agent and start time', () => {
    expect(
      sessionName({
        runtime_session_id: 'abcdef123456',
        agent_name: 'Claude Code',
        started_at: '2026-10-13T11:40:00Z',
      })
    ).to.match(/^Claude Code, /);
    expect(sessionName({ runtime_session_id: 'abcdef123456' })).to.equal(
      'Session abcdef12'
    );
  });
  it('gates recorded outcomes by capability and View cost permission', async () => {
    const hidden = await mount();
    expect(hidden.shadowRoot!.querySelector('weekly-value-section')).not.to
      .exist;
    expect(api.callsTo('/api/v1/billing/weekly-value')).to.have.length(0);
    hidden.remove();
    valueEnabled = true;
    costAllowed = false;
    const denied = await mount();
    expect(denied.shadowRoot!.querySelector('weekly-value-section')).not.to
      .exist;
    expect(api.callsTo('/api/v1/billing/weekly-value')).to.have.length(0);
  });

  it('uses the exact email window and switches to the newly selected week', async () => {
    valueEnabled = true;
    const scroll = sinon.spy(Element.prototype, 'scrollIntoView');
    weeks = [
      run,
      {
        ...run,
        id: 'run-2',
        window_start: '2026-10-03T09:00:00Z',
        window_end: run.window_start,
      },
    ];
    const el = await mount(
      '?section=valuable_work&start=2026-10-11T00:00:00Z&end=2026-10-16T00:00:00Z'
    );
    await waitUntil(
      () => api.callsTo('/api/v1/billing/weekly-value').length > 0
    );
    expect(
      new URLSearchParams(
        api.callsTo('/api/v1/billing/weekly-value')[0].search
      ).get('start')
    ).to.equal('2026-10-11T00:00:00Z');
    expect(
      scroll.calledOn(el.shadowRoot!.querySelector('weekly-value-section'))
    ).to.equal(true);
    const selector = el.shadowRoot!.querySelector(
      'select[name="run_id"]'
    ) as HTMLSelectElement;
    selector.value = 'run-2';
    selector.dispatchEvent(new Event('change', { bubbles: true }));
    await waitUntil(() =>
      api
        .callsTo('/api/v1/billing/weekly-value')
        .some(
          (c) =>
            new URLSearchParams(c.search).get('start') === weeks[1].window_start
        )
    );
  });

  it('uses the selected run when an outcomes link omits the window', async () => {
    valueEnabled = true;
    await mount('?section=valuable_work');
    await waitUntil(
      () => api.callsTo('/api/v1/billing/weekly-value').length > 0
    );
    const params = new URLSearchParams(
      api.callsTo('/api/v1/billing/weekly-value')[0].search
    );
    expect(params.get('start')).to.equal(run.window_start);
    expect(params.get('end')).to.equal(run.window_end);
  });
});
