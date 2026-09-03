import { expect, fixture, html, waitUntil } from '@open-wc/testing';

import '../components/confirm-dialog';
import type { ConfirmDialog } from '../components/confirm-dialog';

/**
 * The console centres modals over the content area, not over the window: a
 * dialog opened from a button in the middle of the page is half a sidebar off
 * when it centres on the viewport. `console-shell` publishes the sidebar width
 * as `--console-main-offset` and `consoleDialogStyles` reads it.
 */
describe('console dialogs', () => {
  async function openDialog(offset: string): Promise<HTMLElement> {
    const host = (await fixture(html`
      <div style="--console-main-offset: ${offset};">
        <confirm-dialog></confirm-dialog>
      </div>
    `)) as HTMLElement;

    const dialog = host.querySelector('confirm-dialog') as ConfirmDialog;
    dialog.ask({ title: 'Remove agent', message: 'Are you sure?' });
    await waitUntil(
      () => Boolean(dialog.shadowRoot?.querySelector('sl-dialog')),
      'the dialog did not render'
    );
    await dialog.updateComplete;

    const slDialog = dialog.shadowRoot?.querySelector(
      'sl-dialog'
    ) as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await slDialog.updateComplete;
    return slDialog.shadowRoot?.querySelector('[part~="base"]') as HTMLElement;
  }

  it('shifts the centring box by the sidebar width', async () => {
    const base = await openDialog('250px');
    expect(base, 'the dialog base part').to.exist;
    expect(getComputedStyle(base).left).to.equal('250px');

    // The dim still covers the sidebar: the overlay is fixed on its own.
    const overlay = base.querySelector('[part~="overlay"]') as HTMLElement;
    const overlayStyles = getComputedStyle(overlay);
    expect(overlayStyles.position).to.equal('fixed');
    expect(overlayStyles.left).to.equal('0px');
  });

  it('leaves the dialog on the window when the sidebar overlays the page', async () => {
    // Mobile: the sidebar is not taking space, so there is nothing to offset.
    const base = await openDialog('0px');
    expect(getComputedStyle(base).left).to.equal('0px');
  });
});
