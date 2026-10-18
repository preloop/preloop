import { LitElement, css, html, nothing, svg, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import {
  FLEET_MAP_CAP,
  FLEET_MAP_COLUMN_LABEL,
  FLEET_MAP_EDGE_LABEL,
  FLEET_MAP_NODE_HEIGHT,
  FLEET_MAP_TYPES,
  FLEET_MAP_TYPE_LABEL,
  agentState,
  blastRadius,
  dependencies,
  filterGraph,
  layoutGraph,
  nodeGaps,
  type FleetMapAgentState,
  type FleetMapColumn,
  type FleetMapEdge,
  type FleetMapGapCode,
  type FleetMapGraph,
  type FleetMapLayout,
  type FleetMapNode,
  type FleetMapNodeType,
  type FleetCodeAction,
  type FleetCodeActivity,
  type FleetMapPosition,
  getFleetCodeActivity,
  withCodeFlows,
} from '../fleet-map-api';
import { formatRelativeTime } from '../utils/date';
import {
  FLEET_ACTIVITY_TOPICS,
  FLEET_MAP_WINDOWS,
  FleetActivityTracker,
  activeGraph,
  activityFromRealtime,
  FLEET_MAP_STALE_DEBOUNCE_MS,
  windowRefreshMs,
  activityLabel,
  edgesForActivity,
  saveFleetMapWindow,
  windowMinutes,
  type FleetMapWindow,
} from '../utils/fleet-map-activity';
import {
  loadArrangement,
  loadPositions,
  saveArrangement,
  savePositions,
  spatialLayout,
  type FleetMapArrangement,
  type FleetMapPoints,
} from '../utils/fleet-map-spatial';
import {
  FleetLights,
  PIP_MS,
  clusterOf,
  codeHitFromRealtime,
  codeLabel,
  countCode,
  messagesForRealtime,
  nextPresence,
  ownerOf,
  presenceStyle,
  type FleetMessage,
  type FleetPip,
  type FleetPresence,
} from '../utils/fleet-map-motion';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import { getAgentKindPresentation } from '../utils/agent-kinds';
import consoleStyles from '../styles/console-styles.css?inline';

const ICON: Record<FleetMapNodeType, string> = {
  user: 'person',
  device: 'laptop',
  runner: 'hdd-rack',
  agent: 'robot',
  flow: 'diagram-2',
  model: 'stars',
  provider_account: 'key',
  provider: 'cloud',
  cluster: 'collection',
};

const STATES: { state: FleetMapAgentState; label: string }[] = [
  { state: 'onboarded', label: 'Onboarded' },
  { state: 'discovered', label: 'Discovered' },
  { state: 'inactive', label: 'Inactive' },
];

/** Type toggles, in reading order; runners and accounts are nested rows. */
const TYPE_TOGGLE_LABEL: Record<(typeof FLEET_MAP_TYPES)[number], string> = {
  user: 'People',
  device: 'Devices',
  runner: 'Runners',
  agent: 'Agents',
  flow: 'Flows',
  model: 'Models',
  provider_account: 'Accounts',
  provider: 'Providers',
};

const EMPTY: FleetMapGraph = {
  schema_version: 1,
  limit: 500,
  clusters: [],
  unavailable: [],
  truncated: false,
  nodes: [],
  edges: [],
};

const MIN_FIT_SCALE = 0.6;
/** Spatial maps fit both ways, so they may shrink further. */
const MIN_SPATIAL_FIT_SCALE = 0.35;
/** Nodes and the view glide to a new arrangement in this long (medium). */
const GLIDE_MS = 400;
/** After a pan or zoom by hand the view stops following the fleet a while. */
const HANDS_OFF_MS = 15_000;
const glide = (t: number): number =>
  t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
type View = { x: number; y: number; s: number };

/** Counter order on a node: opened, reviewed, commented, merged. */
const CODE_BADGES: readonly [FleetCodeAction, string, string][] = [
  ['opened', 'plus-circle', 'opened'],
  ['reviewed', 'eye', 'reviewed'],
  ['commented', 'chat-left-text', 'commented on'],
  ['merged', 'check2-circle', 'merged'],
];

/** "cursor_cli" reads as "Cursor CLI"; known kinds use their product name. */
export function agentKindLabel(kind: string): string {
  return (
    getAgentKindPresentation(kind)?.label ??
    kind
      .split(/[_\s-]+/)
      .filter(Boolean)
      .map((word) =>
        ['cli', 'ai', 'ide'].includes(word.toLowerCase())
          ? word.toUpperCase()
          : word.charAt(0).toUpperCase() + word.slice(1)
      )
      .join(' ')
  );
}

/** Discovered agents are named by their kind; show it as a product name. */
const displayLabel = (node: FleetMapNode): string =>
  node.type === 'agent' && node.detail && node.label === node.detail
    ? agentKindLabel(node.detail)
    : node.label;

/**
 * The fleet as one picture: people, their machines (runners inside them),
 * the agents on them, the models those agents call and the providers behind
 * them (provider accounts inside). By default it shows onboarded agents that
 * were active recently; filters bring in discovered and inactive ones.
 * Selecting a node lights what depends on it and what it relies on, lists
 * the blast radius beside the map, and can hide everything unrelated. Gaps
 * (ungoverned agents, offline runners, accounts that bypass the gateway,
 * open findings) are marked on the node itself.
 *
 * Public surface: `graph`, `fullscreen` (reflected), `hasSelection()`,
 * `clearSelection()`. Fires `fleet-map-fullscreen` with `{ fullscreen }`.
 */
@customElement('fleet-map')
export class FleetMap extends LitElement {
  @property({ attribute: false }) graph: FleetMapGraph | null = null;
  /** Covers the viewport. Toggled by the map's own button or by a host. */
  @property({ type: Boolean, reflect: true }) fullscreen = false;
  /**
   * Which resources the map shows by recent activity. The host refetches on
   * `fleet-map-window-change`; a server that ignores the window is filtered
   * here with the same rule.
   */
  @property({ attribute: false }) activityWindow: FleetMapWindow = 'any';
  /** Light relationships from the console's realtime stream. */
  @property({ type: Boolean }) live = false;
  /** Instance administrators may arrange the map in space. */
  @property({ type: Boolean, attribute: 'instance-admin' })
  instanceAdmin = false;
  /** Always spatial, with no Columns choice (the Overview inventory map). */
  @property({ type: Boolean, attribute: 'spatial-only' }) spatialOnly = false;
  /** Arrangement until the user picks one (stored per user and scope). */
  @property({ attribute: 'default-arrangement' })
  defaultArrangement: FleetMapArrangement = 'columns';
  /** Whose arrangement and dragged positions are stored. */
  @property({ attribute: 'user-id' }) userId = '';
  /** Separates stored positions of different maps (fleet, inventory). */
  @property({ attribute: 'positions-scope' }) positionsScope = 'fleet';
  /**
   * Pull and merge requests per node in the window. A live map loads it
   * itself from `/fleet/code-activity`; a host may set it instead.
   */
  @property({ attribute: false }) codeActivity: FleetCodeActivity | null = null;
  /** Force the reduced-motion fallback (tests); the media query also does. */
  @property({ type: Boolean, attribute: 'reduced-motion' })
  reducedMotion = false;
  @state() private pips: FleetPip[] = [];
  @state() private toasts: FleetPip[] = [];
  @state() private arrangement: FleetMapArrangement = 'columns';
  @state() private points: FleetMapPoints = {};
  @state() private selected = '';
  @state() private types = new Set<FleetMapNodeType>(FLEET_MAP_TYPES);
  @state() private owner = '';
  @state() private provider = '';
  @state() private states = new Set<FleetMapAgentState>(['onboarded']);
  @state() private gapsOnly = false;
  @state() private query = '';
  @state() private focusGap: FleetMapGapCode | '' = '';
  @state() private focusOnly = false;
  @state() private expanded = new Set<FleetMapColumn>();
  @state() private collapsed = new Set<string>();

  /** Whether a node or a gap is selected, which Esc would clear. */
  hasSelection(): boolean {
    return Boolean(this.selected || this.focusGap);
  }

  /** Clear the node and gap selection, as Esc inside the canvas does. */
  clearSelection(): void {
    this.selected = '';
    this.focusGap = '';
    this.focusOnly = false;
  }

  // A line stays lit 2.5 s after its last request: long enough to read,
  // short enough that a busy fleet still shows which lines are live now.
  private readonly activity = new FleetActivityTracker(
    () => this.requestUpdate(),
    { holdMs: 2500 }
  );
  private unsubscribes: (() => void)[] = [];
  private drag: {
    id: number;
    node: string;
    x: number;
    y: number;
    origin: { x: number; y: number };
    moved: boolean;
  } | null = null;
  private suppressClick = false;
  private spatialCache?: { key: unknown[]; layout: FleetMapLayout };

  private refreshTimer?: ReturnType<typeof setInterval>;
  private windowFit = false;
  private staleTimer?: ReturnType<typeof setTimeout>;

  /**
   * Place one realtime message on the map. Activity from an agent the map
   * doesn't show means it just became active, so the host is asked to
   * refetch (debounced, so a burst costs one request).
   */
  recordActivity(topic: string, message: unknown): void {
    if (topic === 'code_activity') {
      this.recordCode(message);
      return;
    }
    if (this.graph)
      this.send(messagesForRealtime(this.shown(), topic, message));
    const hit = activityFromRealtime(topic, message);
    if (!hit || !this.graph) return;
    const edges = edgesForActivity(this.graph, hit);
    if (edges.length) this.activity.add(edges, hit.kind);
    // Only an agent the map lacks makes the graph stale. A mapped agent with
    // no matching line (ambient model, no device) just shows no highlight,
    // and under Any time activity can't reveal anything new.
    else if (
      this.activityWindow !== 'any' &&
      !this.graph.nodes.some((node) => node.id === hit.agent)
    )
      this.requestRefresh(FLEET_MAP_STALE_DEBOUNCE_MS);
  }

  private readonly lights = new FleetLights();
  private presence: FleetPresence = {
    entered: new Map(),
    leaving: new Map(),
  };
  private shownNodes: FleetMapNode[] | null = null;
  private frame = 0;
  private nextPip = 1;
  private codeController?: AbortController;
  private codeTimer?: ReturnType<typeof setTimeout>;

  /** True when nothing should travel or fade. */
  get motionOff(): boolean {
    return (
      this.reducedMotion ||
      (typeof window !== 'undefined' &&
        !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
    );
  }

  /** Nodes lit by live traffic right now, for tests and the demo harness. */
  get litNodes(): string[] {
    return this.lights.active(Date.now()).map((item) => item.node);
  }

  /** The graph as drawn: the server graph plus flows that did code work. */
  private shown(): FleetMapGraph {
    return this.filtered();
  }

  /**
   * Light the node each message starts from: the agent or flow sending a
   * request, the model answering, the person deciding an approval. The line
   * it travels lights through the activity tracker. Nothing moves, so this
   * is the same with reduced motion. A node folded into a "+N more" row or
   * hidden by a filter is not lit.
   */
  private send(messages: FleetMessage[]): void {
    if (!messages.length || !this.instanceAdmin) return;
    const now = Date.now();
    let lit = false;
    for (const message of messages)
      if (this.positionOf(message.from)) {
        this.lights.light(message, now);
        lit = true;
      }
    if (lit) {
      this.lights.changed(now);
      this.requestUpdate();
      this.startFrames();
    }
  }

  /** A code event: count it, surface it on the node and in the corner. */
  private recordCode(message: unknown): void {
    const hit = codeHitFromRealtime(message);
    if (!hit || !this.instanceAdmin) return;
    const view = this.shown();
    const ids = new Set(view.nodes.map((node) => node.id));
    const nodes = hit.nodes.filter((id) => ids.has(id));
    const owners = nodes
      .filter((id) => id.startsWith('agent:'))
      .map((id) => ownerOf(view, id))
      .filter((id): id is string => !!id);
    const counts = countCode(
      this.codeActivity?.counts ?? {},
      [...nodes, ...owners],
      hit.action
    );
    this.codeActivity = {
      active_within_minutes: windowMinutes(this.activityWindow),
      since: this.codeActivity?.since ?? new Date().toISOString(),
      truncated: this.codeActivity?.truncated ?? false,
      flows: this.codeActivity?.flows ?? [],
      events: this.codeActivity?.events ?? [],
      counts,
    };
    // A flow or agent the map lacks: its node arrives with the next read.
    if (nodes.length < hit.nodes.length) this.scheduleCodeLoad(1500);
    if (!nodes.length) return;
    const now = Date.now();
    const label = codeLabel(hit);
    const fresh = nodes.map((node) => ({
      id: this.nextPip++,
      node,
      action: hit.action,
      label,
      start: now,
    }));
    // Under reduced motion nothing rises off the node; the counter and the
    // corner list still say what happened.
    if (!this.motionOff) this.pips = [...this.pips, ...fresh].slice(-8);
    this.toasts = [...fresh.slice(0, 1), ...this.toasts].slice(0, 4);
    for (const node of nodes) {
      const owner = owners.find((id) => ownerOf(view, node) === id);
      if (owner) this.send([{ from: node, to: owner, kind: 'code', label }]);
    }
    this.startFrames();
  }

  private seenEvents = new Set<string>();

  /**
   * List what just happened on load too: events from the last two minutes
   * that the corner has not shown yet, newest first, at most three.
   */
  seedToasts(activity: FleetCodeActivity, now = Date.now()): void {
    const fresh = activity.events.filter(
      (event) =>
        !this.seenEvents.has(event.id) &&
        now - new Date(event.at).getTime() < 120_000
    );
    for (const event of activity.events) this.seenEvents.add(event.id);
    if (!fresh.length || !this.instanceAdmin) return;
    const toasts = fresh.slice(0, 3).map((event) => ({
      id: this.nextPip++,
      node: event.node_id,
      action: event.action,
      label: codeLabel({ action: event.action, pr: event.pr ?? '' }),
      start: now,
    }));
    this.toasts = [...toasts, ...this.toasts].slice(0, 4);
    this.startFrames();
  }

  /** Load the code activity for the window, debounced. */
  private scheduleCodeLoad(delay = 0): void {
    // The code layer is instance-admin only for now, like the spatial map.
    if (!this.live || !this.instanceAdmin) return;
    if (this.codeTimer !== undefined) clearTimeout(this.codeTimer);
    this.codeTimer = setTimeout(() => {
      this.codeTimer = undefined;
      void this.loadCode();
    }, delay);
  }

  private async loadCode(): Promise<void> {
    this.codeController?.abort();
    const controller = (this.codeController = new AbortController());
    try {
      const activity = await getFleetCodeActivity(
        controller.signal,
        windowMinutes(this.activityWindow)
      );
      if (controller.signal.aborted) return;
      this.codeActivity = activity;
      this.seedToasts(activity);
    } catch {
      // Code activity is a layer on the map; the map stands without it.
    }
  }

  /** Run frames while anything moves; stop as soon as nothing does. */
  private startFrames(): void {
    if (this.frame || !this.isConnected) return;
    this.frame = requestAnimationFrame(this.tick);
  }

  private tick = (): void => {
    this.frame = 0;
    const now = Date.now();
    if (this.lights.changed(now)) this.requestUpdate();
    const camera = this.stepCamera(now);
    const tweening = this.tween && now - this.tween.start < GLIDE_MS;
    if (tweening) this.requestUpdate();
    else if (this.tween) {
      this.tween = undefined;
      this.requestUpdate();
    }
    const pips = this.pips.filter((pip) => now - pip.start < PIP_MS);
    if (pips.length !== this.pips.length) this.pips = pips;
    const toasts = this.toasts.filter((pip) => now - pip.start < PIP_MS * 2);
    if (toasts.length !== this.toasts.length) this.toasts = toasts;
    const before = this.presence.leaving.size + this.presence.entered.size;
    this.presence = nextPresence(this.presence, null, [], now);
    if (this.presence.leaving.size + this.presence.entered.size !== before)
      this.requestUpdate();
    this.paintFrame(now);
    if (
      this.lights.size ||
      camera ||
      tweening ||
      this.pips.length ||
      this.toasts.length ||
      this.presence.leaving.size ||
      this.presence.entered.size
    )
      this.frame = requestAnimationFrame(this.tick);
  };

  /** Move messages and fade nodes for this instant, without a re-render. */
  private paintFrame(now: number): void {
    const root = this.shadowRoot;
    const layout = this.lastLayout;
    if (!root || !layout) return;
    for (const el of root.querySelectorAll<HTMLElement>('[data-presence]')) {
      const style = presenceStyle(
        this.presence,
        el.dataset.presence || '',
        now
      );
      el.style.opacity = style ? style.opacity.toFixed(3) : '';
      el.style.transform = style ? `scale(${style.scale.toFixed(3)})` : '';
    }
    for (const el of root.querySelectorAll<HTMLElement>('[data-pip]')) {
      const start = Number(el.dataset.start);
      const t = Math.min(1, Math.max(0, (now - start) / PIP_MS));
      // Shown beside the node, then faded over the last third.
      el.style.opacity = (t < 0.66 ? 1 : 1 - (t - 0.66) / 0.34).toFixed(3);
    }
  }

  private positionOf(id: string): FleetMapPosition | undefined {
    const layout = this.lastLayout;
    const anchor = layout?.anchor(id);
    return anchor ? layout?.positions.get(anchor) : undefined;
  }

  /** Fire `fleet-map-stale` once after ``delay`` ms; repeat calls coalesce. */
  private requestRefresh(delay = 0): void {
    if (!this.live || this.staleTimer !== undefined) return;
    this.staleTimer = setTimeout(() => {
      this.staleTimer = undefined;
      this.dispatchEvent(
        new CustomEvent('fleet-map-stale', { bubbles: true, composed: true })
      );
    }, delay);
  }

  /** Poll on the window's cadence so quiet resources leave a short window. */
  private scheduleRefresh(): void {
    if (this.refreshTimer !== undefined) clearInterval(this.refreshTimer);
    this.refreshTimer = undefined;
    const every = this.live ? windowRefreshMs(this.activityWindow) : null;
    if (every !== null && this.isConnected)
      this.refreshTimer = setInterval(() => this.requestRefresh(), every);
  }

  /** Repaint pending activity now rather than at the next throttle tick. */
  flushActivity(): void {
    this.activity.flush();
  }

  connectedCallback(): void {
    super.connectedCallback();
    if (this.live)
      for (const topic of FLEET_ACTIVITY_TOPICS)
        this.unsubscribes.push(
          unifiedWebSocketManager.subscribe(topic, (message: unknown) =>
            this.recordActivity(topic, message)
          )
        );
    this.syncCodeSubscription();
  }

  private codeUnsubscribe?: () => void;

  /**
   * Only an instance administrator's live map listens to `code_activity`;
   * nobody else is sent events the map would not show them.
   */
  private syncCodeSubscription(): void {
    const want = this.live && this.instanceAdmin && this.isConnected;
    if (want && !this.codeUnsubscribe)
      this.codeUnsubscribe = unifiedWebSocketManager.subscribe(
        'code_activity',
        (message: unknown) => this.recordActivity('code_activity', message)
      );
    else if (!want && this.codeUnsubscribe) {
      this.codeUnsubscribe();
      this.codeUnsubscribe = undefined;
    }
  }

  private get spatial(): boolean {
    return (
      this.instanceAdmin && (this.spatialOnly || this.arrangement === 'spatial')
    );
  }

  private setArrangement(value: FleetMapArrangement): void {
    if (!this.instanceAdmin || value === this.arrangement) return;
    this.arrangement = value;
    saveArrangement(this.userId, value, this.positionsScope);
    this.refit = true;
  }

  private setWindow(value: FleetMapWindow): void {
    if (
      value === this.activityWindow ||
      !FLEET_MAP_WINDOWS.some((item) => item.value === value)
    )
      return;
    this.activityWindow = value;
    saveFleetMapWindow(value);
    this.dispatchEvent(
      new CustomEvent('fleet-map-window-change', {
        detail: { window: value, minutes: windowMinutes(value) },
        bubbles: true,
        composed: true,
      })
    );
  }

  private scale = 1;
  private translateX = 24;
  private translateY = 12;
  private pan: { id: number; x: number; y: number } | null = null;
  private lastLayout: FleetMapLayout | null = null;
  private refit = true;

  /** Gestures change only the transform and zoom label, never graph templates. */
  private applyTransform(): void {
    const plane = this.shadowRoot?.querySelector<HTMLElement>('.plane');
    if (plane)
      plane.style.transform = `translate(${this.translateX}px,${this.translateY}px) scale(${this.scale})`;
    const zoom = this.shadowRoot?.querySelector('.zoom');
    if (zoom) zoom.textContent = `${Math.round(this.scale * 100)}%`;
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (
      changed.has('userId') ||
      changed.has('positionsScope') ||
      changed.has('defaultArrangement')
    ) {
      this.arrangement = loadArrangement(
        this.userId,
        this.positionsScope,
        this.defaultArrangement
      );
      this.points = loadPositions(this.userId, this.positionsScope);
    }
    this.dropHiddenSelection();
    if (changed.has('graph') || changed.has('codeActivity')) {
      const next = this.filtered().nodes;
      const previous = this.shownNodes;
      this.shownNodes = next;
      if (this.spatial && !this.motionOff) {
        this.presence = nextPresence(this.presence, previous, next, Date.now());
        if (this.presence.entered.size || this.presence.leaving.size)
          this.startFrames();
      }
    }
    if (
      this.live &&
      (changed.has('graph') ||
        changed.has('activityWindow') ||
        changed.has('live'))
    )
      this.scheduleCodeLoad(changed.has('activityWindow') ? 0 : 400);
  }

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('fullscreen')) {
      document.body.style.overflow = this.fullscreen ? 'hidden' : '';
      if (this.fullscreen) window.addEventListener('keydown', this.onWindowKey);
      else window.removeEventListener('keydown', this.onWindowKey);
      this.refit = true;
    }
    // A background refresh replaces the graph; only the first graph or a new
    // window re-fits, so polling never resets the user's zoom and pan.
    // A window change is fitted when the graph for that window arrives, not
    // against the old graph still on screen.
    if (changed.has('activityWindow') && changed.get('activityWindow'))
      this.windowFit = true;
    if (changed.has('graph') && (this.windowFit || !changed.get('graph'))) {
      this.windowFit = false;
      this.refit = true;
    }
    if (changed.has('activityWindow') || changed.has('live'))
      this.scheduleRefresh();
    if (changed.has('live') || changed.has('instanceAdmin'))
      this.syncCodeSubscription();
    if (this.refit && this.lastLayout) {
      this.refit = false;
      this.recenter = false;
      this.fit();
    } else if (this.recenter && this.spatial) {
      this.recenter = false;
      this.follow();
    } else if (!this.camera) this.applyTransform();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribes.forEach((unsubscribe) => unsubscribe());
    this.unsubscribes = [];
    this.codeUnsubscribe?.();
    this.codeUnsubscribe = undefined;
    this.activity.stop();
    this.lights.clear();
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.codeController?.abort();
    if (this.codeTimer !== undefined) clearTimeout(this.codeTimer);
    this.codeTimer = undefined;
    if (this.refreshTimer !== undefined) clearInterval(this.refreshTimer);
    if (this.staleTimer !== undefined) clearTimeout(this.staleTimer);
    this.refreshTimer = this.staleTimer = undefined;
    window.removeEventListener('keydown', this.onWindowKey);
    if (this.fullscreen) document.body.style.overflow = '';
  }

  // The Agents canvas uses the same transformed Lit nodes, SVG links and
  // pointer-centred zoom. Keep the map keyboard-scrollable via its controls.
  private zoom(factor: number, clientX?: number, clientY?: number): void {
    const bounds = this.shadowRoot
      ?.querySelector('.stage')
      ?.getBoundingClientRect();
    if (!bounds) return;
    this.userMovedAt = Date.now();
    this.camera = undefined;
    const x = clientX === undefined ? bounds.width / 2 : clientX - bounds.left;
    const y = clientY === undefined ? bounds.height / 2 : clientY - bounds.top;
    const next = Math.max(0.2, Math.min(2, this.scale * factor));
    this.translateX = x - ((x - this.translateX) * next) / this.scale;
    this.translateY = y - ((y - this.translateY) * next) / this.scale;
    this.scale = next;
    this.applyTransform();
  }

  /**
   * Fit the width at a legible size and pin the top: column headers sit at
   * the top of the stage and long columns scroll, instead of shrinking the
   * whole fleet into unreadable text.
   */
  private fit(): void {
    const bounds = this.shadowRoot
      ?.querySelector('.stage')
      ?.getBoundingClientRect();
    const layout = this.lastLayout;
    if (!bounds || !layout?.width) return;
    // The selection panel covers the right of a wide stage.
    const panel =
      this.selected && !window.matchMedia('(max-width: 900px)').matches
        ? 364
        : 0;
    const room = bounds.width - panel;
    if (this.spatial) {
      // A spatial map has no column headers to pin: centre it on its centre
      // of gravity, with every node in view.
      const view = this.gravityView(layout);
      if (!view) return;
      this.camera = undefined;
      this.translateX = view.x;
      this.translateY = view.y;
      this.scale = view.s;
      this.applyTransform();
      return;
    }
    this.scale = Math.max(
      MIN_FIT_SCALE,
      Math.min(1, (room - 48) / layout.width)
    );
    const width = layout.width * this.scale;
    this.translateX = width < room ? (room - width) / 2 : 24;
    this.translateY = 16;
    this.applyTransform();
  }

  private tween?: {
    from: Map<string, FleetMapPosition>;
    to: FleetMapLayout;
    start: number;
  };
  private camera?: { from: View; to: View; start: number };
  private shownLayout?: FleetMapLayout;
  private recenter = false;
  private userMovedAt = -Infinity;
  private glidePoints?: FleetMapPoints;

  /**
   * The view that puts the nodes' centre of gravity (the mean of their
   * centres) in the middle of the stage, at the largest scale up to 100%
   * that keeps every node in view around it.
   */
  private gravityView(layout: FleetMapLayout): View | null {
    const bounds = this.shadowRoot
      ?.querySelector('.stage')
      ?.getBoundingClientRect();
    const boxes = [...layout.positions.values()];
    if (!bounds?.width || !boxes.length) return null;
    const panel =
      this.selected && !window.matchMedia('(max-width: 900px)').matches
        ? 364
        : 0;
    const room = bounds.width - panel;
    const cx = boxes.reduce((sum, b) => sum + b.x + b.w / 2, 0) / boxes.length;
    const cy = boxes.reduce((sum, b) => sum + b.y + b.h / 2, 0) / boxes.length;
    let dx = 1;
    let dy = 1;
    for (const b of boxes) {
      dx = Math.max(dx, Math.abs(b.x - cx), Math.abs(b.x + b.w - cx));
      dy = Math.max(dy, Math.abs(b.y - cy), Math.abs(b.y + b.h - cy));
    }
    const s = Math.max(
      MIN_SPATIAL_FIT_SCALE,
      Math.min(1, (room / 2 - 24) / dx, (bounds.height / 2 - 36) / dy)
    );
    return { x: room / 2 - cx * s, y: bounds.height / 2 - cy * s, s };
  }

  /** Glide the view to the fleet's new centre of gravity. */
  private follow(): void {
    const layout = this.lastLayout;
    if (!layout || Date.now() - this.userMovedAt < HANDS_OFF_MS) return;
    const to = this.gravityView(layout);
    if (!to) return;
    if (this.motionOff) {
      this.translateX = to.x;
      this.translateY = to.y;
      this.scale = to.s;
      this.applyTransform();
      return;
    }
    this.camera = {
      from: { x: this.translateX, y: this.translateY, s: this.scale },
      to,
      start: Date.now(),
    };
    this.startFrames();
  }

  /** Advance the view glide; true while it is still moving. */
  private stepCamera(now: number): boolean {
    const camera = this.camera;
    if (!camera) return false;
    const t = Math.min(1, (now - camera.start) / GLIDE_MS);
    const e = glide(t);
    this.translateX = camera.from.x + (camera.to.x - camera.from.x) * e;
    this.translateY = camera.from.y + (camera.to.y - camera.from.y) * e;
    this.scale = camera.from.s + (camera.to.s - camera.from.s) * e;
    this.applyTransform();
    if (t >= 1) this.camera = undefined;
    return !!this.camera;
  }

  /**
   * The positions drawn this frame: when the arrangement changes (people,
   * machines or agents joined or left), nodes glide from where they were.
   */
  private glideLayout(target: FleetMapLayout): FleetMapLayout {
    const now = Date.now();
    if (target !== this.shownLayout) {
      const previous = this.shownLayout;
      this.shownLayout = target;
      // A node dragged by hand follows the pointer; nothing glides.
      const dragged = this.points !== this.glidePoints;
      this.glidePoints = this.points;
      if (dragged) this.tween = undefined;
      else if (previous) {
        this.recenter = true;
        if (!this.motionOff)
          this.tween = {
            from: this.tween
              ? this.glidePositions(this.tween, now)
              : previous.positions,
            to: target,
            start: now,
          };
      }
    }
    const tween = this.tween;
    if (!tween || tween.to !== target || now - tween.start >= GLIDE_MS)
      return target;
    this.startFrames();
    return { ...target, positions: this.glidePositions(tween, now) };
  }

  private glidePositions(
    tween: NonNullable<FleetMap['tween']>,
    now: number
  ): Map<string, FleetMapPosition> {
    const e = glide(Math.min(1, (now - tween.start) / GLIDE_MS));
    const out = new Map<string, FleetMapPosition>();
    for (const [id, to] of tween.to.positions) {
      const from = tween.from.get(id);
      out.set(
        id,
        from
          ? {
              ...to,
              x: Math.round(from.x + (to.x - from.x) * e),
              y: Math.round(from.y + (to.y - from.y) * e),
            }
          : to
      );
    }
    return out;
  }

  private pointerDown(event: PointerEvent): void {
    if (event.button !== 0) return;
    this.suppressClick = false;
    const target = (event.target as Element).closest?.('[data-node]');
    if (this.spatial && target && this.lastLayout) {
      const id = target.getAttribute('data-node') || '';
      const position = this.lastLayout.positions.get(id);
      if (!position) return;
      this.drag = {
        id: event.pointerId,
        node: id,
        x: event.clientX,
        y: event.clientY,
        origin: { x: position.x, y: position.y },
        moved: false,
      };
      // Pin the picture as it is, so only the dragged node moves.
      const all: FleetMapPoints = {};
      for (const [key, p] of this.lastLayout.positions)
        all[key] = { x: p.x, y: p.y };
      this.points = { ...all, ...this.points };
      capture(event, true);
      return;
    }
    if ((event.target as Element).closest('button')) return;
    this.userMovedAt = Date.now();
    this.camera = undefined;
    this.pan = {
      id: event.pointerId,
      x: event.clientX - this.translateX,
      y: event.clientY - this.translateY,
    };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  private pointerMove(event: PointerEvent): void {
    const drag = this.drag;
    if (drag?.id === event.pointerId) {
      const dx = (event.clientX - drag.x) / this.scale;
      const dy = (event.clientY - drag.y) / this.scale;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      this.points = {
        ...this.points,
        [drag.node]: {
          x: Math.round(drag.origin.x + dx),
          y: Math.round(drag.origin.y + dy),
        },
      };
      return;
    }
    if (this.pan?.id !== event.pointerId) return;
    this.translateX = event.clientX - this.pan.x;
    this.translateY = event.clientY - this.pan.y;
    this.applyTransform();
  }

  private pointerUp(event: PointerEvent): void {
    const drag = this.drag;
    if (drag?.id === event.pointerId) {
      this.drag = null;
      capture(event, false);
      if (!drag.moved) return;
      savePositions(this.userId, this.points, this.positionsScope);
      // Swallow only the click the drop itself produces.
      this.suppressClick = true;
      setTimeout(() => (this.suppressClick = false), 0);
      return;
    }
    if (this.pan?.id !== event.pointerId) return;
    this.pan = null;
    (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
  }

  /** Scroll pans, as on a page; pinch or Ctrl/Cmd + scroll zooms. */
  private onWheel(event: WheelEvent): void {
    event.preventDefault();
    this.userMovedAt = Date.now();
    this.camera = undefined;
    if (event.ctrlKey || event.metaKey) {
      this.zoom(Math.exp(-event.deltaY * 0.01), event.clientX, event.clientY);
      return;
    }
    const dx = event.shiftKey ? event.deltaY : event.deltaX;
    const dy = event.shiftKey ? 0 : event.deltaY;
    this.translateX -= dx;
    this.translateY -= dy;
    this.applyTransform();
  }

  private toggleFullscreen(): void {
    this.fullscreen = !this.fullscreen;
    this.dispatchEvent(
      new CustomEvent('fleet-map-fullscreen', {
        detail: { fullscreen: this.fullscreen },
        bubbles: true,
        composed: true,
      })
    );
  }

  /** Esc anywhere first clears a selection, then leaves full screen. */
  private onWindowKey = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (this.hasSelection()) this.clearSelection();
    else this.toggleFullscreen();
    event.preventDefault();
  };

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
      }
      :host([fullscreen]) .stage {
        flex: 1;
        min-height: 0;
        height: auto;
      }
      .toolbar {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        align-items: center;
        margin-bottom: var(--sl-spacing-small);
      }
      .toolbar label {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .toolbar select,
      .search input {
        font: inherit;
        font-size: var(--console-text-body);
        color: var(--console-body-color);
        background: var(--console-button-face);
        border: 1px solid var(--console-button-border);
        border-radius: var(--sl-border-radius-medium);
        padding: 4px 8px;
        min-height: 32px;
        box-sizing: border-box;
      }
      .search {
        position: relative;
        display: inline-flex;
        align-items: center;
      }
      .search sl-icon {
        position: absolute;
        left: 9px;
        color: var(--console-meta-color);
        pointer-events: none;
      }
      .search input {
        width: 240px;
        padding-left: 30px;
      }
      .search .hits {
        margin-left: 8px;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        white-space: nowrap;
      }
      .seg {
        display: inline-flex;
        border: 0;
        margin: 0;
        padding: 0;
        gap: 4px;
      }
      .pill {
        font: inherit;
        font-size: var(--console-text-meta);
        font-weight: 500;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border: 1px solid var(--console-hairline);
        background: transparent;
        color: var(--console-meta-color);
        border-radius: 999px;
        padding: 3px 12px;
        min-height: 28px;
        cursor: pointer;
      }
      .pill .n {
        font-variant-numeric: tabular-nums;
        opacity: 0.8;
      }
      .pill[aria-pressed='true'] {
        color: var(--console-body-color);
        border-color: var(--sl-color-neutral-400);
        background: var(--console-hover-tint);
      }
      .pill:hover {
        background: var(--console-hover-tint);
      }
      .pill:disabled {
        cursor: default;
        opacity: 0.5;
      }
      .group-label {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .count {
        margin-left: auto;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
      }
      .reset {
        font: inherit;
        font-size: var(--console-text-meta);
        background: none;
        border: 0;
        padding: 0;
        color: var(--console-link-color);
        cursor: pointer;
      }
      .gaps {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px 8px;
        margin-bottom: var(--sl-spacing-small);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .gaps .title {
        font-weight: 600;
        color: var(--console-body-color);
        margin-right: 4px;
      }
      .gaps .title sl-icon {
        color: var(--sl-color-warning-600);
        vertical-align: -2px;
        margin-right: 4px;
      }
      .gap-chip {
        color: var(--console-body-color);
        border-color: color-mix(
          in srgb,
          var(--sl-color-warning-500) 40%,
          transparent
        );
      }
      .gap-chip strong {
        font-variant-numeric: tabular-nums;
        color: var(--sl-color-warning-800);
      }
      .gap-chip[aria-pressed='true'] {
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 16%,
          transparent
        );
        border-color: var(--sl-color-warning-600);
      }
      .stage {
        position: relative;
        overflow: hidden;
        touch-action: none;
        cursor: grab;
        height: var(--fleet-map-stage-height, max(520px, calc(100vh - 300px)));
        background: var(--console-page);
        border: 1px solid var(--console-hairline);
        border-radius: var(--console-card-radius);
        background-image: radial-gradient(
          color-mix(in srgb, var(--sl-color-neutral-500) 22%, transparent) 1px,
          transparent 1px
        );
        background-size: 24px 24px;
      }
      .stage:active {
        cursor: grabbing;
      }
      .stage:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }
      .plane {
        position: relative;
        transform-origin: 0 0;
      }
      .plane svg {
        position: absolute;
        inset: 0;
        overflow: visible;
        pointer-events: none;
      }
      .edge {
        fill: none;
        stroke: color-mix(
          in srgb,
          var(--sl-color-neutral-500) 60%,
          transparent
        );
        stroke-width: 1.25;
        transition:
          opacity 150ms,
          stroke 150ms;
      }
      .edge.gap {
        stroke: var(--sl-color-warning-600);
      }
      .edge.observed {
        stroke-dasharray: 4 4;
      }
      .edge.lit {
        stroke: var(--sl-color-primary-600);
        stroke-width: 2;
      }
      .edge.dim {
        opacity: 0.08;
      }
      /* Live traffic: success means live (DESIGN.md, Colour). A state
         change, held a few seconds, never an ambient animation. */
      .edge.active {
        stroke: var(--sl-color-success-600);
        stroke-width: 2.5;
        stroke-dasharray: none;
        opacity: 1;
      }
      .activity-label {
        font-size: 11px;
        font-weight: 500;
        fill: var(--sl-color-success-800);
        paint-order: stroke;
        stroke: var(--console-page);
        stroke-width: 3px;
        pointer-events: none;
      }
      .column-title {
        position: absolute;
        top: 0;
        display: flex;
        align-items: baseline;
        gap: 6px;
        font-size: var(--console-text-eyebrow);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--console-meta-color);
      }
      .column-title .n {
        font-weight: 500;
        font-variant-numeric: tabular-nums;
      }
      .column-title .reset {
        text-transform: none;
        letter-spacing: 0;
        font-weight: 500;
        margin-left: auto;
      }
      .box {
        position: absolute;
        box-sizing: border-box;
        border-radius: var(--sl-border-radius-large);
        border: 1px solid var(--console-hairline);
        background: var(--console-surface);
        transition: opacity 150ms;
        z-index: 0;
      }
      .box.dim {
        opacity: 0.35;
      }
      .node,
      .fold,
      .more {
        z-index: 1;
      }
      .node {
        position: absolute;
        box-sizing: border-box;
        display: grid;
        grid-template-columns: 28px minmax(0, 1fr) auto;
        align-items: center;
        gap: 8px;
        padding: 6px 10px 6px 8px;
        border-radius: var(--sl-border-radius-large);
        border: 1px solid var(--console-hairline);
        background: var(--console-surface);
        cursor: pointer;
        text-align: left;
        font: inherit;
        color: inherit;
        transition:
          opacity 150ms,
          border-color 150ms;
      }
      .node.header {
        border-color: transparent;
        background: transparent;
        padding-right: 36px;
      }
      .node.child {
        grid-template-columns: 20px minmax(0, 1fr) auto;
        padding: 4px 8px 4px 6px;
        border-radius: var(--sl-border-radius-medium);
      }
      .node:hover {
        border-color: var(--sl-color-neutral-400);
      }
      .node:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }
      .node.has-gap {
        border-color: color-mix(
          in srgb,
          var(--sl-color-warning-500) 45%,
          transparent
        );
      }
      .node.match {
        border-color: var(--sl-color-primary-600);
      }
      .node.selected {
        border-color: var(--sl-color-primary-600);
        box-shadow: 0 0 0 1px var(--sl-color-primary-600);
      }
      .node.focus {
        border-color: var(--sl-color-warning-600);
        box-shadow: 0 0 0 1px var(--sl-color-warning-600);
      }
      .node.dim {
        opacity: 0.28;
      }
      .node.cluster {
        border-style: dashed;
      }
      .node.inactive .label {
        color: var(--console-meta-color);
      }
      .node .glyph {
        width: 28px;
        height: 28px;
        border-radius: 50%;
        display: grid;
        place-items: center;
        background: color-mix(
          in srgb,
          var(--sl-color-neutral-500) 14%,
          transparent
        );
        color: var(--console-body-color);
        font-size: 14px;
      }
      .node.child .glyph {
        width: 20px;
        height: 20px;
        font-size: 12px;
        background: none;
        color: var(--console-meta-color);
      }
      .node.selected .glyph,
      .node.lit .glyph {
        background: color-mix(
          in srgb,
          var(--sl-color-primary-500) 16%,
          transparent
        );
        color: var(--sl-color-primary-700);
      }
      .node .text {
        display: flex;
        flex-direction: column;
        min-width: 0;
        line-height: 1.3;
      }
      .node .label {
        font-size: var(--console-text-body);
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .node.child .label {
        font-size: var(--console-text-meta);
        font-weight: 500;
      }
      .node .sub {
        font-size: 12px;
        color: var(--console-meta-color);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .node .sub.warn {
        color: var(--sl-color-warning-800);
      }
      .gap-dot {
        display: grid;
        place-items: center;
        font-size: 13px;
        color: var(--sl-color-warning-600);
      }
      .fold {
        position: absolute;
        width: 28px;
        height: 28px;
        display: grid;
        place-items: center;
        border: 0;
        border-radius: var(--sl-border-radius-medium);
        background: none;
        color: var(--console-meta-color);
        cursor: pointer;
      }
      .fold:hover {
        background: var(--console-hover-tint);
      }
      .more {
        position: absolute;
        box-sizing: border-box;
        font: inherit;
        font-size: var(--console-text-meta);
        font-weight: 500;
        color: var(--console-link-color);
        background: transparent;
        border: 1px dashed var(--console-hairline);
        border-radius: var(--sl-border-radius-large);
        cursor: pointer;
      }
      .more:hover {
        background: var(--console-hover-tint);
      }
      .overlay {
        position: absolute;
        display: flex;
        gap: 4px;
        align-items: center;
        z-index: 2;
      }
      .canvas-tools {
        bottom: 10px;
        right: 12px;
      }
      .stage.has-selection .canvas-tools {
        right: 364px;
      }
      .canvas-tools button {
        font: inherit;
        font-size: var(--console-text-meta);
        color: var(--console-body-color);
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-medium);
        min-width: 32px;
        min-height: 32px;
        padding: 0 10px;
        cursor: pointer;
        display: inline-grid;
        place-items: center;
      }
      .canvas-tools .zoom {
        min-width: 44px;
        text-align: center;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
      }
      .legend {
        left: 12px;
        bottom: 10px;
        gap: var(--sl-spacing-medium);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        pointer-events: none;
        background: color-mix(in srgb, var(--console-page) 85%, transparent);
        padding: 2px 6px;
        border-radius: var(--sl-border-radius-medium);
      }
      .legend span {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .legend svg {
        width: 24px;
        height: 8px;
      }
      .panel {
        position: absolute;
        top: 12px;
        right: 12px;
        bottom: 12px;
        width: 340px;
        z-index: 3;
        box-sizing: border-box;
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        box-shadow: var(--sl-shadow-large);
        border-radius: var(--console-card-radius);
        padding: var(--sl-spacing-medium) var(--sl-spacing-large);
        overflow: auto;
        cursor: auto;
      }
      .panel h3 {
        margin: 2px 0 0;
        font-size: var(--console-text-card-title);
        padding-right: 24px;
      }
      .panel .eyebrow {
        font-size: var(--console-text-eyebrow);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--console-meta-color);
      }
      .panel h4 {
        margin: var(--sl-spacing-large) 0 2px;
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }
      .panel .group {
        margin: var(--sl-spacing-small) 0 0;
        font-size: var(--console-text-eyebrow);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--console-meta-color);
      }
      .panel ul {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .panel li {
        padding: 6px 0;
        border-top: 1px solid var(--console-hairline);
        font-size: var(--console-text-body);
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 4px 8px;
      }
      .panel li:first-child {
        border-top: 0;
      }
      .panel li .act {
        font-size: var(--console-text-meta);
      }
      .panel a {
        color: var(--console-link-color);
      }
      .panel .meta {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        margin: 4px 0 0;
      }
      .panel .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: var(--sl-spacing-small);
      }
      .panel .actions {
        display: flex;
        flex-wrap: wrap;
        gap: 4px 12px;
        margin-top: var(--sl-spacing-small);
        font-size: var(--console-text-body);
      }
      .panel .reach {
        font-size: var(--console-text-body);
        margin: 2px 0 0;
      }
      .panel .pill {
        margin-top: var(--sl-spacing-small);
      }
      .close {
        position: absolute;
        top: 8px;
        right: 8px;
        background: none;
        border: 0;
        cursor: pointer;
        color: var(--console-meta-color);
        font-size: 18px;
        width: 32px;
        height: 32px;
      }
      .notice {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        margin: 0 0 var(--sl-spacing-small);
      }
      .empty-state {
        min-height: 72px;
        height: 100%;
        display: grid;
        place-items: center;
        text-align: center;
        color: var(--console-meta-color);
      }
      @media (max-width: 900px) {
        .stage.has-selection .canvas-tools {
          right: 12px;
          top: 12px;
          bottom: auto;
        }
        .panel {
          left: 12px;
          width: auto;
          top: auto;
          max-height: 60%;
        }
        .legend {
          display: none;
        }
        .search input {
          width: 180px;
        }
      }
      /* Code activity: counters on a node and events rising off it. */
      .code-badges {
        position: absolute;
        top: -10px;
        right: 8px;
        display: flex;
        gap: 3px;
        pointer-events: none;
      }
      .code-badge {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        height: 18px;
        padding: 0 6px;
        border-radius: 9px;
        font-size: 11px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        line-height: 1;
        color: var(--console-body-color);
        background: var(--console-surface);
        border: 1px solid var(--console-hairline);
      }
      .code-badge sl-icon {
        font-size: 11px;
      }
      .code-badge.opened sl-icon,
      .pip.opened,
      .toast.opened sl-icon {
        color: var(--sl-color-primary-600);
      }
      .code-badge.reviewed sl-icon,
      .code-badge.commented sl-icon,
      .pip.reviewed,
      .pip.commented,
      .toast.reviewed sl-icon,
      .toast.commented sl-icon {
        color: var(--console-meta-color);
      }
      .code-badge.merged sl-icon,
      .pip.merged,
      .toast.merged sl-icon {
        color: var(--sl-color-success-600);
      }
      .pip {
        position: absolute;
        z-index: 3;
        white-space: nowrap;
        pointer-events: none;
        font-size: 11px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--console-surface);
        border: 1px solid currentColor;
      }
      .node.ghost {
        pointer-events: none;
        cursor: default;
      }
      /* Where live traffic starts: the node lights, a state change held
         about a second and a half, never a movement. */
      .node.speaking {
        border-color: var(--sl-color-success-600);
        box-shadow: 0 0 0 1px var(--sl-color-success-600);
      }
      .node.speaking .glyph {
        background: color-mix(
          in srgb,
          var(--sl-color-success-500) 22%,
          transparent
        );
        color: var(--sl-color-success-600);
      }
      .node.speaking.speak-response {
        border-color: var(--sl-color-primary-600);
        box-shadow: 0 0 0 1px var(--sl-color-primary-600);
      }
      .node.speaking.speak-response .glyph {
        background: color-mix(
          in srgb,
          var(--sl-color-primary-500) 22%,
          transparent
        );
        color: var(--sl-color-primary-600);
      }
      .node.speaking.speak-approval {
        border-color: var(--sl-color-warning-600);
        box-shadow: 0 0 0 1px var(--sl-color-warning-600);
      }
      .node.speaking.speak-approval .glyph {
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 22%,
          transparent
        );
        color: var(--sl-color-warning-600);
      }
      .node.speaking.speak-tool {
        border-color: var(--sl-color-neutral-400);
        box-shadow: none;
      }
      .node.speaking.speak-tool .glyph {
        background: color-mix(
          in srgb,
          var(--sl-color-neutral-500) 22%,
          transparent
        );
        color: var(--console-body-color);
      }
      /* Above the zoom controls, clear of the column headers. */
      .toasts {
        bottom: 52px;
        right: 12px;
        flex-direction: column;
        align-items: flex-end;
        gap: 6px;
        pointer-events: none;
      }
      .stage.has-selection .toasts {
        right: 364px;
      }
      .toast {
        display: flex;
        align-items: center;
        gap: 6px;
        max-width: 320px;
        font-size: var(--console-text-meta);
        color: var(--console-body-color);
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-medium);
        padding: 4px 10px;
      }
      @media (prefers-reduced-motion: reduce) {
        .edge,
        .activity-label,
        .node,
        .box {
          transition: none;
        }
      }
    `,
  ];

  /** Filters cannot leave a hidden selection or gap focus dimming the canvas. */
  private dropHiddenSelection(): void {
    const visible = this.filtered().nodes;
    if (this.selected && !visible.some((node) => node.id === this.selected)) {
      this.selected = '';
      this.focusOnly = false;
    }
    if (
      this.focusGap &&
      !visible.some((node) =>
        node.gaps.includes(this.focusGap as FleetMapGapCode)
      )
    )
      this.focusGap = '';
  }

  private filterCache?: { key: unknown[]; view: FleetMapGraph };

  private filtered(): FleetMapGraph {
    const key = [
      this.graph,
      this.codeActivity?.flows,
      this.activityWindow,
      this.types,
      this.owner,
      this.provider,
      this.states,
      this.gapsOnly,
    ];
    const cached = this.filterCache;
    if (cached && cached.key.every((value, index) => value === key[index]))
      return cached.view;
    const minutes = windowMinutes(this.activityWindow);
    // A server that applied the window says so; otherwise apply it here.
    const source =
      this.graph && minutes && this.graph.active_within_minutes === undefined
        ? activeGraph(this.graph, Date.now() - minutes * 60_000)
        : (this.graph ?? EMPTY);
    const view = filterGraph(withCodeFlows(source, this.codeActivity), {
      types: this.types,
      owner: this.owner,
      provider: this.provider,
      states: this.states,
      gapsOnly: this.gapsOnly,
    });
    this.filterCache = { key, view };
    return view;
  }

  private searchHits(view: FleetMapGraph): Set<string> {
    const query = this.query.trim().toLowerCase();
    if (!query) return new Set();
    return new Set(
      view.nodes
        .filter(
          (node) =>
            node.type !== 'cluster' &&
            `${displayLabel(node)} ${node.label} ${node.detail ?? ''}`
              .toLowerCase()
              .includes(query)
        )
        .map((node) => node.id)
    );
  }

  private select(id: string): void {
    this.selected = this.selected === id ? '' : id;
    if (!this.selected) {
      this.focusOnly = false;
      return;
    }
    this.focusGap = '';
    void this.updateComplete.then(() => this.reveal(id));
  }

  /** The panel covers the right of the stage; keep the selection in view. */
  private reveal(id: string): void {
    const stage = this.shadowRoot?.querySelector('.stage');
    const anchor = this.lastLayout?.anchor(id);
    const position = anchor && this.lastLayout?.positions.get(anchor);
    if (!stage || !position) return;
    const narrow = window.matchMedia('(max-width: 900px)').matches;
    const bounds = stage.getBoundingClientRect();
    const right = this.translateX + (position.x + position.w) * this.scale;
    const limit = bounds.width - (narrow ? 24 : 376);
    if (right > limit) this.translateX -= right - limit;
    const left = this.translateX + position.x * this.scale;
    if (left < 24) this.translateX += 24 - left;
    const top = this.translateY + position.y * this.scale;
    const bottom = narrow ? bounds.height * 0.4 : bounds.height - 48;
    if (narrow || top < 24 || top > bottom)
      this.translateY = 64 - position.y * this.scale;
    this.applyTransform();
  }

  private toggle<T>(set: Set<T>, value: T): Set<T> {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  }

  private onKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape') {
      if (this.hasSelection()) {
        this.clearSelection();
        event.preventDefault();
      }
      return;
    }
    if ((event.target as Element)?.closest?.('input, select')) return;
    if (event.key === '+' || event.key === '=') this.zoom(1.25);
    if (event.key === '-') this.zoom(0.8);
    const step = 48;
    if (event.key === 'ArrowLeft') this.translateX += step;
    if (event.key === 'ArrowRight') this.translateX -= step;
    if (event.key === 'ArrowUp') this.translateY += step;
    if (event.key === 'ArrowDown') this.translateY -= step;
    if (event.key.startsWith('Arrow')) {
      event.preventDefault();
      this.applyTransform();
    }
  };

  private renderWindowSelect() {
    return html`<label
      >Active
      <select
        data-testid="fleet-map-window"
        aria-label="Show resources active in"
        .value=${this.activityWindow}
        @change=${(e: Event) =>
          this.setWindow(
            (e.target as HTMLSelectElement).value as FleetMapWindow
          )}
      >
        ${FLEET_MAP_WINDOWS.map(
          (item) =>
            html`<option
              value=${item.value}
              ?selected=${item.value === this.activityWindow}
            >
              ${item.label}
            </option>`
        )}
      </select></label
    >`;
  }

  private renderToolbar(
    graph: FleetMapGraph,
    view: FleetMapGraph,
    hits: Set<string>
  ) {
    const total = graph.nodes.filter((node) => node.type !== 'cluster').length;
    const shown = view.nodes.filter((node) => node.type !== 'cluster').length;
    const groups = graph.nodes.filter((node) => node.type === 'cluster').length;
    const people = graph.nodes.filter((node) => node.type === 'user');
    const providers = graph.nodes.filter((node) => node.type === 'provider');
    const agents = graph.nodes.filter((node) => node.type === 'agent');
    const perState = new Map<FleetMapAgentState, number>();
    for (const node of agents)
      perState.set(agentState(node), (perState.get(agentState(node)) || 0) + 1);
    const changed =
      this.owner ||
      this.provider ||
      this.gapsOnly ||
      this.query ||
      this.types.size < FLEET_MAP_TYPES.length ||
      this.states.size !== 1 ||
      !this.states.has('onboarded');
    const retired = graph.decommissioned_agents ?? 0;
    return html`<div class="toolbar">
        ${
          this.instanceAdmin && !this.spatialOnly
            ? html`<fieldset
                class="seg"
                aria-label="Arrangement"
                data-testid="fleet-map-arrangement"
              >
                ${(
                  [
                    ['columns', 'Columns'],
                    ['spatial', 'Spatial'],
                  ] as const
                ).map(
                  ([value, label]) =>
                    html`<button
                      type="button"
                      class="pill"
                      data-arrangement=${value}
                      aria-pressed=${this.arrangement === value ? 'true' : 'false'}
                      @click=${() => this.setArrangement(value)}
                    >
                      ${label}
                    </button>`
                )}
              </fieldset>`
            : nothing
        }
        <span class="search">
          <sl-icon name="search" aria-hidden="true"></sl-icon>
          <input
            type="search"
            aria-label="Find in map"
            placeholder="Find a person, device, agent, model"
            .value=${this.query}
            @input=${(e: Event) =>
              (this.query = (e.target as HTMLInputElement).value)}
            @keydown=${(e: KeyboardEvent) => {
              if (e.key === 'Enter' && hits.size) this.select([...hits][0]);
              if (e.key === 'Escape') this.query = '';
            }}
          />
          ${
            this.query
              ? html`<span class="hits" aria-live="polite"
                  >${hits.size} match${hits.size === 1 ? '' : 'es'}</span
                >`
              : nothing
          }
        </span>
        <fieldset class="seg" aria-label="Agents">
          ${STATES.map(
            ({ state, label }) =>
              html`<button
                type="button"
                class="pill"
                data-state=${state}
                aria-pressed=${this.states.has(state) ? 'true' : 'false'}
                @click=${() => (this.states = this.toggle(this.states, state))}
              >
                ${label}<span class="n">${perState.get(state) ?? 0}</span>
              </button>`
          )}
        </fieldset>
        <button
          type="button"
          class="pill"
          data-testid="gaps-only"
          aria-pressed=${this.gapsOnly ? 'true' : 'false'}
          @click=${() => (this.gapsOnly = !this.gapsOnly)}
        >
          Gaps only
        </button>
        <label
          >Owner
          <select
            .value=${this.owner}
            @change=${(e: Event) =>
              (this.owner = (e.target as HTMLSelectElement).value)}
          >
            <option value="">Anyone</option>
            ${people.map(
              (node) => html`<option value=${node.id}>${node.label}</option>`
            )}
          </select></label
        >
        <label
          >Provider
          <select
            .value=${this.provider}
            @change=${(e: Event) =>
              (this.provider = (e.target as HTMLSelectElement).value)}
          >
            <option value="">Any provider</option>
            ${providers.map(
              (node) => html`<option value=${node.id}>${node.label}</option>`
            )}
          </select></label
        >
      </div>
      <div class="toolbar">
        <span class="group-label">Show</span>
        <fieldset class="seg" aria-label="Show">
          ${FLEET_MAP_TYPES.map(
            (type) =>
              html`<button
                type="button"
                class="pill"
                aria-pressed=${this.types.has(type) ? 'true' : 'false'}
                @click=${() => (this.types = this.toggle(this.types, type))}
              >
                ${TYPE_TOGGLE_LABEL[type]}
              </button>`
          )}
        </fieldset>
        ${this.renderWindowSelect()}
        <span class="count" aria-live="polite"
          >${shown === total ? total : `${shown} of ${total}`}
          node${total === 1 ? '' : 's'}${groups ? ` · ${groups} summary group${groups === 1 ? '' : 's'}` : ''}${
            retired
              ? ` · ${retired} decommissioned agent${retired === 1 ? '' : 's'} not shown`
              : ''
          }</span
        >
        ${
          changed
            ? html`<button
                type="button"
                class="reset"
                @click=${() => {
                  this.owner = '';
                  this.provider = '';
                  this.gapsOnly = false;
                  this.query = '';
                  this.types = new Set(FLEET_MAP_TYPES);
                  this.states = new Set(['onboarded']);
                }}
              >
                Reset filters
              </button>`
            : nothing
        }
      </div>`;
  }

  /**
   * One line naming every kind of gap in the shown map with its count.
   * A chip lights the nodes that carry that gap, so "1 runner offline" is
   * a way to find it, not a number to take on trust.
   */
  private renderGaps(graph: FleetMapGraph) {
    const counts = new Map<FleetMapGapCode, { label: string; n: number }>();
    for (const node of graph.nodes)
      for (const gap of nodeGaps(node)) {
        const entry = counts.get(gap.code);
        if (entry) entry.n += 1;
        else counts.set(gap.code, { label: gap.label, n: 1 });
      }
    if (!counts.size)
      return html`<div class="gaps" data-testid="fleet-map-gaps">
        <span>No governance gaps in this view.</span>
      </div>`;
    return html`<div
      class="gaps"
      role="group"
      aria-label="Governance gaps"
      data-testid="fleet-map-gaps"
    >
      <span class="title"
        ><sl-icon name="exclamation-triangle-fill"></sl-icon>Gaps</span
      >
      ${[...counts].map(
        ([code, { label, n }]) =>
          html`<button
            type="button"
            class="pill gap-chip"
            data-gap=${code}
            aria-pressed=${this.focusGap === code ? 'true' : 'false'}
            @click=${() => {
              this.focusGap = this.focusGap === code ? '' : code;
              if (this.focusGap) this.selected = '';
            }}
          >
            <strong>${n}</strong>${GAP_CHIP_LABEL[code] ?? label}
          </button>`
      )}
    </div>`;
  }

  /** The second line of a node: its gap, else what it is in context. */
  private subtitle(
    node: FleetMapNode,
    view: FleetMapGraph,
    groups: FleetMapLayout['groups'],
    byId: Map<string, FleetMapNode>
  ): string {
    const gaps = nodeGaps(node);
    if (node.type === 'cluster') return node.detail || 'Outside this map';
    if (gaps.length) {
      const text = gaps
        .map((gap) => GAP_CHIP_LABEL[gap.code] ?? gap.label)
        .join(' · ');
      return text.charAt(0).toUpperCase() + text.slice(1);
    }
    if (node.type === 'agent') {
      const state = agentState(node);
      const seen = node.last_seen_at
        ? `seen ${formatRelativeTime(node.last_seen_at, new Date(), { maxRelativeDays: 365 })}`
        : '';
      if (state === 'inactive')
        return ['Inactive', seen].filter(Boolean).join(' · ');
      return node.detail ? agentKindLabel(node.detail) : 'Agent';
    }
    if (node.type === 'model') {
      const agents = new Set(
        view.edges
          .filter(
            (edge) => edge.kind === 'uses_model' && edge.target === node.id
          )
          .map((edge) => edge.source)
      ).size;
      const providers = node.provider_ids
        .map((id) => byId.get(id)?.label)
        .filter(Boolean)
        .join(', ');
      return [
        providers,
        agents ? `${agents} agent${agents === 1 ? '' : 's'}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
    }
    const kids = groups.get(node.id)?.length;
    if (node.type === 'provider')
      return kids ? `${kids} account${kids === 1 ? '' : 's'}` : 'Provider';
    if (node.type === 'device')
      return (
        [node.status, kids ? `${kids} runner${kids === 1 ? '' : 's'}` : '']
          .filter(Boolean)
          .join(' · ') || 'Device'
      );
    if (node.type === 'runner') return node.status || 'Runner';
    return node.detail || FLEET_MAP_TYPE_LABEL[node.type];
  }

  /** Accounts sit inside their provider; the provider name need not repeat. */
  private childLabel(node: FleetMapNode, parent?: FleetMapNode): string {
    if (!parent) return node.label;
    const prefix = `${parent.label} `;
    const label = node.label.toLowerCase().startsWith(prefix.toLowerCase())
      ? node.label.slice(prefix.length)
      : node.label;
    return label.charAt(0).toUpperCase() + label.slice(1);
  }

  private renderNode(
    node: FleetMapNode,
    position: { x: number; y: number; w: number; h: number },
    context: {
      view: FleetMapGraph;
      layout: FleetMapLayout;
      byId: Map<string, FleetMapNode>;
      lit: Set<string> | null;
      hits: Set<string>;
      parent?: FleetMapNode;
    }
  ) {
    const { lit, hits, layout } = context;
    const gaps = nodeGaps(node);
    const focused = !!this.focusGap && node.gaps.includes(this.focusGap);
    const kids = layout.groups.get(node.id);
    const isHeader = !!kids && !this.collapsed.has(node.id);
    const child = !!context.parent;
    const dim =
      (lit && !lit.has(node.id)) ||
      (!lit && this.focusGap && !focused) ||
      (!lit && !this.focusGap && hits.size > 0 && !hits.has(node.id));
    const speaking = this.instanceAdmin
      ? this.lights.at(node.id, Date.now())
      : undefined;
    const classes = [
      'node',
      node.type === 'cluster' ? 'cluster' : '',
      isHeader ? 'header' : '',
      child ? 'child' : '',
      node.type === 'agent' && agentState(node) === 'inactive'
        ? 'inactive'
        : '',
      gaps.length ? 'has-gap' : '',
      hits.has(node.id) ? 'match' : '',
      node.id === this.selected ? 'selected' : '',
      focused ? 'focus' : '',
      speaking ? `speaking speak-${speaking.kind}` : '',
      lit && lit.has(node.id) && node.id !== this.selected ? 'lit' : '',
      dim ? 'dim' : '',
    ].join(' ');
    const label = child
      ? this.childLabel(node, context.parent)
      : displayLabel(node);
    const sub = this.subtitle(node, context.view, layout.groups, context.byId);
    const height = isHeader ? FLEET_MAP_NODE_HEIGHT : position.h;
    const entering = presenceStyle(this.presence, node.id, Date.now());
    const button = html`<button
      type="button"
      class=${classes}
      data-node=${node.id}
      data-presence=${node.id}
      style="left:${position.x}px;top:${position.y}px;width:${position.w}px;height:${height}px${
        entering
          ? `;opacity:${entering.opacity.toFixed(3)};transform:scale(${entering.scale.toFixed(3)})`
          : ''
      }"
      aria-pressed=${node.id === this.selected ? 'true' : 'false'}
      aria-label="${FLEET_MAP_TYPE_LABEL[node.type]} ${node.label}${
        gaps.length ? `, ${gaps.map((gap) => gap.label).join(', ')}` : ''
      }"
      title="${node.label}${node.detail ? `\n${node.detail}` : ''}${
        gaps.length ? `\n${gaps.map((gap) => gap.label).join('\n')}` : ''
      }"
      @click=${() => {
        if (this.suppressClick) {
          this.suppressClick = false;
          return;
        }
        this.select(node.id);
      }}
    >
      <span class="glyph"><sl-icon name=${ICON[node.type]}></sl-icon></span>
      ${child ? nothing : this.renderCode(node)}
      <span class="text"
        ><span class="label">${label}</span>${
          child
            ? nothing
            : html`<span class="sub ${gaps.length ? 'warn' : ''}">${sub}</span>`
        }</span
      >
      ${
        gaps.length
          ? html`<sl-icon
              class="gap-dot"
              name="exclamation-triangle-fill"
              aria-hidden="true"
            ></sl-icon>`
          : nothing
      }
    </button>`;
    if (!kids) return button;
    const open = !this.collapsed.has(node.id);
    return html`<div
        class="box ${dim && !(lit && kids.some((id) => lit.has(id))) ? 'dim' : ''}"
        style="left:${position.x}px;top:${position.y}px;width:${position.w}px;height:${position.h}px"
      ></div>
      ${button}
      <button
        type="button"
        class="fold"
        data-fold=${node.id}
        style="left:${position.x + position.w - 34}px;top:${position.y + 10}px"
        aria-expanded=${open ? 'true' : 'false'}
        aria-label="${open ? 'Collapse' : 'Expand'} ${node.label}"
        @click=${() => (this.collapsed = this.toggle(this.collapsed, node.id))}
      >
        <sl-icon name=${open ? 'chevron-up' : 'chevron-down'}></sl-icon>
      </button>`;
  }

  /** Pull and merge request counters on an agent, flow or person. */
  private renderCode(node: FleetMapNode) {
    if (!this.instanceAdmin) return nothing;
    const counts = this.codeActivity?.counts[node.id];
    if (!counts) return nothing;
    const items = CODE_BADGES.filter(([action]) => counts[action] > 0);
    if (!items.length) return nothing;
    const title = items
      .map(([action, , label]) => `${counts[action]} ${label}`)
      .join(', ');
    return html`<span
      class="code-badges"
      data-code=${node.id}
      title="Pull and merge requests in this window: ${title}"
      aria-label="Pull and merge requests: ${title}"
      >${items.map(
        ([action, icon]) =>
          html`<span class="code-badge ${action}" data-action=${action}
            ><sl-icon name=${icon} aria-hidden="true"></sl-icon
            >${counts[action]}</span
          >`
      )}</span
    >`;
  }

  /** Nodes that just left the window, fading where they stood. */
  private renderLeaving(positions: Map<string, FleetMapPosition>) {
    if (!this.presence.leaving.size) return nothing;
    const now = Date.now();
    return [...this.presence.leaving].map(([id, { node }]) => {
      const position = positions.get(id) ?? this.lastPositions.get(id);
      if (!position) return nothing;
      const style = presenceStyle(this.presence, id, now);
      return html`<div
        class="node ghost"
        data-presence=${id}
        data-leaving=${id}
        aria-hidden="true"
        style="left:${position.x}px;top:${position.y}px;width:${position.w}px;height:${position.h}px;opacity:${style?.opacity.toFixed(3) ?? 0}"
      >
        <span class="glyph"><sl-icon name=${ICON[node.type]}></sl-icon></span>
        <span class="text"
          ><span class="label">${displayLabel(node)}</span></span
        >
      </div>`;
    });
  }

  /** Code events shown beside the node that made them. */
  private renderPips(positions: Map<string, FleetMapPosition>) {
    if (!this.pips.length) return nothing;
    return this.pips.map((pip) => {
      const position = positions.get(pip.node);
      if (!position) return nothing;
      return html`<div
        class="pip ${pip.action}"
        data-pip=${pip.id}
        data-start=${pip.start}
        aria-hidden="true"
        style="left:${position.x + position.w + 10}px;top:${position.y + 14}px"
      >
        ${pip.label}
      </div>`;
    });
  }

  private renderToasts(byId: Map<string, FleetMapNode>) {
    return html`<div
      class="overlay toasts"
      role="status"
      aria-live="polite"
      data-testid="fleet-map-toasts"
    >
      ${this.toasts.map((toast) => {
        const node = byId.get(toast.node);
        return html`<div class="toast ${toast.action}" data-toast=${toast.id}>
          <sl-icon
            name=${CODE_BADGES.find(([action]) => action === toast.action)?.[1] ?? 'git'}
            aria-hidden="true"
          ></sl-icon>
          <span
            ><strong>${node ? displayLabel(node) : 'An agent'}</strong>
            ${toast.label}</span
          >
        </div>`;
      })}
    </div>`;
  }

  private lastPositions = new Map<string, FleetMapPosition>();

  /**
   * Which relationships are drawn. Containment replaces account-of-provider
   * and device-hosts-runner lines; an owner's line to an agent on their own
   * device repeats the device's. Agent-to-account lines appear when lit:
   * each agent-to-model line already names its account in its tooltip.
   */
  private drawnEdges(
    view: FleetMapGraph,
    layout: FleetMapLayout,
    lit: Set<string> | null
  ): { edge: FleetMapEdge; from: string; to: string; accounts: string[] }[] {
    const owned = new Set(
      view.edges
        .filter(
          (edge) => edge.kind === 'owns' && edge.target.startsWith('device:')
        )
        .map((edge) => `${edge.source}>${edge.target}`)
    );
    const hostedBy = new Map<string, string[]>();
    for (const edge of view.edges)
      if (edge.kind === 'hosts')
        hostedBy.set(edge.target, [
          ...(hostedBy.get(edge.target) || []),
          edge.source,
        ]);
    const drawn = new Map<
      string,
      { edge: FleetMapEdge; from: string; to: string; accounts: string[] }
    >();
    for (const edge of view.edges) {
      if (edge.kind === 'account_of_provider' && !this.spatial) continue;
      if (
        edge.kind === 'uses_account' &&
        !(lit && lit.has(edge.source) && lit.has(edge.target))
      )
        continue;
      if (
        edge.kind === 'owns' &&
        edge.target.startsWith('agent:') &&
        (hostedBy.get(edge.target) || []).some((device) =>
          owned.has(`${edge.source}>${device}`)
        )
      )
        continue;
      const from = layout.anchor(edge.source);
      const to = layout.anchor(edge.target);
      if (!from || !to || from === to) continue;
      const key = `${from}>${to}`;
      const existing = drawn.get(key);
      const account = edge.account_id ?? '';
      if (existing) {
        if (account && !existing.accounts.includes(account))
          existing.accounts.push(account);
        if (edge.gaps.length && !existing.edge.gaps.length)
          existing.edge = edge;
        continue;
      }
      drawn.set(key, { edge, from, to, accounts: account ? [account] : [] });
    }
    return [...drawn.values()];
  }

  private renderPanel(
    graph: FleetMapGraph,
    reach: Set<string>,
    uses: Set<string>
  ) {
    const node = graph.nodes.find((item) => item.id === this.selected);
    if (!node) return nothing;
    const gaps = nodeGaps(node);
    const byId = new Map(graph.nodes.map((item) => [item.id, item]));
    const dependents = [...reach]
      .filter((id) => id !== node.id)
      .map((id) => byId.get(id) as FleetMapNode);
    const relied = [...uses]
      .filter((id) => id !== node.id)
      .map((id) => byId.get(id) as FleetMapNode);
    const order = FLEET_MAP_TYPES;
    const groups = order
      .map((type) => ({
        type,
        items: dependents.filter((item) => item.type === type),
      }))
      .filter((group) => group.items.length);
    const people = groups.find((group) => group.type === 'user')?.items.length;
    const agents = groups.find((group) => group.type === 'agent')?.items.length;
    const seen =
      node.type === 'agent' && node.last_seen_at
        ? formatRelativeTime(node.last_seen_at, new Date(), {
            maxRelativeDays: 365,
          })
        : '';
    return html`<aside
      class="panel"
      aria-label="Blast radius"
      data-testid="fleet-map-panel"
      @pointerdown=${(event: Event) => event.stopPropagation()}
      @wheel=${(event: Event) => event.stopPropagation()}
    >
      <button
        class="close"
        type="button"
        aria-label="Clear selection"
        @click=${() => this.clearSelection()}
      >
        ×
      </button>
      <div class="eyebrow">${FLEET_MAP_TYPE_LABEL[node.type]}</div>
      <h3>
        ${node.href ? html`<a href=${node.href}>${displayLabel(node)}</a>` : displayLabel(node)}
      </h3>
      ${
        node.detail || seen
          ? html`<p class="meta">
              ${[
                node.detail,
                node.type === 'agent' ? agentStateLabel(node) : '',
                seen ? `last seen ${seen}` : '',
              ]
                .filter(Boolean)
                .join(' · ')}
            </p>`
          : nothing
      }
      ${
        gaps.length
          ? html`<div class="chips">
              ${gaps.map(
                (gap) =>
                  html`<sl-badge class="chip" pill variant=${gap.severity}
                    >${gap.label}</sl-badge
                  >`
              )}
            </div>`
          : nothing
      }
      ${
        node.actions?.length
          ? html`<div class="actions">
              ${node.actions.map(
                (action) => html`<a href=${action.href}>${action.label}</a>`
              )}
            </div>`
          : nothing
      }
      <button
        type="button"
        class="pill"
        data-testid="focus-only"
        aria-pressed=${this.focusOnly ? 'true' : 'false'}
        @click=${() => {
          this.focusOnly = !this.focusOnly;
          this.refit = true;
        }}
      >
        <sl-icon name="bullseye" aria-hidden="true"></sl-icon>Show only related
      </button>
      ${
        relied.length
          ? html`<h4>Relies on</h4>
              <ul>
                ${relied.map(
                  (item) =>
                    html`<li>
                      <a href=${item.href}>${item.label}</a
                      ><span class="meta"
                        >${FLEET_MAP_TYPE_LABEL[item.type]}</span
                      >
                    </li>`
                )}
              </ul>`
          : nothing
      }
      <h4>Blast radius</h4>
      <p class="reach">
        ${
          dependents.length
            ? html`Reaches ${dependents.length} connected
              resource${dependents.length === 1 ? '' : 's'}${
                agents || people
                  ? html`, including
                    ${[
                      agents ? `${agents} agent${agents === 1 ? '' : 's'}` : '',
                      people
                        ? `${people} ${people === 1 ? 'person' : 'people'}`
                        : '',
                    ]
                      .filter(Boolean)
                      .join(' and ')}`
                  : nothing
              }.`
            : 'No downstream resources in this map.'
        }
      </p>
      <p class="meta">
        What depends on this across the loaded map, including filtered-out
        resources; not an outage report.
        ${
          graph.truncated || graph.unavailable.length
            ? html`<span role="status"
                >Partial map: this impact list may be incomplete.</span
              >`
            : nothing
        }
      </p>
      ${groups.map(
        ({ type, items }) =>
          html`<div class="group">
              ${FLEET_MAP_COLUMN_LABEL[type]} · ${items.length}
            </div>
            <ul>
              ${items.map((item) => {
                const itemGaps = nodeGaps(item);
                return html`<li>
                  ${
                    item.href
                      ? html`<a href=${item.href}>${item.label}</a>`
                      : html`<span>${item.label}</span>`
                  }
                  ${
                    itemGaps.length
                      ? html`<sl-badge class="chip" pill variant="warning"
                          >${itemGaps[0].label}</sl-badge
                        >`
                      : nothing
                  }
                  ${(item.actions || []).map(
                    (action) =>
                      html`<a class="act" href=${action.href}
                        >${action.label}</a
                      >`
                  )}
                </li>`;
              })}
            </ul>`
      )}
    </aside>`;
  }

  /** The simulation reruns only when the shown graph or a position changes. */
  private spatialFor(view: FleetMapGraph): FleetMapLayout {
    const key = [view, this.points];
    const cached = this.spatialCache;
    if (cached && cached.key.every((value, index) => value === key[index]))
      return cached.layout;
    const seed = layoutGraph(view).positions;
    const clusters = clusterOf(view);
    // When a person (or a lone machine) joins or leaves, the islands are
    // dealt round the ring again and every node glides to its new place, so
    // the picture stays balanced around its centre of gravity. Otherwise
    // nodes already on screen stay put and only arrivals are placed, beside
    // what they link to.
    const groups = [...new Set(clusters.values())].sort().join('|');
    if (groups !== this.settledGroups) {
      this.settledGroups = groups;
      this.settled.clear();
    }
    const settled: FleetMapPoints = {};
    for (const node of view.nodes) {
      const at = this.settled.get(node.id);
      if (at) settled[node.id] = at;
    }
    const layout = spatialLayout(
      view,
      seed,
      { ...settled, ...this.points },
      clusters
    );
    for (const [id, position] of layout.positions)
      this.settled.set(id, { x: position.x, y: position.y });
    this.spatialCache = { key, layout };
    return layout;
  }

  /** Where each node was last placed this session, so refreshes keep it. */
  private settled = new Map<string, { x: number; y: number }>();
  private settledGroups = '';

  /** Lit relationships by drawn line, with the label of what travelled. */
  private activityByLine(
    view: FleetMapGraph,
    layout: FleetMapLayout
  ): Map<string, string> {
    const lines = new Map<string, string>();
    if (!this.activity.marks.size) return lines;
    const now = Date.now();
    const byId = new Map(view.edges.map((edge) => [edge.id, edge]));
    for (const [id, mark] of this.activity.marks) {
      const edge = byId.get(id);
      if (!edge || mark.until <= now) continue;
      const from = layout.anchor(edge.source);
      const to = layout.anchor(edge.target);
      if (from && to && !lines.has(`${from}>${to}`))
        lines.set(`${from}>${to}`, activityLabel(mark));
    }
    return lines;
  }

  render() {
    const graph = this.graph && withCodeFlows(this.graph, this.codeActivity);
    if (!graph) return nothing;
    if (!graph.nodes.length) {
      this.lastLayout = null;
      const windowLabel = FLEET_MAP_WINDOWS.find(
        (item) => item.value === this.activityWindow
      )?.label;
      // Keep the window reachable: a narrow window must not strand the user.
      return html`${
          this.activityWindow === 'any'
            ? nothing
            : html`<div class="toolbar">${this.renderWindowSelect()}</div>`
        }
        <div class="empty-state" data-testid="fleet-map-empty">
          ${graph.unavailable.length ? html`<p>Device evidence is unavailable. Check fleet collection and refresh.</p>` : nothing}
          ${
            this.activityWindow === 'any'
              ? html`Nothing to map yet. Register a runner with
                  <code>preloop runner fg</code> or onboard an agent, then
                  configure its model to connect the map.`
              : html`Nothing active in the ${windowLabel?.toLowerCase()}. Choose
                a longer window or Any time.`
          }
        </div>`;
    }
    const filtered = this.filtered();
    const hits = this.searchHits(filtered);
    const selected =
      this.selected && graph.nodes.some((node) => node.id === this.selected)
        ? this.selected
        : '';
    const reach = selected ? blastRadius(graph, selected).nodes : null;
    const uses = selected ? dependencies(graph, selected) : null;
    const lit = reach && uses ? new Set([...reach, ...uses]) : null;
    const view =
      lit && this.focusOnly
        ? {
            ...filtered,
            nodes: filtered.nodes.filter((node) => lit.has(node.id)),
            edges: filtered.edges.filter(
              (edge) => lit.has(edge.source) && lit.has(edge.target)
            ),
          }
        : filtered;
    const pinned = new Set([...hits, ...(lit ?? [])]);
    const target = this.spatial
      ? this.spatialFor(view)
      : layoutGraph(view, {
          cap: FLEET_MAP_CAP,
          expanded: this.expanded,
          collapsed: this.collapsed,
          pinned,
        });
    this.lastLayout = target;
    const layout = this.spatial ? this.glideLayout(target) : target;
    for (const [id, position] of target.positions)
      this.lastPositions.set(id, position);
    if (this.lastPositions.size > 2000)
      this.lastPositions = new Map(layout.positions);
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    const parentOf = new Map<string, string>();
    for (const [parent, kids] of layout.groups)
      for (const kid of kids) parentOf.set(kid, parent);
    const half = FLEET_MAP_NODE_HEIGHT / 2;
    const centreY = (id: string) => {
      const p = layout.positions.get(id)!;
      return (
        p.y +
        Math.min(p.h, layout.groups.has(id) ? FLEET_MAP_NODE_HEIGHT : p.h) / 2
      );
    };
    const context = { view, layout, byId, lit, hits };
    const active = this.activityByLine(view, layout);
    // On an administrator's spatial canvas the lit node says where traffic
    // started; the line lights without a label, so a busy map stays legible.
    const quiet = this.instanceAdmin && this.spatial;
    return html`${this.renderToolbar(graph, filtered, hits)}
      ${this.renderGaps(filtered)}
      ${
        graph.unavailable.length
          ? html`<p class="notice" role="status">
              Device evidence is unavailable. Refresh when fleet collection is
              available; existing agents and model configuration remain visible.
              Blast radius is incomplete.
            </p>`
          : nothing
      }
      ${
        graph.truncated
          ? html`<p class="notice" role="status">
              This map is capped at ${graph.limit} resources. Summary groups
              show omitted resources; filters only narrow the loaded map. Blast
              radius is incomplete. Use the table or resource pages to inspect
              the rest.
            </p>`
          : nothing
      }
      ${
        this.codeActivity?.truncated
          ? html`<p class="notice" role="status" data-testid="code-truncated">
              Pull request counts are capped for this window and may be low.
              Choose a shorter window for exact counts.
            </p>`
          : nothing
      }
      <div
        class="stage ${lit ? 'has-selection' : ''}"
        tabindex="0"
        role="region"
        aria-label="Fleet dependency map"
        @keydown=${this.onKeydown}
        @wheel=${this.onWheel}
        @pointerdown=${this.pointerDown}
        @pointermove=${this.pointerMove}
        @pointerup=${this.pointerUp}
        @pointercancel=${this.pointerUp}
        data-testid="fleet-map"
      >
        ${
          view.nodes.length
            ? html`<div
                class="plane"
                style="width:${layout.width}px;height:${layout.height}px;transform:translate(${this.translateX}px,${this.translateY}px) scale(${this.scale})"
              >
                ${layout.columns.map(
                  (column) =>
                    html`<div
                      class="column-title"
                      style="left:${column.x}px;width:${216}px"
                    >
                      ${FLEET_MAP_COLUMN_LABEL[column.type]}<span class="n"
                        >${column.count}</span
                      >
                      ${
                        this.expanded.has(column.type) &&
                        column.count > FLEET_MAP_CAP
                          ? html`<button
                              type="button"
                              class="reset"
                              @click=${() =>
                                (this.expanded = this.toggle(
                                  this.expanded,
                                  column.type
                                ))}
                            >
                              Show fewer
                            </button>`
                          : nothing
                      }
                    </div>`
                )}
                <svg
                  width=${layout.width}
                  height=${layout.height}
                  aria-hidden="true"
                >
                  ${this.drawnEdges(view, layout, lit).map(
                    ({ edge, from, to, accounts }) => {
                      const live = active.get(`${from}>${to}`);
                      const a = layout.positions.get(from)!;
                      const b = layout.positions.get(to)!;
                      const na = byId.get(edge.source)!;
                      const nb = byId.get(edge.target)!;
                      const y1 = centreY(from);
                      const y2 = centreY(to);
                      let d: string;
                      if (Math.abs(a.x - b.x) < 40) {
                        const x = Math.max(a.x + a.w, b.x + b.w);
                        d = `M${a.x + a.w} ${y1} C${x + 36} ${y1} ${x + 36} ${y2} ${b.x + b.w} ${y2}`;
                      } else {
                        const [l, ly, r, ry] =
                          a.x <= b.x ? [a, y1, b, y2] : [b, y2, a, y1];
                        const x1 = l.x + l.w;
                        const x2 = r.x;
                        const mid = (x1 + x2) / 2;
                        d = `M${x1} ${ly} C${mid} ${ly} ${mid} ${ry} ${x2} ${ry}`;
                      }
                      const inRadius =
                        !!lit &&
                        lit.has(edge.impact_source) &&
                        lit.has(edge.impact_target);
                      const focused =
                        !!this.focusGap &&
                        (na.gaps.includes(this.focusGap) ||
                          nb.gaps.includes(this.focusGap));
                      const cls = [
                        'edge',
                        edge.provenance,
                        edge.gaps.length ? 'gap' : '',
                        inRadius ? 'lit' : '',
                        live ? 'active' : '',
                        (lit && !inRadius) ||
                        (!lit && this.focusGap && !focused)
                          ? 'dim'
                          : '',
                      ].join(' ');
                      const via = accounts
                        .map((id) => byId.get(id)?.label)
                        .filter(Boolean)
                        .join(', ');
                      const path = svg`<path class=${cls} data-edge=${edge.kind} data-edge-id=${edge.id} data-activity=${live || nothing} d=${d}><title>${na.label} ${FLEET_MAP_EDGE_LABEL[edge.kind]} ${nb.label}${via ? ` via ${via}` : ''}${live ? ` · ${live}` : ''}</title></path>`;
                      if (!live || quiet) return path;
                      const mx = (a.x + a.w / 2 + b.x + b.w / 2) / 2;
                      const my = (y1 + y2) / 2 - 6;
                      return svg`${path}<text class="activity-label" x=${mx} y=${my} text-anchor="middle">${live}</text>`;
                    }
                  )}
                </svg>
                ${view.nodes.map((node) => {
                  const position = layout.positions.get(node.id);
                  if (!position) return nothing;
                  const parent = parentOf.get(node.id);
                  return this.renderNode(node, position, {
                    ...context,
                    parent: parent ? byId.get(parent) : undefined,
                  });
                })}
                ${this.renderLeaving(layout.positions)}
                ${this.renderPips(layout.positions)}
                ${layout.more.map(
                  (row) =>
                    html`<button
                      type="button"
                      class="more"
                      data-more=${row.column}
                      style="left:${row.x}px;top:${row.y}px;width:216px;height:36px"
                      @click=${() =>
                        (this.expanded = this.toggle(
                          this.expanded,
                          row.column
                        ))}
                    >
                      +${row.hidden} more
                      ${FLEET_MAP_COLUMN_LABEL[row.column].toLowerCase()}
                    </button>`
                )}
              </div>`
            : html`<div class="empty-state">
                No nodes match these filters.
                ${
                  this.states.size < 3
                    ? 'Include discovered or inactive agents, or clear a filter.'
                    : 'Clear a filter to see more.'
                }
              </div>`
        }
        <div
          class="overlay canvas-tools"
          role="group"
          aria-label="Map navigation"
        >
          <button
            type="button"
            @click=${() => this.zoom(0.8)}
            aria-label="Zoom out"
          >
            −
          </button>
          <span class="zoom"></span>
          <button
            type="button"
            @click=${() => this.zoom(1.25)}
            aria-label="Zoom in"
          >
            +
          </button>
          <button type="button" @click=${this.fit}>Fit</button>
          <button
            type="button"
            class="fullscreen-toggle"
            data-testid="fleet-map-fullscreen"
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
        <div class="overlay legend">
          <span
            ><svg viewBox="0 0 24 8" aria-hidden="true">
              <line x1="0" y1="4" x2="24" y2="4" class="edge" /></svg
            >Configured</span
          >
          <span
            ><svg viewBox="0 0 24 8" aria-hidden="true">
              <line x1="0" y1="4" x2="24" y2="4" class="edge observed" /></svg
            >Observed</span
          >
          <span
            ><sl-icon class="gap-dot" name="exclamation-triangle-fill"></sl-icon
            >Gap</span
          >
          <span
            >Select a node to see what depends on it. Scroll to pan, Ctrl+scroll
            to zoom.</span
          >
        </div>
        ${this.instanceAdmin ? this.renderToasts(new Map(view.nodes.map((node) => [node.id, node]))) : nothing}
        ${lit && reach && uses ? this.renderPanel(graph, reach, uses) : nothing}
      </div>`;
  }
}

/** Pointer capture fails for synthetic or already released pointers. */
function capture(event: PointerEvent, on: boolean): void {
  const target = event.currentTarget as HTMLElement | null;
  try {
    if (on) target?.setPointerCapture(event.pointerId);
    else target?.releasePointerCapture(event.pointerId);
  } catch {
    // Dragging still works without capture while the pointer is over the map.
  }
}

const agentStateLabel = (node: FleetMapNode): string =>
  ({
    onboarded: 'Onboarded',
    discovered: 'Discovered, not onboarded',
    inactive: 'Inactive',
  })[agentState(node)];

/** Short chip wording; the node and the side panel keep the full label. */
const GAP_CHIP_LABEL: Partial<Record<FleetMapGapCode, string>> = {
  discovered_agent: 'not onboarded',
  unknown_device: 'device unknown',
  offline_runner: 'runner offline',
  not_via_gateway: 'not via the gateway',
  open_finding: 'open finding',
};

declare global {
  interface HTMLElementTagNameMap {
    'fleet-map': FleetMap;
  }
}
