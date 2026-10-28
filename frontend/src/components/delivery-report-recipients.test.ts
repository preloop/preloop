import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import './delivery-report-policy-settings';
import type { DeliveryReportPolicySettings } from './delivery-report-policy-settings';

const RECIPIENTS = {
  per_member: false,
  recipients: 'account_owner',
  sponsoring_lead_user_id: null,
  opted_out_user_ids: [] as string[],
  members: [
    {
      user_id: 'u1',
      name: 'Account owner',
      email: 'owner@example.com',
      rbac_roles: ['owner'],
      report_role: 'owner',
      role_source: 'owner',
      delivery: 'full_report',
      opted_out: false,
    },
    {
      user_id: 'u2',
      name: 'Jane Doe',
      email: 'jane@example.com',
      rbac_roles: ['tracker_manager'],
      report_role: 'lead',
      role_source: 'rbac',
      delivery: 'none',
      opted_out: false,
    },
  ],
};
const POLICY = {
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
  drop_order: [],
  max_assessed_changes: 25,
  pr_ready_days: 7,
  issue_inactive_days: 14,
  audit_ci_max_wait_minutes: 30,
  ui_screenshot_min_lines: 40,
  criteria_source: 'description',
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
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function stubApi(
  options: {
    enabled?: boolean;
    permissions?: string[];
    putStatus?: number;
    readStatus?: number;
    memberUpdates?: boolean;
    policyOverrides?: Record<string, unknown>;
    readDeferred?: Promise<Response>;
  } = {}
) {
  const puts: { path: string; body: any }[] = [];
  const data = structuredClone(RECIPIENTS);
  const stub = sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), location.origin).pathname;
      if (path === '/api/v1/auth/users/me')
        return json({
          id: 'u1',
          permissions: options.permissions ?? [
            'manage_budgets',
            'view_policies',
            'manage_policies',
          ],
        });
      if (path === '/api/v1/features')
        return json({
          features: {
            delivery_report_recipients: options.enabled ?? true,
            delivery_report_member_updates: options.memberUpdates ?? true,
          },
        });
      if (path === '/api/v1/delivery-report/recipients') {
        if (init?.method === 'PUT') {
          const body = JSON.parse(String(init.body));
          puts.push({ path, body });
          if (options.putStatus)
            return json(
              {
                detail: {
                  code: 'unknown_member',
                  message: 'A member has left the account.',
                },
              },
              options.putStatus
            );
          Object.assign(data, body);
          for (const member of data.members) {
            if (body.roles && member.user_id in body.roles) {
              member.report_role = body.roles[member.user_id] ?? 'lead';
              member.role_source = body.roles[member.user_id]
                ? 'override'
                : 'rbac';
            }
            member.opted_out = data.opted_out_user_ids.includes(member.user_id);
          }
        } else if (options.readDeferred) return options.readDeferred;
        else if (options.readStatus)
          return json(
            { detail: 'Cannot read recipients.' },
            options.readStatus
          );
        return json(data);
      }
      if (path.startsWith('/api/v1/delivery-report/policy')) {
        const project = path.includes('/projects/');
        if (init?.method === 'PUT')
          puts.push({ path, body: JSON.parse(String(init.body)) });
        return json({
          level: project ? 'project' : 'account',
          project_id: project ? 'p1' : null,
          overrides: options.policyOverrides ?? {},
          defaults: POLICY,
          effective: { ...POLICY, ...options.policyOverrides },
          project_override_fields: [],
        });
      }
      if (path === '/api/v1/delivery-report/subscription')
        return json({
          opted_out: false,
          is_recipient: true,
          report_enabled: true,
        });
      return json({}, 404);
    });
  return { puts, stub };
}
async function mount(projectId = '') {
  const el = await fixture<DeliveryReportPolicySettings>(
    html`<delivery-report-policy-settings
      project-id=${projectId}
    ></delivery-report-policy-settings>`
  );
  await waitUntil(() => el.shadowRoot?.querySelector('section'));
  return el;
}
async function roster(el: DeliveryReportPolicySettings) {
  await waitUntil(() => el.shadowRoot!.querySelector('[data-recipient="u2"]'));
  return el.shadowRoot!;
}
function change(root: ShadowRoot, selector: string, value: string | boolean) {
  const field = root.querySelector(selector) as any;
  if (typeof value === 'boolean') field.checked = value;
  else field.value = value;
  field.dispatchEvent(new Event('sl-change'));
}
async function save(el: DeliveryReportPolicySettings) {
  await el.updateComplete;
  (
    el.shadowRoot!.querySelector(
      '[data-action="save-recipients"]'
    ) as HTMLElement
  ).click();
}

describe('enterprise Delivery Report recipients', () => {
  beforeEach(() => localStorage.setItem('accessToken', 'synthetic-test-token'));
  afterEach(() => {
    localStorage.removeItem('accessToken');
    sinon.restore();
    invalidateApiCaches();
  });
  it('hides the entire section and makes no recipient requests without the plugin', async () => {
    const { stub } = stubApi({ enabled: false });
    const el = await mount();
    expect(el.shadowRoot!.textContent).not.to.contain('Recipients');
    expect(
      stub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/delivery-report/recipients'))
    ).to.equal(false);
  });
  it('saves member opt-in and chooses a sponsoring lead from active members', async () => {
    const { puts } = stubApi();
    const el = await mount();
    const root = await roster(el);
    change(root, '[data-field="per_member_updates"]', true);
    change(root, '[data-field="recipients"]', 'sponsoring_lead');
    await el.updateComplete;
    const lead = root.querySelector('[data-field="sponsoring_lead_user_id"]')!;
    expect(lead.tagName).to.equal('SL-SELECT');
    expect(lead.textContent).to.contain('Jane Doe');
    change(root, '[data-field="sponsoring_lead_user_id"]', 'u2');
    await save(el);
    await waitUntil(() => puts.length === 1);
    expect(puts[0]).to.deep.equal({
      path: '/api/v1/delivery-report/recipients',
      body: {
        per_member: true,
        recipients: 'sponsoring_lead',
        sponsoring_lead_user_id: 'u2',
      },
    });
  });
  it('shows role mapping, delivery and a fixed account-owner role', async () => {
    stubApi();
    const el = await mount();
    const root = await roster(el);
    expect(root.textContent).to.contain('tracker_manager');
    expect(root.textContent).to.contain('Full report');
    expect(root.textContent).to.contain('RBAC');
    expect(
      (root.querySelector('[data-role-user="u1"]') as any).disabled
    ).to.equal(true);
    expect((root.querySelector('[data-role-user="u2"]') as any).value).to.equal(
      'default'
    );
  });
  it('saves role overrides and per-person opt-out, and clears an override to its default', async () => {
    const { puts } = stubApi();
    const el = await mount();
    const root = await roster(el);
    change(root, '[data-role-user="u2"]', 'viewer');
    change(root, '[data-opt-out-user="u2"]', true);
    await save(el);
    await waitUntil(() => puts.length === 1);
    expect(puts[0].body).to.deep.equal({
      roles: { u2: 'viewer' },
      opted_out_user_ids: ['u2'],
    });
    await waitUntil(
      () =>
        !(root.querySelector('[data-action="save-recipients"]') as any).loading
    );
    change(root, '[data-role-user="u2"]', 'default');
    await save(el);
    await waitUntil(() => puts.length === 2);
    expect(puts[1].body).to.deep.equal({ roles: { u2: null } });
  });
  it('uses manage_policies independently of budget editing', async () => {
    stubApi({ permissions: ['view_policies', 'manage_policies'] });
    const el = await mount();
    const root = await roster(el);
    expect(root.querySelector('[data-action="save"]')).to.equal(null);
    expect(root.querySelector('[data-action="save-recipients"]')).to.exist;
    expect(
      (root.querySelector('[data-role-user="u2"]') as any).disabled
    ).to.equal(false);
  });
  it('keeps roles read-only for a budget manager without manage_policies', async () => {
    stubApi({ permissions: ['view_policies', 'manage_budgets'] });
    const el = await mount();
    const root = await roster(el);
    expect(root.querySelector('[data-action="save-recipients"]')).to.equal(
      null
    );
    expect(
      (root.querySelector('[data-role-user="u2"]') as any).disabled
    ).to.equal(true);
  });
  it('does not fetch the roster without view_policies', async () => {
    const { stub } = stubApi({ permissions: [] });
    const el = await mount();
    expect(el.shadowRoot!.textContent).not.to.contain('Recipients');
    expect(
      stub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/delivery-report/recipients'))
    ).to.equal(false);
  });
  it('keeps recipients off project settings', async () => {
    const { stub } = stubApi();
    const el = await mount('p1');
    expect(el.shadowRoot!.textContent).not.to.contain('Recipients');
    expect(
      stub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/delivery-report/recipients'))
    ).to.equal(false);
  });
  it('shows a failed save without losing role changes', async () => {
    stubApi({ putStatus: 422 });
    const el = await mount();
    const root = await roster(el);
    change(root, '[data-role-user="u2"]', 'viewer');
    await save(el);
    await waitUntil(() =>
      root.textContent!.includes('A member has left the account.')
    );
    expect((root.querySelector('[data-role-user="u2"]') as any).value).to.equal(
      'viewer'
    );
  });
  it('clears account data on authentication invalidation', async () => {
    stubApi();
    const el = await mount();
    await roster(el);
    invalidateApiCaches();
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).not.to.contain('jane@example.com');
  });
  it('keeps the member-update control hidden when that capability is unavailable', async () => {
    stubApi({ memberUpdates: false });
    const el = await mount();
    const root = await roster(el);
    expect(root.querySelector('[data-field="per_member_updates"]')).to.equal(
      null
    );
  });
  it('requires a selected sponsoring lead before saving', async () => {
    const { puts } = stubApi();
    const el = await mount();
    const root = await roster(el);
    change(root, '[data-field="recipients"]', 'sponsoring_lead');
    await save(el);
    await waitUntil(() =>
      root.textContent!.includes('Choose the sponsoring lead')
    );
    expect(puts).to.have.length(0);
  });
  it('shows recipient read failures without hiding the policy', async () => {
    stubApi({ readStatus: 503 });
    const el = await mount();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Cannot read recipients.')
    );
    expect(el.shadowRoot!.querySelector('[data-action="retry-recipients"]')).to
      .exist;
    expect(el.shadowRoot!.querySelector('[data-action="save"]')).to.exist;
  });
  it('preserves freshly read recipient overrides when saving budgets with the feature off', async () => {
    const policyOverrides: Record<string, unknown> = {
      per_member_updates: true,
    };
    const { puts } = stubApi({ enabled: false, policyOverrides });
    const el = await mount();
    Object.assign(policyOverrides, {
      recipients: 'sponsoring_lead',
      sponsoring_lead_user_id: 'u2',
    });
    (
      el.shadowRoot!.querySelector('[data-action="save"]') as HTMLElement
    ).click();
    await waitUntil(() => puts.length === 1);
    expect(puts[0].body.overrides).to.deep.equal(policyOverrides);
    expect(puts[0].path).to.equal('/api/v1/delivery-report/policy');
  });
  it('ignores a roster response from a previous authentication context', async () => {
    let finish!: (response: Response) => void;
    const readDeferred = new Promise<Response>((resolve) => {
      finish = resolve;
    });
    const { stub } = stubApi({ readDeferred });
    const el = await mount();
    await waitUntil(() =>
      stub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/delivery-report/recipients'))
    );
    invalidateApiCaches();
    finish(json(RECIPIENTS));
    await new Promise((resolve) => setTimeout(resolve, 0));
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).not.to.contain('jane@example.com');
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });
});
