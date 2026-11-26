import {
  expect,
  fixture,
  html,
  waitUntil,
  fixtureCleanup,
} from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import type {
  RunnerPolicy,
  RunnerPolicyEntry,
  MemberCompliance,
} from '../api/runner-policy';
import { RunnerPolicySettings } from './runner-policy-settings';
import { RunnerComplianceView } from './runner-compliance';
import { RunnerPolicyBanner } from './runner-policy-banner';
import './runner-policy-settings';
import './runner-compliance';
import './runner-policy-banner';

const entry = (
  requirement: RunnerPolicyEntry['requirement'] = 'optional'
): RunnerPolicyEntry => ({
  requirement,
  mandated_capabilities: [],
  grace_days: 7,
  enforcement: 'none',
});
const member = (
  state: MemberCompliance['state'],
  user_id = state
): MemberCompliance => ({
  user_id,
  username: `Member ${user_id}`,
  requirement: 'required',
  enforcement: 'warn',
  policy_source: 'account',
  state,
  inventory_known: true,
  inventory_fresh: true,
  runners: [],
  harnesses: [],
  ungoverned_harnesses: [],
});

describe('runner requirement console', () => {
  let enabled: boolean;
  let permissions: string[];
  let policy: RunnerPolicy;
  let members: MemberCompliance[];
  let total: number;
  let mine: Record<string, unknown>;
  let requests: { url: string; init?: RequestInit }[];
  let rejectSave: boolean;
  let rejectRoster: boolean;
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    localStorage.setItem('accessToken', 'synthetic-token');
    invalidateApiCaches();
    enabled = true;
    permissions = [
      'view_policies',
      'manage_policies',
      'view_users',
      'view_teams',
    ];
    policy = {
      account: entry(),
      roles: { 'role-existing': entry('forbidden') },
      teams: {},
      stale_after_days: 3,
    };
    members = [member('never_connected')];
    total = 1;
    rejectSave = false;
    rejectRoster = false;
    mine = {
      requirement: 'required',
      enforcement: 'warn',
      state: 'never_connected',
      show_banner: true,
      message: 'Please connect your work runner.',
    };
    requests = [];
    sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      requests.push({ url, init });
      if (url.endsWith('/features'))
        return json({ features: { runner_requirement_policy: enabled } });
      if (url.endsWith('/auth/users/me'))
        return json({ id: 'member', account_id: 'account', permissions });
      if (url.endsWith('/roles'))
        return json({
          roles: [{ id: 'role-existing', name: 'Reviewers' }],
          total: 1,
        });
      if (url.includes('/teams?'))
        return json({
          teams: [{ id: 'team-new', name: 'Engineering' }],
          total: 1,
        });
      if (url.endsWith('/runner-policy')) {
        if (init?.method === 'PUT') {
          if (rejectSave) return json({ detail: 'Policy edit denied' }, 403);
          policy = JSON.parse(init.body as string);
        }
        return json(policy);
      }
      if (url.endsWith('/runner-compliance/me')) return json(mine);
      if (url.includes('/runner-compliance?')) {
        if (rejectRoster)
          return json({ detail: 'Temporary roster failure' }, 503);
        return json({
          members,
          total,
          skip: Number(new URL(url, location.origin).searchParams.get('skip')),
          limit: 100,
          counts: { never_connected: members.length },
          generated_at: '2026-01-01T12:00:00Z',
        });
      }
      return json({ detail: 'Unexpected request' }, 404);
    });
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    invalidateApiCaches();
    localStorage.removeItem('accessToken');
  });

  const settings = () =>
    fixture<RunnerPolicySettings>(
      html`<runner-policy-settings></runner-policy-settings>`
    );
  const roster = () =>
    fixture<RunnerComplianceView>(
      html`<runner-compliance></runner-compliance>`
    );
  const banner = () =>
    fixture<RunnerPolicyBanner>(
      html`<runner-policy-banner></runner-policy-banner>`
    );
  async function loaded(el: RunnerPolicySettings) {
    await waitUntil(() => !!el.shadowRoot?.querySelector('form'));
    await el.updateComplete;
  }
  const putRequests = () => requests.filter((r) => r.init?.method === 'PUT');

  it('never calls EE routes with the feature disabled', async () => {
    enabled = false;
    const elements = await Promise.all([settings(), roster(), banner()]);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(
      requests.some((r) => /runner-policy|runner-compliance/.test(r.url))
    ).to.equal(false);
    elements.forEach((el) =>
      expect(el.shadowRoot!.textContent!.trim()).to.equal('')
    );
  });
  it('does not fetch policy or roster without their read permissions', async () => {
    permissions = [];
    await Promise.all([settings(), roster()]);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(
      requests.some((r) => /runner-policy|runner-compliance/.test(r.url))
    ).to.equal(false);
  });
  it('shows read-only policy and prevents forged submission without manage permission', async () => {
    permissions = ['view_policies'];
    const el = await settings();
    await loaded(el);
    expect(el.shadowRoot!.textContent).to.include('Read only');
    expect(el.shadowRoot!.querySelector('fieldset')!.disabled).to.equal(true);
    el.shadowRoot!.querySelector('form')!.dispatchEvent(
      new SubmitEvent('submit', { cancelable: true })
    );
    await el.updateComplete;
    expect(putRequests()).to.have.length(0);
    expect(
      requests.some((r) => r.url.includes('/teams') || r.url.endsWith('/roles'))
    ).to.equal(false);
  });
  for (const requirement of ['required', 'forbidden', 'optional'] as const) {
    it(`saves ${requirement}, capabilities and existing role overrides`, async () => {
      const el = await settings();
      await loaded(el);
      const requirementSelect = el.shadowRoot!.querySelector('select')!;
      requirementSelect.value = requirement;
      requirementSelect.dispatchEvent(new Event('change'));
      await el.updateComplete;
      const checkbox = el.shadowRoot!.querySelector<HTMLInputElement>(
        'input[type=checkbox]'
      );
      if (requirement === 'required') {
        checkbox!.checked = true;
        checkbox!.dispatchEvent(new Event('change'));
      }
      el.shadowRoot!.querySelector('form')!.dispatchEvent(
        new SubmitEvent('submit', { cancelable: true })
      );
      await waitUntil(() => putRequests().length === 1);
      const body = JSON.parse(putRequests()[0].init!.body as string);
      expect(body.account.requirement).to.equal(requirement);
      expect(body.account.mandated_capabilities).to.deep.equal(
        requirement === 'required' ? ['flows'] : []
      );
      expect(body.roles['role-existing'].requirement).to.equal('forbidden');
      expect(
        el.shadowRoot!.querySelectorAll('option[value=block_routing]')
      ).to.have.length(0);
    });
  }
  it('adds named team overrides and removes role overrides without changing account defaults', async () => {
    const el = await settings();
    await loaded(el);
    await waitUntil(
      () => !!el.shadowRoot!.querySelector('option[value="teams:team-new"]')
    );
    const add = el.shadowRoot!.querySelector<HTMLSelectElement>(
      'select[aria-label="Add role or team override"]'
    )!;
    add.value = 'teams:team-new';
    add.dispatchEvent(new Event('change'));
    await el.updateComplete;
    const remove = Array.from(el.shadowRoot!.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Remove override')
    )!;
    remove.click();
    await el.updateComplete;
    el.shadowRoot!.querySelector('form')!.dispatchEvent(
      new SubmitEvent('submit', { cancelable: true })
    );
    await waitUntil(() => putRequests().length === 1);
    expect(policy.teams['team-new']).to.deep.equal(entry());
    expect(policy.roles).to.deep.equal({});
    expect(policy.account).to.deep.equal(entry());
  });
  it('does not list teams without view_teams while allowing role choices', async () => {
    permissions = ['view_policies', 'manage_policies', 'view_users'];
    const el = await settings();
    await loaded(el);
    await waitUntil(
      () =>
        !!el.shadowRoot!.querySelector('option[value="roles:role-existing"]') ||
        requests.some((r) => r.url.endsWith('/roles'))
    );
    expect(requests.some((r) => r.url.includes('/teams?'))).to.equal(false);
  });
  it('lists teams independently of roster permission', async () => {
    permissions = ['view_policies', 'manage_policies', 'view_teams'];
    const el = await settings();
    await loaded(el);
    await waitUntil(
      () => !!el.shadowRoot!.querySelector('option[value="teams:team-new"]')
    );
    expect(requests.some((r) => r.url.endsWith('/roles'))).to.equal(false);
  });
  it('keeps edits and displays server permission failures', async () => {
    rejectSave = true;
    const el = await settings();
    await loaded(el);
    el.shadowRoot!.querySelector('form')!.dispatchEvent(
      new SubmitEvent('submit', { cancelable: true })
    );
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Policy edit denied')
    );
    expect(el.shadowRoot!.textContent).not.to.include('Runner policy saved.');
    expect(el.shadowRoot!.querySelector('form')).to.exist;
  });
  it('renders every compliance state and truthful harness governance and freshness', async () => {
    members = [
      'compliant',
      'stale',
      'never_connected',
      'in_grace',
      'exempt',
    ].map((s) => member(s as MemberCompliance['state']));
    total = members.length;
    members[1].inventory_fresh = false;
    members[1].last_heartbeat = '2026-01-01T12:00:00Z';
    members[1].harnesses = [
      {
        harness: 'claude-desktop',
        display_name: 'Claude Desktop',
        governance: 'ungoverned',
        runner_name: 'Work laptop',
      },
      {
        harness: 'opencode',
        display_name: 'OpenCode',
        governance: 'ungoverned',
        runner_name: 'Work laptop',
      },
    ];
    members[2].inventory_known = false;
    const el = await roster();
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
    const text = el.shadowRoot!.textContent!;
    [
      'Compliant',
      'Stale',
      'Never connected',
      'In grace period',
      'Exempt',
      'Claude Desktop',
      'OpenCode',
      'ungoverned',
      'Stale inventory',
      'Inventory unknown',
    ].forEach((t) => expect(text).to.include(t));
    const inventory = el.shadowRoot!.querySelector('td details')!;
    expect(inventory.hasAttribute('open')).to.be.false;
    const toggle = inventory.querySelector('summary')!;
    expect(toggle.textContent?.replace(/\s+/g, ' ')).to.include(
      '2 harness entries'
    );
    expect(toggle.textContent).to.include('2 ungoverned');
    toggle.click();
    expect(inventory.hasAttribute('open')).to.be.true;
    expect(inventory.textContent).to.include('Work laptop');
    expect(el.shadowRoot!.querySelectorAll('tbody tr')).to.have.length(5);
  });
  it('paginates beyond the first hundred members', async () => {
    members = Array.from({ length: 100 }, (_, i) =>
      member('compliant', `user-${i}`)
    );
    total = 101;
    const el = await roster();
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
    members = [member('stale', 'last-member')];
    Array.from(el.shadowRoot!.querySelectorAll('button'))
      .find((b) => b.textContent?.includes('Next members'))!
      .click();
    await waitUntil(() => el.shadowRoot!.textContent!.includes('last-member'));
    expect(requests.some((r) => r.url.includes('skip=100&limit=100'))).to.equal(
      true
    );
    expect(el.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.include(
      '101–101 of 101'
    );
  });
  it('keeps the current roster and page after failed pagination or refresh', async () => {
    members = Array.from({ length: 100 }, (_, i) =>
      member('compliant', `user-${i}`)
    );
    total = 201;
    const el = await roster();
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
    members = [member('stale', 'second-page')];
    Array.from(el.shadowRoot!.querySelectorAll('button'))
      .find((b) => b.textContent?.includes('Next members'))!
      .click();
    await waitUntil(() => el.shadowRoot!.textContent!.includes('second-page'));
    rejectRoster = true;
    Array.from(el.shadowRoot!.querySelectorAll('button'))
      .find((b) => b.textContent?.includes('Next members'))!
      .click();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Temporary roster failure')
    );
    expect(el.shadowRoot!.textContent).to.include('second-page');
    Array.from(el.shadowRoot!.querySelectorAll('button'))
      .find((b) => b.textContent?.includes('Refresh compliance'))!
      .click();
    await waitUntil(
      () =>
        requests.filter((r) => r.url.includes('skip=100&limit=100')).length ===
        2
    );
    expect(el.shadowRoot!.textContent).to.include('second-page');
  });

  it('preserves unknown server states and uses friendly labels for page counts', async () => {
    members = [member('future_state' as MemberCompliance['state'])];
    const el = await roster();
    await waitUntil(() => !!el.shadowRoot!.querySelector('table'));
    expect(el.shadowRoot!.textContent).to.include('future_state');
    expect(el.shadowRoot!.textContent).to.include('Never connected: 1');
  });

  it('warns an ordinary member through only the self endpoint', async () => {
    permissions = [];
    const el = await banner();
    await waitUntil(() => !!el.shadowRoot!.querySelector('aside'));
    expect(el.shadowRoot!.textContent).to.include(
      'Please connect your work runner.'
    );
    expect(
      requests.some((r) => r.url.endsWith('/runner-compliance/me'))
    ).to.equal(true);
    expect(
      requests.some((r) => r.url.includes('/runner-compliance?'))
    ).to.equal(false);
  });
  for (const override of [
    { show_banner: false },
    { enforcement: 'none' },
    { requirement: 'optional' },
    { requirement: 'forbidden' },
  ]) {
    it(`hides warning for ${JSON.stringify(override)}`, async () => {
      Object.assign(mine, override);
      const el = await banner();
      await waitUntil(() =>
        requests.some((r) => r.url.endsWith('/runner-compliance/me'))
      );
      await new Promise((resolve) => setTimeout(resolve, 10));
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('aside')).not.to.exist;
    });
  }
  it('refreshes member warning when a policy is saved', async () => {
    const el = await banner();
    await waitUntil(() => !!el.shadowRoot!.querySelector('aside'));
    mine.show_banner = false;
    window.dispatchEvent(new Event('runner-policy-changed'));
    await waitUntil(() => !el.shadowRoot!.querySelector('aside'));
  });
});
