/**
 * Chat-style session transcript: ONLY top-level user prompts and final agent
 * responses are expanded; tool calls, tool results, system/injected segments
 * and intermediate agent output are nested in collapsed, manually expandable
 * step groups. Reference UX: Claude Code web chat. When in doubt, collapse —
 * except for prompt classification, where doubt keeps the prompt visible
 * (see utils/transcript.ts).
 *
 * Presentational only: events/activity arrive as props from
 * <preloop-session-observer>; paging is requested by re-emitting the
 * observer's existing `session-events-page-requested` event.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';
import type {
  TranscriptItem,
  TranscriptMessageItem,
  TranscriptStep,
  TranscriptStepGroupItem,
} from '../utils/transcript';
import { buildConversation } from '../utils/transcript';

const MESSAGE_PREVIEW_CHARS = 2000;
const STEP_PREVIEW_CHARS = 600;

@customElement('session-chat-view')
export class SessionChatView extends LitElement {
  @property({ type: Array })
  events: FlowGatewayEvent[] = [];

  @property({ type: Array })
  activity: RuntimeSessionActivityItem[] = [];

  @property({ type: Boolean })
  loading = false;

  @property({ type: Boolean })
  hasMoreEvents = false;

  @property({ type: Boolean })
  loadingMoreEvents = false;

  /** Pin the scroll position to the newest message as events stream in. */
  @property({ type: Boolean })
  followLive = false;

  @property({ type: String })
  emptyText = 'No conversation captured for this session yet.';

  @state()
  private expandedMessageKeys = new Set<string>();

  @state()
  private expandedStepKeys = new Set<string>();

  static styles = css`
    :host {
      display: block;
      min-height: 0;
    }

    .thread {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
      padding: var(--sl-spacing-small) 0;
    }

    .empty,
    .loading {
      color: var(--sl-color-neutral-600);
      padding: var(--sl-spacing-x-large);
      text-align: center;
    }

    .coverage-note {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-left: 3px solid var(--sl-color-amber-400, #f5c518);
      border-radius: 4px;
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-x-small);
      line-height: 1.5;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .bubble {
      border-radius: var(--sl-border-radius-large);
      max-width: 88%;
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
    }

    .bubble.user {
      align-self: flex-end;
      background: var(--sl-color-primary-100);
      border: 1px solid var(--sl-color-primary-200);
    }

    .bubble.agent {
      align-self: flex-start;
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
    }

    .bubble.operator {
      align-self: flex-end;
      background: var(--sl-color-success-100, #e6f6ec);
      border: 1px solid var(--sl-color-success-200, #bfe8cd);
    }

    .bubble.failed {
      border-color: var(--sl-color-danger-300);
    }

    .bubble-meta {
      align-items: center;
      color: var(--sl-color-neutral-600);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-x-small);
      margin-bottom: var(--sl-spacing-2x-small);
    }

    .bubble-role {
      font-weight: var(--sl-font-weight-semibold);
      text-transform: capitalize;
    }

    .bubble-text {
      color: var(--sl-color-neutral-900);
      font-size: var(--sl-font-size-small);
      line-height: 1.55;
      margin: 0;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }

    .steps {
      align-self: stretch;
      border: 1px dashed var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-color-neutral-50);
      margin: 0 var(--sl-spacing-medium);
    }

    .steps::part(summary) {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .steps::part(content) {
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small)
        var(--sl-spacing-small);
    }

    .steps-summary {
      align-items: center;
      display: inline-flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
    }

    .step {
      border-left: 2px solid var(--sl-color-neutral-300);
      margin: var(--sl-spacing-x-small) 0;
      padding: var(--sl-spacing-2x-small) var(--sl-spacing-small);
    }

    .step-header {
      align-items: center;
      color: var(--sl-color-neutral-700);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-x-small);
    }

    .step-label {
      font-weight: var(--sl-font-weight-semibold);
    }

    .step-kind-tool_call .step-label,
    .step-kind-tool_result .step-label {
      color: var(--sl-color-primary-700);
    }

    .step-kind-injected .step-label {
      color: var(--sl-color-warning-700);
    }

    .step-text {
      color: var(--sl-color-neutral-700);
      font-family: var(--sl-font-mono);
      font-size: var(--sl-font-size-x-small);
      line-height: 1.5;
      margin: var(--sl-spacing-2x-small) 0 0;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }

    .divider {
      align-items: center;
      color: var(--sl-color-neutral-500);
      display: flex;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-small);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .divider::before,
    .divider::after {
      background: var(--sl-color-neutral-200);
      content: '';
      flex: 1;
      height: 1px;
    }

    .load-earlier {
      align-self: center;
    }

    .inline-toggle {
      background: none;
      border: none;
      color: var(--sl-color-primary-600);
      cursor: pointer;
      font: inherit;
      font-size: var(--sl-font-size-x-small);
      padding: 0;
      text-decoration: underline;
    }
  `;

  updated(): void {
    if (!this.followLive) return;
    const thread = this.renderRoot.querySelector('.thread');
    if (thread) thread.scrollTop = thread.scrollHeight;
  }

  private formatTime(value: string | null): string {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleTimeString();
  }

  private toggleMessage(key: string): void {
    const next = new Set(this.expandedMessageKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expandedMessageKeys = next;
  }

  private toggleStep(key: string): void {
    const next = new Set(this.expandedStepKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expandedStepKeys = next;
  }

  private requestEarlierEvents(): void {
    this.dispatchEvent(
      new CustomEvent('session-events-page-requested', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private renderClampedText(
    text: string,
    key: string,
    limit: number,
    expanded: boolean,
    className: string
  ) {
    const isLong = text.length > limit;
    const shown = isLong && !expanded ? `${text.slice(0, limit)}…` : text;
    return html`
      <pre class=${className}>${shown}</pre>
      ${
        isLong
          ? html`
              <button
                class="inline-toggle"
                type="button"
                @click=${() => this.toggleMessage(key)}
              >
                ${expanded ? 'Show less' : 'Show full message'}
              </button>
            `
          : nothing
      }
    `;
  }

  private renderMessage(item: TranscriptMessageItem) {
    const bubbleClass =
      item.kind === 'user_prompt'
        ? 'user'
        : item.kind === 'operator'
          ? 'operator'
          : 'agent';
    const displayText = item.text
      ? item.text
      : item.redacted
        ? 'Content redacted by capture policy.'
        : 'No text content captured.';
    return html`
      <div
        class="bubble ${bubbleClass} ${item.failed ? 'failed' : ''}"
        data-kind=${item.kind}
      >
        <div class="bubble-meta">
          <span class="bubble-role">
            ${
              item.kind === 'user_prompt'
                ? 'You'
                : item.kind === 'operator'
                  ? 'Operator'
                  : 'Agent'
            }
          </span>
          <span>${this.formatTime(item.timestamp)}</span>
          ${
            item.redacted
              ? html`<sl-badge variant="warning" pill>Redacted</sl-badge>`
              : nothing
          }
          ${
            item.truncated
              ? html`<sl-badge variant="warning" pill>Truncated</sl-badge>`
              : nothing
          }
          ${
            item.failed
              ? html`<sl-badge variant="danger" pill>Failed</sl-badge>`
              : nothing
          }
        </div>
        ${this.renderClampedText(
          displayText,
          item.key,
          MESSAGE_PREVIEW_CHARS,
          this.expandedMessageKeys.has(item.key),
          'bubble-text'
        )}
      </div>
    `;
  }

  private describeSteps(steps: TranscriptStep[]): string {
    const counts = new Map<string, number>();
    for (const step of steps) {
      const label =
        step.kind === 'tool_call'
          ? 'tool call'
          : step.kind === 'tool_result'
            ? 'tool result'
            : step.kind === 'injected'
              ? 'injected segment'
              : step.kind === 'system'
                ? 'system segment'
                : 'agent step';
      counts.set(label, (counts.get(label) || 0) + 1);
    }
    return Array.from(counts.entries())
      .map(([label, count]) => `${count} ${label}${count === 1 ? '' : 's'}`)
      .join(' · ');
  }

  private renderStep(step: TranscriptStep) {
    const expanded = this.expandedStepKeys.has(step.key);
    const displayText = step.text
      ? step.text
      : step.redacted
        ? 'Content redacted by capture policy.'
        : '';
    return html`
      <div class="step step-kind-${step.kind}">
        <div class="step-header">
          <span class="step-label">${step.label}</span>
          ${step.serverName ? html`<span>${step.serverName}</span>` : nothing}
          <span>${this.formatTime(step.timestamp)}</span>
          ${
            step.status
              ? html`<sl-badge
                  pill
                  variant=${
                    step.status.toLowerCase().includes('fail')
                      ? 'danger'
                      : 'neutral'
                  }
                  >${step.status}</sl-badge
                >`
              : nothing
          }
          ${
            step.kind === 'injected'
              ? html`<sl-badge variant="warning" pill>Injected</sl-badge>`
              : nothing
          }
          ${
            !step.detectionExact
              ? html`<sl-badge
                  variant="neutral"
                  pill
                  title="Classified by pattern, not by message structure"
                >
                  pattern match
                </sl-badge>`
              : nothing
          }
          ${
            displayText.length > STEP_PREVIEW_CHARS
              ? html`
                  <button
                    class="inline-toggle"
                    type="button"
                    @click=${() => this.toggleStep(step.key)}
                  >
                    ${expanded ? 'Show less' : 'Show all'}
                  </button>
                `
              : nothing
          }
        </div>
        ${
          displayText
            ? html`
                <pre class="step-text">
${
  displayText.length > STEP_PREVIEW_CHARS && !expanded
    ? `${displayText.slice(0, STEP_PREVIEW_CHARS)}…`
    : displayText
}</pre>
              `
            : nothing
        }
      </div>
    `;
  }

  private renderStepGroup(item: TranscriptStepGroupItem) {
    return html`
      <sl-details class="steps">
        <span slot="summary" class="steps-summary">
          <sl-icon name="three-dots"></sl-icon>
          ${item.steps.length} step${item.steps.length === 1 ? '' : 's'} —
          ${this.describeSteps(item.steps)}
        </span>
        ${item.steps.map((step) => this.renderStep(step))}
      </sl-details>
    `;
  }

  private renderItem(item: TranscriptItem) {
    if (item.type === 'message') return this.renderMessage(item);
    if (item.type === 'steps') return this.renderStepGroup(item);
    return html`
      <div class="divider">
        ${item.label}
        ${item.timestamp ? html`· ${this.formatTime(item.timestamp)}` : nothing}
      </div>
    `;
  }

  render() {
    if (this.loading && !this.events.length && !this.activity.length) {
      return html`
        <div class="loading">
          <sl-spinner></sl-spinner>
          <div>Loading conversation...</div>
        </div>
      `;
    }

    const { items, stats } = buildConversation(this.events, this.activity);
    if (!items.length) {
      return html`<div class="empty">${this.emptyText}</div>`;
    }

    return html`
      <div class="thread">
        ${
          this.hasMoreEvents
            ? html`
                <sl-button
                  class="load-earlier"
                  size="small"
                  ?loading=${this.loadingMoreEvents}
                  @click=${() => this.requestEarlierEvents()}
                >
                  Load earlier conversation
                </sl-button>
              `
            : nothing
        }
        ${
          stats.eventsWithoutRawBody > 0
            ? html`
                <div class="coverage-note" role="note">
                  Message structure was unavailable for
                  ${stats.eventsWithoutRawBody} of ${stats.totalEvents}
                  requests, so some tool results may appear as user prompts
                  there.
                </div>
              `
            : nothing
        }
        ${repeat(
          items,
          (item) => item.key,
          (item) => this.renderItem(item)
        )}
      </div>
    `;
  }
}
