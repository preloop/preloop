import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  exportIssueCosts,
  getFlows,
  getIssueCostExecutions,
  getIssueCosts,
  getUnassignedIssueCostExecutions,
  listProjects,
  type CostCoverage,
  type IssueCostCoverage,
  type IssueCostExecution,
  type IssueCostFilter,
  type IssueCostReport,
  type IssueCostRow,
  type IssueCostSummary,
} from '../../api';
import consoleStyles from '../../styles/console-styles.css?inline';
import { resolveTimeRange } from '../../utils/time-range';
import { downloadBlob } from '../../utils/records-format';
import '../../components/view-header.ts';
import '../../components/time-range-select.ts';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

const RANGE_OPTIONS = [
  { value: 'last-7', label: '7d' },
  { value: 'last-30', label: '30d' },
  { value: 'last-90', label: '90d' },
  { value: 'last-365', label: '1y' },
  { value: 'all', label: 'All' },
];

interface NamedOption {
  id: string;
  name: string;
}

/** Dollars with four decimals below a cent, two above. */
export function formatIssueCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  const digits = value !== 0 && Math.abs(value) < 0.01 ? 4 : 2;
  return `$${value.toFixed(digits)}`;
}

/** What the Cost column says, and why, for one bucket (#1057). */
export function issueCostTitle(bucket: IssueCostCoverage): string {
  const coverage = issueCostCoverage(bucket);
  if (coverage === 'unknown') {
    return 'No run here has a cost estimate, so this is not a free ticket. Subscription-backed runs report no per-run price.';
  }
  if (coverage === 'partial') {
    return `Estimated cost of ${bucket.unknown_cost_run_count} of ${
      bucket.known_cost_run_count + bucket.unknown_cost_run_count
    } runs is missing. The amount shown is the priced subtotal, not total spend.`;
  }
  return 'Every run here has a cost estimate, priced from published model rates. This is an estimate, not a measured or invoiced amount.';
}

/**
 * The coverage the server stated; a response without it is read as unpriced
 * rather than complete, because claiming completeness nobody stated is the
 * bug this fixes.
 */
export function issueCostCoverage(bucket: IssueCostCoverage): CostCoverage {
  return bucket.cost_coverage ?? 'unknown';
}

/**
 * The Cost cell text: an unpriced bucket is Unknown, never $0.00.
 *
 * A partial bucket shows the known subtotal and how many runs it is missing.
 */
export function formatIssueAmount(bucket: IssueCostCoverage): string {
  const coverage = issueCostCoverage(bucket);
  if (coverage === 'unknown') return 'Unknown';
  const amount = formatIssueCost(bucket.estimated_cost);
  if (coverage === 'complete') return amount;
  const runs = bucket.unknown_cost_run_count ?? 0;
  return `${amount} partial; ${runs} ${
    runs === 1 ? 'run' : 'runs'
  } without cost`;
}

/** Hours with one decimal; blank when the later milestone is missing. */
export function formatIssueHours(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return `${value.toFixed(1)} h`;
}

/** The tracker's estimate, hours and/or points; blank when it has none. */
export function formatIssueEstimate(row: IssueCostRow): string {
  const parts: string[] = [];
  if (row.estimate_hours !== null && row.estimate_hours !== undefined) {
    parts.push(`${row.estimate_hours} h`);
  }
  if (row.estimate_points !== null && row.estimate_points !== undefined) {
    parts.push(`${row.estimate_points} pts`);
  }
  return parts.join(' / ');
}

function estimateTitle(row: IssueCostRow): string {
  return [row.estimate_hours_source, row.estimate_points_source]
    .filter((source): source is string => Boolean(source))
    .map((source) => `From ${source}`)
    .join('; ');
}

/**
 * Exact definitions of the three cycle-time columns (#1064). They start at
 * the first attributed run, not at ticket creation, and end at a recorded
 * approval, which is not verified mergeability.
 */
export const INTERVAL_TITLES = {
  toPr: 'Hours from the earliest attributed execution start (not ticket creation) to PR opened',
  toApproval:
    'Hours from PR opened to the first recorded approval event (not verified mergeability)',
  toMerge: 'Hours from the first recorded approval event to the recorded merge',
} as const;

/** The note under the toolbar that states what the intervals measure. */
export const CYCLE_TIME_NOTE =
  'Cycle time starts at the earliest attributed execution start, not when the ticket was created, and the period selects issues by that start (start inclusive, end exclusive). Approval is a recorded approval event, not verified mergeability: required checks and branch rules are not evaluated. A flow filter narrows cost and runs to that flow; the times stay those of the whole issue.';

const OPENED_SOURCE_TITLES: Record<string, string> = {
  forge: 'PR opened: the pull request creation time from the forge',
  bind: 'PR opened: approximate, the time Preloop bound the pull request',
  run_end: 'PR opened: approximate, the end of the publishing run',
};

/** Tooltip of one execution row that carries no cost estimate. */
const RUN_COST_TITLE =
  'This run has no cost estimate, so it is not a free run. Subscription-backed runs report no per-run price.';

function formatTime(value: string | null): string {
  if (!value) return '';
  return new Date(value).toLocaleString();
}

/** Only http(s) links are rendered; anything else is shown as text. */
function safeHref(url: string | null): string | null {
  if (!url) return null;
  return /^https?:\/\//i.test(url) ? url : null;
}

/**
 * Cost and cycle time per tracker issue (#958).
 *
 * Every row is one tracker issue; its cost is the sum of the estimated cost
 * of the executions that worked on it. Executions that could not be tied to
 * exactly one issue are shown as the unassigned bucket, never guessed.
 */
@customElement('issue-cost-view')
export class IssueCostView extends AuthedElement {
  @state() report: IssueCostReport | null = null;
  @state() loading = false;
  @state() error: string | null = null;
  @state() range = 'last-30';
  @state() projectId = '';
  @state() flowId = '';
  @state() projects: NamedOption[] = [];
  @state() flows: NamedOption[] = [];
  @state() expanded: Record<
    string,
    IssueCostExecution[] | 'loading' | 'error'
  > = {};
  @state() exporting: 'csv' | 'json' | null = null;
  @state() unassignedRuns: IssueCostExecution[] | 'loading' | 'error' | null =
    null;
  /** Bumped on each unassigned toggle and reload; stale responses drop. */
  private unassignedRequest = 0;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .page {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }
      .toolbar {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: center;
      }
      .toolbar sl-select {
        min-width: 12rem;
      }
      .toolbar .spacer {
        flex: 1;
      }
      .summaries {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
        gap: var(--sl-spacing-large);
      }
      .num {
        text-align: right;
        white-space: nowrap;
      }
      .expand {
        background: none;
        border: none;
        cursor: pointer;
        color: inherit;
        padding: 0 var(--sl-spacing-2x-small);
      }
      .detail td {
        background: var(--sl-color-neutral-50);
      }
      .muted {
        color: var(--sl-color-neutral-500);
      }
      .loading-state {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: center;
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    void this.loadFilters();
    void this.load();
  }

  filter(): IssueCostFilter {
    const window = resolveTimeRange(this.range);
    return {
      startDate: window.startDate,
      endDate: window.endDate,
      projectId: this.projectId || null,
      flowId: this.flowId || null,
    };
  }

  async loadFilters(): Promise<void> {
    try {
      const [projects, flows] = await Promise.all([
        listProjects(),
        getFlows({ limit: 500 }),
      ]);
      this.projects = projects.map((project) => ({
        id: String(project.id),
        name: project.name,
      }));
      this.flows = flows.map((flow: { id: string; name: string }) => ({
        id: String(flow.id),
        name: flow.name,
      }));
    } catch {
      // Filters are optional; the table still loads without them.
    }
  }

  async load(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      this.report = await getIssueCosts(this.filter());
      this.expanded = {};
      this.unassignedRequest++;
      this.unassignedRuns = null;
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load cost per issue';
    } finally {
      this.loading = false;
    }
  }

  async toggle(row: IssueCostRow): Promise<void> {
    if (this.expanded[row.id]) {
      const { [row.id]: _removed, ...rest } = this.expanded;
      this.expanded = rest;
      return;
    }
    this.expanded = { ...this.expanded, [row.id]: 'loading' };
    try {
      const executions = await getIssueCostExecutions(row.id, this.flowId);
      this.expanded = { ...this.expanded, [row.id]: executions };
    } catch {
      this.expanded = { ...this.expanded, [row.id]: 'error' };
    }
  }

  async toggleUnassigned(): Promise<void> {
    // Any hide, reload or newer request supersedes a fetch in flight, so a
    // panel the user closed stays closed when the old response lands.
    const request = ++this.unassignedRequest;
    if (this.unassignedRuns) {
      this.unassignedRuns = null;
      return;
    }
    this.unassignedRuns = 'loading';
    let runs: IssueCostExecution[] | 'error';
    try {
      runs = await getUnassignedIssueCostExecutions(this.filter());
    } catch {
      runs = 'error';
    }
    if (request === this.unassignedRequest) this.unassignedRuns = runs;
  }

  async download(format: 'csv' | 'json'): Promise<void> {
    this.exporting = format;
    try {
      const blob = await exportIssueCosts(format, this.filter());
      downloadBlob(blob, `issue-costs.${format}`);
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to export cost per issue';
    } finally {
      this.exporting = null;
    }
  }

  private onRange = (event: Event) => {
    const value = (event as CustomEvent<{ value?: string }>).detail?.value;
    if (!value || value === this.range) return;
    this.range = value;
    void this.load();
  };

  private onProject = (event: Event) => {
    this.projectId = String((event.target as HTMLSelectElement).value || '');
    void this.load();
  };

  private onFlow = (event: Event) => {
    this.flowId = String((event.target as HTMLSelectElement).value || '');
    void this.load();
  };

  renderLink(url: string | null, label: string) {
    const href = safeHref(url);
    if (!href) return label ? html`<span>${label}</span>` : nothing;
    return html`<a href=${href} target="_blank" rel="noopener noreferrer"
      >${label}</a
    >`;
  }

  renderExecutionTable(
    state: IssueCostExecution[] | 'loading' | 'error',
    label: string,
    showLink = false
  ) {
    if (state === 'loading') {
      return html`<sl-spinner></sl-spinner>`;
    }
    if (state === 'error') {
      return html`<span class="muted">Could not load the executions.</span>`;
    }
    return html`<table class="styled-table" aria-label=${label}>
      <thead>
        <tr>
          <th>Flow</th>
          <th>Status</th>
          ${showLink ? html`<th>Reason</th>` : nothing}
          <th class="num">Cost</th>
          <th>Start</th>
          <th>End</th>
        </tr>
      </thead>
      <tbody>
        ${state.map(
          (execution) =>
            html`<tr data-execution=${execution.execution_id}>
              <td>
                <a href="/console/flows/executions/${execution.execution_id}"
                  >${execution.flow_name || execution.flow_id}</a
                >
              </td>
              <td>${execution.status}</td>
              ${showLink ? html`<td>${execution.link}</td>` : nothing}
              <td
                class="num"
                title=${
                  execution.estimated_cost === null ? RUN_COST_TITLE : nothing
                }
              >
                ${formatIssueCost(execution.estimated_cost) || 'Unknown'}
              </td>
              <td>${formatTime(execution.start_time)}</td>
              <td>${formatTime(execution.end_time)}</td>
            </tr>`
        )}
      </tbody>
    </table>`;
  }

  renderExecutions(row: IssueCostRow) {
    const state = this.expanded[row.id];
    if (!state) return nothing;
    return html`<tr class="detail">
      <td colspan="11">
        ${this.renderExecutionTable(state, `Executions of ${row.issue_key}`)}
      </td>
    </tr>`;
  }

  renderIssues(report: IssueCostReport) {
    if (!report.issues.length) {
      return html`<p class="muted">No issue had agent work in this period.</p>`;
    }
    return html`<table class="styled-table" aria-label="Cost per issue">
      <thead>
        <tr>
          <th></th>
          <th>Tracker</th>
          <th>Issue</th>
          <th class="num">Cost</th>
          <th class="num">Tokens</th>
          <th class="num">Runs</th>
          <th class="num" title=${INTERVAL_TITLES.toPr}>Run to PR</th>
          <th class="num" title=${INTERVAL_TITLES.toApproval}>
            To recorded approval
          </th>
          <th class="num" title=${INTERVAL_TITLES.toMerge}>To merge</th>
          <th class="num" title="The tracker's own estimate">Estimate</th>
          <th>PR</th>
        </tr>
      </thead>
      <tbody>
        ${report.issues.map(
          (row) =>
            html`<tr data-issue=${row.issue_key}>
                <td>
                  <button
                    class="expand"
                    aria-expanded=${this.expanded[row.id] ? 'true' : 'false'}
                    aria-label="Show executions of ${row.issue_key}"
                    @click=${() => void this.toggle(row)}
                  >
                    ${this.expanded[row.id] ? '▾' : '▸'}
                  </button>
                </td>
                <td>${row.tracker_name || row.tracker_type}</td>
                <td>
                  ${this.renderLink(row.issue_url, row.issue_key)}
                  ${
                    row.title
                      ? html`<div class="muted">${row.title}</div>`
                      : nothing
                  }
                </td>
                <td class="num cost" title=${issueCostTitle(row) || nothing}>
                  ${formatIssueAmount(row)}
                </td>
                <td class="num">${row.total_tokens.toLocaleString()}</td>
                <td class="num">
                  ${row.run_count}${
                    row.failed_run_count
                      ? html` <span class="muted"
                          >(${row.failed_run_count} failed)</span
                        >`
                      : nothing
                  }
                </td>
                <td
                  class="num"
                  title=${
                    OPENED_SOURCE_TITLES[row.pr_opened_at_source ?? ''] ??
                    nothing
                  }
                >
                  ${formatIssueHours(row.first_event_to_pr_opened_hours)}
                </td>
                <td class="num">
                  ${formatIssueHours(row.pr_opened_to_approved_hours)}
                </td>
                <td class="num">
                  ${formatIssueHours(row.approved_to_merged_hours)}
                </td>
                <td class="num estimate" title=${estimateTitle(row) || nothing}>
                  ${formatIssueEstimate(row)}
                </td>
                <td>${this.renderLink(row.pr_url, row.pr_url ? 'PR' : '')}</td>
              </tr>
              ${this.renderExecutions(row)}`
        )}
      </tbody>
    </table>`;
  }

  renderSummary(label: string, items: IssueCostSummary[]) {
    return html`<sl-card>
      <h3 slot="header">${label}</h3>
      ${
        items.length
          ? html`<table class="styled-table" aria-label=${label}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th class="num">Issues</th>
                  <th class="num">Runs</th>
                  <th class="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                ${items.map(
                  (item) =>
                    html`<tr>
                      <td>${item.name || 'No project'}</td>
                      <td class="num">${item.issue_count}</td>
                      <td class="num">${item.run_count}</td>
                      <td class="num" title=${issueCostTitle(item) || nothing}>
                        ${formatIssueAmount(item)}
                      </td>
                    </tr>`
                )}
              </tbody>
            </table>`
          : html`<p class="muted">Nothing in this period.</p>`
      }
    </sl-card>`;
  }

  renderUnassigned(report: IssueCostReport) {
    const bucket = report.unassigned;
    if (!bucket.run_count) return nothing;
    return html`<sl-card class="unassigned">
      <h3 slot="header">Unassigned</h3>
      <p>
        ${bucket.run_count} runs (${formatIssueAmount(bucket)}) could not be
        tied to exactly one issue. They are counted here and not in any issue
        row.
      </p>
      <sl-button
        size="small"
        class="show-unassigned"
        aria-expanded=${this.unassignedRuns ? 'true' : 'false'}
        @click=${() => void this.toggleUnassigned()}
        >${this.unassignedRuns ? 'Hide runs' : 'Show runs'}</sl-button
      >
      ${
        this.unassignedRuns
          ? this.renderExecutionTable(
              this.unassignedRuns,
              'Unassigned executions',
              true
            )
          : nothing
      }
    </sl-card>`;
  }

  render() {
    const report = this.report;
    return html`<div class="page">
      <view-header
        headerText="Cost per issue"
        description="Estimated agent cost and cycle time for each tracker issue."
      ></view-header>
      <div class="toolbar">
        <time-range-select
          ariaLabel="Issue cost period"
          .value=${this.range}
          .options=${RANGE_OPTIONS}
          @range-change=${this.onRange}
        ></time-range-select>
        <sl-select
          size="small"
          placeholder="All projects"
          clearable
          aria-label="Project"
          .value=${this.projectId}
          @sl-change=${this.onProject}
        >
          ${this.projects.map(
            (project) =>
              html`<sl-option value=${project.id}>${project.name}</sl-option>`
          )}
        </sl-select>
        <sl-select
          size="small"
          placeholder="All flows"
          clearable
          aria-label="Flow"
          .value=${this.flowId}
          @sl-change=${this.onFlow}
        >
          ${this.flows.map(
            (flow) => html`<sl-option value=${flow.id}>${flow.name}</sl-option>`
          )}
        </sl-select>
        <span class="spacer"></span>
        <sl-button
          size="small"
          class="export-csv"
          ?loading=${this.exporting === 'csv'}
          @click=${() => void this.download('csv')}
          >Export CSV</sl-button
        >
        <sl-button
          size="small"
          class="export-json"
          ?loading=${this.exporting === 'json'}
          @click=${() => void this.download('json')}
          >Export JSON</sl-button
        >
      </div>
      <p class="muted cost-note">
        Cost is what Preloop estimated per run. Each row says how much of it is
        actually priced: complete when every run has a cost, partial when some
        runs do not, and Unknown instead of a dollar amount when none does.
        Three sources stay deliberately apart: a CLI run's premium-request count
        (a subscription has no per-run price), the daily GitHub Copilot import
        (account level, never attributed to a ticket), and gateway-priced
        estimates, which is what this page sums.
      </p>
      <p class="muted cycle-time-note">${CYCLE_TIME_NOTE}</p>
      ${
        this.error
          ? html`<sl-alert variant="danger" open role="alert"
              >${this.error}
              <sl-button size="small" @click=${() => void this.load()}
                >Retry</sl-button
              ></sl-alert
            >`
          : nothing
      }
      ${
        this.loading && !report
          ? html`<div class="loading-state" role="status">
              <sl-spinner></sl-spinner><span>Loading cost per issue...</span>
            </div>`
          : nothing
      }
      ${
        report
          ? html`${
                report.truncated
                  ? html`<sl-alert variant="warning" open
                      >Showing the most expensive issues only; narrow the filter
                      or use the export.</sl-alert
                    >`
                  : nothing
              }
              <sl-card>${this.renderIssues(report)}</sl-card>
              ${this.renderUnassigned(report)}
              <div class="summaries">
                ${this.renderSummary('By project', report.by_project)}
                ${this.renderSummary('By flow', report.by_flow)}
              </div>`
          : nothing
      }
    </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'issue-cost-view': IssueCostView;
  }
}
