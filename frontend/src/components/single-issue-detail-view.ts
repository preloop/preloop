import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import DOMPurify from 'dompurify';
import { when } from 'lit/directives/when.js';
import { getStatusVariant, getComplianceVariant } from '../utils/verdict';
import { Issue, IssueComplianceResult } from '../types';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';

@customElement('single-issue-detail-view')
export class SingleIssueDetailView extends LitElement {
  @property({ type: Object }) issue: Issue | null = null;
  @property({ type: Object }) complianceResult: IssueComplianceResult | null =
    null;

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

    .detail-section .row {
      font-size: var(--sl-font-size-medium);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .review-section {
      margin-top: 3rem;
    }

    .issue-description {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-700);
      background-color: var(--sl-color-neutral-100);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
      white-space: pre-line;
      overflow-wrap: break-word;
      max-height: 400px;
      overflow-y: auto;
    }
    .issue-status {
      font-size: var(--sl-font-size-x-small);
      text-transform: uppercase;
    }
    .compliance-reason {
      margin-top: var(--sl-spacing-small);
    }
    .compliance-rows {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
    }
  `;

  render() {
    if (!this.issue) {
      return nothing;
    }

    return html`
      <div class="detail-section">
        <h3>
          <span> ${this.issue.title} </span>
          <sl-badge
            variant=${getStatusVariant(this.issue.status)}
            class="issue-status"
            >${this.issue.status}</sl-badge
          >
        </h3>
        ${when(
          this.issue.description,
          () =>
            html`<div class="issue-description">
              ${unsafeHTML(DOMPurify.sanitize(this.issue.description ?? ''))}
            </div>`,
          () =>
            html`<sl-alert variant="primary" open>
              <sl-icon slot="icon" name="info-circle"></sl-icon>
              No description provided.
            </sl-alert>`
        )}
      </div>

      <slot name="additional-info"></slot>

      ${when(
        this.complianceResult,
        () => html`
          <div class="review-section">
            <h3>${this.complianceResult!.name} Review</h3>
            <sl-badge
              variant=${getComplianceVariant(
                this.complianceResult!.compliance_factor
              )}
              pill
            >
              ${(this.complianceResult!.compliance_factor * 100).toFixed(0)}%
            </sl-badge>
            <div class="issue-description compliance-reason">
              ${this.complianceResult!.reason}
            </div>
          </div>
        `
      )}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'single-issue-detail-view': SingleIssueDetailView;
  }
}
