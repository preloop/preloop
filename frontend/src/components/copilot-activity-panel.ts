import { LitElement, css, html, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getCopilotSeatActivity,
  getCopilotOrganizationMetrics,
  type CopilotSeatActivity,
  type CopilotOrganizationMetrics,
} from '../api/copilot-activity';
import { parseUTCDate } from '../utils/date';

/** Optional vendor evidence beside, and never part of, gateway measurements. */
@customElement('copilot-activity-panel')
export class CopilotActivityPanel extends LitElement {
  @property({ type: Boolean }) enabled = false;
  @property({ type: Boolean }) inventory = false;
  @property() startDate?: string;
  @property() endDate?: string;
  @state() private activity: CopilotSeatActivity | null = null;
  @state() private metrics: CopilotOrganizationMetrics | null = null;
  @state() private error = '';
  @state() private loading = false;
  private generation = 0;

  static styles = css`
    :host {
      display: block;
    }
    section {
      margin-block: 1rem;
      padding: 1rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
    }
    h3,
    h4 {
      margin-top: 0;
    }
    .muted {
      color: var(--sl-color-neutral-600);
    }
    .scroll {
      overflow-x: auto;
    }
    table {
      border-collapse: collapse;
      width: 100%;
    }
    th,
    td {
      padding: 0.5rem;
      text-align: left;
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
    caption {
      text-align: left;
      font-weight: 600;
      padding-block: 0.5rem;
    }
    details {
      margin-top: 1rem;
    }
    button {
      cursor: pointer;
    }
  `;

  protected updated(changed: PropertyValues): void {
    if (
      ['enabled', 'inventory', 'startDate', 'endDate'].some((key) =>
        changed.has(key)
      )
    )
      void this.load();
  }

  disconnectedCallback(): void {
    this.generation++;
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.activity = null;
    this.metrics = null;
    this.error = '';
    if (!this.enabled) {
      this.loading = false;
      return;
    }
    this.loading = true;
    try {
      const activity = await getCopilotSeatActivity();
      if (generation !== this.generation) return;
      if (!activity) return;
      this.activity = activity;
      if (!this.inventory) {
        const metrics = await getCopilotOrganizationMetrics(
          this.startDate,
          this.endDate
        );
        if (generation === this.generation) this.metrics = metrics;
      }
    } catch {
      if (generation === this.generation)
        this.error = 'Copilot activity could not be loaded.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  private count(value: number | null | undefined): string {
    return value == null ? 'Unknown' : value.toLocaleString();
  }

  private renderMetrics() {
    if (!this.metrics) return nothing;
    return html`<h4>Daily organization usage</h4>
      <p class="muted">
        GitHub reports daily active users and activity counts. Overall daily
        engaged users remain unknown. Rolling 28-day feature engagement is shown
        separately below and never converted to premium requests.
      </p>
      ${
        this.metrics.days.length
          ? html`<div class="scroll">
                <table data-testid="copilot-daily">
                  <thead>
                    <tr>
                      <th>Day (UTC)</th>
                      <th>Active users</th>
                      <th>Engaged users</th>
                      <th>Interactions</th>
                      <th>Code generations</th>
                      <th>Code acceptances</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this.metrics.days.map(
                      (day) =>
                        html`<tr>
                          <td>
                            ${day.day}${day.reason ? html`<p>${day.reason}</p>` : nothing}
                          </td>
                          <td>${this.count(day.active_users)}</td>
                          <td>${this.count(day.engaged_users)}</td>
                          <td>
                            ${this.count(day.user_initiated_interaction_count)}
                          </td>
                          <td>
                            ${this.count(day.code_generation_activity_count)}
                          </td>
                          <td>
                            ${this.count(day.code_acceptance_activity_count)}
                          </td>
                        </tr>`
                    )}
                  </tbody>
                </table>
              </div>
              ${this.metrics.days
                .filter((day) => day.status === 'available')
                .map(
                  (day) =>
                    html`<details>
                      <summary>Features and editors · ${day.day}</summary>
                      <div class="scroll">
                        <table>
                          <caption>
                            Daily feature usage
                          </caption>
                          <thead>
                            <tr>
                              <th>Feature</th>

                              <th>Interactions</th>
                              <th>Code generations</th>
                              <th>Code acceptances</th>
                            </tr>
                          </thead>
                          <tbody>
                            ${day.feature_usage.map(
                              (row) =>
                                html`<tr>
                                  <td>${row.feature}</td>

                                  <td>
                                    ${this.count(row.user_initiated_interaction_count)}
                                  </td>
                                  <td>
                                    ${this.count(row.code_generation_activity_count)}
                                  </td>
                                  <td>
                                    ${this.count(row.code_acceptance_activity_count)}
                                  </td>
                                </tr>`
                            )}
                          </tbody>
                        </table>
                      </div>
                      ${
                        day.feature_usage.some(
                          (row) => row.engaged_users !== undefined
                        ) || day.engagement_active_users != null
                          ? html`
                              <div class="scroll">
                                <table data-testid="copilot-rolling-engagement">
                                  <caption>
                                    Rolling 28-day engagement · ending
                                    ${day.day}
                                  </caption>
                                  <thead>
                                    <tr>
                                      <th>Feature</th>
                                      <th>Engaged users (28 days)</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    ${day.feature_usage
                                      .filter(
                                        (row) => row.engaged_users !== undefined
                                      )
                                      .map(
                                        (row) =>
                                          html`<tr>
                                            <td>${row.feature}</td>
                                            <td>
                                              ${this.count(row.engaged_users)}
                                            </td>
                                          </tr>`
                                      )}
                                  </tbody>
                                </table>
                              </div>
                              <p class="muted">
                                Distinct active users in this 28-day window:
                                ${this.count(day.engagement_active_users)}.
                                Feature counts overlap and are not added
                                together.
                              </p>
                            `
                          : nothing
                      }
                      <div class="scroll">
                        <table>
                          <caption>
                            Editor usage
                          </caption>
                          <thead>
                            <tr>
                              <th>Editor</th>
                              <th>Interactions</th>
                              <th>Code generations</th>
                              <th>Code acceptances</th>
                            </tr>
                          </thead>
                          <tbody>
                            ${day.editor_usage.map(
                              (row) =>
                                html`<tr>
                                  <td>${row.editor}</td>
                                  <td>
                                    ${this.count(row.user_initiated_interaction_count)}
                                  </td>
                                  <td>
                                    ${this.count(row.code_generation_activity_count)}
                                  </td>
                                  <td>
                                    ${this.count(row.code_acceptance_activity_count)}
                                  </td>
                                </tr>`
                            )}
                          </tbody>
                        </table>
                      </div>
                    </details>`
                )}`
          : html`<p>No daily organization report imported yet.</p>`
      }`;
  }

  render() {
    if (!this.enabled) return nothing;
    if (this.loading && !this.activity)
      return html`<p role="status">Loading Copilot activity…</p>`;
    const activity = this.activity;
    if (!activity && !this.error) return nothing;
    const gaps = new Map(
      activity?.gaps.map((gap) => [gap.github_login, gap.reason]) ?? []
    );
    const seats = this.inventory
      ? activity?.seats.filter((seat) => gaps.has(seat.github_login))
      : activity?.seats;
    return html`<section aria-label="Copilot vendor activity">
      <h3>
        ${this.inventory ? 'Copilot inventory gaps' : 'Copilot seat activity'}
      </h3>
      <p data-testid="vendor-source">
        Vendor-reported · Not metered by the gateway. Separate from gateway
        tokens, budgets and quota.
      </p>
      ${this.error ? html`<p role="alert">${this.error} <button @click=${() => void this.load()}>Retry</button></p>` : nothing}
      ${
        activity
          ? html`<p>
                ${activity.organization} · Seat snapshot:
                ${activity.snapshot_day ?? 'not imported yet'} · Activity in the
                last ${activity.active_days} days.
                ${!activity.is_active ? 'Imports are paused.' : ''}
              </p>
              <p>
                ${activity.snapshot_day ? `${activity.gaps.length} active seat(s) without a matched governed agent.` : 'Inventory coverage is unknown until seats are imported.'}
              </p>
              ${
                seats?.length
                  ? html`<div class="scroll">
                      <table data-testid="copilot-seat-activity">
                        <thead>
                          <tr>
                            <th>GitHub login</th>
                            <th>Preloop user</th>
                            <th>Last activity</th>
                            <th>Editor</th>
                            <th>Coverage</th>
                            ${!this.inventory ? html`<th>Premium net spend (USD, last ${activity.active_days} days)</th>` : nothing}
                          </tr>
                        </thead>
                        <tbody>
                          ${seats.map(
                            (seat) =>
                              html`<tr>
                                <td>${seat.github_login}</td>
                                <td>
                                  ${seat.user_name ?? (seat.user_id ? 'Linked user' : 'Unmapped')}
                                </td>
                                <td>
                                  ${seat.last_activity_at ? parseUTCDate(seat.last_activity_at).toLocaleString() : 'Unknown'}
                                </td>
                                <td>
                                  ${seat.last_activity_editor ?? 'Unknown'}
                                </td>
                                <td>
                                  ${gaps.get(seat.github_login) === 'unmapped_login' ? 'Identity link missing' : gaps.has(seat.github_login) ? 'No governed agent' : seat.governed_agent ? 'Governed agent linked' : 'No recent activity'}
                                </td>
                                ${!this.inventory ? html`<td>${seat.premium_net_amount === null ? 'Unknown' : new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(seat.premium_net_amount)}</td>` : nothing}
                              </tr>`
                          )}
                        </tbody>
                      </table>
                    </div>`
                  : nothing
              }
              ${!this.inventory ? this.renderMetrics() : nothing}`
          : nothing
      }
    </section>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'copilot-activity-panel': CopilotActivityPanel;
  }
}
