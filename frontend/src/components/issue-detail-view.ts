import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import DOMPurify from 'dompurify';
import { when } from 'lit/directives/when.js';
import {
  AIModelVerdict,
  renderVerdict,
  getStatusVariant,
} from '../utils/verdict';
import type { DuplicatePair, VerdictState } from '../types';

@customElement('issue-detail-view')
export class IssueDetailView extends LitElement {
  @property({ type: Object }) pair: DuplicatePair | null = null;

  @property({ type: Object })
  aiVerdict: AIModelVerdict | null = null;

  @property({ type: Object })
  verdictState: VerdictState | null = null;

  @property({ type: String })
  modelName = '';

  static styles = css`
    .detail-view-card {
      padding: var(--sl-spacing-large);
      background-color: var(--sl-color-neutral-0);
    }
    .detail-section {
      margin-bottom: var(--sl-spacing-large);
    }
    .detail-section:last-child {
      margin-bottom: 0;
    }
    .detail-section h3 {
      font-size: var(--sl-font-size-medium);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .detail-issue-key {
      color: var(--sl-color-neutral-600);
      font-weight: normal;
    }
    .issue-description {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-700);
      background-color: var(--sl-color-neutral-100);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
      white-space: pre-wrap;
      word-wrap: break-word;
      max-height: 200px;
      overflow-y: auto;
    }
    .issue-id-link {
      color: var(--sl-color-primary-600);
      text-decoration: none;
    }
    .issue-id-link:hover {
      text-decoration: underline;
    }
    .issue-id {
      font-weight: 400;
      margin-right: var(--sl-spacing-x-small);
    }
    .issue-status {
      font-size: var(--sl-font-size-x-small);
      text-transform: uppercase;
    }
    .review-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: var(--sl-spacing-medium);
    }
    .compliance-title {
      display: block;
      margin-top: var(--sl-spacing-medium);
      margin-bottom: var(--sl-spacing-x-small);
      font-weight: var(--sl-font-weight-semibold);
    }
    .actions-container {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: var(--sl-spacing-small);
    }
    .verdict-copy {
      margin: 0;
      font-size: var(--console-text-meta, var(--sl-font-size-small));
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }
    .verdict-copy a {
      color: var(--console-link-color, var(--sl-color-primary-600));
    }
  `;

  private _resolvedVerdict(): AIModelVerdict | null {
    if (this.aiVerdict) return this.aiVerdict;
    const verdict = this.verdictState?.verdict;
    if (!verdict) return null;
    return {
      decision: (verdict.decision as AIModelVerdict['decision']) || 'undecided',
      reason: verdict.reason,
      suggestion: verdict.suggestion,
      resolution: verdict.resolution,
    };
  }

  private _state(): VerdictState['state'] | 'done' {
    if (this.verdictState?.state) return this.verdictState.state;
    if (this.aiVerdict) return 'done';
    return 'checking';
  }

  private _retry() {
    this.dispatchEvent(
      new CustomEvent('retry-verdict', { bubbles: true, composed: true })
    );
  }

  private _renderVerdictBody() {
    const state = this._state();
    const verdict = this._resolvedVerdict();

    if (state === 'checking') {
      const label = this.modelName
        ? `Checking with ${this.modelName}`
        : 'Checking with the default model';
      return html`
        <div class="verdict-copy">
          <sl-spinner></sl-spinner>
          ${label}
        </div>
      `;
    }

    if (state === 'no_model') {
      return html`
        <p class="verdict-copy">
          No AI model configured. Set a default model under
          <a href="/console/ai-models">Models</a>.
        </p>
      `;
    }

    if (state === 'failed' || state === 'timeout') {
      return html`
        <p class="verdict-copy">
          AI review failed.
          <sl-button size="small" variant="text" @click=${this._retry}
            >Retry</sl-button
          >
        </p>
      `;
    }

    if (!verdict) {
      return html`<p class="verdict-copy">Could not load verdict.</p>`;
    }

    return html`
      <div>
        <b class="compliance-title">Reason</b>
        <div class="issue-description">
          ${verdict.reason?.trim() || 'No reasoning provided.'}
        </div>
      </div>
      ${when(
        verdict.suggestion,
        () => html`
          <div>
            <b class="compliance-title">Suggestion for Improvement</b>
            <div class="issue-description">${verdict.suggestion?.trim()}</div>
          </div>
        `
      )}
    `;
  }

  render() {
    if (!this.pair) {
      return nothing;
    }

    const { issue1, issue2 } = this.pair;
    const verdict = this._resolvedVerdict();
    const state = this._state();

    return html`
      <div class="detail-section">
        <h3>
          <span> ${issue1.title} </span>
          <sl-badge
            variant=${getStatusVariant(issue1.status)}
            class="issue-status"
            >${issue1.status}</sl-badge
          >
        </h3>
        ${when(
          issue1.description,
          () =>
            html`<div class="issue-description">
              ${unsafeHTML(DOMPurify.sanitize(issue1.description ?? ''))}
            </div>`
        )}
      </div>

      <div class="detail-section">
        <h3>
          <span> ${issue2.title} </span>
          <sl-badge
            variant=${getStatusVariant(issue2.status)}
            class="issue-status"
            >${issue2.status}</sl-badge
          >
        </h3>
        ${when(
          issue2.description,
          () =>
            html`<div class="issue-description">
              ${unsafeHTML(DOMPurify.sanitize(issue2.description ?? ''))}
            </div>`
        )}
      </div>

      <div class="detail-section">
        <div class="review-header">
          <h3>AI Review</h3>
          ${when(
            state === 'done' && verdict,
            () => html` <div>${renderVerdict(verdict)}</div> `
          )}
        </div>
        ${this._renderVerdictBody()}
      </div>

      <div class="actions-container">
        <sl-button
          variant="primary"
          size="small"
          @click=${() => this.dispatchEvent(new CustomEvent('resolve'))}
          ?disabled=${verdict?.resolution === 'resolved'}
        >
          <sl-icon slot="prefix" name="check-circle"></sl-icon>
          Resolve
        </sl-button>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'issue-detail-view': IssueDetailView;
  }
}
