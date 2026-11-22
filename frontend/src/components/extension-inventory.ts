import { LitElement, css, html, nothing } from 'lit';
import type { PropertyValues, TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import { tableScrollStyles } from '../styles/table-scroll';
import { formatRelativeTime } from '../utils/date';
import { downloadText } from '../utils/records-format';
import {
  extensionAccess,
  fetchExtensionPolicyText,
  getExtensions,
  setExtensionApproval,
  type ExtensionApprovalState,
  type ExtensionFilters,
  type ExtensionKey,
  type ExtensionPage,
  type ExtensionSource,
  type InstalledExtension,
} from '../extension-inventory-api';

const sources: ExtensionSource[] = [
  'official_marketplace',
  'third_party_marketplace',
  'git',
  'local',
  'builtin',
  'unknown',
];
const states: ExtensionApprovalState[] = ['approved', 'unapproved', 'unknown'];
const label = (value: string): string =>
  value.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
const keyOf = (row: ExtensionKey): string =>
  JSON.stringify([row.agent_kind, row.extension_kind, row.extension_id]);

/** Presence and account-wide decisions; never implies loading or execution. */
@customElement('extension-inventory')
export class ExtensionInventory extends LitElement {
  @property() fingerprint = '';
  @property({ attribute: false }) loader = getExtensions;
  @property({ attribute: false }) updater = setExtensionApproval;
  @property({ attribute: false }) accessLoader = extensionAccess;
  @property({ attribute: false }) policyLoader = fetchExtensionPolicyText;
  @property({ attribute: false }) policySaver = downloadText;
  @state() private page: ExtensionPage | null = null;
  @state() private loading = true;
  @state() private canView = false;
  @state() private canManage = false;
  @state() private error = '';
  @state() private busy = '';
  @state() private downloading = false;
  @state() private stateFilter = '';
  @state() private sourceFilter = '';
  private requestId = 0;

  static styles = [
    tableScrollStyles,
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        gap: 16px;
        margin-bottom: 16px;
        align-items: end;
      }
      sl-select {
        min-width: 150px;
      }
      .meta,
      .empty {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .empty {
        min-height: 72px;
        display: grid;
        place-items: center;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        padding: 8px;
        text-align: left;
        border-bottom: 1px solid var(--console-hairline);
        vertical-align: top;
      }
      th {
        font-weight: 600;
      }
      .identity {
        overflow-wrap: anywhere;
        min-width: 150px;
      }
      .number {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .meta {
        display: block;
      }
      code {
        font-size: var(--console-text-meta);
      }
      .footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: 16px;
      }
      @media (max-width: 640px) {
        thead {
          display: none;
        }
        table,
        tbody,
        tr,
        td {
          display: block;
        }
        td {
          border: 0;
          padding: 4px 0;
        }
        tr {
          padding: 12px 0;
          border-bottom: 1px solid var(--console-hairline);
        }
        td[data-label]::before {
          content: attr(data-label) ': ';
          color: var(--console-meta-color);
        }
        .number {
          text-align: left;
        }
      }
    `,
  ];

  protected firstUpdated(): void {
    void this.refresh();
  }
  protected updated(changes: PropertyValues): void {
    if (changes.has('fingerprint') && changes.get('fingerprint') !== undefined)
      void this.refresh();
  }
  disconnectedCallback(): void {
    this.requestId++;
    super.disconnectedCallback();
  }

  async refresh(append = false): Promise<void> {
    const id = ++this.requestId;
    this.loading = true;
    this.error = '';
    if (!append) this.page = null;
    try {
      const access = await this.accessLoader();
      if (id !== this.requestId) return;
      this.canView = access.view;
      this.canManage = access.manage;
      if (!access.view) return;
      const filters: ExtensionFilters = {
        fingerprint: this.fingerprint || undefined,
        state: (this.stateFilter || undefined) as
          ExtensionApprovalState | undefined,
        source_kind: (this.sourceFilter || undefined) as
          ExtensionSource | undefined,
        cursor: append ? (this.page?.next_cursor ?? undefined) : undefined,
      };
      const page = await this.loader(filters);
      if (id !== this.requestId) return;
      this.page =
        append && this.page
          ? { ...page, items: [...this.page.items, ...page.items] }
          : page;
    } catch (error) {
      if (id === this.requestId)
        this.error =
          error instanceof Error ? error.message : 'Could not load extensions';
    } finally {
      if (id === this.requestId) this.loading = false;
    }
  }

  private async approve(row: ExtensionKey, event: Event): Promise<void> {
    const select = event.currentTarget as HTMLElement & {
      value: ExtensionApprovalState;
    };
    const next = select.value;
    // Keep the saved decision visible until the server accepts the change.
    select.value = row.approval_state;
    if (!this.canManage || this.busy || next === row.approval_state) return;
    this.busy = keyOf(row);
    this.error = '';
    try {
      await this.updater(row, next);
      // Decisions are account-wide; refresh every visible occurrence and filter.
      await this.refresh();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not save approval';
    } finally {
      this.busy = '';
    }
  }

  private async downloadPolicy(): Promise<void> {
    if (this.downloading || !this.canView) return;
    this.downloading = true;
    this.error = '';
    try {
      const text = await this.policyLoader();
      this.policySaver('preloop-extension-policy.json', text);
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not download policy file';
    } finally {
      this.downloading = false;
    }
  }

  private approval(row: ExtensionKey): TemplateResult {
    if (!this.canManage) return html`<span>${label(row.approval_state)}</span>`;
    return html`<sl-select
      size="small"
      aria-label=${`Approval for ${row.extension_id}`}
      .value=${row.approval_state}
      ?disabled=${!!this.busy}
      @sl-change=${(event: Event) => this.approve(row, event)}
    >
      ${states.map((state) => html`<sl-option value=${state}>${label(state)}</sl-option>`)}
    </sl-select>`;
  }

  private hash(value: string | null): TemplateResult | typeof nothing {
    return value
      ? html`<code class="meta" title=${value}>${value.slice(0, 19)}…</code>`
      : nothing;
  }

  private deviceRow(row: InstalledExtension): TemplateResult {
    return html`<tr>
      <td data-label="Agent">
        ${row.agent_kind}<span class="meta" title=${row.profile_ref}
          >Profile
          ${row.profile_ref === 'current' ? 'current' : row.profile_ref.slice(0, 8)}</span
        >
      </td>
      <td data-label="Kind">${label(row.extension_kind)}</td>
      <td class="identity" data-label="Extension">
        ${row.extension_id}${row.id_hashed ? html`<span class="meta">Hashed identifier</span>` : nothing}
      </td>
      <td data-label="Version">${row.version ?? 'Unknown'}</td>
      <td data-label="Source">
        ${label(row.source_kind)}${this.hash(row.source_ref_hash)}
      </td>
      <td data-label="Scope">
        ${label(row.scope)}${this.hash(row.project_ref_hash)}
      </td>
      <td data-label="Enabled">
        ${row.enabled === 'true' ? 'Yes' : row.enabled === 'false' ? 'No' : 'Unknown'}
      </td>
      <td data-label="First seen" title=${row.first_seen_at}>
        ${formatRelativeTime(row.first_seen_at)}
      </td>
      <td data-label="Approval">${this.approval(row)}</td>
    </tr>`;
  }

  render(): TemplateResult | typeof nothing {
    if (!this.loading && !this.canView && !this.error) return nothing;
    return html`
      <p class="meta">
        Installed files do not prove an agent loaded or ran an extension.
        Approval decisions apply to the whole account.
      </p>
      <div class="filters">
        <sl-select
          size="small"
          label="Approval state"
          .value=${this.stateFilter}
          @sl-change=${(event: Event) => {
            this.stateFilter = (event.target as HTMLSelectElement).value;
            void this.refresh();
          }}
        >
          <sl-option value="">All states</sl-option
          >${states.map((state) => html`<sl-option value=${state}>${label(state)}</sl-option>`)}
        </sl-select>
        <sl-select
          size="small"
          label="Source"
          .value=${this.sourceFilter}
          @sl-change=${(event: Event) => {
            this.sourceFilter = (event.target as HTMLSelectElement).value;
            void this.refresh();
          }}
        >
          <sl-option value="">All sources</sl-option
          >${sources.map((source) => html`<sl-option value=${source}>${label(source)}</sl-option>`)}
        </sl-select>
        <sl-button
          size="small"
          data-testid="download-policy"
          ?loading=${this.downloading}
          ?disabled=${this.downloading}
          @click=${() => this.downloadPolicy()}
        >
          Download policy file
        </sl-button>
      </div>
      ${this.error ? html`<p role="alert">${this.error} <sl-button size="small" @click=${() => this.refresh()}>Retry</sl-button></p>` : nothing}
      ${this.page?.completeness !== 'complete' && this.page ? html`<p class="meta" role="note">${this.page.completeness === 'partial' ? 'Collection is incomplete. Previously observed extensions are retained; other installed extensions may be missing.' : 'Collection completeness is unknown. Run discovery with a current collector.'}</p>` : nothing}
      ${this.loading && !this.page ? html`<p role="status">Loading extensions…</p>` : nothing}
      ${
        this.page?.items.length
          ? html`<div class="table-scroll">
              <table aria-label="Installed extensions">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Kind</th>
                    <th>Extension</th>
                    ${
                      this.fingerprint
                        ? html`<th>Version</th>
                            <th>Source</th>
                            <th>Scope</th>
                            <th>Enabled</th>
                            <th>First seen</th>`
                        : html`<th class="number">Devices</th>
                            <th>Versions seen</th>
                            <th>Sources</th>`
                    }
                    <th>Approval</th>
                  </tr>
                </thead>
                <tbody>
                  ${this.page.items.map((row) =>
                    'fingerprint' in row
                      ? this.deviceRow(row)
                      : html`<tr>
                          <td data-label="Agent">${row.agent_kind}</td>
                          <td data-label="Kind">
                            ${label(row.extension_kind)}
                          </td>
                          <td class="identity" data-label="Extension">
                            ${row.extension_id}
                          </td>
                          <td class="number" data-label="Devices">
                            ${row.device_count}
                          </td>
                          <td data-label="Versions seen">
                            ${row.versions.join(', ') || 'Unknown'}
                          </td>
                          <td data-label="Sources">
                            ${row.source_kinds.map(label).join(', ')}
                          </td>
                          <td data-label="Approval">${this.approval(row)}</td>
                        </tr>`
                  )}
                </tbody>
              </table>
            </div>`
          : this.page && !this.loading
            ? html`<p class="empty">
                No extensions reported for this selection.
              </p>`
            : nothing
      }
      ${this.page ? html`<div class="footer"><span class="meta" aria-live="polite">${this.page.items.length} extensions shown</span>${this.page.next_cursor ? html`<sl-button size="small" ?loading=${this.loading} @click=${() => this.refresh(true)}>Load more</sl-button>` : nothing}</div>` : nothing}
    `;
  }
}
