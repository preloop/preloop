/**
 * "Talking" chips in the console header: one per open talk window.
 *
 * A popup window is easy to lose behind the browser, and an operator who has
 * asked an agent to do something will go back to reading the console while it
 * works. The chip is how they find the conversation again and how they learn
 * the agent has answered, without the window having to steal focus.
 *
 * There is no pulse: a chip that animates on a page the operator is trying to
 * read is a distraction, and the unread dot already says what changed.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import {
  TALK_HEARTBEAT_MS,
  TalkWindowRegistry,
  openTalkChannel,
  type TalkChannelMessage,
  type TalkWindowEntry,
} from '../utils/talk-channel';
import { openTalkWindow } from '../utils/talk-window';

@customElement('talking-indicator')
export class TalkingIndicator extends LitElement {
  @state() private entries: TalkWindowEntry[] = [];

  private registry = new TalkWindowRegistry();
  private channel: BroadcastChannel | null = null;
  private pruneTimer: number | null = null;

  static styles = css`
    :host {
      align-items: center;
      display: flex;
      gap: var(--sl-spacing-2x-small);
    }

    .chip {
      align-items: center;
      background: var(--console-hover-tint, var(--sl-color-neutral-100));
      border: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      border-radius: 999px;
      color: var(--console-body-color, var(--sl-color-neutral-800));
      cursor: pointer;
      display: inline-flex;
      font: inherit;
      font-size: var(--console-text-meta, 13px);
      gap: var(--sl-spacing-2x-small);
      max-width: 12rem;
      padding: 2px 10px;
    }

    .chip:hover {
      background: var(--sl-color-neutral-200);
    }

    .name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .unread {
      background: var(--sl-color-primary-600);
      border-radius: 50%;
      flex-shrink: 0;
      height: 7px;
      width: 7px;
    }

    @media (max-width: 640px) {
      .name {
        display: none;
      }
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    this.channel = openTalkChannel();
    if (this.channel) {
      this.channel.addEventListener('message', this.handleMessage);
    }
    this.pruneTimer = window.setInterval(() => {
      if (this.registry.prune()) this.entries = this.registry.entries;
    }, TALK_HEARTBEAT_MS);
  }

  disconnectedCallback(): void {
    this.channel?.removeEventListener('message', this.handleMessage);
    this.channel?.close();
    this.channel = null;
    if (this.pruneTimer !== null) window.clearInterval(this.pruneTimer);
    super.disconnectedCallback();
  }

  private handleMessage = (event: MessageEvent<TalkChannelMessage>): void => {
    if (this.registry.apply(event.data)) {
      this.entries = this.registry.entries;
    }
  };

  /** Test seam: feed the registry without a real channel. */
  public receive(message: TalkChannelMessage): void {
    if (this.registry.apply(message)) this.entries = this.registry.entries;
  }

  /** Test seam: run the staleness sweep on demand. */
  public pruneNow(now = Date.now()): void {
    if (this.registry.prune(now)) this.entries = this.registry.entries;
  }

  private focusWindow(entry: TalkWindowEntry): void {
    if (this.registry.clearUnread(entry.agentId)) {
      this.entries = this.registry.entries;
    }
    // The console may not have opened this window itself (another tab did),
    // in which case `openTalkWindow` re-opens it by name and the browser
    // hands back the existing one.
    const result = openTalkWindow(
      { id: entry.agentId, display_name: entry.agentName },
      entry.sessionId
    );
    if (result.outcome === 'blocked') {
      this.registry.drop(entry.agentId);
      this.entries = this.registry.entries;
    }
  }

  render() {
    if (!this.entries.length) return nothing;
    return html`
      ${this.entries.map(
        (entry) => html`
          <sl-tooltip content=${`Talking to ${entry.agentName}`}>
            <button
              class="chip"
              type="button"
              data-testid="talking-chip"
              data-agent-id=${entry.agentId}
              @click=${() => this.focusWindow(entry)}
            >
              ${
                entry.unread
                  ? html`<span
                      class="unread"
                      data-testid="talking-unread"
                      aria-label="New message"
                    ></span>`
                  : nothing
              }
              <span class="name">${entry.agentName}</span>
            </button>
          </sl-tooltip>
        `
      )}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talking-indicator': TalkingIndicator;
  }
}
