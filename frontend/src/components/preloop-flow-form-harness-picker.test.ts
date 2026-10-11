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

const LAPTOP = '11111111-1111-4111-8111-111111111111';
const DESKTOP = '22222222-2222-4222-8222-222222222222';

describe('PreloopFlowForm harness + model picker', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/flows/harness-options')) {
        return new Response(
          JSON.stringify({
            harnesses: [
              {
                harness: 'copilot_cli',
                display_name: 'GitHub Copilot CLI',
                agent_type: 'copilot',
                billing: 'seat',
                runners_online: 1,
                runners_total: 2,
                models: [
                  { id: 'auto', source: 'static', runners_online: 1 },
                  {
                    id: 'claude-sonnet-4.6',
                    source: 'configured',
                    runners_online: 1,
                  },
                ],
                runners: [
                  {
                    id: LAPTOP,
                    name: 'jonas-laptop',
                    online: true,
                    eligible: true,
                    reason: null,
                  },
                  {
                    id: DESKTOP,
                    name: 'jonas-desktop',
                    online: false,
                    eligible: false,
                    reason: 'runner_offline',
                  },
                ],
              },
            ],
          })
        );
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      if (target.includes('/api/v1/account/details')) {
        return new Response(JSON.stringify({ id: 'acct-1' }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
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

  it('shows online runner counts, seat billing and both runners', async () => {
    const element = await mount({
      name: 'Review on seats',
      prompt_template: 'review',
      agent_type: 'copilot',
      agent_config: { harness: 'copilot_cli' },
    });
    const root = element.shadowRoot!;
    const summary = root.querySelector('[data-harness-summary]');
    expect(summary?.textContent).to.include('1 of 2 runners');
    expect(summary?.textContent).to.include('GitHub Copilot CLI');
    expect(
      root.querySelector('[data-harness-billing]')?.textContent
    ).to.include('Seat (not metered by gateway)');
    const models = [
      ...root.querySelectorAll('[data-harness-model] sl-option'),
    ].map((node) => node.textContent?.replace(/\s+/g, ' ').trim());
    expect(models).to.deep.equal([
      'auto (1 online)',
      'claude-sonnet-4.6 (1 online)',
    ]);
    const runners = [
      ...root.querySelectorAll('[data-harness-runner] sl-option'),
    ].map((node) => node.textContent?.replace(/\s+/g, ' ').trim());
    expect(runners).to.deep.equal([
      'jonas-laptop (ready)',
      'jonas-desktop (runner_offline)',
    ]);
  });

  it('saves harness, model, runner pin and fallback in agent_config', async () => {
    const element = await mount({
      name: 'Review on seats',
      prompt_template: 'review',
      agent_type: 'copilot',
      agent_config: {
        harness: 'copilot_cli',
        copilot_model: 'claude-sonnet-4.6',
        runner_id: LAPTOP,
        harness_fallback: 'fallback_server',
        fallback_model_identifier: 'gpt-5.2',
        cursor_model: 'stale',
      },
    });
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    const config = event.detail.flow.agent_config;
    expect(config).to.deep.include({
      harness: 'copilot_cli',
      copilot_model: 'claude-sonnet-4.6',
      runner_id: LAPTOP,
      harness_fallback: 'fallback_server',
      fallback_model_identifier: 'gpt-5.2',
    });
    expect(config.cursor_model).to.equal(undefined);
    expect(config.host_exec_profile).to.equal(undefined);
  });

  it('turning routing off drops every harness routing key', async () => {
    const element = await mount({
      name: 'Review on seats',
      prompt_template: 'review',
      agent_type: 'copilot',
      agent_config: {
        harness: 'copilot_cli',
        runner_id: LAPTOP,
        harness_queue_timeout_seconds: 600,
        host_exec_profile: 'copilot-review',
      },
    });
    const toggle = element.shadowRoot!.querySelector(
      '[data-harness-route]'
    ) as HTMLInputElement;
    expect(toggle).to.exist;
    toggle.checked = false;
    toggle.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector('[data-harness-summary]')
    ).to.equal(null);
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    const config = event.detail.flow.agent_config;
    expect(config.harness).to.equal(undefined);
    expect(config.runner_id).to.equal(undefined);
    expect(config.harness_queue_timeout_seconds).to.equal(undefined);
    expect(config.host_exec_profile).to.equal('copilot-review');
  });

  it('drops harness keys when the flow moves to a container harness', async () => {
    const element = await mount({
      name: 'Review',
      prompt_template: 'review',
      agent_type: 'codex',
      agent_config: { harness: 'copilot_cli', runner_id: LAPTOP },
    });
    expect(element.shadowRoot!.querySelector('[data-harness-picker]')).to.equal(
      null
    );
    const config = (element as any).composedAgentConfig();
    expect(config.harness).to.equal(undefined);
    expect(config.runner_id).to.equal(undefined);
  });
});
