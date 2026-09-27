import { html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  AuthedElement,
  deleteCopilotConnection,
  getCopilotUsage,
  saveCopilotConnection,
  syncCopilotConnection,
} from '../api';
import type { CopilotConnectionUpsert, CopilotUsageSummary } from '../types';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

/** Shown next to every imported Copilot figure. */
export const COPILOT_NOT_METERED = 'Not metered by the gateway';

/**
 * Cost page section for GitHub Copilot spend imported from GitHub's billing
 * and usage-metrics APIs. None of these numbers are gateway usage: they never
 * count toward gateway totals, budgets or quota, and every block carries the
 * "Not metered by the gateway" marker. The seat line is the operator's seat
 * price times the seat count; without a price there is no dollar seat line.
 */
@customElement('copilot-usage-panel')
export class CopilotUsagePanel extends AuthedElement {
  /** Window start (ISO) shared with the rest of the Cost page. */
  @property({ attribute: false }) startDate?: string;
  /** Window end (ISO) shared with the rest of the Cost page. */
  @property({ attribute: false }) endDate?: string;

  @state() private summary: CopilotUsageSummary | null = null;
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private actionError: string | null = null;
  @state() private notice: string | null = null;
  @state() private editing = false;
  @state() private saving = false;
  @state() private syncing = false;
  @state() private formOrganization = '';
  @state() private formEnterprise = '';
  @state() private formToken = '';
  @state() private formEnterpriseToken = '';
  @state() private formSeatPrice = '';
  private loadSeq = 0;

  static styles = css`
    :host {
      display: block;
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
  `;

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
      const summary = await getCopilotUsage({
        startDate: this.startDate,
        endDate: this.endDate,
      });
      if (seq !== this.loadSeq) return;
      this.summary = summary;
      if (!summary.connection) this.editing = true;
    } catch (error) {
      if (seq !== this.loadSeq) return;
      this.error =
        error instanceof Error ? error.message : 'Could not load Copilot usage';
    } finally {
      if (seq === this.loadSeq) this.loading = false;
    }
  }

  private formatCurrency(value: number, currency = 'USD'): string {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  }

  private formatNumber(value: number): string {
    return new Intl.NumberFormat(undefined, {
      maximumFractionDigits: 2,
    }).format(value);
  }

  private formatShare(value: number): string {
    return `${(value * 100).toFixed(1)}%`;
  }

  private startEditing(): void {
    const connection = this.summary?.connection;
    this.formOrganization = connection?.organization ?? '';
    this.formEnterprise = connection?.enterprise ?? '';
    this.formToken = '';
    this.formEnterpriseToken = '';
    this.formSeatPrice =
      connection?.seat_price_monthly === null ||
      connection?.seat_price_monthly === undefined
        ? ''
        : String(connection.seat_price_monthly);
    this.actionError = null;
    this.editing = true;
  }

  private async save(): Promise<void> {
    this.actionError = null;
    this.notice = null;
    const priceText = this.formSeatPrice.trim();
    const price = priceText === '' ? null : Number(priceText);
    if (price !== null && (!Number.isFinite(price) || price < 0)) {
      this.actionError = 'Seat price must be a number of 0 or more.';
      return;
    }
    const payload: CopilotConnectionUpsert = {
      organization: this.formOrganization.trim(),
      enterprise: this.formEnterprise.trim() || null,
      seat_price_monthly: price,
    };
    if (this.formToken) payload.token = this.formToken;
    if (this.formEnterpriseToken)
      payload.enterprise_token = this.formEnterpriseToken;
    if (!payload.enterprise) payload.clear_enterprise_token = true;
    this.saving = true;
    try {
      await saveCopilotConnection(payload);
      this.editing = false;
      this.formToken = '';
      this.formEnterpriseToken = '';
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
      await syncCopilotConnection();
      this.notice =
        'Import queued. GitHub reports lag by up to two days, so the newest day imported is three days ago.';
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not queue the import';
    } finally {
      this.syncing = false;
    }
  }

  private async removeConnection(): Promise<void> {
    this.actionError = null;
    this.notice = null;
    try {
      await deleteCopilotConnection();
      await this.load();
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not remove';
    }
  }

  private renderMarker() {
    return html`<sl-badge variant="neutral" data-testid="copilot-marker"
      >${this.summary?.marker || COPILOT_NOT_METERED}</sl-badge
    >`;
  }

  private renderForm() {
    const connection = this.summary?.connection;
    return html`
      <div class="form" data-testid="copilot-form">
        <p class="muted">
          Use an organization owner token (classic scopes
          <code>manage_billing:copilot</code> or <code>read:org</code>) for
          seats, premium-request spend and usage metrics. If the organization
          belongs to an enterprise and its billing is only readable there, add
          the enterprise slug and an enterprise billing reader token. Tokens are
          stored encrypted and never shown again.
        </p>
        <div class="form-grid">
          <sl-input
            label="GitHub organization"
            name="organization"
            .value=${this.formOrganization}
            @sl-input=${(event: Event) =>
              (this.formOrganization = (
                event.target as HTMLInputElement
              ).value)}
          ></sl-input>
          <sl-input
            label=${connection ? 'New organization token (optional)' : 'Organization token'}
            name="token"
            type="password"
            password-toggle
            .value=${this.formToken}
            @sl-input=${(event: Event) =>
              (this.formToken = (event.target as HTMLInputElement).value)}
          ></sl-input>
          <sl-input
            label="Enterprise slug (optional)"
            name="enterprise"
            .value=${this.formEnterprise}
            @sl-input=${(event: Event) =>
              (this.formEnterprise = (event.target as HTMLInputElement).value)}
          ></sl-input>
          <sl-input
            label=${
              connection?.has_enterprise_token
                ? 'New enterprise billing token (optional)'
                : 'Enterprise billing token (optional)'
            }
            name="enterprise_token"
            type="password"
            password-toggle
            .value=${this.formEnterpriseToken}
            @sl-input=${(event: Event) =>
              (this.formEnterpriseToken = (
                event.target as HTMLInputElement
              ).value)}
          ></sl-input>
          <sl-input
            label="Seat price per month (optional)"
            name="seat_price"
            type="number"
            min="0"
            step="0.01"
            help-text="Your contract price. Leave empty to show seats without a dollar line."
            .value=${this.formSeatPrice}
            @sl-input=${(event: Event) =>
              (this.formSeatPrice = (event.target as HTMLInputElement).value)}
          ></sl-input>
        </div>
        <div class="actions" style="margin-top: var(--sl-spacing-small);">
          <sl-button
            variant="primary"
            data-testid="copilot-save"
            .loading=${this.saving}
            ?disabled=${
              !this.formOrganization.trim() || (!connection && !this.formToken)
            }
            @click=${() => void this.save()}
            >${connection ? 'Save' : 'Connect Copilot'}</sl-button
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
      ? new Date(connection.last_synced_at).toLocaleString()
      : 'never';
    return html`
      <div class="muted" data-testid="copilot-status">
        Organization <strong>${connection.organization}</strong>${
          connection.enterprise
            ? html` in enterprise <strong>${connection.enterprise}</strong>`
            : nothing
        }.
        Last import:
        ${lastSynced}${
          connection.last_synced_day
            ? html`, data through ${connection.last_synced_day}`
            : nothing
        }.
      </div>
      ${
        connection.last_error
          ? html`<sl-alert variant="danger" open data-testid="copilot-error">
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              The last import failed: ${connection.last_error}
            </sl-alert>`
          : nothing
      }
      ${
        connection.metrics_status === 'unavailable' && connection.metrics_reason
          ? html`<sl-alert variant="warning" open data-testid="copilot-metrics">
              <sl-icon slot="icon" name="info-circle"></sl-icon>
              Usage metrics are unavailable: ${connection.metrics_reason}
            </sl-alert>`
          : nothing
      }
      <div class="actions">
        <sl-button
          size="small"
          data-testid="copilot-sync"
          .loading=${this.syncing}
          @click=${() => void this.sync()}
          >Sync now</sl-button
        >
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

  private renderSeats(summary: CopilotUsageSummary) {
    const seats = summary.seats;
    return html`
      <section data-testid="copilot-seats">
        <h4>Seats ${this.renderMarker()}</h4>
        <div class="stats">
          <div class="stat">
            <div class="stat-label">Assigned seats</div>
            <div class="stat-value">
              ${
                seats.total_seats === null
                  ? html`<span class="muted">not imported yet</span>`
                  : this.formatNumber(seats.total_seats)
              }
            </div>
          </div>
          <div class="stat">
            <div class="stat-label">Seat price (entered)</div>
            <div class="stat-value">
              ${
                seats.seat_price_monthly === null
                  ? html`<span class="muted">not set</span>`
                  : this.formatCurrency(
                      seats.seat_price_monthly,
                      seats.currency
                    )
              }
            </div>
          </div>
          ${
            seats.monthly_seat_estimate === null
              ? nothing
              : html`<div class="stat" data-testid="copilot-seat-estimate">
                  <div class="stat-label">Monthly seat estimate</div>
                  <div class="stat-value">
                    ${this.formatCurrency(
                      seats.monthly_seat_estimate,
                      seats.currency
                    )}
                  </div>
                </div>`
          }
        </div>
        ${
          seats.monthly_seat_estimate === null && seats.total_seats !== null
            ? html`<p class="muted" data-testid="copilot-no-seat-price">
                Enter your seat price to see a monthly seat estimate. GitHub
                does not report it.
              </p>`
            : nothing
        }
        ${
          seats.assigned.length
            ? html`<table>
                <thead>
                  <tr>
                    <th>Developer</th>
                    <th>Last activity</th>
                    <th>Editor</th>
                  </tr>
                </thead>
                <tbody>
                  ${seats.assigned.map(
                    (seat) =>
                      html`<tr>
                        <td>${seat.login}</td>
                        <td>
                          ${
                            seat.last_activity_at
                              ? new Date(seat.last_activity_at).toLocaleString()
                              : 'no activity'
                          }
                        </td>
                        <td>${seat.last_activity_editor ?? ''}</td>
                      </tr>`
                  )}
                </tbody>
              </table>`
            : nothing
        }
      </section>
    `;
  }

  private renderPremium(summary: CopilotUsageSummary) {
    const premium = summary.premium_requests;
    const currency = premium.currency;
    return html`
      <section data-testid="copilot-premium">
        <h4>Premium requests ${this.renderMarker()}</h4>
        <div class="stats">
          <div class="stat">
            <div class="stat-label">Net spend in range</div>
            <div class="stat-value" data-testid="copilot-premium-total">
              ${
                premium.total_net_amount === null
                  ? html`<span class="muted">no data imported</span>`
                  : this.formatCurrency(premium.total_net_amount, currency)
              }
            </div>
          </div>
          ${
            premium.org_aggregate_net_amount === null
              ? nothing
              : html`<div class="stat" data-testid="copilot-org-total">
                  <div class="stat-label">Organization total only</div>
                  <div class="stat-value">
                    ${this.formatCurrency(
                      premium.org_aggregate_net_amount,
                      currency
                    )}
                  </div>
                </div>`
          }
        </div>
        ${
          premium.per_user_status === 'unavailable'
            ? html`<sl-alert
                variant="warning"
                open
                data-testid="copilot-per-user-reason"
              >
                <sl-icon slot="icon" name="info-circle"></sl-icon>
                ${
                  premium.aggregate_days
                    ? html`Spend for ${premium.aggregate_days} day(s) is an
                      organization total, not split by developer.`
                    : nothing
                }
                ${
                  premium.per_user_unavailable_reason ??
                  'Per-developer spend is unavailable.'
                }
              </sl-alert>`
            : nothing
        }
        ${
          premium.by_developer.length
            ? html`<h5>By developer</h5>
                <table data-testid="copilot-by-developer">
                  <thead>
                    <tr>
                      <th>Developer</th>
                      <th class="num">Requests</th>
                      <th class="num">Net spend</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${premium.by_developer.map(
                      (row) =>
                        html`<tr>
                          <td>${row.login}</td>
                          <td class="num">
                            ${this.formatNumber(row.net_quantity)}
                          </td>
                          <td class="num">
                            ${this.formatCurrency(row.net_amount, currency)}
                          </td>
                        </tr>`
                    )}
                  </tbody>
                </table>`
            : nothing
        }
        ${
          premium.by_model.length
            ? html`<h5>By model</h5>
                <table data-testid="copilot-by-model">
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th class="num">Requests</th>
                      <th class="num">Net spend</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${premium.by_model.map(
                      (row) =>
                        html`<tr>
                          <td>${row.model}</td>
                          <td class="num">
                            ${this.formatNumber(row.net_quantity)}
                          </td>
                          <td class="num">
                            ${this.formatCurrency(row.net_amount, currency)}
                          </td>
                        </tr>`
                    )}
                  </tbody>
                </table>`
            : nothing
        }
      </section>
    `;
  }

  private renderModelMix(summary: CopilotUsageSummary) {
    if (!summary.model_mix.length) return nothing;
    return html`
      <section data-testid="copilot-model-mix">
        <h4>Model mix by developer ${this.renderMarker()}</h4>
        <table>
          <thead>
            <tr>
              <th>Developer</th>
              <th>Models</th>
              <th>Based on</th>
            </tr>
          </thead>
          <tbody>
            ${summary.model_mix.map(
              (entry) =>
                html`<tr>
                  <td>${entry.login}</td>
                  <td>
                    ${entry.models
                      .map(
                        (model) =>
                          `${model.model} ${this.formatShare(model.share)}`
                      )
                      .join(', ')}
                  </td>
                  <td>
                    ${
                      entry.basis === 'net_amount'
                        ? 'net spend'
                        : 'request counts (no spend data)'
                    }
                  </td>
                </tr>`
            )}
          </tbody>
        </table>
      </section>
    `;
  }

  render() {
    if (this.loading && !this.summary) {
      return html`<div role="status" aria-busy="true">
        <sl-spinner></sl-spinner> Loading Copilot usage…
      </div>`;
    }
    if (this.error) {
      return html`<sl-alert variant="danger" open role="alert">
        ${this.error}
        <sl-button size="small" @click=${() => void this.load()}
          >Retry</sl-button
        >
      </sl-alert>`;
    }
    const summary = this.summary;
    if (!summary) return nothing;
    return html`
      <div class="panel">
        <div class="header">
          <h3>GitHub Copilot</h3>
          ${this.renderMarker()}
        </div>
        <p class="muted">
          Imported from GitHub once a day. These amounts are billed by GitHub,
          are not metered by the gateway, and never count toward gateway usage,
          budgets or quota.
        </p>
        ${
          this.actionError
            ? html`<sl-alert variant="danger" open role="alert"
                >${this.actionError}</sl-alert
              >`
            : nothing
        }
        ${
          this.notice
            ? html`<sl-alert variant="success" open role="status"
                >${this.notice}</sl-alert
              >`
            : nothing
        }
        ${
          this.editing || !summary.connection
            ? this.renderForm()
            : this.renderStatus()
        }
        ${
          summary.connection
            ? html`${this.renderSeats(summary)} ${this.renderPremium(summary)}
              ${this.renderModelMix(summary)}`
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'copilot-usage-panel': CopilotUsagePanel;
  }
}
