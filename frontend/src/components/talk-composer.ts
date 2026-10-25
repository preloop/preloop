/**
 * The keyboard half of the talk window: one input bar that turns typing (or
 * dictation) into an audited Agent Control turn for a runtime session.
 *
 * The plumbing moved here from `agent-talk-composer`'s dialog when the founder
 * retired that dialog in favour of the talk window (wave 5). The semantics are
 * unchanged and deliberate:
 *
 * - **Takeover is implicit, release is explicit.** A session driven by a local
 *   TUI ignores injected turns, so Send takes the session over first when the
 *   control mode is `local`, then sends the command. Nothing releases the
 *   session automatically: an operator who typed one instruction usually means
 *   to type a second one, and handing the session back under them would drop
 *   the next message. Release is a button, next to Interrupt.
 * - **The turn is a user turn**, not a system prompt: the server records it as
 *   an operator message on the session and the runtime injects it into the
 *   agent's normal conversation.
 * - **A message goes where it can arrive.** For a session-bound window the
 *   server says, per session, whether a line is a turn (command mode) or a
 *   note (`/runtime-sessions/<id>/control`). A terminal session Preloop
 *   cannot type into gets a note, read at its next tool call, at the end of
 *   the turn, or with the next prompt typed in that terminal; the bubble says
 *   when it was read. Interrupt and Release show only in command mode.
 */
import { LitElement, css, html, nothing } from 'lit';
import type { PropertyValues } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import {
  getRuntimeSessionControl,
  sendOperatorNote,
  sendAgentControlCommand,
  sendAgentControlRelease,
  sendAgentControlTakeover,
  sendAgentControlVoiceTranscript,
  transcribeAudio,
} from '../api';
import type { RuntimeSessionControl } from '../api';
import type { ManagedAgentSummary } from '../types';
import {
  getAgentControlInstallHint,
  getAgentControlSessionMode,
  getAgentControlState,
  noteModeLine,
  noteReadPointLabel,
} from '../utils/agent-control';
import type { PendingTalkMessage } from './session-chat-view';
import './talk-command-menu';
import { TALK_COMMANDS, matchCommands } from './talk-command-menu';
import type { TalkCommand, TalkCommandName } from './talk-command-menu';

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  processLocally?: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult:
    | ((event: {
        resultIndex: number;
        results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
      }) => void)
    | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

type SpeechWindow = Window &
  typeof globalThis & {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  };

/** Emitted whenever the pending list changes, so the thread can render it. */
export const TALK_PENDING_CHANGED_EVENT = 'talk-pending-changed';

/** Emitted after a turn is accepted by the server, so the page can reload. */
export const TALK_MESSAGE_SENT_EVENT = 'talk-message-sent';

/** Asked by the ended-session guard: open this live session instead. */
export const TALK_SWITCH_SESSION_EVENT = 'talk-switch-session';

/** Asked by the ended-session guard: start a new session instead. */
export const TALK_NEW_SESSION_EVENT = 'talk-new-session';

/** Asked by `/resume`: open the session picker. */
export const TALK_OPEN_PICKER_EVENT = 'talk-open-session-picker';

/** How many prompts Up can recall per session. */
export const TALK_HISTORY_LIMIT = 50;

const HISTORY_KEY_PREFIX = 'preloop-talk-history:';

/** The prompts typed into one session, oldest first (tab-scoped storage). */
function readHistory(sessionKey: string): string[] {
  try {
    const raw = window.sessionStorage.getItem(HISTORY_KEY_PREFIX + sessionKey);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string')
      : [];
  } catch {
    return [];
  }
}

function writeHistory(sessionKey: string, items: string[]): void {
  try {
    window.sessionStorage.setItem(
      HISTORY_KEY_PREFIX + sessionKey,
      JSON.stringify(items.slice(-TALK_HISTORY_LIMIT))
    );
  } catch {
    /* Storage full or blocked: history is a convenience. */
  }
}

/** How long a note may wait on an idle session before the bubble says so. */
export const NOTE_STALE_AFTER_MS = 10 * 60 * 1000;

/** Where an operator can go when the open session has ended. */
export interface TalkSessionAlternative {
  id: string;
  subject: string;
}

@customElement('talk-composer')
export class TalkComposer extends LitElement {
  @property({ attribute: false })
  agent: ManagedAgentSummary | null = null;

  /** The session this composer talks to; null starts a new one. */
  @property({ type: String })
  sessionId: string | null = null;

  @property({ type: String })
  sourceContext = 'talk-window';

  /** The open session has ended: sending is blocked until the operator picks. */
  @property({ type: Boolean })
  sessionEnded = false;

  /** The live session offered when the open one has ended, if any. */
  @property({ attribute: false })
  liveAlternative: TalkSessionAlternative | null = null;

  /** The open session is idle (no live turn), so a note waits for a prompt. */
  @property({ type: Boolean })
  sessionIdle = false;

  /** The open session failed to load: show no control buttons at all. */
  @property({ type: Boolean })
  sessionLoadFailed = false;

  /** The server's per-session answer: turn or note, and why. */
  @state()
  private sessionControl: RuntimeSessionControl | null = null;

  /** Reading the session's control mode failed. */
  @state()
  private sessionControlFailed = false;

  /** The server answered without a mode (an older backend): agent-level mode. */
  @state()
  private sessionControlLegacy = false;

  @state()
  private endedGuardVisible = false;

  @state()
  private message = '';

  @state()
  private sending = false;

  @state()
  private listening = false;

  @state()
  private recordingFallback = false;

  @state()
  private statusMessage: string | null = null;

  @state()
  private controlError: string | null = null;

  @state()
  private pending: PendingTalkMessage[] = [];

  @state()
  private commandCopied = false;

  /** Highlighted row of the `/` menu. */
  @state()
  private menuIndex = 0;

  /** Esc closed the `/` menu for the text as it is now. */
  @state()
  private menuDismissed = false;

  /** Position in this session's history while Up/Down recall; null = live. */
  private historyCursor: number | null = null;

  @query('sl-textarea')
  private textarea!: HTMLElement & { focus: () => void };

  private lastInputMode: 'text' | 'voice_transcript' = 'text';
  private recognition: SpeechRecognitionLike | null = null;
  private recognitionStartedAt: number | null = null;
  private mediaRecorder: MediaRecorder | null = null;
  private mediaRecorderChunks: Blob[] = [];
  private fallbackTranscribing = false;
  private fallbackTranscriptionUnavailable = false;
  private copyResetTimer: number | null = null;
  private controlGeneration = 0;
  private controlRefreshTimer: number | null = null;
  private staleTimer: number | null = null;

  static styles = css`
    :host {
      display: block;
      border-top: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      background: var(--console-surface, var(--sl-color-neutral-0));
      /* The composer is the last thing above the home indicator on a phone. */
      padding: var(--sl-spacing-small) var(--sl-spacing-medium)
        calc(var(--sl-spacing-small) + env(safe-area-inset-bottom, 0px));
    }

    .control-row {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      margin-bottom: var(--sl-spacing-x-small);
    }

    .mode {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
    }

    .input-row {
      align-items: flex-end;
      display: flex;
      gap: var(--sl-spacing-x-small);
    }

    .input-row sl-textarea {
      flex: 1;
      min-width: 0;
    }

    sl-textarea::part(form-control) {
      margin: 0;
    }

    /*
     * The label says the same thing as the placeholder, and in a 520px window
     * that duplicate line eats a whole row of the thread. Keep the label in the
     * accessibility tree (Shoelace wires the label to the textarea) and take it
     * out of the picture.
     */
    sl-textarea::part(form-control-label) {
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      height: 1px;
      overflow: hidden;
      position: absolute;
      white-space: nowrap;
      width: 1px;
    }

    sl-textarea::part(textarea) {
      max-height: 30vh;
    }

    .helptext,
    .hint {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 13px);
      margin-top: var(--sl-spacing-2x-small);
    }

    .install-command {
      align-items: center;
      background: var(--console-page, var(--sl-color-neutral-50));
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      font-family: var(--sl-font-mono);
      font-size: 12px;
      gap: var(--sl-spacing-x-small);
      margin-top: var(--sl-spacing-2x-small);
      overflow-x: auto;
      padding: var(--sl-spacing-2x-small) var(--sl-spacing-x-small);
    }

    .install-command code {
      white-space: nowrap;
    }

    a {
      color: var(--console-link-color, var(--sl-color-primary-600));
    }

    .ended-guard {
      align-items: center;
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      display: flex;
      flex-wrap: wrap;
      font-size: var(--console-text-meta, 13px);
      gap: var(--sl-spacing-x-small);
      margin-top: var(--sl-spacing-2x-small);
    }

    .error {
      color: var(--sl-color-danger-700);
      font-size: var(--console-text-meta, 13px);
      margin-top: var(--sl-spacing-2x-small);
    }
  `;

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopListening();
    this.stopFallbackRecording();
    if (this.copyResetTimer !== null) window.clearTimeout(this.copyResetTimer);
    if (this.controlRefreshTimer !== null)
      window.clearTimeout(this.controlRefreshTimer);
    if (this.staleTimer !== null) window.clearInterval(this.staleTimer);
  }

  /** Ask the server how a line typed on this session is delivered. */
  private async loadSessionControl(): Promise<void> {
    const sessionId = this.sessionId;
    const generation = ++this.controlGeneration;
    if (!sessionId) {
      this.sessionControl = null;
      this.sessionControlFailed = false;
      return;
    }
    try {
      const control = await getRuntimeSessionControl(sessionId);
      if (generation !== this.controlGeneration) return;
      const known = control?.mode === 'command' || control?.mode === 'note';
      this.sessionControl = known ? control : null;
      this.sessionControlLegacy = !known;
      this.sessionControlFailed = false;
    } catch {
      if (generation !== this.controlGeneration) return;
      this.sessionControl = null;
      this.sessionControlFailed = true;
    }
  }

  /**
   * A realtime message for the open session (runtime_sessions or
   * agent_control topic). A note delivery marks its bubble read; anything
   * else may have changed the routing, so the control mode is read again.
   */
  public handleRealtime(message: {
    type?: string;
    event_type?: string;
    payload?: Record<string, unknown>;
  }): void {
    const payload = message?.payload ?? {};
    const metadata = (payload.metadata ?? {}) as Record<string, unknown>;
    if (
      metadata.kind === 'operator_note' &&
      typeof metadata.note_id === 'string'
    ) {
      this.markNoteRead(
        metadata.note_id,
        typeof metadata.hook_event === 'string' ? metadata.hook_event : null,
        typeof metadata.delivered_at === 'string'
          ? metadata.delivered_at
          : typeof payload.last_activity_at === 'string'
            ? payload.last_activity_at
            : new Date().toISOString()
      );
      return;
    }
    if (!this.sessionId) return;
    if (this.controlRefreshTimer !== null)
      window.clearTimeout(this.controlRefreshTimer);
    this.controlRefreshTimer = window.setTimeout(() => {
      this.controlRefreshTimer = null;
      void this.loadSessionControl();
    }, 500);
  }

  private markNoteRead(
    noteId: string,
    hookEvent: string | null,
    readAt: string
  ): void {
    if (!this.pending.some((item) => item.noteId === noteId)) return;
    this.setPending(
      this.pending.map((item) =>
        item.noteId === noteId
          ? {
              ...item,
              state: 'read',
              stale: false,
              readPoint: noteReadPointLabel(hookEvent),
              readAt,
            }
          : item
      )
    );
  }

  /** Flag notes that have waited too long on an idle session. */
  private checkStaleNotes(): void {
    const now = Date.now();
    let changed = false;
    const next = this.pending.map((item) => {
      const stale =
        item.state === 'waiting' &&
        this.sessionIdle &&
        now - new Date(item.at).getTime() >= NOTE_STALE_AFTER_MS;
      if (Boolean(item.stale) === stale) return item;
      changed = true;
      return { ...item, stale };
    });
    if (changed) this.setPending(next);
    const waiting = this.pending.some((item) => item.state === 'waiting');
    if (!waiting && this.staleTimer !== null) {
      window.clearInterval(this.staleTimer);
      this.staleTimer = null;
    }
  }

  private watchStaleNotes(): void {
    if (this.staleTimer !== null) return;
    this.staleTimer = window.setInterval(() => this.checkStaleNotes(), 30_000);
  }

  /** The window opens with the cursor already in the box. */
  public focusInput(): void {
    const focus = () => this.textarea?.focus();
    window.setTimeout(focus, 0);
    window.setTimeout(focus, 120);
  }

  /** Every pending turn, across sessions. */
  public get pendingMessages(): PendingTalkMessage[] {
    return this.pending;
  }

  /** The pending turns that belong to the session on screen. */
  private get visiblePending(): PendingTalkMessage[] {
    const current = this.sessionId ?? null;
    return this.pending.filter(
      (item) => (item.targetSessionId ?? null) === current
    );
  }

  private setPending(next: PendingTalkMessage[]): void {
    this.pending = next;
    this.emitPending();
  }

  private emitPending(): void {
    this.dispatchEvent(
      new CustomEvent(TALK_PENDING_CHANGED_EVENT, {
        detail: { pending: this.visiblePending },
        bubbles: true,
        composed: true,
      })
    );
  }

  willUpdate(changed: PropertyValues<this>): void {
    if (
      changed.has('sessionId') ||
      (changed.has('sessionEnded') && !this.sessionEnded)
    ) {
      this.endedGuardVisible = false;
    }
    if (changed.has('sessionId')) {
      // Up recalls this session's prompts only, from the newest again.
      this.historyCursor = null;
      // A new session has no answer yet: show no control buttons until the
      // server says how it is routed.
      this.sessionControl = null;
      this.sessionControlFailed = false;
      this.sessionControlLegacy = false;
    }
  }

  updated(changed: PropertyValues<this>): void {
    if (changed.has('sessionIdle')) this.checkStaleNotes();
    if (!changed.has('sessionId')) return;
    void this.loadSessionControl();
    // Re-emit for the new session: its pending turns, never the old one's.
    if (changed.get('sessionId') !== undefined) this.emitPending();
  }

  private getSpeechRecognitionConstructor(): SpeechRecognitionConstructor | null {
    const speechWindow = window as SpeechWindow;
    return (
      speechWindow.SpeechRecognition ??
      speechWindow.webkitSpeechRecognition ??
      null
    );
  }

  private get localSpeechRecognitionAvailable(): boolean {
    const Recognition = this.getSpeechRecognitionConstructor();
    if (!Recognition) return false;
    try {
      return 'processLocally' in new Recognition();
    } catch {
      return false;
    }
  }

  private get mediaRecorderAvailable(): boolean {
    return (
      typeof navigator !== 'undefined' &&
      Boolean(navigator.mediaDevices?.getUserMedia) &&
      typeof MediaRecorder !== 'undefined'
    );
  }

  private startListening(): void {
    const Recognition = this.getSpeechRecognitionConstructor();
    if (!Recognition || !this.localSpeechRecognitionAvailable) {
      void this.startFallbackRecording();
      return;
    }
    this.stopListening();
    const recognition = new Recognition();
    recognition.lang = navigator.language || 'en-US';
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.processLocally = true;
    recognition.onresult = (event) => {
      let finalTranscript = '';
      let interimTranscript = '';
      for (
        let index = event.resultIndex;
        index < event.results.length;
        index += 1
      ) {
        const result = event.results[index];
        const transcript = result[0]?.transcript?.trim() ?? '';
        if (!transcript) continue;
        if (result.isFinal) {
          finalTranscript = `${finalTranscript} ${transcript}`.trim();
        } else {
          interimTranscript = `${interimTranscript} ${transcript}`.trim();
        }
      }
      const transcript = finalTranscript || interimTranscript;
      if (transcript) {
        this.message = transcript;
        this.lastInputMode = 'voice_transcript';
        this.statusMessage = finalTranscript
          ? 'Voice captured. Review or send it.'
          : 'Listening...';
      }
    };
    recognition.onerror = (event) => {
      this.listening = false;
      this.recognition = null;
      if (this.mediaRecorderAvailable) {
        this.statusMessage = 'Recording for server transcription...';
        void this.startFallbackRecording();
        return;
      }
      this.controlError = event.error
        ? `Speech recognition failed: ${event.error}`
        : 'Speech recognition failed.';
    };
    recognition.onend = () => {
      this.listening = false;
      this.recognition = null;
    };
    try {
      recognition.start();
      this.recognition = recognition;
      this.recognitionStartedAt = Date.now();
      this.listening = true;
      this.controlError = null;
      this.statusMessage = 'Listening...';
    } catch (error) {
      this.controlError =
        error instanceof Error ? error.message : 'Unable to start microphone.';
      this.recognition = null;
      this.listening = false;
    }
  }

  private stopListening(): void {
    if (!this.recognition) {
      this.listening = false;
      return;
    }
    try {
      this.recognition.stop();
    } catch {
      this.recognition.abort();
    }
    this.recognition = null;
    this.listening = false;
  }

  private async startFallbackRecording(): Promise<void> {
    if (!this.mediaRecorderAvailable) {
      this.controlError =
        'Microphone capture is not available in this browser. Type your message instead.';
      return;
    }
    this.stopFallbackRecording();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.mediaRecorderChunks = [];
      this.fallbackTranscriptionUnavailable = false;
      const recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) this.mediaRecorderChunks.push(event.data);
      };
      recorder.onstop = () => {
        for (const track of stream.getTracks()) track.stop();
        void this.transcribeFallbackRecording();
      };
      recorder.start();
      this.mediaRecorder = recorder;
      this.recordingFallback = true;
      this.recognitionStartedAt = Date.now();
      this.statusMessage = 'Recording. Stop to transcribe.';
      this.controlError = null;
    } catch (error) {
      this.controlError =
        error instanceof Error ? error.message : 'Unable to start microphone.';
      this.recordingFallback = false;
      this.mediaRecorder = null;
    }
  }

  private stopFallbackRecording(): void {
    if (!this.mediaRecorder) {
      this.recordingFallback = false;
      return;
    }
    if (this.mediaRecorder.state !== 'inactive') this.mediaRecorder.stop();
    this.mediaRecorder = null;
    this.recordingFallback = false;
  }

  private async transcribeFallbackRecording(): Promise<void> {
    if (
      this.fallbackTranscribing ||
      this.fallbackTranscriptionUnavailable ||
      this.mediaRecorderChunks.length === 0
    ) {
      return;
    }
    this.fallbackTranscribing = true;
    this.statusMessage = 'Transcribing audio...';
    try {
      const audio = new Blob(this.mediaRecorderChunks, {
        type: this.mediaRecorderChunks[0]?.type || 'audio/webm',
      });
      const response = await transcribeAudio(audio, {
        filename: 'agent-talk.webm',
      });
      this.message = response.text;
      this.lastInputMode = 'voice_transcript';
      this.statusMessage = 'Voice captured. Review or send it.';
    } catch (error) {
      this.fallbackTranscriptionUnavailable = true;
      this.controlError =
        error instanceof Error
          ? error.message
          : 'Failed to transcribe recorded audio.';
      this.statusMessage = null;
    } finally {
      this.fallbackTranscribing = false;
      this.mediaRecorderChunks = [];
    }
  }

  private get controlMode(): 'local' | 'remote' | 'queued' | 'offline' {
    return getAgentControlSessionMode(this.agent);
  }

  /** The open session takes notes, not turns (server's per-session answer). */
  private get noteMode(): boolean {
    return Boolean(this.sessionId) && this.sessionControl?.mode === 'note';
  }

  /**
   * Whether Interrupt, Release and "You have this session" may show: only
   * when the server says this session takes commands. Before that answer
   * arrives, and when it failed, the window shows no control buttons.
   */
  private get sessionDrivable(): boolean {
    if (!this.sessionId || this.sessionControlLegacy) return true;
    return this.sessionControl?.mode === 'command';
  }

  private get startsNewSession(): boolean {
    return !this.sessionId && this.agent?.supports_new_session !== false;
  }

  private async takeOverSession(
    targetSessionId: string | null,
    startNewSession: boolean
  ): Promise<void> {
    if (!this.agent) return;
    await sendAgentControlTakeover(this.agent.id, {
      target_session_id: targetSessionId,
      start_new_session: startNewSession,
    });
  }

  private async releaseSession(): Promise<void> {
    if (!this.agent) return;
    this.controlError = null;
    try {
      await sendAgentControlRelease(this.agent.id, {
        target_session_id: this.sessionId,
      });
      this.statusMessage = 'Released. The local session can resume.';
    } catch (error) {
      this.controlError =
        error instanceof Error ? error.message : 'Release failed';
    }
  }

  private async interruptSession(): Promise<void> {
    if (!this.agent) return;
    this.controlError = null;
    try {
      await sendAgentControlCommand(this.agent.id, {
        message: 'interrupt',
        interrupt: true,
        target_session_id: this.sessionId,
        start_new_session: false,
        metadata: {
          source: 'preloop_console',
          requested_from: this.sourceContext,
        },
      });
      this.statusMessage = 'Interrupt sent.';
    } catch (error) {
      this.controlError =
        error instanceof Error ? error.message : 'Interrupt failed';
    }
  }

  /** The control row shows (and Interrupt/Release may appear in it). */
  private get controlRowVisible(): boolean {
    return (
      getAgentControlState(this.agent).enabled &&
      !this.sessionLoadFailed &&
      !(this.sessionId && this.sessionControlFailed)
    );
  }

  /** Interrupt is offered: the runtime supports it and Preloop drives this session. */
  private get canInterrupt(): boolean {
    return (
      this.controlRowVisible &&
      !this.noteMode &&
      this.sessionDrivable &&
      Boolean(this.agent?.supports_interrupt) &&
      this.controlMode === 'remote'
    );
  }

  private get canRelease(): boolean {
    return (
      this.controlRowVisible &&
      !this.noteMode &&
      this.sessionDrivable &&
      this.controlMode === 'remote' &&
      Boolean(this.agent?.control_capabilities?.includes('release'))
    );
  }

  /** Commands this window can run right now. */
  private get availableCommands(): TalkCommand[] {
    return TALK_COMMANDS.filter((command) => {
      if (command.name === 'interrupt') return this.canInterrupt;
      if (command.name === 'release') return this.canRelease;
      if (command.name === 'new')
        return this.agent?.supports_new_session !== false;
      return true;
    });
  }

  /** The `/` menu rows for the text in the box, empty when it is closed. */
  private get menuCommands(): TalkCommand[] {
    if (this.menuDismissed || !getAgentControlState(this.agent).enabled)
      return [];
    return matchCommands(this.message, this.availableCommands);
  }

  /** The words of the line above the input, also what `/status` says. */
  private get modeLine(): string {
    if (this.noteMode)
      return noteModeLine(
        this.sessionControl?.reason_code,
        this.sessionControl?.reason,
        this.sessionIdle
      );
    if (this.controlMode === 'remote') return 'You have this session';
    if (this.controlMode === 'local')
      return 'Local session. Sending takes it over.';
    if (this.controlMode === 'queued')
      return 'Queued. The agent picks this up when it connects.';
    return 'Offline. Your message waits for the agent.';
  }

  private get historyKey(): string {
    return this.sessionId ?? '';
  }

  private setMessage(text: string): void {
    this.message = text;
    this.menuIndex = 0;
    this.menuDismissed = false;
  }

  private runCommand(name: TalkCommandName): void {
    this.setMessage('');
    this.historyCursor = null;
    this.controlError = null;
    if (name === 'new') {
      this.dispatchEvent(
        new CustomEvent(TALK_NEW_SESSION_EVENT, {
          bubbles: true,
          composed: true,
        })
      );
      this.statusMessage = 'New session. Your next message starts it.';
    } else if (name === 'resume') {
      this.dispatchEvent(
        new CustomEvent(TALK_OPEN_PICKER_EVENT, {
          bubbles: true,
          composed: true,
        })
      );
    } else if (name === 'interrupt') {
      void this.interruptSession();
    } else if (name === 'release') {
      void this.releaseSession();
    } else if (name === 'status') {
      this.statusMessage = this.modeLine;
    }
    this.focusInput();
  }

  /** Up: the previous prompt typed into this session, never another's. */
  private recallHistory(direction: -1 | 1): boolean {
    const history = readHistory(this.historyKey);
    if (!history.length) return false;
    if (this.historyCursor === null) {
      // Only an empty box recalls, so Up still moves the caret in a draft.
      if (direction === 1 || this.message.trim()) return false;
      this.historyCursor = history.length - 1;
    } else {
      const next = this.historyCursor + direction;
      if (next >= history.length) {
        this.historyCursor = null;
        this.setMessage('');
        return true;
      }
      this.historyCursor = Math.max(0, next);
    }
    this.setMessage(history[this.historyCursor]);
    return true;
  }

  private handleKeydown(event: KeyboardEvent): void {
    if (event.isComposing) return;
    const menu = this.menuCommands;
    if (menu.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        this.menuIndex = (this.menuIndex + step + menu.length) % menu.length;
        return;
      }
      if (event.key === 'Tab') {
        event.preventDefault();
        this.setMessage(
          `/${menu[Math.min(this.menuIndex, menu.length - 1)].name}`
        );
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        this.menuDismissed = true;
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.runCommand(menu[Math.min(this.menuIndex, menu.length - 1)].name);
        return;
      }
    }
    if (event.key === 'Escape') {
      // Esc interrupts, as in Claude Code, only where Preloop can.
      if (!this.canInterrupt) return;
      event.preventDefault();
      void this.interruptSession();
      return;
    }
    if (
      (event.key === 'ArrowUp' || event.key === 'ArrowDown') &&
      !event.shiftKey &&
      !event.altKey &&
      !event.metaKey &&
      !event.ctrlKey
    ) {
      if (this.recallHistory(event.key === 'ArrowUp' ? -1 : 1))
        event.preventDefault();
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.send();
    }
  }

  /** Retry a failed turn, keeping its place in the thread. */
  public async retry(id: string): Promise<void> {
    const found = this.pending.find((item) => item.id === id);
    if (!found) return;
    // Route by what the server says now, not by the shape the turn had when
    // it failed: the session's mode may have been learned since.
    let message = found;
    if (
      found.targetSessionId &&
      found.targetSessionId === this.sessionId &&
      found.noteId === undefined &&
      this.noteMode
    ) {
      message = { ...found, noteId: '' };
    }
    this.setPending(
      this.pending.map((item) =>
        item.id === id ? { ...item, state: 'sending', error: undefined } : item
      )
    );
    // The turn goes where it was typed, not wherever the window is now.
    await this.deliver({
      ...message,
      targetSessionId: message.targetSessionId ?? null,
      startNewSession: Boolean(message.startNewSession),
    });
  }

  private async send(): Promise<void> {
    const text = this.message.trim();
    if (!text || !this.agent) return;
    if (!getAgentControlState(this.agent).enabled) return;
    // A turn typed into an ended session would go nowhere the operator can
    // see. Keep the text and ask where it should go instead.
    if (this.sessionId && this.sessionEnded) {
      this.endedGuardVisible = true;
      return;
    }

    const id = `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const inputMode = this.lastInputMode;
    const targetSessionId = this.sessionId ?? null;
    // The target is fixed here, at Enter. Anything that moves the window
    // after this point (a control read, a takeover round trip, a session
    // switch) must not move the turn with it.
    const pending: PendingTalkMessage = {
      id,
      text,
      at: new Date().toISOString(),
      state: 'sending',
      inputMode,
      targetSessionId,
      startNewSession: this.startsNewSession,
    };
    // The turn shows in the thread before the server has seen it: a chat that
    // swallows what you typed until a round trip finishes reads as broken.
    // Clearing the box now also means a second Enter cannot resend it.
    this.setPending([...this.pending, pending]);
    this.setMessage('');
    this.historyCursor = null;
    const historyKey = targetSessionId ?? '';
    const history = readHistory(historyKey);
    if (history[history.length - 1] !== text)
      writeHistory(historyKey, [...history, text]);
    this.lastInputMode = 'text';
    const asNote = targetSessionId
      ? await this.sessionTakesNotes(targetSessionId)
      : false;
    const routed: PendingTalkMessage = asNote
      ? { ...pending, noteId: '' }
      : pending;
    if (asNote) {
      this.setPending(
        this.pending.map((item) => (item.id === id ? routed : item))
      );
    }
    await this.deliver(routed);
  }

  /**
   * Whether a line typed on ``sessionId`` goes as a note. Uses the known
   * answer for the open session, otherwise asks the server for that session.
   * A failed read routes as a command; a terminal session then answers 409
   * and the line is resent as a note.
   */
  private async sessionTakesNotes(sessionId: string): Promise<boolean> {
    if (sessionId === this.sessionId) {
      if (this.sessionControlLegacy) return false;
      if (this.sessionControl) return this.sessionControl.mode === 'note';
    }
    try {
      const control = await getRuntimeSessionControl(sessionId);
      return control?.mode === 'note';
    } catch {
      return false;
    }
  }

  /** Send one line as an operator note the session reads at its next hook. */
  private async deliverNote(turn: PendingTalkMessage): Promise<void> {
    const { id, text } = turn;
    this.sending = true;
    this.controlError = null;
    try {
      const note = await sendOperatorNote({
        text,
        runtime_session_id: turn.targetSessionId ?? undefined,
      });
      this.setPending(
        this.pending.map((item) =>
          item.id === id
            ? { ...item, state: 'waiting', noteId: note.note_id }
            : item
        )
      );
      this.statusMessage = null;
      this.watchStaleNotes();
      this.dispatchEvent(
        new CustomEvent(TALK_MESSAGE_SENT_EVENT, {
          detail: { response: note, pendingId: id },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.setPending(
        this.pending.map((item) =>
          item.id === id
            ? {
                ...item,
                state: 'failed',
                error:
                  error instanceof Error ? error.message : 'Failed to send',
              }
            : item
        )
      );
    } finally {
      this.sending = false;
    }
  }

  private async deliver(turn: PendingTalkMessage): Promise<void> {
    if (!this.agent) return;
    if (turn.noteId !== undefined && turn.targetSessionId) {
      await this.deliverNote(turn);
      return;
    }
    const { id, text } = turn;
    const inputMode = turn.inputMode ?? 'text';
    const targetSessionId = turn.targetSessionId ?? null;
    const startNewSession = Boolean(turn.startNewSession);
    this.sending = true;
    this.controlError = null;
    const metadata = {
      source: 'preloop_console',
      requested_from: this.sourceContext,
    };
    try {
      // A session still driven by its local TUI ignores injected turns, so
      // claim it first. Already-remote sessions skip the extra round trip.
      if (this.controlMode === 'local') {
        await this.takeOverSession(targetSessionId, startNewSession);
      }
      const response =
        inputMode === 'voice_transcript'
          ? await sendAgentControlVoiceTranscript(this.agent.id, {
              transcript: text,
              target_session_id: targetSessionId,
              start_new_session: startNewSession,
              metadata: {
                ...metadata,
                input_method: 'browser_speech_recognition',
              },
              voice: {
                locale: navigator.language || 'en-US',
                duration_ms: this.recognitionStartedAt
                  ? Date.now() - this.recognitionStartedAt
                  : undefined,
                transcript_source: 'browser_speech_recognition',
              },
            })
          : await sendAgentControlCommand(this.agent.id, {
              message: text,
              target_session_id: targetSessionId,
              session_mode: targetSessionId
                ? 'existing'
                : startNewSession
                  ? 'new'
                  : 'current',
              start_new_session: startNewSession,
              metadata,
            });
      this.statusMessage =
        response.status === 'queued' || response.published
          ? 'Queued for delivery'
          : 'Delivered';
      this.recognitionStartedAt = null;
      const commandId =
        typeof response.command_id === 'string'
          ? response.command_id
          : undefined;
      // A turn that started a new session belongs to the session the server
      // created for it, so the bubble follows the page there.
      // The server names the created session in `target_session_id`;
      // `runtime_session_id` is the agent's existing binding.
      const createdSessionId =
        startNewSession && typeof response.target_session_id === 'string'
          ? response.target_session_id
          : null;
      this.setPending(
        this.pending.map((item) =>
          item.id === id
            ? {
                ...item,
                state: 'sent',
                commandId,
                ...(createdSessionId
                  ? { targetSessionId: createdSessionId }
                  : {}),
              }
            : item
        )
      );
      this.dispatchEvent(
        new CustomEvent(TALK_MESSAGE_SENT_EVENT, {
          detail: { response, pendingId: id },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.setPending(
        this.pending.map((item) =>
          item.id === id
            ? {
                ...item,
                state: 'failed',
                error:
                  error instanceof Error ? error.message : 'Failed to send',
              }
            : item
        )
      );
      this.statusMessage = null;
      // The server refused a command because this session is a terminal it
      // cannot type into: resend the same line as a note, and learn the mode.
      if (
        (error as { code?: string } | null)?.code === 'terminal_session' &&
        targetSessionId &&
        !startNewSession
      ) {
        void this.loadSessionControl();
        this.setPending(
          this.pending.map((item) =>
            item.id === id
              ? { ...item, state: 'sending', error: undefined, noteId: '' }
              : item
          )
        );
        this.sending = false;
        await this.deliverNote({ ...turn, noteId: '' });
        return;
      }
    } finally {
      this.sending = false;
    }
  }

  /**
   * Drop sent turns whose echo the thread now renders itself.
   *
   * A sent turn is matched by its command id against the ids the page found
   * in the reloaded timeline. One that has not been echoed yet stays on
   * screen: dropping it on every reload made a queued turn vanish until the
   * agent picked it up. A turn the server returned no id for cannot be
   * matched and is dropped on reload, as before.
   */
  public clearSentPending(echoedCommandIds: ReadonlySet<string>): void {
    const keep = this.pending.filter((item) => {
      // A note the timeline now shows (with its own "Read at" label) is
      // dropped, so it never renders twice. An unread note stays.
      if (item.noteId) return !echoedCommandIds.has(item.noteId);
      return (
        item.state !== 'sent' ||
        (item.commandId !== undefined && !echoedCommandIds.has(item.commandId))
      );
    });
    if (keep.length === this.pending.length) return;
    this.setPending(keep);
  }

  private renderEndedGuard() {
    if (!this.endedGuardVisible || !this.sessionEnded) return nothing;
    const alternative = this.liveAlternative;
    return html`
      <div class="ended-guard" role="alert" data-testid="ended-session-guard">
        <span>This session has ended. Your message was not sent.</span>
        ${
          alternative
            ? html`<sl-button
                size="small"
                data-testid="ended-switch"
                @click=${() =>
                  this.dispatchEvent(
                    new CustomEvent(TALK_SWITCH_SESSION_EVENT, {
                      detail: { sessionId: alternative.id },
                      bubbles: true,
                      composed: true,
                    })
                  )}
              >
                Switch to ${alternative.subject}
              </sl-button>`
            : nothing
        }
        ${
          this.agent?.supports_new_session !== false
            ? html`<sl-button
                size="small"
                data-testid="ended-new"
                @click=${() =>
                  this.dispatchEvent(
                    new CustomEvent(TALK_NEW_SESSION_EVENT, {
                      bubbles: true,
                      composed: true,
                    })
                  )}
              >
                Start new session
              </sl-button>`
            : nothing
        }
      </div>
    `;
  }

  private copyInstallCommand(command: string): void {
    void navigator.clipboard?.writeText(command);
    this.commandCopied = true;
    if (this.copyResetTimer !== null) window.clearTimeout(this.copyResetTimer);
    this.copyResetTimer = window.setTimeout(() => {
      this.commandCopied = false;
      this.copyResetTimer = null;
    }, 2000);
  }

  private renderDisabledHelp() {
    const hint = getAgentControlInstallHint(this.agent);
    return html`
      <div class="helptext" data-testid="composer-help">${hint.helptext}</div>
      ${
        hint.command
          ? html`
              <div class="install-command">
                <code>${hint.command}</code>
                <sl-icon-button
                  name=${this.commandCopied ? 'check2' : 'clipboard'}
                  label="Copy install command"
                  @click=${() => this.copyInstallCommand(hint.command!)}
                ></sl-icon-button>
              </div>
            `
          : nothing
      }
      <div class="helptext">
        <a href=${hint.docsUrl} target="_blank" rel="noopener noreferrer">
          Agent Control docs
        </a>
      </div>
    `;
  }

  render() {
    const state = getAgentControlState(this.agent);
    const hint = getAgentControlInstallHint(this.agent);
    const enabled = state.enabled;
    const microphoneAvailable =
      this.agent?.supports_voice !== false &&
      (this.localSpeechRecognitionAvailable || this.mediaRecorderAvailable);
    const listening = this.listening || this.recordingFallback;

    const showControlRow = this.controlRowVisible;
    const menu = this.menuCommands;
    return html`
      ${
        showControlRow && this.noteMode
          ? html`
              <div class="control-row">
                <span class="mode" data-testid="composer-mode">
                  ${this.modeLine}
                </span>
              </div>
            `
          : nothing
      }
      ${
        showControlRow && !this.noteMode && this.sessionDrivable
          ? html`
              <div class="control-row">
                <span class="mode" data-testid="composer-mode">
                  ${this.modeLine}
                </span>
                ${
                  this.canInterrupt
                    ? html`<sl-button
                        size="small"
                        @click=${() => void this.interruptSession()}
                      >
                        Interrupt
                      </sl-button>`
                    : nothing
                }
                ${
                  this.canRelease
                    ? html`<sl-button
                        size="small"
                        @click=${() => void this.releaseSession()}
                      >
                        Release
                      </sl-button>`
                    : nothing
                }
              </div>
            `
          : nothing
      }
      ${
        menu.length
          ? html`<talk-command-menu
              .commands=${menu}
              .activeIndex=${Math.min(this.menuIndex, menu.length - 1)}
              @talk-command-pick=${(
                event: CustomEvent<{ name: TalkCommandName }>
              ) => {
                event.stopPropagation();
                this.runCommand(event.detail.name);
              }}
            ></talk-command-menu>`
          : nothing
      }
      <div class="input-row">
        <sl-textarea
          data-testid="composer-input"
          resize="auto"
          rows="2"
          label=${`Message ${this.agent?.display_name || 'agent'}`}
          placeholder=${
            enabled
              ? `Message ${this.agent?.display_name || 'this agent'}`
              : hint.placeholder
          }
          .value=${this.message}
          ?disabled=${!enabled}
          @sl-input=${(event: Event) => {
            this.setMessage((event.target as HTMLTextAreaElement).value);
            this.historyCursor = null;
            this.lastInputMode = 'text';
          }}
          @keydown=${(event: KeyboardEvent) => this.handleKeydown(event)}
        ></sl-textarea>
        ${
          enabled && microphoneAvailable
            ? html`
                <sl-tooltip content=${listening ? 'Stop' : 'Dictate'}>
                  <sl-button
                    size="medium"
                    circle
                    variant=${listening ? 'danger' : 'default'}
                    @click=${() =>
                      this.listening
                        ? this.stopListening()
                        : this.recordingFallback
                          ? this.stopFallbackRecording()
                          : this.startListening()}
                  >
                    <sl-icon name=${listening ? 'mic-mute' : 'mic'}></sl-icon>
                  </sl-button>
                </sl-tooltip>
              `
            : nothing
        }
        <sl-button
          variant="primary"
          data-testid="composer-send"
          ?loading=${this.sending}
          ?disabled=${!enabled || !this.message.trim()}
          @click=${() => void this.send()}
        >
          Send
        </sl-button>
      </div>

      ${
        enabled
          ? html`<div class="hint">
              Enter sends. Shift+Enter adds a new line. / for
              commands.${this.canInterrupt ? ' Esc interrupts.' : nothing}${
                this.statusMessage ? html` · ${this.statusMessage}` : nothing
              }
            </div>`
          : this.renderDisabledHelp()
      }
      ${this.renderEndedGuard()}
      ${
        this.controlError
          ? html`<div class="error" role="alert">${this.controlError}</div>`
          : nothing
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talk-composer': TalkComposer;
  }
}
