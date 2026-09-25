import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import {
  downloadEvidenceMember,
  listEvidenceMembers,
  readEvidenceMember,
  type EvidenceMember,
  type EvidenceStatus,
} from '../records-api';
import consoleStyles from '../styles/console-styles.css?inline';
import {
  findingsSummaryLabel,
  isCraFindings,
  markdownOutline,
  parseFindings,
  parseRegister,
  renderReportMarkdown,
  severityRank,
  severityVariant,
  type FindingRow,
  type ReportHeading,
} from '../utils/evidence-report';
import { markdownBodyCss } from '../utils/markdown';
import {
  downloadBlob,
  formatBytes,
  truncateMiddle,
} from '../utils/records-format';

const STATUS_LABEL: Record<string, string> = {
  available: 'Present',
  missing: 'Missing',
  expired: 'Expired',
  failed: 'Failed',
};

type SortKey = 'severity' | 'lens' | 'id' | 'title';

@customElement('execution-report-panel')
export class ExecutionReportPanel extends LitElement {
  @property({ attribute: 'execution-id' })
  executionId = '';

  @property({ attribute: false })
  result: Record<string, unknown> | null = null;

  @property({ attribute: false })
  evidence: EvidenceStatus | null = null;

  @state() private reportHtml = '';
  @state() private headings: ReportHeading[] = [];
  @state() private findings: FindingRow[] = [];
  @state() private registerHtml = '';
  @state() private members: EvidenceMember[] = [];
  @state() private error: string | null = null;
  @state() private loading = false;
  @state() private severityFilter = '';
  @state() private lensFilter = '';
  @state() private sortKey: SortKey = 'severity';
  @state() private sortAsc = true;

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(markdownBodyCss),
    css`
      .layout {
        display: grid;
        grid-template-columns: 12rem 1fr;
        gap: var(--sl-spacing-medium);
      }
      .outline {
        position: sticky;
        top: 0;
        align-self: start;
      }
      .outline a {
        display: block;
        color: var(--console-link-color);
        font-size: var(--console-text-meta);
        margin: 0 0 var(--sl-spacing-2x-small);
        text-decoration: none;
      }
      .outline a.l2 {
        padding-left: var(--sl-spacing-small);
      }
      .outline a.l3 {
        padding-left: var(--sl-spacing-medium);
      }
      h2 {
        font-size: var(--console-text-body);
        margin: var(--sl-spacing-medium) 0 var(--sl-spacing-x-small);
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-small);
      }
      select {
        font: inherit;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--console-text-meta);
      }
      th,
      td {
        border-bottom: 1px solid var(--console-hairline);
        padding: 6px 8px;
        text-align: left;
        vertical-align: top;
      }
      th button {
        background: none;
        border: 0;
        color: inherit;
        cursor: pointer;
        font: inherit;
        padding: 0;
      }
      .mono {
        font-family: var(--sl-font-mono);
      }
      .note,
      .error {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .error {
        color: var(--sl-color-danger-600);
      }
      .pack-line {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: center;
      }
      @media (max-width: 720px) {
        .layout {
          grid-template-columns: 1fr;
        }
      }
    `,
  ];

  updated(changed: Map<string, unknown>): void {
    if (
      (changed.has('executionId') || changed.has('evidence')) &&
      this.evidence?.status === 'available' &&
      this.executionId
    ) {
      void this.load();
    }
  }

  private artifactPath(key: string): string | null {
    const artifacts = this.result?.artifacts;
    if (!artifacts || typeof artifacts !== 'object') return null;
    const value = (artifacts as Record<string, unknown>)[key];
    return typeof value === 'string' && value ? value : null;
  }

  private async load(): Promise<void> {
    const executionId = this.executionId;
    try {
      const listed = await listEvidenceMembers(executionId);
      if (this.executionId !== executionId) return;
      this.loading = true;
      this.error = null;
      this.members = listed.members || [];
      const reportPath = this.artifactPath('report');
      const findingsPath = this.artifactPath('findings');
      const [report, findings] = await Promise.all([
        reportPath
          ? readEvidenceMember(executionId, reportPath)
          : Promise.resolve(''),
        findingsPath
          ? readEvidenceMember(executionId, findingsPath)
          : Promise.resolve(''),
      ]);
      if (this.executionId !== executionId) return;
      this.headings = markdownOutline(report);
      this.reportHtml = report ? renderReportMarkdown(report) : '';
      this.findings = findings ? parseFindings(JSON.parse(findings)) : [];
      this.registerHtml = '';
      if (!parseRegister(this.result).length) {
        const registerPath = this.artifactPath('register');
        if (registerPath) {
          const markdown = await readEvidenceMember(executionId, registerPath);
          if (this.executionId !== executionId) return;
          this.registerHtml = markdown ? renderReportMarkdown(markdown) : '';
        }
      }
    } catch (err) {
      if (this.executionId !== executionId) return;
      this.error =
        err instanceof Error ? err.message : 'Could not read the evidence pack';
    } finally {
      if (this.executionId === executionId) this.loading = false;
    }
  }

  private jump(id: string): void {
    this.shadowRoot?.getElementById(id)?.scrollIntoView({ block: 'start' });
  }

  private async download(path: string): Promise<void> {
    try {
      const file = await downloadEvidenceMember(this.executionId, path);
      downloadBlob(file.blob, file.filename);
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Could not download the member';
    }
  }

  private toggleSort(key: SortKey): void {
    if (this.sortKey === key) {
      this.sortAsc = !this.sortAsc;
    } else {
      this.sortKey = key;
      this.sortAsc = true;
    }
  }

  private visibleFindings(): FindingRow[] {
    const rows = this.findings.filter((row) => {
      if (
        this.severityFilter &&
        row.severity.toLowerCase() !== this.severityFilter
      )
        return false;
      if (this.lensFilter && row.lens !== this.lensFilter) return false;
      return true;
    });
    const dir = this.sortAsc ? 1 : -1;
    return rows.sort((left, right) => {
      if (this.sortKey === 'severity') {
        return (
          (severityRank(left.severity) - severityRank(right.severity)) * dir
        );
      }
      const a = left[this.sortKey] || '';
      const b = right[this.sortKey] || '';
      return a.localeCompare(b) * dir;
    });
  }

  private renderUnavailable() {
    const evidence = this.evidence;
    const label = evidence
      ? STATUS_LABEL[evidence.status] || evidence.status
      : 'Unknown';
    return html`
      <p data-testid="report-unavailable">
        Evidence pack: ${label}.
        ${evidence?.integrity_note || evidence?.error || ''}
      </p>
    `;
  }

  private renderFindings() {
    const cra = isCraFindings(this.findings);
    const lenses = [
      ...new Set(this.findings.map((row) => row.lens).filter(Boolean)),
    ];
    const severities = [
      ...new Set(
        this.findings.map((row) => row.severity.toLowerCase()).filter(Boolean)
      ),
    ];
    const summary = findingsSummaryLabel(this.result?.findings_summary);
    const rows = this.visibleFindings();
    const sortButton = (key: SortKey, label: string) => html`
      <button type="button" @click=${() => this.toggleSort(key)}>
        ${label}
      </button>
    `;
    return html`
      <h2>Findings</h2>
      ${summary ? html`<p data-testid="findings-summary">${summary}</p>` : nothing}
      <div class="filters">
        <label>
          Severity
          <select
            data-testid="severity-filter"
            @change=${(event: Event) => {
              this.severityFilter = (event.target as HTMLSelectElement).value;
            }}
          >
            <option value="">All</option>
            ${severities.map(
              (value) => html`<option value=${value}>${value}</option>`
            )}
          </select>
        </label>
        <label>
          Lens
          <select
            data-testid="lens-filter"
            @change=${(event: Event) => {
              this.lensFilter = (event.target as HTMLSelectElement).value;
            }}
          >
            <option value="">All</option>
            ${lenses.map((value) => html`<option value=${value}>${value}</option>`)}
          </select>
        </label>
      </div>
      <table data-testid="findings-table">
        <thead>
          <tr>
            <th>${sortButton('id', 'Id')}</th>
            <th>${sortButton('lens', 'Lens')}</th>
            <th>${sortButton('severity', 'Severity')}</th>
            <th>${sortButton('title', 'Summary')}</th>
            ${
              cra
                ? html`<th>Package</th>
                    <th>CVSS</th>
                    <th>KEV</th>
                    <th>Fix</th>
                    <th>VEX</th>`
                : html`<th>Evidence</th>
                    <th>Status</th>`
            }
          </tr>
        </thead>
        <tbody>
          ${rows.map(
            (row) => html`
              <tr>
                <td class="mono">${row.id}</td>
                <td>${row.lens}</td>
                <td>
                  <sl-badge variant=${severityVariant(row.severity)} pill
                    >${row.severity}</sl-badge
                  >
                </td>
                <td>${row.title}</td>
                ${
                  cra
                    ? html`<td>${row.pkg}</td>
                        <td>${row.cvss}</td>
                        <td>${row.kev}</td>
                        <td>${row.fix}</td>
                        <td>${row.vex}</td>`
                    : html`<td class="mono">${row.evidence}</td>
                        <td>${row.status}</td>`
                }
              </tr>
            `
          )}
        </tbody>
      </table>
    `;
  }

  private renderRegister() {
    const rows = parseRegister(this.result);
    if (!rows.length) {
      if (!this.registerHtml) return nothing;
      return html`
        <h2>Register</h2>
        <div class="markdown-body" data-testid="register-markdown">
          ${unsafeHTML(this.registerHtml)}
        </div>
      `;
    }
    return html`
      <h2>Register</h2>
      <table data-testid="register-table">
        <thead>
          <tr>
            <th>Module</th>
            <th>Lens</th>
            <th>Status</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(
            (row) => html`
              <tr>
                <td>${row.module}</td>
                <td>${row.lens}</td>
                <td>${row.status}</td>
                <td>${row.note}</td>
              </tr>
            `
          )}
        </tbody>
      </table>
    `;
  }

  private renderMembers() {
    const evidence = this.evidence;
    return html`
      <h2>Pack members</h2>
      <div class="pack-line" data-testid="pack-integrity">
        ${
          evidence?.legal_hold
            ? html`<sl-badge variant="warning" pill>Held</sl-badge>`
            : nothing
        }
        <span>Integrity: ${evidence?.integrity || 'Unknown'}</span>
        <span class="mono"
          >SHA-256
          ${
            evidence?.sha256 ? truncateMiddle(evidence.sha256, 24) : 'None'
          }</span
        >
      </div>
      ${
        evidence?.integrity_note
          ? html`<p class="note">${evidence.integrity_note}</p>`
          : nothing
      }
      <table data-testid="members-table">
        <thead>
          <tr>
            <th>Path</th>
            <th>Size</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${this.members.map(
            (member) => html`
              <tr>
                <td class="mono">${member.path}</td>
                <td>${formatBytes(member.size_bytes)}</td>
                <td>
                  <button
                    type="button"
                    data-testid="member-download"
                    @click=${() => this.download(member.path)}
                  >
                    Download
                  </button>
                </td>
              </tr>
            `
          )}
        </tbody>
      </table>
    `;
  }

  render() {
    if (!this.evidence || this.evidence.status !== 'available') {
      return this.renderUnavailable();
    }
    return html`
      <div data-testid="execution-report">
        ${this.loading ? html`<p class="note">Reading the pack...</p>` : nothing}
        ${
          this.error
            ? html`<p class="error" data-testid="report-error">
                ${this.error}
              </p>`
            : nothing
        }
        <div class="layout">
          <nav class="outline" data-testid="report-outline">
            ${this.headings.map(
              (heading) => html`
                <a
                  class=${heading.level > 1 ? `l${heading.level}` : ''}
                  href=${`#${heading.id}`}
                  @click=${(event: Event) => {
                    event.preventDefault();
                    this.jump(heading.id);
                  }}
                  >${heading.text}</a
                >
              `
            )}
          </nav>
          <div>
            ${
              this.reportHtml
                ? html`<div class="markdown-body" data-testid="report-body">
                    ${unsafeHTML(this.reportHtml)}
                  </div>`
                : nothing
            }
            ${this.renderFindings()} ${this.renderRegister()}
            ${this.renderMembers()}
          </div>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'execution-report-panel': ExecutionReportPanel;
  }
}
