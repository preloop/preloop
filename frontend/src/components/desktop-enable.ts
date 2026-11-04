/**
 * "Enable desktop" for an agent without a live desktop (#1566).
 *
 * With a live Agent Control connection the API asks the runtime plugin to
 * run `preloop desktop enable` on its host and this element follows the
 * request until the plugin answers. Without one, or when the plugin does
 * not know the command, it shows the one-line command to run on the host.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import {
  DesktopApiError,
  desktopEnableCommand,
  getDesktopInstall,
  requestDesktopInstall,
} from '../desktop-api';

const DOCS_URL =
  'https://docs.preloop.ai/operations/agent-deployment/#live-desktop-view';

export const DESKTOP_ENABLE_STRINGS = {
  enable: 'Enable desktop',
  installing: 'Installing the desktop on the host...',
  done: 'Desktop installed. It appears here once the host connects.',
  runOnHost: 'Run this on the agent host to enable a live desktop:',
  unsupportedPlugin:
    "This agent's plugin cannot install a desktop. Run this on the host instead:",
  failed: {
    desktop_unsupported_os:
      'The host is not Linux, so no desktop was installed.',
    desktop_unsupported_distro:
      'Desktops install on Debian or Ubuntu only. The agent is unaffected.',
    desktop_privilege_required:
      'Installing needs root or passwordless sudo on the host. The agent is unaffected.',
    desktop_failed:
      'The desktop install failed on the host. The agent is unaffected.',
    desktop_timed_out:
      'The host has not reported back. Check the agent host, or run the command there.',
  } as Record<string, string>,
  docs: 'Desktop setup',
};

type Phase = 'idle' | 'installing' | 'done' | 'failed' | 'command';

@customElement('desktop-enable')
export class DesktopEnableElement extends LitElement {
  @property({ type: String }) agentId = '';

  /** Whether the agent has a live Agent Control connection. */
  @property({ type: Boolean }) controlOnline = false;

  /** How often a running install is re-checked. */
  @property({ attribute: false }) pollMs = 3000;

  /** Give up following after this long (the install itself takes minutes). */
  @property({ attribute: false }) followMs = 20 * 60 * 1000;

  @state() private phase: Phase = 'idle';
  @state() private error: string | null = null;
  @state() private unsupported = false;

  private poll: number | null = null;

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
      font-size: var(--console-text-body, 14px);
    }
    .note {
      color: var(--console-meta-color);
      display: inline-flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
    }
    .command-row {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
    }
    code {
      flex: 1;
      font-family: var(--sl-font-mono);
      font-size: var(--console-text-meta, 13px);
      overflow-wrap: anywhere;
      user-select: all;
    }
    code::before {
      content: '$ ';
      color: var(--console-meta-color);
      user-select: none;
    }
  `;

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopPoll();
  }

  private stopPoll(): void {
    if (this.poll !== null) {
      window.clearInterval(this.poll);
      this.poll = null;
    }
  }

  private async start(): Promise<void> {
    this.stopPoll();
    this.phase = 'installing';
    this.error = null;
    let requestId: string;
    try {
      requestId = (await requestDesktopInstall(this.agentId)).request_id;
    } catch {
      // No control channel (409) or the request failed: the command works
      // whatever the plugin supports. "Unsupported plugin" is only said
      // when the plugin itself answered that way.
      this.unsupported = false;
      this.phase = 'command';
      return;
    }
    if (!this.isConnected) return;
    const deadline = Date.now() + this.followMs;
    const giveUp = (error: string) => {
      this.stopPoll();
      this.error = error;
      this.phase = 'failed';
    };
    this.poll = window.setInterval(async () => {
      if (Date.now() > deadline) {
        giveUp('desktop_timed_out');
        return;
      }
      try {
        const status = await getDesktopInstall(this.agentId, requestId);
        if (status.state === 'pending') return;
        this.stopPoll();
        if (status.state === 'done') {
          this.phase = 'done';
          this.dispatchEvent(
            new CustomEvent('desktop-installed', {
              bubbles: true,
              composed: true,
            })
          );
        } else if (status.state === 'unsupported') {
          this.unsupported = true;
          this.phase = 'command';
        } else {
          this.error = status.error || 'desktop_failed';
          this.phase = 'failed';
        }
      } catch (error) {
        // 403/404 will not change on retry; anything else is transient.
        if (
          error instanceof DesktopApiError &&
          (error.status === 403 || error.status === 404)
        ) {
          giveUp('desktop_failed');
        }
      }
    }, this.pollMs);
  }

  private renderCommand(lead: string) {
    const command = desktopEnableCommand(this.agentId);
    return html`<span class="note">${lead}</span>
      <div class="command-row" data-testid="desktop-enable-command">
        <code>${command}</code>
        <sl-copy-button .value=${command}></sl-copy-button>
      </div>`;
  }

  render() {
    const s = DESKTOP_ENABLE_STRINGS;
    if (!this.agentId) return nothing;
    if (!this.controlOnline || this.phase === 'command') {
      return this.renderCommand(
        this.unsupported ? s.unsupportedPlugin : s.runOnHost
      );
    }
    switch (this.phase) {
      case 'installing':
        return html`<span
          class="note"
          role="status"
          data-testid="desktop-enable-status"
          ><sl-spinner></sl-spinner>${s.installing}</span
        >`;
      case 'done':
        return html`<span
          class="note"
          role="status"
          data-testid="desktop-enable-status"
          >${s.done}</span
        >`;
      case 'failed':
        return html`<span
            class="note"
            role="status"
            data-testid="desktop-enable-status"
            >${s.failed[this.error ?? ''] ?? s.failed.desktop_failed}
            <a href=${DOCS_URL} target="_blank" rel="noopener"
              >${s.docs}</a
            ></span
          >
          <div>
            <sl-button size="small" @click=${() => void this.start()}
              >${s.enable}</sl-button
            >
          </div>`;
      default:
        return html`<div>
          <sl-button
            class="enable"
            size="small"
            @click=${() => void this.start()}
          >
            <sl-icon slot="prefix" name="display"></sl-icon>
            ${s.enable}
          </sl-button>
        </div>`;
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'desktop-enable': DesktopEnableElement;
  }
}
