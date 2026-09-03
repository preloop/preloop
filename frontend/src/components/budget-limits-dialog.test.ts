import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
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

    const hidden = oneEvent(element, 'budget-limits-hide');
    dialog.dispatchEvent(new CustomEvent('sl-hide', { bubbles: true }));
    await hidden;
    await element.updateComplete;

    expect(element.open).to.be.false;
  });

  it('does not dismiss when the overlay is clicked', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog open></budget-limits-dialog>`
    );
    await element.updateComplete;
    const dialog = element.shadowRoot!.querySelector('sl-dialog')!;
    const event = new CustomEvent('sl-request-close', {
      bubbles: true,
      cancelable: true,
      composed: true,
      detail: { source: 'overlay' },
    });

    dialog.dispatchEvent(event);
    await element.updateComplete;

    expect(event.defaultPrevented).to.be.true;
    expect(element.open).to.be.true;
  });

  it('does not close when a nested select or dropdown hides', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog open></budget-limits-dialog>`
    );
    await element.updateComplete;
    const editor = element.shadowRoot!.querySelector('budget-policy-editor')!;

    editor.dispatchEvent(
      new CustomEvent('sl-hide', { bubbles: true, composed: true })
    );
    await element.updateComplete;

    expect(element.open).to.be.true;
  });

  it('does not let nested sl-hide reach parent listeners', async () => {
    let parentSawHide = false;
    const wrap = await fixture(
      html`<div
        @sl-hide=${() => {
          parentSawHide = true;
        }}
      >
        <budget-limits-dialog open></budget-limits-dialog>
      </div>`
    );
    const element = wrap.querySelector('budget-limits-dialog')!;
    await element.updateComplete;
    const editor = element.shadowRoot!.querySelector('budget-policy-editor')!;

    editor.dispatchEvent(
      new CustomEvent('sl-hide', { bubbles: true, composed: true })
    );
    await element.updateComplete;

    expect(element.open).to.be.true;
    expect(parentSawHide).to.be.false;
  });

  it('lets a nested dialog sl-hide handler run', async () => {
    const element = await fixture<BudgetLimitsDialog>(
      html`<budget-limits-dialog open></budget-limits-dialog>`
    );
    await element.updateComplete;
    const editor = element.shadowRoot!.querySelector('budget-policy-editor')!;
    const nested = document.createElement('sl-dialog');
    let nestedHideRan = false;
    nested.addEventListener('sl-hide', () => {
      nestedHideRan = true;
    });
    editor.appendChild(nested);

    let outerHide = false;
    element.addEventListener('budget-limits-hide', () => {
      outerHide = true;
    });
    nested.dispatchEvent(
      new CustomEvent('sl-hide', { bubbles: true, composed: true })
    );
    await element.updateComplete;

    expect(nestedHideRan).to.be.true;
    expect(element.open).to.be.true;
    expect(outerHide).to.be.false;
  });
});
