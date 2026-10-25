import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';
import {
  getAccountAgents,
  getAccountGatewayUsageSummary,
  getAccountRuntimeSessions,
  getFeatures,
  getRunners,
  getTeamMembers,
  getTeams,
  getUser,
  type RunnerRecord,
} from '../../../api';
import {
  deviceLabel,
  fleetAccess,
  fleetHref,
  getDeviceFleet,
  osLabel,
  runnerStateChip,
  type DeviceFleetDevice,
} from '../../../device-fleet-api';
import type {
  ManagedAgentSummary,
  RuntimeSessionSummary,
  Team,
  User,
} from '../../../types';
import { ConsoleStatus } from '../../../controllers/console-status';
import { readStoredUsageRange } from '../../../components/usage-card';
import '../../../components/token-figures';
import '../../../components/user-avatar';
import '../../../components/view-header';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { getAgentStatusChip } from '../../../utils/agent-display';
import { formatLocalDateTime, formatRelativeTime } from '../../../utils/date';
import { formatUsd, formatUsdExact } from '../../../utils/money';
import { roleLabel } from '../../../utils/role-label';
import {
  resolveTimeRange,
  timeRangeShortLabel,
} from '../../../utils/time-range';
import { renderWithoutSubscriptionsLine } from '../../../utils/subscription-estimate';
import { usageForOwner, type UsageTotals } from '../../../utils/user-usage';

/** A role as GET /users/{id} returns it; inherited ones name their team. */
interface UserRole {
  id: string;
  name: string;
  team_name?: string | null;
}

/** The person, with the role lists the endpoint adds to a plain User. */
export type UserDetail = User & {
  roles?: UserRole[];
  inherited_roles?: UserRole[];
};

/** One section's request: still running, landed, or failed with a reason. */
type Section<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'error'; error: string };

type SectionKey =
  'teams' | 'devices' | 'runners' | 'agents' | 'sessions' | 'usage';

/** How many sessions the page lists; the sessions page has the rest. */
export const RECENT_SESSION_LIMIT = 10;

const LOADING = { status: 'loading' } as const;

/** What a failed section says, in the reader's terms. */
const SECTION_ERRORS: Record<SectionKey, string> = {
  teams: 'Teams could not be loaded.',
  devices: 'Devices could not be loaded.',
  runners: 'Runners could not be loaded.',
  agents: 'Agents could not be loaded.',
  sessions: 'Sessions could not be loaded.',
  usage: 'Activity could not be loaded.',
};

const agentHref = (id: string): string =>
  `/console/agents/${encodeURIComponent(id)}`;
const runnerHref = (id: string): string =>
  `/console/settings/runners?runner=${encodeURIComponent(id)}`;
const sessionHref = (id: string): string =>
  `/console/runtime-sessions?sessionId=${encodeURIComponent(id)}`;

/**
 * The name a person goes by here: full name, then username, then email.
 *
 * Args:
 *   user: The account member.
 *
 * Returns:
 *   A non-empty display name.
 */
export function userDisplayName(user: User): string {
  return user.full_name || user.username || user.email || 'Unknown user';
}

/** How many team member lists are read at once. */
const TEAM_MEMBER_CONCURRENCY = 6;
const TEAMS_PAGE = 100;

/**
 * Teams the user belongs to, from the account's teams and their members.
 *
 * There is no per-user teams endpoint, so every page of teams is read (an
 * account past the first hundred is not cut off) and their member lists are
 * asked a few at a time rather than all at once.
 *
 * Args:
 *   userId: The account member.
 *
 * Returns:
 *   The teams that list the user as a member, by name.
 */
export async function teamsForUser(userId: string): Promise<Team[]> {
  const teams: Team[] = [];
  for (let skip = 0; ; skip += TEAMS_PAGE) {
    const page = await getTeams(skip, TEAMS_PAGE);
    teams.push(...page.teams);
    if (page.teams.length < TEAMS_PAGE || teams.length >= page.total) break;
  }
  const mine: Team[] = [];
  let next = 0;
  const worker = async () => {
    while (next < teams.length) {
      const team = teams[next++];
      const members = await getTeamMembers(team.id);
      if (members.some((member) => member.user_id === userId)) mine.push(team);
    }
  };
  await Promise.all(
    Array.from(
      { length: Math.min(TEAM_MEMBER_CONCURRENCY, teams.length) },
      worker
    )
  );
  return mine.sort((a, b) => a.name.localeCompare(b.name));
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * One account member: who they are, what they may do, and what they own.
 *
 * Every section has its own request, so a failing one says so in its own
 * card, with Retry, while the others render.
 */
@customElement('user-detail-view')
export class UserDetailView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);

  /** Set by the router; `params.userId` is the member on this page. */
  @property({ type: Object }) location?: { params?: { userId?: string } };

  @state() private userId = '';
  @state() private featureEnabled = true;
  @state() private user: Section<UserDetail> = LOADING;
  @state() private teams: Section<Team[]> = LOADING;
  /** Null when the reader may not see the device fleet: no section at all. */
  @state() private devices: Section<DeviceFleetDevice[]> | null = null;
  @state() private runners: Section<RunnerRecord[]> = LOADING;
  @state() private agents: Section<ManagedAgentSummary[]> = LOADING;
  @state() private sessions: Section<RuntimeSessionSummary[]> = LOADING;
  @state() private usage: Section<UsageTotals & { agents: number }> = LOADING;

  /** The Overview's Usage range, so both pages state the same window. */
  private readonly range = readStoredUsageRange();
  private generation = 0;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
      }
      .content {
        /* The shell pays the side inset; the header and the strips share one
           left edge (styles/console-styles.css, "The page box"). */
        padding: 0 0 var(--sl-spacing-2x-large);
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }
      a {
        color: var(--console-link-color);
      }
      .back {
        font-size: var(--console-text-meta);
      }
      .title-avatar {
        display: inline-flex;
        margin-right: var(--sl-spacing-small);
        vertical-align: middle;
      }
      .identity-line {
        color: var(--console-meta-color);
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
      .facts .label,
      .stat-caption {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .sep {
        color: var(--console-meta-color);
      }
      .strip {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-end;
        gap: var(--sl-spacing-medium) var(--sl-spacing-2x-large);
        padding: 12px 0;
        border-bottom: 1px solid var(--console-hairline);
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
        font-variant-numeric: tabular-nums;
      }
      .stat sl-skeleton {
        width: 5rem;
        height: 26px;
      }
      .detail-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(24rem, 1fr));
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
      .card-title .meta {
        font-weight: 400;
        margin-left: auto;
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
      ul.rows > li:first-child {
        padding-top: 0;
      }
      ul.rows > li:last-child {
        border-bottom: 0;
        padding-bottom: 0;
      }
      .row-main {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .card-empty,
      .section-error {
        margin: 0;
        color: var(--console-meta-color);
        text-align: center;
      }
      .section-error {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: var(--sl-spacing-small);
      }
      .table-card .card-empty,
      .table-card .section-error {
        padding: var(--sl-spacing-large) var(--sl-spacing-medium);
      }
      .skeleton-rows {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .table-card .skeleton-rows {
        padding: var(--sl-spacing-medium);
      }
      .skeleton-rows sl-skeleton {
        height: 16px;
      }
      .table-scroll {
        overflow-x: auto;
      }
      .styled-table td.num,
      .styled-table th.num {
        text-align: right;
      }
      .styled-table td {
        vertical-align: middle;
      }
      .primary-link {
        font-weight: 600;
      }
      .cell-sub {
        display: block;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .page-message {
        min-height: 72px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: var(--sl-spacing-small);
        color: var(--console-meta-color);
      }
    `,
  ];

  onBeforeEnter(location: { params?: { userId?: string } }): void {
    const next = location.params?.userId ?? '';
    if (next === this.userId) return;
    this.userId = next;
    if (this.isConnected) void this.load();
  }

  connectedCallback(): void {
    super.connectedCallback();
    if (!this.userId) this.userId = this.location?.params?.userId ?? '';
    void this.load();
  }

  disconnectedCallback(): void {
    // A request that lands after the page is gone writes nothing.
    this.generation += 1;
    this.sessionsObserver?.disconnect();
    this.sessionsObserver = null;
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    // Nothing watches the sessions card until this load has confirmed the
    // page is available: an earlier reveal would read sessions twice, or on
    // an edition without user management.
    this.sessionsGateOpen = false;
    this.sessionsRequested = false;
    this.sessionsObserver?.disconnect();
    this.sessionsObserver = null;
    if (!this.userId) return;
    try {
      const features = await getFeatures();
      if (generation !== this.generation) return;
      if (!features.features?.['user_management']) {
        this.featureEnabled = false;
        return;
      }
    } catch {
      // The features call failing is no reason to hide the page; the
      // requests below say for themselves whether they are allowed.
    }
    this.featureEnabled = true;
    this.sessionsGateOpen = true;
    this.requestUpdate();
    // Recent sessions sit at the bottom of the page and are its heaviest
    // read; they wait for the sections above, or for the card to scroll into
    // view, whichever comes first (UX3-20).
    this.sessions = LOADING;
    const first = [
      this.loadUser(),
      ...(
        ['teams', 'devices', 'runners', 'agents', 'usage'] as SectionKey[]
      ).map((key) => this.loadSection(key)),
    ];
    void Promise.allSettled(first).then(() => {
      if (generation === this.generation) this.requestSessions();
    });
  }

  /** Whether this load has asked for the recent sessions yet. */
  private sessionsRequested = false;
  /** Set once this load may read sessions at all. */
  private sessionsGateOpen = false;
  private sessionsObserver: IntersectionObserver | null = null;

  private requestSessions(): void {
    if (!this.sessionsGateOpen || this.sessionsRequested) return;
    this.sessionsRequested = true;
    this.sessionsObserver?.disconnect();
    this.sessionsObserver = null;
    void this.loadSection('sessions');
  }

  protected updated(): void {
    if (
      !this.sessionsGateOpen ||
      this.sessionsRequested ||
      this.sessionsObserver
    )
      return;
    const target = this.renderRoot.querySelector(
      '[data-testid="user-sessions"]'
    );
    if (!target) return;
    if (typeof IntersectionObserver === 'undefined') {
      this.requestSessions();
      return;
    }
    this.sessionsObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting))
          this.requestSessions();
      },
      { rootMargin: '200px' }
    );
    this.sessionsObserver.observe(target);
  }

  private async loadUser(): Promise<void> {
    const generation = this.generation;
    this.user = LOADING;
    try {
      const user = (await getUser(this.userId)) as UserDetail;
      if (generation === this.generation)
        this.user = { status: 'ready', data: user };
    } catch (error) {
      if (generation === this.generation)
        this.user = {
          status: 'error',
          error: errorText(error, 'This user could not be loaded.'),
        };
    }
  }

  /** The account's agents, shared by the Agents section and the spend. */
  private agentsRequest: Promise<ManagedAgentSummary[]> | null = null;

  private accountAgents(fresh: boolean): Promise<ManagedAgentSummary[]> {
    if (fresh || !this.agentsRequest) {
      this.agentsRequest = getAccountAgents({ status: 'all', limit: 100 }).then(
        (response) => response.items || []
      );
      // A failed read is not cached: Retry asks again.
      this.agentsRequest.catch(() => (this.agentsRequest = null));
    }
    return this.agentsRequest;
  }

  private async loadSection(key: SectionKey, retry = false): Promise<void> {
    const generation = this.generation;
    const userId = this.userId;
    const set = (value: Section<unknown> | null) => {
      if (generation !== this.generation) return;
      (this as unknown as Record<SectionKey, Section<unknown> | null>)[key] =
        value;
    };
    // The Devices card exists only for a reader who may see the fleet, so
    // access is settled before a skeleton is drawn for it.
    if (key === 'devices') {
      const access = await fleetAccess().catch(() => ({ view: false }));
      if (!access.view) {
        set(null);
        return;
      }
    }
    set(LOADING);
    try {
      switch (key) {
        case 'teams':
          set({ status: 'ready', data: await teamsForUser(userId) });
          break;
        case 'devices': {
          const page = await getDeviceFleet();
          set({
            status: 'ready',
            data: page.items.filter((device) =>
              device.users.some((user) => user.id === userId)
            ),
          });
          break;
        }
        case 'runners': {
          const runners = await getRunners();
          set({
            status: 'ready',
            data: runners.filter(
              (runner) => runner.registered_by_user_id === userId
            ),
          });
          break;
        }
        case 'agents': {
          const agents = await this.accountAgents(retry);
          set({
            status: 'ready',
            data: agents.filter((agent) => agent.owner_user_id === userId),
          });
          break;
        }
        case 'sessions': {
          const response = await getAccountRuntimeSessions({
            ownerUserId: userId,
            status: 'all',
            limit: RECENT_SESSION_LIMIT,
          });
          set({ status: 'ready', data: response.items || [] });
          break;
        }
        case 'usage': {
          const startDate = resolveTimeRange(this.range).startDate ?? undefined;
          const [agents, summary] = await Promise.all([
            this.accountAgents(retry),
            // The per-agent section only: the whole window, by the same
            // attribution as the agents list (B-02). The session list is
            // capped at the most recent sessions.
            getAccountGatewayUsageSummary({
              startDate,
              includeBreakdown: true,
              breakdowns: ['agents'],
            }),
          ]);
          set({
            status: 'ready',
            data: usageForOwner(userId, summary.usage_by_agent || [], agents),
          });
          break;
        }
      }
    } catch (error) {
      // The section names itself; the server's reason stays in the title.
      set({
        status: 'error',
        error: errorText(error, 'No reason given.'),
      });
    }
  }

  private timestamp(value: string | null | undefined, never = 'Never') {
    return value
      ? html`<time title=${formatLocalDateTime(value)} datetime=${value}
          >${formatRelativeTime(value)}</time
        >`
      : html`<span class="label">${never}</span>`;
  }

  private money(value: number) {
    return html`<span title=${formatUsdExact(value)}
      >${formatUsd(value)}</span
    >`;
  }

  private skeletonRows(count: number) {
    return html`<div class="skeleton-rows" aria-hidden="true">
      ${Array.from(
        { length: count },
        () => html`<sl-skeleton effect="none"></sl-skeleton>`
      )}
    </div>`;
  }

  private sectionError(key: SectionKey, reason: string) {
    return html`<p
      class="section-error"
      role="alert"
      data-testid="error-${key}"
    >
      <span title=${reason}>${SECTION_ERRORS[key]}</span>
      <sl-button size="small" @click=${() => void this.loadSection(key, true)}
        >Retry</sl-button
      >
    </p>`;
  }

  /**
   * The three states every section shares: skeleton, error with Retry, and
   * one line when the list is empty.
   */
  private sectionBody<T>(
    key: SectionKey,
    section: Section<T[]>,
    empty: string,
    rows: (data: T[]) => TemplateResult,
    skeleton = 3
  ) {
    if (section.status === 'loading') return this.skeletonRows(skeleton);
    if (section.status === 'error')
      return this.sectionError(key, section.error);
    if (!section.data.length)
      return html`<p class="card-empty" data-testid="empty-${key}">
        ${empty}
      </p>`;
    return rows(section.data);
  }

  private count(section: Section<unknown[]> | null) {
    return section?.status === 'ready' && section.data.length
      ? html`<sl-badge class="chip" variant="neutral" pill
          >${section.data.length}</sl-badge
        >`
      : nothing;
  }

  private renderFacts(user: UserDetail) {
    return html`<div
      class="facts"
      role="region"
      aria-label="User summary"
      data-testid="user-facts"
    >
      <sl-badge
        class="chip"
        variant=${user.is_active ? 'success' : 'neutral'}
        pill
        >${user.is_active ? 'Active' : 'Inactive'}</sl-badge
      >
      <span class="sep" aria-hidden="true">·</span>
      <span
        ><span class="label">Last login</span>
        ${this.timestamp(user.last_login)}</span
      >
      <span class="sep" aria-hidden="true">·</span>
      <span
        ><span class="label">Member since</span>
        ${this.timestamp(user.created_at, 'Unknown')}</span
      >
    </div>`;
  }

  private renderStrip() {
    const usage = this.usage;
    const range = timeRangeShortLabel(this.range);
    const stat = (
      value: TemplateResult | string,
      caption: string,
      id: string
    ) =>
      html`<div class="stat" data-testid=${id}>
        <span class="stat-number">${value}</span
        ><span class="stat-caption">${caption}</span>
      </div>`;
    if (usage.status === 'error')
      return html`<div class="strip" role="region" aria-label="Activity">
        ${this.sectionError('usage', usage.error)}
      </div>`;
    const loading = html`<sl-skeleton effect="none"></sl-skeleton>`;
    const ready = usage.status === 'ready' ? usage.data : null;
    return html`<div class="strip" role="region" aria-label="Activity">
      ${stat(ready ? String(ready.agents) : loading, ready?.agents === 1 ? 'Agent owned' : 'Agents owned', 'stat-agents')}
      ${stat(
        ready
          ? html`<token-figures
              total-only
              .usage=${ready.tokenUsage}
            ></token-figures>`
          : loading,
        `Tokens · ${range}`,
        'stat-tokens'
      )}
      ${stat(ready ? html`${this.money(ready.cost)}${renderWithoutSubscriptionsLine(ready.withoutSubscriptions, ready.subscription)}` : loading, `$ est. · ${range}`, 'stat-spend')}
    </div>`;
  }

  private renderRoles(user: UserDetail) {
    const direct = user.roles || [];
    const inherited = user.inherited_roles || [];
    return html`<sl-card data-testid="user-roles">
      <div slot="header" class="card-title">Roles</div>
      ${
        direct.length || inherited.length
          ? html`<ul class="rows">
              ${direct.map(
                (role) =>
                  html`<li>
                    <span>${roleLabel(role.name)}</span>
                    <span class="meta">Direct</span>
                  </li>`
              )}
              ${inherited.map(
                (role) =>
                  html`<li>
                    <span>${roleLabel(role.name)}</span>
                    <span class="meta"
                      >From team ${role.team_name || 'unknown'}</span
                    >
                  </li>`
              )}
            </ul>`
          : html`<p class="card-empty" data-testid="empty-roles">
              No roles assigned.
            </p>`
      }
    </sl-card>`;
  }

  private renderTeams() {
    return html`<sl-card data-testid="user-teams">
      <div slot="header" class="card-title">
        Teams ${this.count(this.teams)}
      </div>
      ${this.sectionBody(
        'teams',
        this.teams,
        'Not on any team.',
        (teams) =>
          html`<ul class="rows">
            ${teams.map(
              (team) =>
                html`<li>
                  <div class="row-main">
                    <a href="/console/settings/teams">${team.name}</a>
                    ${team.description ? html`<span class="meta">${team.description}</span>` : nothing}
                  </div>
                </li>`
            )}
          </ul>`
      )}
    </sl-card>`;
  }

  private renderDevices() {
    const devices = this.devices;
    if (!devices) return nothing;
    return html`<sl-card data-testid="user-devices">
      <div slot="header" class="card-title">Devices ${this.count(devices)}</div>
      ${this.sectionBody(
        'devices',
        devices,
        'No devices linked.',
        (items) =>
          html`<ul class="rows">
            ${items.map((device) => {
              const chip = runnerStateChip(device.runner_state);
              return html`<li>
                <div class="row-main">
                  <a href=${fleetHref(device.device_key)}
                    >${deviceLabel(device)}</a
                  >
                  <span class="meta"
                    >${osLabel(device.os)} · last seen
                    ${this.timestamp(device.last_seen_at)}</span
                  >
                </div>
                <sl-badge class="chip" variant=${chip.variant} pill
                  >${chip.label}</sl-badge
                >
              </li>`;
            })}
          </ul>`
      )}
    </sl-card>`;
  }

  private renderRunners() {
    return html`<sl-card data-testid="user-runners">
      <div slot="header" class="card-title">
        Runners ${this.count(this.runners)}
      </div>
      ${this.sectionBody(
        'runners',
        this.runners,
        'No runners registered.',
        (runners) =>
          html`<ul class="rows">
            ${runners.map((runner) => {
              const chip = runnerStateChip(runner.presence || runner.status);
              return html`<li>
                <div class="row-main">
                  <a href=${runnerHref(runner.id)}>${runner.name}</a>
                  <span class="meta"
                    >Last heartbeat
                    ${this.timestamp(runner.last_heartbeat)}</span
                  >
                </div>
                <sl-badge class="chip" variant=${chip.variant} pill
                  >${chip.label}</sl-badge
                >
              </li>`;
            })}
          </ul>`
      )}
    </sl-card>`;
  }

  private renderAgents() {
    return html`<sl-card class="table-card" data-testid="user-agents">
      <div slot="header" class="card-title">
        Agents owned ${this.count(this.agents)}
      </div>
      ${this.sectionBody(
        'agents',
        this.agents,
        'Owns no agents.',
        (agents) =>
          html`<div class="table-scroll">
            <table class="styled-table">
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Status</th>
                  <th class="num">Last active</th>
                </tr>
              </thead>
              <tbody>
                ${agents.map((agent) => {
                  const chip = getAgentStatusChip(agent);
                  return html`<tr>
                    <td>
                      <a class="primary-link" href=${agentHref(agent.id)}
                        >${agent.display_name}</a
                      >
                    </td>
                    <td>
                      <sl-badge class="chip" variant=${chip.variant} pill
                        >${chip.label}</sl-badge
                      >
                    </td>
                    <td class="num">
                      ${this.timestamp(agent.last_activity_at || agent.last_seen_at)}
                    </td>
                  </tr>`;
                })}
              </tbody>
            </table>
          </div>`
      )}
    </sl-card>`;
  }

  private renderSessions() {
    return html`<sl-card class="table-card" data-testid="user-sessions">
      <div slot="header" class="card-title">Recent sessions</div>
      ${this.sectionBody(
        'sessions',
        this.sessions,
        'No sessions yet.',
        (sessions) =>
          html`<div class="table-scroll">
            <table class="styled-table">
              <thead>
                <tr>
                  <th>Session</th>
                  <th class="num">Last activity</th>
                  <th class="num">Tokens</th>
                  <th class="num">$ est.</th>
                </tr>
              </thead>
              <tbody>
                ${sessions.map(
                  (session) =>
                    html`<tr>
                      <td>
                        <a class="primary-link" href=${sessionHref(session.id)}
                          >${session.title || session.session_reference || session.session_source_id}</a
                        >
                        ${session.agent_name ? html`<span class="cell-sub">${session.agent_name}</span>` : nothing}
                      </td>
                      <td class="num">
                        ${this.timestamp(session.last_activity_at || session.started_at)}
                      </td>
                      <td class="num">
                        <token-figures
                          total-only
                          .usage=${session.token_usage}
                        ></token-figures>
                      </td>
                      <td class="num">
                        ${this.money(session.estimated_cost || 0)}
                      </td>
                    </tr>`
                )}
              </tbody>
            </table>
          </div>`,
        4
      )}
    </sl-card>`;
  }

  private renderHeader(user: UserDetail | null) {
    const name = user ? userDisplayName(user) : 'User';
    const identity = user
      ? [user.email, user.username ? `@${user.username}` : null]
          .filter(Boolean)
          .join(' · ')
      : '';
    return html`<view-header headerText=${name} width="wide">
      <a slot="top" class="back" href="/console/settings/users">← All users</a>
      ${
        user
          ? html`<span slot="title-prefix" class="title-avatar"
              ><user-avatar
                .image=${user.avatar_url || ''}
                .label=${name}
                .seed=${user.id}
                .size=${32}
              ></user-avatar
            ></span>`
          : nothing
      }
      <div slot="description" class="identity-line">${identity}</div>
    </view-header>`;
  }

  render() {
    if (!this.featureEnabled)
      return html`<div class="content">
        <p class="page-message">
          User management is not available in this edition.
        </p>
      </div>`;
    const user = this.user;
    if (user.status === 'error')
      return html`${this.renderHeader(null)}
        <div class="content">
          <p class="page-message" role="alert" data-testid="user-error">
            <span title=${user.error}>This user could not be loaded.</span>
            <sl-button size="small" @click=${() => void this.loadUser()}
              >Retry</sl-button
            >
          </p>
        </div>`;
    const ready = user.status === 'ready' ? user.data : null;
    return html`${this.renderHeader(ready)}
      <div class="content">
        ${
          ready
            ? this.renderFacts(ready)
            : html`<div class="facts">${this.skeletonRows(1)}</div>`
        }
        ${this.renderStrip()}
        <div class="detail-grid">
          ${
            ready
              ? this.renderRoles(ready)
              : html`<sl-card
                  ><div slot="header" class="card-title">Roles</div>
                  ${this.skeletonRows(2)}</sl-card
                >`
          }
          ${this.renderTeams()} ${this.renderDevices()} ${this.renderRunners()}
        </div>
        ${this.renderAgents()} ${this.renderSessions()}
      </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'user-detail-view': UserDetailView;
  }
}
