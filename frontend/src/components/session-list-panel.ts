import { LitElement, css, html, unsafeCSS } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import type { ObservedSession } from '../utils/session-observer';
import { formatCost, formatNumber } from '../utils/session-observer';
import consoleStyles from '../styles/console-styles.css?inline';

@customElement('session-list-panel')
export class SessionListPanel extends LitElement {
  @property({ type: Array })
  sessions: ObservedSession[] = [];

  @property({ type: String })
  activeSessionId: string | null = null;

  @property({ type: String })
  emptyText = '';

  // The console chip recipe, so "Idle" here is the same object as "Idle" on
  // the agent header instead of a solid Shoelace badge beside it.
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        min-height: 0;
      }

      .list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
      }

      .session-card {
        appearance: none;
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        background: var(--sl-color-neutral-0);
        color: inherit;
        cursor: pointer;
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        text-align: left;
        transition:
          border-color 0.15s ease,
          background 0.15s ease,
          box-shadow 0.15s ease;
        width: 100%;
      }

      .session-card:hover,
      .session-card.active {
        background: var(--sl-color-primary-50);
        border-color: var(--sl-color-primary-500);
      }

      .session-card.active {
        box-shadow: 0 0 0 1px var(--sl-color-primary-500);
      }

      .title-row,
      .metric-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: space-between;
      }

      .title {
        color: var(--sl-color-neutral-900);
        font-weight: 600;
        overflow-wrap: anywhere;
      }

      .meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-2x-small);
        overflow-wrap: anywhere;
      }

      .metric {
        color: var(--sl-color-primary-700);
        font-size: var(--sl-font-size-small);
        font-weight: 600;
        margin-top: var(--sl-spacing-2x-small);
      }

      .empty {
        color: var(--sl-color-neutral-600);
        padding: var(--sl-spacing-large);
        text-align: center;
      }

      .waste-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-2x-small);
      }

      .waste-savings {
        color: var(--sl-color-success-700);
        font-size: var(--sl-font-size-x-small);
        font-weight: 600;
      }
    `,
  ];

  private getWasteVariant(score: number) {
    if (score >= 40) return 'danger';
    if (score >= 15) return 'warning';
    return 'neutral';
  }

  private renderWasteBadge(session: ObservedSession) {
    const score = session.optimizationWasteScore;
    if (score === null || score === undefined) return '';
    const savings = session.optimizationPotentialSavingsUsd;
    return html`
      <div class="waste-row">
        <sl-badge class="chip" variant=${this.getWasteVariant(score)} pill>
          Waste ${score}%
        </sl-badge>
        ${
          savings && savings > 0
            ? html`<span class="waste-savings">
                save up to ${formatCost(savings)}
              </span>`
            : ''
        }
      </div>
    `;
  }

  /**
   * A state is a tint, and idle is a state, not an outcome (DESIGN.md
   * "Chips"). The idle case used to return `primary`, so one page carried
   * two dialects for one word: a soft neutral chip in the agent header and a
   * solid blue badge in the session list beside it.
   */
  private getVariant(session: ObservedSession) {
    if (session.status === 'active_now') return 'success';
    if (session.failedRequests > 0) return 'warning';
    return 'neutral';
  }

  private getLabel(session: ObservedSession): string {
    if (session.status === 'active_now') return 'Active now';
    if (session.status === 'ended') return 'Ended';
    if (session.status === 'recently_active') return 'Recently active';
    return 'Idle';
  }

  private formatDate(value: string | null): string {
    if (!value) return 'No activity yet';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  private selectSession(session: ObservedSession): void {
    this.dispatchEvent(
      new CustomEvent('session-selected', {
        detail: { sessionId: session.id },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    if (!this.sessions.length) {
      return html`<div class="empty">
        ${this.emptyText || 'No sessions recorded for this scope.'}
      </div>`;
    }

    return html`
      <div class="list">
        ${repeat(
          this.sessions,
          (session) => session.id,
          (session) => html`
            <button
              class="session-card ${
                this.activeSessionId === session.id ? 'active' : ''
              }"
              @click=${() => this.selectSession(session)}
            >
              <div class="title-row">
                <div class="title">${session.title}</div>
                <sl-badge class="chip" variant=${this.getVariant(session)} pill>
                  ${this.getLabel(session)}
                </sl-badge>
              </div>
              ${
                session.subtitle
                  ? html`<div class="meta">${session.subtitle}</div>`
                  : ''
              }
              <div class="meta">
                Last activity ${this.formatDate(session.lastActivityAt)}
              </div>
              <div class="metric-row">
                <div class="metric">
                  ${formatNumber(session.totalRequests)} requests
                </div>
                <div class="metric">
                  ${formatNumber(session.tokenUsage.total_tokens)} tokens ·
                  ${formatCost(session.estimatedCost)}
                </div>
              </div>
              ${this.renderWasteBadge(session)}
            </button>
          `
        )}
      </div>
    `;
  }
}
