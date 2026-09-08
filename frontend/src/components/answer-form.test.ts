import { html, fixture, expect } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import './answer-form.ts';
import type { AnswerForm } from './answer-form';
import type { QuestionItem, QuestionSchema } from '../types';

/**
 * The form is the whole point of the feature: a person answering "which of
 * these findings do you waive, and why each" must never be asked to type
 * JSON. So the tests care about four things: every schema type draws a real
 * control, required fields block a submit, the payload that leaves matches
 * the schema, and an auto-filled author is not an input.
 */
describe('AnswerForm', () => {
  const waiverSchema: QuestionSchema = {
    type: 'object',
    properties: {
      waived: {
        type: 'array',
        title: 'Findings to waive',
        items: {
          type: 'object',
          properties: {
            id: { type: 'string', enum: ['CVE-1', 'CVE-2'] },
            reason: { type: 'string', title: 'Reason', minLength: 5 },
          },
          required: ['id', 'reason'],
        },
      },
      approver: { type: 'string', title: 'Approver', 'x-autofill': 'author' },
    },
    required: ['waived'],
  };

  const items: QuestionItem[] = [
    {
      id: 'CVE-1',
      title: 'curl 8.4.0',
      description: 'Heap overflow in SOCKS5',
      severity: 'high',
      badges: ['KEV'],
    },
    { id: 'CVE-2', title: 'requests 2.31.0', severity: 'medium' },
  ];

  async function mount(
    schema: QuestionSchema | null,
    rows: QuestionItem[] = []
  ): Promise<AnswerForm> {
    const element = (await fixture(html`
      <answer-form
        .schema=${schema}
        .items=${rows}
        .author=${'dimo@example.com'}
      ></answer-form>
    `)) as AnswerForm;
    await element.updateComplete;
    return element;
  }

  function text(element: AnswerForm): string {
    return element.shadowRoot?.textContent || '';
  }

  async function check(element: AnswerForm, itemId: string) {
    const box = element.shadowRoot?.querySelector(
      `tr[data-item-id="${itemId}"] .item-checkbox`
    ) as HTMLInputElement;
    box.checked = true;
    box.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;
  }

  it('renders nothing without a schema', async () => {
    const element = await mount(null);
    expect(element.shadowRoot?.querySelector('.answer-form')).to.equal(null);
  });

  it('draws the items as a table with a checkbox per row', async () => {
    const element = await mount(waiverSchema, items);

    expect(element.shadowRoot?.querySelectorAll('tbody tr').length).to.equal(2);
    expect(
      element.shadowRoot?.querySelectorAll('.item-checkbox').length
    ).to.equal(2);
    // The human reads the finding, not the id.
    expect(text(element)).to.contain('curl 8.4.0');
    expect(text(element)).to.contain('Heap overflow in SOCKS5');
    expect(text(element)).to.contain('high');
    expect(text(element)).to.contain('KEV');
  });

  it('reveals the per-row field only for the rows that were picked', async () => {
    const element = await mount(waiverSchema, items);
    expect(
      element.shadowRoot?.querySelectorAll('.row-fields').length,
      'no row fields before a pick'
    ).to.equal(0);

    await check(element, 'CVE-1');
    expect(element.shadowRoot?.querySelectorAll('.row-fields').length).to.equal(
      1
    );
  });

  it('builds the answer the schema described', async () => {
    const element = await mount(waiverSchema, items);
    await check(element, 'CVE-2');

    const input = element.shadowRoot?.querySelector(
      'tr[data-item-id="CVE-2"] .field-input'
    ) as HTMLInputElement;
    input.value = 'no fix released yet';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;

    expect(element.answer).to.deep.equal({
      waived: [{ id: 'CVE-2', reason: 'no fix released yet' }],
    });
    // The author is stamped by the server, never carried from the client.
    expect(Object.keys(element.answer)).to.not.contain('approver');
  });

  it('refuses to validate while a required field is empty', async () => {
    const element = await mount(waiverSchema, items);
    expect(element.validate(), 'empty selection').to.be.false;

    await check(element, 'CVE-1');
    expect(element.validate(), 'picked but no reason').to.be.false;
    await element.updateComplete;
    expect(text(element)).to.contain('Reason is required');
  });

  it('enforces the schema minimum length before the server has to', async () => {
    const element = await mount(waiverSchema, items);
    await check(element, 'CVE-1');
    const input = element.shadowRoot?.querySelector(
      'tr[data-item-id="CVE-1"] .field-input'
    ) as HTMLInputElement;
    input.value = 'no';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;

    expect(element.validate()).to.be.false;
    await element.updateComplete;
    expect(text(element)).to.contain('Needs at least 5 characters');
  });

  it('puts the server errors on the fields they name', async () => {
    const element = await mount(waiverSchema, items);
    await check(element, 'CVE-1');
    element.setServerErrors([
      { path: 'waived[0].reason', message: 'is required' },
    ]);
    await element.updateComplete;
    expect(text(element)).to.contain('is required');
  });

  it('renders a boolean as a switch and a small enum as radios', async () => {
    const element = await mount({
      type: 'object',
      properties: {
        acknowledged: { type: 'boolean', title: 'I have read the advisory' },
        window: { type: 'string', enum: ['now', 'tonight', 'next release'] },
      },
      required: ['window'],
    });

    expect(element.shadowRoot?.querySelector('sl-switch')).to.not.equal(null);
    expect(element.shadowRoot?.querySelector('sl-radio-group')).to.not.equal(
      null
    );
    expect(element.shadowRoot?.querySelectorAll('sl-radio').length).to.equal(3);
  });

  it('uses a select once an enum outgrows radios', async () => {
    const element = await mount({
      type: 'object',
      properties: {
        owner: { type: 'string', enum: ['a', 'b', 'c', 'd', 'e'] },
      },
    });
    expect(element.shadowRoot?.querySelector('sl-select')).to.not.equal(null);
    expect(element.shadowRoot?.querySelector('sl-radio-group')).to.equal(null);
  });

  it('renders text, numbers, dates and textareas as their own controls', async () => {
    const element = await mount({
      type: 'object',
      properties: {
        ticket: { type: 'string', title: 'Ticket' },
        days: { type: 'integer', title: 'Days', minimum: 1, maximum: 90 },
        until: { type: 'string', format: 'date', title: 'Until' },
        notes: { type: 'string', format: 'textarea', title: 'Notes' },
      },
    });

    const types = Array.from(
      element.shadowRoot?.querySelectorAll('sl-input') ?? []
    ).map((input) => input.getAttribute('type'));
    expect(types).to.include('text');
    expect(types).to.include('number');
    expect(types).to.include('date');
    expect(element.shadowRoot?.querySelector('sl-textarea')).to.not.equal(null);
  });

  it('never offers the auto-filled author as an input', async () => {
    const element = await mount(waiverSchema, items);
    const autofilled = element.shadowRoot?.querySelector(
      '[data-autofill="author"]'
    );
    expect(autofilled).to.not.equal(null);
    expect(autofilled?.querySelector('sl-input')).to.equal(null);
    expect(text(element)).to.contain('dimo@example.com');
    expect(text(element)).to.contain('Filled in by Preloop');
  });

  it('carries the accessible roles a keyboard user navigates by', async () => {
    const element = await mount(waiverSchema, items);
    const group = element.shadowRoot?.querySelector('[role="group"]');
    expect(group?.getAttribute('aria-label')).to.equal('Answer form');

    const headers = Array.from(
      element.shadowRoot?.querySelectorAll('th') ?? []
    );
    expect(headers.length).to.be.greaterThan(0);
    expect(headers.every((th) => th.getAttribute('scope') === 'col')).to.be
      .true;

    element.validate();
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('[role="alert"]'),
      'a complaint is announced'
    ).to.not.equal(null);
  });

  it('fits a 390px phone without a sideways scroll', async () => {
    await setViewport({ width: 390, height: 844 });
    const element = await mount(waiverSchema, items);
    await check(element, 'CVE-1');

    const table = element.shadowRoot?.querySelector(
      '.item-table'
    ) as HTMLElement;
    expect(table.scrollWidth).to.be.at.most(390);
    await setViewport({ width: 1280, height: 800 });
  });

  it('reports validity on every edit so a parent can gate its button', async () => {
    const element = await mount(waiverSchema, items);
    let last: { valid: boolean } | null = null;
    element.addEventListener('answer-change', (e) => {
      last = (e as CustomEvent).detail;
    });

    await check(element, 'CVE-1');
    expect(last).to.not.equal(null);
    expect(last!.valid, 'reason still empty').to.be.false;
  });
});
