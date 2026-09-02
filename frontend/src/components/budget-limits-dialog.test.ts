import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import './budget-limits-dialog.ts';
import type { BudgetLimitsDialog } from './budget-limits-dialog';

describe('budget-limits-dialog', () => {
  it('hosts the limits editor with the shared explanation', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog open></budget-limits-dialog>`
    );
    await element.updateComplete;

    const dialog = element.shadowRoot!.querySelector('sl-dialog')!;
    expect(dialog.getAttribute('label')).to.equal('Spending limits');
    expect(
      element.shadowRoot!.querySelector('.description')!.textContent!.trim()
    ).to.equal(
      'Soft limits notify. Hard limits stop model calls through the gateway.'
    );
    expect(element.shadowRoot!.querySelector('budget-policy-editor')).to.exist;
  });

  it('keeps the editor out of the tree until it is opened', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog></budget-limits-dialog>`
    );
    await element.updateComplete;

    expect(element.shadowRoot!.querySelector('budget-policy-editor')).to.not
      .exist;
  });

  it('forwards budget-policies-changed from the editor', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog open></budget-limits-dialog>`
    );
    await element.updateComplete;
    const editor = element.shadowRoot!.querySelector('budget-policy-editor')!;

    setTimeout(() =>
      editor.dispatchEvent(
        new CustomEvent('budget-policies-changed', {
          bubbles: true,
          composed: true,
          detail: { policies: [] },
        })
      )
    );
    await oneEvent(element, 'budget-policies-changed');
  });

  it('clears open when the dialog is dismissed', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog open></budget-limits-dialog>`
    );
    await element.updateComplete;
    const dialog = element.shadowRoot!.querySelector('sl-dialog')!;

    dialog.dispatchEvent(new CustomEvent('sl-hide', { bubbles: true }));
    await element.updateComplete;

    expect(element.open).to.be.false;
  });
});
