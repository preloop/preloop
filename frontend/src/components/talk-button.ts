/**
 * The Talk button, wherever an agent is listed.
 *
 * It used to open a dialog over the current page (`agent-talk-composer`).
 * Wave 5 retired that dialog: a conversation you have to keep open while you
 * read the rest of the console is a window, not a modal. The button is now a
 * thin thing that opens one, and every entry point renders the same element so
 * the enabled/disabled rules and the tooltip cannot drift between pages.
 */
import { LitElement, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import type { ManagedAgentSummary, RuntimeSessionSummary } from '../types';
import { getAgentControlState } from '../utils/agent-control';
import { openTalkWindow } from '../utils/talk-window';

@customElement('talk-button')
export class TalkButton extends LitElement {
  @property({ attribute: false })
  agent: ManagedAgentSummary | null = null;

  /** Talk about one session in particular, rather than the latest one. */
  @property({ attribute: false })
  session: RuntimeSessionSummary | { id: string } | null = null;

  /** Row density: small and neutral in a list, primary on a detail page. */
  @property({ type: Boolean })
  compact = false;

  @property({ type: Boolean })
  disabled = false;

  /** Render even when the agent kind has no Agent Control at all. */
  @property({ type: Boolean, attribute: 'always-visible' })
  alwaysVisible = false;

  /**
   * Where this button lives ('dashboard-active-agents', 'agent-detail-view',
   * ...). It travels to the talk window in the URL and ends up on every turn
   * as `requested_from`, so the audit trail keeps naming the entry point the
   * retired dialog used to record.
   */
  @property({ type: String, attribute: 'source-context' })
  sourceContext: string | null = null;

  createRenderRoot() {
    // Light DOM: the button inherits the surrounding row's density and the
    // host page's tooltip stacking, exactly as the old composer button did.
    return this;
  }

  private open(): void {
    if (!this.agent) return;
    // Synchronous inside the click handler: a `window.open` after an await is
    // no longer trusted by the browser and gets blocked.
    openTalkWindow(this.agent, this.session ?? undefined, {
      sourceContext: this.sourceContext,
    });
  }

  render() {
    const agent = this.agent;
    if (!agent) return nothing;
    const controlState = getAgentControlState(agent);
    if (!controlState.visible && !this.alwaysVisible) return nothing;
    return html`
      <sl-tooltip content=${controlState.detail}>
        <sl-button
          size=${this.compact ? 'small' : 'medium'}
          variant=${!this.compact && controlState.online ? 'primary' : 'default'}
          data-testid="talk-button"
          ?disabled=${!controlState.enabled || this.disabled}
          @click=${() => this.open()}
        >
          <sl-icon slot="prefix" name="chat-dots"></sl-icon>
          Talk
        </sl-button>
      </sl-tooltip>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talk-button': TalkButton;
  }
}
