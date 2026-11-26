import { formatRelativeTime, parseUTCDate } from '../utils/date';
import { agentStateChip } from '../utils/agent-status';
import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import type { ObservedSession } from '../utils/session-observer';
import { formatCost } from '../utils/session-observer';
import { formatCount, formatCountExact } from '../utils/count-format';
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
import { totalTokensOf } from './token-figures';

@customElement('session-list-panel')
export class SessionListPanel extends LitElement {
  @property({ type: Array })
  sessions: ObservedSession[] = [];

  @property({ type: String })
  activeSessionId: string | null = null;

  @property({ type: String })
  emptyText = '';

  @state() private focusedSessionId: string | null = null;

  // The console chip recipe, so "Idle" here is the same object as "Idle" on
  // the agent header instead of a solid Shoelace badge beside it.
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        min-height: 0;
      }

      /* Compact rows (#1585): three single lines per session, so a page of
         20 fits in about two viewports instead of 50 tall cards in 30. */
      .list {
        display: flex;
        flex-direction: column;
        gap: 0;
      }

      .session-card {
        appearance: none;
        border: 0;
        border-bottom: 1px solid var(--console-hairline);
        border-left: 3px solid transparent;
        border-radius: 0;
        background: transparent;
        box-sizing: border-box;
        color: inherit;
        cursor: pointer;
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
        text-align: left;
        transition: background 0.15s ease;
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
        text-decoration: none;
        width: 100%;
      }

      .session-select:focus-visible {
        border-radius: var(--sl-border-radius-small);
        outline: 2px solid var(--sl-color-primary-500);
        outline-offset: 2px;
      }

      .session-card:hover {
        background: var(--console-hover-tint);
      }

      .session-card.active {
        border-left-color: var(--sl-color-primary-600);
      }

      .title-row,
      .metric-row {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: space-between;
      }

      .title,
      .meta {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .title {
        color: var(--sl-color-neutral-900);
        font-weight: 600;
      }

      .meta {
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-small);
      }

      /* One line, never wrapped, so no separator is left dangling at a line
         end. In a narrow column the token figures give way (their tooltip
         has the full sentence); the request count and the cost do not. */
      .metric-row {
        column-gap: var(--sl-spacing-2x-small);
        flex-wrap: nowrap;
        justify-content: flex-start;
        min-width: 0;
      }

      .metric {
        flex: 0 0 auto;
        white-space: nowrap;
      }

      .metric.tokens {
        flex: 0 1 auto;
        min-width: 0;
        overflow: hidden;
      }

      .metric {
        color: var(--sl-color-neutral-900);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }

      .empty {
        color: var(--console-meta-color);
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
        color: var(--console-meta-color);
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
        background: transparent;
        border: 0;
        border-radius: 999px;
        color: var(--sl-color-neutral-700);
        cursor: pointer;
        display: inline-flex;
        font: inherit;
        font-size: var(--sl-font-size-x-small);
        gap: 0.2rem;
        padding: 0.05rem 0.45rem;
      }

      @media (max-width: 640px) {
        .artifact-kind {
          min-height: 44px;
          min-width: 44px;
        }
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
              tabindex=${this.rowTabIndex(session.id)}
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
                tabindex=${this.rowTabIndex(session.id)}
                aria-label="Open all artifacts: ${tooltip}"
                @click=${(event: MouseEvent) => {
                  if (
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                  )
                    return;
                  event.preventDefault();
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
    const chip = this.getChip(session);
    if (chip.label === 'Idle' && session.failedRequests > 0) return 'warning';
    return chip.variant;
  }

  /**
   * The one agent state (Working, Needs approval, Your turn, Idle, Ended)
   * from the row's server `turn_state`; recency alone is not "working".
   */
  private getChip(session: ObservedSession) {
    const raw = (session.raw ?? {}) as Record<string, unknown>;
    return agentStateChip({
      turn_state: typeof raw.turn_state === 'string' ? raw.turn_state : null,
      ended_at:
        session.endedAt || (session.status === 'ended' ? 'ended' : null),
      last_activity_at: session.lastActivityAt,
    });
  }

  private getLabel(session: ObservedSession): string {
    return this.getChip(session).label;
  }

  /** Relative activity time; the absolute timestamp stays in the tooltip. */
  private formatShortDate(value: string | null): string {
    if (!value) return 'No activity yet';
    const parsed = parseUTCDate(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return formatRelativeTime(value);
  }

  /**
   * Subtitle and last activity on one line: "Gateway · m-2 · 5m ago".
   * The title spells it out ("Last activity ..."), with the full timestamp.
   */
  private metaLine(session: ObservedSession, long = false): string {
    const when = long
      ? `Last activity ${this.formatLongDate(session.lastActivityAt)}`
      : this.formatShortDate(session.lastActivityAt);
    return session.subtitle ? `${session.subtitle} · ${when}` : when;
  }

  private formatLongDate(value: string | null): string {
    if (!value) return 'none yet';
    const parsed = parseUTCDate(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
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

  private hasMeasuredRequests(session: ObservedSession): boolean {
    const raw = session.raw as Record<string, unknown> | undefined;
    return raw?.total_requests !== null && raw?.usage_source !== 'none';
  }

  /** Keep one row in the tab order, including after filtering or live refresh. */
  private rowTabIndex(id: string): number {
    const current =
      [this.focusedSessionId, this.activeSessionId].find((candidate) =>
        this.sessions.some((session) => session.id === candidate)
      ) ?? this.sessions[0]?.id;
    return id === current ? 0 : -1;
  }

  /** Native links retain the page filters but drop another session's turn. */
  private sessionHref(id: string): string {
    const url = new URL(window.location.href);
    url.pathname = '/console/runtime-sessions';
    url.searchParams.set('sessionId', id);
    url.searchParams.delete('turn');
    url.searchParams.delete('artifact');
    return `${url.pathname}${url.search}`;
  }

  /** Move focus without changing the selected session; Enter activates the link. */
  private async navigateRows(event: KeyboardEvent, id: string): Promise<void> {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey)
      return;
    const index = this.sessions.findIndex((session) => session.id === id);
    const target =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? this.sessions.length - 1
          : event.key === 'ArrowDown'
            ? Math.min(index + 1, this.sessions.length - 1)
            : event.key === 'ArrowUp'
              ? Math.max(index - 1, 0)
              : -1;
    if (target < 0) return;
    event.preventDefault();
    this.focusedSessionId = this.sessions[target].id;
    await this.updateComplete;
    this.renderRoot
      .querySelectorAll<HTMLAnchorElement>('.session-select')
      [target]?.focus();
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
            <div
              class="session-card ${
                this.activeSessionId === session.id ? 'active' : ''
              }"
              @click=${(event: MouseEvent) => {
                if (
                  event.button !== 0 ||
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                )
                  return;
                if ((event.target as HTMLElement).closest('a, button')) return;
                this.selectSession(session);
              }}
            >
              <a
                class="session-select"
                href=${this.sessionHref(session.id)}
                tabindex=${this.rowTabIndex(session.id)}
                @focus=${() => (this.focusedSessionId = session.id)}
                @keydown=${(event: KeyboardEvent) => void this.navigateRows(event, session.id)}
                aria-current=${
                  this.activeSessionId === session.id ? 'true' : 'false'
                }
                @click=${(event: MouseEvent) => {
                  if (
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                  )
                    return;
                  event.preventDefault();
                  this.selectSession(session);
                }}
              >
                <div class="title-row">
                  <div class="title" title=${session.title}>
                    ${session.title}
                  </div>
                  <sl-badge
                    class="chip"
                    variant=${this.getVariant(session)}
                    pill
                  >
                    ${this.getLabel(session)}
                  </sl-badge>
                </div>
                <!-- One line of context: what ran it and when. The full
                     text is the tooltip when the row truncates it. -->
                <div class="meta" title=${this.metaLine(session, true)}>
                  ${this.metaLine(session)}
                </div>
                <div class="metric-row">
                  ${
                    this.hasMeasuredRequests(session)
                      ? html`<div
                          class="metric"
                          title="${formatCountExact(session.totalRequests)} requests"
                        >
                          ${formatCount(session.totalRequests)} requests
                        </div>`
                      : nothing
                  }
                  <!-- Tokens before cost: the split says whether a session is
                     expensive because it reads a lot or writes a lot. The
                     cache rate is in the figures' tooltip, so the row stays
                     one line in the list column. No tokens recorded drops
                     the segment instead of leaving an empty slot. -->
                  ${
                    totalTokensOf(session.tokenUsage) > 0
                      ? html`<div class="metric tokens">
                          ${this.hasMeasuredRequests(session) ? '·' : nothing}
                          <token-figures
                            hide-cache
                            .usage=${session.tokenUsage}
                          ></token-figures>
                        </div>`
                      : nothing
                  }
                  ${
                    (session.raw as Record<string, unknown>)?.cost_is_known !==
                    false
                      ? html`<div class="metric cost">
                          ${this.hasMeasuredRequests(session) || totalTokensOf(session.tokenUsage) > 0 ? '·' : nothing}
                          ${formatCost(session.estimatedCost)} est.
                        </div>`
                      : nothing
                  }
                </div>
                ${this.renderWasteBadge(session)}
                ${this.renderNoteIndicator(session)}
              </a>
              ${this.renderArtifactCell(session)}
            </div>
          `
        )}
      </div>
    `;
  }
}
