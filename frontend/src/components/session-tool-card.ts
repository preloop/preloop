import { parseUTCDate } from '../utils/date';
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import type { LiveToolCall, ToolCallState } from '../utils/live-session';
import { TOOL_CALL_STATE_LABELS, formatDuration } from '../utils/live-session';

/** Reasons the server gives for an ordinary allow, not worth a line. */
const DEFAULT_ALLOW_REASONS = new Set([
  'No central native rule requires approval',
]);
import {
  readablePayload,
  toolSummary,
  type SessionTool,
} from '../utils/session-live';
import { getApprovalRepository } from '../utils/approval-identity';
import './repository-chip';
import type { ToolRow } from '../utils/session-turns';

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

  /**
   * Terminal-style row from the turn model (`buildSessionTurns`). Takes
   * precedence over `call` and `tool`.
   */
  @property({ attribute: false }) row: ToolRow | null = null;

  @state()
  private expanded = false;

  @state()
  private showAllLines = false;

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

    .row {
      margin: 0 var(--sl-spacing-medium, 1rem);
      color: var(--sl-color-neutral-800, #1f2937);
      font-size: var(--sl-font-size-small, 0.875rem);
    }

    .row-head {
      align-items: baseline;
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }

    .mono,
    .row .name,
    .row-target,
    .row-lines,
    .diff {
      font-family: var(
        --console-font-mono,
        ui-monospace,
        SFMono-Regular,
        Menlo,
        Consolas,
        monospace
      );
    }

    .glyph {
      color: var(--sl-color-neutral-500, #6b7280);
    }

    .row.failed .glyph {
      color: var(--sl-color-danger-600, #dc2626);
    }

    .verb {
      font-weight: 600;
    }

    .row .name {
      color: var(--sl-color-neutral-600, #4b5563);
      font-size: 0.8rem;
    }

    .row-target {
      overflow-wrap: anywhere;
    }

    .row-meta {
      color: var(--sl-color-neutral-500, #6b7280);
      font-size: var(--sl-font-size-x-small, 0.75rem);
    }

    .row-detail,
    .row-lines,
    .diff {
      margin: 0.15rem 0 0 1.1rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: 0.8rem;
      color: var(--sl-color-neutral-700, #374151);
    }

    .row-lines {
      margin-top: 0.15rem;
    }

    .diff {
      border-left: 2px solid var(--sl-color-neutral-200, #e5e7eb);
      padding-left: 0.5rem;
    }

    .diff .add {
      background: var(--sl-color-success-50, #f0fdf4);
      color: var(--sl-color-success-800, #166534);
    }

    .diff .del {
      background: var(--sl-color-danger-50, #fef2f2);
      color: var(--sl-color-danger-800, #991b1b);
    }

    .diff .hunk {
      color: var(--sl-color-neutral-500, #6b7280);
    }

    .more {
      background: none;
      border: none;
      color: var(--sl-color-primary-600, #2563eb);
      cursor: pointer;
      font: inherit;
      font-size: var(--sl-font-size-x-small, 0.75rem);
      margin-left: 1.1rem;
      padding: 0;
    }

    .more:focus-visible {
      outline: 2px solid var(--sl-color-primary-500, #0ea5e9);
      outline-offset: 2px;
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
    if (this.row) return this.renderRow();
    if (this.call) return this.renderCall();
    if (this.tool) return this.renderCaptured();
    return nothing;
  }

  private renderRow() {
    const row = this.row;
    if (!row) return nothing;
    const claude = row.runtime === 'claude';
    const time = this.formatTime(row.timestamp);
    const lines = this.showAllLines
      ? (row.resultText ?? '').replace(/\s+$/, '').split('\n')
      : row.previewLines;
    const duration = formatDuration(row.durationMs);
    const head = claude
      ? html`<span class="verb mono">${row.name}</span
          ><span class="row-target">(${row.target})</span>`
      : html`<span class="verb">${row.verb}</span>
          <span class="row-target">${row.detail ? '' : row.target}</span>`;
    return html`<div
      class="row ${row.failed ? 'failed' : ''}"
      data-testid="session-tool-card"
      data-state=${row.state}
      data-runtime=${row.runtime}
      title=${time ? parseUTCDate(row.timestamp ?? '').toLocaleString() : ''}
    >
      <div class="row-head">
        <span class="glyph" aria-hidden="true">${row.glyph}</span>
        ${head}
        ${claude ? nothing : html`<span class="name">${row.name}</span>`}
        ${
          row.failed
            ? html`<sl-badge variant="danger" pill data-testid="tool-failed"
                >${row.exitCode !== null ? `Failed (exit ${row.exitCode})` : 'Failed'}</sl-badge
              >`
            : html`<span class="row-meta state"
                >${row.state === 'unknown' ? '' : row.state}</span
              >`
        }
        ${duration ? html`<span class="row-meta duration">${duration}</span>` : nothing}
        ${time ? html`<span class="row-meta time">${time}</span>` : nothing}
        ${
          getApprovalRepository(row.repositoryArgs)
            ? html`<repository-chip
                .toolArgs=${row.repositoryArgs}
              ></repository-chip>`
            : nothing
        }
        ${row.redacted ? html`<sl-badge variant="warning" pill>Redacted</sl-badge>` : nothing}
        ${row.truncated ? html`<sl-badge variant="warning" pill>Truncated</sl-badge>` : nothing}
        ${
          row.argumentsText || row.resultText
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
      ${row.detail ? html`<div class="row-detail mono">└ ${row.detail}</div>` : nothing}
      ${
        row.diff
          ? html`<div class="diff" data-testid="tool-diff">
              ${row.diff.map(
                (line) =>
                  html`<div class=${line.kind}>
                    ${
                      line.kind === 'add'
                        ? '+ '
                        : line.kind === 'del'
                          ? '- '
                          : line.kind === 'ctx'
                            ? '  '
                            : ''
                    }${line.text}
                  </div>`
              )}
            </div>`
          : nothing
      }
      ${
        lines.length && !row.diff
          ? html`<div class="row-lines" data-testid="tool-preview">
              ${lines.map(
                (line, index) =>
                  html`<div>
                    ${index === 0 ? (claude ? '⎿  ' : '└ ') : '   '}${line}
                  </div>`
              )}
            </div>`
          : nothing
      }
      ${
        row.hiddenLineCount > 0 && !row.diff
          ? html`<button
              class="more"
              type="button"
              data-testid="tool-more"
              aria-expanded=${this.showAllLines ? 'true' : 'false'}
              @click=${() => (this.showAllLines = !this.showAllLines)}
            >
              ${this.showAllLines ? 'Show fewer lines' : `+ ${row.hiddenLineCount} lines`}
            </button>`
          : nothing
      }
      ${
        this.expanded
          ? html`<div class="body">
              ${this.renderPayload('Arguments', row.argumentsText ? readablePayload(row.argumentsText) : null, row.redacted)}
              ${this.renderPayload('Result', row.resultText, row.redacted)}
            </div>`
          : nothing
      }
    </div>`;
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
    // A zero duration and an "unknown" state are absences, not facts: the
    // row read "Bash · unknown · 0.0s" for every native call (U-07).
    const duration =
      call.durationMs && call.durationMs > 0
        ? formatDuration(call.durationMs)
        : null;
    // The server's reason for an ordinary allow repeats on every native
    // call and says nothing about this one.
    const summary =
      call.summary && !DEFAULT_ALLOW_REASONS.has(call.summary.trim())
        ? call.summary
        : null;
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
          ${
            state === 'unknown'
              ? nothing
              : html`<span class="state"
                  >${TOOL_CALL_STATE_LABELS[state]}</span
                >`
          }
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
          summary
            ? html`<p
                class="summary ${this.expanded ? '' : 'clamped'}"
                data-testid="tool-summary"
              >
                ${summary}
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
