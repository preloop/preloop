import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

function controlAgent(
  id: string,
  name: string,
  online: boolean
): Record<string, unknown> {
  return {
    id,
    display_name: name,
    agent_kind: 'openclaw',
    control_enabled: true,
    control_capabilities: ['send_text_prompt'],
    control_online: online,
    control_state: online ? 'plugin_connected' : 'plugin_configured',
  };
}

describe('PreloopFlowForm persistent target picker', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify([])));
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  async function mount(): Promise<PreloopFlowForm> {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form
        .flow=${{
          name: 'Persistent review',
          prompt_template: 'Review the pull request',
          agent_type: 'codex',
        }}
      ></preloop-flow-form>`
    );
    while (
      (element as unknown as { _loadingReferenceData: boolean })
        ._loadingReferenceData
    ) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    (element as unknown as { longRunningAgents: unknown[] }).longRunningAgents =
      [
        controlAgent('agent-online', 'Online node', true),
        controlAgent('agent-offline', 'Offline node', false),
      ];
    (
      element as unknown as { flowExecutionPath: 'ephemeral' | 'persistent' }
    ).flowExecutionPath = 'persistent';
    (element as unknown as { targetAgentId: string }).targetAgentId =
      'agent-online';
    element.requestUpdate();
    await element.updateComplete;
    return element;
  }

  it('shows the persistent radio and control-state labels', async () => {
    const element = await mount();
    const radios = [
      ...(element.shadowRoot?.querySelectorAll('sl-radio') || []),
    ].map((radio) => radio.getAttribute('value'));
    expect(radios).to.include('ephemeral');
    expect(radios).to.include('persistent');

    const options = [
      ...(element.shadowRoot?.querySelectorAll('sl-option') || []),
    ];
    const labels = options.map((option) => option.textContent || '');
    expect(
      labels.some((text) => text.includes('Agent Control online'))
    ).to.equal(true);
    expect(
      labels.some((text) => text.includes('Agent Control configured'))
    ).to.equal(true);
  });

  it('disables offline targets', async () => {
    const element = await mount();
    const options = [
      ...(element.shadowRoot?.querySelectorAll('sl-option') || []),
    ];
    const offline = options.find((option) =>
      (option.textContent || '').includes('Offline node')
    );
    const online = options.find((option) =>
      (option.textContent || '').includes('Online node')
    );
    expect(offline).to.exist;
    expect(online).to.exist;
    expect((offline as { disabled: boolean }).disabled).to.equal(true);
    expect((online as { disabled: boolean }).disabled).to.equal(false);
  });

  it('warns when the chosen target is offline', async () => {
    const element = await mount();
    (element as unknown as { targetAgentId: string }).targetAgentId =
      'agent-offline';
    element.requestUpdate();
    await element.updateComplete;

    const warning = element.shadowRoot?.querySelector(
      '[data-testid="persistent-target-offline"]'
    );
    expect(warning).to.exist;
    expect((warning?.textContent || '').replace(/\s+/g, ' ')).to.contain(
      'This agent is not connected to Agent Control; the flow will fail at start until it reconnects.'
    );
  });

  it('shows the runtime select in ephemeral mode without the offline warning', async () => {
    const element = await mount();
    (
      element as unknown as { flowExecutionPath: 'ephemeral' | 'persistent' }
    ).flowExecutionPath = 'ephemeral';
    element.requestUpdate();
    await element.updateComplete;

    const runtime = [
      ...(element.shadowRoot?.querySelectorAll('sl-select') || []),
    ].find((select) => select.getAttribute('label') === 'Agent runtime');
    expect(runtime).to.exist;
    expect(
      element.shadowRoot?.querySelector(
        '[data-testid="persistent-target-offline"]'
      )
    ).to.not.exist;
  });

  it('disables presets that do not support persistent execution', async () => {
    const element = await mount();
    (element as unknown as { presets: unknown[] }).presets = [
      {
        id: 'preset-review',
        name: 'Pull Request Reviewer',
        description: 'Reviews a pull request.',
        supports_persistent: true,
      },
      {
        id: 'preset-impl',
        name: 'Automated Issue Implementation',
        description: 'Implements an issue in a clone.',
        supports_persistent: false,
      },
    ];
    element.requestUpdate();
    await element.updateComplete;
    const picker = element.shadowRoot?.querySelector(
      'preloop-flow-preset-picker'
    ) as HTMLElement & { updateComplete: Promise<unknown> };
    await picker.updateComplete;
    const rows = [
      ...(picker.shadowRoot?.querySelectorAll('[role="option"]') || []),
    ];
    const impl = rows.find((row) =>
      (row.textContent || '').includes('Automated Issue Implementation')
    );
    const review = rows.find((row) =>
      (row.textContent || '').includes('Pull Request Reviewer')
    );
    expect(impl?.getAttribute('aria-disabled')).to.equal('true');
    expect(impl?.textContent || '').to.contain(
      'does not support persistent execution'
    );
    expect(review?.getAttribute('aria-disabled')).to.equal('false');
  });

  it('clears an unsupported preset when switching to persistent', async () => {
    const element = await mount();
    const form = element as unknown as {
      presets: unknown[];
      pickerSelectedId: string;
      sourcePresetId: string | null;
      flowExecutionPath: string;
      applyExecutionPath: (path: 'ephemeral' | 'persistent') => void;
      persistentPresetNotice: string;
      requestUpdate: () => void;
      updateComplete: Promise<unknown>;
    };
    form.presets = [
      {
        id: 'preset-impl',
        name: 'Automated Issue Implementation',
        supports_persistent: false,
      },
    ];
    form.pickerSelectedId = 'preset-impl';
    form.sourcePresetId = 'preset-impl';
    form.applyExecutionPath('persistent');
    form.requestUpdate();
    await form.updateComplete;
    expect(form.pickerSelectedId).to.equal('');
    expect(form.sourcePresetId).to.equal(null);
    expect(form.persistentPresetNotice).to.contain(
      'does not support persistent execution'
    );
  });
});
