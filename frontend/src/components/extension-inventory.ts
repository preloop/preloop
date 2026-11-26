import { LitElement, css, html, nothing } from 'lit';
import type { PropertyValues, TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import './extension-enforcement';
import { tableScrollStyles } from '../styles/table-scroll';
import { formatRelativeTime } from '../utils/date';
import { fleetHref } from '../device-fleet-api';
import { downloadText } from '../utils/records-format';
import {
  addExtensionPin,
  deleteExtensionPin,
  extensionAccess,
  fetchExtensionPolicyText,
  getExtensionPins,
  getExtensions,
  getExtensionDevices,
  setExtensionApproval,
  type ExtensionAggregate,
  type ExtensionCoverage,
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

/** Unknown and development versions are not evidence that an upgrade is needed. */
export function collectorTooOld(version: string | null): boolean {
  const match =
    /^v?(\d+)\.(\d+)\.(\d+)(?:-[a-zA-Z][\w.-]*)?(?:\+[\w.-]+)?$/.exec(
      version || ''
    );
  if (!match) return false;
  const floor = EXTENSION_COLLECTOR_VERSION_FLOOR.split('.').map(Number);
  for (let i = 0; i < 3; i++) {
    const difference = Number(match[i + 1]) - floor[i];
    if (difference) return difference < 0;
  }
  return true;
}

/** A report older than this is stale: the device should report again. */
export const EXTENSION_REPORT_STALE_MS = 24 * 60 * 60 * 1000;

export interface ReasonText {
  /** What happened, in plain words. */
  text: string;
  /** The one next step for this reason. */
  step: string;
  /** Command for the step, if any; rendered on its own line. */
  command?: string;
}

/**
 * Collector failure codes, from cli/internal/cmd/agents_inventory_extensions.go.
 * Reports carry only the code, never the affected file, hook or project.
 */
export const EXTENSION_REASONS: Record<string, ReasonText> = {
  extension_hook_unbound: {
    text: 'A hooks folder holds a script that is not tied to an agent event, so it was not listed as a hook.',
    step: 'Register the script under an event in the agent settings, or remove it from the hooks folder.',
  },
  extension_unreadable: {
    text: 'Some extension files or folders could not be read, often because of file permissions.',
    step: 'Check which files fail locally, then fix their permissions.',
    command: 'preloop agents extensions --project .',
  },
  extension_malformed: {
    text: 'A settings, plugin or hooks file is not valid, or an entry has an unexpected shape, so it was skipped.',
    step: 'Find the file that fails locally and correct it.',
    command: 'preloop agents extensions --project .',
  },
  extension_project_unscanned: {
    text: 'A project has project-level extensions that this report did not scan.',
    step: 'From that project folder, report again with the project included.',
    command: 'preloop agents discover --report --project .',
  },
  extension_symlink: {
    text: 'An extension path is a symbolic link. Links are not followed, so its contents were not listed.',
    step: 'Replace the link with the real folder if it should be inventoried.',
  },
  extension_unsafe_path: {
    text: 'A plugin points to an install path outside the home folder, so it was not read.',
    step: 'Reinstall the plugin in the default location if it should be inventoried.',
  },
  extension_file_limit: {
    text: 'An extension file is larger than the collector reads, so it was skipped.',
    step: 'Check for an unexpectedly large settings or plugin file.',
  },
  extension_limit: {
    text: 'The collector reached its limit on extensions, folder entries or projects. Some were not listed.',
    step: 'Remove unused extensions or projects, then report again.',
  },
  extension_unsupported_scope: {
    text: 'A Claude Code plugin is installed with a scope other than user, project or local, so it was not listed.',
    step: 'Update the Preloop CLI, then report again.',
    command: 'preloop update',
  },
  extension_unsupported_version: {
    text: 'The Claude Code plugin registry uses a format version this collector does not read, so its plugins were not listed.',
    step: 'Update the Preloop CLI, then report again.',
    command: 'preloop update',
  },
};

export const COLLECTOR_TOO_OLD: ReasonText = {
  text: `This collector does not collect extensions. Extension collection needs a Preloop CLI newer than ${EXTENSION_COLLECTOR_VERSION_FLOOR}.`,
  step: 'Update the Preloop CLI, then report again.',
  command: 'preloop update',
};

export function reasonText(code: string): ReasonText {
  return (
    EXTENSION_REASONS[code] ?? {
      text: 'The collector reported a problem this page does not describe yet.',
      step: 'Contact support with the code below.',
    }
  );
}

/** True when the device never reported a time or reported over a day ago. */
export function reportStale(
  device: ExtensionCoverage,
  now = Date.now()
): boolean {
  if (!device.reported_at) return true;
  return now - Date.parse(device.reported_at) > EXTENSION_REPORT_STALE_MS;
}

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

const detailKey = (row: ExtensionKey): string =>
  JSON.stringify([
    keyOf(row),
    'fingerprint' in row ? (row as InstalledExtension).fingerprint : '',
    'profile_ref' in row ? (row as InstalledExtension).profile_ref : '',
    'scope' in row ? (row as InstalledExtension).scope : '',
    'project_ref_hash' in row
      ? (row as InstalledExtension).project_ref_hash
      : '',
  ]);

/** Presence and account-wide decisions; never implies loading or execution. */
@customElement('extension-inventory')
export class ExtensionInventory extends LitElement {
  @property() fingerprint = '';
  /** Limit rows to one agent kind, for example claude_code. */
  @property() agentKind = '';
  /** Replaces the default empty-state sentence when set. */
  @property() emptyText = '';
  @property({ attribute: false }) loader = getExtensions;
  @property({ attribute: false }) updater = setExtensionApproval;
  @property({ attribute: false }) accessLoader = extensionAccess;
  @property({ attribute: false }) policyLoader = fetchExtensionPolicyText;
  @property({ attribute: false }) policySaver = downloadText;
  @property({ attribute: false }) deviceLoader = getExtensionDevices;
  @property({ attribute: false }) pinLoader = getExtensionPins;
  @property({ attribute: false }) pinAdder = addExtensionPin;
  @property({ attribute: false }) pinRemover = deleteExtensionPin;
  @property({ attribute: false }) confirmUnpin = (message: string): boolean =>
    window.confirm(message);
  @property({ attribute: false }) copier = (text: string): Promise<void> =>
    navigator.clipboard.writeText(text);
  @state() private expanded = '';
  /** Row whose approval this user just set to Unapproved. */
  @state() private unapprovedHint = '';
  @state() private copied = '';
  private expandedRow: InstalledExtension | ExtensionAggregate | null = null;
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
  @state() private search = '';
  @state() private detailDevices: InstalledExtension[] = [];
  @state() private deviceCursor: string | null = null;
  @state() private deviceError = '';
  private searchTimer?: ReturnType<typeof setTimeout>;
  private requestId = 0;
  private access?: Promise<{ view: boolean; manage: boolean }>;

  static styles = [
    tableScrollStyles,
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }
      .description {
        margin: 5px 0;
        font-weight: 400;
        max-width: 48ch;
        white-space: normal;
      }
      .invocation {
        display: block;
        font-size: var(--console-text-meta);
        margin-top: 4px;
        white-space: normal;
      }
      .search {
        display: grid;
        gap: 5px;
        font-size: var(--console-text-meta);
      }
      .search input {
        font: inherit;
        color: inherit;
        background: var(--sl-input-background-color);
        border: 1px solid var(--console-hairline);
        border-radius: 4px;
        padding: 8px;
        min-width: 240px;
      }
      .extension-details td {
        white-space: normal;
        background: var(--console-surface);
      }
      .extension-details code {
        overflow-wrap: anywhere;
      }
      .extension-details p {
        margin: 8px 0;
      }
      .extension-details ul {
        padding-left: 20px;
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
      .summary {
        display: flex;
        flex-wrap: wrap;
        gap: 8px 16px;
        align-items: flex-start;
        margin-bottom: 16px;
      }
      .summary-text {
        padding-top: 2px;
      }
      details > summary {
        cursor: pointer;
        list-style-position: inside;
      }
      .coverage {
        flex: 1 1 320px;
      }
      .coverage-body {
        margin-top: 8px;
        padding: 12px;
        border: 1px solid var(--console-hairline);
        border-radius: 6px;
        max-width: 720px;
      }
      .coverage-body p,
      .steps p {
        margin: 0 0 8px;
      }
      .device + .device {
        border-top: 1px solid var(--console-hairline);
        padding-top: 8px;
      }
      .device h4 {
        margin: 0 0 4px;
        font-size: var(--console-text-body);
      }
      .meta.inline {
        display: inline;
      }
      .reasons {
        list-style: none;
        padding: 0;
        margin: 0 0 8px;
        display: grid;
        gap: 12px;
      }
      .reason p {
        margin: 0 0 4px;
      }
      .reason .code {
        color: var(--console-meta-color);
      }
      .command {
        display: flex;
        align-items: center;
        gap: 4px;
        margin: 0 0 8px;
        overflow-x: auto;
      }
      .command code {
        padding: 2px 6px;
        border: 1px solid var(--console-hairline);
        border-radius: 4px;
        white-space: pre;
      }
      .how {
        display: inline-block;
        font-weight: 400;
      }
      .how summary {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .how ul {
        margin: 4px 0 0;
        padding-left: 16px;
        max-width: 320px;
        font-size: var(--console-text-meta);
      }
      .skeleton {
        display: grid;
        gap: 12px;
        padding: 8px 0;
      }
      .skeleton sl-skeleton {
        height: 20px;
      }
      .empty-state {
        padding: 16px 0;
      }
      .footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: 16px;
      }
      .how-narrow {
        display: none;
      }
      .how-always {
        flex-basis: 100%;
      }
      @media (max-width: 640px) {
        .how-narrow {
          display: block;
          flex-basis: 100%;
        }
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
    for (const key of ['fingerprint', 'agentKind'] as const)
      if (changes.has(key) && changes.get(key) !== undefined) {
        void this.refresh();
        return;
      }
  }
  disconnectedCallback(): void {
    this.requestId++;
    super.disconnectedCallback();
  }

  async refresh(append = false): Promise<void> {
    const id = ++this.requestId;
    this.loading = true;
    this.error = '';
    // Keep the current rows on screen while a refresh loads; the skeleton is
    // only for a panel that has nothing to show yet.
    try {
      // Permissions do not change while the panel is open: read them once.
      this.access ??= this.accessLoader();
      const access = await this.access;
      if (id !== this.requestId) return;
      this.canView = access.view;
      this.canManage = access.manage;
      if (!access.view) return;
      const filters: ExtensionFilters = {
        q: this.search || undefined,
        fingerprint: this.fingerprint || undefined,
        agent_kind: this.agentKind || undefined,
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
      this.dispatchEvent(
        new CustomEvent('extensions-loaded', {
          detail: {
            count: this.page.items.length,
            more: !!this.page.next_cursor,
          },
          bubbles: true,
          composed: true,
        })
      );
      // An open hash list re-reads its pins so pin and unpin show at once.
      if (this.expandedRow && !append) {
        const pins = await this.pinLoader(this.expandedRow);
        if (id === this.requestId) {
          this.pins = pins;
          if (
            !this.page.items.some((row) => detailKey(row) === this.expanded)
          ) {
            this.expanded = '';
            this.expandedRow = null;
          } else if (this.expandedRow && !('fingerprint' in this.expandedRow)) {
            void this.loadDetailDevices(this.expandedRow);
          }
        }
      }
    } catch (error) {
      this.access = undefined;
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
      this.unapprovedHint = next === 'unapproved' ? keyOf(row) : '';
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

  private async toggle(
    row: InstalledExtension | ExtensionAggregate
  ): Promise<void> {
    const key = detailKey(row);
    if (this.expanded === key) {
      this.expanded = '';
      this.expandedRow = null;
      return;
    }
    this.expanded = key;
    this.expandedRow = row;
    this.pins = [];
    this.detailDevices = [];
    this.deviceCursor = null;
    this.deviceError = '';
    if (!('fingerprint' in row)) void this.loadDetailDevices(row);
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
    }
    ${
      this.unapprovedHint === keyOf(row) && row.approval_state === 'unapproved'
        ? html`<span class="meta unapproved-hint" role="status"
            >Unapproved is a record only. It does not disable or block the
            extension. ${this.enforcementLink()}</span
          >`
        : nothing
    }`;
  }

  /** Enforcement is account-wide: only the unfiltered account view has it. */
  private hasEnforcement(): boolean {
    return !this.fingerprint && !this.agentKind && this.canManage;
  }

  private enforcementLink(): TemplateResult {
    return this.hasEnforcement()
      ? html`<a
          href="#extension-enforcement"
          class="enforcement-link"
          @click=${(event: Event) => {
            event.preventDefault();
            this.renderRoot
              .querySelector('#extension-enforcement')
              ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }}
          >Set up blocking in Enforcement</a
        >`
      : html`<span>To block, use Enforcement on the Agents page.</span>`;
  }

  private async copy(text: string): Promise<void> {
    try {
      await this.copier(text);
      this.copied = text;
    } catch {
      this.copied = '';
    }
  }

  /** One command per line, with a copy button. */
  private command(text: string): TemplateResult {
    return html`<div class="command">
      <code>${text}</code
      ><sl-button
        size="small"
        variant="text"
        class="copy copy-command"
        aria-label=${`Copy ${text}`}
        @click=${() => this.copy(text)}
        >${this.copied === text ? 'Copied' : 'Copy'}</sl-button
      >
    </div>`;
  }

  private howApprovalsWork(): TemplateResult {
    return html`<details class="how">
      <summary>How approvals work</summary>
      <ul>
        <li>
          This list shows what is installed. It does not show what an agent
          loaded or ran.
        </li>
        <li>An approval applies to the whole account, on every device.</li>
        <li>
          Unapproved is a record only. It does not disable or block an
          extension. Use Enforcement to block.
        </li>
        <li>
          Approving a name does not check versions. Stored version constraints
          are shown read-only.
        </li>
      </ul>
    </details>`;
  }

  /** Report and sign-in steps, for devices that have not reported recently. */
  private reportSteps(): TemplateResult {
    return html`<div class="steps">
      <p>
        On the device, sign in to this instance, then report from the
        repository:
      </p>
      ${this.command(`preloop auth login --url ${window.location.origin}`)}
      ${this.command('preloop agents discover --report --project .')}
      <p>Reload this page after reporting.</p>
    </div>`;
  }

  private reasonItem(reason: ReasonText, code: string): TemplateResult {
    return html`<li class="reason" data-code=${code}>
      <p>${reason.text}</p>
      <p>${reason.step}</p>
      ${reason.command ? this.command(reason.command) : nothing}
      <code class="code">${code}</code>
    </li>`;
  }

  private coverageStatus(): TemplateResult | typeof nothing {
    const page = this.page;
    if (!page) return nothing;
    const devices = page.coverage ?? [];
    const label =
      page.completeness === 'complete'
        ? 'Complete'
        : page.completeness === 'partial'
          ? 'Scan partial'
          : 'Coverage unknown';
    // Literal variants per state; computed badge variants are not allowed.
    const badge =
      page.completeness === 'complete'
        ? html`<sl-badge variant="success" pill class="coverage-badge"
            >${label}</sl-badge
          >`
        : page.completeness === 'partial'
          ? html`<sl-badge variant="warning" pill class="coverage-badge"
              >${label}</sl-badge
            >`
          : html`<sl-badge variant="neutral" pill class="coverage-badge"
              >${label}</sl-badge
            >`;
    const one = devices.length === 1 ? devices[0] : null;
    return html`<details class="coverage" data-state=${page.completeness}>
      <summary>
        ${badge}
        ${
          one
            ? html`<span class="meta inline"
                >Device ${one.fingerprint.slice(0, 8)} · Collector
                ${one.collector_version || 'unknown'}</span
              >`
            : nothing
        }
      </summary>
      <div class="coverage-body">
        ${
          page.completeness === 'complete'
            ? html`<p>
                Every reporting device scanned all extension locations.
              </p>`
            : page.completeness === 'partial'
              ? html`<p>
                  Some extensions could not be read. Extensions seen before stay
                  listed; others may be missing.
                </p>`
              : html`<p>
                  The reports do not show whether every extension was scanned.
                </p>`
        }
        ${devices.map((device) => this.deviceCoverage(device, devices.length > 1))}
        ${
          !devices.length
            ? html`<p>No device has reported extensions yet.</p>
                ${this.reportSteps()}`
            : nothing
        }
      </div>
    </details>`;
  }

  private deviceCoverage(
    device: ExtensionCoverage,
    heading: boolean
  ): TemplateResult {
    const old = collectorTooOld(device.collector_version);
    const stale = reportStale(device);
    const reasons = device.reasons.filter(
      // Updating fixes these; the too-old item already says so.
      (code) => !(old && code.startsWith('extension_unsupported'))
    );
    return html`<section class="device" data-fingerprint=${device.fingerprint}>
      ${
        heading
          ? html`<h4>
              Device ${device.fingerprint.slice(0, 8)}
              <span class="meta inline"
                >Collector ${device.collector_version || 'unknown'} ·
                ${label(device.completeness)}</span
              >
            </h4>`
          : nothing
      }
      <p class="meta">
        ${
          device.reported_at
            ? html`Last report
                <span title=${device.reported_at}
                  >${formatRelativeTime(device.reported_at)}</span
                >`
            : 'Report time unknown'
        }
      </p>
      ${
        old || reasons.length
          ? html`<ul class="reasons">
              ${old ? this.reasonItem(COLLECTOR_TOO_OLD, 'collector_too_old') : nothing}
              ${reasons.map((code) => this.reasonItem(reasonText(code), code))}
            </ul>`
          : device.completeness !== 'complete'
            ? html`<p>This device did not report a reason.</p>`
            : nothing
      }
      ${
        stale || (device.completeness === 'unknown' && !old)
          ? html`<p>
                ${stale ? 'This device has not reported in the last day.' : 'Report again to establish coverage.'}
              </p>
              ${this.reportSteps()}`
          : nothing
      }
      ${
        device.reasons.length && !stale
          ? html`<p class="meta">
              Each code above shows the reason, not the file. Run the local
              check on the device to see which file failed.
            </p>`
          : nothing
      }
    </section>`;
  }

  private summary(): TemplateResult | typeof nothing {
    const page = this.page;
    if (!page) return nothing;
    const counts = new Map<string, number>();
    for (const row of page.items)
      counts.set(row.extension_kind, (counts.get(row.extension_kind) ?? 0) + 1);
    const devices = page.coverage ?? [];
    const times = devices
      .map((device) => device.reported_at)
      .filter((value): value is string => !!value)
      .sort();
    const last = times[times.length - 1];
    const kinds = [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(
        ([kind, count]) =>
          `${count} ${label(kind).toLowerCase()}${count === 1 ? '' : 's'}`
      )
      .join(', ');
    return html`<div class="summary">
      <span class="summary-text" aria-live="polite"
        >${page.items.length}${page.next_cursor ? '+' : ''}
        extensions${kinds ? html` (${kinds})` : nothing} · ${devices.length}
        ${devices.length === 1 ? 'device' : 'devices'}
        reporting${
          last
            ? html` · Last report
                <span title=${last}>${formatRelativeTime(last)}</span>`
            : nothing
        }</span
      >
      ${this.coverageStatus()}
      <!-- The table heading is hidden on narrow screens; keep the help reachable. -->
      <div class=${page.items.length ? 'how-narrow' : 'how-always'}>
        ${this.howApprovalsWork()}
      </div>
    </div>`;
  }

  private skeleton(): TemplateResult {
    return html`<div
      class="skeleton"
      role="status"
      aria-label="Loading extensions"
    >
      ${Array.from({ length: 6 }, () => html`<sl-skeleton effect="pulse"></sl-skeleton>`)}
    </div>`;
  }

  private hash(value: string | null): TemplateResult | typeof nothing {
    return value
      ? html`<code class="meta" title=${value}>${value.slice(0, 19)}…</code>`
      : nothing;
  }

  private extensionIdentity(row: ExtensionKey): TemplateResult {
    const details = row.details;
    return html`<strong>${details?.name || row.extension_id}</strong>
      ${details?.name && details.name !== row.extension_id ? html`<span class="meta">${row.extension_id}</span>` : nothing}
      ${details?.description ? html`<p class="description">${details.description}</p>` : details?.description_withheld ? html`<span class="meta">Description withheld for privacy</span>` : html`<span class="meta">No description reported</span>`}
      ${details?.invocation ? html`<span class="invocation">${details.invocation}</span>` : nothing}`;
  }

  private detailsToggle(
    row: InstalledExtension | ExtensionAggregate
  ): TemplateResult {
    return html`<sl-button
      size="small"
      variant="text"
      class="toggle-details toggle-hashes"
      aria-expanded=${this.expanded === detailKey(row) ? 'true' : 'false'}
      aria-label=${`Details for ${row.extension_id}`}
      @click=${() => this.toggle(row)}
      >Details<span class="sr-only"> for ${row.extension_id}</span></sl-button
    >`;
  }

  private async loadDetailDevices(
    row: ExtensionKey,
    append = false
  ): Promise<void> {
    const key = detailKey(row);
    try {
      const page = await this.deviceLoader(
        row,
        append ? (this.deviceCursor ?? undefined) : undefined
      );
      if (this.expanded !== key) return;
      const devices = page.items.filter(
        (item): item is InstalledExtension => 'fingerprint' in item
      );
      this.detailDevices = append
        ? [...this.detailDevices, ...devices]
        : devices;
      this.deviceCursor = page.next_cursor;
    } catch {
      if (this.expanded === key)
        this.deviceError = 'Device details could not be loaded.';
    }
  }

  /** Extension point for a future content viewer keyed by the reported hash. */
  protected renderExtensionContentViewer(
    _row: InstalledExtension | ExtensionAggregate,
    _contentHash: string | null
  ): TemplateResult | typeof nothing {
    return nothing;
  }

  private detailsRow(
    row: InstalledExtension | ExtensionAggregate
  ): TemplateResult {
    const details = row.details;
    const quote = (value: string): string =>
      "'" + value.replace(/'/g, "'\"'\"'") + "'";
    const agent =
      row.agent_kind === 'claude_code'
        ? 'claude-code'
        : row.agent_kind === 'codex' || row.agent_kind === 'codex_cli'
          ? 'codex-cli'
          : row.agent_kind;
    const command = `preloop agents extensions show ${row.extension_kind} ${quote(row.extension_id)} --agent ${quote(agent)}`;
    const devices = 'fingerprint' in row ? [row] : this.detailDevices;
    const hashes =
      'fingerprint' in row
        ? [row.content_hash ?? null]
        : (row.content_hashes ?? []).map((bucket) => bucket.content_hash);
    return html`<tr class="extension-details">
      <td colspan=${this.fingerprint ? 9 : 7}>
        ${details?.author ? html`<p>Author: ${details.author}</p>` : nothing}
        ${details?.plugin ? html`<p>Bundled by ${details.plugin}</p>` : nothing}
        ${details?.script_count != null ? html`<p>Bundled scripts: ${details.script_count}</p>` : nothing}
        ${details?.bundles ? html`<p class="plugin-contents">Plugin contents: ${details.bundles.skills} ${details.bundles.skills === 1 ? 'skill' : 'skills'}, ${details.bundles.commands} ${details.bundles.commands === 1 ? 'command' : 'commands'}, ${details.bundles.hooks} ${details.bundles.hooks === 1 ? 'hook' : 'hooks'}, ${details.bundles.mcp_servers} MCP ${details.bundles.mcp_servers === 1 ? 'server' : 'servers'}, ${details.bundles.agents} ${details.bundles.agents === 1 ? 'agent' : 'agents'}</p>` : nothing}
        ${
          details?.hook_matchers?.length
            ? html`<ul aria-label="Hook matchers">
                ${details.hook_matchers.map(
                  (group) =>
                    html`<li>
                      <strong
                        >${group.matcher_withheld ? 'Matcher withheld for privacy' : group.matcher || 'All tools / no matcher'}</strong
                      >: ${group.count} commands
                      <ul>
                        ${group.commands.map((command) => html`<li><code>${command.executable}</code>${command.preloop_managed ? ' (Preloop-managed)' : ''}</li>`)}
                      </ul>
                    </li>`
                )}
              </ul>`
            : nothing
        }
        ${!('fingerprint' in row) ? html`<p>Installed on ${row.device_count} ${row.device_count === 1 ? 'device' : 'devices'}. Metadata above is from the latest matching report.</p>` : nothing}
        ${
          devices.length
            ? html`<ul aria-label="Device locations">
                ${devices.map(
                  (device) =>
                    html`<li>
                      <a href=${fleetHref(`fp:${device.fingerprint}`)}
                        >Device ${device.fingerprint.slice(0, 12)}</a
                      >
                      <span>
                        ·
                        ${label(device.scope)}${device.project_ref_hash ? html` · Project <code>${device.project_ref_hash}</code>` : nothing}</span
                      >
                      <span>
                        · Profile
                        ${device.profile_ref === 'current' ? 'current' : device.profile_ref.slice(0, 12)}</span
                      >
                      ${device.content_hash ? html`<div>Content hash: <code>${device.content_hash}</code></div>` : html`<span> · Content hash unavailable</span>`}
                      ${device.source_ref_hash ? html`<div>Source hash: <code>${device.source_ref_hash}</code></div>` : nothing}
                    </li>`
                )}
              </ul>`
            : nothing
        }
        ${this.deviceError ? html`<p role="status">${this.deviceError}</p>` : nothing}
        ${this.deviceCursor ? html`<sl-button size="small" @click=${() => this.loadDetailDevices(row, true)}>More devices</sl-button>` : nothing}
        <p>
          Read the full content locally on a device that reported this
          extension:
        </p>
        ${this.command(command)}
        <p class="meta">
          For project extensions, run from that repository and append --project
          .
        </p>
        ${hashes.map((hash) => this.renderExtensionContentViewer(row, hash))}
      </td>
    </tr>`;
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
        ${this.extensionIdentity(row)}${row.id_hashed ? html`<span class="meta">Hashed identifier</span>` : nothing}
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
        ${this.detailsToggle(row)}
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
      ${this.summary()}
      <div class="filters">
        <label class="search"
          >Search name or description
          <input
            type="search"
            .value=${this.search}
            @input=${(event: Event) => {
              this.search = (event.target as HTMLInputElement).value;
              clearTimeout(this.searchTimer);
              this.searchTimer = setTimeout(() => void this.refresh(), 200);
            }}
          />
        </label>
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
      ${this.loading && !this.page ? this.skeleton() : nothing}
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
                    <th class="approval-heading">
                      Approval ${this.howApprovalsWork()}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  ${[...this.page.items]
                    .sort(
                      (a, b) =>
                        a.extension_kind.localeCompare(b.extension_kind) ||
                        (a.details?.name || a.extension_id).localeCompare(
                          b.details?.name || b.extension_id
                        )
                    )
                    .map((row) =>
                      'fingerprint' in row
                        ? html`${this.deviceRow(row)}${this.expanded === detailKey(row) ? this.detailsRow(row) : nothing}`
                        : html`<tr>
                              <td data-label="Agent">${row.agent_kind}</td>
                              <td data-label="Kind">
                                ${label(row.extension_kind)}
                              </td>
                              <td class="identity" data-label="Extension">
                                ${this.extensionIdentity(row)}
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
                                ${this.detailsToggle(row)}
                              </td>
                            </tr>
                            ${this.expanded === detailKey(row) ? html`${this.detailsRow(row)}${this.hashList(row)}` : nothing}`
                    )}
                </tbody>
              </table>
            </div>`
          : this.page && !this.loading
            ? this.emptyText &&
              !this.stateFilter &&
              !this.sourceFilter &&
              !this.search.trim()
              ? html`<p class="empty">${this.emptyText}</p>
                  ${this.page.coverage?.length ? nothing : this.reportSteps()}`
              : this.page.coverage?.length ||
                  this.stateFilter ||
                  this.sourceFilter ||
                  this.search.trim()
                ? html`<p class="empty">
                    No extensions reported for this selection.
                  </p>`
                : html`<div class="empty-state">
                    <p>No extensions reported yet.</p>
                    ${this.reportSteps()}
                  </div>`
            : nothing
      }
      ${this.page ? html`<div class="footer"><span class="meta">${this.page.items.length} extensions shown</span>${this.page.next_cursor ? html`<sl-button size="small" ?loading=${this.loading} @click=${() => this.refresh(true)}>Load more</sl-button>` : nothing}</div>` : nothing}
      ${this.hasEnforcement() ? html`<extension-enforcement id="extension-enforcement" .canManage=${this.canManage} .approvalRevision=${this.approvalRevision}></extension-enforcement>` : nothing}
    `;
  }
}
