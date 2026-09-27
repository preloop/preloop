import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
} from '@open-wc/testing';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

let source: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
});

describe('PreloopFlowForm host execution profile', () => {
  it('offers Cursor as a private-runner host profile, not Agent Control', () => {
    expect(source).to.include('value="cursor"');
    expect(source).to.include('private runner host profile');
    expect(source).to.include('renderHostExecProfileField()');
    expect(source).to.include('composedAgentConfig()');
  });

  it('offers Copilot CLI as a private-runner host profile', () => {
    expect(source).to.include('value="copilot"');
    expect(source).to.include('Copilot CLI (private runner host profile)');
  });
});

describe('PreloopFlowForm host execution submit', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/runners')) {
        return new Response(
          JSON.stringify([
            {
              id: '11111111-1111-4111-8111-111111111111',
              name: 'office-mac',
              labels: ['local'],
              status: 'online',
              capabilities: {
                host_exec_profiles: [
                  {
                    name: 'cursor-ask',
                    capabilities: ['host_exec', 'cursor_cli'],
                  },
                  {
                    name: 'copilot-review',
                    capabilities: ['host_exec', 'copilot_cli'],
                  },
                ],
              },
            },
          ])
        );
      }
      if (target.includes('/api/v1/account/details')) {
        return new Response(
          JSON.stringify({
            id: 'acct-1',
            organization_name: 'Example Org',
            default_runner_pool: null,
            hosted_minutes_remaining: null,
            created_at: '2026-09-04T00:00:00Z',
            updated_at: '2026-09-04T00:00:00Z',
          })
        );
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
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

  it('merges host_exec_profile into agent_config for Cursor', async () => {
    const element = await mount({
      name: 'Ask locally',
      prompt_template: 'summarize',
      agent_type: 'cursor',
      runner_pool: 'office-mac',
      agent_config: {
        image: 'registry.example.com/team/project:release',
        environment_profile: 'team-tests',
      },
    });
    const input = element.shadowRoot?.querySelector(
      'sl-input[label="Host execution profile"]'
    ) as SlInput;
    expect(input).to.exist;
    input.value = 'cursor-ask';
    input.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;

    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.agent_type).to.equal('cursor');
    expect(event.detail.flow.agent_config).to.deep.include({
      image: 'registry.example.com/team/project:release',
      environment_profile: 'team-tests',
      host_exec_profile: 'cursor-ask',
    });
  });

  it('preserves the profile default without selecting API credentials', async () => {
    const element = await mount({
      name: 'Local default',
      prompt_template: 'summarize',
      agent_type: 'cursor',
      runner_pool: 'office-mac',
      agent_config: { host_exec_profile: 'cursor-ask' },
    });
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.ai_model_id ?? '').to.equal('');
    expect(element.shadowRoot?.textContent).to.include(
      'These flow tool settings do not apply.'
    );
    expect(
      element.shadowRoot?.querySelector('sl-select[label="AI model"]')
    ).to.equal(null);
    expect(element.shadowRoot?.textContent).to.include('not Grok 4.7');
    const model = element.shadowRoot?.querySelector(
      '[data-cursor-model]'
    ) as HTMLInputElement;
    expect(model).to.exist;
    model.value = 'grok-4.7-high';
    model.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
    const pinned = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const pinnedEvent = await pinned;
    expect(pinnedEvent.detail.flow.agent_config.cursor_model).to.equal(
      'grok-4.7-high'
    );
  });

  it('omits host_exec_profile when saving a Docker harness', async () => {
    const element = await mount({
      name: 'Review',
      prompt_template: 'review',
      agent_type: 'codex',
      agent_config: {
        image: 'registry.example.com/team/project:release',
        host_exec_profile: 'stale-profile',
      },
    });
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.agent_config.host_exec_profile).to.equal(
      undefined
    );
    expect(event.detail.flow.agent_config.image).to.equal(
      'registry.example.com/team/project:release'
    );
  });

  it('saves a Copilot host profile with copilot_model only', async () => {
    const element = await mount({
      name: 'Review locally',
      prompt_template: 'review',
      agent_type: 'copilot',
      runner_pool: 'office-mac',
      agent_config: { cursor_model: 'composer-2.5' },
    });
    const input = element.shadowRoot?.querySelector(
      'sl-input[label="Host execution profile"]'
    ) as SlInput;
    expect(input).to.exist;
    // Only profiles that advertise the Copilot harness are suggested.
    expect(input.placeholder).to.equal('copilot-review');
    input.value = 'copilot-review';
    input.dispatchEvent(new CustomEvent('sl-input'));
    const model = element.shadowRoot?.querySelector(
      '[data-copilot-model]'
    ) as HTMLInputElement;
    expect(model).to.exist;
    model.value = ' team-default ';
    model.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('sl-select[label="AI model"]')
    ).to.equal(null);
    const text = element.shadowRoot?.textContent || '';
    expect(text).to.include('not metered by the Preloop gateway');
    expect(text).to.include(
      "Copilot profiles use the runner user's local Copilot MCP configuration."
    );

    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.agent_type).to.equal('copilot');
    expect(event.detail.flow.ai_model_id ?? '').to.equal('');
    expect(event.detail.flow.agent_config.host_exec_profile).to.equal(
      'copilot-review'
    );
    expect(event.detail.flow.agent_config.copilot_model).to.equal(
      'team-default'
    );
    expect(event.detail.flow.agent_config.cursor_model).to.equal(undefined);
  });

  it('drops copilot_model when the flow switches to a Docker harness', async () => {
    const element = await mount({
      name: 'Review',
      prompt_template: 'review',
      agent_type: 'codex',
      agent_config: {
        host_exec_profile: 'copilot-review',
        copilot_model: 'team-default',
      },
    });
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    expect(event.detail.flow.agent_config.copilot_model).to.equal(undefined);
    expect(event.detail.flow.agent_config.host_exec_profile).to.equal(
      undefined
    );
  });
});
