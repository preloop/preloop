import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import {
  FLOW_TIMEOUT_MAX_SECONDS,
  FLOW_TIMEOUT_MIN_SECONDS,
} from './preloop-flow-form';

const preset = {
  id: 'timeout-preset',
  name: 'Implementation',
  prompt_template: 'Implement the issue',
  agent_type: 'codex',
  trigger_event_types: ['webhook'],
  timeout_seconds: 5400,
};

describe('PreloopFlowForm execution timeout', () => {
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

  const input = (element: PreloopFlowForm): SlInput => {
    const control = element.shadowRoot!.querySelector<SlInput>(
      'sl-input[name="timeout_seconds"]'
    );
    expect(control, 'Execution timeout control').to.exist;
    return control!;
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

  const enter = async (
    element: PreloopFlowForm,
    value: string
  ): Promise<void> => {
    const control = input(element);
    control.value = value;
    control.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;
  };

  it('shows and preserves an existing timeout when editing other fields', async () => {
    const element = await mount(5400);
    element.flow = {
      ...element.flow,
      id: 'flow-1',
      description: 'Updated description',
    };
    await element.updateComplete;
    expect(input(element).value).to.equal('5400');
    expect((await submit(element)).timeout_seconds).to.equal(5400);
  });

  it('keeps blank on a new flow and submits the deployment default explicitly', async () => {
    const element = await mount();
    expect(input(element).value).to.equal('');
    expect((await submit(element)).timeout_seconds).to.equal(null);
  });

  it('clears a saved override with null', async () => {
    const element = await mount(5400);
    await enter(element, '');
    expect((await submit(element)).timeout_seconds).to.equal(null);
  });

  it('preserves a selected preset timeout in the control and payload', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection(preset.id);
    await element.updateComplete;
    expect(input(element).value).to.equal('5400');
    expect((await submit(element)).timeout_seconds).to.equal(5400);
  });

  for (const value of [
    FLOW_TIMEOUT_MIN_SECONDS,
    90,
    FLOW_TIMEOUT_MAX_SECONDS,
  ]) {
    it(`submits valid whole seconds without rounding: ${value}`, async () => {
      const element = await mount();
      await enter(element, String(value));
      expect((await submit(element)).timeout_seconds).to.equal(value);
    });
  }

  for (const value of [
    0,
    FLOW_TIMEOUT_MIN_SECONDS - 1,
    FLOW_TIMEOUT_MAX_SECONDS + 1,
    60.5,
    NaN,
  ]) {
    it(`rejects invalid timeout ${value} before submission`, async () => {
      const element = await mount(value);
      const listener = sandbox.spy();
      element.addEventListener('flow-submit', listener);
      await (element as any).handleFormSubmit(new Event('submit'));
      await element.updateComplete;
      expect(listener.callCount).to.equal(0);
      expect(element.shadowRoot!.textContent).to.include(
        `whole number between ${FLOW_TIMEOUT_MIN_SECONDS} and ${FLOW_TIMEOUT_MAX_SECONDS} seconds`
      );
    });
  }
});
