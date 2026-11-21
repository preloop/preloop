import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

const MODEL_A = '11111111-1111-1111-1111-111111111111';
const MODEL_B = '22222222-2222-2222-2222-222222222222';

/**
 * The flow-wide reasoning effort select (agent_config.reasoning_effort),
 * shown with the flow's model choice rather than in the label rules.
 */
describe('PreloopFlowForm flow reasoning effort', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const target = String(url);
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

  const select = (element: PreloopFlowForm): any =>
    element.shadowRoot!.querySelector('[data-flow-reasoning-effort]');

  const choose = async (element: PreloopFlowForm, value: string) => {
    const el = select(element);
    el.value = value;
    el.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
  };

  const submit = async (
    element: PreloopFlowForm
  ): Promise<Record<string, any>> => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow.agent_config;
  };

  it('sits with the model choice, outside advanced model routing', async () => {
    const element = await mount();
    const el = select(element);
    expect(el).to.exist;
    expect(el.getAttribute('label')).to.equal('Reasoning effort');
    expect(el.closest('[data-advanced="routing"]')).to.equal(null);
    const options = Array.from(el.querySelectorAll('sl-option')).map(
      (o: any) => [o.value, o.textContent.trim()]
    );
    expect(options).to.deep.equal([
      ['', 'Model default'],
      ['low', 'Low'],
      ['medium', 'Medium'],
      ['high', 'High'],
    ]);
    const help = el.querySelector('[slot="help-text"]');
    expect(help.textContent).to.include('no label rule sets an effort');
    expect(help.querySelector('a').getAttribute('href')).to.include(
      'model-routing#reasoning-effort-for-the-whole-flow'
    );
  });

  it('loads a stored effort', async () => {
    const element = await mount({ reasoning_effort: 'medium' });
    expect(select(element).value).to.equal('medium');
  });

  it('shows the model default when nothing is stored', async () => {
    const element = await mount();
    expect(select(element).value).to.equal('');
  });

  it('saves the chosen effort in agent_config', async () => {
    const element = await mount({ max_iterations: 6 });
    await choose(element, 'low');
    const config = await submit(element);
    expect(config.reasoning_effort).to.equal('low');
    expect(config.max_iterations).to.equal(6);
  });

  it('removes the key when set back to the model default', async () => {
    const element = await mount({ reasoning_effort: 'high' });
    await choose(element, '');
    const config = await submit(element);
    expect(config).to.not.have.property('reasoning_effort');
  });

  it('keeps a stored effort through a save that did not touch it', async () => {
    const element = await mount({ reasoning_effort: 'low' });
    const config = await submit(element);
    expect(config.reasoning_effort).to.equal('low');
  });

  it('marks the form as changed', async () => {
    const element = await mount();
    element.markSaved();
    expect(element.isDirty).to.equal(false);
    await choose(element, 'high');
    expect(element.isDirty).to.equal(true);
  });

  it("saves a label rule's own effort unchanged next to the flow effort", async () => {
    const rules = [
      {
        label: 'complexity:high',
        ai_model_id: MODEL_A,
        reasoning_effort: 'high',
      },
      { label: 'docs', reasoning_effort: 'medium' },
    ];
    const element = await mount({ model_by_label: rules });
    await choose(element, 'low');
    const config = await submit(element);
    expect(config.reasoning_effort).to.equal('low');
    expect(config.model_by_label).to.deep.equal(rules);
  });
});
