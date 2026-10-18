/**
 * The Notes tab on an agent page: a compact box to write one, and the notes
 * already sent, newest first, one line each.
 *
 * A busy agent collects dozens of long notes. Rendered in full above the tabs
 * they pushed the agent's sessions off the first screen, so here each note is
 * a one-line preview that expands on click, the first page is three notes,
 * and older ones load on demand.
 *
 * An agent can run several sessions at once, so a note names its session:
 * the box asks which one gets it, every row links to the session it went to,
 * and the list narrows to one session on request.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';

import {
  cancelOperatorNote,
  getAccountRuntimeSessions,
  listAllOperatorNoteSessionTargets,
  listOperatorNotesPage,
} from '../api';
import type { OperatorNote, OperatorNoteSessionTarget } from '../types';
import {
  noteAuthorIcon,
  noteAuthorKind,
  noteAuthorName,
} from '../utils/note-author';
import {
  type NoteSessionRow,
  noteSessionLabel,
  sessionLogHref,
  sessionName,
  sessionOptionLabel,
  shortSessionId,
} from '../utils/note-session';
import { noteStateLabel } from './operator-note-composer';
import './operator-note-composer';

/** Notes shown before the operator asks for more. */
export const NOTES_FIRST_PAGE = 3;
/** Notes per "Show older" click. */
export const NOTES_OLDER_PAGE = 20;
/** Sessions read to name the notes on them; older ones fall back to an id. */
const SESSIONS_FOR_LABELS = 100;

/** Compose target: the agent's current or next session. */
export const TARGET_AUTO = 'auto';
/** Compose target: a session the agent has not started yet. */
export const TARGET_NEXT = 'next';
/** List filter: every session. */
const FILTER_ALL = 'all';

@customElement('agent-notes-tab')
export class AgentNotesTab extends LitElement {
  @property({ type: String, attribute: 'agent-id' })
  agentId = '';

  /** False for an agent no note can reach: the list stays, the box goes. */
  @property({ type: Boolean, attribute: 'can-send' })
  canSend = false;

  /** First page, when the host already read it (for the tab's count). */
  @property({ attribute: false })
  initial: { notes: OperatorNote[]; total: number } | null = null;

  @state() private notes: OperatorNote[] = [];
  @state() private total = 0;
  @state() private loading = false;
  @state() private error: string | null = null;
  @state() private expanded = new Set<string>();
  /** Live sessions, the ones a note can be aimed at now. */
  @state() private liveSessions: OperatorNoteSessionTarget[] = [];
  /** Every session read, by id, to name the session on each note. */
  @state() private sessionsById = new Map<string, NoteSessionRow>();
  /**
   * `auto`, `next`, a live session id, or '' while the operator must pick.
   * Starts blocked: until the live sessions are known, "current or next"
   * could land on any of several parallel conversations.
   */
  @state() private target = '';
  /** Whether the live sessions are known yet, or could not be read. */
  @state() private targetsState: 'loading' | 'ready' | 'failed' = 'loading';
  /** `all`, or the session id the list is narrowed to. */
  @state() private sessionFilter = FILTER_ALL;

  private loadedFor = '';
  /** The agent whose live sessions were read, apart from its notes. */
  private sessionsLoadedFor = '';
  /**
   * Notes across every session: the tab's count. `total` narrows with the
   * session filter, and the host's badge must not.
   */
  private agentTotal = 0;

  static styles = css`
    :host {
      display: block;
    }

    .composer {
      margin-bottom: var(--sl-spacing-medium);
    }

    .target {
      margin-bottom: var(--sl-spacing-x-small);
      max-width: 32rem;
    }

    .target-hint {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      margin-top: var(--sl-spacing-2x-small);
    }

    .filter {
      margin-bottom: var(--sl-spacing-x-small);
      max-width: 20rem;
    }

    .line {
      align-items: baseline;
      display: grid;
      gap: var(--sl-spacing-small);
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .session {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      max-width: 14rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    a.session {
      color: var(--sl-color-primary-700);
      text-decoration: none;
    }

    a.session:hover,
    a.session:focus-visible {
      text-decoration: underline;
    }

    /* Phone: the session drops under the preview instead of squeezing it. */
    @media (max-width: 720px) {
      .line {
        grid-template-columns: minmax(0, 1fr);
      }
    }

    ul {
      list-style: none;
      margin: 0;
      padding: 0;
    }

    li {
      border-top: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
    }

    .row {
      align-items: baseline;
      background: none;
      border: 0;
      color: inherit;
      cursor: pointer;
      display: grid;
      font: inherit;
      gap: var(--sl-spacing-small);
      grid-template-columns: auto minmax(0, 1fr) auto;
      padding: var(--sl-spacing-x-small) 0;
      text-align: left;
      width: 100%;
    }

    .row:focus-visible {
      outline: 2px solid var(--sl-color-primary-600);
      outline-offset: 2px;
    }

    .author {
      align-items: center;
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      display: inline-flex;
      font-size: var(--console-text-meta, 13px);
      font-weight: 600;
      gap: var(--sl-spacing-3x-small);
      white-space: nowrap;
    }

    .preview {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .meta {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      white-space: nowrap;
    }

    .state.pending {
      color: var(--sl-color-warning-700);
    }

    .state.delivered,
    .state.acknowledged {
      color: var(--sl-color-success-700);
    }

    .state.undeliverable,
    .state.failed {
      color: var(--sl-color-danger-700, #b91c1c);
    }

    .full {
      padding: 0 0 var(--sl-spacing-small);
    }

    .full .text {
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }

    .full .times {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      margin-top: var(--sl-spacing-2x-small);
    }

    .more {
      align-items: center;
      display: flex;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-small);
    }

    .empty,
    .error {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      padding: var(--sl-spacing-x-small) 0;
    }

    .error {
      color: var(--sl-color-danger-600);
    }
  `;

  protected willUpdate(changed: Map<string, unknown>): void {
    // Seed once per agent. A later page object from the host (for example
    // its count after a send) must not replace notes this tab added, paged
    // or expanded itself.
    if (
      changed.has('initial') &&
      this.initial &&
      this.loadedFor !== this.agentId
    ) {
      this.notes = this.initial.notes;
      this.total = this.initial.total;
      this.agentTotal = this.initial.total;
      this.loadedFor = this.agentId;
    }
  }

  protected updated(changed: Map<string, unknown>): void {
    if (
      changed.has('agentId') &&
      this.agentId &&
      this.loadedFor !== this.agentId
    ) {
      void this.load(true);
    }
    // Apart from the notes: a page the host already read must not skip it.
    if (
      (changed.has('agentId') || changed.has('canSend')) &&
      this.agentId &&
      this.canSend &&
      this.sessionsLoadedFor !== this.agentId
    ) {
      void this.loadSessions();
    }
  }

  /** Focus the box, for the header's "Note" action. */
  focusComposer(): void {
    const composer = this.renderRoot.querySelector('operator-note-composer');
    const input =
      composer?.shadowRoot?.querySelector<HTMLElement>('sl-textarea');
    input?.focus();
  }

  private async load(reset: boolean): Promise<void> {
    const agentId = this.agentId;
    if (!agentId || this.loading) return;
    this.loading = true;
    this.error = null;
    try {
      const page = await listOperatorNotesPage({
        agentId,
        runtimeSessionId:
          this.sessionFilter === FILTER_ALL ? undefined : this.sessionFilter,
        limit: reset ? NOTES_FIRST_PAGE : NOTES_OLDER_PAGE,
        offset: reset ? 0 : this.notes.length,
      });
      if (agentId !== this.agentId) return;
      this.loadedFor = agentId;
      this.notes = reset ? page.notes : [...this.notes, ...page.notes];
      this.total = page.total;
      if (this.sessionFilter === FILTER_ALL) this.agentTotal = page.total;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not load the notes';
    } finally {
      this.loading = false;
    }
  }

  /**
   * Read the agent's sessions: the live ones to aim a note at, the recent
   * ones to name the session on each note. A failed read keeps the box
   * working on the agent's current or next session, as before.
   */
  private async loadSessions(): Promise<void> {
    const agentId = this.agentId;
    if (!agentId) return;
    this.sessionsLoadedFor = agentId;
    this.targetsState = 'loading';
    const [live, recent] = await Promise.allSettled([
      listAllOperatorNoteSessionTargets(agentId),
      getAccountRuntimeSessions({
        agent: agentId,
        status: 'all',
        limit: SESSIONS_FOR_LABELS,
      }),
    ]);
    if (agentId !== this.agentId) return;
    const liveRows = live.status === 'fulfilled' ? live.value : [];
    const recentRows =
      recent.status === 'fulfilled' ? recent.value.items || [] : [];
    const byId = new Map<string, NoteSessionRow>();
    for (const row of [...recentRows, ...liveRows]) byId.set(row.id, row);
    this.sessionsById = byId;
    this.liveSessions = liveRows;
    if (live.status === 'rejected') {
      // Unknown is not "none live": keep the box blocked unless the operator
      // already chose Next session, which needs no live session.
      this.targetsState = 'failed';
      if (this.target !== TARGET_NEXT) this.target = '';
      return;
    }
    this.targetsState = 'ready';
    // An explicit choice made while the read was in flight stands.
    if (this.target === '') this.target = defaultTarget(liveRows);
  }

  private onTargetChange(value: string): void {
    this.target = value;
  }

  private onFilterChange(value: string): void {
    if (value === this.sessionFilter) return;
    this.sessionFilter = value || FILTER_ALL;
    this.expanded = new Set();
    void this.load(true);
  }

  private onSent(event: CustomEvent<OperatorNote>): void {
    const note = event.detail;
    if (!note?.note_id) return;
    this.agentTotal += 1;
    // A note for another session than the one listed belongs to that list.
    const listed =
      this.sessionFilter === FILTER_ALL ||
      note.runtime_session_id === this.sessionFilter;
    if (listed) {
      this.notes = [
        note,
        ...this.notes.filter((n) => n.note_id !== note.note_id),
      ];
      this.total += 1;
    }
    this.dispatchEvent(
      new CustomEvent('notes-count-changed', {
        detail: { total: this.agentTotal },
        bubbles: true,
        composed: true,
      })
    );
  }

  private toggle(noteId: string): void {
    const next = new Set(this.expanded);
    if (next.has(noteId)) next.delete(noteId);
    else next.add(noteId);
    this.expanded = next;
  }

  private async withdraw(note: OperatorNote): Promise<void> {
    try {
      const updated = await cancelOperatorNote(note.note_id);
      this.notes = this.notes.map((n) =>
        n.note_id === updated.note_id ? updated : n
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to withdraw the note';
    }
  }

  private renderTimes(note: OperatorNote) {
    const times: Array<[string, string | null | undefined]> = [
      ['Created', note.created_at],
      ['Claimed', note.claimed_at],
      ['Delivered', note.delivered_at],
      ['Expired', note.expired_at],
      ['Withdrawn', note.cancelled_at],
      ['Undeliverable', note.undeliverable_at],
    ];
    const shown = times
      .filter(([, at]) => at)
      .map(
        ([label, at], index) =>
          html`${index ? ' · ' : ''}${label}
            <time datetime=${at!}>${new Date(at!).toLocaleString()}</time>`
      );
    return typeof note.delivered_turn_index === 'number'
      ? [...shown, html` · at turn ${note.delivered_turn_index}`]
      : shown;
  }

  /** The session a note went to, linked to its log at that note's turn. */
  private renderSession(note: OperatorNote) {
    const label = noteSessionLabel(note, this.sessionsById);
    const id = note.runtime_session_id;
    if (!id) {
      return html`<span class="session" data-testid="note-session"
        >${label}</span
      >`;
    }
    const delivered = Boolean(note.delivered_at);
    return html`<a
      class="session"
      data-testid="note-session"
      href=${sessionLogHref(id, delivered ? note.note_id : undefined)}
      title=${delivered ? `Open ${label} at this note` : `Open ${label}`}
      >${label}</a
    >`;
  }

  private renderNote(note: OperatorNote) {
    const open = this.expanded.has(note.note_id);
    const kind = noteAuthorKind(note.author?.auth_method);
    const when = note.delivered_at || note.created_at;
    return html`<li data-testid="agent-note-${note.note_id}">
      <div class="line">
        <button
          class="row"
          type="button"
          aria-expanded=${open ? 'true' : 'false'}
          @click=${() => this.toggle(note.note_id)}
        >
          <span class="author" data-author-kind=${kind}>
            <sl-icon name=${noteAuthorIcon(note.author?.auth_method)}></sl-icon>
            ${noteAuthorName(note.author?.display)}
          </span>
          <span class="preview">${note.text}</span>
          <span class="meta">
            <span class="state ${note.state}">${noteStateLabel(note)}</span>
            ${when ? html` · ${new Date(when).toLocaleDateString()}` : nothing}
          </span>
        </button>
        ${this.renderSession(note)}
      </div>
      ${
        open
          ? html`<div class="full" data-testid="agent-note-full">
              <div class="text">${note.text}</div>
              <div class="times">
                ${this.renderTimes(note)}
                ${
                  note.state === 'pending'
                    ? html`<sl-button
                        size="small"
                        variant="text"
                        @click=${() => void this.withdraw(note)}
                        >Withdraw</sl-button
                      >`
                    : nothing
                }
              </div>
            </div>`
          : nothing
      }
    </li>`;
  }

  /**
   * Which session the next note goes to. One live session is picked for the
   * operator; with several, nothing is, because a note to the wrong one of
   * three parallel conversations is worse than a second click.
   */
  private renderTarget() {
    const live = this.liveSessions;
    return html`<div class="target">
      <sl-select
        size="small"
        label="Send to"
        data-testid="note-target"
        placeholder="Choose a session"
        .value=${this.target}
        @sl-change=${(event: Event) =>
          this.onTargetChange((event.target as HTMLSelectElement).value)}
      >
        ${
          live.length === 0 && this.targetsState === 'ready'
            ? html`<sl-option value=${TARGET_AUTO}
                >Current or next session</sl-option
              >`
            : nothing
        }
        ${live.map(
          (session) =>
            html`<sl-option value=${session.id}
              >${sessionOptionLabel(session)}</sl-option
            >`
        )}
        <sl-option value=${TARGET_NEXT}>Next session</sl-option>
      </sl-select>
      <div class="target-hint" data-testid="note-target-hint">
        ${
          this.targetsState === 'failed' && this.target !== TARGET_NEXT
            ? html`Could not load the live sessions.
                <sl-button
                  size="small"
                  variant="text"
                  data-testid="note-targets-retry"
                  @click=${() => void this.loadSessions()}
                  >Retry</sl-button
                >
                or choose Next session.`
            : this.targetsState === 'loading' && this.target === ''
              ? 'Loading live sessions.'
              : targetHint(this.target, live.length)
        }
      </div>
    </div>`;
  }

  /** Narrow the list to one session: the live ones and any a note names. */
  private renderFilter() {
    const ids = new Set<string>(this.liveSessions.map((row) => row.id));
    for (const note of this.notes) {
      if (note.runtime_session_id) ids.add(note.runtime_session_id);
    }
    if (this.sessionFilter !== FILTER_ALL) ids.add(this.sessionFilter);
    if (ids.size < 2 && this.sessionFilter === FILTER_ALL) return nothing;
    return html`<div class="filter">
      <sl-select
        size="small"
        label="Session"
        data-testid="notes-session-filter"
        .value=${this.sessionFilter}
        @sl-change=${(event: Event) =>
          this.onFilterChange((event.target as HTMLSelectElement).value)}
      >
        <sl-option value=${FILTER_ALL}>All sessions</sl-option>
        ${[...ids].map((id) => {
          const row = this.sessionsById.get(id);
          return html`<sl-option value=${id}
            >${row ? sessionName(row) : `Session ${shortSessionId(id)}`}</sl-option
          >`;
        })}
      </sl-select>
    </div>`;
  }

  render() {
    const remaining = Math.max(0, this.total - this.notes.length);
    return html`
      ${
        this.canSend
          ? html`<div class="composer">
              ${this.renderTarget()}
              <operator-note-composer
                agent-id=${this.target === '' ? '' : this.agentId}
                session-id=${isSessionTarget(this.target) ? this.target : ''}
                ?next-session=${this.target === TARGET_NEXT}
                no-history
                @operator-note-sent=${(event: CustomEvent<OperatorNote>) =>
                  this.onSent(event)}
              ></operator-note-composer>
            </div>`
          : html`<div class="empty" data-testid="notes-unsupported">
              No note can reach this agent: it sends no model traffic through
              the gateway and has no note hook. Earlier notes stay listed here.
            </div>`
      }
      ${this.error ? html`<div class="error" role="alert">${this.error}</div>` : nothing}
      ${this.renderFilter()}
      ${
        this.notes.length
          ? html`<ul data-testid="agent-notes-list">
              ${this.notes.map((note) => this.renderNote(note))}
            </ul>`
          : this.loading
            ? nothing
            : html`<div class="empty">
                ${
                  this.sessionFilter === FILTER_ALL
                    ? 'No notes yet.'
                    : 'No notes on this session.'
                }
              </div>`
      }
      ${
        remaining > 0
          ? html`<div class="more">
              <sl-button
                size="small"
                ?loading=${this.loading}
                data-testid="notes-older"
                @click=${() => void this.load(false)}
                >Show older notes (${remaining})</sl-button
              >
            </div>`
          : nothing
      }
    `;
  }
}

/** One live session is the obvious target; none or several are not. */
export function defaultTarget(live: readonly { id: string }[]): string {
  if (live.length === 1) return live[0].id;
  return live.length === 0 ? TARGET_AUTO : '';
}

function isSessionTarget(target: string): boolean {
  return target !== '' && target !== TARGET_AUTO && target !== TARGET_NEXT;
}

function targetHint(target: string, liveCount: number): string {
  if (target === '') {
    return `${liveCount} sessions are live. Choose the one this note is for.`;
  }
  if (target === TARGET_NEXT) {
    return 'Held for the next session this agent starts. Live sessions will not see it.';
  }
  if (target === TARGET_AUTO) {
    return 'No session is live. The next one this agent starts gets it.';
  }
  return 'Only this session gets it, at its next turn.';
}

declare global {
  interface HTMLElementTagNameMap {
    'agent-notes-tab': AgentNotesTab;
  }
}
