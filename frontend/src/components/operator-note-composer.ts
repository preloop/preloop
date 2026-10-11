/**
 * One box for steering a running agent: type a note, press Enter, and the
 * agent reads it at its next turn boundary.
 *
 * This is deliberately not the talk composer. Talking takes a session over and
 * drives it; a note leaves the agent running and hands it one instruction from
 * a named human, which is what an unattended run needs at 2am. So the surface
 * is one textarea and a list of what was already said, with the delivery state
 * next to each line: the failure mode of every push channel is a silent drop,
 * and an operator who cannot tell whether the agent got it will send it twice.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';

import {
  cancelOperatorNote,
  listOperatorNotes,
  sendOperatorNote,
} from '../api';
import {
  noteAuthorIcon,
  noteAuthorKind,
  noteAuthorMarker,
  noteAuthorName,
} from '../utils/note-author';
import type { OperatorNote } from '../types';

/** Emitted after a note is accepted, so a page can refresh its timeline. */
export const OPERATOR_NOTE_SENT_EVENT = 'operator-note-sent';

/** Mirrors MAX_NOTE_BODY_CHARS on the server, which is the real bound. */
const MAX_NOTE_CHARS = 4096;

@customElement('operator-note-composer')
export class OperatorNoteComposer extends LitElement {
  /** Target the agent: the note rides its current or next session. */
  @property({ type: String, attribute: 'agent-id' })
  agentId: string | null = null;

  /** Target one flow execution, resolved server side to its session. */
  @property({ type: String, attribute: 'execution-id' })
  executionId: string | null = null;

  /** Target one runtime session, and only that session. */
  @property({ type: String, attribute: 'session-id' })
  sessionId: string | null = null;

  @state()
  private text = '';

  @state()
  private sending = false;

  @state()
  private error: string | null = null;

  @state()
  private notes: OperatorNote[] = [];

  /**
   * Guards against a slow list answer landing after a fresh send and wiping
   * the note the operator is watching. Every load and every send takes the
   * next number; a load that comes back stale is dropped.
   */
  private loadSeq = 0;

  static styles = css`
    :host {
      display: block;
    }

    .row {
      align-items: flex-end;
      display: flex;
      gap: var(--sl-spacing-x-small);
    }

    .row sl-textarea {
      flex: 1;
      min-width: 0;
    }

    sl-textarea::part(form-control-label) {
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      height: 1px;
      overflow: hidden;
      position: absolute;
      white-space: nowrap;
      width: 1px;
    }

    .hint,
    .error {
      font-size: var(--console-text-meta, 13px);
      margin-top: var(--sl-spacing-2x-small);
    }

    .hint {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }

    .error {
      color: var(--sl-color-danger-600);
    }

    ul {
      list-style: none;
      margin: var(--sl-spacing-small) 0 0;
      padding: 0;
    }

    li {
      border-top: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
      padding: var(--sl-spacing-x-small) 0;
    }

    .note-body {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-3x-small);
      min-width: 0;
    }

    .note-text {
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }

    /* Who wrote it, above what they wrote: once an agent can write a note,
       an unattributed line is a misleading audit surface. */
    .author {
      align-items: center;
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      display: flex;
      font-size: var(--console-text-meta, 13px);
      gap: var(--sl-spacing-3x-small);
    }

    .author-name {
      font-weight: 600;
    }

    /* The credential kind, stamped server side. An agent author is tinted,
       so the two kinds differ at a glance and not only in wording. */
    .author-mark {
      border: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      border-radius: var(--sl-border-radius-pill);
      font-size: var(--sl-font-size-x-small);
      padding: 0 var(--sl-spacing-2x-small);
    }

    .author-mark.agent {
      background: var(--sl-color-primary-50);
      border-color: var(--sl-color-primary-200);
      color: var(--sl-color-primary-700);
    }

    /* Unknown is a state, not an outcome (DESIGN.md Chips): meta ink, no
       person tint and no warning amber. The question icon carries the rest. */
    .author-mark.unknown {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }

    .state {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      flex: none;
      font-size: var(--console-text-meta, 13px);
    }

    .state.pending {
      color: var(--sl-color-warning-700);
    }

    .state.delivered,
    .state.acknowledged {
      color: var(--sl-color-success-700);
    }
  `;

  /**
   * One load per target, including the first render: the properties arrive in
   * the first ``changed`` map, so loading here and not in connectedCallback
   * avoids a duplicate request whose late answer would overwrite a note the
   * operator just sent.
   */
  updated(changed: Map<string, unknown>): void {
    if (
      changed.has('agentId') ||
      changed.has('executionId') ||
      changed.has('sessionId')
    ) {
      void this.refresh();
    }
  }

  /** Reload the recent notes for whichever target this composer names. */
  async refresh(): Promise<void> {
    if (!this.agentId && !this.executionId && !this.sessionId) {
      this.notes = [];
      return;
    }
    const seq = ++this.loadSeq;
    try {
      const notes = await listOperatorNotes({
        agentId: this.agentId || undefined,
        executionId: this.executionId || undefined,
        runtimeSessionId: this.sessionId || undefined,
        limit: 5,
      });
      if (seq === this.loadSeq) {
        this.notes = notes;
      }
    } catch {
      // A note channel that cannot list is still a note channel that can
      // send. Failing the whole panel here would take the box away too.
      this.notes = [];
    }
  }

  private targetPayload(): {
    agent_id?: string;
    execution_id?: string;
    runtime_session_id?: string;
  } {
    if (this.executionId) return { execution_id: this.executionId };
    if (this.sessionId) return { runtime_session_id: this.sessionId };
    return { agent_id: this.agentId || '' };
  }

  private async send(): Promise<void> {
    const text = this.text.trim();
    if (!text || this.sending) return;
    this.sending = true;
    this.error = null;
    try {
      const note = await sendOperatorNote({ text, ...this.targetPayload() });
      this.loadSeq += 1;
      this.text = '';
      this.notes = [note, ...this.notes].slice(0, 5);
      this.dispatchEvent(
        new CustomEvent(OPERATOR_NOTE_SENT_EVENT, {
          detail: note,
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to send the note';
    } finally {
      this.sending = false;
    }
  }

  private async cancel(note: OperatorNote): Promise<void> {
    try {
      const updated = await cancelOperatorNote(note.note_id);
      this.notes = this.notes.map((entry) =>
        entry.note_id === updated.note_id ? updated : entry
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to cancel the note';
    }
  }

  private onKeyDown(event: KeyboardEvent): void {
    // Enter sends, Shift+Enter is a newline: the same bargain as every other
    // composer in the console, so muscle memory carries over.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.send();
    }
  }

  /**
   * The author line: the name, and a marker for the credential behind it.
   *
   * The marker is not decoration. A note that changed an agent's behaviour
   * and reads as if a person sent it, when another agent sent it, is a
   * misleading record, and the credential kind is the one part of the author
   * the sender never chose.
   */
  private renderAuthor(note: OperatorNote) {
    const authMethod = note.author?.auth_method;
    const kind = noteAuthorKind(authMethod);
    return html`<span
      class="author"
      data-testid="note-author-${note.note_id}"
      data-author-kind=${kind}
    >
      <sl-icon name=${noteAuthorIcon(authMethod)}></sl-icon>
      <span class="author-name">${noteAuthorName(note.author?.display)}</span>
      <span class="author-mark ${kind}">${noteAuthorMarker(authMethod)}</span>
    </span>`;
  }

  private stateLabel(note: OperatorNote): string {
    switch (note.state) {
      case 'delivered':
        if (note.delivery_event === 'stop') {
          return 'Delivered to keep the run going';
        }
        if (note.delivery_event === 'session_start') {
          return 'Delivered at session start';
        }
        return note.delivery_channel === 'gateway'
          ? 'Delivered at a turn boundary'
          : 'Delivered to the agent';
      case 'acknowledged':
        return 'Acknowledged by the agent';
      case 'cancelled':
        return 'Withdrawn';
      case 'expired':
        return 'Expired undelivered';
      case 'failed':
        return 'Not delivered';
      case 'undeliverable':
        return 'Session ended before delivery';
      default:
        return 'Waiting for the next turn';
    }
  }

  render() {
    const disabled =
      this.sending || (!this.agentId && !this.executionId && !this.sessionId);
    return html`
      <div class="row">
        <sl-textarea
          data-testid="note-input"
          resize="auto"
          rows="2"
          maxlength=${MAX_NOTE_CHARS}
          label="Note to this agent"
          placeholder="Tell the agent something before its next turn"
          .value=${this.text}
          ?disabled=${disabled}
          @sl-input=${(event: Event) => {
            this.text = (event.target as HTMLInputElement).value;
          }}
          @keydown=${(event: KeyboardEvent) => this.onKeyDown(event)}
        ></sl-textarea>
        <sl-button
          data-testid="note-send"
          variant="primary"
          ?disabled=${disabled || !this.text.trim()}
          ?loading=${this.sending}
          @click=${() => void this.send()}
        >
          Send
        </sl-button>
      </div>
      <div class="hint">
        Delivered at the agent's next turn, with your name attached. Enter
        sends, Shift+Enter adds a line.
      </div>
      ${
        this.error
          ? html`<div class="error" data-testid="note-error">
              ${this.error}
            </div>`
          : nothing
      }
      ${
        this.notes.length
          ? html`<ul data-testid="note-list">
              ${this.notes.map(
                (note) => html`
                  <li>
                    <div class="note-body">
                      ${this.renderAuthor(note)}
                      <span class="note-text">${note.text}</span>
                    </div>
                    <span
                      class="state ${note.state}"
                      data-testid="note-state-${note.note_id}"
                    >
                      ${this.stateLabel(note)}
                      ${
                        note.state === 'pending'
                          ? html`<sl-button
                              size="small"
                              variant="text"
                              data-testid="note-cancel-${note.note_id}"
                              @click=${() => void this.cancel(note)}
                            >
                              Withdraw
                            </sl-button>`
                          : nothing
                      }
                    </span>
                  </li>
                `
              )}
            </ul>`
          : nothing
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'operator-note-composer': OperatorNoteComposer;
  }
}
