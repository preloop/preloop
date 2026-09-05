/**
 * Chat-style session transcript: ONLY top-level user prompts and final agent
 * responses are expanded; tool calls, tool results, system/injected segments
 * and intermediate agent output are nested in collapsed, manually expandable
 * step groups. Reference UX: Claude Code web chat. When in doubt, collapse —
 * except for prompt classification, where doubt keeps the prompt visible
 * (see utils/transcript.ts).
 *
 * Presentational only: events/activity arrive as props from
 * <preloop-session-observer>; paging is requested by re-emitting the
 * observer's existing `session-events-page-requested` event.
 */
import { LitElement, css, html, nothing } from 'lit';
import type { PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';
import type {
  TranscriptBuildResult,
  TranscriptItem,
  TranscriptMessageItem,
  TranscriptStep,
  TranscriptStepGroupItem,
} from '../utils/transcript';
import { buildConversation } from '../utils/transcript';
import { SESSION_EVENTS_PAGE_REQUESTED_EVENT } from '../utils/session-observer';

const MESSAGE_PREVIEW_CHARS = 2000;
const STEP_PREVIEW_CHARS = 600;
/** How far from the bottom still counts as "reading the latest". */
const FOLLOW_THRESHOLD_PX = 48;
/**
 * How long after a real gesture a scroll event still counts as user intent.
 * Wheel and touch scrolling keep emitting scroll events for a few hundred ms
 * after the last input event (momentum), so the window has to outlive the
 * gesture itself.
 */
const USER_SCROLL_WINDOW_MS = 700;
/**
 * How long an expand/collapse click stops the follow re-stick. Growing a
 * message the reader just opened must not scroll it out from under them.
 */
const EXPAND_SETTLE_MS = 500;
/** Input that means the operator moved the thread themselves. */
const GESTURE_EVENTS = [
  'wheel',
  'touchstart',
  'touchmove',
  'pointerdown',
  'mousedown',
  'keydown',
] as const;

/** A turn the operator sent that the server has not echoed back yet. */
export interface PendingTalkMessage {
  id: string;
  text: string;
  at: string;
  state: 'sending' | 'sent' | 'failed';
  error?: string;
  inputMode?: 'text' | 'voice_transcript';
}

/** Bubbled by the Retry link under a failed turn. */
export const TALK_RETRY_EVENT = 'talk-retry';

@customElement('session-chat-view')
export class SessionChatView extends LitElement {
  // `attribute: false`: these are data-only properties set via property
  // bindings; an attribute path would (de)serialize large arrays as JSON.
  @property({ attribute: false })
  events: FlowGatewayEvent[] = [];

  @property({ attribute: false })
  activity: RuntimeSessionActivityItem[] = [];

  @property({ type: Boolean })
  loading = false;

  @property({ type: Boolean })
  hasMoreEvents = false;

  @property({ type: Boolean })
  loadingMoreEvents = false;

  /** Pin the scroll position to the newest message as events stream in. */
  @property({ type: Boolean })
  followLive = false;

  @property({ type: String })
  emptyText = 'No conversation captured for this session yet.';

  /**
   * Own the viewport instead of growing the page.
   *
   * Embedded in the session widget the thread grows and the page scrolls, which
   * is right for reading a finished session. In the talk window the composer
   * must stay pinned at the bottom, so the thread scrolls inside itself and
   * offers a jump-to-latest pill when the operator has scrolled up.
   */
  @property({ type: Boolean, reflect: true })
  scrollable = false;

  /** Optimistic turns rendered after the transcript, newest last. */
  @property({ attribute: false })
  pending: PendingTalkMessage[] = [];

  @state()
  private atBottom = true;

  @state()
  private expandedMessageKeys = new Set<string>();

  @state()
  private expandedStepKeys = new Set<string>();

  // Rebuilt in willUpdate() only when `events`/`activity` change, NOT on
  // every render: expanding a message or toggling a step must not re-run the
  // whole O(n log n) transcript reconstruction.
  private conversation: TranscriptBuildResult = {
    items: [],
    stats: {
      promptCount: 0,
      responseCount: 0,
      stepCount: 0,
      toolResultCount: 0,
      injectedCount: 0,
      toolCallCount: 0,
      eventsWithoutRawBody: 0,
      eventsWithPartialToolResults: 0,
      totalEvents: 0,
    },
  };

  // What the thread held at the last follow-live scroll; scrolling happens
  // only when the conversation actually changed, never on state-only
  // re-renders. This is a signature, not a count: the talk view refetches a
  // fixed page of the newest events, so once a session is longer than one
  // page the array length stops growing while its contents keep changing.
  private lastContentSignature = '';
  private lastScrolledPendingCount = 0;
  private lastAnnouncedKey: string | null = null;
  /**
   * The `.thread` node the listeners are attached to. Not a boolean: the
   * loading and empty render branches have no `.thread`, so switching
   * sessions destroys it and Lit builds a new one. A latched boolean would
   * leave the listeners on the detached node and freeze `atBottom` for good.
   */
  private boundThread: HTMLElement | null = null;
  /** The thread node went away (loading or empty branch) and will come back. */
  private threadWasDestroyed = false;
  private contentObserver: ResizeObserver | null = null;
  private observedContent: Element | null = null;
  /**
   * When the last real user gesture on the thread happened. Negative
   * infinity, not zero: `performance.now()` starts at zero, so zero would
   * read as "a gesture just happened" for the first second of the page.
   */
  private lastGestureAt = Number.NEGATIVE_INFINITY;
  private pointerDown = false;
  /** Re-sticking is paused until this timestamp (an expand/collapse click). */
  private stickPausedUntil = 0;

  @state()
  private liveAnnouncement = '';

  static styles = css`
    :host {
      display: block;
      min-height: 0;
    }

    :host([scrollable]) {
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
      position: relative;
    }

    /*
     * Two elements, not one: the thread is the scroll viewport and the
     * content inside it is the thing whose height changes. A ResizeObserver
     * on a scroll container never fires for content growth, and content
     * growth after an update is exactly what used to leave the talk window
     * short of the bottom.
     */
    .thread {
      display: block;
    }

    .thread-content {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
      padding: var(--sl-spacing-small) 0;
    }

    :host([scrollable]) .thread {
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      overscroll-behavior: contain;
    }

    :host([scrollable]) .thread-content {
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
    }

    .jump-latest {
      bottom: var(--sl-spacing-small);
      left: 50%;
      position: absolute;
      transform: translateX(-50%);
      z-index: 1;
    }

    .jump-latest::part(base) {
      border-radius: 999px;
    }

    .pending-error {
      align-items: center;
      color: var(--sl-color-danger-700);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-x-small);
      margin-top: var(--sl-spacing-2x-small);
    }

    .bubble.pending {
      opacity: 0.72;
    }

    .live-region {
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      height: 1px;
      overflow: hidden;
      position: absolute;
      white-space: nowrap;
      width: 1px;
    }

    .empty,
    .loading {
      color: var(--sl-color-neutral-600);
      padding: var(--sl-spacing-x-large);
      text-align: center;
    }

    .coverage-note {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-left: 3px solid var(--sl-color-amber-400, #f5c518);
      border-radius: 4px;
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-x-small);
      line-height: 1.5;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .bubble {
      border-radius: var(--sl-border-radius-large);
      max-width: 88%;
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
    }

    .bubble.user {
      align-self: flex-end;
      background: var(--sl-color-primary-100);
      border: 1px solid var(--sl-color-primary-200);
    }

    .bubble.agent {
      align-self: flex-start;
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
    }

    .bubble.operator {
      align-self: flex-end;
      background: var(--sl-color-success-100, #e6f6ec);
      border: 1px solid var(--sl-color-success-200, #bfe8cd);
    }

    .bubble.failed {
      border-color: var(--sl-color-danger-300);
    }

    .queue-chip {
      background: rgba(242, 169, 59, 0.15);
      border: 1px solid #f2a93b;
      border-radius: 999px;
      color: #f2a93b;
      font-size: 11px;
      font-weight: 600;
      padding: 1px 8px;
    }

    .bubble-meta {
      align-items: center;
      color: var(--sl-color-neutral-600);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-x-small);
      margin-bottom: var(--sl-spacing-2x-small);
    }

    .bubble-role {
      font-weight: var(--sl-font-weight-semibold);
      text-transform: capitalize;
    }

    .bubble-text {
      color: var(--sl-color-neutral-900);
      font-size: var(--sl-font-size-small);
      line-height: 1.55;
      margin: 0;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }

    .steps {
      align-self: stretch;
      border: 1px dashed var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-color-neutral-50);
      margin: 0 var(--sl-spacing-medium);
    }

    .steps::part(summary) {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .steps::part(content) {
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small)
        var(--sl-spacing-small);
    }

    .steps-summary {
      align-items: center;
      display: inline-flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
    }

    .step {
      border-left: 2px solid var(--sl-color-neutral-300);
      margin: var(--sl-spacing-x-small) 0;
      padding: var(--sl-spacing-2x-small) var(--sl-spacing-small);
    }

    .step-header {
      align-items: center;
      color: var(--sl-color-neutral-700);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-x-small);
    }

    .step-label {
      font-weight: var(--sl-font-weight-semibold);
    }

    .step-kind-tool_call .step-label,
    .step-kind-tool_result .step-label {
      color: var(--sl-color-primary-700);
    }

    .step-kind-injected .step-label {
      color: var(--sl-color-warning-700);
    }

    .step-text {
      color: var(--sl-color-neutral-700);
      font-family: var(--sl-font-mono);
      font-size: var(--sl-font-size-x-small);
      line-height: 1.5;
      margin: var(--sl-spacing-2x-small) 0 0;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }

    .divider {
      align-items: center;
      color: var(--sl-color-neutral-500);
      display: flex;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-small);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .divider::before,
    .divider::after {
      background: var(--sl-color-neutral-200);
      content: '';
      flex: 1;
      height: 1px;
    }

    .load-earlier {
      align-self: center;
    }

    .inline-toggle {
      background: none;
      border: none;
      color: var(--sl-color-primary-600);
      cursor: pointer;
      font: inherit;
      font-size: var(--sl-font-size-x-small);
      padding: 0;
      text-decoration: underline;
    }
  `;

  willUpdate(changed: PropertyValues<this>): void {
    if (changed.has('events') || changed.has('activity')) {
      this.conversation = buildConversation(this.events, this.activity);
      // Screen readers get the agent's reply, not the whole thread: the last
      // message is the only thing that changed for the person listening.
      const messages = this.conversation.items.filter(
        (item): item is TranscriptMessageItem =>
          item.type === 'message' && item.kind === 'agent_response'
      );
      const latest = messages[messages.length - 1];
      if (latest && latest.key !== this.lastAnnouncedKey) {
        this.lastAnnouncedKey = latest.key;
        this.liveAnnouncement = latest.text
          ? `Agent replied: ${latest.text.slice(0, 400)}`
          : 'Agent replied.';
      }
    }
  }

  connectedCallback(): void {
    super.connectedCallback();
    // Keyboard scrolling (arrows, PageDown, Home/End) targets whatever has
    // focus inside the thread, so the host is where it can always be seen.
    this.addEventListener('keydown', this.markGesture);
    window.addEventListener('pointerup', this.handlePointerUp);
    window.addEventListener('pointercancel', this.handlePointerUp);
    // The release half of a scrollbar-thumb drag is a `mouseup` as well.
    window.addEventListener('mouseup', this.handlePointerUp);
  }

  updated(changed: PropertyValues<this>): void {
    this.bindThread();
    // A turn the operator just typed always pulls the thread down: they are
    // looking at what they sent.
    const pendingGrew = this.pending.length > this.lastScrolledPendingCount;
    this.lastScrolledPendingCount = this.pending.length;
    const signature = this.contentSignature();
    const contentChanged = signature !== this.lastContentSignature;
    this.lastContentSignature = signature;
    if (!this.followLive) return;
    // Scroll only when the conversation changed (or follow-live was just
    // switched on); a scrollTop write on every update would force a reflow
    // and yank the view each time the user expands a message they are
    // reading.
    if (!contentChanged && !pendingGrew && !changed.has('followLive')) return;
    // Someone who scrolled up to re-read is not interrupted; the pill takes
    // them back when they want it.
    if (this.scrollable && !this.atBottom && !pendingGrew) return;
    this.scrollToLatest();
  }

  disconnectedCallback(): void {
    this.removeEventListener('keydown', this.markGesture);
    window.removeEventListener('pointerup', this.handlePointerUp);
    window.removeEventListener('pointercancel', this.handlePointerUp);
    window.removeEventListener('mouseup', this.handlePointerUp);
    this.releaseThread();
    this.contentObserver?.disconnect();
    this.contentObserver = null;
    this.observedContent = null;
    super.disconnectedCallback();
  }

  /** What the thread is showing right now, cheap enough to compare per update. */
  private contentSignature(): string {
    const items = this.conversation.items;
    const last = items[items.length - 1];
    return `${items.length}|${last ? last.key : ''}|${this.events.length}|${
      this.activity.length
    }`;
  }

  /**
   * Attach scroll/gesture listeners and the size observer to the current
   * `.thread`, re-attaching whenever Lit replaces it (loading and empty
   * branches remove it, and a session switch goes through both).
   */
  private bindThread(): void {
    const thread = this.renderRoot.querySelector(
      '.thread'
    ) as HTMLElement | null;
    if (thread !== this.boundThread) {
      if (this.boundThread && !thread) this.threadWasDestroyed = true;
      this.releaseThread();
      this.boundThread = thread;
      // A rebuilt thread is a different conversation (the talk view empties
      // events when it follows the agent into a new session), so following
      // resumes whether or not the reader had scrolled up in the old one.
      if (thread && this.threadWasDestroyed) {
        this.threadWasDestroyed = false;
        if (this.followLive) this.atBottom = true;
      }
      if (thread && this.scrollable) {
        thread.addEventListener('scroll', this.handleScroll, { passive: true });
        for (const type of GESTURE_EVENTS) {
          thread.addEventListener(type, this.markGesture, { passive: true });
        }
      }
    }
    this.observeContent();
  }

  private releaseThread(): void {
    const thread = this.boundThread;
    if (!thread) return;
    thread.removeEventListener('scroll', this.handleScroll);
    for (const type of GESTURE_EVENTS) {
      thread.removeEventListener(type, this.markGesture);
    }
    if (this.observedContent) {
      this.contentObserver?.unobserve(this.observedContent);
      this.observedContent = null;
    }
    this.contentObserver?.unobserve(thread);
    this.boundThread = null;
  }

  /**
   * Follow the thread's own height and its content's height. Content that
   * grows after the update that scrolled (Shoelace elements rendering in
   * their own cycle, fonts, images, a `<pre>` reflowing) would otherwise
   * leave the view a few hundred pixels short of the bottom, and the viewport
   * shrinking (the composer growing, the follow banner appearing, a window
   * resize) would do the same.
   */
  private observeContent(): void {
    if (!this.scrollable || !this.boundThread) return;
    const content = this.renderRoot.querySelector('.thread-content');
    if (!content || content === this.observedContent) return;
    if (!this.contentObserver) {
      this.contentObserver = new ResizeObserver(() => this.stickIfFollowing());
    }
    if (this.observedContent)
      this.contentObserver.unobserve(this.observedContent);
    this.contentObserver.observe(content);
    this.contentObserver.observe(this.boundThread);
    this.observedContent = content;
  }

  private markGesture = (event?: Event): void => {
    // Chromium fires `mousedown` and no `pointerdown` when the press lands on
    // the scrollbar itself, so a thumb drag has to hold the flag too. Without
    // it a reader who grabs the thumb, pauses past the gesture window, then
    // drags would be snapped back to the bottom.
    if (event?.type === 'pointerdown' || event?.type === 'mousedown')
      this.pointerDown = true;
    this.lastGestureAt = performance.now();
  };

  private handlePointerUp = (): void => {
    if (!this.pointerDown) return;
    this.pointerDown = false;
    this.lastGestureAt = performance.now();
  };

  /** Did the operator's own hands move the thread just now? */
  private gestureInFlight(): boolean {
    if (this.pointerDown) return true;
    return performance.now() - this.lastGestureAt <= USER_SCROLL_WINDOW_MS;
  }

  private handleScroll = (event: Event): void => {
    const thread = event.currentTarget as HTMLElement;
    const distance =
      thread.scrollHeight - thread.scrollTop - thread.clientHeight;
    if (distance <= FOLLOW_THRESHOLD_PX) {
      // Scrolling back to the bottom always resumes following, however the
      // view got there.
      if (!this.atBottom) this.atBottom = true;
      return;
    }
    if (!this.atBottom) return;
    if (this.followLive && !this.gestureInFlight()) {
      // Layout moved the viewport, not the reader: a growing composer, an
      // inserted banner, a resize, our own write racing a height change.
      // Following is not something the page gets to switch off. Only a live
      // thread is put back; a static one is the reader's to move.
      this.stickToBottom();
      return;
    }
    this.atBottom = false;
  };

  /** Re-pin the viewport without touching follow state. */
  private stickToBottom(): void {
    const thread = this.boundThread;
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
  }

  private stickIfFollowing(): void {
    if (!this.followLive || !this.scrollable || !this.atBottom) return;
    if (performance.now() < this.stickPausedUntil) return;
    this.stickToBottom();
  }

  /** Public so the talk page can snap the thread down after it loads. */
  public scrollToLatest(): void {
    const thread =
      this.boundThread ??
      (this.renderRoot.querySelector('.thread') as HTMLElement | null);
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
    this.atBottom = true;
  }

  private formatTime(value: string | null): string {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleTimeString();
  }

  private toggleMessage(key: string): void {
    this.pauseSticking();
    const next = new Set(this.expandedMessageKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expandedMessageKeys = next;
  }

  private toggleStep(key: string): void {
    this.pauseSticking();
    const next = new Set(this.expandedStepKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expandedStepKeys = next;
  }

  /**
   * Opening a message the reader is looking at grows the thread, and growing
   * the thread re-sticks it to the bottom. That would scroll the thing they
   * just opened out of view, so the observer keeps its hands off for a moment.
   */
  private pauseSticking(): void {
    this.stickPausedUntil = performance.now() + EXPAND_SETTLE_MS;
  }

  private requestEarlierEvents(): void {
    this.dispatchEvent(
      new CustomEvent(SESSION_EVENTS_PAGE_REQUESTED_EVENT, {
        bubbles: true,
        composed: true,
      })
    );
  }

  private renderClampedText(
    text: string,
    key: string,
    limit: number,
    expanded: boolean,
    className: string
  ) {
    const isLong = text.length > limit;
    const shown = isLong && !expanded ? `${text.slice(0, limit)}…` : text;
    return html`
      <pre class=${className}>${shown}</pre>
      ${
        isLong
          ? html`
              <button
                class="inline-toggle"
                type="button"
                @click=${() => this.toggleMessage(key)}
              >
                ${expanded ? 'Show less' : 'Show full message'}
              </button>
            `
          : nothing
      }
    `;
  }

  private renderMessage(item: TranscriptMessageItem) {
    const bubbleClass =
      item.kind === 'user_prompt'
        ? 'user'
        : item.kind === 'operator'
          ? 'operator'
          : 'agent';
    const displayText = item.text
      ? item.text
      : item.redacted
        ? 'Content redacted by capture policy.'
        : 'No text content captured.';
    return html`
      <div
        class="bubble ${bubbleClass} ${item.failed ? 'failed' : ''}"
        data-kind=${item.kind}
      >
        <div class="bubble-meta">
          <span class="bubble-role">
            ${
              item.kind === 'user_prompt'
                ? 'You'
                : item.kind === 'operator'
                  ? 'Operator'
                  : 'Agent'
            }
          </span>
          <span>${this.formatTime(item.timestamp)}</span>
          ${
            item.queued ? html`<span class="queue-chip">Queued</span>` : nothing
          }
          ${
            item.redacted
              ? html`<sl-badge variant="warning" pill>Redacted</sl-badge>`
              : nothing
          }
          ${
            item.truncated
              ? html`<sl-badge variant="warning" pill>Truncated</sl-badge>`
              : nothing
          }
          ${
            item.failed
              ? html`<sl-badge variant="danger" pill>Failed</sl-badge>`
              : nothing
          }
        </div>
        ${this.renderClampedText(
          displayText,
          item.key,
          MESSAGE_PREVIEW_CHARS,
          this.expandedMessageKeys.has(item.key),
          'bubble-text'
        )}
      </div>
    `;
  }

  private describeSteps(steps: TranscriptStep[]): string {
    const counts = new Map<string, number>();
    for (const step of steps) {
      const label =
        step.kind === 'tool_call'
          ? 'tool call'
          : step.kind === 'tool_result'
            ? 'tool result'
            : step.kind === 'injected'
              ? 'injected segment'
              : step.kind === 'system'
                ? 'system segment'
                : 'agent step';
      counts.set(label, (counts.get(label) || 0) + 1);
    }
    return Array.from(counts.entries())
      .map(([label, count]) => `${count} ${label}${count === 1 ? '' : 's'}`)
      .join(' · ');
  }

  private renderStep(step: TranscriptStep) {
    const expanded = this.expandedStepKeys.has(step.key);
    const displayText = step.text
      ? step.text
      : step.redacted
        ? 'Content redacted by capture policy.'
        : '';
    return html`
      <div class="step step-kind-${step.kind}">
        <div class="step-header">
          <span class="step-label">${step.label}</span>
          ${step.serverName ? html`<span>${step.serverName}</span>` : nothing}
          <span>${this.formatTime(step.timestamp)}</span>
          ${
            step.status
              ? html`<sl-badge
                  pill
                  variant=${
                    step.status.toLowerCase().includes('fail')
                      ? 'danger'
                      : 'neutral'
                  }
                  >${step.status}</sl-badge
                >`
              : nothing
          }
          ${
            step.kind === 'injected'
              ? html`<sl-badge variant="warning" pill>Injected</sl-badge>`
              : nothing
          }
          ${
            !step.detectionExact
              ? html`<sl-badge
                  variant="neutral"
                  pill
                  title="Classified by pattern, not by message structure"
                >
                  pattern match
                </sl-badge>`
              : nothing
          }
          ${
            displayText.length > STEP_PREVIEW_CHARS
              ? html`
                  <button
                    class="inline-toggle"
                    type="button"
                    @click=${() => this.toggleStep(step.key)}
                  >
                    ${expanded ? 'Show less' : 'Show all'}
                  </button>
                `
              : nothing
          }
        </div>
        ${
          displayText
            ? html`
                <pre class="step-text">
${
  displayText.length > STEP_PREVIEW_CHARS && !expanded
    ? `${displayText.slice(0, STEP_PREVIEW_CHARS)}…`
    : displayText
}</pre>
              `
            : nothing
        }
      </div>
    `;
  }

  private renderStepGroup(item: TranscriptStepGroupItem) {
    return html`
      <sl-details class="steps">
        <span slot="summary" class="steps-summary">
          <sl-icon name="three-dots"></sl-icon>
          ${item.steps.length} step${item.steps.length === 1 ? '' : 's'} —
          ${this.describeSteps(item.steps)}
        </span>
        ${item.steps.map((step) => this.renderStep(step))}
      </sl-details>
    `;
  }

  private renderItem(item: TranscriptItem) {
    if (item.type === 'message') return this.renderMessage(item);
    if (item.type === 'steps') return this.renderStepGroup(item);
    return html`
      <div class="divider">
        ${item.label}
        ${item.timestamp ? html`· ${this.formatTime(item.timestamp)}` : nothing}
      </div>
    `;
  }

  private renderPending(message: PendingTalkMessage) {
    return html`
      <div
        class="bubble operator pending ${message.state === 'failed' ? 'failed' : ''}"
        data-kind="pending"
        data-pending-state=${message.state}
      >
        <div class="bubble-meta">
          <span class="bubble-role">You</span>
          <span>${this.formatTime(message.at)}</span>
          ${
            message.state === 'sending'
              ? html`<sl-badge variant="neutral" pill>Sending</sl-badge>`
              : nothing
          }
          ${
            message.state === 'failed'
              ? html`<sl-badge variant="danger" pill>Not sent</sl-badge>`
              : nothing
          }
        </div>
        <pre class="bubble-text">${message.text}</pre>
        ${
          message.state === 'failed'
            ? html`
                <div class="pending-error">
                  <span>${message.error || 'Failed to send.'}</span>
                  <button
                    class="inline-toggle"
                    type="button"
                    data-testid="pending-retry"
                    @click=${() =>
                      this.dispatchEvent(
                        new CustomEvent(TALK_RETRY_EVENT, {
                          detail: { id: message.id },
                          bubbles: true,
                          composed: true,
                        })
                      )}
                  >
                    Retry
                  </button>
                </div>
              `
            : nothing
        }
      </div>
    `;
  }

  private renderLiveRegion() {
    return html`<div class="live-region" aria-live="polite" role="status">
      ${this.liveAnnouncement}
    </div>`;
  }

  private renderJumpPill() {
    if (!this.scrollable || this.atBottom) return nothing;
    return html`
      <sl-button
        class="jump-latest"
        size="small"
        variant="neutral"
        data-testid="jump-latest"
        @click=${() => this.scrollToLatest()}
      >
        <sl-icon slot="prefix" name="arrow-down"></sl-icon>
        Jump to latest
      </sl-button>
    `;
  }

  render() {
    if (this.loading && !this.events.length && !this.activity.length) {
      return html`
        <div class="loading">
          <sl-spinner></sl-spinner>
          <div>Loading conversation...</div>
        </div>
      `;
    }

    const { items, stats } = this.conversation;
    if (!items.length && !this.pending.length) {
      return html`${this.renderLiveRegion()}
        <div class="empty">${this.emptyText}</div>`;
    }

    return html`
      ${this.renderLiveRegion()}${this.renderJumpPill()}
      <div class="thread">
        <div class="thread-content">
          ${
            this.hasMoreEvents
              ? html`
                  <sl-button
                    class="load-earlier"
                    size="small"
                    ?loading=${this.loadingMoreEvents}
                    @click=${() => this.requestEarlierEvents()}
                  >
                    Load earlier conversation
                  </sl-button>
                `
              : nothing
          }
          ${
            stats.eventsWithoutRawBody > 0
              ? html`
                  <div class="coverage-note" role="note">
                    Message structure was unavailable for
                    ${stats.eventsWithoutRawBody} of ${stats.totalEvents}
                    requests, so some tool results may appear as user prompts
                    there.
                  </div>
                `
              : nothing
          }
          ${
            stats.eventsWithPartialToolResults > 0
              ? html`
                  <div class="coverage-note" role="note">
                    ${stats.eventsWithPartialToolResults} of
                    ${stats.totalEvents} requests carried tool results with no
                    extractable text, so some of those may appear as user
                    prompts.
                  </div>
                `
              : nothing
          }
          ${repeat(
            items,
            (item) => item.key,
            (item) => this.renderItem(item)
          )}
          ${repeat(
            this.pending,
            (message) => message.id,
            (message) => this.renderPending(message)
          )}
        </div>
      </div>
    `;
  }
}
