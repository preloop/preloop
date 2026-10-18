import { LitElement, css, html, nothing, svg, unsafeCSS } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/time-range-select';
import consoleStyles from '../../styles/console-styles.css?inline';
import { isCapabilityOff } from '../../capabilities';
import {
  ActivityForbiddenError,
  getActivityBoard,
  type ActivityAgent,
  type ActivityBoard,
  type ActivityPerson,
} from '../../live-activity-api';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../../services/unified-websocket-manager';
import { attachFullscreen } from '../../utils/fullscreen-host';
import { formatLocalDateTime, formatRelativeTime } from '../../utils/date';
import { getAgentKindPresentation } from '../../utils/agent-kinds';
import type { FleetMapWindow } from '../../utils/fleet-map-activity';
import {
  ACTIVITY_WINDOW_OPTIONS,
  AnnounceThrottle,
  FLIP_TTL_MS,
  agentChipVariant,
  agentStateLabel,
  announcement,
  formatAgentRate,
  formatCount,
  formatLiveRelative,
  formatRate,
  formatUsd,
  isStale,
  latencyCell,
  loadActivityWindow,
  liveFlips,
  personChipVariant,
  personStateDurationLabel,
  saveActivityWindow,
  shortDuration,
  sortAgents,
  sortPeople,
  sparklinePoints,
  sparklineSummary,
  sparklineY,
  windowMinutes,
  windowRefreshMs,
  windowShortLabel,
  type StatCell,
} from '../../utils/live-activity';

const ATTRIBUTION = 'Attributed through the onboarding credential';
const LIVE_TOPICS = [
  'activity_metrics',
  'presence',
  'gateway_activity',
  'approvals',
] as const;

/**
 * The Activity board (`/console/activity`): who and what is working right
 * now, and who is being waited on. Reads the live activity plugin's board,
 * follows its realtime topics, and can cover the viewport for a wall display.
 */
@customElement('live-activity-view')
export class LiveActivityView extends LitElement {
  /** Covers the viewport. */
  @property({ type: Boolean, reflect: true }) fullscreen = false;

  @state() board: ActivityBoard | null = null;
  @state() activityWindow: FleetMapWindow = loadActivityWindow();
  @state() loading = false;
  @state() error: string | null = null;
  @state() forbidden = false;
  @state() unavailable = false;
  @state() liveText = '';
  /**
   * Agents flipped to Working client-side between sweeps, with when. A flip
   * lasts until it expires or a sweep reports a newer state (`liveFlips`).
   */
  @state() private flipped = new Map<string, number>();
  /** The board's one clock: chip durations and relative times tick on it. */
  @state() now = Date.now();
  /** Rows hidden in full screen because the list ran out of room. */
  @state() private overflow = { people: 0, agents: 0 };

  private refreshTimer?: ReturnType<typeof setInterval>;
  private ticker?: ReturnType<typeof setInterval>;
  private resizeObserver?: ResizeObserver;
  private unsubscribes: (() => void)[] = [];
  private releaseFullscreen?: () => void;
  private throttle = new AnnounceThrottle();
  private lastState: ConnectionState | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    for (const topic of LIVE_TOPICS)
      this.unsubscribes.push(
        unifiedWebSocketManager.subscribe(topic, (message: unknown) =>
          this.onRealtime(topic, message)
        )
      );
    this.unsubscribes.push(
      unifiedWebSocketManager.onStateChange((next) => {
        if (
          next === ConnectionState.CONNECTED &&
          this.lastState !== null &&
          this.lastState !== ConnectionState.CONNECTED
        )
          void this.load();
        this.lastState = next;
      })
    );
    void this.load();
    this.scheduleRefresh();
    this.ticker = setInterval(() => {
      this.now = Date.now();
    }, 1000);
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => {
        this.fitSignature = '';
        this.fitLists();
      });
      this.resizeObserver.observe(this);
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribes.forEach((unsubscribe) => unsubscribe());
    this.unsubscribes = [];
    if (this.refreshTimer !== undefined) clearInterval(this.refreshTimer);
    this.refreshTimer = undefined;
    if (this.ticker !== undefined) clearInterval(this.ticker);
    this.ticker = undefined;
    this.resizeObserver?.disconnect();
    this.resizeObserver = undefined;
    this.releaseFullscreen?.();
    this.releaseFullscreen = undefined;
  }

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('fullscreen')) {
      this.releaseFullscreen?.();
      this.releaseFullscreen = this.fullscreen
        ? attachFullscreen(this, { onEscape: this.onEscape })
        : undefined;
    }
    this.fitLists();
  }

  /**
   * In full screen a list never scrolls and never clips a row: the rows that
   * do not fit are hidden and a "+N more" footer says how many. Measured
   * after each render, so it follows the viewport and the data.
   */
  private fitSignature = '';

  private fitLists(): void {
    // Only the viewport, full screen and the row order change the fit; the
    // 1 s ticker does not, so skip the layout reads when none of them moved.
    const rowKeys = [
      ...this.renderRoot.querySelectorAll<HTMLElement>('section.list .row'),
    ]
      .map((row) => row.dataset.key ?? '')
      .join(',');
    const signature = `${this.fullscreen}|${this.clientWidth}x${this.clientHeight}|${rowKeys}|${this.overflow.people},${this.overflow.agents}`;
    if (signature === this.fitSignature) return;
    this.fitSignature = signature;
    const next = { people: 0, agents: 0 };
    for (const key of ['people', 'agents'] as const) {
      const section = this.renderRoot.querySelector<HTMLElement>(
        `section.list[data-list="${key}"]`
      );
      const rows = section
        ? [...section.querySelectorAll<HTMLElement>('.row')]
        : [];
      rows.forEach((row) => (row.hidden = false));
      if (!this.fullscreen || !section || !rows.length) continue;
      const bottom = section.getBoundingClientRect().bottom;
      const fits = (row: HTMLElement, reserve: number) =>
        row.getBoundingClientRect().bottom <= bottom - reserve + 0.5;
      if (fits(rows[rows.length - 1], 0)) continue;
      const footer = section.querySelector<HTMLElement>('.more-footer');
      const reserve = Math.max(footer?.offsetHeight ?? 0, 28);
      let shown = rows.findIndex((row) => !fits(row, reserve));
      if (shown < 0) shown = rows.length;
      rows.slice(shown).forEach((row) => (row.hidden = true));
      next[key] = rows.length - shown;
    }
    if (
      next.people !== this.overflow.people ||
      next.agents !== this.overflow.agents
    )
      this.overflow = next;
  }

  private onEscape = (event: KeyboardEvent): void => {
    this.fullscreen = false;
    event.preventDefault();
  };

  private toggleFullscreen(): void {
    this.fullscreen = !this.fullscreen;
  }

  private scheduleRefresh(): void {
    if (this.refreshTimer !== undefined) clearInterval(this.refreshTimer);
    this.refreshTimer = undefined;
    const every = windowRefreshMs(this.activityWindow);
    if (every !== null)
      this.refreshTimer = setInterval(() => void this.load(), every);
  }

  /** Fetch the board; the numbers stay on screen (dimmed) meanwhile. */
  async load(): Promise<void> {
    this.loading = true;
    try {
      this.board = await getActivityBoard(windowMinutes(this.activityWindow));
      this.now = Date.now();
      this.flipped = liveFlips(this.flipped, this.board.agents, this.now);
      this.error = null;
      this.forbidden = false;
      this.announce();
    } catch (error) {
      if (isCapabilityOff(error)) this.unavailable = true;
      else if (error instanceof ActivityForbiddenError) this.forbidden = true;
      else this.error = 'Could not load activity.';
    } finally {
      this.loading = false;
    }
  }

  private onWindowChange(event: CustomEvent<{ value: string }>): void {
    const value = event.detail?.value as FleetMapWindow;
    if (!value || value === this.activityWindow) return;
    this.activityWindow = value;
    saveActivityWindow(value);
    this.scheduleRefresh();
    void this.load();
  }

  private announce(): void {
    if (!this.board) return;
    const text = this.throttle.next(
      announcement(
        this.board.metrics.active_people,
        this.board.metrics.active_agents
      ),
      Date.now()
    );
    if (text !== null) this.liveText = text;
  }

  /** Apply one realtime message. Client flips never change counts. */
  onRealtime(topic: string, message: unknown): void {
    if (!this.board || !message || typeof message !== 'object') return;
    const record = message as Record<string, unknown>;
    const payload = (record.payload || {}) as Record<string, unknown>;
    const type = String(record.type || '');
    if (topic === 'activity_metrics' && type === 'activity_metrics_updated') {
      const { bucket, ...metrics } = payload as Record<string, unknown>;
      const series = [...this.board.series];
      const newest = (payload.series_bucket || bucket) as
        ActivityBoard['series'][number] | undefined;
      if (newest?.bucket_start) {
        const last = series[series.length - 1];
        if (last?.bucket_start === newest.bucket_start)
          series[series.length - 1] = newest;
        else series.push(newest);
      }
      this.board = {
        ...this.board,
        metrics: { ...this.board.metrics, ...(metrics as object) },
        series,
        generated_at: String(record.timestamp || this.board.generated_at),
      };
      this.announce();
      return;
    }
    if (topic === 'presence' && type === 'presence_changed') {
      const id = String(payload.subject_id || '');
      const patch = (row: object) => ({
        ...row,
        ...(payload.state ? { state: payload.state } : {}),
        ...(payload.since !== undefined ? { since: payload.since } : {}),
        ...(payload.last_signal_at !== undefined
          ? { last_signal_at: payload.last_signal_at }
          : {}),
        ...(payload.awaiting_reply_since !== undefined
          ? { awaiting_reply_since: payload.awaiting_reply_since }
          : {}),
        ...(payload.awaiting_approval_count !== undefined
          ? { awaiting_approval_count: payload.awaiting_approval_count }
          : {}),
      });
      if (payload.subject_type === 'agent') {
        this.board = {
          ...this.board,
          agents: this.board.agents.map((agent) =>
            agent.managed_agent_id === id
              ? (patch(agent) as ActivityAgent)
              : agent
          ),
        };
      } else if (payload.subject_type === 'person') {
        const me = this.board.me;
        this.board = {
          ...this.board,
          people: this.board.people.map((person) =>
            person.user_id === id ? (patch(person) as ActivityPerson) : person
          ),
          me: me && me.user_id === id ? (patch(me) as ActivityPerson) : me,
        };
      }
      return;
    }
    if (
      topic === 'gateway_activity' &&
      type === 'model_gateway_request_started'
    ) {
      const id = String(payload.managed_agent_id || '');
      if (id && this.board.agents.some((a) => a.managed_agent_id === id)) {
        this.flipped = new Map([...this.flipped, [id, Date.now()]]);
      }
    }
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      :host([fullscreen]) {
        position: fixed;
        inset: 0;
        z-index: 1000;
        display: flex;
        flex-direction: column;
        box-sizing: border-box;
        padding: var(--sl-spacing-medium) var(--sl-spacing-large);
        background: var(--console-page);
        overflow: hidden;
      }
      header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: var(--sl-spacing-small) var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-medium);
      }
      h1 {
        font-size: var(--console-text-h1);
        font-weight: 600;
        margin: 0;
        flex: 1;
      }
      .controls {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }
      :host([fullscreen]) time-range-select {
        display: none;
      }
      .icon-button {
        background: none;
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-medium);
        color: inherit;
        cursor: pointer;
        min-height: 32px;
        min-width: 32px;
      }
      :host([fullscreen]) .icon-button {
        min-height: 44px;
        min-width: 44px;
      }
      .live {
        align-items: center;
        color: var(--console-meta-color);
        display: inline-flex;
        font-size: var(--console-text-meta);
        gap: 6px;
      }
      /* Full screen is read from across a room: the type follows the
         viewport instead of the console's compact scale. */
      :host([fullscreen]) {
        --activity-body: clamp(14px, 0.95vw, 22px);
        --activity-meta: clamp(13px, 0.82vw, 19px);
        --activity-title: clamp(15px, 1.05vw, 24px);
      }
      :host([fullscreen]) h1 {
        font-size: clamp(26px, 1.7vw, 40px);
      }
      :host([fullscreen]) .live {
        font-size: var(--activity-meta);
      }
      :host([fullscreen]) .live-dot {
        height: 0.6em;
        width: 0.6em;
      }
      .live-dot {
        background: var(--sl-color-success-600);
        border-radius: 50%;
        height: 6px;
        width: 6px;
      }
      .live.paused .live-dot {
        background: var(--sl-color-neutral-400);
      }
      dl.stats {
        display: grid;
        grid-template-columns:
          repeat(4, minmax(0, 1fr)) minmax(0, 1.35fr)
          repeat(2, minmax(0, 1fr));
        gap: var(--sl-spacing-medium);
        margin: 0 0 var(--sl-spacing-medium);
        min-height: 64px;
      }
      dl.stats.busy .stat {
        opacity: 0.6;
      }
      /* Value, then its label, then the secondary line: the same order as
         the console's other stat tiles (.stat-value, .stat-label). */
      .stat {
        display: flex;
        flex-direction: column;
        min-width: 0;
      }
      .stat dd {
        display: contents;
        margin: 0;
      }
      .stat .value {
        order: 1;
        font-size: var(--console-stat-value);
        font-weight: 600;
        line-height: 1.2;
        font-variant-numeric: tabular-nums;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .stat .value .unit {
        color: var(--console-meta-color);
        font-size: 0.5em;
        font-weight: 500;
        margin-left: 2px;
      }
      :host([fullscreen]) .stat .value {
        font-size: clamp(22px, 2.1vw, 44px);
      }
      .stat dt {
        order: 2;
      }
      .stat .meta-line {
        order: 3;
      }
      .stat dt,
      .stat .meta-line {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      :host([fullscreen]) .stat dt,
      :host([fullscreen]) .stat .meta-line {
        font-size: var(--activity-meta);
      }
      .sparkline {
        border-top: 1px solid var(--console-hairline);
        border-bottom: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-x-small) 0;
        margin-bottom: var(--sl-spacing-medium);
      }
      .sparkline .plot {
        display: flex;
        align-items: stretch;
        gap: var(--sl-spacing-small);
      }
      .sparkline svg {
        display: block;
        flex: 1;
        height: 56px;
        min-width: 0;
      }
      .sparkline polyline {
        fill: none;
        stroke: var(--sl-color-primary-600);
        stroke-width: 1.5;
        vector-effect: non-scaling-stroke;
      }
      .sparkline line.ref {
        stroke: var(--console-meta-color);
        stroke-dasharray: 4 4;
        stroke-width: 1;
        opacity: 0.6;
        vector-effect: non-scaling-stroke;
      }
      .sparkline .now {
        display: flex;
        flex-direction: column;
        justify-content: center;
        min-width: 6.5em;
        text-align: right;
      }
      .sparkline .now-value {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        font-variant-numeric: tabular-nums;
      }
      .sparkline .caption,
      .sparkline .now-meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }
      :host([fullscreen]) .sparkline .caption,
      :host([fullscreen]) .sparkline .now-meta {
        font-size: var(--activity-meta);
      }
      :host([fullscreen]) .sparkline .now-value {
        font-size: var(--activity-title);
      }
      .lists {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        gap: var(--sl-spacing-large);
      }
      :host([fullscreen]) .lists {
        flex: 1;
        min-height: 0;
      }
      :host([fullscreen]) .list {
        display: flex;
        flex-direction: column;
        overflow: hidden;
        min-height: 0;
      }
      .more-footer {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        padding-top: var(--sl-spacing-x-small);
      }
      :host([fullscreen]) .more-footer {
        font-size: var(--activity-meta);
      }
      :host([fullscreen]) h2 {
        font-size: var(--activity-title);
      }
      h2 {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        margin: 0 0 var(--sl-spacing-x-small);
      }
      h2 .muted,
      .muted {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-weight: 400;
      }
      /* The state column has a fixed width and its chips are left-aligned,
         so every chip, and the text after it, starts at the same x. */
      .row {
        display: grid;
        grid-template-columns:
          minmax(0, 1.5fr) var(--activity-state-col)
          minmax(0, 1.8fr) var(--activity-num-col);
        gap: var(--sl-spacing-small);
        align-items: center;
        border-bottom: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-x-small) 0;
        font-size: var(--console-text-body);
        min-height: 32px;
      }
      /* Every row is its own grid, so every track is fixed per list: an
         'auto' column would size per row and shift the chips. */
      section[data-list='agents'] {
        --activity-state-col: 13em;
        --activity-num-col: 7em;
      }
      section[data-list='people'],
      section[data-testid='people-me'] {
        --activity-state-col: 9em;
        --activity-num-col: 9.5em;
      }
      .row[hidden] {
        display: none;
      }
      :host([fullscreen]) .row {
        font-size: var(--activity-body);
        padding: 0.4em 0;
      }
      :host([fullscreen]) .row .meta,
      :host([fullscreen]) .row .host {
        font-size: var(--activity-meta);
      }
      :host([fullscreen]) sl-badge.chip::part(base) {
        font-size: clamp(12px, 0.75vw, 17px);
      }
      .row a {
        color: var(--console-link-color);
        text-decoration: none;
      }
      .row .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .row .meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .name {
        align-items: center;
        display: flex;
        gap: 8px;
        min-width: 0;
        overflow: hidden;
        white-space: nowrap;
      }
      .name .label {
        flex: 0 1 auto;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .name .host {
        flex: 1 1 0;
        min-width: 3em;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .kind-icon {
        flex: none;
        height: 1.15em;
        width: 1.15em;
        object-fit: contain;
        font-size: inherit;
        color: var(--console-meta-color);
      }
      .state {
        display: flex;
        align-items: center;
        justify-content: flex-start;
        gap: 6px;
        min-width: 0;
      }
      /* The dot slot is always there, so a chip never shifts when a row
         starts or stops working; only a working row paints it. */
      .pulse {
        border-radius: 50%;
        flex: none;
        height: 8px;
        width: 8px;
      }
      .pulse.on {
        background: var(--sl-color-success-600);
        animation: pulse 1.6s ease-in-out infinite;
      }
      .waiting {
        display: inline-flex;
        gap: 6px;
        margin-right: 6px;
        vertical-align: middle;
      }
      .waiting a {
        color: inherit;
      }
      @keyframes pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.35;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .pulse.on {
          animation: none;
        }
      }
      .empty {
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
        min-height: 72px;
        display: flex;
        align-items: center;
        justify-content: center;
        text-align: center;
      }
      code {
        background: var(--console-page);
        padding: 2px 6px;
        border-radius: var(--sl-border-radius-small);
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }
      @media (max-width: 1100px) {
        dl.stats {
          grid-template-columns: repeat(4, minmax(0, 1fr));
        }
      }
      @media (max-width: 640px) {
        dl.stats {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .lists {
          grid-template-columns: minmax(0, 1fr);
        }
        .fullscreen-toggle {
          display: none;
        }
      }
    `,
  ];

  private stat(
    label: string,
    cell: StatCell,
    meta: string,
    testId: string
  ): TemplateResult {
    return html`<div class="stat" data-testid=${testId}>
      <dt>${label}</dt>
      <dd>
        <div class="value" title=${cell.title ?? nothing}>
          ${cell.value}${
            cell.unit ? html`<span class="unit">${cell.unit}</span>` : nothing
          }
        </div>
        <div class="meta-line">${meta}</div>
      </dd>
    </div>`;
  }

  private renderStats(board: ActivityBoard): TemplateResult {
    const m = board.metrics;
    const w = windowShortLabel(this.activityWindow);
    const num = (value: number | null | undefined, why: string): StatCell =>
      value === null || value === undefined
        ? { value: '—', title: why }
        : { value: formatRate(value) };
    const minutes = board.window_minutes;
    const avg = (count: number) =>
      minutes
        ? `avg ${formatRate(count / minutes)} over ${w}`
        : `${formatCount(count)} in ${w}`;
    const spend: StatCell =
      m.spend_rate_usd_per_hour === null ||
      m.spend_rate_usd_per_hour === undefined
        ? { value: '—', title: 'No priced requests in the last 5 minutes' }
        : {
            value: formatUsd(m.spend_rate_usd_per_hour),
            unit: '/h est.',
          };
    const presenceOff = !board.presence_enabled;
    return html`<dl class="stats ${this.loading ? 'busy' : ''}">
      ${this.stat(
        'active people',
        presenceOff
          ? { value: '—', title: 'Presence is turned off for this account' }
          : { value: formatCount(m.active_people) },
        presenceOff
          ? ''
          : `${m.idle_people} idle · ${m.people_in_window} in ${w}`,
        'stat-people'
      )}
      ${this.stat(
        'active agents',
        { value: formatCount(m.active_agents) },
        `${m.waiting_agents} waiting`,
        'stat-agents'
      )}
      ${this.stat(
        'req/min',
        num(m.requests_per_min, 'No sweep has reported requests yet'),
        avg(m.requests_in_window),
        'stat-requests'
      )}
      ${this.stat(
        'tools/min',
        num(m.tool_calls_per_min, 'No sweep has reported tool calls yet'),
        avg(m.tool_calls_in_window),
        'stat-tools'
      )}
      ${this.stat(
        'spend rate',
        spend,
        m.spend_in_window_usd === null || m.spend_in_window_usd === undefined
          ? 'no priced usage'
          : `${formatUsd(m.spend_in_window_usd)} est. in ${w}`,
        'stat-spend'
      )}
      ${this.stat(
        'reply',
        latencyCell(m.reply_latency_median_s, m.reply_samples, 'replies'),
        `median · ${m.reply_samples}`,
        'stat-reply'
      )}
      ${this.stat(
        'approval',
        latencyCell(
          m.approval_latency_median_s,
          m.approval_samples,
          'decisions'
        ),
        `median · ${m.approval_samples}`,
        'stat-approval'
      )}
    </dl>`;
  }

  private renderSparkline(board: ActivityBoard): TemplateResult {
    const values = board.series.map((bucket) => bucket.requests);
    const bucket =
      board.window_minutes !== null && board.window_minutes <= 1440
        ? '1 min'
        : '1 h';
    const unit = bucket === '1 min' ? 'req/min' : 'req/h';
    const summary = sparklineSummary(values);
    const avgY = sparklineY(summary.avg, summary.peak, 56).toFixed(1);
    return html`<div class="sparkline" data-testid="sparkline">
      <div class="plot">
        <svg viewBox="0 0 600 56" preserveAspectRatio="none" aria-hidden="true">
          ${
            values.length > 1
              ? svg`<line class="ref" x1="0" x2="600" y1=${avgY} y2=${avgY}></line>`
              : nothing
          }
          <polyline points=${sparklinePoints(values, 600, 56)}></polyline>
        </svg>
        ${
          summary.latest !== null
            ? html`<div class="now" data-testid="sparkline-now">
                <span class="now-value"
                  >${formatCount(summary.latest)} ${unit}</span
                >
                <span class="now-meta"
                  >peak ${formatCount(summary.peak)} · avg
                  ${formatRate(summary.avg)}</span
                >
              </div>`
            : nothing
        }
      </div>
      <div class="caption">
        requests · ${windowShortLabel(this.activityWindow)} · ${bucket} buckets
        · dashed line is the average
      </div>
    </div>`;
  }

  private relative(at: string | null | undefined): TemplateResult | string {
    if (!at) return '—';
    return html`<span title=${formatLocalDateTime(at)}
      >${formatLiveRelative(at, this.now)}</span
    >`;
  }

  /** The kind's logo or icon, sized to the row's text. */
  private kindIcon(kind: string | undefined): TemplateResult {
    const look = getAgentKindPresentation(kind);
    if (look?.logo)
      return html`<img
        class="kind-icon"
        data-testid="agent-kind-icon"
        src=${look.logo}
        alt=""
        aria-hidden="true"
      />`;
    return html`<sl-icon
      class="kind-icon"
      data-testid="agent-kind-icon"
      name=${look?.icon || 'robot'}
      aria-hidden="true"
    ></sl-icon>`;
  }

  private personRow(person: ActivityPerson, now: number): TemplateResult {
    const name = person.display_name || 'You';
    const meta: string[] = [];
    const hosts = (person.devices ?? [])
      .map((d) => d.hostname)
      .filter((h): h is string => !!h);
    if (hosts.length) meta.push(hosts.join(' · '));
    if (person.agents?.length)
      meta.push(
        `${person.agents.length} ${person.agents.length === 1 ? 'agent' : 'agents'}`
      );
    const reply = latencyCell(
      person.reply_latency_median_s,
      person.reply_samples,
      'replies'
    );
    if (reply.value !== '—') meta.push(`${reply.value} reply`);
    const waitingFor = person.awaiting_reply_since
      ? `Waiting on them · ${shortDuration(
          now - Date.parse(person.awaiting_reply_since)
        )}`
      : null;
    const approvals = person.awaiting_approval_count || 0;
    return html`<div
      class="row"
      role="row"
      data-testid="person-row"
      data-key=${person.user_id}
    >
      <span class="name" role="cell" title=${ATTRIBUTION}>
        <span class="label"
          >${person.href ? html`<a href=${person.href}>${name}</a>` : name}</span
        >
      </span>
      <span class="state" role="cell">
        <span class="pulse" aria-hidden="true" data-testid="pulse-slot"></span>
        <sl-badge pill class="chip" variant=${personChipVariant(person.state)}
          >${personStateDurationLabel(person, now)}</sl-badge
        >
      </span>
      <span class="meta" role="cell">
        ${
          waitingFor || approvals
            ? html`<span class="waiting">
                ${
                  waitingFor
                    ? html`<sl-badge
                        pill
                        class="chip"
                        variant="warning"
                        data-testid="waiting-on-them"
                        >${
                          person.awaiting_reply_session_href
                            ? html`<a href=${person.awaiting_reply_session_href}
                                >${waitingFor}</a
                              >`
                            : waitingFor
                        }</sl-badge
                      >`
                    : nothing
                }${
                  approvals
                    ? html`<sl-badge
                        pill
                        class="chip"
                        variant="warning"
                        data-testid="awaiting-approvals"
                        >${approvals}
                        ${approvals === 1 ? 'approval' : 'approvals'}</sl-badge
                      >`
                    : nothing
                }
              </span>`
            : nothing
        }${meta.join(' · ')}
      </span>
      <span class="meta num" role="cell">
        ${person.last_signal_kind ?? ''} ${this.relative(person.last_signal_at)}
      </span>
    </div>`;
  }

  private agentRow(agent: ActivityAgent, now: number): TemplateResult {
    // The TTL is part of the read, so a flip expires on the ticker even when
    // nothing polls (the "any time" window, or failing reads).
    const flippedAt = this.flipped.get(agent.managed_agent_id);
    const working =
      agent.state === 'working' ||
      (flippedAt !== undefined && now - flippedAt < FLIP_TTL_MS);
    const shown: ActivityAgent = working
      ? { ...agent, state: 'working' }
      : agent;
    const subject = agent.session_title || agent.cwd_basename || '';
    const parts: (TemplateResult | string)[] = [];
    const host = agent.device?.hostname;
    if (agent.owner_display_name && agent.owner_display_name !== host)
      parts.push(agent.owner_display_name);
    if (subject)
      parts.push(
        agent.session_href
          ? html`<a href=${agent.session_href}>${subject}</a>`
          : subject
      );
    if (agent.pending_approval_id)
      parts.push(
        html`<a href="/console/approval/${agent.pending_approval_id}"
          >approval</a
        >`
      );
    return html`<div
      class="row"
      role="row"
      data-testid="agent-row"
      data-key=${agent.managed_agent_id}
    >
      <span class="name" role="cell">
        ${this.kindIcon(agent.agent_kind)}
        <span class="label"
          >${
            agent.href
              ? html`<a href=${agent.href}>${agent.display_name}</a>`
              : agent.display_name
          }</span
        >
        ${
          host
            ? html`<span class="host" data-testid="agent-host">${host}</span>`
            : nothing
        }
      </span>
      <span class="state" role="cell">
        <span
          class="pulse ${working ? 'on' : ''}"
          aria-hidden="true"
          data-testid="pulse-slot"
        ></span>
        <sl-badge pill class="chip" variant=${agentChipVariant(shown.state)}
          >${agentStateLabel(shown, now)}</sl-badge
        >
      </span>
      <span class="meta" role="cell"
        >${parts.map((part, i) => html`${i ? ' · ' : ''}${part}`)}</span
      >
      <span class="num meta" role="cell"
        >${(() => {
          const rate = formatAgentRate(agent.requests_per_min, working);
          return rate ? `${rate} req/min` : '';
        })()}</span
      >
    </div>`;
  }

  private renderPeople(board: ActivityBoard, now: number): TemplateResult {
    const w = windowShortLabel(this.activityWindow);
    if (!board.presence_enabled)
      return html`<section class="list" data-testid="people">
        <h2>People</h2>
        <div class="empty">Presence is turned off for this account.</div>
      </section>`;
    if (!board.viewer.can_see_people)
      return html`<section
        class="list"
        data-testid="people-me"
        data-list="people"
      >
        <h2>You</h2>
        ${
          board.me
            ? html`<div role="table" aria-label="You">
                ${this.personRow(board.me, now)}
              </div>`
            : html`<div class="empty">
                No activity from you in this window.
              </div>`
        }
        <p class="muted">
          People on this account are visible to administrators.
        </p>
        ${this.moreFooter(this.overflow.people, 'people')}
      </section>`;
    const people = sortPeople(board.people);
    return html`<section class="list" data-testid="people" data-list="people">
      <h2>
        People (${board.metrics.people_in_window})
        <span class="muted">· ${w}</span>
      </h2>
      ${
        people.length
          ? html`<div role="table" aria-label="People">
              ${repeat(
                people,
                (person) => person.user_id,
                (person) => this.personRow(person, now)
              )}
            </div>`
          : html`<div class="empty">No one was active in this window.</div>`
      }
      ${
        board.more?.people
          ? html`<p class="muted">${board.more.people} more not shown</p>`
          : nothing
      }
      ${this.moreFooter(this.overflow.people, 'people')}
    </section>`;
  }

  private moreFooter(
    hidden: number,
    noun: string
  ): TemplateResult | typeof nothing {
    if (!hidden) return nothing;
    return html`<div class="more-footer" data-testid="more-${noun}">
      +${hidden} more ${noun} · leave full screen to see all
    </div>`;
  }

  private renderAgents(board: ActivityBoard, now: number): TemplateResult {
    const w = windowShortLabel(this.activityWindow);
    const agents = sortAgents(board.agents);
    return html`<section class="list" data-testid="agents" data-list="agents">
      <h2>Agents (${agents.length}) <span class="muted">· ${w}</span></h2>
      ${
        agents.length
          ? html`<div role="table" aria-label="Agents">
              ${repeat(
                agents,
                (agent) => agent.managed_agent_id,
                (agent) => this.agentRow(agent, now)
              )}
            </div>`
          : html`<div class="empty">
              Connect your first agent:&nbsp;<code>preloop agents onboard</code>
            </div>`
      }
      ${
        board.more?.agents
          ? html`<p class="muted">${board.more.agents} more not shown</p>`
          : nothing
      }
      ${this.moreFooter(this.overflow.agents, 'agents')}
    </section>`;
  }

  render(): TemplateResult {
    if (this.unavailable) return html``;
    if (this.forbidden)
      return html`<div class="empty" data-testid="forbidden">
        You do not have permission to view activity.&nbsp;<a href="/console"
          >Back</a
        >
      </div>`;
    const board = this.board;
    const now = this.now;
    const stale = board ? isStale(board.generated_at, now) : false;
    return html`
      <header>
        <h1>Activity</h1>
        <span class="live ${stale ? 'paused' : ''}" data-testid="live">
          <span class="live-dot" aria-hidden="true"></span>
          ${
            stale && board
              ? html`Live data paused · last update
                ${formatRelativeTime(board.generated_at, new Date(now))}`
              : 'live'
          }
        </span>
        <div class="controls">
          <time-range-select
            .value=${this.activityWindow}
            .options=${ACTIVITY_WINDOW_OPTIONS}
            ?busy=${this.loading}
            ariaLabel="Activity window"
            style="--time-range-select-width: 160px"
            @range-change=${this.onWindowChange}
          ></time-range-select>
          <button
            type="button"
            class="icon-button fullscreen-toggle"
            data-testid="activity-fullscreen"
            aria-pressed=${this.fullscreen ? 'true' : 'false'}
            aria-label=${this.fullscreen ? 'Exit full screen' : 'Full screen'}
            title=${this.fullscreen ? 'Exit full screen (Esc)' : 'Full screen'}
            @click=${this.toggleFullscreen}
          >
            <sl-icon
              name=${this.fullscreen ? 'fullscreen-exit' : 'arrows-fullscreen'}
            ></sl-icon>
          </button>
        </div>
      </header>
      <div class="sr-only" aria-live="polite" data-testid="announce">
        ${this.liveText}
      </div>
      ${
        this.error && !board
          ? html`<div class="empty">${this.error}</div>`
          : nothing
      }
      ${
        board
          ? html`
              ${this.renderStats(board)} ${this.renderSparkline(board)}
              ${
                board.unavailable.includes('history_before') &&
                board.history_before
                  ? html`<p class="muted">
                      History before
                      ${formatLocalDateTime(board.history_before)} is not
                      available.
                    </p>`
                  : nothing
              }
              <div class="lists">
                ${this.renderPeople(board, now)}
                ${this.renderAgents(board, now)}
              </div>
            `
          : nothing
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'live-activity-view': LiveActivityView;
  }
}
