import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import consoleStyles from '../../styles/console-styles.css?inline';
import {
  actOnDeliveryReport,
  getDeliveryReport,
  type ClosedIssue,
  type DeliveryReportAction,
  type Criterion,
  type DeliveryReportDetail,
  type FollowUp,
  type ProposalDecision,
  type ReopenRequest,
  type ShippedChange,
} from '../../delivery-reports-api';
import { PermissionError } from '../../permissions';
import { formatRelativeTime, parseUTCDate } from '../../utils/date';
import {
  denominators,
  flagLabel,
  periodLabel,
  runLabel,
  sourceLabel,
  verdictChip,
  verdictCounts,
} from '../../utils/delivery-report';
import { isSafeSubjectUrl } from '../../utils/execution-subject';
import { spendLabel } from './delivery-reports-view';
import '../../components/view-header';
import '../../components/permission-denied';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

/** Why a proposal action is disabled on a server that cannot act on it. */
export const ACTIONS_UNAVAILABLE =
  'Not available: acting on proposals from the report is not supported by this server.';

/** The decision's label in place of the buttons once a proposal is decided. */
export function decisionLabel(decision: ProposalDecision): string {
  if (decision.status === 'dismissed')
    return decision.reason ? `Dismissed: ${decision.reason}` : 'Dismissed';
  if (decision.status === 'pending') return 'In progress';
  return decision.kind === 'reopen' ? 'Reopened' : 'Issue opened';
}

/** An anchor id for a project or area name. */
export function anchorId(prefix: string, name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return `${prefix}-${slug || 'none'}`;
}

/**
 * `/console/delivery-reports/:reportId`: one run's full report.
 *
 * Header, a hairline summary strip with four separate numbers (never one
 * score), then Needs you, Shipped by project, Closed issues, Open past the
 * window, Since the previous report, Open questions and What this run spent.
 * Every section and project has an anchor so a short update can link to it.
 */
@customElement('delivery-report-view')
export class DeliveryReportView extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .back {
        display: inline-flex;
        gap: 4px;
        align-items: center;
        margin-bottom: 8px;
        font-size: var(--console-text-meta);
      }
      a {
        color: var(--sl-color-primary-700);
        text-decoration: none;
      }
      a:hover,
      a:focus-visible {
        text-decoration: underline;
      }
      .header-actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }
      .summary-strip {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 8px 28px;
        padding: 12px 0;
        border-top: 1px solid var(--console-hairline);
        border-bottom: 1px solid var(--console-hairline);
        margin-bottom: 16px;
      }
      .strip-item {
        display: flex;
        align-items: baseline;
        gap: 6px;
      }
      .strip-label,
      .meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .strip-value {
        color: var(--sl-color-neutral-900);
        font-variant-numeric: tabular-nums;
      }
      .notice {
        margin: 0 0 16px;
        color: var(--console-meta-color);
      }
      section {
        background: var(--console-surface);
        border-radius: var(--sl-border-radius-large);
        margin-bottom: 16px;
        padding: 4px 0 8px;
      }
      section > h2 {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 0;
        padding: 12px 16px;
        font-size: var(--console-text-body);
        font-weight: 600;
      }
      section > h2 .meta {
        margin-left: auto;
        font-weight: 400;
      }
      h3 {
        margin: 12px 16px 4px;
        font-size: var(--console-text-meta);
        font-weight: 500;
        color: var(--console-meta-color);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      ul.rows {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      ul.rows > li {
        border-top: 1px solid var(--console-hairline);
        padding: 10px 16px;
      }
      .row-head {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 6px 12px;
      }
      .row-head .title {
        flex: 1 1 240px;
        min-width: 0;
      }
      .row-head .end {
        margin-left: auto;
        display: inline-flex;
        gap: 8px;
        align-items: baseline;
        font-variant-numeric: tabular-nums;
      }
      .toggle {
        background: none;
        border: 0;
        padding: 0;
        cursor: pointer;
        color: var(--sl-color-neutral-700);
        display: inline-flex;
        align-items: center;
      }
      .ref {
        font-variant-numeric: tabular-nums;
      }
      .detail {
        margin: 8px 0 0 22px;
        color: var(--sl-color-neutral-700);
        font-size: var(--console-text-meta);
      }
      .detail ul {
        margin: 4px 0;
        padding-left: 18px;
      }
      .proposal-actions {
        display: flex;
        gap: 8px;
        margin-left: auto;
      }
      .dismiss-form {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-end;
        gap: 8px;
        margin: 8px 0 0 22px;
        font-size: var(--console-text-meta);
      }
      .dismiss-form label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        flex: 1 1 240px;
      }
      .dismiss-form input {
        font: inherit;
        padding: 4px 8px;
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-medium);
      }
      .empty {
        padding: 12px 16px;
        color: var(--console-meta-color);
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: 8px 16px;
        border-top: 1px solid var(--console-hairline);
      }
      th {
        color: var(--console-meta-color);
        font-weight: 500;
      }
      .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      sl-badge.chip-warning::part(base) {
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 16%,
          transparent
        );
        color: var(--sl-color-warning-800);
        border: 0;
      }
      sl-badge.chip-success::part(base) {
        background: color-mix(
          in srgb,
          var(--sl-color-success-500) 16%,
          transparent
        );
        color: var(--sl-color-success-800);
        border: 0;
      }
      sl-badge.chip-neutral::part(base) {
        background: color-mix(
          in srgb,
          var(--sl-color-neutral-500) 16%,
          transparent
        );
        color: var(--sl-color-neutral-800);
        border: 0;
      }
      @media (max-width: 640px) {
        .row-head .end,
        .proposal-actions {
          margin-left: 0;
        }
        .detail {
          margin-left: 0;
        }
      }
    `,
  ];

  @property({ type: String }) reportId = '';

  @state() private detail: DeliveryReportDetail | null = null;
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private permissionError: PermissionError | null = null;
  @state() private expanded = new Set<string>();
  /** The proposal whose action is in flight. */
  @state() private busyKey: string | null = null;
  /** The proposal whose Dismiss form is open, and its reason. */
  @state() private dismissKey: string | null = null;
  @state() private dismissReason = '';
  @state() private actionError: { key: string; message: string } | null = null;

  /** Vaadin Router hands the route params over before the view connects. */
  onBeforeEnter(location: { params: Record<string, unknown> }): void {
    this.reportId = String(location.params.reportId ?? '');
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  async load(): Promise<void> {
    if (!this.reportId) {
      this.error = 'No report selected.';
      this.loading = false;
      return;
    }
    this.loading = true;
    try {
      this.detail = await getDeliveryReport(this.reportId);
      this.error = null;
    } catch (error) {
      if (error instanceof PermissionError) {
        this.permissionError = error;
      } else {
        this.error = 'Could not load this delivery report.';
      }
    } finally {
      this.loading = false;
    }
  }

  private async act(key: string, action: DeliveryReportAction): Promise<void> {
    this.busyKey = key;
    this.actionError = null;
    try {
      await actOnDeliveryReport(this.reportId, action);
      this.dismissKey = null;
      this.dismissReason = '';
    } catch (error) {
      this.actionError = {
        key,
        message: error instanceof Error ? error.message : String(error),
      };
    } finally {
      this.busyKey = null;
    }
    // Reload either way: a refusal (stale, already decided) is shown from
    // the current state of the report.
    await this.load();
  }

  private toggle(key: string): void {
    const next = new Set(this.expanded);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expanded = next;
  }

  private chip(variant: string, label: string, title = '') {
    return html`<sl-badge
      pill
      variant=${variant}
      class="chip-${variant}"
      title=${title}
      data-testid="chip"
      data-variant=${variant}
      >${label}</sl-badge
    >`;
  }

  private verdict(verdict: string | undefined) {
    const chip = verdictChip(verdict);
    return this.chip(chip.variant, chip.label, chip.title);
  }

  private time(value: string | null | undefined) {
    if (!value) return nothing;
    return html`<span class="meta" title=${parseUTCDate(value).toLocaleString()}
      >${formatRelativeTime(value, new Date(), { maxRelativeDays: 3650 })}</span
    >`;
  }

  private external(url: string | null | undefined, label: string) {
    // Report URLs come from the run's agent: only http(s) and console paths
    // become links, anything else (a javascript: URL) is plain text.
    if (!url || !isSafeSubjectUrl(url)) {
      return html`<span class="ref">${label}</span>`;
    }
    return html`<a
      class="ref"
      href=${url}
      target="_blank"
      rel="noopener noreferrer"
      >${label}<sl-icon
        name="box-arrow-up-right"
        label="Opens in a new tab"
      ></sl-icon
    ></a>`;
  }

  private expander(key: string, label: string) {
    const open = this.expanded.has(key);
    return html`<button
      class="toggle"
      aria-expanded=${open ? 'true' : 'false'}
      aria-controls="detail-${key}"
      aria-label="${open ? 'Hide' : 'Show'} evidence for ${label}"
      @click=${() => this.toggle(key)}
    >
      <sl-icon name=${open ? 'chevron-down' : 'chevron-right'}></sl-icon>
    </button>`;
  }

  private criteria(criteria: Criterion[] | undefined) {
    if (!criteria?.length) return html`<div>No criteria stated.</div>`;
    return html`<ul>
      ${criteria.map(
        (c) =>
          html`<li>
            ${c.met === true ? 'Met' : c.met === false ? 'Not met' : 'Not shown'}:
            ${c.text}
            ${
              c.evidence?.length
                ? html`<ul>
                    ${c.evidence.map((e) => html`<li>${e}</li>`)}
                  </ul>`
                : c.met !== true
                  ? html`<span class="meta">(evidence gap)</span>`
                  : nothing
            }
          </li>`
      )}
    </ul>`;
  }

  private actionButton(
    label: string,
    enabled: boolean,
    variant = 'default',
    onClick?: () => void,
    disabledTitle = ACTIONS_UNAVAILABLE
  ) {
    return html`<sl-button
      size="small"
      variant=${variant}
      ?disabled=${!enabled || this.busyKey !== null}
      title=${enabled ? label : disabledTitle}
      data-testid="action-${label.toLowerCase().replace(/\s+/g, '-')}"
      @click=${() => onClick?.()}
      >${label}</sl-button
    >`;
  }

  /** Buttons for an open proposal, or its decision once it has one. */
  private proposalActions(
    key: string,
    decision: ProposalDecision | undefined,
    primary: {
      label: string;
      enabled: boolean;
      title?: string;
      run: () => void;
    },
    detail: DeliveryReportDetail
  ) {
    if (decision) {
      const label = decisionLabel(decision);
      return html`<span class="proposal-actions" data-testid="decision">
        ${this.chip(decision.status === 'dismissed' ? 'neutral' : 'success', label)}
        ${
          decision.issue_url
            ? this.external(decision.issue_url, 'View issue')
            : nothing
        }
      </span>`;
    }
    return html`<span class="proposal-actions">
      ${this.actionButton(
        primary.label,
        primary.enabled,
        'primary',
        primary.run,
        primary.title
      )}
      ${this.actionButton('Dismiss', detail.actions.dismiss, 'default', () => {
        this.dismissKey = key;
        this.dismissReason = '';
      })}
    </span>`;
  }

  /** The inline Dismiss form and any refusal for one proposal. */
  private proposalFooter(key: string) {
    const error =
      this.actionError?.key === key
        ? html`<div class="meta" role="alert" data-testid="action-error">
            ${this.actionError.message}
          </div>`
        : nothing;
    if (this.dismissKey !== key) return error;
    return html`${error}
      <form
        class="dismiss-form"
        data-testid="dismiss-form"
        @submit=${(e: Event) => {
          e.preventDefault();
          const reason = this.dismissReason.trim();
          if (reason) void this.act(key, { kind: 'dismiss', key, reason });
        }}
      >
        <label
          >Why dismiss it?
          <input
            type="text"
            maxlength="2000"
            required
            .value=${this.dismissReason}
            @input=${(e: Event) => {
              this.dismissReason = (e.target as HTMLInputElement).value;
            }}
        /></label>
        <sl-button
          size="small"
          type="submit"
          ?disabled=${!this.dismissReason.trim() || this.busyKey !== null}
          data-testid="dismiss-confirm"
          >Dismiss</sl-button
        >
        <sl-button
          size="small"
          variant="text"
          @click=${() => {
            this.dismissKey = null;
          }}
          >Cancel</sl-button
        >
      </form>`;
  }

  private renderStrip(detail: DeliveryReportDetail) {
    const summary = detail.report.summary;
    return html`<div class="summary-strip" data-testid="summary-strip">
      <span class="strip-item"
        ><span class="strip-label">Shipped</span
        ><span class="strip-value">${summary?.shipped ?? 0}</span></span
      >
      <span class="strip-item"
        ><span class="strip-label">Closed</span
        ><span class="strip-value">${summary?.closed ?? 0}</span></span
      >
      ${denominators(summary).map(
        (d) =>
          html`<span
            class="strip-item"
            data-testid="denominator-${d.key}"
            title=${d.title}
            ><span class="strip-label">${d.label}</span
            ><span class="strip-value">${d.part} of ${d.total}</span></span
          >`
      )}
      <span class="strip-item" data-testid="verdict-counts"
        ><span class="strip-label">Closed issue verdicts</span
        ><span class="strip-value"
          >${verdictCounts(summary).map(
            (v, i) =>
              html`${i ? ' · ' : ''}<span title=${verdictChip(v.verdict).title}
                  >${verdictChip(v.verdict).label} ${v.count}</span
                >`
          )}</span
        ></span
      >
      <span class="strip-item"
        ><span class="strip-label">Spend</span
        ><span class="strip-value">${spendLabel(detail.cost_usd)}</span></span
      >
    </div>`;
  }

  private renderNeedsYou(detail: DeliveryReportDetail) {
    const reopens = detail.report.reopen_requests ?? [];
    const followUps = detail.report.follow_ups ?? [];
    const count = reopens.length + followUps.length;
    return html`<section id="needs-you" data-testid="needs-you">
      <h2>
        Needs you ${count ? this.chip('neutral', String(count)) : nothing}
        <span class="meta">Proposals; opening one is the approval</span>
      </h2>
      ${
        count === 0
          ? html`<div class="empty">Nothing proposed in this period.</div>`
          : nothing
      }
      ${
        count > 0 &&
        !(
          detail.actions.open_issue &&
          detail.actions.reopen &&
          detail.actions.dismiss
        )
          ? html`<p class="empty" data-testid="actions-note">
              ${ACTIONS_UNAVAILABLE}
            </p>`
          : nothing
      }
      ${
        reopens.length
          ? html`<h3>Reopen requests</h3>
              <ul class="rows">
                ${reopens.map((r) => this.renderReopen(r, detail))}
              </ul>`
          : nothing
      }
      ${
        followUps.length
          ? html`<h3>Proposed follow-ups</h3>
              <ul class="rows">
                ${followUps.map((f, i) => this.renderFollowUp(f, i, detail))}
              </ul>`
          : nothing
      }
    </section>`;
  }

  private renderReopen(request: ReopenRequest, detail: DeliveryReportDetail) {
    const stale = detail.stale_reopen_requests[request.ref];
    const key = `reopen:${request.ref}`;
    const decision = detail.report.proposal_actions?.[key];
    return html`<li
      data-testid="reopen-request"
      data-stale=${stale ? 'true' : 'false'}
    >
      <div class="row-head">
        <span class="title"
          >Reopen ${this.external(request.url, request.ref)}
          ${request.title ?? ''}</span
        >
        ${stale ? this.chip('neutral', 'Stale', stale) : nothing}
        ${this.proposalActions(
          key,
          decision,
          {
            label: 'Reopen',
            enabled: !stale && detail.actions.reopen,
            title: stale
              ? `${stale} Assess the issue again before reopening it.`
              : undefined,
            run: () => void this.act(key, { kind: 'reopen', ref: request.ref }),
          },
          detail
        )}
      </div>
      <div class="detail">
        ${request.reason ? html`<div>${request.reason}</div>` : nothing}
        <div class="meta">
          Assessed at
          ${request.revision.slice(0, 8)}${stale ? ` · ${stale}` : ''}
        </div>
        ${
          request.evidence?.length
            ? html`<ul>
                ${request.evidence.map((e) => html`<li>${e}</li>`)}
              </ul>`
            : nothing
        }
        ${this.proposalFooter(key)}
      </div>
    </li>`;
  }

  private renderFollowUp(
    item: FollowUp,
    index: number,
    detail: DeliveryReportDetail
  ) {
    const key = `follow_up:${index}`;
    // A follow-up the run opened itself under the policy is already decided.
    const decision: ProposalDecision | undefined = item.issue_url
      ? { kind: 'open_issue', status: 'applied', issue_url: item.issue_url }
      : detail.report.proposal_actions?.[key];
    return html`<li data-testid="follow-up">
      <div class="row-head">
        <span class="title">${item.title ?? 'Follow-up'}</span>
        ${this.proposalActions(
          key,
          decision,
          {
            label: 'Open issue',
            enabled: detail.actions.open_issue,
            run: () => void this.act(key, { kind: 'open_issue', index }),
          },
          detail
        )}
      </div>
      ${
        item.reason || item.evidence?.length
          ? html`<div class="detail">
              ${item.reason ? html`<div>${item.reason}</div>` : nothing}
              ${
                item.evidence?.length
                  ? html`<ul>
                      ${item.evidence.map((e) => html`<li>${e}</li>`)}
                    </ul>`
                  : nothing
              }
            </div>`
          : nothing
      }
      ${this.proposalFooter(key)}
    </li>`;
  }

  private renderChange(change: ShippedChange) {
    const key = `pr-${change.ref}`;
    const open = this.expanded.has(key);
    const flags = change.risk_flags ?? [];
    return html`<li data-testid="shipped-change">
      <div class="row-head">
        ${this.expander(key, change.ref)}
        ${this.external(change.url, change.ref.split('#').length > 1 ? `#${change.ref.split('#').pop()}` : change.ref)}
        <span class="title">${change.title ?? ''}</span>
        <span class="meta">${change.area ?? ''}</span>
        <span class="end">
          ${this.verdict(change.purpose?.verdict)}
          ${flags.map((f) =>
            this.chip(
              'warning',
              flagLabel(f.flag),
              (f.evidence ?? []).join('\n')
            )
          )}
          ${
            change.evidence_gaps?.length
              ? this.chip(
                  'neutral',
                  `${change.evidence_gaps.length} not verified`,
                  'Evidence gaps the change itself names'
                )
              : nothing
          }
          ${
            typeof change.cost_usd === 'number'
              ? html`<span class="meta">${spendLabel(change.cost_usd)}</span>`
              : nothing
          }
        </span>
      </div>
      ${
        open
          ? html`<div class="detail" id="detail-${key}">
              ${change.summary ? html`<div>${change.summary}</div>` : nothing}
              <div class="meta">
                Verdict
                ${sourceLabel(change.purpose?.source)}${
                  change.author ? ` · by ${change.author}` : ''
                }${change.merged_at ? ' · merged ' : ''}${this.time(change.merged_at)}
              </div>
              ${this.criteria(change.purpose?.criteria)}
              ${
                flags.length
                  ? html`<div>Risk</div>
                      <ul>
                        ${flags.map(
                          (f) =>
                            html`<li>
                              ${flagLabel(f.flag)}${
                                f.evidence?.length
                                  ? `: ${f.evidence.join(', ')}`
                                  : ''
                              }
                            </li>`
                        )}
                      </ul>`
                  : nothing
              }
              ${
                change.evidence_gaps?.length
                  ? html`<div>Not verified</div>
                      <ul>
                        ${change.evidence_gaps.map((g) => html`<li>${g}</li>`)}
                      </ul>`
                  : nothing
              }
              ${
                change.metadata_only
                  ? html`<div class="meta">
                      Reported from metadata: ${change.metadata_only}
                    </div>`
                  : nothing
              }
            </div>`
          : nothing
      }
    </li>`;
  }

  private renderShipped(detail: DeliveryReportDetail) {
    const shipped = detail.report.shipped ?? [];
    const byProject = new Map<string, ShippedChange[]>();
    for (const change of shipped) {
      const project = change.project || 'Other';
      byProject.set(project, [...(byProject.get(project) ?? []), change]);
    }
    return html`<section id="shipped" data-testid="shipped">
      <h2>
        Shipped ${this.chip('neutral', String(shipped.length))}
        <span class="meta">Merged ${periodLabel(detail.window)}</span>
      </h2>
      ${
        shipped.length === 0
          ? html`<div class="empty">Nothing merged in this period.</div>`
          : [...byProject.entries()].map(
              ([project, changes]) =>
                html`<h3 id=${anchorId('project', project)}>${project}</h3>
                  <ul class="rows">
                    ${changes.map((c) => this.renderChange(c))}
                  </ul>`
            )
      }
    </section>`;
  }

  private renderClosed(detail: DeliveryReportDetail) {
    const closed = detail.report.closed_issues ?? [];
    const reopenRefs = new Set(
      (detail.report.reopen_requests ?? []).map((r) => r.ref)
    );
    return html`<section id="closed-issues" data-testid="closed-issues">
      <h2>Closed issues ${this.chip('neutral', String(closed.length))}</h2>
      ${
        closed.length === 0
          ? html`<div class="empty">No issues closed in this period.</div>`
          : html`<ul class="rows">
              ${closed.map((issue) => this.renderClosedIssue(issue, reopenRefs))}
            </ul>`
      }
    </section>`;
  }

  private renderClosedIssue(issue: ClosedIssue, reopenRefs: Set<string>) {
    const key = `issue-${issue.ref}`;
    const open = this.expanded.has(key);
    return html`<li data-testid="closed-issue">
      <div class="row-head">
        ${this.expander(key, issue.ref)} ${this.external(issue.url, issue.ref)}
        <span class="title">${issue.title ?? ''}</span>
        <span class="end">
          ${this.verdict(issue.purpose?.verdict)}
          ${
            reopenRefs.has(issue.ref)
              ? this.chip('warning', 'Reopen proposed')
              : nothing
          }
        </span>
      </div>
      ${
        open
          ? html`<div class="detail" id="detail-${key}">
              <div class="meta">
                Verdict
                ${sourceLabel(issue.purpose?.source)}${
                  issue.closed_by?.length
                    ? ` · closed by ${issue.closed_by.join(', ')}`
                    : ''
                }
              </div>
              ${this.criteria(issue.purpose?.criteria)}
            </div>`
          : nothing
      }
    </li>`;
  }

  private renderOpenItems(detail: DeliveryReportDetail) {
    const items = detail.report.open_past_window ?? [];
    return html`<section id="open-past-window">
      <h2>
        Open past the window ${this.chip('neutral', String(items.length))}
      </h2>
      ${
        items.length === 0
          ? html`<div class="empty">Nothing aged past the policy window.</div>`
          : html`<table>
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Title</th>
                  <th class="num">Age</th>
                </tr>
              </thead>
              <tbody>
                ${items.map(
                  (i) =>
                    html`<tr>
                      <td>${this.external(i.url, i.ref)}</td>
                      <td>
                        ${i.title ?? ''}
                        ${i.draft ? this.chip('neutral', 'Draft') : nothing}
                      </td>
                      <td class="num">${i.age_days ?? '—'} days</td>
                    </tr>`
                )}
              </tbody>
            </table>`
      }
    </section>`;
  }

  private renderDelta(detail: DeliveryReportDetail) {
    const delta = detail.report.delta;
    const carried = delta?.open_questions ?? [];
    const asked = detail.report.open_questions ?? [];
    return html`<section id="delta">
        <h2>Since the previous report</h2>
        ${
          !delta?.previous_report
            ? html`<div class="empty">No previous report to compare with.</div>`
            : carried.length === 0
              ? html`<div class="empty">
                  The previous report left no open questions.
                </div>`
              : html`<ul class="rows">
                  ${carried.map(
                    (q) =>
                      html`<li>
                        <div class="row-head">
                          <span class="title">${q.question}</span>
                          ${this.chip(
                            q.status === 'answered' ? 'success' : 'neutral',
                            q.status === 'answered' ? 'Answered' : 'Still open'
                          )}
                        </div>
                      </li>`
                  )}
                </ul>`
        }
      </section>
      <section id="open-questions">
        <h2>Open questions ${this.chip('neutral', String(asked.length))}</h2>
        ${
          asked.length === 0
            ? html`<div class="empty">No open questions.</div>`
            : html`<ul class="rows">
                ${asked.map(
                  (q) =>
                    html`<li>
                      ${q.question}${q.ref ? html` <span class="meta">${q.ref}</span>` : nothing}
                    </li>`
                )}
              </ul>`
        }
      </section>`;
  }

  private renderSpend(detail: DeliveryReportDetail) {
    const spend = detail.report.spend ?? {};
    const skipped = spend.skipped_for_budget ?? [];
    return html`<section id="spend">
      <h2>
        What this run spent
        <span class="meta"
          >${
            typeof spend.limit_usd === 'number'
              ? `Ceiling ${spendLabel(spend.limit_usd).replace(' est.', '')} per run · `
              : ''
          }<a href="/console/flows/executions/${detail.id}"
            >Open execution</a
          ></span
        >
      </h2>
      <table>
        <tbody>
          <tr>
            <td>Cost</td>
            <td
              class="num"
              title=${typeof spend.cost_usd === 'number' ? '' : 'The gateway could not price this run'}
            >
              ${typeof spend.cost_usd === 'number' ? spendLabel(spend.cost_usd) : 'Unknown'}
            </td>
          </tr>
          <tr>
            <td>Tokens</td>
            <td class="num">
              ${
                typeof spend.total_tokens === 'number'
                  ? spend.total_tokens.toLocaleString()
                  : '—'
              }
            </td>
          </tr>
        </tbody>
      </table>
      ${
        skipped.length
          ? html`<div class="empty">
              Skipped for budget: ${skipped.join(', ')}
            </div>`
          : nothing
      }
    </section>`;
  }

  render() {
    if (this.permissionError) {
      return html`<view-header
          headerText="Delivery report"
          width="wide"
        ></view-header>
        <div class="column-layout wide">
          <permission-denied
            required-permission=${
              this.permissionError.requiredPermission || 'view_flows'
            }
            message=${this.permissionError.message}
          ></permission-denied>
        </div>`;
    }
    const detail = this.detail;
    if (!detail) {
      return html`<div class="column-layout wide">
        <a class="back" href="/console/delivery-reports"
          ><sl-icon name="arrow-left"></sl-icon>All delivery reports</a
        >
        <div class="empty" role=${this.error ? 'alert' : 'status'}>
          ${this.error ?? 'Loading…'}
        </div>
      </div>`;
    }
    const projects = detail.projects.join(', ');
    const title = `Delivery report · ${periodLabel(detail.window)}${projects ? ` · ${projects}` : ''}`;
    const failed =
      detail.status === 'FAILED' || detail.report.status === 'error';
    return html`
      <view-header headerText=${title} width="wide">
        <a slot="top" class="back" href="/console/delivery-reports"
          ><sl-icon name="arrow-left"></sl-icon>All delivery reports</a
        >
        <span slot="description"
          >${detail.flow_name ?? 'Delivery Report'} ·
          ${runLabel(detail.window, detail.started_at)} ·
          ${this.time(detail.started_at)}
          ${failed ? this.chip('neutral', detail.status) : nothing}</span
        >
        <div slot="meta" class="header-actions">
          <sl-button size="small" href="/console/flows/${detail.flow_id}"
            >Open flow</sl-button
          >
          <sl-button size="small" href="/console/flows/executions/${detail.id}"
            >Open execution</sl-button
          >
        </div>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${this.renderStrip(detail)}
          ${
            detail.report.summary?.assessments_skipped_for_budget
              ? html`<p class="notice" data-testid="budget-notice">
                  Assessments were skipped for budget; this report is
                  deterministic.
                </p>`
              : nothing
          }
          ${
            failed && detail.report.reason
              ? html`<p class="notice">${detail.report.reason}</p>`
              : nothing
          }
          ${this.renderNeedsYou(detail)} ${this.renderShipped(detail)}
          ${this.renderClosed(detail)} ${this.renderOpenItems(detail)}
          ${this.renderDelta(detail)} ${this.renderSpend(detail)}
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'delivery-report-view': DeliveryReportView;
  }
}
