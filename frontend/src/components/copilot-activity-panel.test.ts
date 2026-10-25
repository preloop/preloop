import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './copilot-activity-panel';
import type { CopilotActivityPanel } from './copilot-activity-panel';

const activity = {
  organization: 'example-org',
  usage_source: 'vendor_reported',
  metered_by_gateway: false,
  is_active: true,
  snapshot_day: '2026-10-08',
  active_days: 30,
  seats: [
    {
      github_login: 'jane',
      user_id: 'user-1',
      last_activity_at: '2026-10-08T12:00:00Z',
      last_activity_editor: 'vscode',
      active: true,
      governed_agent: false,
      premium_net_amount: null,
    },
    {
      github_login: 'unmapped',
      user_id: null,
      last_activity_at: null,
      last_activity_editor: null,
      active: true,
      governed_agent: false,
      premium_net_amount: 0,
    },
    {
      github_login: 'governed',
      user_id: 'user-2',
      last_activity_at: null,
      last_activity_editor: null,
      active: true,
      governed_agent: true,
      premium_net_amount: 2,
    },
  ],
  gaps: [
    { github_login: 'jane', user_id: 'user-1', reason: 'no_governed_agent' },
    { github_login: 'unmapped', user_id: null, reason: 'unmapped_login' },
  ],
};
const metrics = {
  organization: 'example-org',
  usage_source: 'vendor_reported',
  metered_by_gateway: false,
  days: [
    {
      day: '2026-10-08',
      status: 'available',
      reason: null,
      active_users: 4,
      engagement_window_days: 28,
      engagement_active_users: 8,
      engaged_users: null,
      user_initiated_interaction_count: 12,
      code_generation_activity_count: 8,
      code_acceptance_activity_count: 3,
      feature_usage: [
        {
          feature: 'chat',
          engaged_users: 3,
          user_initiated_interaction_count: 7,
        },
      ],
      editor_usage: [{ editor: 'vscode', user_initiated_interaction_count: 9 }],
    },
  ],
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('Copilot vendor activity', () => {
  let fetchStub: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (url) =>
        json(String(url).includes('organization-metrics') ? metrics : activity)
      );
  });
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
  });

  it('does not request data when the feature is disabled', async () => {
    const el = await fixture<CopilotActivityPanel>(
      html`<copilot-activity-panel></copilot-activity-panel>`
    );
    await el.updateComplete;
    expect(fetchStub.callCount).to.equal(0);
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });

  it('shows identity gaps and vendor daily counters with explicit unknowns', async () => {
    const el = await fixture<CopilotActivityPanel>(
      html`<copilot-activity-panel
        enabled
        startDate="2026-10-01T00:00:00Z"
        endDate="2026-10-10T00:00:00Z"
      ></copilot-activity-panel>`
    );
    await waitUntil(
      () => !!el.shadowRoot!.querySelector('[data-testid="copilot-daily"]')
    );
    const text = el.shadowRoot!.textContent!;
    expect(text).to.include('Vendor-reported');
    expect(text).to.include('Not metered by the gateway');
    expect(text).to.include('No governed agent');
    expect(text).to.include('Identity link missing');
    expect(text).to.include('Unknown');
    expect(text).to.include('Daily feature usage');
    expect(text).to.include('Rolling 28-day engagement');
    expect(text).to.include('Engaged users (28 days)');
    const dailyFeatureTable = Array.from(
      el.shadowRoot!.querySelectorAll('table')
    ).find((table) =>
      table
        .querySelector('caption')
        ?.textContent?.includes('Daily feature usage')
    )!;
    expect(dailyFeatureTable.textContent).to.not.include('Engaged users');
    expect(text).to.match(/Distinct active users in this 28-day window:\s*8/);

    expect(text).to.include('vscode');
    expect(String(fetchStub.lastCall.args[0])).to.include(
      'start_date=2026-10-01'
    );
    const rows = el.shadowRoot!.querySelectorAll(
      '[data-testid="copilot-seat-activity"] tbody tr'
    );
    expect(rows[0].textContent).to.include('Unknown');
    expect(rows[1].textContent).to.include('$0.00');
  });

  it('shows only gap seats in inventory and does not query daily metrics', async () => {
    const el = await fixture<CopilotActivityPanel>(
      html`<copilot-activity-panel enabled inventory></copilot-activity-panel>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
    expect(el.shadowRoot!.querySelectorAll('tbody tr').length).to.equal(2);
    expect(el.shadowRoot!.textContent).to.include('Copilot inventory gaps');
    expect(fetchStub.callCount).to.equal(1);
  });

  for (const status of [402, 403, 404]) {
    it(`hides data and avoids upgrade popups for HTTP ${status}`, async () => {
      fetchStub.callsFake(async () =>
        json(
          { detail: { code: 'upgrade_required', feature: 'reconciliation' } },
          status
        )
      );
      const event = sinon.spy(window, 'dispatchEvent');
      const el = await fixture<CopilotActivityPanel>(
        html`<copilot-activity-panel enabled></copilot-activity-panel>`
      );
      await waitUntil(() => !el['loading']);
      expect(el.shadowRoot!.querySelector('section')).to.equal(null);
      expect(
        event
          .getCalls()
          .some((call) => String(call.args[0].type).includes('upgrade'))
      ).to.equal(false);
    });
  }

  it('clears loaded data immediately when the feature is disabled', async () => {
    const el = await fixture<CopilotActivityPanel>(
      html`<copilot-activity-panel enabled inventory></copilot-activity-panel>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
    el.enabled = false;
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });

  it('rejects malformed or unlabelled data without rendering vendor numbers', async () => {
    fetchStub.callsFake(async () => json({ seats: [], gaps: [] }));
    const el = await fixture<CopilotActivityPanel>(
      html`<copilot-activity-panel enabled></copilot-activity-panel>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('[role="alert"]'));
    expect(el.shadowRoot!.querySelector('table')).to.equal(null);
  });

  for (const malformed of [
    { ...activity, seats: [null] },
    { ...activity, gaps: [null] },
    {
      ...activity,
      seats: [{ ...activity.seats[0], premium_net_amount: 'invalid' }],
    },
  ]) {
    it('rejects malformed seat and gap items before rendering', async () => {
      fetchStub.callsFake(async () => json(malformed));
      const el = await fixture<CopilotActivityPanel>(
        html`<copilot-activity-panel enabled></copilot-activity-panel>`
      );
      await waitUntil(() => !!el.shadowRoot!.querySelector('[role="alert"]'));
      expect(el.shadowRoot!.querySelector('table')).to.equal(null);
    });
  }
  for (const days of [
    [null],
    [{ ...metrics.days[0], feature_usage: null }],
    [{ ...metrics.days[0], editor_usage: [null] }],
  ]) {
    it('rejects malformed nested daily metrics', async () => {
      fetchStub.callsFake(async (url) =>
        json(
          String(url).includes('organization-metrics')
            ? { ...metrics, days }
            : activity
        )
      );
      const el = await fixture<CopilotActivityPanel>(
        html`<copilot-activity-panel enabled></copilot-activity-panel>`
      );
      await waitUntil(() => !!el.shadowRoot!.querySelector('[role="alert"]'));
      expect(
        el.shadowRoot!.querySelector('[data-testid="copilot-daily"]')
      ).to.equal(null);
    });
  }

  it('reports a failed import read and allows a retry', async () => {
    fetchStub.callsFake(async () => json({}, 500));
    const el = await fixture<CopilotActivityPanel>(
      html`<copilot-activity-panel enabled inventory></copilot-activity-panel>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('[role="alert"]'));
    fetchStub.callsFake(async () => json(activity));
    el.shadowRoot!.querySelector('button')!.click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
  });
});
