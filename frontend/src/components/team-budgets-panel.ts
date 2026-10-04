import { html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { AuthedElement } from '../api';
import {
  createTeamBudget,
  deleteTeamBudget,
  getTeamUsage,
  listTeamBudgets,
  type TeamBudget,
  type TeamBudgetPeriod,
  type TeamUsageRow,
} from '../team-budgets-api';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import { confirmDialog } from './confirm-dialog';
import { formatUsd } from '../utils/money';

const PERIODS: TeamBudgetPeriod[] = ['daily', 'weekly', 'monthly'];

const PERIOD_LABELS: Record<string, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
};

function periodLabel(period: string): string {
  return PERIOD_LABELS[period] || period;
}

/**
 * Cost page Teams tab: member-attributed spend per team for the page's date
 * range, and the team budgets. A member of several teams counts in each of
 * them, so the rows can add up to more than the account's spend.
 *
 * Rendered only when the server advertises `team_budgets` (and teams exist,
 * `team_management`); the endpoints come from an extension plugin.
 */
@customElement('team-budgets-panel')
export class TeamBudgetsPanel extends AuthedElement {
  /** Window start (ISO) shared with the rest of the Cost page. */
  @property({ attribute: false }) startDate?: string;
  /** Window end (ISO) shared with the rest of the Cost page. */
  @property({ attribute: false }) endDate?: string;

  @state() private usage: TeamUsageRow[] = [];
  @state() private budgets: TeamBudget[] = [];
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private actionError: string | null = null;
  @state() private formTeam = '';
  @state() private formPeriod: TeamBudgetPeriod = 'monthly';
  @state() private formLimit = '';
  @state() private saving = false;
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
    .muted {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    /* Six columns do not fit a phone: scroll inside the panel instead of
       pushing the page sideways. */
    .table-scroll {
      overflow-x: auto;
    }
    th,
    td {
      text-align: left;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
    td.num,
    th.num {
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    .form {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      align-items: flex-end;
    }
  `;

  private requested = false;

  protected updated(changed: Map<string, unknown>): void {
    // Load once on first render (with or without a window), then again
    // whenever the Cost page changes its window.
    if (!this.requested || changed.has('startDate') || changed.has('endDate')) {
      this.requested = true;
      void this.load();
    }
  }

  async load(): Promise<void> {
    const seq = ++this.loadSeq;
    this.loading = true;
    this.error = null;
    try {
      const [usage, budgets] = await Promise.all([
        getTeamUsage({ start: this.startDate, end: this.endDate }),
        listTeamBudgets(),
      ]);
      if (seq !== this.loadSeq) return;
      this.usage = usage;
      this.budgets = budgets;
      if (!this.formTeam && usage.length) this.formTeam = usage[0].team_id;
    } catch (error) {
      if (seq !== this.loadSeq) return;
      this.error =
        error instanceof Error ? error.message : 'Could not load team spend';
    } finally {
      if (seq === this.loadSeq) this.loading = false;
    }
  }

  private money(value: number | null | undefined): string {
    if (value === null || value === undefined) return '-';
    return formatUsd(value);
  }

  private async addBudget(): Promise<void> {
    // Number('') is 0: an untouched field must not become a $0 hard limit.
    const limit = Number(this.formLimit);
    if (
      !this.formTeam ||
      this.formLimit.trim() === '' ||
      !Number.isFinite(limit) ||
      limit < 0
    ) {
      this.actionError = 'Choose a team and a limit of 0 or more.';
      return;
    }
    this.saving = true;
    this.actionError = null;
    try {
      await createTeamBudget({
        team_id: this.formTeam,
        period: this.formPeriod,
        hard_limit_usd: limit,
      });
      this.formLimit = '';
      await this.load();
      this.dispatchEvent(
        new CustomEvent('team-budgets-changed', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not save the budget';
    } finally {
      this.saving = false;
    }
  }

  private async removeBudget(budget: TeamBudget): Promise<void> {
    this.actionError = null;
    const confirmed = await confirmDialog({
      title: 'Remove team budget?',
      message: `Remove the ${periodLabel(budget.period).toLowerCase()} budget for ${budget.team_name}?`,
      detail: `Spending by the team's members will no longer be capped at ${this.money(budget.hard_limit_usd)}.`,
      confirmLabel: 'Remove budget',
      variant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteTeamBudget(budget.id);
      await this.load();
      this.dispatchEvent(
        new CustomEvent('team-budgets-changed', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.actionError =
        error instanceof Error ? error.message : 'Could not remove the budget';
    }
  }

  private renderUsage() {
    if (!this.usage.length) {
      return html`<p class="muted">No teams in this account yet.</p>`;
    }
    return html`<div class="table-scroll">
      <table aria-label="Spend per team">
        <thead>
          <tr>
            <th>Team</th>
            <th class="num">Members</th>
            <th class="num">Spend</th>
          </tr>
        </thead>
        <tbody>
          ${this.usage.map(
            (row) =>
              html`<tr>
                <td>${row.team_name}</td>
                <td class="num">${row.member_count}</td>
                <td class="num">${this.money(row.cost_usd)}</td>
              </tr>`
          )}
        </tbody>
      </table>
    </div>`;
  }

  private renderBudgets() {
    return html`<div class="table-scroll">
        <table aria-label="Team budgets">
          <thead>
            <tr>
              <th>Team</th>
              <th>Period</th>
              <th class="num">Spent this period</th>
              <th class="num">Hard limit</th>
              <th class="num">Soft limit</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${
              this.budgets.length
                ? this.budgets.map(
                    (budget) =>
                      html`<tr>
                        <td>${budget.team_name}</td>
                        <td>${periodLabel(budget.period)}</td>
                        <td class="num">
                          ${this.money(budget.current_spend_usd)}
                        </td>
                        <td class="num">
                          ${this.money(budget.hard_limit_usd)}
                        </td>
                        <td class="num">
                          ${this.money(budget.soft_limit_usd)}
                        </td>
                        <td>
                          <sl-button
                            size="small"
                            variant="text"
                            @click=${() => void this.removeBudget(budget)}
                            >Remove</sl-button
                          >
                        </td>
                      </tr>`
                  )
                : html`<tr>
                    <td colspan="6" class="muted">No team budgets.</td>
                  </tr>`
            }
          </tbody>
        </table>
      </div>
      ${
        this.usage.length
          ? html`<div class="form">
              <sl-select
                label="Team"
                size="small"
                .value=${this.formTeam}
                @sl-change=${(e: Event) =>
                  (this.formTeam = (e.target as HTMLSelectElement).value)}
              >
                ${this.usage.map(
                  (row) =>
                    html`<sl-option value=${row.team_id}
                      >${row.team_name}</sl-option
                    >`
                )}
              </sl-select>
              <sl-select
                label="Period"
                size="small"
                .value=${this.formPeriod}
                @sl-change=${(e: Event) =>
                  (this.formPeriod = (e.target as HTMLSelectElement)
                    .value as TeamBudgetPeriod)}
              >
                ${PERIODS.map(
                  (period) =>
                    html`<sl-option value=${period}
                      >${periodLabel(period)}</sl-option
                    >`
                )}
              </sl-select>
              <sl-input
                label="Hard limit (USD)"
                size="small"
                type="number"
                min="0"
                .value=${this.formLimit}
                @sl-input=${(e: Event) =>
                  (this.formLimit = (e.target as HTMLInputElement).value)}
              ></sl-input>
              <sl-button
                size="small"
                variant="primary"
                ?loading=${this.saving}
                @click=${() => void this.addBudget()}
                >Add team budget</sl-button
              >
            </div>`
          : nothing
      }`;
  }

  render() {
    if (this.loading) {
      return html`<sl-spinner aria-label="Loading team spend"></sl-spinner>`;
    }
    if (this.error) {
      return html`<sl-alert variant="danger" open role="alert"
        >${this.error}</sl-alert
      >`;
    }
    return html`<div class="panel">
      <p class="muted">
        Spend made with the API keys and agents of each team's members. A member
        of several teams counts in each of them.
      </p>
      ${this.renderUsage()}
      ${
        this.actionError
          ? html`<sl-alert variant="danger" open role="alert"
              >${this.actionError}</sl-alert
            >`
          : nothing
      }
      ${this.renderBudgets()}
    </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'team-budgets-panel': TeamBudgetsPanel;
  }
}
