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
      element.shadowRoot
        ?.querySelector('sl-select[label="Requested AI Model"]')
        ?.getAttribute('help-text')
    ).to.include('local profile');
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
});
