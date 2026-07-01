import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { when } from 'lit/directives/when.js';
import {
  AIModelVerdict,
  renderVerdict,
  getStatusVariant,
} from '../utils/verdict';
import { DuplicatePair, checkAIVerdict } from '../api';

@customElement('issue-detail-view')
export class IssueDetailView extends LitElement {
  @property({ type: Object }) pair: DuplicatePair | null = null;

  @state()
  private aiVerdict: AIModelVerdict | null = null;

  @state()
  private loadingVerdict = false;

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
      justify-content: flex-end;
    }
  `;

  willUpdate(changedProperties: Map<string, unknown>) {
    if (changedProperties.has('pair') && this.pair) {
      this.fetchVerdict();
    }
  }

  async fetchVerdict() {
    if (!this.pair || this.loadingVerdict) return;
    this.loadingVerdict = true;
    this.aiVerdict = null;
    try {
      this.aiVerdict = await checkAIVerdict(
        this.pair.issue1.id,
        this.pair.issue2.id
      );
    } catch (error) {
      console.error('Failed to fetch Ai Review:', error);
    } finally {
      this.loadingVerdict = false;
    }
  }

  render() {
    if (!this.pair) {
      return nothing;
    }

    const { issue1, issue2 } = this.pair;

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
              ${unsafeHTML(issue1.description)}
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
              ${unsafeHTML(issue2.description)}
            </div>`
        )}
      </div>

      <div class="detail-section">
        <div class="review-header">
          <h3>AI Review</h3>
          ${when(
            this.aiVerdict,
            () => html` <div>${renderVerdict(this.aiVerdict)}</div> `
          )}
        </div>

        ${when(
          this.loadingVerdict,
          () => html`<sl-spinner></sl-spinner>`,
          () =>
            when(
              this.aiVerdict,
              () => html`
                <div>
                  <b class="compliance-title">Reason</b>
                  <div class="issue-description">
                    ${
                      this.aiVerdict?.reason?.trim() || 'No reasoning provided.'
                    }
                  </div>
                </div>

                ${when(
                  this.aiVerdict?.suggestion,
                  () => html`
                    <div>
                      <b class="compliance-title">Suggestion for Improvement</b>
                      <div class="issue-description">
                        ${this.aiVerdict?.suggestion?.trim()}
                      </div>
                    </div>
                  `
                )}
              `,
              () => html`<p>Could not load verdict.</p>`
            )
        )}
      </div>

      <div class="actions-container">
        <sl-button
          variant="primary"
          size="small"
          @click=${() => this.dispatchEvent(new CustomEvent('resolve'))}
          ?disabled=${this.aiVerdict?.resolution === 'resolved'}
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
