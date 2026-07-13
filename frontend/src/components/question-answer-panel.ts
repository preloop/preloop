import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';

/**
 * Payload emitted when the operator answers an agent question.
 *
 * Exactly one of `selectedOption` / `answerText` is set. Both map onto an
 * APPROVE decision server-side (precedence: answer_text > selected_option).
 */
export interface QuestionAnswerDetail {
  selectedOption?: string;
  answerText?: string;
}

/**
 * Shared UI for answering an agent question (`ask_user`).
 *
 * Renders the question, one button per offered option, and — when the agent
 * allows it — a free-text answer box. "Dismiss" maps to a DECLINE, mirroring
 * the iOS/Android apps.
 *
 * Events:
 *  - `question-answer` (detail: QuestionAnswerDetail) — operator answered.
 *  - `question-dismiss` — operator dismissed the question (decline).
 */
@customElement('question-answer-panel')
export class QuestionAnswerPanel extends LitElement {
  /** The question to put to the operator. */
  @property({ type: String })
  question = '';

  /** Options the agent offered. May be empty. */
  @property({ type: Array })
  options: string[] = [];

  /** Whether the agent accepts a typed answer. */
  @property({ type: Boolean })
  allowFreeText = false;

  /** Disables all controls while a decision is in flight. */
  @property({ type: Boolean })
  submitting = false;

  /** Compact variant for inline use inside a list card. */
  @property({ type: Boolean, reflect: true })
  compact = false;

  @state()
  private answerText = '';

  static styles = css`
    :host {
      display: block;
    }

    .question-panel {
      border: 1px solid var(--sl-color-neutral-200);
      border-left: 3px solid #30c9e8;
      border-radius: 4px;
      padding: 1rem;
      background: var(--sl-color-neutral-50);
    }

    :host([compact]) .question-panel {
      padding: 0.75rem;
    }

    .question-label {
      display: flex;
      align-items: center;
      gap: 0.375rem;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: #30c9e8;
      margin-bottom: 0.5rem;
    }

    .question-text {
      font-size: 1.125rem;
      line-height: 1.5;
      color: var(--sl-color-neutral-900);
      margin: 0 0 1rem 0;
    }

    :host([compact]) .question-text {
      font-size: 1rem;
      margin-bottom: 0.75rem;
    }

    .options {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }

    .options sl-button::part(base) {
      border-radius: 4px;
    }

    .free-text {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }

    .free-text-actions {
      display: flex;
      justify-content: flex-end;
    }

    .dismiss-row {
      display: flex;
      justify-content: flex-end;
      border-top: 1px solid var(--sl-color-neutral-200);
      padding-top: 0.75rem;
    }

    .no-answer-hint {
      font-size: 0.875rem;
      color: var(--sl-color-neutral-600);
      margin-bottom: 0.75rem;
    }
  `;

  private emitOption(option: string) {
    if (this.submitting) return;
    this.dispatchEvent(
      new CustomEvent<QuestionAnswerDetail>('question-answer', {
        detail: { selectedOption: option },
        bubbles: true,
        composed: true,
      })
    );
  }

  private emitFreeText() {
    const text = this.answerText.trim();
    if (this.submitting || !text) return;
    this.dispatchEvent(
      new CustomEvent<QuestionAnswerDetail>('question-answer', {
        detail: { answerText: text },
        bubbles: true,
        composed: true,
      })
    );
    this.answerText = '';
  }

  private emitDismiss() {
    if (this.submitting) return;
    this.dispatchEvent(
      new CustomEvent('question-dismiss', { bubbles: true, composed: true })
    );
  }

  render() {
    const options = this.options ?? [];
    const hasAnswerControl = options.length > 0 || this.allowFreeText;

    return html`
      <div class="question-panel" part="panel">
        <div class="question-label">
          <sl-icon name="chat-left-quote"></sl-icon>
          Agent question
        </div>
        <p class="question-text">${this.question}</p>

        ${
          options.length > 0
            ? html`
                <div class="options">
                  ${options.map(
                    (option) => html`
                      <sl-button
                        class="question-option"
                        size=${this.compact ? 'small' : 'medium'}
                        variant="primary"
                        outline
                        data-option=${option}
                        ?disabled=${this.submitting}
                        @click=${() => this.emitOption(option)}
                      >
                        ${option}
                      </sl-button>
                    `
                  )}
                </div>
              `
            : ''
        }
        ${
          this.allowFreeText
            ? html`
                <div class="free-text">
                  <sl-textarea
                    class="answer-input"
                    label=${options.length > 0 ? 'Or type an answer' : 'Answer'}
                    placeholder="Type your answer..."
                    rows="2"
                    resize="auto"
                    .value=${this.answerText}
                    ?disabled=${this.submitting}
                    @sl-input=${(e: Event) =>
                      (this.answerText = (
                        e.target as HTMLTextAreaElement
                      ).value)}
                  ></sl-textarea>
                  <div class="free-text-actions">
                    <sl-button
                      class="send-answer"
                      size=${this.compact ? 'small' : 'medium'}
                      variant="success"
                      ?loading=${this.submitting}
                      ?disabled=${this.submitting || !this.answerText.trim()}
                      @click=${this.emitFreeText}
                    >
                      <sl-icon slot="prefix" name="send"></sl-icon>
                      Send answer
                    </sl-button>
                  </div>
                </div>
              `
            : ''
        }
        ${
          !hasAnswerControl
            ? html`<div class="no-answer-hint">
                This agent asked a question but offered no options and does not
                accept free text. You can only dismiss it.
              </div>`
            : ''
        }

        <div class="dismiss-row">
          <sl-button
            class="dismiss-question"
            size="small"
            variant="text"
            ?disabled=${this.submitting}
            @click=${this.emitDismiss}
          >
            <sl-icon slot="prefix" name="x-circle"></sl-icon>
            Dismiss
          </sl-button>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'question-answer-panel': QuestionAnswerPanel;
  }
}
