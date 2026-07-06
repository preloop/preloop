import { LitElement, css, html, nothing, render as renderToDom } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import {
  getAccountRuntimeSessionActivityTimeline,
  getRuntimeSessionGatewayEvents,
  sendAgentControlCommand,
  sendAgentControlVoiceTranscript,
  transcribeAudio,
} from '../api';
import type {
  AgentControlCommandResponse,
  FlowGatewayEvent,
  ManagedAgentSummary,
  RuntimeSessionActivityItem,
  RuntimeSessionSummary,
} from '../types';
import {
  formatAgentControlSessionLabel,
  getAgentControlState,
} from '../utils/agent-control';
import type { ObservedSession } from '../utils/session-observer';
import { normalizeObservedSession } from '../utils/session-observer';
import './session-replay-panel';

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
        results: ArrayLike<{
          isFinal: boolean;
          0: { transcript: string };
        }>;
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

@customElement('agent-talk-composer')
export class AgentTalkComposer extends LitElement {
  @property({ attribute: false })
  agent: ManagedAgentSummary | null = null;

  @property({ attribute: false })
  sessions: RuntimeSessionSummary[] = [];

  @property({ type: String })
  sourceContext = 'agent-talk-composer';

  @property({ type: Boolean })
  compact = false;

  @property({ type: Boolean })
  disabled = false;

  @state()
  private dialogOpen = false;

  @state()
  private message = '';

  @state()
  private targetSessionId = '__new__';

  @state()
  private sending = false;

  @state()
  private listening = false;

  @state()
  private statusMessage: string | null = null;

  @state()
  private errorMessage: string | null = null;

  @state()
  private lastInputMode: 'text' | 'voice_transcript' = 'text';

  @state()
  private recordingFallback = false;

  @state()
  private continuousChat = false;

  @state()
  private sessionHistoryLoading = false;

  @state()
  private sessionHistorySession: ObservedSession | null = null;

  @state()
  private sessionHistoryEvents: FlowGatewayEvent[] = [];

  @state()
  private sessionHistoryActivity: RuntimeSessionActivityItem[] = [];

  private recognition: SpeechRecognitionLike | null = null;
  private recognitionStartedAt: number | null = null;
  private mediaRecorder: MediaRecorder | null = null;
  private mediaRecorderChunks: Blob[] = [];
  private fallbackTranscribing = false;
  private fallbackTranscriptionRequested = false;
  private fallbackFinalRequested = false;
  private fallbackTranscriptionUnavailable = false;
  private dialogPortal: HTMLDivElement | null = null;

  static styles = css`
    :host {
      display: inline-block;
    }
  `;

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopListening();
    this.stopFallbackRecording();
    this.removeDialogPortal();
  }

  updated(changedProperties: Map<string, unknown>): void {
    if (changedProperties.has('agent')) {
      this.loadSavedTargetSession();
    } else if (changedProperties.has('sessions')) {
      this.ensureValidTargetSession();
    }
    if (
      changedProperties.has('agent') ||
      changedProperties.has('sessions') ||
      changedProperties.has('targetSessionId') ||
      changedProperties.has('continuousChat')
    ) {
      void this.loadSelectedSessionHistory();
    }
    this.renderDialogPortal();
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

  private get sessionStorageKey(): string | null {
    return this.agent?.id
      ? `preloop-agent-talk-session:${this.agent.id}`
      : null;
  }

  private loadSavedTargetSession(): void {
    const key = this.sessionStorageKey;
    const saved = key ? localStorage.getItem(key) : null;
    this.targetSessionId = saved || '__new__';
    this.ensureValidTargetSession();
  }

  private saveTargetSession(): void {
    const key = this.sessionStorageKey;
    if (!key) return;
    localStorage.setItem(key, this.targetSessionId);
  }

  private get selectedHistorySessionId(): string | null {
    if (this.targetSessionId !== '__new__') {
      return this.targetSessionId;
    }
    if (this.continuousChat) {
      return this.agent?.runtime_session_id ?? null;
    }
    return null;
  }

  private ensureValidTargetSession(): void {
    if (this.targetSessionId === '__new__') {
      return;
    }
    if (
      this.getSessionOptions().some(
        (session) => session.id === this.targetSessionId
      )
    ) {
      return;
    }
    this.targetSessionId = '__new__';
    this.saveTargetSession();
  }

  private getSessionOptions(): RuntimeSessionSummary[] {
    if (this.sessions.length > 0) {
      return this.sessions;
    }
    if (!this.agent?.runtime_session_id) {
      return [];
    }
    return [
      {
        id: this.agent.runtime_session_id,
        session_source_type: this.agent.session_source_type,
        session_source_id: this.agent.session_source_id,
        session_reference: this.agent.session_reference,
        runtime_principal_type: null,
        runtime_principal_id: null,
        runtime_principal_name: null,
        started_at: this.agent.started_at ?? this.agent.last_seen_at,
        last_activity_at: this.agent.last_activity_at,
        ended_at: this.agent.ended_at,
        flow_id: null,
        flow_name: null,
        flow_execution_id: null,
        latest_model_alias: this.agent.latest_model_alias,
        latest_provider_name: this.agent.latest_provider_name,
        is_active_now: this.agent.is_active_now,
        activity_status: this.agent.activity_status,
        total_requests: this.agent.total_requests,
        successful_requests: this.agent.total_requests,
        failed_requests: 0,
        token_usage: {
          prompt_tokens: 0,
          completion_tokens: 0,
          total_tokens: 0,
        },
        estimated_cost: this.agent.estimated_cost,
        last_request_at: this.agent.last_request_at,
      },
    ];
  }

  private async loadSelectedSessionHistory(): Promise<void> {
    const sessionId = this.selectedHistorySessionId;
    if (!this.dialogOpen || !sessionId) {
      this.sessionHistorySession = null;
      this.sessionHistoryEvents = [];
      this.sessionHistoryActivity = [];
      return;
    }
    const summary = this.getSessionOptions().find(
      (session) => session.id === sessionId
    );
    this.sessionHistoryLoading = true;
    try {
      const [events, activity] = await Promise.all([
        getRuntimeSessionGatewayEvents(sessionId, { limit: 25, offset: 0 }),
        getAccountRuntimeSessionActivityTimeline(sessionId).catch(() => ({
          items: [],
        })),
      ]);
      this.sessionHistorySession = normalizeObservedSession(
        summary ?? { id: sessionId }
      );
      this.sessionHistoryEvents = (events.logs || []).sort(
        (left, right) =>
          new Date(left.timestamp || 0).getTime() -
          new Date(right.timestamp || 0).getTime()
      );
      this.sessionHistoryActivity = activity.items || [];
    } catch (error) {
      console.error('Failed to load Agent Control session history:', error);
      this.sessionHistorySession = summary
        ? normalizeObservedSession(summary)
        : null;
      this.sessionHistoryEvents = [];
      this.sessionHistoryActivity = [];
    } finally {
      this.sessionHistoryLoading = false;
    }
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
          : `Listening: ${interimTranscript}`;
      }
    };
    recognition.onerror = (event) => {
      this.listening = false;
      this.recognition = null;
      if (this.mediaRecorderAvailable) {
        this.statusMessage =
          'On-device speech recognition is unavailable. Recording for server transcription...';
        void this.startFallbackRecording();
        return;
      }
      this.errorMessage = event.error
        ? `Speech recognition failed: ${event.error}`
        : 'Speech recognition failed.';
    };
    recognition.onend = () => {
      this.listening = false;
      this.recognition = null;
      if (this.lastInputMode === 'voice_transcript' && this.message.trim()) {
        this.statusMessage = 'Voice captured. Review or send it.';
      }
    };

    try {
      recognition.start();
      this.recognition = recognition;
      this.recognitionStartedAt = Date.now();
      this.listening = true;
      this.errorMessage = null;
      this.statusMessage = 'Listening...';
    } catch (error) {
      this.errorMessage =
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
      this.errorMessage =
        'Microphone capture is not available in this browser. Type your prompt instead.';
      return;
    }
    this.stopFallbackRecording();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.mediaRecorderChunks = [];
      this.fallbackTranscriptionUnavailable = false;
      const recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          this.mediaRecorderChunks.push(event.data);
          void this.transcribeFallbackRecording(false);
        }
      };
      recorder.onstop = () => {
        for (const track of stream.getTracks()) {
          track.stop();
        }
        void this.transcribeFallbackRecording(true);
      };
      recorder.start(3000);
      this.mediaRecorder = recorder;
      this.recordingFallback = true;
      this.recognitionStartedAt = Date.now();
      this.statusMessage = 'Recording. Transcript will appear as you speak.';
      this.errorMessage = null;
    } catch (error) {
      this.errorMessage =
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
    if (this.mediaRecorder.state !== 'inactive') {
      this.mediaRecorder.stop();
    }
    this.mediaRecorder = null;
    this.recordingFallback = false;
  }

  private async transcribeFallbackRecording(final = true): Promise<void> {
    if (this.fallbackTranscriptionUnavailable) {
      return;
    }
    if (this.mediaRecorderChunks.length === 0) {
      this.statusMessage = null;
      return;
    }

    if (this.fallbackTranscribing) {
      this.fallbackTranscriptionRequested = true;
      this.fallbackFinalRequested = this.fallbackFinalRequested || final;
      return;
    }

    let shouldFinalize = final;
    this.fallbackTranscribing = true;
    try {
      while (true) {
        this.fallbackTranscriptionRequested = false;
        this.fallbackFinalRequested = false;
        const audio = new Blob(this.mediaRecorderChunks, {
          type: this.mediaRecorderChunks[0]?.type || 'audio/webm',
        });
        this.statusMessage = shouldFinalize
          ? 'Transcribing audio...'
          : 'Listening and transcribing...';
        try {
          const response = await transcribeAudio(audio, {
            filename: 'agent-talk.webm',
          });
          this.message = response.text;
          this.lastInputMode = 'voice_transcript';
          this.statusMessage = shouldFinalize
            ? 'Voice captured. Review or send it.'
            : 'Listening. Transcript is editable before sending.';
        } catch (error) {
          this.fallbackTranscriptionUnavailable = true;
          this.errorMessage =
            error instanceof Error
              ? error.message
              : 'Failed to transcribe recorded audio.';
          this.statusMessage = 'Transcription unavailable';
          if (this.mediaRecorder?.state === 'recording') {
            this.mediaRecorder.stop();
          }
          break;
        }

        if (!this.fallbackTranscriptionRequested) {
          break;
        }
        shouldFinalize = this.fallbackFinalRequested;
      }
    } finally {
      this.fallbackTranscribing = false;
      if (shouldFinalize) {
        this.mediaRecorderChunks = [];
      }
    }
  }

  private getTargeting(): {
    target_session_id: string | null;
    start_new_session: boolean;
    session_mode: 'new' | 'existing';
  } {
    const targetSessionId =
      this.targetSessionId === '__new__' ? null : this.targetSessionId;
    return {
      target_session_id: targetSessionId,
      start_new_session: targetSessionId === null,
      session_mode: targetSessionId ? 'existing' : 'new',
    };
  }

  private getResponseStatus(response: AgentControlCommandResponse): string {
    if (response.status === 'delivered' || response.local_delivery) {
      return 'Delivered';
    }
    if (response.status === 'queued' || response.published) {
      return 'Queued for delivery';
    }
    return 'Sent';
  }

  private async sendPrompt(): Promise<void> {
    if (!this.agent || !this.message.trim()) {
      return;
    }

    const controlState = getAgentControlState(this.agent);
    if (!controlState.enabled) {
      this.errorMessage = 'Agent Control is not available for this agent.';
      return;
    }

    const message = this.message.trim();
    const targeting = this.getTargeting();
    this.sending = true;
    this.errorMessage = null;
    this.statusMessage = 'Sending...';

    try {
      const inputMode = this.lastInputMode;
      const metadata = {
        source: 'preloop_console',
        requested_from: this.sourceContext,
      };
      const response =
        inputMode === 'voice_transcript'
          ? await sendAgentControlVoiceTranscript(this.agent.id, {
              transcript: message,
              target_session_id: targeting.target_session_id,
              start_new_session: targeting.start_new_session,
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
              message,
              target_session_id: targeting.target_session_id,
              session_mode: targeting.session_mode,
              start_new_session: targeting.start_new_session,
              metadata,
            });

      const status = this.getResponseStatus(response);
      this.statusMessage =
        inputMode === 'voice_transcript'
          ? `${status}. Voice transcript routed.`
          : status;
      this.message = '';
      this.lastInputMode = 'text';
      this.recognitionStartedAt = null;
      const responseSessionId =
        response.target_session_id ?? response.runtime_session_id ?? null;
      if (this.continuousChat && responseSessionId) {
        this.targetSessionId = String(responseSessionId);
        this.saveTargetSession();
      }
      this.dispatchEvent(
        new CustomEvent('agent-control-sent', {
          detail: { response, inputMode },
          bubbles: true,
          composed: true,
        })
      );
      if (this.continuousChat) {
        await this.loadSelectedSessionHistory();
        this.focusMessage();
      } else {
        this.closeDialog();
      }
    } catch (error) {
      this.errorMessage =
        error instanceof Error
          ? error.message
          : 'Failed to send Agent Control prompt';
      this.statusMessage = 'Send failed';
    } finally {
      this.sending = false;
    }
  }

  public openDialog(): void {
    const controlState = getAgentControlState(this.agent);
    if (!controlState.enabled) return;
    this.dialogOpen = true;
    this.renderDialogPortal();
    void this.loadSelectedSessionHistory();
    this.focusMessage();
  }

  private focusMessage(): void {
    const focus = () => {
      const textarea = this.dialogPortal?.querySelector(
        'sl-textarea[data-message-input]'
      );
      (textarea as HTMLElement | null)?.focus();
    };
    window.setTimeout(focus, 0);
    window.setTimeout(focus, 100);
  }

  private closeDialog(): void {
    if (this.dialogPortal?.contains(document.activeElement)) {
      (document.activeElement as HTMLElement).blur();
    }
    this.dialogOpen = false;
    this.stopListening();
    this.stopFallbackRecording();
    this.renderDialogPortal();
  }

  private handleDialogHide(event: Event): void {
    if (event.target !== event.currentTarget) {
      return;
    }
    this.closeDialog();
  }

  private handleDialogAfterShow(event: Event): void {
    if (event.target !== event.currentTarget) {
      return;
    }
    this.focusMessage();
  }

  private ensureDialogPortal(): HTMLDivElement {
    if (this.dialogPortal) {
      return this.dialogPortal;
    }
    const portal = document.createElement('div');
    portal.setAttribute('data-agent-talk-dialog', '');
    document.body.appendChild(portal);
    this.dialogPortal = portal;
    return portal;
  }

  private renderSessionHistory() {
    if (!this.selectedHistorySessionId) {
      return nothing;
    }
    return html`
      <div class="session-history">
        <div class="history-header">
          <div>
            <strong>Session history</strong>
            <div class="microcopy">
              Recent interaction context for this agent session.
            </div>
          </div>
          ${
            this.sessionHistoryLoading
              ? html`<sl-spinner></sl-spinner>`
              : nothing
          }
        </div>
        <session-replay-panel
          .session=${this.sessionHistorySession}
          .events=${this.sessionHistoryEvents}
          .activity=${this.sessionHistoryActivity}
          replayMode="chat"
          ?loading=${this.sessionHistoryLoading}
        ></session-replay-panel>
      </div>
    `;
  }

  private removeDialogPortal(): void {
    if (!this.dialogPortal) return;
    renderToDom(nothing, this.dialogPortal);
    this.dialogPortal.remove();
    this.dialogPortal = null;
  }

  private renderComposer() {
    const controlState = getAgentControlState(this.agent);
    const sessionOptions = this.getSessionOptions();
    const speechRecognitionAvailable = this.localSpeechRecognitionAvailable;
    const mediaRecorderAvailable = this.mediaRecorderAvailable;
    const microphoneAvailable =
      speechRecognitionAvailable || mediaRecorderAvailable;

    return html`
      <div class="composer">
        ${this.renderSessionHistory()}
        <div class="message-wrap">
          <sl-textarea
            data-message-input
            label="Message"
            placeholder="Tell this agent what to do..."
            resize="auto"
            rows="5"
            .value=${this.message}
            ?disabled=${!controlState.enabled || this.sending}
            @sl-input=${(event: Event) => {
              this.message = (event.target as HTMLTextAreaElement).value;
              this.lastInputMode = 'text';
            }}
            @keydown=${(event: KeyboardEvent) => {
              if (
                event.key === 'Enter' &&
                !event.shiftKey &&
                !event.isComposing
              ) {
                event.preventDefault();
                void this.sendPrompt();
              }
            }}
          ></sl-textarea>
          <sl-tooltip
            content=${
              speechRecognitionAvailable
                ? 'Dictate with on-device speech recognition'
                : mediaRecorderAvailable
                  ? 'Dictate through Preloop server transcription'
                  : 'Microphone capture unavailable'
            }
          >
            <sl-button
              class="mic-button"
              size="small"
              circle
              variant=${
                this.listening || this.recordingFallback ? 'danger' : 'default'
              }
              ?disabled=${
                !controlState.enabled || this.sending || !microphoneAvailable
              }
              @click=${() =>
                this.listening
                  ? this.stopListening()
                  : this.recordingFallback
                    ? this.stopFallbackRecording()
                    : this.startListening()}
            >
              <sl-icon
                name=${
                  this.listening || this.recordingFallback ? 'mic-mute' : 'mic'
                }
              ></sl-icon>
            </sl-button>
          </sl-tooltip>
        </div>

        <div class="widget-footer">
          <div class="widget-left">
            <sl-select
              size="small"
              value=${this.targetSessionId}
              ?disabled=${!controlState.enabled || this.sending}
              @sl-change=${(event: Event) => {
                this.targetSessionId = (
                  event.target as HTMLSelectElement
                ).value;
                this.saveTargetSession();
                if (this.targetSessionId !== '__new__') {
                  this.continuousChat = true;
                }
              }}
              hoist
            >
              <sl-option value="__new__">New session</sl-option>
              ${sessionOptions.map(
                (session) => html`
                  <sl-option value=${session.id}>
                    ${formatAgentControlSessionLabel(session)}
                  </sl-option>
                `
              )}
            </sl-select>
            <sl-switch
              size="small"
              ?checked=${this.continuousChat}
              ?disabled=${!controlState.enabled || this.sending}
              @sl-change=${(event: Event) => {
                this.continuousChat = (
                  event.target as HTMLInputElement
                ).checked;
              }}
            >
              Continuous chat
            </sl-switch>
          </div>
          <div class="dialog-actions">
            <sl-button @click=${() => this.closeDialog()}>Cancel</sl-button>
            <sl-button
              variant="primary"
              ?loading=${this.sending}
              ?disabled=${!controlState.enabled || !this.message.trim()}
              @click=${() => this.sendPrompt()}
            >
              Send
            </sl-button>
          </div>
        </div>
        ${
          !speechRecognitionAvailable && mediaRecorderAvailable
            ? html`
                <div class="microcopy">
                  On-device speech recognition is unavailable here. Dictation
                  uses your configured Preloop STT model and inserts the
                  transcript for review before sending.
                </div>
              `
            : nothing
        }
        <div class="microcopy">Enter sends. Shift+Enter adds a new line.</div>
        ${
          this.errorMessage
            ? html`<sl-alert open variant="danger"
                >${this.errorMessage}</sl-alert
              >`
            : nothing
        }
        ${
          this.statusMessage
            ? html`<sl-alert open variant="primary"
                >${this.statusMessage}</sl-alert
              >`
            : nothing
        }
      </div>
    `;
  }

  private renderDialogStyles() {
    return html`
      <style>
        agent-talk-composer-dialog sl-dialog::part(panel) {
          width: min(720px, calc(100vw - 2rem));
        }
        agent-talk-composer-dialog .composer {
          display: flex;
          flex-direction: column;
          gap: var(--sl-spacing-small);
        }
        agent-talk-composer-dialog .message-wrap {
          position: relative;
        }
        agent-talk-composer-dialog .session-history {
          background: var(--sl-color-neutral-50);
          border: 1px solid var(--sl-color-neutral-200);
          border-radius: var(--sl-border-radius-medium);
          max-height: min(42vh, 420px);
          overflow: auto;
          padding: var(--sl-spacing-small);
        }
        agent-talk-composer-dialog .history-header {
          align-items: center;
          display: flex;
          justify-content: space-between;
          margin-bottom: var(--sl-spacing-small);
        }
        agent-talk-composer-dialog
          .message-wrap
          sl-textarea::part(form-control) {
          margin: 0;
        }
        agent-talk-composer-dialog .message-wrap sl-textarea::part(textarea) {
          min-height: 9rem;
          padding-right: 3rem;
        }
        agent-talk-composer-dialog .mic-button {
          position: absolute;
          right: var(--sl-spacing-x-small);
          bottom: var(--sl-spacing-x-small);
          z-index: 1;
        }
        agent-talk-composer-dialog .widget-footer,
        agent-talk-composer-dialog .widget-left,
        agent-talk-composer-dialog .dialog-actions {
          display: flex;
          align-items: center;
          gap: var(--sl-spacing-x-small);
        }
        agent-talk-composer-dialog .widget-footer {
          justify-content: space-between;
        }
        agent-talk-composer-dialog .widget-left {
          min-width: 0;
          flex-wrap: wrap;
        }
        agent-talk-composer-dialog .widget-left sl-select {
          min-width: 180px;
          max-width: min(340px, calc(100vw - 10rem));
        }
        agent-talk-composer-dialog .microcopy {
          color: var(--sl-color-neutral-600);
          font-size: var(--sl-font-size-small);
        }
      </style>
    `;
  }

  private renderDialogPortal(): void {
    if (!this.dialogOpen) {
      if (this.dialogPortal) {
        renderToDom(nothing, this.dialogPortal);
      }
      return;
    }
    const portal = this.ensureDialogPortal();
    renderToDom(
      html`
        <agent-talk-composer-dialog>
          ${this.renderDialogStyles()}
          <sl-dialog
            label=${`Talk to ${this.agent?.display_name || 'agent'}`}
            open
            @sl-after-show=${(e: Event) => this.handleDialogAfterShow(e)}
            @sl-hide=${(e: Event) => this.handleDialogHide(e)}
          >
            ${this.renderComposer()}
          </sl-dialog>
        </agent-talk-composer-dialog>
      `,
      portal
    );
  }

  render() {
    const controlState = getAgentControlState(this.agent);
    if (!controlState.visible) {
      return nothing;
    }

    return html`
      <sl-tooltip content=${controlState.detail}>
        <sl-button
          size=${this.compact ? 'small' : 'medium'}
          variant=${controlState.online ? 'primary' : 'default'}
          ?disabled=${!controlState.enabled || this.disabled}
          @click=${() => this.openDialog()}
        >
          <sl-icon slot="prefix" name="chat-dots"></sl-icon>
          Talk
        </sl-button>
      </sl-tooltip>
    `;
  }
}
