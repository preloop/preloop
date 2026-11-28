import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../../components/view-header.ts';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { tableScrollStyles } from '../../../styles/table-scroll';
import { relativeTime } from '../../../utils/relative-time';
import { downloadBlob } from '../../../utils/records-format';
import {
  fleetAccess,
  fleetHref,
  getDeviceFleet,
  getFleetFindings,
} from '../../../device-fleet-api';
import {
  UNASSIGNED,
  agentLabel,
  buildRegister,
  filterRows,
  getExtensionDecisions,
  getExtensionStateCounts,
  reasonLabel,
  registerCsv,
  type ComplianceFilters,
  type ComplianceRegister,
  type ExtensionDecisionEvent,
  type ExtensionStateCounts,
} from '../../../compliance-api';

/** Shown above the register; the rows are self-reported, never attested. */
export const REPORTED_NOT_ATTESTED =
  'Rows describe what software on each machine reported. They are not attested.';
export const TEAM_EDITION_NOTE =
  'Team compliance across people and devices is part of Preloop Cloud Team and Preloop Enterprise. This page shows the extensions reported to this account.';
export const EMPTY_REGISTER =
  'Nothing is out of policy on the devices that reported.';

function decisionText(event: ExtensionDecisionEvent): string {
  if (event.action === 'pin_added') return 'Pinned content';
  if (event.action === 'pin_removed') return 'Removed a content pin';
  const to = reasonLabel(event.new_state ?? 'unknown');
  return event.previous_state
    ? `${reasonLabel(event.previous_state)} → ${to}`
    : `Set to ${to}`;
}

@customElement('compliance-view')
export class ComplianceView extends LitElement {
  @state() private loading = true;
  @state() private team = false;
  @state() private error: string | null = null;
  @state() private counts: ExtensionStateCounts | null = null;
  @state() private decisions: ExtensionDecisionEvent[] = [];
  @state() private register: ComplianceRegister | null = null;
  @state() private filters: ComplianceFilters = {};
  @state() private asOf: Date = new Date();

  static styles = [
    tableScrollStyles,
    [
      unsafeCSS(consoleStyles),
      css`
        .section {
          margin-bottom: var(--sl-spacing-large);
        }
        .tiles {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
          gap: var(--sl-spacing-medium);
        }
        .tile h3 {
          margin: 0 0 var(--sl-spacing-x-small);
          font-size: var(--console-text-meta);
          color: var(--console-meta-color);
          font-weight: 500;
        }
        .tile .stat-value {
          font-size: var(--console-stat-value);
          font-weight: 600;
        }
        .tile .stat-subtext {
          display: block;
        }
        .bar {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: var(--sl-spacing-small);
          margin-bottom: var(--sl-spacing-medium);
        }
        .bar sl-select {
          min-width: 11rem;
        }
        .bar .count {
          margin-left: auto;
          color: var(--console-meta-color);
          font-size: var(--console-text-meta);
        }
        .honesty,
        .meta {
          color: var(--console-meta-color);
          font-size: var(--console-text-meta);
        }
        .chips {
          display: flex;
          flex-wrap: wrap;
          gap: var(--sl-spacing-2x-small);
        }
        .nowrap {
          white-space: nowrap;
        }
        td .sub {
          display: block;
          color: var(--console-meta-color);
          font-size: var(--console-text-meta);
        }
        h2 {
          font-size: var(--console-text-card-title);
          margin: 0 0 var(--sl-spacing-small);
        }
      `,
    ],
  ];

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const access = await fleetAccess().catch(() => ({ view: false }));
      this.team = access.view;
      const [counts, decisions, fleet, findings] = await Promise.all([
        getExtensionStateCounts(),
        getExtensionDecisions(20),
        this.team ? getDeviceFleet() : Promise.resolve(null),
        this.team ? getFleetFindings() : Promise.resolve(null),
      ]);
      this.counts = counts;
      this.decisions = decisions;
      this.register =
        fleet && findings ? buildRegister(fleet, findings.items) : null;
      this.asOf = new Date();
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Compliance data could not be loaded. Retry to refresh it.';
    } finally {
      this.loading = false;
    }
  }

  private setFilter(key: keyof ComplianceFilters, event: Event): void {
    // Option values are URI-encoded: sl-select splits a value on spaces.
    const value = decodeURIComponent(
      String((event.target as HTMLSelectElement).value ?? '')
    );
    this.filters = { ...this.filters, [key]: value || undefined };
  }

  private exportCsv(): void {
    const rows = this.register?.rows ?? [];
    const asOf = this.asOf;
    downloadBlob(
      new Blob([registerCsv(rows, asOf)], { type: 'text/csv' }),
      `compliance-register-${asOf.toISOString().slice(0, 19).replace(/:/g, '')}Z.csv`
    );
  }

  private renderTile(
    title: string,
    value: number,
    detail: unknown,
    testId: string
  ) {
    return html`<div class="content-card tile" data-testid=${testId}>
      <h3>${title}</h3>
      <span class="stat-value">${value}</span>
      <span class="stat-subtext">${detail}</span>
    </div>`;
  }

  private renderSummary() {
    const counts = this.counts;
    const summary = this.register?.summary;
    const reasons = Object.entries(summary?.findingsByReason ?? {}).sort(
      (a, b) => b[1] - a[1]
    );
    return html`<section class="section tiles" aria-label="Summary">
      ${
        summary
          ? html`
              ${this.renderTile(
                'Devices reporting',
                summary.devicesReporting,
                `${summary.devicesStale} stale · ${summary.devicesNeverReported} never reported`,
                'tile-devices'
              )}
              ${this.renderTile(
                'Agents onboarded',
                summary.agentsOnboarded,
                `${summary.agentsNotOnboarded} discovered, not onboarded`,
                'tile-agents'
              )}
            `
          : nothing
      }
      ${
        counts
          ? this.renderTile(
              'Extensions approved',
              counts.approved,
              `${counts.unapproved} unapproved · ${counts.unknown} not decided`,
              'tile-extensions'
            )
          : nothing
      }
      ${
        summary
          ? this.renderTile(
              'Open findings',
              summary.openFindings,
              reasons.length
                ? reasons
                    .map(
                      ([reason, n]) =>
                        `${n} ${reasonLabel(reason).toLowerCase()}`
                    )
                    .join(' · ')
                : 'None open',
              'tile-findings'
            )
          : nothing
      }
    </section>`;
  }

  private renderRegister() {
    const register = this.register;
    if (!register) return nothing;
    const all = register.rows;
    const rows = filterRows(all, this.filters);
    const people = [...new Set(all.map((r) => r.person))].sort((a, b) =>
      a === UNASSIGNED ? 1 : b === UNASSIGNED ? -1 : a.localeCompare(b)
    );
    const devices = [...new Map(all.map((r) => [r.deviceKey, r.device]))];
    const agents = [...new Set(all.flatMap((r) => r.agentKinds))].sort();
    const reasons = [...new Set(all.flatMap((r) => r.reasons))].sort();
    const select = (
      key: keyof ComplianceFilters,
      label: string,
      options: [string, string][]
    ) =>
      html`<sl-select
        size="small"
        clearable
        placeholder=${label}
        aria-label=${label}
        data-filter=${key}
        value=${encodeURIComponent(this.filters[key] ?? '')}
        @sl-change=${(e: Event) => this.setFilter(key, e)}
      >
        ${options.map(
          ([value, text]) =>
            html`<sl-option value=${value.replace(/ /g, ' ')}
              >${text}</sl-option
            >`
        )}
      </sl-select>`;
    return html`<section class="section content-card" aria-labelledby="oop">
      <div class="card-header-with-action">
        <h2 id="oop">Out of policy now</h2>
        <sl-button
          size="small"
          data-testid="export-csv"
          @click=${this.exportCsv}
          >Export CSV</sl-button
        >
      </div>
      <p class="honesty" data-testid="honesty">${REPORTED_NOT_ATTESTED}</p>
      <div class="bar">
        ${select(
          'person',
          'All people',
          people.map((p) => [p, p])
        )}
        ${select('device', 'All devices', devices)}
        ${select(
          'agent',
          'All agents',
          agents.map((a) => [a, agentLabel(a)])
        )}
        ${select(
          'reason',
          'All reasons',
          reasons.map((r) => [r, reasonLabel(r)])
        )}
        <span class="count" aria-live="polite" data-testid="row-count"
          >${
            rows.length === all.length
              ? `${all.length} ${all.length === 1 ? 'row' : 'rows'}`
              : `${rows.length} of ${all.length} rows`
          }</span
        >
      </div>
      ${
        rows.length === 0
          ? html`<div class="empty-state" data-testid="empty">
              ${all.length === 0 ? EMPTY_REGISTER : 'No rows match these filters.'}
            </div>`
          : html`<div class="table-scroll">
              <table class="styled-table" data-testid="register">
                <thead>
                  <tr>
                    <th scope="col">Person</th>
                    <th scope="col">Device</th>
                    <th scope="col">Agent</th>
                    <th scope="col">Extensions</th>
                    <th scope="col">Reason</th>
                    <th scope="col" aria-sort="ascending">Since</th>
                    <th scope="col">Last report</th>
                    <th scope="col">
                      <span class="visually-hidden">Decide</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map(
                    (row) =>
                      html`<tr>
                        <td>${row.person}</td>
                        <td>
                          ${
                            row.deviceKey
                              ? html`<a href=${fleetHref(row.deviceKey)}
                                  >${row.device}</a
                                >`
                              : row.device
                          }
                        </td>
                        <td>
                          ${row.agentKinds.map(agentLabel).join(', ') || '—'}
                        </td>
                        <td>
                          ${
                            row.extensions.length
                              ? row.extensions.join(', ')
                              : html`<span class="meta">—</span>`
                          }
                        </td>
                        <td>
                          <span class="chips"
                            >${row.reasons.map(
                              (reason) =>
                                html`<sl-badge
                                  class="chip"
                                  pill
                                  variant="warning"
                                  >${reasonLabel(reason)}</sl-badge
                                >`
                            )}</span
                          >
                        </td>
                        <td class="nowrap">${relativeTime(row.since)}</td>
                        <td class="nowrap">
                          ${relativeTime(row.lastReport, 'Never')}
                        </td>
                        <td><a href=${row.decideHref}>Decide</a></td>
                      </tr>`
                  )}
                </tbody>
              </table>
            </div>`
      }
    </section>`;
  }

  private renderDecisions() {
    return html`<section class="section content-card" aria-labelledby="recent">
      <h2 id="recent">Recent decisions</h2>
      ${
        this.decisions.length === 0
          ? html`<div class="empty-state" data-testid="no-decisions">
              No decisions recorded yet.
            </div>`
          : html`<div class="table-scroll">
              <table class="styled-table" data-testid="decisions">
                <thead>
                  <tr>
                    <th scope="col">Who</th>
                    <th scope="col">Extension</th>
                    <th scope="col">Decision</th>
                    <th scope="col">When</th>
                  </tr>
                </thead>
                <tbody>
                  ${this.decisions.map(
                    (event) =>
                      html`<tr>
                        <td>${event.decided_by ?? 'Removed user'}</td>
                        <td>
                          ${event.extension_id}
                          <span class="sub"
                            >${event.extension_kind} ·
                            ${agentLabel(event.agent_kind)}</span
                          >
                        </td>
                        <td>
                          ${decisionText(event)}
                          ${
                            event.note
                              ? html`<span class="sub">${event.note}</span>`
                              : nothing
                          }
                        </td>
                        <td>${relativeTime(event.created_at)}</td>
                      </tr>`
                  )}
                </tbody>
              </table>
            </div>`
      }
    </section>`;
  }

  render() {
    return html`
      <view-header
        headerText="Compliance"
        description="What is out of policy across people, devices and agents, and who decided what."
        width="wide"
      ></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${
            this.loading
              ? html`<sl-spinner></sl-spinner>`
              : this.error
                ? html`<sl-alert variant="danger" open data-testid="error"
                    >${this.error}
                    <sl-button size="small" @click=${() => this.load()}
                      >Retry</sl-button
                    ></sl-alert
                  >`
                : html`
                    ${
                      this.team
                        ? nothing
                        : html`<p class="meta" data-testid="edition-note">
                            ${TEAM_EDITION_NOTE}
                          </p>`
                    }
                    ${this.renderSummary()} ${this.renderRegister()}
                    ${this.renderDecisions()}
                  `
          }
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'compliance-view': ComplianceView;
  }
}
