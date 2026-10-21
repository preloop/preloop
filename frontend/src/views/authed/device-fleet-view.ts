import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import { Router } from '../../router';
import { ConsoleStatus } from '../../controllers/console-status';
import { formatLocalDateTime, formatRelativeTime } from '../../utils/date';
import { userHref } from '../../utils/user-href';
import {
  deviceLabel,
  fleetFindingTitle,
  fleetAccess,
  fleetHref,
  getDeviceFleet,
  getFleetDevice,
  getFleetRules,
  osLabel,
  runnerStateChip,
  saveFleetRules,
  type DeviceFleetPage,
  type DeviceFleetDevice,
  type FleetAgent,
  type FleetFinding,
  type FleetRule,
} from '../../device-fleet-api';
import '../../components/view-header';
import '../../components/fleet-map';
import { getFleetMap, type FleetMapGraph } from '../../fleet-map-api';
import {
  loadFleetMapWindow,
  windowMinutes,
  type FleetMapWindow,
} from '../../utils/fleet-map-activity';
import { loadViewMode, saveViewMode } from '../../utils/view-mode';
import consoleStyles from '../../styles/console-styles.css?inline';

const RUNNER_STATES = ['online', 'busy', 'idle', 'asleep', 'offline', 'never'];
const LIVE_RUNNER_STATES = new Set(['online', 'busy', 'idle']);
type SortKey = 'device' | 'last_seen';
type FleetView = 'list' | 'canvas';
const VIEW_KEY = 'preloop.device_fleet.view_mode';

const agentHref = (id: string): string =>
  `/console/agents/${encodeURIComponent(id)}`;
const runnerHref = (id: string): string =>
  `/console/settings/runners?runner=${encodeURIComponent(id)}`;

/** How a person came to be listed on a machine, in their terms. */
const USER_SOURCE: Record<string, string> = {
  runner_registrant: 'Registered the runner',
  agent_owner: 'Owns an agent here',
  roster: 'On the roster',
};

/** The paste-able command that onboards a discovered agent on its machine. */
export const onboardCommand = (agent: FleetAgent): string =>
  `preloop agents onboard ${agent.kind}`;

const isOpen = (finding: FleetFinding): boolean => finding.state === 'open';

type DeviceUser = DeviceFleetDevice['users'][number];

/**
 * One row per person. The same person is often both the runner's
 * registrant and an agent's owner; they are listed once, with every way
 * they relate to the machine.
 */
export function devicePeople(
  device: DeviceFleetDevice
): { user: DeviceUser; relations: string[] }[] {
  const people = new Map<string, { user: DeviceUser; relations: string[] }>();
  for (const user of device.users) {
    const key = user.id ? `id:${user.id}` : `label:${user.label}`;
    const entry = people.get(key) || { user, relations: [] };
    const relation = USER_SOURCE[user.source || ''];
    if (relation && !entry.relations.includes(relation))
      entry.relations.push(relation);
    people.set(key, entry);
  }
  return [...people.values()];
}

/**
 * Hosted agents, governed first. A managed agent reported by both a runner
 * and a discovery candidate is one agent; ungoverned entries stay apart,
 * since each is a separate gap to close.
 */
export function hostedAgents(device: DeviceFleetDevice): {
  governed: FleetAgent[];
  ungoverned: FleetAgent[];
} {
  const governed = new Map<string, FleetAgent>();
  const ungoverned: FleetAgent[] = [];
  for (const agent of device.agents) {
    if (agent.managed_agent_id) {
      if (!governed.has(agent.managed_agent_id))
        governed.set(agent.managed_agent_id, agent);
    } else {
      ungoverned.push(agent);
    }
  }
  return { governed: [...governed.values()], ungoverned };
}

/** The strip above the table, counted from the rows the server returned. */
export function fleetCounts(page: DeviceFleetPage): {
  devices: number;
  governed: number;
  ungoverned: number;
  runnersOffline: number;
  openFindings: number;
} {
  let governed = 0;
  let ungoverned = 0;
  let runnersOffline = 0;
  let openFindings = 0;
  for (const device of page.items) {
    governed += device.agents_onboarded;
    ungoverned += device.agents_not_onboarded;
    runnersOffline += device.runners.filter(
      (runner) => !LIVE_RUNNER_STATES.has(runner.state)
    ).length;
    openFindings += device.findings.filter(isOpen).length;
  }
  return {
    devices: page.summary.devices,
    governed,
    ungoverned,
    runnersOffline,
    openFindings,
  };
}

@customElement('device-fleet-view')
export class DeviceFleetView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private active = false;
  @state() private manage = false;
  @state() private loading = true;
  @state() private error = '';
  @state() private notice = '';
  @state() private page: DeviceFleetPage | null = null;
  @state() private device: DeviceFleetDevice | null = null;
  @state() private rules: FleetRule[] = [];
  @state() private tab =
    new URLSearchParams(location.search).get('tab') || 'devices';
  @state() private search = '';
  @state() private runnerState = '';
  @state() private saving = false;
  @state() private sortKey: SortKey = 'device';
  @state() private sortDescending = false;
  @state() private view: FleetView = loadViewMode(VIEW_KEY, [
    'list',
    'canvas',
  ]) as FleetView;
  @state() private map: FleetMapGraph | null = null;
  @state() private mapLoading = false;
  @state() private mapError = '';
  @state() private mapWindow: FleetMapWindow = loadFleetMapWindow();
  @state() private mapIncludeIdle = false;
  @state() private instanceAdmin = false;
  @state() private userId = '';
  private controller?: AbortController;
  private mapController?: AbortController;
  private routeSearch = location.search;
  onBeforeEnter(context: { search: string }): void {
    this.routeSearch = context.search;
    this.tab = new URLSearchParams(context.search).get('tab') || 'devices';
    this.device = null;
    if (this.isConnected) void this.load();
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        min-width: 0;
        font-size: var(--console-text-body);
      }
      .content {
        max-width: 90rem;
        margin: 0 auto;
        padding: 0 var(--sl-spacing-large) var(--sl-spacing-2x-large);
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }
      .content.map-content {
        height: var(--fleet-content-height, calc(100dvh - 220px));
        min-height: 0;
        padding: 0;
        gap: 12px;
      }
      .map-content fleet-map:not([fullscreen]) {
        flex: 1 1 0;
        min-height: 0;
        height: auto;
      }
      .map-content .limits {
        flex: none;
        margin: 0;
      }
      .view-switch {
        display: inline-flex;
        align-self: flex-end;
        border: 1px solid var(--console-button-border);
        border-radius: var(--sl-border-radius-medium);
        overflow: hidden;
        margin-bottom: calc(-1 * var(--sl-spacing-small));
      }
      .view-switch button {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font: inherit;
        font-size: var(--console-text-meta);
        padding: 4px 12px;
        border: 0;
        background: var(--console-button-face);
        color: var(--console-meta-color);
        cursor: pointer;
      }
      .view-switch button + button {
        border-left: 1px solid var(--console-button-border);
      }
      .view-switch button[aria-pressed='true'] {
        background: var(--console-selected-tint);
        color: var(--console-body-color);
        font-weight: 600;
      }
      a {
        color: var(--console-link-color);
      }
      .section-nav {
        display: flex;
        gap: var(--sl-spacing-large);
        border-bottom: 1px solid var(--console-hairline);
      }
      .section-nav a {
        padding: 8px 0;
        color: var(--console-meta-color);
        text-decoration: none;
        border-bottom: 2px solid transparent;
        margin-bottom: -1px;
      }
      .section-nav a[aria-current='page'] {
        color: inherit;
        font-weight: 600;
        border-bottom-color: var(--sl-color-primary-600);
      }
      /* DESIGN "Strip, not cards": facts about the fleet take one hairline
         row, not a card per number. */
      .strip {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-medium) var(--sl-spacing-2x-large);
        padding: 12px 0;
        border-block: 1px solid var(--console-hairline);
      }
      .stat {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .stat-number {
        font-size: var(--console-stat-value);
        font-weight: 600;
        line-height: 1.2;
      }
      .stat-caption,
      .meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        align-items: end;
        gap: var(--sl-spacing-medium);
      }
      .filters label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .filters .count {
        margin-left: auto;
        color: var(--console-meta-color);
      }
      input,
      select {
        font: inherit;
        font-size: var(--console-text-body);
        padding: 6px 8px;
        min-height: 36px;
        color: inherit;
        background: var(--console-surface);
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-medium);
      }
      .table-scroll {
        overflow-x: auto;
      }
      .styled-table {
        width: 100%;
        border-collapse: collapse;
      }
      .styled-table td {
        vertical-align: top;
      }
      th button.sort {
        all: unset;
        cursor: pointer;
        font: inherit;
      }
      th button.sort:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }
      td.num,
      th.num {
        text-align: right;
      }
      .primary-link {
        font-weight: 600;
      }
      .stack {
        display: flex;
        flex-direction: column;
        gap: 4px;
        align-items: flex-start;
      }
      .inline {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-items: center;
      }
      .empty-state {
        min-height: 72px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 8px;
        text-align: center;
        color: var(--console-meta-color);
      }
      .empty-command {
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }
      code {
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-small);
      }
      .back {
        font-size: var(--console-text-meta);
      }
      .facts {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px 10px;
        padding: 12px 0;
        border-block: 1px solid var(--console-hairline);
      }
      .sep {
        color: var(--console-meta-color);
      }
      .detail-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
        gap: var(--sl-spacing-large);
        align-items: start;
      }
      .card-title {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      ul.rows {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      ul.rows > li {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 10px 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      ul.rows > li:last-child {
        border-bottom: 0;
      }
      .row-main {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .subhead {
        margin: var(--sl-spacing-medium) 0 0;
        font-size: var(--console-text-meta);
        font-weight: 600;
        color: var(--console-meta-color);
      }
      .card-empty {
        margin: 0;
        color: var(--console-meta-color);
      }
      details summary {
        cursor: pointer;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      pre {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        font-size: var(--sl-font-size-x-small);
        background: var(--console-page);
        padding: 8px;
        border-radius: var(--sl-border-radius-medium);
      }
      .limits {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        margin: 0;
      }
      fieldset {
        border: 0;
        border-bottom: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-large) 0;
        margin: 0;
      }
      legend {
        font-weight: 600;
      }
      fieldset label {
        display: inline-flex;
        gap: 8px;
        align-items: center;
        margin: 8px 16px 8px 0;
      }
      input[type='number'] {
        width: 5rem;
      }
      @media (max-width: 640px) {
        .styled-table thead {
          display: none;
        }
        .styled-table tr,
        .styled-table td {
          display: block;
        }
        .styled-table td {
          border: 0;
          padding: 4px 0;
          text-align: left;
        }
        .styled-table tr {
          padding: 12px 0;
          border-bottom: 1px solid var(--console-hairline);
        }
        .styled-table td[data-label]::before {
          content: attr(data-label) ': ';
          color: var(--console-meta-color);
        }
      }
    `,
  ];
  private hostObserver?: ResizeObserver;
  private sizeFrame?: number;
  // Resizing a descendant during observer delivery can resize this host again.
  // Defer layout writes until the next frame and coalesce repeated notifications.
  private scheduleSizeMap = (): void => {
    if (this.sizeFrame !== undefined) return;
    this.sizeFrame = requestAnimationFrame(() => {
      this.sizeFrame = undefined;
      if (this.isConnected) this.sizeMap();
    });
  };
  private sizeMap = (): void => {
    const content = this.shadowRoot?.querySelector<HTMLElement>('.map-content');
    if (!content) return;
    const top = content.getBoundingClientRect().top;
    const bottom = 32;
    content.style.setProperty(
      '--fleet-content-height',
      `${Math.max(300, window.innerHeight - top - bottom)}px`
    );
  };
  protected updated(): void {
    this.scheduleSizeMap();
  }
  connectedCallback(): void {
    super.connectedCallback();
    this.hostObserver = new ResizeObserver(this.scheduleSizeMap);
    this.hostObserver.observe(this);
    window.addEventListener('resize', this.scheduleSizeMap);
    void this.load();
  }
  disconnectedCallback(): void {
    this.controller?.abort();
    this.mapController?.abort();
    this.hostObserver?.disconnect();
    window.removeEventListener('resize', this.scheduleSizeMap);
    if (this.sizeFrame !== undefined) cancelAnimationFrame(this.sizeFrame);
    this.sizeFrame = undefined;
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    this.controller?.abort();
    const controller = (this.controller = new AbortController());
    this.loading = true;
    this.error = '';
    try {
      const access = await fleetAccess();
      if (controller.signal.aborted) return;
      if (!access.view) {
        this.active = false;
        Router.go('/console');
        return;
      }
      this.active = true;
      this.manage = access.manage;
      this.instanceAdmin = access.instanceAdmin === true;
      this.userId = access.userId ?? '';
      if (this.tab === 'settings' && !access.manage) this.tab = 'devices';
      const key = new URLSearchParams(this.routeSearch).get('device');
      if (!key && this.tab !== 'settings' && this.view === 'canvas') {
        await this.loadMap();
        return;
      }
      // The device page does not need the whole fleet, and a fleet that
      // fails to load must not take a readable device page down with it.
      const [page, device, rules] = await Promise.all([
        key || this.tab === 'settings'
          ? null
          : getDeviceFleet(controller.signal),
        key ? getFleetDevice(key, controller.signal) : null,
        access.manage && this.tab === 'settings'
          ? getFleetRules(controller.signal)
          : null,
      ]);
      if (controller.signal.aborted) return;
      this.page = page;
      this.device = device;
      this.rules = rules?.items || [];
      if (page && this.view === 'canvas') void this.loadMap();
    } catch (error) {
      if (!controller.signal.aborted)
        this.error =
          error instanceof Error
            ? error.message
            : 'Device fleet could not be loaded. Retry to refresh it.';
    } finally {
      if (!controller.signal.aborted) this.loading = false;
    }
  }
  /** The map is its own request, so a slow graph never holds the table. */
  /** ``quiet``: a background refresh keeps the current map on failure. */
  private async loadMap(quiet = false): Promise<void> {
    this.mapController?.abort();
    const controller = (this.mapController = new AbortController());
    this.mapLoading = true;
    this.mapError = '';
    try {
      const map = await getFleetMap(
        controller.signal,
        windowMinutes(this.mapWindow),
        this.mapIncludeIdle
      );
      if (!controller.signal.aborted) this.map = map;
    } catch (error) {
      if (!controller.signal.aborted && !(quiet && this.map))
        this.mapError =
          error instanceof Error
            ? error.message
            : 'The fleet map could not be loaded. Retry to refresh it.';
    } finally {
      if (!controller.signal.aborted) this.mapLoading = false;
    }
  }

  private setView(view: FleetView): void {
    if (view === this.view) return;
    this.view = view;
    saveViewMode(VIEW_KEY, view);
    if (view === 'canvas' && !this.map && !this.mapLoading) void this.loadMap();
    if (view === 'list' && !this.page) void this.load();
  }

  private renderViewSwitch() {
    return html`<div
      class="view-switch"
      role="group"
      aria-label="Device fleet view"
    >
      ${(
        [
          ['list', 'Table', 'table'],
          ['canvas', 'Map', 'diagram-3'],
        ] as const
      ).map(
        ([view, label, icon]) =>
          html`<button
            type="button"
            aria-pressed=${this.view === view ? 'true' : 'false'}
            @click=${() => this.setView(view)}
          >
            <sl-icon name=${icon} aria-hidden="true"></sl-icon>${label}
          </button>`
      )}
    </div>`;
  }

  private renderMap() {
    if (this.mapLoading && !this.map)
      return html`<div class="empty-state" role="status">
        <sl-spinner></sl-spinner> Mapping people, devices, agents and
        providers...
      </div>`;
    if (this.mapError)
      return html`<sl-alert variant="danger" open>
        <strong>Could not load the fleet map</strong><br />
        ${this.mapError}
        <div>
          <sl-button size="small" @click=${() => void this.loadMap()}
            >Retry</sl-button
          >
          <sl-button size="small" @click=${() => this.setView('list')}
            >Back to the table</sl-button
          >
        </div>
      </sl-alert>`;
    return html`<fleet-map
      wheel-mode="pan"
      .graph=${this.map}
      .activityWindow=${this.mapWindow}
      .includeIdle=${this.mapIncludeIdle}
      live
      ?instance-admin=${this.instanceAdmin}
      user-id=${this.userId}
      @fleet-map-window-change=${(
        event: CustomEvent<{ window: FleetMapWindow }>
      ) => {
        this.mapWindow = event.detail.window;
        void this.loadMap();
      }}
      @fleet-map-stale=${() => void this.loadMap(true)}
      @fleet-map-idle-change=${(
        event: CustomEvent<{ includeIdle: boolean }>
      ) => {
        this.mapIncludeIdle = event.detail.includeIdle;
        void this.loadMap();
      }}
    ></fleet-map>`;
  }

  private async save(event: Event): Promise<void> {
    event.preventDefault();
    if (!this.manage || this.saving) return;
    this.saving = true;
    this.error = '';
    this.notice = '';
    try {
      const result = await saveFleetRules(this.rules, this.controller?.signal);
      if (!this.isConnected) return;
      this.rules = result.items;
      this.notice = 'Alert settings saved.';
    } catch (error) {
      if (this.isConnected)
        this.error =
          error instanceof Error ? error.message : 'Could not save settings.';
    } finally {
      this.saving = false;
    }
  }
  private changeRule(index: number, change: Partial<FleetRule>): void {
    this.rules = this.rules.map((rule, i) =>
      i === index ? { ...rule, ...change } : rule
    );
  }
  private timestamp(value: string | null) {
    return value
      ? html`<time title=${formatLocalDateTime(value)} datetime=${value}
          >${formatRelativeTime(value)}</time
        >`
      : html`<span class="meta">Never</span>`;
  }
  private runnerChip(state: string) {
    const chip = runnerStateChip(state);
    return html`<sl-badge class="chip" variant=${chip.variant} pill
      >${chip.label}</sl-badge
    >`;
  }
  private person(user: DeviceUser) {
    return user.id
      ? html`<a href=${userHref(user.id)}>${user.label}</a>`
      : html`<span>${user.label}</span>`;
  }
  private setSort(key: SortKey): void {
    if (this.sortKey === key) this.sortDescending = !this.sortDescending;
    else {
      this.sortKey = key;
      // Recency reads newest first; names read A to Z.
      this.sortDescending = key === 'last_seen';
    }
  }
  private sortHeader(key: SortKey, label: string) {
    const active = this.sortKey === key;
    return html`<th
      aria-sort=${active ? (this.sortDescending ? 'descending' : 'ascending') : 'none'}
    >
      <button class="sort" @click=${() => this.setSort(key)}>
        ${label}${active ? (this.sortDescending ? ' ↓' : ' ↑') : ''}
      </button>
    </th>`;
  }
  private filteredDevices(page: DeviceFleetPage): DeviceFleetDevice[] {
    const query = this.search.trim().toLowerCase();
    const devices = page.items.filter(
      (device) =>
        (!this.runnerState || device.runner_state === this.runnerState) &&
        [
          device.hostname,
          device.device_key,
          device.os,
          ...device.users.map((user) => user.label),
          ...device.agents.map((agent) => agent.name || agent.kind),
        ]
          .join(' ')
          .toLowerCase()
          .includes(query)
    );
    const direction = this.sortDescending ? -1 : 1;
    return devices.sort((a, b) =>
      this.sortKey === 'last_seen'
        ? direction *
          ((a.last_seen_at ? Date.parse(a.last_seen_at) : 0) -
            (b.last_seen_at ? Date.parse(b.last_seen_at) : 0))
        : direction * deviceLabel(a).localeCompare(deviceLabel(b))
    );
  }

  private renderStrip(page: DeviceFleetPage) {
    const counts = fleetCounts(page);
    const stat = (value: number, caption: string, testId: string) =>
      html`<div class="stat" data-testid=${testId}>
        <span class="stat-number">${value}</span
        ><span class="stat-caption">${caption}</span>
      </div>`;
    return html`<div class="strip" role="region" aria-label="Fleet summary">
      ${stat(counts.devices, counts.devices === 1 ? 'Device' : 'Devices', 'stat-devices')}
      ${stat(counts.governed, 'Governed agents', 'stat-governed')}
      ${stat(counts.ungoverned, 'Ungoverned agents', 'stat-ungoverned')}
      ${stat(counts.runnersOffline, 'Runners offline', 'stat-runners-offline')}
      ${stat(counts.openFindings, 'Open findings', 'stat-findings')}
      ${
        page.summary.has_roster
          ? stat(
              page.summary.expected_missing,
              'Expected, never seen',
              'stat-missing'
            )
          : nothing
      }
    </div>`;
  }

  private renderAgentsCell(device: DeviceFleetDevice) {
    if (!device.agents_onboarded && !device.agents_not_onboarded)
      return html`<span class="meta">None reported</span>`;
    return html`<div class="inline">
      <span>${device.agents_onboarded} governed</span>
      ${
        device.agents_not_onboarded
          ? html`<sl-badge class="chip" variant="warning" pill
              >${device.agents_not_onboarded} not onboarded</sl-badge
            >`
          : nothing
      }
    </div>`;
  }

  private renderRow(device: DeviceFleetDevice) {
    const open = device.findings.filter(isOpen).length;
    return html`<tr>
      <td data-label="Device">
        <div class="stack">
          <a class="primary-link" href=${fleetHref(device.device_key)}
            >${deviceLabel(device)}</a
          >
          <span class="meta">${osLabel(device.os)}</span>
        </div>
      </td>
      <td data-label="People">
        ${
          device.users.length
            ? html`<div class="stack">
                ${devicePeople(device).map(({ user }) => this.person(user))}
              </div>`
            : html`<span class="meta">Unknown</span>`
        }
      </td>
      <td data-label="Runner">
        <div class="stack">
          ${this.runnerChip(device.runner_state)}
          ${device.runners.map(
            (runner) =>
              html`<a class="meta" href=${runnerHref(runner.id)}
                >${runner.name}</a
              >`
          )}
        </div>
      </td>
      <td data-label="Agents">${this.renderAgentsCell(device)}</td>
      <td data-label="Findings" class="num">
        ${
          open
            ? html`<sl-badge class="chip" variant="warning" pill
                >${open} open</sl-badge
              >`
            : html`<span class="meta">None</span>`
        }
      </td>
      <td data-label="Last seen">${this.timestamp(device.last_seen_at)}</td>
    </tr>`;
  }

  private renderDevices() {
    if (this.view === 'canvas') return this.renderMap();
    const page = this.page;
    if (!page) return nothing;
    const firstDevice = html`<span
        >No devices yet. A device appears when a runner registers on it or a
        collector reports from it.</span
      >
      <span class="empty-command">
        <code>preloop runner fg</code>
        <sl-copy-button value="preloop runner fg"></sl-copy-button>
      </span>`;
    // No devices: say how to get the first one, even when agents have
    // already been seen in traffic. Filters over nothing would only mislead.
    if (!page.items.length)
      return html`<sl-card class="table-card">
          <div class="empty-state" data-testid="fleet-empty">
            ${firstDevice}
          </div>
        </sl-card>
        ${this.renderUnassigned(page.unassigned_agents)}`;

    const devices = this.filteredDevices(page);
    const filtered = Boolean(this.search || this.runnerState);
    return html`${this.renderStrip(page)}
      <div class="filters">
        <label
          >Search
          <input
            type="search"
            placeholder="Host, person or agent"
            .value=${this.search}
            @input=${(e: Event) => (this.search = (e.target as HTMLInputElement).value)}
        /></label>
        <label
          >Runner
          <select
            .value=${this.runnerState}
            @change=${(e: Event) => (this.runnerState = (e.target as HTMLSelectElement).value)}
          >
            <option value="">Any state</option>
            ${RUNNER_STATES.map((state) => html`<option value=${state}>${runnerStateChip(state).label}</option>`)}
          </select></label
        >
        <span class="count" aria-live="polite"
          >${filtered ? `${devices.length} of ${page.total}` : page.total}
          device${page.total === 1 ? '' : 's'}</span
        >
      </div>
      ${
        page.truncated
          ? html`<p class="meta" role="status">
              Showing the first ${page.items.length} of ${page.total} devices.
              Summary counts cover the devices shown.
            </p>`
          : nothing
      }
      <sl-card class="table-card">
        ${
          devices.length
            ? html`<div class="table-scroll">
                <table class="styled-table">
                  <thead>
                    <tr>
                      ${this.sortHeader('device', 'Device')}
                      <th>People</th>
                      <th>Runner</th>
                      <th>Agents</th>
                      <th class="num">Findings</th>
                      ${this.sortHeader('last_seen', 'Last seen')}
                    </tr>
                  </thead>
                  <tbody>
                    ${devices.map((device) => this.renderRow(device))}
                  </tbody>
                </table>
              </div>`
            : html`<div class="empty-state">
                <span>No devices match these filters.</span>
                <sl-button
                  size="small"
                  @click=${() => {
                    this.search = '';
                    this.runnerState = '';
                  }}
                  >Clear filters</sl-button
                >
              </div>`
        }
      </sl-card>
      ${this.renderUnassigned(page.unassigned_agents)}`;
  }

  private renderUnassigned(agents: FleetAgent[]) {
    if (!agents.length) return nothing;
    return html`<sl-card>
      <div slot="header" class="card-title">
        Agents not tied to a device
        <sl-badge class="chip" variant="neutral" pill
          >${agents.length}</sl-badge
        >
      </div>
      <p class="meta">
        Seen in traffic or onboarded, but no runner or collector has placed them
        on a machine.
      </p>
      <ul class="rows">
        ${agents.map((agent) => this.renderAgentRow(agent))}
      </ul>
    </sl-card>`;
  }

  private renderAgentRow(agent: FleetAgent) {
    const name = agent.name || agent.kind;
    return html`<li>
      <div class="row-main">
        ${
          agent.managed_agent_id
            ? html`<a href=${agentHref(agent.managed_agent_id)}>${name}</a>`
            : html`<span>${name}</span>`
        }
        <span class="meta"
          >${agent.kind}${agent.version ? ` ${agent.version}` : ''} · last seen
          ${this.timestamp(agent.last_seen_at)}</span
        >
      </div>
      ${
        agent.managed_agent_id
          ? html`<sl-badge class="chip" variant="success" pill
              >Governed</sl-badge
            >`
          : html`<span class="inline">
              <sl-badge class="chip" variant="warning" pill
                >Not onboarded</sl-badge
              >
              <sl-tooltip
                content="Run this on the machine to put the agent under Preloop"
              >
                <sl-copy-button
                  class="onboard"
                  value=${onboardCommand(agent)}
                  copy-label="Copy onboard command"
                  success-label="Onboard command copied"
                ></sl-copy-button>
              </sl-tooltip>
              <code>${onboardCommand(agent)}</code>
            </span>`
      }
    </li>`;
  }

  private renderDevice(device: DeviceFleetDevice) {
    const { governed, ungoverned } = hostedAgents(device);
    const findings = [...device.findings].sort(
      (a, b) => Number(isOpen(b)) - Number(isOpen(a))
    );
    return html`<div
        class="facts"
        role="region"
        aria-label="Device summary"
        data-testid="device-facts"
      >
        <span>${osLabel(device.os)}</span>
        <span class="sep" aria-hidden="true">·</span>
        ${this.runnerChip(device.runner_state)}
        <span class="sep" aria-hidden="true">·</span>
        <span>Last seen ${this.timestamp(device.last_seen_at)}</span>
        <span class="sep" aria-hidden="true">·</span>
        <span class="meta" title=${device.device_key}
          >${device.device_key.slice(0, 16)}…</span
        >
        <sl-copy-button
          value=${device.device_key}
          copy-label="Copy device key"
        ></sl-copy-button>
        <sl-tooltip
          content="Everything here is self-reported by software on the machine."
        >
          <sl-badge class="chip" variant="neutral" pill>Unverified</sl-badge>
        </sl-tooltip>
      </div>
      <div class="detail-grid">
        <sl-card data-testid="device-people">
          <div slot="header" class="card-title">People</div>
          ${
            device.users.length
              ? html`<ul class="rows">
                  ${devicePeople(device).map(
                    ({ user, relations }) =>
                      html`<li>
                        <div class="row-main">
                          ${this.person(user)}
                          <span class="meta">${relations.join(' · ')}</span>
                        </div>
                      </li>`
                  )}
                </ul>`
              : html`<p class="card-empty">
                  Nobody is linked yet. Whoever registers a runner here, or owns
                  an agent onboarded here, is listed.
                </p>`
          }
        </sl-card>
        <sl-card data-testid="device-runners">
          <div slot="header" class="card-title">Runners</div>
          ${
            device.runners.length
              ? html`<ul class="rows">
                  ${device.runners.map(
                    (runner) =>
                      html`<li>
                        <div class="row-main">
                          <a href=${runnerHref(runner.id)}>${runner.name}</a>
                          <span class="meta"
                            >Last heartbeat
                            ${this.timestamp(runner.last_heartbeat ?? null)}</span
                          >
                        </div>
                        ${this.runnerChip(runner.state)}
                      </li>`
                  )}
                </ul>`
              : html`<div class="card-empty">
                  <p class="card-empty">
                    No runner on this machine. Start one here to see its agents
                    and keep them governed:
                  </p>
                  <span class="empty-command">
                    <code>preloop runner fg</code>
                    <sl-copy-button value="preloop runner fg"></sl-copy-button>
                  </span>
                </div>`
          }
        </sl-card>
      </div>
      <sl-card data-testid="device-agents">
        <div slot="header" class="card-title">
          Hosted agents
          <sl-badge class="chip" variant="neutral" pill
            >${governed.length + ungoverned.length}</sl-badge
          >
        </div>
        ${
          governed.length || ungoverned.length
            ? html`${
                ungoverned.length
                  ? html`<p class="subhead">Not onboarded</p>
                      <ul class="rows" data-testid="device-ungoverned">
                        ${ungoverned.map((agent) => this.renderAgentRow(agent))}
                      </ul>`
                  : nothing
              }
              ${
                governed.length
                  ? html`<p class="subhead">Governed</p>
                      <ul class="rows" data-testid="device-governed">
                        ${governed.map((agent) => this.renderAgentRow(agent))}
                      </ul>`
                  : nothing
              }`
            : html`<p class="card-empty">
                No agents reported on this machine. Run
                <code>preloop agents discover --report</code> here to list them.
              </p>`
        }
      </sl-card>
      <sl-card data-testid="device-findings">
        <div slot="header" class="card-title">
          Findings
          ${
            findings.filter(isOpen).length
              ? html`<sl-badge class="chip" variant="warning" pill
                  >${findings.filter(isOpen).length} open</sl-badge
                >`
              : nothing
          }
        </div>
        ${
          findings.length
            ? html`<ul class="rows">
                ${findings.map(
                  (finding) =>
                    html`<li>
                      <div class="row-main">
                        <span
                          >${finding.title || fleetFindingTitle(finding.rule)}</span
                        >
                        ${finding.detail ? html`<span class="meta">${finding.detail}</span>` : nothing}
                        <span class="meta"
                          >Opened ${this.timestamp(finding.opened_at)} · checked
                          ${this.timestamp(finding.last_evaluated_at)}</span
                        >
                        <details>
                          <summary>Evidence</summary>
                          ${finding.limitation ? html`<p class="meta">${finding.limitation}</p>` : nothing}
                          <pre>
${JSON.stringify(finding.evidence, null, 2)}</pre>
                        </details>
                      </div>
                      <sl-badge
                        class="chip"
                        variant=${isOpen(finding) ? 'warning' : 'neutral'}
                        pill
                        >${finding.state}</sl-badge
                      >
                    </li>`
                )}
              </ul>`
            : html`<p class="card-empty">No findings for this machine.</p>`
        }
      </sl-card>
      ${
        device.possible_same_machine_as.length
          ? html`<p class="meta" role="note">
              Possibly the same machine as
              ${device.possible_same_machine_as.map(
                (key, index) =>
                  html`${index ? ', ' : ''}<a href=${fleetHref(key)}
                      >${key.slice(0, 16)}…</a
                    >`
              )}.
              Matching hostnames do not prove identity; upgrade the CLI to
              report a fingerprint.
            </p>`
          : nothing
      }
      <details>
        <summary>Raw evidence (${device.sources.length} sources)</summary>
        <pre>${JSON.stringify(device.sources, null, 2)}</pre>
      </details>`;
  }

  private renderSettings() {
    if (!this.manage) return nothing;
    return html`<sl-card>
      <form @submit=${this.save}>
        <p class="meta">
          Rules are off until you turn them on. Alerts go to eligible owners and
          admins, honoring their notification preferences.
        </p>
        ${this.rules.map(
          (rule, index) =>
            html`<fieldset ?disabled=${this.saving}>
              <legend>
                ${rule.rule === 'traffic_without_runner' ? 'Traffic without a live runner' : 'Discovered agents not onboarded'}
              </legend>
              <label
                ><input
                  type="checkbox"
                  .checked=${rule.enabled}
                  @change=${(e: Event) => this.changeRule(index, { enabled: (e.target as HTMLInputElement).checked })}
                />Enabled</label
              >
              ${rule.rule === 'traffic_without_runner' ? html`<label>Traffic window (minutes)<input type="number" min="1" max="10080" required .value=${String(rule.window_minutes)} @input=${(e: Event) => this.changeRule(index, { window_minutes: Number((e.target as HTMLInputElement).value) })} /></label><label>Offline grace (minutes)<input type="number" min="1" max="10080" required .value=${String(rule.grace_minutes)} @input=${(e: Event) => this.changeRule(index, { grace_minutes: Number((e.target as HTMLInputElement).value) })} /></label>` : html`<label>Not onboarded for (days)<input type="number" min="1" max="365" required .value=${String(rule.days)} @input=${(e: Event) => this.changeRule(index, { days: Number((e.target as HTMLInputElement).value) })} /></label>`}
              <div>
                ${['attention', 'email', 'push', 'webhook'].map((channel) => html`<label><input type="checkbox" .checked=${rule.channels.includes(channel)} @change=${(e: Event) => this.changeRule(index, { channels: (e.target as HTMLInputElement).checked ? [...rule.channels, channel] : rule.channels.filter((c) => c !== channel) })} />${channel}</label>`)}
              </div>
            </fieldset>`
        )}
        <p>
          <sl-button
            type="submit"
            variant="primary"
            ?loading=${this.saving}
            ?disabled=${this.saving || !this.rules.length}
            >Save alert settings</sl-button
          >
        </p>
      </form>
    </sl-card>`;
  }

  private renderError(): TemplateResult {
    return html`<sl-alert variant="danger" open role="alert">
      <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
      <strong>Could not load the device fleet</strong><br />
      ${this.error}
      <div>
        <sl-button size="small" @click=${() => void this.load()}
          >Try again</sl-button
        >
      </div>
    </sl-alert>`;
  }

  private renderBody() {
    if (this.loading)
      return html`<div class="empty-state" role="status">
        <sl-spinner></sl-spinner>
        <span
          >${this.device || new URLSearchParams(this.routeSearch).get('device') ? 'Loading device…' : 'Loading devices…'}</span
        >
      </div>`;
    if (this.error) return this.renderError();
    if (this.tab === 'settings') return this.renderSettings();
    return this.device ? this.renderDevice(this.device) : this.renderDevices();
  }

  render() {
    if (!this.active)
      return this.error
        ? html`<div class="content">${this.renderError()}</div>`
        : nothing;
    const device = this.device;
    const onDevice = Boolean(
      new URLSearchParams(this.routeSearch).get('device')
    );
    return html`<view-header
        headerText=${device ? deviceLabel(device) : 'Device fleet'}
        width="wide"
      >
        ${
          onDevice
            ? html`<a slot="top" class="back" href=${fleetHref()}
                >← All devices</a
              >`
            : nothing
        }
        ${!onDevice && this.tab !== 'settings' ? html`<div slot="main-column">${this.renderViewSwitch()}</div>` : nothing}
        <div slot="description">
          ${
            device
              ? 'Who uses this machine, what runs on it, and what Preloop last heard from it.'
              : 'Machines running Preloop software, the agents on them, and who uses them.'
          }
        </div>
      </view-header>
      <div
        class="content ${!onDevice && this.tab !== 'settings' && this.view === 'canvas' ? 'map-content' : ''}"
      >
        ${
          onDevice
            ? nothing
            : html`<nav class="section-nav" aria-label="Device fleet sections">
                <a
                  href=${fleetHref()}
                  aria-current=${this.tab !== 'settings' ? 'page' : 'false'}
                  >Devices</a
                >${
                  this.manage
                    ? html`<a
                        href="/console/settings/device-fleet?tab=settings"
                        aria-current=${this.tab === 'settings' ? 'page' : 'false'}
                        >Alert settings</a
                      >`
                    : nothing
                }<a href="/console/settings/runners">Runners</a>
              </nav>`
        }
        ${this.notice ? html`<p role="status">${this.notice}</p>` : nothing}
        ${this.renderBody()}
        <p class="limits" data-testid="fleet-limits">
          What this page shows is self-reported by Preloop software and is not
          attestation. A machine that never ran Preloop is invisible unless it
          is on the roster, and nothing here proves traffic did not bypass
          Preloop.
        </p>
      </div>`;
  }
}
