import { LitElement, css, html, nothing } from 'lit';
import type { PropertyValues, TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import './extension-enforcement';
import { tableScrollStyles } from '../styles/table-scroll';
import { formatRelativeTime } from '../utils/date';
import { downloadText } from '../utils/records-format';
import {
  addExtensionPin,
  deleteExtensionPin,
  extensionAccess,
  fetchExtensionPolicyText,
  getExtensionPins,
  getExtensions,
  setExtensionApproval,
  type ExtensionAggregate,
  type ExtensionApprovalState,
  type ExtensionEffectiveState,
  type ExtensionFilters,
  type ExtensionPin,
  type ExtensionKey,
  type ExtensionPage,
  type ExtensionSource,
  type InstalledExtension,
} from '../extension-inventory-api';

// Exclusive lower bound: this release has no extension collector.
export const EXTENSION_COLLECTOR_VERSION_FLOOR = '1.0.2';

const sources: ExtensionSource[] = [
  'official_marketplace',
  'third_party_marketplace',
  'git',
  'local',
  'builtin',
  'unknown',
];
const states: ExtensionApprovalState[] = ['approved', 'unapproved', 'unknown'];
const filterStates: ExtensionEffectiveState[] = [
  ...states,
  'content_changed',
  'content_unverified',
];
const shortHash = (value: string): string => `${value.slice(0, 19)}…`;
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
  @property({ attribute: false }) pinLoader = getExtensionPins;
  @property({ attribute: false }) pinAdder = addExtensionPin;
  @property({ attribute: false }) pinRemover = deleteExtensionPin;
  @property({ attribute: false }) confirmUnpin = (message: string): boolean =>
    window.confirm(message);
  @state() private expanded = '';
  private expandedRow: ExtensionKey | null = null;
  @state() private pins: ExtensionPin[] = [];
  @state() private page: ExtensionPage | null = null;
  @state() private loading = true;
  @state() private canView = false;
  @state() private canManage = false;
  @state() private approvalRevision = 0;
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
      sl-badge {
        margin-left: 4px;
      }
      .hashes ul {
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: 8px;
      }
      .hashes li {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
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
          ExtensionEffectiveState | undefined,
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
      // An open hash list re-reads its pins so pin and unpin show at once.
      if (this.expandedRow && !append) {
        const pins = await this.pinLoader(this.expandedRow);
        if (id === this.requestId) this.pins = pins;
      }
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
      this.approvalRevision += 1;
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

  /** Run one account-wide write, then refresh every visible occurrence. */
  private async act(
    row: ExtensionKey,
    work: () => Promise<unknown>
  ): Promise<void> {
    if (!this.canManage || this.busy) return;
    this.busy = keyOf(row);
    this.error = '';
    try {
      await work();
      await this.refresh();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not save approval';
    } finally {
      this.busy = '';
    }
  }

  private async toggle(row: ExtensionAggregate): Promise<void> {
    const key = keyOf(row);
    if (this.expanded === key) {
      this.expanded = '';
      this.expandedRow = null;
      return;
    }
    this.expanded = key;
    this.expandedRow = row;
    this.pins = [];
    try {
      const pins = await this.pinLoader(row);
      if (this.expanded === key) this.pins = pins;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not load pins';
    }
  }

  private unpin(row: ExtensionAggregate, pin: ExtensionPin): void {
    const last = this.pins.length === 1;
    const message = last
      ? 'Unpin the last content hash? The approval becomes a name approval and any content is approved again.'
      : 'Unpin this content hash? Devices reporting it will show Content changed.';
    if (!this.confirmUnpin(message)) return;
    void this.act(row, () => this.pinRemover(row, pin.id));
  }

  /** Badges for states a name-only select cannot express. */
  private stateBadges(
    state: ExtensionEffectiveState | undefined,
    pinCount = 0
  ): TemplateResult {
    return html`${
      pinCount
        ? html`<sl-badge variant="neutral" pill class="pinned"
            >Pinned (${pinCount})</sl-badge
          >`
        : nothing
    }${
      state === 'content_changed'
        ? html`<sl-badge variant="warning" pill class="content-changed"
            >Content changed</sl-badge
          >`
        : state === 'content_unverified'
          ? html`<sl-badge variant="neutral" pill class="content-unverified"
              >Content unverified</sl-badge
            >`
          : nothing
    }`;
  }

  private hashList(row: ExtensionAggregate): TemplateResult {
    const pinned = new Map(this.pins.map((pin) => [pin.content_hash, pin]));
    const canPin = this.canManage && row.approval_state === 'approved';
    return html`<tr class="hashes">
      <td colspan="7">
        <ul aria-label=${`Content hashes for ${row.extension_id}`}>
          ${(row.content_hashes ?? []).map((bucket) => {
            const pin = bucket.content_hash
              ? pinned.get(bucket.content_hash)
              : undefined;
            return html`<li>
              ${
                bucket.content_hash
                  ? html`<code title=${bucket.content_hash}
                      >${shortHash(bucket.content_hash)}</code
                    >`
                  : html`<span>No hash (${label(bucket.status)})</span>`
              }
              <span class="meta">${bucket.device_count} device profiles</span>
              ${
                pin
                  ? html`<span class="meta"
                      >Pinned by ${pin.pinned_by ?? 'a removed user'}
                      ${formatRelativeTime(pin.pinned_at)}</span
                    >`
                  : nothing
              }
              ${
                this.canManage && pin
                  ? html`<sl-button
                      size="small"
                      class="unpin"
                      ?disabled=${!!this.busy}
                      @click=${() => this.unpin(row, pin)}
                      >Unpin</sl-button
                    >`
                  : canPin && bucket.content_hash && !bucket.pinned
                    ? html`<sl-button
                        size="small"
                        class="pin"
                        ?disabled=${!!this.busy}
                        @click=${() => this.act(row, () => this.pinAdder(row, bucket.content_hash as string))}
                        >Pin this content</sl-button
                      >`
                    : nothing
              }
            </li>`;
          })}
        </ul>
        <p class="meta">
          A hash shows the files changed, not what changed or whether an agent
          loaded them.
        </p>
      </td>
    </tr>`;
  }

  private approval(row: ExtensionKey): TemplateResult {
    const constraint = row.version_constraint;
    return html`${
      this.canManage
        ? html`<sl-select
            size="small"
            aria-label=${`Approval for ${row.extension_id}`}
            .value=${row.approval_state}
            ?disabled=${!!this.busy}
            @sl-change=${(event: Event) => this.approve(row, event)}
          >
            ${states.map((state) => html`<sl-option value=${state}>${label(state)}</sl-option>`)}
          </sl-select>`
        : html`<span>${label(row.approval_state)}</span>`
    }
    ${
      constraint
        ? html`<span class="meta"
            >Version constraint: <code>${constraint}</code></span
          >`
        : constraint === undefined
          ? html`<span class="meta">Version constraint unavailable</span>`
          : nothing
    }`;
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
      <td data-label="Approval">
        ${this.approval(row)}${this.stateBadges(row.effective_state)}
        ${
          this.canManage &&
          row.content_hash &&
          row.effective_state !== 'approved'
            ? html`<sl-button
                size="small"
                class="approve-exact"
                ?disabled=${!!this.busy}
                @click=${() => this.act(row, () => this.updater(row, 'approved', row.content_hash as string))}
                >Approve this exact content</sl-button
              >`
            : nothing
        }
      </td>
    </tr>`;
  }

  render(): TemplateResult | typeof nothing {
    if (!this.loading && !this.canView && !this.error) return nothing;
    return html`
      <p class="meta">
        Installed files do not prove an agent loaded or ran an extension.
        Approval decisions apply to the whole account. Unapproved does not
        disable or block an extension. Name approval is not a version-compliance
        verdict; stored version constraints are shown read-only.
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
          >${filterStates.map((state) => html`<sl-option value=${state}>${label(state)}</sl-option>`)}
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
      ${
        this.page?.completeness !== 'complete' && this.page
          ? html`
              <div class="meta" role="note">
                <p>
                  ${
                    this.page.completeness === 'partial'
                      ? 'Collection is incomplete. Previously observed extensions are retained; other installed extensions may be missing.'
                      : 'Extension coverage is unknown: no device has reported, an older collector reported without extensions, or the report did not cover user configuration. This inventory cannot distinguish these cases.'
                  }
                </p>
                <p>
                  On the device to scan, use a Preloop CLI newer than
                  ${EXTENSION_COLLECTOR_VERSION_FLOOR} with extension
                  collection. Upgrade with <code>preloop update</code> and check
                  <code>preloop version</code>. If no newer release is
                  available, extension reporting requires a later collector
                  release.
                </p>
                <p>
                  Sign in to this instance with
                  <code>preloop auth login --url ${window.location.origin}</code
                  >, then run <code>preloop agents discover --report</code> to
                  send the inventory. Reload this page after reporting.
                </p>
              </div>
            `
          : nothing
      }
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
                            <td data-label="Approval">
                              ${this.approval(row)}${this.stateBadges(row.effective_state, row.pin_count)}
                              ${
                                row.content_hashes?.length
                                  ? html`<sl-button
                                      size="small"
                                      variant="text"
                                      class="toggle-hashes"
                                      aria-expanded=${this.expanded === keyOf(row) ? 'true' : 'false'}
                                      @click=${() => this.toggle(row)}
                                      >Content hashes</sl-button
                                    >`
                                  : nothing
                              }
                            </td>
                          </tr>
                          ${this.expanded === keyOf(row) ? this.hashList(row) : nothing}`
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
      ${!this.fingerprint && this.canManage ? html`<extension-enforcement .canManage=${this.canManage} .approvalRevision=${this.approvalRevision}></extension-enforcement>` : nothing}
    `;
  }
}
