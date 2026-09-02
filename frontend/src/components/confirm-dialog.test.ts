import { expect, aTimeout } from '@open-wc/testing';

import {
  confirmDialog,
  resetConfirmDialogForTests,
  showToast,
} from './confirm-dialog';
import type { ConfirmDialog } from './confirm-dialog';

function dialogElement(): ConfirmDialog {
  const element = document.body.querySelector(
    'confirm-dialog'
  ) as ConfirmDialog | null;
  expect(element, 'confirm-dialog singleton is mounted').to.exist;
  return element as ConfirmDialog;
}

async function clickFooterButton(label: string) {
  const element = dialogElement();
  await element.updateComplete;
  const button = [
    ...(element.shadowRoot?.querySelectorAll('sl-button') || []),
  ].find((candidate) => candidate.textContent?.trim() === label);
  expect(button, `footer button "${label}" exists`).to.exist;
  (button as HTMLElement).click();
}

describe('confirmDialog', () => {
  afterEach(() => {
    resetConfirmDialogForTests();
    document.body
      .querySelectorAll('sl-alert')
      .forEach((alert) => alert.remove());
  });

  it('mounts a single shared dialog no matter how often it is called', async () => {
    const first = confirmDialog({ title: 'One', message: 'First question' });
    await aTimeout(0);
    const second = confirmDialog({ title: 'Two', message: 'Second question' });
    await aTimeout(0);

    expect(document.body.querySelectorAll('confirm-dialog')).to.have.length(1);
    // The superseded question resolves false instead of hanging forever.
    expect(await first).to.equal(false);

    await clickFooterButton('Confirm');
    expect(await second).to.equal(true);
  });

  it('resolves true only when the confirm button is pressed', async () => {
    const answer = confirmDialog({
      title: 'Remove agent',
      message: 'Remove Hermes from the managed agents list?',
      confirmLabel: 'Remove',
      variant: 'danger',
    });
    await aTimeout(0);

    const element = dialogElement();
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain(
      'Remove Hermes from the managed agents list?'
    );
    const dialog = element.shadowRoot?.querySelector('sl-dialog');
    expect(dialog?.getAttribute('label')).to.equal('Remove agent');

    await clickFooterButton('Remove');
    expect(await answer).to.equal(true);
  });

  it('resolves false when cancelled', async () => {
    const answer = confirmDialog({ title: 'Pause', message: 'Pause Hermes?' });
    await aTimeout(0);

    await clickFooterButton('Cancel');
    expect(await answer).to.equal(false);
  });

  it('resolves false when dismissed without answering', async () => {
    const answer = confirmDialog({ title: 'Pause', message: 'Pause Hermes?' });
    await aTimeout(0);

    const element = dialogElement();
    await element.updateComplete;
    element.shadowRoot
      ?.querySelector('sl-dialog')
      ?.dispatchEvent(new CustomEvent('sl-after-hide', { bubbles: false }));

    expect(await answer).to.equal(false);
  });

  it('renders the optional detail line under the message', async () => {
    const answer = confirmDialog({
      title: 'Remove agent',
      message: 'Remove Hermes?',
      detail: 'This also revokes its credentials.',
    });
    await aTimeout(0);

    const element = dialogElement();
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('.detail')?.textContent
    ).to.contain('This also revokes its credentials.');

    await clickFooterButton('Cancel');
    await answer;
  });
});

describe('showToast', () => {
  afterEach(() => {
    document.body
      .querySelectorAll('sl-alert')
      .forEach((alert) => alert.remove());
  });

  it('appends a Shoelace alert instead of blocking the tab', async () => {
    showToast('No user matched that username or email.', 'warning');
    await aTimeout(0);

    const alert = document.body.querySelector('sl-alert');
    expect(alert).to.exist;
    expect(alert?.getAttribute('variant')).to.equal('warning');
    expect(alert?.textContent).to.contain(
      'No user matched that username or email.'
    );
  });
});
