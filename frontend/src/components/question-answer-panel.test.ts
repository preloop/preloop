import { html, fixture, expect, oneEvent } from '@open-wc/testing';
import './question-answer-panel.ts';
import type {
  QuestionAnswerDetail,
  QuestionAnswerPanel,
} from './question-answer-panel';
import type { QuestionSchema } from '../types';

/**
 * The panel is where the founder's complaint lands: "it asked me to select
 * some CVE and type JSON in the freeform text". So the tests assert the two
 * shapes it must keep apart. With a schema it is a form and the payload is
 * JSON the agent can apply. Without one it is exactly the options-and-textarea
 * panel it always was, because presets still use that.
 */
describe('QuestionAnswerPanel', () => {
  const schema: QuestionSchema = {
    type: 'object',
    properties: {
      waived: {
        type: 'array',
        title: 'Findings to waive',
        items: {
          type: 'object',
          properties: {
            id: { type: 'string', enum: ['CVE-1'] },
            reason: { type: 'string', title: 'Reason' },
          },
          required: ['id', 'reason'],
        },
      },
    },
    required: ['waived'],
  };

  async function mount(
    props: Partial<QuestionAnswerPanel> = {}
  ): Promise<QuestionAnswerPanel> {
    const element = (await fixture(html`
      <question-answer-panel
        .question=${'Which findings do you waive?'}
        .options=${props.options ?? []}
        .allowFreeText=${props.allowFreeText ?? false}
        .inputSchema=${props.inputSchema ?? null}
        .items=${props.items ?? []}
      ></question-answer-panel>
    `)) as QuestionAnswerPanel;
    await element.updateComplete;
    return element;
  }

  it('keeps the options and the textarea when no schema was given', async () => {
    const element = await mount({
      options: ['yes', 'no'],
      allowFreeText: true,
    });
    expect(
      element.shadowRoot?.querySelectorAll('.question-option').length
    ).to.equal(2);
    expect(element.shadowRoot?.querySelector('answer-form')).to.equal(null);
    expect(element.shadowRoot?.querySelector('.answer-input')).to.not.equal(
      null
    );
  });

  it('replaces the options with a form when a schema was given', async () => {
    const element = await mount({
      options: ['yes', 'no'],
      inputSchema: schema,
      items: [{ id: 'CVE-1', title: 'curl 8.4.0' }],
    });
    expect(element.shadowRoot?.querySelector('answer-form')).to.not.equal(null);
    // Buttons that bypass the form would answer a different question.
    expect(
      element.shadowRoot?.querySelectorAll('.question-option').length
    ).to.equal(0);
  });

  it('sends the filled-in form as JSON, not as text', async () => {
    const element = await mount({
      inputSchema: schema,
      items: [{ id: 'CVE-1', title: 'curl 8.4.0' }],
    });
    const form = element.shadowRoot?.querySelector('answer-form') as any;
    await form.updateComplete;

    const box = form.shadowRoot.querySelector('.item-checkbox');
    box.checked = true;
    box.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await form.updateComplete;
    const input = form.shadowRoot.querySelector('.field-input');
    input.value = 'no fix yet';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await form.updateComplete;

    const send = element.shadowRoot?.querySelector('.send-form') as HTMLElement;
    setTimeout(() => send.click());
    const event = (await oneEvent(
      element,
      'question-answer'
    )) as CustomEvent<QuestionAnswerDetail>;

    expect(event.detail.answer).to.deep.equal({
      waived: [{ id: 'CVE-1', reason: 'no fix yet' }],
    });
    expect(event.detail.answerText).to.equal(undefined);
  });

  it('does not submit an incomplete form', async () => {
    const element = await mount({
      inputSchema: schema,
      items: [{ id: 'CVE-1', title: 'curl 8.4.0' }],
    });
    let fired = false;
    element.addEventListener('question-answer', () => (fired = true));

    (element.shadowRoot?.querySelector('.send-form') as HTMLElement).click();
    await element.updateComplete;

    expect(fired, 'nothing left with a required field empty').to.be.false;
  });

  it('still dismisses a question that carries a form', async () => {
    const element = await mount({ inputSchema: schema });
    setTimeout(() =>
      (
        element.shadowRoot?.querySelector('.dismiss-question') as HTMLElement
      ).click()
    );
    const event = await oneEvent(element, 'question-dismiss');
    expect(event).to.not.equal(null);
  });
});
