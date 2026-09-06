import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

describe('PreloopFlowForm PR feedback controls', () => {
  let sandbox: SinonSandbox;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async () => new Response('[]'));
  });
  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (agentConfig?: unknown) => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form
        .flow=${{ id: 'saved-flow', name: 'Implement', agent_type: 'codex', agent_config: agentConfig }}
      ></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };
  const control = (element: PreloopFlowForm, field: string): any => {
    const input = element.shadowRoot!.querySelector(
      `[data-feedback="${field}"]`
    );
    expect(input, `feedback ${field} control`).to.exist;
    return input;
  };
  const toggle = async (element: PreloopFlowForm, checked: boolean) => {
    const input = control(element, 'enabled');
    input.checked = checked;
    input.dispatchEvent(new Event('sl-change', { bubbles: true }));
    await element.updateComplete;
  };
  const change = async (
    element: PreloopFlowForm,
    field: string,
    value: string
  ) => {
    const input = control(element, field);
    input.value = value;
    input.dispatchEvent(new Event('sl-input', { bubbles: true }));
    await element.updateComplete;
  };
  const submit = async (element: PreloopFlowForm) => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener, { once: true });
    await (element as any).handleFormSubmit(new Event('submit'));
    return listener;
  };

  it('keeps existing flows opted out without inventing policy on save', async () => {
    const element = await mount({ image: 'project:test' });
    expect(control(element, 'enabled').checked).to.equal(false);
    expect(element.shadowRoot!.querySelector('[data-feedback="max_turns"]')).to
      .not.exist;
    const event = await submit(element);
    expect(event.firstCall.args[0].detail.flow.agent_config).to.deep.equal({
      image: 'project:test',
    });
  });

  it('opts in through rendered controls and serializes bounded numeric policy and exact IDs', async () => {
    const element = await mount();
    await toggle(element, true);
    await change(element, 'trusted_reviewer_ids', '123, 9007199254740993');
    await change(element, 'implementer_actor_ids', '456');
    await change(element, 'max_turns', '7');
    await change(element, 'max_cost', '12.50');
    await change(element, 'max_age_hours', '72');
    await change(element, 'debounce_seconds', '0');
    const event = await submit(element);
    expect(event.callCount).to.equal(1);
    expect(
      event.firstCall.args[0].detail.flow.agent_config.feedback
    ).to.deep.equal({
      enabled: true,
      trusted_reviewer_ids: ['123', '9007199254740993'],
      implementer_actor_ids: ['456'],
      max_turns: 7,
      max_cost: 12.5,
      max_age_hours: 72,
      debounce_seconds: 0,
    });
    expect(element.shadowRoot!.textContent).to.include(
      'starts fresh from the issue and PR context'
    );
    expect(element.shadowRoot!.textContent).to.include('Merge remains manual');
  });

  it('hydrates JSON configuration and preserves routing, execution and advanced feedback keys', async () => {
    const saved = {
      execution_path: 'persistent',
      target_agent_id: 'agent-1',
      environment_profile: 'tests',
      custom_option: { keep: true },
      model_routing: {
        version: 1,
        rules: [
          {
            id: 'rule',
            labels: { any: ['docs'] },
            ai_model_id: 'model-1',
            agent_type: 'codex',
          },
        ],
      },
      feedback: {
        enabled: true,
        trusted_reviewer_ids: [123],
        implementer_actor_ids: ['456'],
        max_turns: 9,
        max_cost: 23,
        max_age_hours: 48,
        debounce_seconds: 60,
        required_checks: ['unit'],
        max_no_progress: 3,
        future_policy: { keep: true },
      },
    };
    const element = await mount(JSON.stringify(saved));
    expect(control(element, 'enabled').checked).to.equal(true);
    expect(control(element, 'trusted_reviewer_ids').value).to.equal('123');
    expect(control(element, 'max_turns').value).to.equal('9');
    const event = await submit(element);
    expect(event.firstCall.args[0].detail.flow.agent_config).to.deep.equal(
      saved
    );
    await toggle(element, false);
    const disabled = await submit(element);
    expect(disabled.firstCall.args[0].detail.flow.agent_config).to.deep.equal({
      ...saved,
      feedback: { ...saved.feedback, enabled: false },
    });
  });

  it('refreshes controls when a preset is selected and clears them for a blank flow', async () => {
    const element = await mount({ feedback: { enabled: true, max_turns: 9 } });
    await (element as any).selectPreset({
      id: 'preset',
      name: 'Preset',
      agent_type: 'codex',
      agent_config: { feedback: { enabled: true, max_turns: 2 } },
    });
    await element.updateComplete;
    expect(control(element, 'max_turns').value).to.equal('2');
    (element as any).selectBlankFlow();
    await element.updateComplete;
    expect(control(element, 'enabled').checked).to.equal(false);
    expect(element.flow.agent_config).to.equal(undefined);
  });

  it('does not mutate the original config while editing feedback', async () => {
    const saved = {
      feedback: { enabled: true, max_turns: 5, trusted_reviewer_ids: [123] },
      image: 'project:test',
    };
    const element = await mount(saved);
    await change(element, 'max_turns', '8');
    await change(element, 'trusted_reviewer_ids', '789');
    expect(saved.feedback).to.deep.equal({
      enabled: true,
      max_turns: 5,
      trusted_reviewer_ids: [123],
    });
  });

  for (const [field, value] of [
    ['max_turns', '0'],
    ['max_turns', '1.5'],
    ['max_cost', '-1'],
    ['max_cost', ''],
    ['max_age_hours', '8761'],
    ['debounce_seconds', '-1'],
    ['trusted_reviewer_ids', 'review-bot'],
    ['implementer_actor_ids', '1.5'],
  ]) {
    it(`rejects invalid ${field} ${JSON.stringify(value)} before submit`, async () => {
      const element = await mount();
      await toggle(element, true);
      await change(element, field, value);
      const event = await submit(element);
      expect(event.callCount).to.equal(0);
      expect((element as any).formError).to.include('Follow-up');
      expect(control(element, field).value).to.equal(value);
    });
  }
});
