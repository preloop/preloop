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
  ui_screenshot_min_lines: 40,
  criteria_source: 'triage_assessment',
  tracker_writes: {
    create_follow_up: 'deny',
    reopen: 'approval',
    close: 'deny',
    follow_up_label: 'preloop:proposed',
  },
  recipients: 'account_owner',
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
      'pr_ready_days',
      'ui_screenshot_min_lines',
    ],
  };
}

function stubApi(options: { permissions?: string[]; putStatus?: number } = {}) {
  const puts: { path: string; body: any }[] = [];
  const stub = sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const path = new URL(url, window.location.origin).pathname;
      if (path === '/api/v1/auth/users/me')
        return json({
          id: 'u1',
          permissions: options.permissions ?? [
            'manage_budgets',
            'edit_projects',
          ],
        });
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

  it('is read-only without the permission', async () => {
    stubApi({ permissions: [] });
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-action="save"]')).to.equal(null);
    expect(el.shadowRoot!.textContent).to.contain('manage budgets');
  });
});
