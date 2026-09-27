import { html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  exportIssueCosts,
  getFlows,
  getIssueCostExecutions,
  getIssueCosts,
  listProjects,
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

/** Hours with one decimal; blank when the later milestone is missing. */
export function formatIssueHours(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return `${value.toFixed(1)} h`;
}

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

  renderExecutions(row: IssueCostRow) {
    const state = this.expanded[row.id];
    if (!state) return nothing;
    let body;
    if (state === 'loading') {
      body = html`<sl-spinner></sl-spinner>`;
    } else if (state === 'error') {
      body = html`<span class="muted">Could not load the executions.</span>`;
    } else {
      body = html`<table
        class="styled-table"
        aria-label="Executions of ${row.issue_key}"
      >
        <thead>
          <tr>
            <th>Flow</th>
            <th>Status</th>
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
                <td class="num">
                  ${formatIssueCost(execution.estimated_cost)}
                </td>
                <td>${formatTime(execution.start_time)}</td>
                <td>${formatTime(execution.end_time)}</td>
              </tr>`
          )}
        </tbody>
      </table>`;
    }
    return html`<tr class="detail">
      <td colspan="10">${body}</td>
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
          <th class="num" title="First event to PR opened">To PR</th>
          <th class="num" title="PR opened to approved">To approval</th>
          <th class="num" title="Approved to merged">To merge</th>
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
                <td class="num">${formatIssueCost(row.estimated_cost)}</td>
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
                <td class="num">
                  ${formatIssueHours(row.first_event_to_pr_opened_hours)}
                </td>
                <td class="num">
                  ${formatIssueHours(row.pr_opened_to_approved_hours)}
                </td>
                <td class="num">
                  ${formatIssueHours(row.approved_to_merged_hours)}
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
                      <td class="num">
                        ${formatIssueCost(item.estimated_cost)}
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
        ${bucket.run_count} runs (${formatIssueCost(bucket.estimated_cost)})
        could not be tied to exactly one issue. They are counted here and not in
        any issue row.
      </p>
    </sl-card>`;
  }

  render() {
    const report = this.report;
    return html`<div class="page">
      <view-header
        headerText="Cost per issue"
        description="Agent cost and cycle time for each tracker issue."
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
