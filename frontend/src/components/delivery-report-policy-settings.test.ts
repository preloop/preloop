import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import './delivery-report-policy-settings';
import type { DeliveryReportPolicySettings } from './delivery-report-policy-settings';

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const DEFAULTS = {
  enabled: true,
  cadence: 'weekly',
  weekday: 'mon',
  at: '07:00',
  timezone: 'UTC',
  projects: [],
  report_mode: 'deterministic',
  onboarding_assessed_run: true,
  per_run_usd: 5,
  monthly_usd: 25,
  drop_order: ['risk_detail', 'assessments_beyond_n'],
  max_assessed_changes: 25,
  pr_ready_days: 7,
  issue_inactive_days: 14,
  audit_ci_max_wait_minutes: 30,
  ui_screenshot_min_lines: 40,
  criteria_source: 'triage_assessment',
  tracker_writes: {
    create_follow_up: 'deny',
    reopen: 'approval',
    close: 'deny',
    follow_up_label: 'preloop:proposed',
  },
  recipients: 'account_owner',
  per_member_updates: false,
  sponsoring_lead_user_id: null,
  opted_out_user_ids: [],
};

function layer(level: 'account' | 'project', overrides = {}) {
  return {
    level,
    project_id: level === 'project' ? 'p1' : null,
    overrides,
    effective: { ...DEFAULTS, ...overrides },
    defaults: DEFAULTS,
    project_override_fields: [
      'criteria_source',
      'enabled',
      'issue_inactive_days',
      'audit_ci_max_wait_minutes',
      'pr_ready_days',
      'ui_screenshot_min_lines',
    ],
  };
}

function stubApi(
  options: {
    permissions?: string[];
    putStatus?: number;
    subscriptionPutStatus?: number;
    isRecipient?: boolean;
    memberUpdates?: boolean;
  } = {}
) {
  const puts: { path: string; body: any }[] = [];
  const subscription = {
    opted_out: false,
    is_recipient: options.isRecipient ?? true,
    report_enabled: true,
  };
  const stub = sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const path = new URL(url, window.location.origin).pathname;
      if (path === '/api/v1/features')
        return json({
          features: {
            delivery_report_member_updates: options.memberUpdates ?? false,
          },
        });
      if (path === '/api/v1/auth/users/me')
        return json({
          id: 'u1',
          permissions: options.permissions ?? [
            'manage_budgets',
            'edit_projects',
          ],
        });
      if (path === '/api/v1/delivery-report/subscription') {
        if (init?.method === 'PUT') {
          const body = JSON.parse(String(init.body));
          puts.push({ path, body });
          if (options.subscriptionPutStatus)
            return json(
              { detail: 'Subscription save failed.' },
              options.subscriptionPutStatus
            );
          return json({ ...subscription, opted_out: body.opted_out });
        }
        return json(subscription);
      }
      if (path.startsWith('/api/v1/delivery-report/policy')) {
        const level = path.includes('/projects/') ? 'project' : 'account';
        if (init?.method === 'PUT') {
          const body = JSON.parse(String(init.body));
          puts.push({ path, body });
          if (options.putStatus)
            return json(
              { detail: { code: 'budget_required', message: 'Need budgets.' } },
              options.putStatus
            );
          return json(layer(level, body.overrides));
        }
        return json(layer(level));
      }
      return json({}, 404);
    });
  return { stub, puts };
}

async function mount(projectId = ''): Promise<DeliveryReportPolicySettings> {
  const el = await fixture<DeliveryReportPolicySettings>(
    html`<delivery-report-policy-settings
      project-id=${projectId}
    ></delivery-report-policy-settings>`
  );
  await waitUntil(() => el.shadowRoot?.querySelector('section'));
  return el;
}

describe('delivery-report-policy-settings', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
  });

  afterEach(() => {
    localStorage.removeItem('accessToken');
    sinon.restore();
    invalidateApiCaches();
  });

  it('shows the defaults with assessment off and writes off', async () => {
    stubApi();
    const el = await mount();
    const root = el.shadowRoot!;
    const mode = root.querySelector(
      'sl-switch[data-field="report_mode"]'
    ) as any;
    expect(mode.checked).to.equal(false);
    expect(root.textContent).to.contain('$5.00 per run');
    expect(root.textContent).to.contain('$25.00 per month');
    const follow = root.querySelector(
      'sl-switch[data-field="create_follow_up"]'
    ) as any;
    expect(follow.checked).to.equal(false);
    const text = root.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('The report never closes an issue.');
  });

  it('shows an account opt-in only when the member update feature is enabled', async () => {
    const { puts } = stubApi({ memberUpdates: true });
    const el = await mount();
    const toggle = el.shadowRoot!.querySelector(
      'sl-switch[data-field="per_member_updates"]'
    ) as any;
    expect(toggle.checked).to.equal(false);
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('[data-action="save"]') as HTMLElement
    ).click();
    await waitUntil(() => puts.length === 1);
    expect(puts[0].body.overrides.per_member_updates).to.equal(true);
  });

  it('keeps the member opt-in off repository settings and OSS feature sets', async () => {
    stubApi({ memberUpdates: false });
    const account = await mount();
    expect(
      account.shadowRoot!.querySelector('[data-field="per_member_updates"]')
    ).to.equal(null);
    const project = await mount('p1');
    expect(
      project.shadowRoot!.querySelector('[data-field="per_member_updates"]')
    ).to.equal(null);
  });

  it('sends both budgets when assessment is turned on', async () => {
    const { puts } = stubApi();
    const el = await mount();
    const mode = el.shadowRoot!.querySelector(
      'sl-switch[data-field="report_mode"]'
    ) as any;
    mode.checked = true;
    mode.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('[data-action="save"]') as HTMLElement
    ).click();
    await waitUntil(() => puts.length === 1);
    expect(puts[0].body.overrides).to.deep.equal({
      report_mode: 'assessed',
      per_run_usd: 5,
      monthly_usd: 25,
    });
  });

  it('shows a refusal from the server', async () => {
    stubApi({ putStatus: 422 });
    const el = await mount();
    (
      el.shadowRoot!.querySelector('[data-action="save"]') as HTMLElement
    ).click();
    await waitUntil(() => el.shadowRoot!.querySelector('[role="alert"]'));
    expect(
      el.shadowRoot!.querySelector('[role="alert"]')!.textContent
    ).to.contain('Need budgets.');
  });

  it('edits only repository fields on a project', async () => {
    const { puts } = stubApi();
    const el = await mount('p1');
    const root = el.shadowRoot!;
    expect(root.querySelector('sl-switch[data-field="report_mode"]')).to.equal(
      null
    );
    const enabled = root.querySelector(
      'sl-switch[data-field="enabled"]'
    ) as any;
    enabled.checked = false;
    enabled.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
    (root.querySelector('[data-action="save"]') as HTMLElement).click();
    await waitUntil(() => puts.length === 1);
    expect(puts[0].path).to.equal('/api/v1/delivery-report/policy/projects/p1');
    expect(puts[0].body.overrides).to.deep.equal({ enabled: false });
  });

  it('saves a repository merge audit CI wait', async () => {
    const { puts } = stubApi();
    const el = await mount('p1');
    const wait = el.shadowRoot!.querySelector(
      'sl-input[data-field="audit_ci_max_wait_minutes"]'
    ) as HTMLInputElement;
    expect(wait).to.exist;
    wait.value = '15';
    wait.dispatchEvent(new Event('sl-input'));
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('[data-action="save"]') as HTMLElement
    ).click();
    await waitUntil(() => puts.length === 1);
    expect(puts[0].body.overrides).to.deep.equal({
      audit_ci_max_wait_minutes: 15,
    });
  });

  it('lets the recipient stop their own email without the permission', async () => {
    const { puts } = stubApi({ permissions: [] });
    const el = await mount();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('sl-switch[data-field="subscribed"]')
    );
    const toggle = el.shadowRoot!.querySelector(
      'sl-switch[data-field="subscribed"]'
    ) as any;
    expect(toggle.checked).to.equal(true);
    expect(toggle.disabled).to.equal(false);
    toggle.checked = false;
    toggle.dispatchEvent(new Event('sl-change'));
    await waitUntil(() => puts.length === 1);
    expect(puts[0]).to.deep.equal({
      path: '/api/v1/delivery-report/subscription',
      body: { opted_out: true },
    });
  });

  it('restores the saved subscription when the request fails', async () => {
    stubApi({ subscriptionPutStatus: 500 });
    const el = await mount();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('sl-switch[data-field="subscribed"]')
    );
    const toggle = el.shadowRoot!.querySelector(
      'sl-switch[data-field="subscribed"]'
    ) as any;
    expect(toggle.checked).to.equal(true);
    toggle.checked = false;
    toggle.dispatchEvent(new Event('sl-change'));
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Subscription save failed.')
    );
    await el.updateComplete;
    expect(toggle.checked).to.equal(true);
  });

  it('shows no email switch to someone who is not the recipient', async () => {
    stubApi({ isRecipient: false });
    const el = await mount();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('sl-switch[data-field="subscribed"]')
    ).to.equal(null);
  });

  it('is read-only without the permission', async () => {
    stubApi({ permissions: [] });
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-action="save"]')).to.equal(null);
    expect(el.shadowRoot!.textContent).to.contain('manage budgets');
  });
});
