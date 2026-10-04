import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  getKillSwitchStatus,
  activateKillSwitch,
  deactivateKillSwitch,
} from '../../../api';
import type {
  KillSwitchScope,
  KillSwitchScopeState,
  KillSwitchStatus,
} from '../../../types';
import consoleStyles from '../../../styles/console-styles.css?inline';
import '../../../components/view-header.ts';
import { confirmDialog } from '../../../components/confirm-dialog';
import { formatRelativeTime, parseUTCDate } from '../../../utils/date';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

/**
 * Emergency controls: the account kill switch (#157).
 *
 * Its own page, and not a card halfway down the account page, for one
 * reason: the person who needs it is in a hurry and is not reading. A page
 * with one purpose can be reached from the sidebar, linked to from a runbook
 * and opened without scrolling past an organisation name field, a
 * subscription and a usage table.
 *
 * Nothing here is billing. The kill switch is part of the open-source
 * console and works identically without the billing plugin, so this page
 * carries no plan gate.
 */
@customElement('emergency-view')
export class EmergencyView extends LitElement {
  @state() private _haltStatus: KillSwitchStatus | null = null;
  /**
   * Whether `_haltStatus` can be trusted. Until the first read succeeds the
   * page does not know whether agents are halted, and it must not show the
   * "not halted" form: an operator would halt twice, or believe a halted
   * account is live.
   */
  @state() private _statusState: 'loading' | 'unknown' | 'known' = 'loading';
  @state() private _haltReason = '';
  @state() private _haltBusy = false;
  @state() private _haltError: string | null = null;

  private static readonly HALT_SCOPE_LABELS: Record<KillSwitchScope, string> = {
    gateway: 'Model requests',
    tools: 'Tool calls',
    flows: 'Flow executions',
  };

  async connectedCallback() {
    super.connectedCallback();
    await this._refreshHaltStatus();
  }

  /**
   * Reload kill-switch state. A failed refresh keeps a state already read;
   * with nothing read yet the state is unknown, never "not halted".
   */
  private async _refreshHaltStatus() {
    if (!this._haltStatus) this._statusState = 'loading';
    try {
      this._haltStatus = await getKillSwitchStatus();
      this._statusState = 'known';
    } catch (error) {
      console.warn('Failed to read the kill switch status:', error);
      if (!this._haltStatus) this._statusState = 'unknown';
    }
  }

  private async _confirmHalt() {
    const scopes = Object.values(EmergencyView.HALT_SCOPE_LABELS)
      .map((label) => `• ${label}`)
      .join('\n');
    const confirmed = await confirmDialog({
      title: 'Halt agent activity?',
      message: `This blocks, for the whole account, within five seconds:\n${scopes}`,
      detail:
        'Running managed flows receive stop requests. You can resume each scope separately afterwards.',
      confirmLabel: 'Halt now',
      variant: 'danger',
    });
    if (confirmed) await this._handleHalt();
  }

  private async _handleHalt() {
    this._haltBusy = true;
    this._haltError = null;
    try {
      this._haltStatus = await activateKillSwitch({
        reason: this._haltReason.trim() || null,
      });
      this._statusState = 'known';
      this._haltReason = '';
      this.dispatchEvent(
        new CustomEvent('kill-switch-changed', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this._haltError =
        (error as Error).message || 'Failed to activate the halt.';
    } finally {
      this._haltBusy = false;
    }
  }

  private async _handleResume(scopes: KillSwitchScope[]) {
    this._haltBusy = true;
    this._haltError = null;
    try {
      this._haltStatus = await deactivateKillSwitch({
        scopes,
        reason: this._haltReason.trim() || null,
      });
      this._statusState = 'known';
      this.dispatchEvent(
        new CustomEvent('kill-switch-changed', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this._haltError = (error as Error).message || 'Failed to lift the halt.';
    } finally {
      this._haltBusy = false;
    }
  }

  render() {
    return html`
      <view-header headerText="Emergency" width="narrow"></view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <sl-card style="margin-bottom: 2rem;">
            ${
              this._haltError
                ? html`
                    <sl-alert variant="danger" open closable>
                      <sl-icon
                        slot="icon"
                        name="exclamation-triangle"
                      ></sl-icon>
                      ${this._haltError}
                    </sl-alert>
                  `
                : ''
            }
            ${
              this._statusState === 'loading'
                ? this._renderLoading()
                : this._statusState === 'unknown'
                  ? this._renderUnknown()
                  : this._haltStatus?.active
                    ? html`
                        <div
                          style="display: flex; flex-direction: column; gap: 0.75rem;"
                        >
                          <div>
                            <strong style="color: var(--sl-color-danger-600);">
                              Agent activity is halted.
                            </strong>
                            The following traffic is rejected until the halt is
                            lifted:
                          </div>
                          <ul class="scope-list">
                            ${this._haltStatus.scopes.map(
                              (entry) => html`
                                <li>
                                  <span class="status-chip pending">
                                    ${EmergencyView.HALT_SCOPE_LABELS[entry.scope]}
                                    blocked
                                  </span>
                                  ${this._renderAttribution(entry)}
                                </li>
                              `
                            )}
                          </ul>
                          ${
                            this._haltStatus.scopes.find((s) => s.reason)
                              ?.reason
                              ? html`
                                  <div>
                                    Reason:
                                    ${
                                      this._haltStatus.scopes.find(
                                        (s) => s.reason
                                      )!.reason
                                    }
                                  </div>
                                `
                              : ''
                          }
                          <sl-input
                            label="Recovery reason"
                            maxlength="500"
                            value=${this._haltReason}
                            @sl-input=${(e: any) => (this._haltReason = e.target.value)}
                            ?disabled=${this._haltBusy}
                          ></sl-input>
                          <div
                            style="display: flex; flex-wrap: wrap; gap: 0.5rem;"
                          >
                            ${this._haltStatus.scopes.map(
                              (entry) => html`
                                <sl-button
                                  size="small"
                                  outline
                                  ?disabled=${this._haltBusy}
                                  @click=${() => this._handleResume([entry.scope])}
                                >
                                  Resume
                                  ${EmergencyView.HALT_SCOPE_LABELS[
                                    entry.scope
                                  ].toLowerCase()}
                                </sl-button>
                              `
                            )}
                            ${
                              this._haltStatus.scopes.length > 1
                                ? html`
                                    <sl-button
                                      size="small"
                                      variant="primary"
                                      ?disabled=${this._haltBusy}
                                      @click=${() =>
                                        this._handleResume(
                                          this._haltStatus!.scopes.map(
                                            (entry) => entry.scope
                                          )
                                        )}
                                    >
                                      Resume all
                                    </sl-button>
                                  `
                                : ''
                            }
                          </div>
                          <div class="more">
                            Staged recovery: restore model requests first and
                            verify behavior, then tool calls, then flow
                            executions.
                          </div>
                        </div>
                      `
                    : html`
                        <div
                          style="display: flex; flex-direction: column; gap: 0.75rem;"
                        >
                          <div>
                            The kill switch blocks new model requests, MCP tool
                            calls, and flow starts for this account within five
                            seconds. Managed flow executions receive stop
                            requests; termination is confirmed separately.
                            Activation is audited.
                          </div>
                          <sl-textarea
                            label="Reason (recorded for audit)"
                            placeholder="What is going wrong?"
                            value=${this._haltReason}
                            @sl-input=${(e: any) =>
                              (this._haltReason = e.target.value)}
                            ?disabled=${this._haltBusy}
                          ></sl-textarea>
                          <div>
                            <sl-button
                              variant="danger"
                              ?loading=${this._haltBusy}
                              @click=${this._confirmHalt}
                            >
                              Block new agent requests
                            </sl-button>
                          </div>
                        </div>
                      `
            }
          </sl-card>
        </div>
      </div>
    `;
  }

  private _renderLoading() {
    return html`
      <div class="status-loading" role="status" aria-live="polite">
        <sl-spinner></sl-spinner>
        Checking whether agent activity is halted…
      </div>
    `;
  }

  private _renderUnknown() {
    return html`
      <sl-alert variant="warning" open role="alert">
        <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
        <strong>Couldn't read the halt status.</strong>
        Agent activity may or may not be halted right now.
        <div class="unknown-actions">
          <sl-button size="small" @click=${() => this._refreshHaltStatus()}>
            <sl-icon slot="prefix" name="arrow-clockwise"></sl-icon>
            Retry
          </sl-button>
          <sl-button
            size="small"
            variant="danger"
            outline
            ?loading=${this._haltBusy}
            @click=${this._confirmHalt}
          >
            Halt anyway
          </sl-button>
        </div>
      </sl-alert>
    `;
  }

  /** "Halted by Jane Doe · 3m ago", with the exact time on hover. */
  private _renderAttribution(entry: KillSwitchScopeState) {
    const parts: string[] = [];
    if (entry.activated_by_username) {
      parts.push(`Halted by ${entry.activated_by_username}`);
    }
    let exact = '';
    if (entry.activated_at) {
      const date = parseUTCDate(entry.activated_at);
      if (!Number.isNaN(date.getTime())) {
        exact = date.toLocaleString();
        parts.push(formatRelativeTime(entry.activated_at));
      }
    }
    if (parts.length === 0) return nothing;
    if (!entry.activated_by_username) parts[0] = `Halted ${parts[0]}`;
    return html`<span class="scope-meta" title=${exact || nothing}
      >${parts.join(' · ')}</span
    >`;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .status-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.25rem 0.5rem;
        border-radius: 999px;
        background: var(--sl-color-neutral-200);
        color: var(--sl-color-neutral-800);
        font-weight: 600;
        font-size: 0.85rem;
      }

      .status-chip.pending {
        background: var(--sl-color-warning-200);
        color: var(--sl-color-warning-800);
      }

      .more {
        color: var(--sl-color-neutral-600);
        font-size: 0.95rem;
      }

      .scope-list {
        list-style: none;
        margin: 0;
        padding: 0;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }

      .scope-list li {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.5rem;
      }

      .scope-meta {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .status-loading {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        color: var(--sl-color-neutral-600);
      }

      .unknown-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-small);
      }
    `,
  ];
}

declare global {
  interface HTMLElementTagNameMap {
    'emergency-view': EmergencyView;
  }
}
