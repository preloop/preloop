import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import { parseUTCDate } from '../utils/date';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { approveRequest, declineRequest, getApprovalRequest } from '../api';
import type { ApprovalRequest, ApprovalDecisionOptions } from '../types';
import {
  isUnexpiredPendingRequest,
  normalizeApprovalRequest,
  approvalStatusLabel,
  approvalStatusVariant,
} from '../utils/approvals';
import { readablePayload } from '../utils/session-live';
import { requestNeedsForm } from '../actions/approval-actions';
import { confirmDialog } from './confirm-dialog';
import './question-answer-panel';
import type { QuestionAnswerDetail } from './question-answer-panel';
import './answer-form';
import type { AnswerForm } from './answer-form';
import './approval-rule-context-block';

/** Decisions always pass through the existing authenticated workflow API. */
@customElement('session-approval-card')
export class SessionApprovalCard extends LitElement {
  @property({ attribute: false }) request!: ApprovalRequest;
  @property({ type: Boolean }) canDecide = false;
  @property({ type: String }) author = '';
  @property({ type: Number }) now = Date.now();
  @state() private busy = false;
  @state() private confirming = false;
  @state() private comment = '';
  @state() private error = '';
  @state() private voted = false;
  @state() private forbidden = false;
  static styles = css`
    :host {
      font-family: var(--sl-font-sans, system-ui, sans-serif);
      color: var(--sl-color-neutral-900);
      display: block;
      margin: 0.5rem 0;
    }
    article {
      border: 1px solid var(--sl-color-warning-400);
      border-radius: 0.5rem;
      padding: 0.8rem;
      background: var(--sl-color-neutral-50);
      overflow-wrap: anywhere;
    }
    header,
    .actions {
      display: flex;
      gap: 0.6rem;
      flex-wrap: wrap;
      align-items: center;
    }
    header {
      font-weight: 600;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-height: 20rem;
      overflow: auto;
    }
    textarea {
      font: inherit;
      box-sizing: border-box;
      width: 100%;
      margin: 0.5rem 0;
      color: inherit;
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-300);
    }
    button,
    a {
      min-height: 2.5rem;
    }
    button:focus-visible,
    a:focus-visible {
      outline: 2px solid var(--sl-color-primary-500);
    }
    .error {
      color: var(--sl-color-danger-700);
    }
  `;
  protected willUpdate(changed: Map<string | number | symbol, unknown>): void {
    const previous = changed.get('request') as ApprovalRequest | undefined;
    if (this.request?.status !== 'pending') this.voted = false;
    if (previous && previous.id !== this.request.id) {
      this.busy = false;
      this.confirming = false;
      this.comment = '';
      this.error = '';
      this.voted = false;
      this.forbidden = false;
    }
  }
  private updatedRequest(request: ApprovalRequest): void {
    if (request.id !== this.request.id) return;
    this.dispatchEvent(
      new CustomEvent('session-approval-updated', {
        detail: request,
        bubbles: true,
        composed: true,
      })
    );
  }
  private async decide(
    approved: boolean,
    options: ApprovalDecisionOptions = {}
  ): Promise<void> {
    if (
      !this.isConnected ||
      this.busy ||
      this.voted ||
      !this.canDecide ||
      !isUnexpiredPendingRequest(this.request)
    )
      return;
    const id = this.request.id;
    this.busy = true;
    this.error = '';
    try {
      const updated = await (approved ? approveRequest : declineRequest)(id, {
        comment: this.comment,
        ...options,
      });
      if (this.request.id !== id) return;
      this.updatedRequest(updated);
      this.voted = updated.status === 'pending';
      this.comment = '';
    } catch (error) {
      if (this.request.id !== id) return;
      if ((error as { status?: number }).status === 403) this.forbidden = true;
      const detail = (
        error as { detail?: { errors?: import('../types').AnswerFieldError[] } }
      ).detail;
      const form =
        this.renderRoot.querySelector<AnswerForm>('answer-form') ||
        this.renderRoot
          .querySelector('question-answer-panel')
          ?.shadowRoot?.querySelector<AnswerForm>('answer-form');
      if (Array.isArray(detail?.errors)) form?.setServerErrors(detail.errors);
      this.error =
        error instanceof Error
          ? error.message
          : 'Decision failed. Retry after refreshing the request.';
      try {
        const updated = await getApprovalRequest(id);
        if (this.request.id === id) this.updatedRequest(updated);
      } catch {
        /* Keep the error and allow an explicit retry. */
      }
    } finally {
      if (this.request.id === id) this.busy = false;
    }
  }
  private async approve(): Promise<void> {
    const form = this.renderRoot.querySelector<AnswerForm>('answer-form');
    if (form && !form.validate()) return;
    await this.decide(true, { answer: form ? form.answer : null });
  }
  private async deny(): Promise<void> {
    if (this.busy || this.confirming) return;
    const id = this.request.id;
    this.confirming = true;
    try {
      if (
        (await confirmDialog({
          title: 'Deny this request?',
          message: `${this.request.tool_name} will not run.`,
          detail: 'The agent receives the denial and continues without it.',
          confirmLabel: 'Deny',
          variant: 'danger',
        })) &&
        this.request.id === id
      )
        await this.decide(false);
    } finally {
      this.confirming = false;
    }
  }
  render() {
    if (!this.request) return nothing;
    const r = normalizeApprovalRequest(this.request, this.now);
    const enabled =
      this.canDecide &&
      !this.forbidden &&
      isUnexpiredPendingRequest(r, this.now) &&
      !this.voted;
    return html`<article aria-label=${`Approval for ${r.tool_name}`}>
      <header>
        <span>${r.tool_name}</span
        ><sl-badge variant=${approvalStatusVariant(r.status)}
          >${approvalStatusLabel(r.status)}</sl-badge
        >
      </header>
      <p>${r.summary || ''}</p>
      <div>
        Requested
        ${parseUTCDate(r.requested_at).toLocaleString()}${r.expires_at ? ` · Expires ${parseUTCDate(r.expires_at).toLocaleString()}` : ''}
      </div>
      ${r.agent_reasoning ? html`<p>${r.agent_reasoning}</p>` : nothing}
      <approval-rule-context-block
        .ruleContext=${r.rule_context ?? null}
      ></approval-rule-context-block>
      <details>
        <summary>Arguments</summary>
        <pre>${readablePayload(JSON.stringify(r.tool_args))}</pre>
      </details>
      ${
        enabled
          ? html` ${
              r.is_question
                ? html`<question-answer-panel
                    compact
                    .question=${r.question || r.summary || r.tool_name}
                    .options=${r.question_options || []}
                    .allowFreeText=${r.allow_free_text || false}
                    .inputSchema=${r.question_schema ?? null}
                    .items=${r.question_items || []}
                    .author=${this.author}
                    .submitting=${this.busy}
                    @question-answer=${(event: CustomEvent<QuestionAnswerDetail>) => this.decide(true, { selected_option: event.detail.selectedOption, answer_text: event.detail.answerText, answer: event.detail.answer, comment: event.detail.comment ?? this.comment })}
                    @question-dismiss=${() => this.deny()}
                  ></question-answer-panel>`
                : html` ${requestNeedsForm(r) ? html`<answer-form .schema=${r.question_schema} .items=${r.question_items || []} .author=${this.author} .disabled=${this.busy}></answer-form>` : nothing}
                    <label
                      >Comment (optional)<textarea
                        aria-label="Decision comment"
                        .value=${this.comment}
                        ?disabled=${this.busy}
                        @input=${(event: Event) => (this.comment = (event.target as HTMLTextAreaElement).value)}
                      ></textarea>
                    </label>
                    <div class="actions">
                      <sl-button
                        ?disabled=${this.busy || this.confirming}
                        variant="success"
                        @click=${() => this.approve()}
                      >
                        ${this.busy ? 'Posting…' : 'Approve'}</sl-button
                      ><sl-button
                        ?disabled=${this.busy || this.confirming}
                        variant="danger"
                        outline
                        @click=${() => this.deny()}
                      >
                        Deny
                      </sl-button>
                    </div>`
            }`
          : nothing
      }
      ${this.voted ? html`<p role="status">Your vote was recorded. Waiting for the remaining approvers.</p>` : nothing}
      ${this.error ? html`<p class="error" role="alert">${this.error}</p>` : nothing}
      ${r.resolved_at ? html`<p>Resolved ${parseUTCDate(r.resolved_at).toLocaleString()} · ${r.was_bypassed ? 'Bypass' : r.decided_by_ai ? 'AI decision' : r.decided_by_human ? 'Human decision' : 'Decision attribution unavailable'}</p>` : nothing}
      ${r.approver_comment ? html`<p>${r.approver_comment}</p>` : nothing}
      <a href=${`/console/approval/${encodeURIComponent(r.id)}`}
        >Approval details</a
      >
    </article>`;
  }
}
