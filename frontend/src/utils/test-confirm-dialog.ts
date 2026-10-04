import { waitUntil } from '@open-wc/testing';

/**
 * Test helper: waits for the shared `confirmDialog` to open, then answers it.
 *
 * @param confirm - Press the confirm button (true) or Cancel (false)
 * @returns The dialog's text, whitespace collapsed, for asserting its copy
 */
export async function answerConfirmDialog(confirm: boolean): Promise<string> {
  await waitUntil(
    () =>
      document
        .querySelector('confirm-dialog')
        ?.shadowRoot?.querySelector('sl-dialog[open]'),
    'the confirm dialog did not open'
  );
  const root = document.querySelector('confirm-dialog')!.shadowRoot!;
  const dialog = root.querySelector('sl-dialog')!;
  const text = `${dialog.getAttribute('label') ?? ''} ${
    dialog.textContent ?? ''
  }`
    .replace(/\s+/g, ' ')
    .trim();
  const button = confirm
    ? root.querySelector('[data-testid="confirm-dialog-confirm"]')
    : root.querySelector('sl-button[slot="footer"]');
  (button as HTMLElement).click();
  return text;
}
