import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/view-header';
import '../../components/resource-actions';
import '../../components/list-toolbar';
import '../../components/weekly-value-section';
import {
  loadViewMode,
  saveViewMode,
  type ListViewMode,
} from '../../utils/view-mode';
import type { ResourceAction } from '../../components/resource-actions';
import {
  getFeatures,
  getGovernanceReviewSettings,
  getUserProfile,
  hasPermission,
  updateGovernanceReviewSettings,
  type UserPermissions,
} from '../../api';
import { hasCapability } from '../../capabilities';
import { ConsoleStatus } from '../../controllers/console-status';
import { showToast } from '../../components/confirm-dialog';
import { formatRelativeTime, formatLocalDateTime } from '../../utils/date';
import { formatUsdExact } from '../../utils/money';
import { tableScrollStyles } from '../../styles/table-scroll';
import consoleStyles from '../../styles/console-styles.css?inline';
import {
  dismissGovernanceFinding,
  getGovernanceFinding,
  getGovernanceFindings,
  getGovernanceRuns,
  restoreGovernanceFinding,
  updateWeeklyReportPreferences,
  type DismissReason,
  type GovernanceFinding,
  type GovernanceRun,
  type SpendEvidence,
  type ToolEvidence,
} from '../../governance-api';
import { LAST_ON_MODE_KEY } from '../../components/governance-policy-panel';

const REASONS: Record<DismissReason, string> = {
  expected: 'Expected',
  fixed: 'Fixed',
  not_applicable: 'Not applicable',
};
const humanize = (value: string) =>
  value.replace(/_/g, ' ').replace(/^./, (s) => s.toUpperCase());
const stopReason = (value: string): string =>
  ({
    governance_hard_cap: 'Run spending limit reached',
    governance_allowance_exhausted: 'Model allowance exhausted',
    governance_allowance_unknown: 'Model allowance unavailable',
    governance_price_unknown: 'Model pricing unavailable',
  })[value] ?? humanize(value);
/** Plain words for a rule ref; the ref itself stays in `title`. */
export function ruleLabel(ref: string | null | undefined): string {
  if (!ref) return 'Not a policy rule';
  const [prefix, ...rest] = ref.split('.');
  if (prefix === 'policy_notice') return `Policy rule ${rest.join('.')}`;
  const cls =
    prefix === 'must' ? 'requirement' : prefix === 'prefer' ? 'preference' : '';
  if (!cls) return ref;
  if (rest[0] === 'account') return `Your policy (${cls})`;
  return `${humanize(rest.join(' '))} (${cls})`;
}
const KIND_HELP: Record<string, string> = {
  violation: 'Broke a requirement in your policy.',
  inefficiency: 'Missed a preference. Costs time or money, never blocks.',
  anomaly: 'Unusual activity your policy does not cover yet.',
};
const EVIDENCE_SOURCES: Record<string, string> = {
  weekly_signals: 'Weekly usage counts',
  governance_session_digest: 'Session summary',
  summary: 'Session summary',
  metadata: 'Gateway usage data',
  policy_notice_hit: 'Policy rule match',
  spend_outlier_finding: 'Spend outlier',
};
/** `section` deep links from the weekly email, by finding category. */
export const SECTIONS: Record<string, string> = {
  spend_outlier: 'Spend spikes',
  tool_hygiene: 'Tool hygiene',
  first_seen: 'First seen this week',
};
const money = (value: number) => formatUsdExact(value);
const tokens = (value: number) =>
  value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1)}M`
    : value >= 1_000
      ? `${(value / 1_000).toFixed(1)}K`
      : String(Math.round(value));
const weekday = (value: string) =>
  new Date(value).toLocaleDateString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  });
/** A session named by its title, else its agent and start time. */
export function sessionName(row: {
  title?: string | null;
  agent_name?: string | null;
  started_at?: string | null;
  runtime_session_id: string;
}): string {
  if (row.title) return row.title;
  const when = row.started_at ? formatLocalDateTime(row.started_at) : '';
  if (row.agent_name)
    return when ? `${row.agent_name}, ${when}` : row.agent_name;
  return `Session ${row.runtime_session_id.slice(0, 8)}`;
}
const evidenceSource = (value: string) =>
  EVIDENCE_SOURCES[value] ?? humanize(value);
const day = (value: string) =>
  new Date(value).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });

/** Weekly findings and evidence, gated by the governance_review capability. */
@customElement('governance-view')
export class GovernanceView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  static styles = [
    unsafeCSS(consoleStyles),
    tableScrollStyles,
    css`
      :host {
        display: block;
      }
      .filters,
      .pager,
      .summary {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-medium);
        align-items: end;
        margin: var(--sl-spacing-medium) 0;
      }
      label {
        display: grid;
        gap: 4px;
        font-size: 13px;
      }
      input,
      select {
        color: var(--sl-color-neutral-900);
        background: var(--sl-input-background-color);
        border: 1px solid var(--sl-input-border-color);
        border-radius: 4px;
        padding: 8px;
        max-width: 100%;
        font: inherit;
      }
      input {
        width: 230px;
      }
      .muted,
      dt,
      time {
        color: var(--console-meta-color);
      }
      .empty {
        min-height: 72px;
        display: grid;
        place-items: center;
      }
      .warning {
        padding: var(--sl-spacing-medium);
        border-left: 3px solid var(--sl-color-warning-600);
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 10%,
          transparent
        );
      }
      .error {
        color: var(--sl-color-danger-600);
      }
      h2,
      h3 {
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 14px;
      }
      th,
      td {
        text-align: left;
        padding: 8px;
        border-bottom: 1px solid var(--console-hairline);
        vertical-align: middle;
      }
      th button {
        font: inherit;
        color: inherit;
        background: none;
        border: 0;
        cursor: pointer;
        padding: 0;
      }
      .number {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .actions {
        width: 72px;
        min-width: 72px;
        box-sizing: border-box;
        overflow: visible;
      }
      resource-actions::part(container) {
        overflow: visible;
      }
      details {
        margin: var(--sl-spacing-large) 0;
      }
      summary {
        cursor: pointer;
        font-weight: 500;
      }
      .kinds {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        gap: 2px 16px;
      }
      .evidence {
        margin: 0;
        border-left: 2px solid var(--sl-color-neutral-300);
        padding-left: var(--sl-spacing-medium);
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      dl {
        display: grid;
        grid-template-columns: max-content minmax(0, 1fr);
        gap: 8px 24px;
      }
      dd {
        margin: 0;
        overflow-wrap: anywhere;
      }
      .cards {
        display: none;
      }
      .cards-active .finding-table {
        display: none;
      }
      .cards-active .cards {
        display: block;
      }
      .collection {
        margin-top: var(--sl-spacing-large);
      }
      list-toolbar {
        margin-bottom: var(--sl-spacing-medium);
      }
      .finding-card {
        padding: var(--sl-spacing-medium) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .notice {
        padding: var(--sl-spacing-medium) var(--sl-spacing-large);
        margin: var(--sl-spacing-medium) 0;
        background: var(--console-surface);
        border-radius: var(--sl-border-radius-large);
      }
      .notice p {
        margin: 0 0 var(--sl-spacing-small);
      }
      .section-filter {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .spend-chart {
        width: 100%;
        max-width: 640px;
        height: auto;
        display: block;
      }
      .spend-chart .bar {
        fill: color-mix(in srgb, var(--sl-color-neutral-500) 45%, transparent);
      }
      .spend-chart .bar.outlier {
        fill: var(--sl-color-warning-500);
      }
      .spend-chart .baseline {
        stroke: var(--console-meta-color);
        stroke-dasharray: 4 4;
      }
      .spend-chart text {
        fill: var(--console-meta-color);
        font-size: 12px;
        font-variant-numeric: tabular-nums;
      }
      .spend-chart text.value {
        fill: var(--sl-color-neutral-900);
      }
      .lead {
        font-size: var(--console-text-card-title);
        margin: var(--sl-spacing-small) 0 var(--sl-spacing-medium);
      }
      .detail-actions {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
      }
      @media (max-width: 639px) {
        .finding-table {
          display: none;
        }
        .cards {
          display: block;
        }
      }
    `,
  ];
  @state() private loading = true;
  @state() private busy = false;
  @state() private error = '';
  @state() private enabled = false;
  @state() private permissions: UserPermissions = [];
  @state() private runs: GovernanceRun[] = [];
  @state() private findings: GovernanceFinding[] = [];
  @state() private dismissed: GovernanceFinding[] = [];
  @state() private detail: GovernanceFinding | null = null;
  @state() private findingId = '';
  @state() private kind = '';
  @state() private severity = '';
  @state() private userId = '';
  @state() private runId = '';
  @state() private offset = 0;
  @state() private runOffset = 0;
  @state() private dismissedOffset = 0;
  @state() private loadingDismissed = false;
  @state() private search = '';
  @state() private sort: 'title' | 'severity' | 'created_at' = 'severity';
  @state() private descending = false;
  @state() private viewMode: ListViewMode = loadViewMode(
    'preloop.governance.view_mode'
  );
  @state() private developers: Record<string, string> = {};
  @state() private dismissedBudgetBanner = false;
  private generation = 0;
  @state() private dismissedOpen = false;
  /** A finding category from a `section` deep link, or '' for all. */
  @state() private section = '';
  /** The review mode, null when unreadable (no view_policies, old server). */
  @state() private reviewMode: string | null = null;
  @state() private unsubscribe: '' | 'ask' | 'done' = '';
  private scrollToSection = false;
  @state() private weeklyValueEnabled = false;
  private valueStart = '';
  private valueEnd = '';
  private valueDeepLink = false;

  connectedCallback(): void {
    super.connectedCallback();
    this.readQuery();
    void this.load();
  }
  /** Email links carry the run, the section and the unsubscribe intent. */
  private readQuery(): void {
    const params = new URLSearchParams(window.location.search);
    this.runId = params.get('run_id') ?? this.runId;
    const section = params.get('section') ?? '';
    this.section = section in SECTIONS ? section : '';
    this.scrollToSection = !!section;
    this.valueDeepLink = section === 'valuable_work';
    this.valueStart = params.get('start') ?? '';
    this.valueEnd = params.get('end') ?? '';
    if (params.get('unsubscribe') === 'weekly-report') this.unsubscribe = 'ask';
  }
  protected updated(): void {
    if (!this.scrollToSection || this.loading) return;
    this.scrollToSection = false;
    this.renderRoot
      .querySelector(
        this.valueDeepLink ? 'weekly-value-section' : '[aria-label="Findings"]'
      )
      ?.scrollIntoView({ block: 'start' });
  }
  private async stopEmails(): Promise<void> {
    this.busy = true;
    try {
      await updateWeeklyReportPreferences({ user_enabled: false });
      this.unsubscribe = 'done';
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Could not stop the emails.',
        'danger'
      );
    } finally {
      this.busy = false;
    }
  }
  private async turnReviewOn(): Promise<void> {
    this.busy = true;
    try {
      const settings = await updateGovernanceReviewSettings({
        mode:
          localStorage.getItem(LAST_ON_MODE_KEY) === 'on_own_model'
            ? 'on_own_model'
            : 'on_platform',
      });
      this.reviewMode = settings.mode;
      showToast('The AI usage policy review is on.', 'success');
    } catch (error) {
      showToast(
        error instanceof Error
          ? error.message
          : 'Could not turn the review on.',
        'danger'
      );
    } finally {
      this.busy = false;
    }
  }
  disconnectedCallback(): void {
    this.generation++;
    super.disconnectedCallback();
  }
  onBeforeEnter(location: { params: { findingId?: string } }): void {
    this.findingId = location.params.findingId ?? '';
    void this.load();
  }
  private get canManage(): boolean {
    return hasPermission(this.permissions, 'manage_policies');
  }
  private get canCost(): boolean {
    return hasPermission(this.permissions, 'view_cost');
  }
  private get selectedRun(): GovernanceRun | undefined {
    return this.runs.find((r) => r.id === this.runId) ?? this.runs[0];
  }
  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.loading = true;
    this.error = '';
    this.detail = null;
    try {
      const [features, profile] = await Promise.all([
        getFeatures(),
        getUserProfile(),
      ]);
      if (generation !== this.generation) return;
      this.enabled = hasCapability(features.features, 'governance_review');
      this.permissions = profile.permissions;
      this.weeklyValueEnabled = features.features?.weekly_work_value === true;
      if (!this.enabled) return;
      if (hasPermission(this.permissions, 'view_policies'))
        getGovernanceReviewSettings()
          .then((settings) => {
            if (generation === this.generation) this.reviewMode = settings.mode;
          })
          .catch(() => undefined);
      if (!hasPermission(this.permissions, 'view_policies') && !this.canCost)
        throw new Error(
          'Governance review requires View policies or View cost permission.'
        );
      const detail = this.findingId
        ? await getGovernanceFinding(this.findingId)
        : null;
      const runs = await getGovernanceRuns(this.runOffset, detail?.run_id);
      if (generation !== this.generation) return;
      if (!this.runId && runs.length) this.runId = runs[0].id;
      const findings = this.findingId
        ? []
        : await getGovernanceFindings(this.filters(false));
      if (generation !== this.generation) return;
      this.runs = runs;
      this.findings = findings;
      this.detail = detail;
      const developers = { ...this.developers };
      for (const row of findings)
        if (row.user_id)
          developers[row.user_id] =
            row.user_name || `Member ${row.user_id.slice(0, 8)}`;
      this.developers = developers;
      if (this.dismissedOpen && !this.findingId) {
        const dismissed = await getGovernanceFindings(this.filters(true));
        if (generation === this.generation) this.dismissed = dismissed;
      }
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not load governance review.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }
  private filters(dismissed: boolean) {
    return {
      kind: this.kind,
      severity: this.severity,
      user_id: this.userId,
      run_id: this.runId,
      category: this.section || undefined,
      dismissed,
      offset: dismissed ? this.dismissedOffset : this.offset,
    };
  }
  private changeFilter(event: Event): void {
    const target = event.target as HTMLInputElement;
    if (target.name === 'kind') this.kind = target.value;
    if (target.name === 'severity') this.severity = target.value;
    if (target.name === 'user_id') this.userId = target.value.trim();
    if (target.name === 'run_id') this.runId = target.value;
    this.offset = 0;
    this.dismissedOffset = 0;
    this.dismissed = [];
    void this.load();
  }
  private toggleDismissed(event: Event): void {
    this.dismissedOpen = (event.target as HTMLDetailsElement).open;
    if (this.dismissedOpen) void this.loadDismissed();
  }
  private async loadDismissed(): Promise<void> {
    const generation = this.generation;
    this.loadingDismissed = true;
    try {
      const rows = await getGovernanceFindings(this.filters(true));
      if (generation === this.generation) this.dismissed = rows;
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not load dismissed findings.';
    } finally {
      if (generation === this.generation) this.loadingDismissed = false;
    }
  }
  private async act(
    row: GovernanceFinding,
    reason?: DismissReason
  ): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.error = '';
    try {
      const updated = reason
        ? await dismissGovernanceFinding(row.id, reason)
        : await restoreGovernanceFinding(row.id);
      if (this.findingId === row.id) this.detail = updated;
      else await this.load();
      showToast(reason ? 'Finding dismissed.' : 'Finding restored.', 'success');
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not update finding.';
    } finally {
      this.busy = false;
    }
  }
  private actions(row: GovernanceFinding): ResourceAction[] {
    if (!this.canManage) return [];
    if (row.dismissed_at)
      return [
        {
          id: 'restore',
          label: 'Restore',
          disabled: this.busy,
          onClick: () => void this.act(row),
        },
      ];
    return (Object.entries(REASONS) as [DismissReason, string][]).map(
      ([reason, label]) => ({
        id: reason,
        label: `Dismiss: ${label}`,
        disabled: this.busy,
        onClick: () => void this.act(row, reason),
      })
    );
  }
  /** Unsubscribe confirmation and the review-off state, above everything. */
  private notices() {
    const unsubscribe =
      this.unsubscribe === 'ask'
        ? html`<div class="notice" data-unsubscribe>
            <p>
              <strong>Stop the weekly report email?</strong> You can turn it
              back on in Policies, AI usage policy.
            </p>
            <div class="detail-actions">
              <sl-button
                variant="primary"
                size="small"
                ?loading=${this.busy}
                @click=${() => void this.stopEmails()}
                >Stop my weekly emails</sl-button
              ><sl-button size="small" @click=${() => (this.unsubscribe = '')}
                >Keep them</sl-button
              >
            </div>
          </div>`
        : this.unsubscribe === 'done'
          ? html`<div class="notice" data-unsubscribed role="status">
              <p>
                You will no longer get the weekly report email. The review
                itself keeps running and stays here.
              </p>
            </div>`
          : nothing;
    const off =
      this.reviewMode === 'off'
        ? html`<div class="notice" data-review-off>
            <p>
              <strong>The AI usage policy review is off.</strong> No sessions
              are read, no review runs and nothing is spent. Findings below are
              from earlier weeks.
            </p>
            ${
              this.canManage
                ? html`<sl-button
                    variant="primary"
                    size="small"
                    ?loading=${this.busy}
                    @click=${() => void this.turnReviewOn()}
                    >Turn it on</sl-button
                  >`
                : html`<p class="muted">
                    Ask someone who manages policies to turn it on.
                  </p>`
            }
          </div>`
        : nothing;
    return html`${unsubscribe}${off}`;
  }
  private coverage(run?: GovernanceRun) {
    if (!run)
      return html`<p class="empty">
        No review runs yet. The first scheduled review runs on Monday after
        enablement.
      </p>`;
    return html`<div class="summary" data-coverage>
        <p>
          Week of ${day(run.window_start)} to ${day(run.window_end)} (UTC).
          Reviewed ${run.sessions_summarized} of ${run.sessions_considered}
          sessions.
        </p>
        <sl-badge
          class="chip"
          pill
          variant=${run.status === 'failed' ? 'danger' : run.status === 'partial' ? 'warning' : 'neutral'}
          >${humanize(run.status)}</sl-badge
        >
        ${this.canCost && run.platform_cost !== undefined ? html`<span>Spend est. ${formatUsdExact(run.platform_cost + (run.account_cost ?? 0))}</span>` : nothing}
      </div>
      ${run.sessions_summarized === 0 ? html`<p class="muted">Not yet reviewed. Session summaries start with the first hourly sweep after the review is turned on, and next week's review includes them. Findings from usage counts still appear below.</p>` : html`<p class="muted">Findings from summaries cover the ${run.sessions_summarized} reviewed sessions only.</p>`}
      ${run.status === 'partial' || run.stop_reason ? html`<sl-alert variant="warning" open class="partial"><sl-icon slot="icon" name="exclamation-triangle"></sl-icon>Partial review: ${run.stop_reason ? stopReason(run.stop_reason) : 'the run did not finish the full week'}. Findings cover only what was reviewed.</sl-alert>` : nothing}
      ${
        // Only the newest run says what the review is doing now.
        this.runOffset === 0 &&
        run.id === this.runs[0]?.id &&
        run.mode_after === 'off_over_budget' &&
        !this.dismissedBudgetBanner
          ? html`<sl-alert
              variant="warning"
              open
              closable
              class="over-budget"
              @sl-after-hide=${() => {
                this.dismissedBudgetBanner = true;
              }}
            >
              <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
              The weekly review is off: it used up the free analysis allowance.
              ${this.canManage && hasPermission(this.permissions, 'view_policies') ? html`<a href="/console/policies?tab=governance">Keep it on with your own model</a>.` : 'Ask someone who manages policies to keep it on with your own model.'}
            </sl-alert>`
          : nothing
      }`;
  }
  private sortHeader(key: 'title' | 'severity' | 'created_at', label: string) {
    return html`<th
      aria-sort=${this.sort === key ? (this.descending ? 'descending' : 'ascending') : 'none'}
    >
      <button
        @click=${() => {
          this.descending = this.sort === key ? !this.descending : false;
          this.sort = key;
        }}
      >
        ${label}
      </button>
    </th>`;
  }
  private findingList(rows: GovernanceFinding[], dismissed = false) {
    const filtered = rows.filter((row) =>
      `${row.title} ${row.rule_ref ?? ''} ${row.suggestion}`
        .toLowerCase()
        .includes(this.search.toLowerCase())
    );
    const rank = { high: 0, medium: 1, low: 2 };
    filtered.sort(
      (a, b) =>
        (this.sort === 'severity'
          ? rank[a.severity] - rank[b.severity]
          : a[this.sort].localeCompare(b[this.sort])) *
        (this.descending ? -1 : 1)
    );
    if (!filtered.length)
      return html`<p class="empty">
        ${dismissed ? 'No dismissed findings match these filters.' : this.search || this.kind || this.severity || this.userId ? 'No findings match these filters.' : !this.selectedRun || this.selectedRun.sessions_summarized === 0 ? 'Not yet reviewed. No findings from usage counts this week.' : 'No findings in the reviewed data.'}
      </p>`;
    return html`<div class="table-scroll finding-table">
        <table
          data-findings
          aria-label=${dismissed ? 'Dismissed findings' : 'Governance findings'}
        >
          <thead>
            <tr>
              ${this.sortHeader('title', 'Finding')}
              <th>Kind</th>
              ${this.sortHeader('severity', 'Severity')}
              <th>Policy rule</th>
              <th>Developer</th>
              ${dismissed ? html`<th>Dismissal</th>` : nothing}
              ${this.sortHeader('created_at', 'Found')}${this.canManage ? html`<th class="actions">Actions</th>` : nothing}
            </tr>
          </thead>
          <tbody>
            ${filtered.map(
              (row) =>
                html`<tr>
                  <td>
                    <a
                      href=${`/console/governance/findings/${encodeURIComponent(row.id)}`}
                      >${row.title}</a
                    >
                  </td>
                  <td title=${KIND_HELP[row.kind] ?? ''}>
                    ${humanize(row.kind)}
                  </td>
                  <td>
                    <sl-badge
                      class="chip"
                      pill
                      variant=${row.severity === 'high' ? 'warning' : 'neutral'}
                      >${humanize(row.severity)}</sl-badge
                    >
                  </td>
                  <td title=${row.rule_ref ?? ''}>
                    ${ruleLabel(row.rule_ref)}
                  </td>
                  <td title=${row.user_id ?? ''}>
                    ${row.user_name ?? (row.user_id ? `Member ${row.user_id.slice(0, 8)}` : 'Whole account')}
                  </td>
                  ${
                    dismissed
                      ? html`<td>
                          ${REASONS[row.dismissed_reason as DismissReason] ?? row.dismissed_reason}<br /><span
                            class="muted"
                            >${row.dismissed_by_name ?? 'Former member'} ·
                            <time
                              title=${formatLocalDateTime(row.dismissed_at!)}
                              >${formatRelativeTime(row.dismissed_at!)}</time
                            ></span
                          >
                        </td>`
                      : nothing
                  }
                  <td>
                    <time title=${formatLocalDateTime(row.created_at)}
                      >${formatRelativeTime(row.created_at)}</time
                    >
                  </td>
                  ${this.canManage ? html`<td class="actions"><resource-actions menu-only .actions=${this.actions(row)}></resource-actions></td>` : nothing}
                </tr>`
            )}
          </tbody>
        </table>
      </div>
      <div class="cards">
        ${filtered.map(
          (row) =>
            html`<article class="finding-card">
              <a
                href=${`/console/governance/findings/${encodeURIComponent(row.id)}`}
                >${row.title}</a
              >
              <p>
                ${humanize(row.kind)} · ${humanize(row.severity)} ·
                ${ruleLabel(row.rule_ref)}
              </p>
              ${dismissed ? html`<p class="muted">${REASONS[row.dismissed_reason as DismissReason] ?? row.dismissed_reason} · ${row.dismissed_by_name ?? 'Former member'} · ${formatRelativeTime(row.dismissed_at!)}</p>` : nothing}${this.canManage ? html`<resource-actions menu-only .actions=${this.actions(row)}></resource-actions>` : nothing}
            </article>`
        )}
      </div>`;
  }
  private history() {
    return html`<details data-history>
      <summary>Run history</summary>
      ${
        this.runs.length
          ? html`<div class="table-scroll">
              <table aria-label="Review run history">
                <thead>
                  <tr>
                    <th>Week (UTC)</th>
                    <th>Status</th>
                    <th>Started</th>
                    <th>Finished</th>
                    <th class="number">Segments</th>
                    ${this.canCost ? html`<th class="number">Spend est.</th>` : nothing}
                    <th>Stop reason</th>
                  </tr>
                </thead>
                <tbody>
                  ${this.runs.map(
                    (run) =>
                      html`<tr>
                        <td>
                          <button
                            @click=${() => {
                              this.runId = run.id;
                              this.offset = 0;
                              void this.load();
                            }}
                          >
                            ${day(run.window_start)}
                          </button>
                        </td>
                        <td>
                          <sl-badge
                            class="chip"
                            pill
                            variant=${run.status === 'failed' ? 'danger' : run.status === 'partial' ? 'warning' : 'neutral'}
                            >${humanize(run.status)}</sl-badge
                          >
                        </td>
                        <td>
                          <time title=${formatLocalDateTime(run.started_at)}
                            >${formatRelativeTime(run.started_at)}</time
                          >
                        </td>
                        <td>
                          ${run.finished_at ? html`<time title=${formatLocalDateTime(run.finished_at)}>${formatRelativeTime(run.finished_at)}</time>` : run.status === 'running' ? 'In progress' : 'Not recorded'}
                        </td>
                        <td class="number">${run.segments}</td>
                        ${this.canCost ? html`<td class="number">${run.platform_cost !== undefined ? formatUsdExact(run.platform_cost + (run.account_cost ?? 0)) : 'Unavailable'}</td>` : nothing}
                        <td>
                          ${run.stop_reason ? stopReason(run.stop_reason) : ''}
                        </td>
                      </tr>`
                  )}
                </tbody>
              </table>
            </div>`
          : html`<p class="empty">No runs yet.</p>`
      }
      <div class="pager">
        <sl-button
          ?disabled=${this.runOffset === 0 || this.loading}
          @click=${() => {
            this.runOffset -= 25;
            this.runId = '';
            this.offset = 0;
            void this.load();
          }}
          >Newer runs</sl-button
        ><sl-button
          ?disabled=${this.runs.length < 25 || this.loading}
          @click=${() => {
            this.runOffset += 25;
            this.runId = '';
            this.offset = 0;
            void this.load();
          }}
          >Older runs</sl-button
        >
      </div>
    </details>`;
  }
  /** The outlier day against the rest of the window, then who spent it. */
  private spendEvidence(spend: SpendEvidence) {
    const series = spend.daily_series ?? [];
    const sessions = spend.contributing_sessions ?? [];
    const sentence =
      spend.ratio != null && spend.baseline != null
        ? `${weekday(spend.day)} cost ${money(spend.cost)}, ${spend.ratio.toFixed(1)}x your usual ${money(spend.baseline)} a day.`
        : `${weekday(spend.day)} cost ${money(spend.cost)}.`;
    const width = 640;
    const height = 200;
    const top = 20;
    const bottom = 24;
    const max = Math.max(
      spend.cost,
      spend.baseline ?? 0,
      ...series.map((d) => d.cost),
      0.01
    );
    const step = series.length ? width / series.length : width;
    const y = (value: number) =>
      top + (height - top - bottom) * (1 - value / max);
    return html`<p class="lead" data-spend-sentence>${sentence}</p>
      ${
        series.length
          ? html`<svg
              class="spend-chart"
              data-spend-chart
              viewBox="0 0 ${width} ${height}"
              role="img"
              aria-label=${`Daily spend: ${series.map((d) => `${weekday(d.day)} ${money(d.cost)}`).join(', ')}`}
            >
              ${series.map(
                (d, index) =>
                  html`<rect
                      class=${d.is_outlier ? 'bar outlier' : 'bar'}
                      x=${index * step + step * 0.2}
                      y=${y(d.cost)}
                      width=${step * 0.6}
                      height=${Math.max(0, height - bottom - y(d.cost))}
                      rx="2"
                    ></rect>
                    ${d.is_outlier ? html`<text class="value" x=${index * step + step / 2} y=${y(d.cost) - 6} text-anchor="middle">${money(d.cost)}</text>` : nothing}
                    <text
                      x=${index * step + step / 2}
                      y=${height - 6}
                      text-anchor="middle"
                      >${weekday(d.day)}</text
                    >`
              )}
              ${
                spend.baseline != null
                  ? html`<line
                        class="baseline"
                        x1="0"
                        x2=${width}
                        y1=${y(spend.baseline)}
                        y2=${y(spend.baseline)}
                      ></line>
                      <text
                        x=${width}
                        y=${y(spend.baseline) - 4}
                        text-anchor="end"
                        >usual ${money(spend.baseline)}</text
                      >`
                  : nothing
              }
            </svg>`
          : nothing
      }
      <h3>Sessions that drove it</h3>
      ${
        sessions.length
          ? html`<div class="table-scroll">
              <table
                data-contributing-sessions
                aria-label="Sessions that drove the spend"
              >
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Agent or flow</th>
                    <th>Started</th>
                    ${this.canCost ? html`<th class="number">Spend est.</th>` : nothing}
                  </tr>
                </thead>
                <tbody>
                  ${sessions.map(
                    (session) =>
                      html`<tr>
                        <td>
                          <a
                            href=${session.href ?? `/console/runtime-sessions?sessionId=${encodeURIComponent(session.runtime_session_id)}`}
                            >${sessionName(session)}</a
                          >
                        </td>
                        <td>
                          ${
                            session.flow_href
                              ? html`Flow
                                  <a href=${session.flow_href}
                                    >${session.flow_name ?? 'Open flow'}</a
                                  >`
                              : session.agent_href
                                ? html`<a href=${session.agent_href}
                                    >${session.agent_name ?? 'Open agent'}</a
                                  >`
                                : (session.agent_name ??
                                  html`<span class="muted">Unattributed</span>`)
                          }
                        </td>
                        <td>
                          ${session.started_at ? html`<time title=${formatLocalDateTime(session.started_at)}>${formatRelativeTime(session.started_at)}</time>` : ''}
                        </td>
                        ${this.canCost ? html`<td class="number">${session.cost != null ? money(session.cost) : ''}</td>` : nothing}
                      </tr>`
                  )}
                </tbody>
              </table>
            </div>`
          : html`<p class="muted">
              No session-level spend was recorded for that day.
            </p>`
      }`;
  }
  /** What keeping a tool costs against what detaching it takes away. */
  private toolEvidence(tool: ToolEvidence) {
    const unused = tool.sessions_used === 0;
    return html`<p class="lead" data-tool-sentence>
        ${tool.name} was used in ${tool.sessions_used} of
        ${tool.sessions_attached} sessions it was attached to.
      </p>
      <dl data-tool-tradeoff>
        <dt>Schema cost</dt>
        <dd>
          ${tool.schema_tokens_per_request != null ? `About ${tokens(tool.schema_tokens_per_request)} tokens on every request` : 'Not measured'}
        </dd>
        <dt>Saving if detached</dt>
        <dd>
          ${tool.estimated_weekly_tokens != null ? `About ${tokens(tool.estimated_weekly_tokens)} tokens a week` : 'Not measured'}${this.canCost && tool.estimated_weekly_savings != null ? `, about ${money(tool.estimated_weekly_savings)} est.` : ''}
        </dd>
        <dt>What the agent loses</dt>
        <dd>
          ${tool.capability ? tool.capability.replace(/^./, (c) => c.toUpperCase()) : 'Not described'}
        </dd>
      </dl>
      <p class="muted">
        ${unused ? 'Unused this week, so detaching it costs nothing observed. Keep it if the capability matters for rare work.' : 'Rarely used but used: this is a trade-off, not a fix. Detach it only if the saving is worth more than the capability.'}
      </p>`;
  }
  private detailView(row: GovernanceFinding) {
    const week = this.runs.find((run) => run.id === row.run_id);
    return html`<a href="/console/governance">All findings</a>
      <h2>${row.title}</h2>
      ${week ? html`<p class="muted">Week of ${day(week.window_start)} to ${day(week.window_end)} (UTC)</p>` : nothing}
      <dl>
        <dt>Kind</dt>
        <dd>
          ${humanize(row.kind)}
          <span class="muted">${KIND_HELP[row.kind] ?? ''}</span>
        </dd>
        <dt>Severity</dt>
        <dd>${humanize(row.severity)}</dd>
        <dt>Rule</dt>
        <dd>
          ${ruleLabel(row.rule_ref)}
          ${row.rule_ref ? html`<code class="muted">${row.rule_ref}</code>` : nothing}
          ${hasPermission(this.permissions, 'view_policies') ? html`<a href="/console/policies?tab=governance">Open the policy</a>` : nothing}
        </dd>
        ${
          row.agent_ref
            ? html`<dt>Agent</dt>
                <dd>${row.agent_ref}</dd>`
            : nothing
        }${
          row.user_id
            ? html`<dt>Developer</dt>
                <dd title=${row.user_id}>
                  ${row.user_name ?? `Member ${row.user_id.slice(0, 8)}`}
                </dd>`
            : nothing
        }${
          this.canCost && row.estimated_weekly_savings != null
            ? html`<dt>Estimated weekly savings</dt>
                <dd>${formatUsdExact(row.estimated_weekly_savings)}</dd>`
            : nothing
        }
      </dl>
      <h3>Suggestion</h3>
      <p>${row.suggestion}</p>
      <h3>Evidence</h3>
      ${
        row.evidence.length
          ? row.evidence.map((evidence) =>
              evidence.spend
                ? this.spendEvidence(evidence.spend)
                : evidence.tool
                  ? this.toolEvidence(evidence.tool)
                  : html`<blockquote class="evidence">
                        ${evidence.excerpt}
                      </blockquote>
                      <p class="muted">
                        ${evidenceSource(evidence.source)}
                        ${evidence.audit_event_id ? html`<a href=${`/console/audit?event=${encodeURIComponent(evidence.audit_event_id)}`}>Open in audit</a>` : nothing}
                      </p>`
            )
          : html`<p>No excerpts recorded.</p>`
      }
      <h3>Sessions</h3>
      ${
        row.session_ids.length
          ? html`<ul>
              ${row.session_ids.map((id) => html`<li><a href=${`/console/runtime-sessions?sessionId=${encodeURIComponent(id)}`}>Session ${id.slice(0, 8)}</a></li>`)}
            </ul>`
          : html`<p>Account-wide finding.</p>`
      }
      ${row.dismissed_at ? html`<p>Dismissed: ${REASONS[row.dismissed_reason as DismissReason] ?? row.dismissed_reason} · ${formatLocalDateTime(row.dismissed_at)}${row.dismissed_by_id ? html` · by ${row.dismissed_by_name ?? `Member ${row.dismissed_by_id.slice(0, 8)}`}` : nothing}</p>` : nothing}
      ${this.canManage ? html`<div class="detail-actions">${row.dismissed_at ? html`<sl-button data-restore ?disabled=${this.busy} @click=${() => void this.act(row)}>Restore</sl-button>` : (Object.entries(REASONS) as [DismissReason, string][]).map(([reason, label]) => html`<sl-button data-dismiss=${reason} ?disabled=${this.busy} @click=${() => void this.act(row, reason)}>Dismiss: ${label}</sl-button>`)}</div>` : nothing}`;
  }
  render() {
    if (!this.enabled && !this.loading && !this.error) return nothing;
    return html`<view-header
        headerText="Weekly review"
        description="Each Monday, last week's agent sessions are checked against your AI usage policy. The review reports; it never blocks work."
      >
        ${hasPermission(this.permissions, 'view_policies') ? html`<a slot="main-column" href="/console/policies?tab=governance">Edit the policy</a>` : nothing}
      </view-header>
      ${
        this.error
          ? html`<p class="error" role="alert">${this.error}</p>
              <sl-button @click=${() => void this.load()}>Retry</sl-button>`
          : nothing
      }
      ${
        this.loading
          ? html`<p role="status">Loading governance review…</p>`
          : this.error && !this.runs.length && !this.detail
            ? nothing
            : this.findingId
              ? this.detail
                ? this.detailView(this.detail)
                : nothing
              : html` ${this.notices()} ${this.coverage(this.selectedRun)}
                  ${
                    this.weeklyValueEnabled && this.canCost
                      ? html`<weekly-value-section
                          .start=${this.valueDeepLink ? this.valueStart : (this.selectedRun?.window_start ?? '')}
                          .end=${this.valueDeepLink ? this.valueEnd : (this.selectedRun?.window_end ?? '')}
                        ></weekly-value-section>`
                      : nothing
                  }
                  <dl class="kinds" data-kind-legend>
                    ${Object.entries(KIND_HELP).map(
                      ([kind, help]) =>
                        html`<dt>${humanize(kind)}</dt>
                          <dd>${help}</dd>`
                    )}
                  </dl>
                  <section
                    class=${`collection ${this.viewMode === 'cards' ? 'cards-active' : ''}`}
                    aria-label="Findings"
                  >
                    <list-toolbar
                      .search=${this.search}
                      searchPlaceholder="Search this page"
                      toggleLabel="Findings view"
                      .view=${this.viewMode}
                      .views=${['list', 'cards']}
                      @search-change=${(
                        event: CustomEvent<{ value: string }>
                      ) => {
                        this.search = event.detail.value;
                      }}
                      @view-change=${(
                        event: CustomEvent<{ value: ListViewMode }>
                      ) => {
                        this.viewMode = event.detail.value;
                        saveViewMode(
                          'preloop.governance.view_mode',
                          this.viewMode
                        );
                      }}
                    >
                      <label
                        >Kind<select
                          name="kind"
                          .value=${this.kind}
                          @change=${this.changeFilter}
                        >
                          <option value="">All kinds</option>
                          ${
                            hasPermission(this.permissions, 'view_policies')
                              ? html`<option value="violation">
                                    Violations
                                  </option>
                                  <option value="anomaly">Anomalies</option>`
                              : nothing
                          }${this.canCost ? html`<option value="inefficiency">Inefficiencies</option>` : nothing}
                        </select></label
                      >
                      <label
                        >Severity<select
                          name="severity"
                          .value=${this.severity}
                          @change=${this.changeFilter}
                        >
                          <option value="">All severities</option>
                          <option value="high">High</option>
                          <option value="medium">Medium</option>
                          <option value="low">Low</option>
                        </select></label
                      >
                      <label
                        >Developer<select
                          name="user_id"
                          .value=${this.userId}
                          @change=${this.changeFilter}
                        >
                          <option value="">All developers</option>
                          ${Object.entries(this.developers).map(([id, name]) => html`<option value=${id}>${name}</option>`)}
                        </select></label
                      >
                      <label
                        >Week (UTC)<select
                          name="run_id"
                          .value=${this.runId}
                          @change=${this.changeFilter}
                        >
                          <option value="" disabled>Select a week</option>
                          ${this.runs.map((run) => html`<option value=${run.id} ?selected=${run.id === this.selectedRun?.id}>${day(run.window_start)}</option>`)}
                        </select></label
                      >
                      <span slot="count"
                        >${!this.findings.length && (!this.selectedRun || this.selectedRun.sessions_summarized === 0) ? 'Not yet reviewed' : `${this.findings.filter((row) => `${row.title} ${row.rule_ref ?? ''} ${row.suggestion}`.toLowerCase().includes(this.search.toLowerCase())).length} of ${this.findings.length} findings on this page`}${this.offset ? ` · starting at ${this.offset + 1}` : ''}</span
                      >
                    </list-toolbar>
                    ${
                      this.section
                        ? html`<p class="section-filter" data-section>
                            Showing ${SECTIONS[this.section]}.
                            <a
                              href="/console/governance"
                              @click=${(event: Event) => {
                                event.preventDefault();
                                this.section = '';
                                history.replaceState(
                                  null,
                                  '',
                                  `/console/governance${this.runId ? `?run_id=${encodeURIComponent(this.runId)}` : ''}`
                                );
                                void this.load();
                              }}
                              >Show all findings</a
                            >
                          </p>`
                        : nothing
                    }
                    ${this.findingList(this.findings)}
                    <div class="pager">
                      <sl-button
                        ?disabled=${this.offset === 0 || this.loading}
                        @click=${() => {
                          this.offset -= 50;
                          void this.load();
                        }}
                        >Previous findings</sl-button
                      ><sl-button
                        ?disabled=${this.findings.length < 50 || this.loading}
                        @click=${() => {
                          this.offset += 50;
                          void this.load();
                        }}
                        >Next findings</sl-button
                      >
                    </div>
                    <details
                      data-dismissed
                      .open=${this.dismissedOpen}
                      @toggle=${this.toggleDismissed}
                    >
                      <summary>Dismissed findings</summary>
                      ${this.dismissedOpen ? this.findingList(this.dismissed, true) : nothing}${
                        this.dismissedOpen
                          ? html`<div class="pager">
                              <sl-button
                                ?disabled=${this.dismissedOffset === 0 || this.loadingDismissed}
                                @click=${() => {
                                  this.dismissedOffset -= 50;
                                  void this.loadDismissed();
                                }}
                                >Previous dismissed</sl-button
                              ><sl-button
                                ?disabled=${this.dismissed.length < 50 || this.loadingDismissed}
                                @click=${() => {
                                  this.dismissedOffset += 50;
                                  void this.loadDismissed();
                                }}
                                >Next dismissed</sl-button
                              >
                            </div>`
                          : nothing
                      }
                    </details>
                  </section>
                  ${this.history()}`
      }`;
  }
}
