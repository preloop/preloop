import { html, fixture, expect } from '@open-wc/testing';
import './notify-recipients-field.ts';
import type { NotifyRecipientsField } from './notify-recipients-field';

describe('NotifyRecipientsField', () => {
  async function addCustomEmail(element: NotifyRecipientsField, value: string) {
    const input = element.shadowRoot?.querySelector('sl-input');
    expect(input).to.exist;
    (input as HTMLInputElement).value = value;
    input?.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    const addButton = [
      ...(element.shadowRoot?.querySelectorAll('sl-button') || []),
    ].find((button) => button.textContent?.trim() === 'Add');
    expect(addButton).to.exist;
    addButton?.click();
    await element.updateComplete;
  }

  it('rejects invalid custom email addresses with inline feedback', async () => {
    const element = (await fixture(
      html`<notify-recipients-field></notify-recipients-field>`
    )) as NotifyRecipientsField;

    await addCustomEmail(element, 'not-an-email');

    const badges = element.shadowRoot?.querySelectorAll('sl-badge');
    expect(badges?.length || 0).to.equal(0);
    expect(element.shadowRoot?.textContent).to.contain(
      'Enter a valid email address.'
    );
  });

  it('accepts valid custom email addresses', async () => {
    const element = (await fixture(
      html`<notify-recipients-field></notify-recipients-field>`
    )) as NotifyRecipientsField;

    await addCustomEmail(element, 'alerts@example.com');

    expect(element.shadowRoot?.textContent).to.contain('alerts@example.com');
  });
});
