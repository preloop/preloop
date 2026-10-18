import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
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
import type { FleetMapWindow } from '../../utils/fleet-map-activity';
import {
  ACTIVITY_WINDOW_OPTIONS,
  AnnounceThrottle,
  agentChipVariant,
  agentStateLabel,
  announcement,
  formatCount,
  formatDuration,
  formatRate,
  formatUsd,
  isStale,
  latencyCell,
  loadActivityWindow,
  personChipVariant,
  personStateLabel,
  saveActivityWindow,
  sortAgents,
  sortPeople,
  sparklinePoints,
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
  /** Agents flipped to Working client-side between sweeps. */
  @state() private flipped = new Set<string>();

  private refreshTimer?: ReturnType<typeof setInterval>;
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
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribes.forEach((unsubscribe) => unsubscribe());
    this.unsubscribes = [];
    if (this.refreshTimer !== undefined) clearInterval(this.refreshTimer);
    this.refreshTimer = undefined;
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
      this.flipped = new Set();
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
        this.flipped = new Set([...this.flipped, id]);
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
        grid-template-columns: repeat(7, minmax(0, 1fr));
        gap: var(--sl-spacing-medium);
        margin: 0 0 var(--sl-spacing-medium);
        min-height: 64px;
      }
      dl.stats.busy .stat {
        opacity: 0.6;
      }
      .stat {
        display: flex;
        flex-direction: column-reverse;
        justify-content: flex-end;
      }
      .stat dd {
        margin: 0;
      }
      .stat .value {
        font-size: var(--console-stat-value);
        font-weight: 600;
        line-height: 1.2;
        font-variant-numeric: tabular-nums;
      }
      :host([fullscreen]) .stat .value {
        font-size: clamp(22px, 2.2vw, 40px);
      }
      .stat dt,
      .stat .meta-line {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }
      .sparkline {
        border-top: 1px solid var(--console-hairline);
        border-bottom: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-x-small) 0;
        margin-bottom: var(--sl-spacing-medium);
      }
      .sparkline svg {
        display: block;
        height: 56px;
        width: 100%;
      }
      .sparkline polyline {
        fill: none;
        stroke: var(--sl-color-primary-600);
        stroke-width: 1.5;
      }
      .sparkline .caption {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
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
        overflow-y: auto;
        min-height: 0;
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
      .row {
        display: grid;
        grid-template-columns: minmax(0, 1.4fr) auto minmax(0, 2fr) auto;
        gap: var(--sl-spacing-small);
        align-items: center;
        border-bottom: 1px solid var(--console-hairline);
        padding: var(--sl-spacing-x-small) 0;
        font-size: var(--console-text-body);
      }
      :host([fullscreen]) .row {
        min-height: 44px;
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
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .state {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .pulse {
        background: var(--sl-color-success-600);
        border-radius: 50%;
        height: 8px;
        width: 8px;
        animation: pulse 1.6s ease-in-out infinite;
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
        .pulse {
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
        <div class="value" title=${cell.title ?? nothing}>${cell.value}</div>
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
        : { value: `${formatUsd(m.spend_rate_usd_per_hour)}/h est.` };
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
    return html`<div class="sparkline">
      <svg viewBox="0 0 600 56" preserveAspectRatio="none" aria-hidden="true">
        <polyline points=${sparklinePoints(values, 600, 56)}></polyline>
      </svg>
      <div class="caption">
        requests/min · ${windowShortLabel(this.activityWindow)} · ${bucket}
        buckets
      </div>
    </div>`;
  }

  private relative(at: string | null | undefined): TemplateResult | string {
    if (!at) return '—';
    return html`<span title=${formatLocalDateTime(at)}
      >${formatRelativeTime(at)}</span
    >`;
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
    return html`<div class="row" role="row" data-testid="person-row">
      <span class="name" role="cell" title=${ATTRIBUTION}>
        ${person.href ? html`<a href=${person.href}>${name}</a>` : name}
      </span>
      <span role="cell">
        <sl-badge pill class="chip" variant=${personChipVariant(person.state)}
          >${personStateLabel(person.state)}</sl-badge
        >
      </span>
      <span class="meta" role="cell">
        ${
          person.awaiting_reply_since
            ? html`${
                person.awaiting_reply_session_href
                  ? html`<a href=${person.awaiting_reply_session_href}
                      >Waiting on them</a
                    >`
                  : 'Waiting on them'
              }
              ·
              ${formatDuration(
                (now - Date.parse(person.awaiting_reply_since)) / 1000
              )}${meta.length ? ' · ' : ''}`
            : nothing
        }${meta.join(' · ')}
      </span>
      <span class="meta num" role="cell">
        ${person.last_signal_kind ?? ''} ${this.relative(person.last_signal_at)}
      </span>
    </div>`;
  }

  private agentRow(agent: ActivityAgent, now: number): TemplateResult {
    const shown: ActivityAgent = this.flipped.has(agent.managed_agent_id)
      ? { ...agent, state: 'working' }
      : agent;
    const subject = agent.session_title || agent.cwd_basename || '';
    const parts: (TemplateResult | string)[] = [];
    if (agent.owner_display_name) parts.push(agent.owner_display_name);
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
    return html`<div class="row" role="row" data-testid="agent-row">
      <span class="name" role="cell">
        ${
          agent.href
            ? html`<a href=${agent.href}>${agent.display_name}</a>`
            : agent.display_name
        }
      </span>
      <span class="state" role="cell">
        ${
          shown.state === 'working'
            ? html`<span class="pulse" aria-hidden="true"></span>`
            : nothing
        }
        <sl-badge pill class="chip" variant=${agentChipVariant(shown.state)}
          >${agentStateLabel(shown, now)}</sl-badge
        >
      </span>
      <span class="meta" role="cell"
        >${parts.map((part, i) => html`${i ? ' · ' : ''}${part}`)}</span
      >
      <span class="num meta" role="cell"
        >${
          agent.requests_per_min !== undefined
            ? `${formatRate(agent.requests_per_min)} req/min`
            : ''
        }</span
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
      return html`<section class="list" data-testid="people-me">
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
      </section>`;
    const people = sortPeople(board.people);
    return html`<section class="list" data-testid="people">
      <h2>
        People (${board.metrics.people_in_window})
        <span class="muted">· ${w}</span>
      </h2>
      ${
        people.length
          ? html`<div role="table" aria-label="People">
              ${people.map((person) => this.personRow(person, now))}
            </div>`
          : html`<div class="empty">No one was active in this window.</div>`
      }
      ${
        board.more?.people
          ? html`<p class="muted">${board.more.people} more not shown</p>`
          : nothing
      }
    </section>`;
  }

  private renderAgents(board: ActivityBoard, now: number): TemplateResult {
    const w = windowShortLabel(this.activityWindow);
    const agents = sortAgents(board.agents);
    return html`<section class="list" data-testid="agents">
      <h2>Agents (${agents.length}) <span class="muted">· ${w}</span></h2>
      ${
        agents.length
          ? html`<div role="table" aria-label="Agents">
              ${agents.map((agent) => this.agentRow(agent, now))}
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
    const now = Date.now();
    const stale = board ? isStale(board.generated_at, now) : false;
    return html`
      <header>
        <h1>Activity</h1>
        <span class="live ${stale ? 'paused' : ''}" data-testid="live">
          <span class="live-dot" aria-hidden="true"></span>
          ${
            stale && board
              ? html`Live data paused · last update
                ${formatRelativeTime(board.generated_at)}`
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
