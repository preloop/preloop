import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import type { Tool } from './tool-card.ts';
import {
  getTools,
  createToolConfiguration,
  updateToolConfiguration,
} from '../api';

interface UnlockRow {
  tool: Tool;
  keepEnabled: boolean;
}

/**
 * Review dialog shown after a tracker unlocks builtin tools.
 * Toggles default ON (today's behavior). Confirm persists only opt-outs.
 */
@customElement('unlocked-tools-review-dialog')
export class UnlockedToolsReviewDialog extends LitElement {
  @property({ type: Boolean }) open = false;

  @property({ type: Array }) toolNames: string[] = [];

  @state() private _rows: UnlockRow[] = [];
  @state() private _loading = false;
  @state() private _saving = false;
  @state() private _error: string | null = null;

  /** Injectable for tests. */
  _api = {
    getTools,
    createToolConfiguration,
    updateToolConfiguration,
  };

  static styles = css`
    :host {
      display: contents;
    }

    .intro {
      margin: 0 0 1rem;
      color: var(--sl-color-neutral-700);
      font-size: 0.875rem;
      line-height: 1.45;
    }

    .tool-list {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      max-height: min(50vh, 360px);
      overflow-y: auto;
    }

    .tool-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 0.25rem 1rem;
      align-items: center;
      padding: 0.65rem 0.75rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 4px;
      background: var(--sl-color-neutral-0);
    }

    .tool-row.disabled {
      opacity: 0.65;
    }

    .tool-name {
      font-weight: 600;
      font-size: 0.875rem;
      font-family: var(--sl-font-mono, ui-monospace, monospace);
    }

    .tool-desc {
      grid-column: 1 / 2;
      font-size: 0.8rem;
      color: var(--sl-color-neutral-600);
      line-height: 1.35;
    }

    .tool-meta {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      grid-column: 2 / 3;
      grid-row: 1 / 3;
    }

    .schema-tokens {
      font-size: 0.75rem;
      color: var(--sl-color-neutral-600);
      white-space: nowrap;
    }

    .context-tax {
      margin-top: 0.85rem;
      font-size: 0.875rem;
      color: var(--sl-color-neutral-700);
    }

    .context-tax strong {
      font-weight: 600;
    }

    .dialog-footer {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem;
      justify-content: flex-end;
    }

    .review-later {
      margin-right: auto;
      font-size: 0.875rem;
      color: var(--sl-color-primary-600);
      text-decoration: underline;
      background: none;
      border: none;
      padding: 0;
      cursor: pointer;
    }

    .review-later:hover {
      color: var(--sl-color-primary-700);
    }

    .loading,
    .empty {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 1rem 0;
      color: var(--sl-color-neutral-600);
      font-size: 0.875rem;
    }
  `;

  updated(changed: Map<string, unknown>) {
    if (changed.has('open') && !this.open) {
      // Defer state reset so we do not schedule during this update cycle.
      queueMicrotask(() => {
        if (!this.open) {
          this._rows = [];
          this._error = null;
          this._saving = false;
        }
      });
      return;
    }
    if (
      (changed.has('open') || changed.has('toolNames')) &&
      this.open &&
      this.toolNames.length > 0
    ) {
      queueMicrotask(() => {
        if (this.open && this.toolNames.length > 0) {
          void this._loadTools();
        }
      });
    }
  }

  private async _loadTools() {
    this._loading = true;
    this._error = null;
    try {
      const tools = (await this._api.getTools()) as Tool[];
      const byName = new Map(tools.map((t) => [t.name, t]));
      this._rows = this.toolNames
        .map((name) => {
          const tool = byName.get(name);
          if (!tool) {
            return null;
          }
          return { tool, keepEnabled: true };
        })
        .filter((row): row is UnlockRow => row !== null);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Failed to load unlocked tools';
      this._error = message;
      this._rows = [];
    } finally {
      this._loading = false;
    }
  }

  private _selectedTokens(): number {
    return this._rows.reduce((sum, row) => {
      if (!row.keepEnabled) {
        return sum;
      }
      const tokens = row.tool.schema_tokens_estimate;
      return sum + (typeof tokens === 'number' && tokens > 0 ? tokens : 0);
    }, 0);
  }

  private _handleToggle(name: string, checked: boolean) {
    this._rows = this._rows.map((row) =>
      row.tool.name === name ? { ...row, keepEnabled: checked } : row
    );
  }

  private _handleRequestClose(event: CustomEvent) {
    // Allow escape / overlay dismiss without persisting opt-outs.
    if (this._saving) {
      event.preventDefault();
      return;
    }
    this._close();
  }

  private _close() {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true })
    );
  }

  private _reviewLater(event: Event) {
    event.preventDefault();
    this._close();
    Router.go('/console/tools');
  }

  private async _confirm() {
    const toDisable = this._rows.filter((row) => !row.keepEnabled);
    if (toDisable.length === 0) {
      this._close();
      return;
    }

    this._saving = true;
    this._error = null;
    try {
      for (const row of toDisable) {
        const tool = row.tool;
        if (tool.config_id) {
          await this._api.updateToolConfiguration(tool.config_id, {
            is_enabled: false,
          });
        } else {
          await this._api.createToolConfiguration({
            tool_name: tool.name,
            tool_source: 'builtin',
            mcp_server_id: tool.source_id,
            is_enabled: false,
            // Placeholder: POST /tool-configurations overrides account_id from
            // the authenticated session (same pattern as tools-view toggles).
            account_id: '',
          });
        }
      }
      this.dispatchEvent(
        new CustomEvent('tools-reviewed', {
          bubbles: true,
          composed: true,
          detail: { disabled: toDisable.map((r) => r.tool.name) },
        })
      );
      this._close();
    } catch (err: unknown) {
      const message =
        err instanceof Error
          ? err.message
          : 'Failed to save tool configuration';
      this._error = message;
    } finally {
      this._saving = false;
    }
  }

  render() {
    const selectedTokens = this._selectedTokens();
    return html`
      <sl-dialog
        label="Review newly unlocked tools"
        ?open=${this.open}
        @sl-request-close=${this._handleRequestClose}
      >
        <p class="intro">
          Connecting this tracker unlocked the tools below. They will stay
          enabled unless you turn them off. Deselected tools are disabled for
          your account (you can re-enable them later on the Tools page).
        </p>

        ${
          this._error
            ? html`<sl-alert variant="danger" open> ${this._error} </sl-alert>`
            : ''
        }
        ${
          this._loading
            ? html`<div class="loading">
                <sl-spinner></sl-spinner>
                Loading tool costs…
              </div>`
            : this._rows.length === 0
              ? html`<div class="empty">No unlocked tools to review.</div>`
              : html`
                  <div class="tool-list">
                    ${this._rows.map(
                      (row) => html`
                        <div
                          class="tool-row ${row.keepEnabled ? '' : 'disabled'}"
                        >
                          <span class="tool-name">${row.tool.name}</span>
                          <div class="tool-meta">
                            ${
                              typeof row.tool.schema_tokens_estimate ===
                                'number' && row.tool.schema_tokens_estimate > 0
                                ? html`<span class="schema-tokens"
                                    >~${row.tool.schema_tokens_estimate.toLocaleString()}
                                    tokens/request</span
                                  >`
                                : ''
                            }
                            <sl-switch
                              ?checked=${row.keepEnabled}
                              @sl-change=${(e: Event) => {
                                const target = e.target as HTMLInputElement & {
                                  checked?: boolean;
                                };
                                this._handleToggle(
                                  row.tool.name,
                                  Boolean(target.checked)
                                );
                              }}
                            ></sl-switch>
                          </div>
                          <div class="tool-desc">${row.tool.description}</div>
                        </div>
                      `
                    )}
                  </div>
                  ${
                    selectedTokens > 0
                      ? html`<div class="context-tax">
                          Keeping these enabled adds
                          <strong
                            >~${selectedTokens.toLocaleString()} tokens</strong
                          >
                          to every agent request
                        </div>`
                      : html`<div class="context-tax">
                          No enabled tools selected — no added context tax from
                          this unlock.
                        </div>`
                  }
                `
        }

        <div slot="footer" class="dialog-footer">
          <button
            type="button"
            class="review-later"
            @click=${this._reviewLater}
            ?disabled=${this._saving}
          >
            Review later
          </button>
          <sl-button @click=${this._close} ?disabled=${this._saving}
            >Dismiss</sl-button
          >
          <sl-button
            variant="primary"
            @click=${this._confirm}
            ?loading=${this._saving}
            ?disabled=${this._loading}
          >
            Confirm
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'unlocked-tools-review-dialog': UnlockedToolsReviewDialog;
  }
}
