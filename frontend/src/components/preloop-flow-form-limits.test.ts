import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * Per-run limits: the spend limit and the model-call limit are stored as
 * agent_config.limits and enforced by the gateway. The form used to show "30"
 * and "$10" as if they were defaults while sending nothing, and could not
 * clear a limit.
 */
describe('PreloopFlowForm per-run limits', () => {
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

  const mount = async (
    limits: Record<string, unknown> = {}
  ): Promise<PreloopFlowForm> => {
    const flow = {
      name: 'Issue fixer',
      prompt_template: 'Fix it',
      agent_type: 'codex',
      ...limits,
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

  const control = (element: PreloopFlowForm, field: string): SlInput => {
    const input = element.shadowRoot!.querySelector<SlInput>(
      `sl-input[data-field="${field}"]`
    );
    expect(input, field).to.exist;
    return input!;
  };

  const enter = async (
    element: PreloopFlowForm,
    field: string,
    value: string
  ): Promise<void> => {
    const input = control(element, field);
    input.value = value;
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;
  };

  const submit = async (
    element: PreloopFlowForm
  ): Promise<Record<string, unknown> | null> => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    return listener.callCount ? listener.firstCall.args[0].detail.flow : null;
  };

  it('shows no limit as blank instead of a pretend default', async () => {
    const element = await mount();
    expect(control(element, 'max_budget').value).to.equal('');
    expect(control(element, 'max_iterations').value).to.equal('');
    expect(control(element, 'max_budget').placeholder).to.equal('No limit');
  });

  it('shows and resubmits the stored limits when editing other fields', async () => {
    const element = await mount({ max_budget: 5, max_iterations: 40 });
    expect(control(element, 'max_budget').value).to.equal('5');
    expect(control(element, 'max_iterations').value).to.equal('40');
    const payload = await submit(element);
    expect(payload?.max_budget).to.equal(5);
    expect(payload?.max_iterations).to.equal(40);
  });

  it('clears a stored limit with null', async () => {
    const element = await mount({ max_budget: 5, max_iterations: 40 });
    await enter(element, 'max_budget', '');
    const payload = await submit(element);
    expect(payload?.max_budget).to.equal(null);
    expect(payload?.max_iterations).to.equal(40);
  });

  it('leaves the stored limits alone when the form never loaded them', async () => {
    const element = await mount();
    const payload = await submit(element);
    expect(payload).to.not.have.property('max_budget');
    expect(payload).to.not.have.property('max_iterations');
  });

  it('submits a newly entered limit', async () => {
    const element = await mount();
    await enter(element, 'max_budget', '2.5');
    await enter(element, 'max_iterations', '60');
    const payload = await submit(element);
    expect(payload?.max_budget).to.equal(2.5);
    expect(payload?.max_iterations).to.equal(60);
  });

  for (const [field, value] of [
    ['max_budget', '0'],
    ['max_budget', '-3'],
    ['max_iterations', '0'],
    ['max_iterations', '2.5'],
  ] as const) {
    it(`rejects ${field}=${value} on the field instead of sending it`, async () => {
      const element = await mount();
      await enter(element, field, value);
      expect(await submit(element)).to.equal(null);
      expect((element as any).formError).to.be.a('string').and.not.empty;
    });
  }
});
