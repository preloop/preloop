import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import { splitApprovalWindow } from './preloop-flow-form';

/**
 * The approval window on the flow form.
 *
 * Compliance approvals are set in days, so the form is set in days. Typing
 * 259200 into a seconds box is how a three day window becomes a three hour
 * one.
 */
describe('PreloopFlowForm approval window', () => {
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
    approvalWindowSeconds?: number | null
  ): Promise<PreloopFlowForm> => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form
        .flow=${{
          name: 'Release security audit',
          prompt_template: 'Audit it',
          agent_type: 'codex',
          approval_window_seconds: approvalWindowSeconds,
        }}
      ></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  const amountInput = (element: PreloopFlowForm) =>
    element.shadowRoot?.querySelector(
      'sl-input[name="approval_window_amount"]'
    ) as SlInput;

  const unitSelect = (element: PreloopFlowForm) =>
    element.shadowRoot?.querySelector(
      'sl-select[name="approval_window_unit"]'
    ) as HTMLElement & { value: string };

  describe('splitApprovalWindow', () => {
    it('shows three days as three days', () => {
      expect(splitApprovalWindow(259200)).to.deep.equal({
        amount: 3,
        unit: 'days',
      });
    });

    it('shows two hours as two hours', () => {
      expect(splitApprovalWindow(7200)).to.deep.equal({
        amount: 2,
        unit: 'hours',
      });
    });

    it('falls back to minutes for anything that is not whole hours', () => {
      expect(splitApprovalWindow(300)).to.deep.equal({
        amount: 5,
        unit: 'minutes',
      });
    });

    it('treats a missing window as unset rather than zero', () => {
      expect(splitApprovalWindow(null).amount).to.equal(null);
      expect(splitApprovalWindow(undefined).amount).to.equal(null);
    });
  });

  it('renders the saved window in the unit it was set in', async () => {
    const element = await mount(259200);

    expect(amountInput(element).value).to.equal('3');
    expect(unitSelect(element).value).to.equal('days');
  });

  it('offers the deployment default when no window is saved', async () => {
    const element = await mount(null);

    expect(amountInput(element).value).to.equal('');
    expect(amountInput(element).getAttribute('placeholder')).to.contain(
      '5 minutes'
    );
  });

  it('composes the amount and the unit into seconds', async () => {
    const element = await mount(null);
    const input = amountInput(element);
    input.value = '3';
    input.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
    const select = unitSelect(element);
    select.value = 'days';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;

    expect((element as any).flow.approval_window_seconds).to.equal(259200);
  });

  it('clears the override when the amount is emptied', async () => {
    const element = await mount(259200);
    const input = amountInput(element);
    input.value = '';
    input.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;

    expect((element as any).flow.approval_window_seconds).to.equal(null);
  });

  it('explains that a parked run does not spend the execution timeout', async () => {
    const element = await mount(259200);
    const help = element.shadowRoot?.querySelector('.approval-window-help');

    expect(help?.textContent).to.contain('parked');
    expect(help?.textContent).to.contain('paused');
  });
});
