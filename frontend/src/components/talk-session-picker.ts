/**
 * The talk window's session switcher: the agent's recent sessions and a way
 * to start a new one, the web form of `/resume` and `/clear` in the CLIs.
 *
 * A talk window shows one session and never moves on its own; this is the
 * only way it changes session, and it is the operator who presses it. Light
 * DOM, so the session names stay in the page's own text and styles.
 */
import { LitElement, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';

import type { RuntimeSessionSummary } from '../types';
import { formatRelativeTime } from '../utils/date';
import { normalizeObservedSession } from '../utils/session-observer';

/** Bubbled when the operator picks a session: `{ sessionId }`. */
export const TALK_SESSION_SELECT_EVENT = 'talk-session-select';

/** Bubbled when the operator asks for a new session. */
export const TALK_SESSION_NEW_EVENT = 'talk-session-new';

/** Enough to tell concurrent sessions apart; older ones live in the console. */
const MAX_LISTED_SESSIONS = 20;

@customElement('talk-session-picker')
export class TalkSessionPicker extends LitElement {
  @property({ attribute: false })
  sessions: RuntimeSessionSummary[] = [];

  /** The session on screen, or null while a new session is being started. */
  @property({ attribute: false })
  currentSessionId: string | null = null;

  /** Hidden for runtimes that cannot start a session on request. */
  @property({ type: Boolean })
  canStartNew = true;

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  private select(sessionId: string): void {
    this.dispatchEvent(
      new CustomEvent(TALK_SESSION_SELECT_EVENT, {
        detail: { sessionId },
        bubbles: true,
        composed: true,
      })
    );
  }

  private startNew(): void {
    this.dispatchEvent(
      new CustomEvent(TALK_SESSION_NEW_EVENT, { bubbles: true, composed: true })
    );
  }

  private get ranked(): RuntimeSessionSummary[] {
    return [...this.sessions]
      .sort((left, right) => {
        const leftAt = left.last_activity_at || left.started_at || '';
        const rightAt = right.last_activity_at || right.started_at || '';
        return rightAt.localeCompare(leftAt);
      })
      .slice(0, MAX_LISTED_SESSIONS);
  }

  private renderSession(session: RuntimeSessionSummary) {
    const observed = normalizeObservedSession(session);
    const live = session.is_active_now && !session.ended_at;
    const where = session.session_reference || observed.subtitle;
    const lastAt = session.last_activity_at || session.started_at;
    const current = session.id === this.currentSessionId;
    return html`
      <sl-menu-item
        class="talk-session-option"
        type="checkbox"
        ?checked=${current}
        value=${session.id}
        data-session-id=${session.id}
        @click=${() => this.select(session.id)}
      >
        <span class="talk-session-option-title">${observed.title}</span>
        <span class="talk-session-option-meta">
          ${live ? html`<span class="talk-session-live">Live</span>` : nothing}
          ${where ? html`<span>${where}</span>` : nothing}
          ${lastAt ? html`<span>${formatRelativeTime(lastAt)}</span>` : nothing}
        </span>
      </sl-menu-item>
    `;
  }

  render() {
    const sessions = this.ranked;
    return html`
      <sl-dropdown placement="bottom-end" hoist>
        <sl-button
          slot="trigger"
          size="small"
          caret
          data-testid="talk-session-picker-trigger"
        >
          Sessions
        </sl-button>
        <sl-menu aria-label="Sessions">
          ${
            this.canStartNew
              ? html`<sl-menu-item
                    data-testid="talk-new-session"
                    @click=${() => this.startNew()}
                  >
                    <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                    New session
                  </sl-menu-item>
                  ${sessions.length ? html`<sl-divider></sl-divider>` : nothing}`
              : nothing
          }
          ${sessions.map((session) => this.renderSession(session))}
          ${
            !sessions.length && !this.canStartNew
              ? html`<sl-menu-item disabled>No sessions yet</sl-menu-item>`
              : nothing
          }
        </sl-menu>
      </sl-dropdown>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talk-session-picker': TalkSessionPicker;
  }
}
