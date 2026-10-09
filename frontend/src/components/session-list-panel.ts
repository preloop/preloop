import { parseUTCDate } from '../utils/date';
import { LitElement, css, html, unsafeCSS } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import type { ObservedSession } from '../utils/session-observer';
import { formatCost, formatNumber } from '../utils/session-observer';
import {
  noteAuthorIcon,
  noteAuthorKind,
  noteAuthorLabel,
} from '../utils/note-author';
import {
  ARTIFACT_KIND_GROUPS,
  ARTIFACT_KIND_ICONS,
  ARTIFACT_KIND_LABELS,
  artifactKindGroup,
  type ArtifactKindGroup,
} from '../utils/session-artifacts';
import consoleStyles from '../styles/console-styles.css?inline';

/** Kind icons shown on a row before the rest fold into "+N". */
const MAX_ARTIFACT_ICONS = 3;

/**
 * Fold a row's per-kind counts into the header's kind groups, largest first.
 *
 * The groups are the ones the session header filters by (#1083), so an icon
 * here always has a filter to open.
 */
export function artifactGroupCounts(
  counts: Record<string, number> | undefined
): Array<[ArtifactKindGroup, number]> {
  const totals = new Map<ArtifactKindGroup, number>();
  for (const [kind, count] of Object.entries(counts ?? {})) {
    if (!count) continue;
    const group = artifactKindGroup(kind);
    totals.set(group, (totals.get(group) ?? 0) + count);
  }
  return Array.from(totals.entries()).sort(
    (left, right) =>
      right[1] - left[1] ||
      ARTIFACT_KIND_GROUPS.indexOf(left[0]) -
        ARTIFACT_KIND_GROUPS.indexOf(right[0])
  );
}
import './token-figures.ts';

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
        box-sizing: border-box;
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

      .session-select {
        appearance: none;
        background: transparent;
        border: 0;
        color: inherit;
        cursor: pointer;
        display: block;
        font: inherit;
        padding: 0;
        text-align: left;
        width: 100%;
      }

      .session-select:focus-visible {
        border-radius: var(--sl-border-radius-small);
        outline: 2px solid var(--sl-color-primary-500);
        outline-offset: 2px;
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

      .note-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-2x-small);
      }

      .note-author {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-x-small);
        overflow-wrap: anywhere;
      }

      /* An agent author is a different fact, so it is a different tint, not a
         differently worded neutral chip. An unknown credential stays meta
         (DESIGN.md: a state, not an outcome), never the person tint. */
      .note-author.agent {
        color: var(--sl-color-primary-700);
      }

      .note-author.unknown {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
      }

      .artifact-row {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
        margin-top: var(--sl-spacing-2x-small);
      }

      .artifact-kind {
        align-items: center;
        appearance: none;
        background: var(--sl-color-neutral-0);
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: 999px;
        color: var(--sl-color-neutral-700);
        cursor: pointer;
        display: inline-flex;
        font: inherit;
        font-size: var(--sl-font-size-x-small);
        gap: 0.2rem;
        padding: 0.05rem 0.45rem;
      }

      .artifact-kind:hover,
      .artifact-kind:focus-visible {
        border-color: var(--sl-color-primary-500);
        color: var(--sl-color-primary-700);
      }
    `,
  ];

  /**
   * "This session was steered, and here is who last steered it."
   *
   * Notes ride the list row, so this costs no request. A session nobody
   * noted renders nothing at all: an indicator that is always there stops
   * being an indicator.
   */
  private renderNoteIndicator(session: ObservedSession) {
    const count = session.noteCount;
    if (!count) return '';
    const kind = noteAuthorKind(session.latestNoteAuthorAuthMethod);
    const author = noteAuthorLabel(
      session.latestNoteAuthorDisplay,
      session.latestNoteAuthorAuthMethod
    );
    return html`
      <div
        class="note-row"
        data-testid="session-notes-${session.id}"
        data-note-author-kind=${kind}
        title="Most recent note from ${author}"
      >
        <sl-badge class="chip" variant="neutral" pill>
          <sl-icon
            name=${noteAuthorIcon(session.latestNoteAuthorAuthMethod)}
          ></sl-icon>
          ${count} note${count === 1 ? '' : 's'}
        </sl-badge>
        <span class="note-author ${kind}">Last from ${author}</span>
      </div>
    `;
  }

  /**
   * Kind icons with counts for the session's artifacts, at most three, then
   * "+N". Each icon opens the session with the header filter set to that
   * kind; "+N" opens it unfiltered. The tooltip names every stored kind. A
   * session without artifacts renders nothing.
   */
  private renderArtifactCell(session: ObservedSession) {
    const groups = artifactGroupCounts(session.artifactCounts);
    if (!groups.length) return '';
    const shown = groups.slice(0, MAX_ARTIFACT_ICONS);
    const hidden = groups.slice(MAX_ARTIFACT_ICONS);
    const tooltip = Object.entries(session.artifactCounts ?? {})
      .filter(([, count]) => count > 0)
      .map(([kind, count]) => `${kind.replace(/_/g, ' ')}: ${count}`)
      .join(', ');
    const hiddenCount = hidden.reduce((sum, [, count]) => sum + count, 0);
    return html`
      <div
        class="artifact-row"
        data-testid="session-artifacts-${session.id}"
        title="Artifacts: ${tooltip}"
      >
        ${shown.map(
          ([group, count]) =>
            html`<button
              type="button"
              class="artifact-kind"
              data-kind=${group}
              aria-label="Open ${count} ${ARTIFACT_KIND_LABELS[
                group
              ].toLowerCase()}"
              @click=${(event: Event) => {
                event.stopPropagation();
                this.selectSession(session, group);
              }}
            >
              <sl-icon name=${ARTIFACT_KIND_ICONS[group]}></sl-icon>${count}
            </button>`
        )}
        ${
          hidden.length
            ? html`<button
                type="button"
                class="artifact-kind more"
                data-kind="more"
                aria-label="Open all artifacts: ${tooltip}"
                @click=${(event: Event) => {
                  event.stopPropagation();
                  this.selectSession(session);
                }}
              >
                +${hiddenCount}
              </button>`
            : ''
        }
      </div>
    `;
  }

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
   *
   * An ended session stays neutral even if a request in it failed: warning
   * means "needs a person" (DESIGN.md), and a finished run does not.
   */
  private getVariant(session: ObservedSession) {
    if (session.status === 'active_now') return 'success';
    if (session.status === 'ended') return 'neutral';
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
    const parsed = parseUTCDate(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  /**
   * Ask the host to open a session. `artifactKind` is the header kind filter
   * to apply once it is open, set when an artifact icon was clicked.
   */
  private selectSession(
    session: ObservedSession,
    artifactKind: ArtifactKindGroup | null = null
  ): void {
    this.dispatchEvent(
      new CustomEvent('session-selected', {
        detail: { sessionId: session.id, artifactKind },
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
            <!-- The card is a plain container. Selection is the real
                 <button> below and the artifact icons are sibling buttons,
                 so assistive technology reaches both (a role="button" card
                 would flatten the icons away). A click anywhere else on the
                 card still selects, for the mouse. -->
            <div
              class="session-card ${
                this.activeSessionId === session.id ? 'active' : ''
              }"
              @click=${() => this.selectSession(session)}
            >
              <button
                type="button"
                class="session-select"
                aria-current=${
                  this.activeSessionId === session.id ? 'true' : 'false'
                }
                @click=${(event: Event) => {
                  event.stopPropagation();
                  this.selectSession(session);
                }}
              >
                <div class="title-row">
                  <div class="title">${session.title}</div>
                  <sl-badge
                    class="chip"
                    variant=${this.getVariant(session)}
                    pill
                  >
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
                  <!-- Tokens before cost: the split says whether a session is
                     expensive because it reads a lot or writes a lot. -->
                  <div class="metric">
                    <token-figures .usage=${session.tokenUsage}></token-figures>
                    · ${formatCost(session.estimatedCost)}
                  </div>
                </div>
                ${this.renderWasteBadge(session)}
                ${this.renderNoteIndicator(session)}
              </button>
              ${this.renderArtifactCell(session)}
            </div>
          `
        )}
      </div>
    `;
  }
}
