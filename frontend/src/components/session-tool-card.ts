import { parseUTCDate } from '../utils/date';
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import type { LiveToolCall, ToolCallState } from '../utils/live-session';
import { TOOL_CALL_STATE_LABELS, formatDuration } from '../utils/live-session';
import {
  readablePayload,
  toolSummary,
  type SessionTool,
} from '../utils/session-live';
import { getApprovalRepository } from '../utils/approval-identity';
import './repository-chip';

/**
 * One tool invocation, in chronological context, without opening anything.
 *
 * The header is the whole answer to "what happened": `terminal · completed ·
 * 1.2s` plus the one argument that distinguishes this call from the next one.
 * Arguments and the result are one keystroke away, and are rendered as text
 * only — nothing here evaluates, executes or injects markup from a captured
 * payload.
 *
 * The lifecycle word comes from `LiveToolCall.state` and is never inferred
 * here: `requested` means the model emitted a call and nothing observed it
 * executing, which is a different claim from `completed`.
 */
@customElement('session-tool-card')
export class SessionToolCard extends LitElement {
  @property({ attribute: false }) call: LiveToolCall | null = null;

  /** Captured tool from `sessionTools`. Used when the row is not a live call. */
  @property({ attribute: false }) tool: SessionTool | null = null;

  @state()
  private expanded = false;

  static styles = css`
    :host {
      display: block;
      align-self: stretch;
    }

    .card {
      margin: 0 var(--sl-spacing-medium, 1rem);
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      border-left: 3px solid var(--sl-color-neutral-300, #d1d5db);
      border-radius: 8px;
      background: var(--sl-color-neutral-0, #fff);
      color: var(--sl-color-neutral-800, #1f2937);
    }

    .card.requested {
      border-left-color: var(--sl-color-warning-500, #f59e0b);
    }

    .card.running {
      border-left-color: var(--sl-color-primary-500, #0ea5e9);
    }

    .card.completed {
      border-left-color: var(--sl-color-success-500, #22c55e);
    }

    .card.failed {
      border-left-color: var(--sl-color-danger-500, #ef4444);
    }

    .header {
      align-items: baseline;
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      padding: 0.45rem 0.65rem;
    }

    .name {
      font-weight: 600;
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.85rem;
      overflow-wrap: anywhere;
    }

    .name.unnamed {
      color: var(--sl-color-neutral-500, #6b7280);
      font-style: italic;
      font-family: inherit;
    }

    .state {
      color: var(--sl-color-neutral-600, #4b5563);
      font-size: 0.75rem;
    }

    .duration,
    .time {
      color: var(--sl-color-neutral-500, #6b7280);
      font-size: 0.75rem;
    }

    .summary {
      color: var(--sl-color-neutral-700, #374151);
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.78rem;
      overflow-wrap: anywhere;
      padding: 0 0.65rem 0.4rem;
      margin: 0;
    }

    .summary.clamped {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .toggle {
      background: none;
      border: none;
      color: var(--sl-color-primary-600, #2563eb);
      cursor: pointer;
      font: inherit;
      font-size: 0.75rem;
      margin-left: auto;
      padding: 0.2rem 0.65rem;
      text-decoration: underline;
    }

    .toggle:focus-visible {
      outline: 2px solid var(--sl-color-primary-500, #0ea5e9);
      outline-offset: 2px;
    }

    .body {
      border-top: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      padding: 0.5rem 0.65rem 0.65rem;
    }

    .section-label {
      color: var(--sl-color-neutral-500, #6b7280);
      display: block;
      font-size: 0.7rem;
      letter-spacing: 0.04em;
      margin: 0.4rem 0 0.2rem;
      text-transform: uppercase;
    }

    pre.payload {
      background: var(--sl-color-neutral-50, #f9fafb);
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      border-radius: 6px;
      color: var(--sl-color-neutral-800, #1f2937);
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.78rem;
      line-height: 1.45;
      margin: 0;
      max-height: 22rem;
      overflow: auto;
      overflow-wrap: anywhere;
      padding: 0.45rem 0.55rem;
      white-space: pre-wrap;
    }

    .withheld {
      color: var(--sl-color-neutral-500, #6b7280);
      font-size: 0.78rem;
      font-style: italic;
    }

    .flags {
      display: flex;
      flex-wrap: wrap;
      gap: 0.3rem;
      padding: 0 0.65rem 0.5rem;
    }

    details.captured {
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 0.5rem;
      margin: 0.5rem 0;
      padding: 0.7rem;
      background: var(--sl-color-neutral-50);
      color: var(--sl-color-neutral-900);
      font-family: var(--sl-font-sans, system-ui, sans-serif);
    }

    details.captured summary {
      cursor: pointer;
      overflow-wrap: anywhere;
    }

    details.captured summary:focus-visible {
      outline: 2px solid var(--sl-color-primary-500);
    }

    details.captured pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-height: 30rem;
      overflow: auto;
      font-size: 0.8rem;
    }

    .preview {
      font-size: 0.8rem;
      color: var(--sl-color-neutral-600);
      margin: 0.4rem 0;
    }
  `;

  private toggle(): void {
    this.expanded = !this.expanded;
  }

  private formatTime(value: string | null): string {
    if (!value) return '';
    const parsed = parseUTCDate(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString();
  }

  private renderPayload(label: string, text: string | null, withheld: boolean) {
    return html`
      <span class="section-label">${label}</span>
      ${
        text
          ? html`<pre
              class="payload"
              data-testid=${`tool-${label.toLowerCase()}`}
            >
${text}</pre>`
          : html`<p
              class="withheld"
              data-testid=${`tool-${label.toLowerCase()}-withheld`}
            >
              ${
                withheld
                  ? 'Withheld by capture policy.'
                  : 'Not captured for this call.'
              }
            </p>`
      }
    `;
  }

  render() {
    if (this.call) return this.renderCall();
    if (this.tool) return this.renderCaptured();
    return nothing;
  }

  private renderCaptured() {
    const captured = this.tool;
    if (!captured) return nothing;
    return html`<details
      class="captured"
      @toggle=${(event: Event) =>
        (this.expanded = (event.target as HTMLDetailsElement).open)}
    >
      <summary>
        ${captured.name} ·
        ${captured.phase === 'requested' ? 'Tool requested' : captured.phase}${
          captured.durationMs !== undefined
            ? ` · ${(captured.durationMs / 1000).toFixed(1)}s`
            : ''
        }
        ${
          getApprovalRepository(captured.repositoryArgs)
            ? html`<repository-chip
                .toolArgs=${captured.repositoryArgs}
              ></repository-chip>`
            : ''
        }
        <div class="preview">${toolSummary(captured)}</div>
      </summary>
      ${
        captured.redacted
          ? html`<p>Content redacted by capture policy.</p>`
          : ''
      }${captured.truncated ? html`<p>Captured content is truncated.</p>` : ''}
      ${
        this.expanded
          ? html`<h4>Arguments</h4>
              <pre>${readablePayload(captured.arguments)}</pre>
              <h4>Result</h4>
              <pre>${readablePayload(captured.result)}</pre>`
          : ''
      }
    </details>`;
  }

  private renderCall() {
    const call = this.call;
    if (!call) return nothing;
    const state: ToolCallState = call.state;
    const duration = formatDuration(call.durationMs);
    const time = this.formatTime(call.timestamp);
    const displayName = call.name || 'tool (name not captured)';
    // Redacted-but-known counts as expandable: the operator still deserves to
    // see that a payload exists and that capture policy, not a missing row, is
    // why there is nothing in it.
    const expandable = Boolean(
      call.argumentsText || call.resultText || call.redacted
    );

    return html`
      <div
        class="card ${state}"
        data-testid="session-tool-card"
        data-state=${state}
      >
        <div class="header">
          <span class="name ${call.name ? '' : 'unnamed'}">${displayName}</span>
          <span class="state">${TOOL_CALL_STATE_LABELS[state]}</span>
          ${duration ? html`<span class="duration">${duration}</span>` : nothing}
          ${
            call.serverName
              ? html`<span class="time">${call.serverName}</span>`
              : nothing
          }
          ${time ? html`<span class="time">${time}</span>` : nothing}
          ${
            expandable
              ? html`<button
                  class="toggle"
                  type="button"
                  aria-expanded=${this.expanded ? 'true' : 'false'}
                  data-testid="tool-toggle"
                  @click=${() => this.toggle()}
                >
                  ${this.expanded ? 'Hide details' : 'Arguments & result'}
                </button>`
              : nothing
          }
        </div>
        ${
          call.summary
            ? html`<p
                class="summary ${this.expanded ? '' : 'clamped'}"
                data-testid="tool-summary"
              >
                ${call.summary}
              </p>`
            : nothing
        }
        <div class="flags">
          ${
            getApprovalRepository(call.repositoryArgs)
              ? html`<repository-chip
                  .toolArgs=${call.repositoryArgs}
                ></repository-chip>`
              : nothing
          }
          ${
            call.redacted
              ? html`<sl-badge variant="warning" pill>Redacted</sl-badge>`
              : nothing
          }
          ${
            call.truncated
              ? html`<sl-badge variant="warning" pill>Truncated</sl-badge>`
              : nothing
          }
          ${
            !call.stableId
              ? html`<sl-badge
                  variant="neutral"
                  pill
                  title="No provider call id was captured, so this row cannot be matched to a retry or a parallel call"
                  >unmatched</sl-badge
                >`
              : nothing
          }
        </div>
        ${
          this.expanded && expandable
            ? html`<div class="body">
                ${this.renderPayload(
                  'Arguments',
                  call.argumentsText,
                  call.redacted
                )}
                ${this.renderPayload('Result', call.resultText, call.redacted)}
              </div>`
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-tool-card': SessionToolCard;
  }
}
