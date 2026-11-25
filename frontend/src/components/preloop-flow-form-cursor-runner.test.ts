import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
} from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import { invalidateUserProfileCache } from '../api';

const OWNER_ID = 'user-1';
const RUNNER_ID = '11111111-1111-4111-8111-111111111111';

/** A personal runner with a signed-in Cursor CLI, as `preloop runner` reports it. */
function cursorRunner(overrides: Record<string, unknown> = {}) {
  return {
    id: RUNNER_ID,
    name: 'jane-mac',
    labels: ['personal', 'os:darwin', 'arch:arm64'],
    status: 'online',
    presence: 'online',
    accept_untagged: false,
    flow_execution_consent: 'ask_me',
    registered_by_user_id: OWNER_ID,
    capabilities: {
      host_exec_profiles: [
        {
          name: 'cursor',
          capabilities: ['host_exec', 'cursor_cli'],
          models: ['auto'],
        },
      ],
    },
    harness_inventory: {
      schema: 1,
      generated_at: '2026-11-23T10:00:00Z',
      hash: 'h1',
      entries: [
        {
          harness: 'cursor_cli',
          display_name: 'Cursor CLI',
          login_state: 'signed_in',
          login_source: 'cli',
          governance: 'partial',
          support_level: 'flows_only',
          enabled: true,
          sessions_enabled: false,
          session_mode: 'none',
          billing: 'seat',
          models: [{ id: 'auto', source: 'profile' }],
          generated_profile: 'cursor',
          capabilities: ['host_exec', 'cursor_cli'],
        },
      ],
    },
    ...overrides,
  };
}

/** A runner that reports Cursor but is signed out, so offers no profile. */
function signedOutRunner() {
  const runner = cursorRunner({
    id: '22222222-2222-4222-8222-222222222222',
    name: 'build-box',
    capabilities: { host_exec_profiles: [] },
  });
  (runner.harness_inventory as any).entries[0].login_state = 'signed_out';
  (runner.harness_inventory as any).entries[0].generated_profile = null;
  return runner;
}

describe('PreloopFlowForm Cursor on my runner', () => {
  let sandbox: SinonSandbox;
  let runners: any[];
  let agents: any[];
  let consentCalls: Array<{ url: string; body: any }>;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    invalidateUserProfileCache();
    runners = [cursorRunner()];
    agents = [];
    consentCalls = [];
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any, init?: any) => {
      const target = String(url);
      if (target.includes('/consent') && init?.method === 'PATCH') {
        const body = JSON.parse(init.body);
        consentCalls.push({ url: target, body });
        return new Response(
          JSON.stringify({
            ...runners[0],
            flow_execution_consent: body.flow_execution_consent,
          })
        );
      }
      if (target.includes('/api/v1/runners')) {
        return new Response(JSON.stringify(runners));
      }
      if (target.includes('/auth/users/me')) {
        return new Response(JSON.stringify({ id: OWNER_ID }));
      }
      if (target.includes('/api/v1/account/details')) {
        return new Response(
          JSON.stringify({ id: 'acct-1', default_runner_pool: null })
        );
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: agents }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    invalidateUserProfileCache();
    window.history.replaceState({}, '', window.location.pathname);
  });

  const mount = async (flow: Record<string, unknown>) => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  const q = (element: PreloopFlowForm, selector: string) =>
    element.shadowRoot?.querySelector(selector) as HTMLElement | null;

  const chooseRuntime = async (element: PreloopFlowForm, value: string) => {
    const select = q(element, 'sl-select[label="Agent runtime"]') as any;
    select.value = value;
    select.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;
  };

  const newFlow = () => ({
    name: 'Review',
    prompt_template: 'review',
    agent_type: 'codex',
    agent_config: {},
  });

  it('offers Cursor on my runner when a runner advertises it', async () => {
    const element = await mount(newFlow());
    expect(q(element, '[data-cursor-option]')?.textContent?.trim()).to.equal(
      'Cursor on my runner'
    );
  });

  it('says why when no runner reports Cursor', async () => {
    runners = [];
    const element = await mount(newFlow());
    expect(q(element, '[data-cursor-option]')?.textContent).to.include(
      'no runner reports Cursor yet'
    );
    await chooseRuntime(element, 'cursor');
    const panel = q(element, '[data-cursor-runner="none"]');
    expect(panel?.textContent).to.include('cursor-agent login');
    expect(panel?.querySelector('a[href="/console/runners"]')).to.exist;
  });

  it('names a runner whose Cursor CLI is signed out', async () => {
    runners = [signedOutRunner()];
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    const blocker = q(element, '[data-cursor-blocker="signed_out"]');
    expect(blocker?.textContent).to.include('build-box');
    expect(blocker?.textContent).to.include('signed out');
  });

  it('clears another runtime profile when Cursor has no eligible runner', async () => {
    runners = [];
    const element = await mount({
      ...newFlow(),
      agent_type: 'devin',
      agent_config: { host_exec_profile: 'devin' },
    });
    await chooseRuntime(element, 'cursor');
    expect(element.flow.agent_config).not.to.have.property('host_exec_profile');
  });

  it('explains an unknown Cursor login instead of offering the runner', async () => {
    runners[0].harness_inventory.entries[0].login_state = 'unknown';
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    expect(
      q(element, '[data-cursor-blocker="login_unknown"]')?.textContent
    ).to.include('could not be verified');
    expect(element.flow.agent_config).not.to.have.property('host_exec_profile');
  });

  it('choosing Cursor selects the runner and its profile', async () => {
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    expect(element.flow.runner_pool).to.equal(RUNNER_ID);
    expect((element.flow.agent_config as any).host_exec_profile).to.equal(
      'cursor'
    );
    expect(q(element, '[data-cursor-runner-selected]')?.textContent).to.include(
      'jane-mac'
    );
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.agent_type).to.equal('cursor');
    expect(event.detail.flow.runner_pool).to.equal(RUNNER_ID);
    expect(event.detail.flow.agent_config.host_exec_profile).to.equal('cursor');
  });

  it('leaving Cursor drops the profile it set', async () => {
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    await chooseRuntime(element, 'codex');
    expect(element.flow.agent_config as any).not.to.have.property(
      'host_exec_profile'
    );
  });

  for (const unavailable of [false, true]) {
    it(`clears a custom Cursor profile on runtime change (runner unavailable: ${unavailable})`, async () => {
      if (unavailable) runners = [];
      const element = await mount({
        ...newFlow(),
        agent_type: 'cursor',
        agent_config: {
          host_exec_profile: 'cursor-review',
          cursor_model: 'auto',
        },
      });
      await chooseRuntime(element, 'codex');
      expect(element.flow.agent_config).not.to.have.property(
        'host_exec_profile'
      );
    });
  }

  it('lets the user pick among several Cursor runners, online first', async () => {
    runners = [
      cursorRunner({
        id: '33333333-3333-4333-8333-333333333333',
        name: 'old-laptop',
        status: 'offline',
        presence: 'offline',
      }),
      cursorRunner(),
    ];
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    expect(element.flow.runner_pool).to.equal(RUNNER_ID);
    const select = q(element, '[data-cursor-runner-select]') as any;
    expect(select).to.exist;
    select.value = '33333333-3333-4333-8333-333333333333';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;
    expect(element.flow.runner_pool).to.equal(
      '33333333-3333-4333-8333-333333333333'
    );
    expect(q(element, '[data-cursor-runner-offline]')?.textContent).to.include(
      'offline'
    );
  });

  it('moves an existing harness runner pin with the Cursor runner picker', async () => {
    const otherId = '33333333-3333-4333-8333-333333333333';
    runners.push(cursorRunner({ id: otherId, name: 'second-mac' }));
    const element = await mount({
      ...newFlow(),
      agent_type: 'cursor',
      runner_pool: RUNNER_ID,
      agent_config: {
        harness: 'cursor_cli',
        runner_id: RUNNER_ID,
        host_exec_profile: 'cursor',
      },
    });
    const select = q(element, '[data-cursor-runner-select]') as any;
    select.value = otherId;
    select.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.runner_pool).to.equal(otherId);
    expect(event.detail.flow.agent_config.runner_id).to.equal(otherId);
  });

  it('explains ask_me consent and lets the owner allow runs', async () => {
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    const consent = q(element, '[data-cursor-consent="ask_me"]');
    expect(consent?.textContent).to.include('you get an approval request');
    const control = consent?.querySelector('owner-consent-control') as any;
    expect(control.editable).to.equal(true);
    control.dispatchEvent(
      new CustomEvent('consent-change', {
        detail: { value: 'always' },
        bubbles: true,
        composed: true,
      })
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    expect(consentCalls).to.have.length(1);
    expect(consentCalls[0].url).to.include(`/runners/${RUNNER_ID}/consent`);
    expect(consentCalls[0].body).to.deep.equal({
      flow_execution_consent: 'always',
    });
    expect(q(element, '[data-cursor-consent="always"]')).to.exist;
  });

  it('warns when the runner never runs flows', async () => {
    runners = [cursorRunner({ flow_execution_consent: 'never' })];
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    const consent = q(element, '[data-cursor-consent="never"]');
    expect(consent?.querySelector('sl-alert[variant="warning"]')).to.exist;
    expect(consent?.textContent).to.include('will not start');
  });

  it('shows consent read-only on a runner someone else owns', async () => {
    runners = [cursorRunner({ registered_by_user_id: 'someone-else' })];
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    const consent = q(element, '[data-cursor-consent="ask_me"]');
    expect(consent?.textContent).to.include('the runner owner gets');
    const control = consent?.querySelector('owner-consent-control') as any;
    expect(control.editable).to.equal(false);
    expect(control.ownerOnly).to.include('Only the runner owner');
  });

  it('states that Cursor runs are not gateway metered', async () => {
    const element = await mount(newFlow());
    await chooseRuntime(element, 'cursor');
    expect(q(element, '[data-cursor-metering]')?.textContent).to.include(
      'not gateway metered'
    );
    expect(element.shadowRoot?.innerHTML).to.include('data-cursor-limits-note');
  });

  it('explains Cursor in persistent mode and switches to the runner route', async () => {
    agents = [
      {
        id: 'agent-1',
        name: 'Ops agent',
        agent_kind: 'claude_code',
        agent_control_enabled: true,
      },
    ];
    const element = await mount(newFlow());
    (element as any).applyExecutionPath('persistent');
    await element.updateComplete;
    const note = q(element, '[data-persistent-cursor-note]');
    expect(note?.textContent).to.include('Cursor is not listed here');
    expect(q(element, '[data-cursor-consent]')).to.equal(null);
    (q(element, '[data-use-cursor-runner]') as HTMLElement).click();
    await element.updateComplete;
    expect((element as any).flowExecutionPath).to.equal('ephemeral');
    expect(element.flow.agent_type).to.equal('cursor');
    expect(element.flow.runner_pool).to.equal(RUNNER_ID);
  });

  it('persistent note points to Runners when no runner has Cursor', async () => {
    runners = [];
    agents = [{ id: 'agent-1', name: 'Ops agent', agent_kind: 'claude_code' }];
    const element = await mount(newFlow());
    (element as any).applyExecutionPath('persistent');
    await element.updateComplete;
    const note = q(element, '[data-persistent-cursor-note]');
    expect(note?.querySelector('a[href="/console/runners"]')).to.exist;
    expect(q(element, '[data-use-cursor-runner]')).to.equal(null);
  });

  it('opens on Cursor and the runner from a Runners page link', async () => {
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?agent_type=cursor&runner_pool=jane-mac`
    );
    const element = await mount(newFlow());
    expect(element.flow.agent_type).to.equal('cursor');
    expect(element.flow.runner_pool).to.equal(RUNNER_ID);
    expect((element as any).flowExecutionPath).to.equal('ephemeral');
  });

  it('a Cursor agent link does not open persistent mode', async () => {
    agents = [{ id: 'agent-cursor', name: 'Cursor', agent_kind: 'cursor' }];
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}?agent_id=agent-cursor`
    );
    const element = await mount(newFlow());
    expect((element as any).flowExecutionPath).to.equal('ephemeral');
    expect(element.flow.agent_type).to.equal('cursor');
  });
});
