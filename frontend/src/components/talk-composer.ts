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
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import {
  sendAgentControlCommand,
  sendAgentControlRelease,
  sendAgentControlTakeover,
  sendAgentControlVoiceTranscript,
  transcribeAudio,
} from '../api';
import type { ManagedAgentSummary } from '../types';
import {
  getAgentControlInstallHint,
  getAgentControlSessionMode,
  getAgentControlState,
} from '../utils/agent-control';
import type { PendingTalkMessage } from './session-chat-view';

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

@customElement('talk-composer')
export class TalkComposer extends LitElement {
  @property({ attribute: false })
  agent: ManagedAgentSummary | null = null;

  /** The session this composer talks to; null starts a new one. */
  @property({ type: String })
  sessionId: string | null = null;

  @property({ type: String })
  sourceContext = 'talk-window';

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
  }

  /** The window opens with the cursor already in the box. */
  public focusInput(): void {
    const focus = () => this.textarea?.focus();
    window.setTimeout(focus, 0);
    window.setTimeout(focus, 120);
  }

  public get pendingMessages(): PendingTalkMessage[] {
    return this.pending;
  }

  private setPending(next: PendingTalkMessage[]): void {
    this.pending = next;
    this.dispatchEvent(
      new CustomEvent(TALK_PENDING_CHANGED_EVENT, {
        detail: { pending: next },
        bubbles: true,
        composed: true,
      })
    );
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

  private async takeOverSession(): Promise<void> {
    if (!this.agent) return;
    await sendAgentControlTakeover(this.agent.id, {
      target_session_id: this.sessionId,
      start_new_session: !this.sessionId,
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

  private handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void this.send();
    }
  }

  /** Retry a failed turn, keeping its place in the thread. */
  public async retry(id: string): Promise<void> {
    const message = this.pending.find((item) => item.id === id);
    if (!message) return;
    this.setPending(
      this.pending.map((item) =>
        item.id === id ? { ...item, state: 'sending', error: undefined } : item
      )
    );
    await this.deliver(message.id, message.text, message.inputMode ?? 'text');
  }

  private async send(): Promise<void> {
    const text = this.message.trim();
    if (!text || !this.agent) return;
    if (!getAgentControlState(this.agent).enabled) return;

    const id = `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const inputMode = this.lastInputMode;
    // The turn shows in the thread before the server has seen it: a chat that
    // swallows what you typed until a round trip finishes reads as broken.
    this.setPending([
      ...this.pending,
      {
        id,
        text,
        at: new Date().toISOString(),
        state: 'sending',
        inputMode,
      },
    ]);
    this.message = '';
    this.lastInputMode = 'text';
    await this.deliver(id, text, inputMode);
  }

  private async deliver(
    id: string,
    text: string,
    inputMode: 'text' | 'voice_transcript'
  ): Promise<void> {
    if (!this.agent) return;
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
        await this.takeOverSession();
      }
      const response =
        inputMode === 'voice_transcript'
          ? await sendAgentControlVoiceTranscript(this.agent.id, {
              transcript: text,
              target_session_id: this.sessionId,
              start_new_session: !this.sessionId,
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
              target_session_id: this.sessionId,
              session_mode: this.sessionId ? 'existing' : 'new',
              start_new_session: !this.sessionId,
              metadata,
            });
      this.statusMessage =
        response.status === 'queued' || response.published
          ? 'Queued for delivery'
          : 'Delivered';
      this.recognitionStartedAt = null;
      this.setPending(
        this.pending.map((item) =>
          item.id === id ? { ...item, state: 'sent' } : item
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
    } finally {
      this.sending = false;
    }
  }

  /** Drop turns the server has confirmed and the thread now renders itself. */
  public clearSentPending(): void {
    if (!this.pending.some((item) => item.state === 'sent')) return;
    this.setPending(this.pending.filter((item) => item.state !== 'sent'));
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
      this.localSpeechRecognitionAvailable || this.mediaRecorderAvailable;
    const listening = this.listening || this.recordingFallback;

    return html`
      ${
        enabled
          ? html`
              <div class="control-row">
                <span class="mode" data-testid="composer-mode">
                  ${
                    this.controlMode === 'remote'
                      ? 'You have this session'
                      : this.controlMode === 'local'
                        ? 'Local session. Sending takes it over.'
                        : this.controlMode === 'queued'
                          ? 'Queued. The agent picks this up when it connects.'
                          : 'Offline. Your message waits for the agent.'
                  }
                </span>
                ${
                  this.agent?.supports_interrupt &&
                  this.controlMode === 'remote'
                    ? html`<sl-button
                        size="small"
                        @click=${() => void this.interruptSession()}
                      >
                        Interrupt
                      </sl-button>`
                    : nothing
                }
                ${
                  this.controlMode === 'remote'
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
            this.message = (event.target as HTMLTextAreaElement).value;
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
              Enter sends. Shift+Enter adds a new
              line.${
                this.statusMessage ? html` · ${this.statusMessage}` : nothing
              }
            </div>`
          : this.renderDisabledHelp()
      }
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
