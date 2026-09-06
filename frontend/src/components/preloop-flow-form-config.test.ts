import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

const preset = {
  id: 'timeout-preset',
  name: 'Implementation',
  prompt_template: 'Implement the issue',
  agent_type: 'codex',
  trigger_event_types: ['webhook'],
  timeout_seconds: 5400,
};

describe('PreloopFlowForm saved agent configuration', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox
      .stub(window, 'fetch')
      .callsFake(
        async (url) =>
          new Response(
            JSON.stringify(
              String(url).includes('/api/v1/flows/presets') ? [preset] : []
            )
          )
      );
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (timeout?: number | null): Promise<PreloopFlowForm> => {
    const flow = {
      name: 'Issue fixer',
      prompt_template: 'Fix it',
      agent_type: 'codex',
      timeout_seconds: timeout,
    };
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  for (const path of ['ephemeral', 'persistent']) {
    it(`preserves API-configured image and profile while choosing ${path} execution`, async () => {
      const element = await mount();
      const saved = {
        image: 'registry.example.com/team/project:release',
        docker_image: 'registry.example.com/team/legacy:release',
        environment_profile: 'team-tests',
        custom_option: { keep: true },
        execution_path: 'persistent',
        target_agent_id: 'old-agent',
      };
      element.flow = { ...element.flow, agent_config: saved };
      (element as any).longRunningAgents = [
        { id: 'new-agent', name: 'Team agent' },
      ];
      (element as any).flowExecutionPath = path;
      (element as any).targetAgentId = 'new-agent';
      const listener = sandbox.spy();
      element.addEventListener('flow-submit', listener);
      await (element as any).handleFormSubmit(new Event('submit'));
      expect(listener.callCount).to.equal(1);
      expect(listener.firstCall.args[0].detail.flow.agent_config).to.deep.equal(
        {
          ...saved,
          execution_path: path,
          target_agent_id: path === 'persistent' ? 'new-agent' : undefined,
        }
      );
      expect(saved.target_agent_id).to.equal('old-agent');
    });
  }
});
