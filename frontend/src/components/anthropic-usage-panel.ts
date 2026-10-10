import { formatCurrencyAmount } from '../utils/money';
import { tableScrollStyles } from '../styles/table-scroll';
import { parseUTCDate } from '../utils/date';
import { html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  AuthedElement,
  deleteAnthropicConnection,
  getAnthropicUsage,
  saveAnthropicConnection,
  syncAnthropicUsage,
} from '../api';
import type {
  AnthropicConnectionUpsert,
  AnthropicUsageSummary,
} from '../types';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';

/** Shown next to every imported Anthropic figure. */
export const ANTHROPIC_NOT_METERED = 'Not metered by the gateway';

/**
 * Cost page section for Claude Code usage imported from the Anthropic Admin
 * API (Claude Code Analytics). The figures are daily estimates for usage that
 * did not pass through the gateway. Usage on keys Preloop itself uses as
 * upstream credentials is shown separately and never added to the total.
 */
@customElement('anthropic-usage-panel')
export class AnthropicUsagePanel extends AuthedElement {
  @property({ attribute: false }) startDate?: string;
  @property({ attribute: false }) endDate?: string;

  @state() private summary: AnthropicUsageSummary | null = null;
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private actionError: string | null = null;
  @state() private notice: string | null = null;
  @state() private editing = false;
  @state() private saving = false;
  @state() private syncing = false;
  @state() private formKey = '';
  @state() private formKeyNames = '';
  private loadSeq = 0;

  static styles = [
    tableScrollStyles,
    css`
      :host {
        display: block;
        margin-top: var(--sl-spacing-x-large);
      }
      .panel {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }
      .header {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
      }
      .header h3 {
        margin: 0;
        font-size: var(--sl-font-size-large);
      }
      .muted {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }
      .stats {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: var(--sl-spacing-small);
      }
      .stat {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-small);
      }
      .stat-label {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
        text-transform: uppercase;
      }
      .stat-value {
        font-size: var(--sl-font-size-large);
        font-weight: 600;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--sl-font-size-small);
      }
      th,
      td {
        text-align: left;
        padding: 4px 8px;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }
      td.num,
      th.num {
        text-align: right;
      }
      .form-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: var(--sl-spacing-small);
        align-items: end;
      }
      .actions {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
      }
    `,
  ];

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('startDate') || changed.has('endDate')) {
      void this.load();
    }
  }

  async load(): Promise<void> {
    const seq = ++this.loadSeq;
    this.loading = true;
    this.error = null;
    try {
      const summary = await getAnthropicUsage({
        startDate: this.startDate,
        endDate: this.endDate,
      });
      if (seq !== this.loadSeq) return;
      this.summary = summary;
      if (!summary.connection) this.editing = true;
    } catch (error) {
      if (seq !== this.loadSeq) return;
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not load Anthropic usage';
    } finally {
      if (seq === this.loadSeq) this.loading = false;
    }
  }

  private formatNumber(value: number): string {
    return new Intl.NumberFormat(undefined, {
      maximumFractionDigits: 0,
    }).format(value);
  }

  private startEditing(): void {
    this.formKey = '';
    this.formKeyNames = (
      this.summary?.connection?.gateway_key_names ?? []
    ).join(', ');
    this.actionError = null;
    this.editing = true;
  }

  private async save(): Promise<void> {
    this.actionError = null;
    this.notice = null;
    const payload: AnthropicConnectionUpsert = {
      gateway_key_names: this.formKeyNames
        .split(',')
        .map((name) => name.trim())
        .filter(Boolean),
    };
    if (this.formKey) payload.admin_key = this.formKey;
    this.saving = true;
    try {
      await saveAnthropicConnection(payload);
      this.editing = false;
      this.formKey = '';
      await this.load();
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not save';
    } finally {
      this.saving = false;
    }
  }

  private async sync(): Promise<void> {
    this.actionError = null;
    this.notice = null;
    this.syncing = true;
    try {
      await syncAnthropicUsage();
      this.notice =
        'Import queued. Days up to yesterday (UTC) are imported, catching up at most seven missed days.';
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not queue the import';
    } finally {
      this.syncing = false;
    }
  }

  private async setActive(isActive: boolean): Promise<void> {
    this.actionError = null;
    try {
      await saveAnthropicConnection({ is_active: isActive });
      await this.load();
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not update';
    }
  }

  private async removeConnection(): Promise<void> {
    this.actionError = null;
    this.notice = null;
    try {
      await deleteAnthropicConnection();
      await this.load();
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not remove';
    }
  }

  private renderMarker() {
    return html`<sl-badge variant="neutral" data-testid="anthropic-marker"
      >${this.summary?.marker || ANTHROPIC_NOT_METERED}</sl-badge
    >`;
  }

  private renderForm() {
    const connection = this.summary?.connection;
    return html`
      <div class="form" data-testid="anthropic-form">
        <p class="muted">
          Use an Anthropic Admin API key dedicated to Preloop. It is stored
          encrypted and never shown again; only its last four characters are
          displayed. Keys that Preloop uses as upstream credentials are found
          automatically; list any other key names whose usage the gateway
          already meters.
        </p>
        <div class="form-grid">
          <sl-input
            label=${connection ? 'New Admin API key (optional)' : 'Admin API key'}
            name="admin_key"
            type="password"
            password-toggle
            .value=${this.formKey}
            @sl-input=${(event: Event) =>
              (this.formKey = (event.target as HTMLInputElement).value)}
          ></sl-input>
          <sl-input
            label="Gateway key names (optional, comma separated)"
            name="gateway_key_names"
            .value=${this.formKeyNames}
            @sl-input=${(event: Event) =>
              (this.formKeyNames = (event.target as HTMLInputElement).value)}
          ></sl-input>
        </div>
        <div class="actions" style="margin-top: var(--sl-spacing-small);">
          <sl-button
            variant="primary"
            data-testid="anthropic-save"
            .loading=${this.saving}
            ?disabled=${!connection && !this.formKey}
            @click=${() => void this.save()}
            >${connection ? 'Save' : 'Connect Anthropic'}</sl-button
          >
          ${
            connection
              ? html`<sl-button @click=${() => (this.editing = false)}
                  >Cancel</sl-button
                >`
              : nothing
          }
        </div>
      </div>
    `;
  }

  private renderStatus() {
    const connection = this.summary?.connection;
    if (!connection) return nothing;
    const lastSynced = connection.last_synced_at
      ? parseUTCDate(connection.last_synced_at).toLocaleString()
      : 'never';
    return html`
      <div class="muted" data-testid="anthropic-status">
        Admin key ending in <strong>${connection.key_hint ?? '????'}</strong>.
        Last import:
        ${lastSynced}${
          connection.last_synced_day
            ? html`, data through ${connection.last_synced_day}`
            : nothing
        }.
      </div>
      ${
        connection.last_error
          ? html`<sl-alert variant="danger" open data-testid="anthropic-error">
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              The last import failed: ${connection.last_error}
            </sl-alert>`
          : nothing
      }
      ${
        connection.last_warning
          ? html`<sl-alert
              variant="warning"
              open
              data-testid="anthropic-warning"
            >
              <sl-icon slot="icon" name="info-circle"></sl-icon>
              ${connection.last_warning}
            </sl-alert>`
          : nothing
      }
      <div class="actions">
        ${
          connection.is_active
            ? html`<sl-button
                size="small"
                data-testid="anthropic-sync"
                .loading=${this.syncing}
                @click=${() => void this.sync()}
                >Sync now</sl-button
              >`
            : html`<sl-button
                size="small"
                variant="primary"
                data-testid="anthropic-resume"
                @click=${() => void this.setActive(true)}
                >Resume imports</sl-button
              >`
        }
        <sl-button size="small" @click=${() => this.startEditing()}
          >Edit connection</sl-button
        >
        <sl-button
          size="small"
          variant="text"
          @click=${() => void this.removeConnection()}
          >Remove connection</sl-button
        >
      </div>
    `;
  }

  private renderUsage(summary: AnthropicUsageSummary) {
    const excluded = summary.excluded_metered_by_gateway ?? {
      estimated_cost: 0,
      tokens: 0,
      actors: [],
    };
    const actors = summary.by_actor ?? [];
    return html`
      <div class="stats">
        <div class="stat" data-testid="anthropic-total">
          <div class="stat-label">Estimated cost outside the gateway</div>
          <div class="stat-value">
            ${
              summary.total_estimated_cost === null ||
              summary.total_estimated_cost === undefined
                ? html`<span class="muted">not imported yet</span>`
                : formatCurrencyAmount(
                    summary.total_estimated_cost,
                    summary.currency
                  )
            }
          </div>
        </div>
        <div class="stat">
          <div class="stat-label">Tokens outside the gateway</div>
          <div class="stat-value">
            ${this.formatNumber(summary.total_tokens ?? 0)}
          </div>
        </div>
        ${
          excluded.actors.length
            ? html`<div class="stat" data-testid="anthropic-excluded">
                <div class="stat-label">Already metered by the gateway</div>
                <div class="stat-value">
                  ${formatCurrencyAmount(excluded.estimated_cost, summary.currency)}
                </div>
                <div class="muted">
                  Not added to the total: ${excluded.actors.join(', ')}
                </div>
              </div>`
            : nothing
        }
      </div>
      ${
        actors.length
          ? html`<section data-testid="anthropic-actors">
              <h4>By developer or key ${this.renderMarker()}</h4>
              <div class="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Actor</th>
                      <th>Preloop user</th>
                      <th class="num">Sessions</th>
                      <th class="num">Lines added</th>
                      <th class="num">Commits</th>
                      <th class="num">Tokens</th>
                      <th class="num">Estimated cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${actors.map(
                      (actor) =>
                        html`<tr>
                          <td>${actor.actor}</td>
                          <td>
                            ${
                              actor.user_id
                                ? actor.mapping_source === 'mapping'
                                  ? 'mapped'
                                  : 'matched by email'
                                : html`<span class="muted">unmapped</span>`
                            }
                          </td>
                          <td class="num">${actor.num_sessions}</td>
                          <td class="num">${actor.lines_added}</td>
                          <td class="num">${actor.commits}</td>
                          <td class="num">
                            ${this.formatNumber(actor.tokens)}
                          </td>
                          <td class="num">
                            ${formatCurrencyAmount(actor.estimated_cost, summary.currency)}
                          </td>
                        </tr>`
                    )}
                  </tbody>
                </table>
              </div>
            </section>`
          : nothing
      }
    `;
  }

  private renderNotAttributable(summary: AnthropicUsageSummary) {
    return html`<details data-testid="anthropic-not-attributable">
      <summary>What this import cannot attribute</summary>
      <ul class="muted">
        ${(summary.not_attributable ?? []).map((item) => html`<li>${item}</li>`)}
      </ul>
    </details>`;
  }

  render() {
    if (this.loading && !this.summary) {
      return html`<div class="muted" role="status">
        Loading Anthropic usage
      </div>`;
    }
    if (this.error) {
      return html`<sl-alert variant="danger" open role="alert"
        >${this.error}</sl-alert
      >`;
    }
    const summary = this.summary;
    if (!summary) return nothing;
    return html`
      <div class="panel">
        <div class="header">
          <h3>Claude Code (Anthropic Admin API)</h3>
          ${this.renderMarker()}
        </div>
        <p class="muted">
          Daily estimates from the Claude Code Analytics report for usage that
          did not pass through the gateway. They never count toward gateway
          usage, budgets or quota.
        </p>
        ${
          this.actionError
            ? html`<sl-alert
                variant="danger"
                open
                data-testid="anthropic-action-error"
                >${this.actionError}</sl-alert
              >`
            : nothing
        }
        ${
          this.notice
            ? html`<sl-alert
                variant="success"
                open
                data-testid="anthropic-notice"
                >${this.notice}</sl-alert
              >`
            : nothing
        }
        ${this.editing ? this.renderForm() : this.renderStatus()}
        ${summary.connection ? this.renderUsage(summary) : nothing}
        ${this.renderNotAttributable(summary)}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'anthropic-usage-panel': AnthropicUsagePanel;
  }
}
