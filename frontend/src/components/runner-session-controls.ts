/**
 * Steering for a runner-hosted session, under its live thread (#1483).
 *
 * Reads the session's control mode once per session and hands the existing
 * operator note composer the runner session to send turns to when the
 * runner is online. Offline or ended, the same box sends a note, and a line
 * above it says which of the two the operator is about to do and why.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';

import {
  getRuntimeSessionControl,
  runnerTurnSessionId,
  type RuntimeSessionControl,
} from '../api';
import './operator-note-composer';

@customElement('runner-session-controls')
export class RunnerSessionControls extends LitElement {
  /** The runtime session id of the runner-hosted session. */
  @property({ type: String, attribute: 'session-id' })
  sessionId = '';

  /**
   * Changes whenever the host saw the session change (activity, status, end).
   * A change re-reads the control mode, debounced.
   */
  @property({ type: String })
  revision = '';

  @state()
  private control: RuntimeSessionControl | null = null;

  private refreshTimer: ReturnType<typeof setTimeout> | null = null;

  /** Debounce window for re-reading the control mode. */
  static refreshDelayMs = 300;

  @state()
  private controlError = false;

  private loadSeq = 0;

  static styles = css`
    :host {
      display: block;
      margin-top: var(--sl-spacing-medium);
    }

    .mode,
    .attach-label {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      margin: 0 0 var(--sl-spacing-x-small);
    }

    .attach {
      margin-top: var(--sl-spacing-small);
    }

    /* DESIGN.md Surfaces: a copyable command block is the one filled box
       allowed inside a card, on the page rung. */
    .command-row {
      align-items: center;
      background: var(--console-page, var(--sl-color-neutral-50));
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      gap: var(--sl-spacing-x-small);
      padding: var(--sl-spacing-2x-small) var(--sl-spacing-x-small);
    }

    code {
      flex: 1;
      font-family: var(--sl-font-mono);
      font-size: var(--console-text-meta, 13px);
      min-width: 0;
      overflow-wrap: anywhere;
      user-select: all;
    }

    @media (max-width: 720px) {
      sl-copy-button::part(button) {
        min-height: 44px;
        min-width: 44px;
      }
    }
  `;

  updated(changed: Map<string, unknown>): void {
    if (changed.has('sessionId')) {
      this.cancelRefresh();
      void this.loadControl(true);
    } else if (
      changed.has('revision') &&
      changed.get('revision') !== undefined
    ) {
      this.refreshControl();
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.cancelRefresh();
  }

  /**
   * Re-read the mode soon: the runner may have gone offline or come back.
   * A burst of calls inside the debounce window costs one request.
   */
  refreshControl(): void {
    this.cancelRefresh();
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null;
      void this.loadControl(false);
    }, RunnerSessionControls.refreshDelayMs);
  }

  private cancelRefresh(): void {
    if (this.refreshTimer !== null) clearTimeout(this.refreshTimer);
    this.refreshTimer = null;
  }

  /**
   * Read the mode. A fresh session starts from "checking"; a refresh keeps
   * the current hint until the answer arrives.
   */
  async loadControl(reset = true): Promise<void> {
    const seq = ++this.loadSeq;
    if (reset) {
      this.control = null;
      this.controlError = false;
    }
    if (!this.sessionId) return;
    try {
      const control = await getRuntimeSessionControl(this.sessionId);
      if (seq === this.loadSeq) {
        this.control = control;
        this.controlError = false;
      }
    } catch {
      if (seq === this.loadSeq) this.controlError = true;
    }
  }

  /** The validated runner session to send turns to, or null for notes. */
  get commandTarget(): string | null {
    const control = this.control;
    // A failed refresh outranks the cached answer: until a read succeeds the
    // box sends notes, which always arrive.
    if (this.controlError) return null;
    if (!control || control.mode !== 'command') return null;
    if (control.agent_kind !== 'runner_session') return null;
    return runnerTurnSessionId(control.send_path);
  }

  private modeLine(): string {
    if (this.commandTarget) {
      if (this.control?.reason_code === 'runner_away') {
        // #1509: the session waits for the host instead of failing.
        return 'Your laptop is asleep or offline: your message is held and starts the next turn when it reconnects.';
      }
      return "Runner online: your message starts the agent's next turn.";
    }
    if (this.controlError) {
      return 'Could not read the session controls: your message is sent as a note.';
    }
    if (!this.control) return 'Checking the runner…';
    switch (this.control.reason_code) {
      case 'control_offline':
        return 'Runner offline: your message is queued as a note.';
      case 'session_ended':
        return 'Session ended: your message is kept as a note.';
      default:
        return 'Your message is sent as a note.';
    }
  }

  render() {
    if (!this.sessionId) return nothing;
    const command = `preloop sessions attach ${this.sessionId}`;
    return html`
      <p class="mode" data-testid="control-mode-hint" role="status">
        ${this.modeLine()}
      </p>
      <operator-note-composer
        session-id=${this.sessionId}
        .runnerSessionId=${this.commandTarget}
        @runner-turn-failed=${() => this.refreshControl()}
      ></operator-note-composer>
      <div class="attach" data-testid="attach-hint">
        <p class="attach-label">Or attach from a terminal:</p>
        <div class="command-row">
          <code>${command}</code>
          <sl-copy-button .value=${command}></sl-copy-button>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'runner-session-controls': RunnerSessionControls;
  }
}
