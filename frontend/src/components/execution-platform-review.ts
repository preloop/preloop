import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import consoleStyles from '../styles/console-styles.css?inline';
import { publishExecutionPlatformReview } from '../api';
import {
  canPublishAgain,
  publicationSummary,
  reasonText,
  type PlatformReviewRecord,
} from '../utils/platform-review';

const ACTION_LABELS: Record<string, string> = {
  comment: 'comment',
  approve: 'approval',
  request_changes: 'change request',
};

/**
 * What Preloop posted on the pull request for one execution.
 *
 * Renders the execution's `platform_review` record: posted, skipped (for
 * example because the head moved) or failed with the reason, with links to
 * the review and the commit that carries the status. "Publish again" calls
 * `POST /flows/executions/{id}/platform-review` and is drawn only when
 * `canPublish` is set (the viewer may edit flows).
 *
 * Fires `platform-review-updated` with the new record in `detail.record`.
 */
@customElement('execution-platform-review')
export class ExecutionPlatformReview extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .head {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
      }
      dl {
        display: grid;
        grid-template-columns: max-content 1fr;
        gap: var(--sl-spacing-2x-small) var(--sl-spacing-medium);
        margin: var(--sl-spacing-small) 0;
        font-size: var(--console-text-body, 14px);
      }
      dt {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta, 13px);
      }
      dd {
        margin: 0;
      }
      a {
        color: var(--console-link-color);
      }
      code {
        font-family: var(--sl-font-mono);
      }
      .detail {
        margin: var(--sl-spacing-x-small) 0 0;
      }
    `,
  ];

  @property({ type: String }) executionId = '';
  /** The record, or null when the flow has not opted in. */
  @property({ attribute: false }) record: PlatformReviewRecord | null = null;
  /** Whether the run is finished; a publish call refuses an unfinished one. */
  @property({ type: Boolean }) finished = false;
  @property({ type: Boolean }) canPublish = false;

  @state() private publishing = false;
  @state() private error = '';

  private async publish(): Promise<void> {
    this.publishing = true;
    this.error = '';
    try {
      const response = await publishExecutionPlatformReview(this.executionId);
      this.record = response.platform_review;
      this.dispatchEvent(
        new CustomEvent('platform-review-updated', {
          detail: { record: response.platform_review },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      const code = (error as { code?: string }).code;
      this.error = code
        ? `Nothing was published: ${reasonText(code) || code}`
        : error instanceof Error
          ? error.message
          : 'Failed to publish the review';
    } finally {
      this.publishing = false;
    }
  }

  /** One tinted chip; variants are literal so the chip audit can see them. */
  private renderChip(variant: string, label: string, state: string) {
    switch (variant) {
      case 'success':
        return html`<sl-badge pill variant="success" data-state=${state}
          >${label}</sl-badge
        >`;
      case 'warning':
        return html`<sl-badge pill variant="warning" data-state=${state}
          >${label}</sl-badge
        >`;
      case 'danger':
        return html`<sl-badge pill variant="danger" data-state=${state}
          >${label}</sl-badge
        >`;
      default:
        return html`<sl-badge pill variant="neutral" data-state=${state}
          >${label}</sl-badge
        >`;
    }
  }

  private renderReview(record: PlatformReviewRecord) {
    if (record.review === 'posted') {
      const label = `Posted as a ${ACTION_LABELS[record.review_action || 'comment'] ?? record.review_action}`;
      return record.review_url
        ? html`<a
            href=${record.review_url}
            target="_blank"
            rel="noopener noreferrer"
            data-link="review"
            >${label}
            <sl-icon name="box-arrow-up-right" label="Opens GitHub"></sl-icon
          ></a>`
        : label;
    }
    if (record.review === 'skipped') {
      return html`Not posted.
        <span class="meta">${reasonText(record.review_reason)}</span>`;
    }
    return html`<span class="meta">Not posted yet</span>`;
  }

  private renderStatus(record: PlatformReviewRecord) {
    if (record.commit_status) {
      const label = `${record.status_context || 'Status'}: ${record.commit_status}`;
      return record.commit_url
        ? html`<a
            href=${record.commit_url}
            target="_blank"
            rel="noopener noreferrer"
            data-link="status"
            >${label}
            <sl-icon name="box-arrow-up-right" label="Opens GitHub"></sl-icon
          ></a>`
        : label;
    }
    if (record.status_reason) {
      return html`Not set.
        <span class="meta">${reasonText(record.status_reason)}</span>`;
    }
    return html`<span class="meta">Not set yet</span>`;
  }

  render() {
    const record = this.record;
    if (!record) return nothing;
    const summary = publicationSummary(record);
    return html`
      <section class="output-section" data-platform-review>
        <h2 class="section-title">Posted on the pull request</h2>
        <div class="head">
          ${this.renderChip(summary.variant, summary.label, record.status)}
          ${
            record.pull_request_url
              ? html`<a
                  href=${record.pull_request_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-link="pull_request"
                  >Open pull request
                  <sl-icon
                    name="box-arrow-up-right"
                    label="Opens GitHub"
                  ></sl-icon
                ></a>`
              : nothing
          }
          ${
            record.retry_scheduled
              ? html`<span class="meta">Preloop will retry on its own.</span>`
              : nothing
          }
        </div>
        ${
          summary.detail
            ? html`<p class="detail" data-reason>${summary.detail}</p>`
            : nothing
        }
        <dl>
          <dt>Review</dt>
          <dd>${this.renderReview(record)}</dd>
          <dt>Commit status</dt>
          <dd>${this.renderStatus(record)}</dd>
          ${
            record.head_sha
              ? html`<dt>Reviewed commit</dt>
                  <dd><code>${record.head_sha.slice(0, 8)}</code></dd>`
              : nothing
          }
          ${
            record.posted_at
              ? html`<dt>Posted</dt>
                  <dd>${new Date(record.posted_at).toLocaleString()}</dd>`
              : nothing
          }
          ${
            record.attempts
              ? html`<dt>Attempts</dt>
                  <dd>${record.attempts}</dd>`
              : nothing
          }
        </dl>
        ${
          this.error
            ? html`<sl-alert variant="danger" open role="alert" data-error>
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                ${this.error}
              </sl-alert>`
            : nothing
        }
        ${
          this.canPublish && this.finished && canPublishAgain(record)
            ? html`<sl-button
                size="small"
                data-action="publish"
                ?loading=${this.publishing}
                @click=${() => this.publish()}
                >Publish again</sl-button
              >`
            : nothing
        }
      </section>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'execution-platform-review': ExecutionPlatformReview;
  }
}
