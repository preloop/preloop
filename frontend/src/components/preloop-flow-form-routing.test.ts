import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

const MODEL_A = '11111111-1111-1111-1111-111111111111';
const MODEL_B = '22222222-2222-2222-2222-222222222222';

const preset = {
  id: 'routing-preset',
  name: 'Implementation',
  prompt_template: 'Implement the issue',
  agent_type: 'codex',
  trigger_event_types: ['webhook'],
};

let source: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
});

describe('PreloopFlowForm model routing editor', () => {
  it('renders a model routing rules section', () => {
    expect(source).to.include('Model routing rules');
    expect(source).to.include('data-routing-editor');
    expect(source).to.include('First matching rule');
    expect(source).to.include('do not swap');
    expect(source).to.include('data-add-routing-rule');
  });
});

describe('PreloopFlowForm model routing roundtrip', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const target = String(url);
      if (target.includes('/api/v1/flows/presets')) {
        return new Response(JSON.stringify([preset]));
      }
      if (
        target.includes('/api/v1/ai-models') ||
        target.includes('/ai_models')
      ) {
        return new Response(
          JSON.stringify([
            { id: MODEL_A, name: 'Fast model' },
            { id: MODEL_B, name: 'Default model' },
          ])
        );
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (
    agentConfig?: Record<string, unknown>
  ): Promise<PreloopFlowForm> => {
    const flow = {
      name: 'Issue fixer',
      prompt_template: 'Fix it',
      agent_type: 'codex',
      ai_model_id: MODEL_B,
      ...(agentConfig ? { agent_config: agentConfig } : {}),
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

  const submit = async (
    element: PreloopFlowForm
  ): Promise<Record<string, unknown>> => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow;
  };

  it('loads saved rules and preserves unrelated agent_config keys', async () => {
    const saved = {
      sandbox_type: 'exec',
      max_iterations: 6,
      model_routing: {
        version: 1,
        rules: [
          {
            id: 'docs-fast',
            labels: { any: ['documentation'] },
            ai_model_id: MODEL_A,
            agent_type: 'codex',
          },
        ],
      },
    };
    const element = await mount(saved);
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(config.sandbox_type).to.equal('exec');
    expect(config.max_iterations).to.equal(6);
    expect(config.model_routing).to.deep.equal(saved.model_routing);
  });

  it('omits model_routing when every rule is removed', async () => {
    const element = await mount({
      sandbox_type: 'exec',
      model_routing: {
        version: 1,
        rules: [
          {
            id: 'docs-fast',
            labels: { any: ['documentation'] },
            ai_model_id: MODEL_A,
            agent_type: 'codex',
          },
        ],
      },
    });
    (element as any).routingRules = [];
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(config.sandbox_type).to.equal('exec');
    expect(config).to.not.have.property('model_routing');
  });

  it('adds a rule into agent_config on save', async () => {
    const element = await mount({ sandbox_type: 'exec' });
    (element as any).addRoutingRule();
    (element as any).updateRoutingRule(0, 'id', 'docs-fast');
    (element as any).updateRoutingRule(0, 'anyLabels', 'documentation');
    (element as any).updateRoutingRule(0, 'ai_model_id', MODEL_A);
    (element as any).updateRoutingRule(0, 'agent_type', 'codex');
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(config.sandbox_type).to.equal('exec');
    expect(config.model_routing).to.deep.equal({
      version: 1,
      rules: [
        {
          id: 'docs-fast',
          labels: { any: ['documentation'] },
          ai_model_id: MODEL_A,
          agent_type: 'codex',
        },
      ],
    });
  });

  for (const missing of ['anyLabels', 'ai_model_id', 'agent_type']) {
    it(`rejects an incomplete rule with missing ${missing} without dropping it`, async () => {
      const element = await mount({ sandbox_type: 'exec' });
      (element as any).addRoutingRule();
      (element as any).updateRoutingRule(0, 'anyLabels', 'documentation');
      (element as any).updateRoutingRule(0, missing, '');
      const listener = sandbox.spy();
      element.addEventListener('flow-submit', listener);
      await (element as any).handleFormSubmit(new Event('submit'));
      expect(listener.callCount).to.equal(0);
      expect((element as any).formError).to.include('Routing rule 1');
      expect((element as any).routingRules).to.have.length(1);
    });
  }
});

describe('PreloopFlowForm model by label editor', () => {
  it('renders a model by label section', () => {
    expect(source).to.include('Model by label');
    expect(source).to.include('data-label-routing-editor');
    expect(source).to.include('data-add-label-rule');
    expect(source).to.include('Reasoning effort');
  });
});

describe('PreloopFlowForm model by label roundtrip', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const target = String(url);
      if (target.includes('/api/v1/flows/presets')) {
        return new Response(JSON.stringify([preset]));
      }
      if (
        target.includes('/api/v1/ai-models') ||
        target.includes('/ai_models')
      ) {
        return new Response(
          JSON.stringify([
            { id: MODEL_A, name: 'Fast model' },
            { id: MODEL_B, name: 'Default model' },
          ])
        );
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (
    agentConfig?: Record<string, unknown>
  ): Promise<PreloopFlowForm> => {
    const flow = {
      name: 'Issue fixer',
      prompt_template: 'Fix it',
      agent_type: 'codex',
      ai_model_id: MODEL_B,
      ...(agentConfig ? { agent_config: agentConfig } : {}),
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

  const submit = async (
    element: PreloopFlowForm
  ): Promise<Record<string, unknown>> => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow;
  };

  it('loads saved label rules and preserves unrelated keys', async () => {
    const saved = {
      sandbox_type: 'exec',
      model_by_label: [
        {
          label: 'complexity:high',
          ai_model_id: MODEL_A,
          reasoning_effort: 'high',
        },
      ],
    };
    const element = await mount(saved);
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(config.sandbox_type).to.equal('exec');
    expect(config.model_by_label).to.deep.equal(saved.model_by_label);
  });

  it('saves a rule that only raises the effort', async () => {
    const element = await mount({ sandbox_type: 'exec' });
    (element as any).addLabelRule();
    (element as any).updateLabelRule(0, 'label', 'complexity:high');
    (element as any).updateLabelRule(0, 'reasoning_effort', 'high');
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(config.model_by_label).to.deep.equal([
      { label: 'complexity:high', reasoning_effort: 'high' },
    ]);
  });

  it('omits model_by_label when every rule is removed', async () => {
    const element = await mount({
      sandbox_type: 'exec',
      model_by_label: [{ label: 'complexity:high', ai_model_id: MODEL_A }],
    });
    (element as any).labelRules = [];
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(config).to.not.have.property('model_by_label');
    expect(config.sandbox_type).to.equal('exec');
  });

  it('keeps the order the rules were arranged in', async () => {
    const element = await mount({ sandbox_type: 'exec' });
    (element as any).addLabelRule();
    (element as any).updateLabelRule(0, 'label', 'complexity:low');
    (element as any).updateLabelRule(0, 'reasoning_effort', 'low');
    (element as any).addLabelRule();
    (element as any).updateLabelRule(1, 'label', 'complexity:high');
    (element as any).updateLabelRule(1, 'reasoning_effort', 'high');
    (element as any).moveLabelRule(1, -1);
    const payload = await submit(element);
    const config = payload.agent_config as Record<string, unknown>;
    expect(
      (config.model_by_label as Array<Record<string, string>>).map(
        (rule) => rule.label
      )
    ).to.deep.equal(['complexity:high', 'complexity:low']);
  });

  it('refuses a rule with no label without dropping it', async () => {
    const element = await mount({ sandbox_type: 'exec' });
    (element as any).addLabelRule();
    (element as any).updateLabelRule(0, 'reasoning_effort', 'high');
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(0);
    expect((element as any).formError).to.include('Label rule 1');
    expect((element as any).labelRules).to.have.length(1);
  });

  it('refuses a rule that changes nothing', async () => {
    const element = await mount({ sandbox_type: 'exec' });
    (element as any).addLabelRule();
    (element as any).updateLabelRule(0, 'label', 'complexity:high');
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(0);
    expect((element as any).formError).to.include('changes nothing');
  });

  it('refuses the same label twice, because the second never applies', async () => {
    const element = await mount({ sandbox_type: 'exec' });
    (element as any).addLabelRule();
    (element as any).updateLabelRule(0, 'label', 'complexity:high');
    (element as any).updateLabelRule(0, 'reasoning_effort', 'high');
    (element as any).addLabelRule();
    (element as any).updateLabelRule(1, 'label', 'complexity:high');
    (element as any).updateLabelRule(1, 'reasoning_effort', 'low');
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(0);
    expect((element as any).formError).to.include('appears twice');
  });
});
