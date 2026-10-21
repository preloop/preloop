import { LitElement, css, html, nothing, svg, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/menu-label/menu-label.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import './list-toolbar';
import {
  FLEET_MAP_CAP,
  FLEET_MAP_COLUMN_LABEL,
  FLEET_MAP_EDGE_LABEL,
  FLEET_MAP_HEADER,
  FLEET_MAP_NODE_HEIGHT,
  FLEET_MAP_NODE_WIDTH,
  FLEET_MAP_TYPES,
  FLEET_MAP_TYPE_LABEL,
  laneTitle,
  FLEET_MAP_MAX_USERS,
  agentState,
  blastRadius,
  dependencies,
  filterGraph,
  scopeToUsers,
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
  type FleetUsageResponse,
  type FleetMapPosition,
  getFleetCodeActivity,
  getFleetUsage,
  withCodeFlows,
} from '../fleet-map-api';
import { formatRelativeTime } from '../utils/date';
import { formatCurrencyAmount } from '../utils/money';
import {
  placeBubbles,
  placeCornerNotes,
  type BubbleRect,
  type BubbleRequest,
  type PlacedBubble,
} from '../utils/fleet-map-bubbles';
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
  modelKey,
  saveFleetMapWindow,
  windowMinutes,
  windowLabel,
  type FleetMapWindow,
} from '../utils/fleet-map-activity';
import {
  ARRANGEMENTS,
  ARRANGEMENT_ICON,
  ARRANGEMENT_LABEL,
  parseArrangement,
} from '../utils/fleet-map-words';
import {
  LEGIBLE_SPATIAL_SCALE,
  loadArrangement,
  loadPositions,
  saveArrangement,
  savePositions,
  spatialLayout,
  type FleetMapArrangement,
  type FleetMapViewport,
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
  object,
  safeEventText,
} from '../utils/fleet-map-motion';
import { FleetStory } from '../utils/fleet-map-story';
import { FleetEventLog } from '../utils/fleet-map-event-log';
import { compactCount } from '../utils/fleet-map-motion';
import { nodeMark } from '../utils/fleet-map-icons';
import {
  avatarSource,
  personInitials,
  nodeUsage,
  workActionWords,
  withWorkRelations,
  isWorkRelation,
  workRelationLabel,
} from '../utils/fleet-map-details';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import { getAgentKindPresentation } from '../utils/agent-kinds';
import consoleStyles from '../styles/console-styles.css?inline';

const ICON: Record<FleetMapNodeType, string> = {
  user: 'person',
  device: 'laptop',
  runner: 'hdd-rack',
  agent: 'robot',
  flow: 'diagram-2',
  work: 'journal-code',
  model: 'stars',
  provider_account: 'key',
  provider: 'cloud',
  cluster: 'collection',
};

/** Past this many people the Users menu gets a search field. */
const USERS_SEARCH_MIN = 9;

/** Type toggles, in reading order; runners and accounts are nested rows. */
const TYPE_TOGGLE_LABEL: Record<(typeof FLEET_MAP_TYPES)[number], string> = {
  user: 'People',
  device: 'Devices',
  runner: 'Runners',
  agent: 'Agents',
  flow: 'Flows',
  work: 'Work (issues, PRs)',
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
/** Spatial fit stops at the legible scale; the layout stacks shelves instead. */
const MIN_SPATIAL_FIT_SCALE = LEGIBLE_SPATIAL_SCALE;
/** Narrower stages fold the legend behind its ? button. */
const LEGEND_INLINE_MIN_WIDTH = 1000;
/**
 * Where the wheel scrolls the page (the Overview card), overflow is not
 * reachable by scrolling and reads as nodes that were never drawn (B-08):
 * a fit there contains the whole picture, down to the zoom-out limit.
 */
const MIN_CONTAINED_FIT_SCALE = 0.2;
/** Nodes and the view glide to a new arrangement in this long (medium). */
const GLIDE_MS = 400;
/** Label tags drawn on a work chip; the rest are a +N count and in the title. */
const LABELS_SHOWN = 2;
/** After a pan or zoom by hand the view stops following the fleet a while. */
const HANDS_OFF_MS = 15_000;
const DEFAULT_TYPES = FLEET_MAP_TYPES.filter(
  (type) => type !== 'provider_account'
);
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
  /** Server measurements per resource for the selected window. */
  @property({ attribute: false }) usage: FleetUsageResponse | null = null;
  /** Force the reduced-motion fallback (tests); the media query also does. */
  @property({ type: Boolean, attribute: 'reduced-motion' })
  reducedMotion = false;
  /**
   * Only the map: no toolbar, gap strip or legend (presentations, the demo).
   * An empty map is a blank stage rather than the empty-state help.
   */
  @property({ type: Boolean, reflect: true }) bare = false;
  @property({ type: Boolean, reflect: true }) bubbles = false;
  /** Plain wheel scrolls the document in cards; viewport hosts opt into pan. */
  @property({ attribute: 'wheel-mode' }) wheelMode: 'page' | 'pan' = 'pan';
  @state() private pips: FleetPip[] = [];
  @state() private toasts: FleetPip[] = [];
  @state() private arrangement: FleetMapArrangement = 'columns';
  @state() private points: FleetMapPoints = {};
  @state() private selected = '';
  @state() private types = new Set<FleetMapNodeType>(DEFAULT_TYPES);
  /**
   * The people the map is scoped to (user node ids); empty means everyone.
   * Hosts pass the server `users=` param on `fleet-map-users-change`; until a
   * response says it applied them (`graph.users`), the map scopes itself.
   */
  @property({ attribute: false }) users: string[] = [];
  @state() private usersQuery = '';
  /** Everyone any loaded map listed, so a scoped map can still offer them. */
  private knownPeople = new Map<string, FleetMapNode>();
  @state() private gapsOnly = false;
  @state() private query = '';
  @state() private filterPage = '';
  @state() private focusGap: FleetMapGapCode | '' = '';
  @state() private focusOnly = false;
  @state() private expanded = new Set<FleetMapColumn>();
  @state() private collapsed = new Set<string>();
  @state() private storyVersion = 0;
  @state() private legendOpen = false;
  @property({ type: Boolean }) includeIdle = false;
  @state() private failedAvatars = new Set<string>();
  private readonly story = new FleetStory();
  private storyTimer?: ReturnType<typeof setInterval>;
  private pendingMessages: FleetMessage[] = [];

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
  private viewport = { width: 1200, height: 720 };
  private stageObserver?: ResizeObserver;
  private observedStage?: Element;
  @state() private resetUndo?: {
    points: FleetMapPoints;
    view: View;
    collapsed: Set<string>;
    expanded: Set<FleetMapColumn>;
    message: string;
  };
  private resetTimer?: ReturnType<typeof setTimeout>;
  private previousBodyOverflow: string | null = null;

  private get layoutOwned(): boolean {
    return Object.keys(this.points).length > 0;
  }

  /** Repack the current nodes. Columns only needs its camera fitted. */
  arrange(): void {
    if (!this.spatial) {
      this.fit();
      return;
    }
    this.repack(false);
  }

  /** Forget positions and folds for this scope, including resources off screen. */
  resetLayout(): void {
    this.repack(true);
    this.dispatchEvent(
      new CustomEvent('fleet-map-layout-reset', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private repack(reset: boolean): void {
    this.resetUndo = {
      points: this.points,
      view: { x: this.translateX, y: this.translateY, s: this.scale },
      collapsed: this.collapsed,
      expanded: this.expanded,
      message: reset ? 'Layout reset' : 'Arranged',
    };
    this.points = {};
    // A deliberate arrangement uses the existing glide, unlike a pointer drag.
    this.glidePoints = this.points;
    if (reset) {
      this.collapsed = new Set();
      this.expanded = new Set();
    }
    this.settled.clear();
    this.spatialCache = undefined;
    this.refit = !this.spatial;
    this.recenter = this.spatial;
    this.joined = true;
    this.userMovedAt = -Infinity;
    savePositions(this.userId, {}, this.positionsScope);
    if (this.resetTimer) clearTimeout(this.resetTimer);
    this.resetTimer = setTimeout(() => {
      this.resetUndo = undefined;
    }, 6000);
  }

  undoResetLayout(): void {
    if (!this.resetUndo) return;
    this.points = this.resetUndo.points;
    this.translateX = this.resetUndo.view.x;
    this.translateY = this.resetUndo.view.y;
    this.scale = this.resetUndo.view.s;
    this.collapsed = this.resetUndo.collapsed;
    this.expanded = this.resetUndo.expanded;
    this.resetUndo = undefined;
    if (this.resetTimer) clearTimeout(this.resetTimer);
    this.settled.clear();
    this.spatialCache = undefined;
    this.camera = undefined;
    this.tween = undefined;
    savePositions(this.userId, this.points, this.positionsScope, [
      ...(this.lastLayout?.positions.keys() ?? []),
    ]);
  }

  private observeStage(): void {
    const stage = this.shadowRoot?.querySelector('.stage');
    if (!stage || stage === this.observedStage) return;
    this.stageObserver?.disconnect();
    this.observedStage = stage;
    this.stageObserver = new ResizeObserver(() => {
      const width = stage.clientWidth;
      const height = stage.clientHeight;
      if (
        !width ||
        !height ||
        (width === this.viewport.width && height === this.viewport.height)
      )
        return;
      this.viewport = { width, height };
      if (!this.layoutOwned) this.settled.clear();
      this.spatialCache = undefined;
      this.refit = true;
      this.requestUpdate();
    });
    this.stageObserver.observe(stage);
  }

  private refreshTimer?: ReturnType<typeof setInterval>;
  private windowFit = false;
  private staleTimer?: ReturnType<typeof setTimeout>;

  /**
   * Place one realtime message on the map. Activity from an agent the map
   * doesn't show means it just became active, so the host is asked to
   * refetch (debounced, so a burst costs one request).
   */
  recordActivity(topic: string, message: unknown): void {
    const record = object(message);
    const flow = this.story.flowForExecution(
      safeEventText(record.execution_id)
    );
    if (flow && !record.flow_id) message = { ...record, flow_id: flow };
    if (
      this.instanceAdmin &&
      (topic === 'flow_executions' || topic === 'code_activity')
    ) {
      const notes = this.story.record(topic, message);
      if (notes.length) {
        this.storyVersion++;
        this.eventLog.record(notes, message);
        this.pendingMessages.push(...notes);
        this.syncStoryTimer();
      }
      if (topic === 'code_activity') {
        this.recordCode(message, notes.length > 0);
        return;
      }
      if (notes.length) return;
    }
    if (topic === 'gateway_activity') this.learnFlowModel(message);
    if (this.graph && this.instanceAdmin) {
      const messages = messagesForRealtime(this.shown(), topic, message);
      if (messages.length && !this.eventLog.record(messages, message)) return;
      this.send(messages);
    }
    const hit = activityFromRealtime(topic, message);
    if (!hit || !this.graph) return;
    const shown = this.shown();
    const edges = edgesForActivity(shown, hit);
    let label = hit.label ?? '';
    if (hit.tokens && edges.length) {
      // A completed model call says which model answered and what it cost.
      const target = shown.edges.find((edge) => edge.id === edges[0])?.target;
      const model = shown.nodes.find((node) => node.id === target)?.label;
      label = `${model ? `${model} · ` : ''}${compactCount(hit.tokens.input)} in / ${compactCount(hit.tokens.output)} out`;
    }
    if (edges.length) this.activity.add(edges, hit.kind, label);
    else if (hit.kind === 'tool call' && !hit.native) return;
    // Only an agent the map lacks makes the graph stale. A mapped agent with
    // no matching line (ambient model, no device) just shows no highlight,
    // and under Any time activity can't reveal anything new.
    else if (
      this.activityWindow !== 'any' &&
      !this.graph.nodes.some((node) => node.id === hit.agent)
    )
      this.requestRefresh(FLEET_MAP_STALE_DEBOUNCE_MS);
  }

  private syncStoryTimer(): void {
    if (
      !this.isConnected ||
      !this.story.hasLiveActivity(windowMinutes(this.activityWindow))
    ) {
      if (this.storyTimer !== undefined) clearInterval(this.storyTimer);
      this.storyTimer = undefined;
    } else if (this.storyTimer === undefined)
      this.storyTimer = setInterval(() => {
        this.storyVersion++;
        this.syncStoryTimer();
      }, 1000);
  }

  // Calm by default: at most eight nodes lit at once.
  private readonly lights = new FleetLights(1600, 8);
  private readonly bubbleLights = new FleetLights(4000, 4);
  private readonly eventLog = new FleetEventLog();
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

  /** Models each flow was seen calling, from gateway traffic in this session. */
  private flowModels = new Map<string, Set<string>>();
  private flowModelsEpoch = 0;

  /** Forget learned flow lines when the story is reset (a reopened map). */
  private syncFlowModels(): void {
    if (this.flowModelsEpoch === this.story.epoch) return;
    this.flowModelsEpoch = this.story.epoch;
    this.flowModels.clear();
  }

  /**
   * A gateway call made inside a flow run names the flow and the model, so
   * the flow gets an observed line to that model, as an agent has one.
   */
  private learnFlowModel(message: unknown): void {
    const record = object(message);
    const payload = object(record.payload);
    const type = safeEventText(record.type);
    if (
      type !== 'model_gateway_call' &&
      type !== 'model_gateway_request_started'
    )
      return;
    const flowId =
      safeEventText(record.flow_id) || safeEventText(payload.flow_id);
    const alias =
      safeEventText(payload.requested_model) ||
      safeEventText(payload.model_alias);
    if (!flowId || !alias || !this.graph) return;
    this.syncFlowModels();
    const key = modelKey(alias);
    // The server's canonical key is the node's detail; older graphs carry
    // it only as the label.
    const model = this.graph.nodes.find(
      (node) =>
        node.type === 'model' &&
        (node.detail
          ? node.detail === key || node.detail.startsWith(`${key} `)
          : modelKey(node.label) === key)
    );
    if (!model) return;
    const flow = `flow:${flowId}`;
    const known = this.flowModels.get(flow) ?? new Set<string>();
    if (known.has(model.id)) return;
    known.add(model.id);
    this.flowModels.set(flow, known);
    this.storyVersion++;
  }

  /** Observed flow-to-model lines for the flows and models on the map. */
  private withFlowModels(graph: FleetMapGraph): FleetMapGraph {
    this.syncFlowModels();
    if (!this.flowModels.size) return graph;
    const ids = new Set(graph.nodes.map((node) => node.id));
    const have = new Set(graph.edges.map((edge) => edge.id));
    const edges = [...graph.edges];
    for (const [flow, models] of this.flowModels) {
      if (!ids.has(flow)) continue;
      for (const model of models) {
        const id = `${flow}>uses_model>${model}`;
        if (!ids.has(model) || have.has(id)) continue;
        edges.push({
          id,
          source: flow,
          target: model,
          kind: 'uses_model',
          provenance: 'observed',
          impact_source: model,
          impact_target: flow,
          gaps: [],
        });
      }
    }
    return { ...graph, edges };
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
      if (
        this.positionOf(message.from) &&
        this.positionOf(message.actor ?? message.from)
      ) {
        this.lights.light(message, now);
        this.bubbleLights.light(message, now);
        lit = true;
      }
    if (lit) {
      this.lights.changed(now);
      this.requestUpdate();
      this.startFrames();
    }
  }

  /** A code event: count it, surface it on the node and in the corner. */
  private recordCode(message: unknown, storyHandled = false): void {
    const hit = codeHitFromRealtime(message);
    if (!hit || !this.instanceAdmin) return;
    const record = object(message),
      payload = object(record.payload);
    const id =
      safeEventText(
        payload.event_id || record.event_id || payload.correlation_id
      ) ||
      (payload.timestamp
        ? `${hit.action}:${hit.nodes.join(',')}:${safeEventText(payload.timestamp)}`
        : '');
    if (id && this.seenCode.has(id)) return;
    if (id) this.seenCode.add(id);
    if (this.seenCode.size > 4000)
      this.seenCode.delete(this.seenCode.values().next().value!);
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
      ...this.codeActivity,
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
    // With Bubbles on, the bubble already says it beside the node.
    if (!this.motionOff && !this.bubbles)
      this.pips = [...this.pips, ...fresh].slice(-8);
    this.toasts = [...fresh.slice(0, 1), ...this.toasts].slice(0, 3);
    // Rich story messages already contain the object reference and verdict.
    if (!storyHandled)
      for (const node of nodes) {
        const owner = owners.find((id) => ownerOf(view, node) === id);
        this.send([{ from: node, to: owner ?? node, kind: 'code', label }]);
      }
    this.startFrames();
  }

  private seenEvents = new Set<string>();
  private seenCode = new Set<string>();

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
    this.toasts = [...toasts, ...this.toasts].slice(0, 3);
    this.startFrames();
  }

  /** Load the code activity for the window, debounced. */
  private scheduleCodeLoad(delay = 0): void {
    // The code layer is instance-admin only for now, like the spatial map.
    if (!this.live) return;
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
      const activity = this.instanceAdmin
        ? await getFleetCodeActivity(
            controller.signal,
            windowMinutes(this.activityWindow),
            this.userResourceIds
          )
        : null;
      if (controller.signal.aborted) return;
      this.codeActivity = activity;
      if (activity) this.seedToasts(activity);
    } catch {
      // Code activity is a layer on the map; the map stands without it.
    }
    try {
      const usage = await getFleetUsage(
        controller.signal,
        windowMinutes(this.activityWindow),
        this.userResourceIds
      );
      if (!controller.signal.aborted) this.usage = usage;
    } catch {
      // Totals are a detail of the side panel; an older server has none.
      if (!controller.signal.aborted) this.usage = null;
    }
  }

  /** Window totals are server measurements; missing fields never become zero. */
  private renderUsage(node: FleetMapNode) {
    const totals = nodeUsage(this.usage, node, this.codeActivity);
    if (!totals) {
      const legacyModel =
        this.usage && (node.type === 'model' || node.type === 'provider');
      return html`<section class="usage" data-testid="fleet-map-usage">
        <h4>In this window</h4>
        <p class="meta">
          ${legacyModel ? 'No gateway requests in this window.' : this.usage ? 'No usage reported for this resource in this window.' : 'Usage unavailable for this window.'}
        </p>
      </section>`;
    }
    const metric = (label: string, value: number | null | undefined) =>
      value === undefined
        ? nothing
        : html`<dt>${label}</dt>
            <dd title=${value === null ? 'Not measured' : String(value)}>
              ${value === null ? 'Unknown' : compactCount(value)}
            </dd>`;
    const cost = totals.estimated_cost;
    const costText =
      cost == null
        ? 'Unknown'
        : Math.abs(cost) > 0 && Math.abs(cost) < 0.01
          ? new Intl.NumberFormat('en-US', {
              style: 'currency',
              currency: totals.currency || 'USD',
              minimumFractionDigits: 4,
              maximumFractionDigits: 4,
            }).format(cost)
          : formatCurrencyAmount(cost, totals.currency || 'USD');
    return html`<section
      class="usage"
      data-testid="fleet-map-usage"
      title="${this.usage?.since} to ${this.usage?.until}"
    >
      <h4>In this window</h4>
      <dl class="usage-metrics">
        ${metric('Requests', totals.request_count)}
        ${
          // No requests: there are no tokens or cost to be unknown about.
          totals.request_count === 0
            ? nothing
            : html`${metric('Tokens in', totals.input_tokens)}
                ${metric('Tokens out', totals.output_tokens)}
                <dt>Cost est.</dt>
                <dd>${costText}</dd>`
        }
        ${metric('Tool calls', totals.tool_calls)}
        ${
          // An issue or PR is acted on; it does not open PRs or ask approvals.
          node.type === 'work'
            ? nothing
            : html`${metric('Approvals requested', totals.approvals_requested)}
              ${metric('Approvals approved', totals.approvals_approved)}
              ${metric('Approvals declined', totals.approvals_declined)}
              ${metric('PRs opened', totals.prs_opened)}
              ${metric('PRs reviewed', totals.prs_reviewed)}
              ${metric('PRs merged', totals.prs_merged)}
              ${metric('PRs touched', totals.prs_touched)}
              ${metric('Issues touched', totals.issues_touched)}
              ${metric('Review comments', totals.review_comments)}`
        }
      </dl>
      ${totals.partial || this.usage?.partial || this.usage?.truncated ? html`<p class="meta">Partial totals. Some values are unknown or the source is capped.</p>` : nothing}
    </section>`;
  }

  private renderWorkDetails(node: FleetMapNode, graph: FleetMapGraph) {
    if (node.type !== 'work') return nothing;
    const item = this.codeActivity?.work?.find(
      (item) =>
        item.kind === node.work_kind &&
        (item.key === node.resource_id ||
          (!!item.url && item.url === node.href))
    );
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const touches = new Map<string, { count?: number; actions: Set<string> }>();
    for (const id of item?.node_ids ?? [])
      if (byId.has(id)) touches.set(id, { actions: new Set() });
    const related = new Map<string, FleetMapNode>();
    for (const edge of graph.edges) {
      if (edge.source !== node.id && edge.target !== node.id) continue;
      const other = byId.get(
        edge.source === node.id ? edge.target : edge.source
      );
      if (!other) continue;
      if (other.type === 'work') related.set(other.id, other);
      else if (
        ['agent', 'flow', 'user'].includes(other.type) &&
        (isWorkRelation(edge) || edge.kind === 'caused')
      ) {
        const previous = touches.get(other.id);
        touches.set(other.id, {
          count: edge.count ?? previous?.count,
          actions: new Set([
            ...(previous?.actions ?? []),
            ...(edge.actions ?? []),
          ]),
        });
      }
    }
    if (node.parent_id && byId.has(node.parent_id))
      related.set(node.parent_id, byId.get(node.parent_id)!);
    for (const other of graph.nodes)
      if (other.parent_id === node.id) related.set(other.id, other);
    const link = (other: FleetMapNode) =>
      other.href
        ? html`<a
            href=${other.href}
            target=${other.type === 'work' ? '_blank' : nothing}
            rel=${other.type === 'work' ? 'noopener noreferrer' : nothing}
            >${displayLabel(other)}</a
          >`
        : displayLabel(other);
    return html`<section data-testid="fleet-map-work-details">
      ${
        // The heading already carries the title when the label embeds it.
        node.work_title && !displayLabel(node).includes(node.work_title)
          ? html`<p class="work-title">${node.work_title}</p>`
          : nothing
      }
      ${node.detail ? html`<p class="meta">Repository: ${node.detail}</p>` : nothing}
      <h4>Who touched it</h4>
      ${
        touches.size
          ? html`<ul>
              ${[...touches].map(([id, touch]) => {
                const other = byId.get(id)!;
                return html`<li>
                  ${link(other)}<span class="meta"
                    >${FLEET_MAP_TYPE_LABEL[other.type]}${touch.count === undefined ? '' : ` · ${compactCount(touch.count)} ${touch.count === 1 ? 'action' : 'actions'}`}${
                      touch.actions.size
                        ? ` · ${workActionWords(touch.actions)
                            .slice(0, 3)
                            .join(', ')}`
                        : ''
                    }</span
                  >
                </li>`;
              })}
            </ul>`
          : html`<p class="meta">No attributed activity reported.</p>`
      }
      ${
        related.size
          ? html`<h4>Related issues and PRs</h4>
              <ul>
                ${[...related.values()].map((other) => html`<li>${link(other)}</li>`)}
              </ul>`
          : nothing
      }
    </section>`;
  }

  /** Run frames while anything moves; stop as soon as nothing does. */
  private startFrames(): void {
    if (this.frame || !this.isConnected) return;
    this.frame = requestAnimationFrame(this.tick);
  }

  private tick = (): void => {
    this.frame = 0;
    const now = Date.now();
    if (this.lights.changed(now) || this.bubbleLights.changed(now))
      this.requestUpdate();
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
      this.bubbleLights.size ||
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
    for (const el of root.querySelectorAll<HTMLElement>('[data-bubble]')) {
      const light = this.bubbleLights.at(el.dataset.bubble!, now);
      el.style.opacity =
        this.motionOff || !light
          ? ''
          : String(Math.min(1, Math.max(0, (light.until - now) / 200)));
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
    this.syncActivitySubscriptions();
    this.syncCodeSubscription();
  }

  private syncActivitySubscriptions(): void {
    const want = this.live && this.isConnected;
    if (want && !this.unsubscribes.length)
      for (const topic of FLEET_ACTIVITY_TOPICS)
        this.unsubscribes.push(
          unifiedWebSocketManager.subscribe(topic, (message: unknown) =>
            this.recordActivity(topic, message)
          )
        );
    else if (!want) {
      this.unsubscribes.forEach((stop) => stop());
      this.unsubscribes = [];
    }
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
    this.positionBubbles();
    this.positionToasts();
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (
      changed.has('activityWindow') &&
      changed.get('activityWindow') !== undefined
    ) {
      this.codeController?.abort();
      this.usage = null;
      this.codeActivity = null;
      this.flowModels.clear();
      this.filterCache = undefined;
    }
    if (
      changed.has('userId') ||
      changed.has('positionsScope') ||
      changed.has('defaultArrangement')
    ) {
      this.arrangement = loadArrangement(
        this.userId,
        this.positionsScope,
        parseArrangement(this.defaultArrangement) ?? 'columns'
      );
      this.points = loadPositions(this.userId, this.positionsScope);
      this.types = loadTypes(this.userId, this.positionsScope);
      this.settled.clear();
      this.eventLog.clear();
      this.story.clear();
      this.syncStoryTimer();
      this.filterCache = undefined;
      this.lights.clear();
      this.bubbleLights.clear();
      this.spatialCache = undefined;
      this.shownLayout = undefined;
      this.tween = undefined;
      this.camera = undefined;
      this.refit = true;
      if (!this.bubbles)
        this.bubbles = loadBubbles(
          this.userId,
          this.positionsScope,
          this.instanceAdmin &&
            this.fullscreen &&
            this.positionsScope !== 'inventory'
        );
    }
    if (changed.has('graph'))
      for (const node of this.graph?.nodes ?? [])
        if (node.type === 'user') this.knownPeople.set(node.id, node);
    if (changed.has('users') && changed.get('users') !== undefined) {
      // Like a window change: the seeded history and totals belong to the
      // old scope. Kept, they showed other people's work in a scoped map.
      this.codeController?.abort();
      this.usage = null;
      this.codeActivity = null;
      this.story.clear();
      this.flowModels.clear();
      this.filterCache = undefined;
    }
    this.dropHiddenSelection();
    // The window's history: every issue and PR active in it, not only those
    // that move while the map is open.
    if (
      changed.has('codeActivity') &&
      this.instanceAdmin &&
      this.story.seedWork(this.codeActivity)
    )
      this.storyVersion++;
    if (
      changed.has('graph') ||
      changed.has('codeActivity') ||
      changed.has('storyVersion')
    ) {
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
        changed.has('users') ||
        changed.has('live'))
    )
      this.scheduleCodeLoad(
        changed.has('activityWindow') || changed.has('users') ? 0 : 400
      );
  }

  protected updated(changed: Map<string, unknown>): void {
    this.observeStage();
    if (this.pendingMessages.length) {
      const notes = this.pendingMessages;
      this.pendingMessages = [];
      this.send(notes);
    }
    if (changed.has('fullscreen')) {
      if (this.fullscreen && this.instanceAdmin)
        this.bubbles = loadBubbles(
          this.userId,
          this.positionsScope,
          this.positionsScope !== 'inventory'
        );
      if (this.fullscreen) {
        this.previousBodyOverflow ??= document.body.style.overflow;
        document.body.style.overflow = 'hidden';
      } else if (this.previousBodyOverflow !== null) {
        document.body.style.overflow = this.previousBodyOverflow;
        this.previousBodyOverflow = null;
      }
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
    if (changed.has('activityWindow')) this.syncStoryTimer();
    if (changed.has('live') || changed.has('instanceAdmin')) {
      this.syncActivitySubscriptions();
      this.syncCodeSubscription();
    }
    // Code activity lands after the graph: fit again so the flows and work
    // it adds are in view, unless the user arranged or moved the map.
    if (
      changed.has('codeActivity') &&
      !changed.get('codeActivity') &&
      this.codeActivity &&
      !this.layoutOwned &&
      Date.now() - this.userMovedAt >= HANDS_OFF_MS
    )
      this.refit = true;
    if (this.refit && this.lastLayout) {
      this.refit = false;
      this.recenter = false;
      this.fit();
    } else if (this.recenter && this.spatial) {
      this.recenter = false;
      this.follow(this.joined);
      this.joined = false;
    } else if (!this.camera) this.applyTransform();
    // New toasts are measured and placed before they are painted.
    if (changed.has('toasts')) this.positionToasts();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribes.forEach((unsubscribe) => unsubscribe());
    this.unsubscribes = [];
    this.codeUnsubscribe?.();
    this.codeUnsubscribe = undefined;
    this.stageObserver?.disconnect();
    this.observedStage = undefined;
    if (this.resetTimer) clearTimeout(this.resetTimer);
    this.activity.stop();
    this.lights.clear();
    this.bubbleLights.clear();
    this.eventLog.clear();
    if (this.storyTimer) clearInterval(this.storyTimer);
    this.storyTimer = undefined;
    this.story.clear();
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.codeController?.abort();
    if (this.codeTimer !== undefined) clearTimeout(this.codeTimer);
    this.codeTimer = undefined;
    if (this.refreshTimer !== undefined) clearInterval(this.refreshTimer);
    if (this.staleTimer !== undefined) clearTimeout(this.staleTimer);
    this.refreshTimer = this.staleTimer = undefined;
    window.removeEventListener('keydown', this.onWindowKey);
    if (this.previousBodyOverflow !== null) {
      document.body.style.overflow = this.previousBodyOverflow;
      this.previousBodyOverflow = null;
    }
  }

  // The Agents canvas uses the same transformed Lit nodes, SVG links and
  // pointer-centred zoom. Keep the map keyboard-scrollable via its controls.
  private zoom(factor: number, clientX?: number, clientY?: number): void {
    const bounds = this.shadowRoot
      ?.querySelector('.stage')
      ?.getBoundingClientRect();
    if (!bounds) return;
    this.handMove();
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
  fit(): void {
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
      this.tween = undefined;
      this.userMovedAt = -Infinity;
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
    // Where the wheel scrolls the page, a lane past the bottom cannot be
    // reached (B-08); flows under the agents make that lane tall (pass 4).
    if (!this.wheelPans && layout.height)
      this.scale = Math.max(
        MIN_CONTAINED_FIT_SCALE,
        Math.min(this.scale, (bounds.height - 16 - 64) / layout.height)
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
  private joined = false;
  private userMovedAt = -Infinity;
  private glidePoints?: FleetMapPoints;

  /**
   * Whether a plain wheel pans the canvas. On the Overview card it scrolls
   * the page instead, so whatever a fit leaves outside the stage is reached
   * only by dragging, which nothing on screen suggests.
   */
  private get wheelPans(): boolean {
    return this.wheelMode !== 'page' || this.fullscreen;
  }

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
    const left = Math.min(...boxes.map((b) => b.x));
    const top = Math.min(...boxes.map((b) => b.y));
    const width = Math.max(...boxes.map((b) => b.x + b.w)) - left;
    const height = Math.max(...boxes.map((b) => b.y + b.h)) - top;
    const availableHeight = Math.max(1, bounds.height - 72);
    const natural = Math.min(
      1.25,
      (room - 48) / Math.max(1, width),
      availableHeight / Math.max(1, height)
    );
    const floor = this.wheelPans
      ? MIN_SPATIAL_FIT_SCALE
      : MIN_CONTAINED_FIT_SCALE;
    const s = Math.max(floor, natural);
    return {
      x: (natural < floor ? 24 : (room - width * s) / 2) - left * s,
      y:
        (natural < floor ? 24 : 24 + (availableHeight - height * s) / 2) -
        top * s,
      s,
    };
  }

  /**
   * The user moved the view by hand (pan, scroll, zoom, arrow keys): stop
   * any glide and stop following the fleet for a while.
   */
  private handMove(): void {
    this.userMovedAt = Date.now();
    this.camera = undefined;
  }

  private dragSnapshot: FleetMapPoints | null = null;

  /** Glide the view to the fleet's new centre of gravity. */
  private follow(joined = false): void {
    const layout = this.lastLayout;
    if (
      this.layoutOwned ||
      !layout ||
      Date.now() - this.userMovedAt < HANDS_OFF_MS
    )
      return;
    const boxes = [...layout.positions.values()];
    if (
      !joined &&
      boxes.length &&
      boxes.every(
        (p) =>
          p.x * this.scale + this.translateX >= 16 &&
          p.y * this.scale + this.translateY >= 16 &&
          (p.x + p.w) * this.scale + this.translateX <=
            this.viewport.width - 16 &&
          (p.y + p.h) * this.scale + this.translateY <=
            this.viewport.height - 48
      )
    )
      return;
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
      else if (previous && !samePositions(previous, target)) {
        this.recenter = true;
        // Something joined an arrangement the user does not own: fit the
        // grown picture even when the newcomer landed inside the old view.
        if (
          !this.layoutOwned &&
          [...target.positions.keys()].some((id) => !previous.positions.has(id))
        )
          this.joined = true;
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
              // A machine box grows and shrinks as agents come and go.
              h: Math.round(from.h + (to.h - from.h) * e),
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
      let id = target.getAttribute('data-node') || '';
      // Dragging an agent inside a machine moves the machine with it.
      for (const [host, kids] of this.lastLayout.groups)
        if (kids.includes(id)) id = host;
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
      this.handMove();
      // The first drag owns the whole picture, including undragged nodes.
      const all: FleetMapPoints = {};
      for (const [key, p] of this.lastLayout.positions)
        all[key] = { x: p.x, y: p.y, w: p.w };
      this.dragSnapshot = all;
      capture(event, true);
      return;
    }
    if ((event.target as Element).closest('button')) return;
    this.pan = {
      id: event.pointerId,
      x: event.clientX - this.translateX,
      y: event.clientY - this.translateY,
    };
    capture(event, true);
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
          w: this.dragSnapshot?.[drag.node]?.w,
        },
      };
      return;
    }
    if (this.pan?.id !== event.pointerId) return;
    this.handMove();
    this.translateX = event.clientX - this.pan.x;
    this.translateY = event.clientY - this.pan.y;
    this.applyTransform();
  }

  private pointerUp(event: PointerEvent): void {
    const drag = this.drag;
    if (drag?.id === event.pointerId) {
      this.drag = null;
      if (drag.moved)
        this.points = { ...(this.dragSnapshot ?? {}), ...this.points };
      this.dragSnapshot = null;
      capture(event, false);
      if (!drag.moved) return;
      savePositions(this.userId, this.points, this.positionsScope, [
        ...(this.lastLayout?.positions.keys() ?? []),
      ]);
      // Swallow only the click the drop itself produces.
      this.suppressClick = true;
      setTimeout(() => (this.suppressClick = false), 0);
      return;
    }
    if (this.pan?.id !== event.pointerId) return;
    this.pan = null;
    capture(event, false);
  }

  /** Scroll pans, as on a page; pinch or Ctrl/Cmd + scroll zooms. */
  private onWheel(event: WheelEvent): void {
    if (
      this.wheelMode === 'page' &&
      !this.fullscreen &&
      !event.ctrlKey &&
      !event.metaKey
    )
      return;
    event.preventDefault();
    this.handMove();
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
        display: flex;
        flex-direction: column;
        min-height: 0;
        min-width: 0;
        max-width: 100%;
        box-sizing: border-box;
        height: var(--fleet-map-height, 640px);
      }
      .legend-toggle {
        bottom: 12px;
        left: 12px;
        width: 28px;
        height: 28px;
        cursor: pointer;
      }
      .node[data-kind='work'] .label {
        font-size: 12px;
      }
      .node[data-status='approved'] .sub,
      .node[data-status='Merged'] .sub {
        color: var(--sl-color-success-700);
      }
      .node[data-status='changes_requested'] .sub {
        color: var(--sl-color-warning-700);
      }
      list-toolbar.toolbar {
        display: block;
        flex: none;
        margin-bottom: 8px;
      }
      .map-actions {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      sl-select::part(form-control-label) {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip-path: inset(50%);
      }
      /* Each select is as wide as its longest option, caret and clear
         button (DESIGN.md collection pages); text is never clipped. */
      .window-select {
        width: 160px;
      }
      .gap-select {
        width: 136px;
      }
      .gap-select::part(combobox) {
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 16%,
          transparent
        );
        color: var(--sl-color-warning-800);
      }
      .filter-options {
        display: grid;
        gap: 10px;
        padding: 12px;
        min-width: 160px;
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        border-radius: 4px;
      }
      .menu-count {
        padding: 8px 16px;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .filters-menu {
        max-height: calc(100dvh - 100px);
        max-width: min(280px, calc(100vw - 32px));
        overflow: auto;
      }
      .filters-menu[hidden] {
        display: none;
      }
      .filters-popup {
        width: min(280px, calc(100vw - 32px));
      }
      .filters-popup .filters-menu {
        width: 100%;
        box-sizing: border-box;
      }
      .filters-menu sl-menu-item::part(label) {
        overflow: hidden;
        text-overflow: ellipsis;
      }
      /* A select's trigger names its value; long values truncate. */
      .toolbar sl-dropdown > sl-button::part(label) {
        max-width: 160px;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .filter-value {
        max-width: 112px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        margin-inline-end: 4px;
      }
      .users-search {
        padding: 4px 12px 8px;
      }
      .user-option {
        display: inline-flex;
        margin-inline-end: 8px;
      }
      .user-option .glyph {
        width: 24px;
        height: 24px;
      }
      .toolbar sl-button::part(base) {
        padding-inline: 8px;
      }
      .privacy-note {
        width: 230px;
        padding: 8px 16px;
        font-size: var(--console-text-meta);
        white-space: normal;
        color: var(--console-meta-color);
      }
      .reset-notice {
        position: absolute;
        bottom: 52px;
        left: 12px;
        padding: 8px;
        background: var(--console-surface-raised, var(--console-surface));
        z-index: 4;
        font-size: var(--console-text-meta);
      }

      :host([fullscreen]) {
        height: 100dvh;
        max-width: none;
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
        flex: 1 1 0;
        min-height: 0;
        height: auto;
        container-type: size;
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
      .edge[data-edge='resolves'] {
        stroke-dasharray: none;
      }
      .edge[data-edge='mentions'] {
        stroke-dasharray: 5 4;
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
      .column-title .amp {
        font-weight: 500;
      }
      /* Flows under the agents in one lane: a hairline and a quiet label. */
      .lane-subheader {
        height: 28px;
        align-items: center;
        border-top: 1px solid var(--console-hairline);
        box-sizing: border-box;
        padding-top: 4px;
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
      .glyph .avatar {
        width: 24px;
        height: 24px;
        object-fit: cover;
        border-radius: 50%;
      }
      .glyph.person-mark {
        width: 24px;
        height: 24px;
        flex: 0 0 24px;
      }
      .glyph .initials {
        font-size: 11px;
        font-weight: 600;
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
      /* Tracker labels are tags, not states: an icon and the word. The row
         is budgeted: status, tags and the +N count shrink and ellipsize
         inside the chip; the count never shrinks. */
      .node .sub.tagged {
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
      }
      .node .sub .sub-text,
      .node .sub .tag-text {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .node .sub .sub-text {
        flex: 0 1 auto;
      }
      .node .sub .tag {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        flex: 0 1 auto;
        min-width: 0;
        max-width: 72px;
      }
      .node .sub .tag.more {
        flex: none;
      }
      .node .sub .tag sl-icon {
        font-size: 11px;
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
      .canvas-tools sl-icon {
        font-size: 14px;
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
      .legend.raised {
        bottom: 48px;
        right: 12px;
        flex-wrap: wrap;
        row-gap: 4px;
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
      .panel-heading {
        display: flex;
        align-items: center;
        gap: 10px;
        padding-right: 20px;
      }
      .panel-heading > div {
        min-width: 0;
      }
      .panel-heading h3 {
        overflow-wrap: anywhere;
      }
      .panel-heading .glyph {
        display: grid;
        place-items: center;
        flex: 0 0 28px;
      }
      .panel .work-title {
        font-size: var(--console-text-body);
        overflow-wrap: anywhere;
      }
      .usage-metrics {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 6px 12px;
        font-size: var(--console-text-meta);
      }
      .usage-metrics dt {
        color: var(--console-meta-color);
      }
      .usage-metrics dd {
        margin: 0;
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .panel .usage {
        font-variant-numeric: tabular-nums;
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
        .legend:not(.raised) {
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
      /* The node's mark: a product, model-family or provider logo. */
      .glyph .mark {
        width: 16px;
        height: 16px;
        object-fit: contain;
      }
      .node.child .glyph .mark {
        width: 14px;
        height: 14px;
      }
      .glyph .mark.mask {
        display: block;
        background: currentColor;
        -webkit-mask: var(--mark) center / contain no-repeat;
        mask: var(--mark) center / contain no-repeat;
      }
      /* A short note beside a node where traffic started (Bubbles on). */
      .bubble {
        position: absolute;
        z-index: 4;
        display: flex;
        align-items: center;
        gap: 6px;
        box-sizing: border-box;
        min-height: 26px;
        padding: 4px 8px;
        font-size: 12px;
        font-weight: 500;
        line-height: 18px;
        color: var(--console-body-color);
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        border-radius: 4px;
        pointer-events: none;
        animation: bubble-in 150ms ease-out;
      }
      .bubble span {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .bubble sl-icon {
        flex: none;
        font-size: 14px;
        color: var(--console-meta-color);
      }
      .bubble.speak-approval {
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 16%,
          var(--console-surface)
        );
      }
      .tool-count {
        position: absolute;
        right: 8px;
        bottom: -8px;
        display: flex;
        align-items: center;
        gap: 3px;
        font-size: 11px;
        padding: 1px 5px;
        background: var(--console-surface);
        border: 1px solid var(--console-hairline);
        border-radius: 4px;
        color: var(--console-meta-color);
      }
      .recent-stats {
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
      }
      .recent-events {
        list-style: none;
        padding: 0;
        font-size: var(--console-text-meta);
      }
      .recent-events li {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        padding: 8px 0;
        border-bottom: 1px solid var(--console-hairline);
        overflow-wrap: anywhere;
      }
      .recent-events sl-icon {
        flex: none;
        margin-top: 3px;
      }
      .recent-events time {
        display: block;
        color: var(--console-meta-color);
      }
      @keyframes bubble-in {
        from {
          opacity: 0;
        }
        to {
          opacity: 1;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .bubble {
          animation: none;
        }
      }
      :host([reduced-motion]) .bubble {
        animation: none;
      }
      .overlay[hidden] {
        display: none;
      }
      :host([bare]) .stage {
        border: none;
      }
      /* Above the zoom controls, clear of the column headers. */
      /* The stage's top right is the toasts' corner, but a node may be
         drawn there: each toast is placed by positionToasts() in the free
         spot nearest the corner (B-08), hidden until it has one. */
      .toasts {
        inset: 0;
        display: block;
        pointer-events: none;
      }
      .toast {
        position: absolute;
        top: 0;
        left: 0;
        visibility: hidden;
        display: flex;
        align-items: center;
        gap: 6px;
        /* Its size never depends on where it was last put: a toast measured
           at one spot and moved left used to grow over a node. */
        width: max-content;
        max-width: min(320px, calc(100% - 24px));
        box-sizing: border-box;
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
      this.codeActivity,
      this.storyVersion,
      this.activityWindow,
      this.types,
      this.users,
      this.gapsOnly,
      this.includeIdle,
    ];
    const cached = this.filterCache;
    if (cached && cached.key.every((value, index) => value === key[index]))
      return cached.view;
    const minutes = windowMinutes(this.activityWindow);
    // A server that applied the window says so; otherwise apply it here.
    const source =
      this.graph && minutes && this.graph.active_within_minutes !== minutes
        ? activeGraph(
            this.graph,
            Date.now() - minutes * 60_000,
            this.includeIdle
          )
        : (this.graph ?? EMPTY);
    const activeSource = this.includeIdle
      ? source
      : { ...source, nodes: source.nodes.filter((node) => !node.idle) };
    const merged = this.withFlowModels(
      withWorkRelations(
        this.story.graph(
          withCodeFlows(activeSource, this.codeActivity),
          minutes
        ),
        this.codeActivity
      )
    );
    const view = filterGraph(
      this.serverScoped
        ? merged
        : scopeToUsers(
            merged,
            new Set(this.users),
            this.codeActivity?.work ?? []
          ),
      { types: this.types, gapsOnly: this.gapsOnly }
    );
    this.filterCache = { key, view };
    return view;
  }

  /** Whether the loaded map is already scoped to exactly these people. */
  private get serverScoped(): boolean {
    const applied = this.graph?.users;
    if (!this.users.length) return true;
    if (!applied) return false;
    const wanted = new Set(this.users);
    return (
      applied.length === wanted.size && applied.every((id) => wanted.has(id))
    );
  }

  /** User resource ids for the server's `users=` param. */
  private get userResourceIds(): string[] {
    return this.users.map(
      (id) => this.knownPeople.get(id)?.resource_id || id.replace(/^user:/, '')
    );
  }

  private setUsers(users: string[]): void {
    // The server scopes at most this many people; never drop one silently.
    if (new Set(users).size > FLEET_MAP_MAX_USERS) return;
    const next = [...new Set(users)].sort();
    if (next.join(',') === [...this.users].sort().join(',')) return;
    this.users = next;
    this.dispatchEvent(
      new CustomEvent('fleet-map-users-change', {
        detail: { users: next, userIds: this.userResourceIds },
        bubbles: true,
        composed: true,
      })
    );
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
      this.handMove();
      event.preventDefault();
      this.applyTransform();
    }
  };

  private resetFilters(): void {
    this.setUsers([]);
    this.gapsOnly = false;
    this.focusGap = '';
    this.query = '';
    this.types = new Set(DEFAULT_TYPES);
    saveTypes(this.userId, this.positionsScope, this.types);
    if (this.includeIdle) this.setIncludeIdle(false);
  }

  private setIncludeIdle(includeIdle: boolean): void {
    this.includeIdle = includeIdle;
    this.dispatchEvent(
      new CustomEvent('fleet-map-idle-change', {
        detail: { includeIdle },
        bubbles: true,
        composed: true,
      })
    );
  }

  private runMapAction(event: Event, action: () => void): void {
    // Dismiss before the action can rerender or disable its menu item.
    void (event.currentTarget as HTMLElement).closest('sl-dropdown')?.hide();
    action.call(this);
  }

  /**
   * A short or narrow stage folds the legend behind its ? button: inline it
   * would run under the zoom controls (it needs about 1000px beside them).
   */
  private get compactLegend(): boolean {
    return (
      this.viewport.height < 600 ||
      this.viewport.width < LEGEND_INLINE_MIN_WIDTH
    );
  }

  private get condensedToolbar(): boolean {
    return this.viewport.width < 960 || window.innerWidth < 1280;
  }

  private setFilterPage(page: string): void {
    const previous = this.filterPage;
    this.filterPage = page;
    void this.updateComplete.then(() => {
      const selector = page
        ? `[data-filter-page="${page}"] sl-menu-item`
        : `[data-testid="${previous}"]`;
      this.shadowRoot?.querySelector<HTMLElement>(selector)?.focus();
    });
  }

  /** Folds an expanded lane or sub-group back to its first rows. */
  private renderShowFewer(type: FleetMapColumn, count: number) {
    if (!this.expanded.has(type) || count <= FLEET_MAP_CAP) return nothing;
    return html`<button
      type="button"
      class="reset"
      data-show-fewer=${type}
      @click=${() => (this.expanded = this.toggle(this.expanded, type))}
    >
      Show fewer
    </button>`;
  }

  /** The Show select's value: every kind, one or two by name, or a count. */
  private showValue(): string {
    const shown = FLEET_MAP_TYPES.filter((type) => this.types.has(type));
    let value =
      shown.length === FLEET_MAP_TYPES.length
        ? 'Everything'
        : shown.length === DEFAULT_TYPES.length &&
            DEFAULT_TYPES.every((type) => this.types.has(type))
          ? 'All resources'
          : !shown.length
            ? 'Nothing'
            : shown.length <= 2
              ? shown.map((type) => TYPE_TOGGLE_LABEL[type]).join(', ')
              : `${shown.length} kinds`;
    if (this.includeIdle) value += ', idle';
    if (this.gapsOnly || this.focusGap) value += ', gaps';
    return value;
  }

  /** The Users select's value: everyone, one name, or a count. */
  private usersValue(): string {
    if (!this.users.length) return 'All users';
    if (this.users.length === 1)
      return this.knownPeople.get(this.users[0])?.label ?? '1 user';
    return `${this.users.length} users`;
  }

  private renderGapItems(
    gaps: Map<FleetMapGapCode, { label: string; count: number }>
  ) {
    if (!gaps.size) return nothing;
    return html`<sl-divider></sl-divider>
      <sl-menu-label data-testid="fleet-map-gap-select">Gaps</sl-menu-label>
      <sl-menu-item
        type="checkbox"
        .checked=${!this.gapsOnly && !this.focusGap}
        @click=${() => {
          this.gapsOnly = false;
          this.focusGap = '';
        }}
        >Any coverage</sl-menu-item
      >
      <sl-menu-item
        type="checkbox"
        data-gap="all"
        .checked=${this.gapsOnly}
        @click=${() => {
          this.gapsOnly = !this.gapsOnly;
          this.focusGap = '';
        }}
        >All gaps</sl-menu-item
      >
      ${[...gaps].map(
        ([code, gap]) =>
          html`<sl-menu-item
            type="checkbox"
            data-gap=${code}
            .checked=${this.focusGap === code}
            @click=${() => {
              this.gapsOnly = false;
              this.focusGap = this.focusGap === code ? '' : code;
            }}
            >${gap.count} ${gap.label}</sl-menu-item
          >`
      )}`;
  }

  /**
   * The Users select: everyone, or any set of people (avatar and name). A
   * long list gets a search field; chosen people always stay listed.
   */
  private renderUserItems() {
    const people = [...this.knownPeople.values()].sort((a, b) =>
      a.label.localeCompare(b.label)
    );
    const chosen = new Set(this.users);
    const query = this.usersQuery.trim().toLowerCase();
    const listed = people.filter(
      (node) =>
        !query ||
        chosen.has(node.id) ||
        `${node.label} ${node.detail ?? ''}`.toLowerCase().includes(query)
    );
    return html`${
        people.length >= USERS_SEARCH_MIN
          ? html`<div class="users-search">
              <sl-input
                size="small"
                type="search"
                clearable
                placeholder="Find a user"
                aria-label="Find a user"
                data-testid="fleet-map-users-search"
                .value=${this.usersQuery}
                @keydown=${(event: KeyboardEvent) => {
                  // The menu's type-to-select would take focus from the field.
                  if (event.key !== 'Escape') event.stopPropagation();
                }}
                @sl-input=${(event: Event) => {
                  this.usersQuery = (event.target as HTMLInputElement).value;
                }}
                @sl-clear=${() => {
                  this.usersQuery = '';
                }}
              ></sl-input>
            </div>`
          : nothing
      }
      <sl-menu-item
        type="checkbox"
        data-user="all"
        .checked=${!chosen.size}
        @click=${() => this.setUsers([])}
        >All users</sl-menu-item
      >
      ${
        chosen.size >= FLEET_MAP_MAX_USERS
          ? html`<sl-menu-label data-testid="fleet-map-users-limit"
              >Up to ${FLEET_MAP_MAX_USERS} users at a time</sl-menu-label
            >`
          : nothing
      }
      ${listed.map(
        (node) =>
          html`<sl-menu-item
            type="checkbox"
            data-user=${node.id}
            .checked=${chosen.has(node.id)}
            ?disabled=${!chosen.has(node.id) && chosen.size >= FLEET_MAP_MAX_USERS}
            @click=${() =>
              this.setUsers(
                chosen.has(node.id)
                  ? this.users.filter((id) => id !== node.id)
                  : [...this.users, node.id]
              )}
            ><span slot="prefix" class="user-option"
              >${this.renderGlyph(node, new Map())}</span
            >${node.label}</sl-menu-item
          >`
      )}
      ${
        query && !listed.length
          ? html`<sl-menu-item disabled>No user matches</sl-menu-item>`
          : nothing
      }`;
  }

  private renderToolbar(
    graph: FleetMapGraph,
    view: FleetMapGraph,
    hits: Set<string>
  ) {
    const narrow = this.condensedToolbar;
    const active = view.nodes.filter(
      (node) => node.type !== 'cluster' && !node.idle
    ).length;
    const idle = view.nodes.filter((node) => node.idle).length;
    const gaps = new Map<FleetMapGapCode, { label: string; count: number }>();
    const visibleIds = new Set(view.nodes.map((node) => node.id));
    for (const node of graph.nodes)
      for (const gap of visibleIds.has(node.id) ? nodeGaps(node) : []) {
        gaps.set(gap.code, {
          label: gap.label,
          count: (gaps.get(gap.code)?.count ?? 0) + 1,
        });
      }
    const changed =
      this.users.length ||
      this.gapsOnly ||
      this.focusGap ||
      this.query ||
      this.includeIdle ||
      this.types.size !== DEFAULT_TYPES.length ||
      this.types.has('provider_account');
    const count = html`<span
      class="count"
      aria-live="polite"
      title="${this.query ? `${hits.size} search matches. ` : ''}${idle} idle configured resources shown separately. ${graph.decommissioned_agents ?? 0} decommissioned agents not shown. ${graph.clusters.length} summary groups. Counts describe the loaded graph."
      >${active} active in this
      window${
        this.users.length
          ? ` for ${this.users.length} ${this.users.length === 1 ? 'user' : 'users'}`
          : ''
      }</span
    >`;
    const show = html`${FLEET_MAP_TYPES.map(
        (type) =>
          html`<sl-menu-item
            type="checkbox"
            data-type=${type}
            .checked=${this.types.has(type)}
            @click=${() => {
              this.types = this.toggle(this.types, type);
              saveTypes(this.userId, this.positionsScope, this.types);
            }}
            >${TYPE_TOGGLE_LABEL[type]}</sl-menu-item
          >`
      )}
      <sl-divider></sl-divider>
      <sl-menu-item
        type="checkbox"
        data-testid="fleet-map-idle"
        .checked=${this.includeIdle}
        @click=${() => this.setIncludeIdle(!this.includeIdle)}
        >Idle (configured)</sl-menu-item
      >${this.renderGapItems(gaps)}`;
    const owners = this.renderUserItems();
    const windows = html`${FLEET_MAP_WINDOWS.map(
      (item) =>
        html`<sl-menu-item
          type="checkbox"
          data-window=${item.value}
          .checked=${this.activityWindow === item.value}
          @click=${() => this.setWindow(item.value)}
          >${item.label}</sl-menu-item
        >`
    )}`;
    // Each select names its current value, never its own label (pass 4).
    const filters = [
      {
        label: 'Show',
        id: 'fleet-map-show',
        value: this.showValue(),
        content: show,
      },
      {
        label: 'Users',
        id: 'fleet-map-users',
        value: this.usersValue(),
        content: owners,
      },
      {
        label: 'Window',
        id: 'fleet-map-window',
        value: windowLabel(this.activityWindow),
        content: windows,
      },
    ];
    const more = html`${
        this.instanceAdmin
          ? html`<sl-menu-item
                type="checkbox"
                .checked=${this.bubbles}
                data-testid="fleet-map-bubbles"
                @click=${() => {
                  this.bubbles = !this.bubbles;
                  saveBubbles(this.userId, this.bubbles, this.positionsScope);
                }}
                >Bubbles</sl-menu-item
              >
              <sl-menu-item
                data-testid="fleet-map-reset-layout"
                @click=${(event: Event) => this.runMapAction(event, this.resetLayout)}
                >Reset layout</sl-menu-item
              >`
          : nothing
      }
      <sl-menu-item
        ?disabled=${!changed}
        data-testid="fleet-map-reset-filters"
        @click=${(event: Event) => this.runMapAction(event, this.resetFilters)}
        >Reset filters</sl-menu-item
      >
      <sl-menu-item
        data-testid="fleet-map-fullscreen-menu"
        @click=${(event: Event) => this.runMapAction(event, this.toggleFullscreen)}
        >${this.fullscreen ? 'Exit full screen' : 'Full screen'}</sl-menu-item
      >
      ${this.instanceAdmin ? html`<div class="privacy-note">Shows tool names, approval commands, PR titles and token counts. Never prompts or replies.</div>` : nothing}`;
    return html`<list-toolbar
      class="toolbar"
      compact
      .condensed=${narrow}
      .search=${this.query}
      searchLabel="Find in map"
      searchPlaceholder="Find in map"
      .views=${[]}
      @search-change=${(e: CustomEvent<{ value: string }>) => {
        this.query = e.detail.value;
      }}
      @keydown=${(e: KeyboardEvent) => {
        if (
          e.key === 'Enter' &&
          e.composedPath().some((n) => n instanceof HTMLInputElement) &&
          hits.size
        )
          this.select([...hits][0]);
        if (e.key === 'Escape') {
          const popup = e
            .composedPath()
            .find(
              (target) =>
                target instanceof HTMLElement &&
                target.localName === 'sl-dropdown'
            ) as HTMLElementTagNameMap['sl-dropdown'] | undefined;
          if (popup?.open) {
            e.preventDefault();
            e.stopPropagation();
            void popup.hide();
          } else this.query = '';
        }
      }}
    >
      ${
        narrow
          ? html`<sl-dropdown
              hoist
              stay-open-on-select
              placement="bottom-start"
              @sl-after-hide=${() => {
                this.filterPage = '';
              }}
            >
              <sl-button
                slot="trigger"
                size="small"
                caret
                data-testid="fleet-map-filters"
                >Filters</sl-button
              >
              <div class="filters-popup">
                <sl-menu
                  class="filters-menu"
                  ?hidden=${!!this.filterPage}
                  aria-label="Map filters"
                >
                  <div class="menu-count">${count}</div>
                  ${filters.map((filter) => html`<sl-menu-item data-testid=${filter.id} aria-label="${filter.label}: ${filter.value}" @click=${() => this.setFilterPage(filter.id)}>${filter.label}<span slot="suffix" class="filter-value">${filter.value}</span><sl-icon slot="suffix" name="chevron-right"></sl-icon></sl-menu-item>`)}
                  <sl-divider></sl-divider>${more}
                </sl-menu>
                ${filters.map(
                  (filter) =>
                    html`<sl-menu
                      class="filters-menu"
                      data-filter-page=${filter.id}
                      ?hidden=${this.filterPage !== filter.id}
                      aria-label=${filter.label}
                    >
                      <sl-menu-item
                        data-testid="fleet-map-filter-back"
                        @click=${() => this.setFilterPage('')}
                        ><sl-icon slot="prefix" name="chevron-left"></sl-icon
                        >Filters</sl-menu-item
                      >
                      <sl-menu-label>${filter.label}</sl-menu-label
                      >${filter.content}
                    </sl-menu>`
                )}
              </div>
            </sl-dropdown>`
          : html`${filters.map(
                (filter) =>
                  html`<sl-dropdown
                    hoist
                    stay-open-on-select
                    placement="bottom-start"
                  >
                    <sl-button
                      slot="trigger"
                      size="small"
                      caret
                      data-testid=${filter.id}
                      aria-label="${filter.label}: ${filter.value}"
                      title="${filter.label}: ${filter.value}"
                      >${filter.value}</sl-button
                    >
                    <sl-menu class="filters-menu" aria-label=${filter.label}
                      >${filter.content}</sl-menu
                    >
                  </sl-dropdown>`
              )}<span slot="count">${count}</span>`
      }
      <div slot="actions" class="map-actions">
        ${
          this.instanceAdmin && !this.spatialOnly
            ? html`<sl-button-group
                label="Arrangement"
                data-testid="fleet-map-arrangement"
              >
                ${ARRANGEMENTS.map(
                  (value) =>
                    html`<sl-tooltip
                      content=${ARRANGEMENT_LABEL[value]}
                      placement="bottom"
                      hoist
                      ><sl-button
                        size="small"
                        data-arrangement=${value}
                        aria-label=${ARRANGEMENT_LABEL[value]}
                        variant=${this.arrangement === value ? 'primary' : 'default'}
                        aria-pressed=${this.arrangement === value ? 'true' : 'false'}
                        @click=${() => this.setArrangement(value)}
                        ><sl-icon
                          name=${ARRANGEMENT_ICON[value]}
                          aria-hidden="true"
                        ></sl-icon></sl-button
                    ></sl-tooltip>`
                )}
              </sl-button-group>`
            : nothing
        }
        ${narrow ? nothing : html`<sl-dropdown hoist placement="bottom-end"><sl-icon-button slot="trigger" name="three-dots-vertical" label="More map options"></sl-icon-button><sl-menu>${more}</sl-menu></sl-dropdown>`}
      </div>
    </list-toolbar>`;
  }

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
      // Agents only: a flow's observed model line is not an agent.
      const agents = new Set(
        view.edges
          .filter(
            (edge) =>
              edge.kind === 'uses_model' &&
              edge.target === node.id &&
              byId.get(edge.source)?.type !== 'flow'
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
    if (node.type === 'provider') {
      // The box holds accounts and, since pass 4, models: name each kind.
      const held = groups.get(node.id) ?? [];
      const count = (type: string, word: string) => {
        const n = held.filter((id) => byId.get(id)?.type === type).length;
        return n ? `${n} ${word}${n === 1 ? '' : 's'}` : '';
      };
      return (
        [count('model', 'model'), count('provider_account', 'account')]
          .filter(Boolean)
          .join(' · ') || 'Provider'
      );
    }
    if (node.type === 'device') {
      // A machine holds runners (both arrangements) and, on the spatial
      // canvas, agents too: count each kind by its own name.
      const held = groups.get(node.id) ?? [];
      const count = (type: string, word: string) => {
        const n = held.filter((id) => byId.get(id)?.type === type).length;
        return n ? `${n} ${word}${n === 1 ? '' : 's'}` : '';
      };
      return (
        [node.status, count('agent', 'agent'), count('runner', 'runner')]
          .filter(Boolean)
          .join(' · ') || 'Device'
      );
    }
    if (node.type === 'runner') return node.status || 'Runner';
    if (node.type === 'flow') return node.status || 'Flow';
    if (node.type === 'work' && node.status === 'Merged') return 'Merged';
    if (node.type === 'work')
      return (
        node.review_state?.replace(/_/g, ' ') || node.status || 'State unknown'
      );
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
    const tags = node.type === 'work' ? (node.labels ?? []).join(', ') : '';
    const height = isHeader ? FLEET_MAP_NODE_HEIGHT : position.h;
    const entering = presenceStyle(this.presence, node.id, Date.now());
    const button = html`<button
      type="button"
      class=${classes}
      data-node=${node.id}
      data-kind=${node.type}
      data-status=${node.status === 'Merged' ? 'Merged' : node.review_state || node.status || nothing}
      data-presence=${node.id}
      style="left:${position.x}px;top:${position.y}px;width:${position.w}px;height:${height}px${
        entering
          ? `;opacity:${entering.opacity.toFixed(3)};transform:scale(${entering.scale.toFixed(3)})`
          : ''
      }"
      aria-pressed=${node.id === this.selected ? 'true' : 'false'}
      aria-label="${FLEET_MAP_TYPE_LABEL[node.type]} ${node.label}${
        tags ? `, labels ${tags}` : ''
      }${gaps.length ? `, ${gaps.map((gap) => gap.label).join(', ')}` : ''}"
      title="${node.label}${node.detail ? `\n${node.detail}` : ''}${
        tags ? `\nLabels: ${tags}` : ''
      }${gaps.length ? `\n${gaps.map((gap) => gap.label).join('\n')}` : ''}"
      @click=${() => {
        if (this.suppressClick) {
          this.suppressClick = false;
          return;
        }
        this.select(node.id);
      }}
    >
      ${this.renderGlyph(node, context.byId)}
      ${child && node.type !== 'agent' ? nothing : this.renderCode(node)}
      ${this.renderToolCount(node)}
      <span class="text"
        ><span class="label">${label}</span>${
          child && node.type !== 'work'
            ? nothing
            : node.type === 'work' && node.labels?.length
              ? html`<span class="sub tagged"
                  ><span class="sub-text">${sub}</span>${node.labels
                    .slice(0, LABELS_SHOWN)
                    .map(
                      (tag) =>
                        html`<span class="tag" data-label=${tag}
                          ><sl-icon name="tag" aria-hidden="true"></sl-icon
                          ><span class="tag-text">${tag}</span></span
                        >`
                    )}${
                    node.labels.length > LABELS_SHOWN
                      ? html`<span class="tag more" data-labels-more
                          >+${node.labels.length - LABELS_SHOWN}</span
                        >`
                      : nothing
                  }</span
                >`
              : html`<span class="sub ${gaps.length ? 'warn' : ''}"
                  >${sub}</span
                >`
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

  /**
   * The node's mark: an agent's product logo, a model's family mark, a
   * provider's own mark, or the type icon.
   */
  private renderGlyph(node: FleetMapNode, byId: Map<string, FleetMapNode>) {
    if (node.type === 'user') {
      const src = avatarSource(node.avatar_url);
      const initials = personInitials(node.label);
      return html`<span class="glyph person-mark" aria-hidden="true">
        ${
          src && !this.failedAvatars.has(src)
            ? html`<img
                class="avatar"
                src=${src}
                width="24"
                height="24"
                alt=""
                loading="lazy"
                @error=${() => {
                  this.failedAvatars = new Set(
                    [...this.failedAvatars, src].slice(-500)
                  );
                }}
              />`
            : initials
              ? html`<span class="initials">${initials}</span>`
              : html`<sl-icon name="person"></sl-icon>`
        }
      </span>`;
    }
    const provider = node.provider_ids
      .map((id) => byId.get(id)?.label)
      .find(Boolean);
    const mark = nodeMark(node, ICON[node.type], provider ?? '');
    return html`<span class="glyph" data-mark=${mark.name}
      >${
        mark.kind === 'img'
          ? html`<img class="mark" src=${mark.src!} alt="" />`
          : mark.kind === 'mask'
            ? html`<span
                class="mark mask"
                style="--mark:url('${mark.src}')"
              ></span>`
            : mark.src
              ? html`<sl-icon src=${mark.src}></sl-icon>`
              : html`<sl-icon name=${mark.icon!}></sl-icon>`
      }</span
    >`;
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
          html`<span
            class="code-badge ${action}"
            data-action=${action}
            title="${counts[action]} ${CODE_BADGES.find(([a]) => a === action)?.[2] ?? action}"
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
    // Providers of fading models are still on the map (or fading too).
    const byId = new Map(
      [
        ...this.shown().nodes,
        ...[...this.presence.leaving.values()].map((item) => item.node),
      ].map((node) => [node.id, node])
    );
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
        ${this.renderGlyph(node, byId)}
        <span class="text"
          ><span class="label">${displayLabel(node)}</span></span
        >
      </div>`;
    });
  }

  /** Four bounded event notes, positioned in screen space so zoom stays legible. */
  private renderBubbles(positions: Map<string, FleetMapPosition>) {
    if (!this.bubbles || !this.instanceAdmin) return nothing;
    const lit = this.bubbleLights
      .active(Date.now())
      .filter((item) => item.label && positions.has(item.node))
      .slice(-4)
      .reverse();
    const placed = this.placeNotes(
      positions,
      lit.map((item) => ({
        node: item.node,
        w: Math.min(280, Math.max(140, item.label.length * 7 + 40)),
        h: 26,
      }))
    );
    return placed.map((spot) => {
      const item = lit.find((entry) => entry.node === spot.node)!;
      return html`<div
        class="bubble speak-${item.kind}"
        data-bubble=${item.node}
        data-side=${spot.side}
        title=${item.label}
        style="left:${spot.x}px;top:${spot.y}px;max-width:${spot.w}px;animation:${this.motionOff ? 'none' : 'bubble-enter 150ms ease-out'}"
      >
        <sl-icon
          name=${iconName(item.icon ?? 'activity')}
          src=${iconSrc(item.icon)}
          aria-hidden="true"
        ></sl-icon
        ><span>${item.label}</span>
      </div>`;
    });
  }

  /**
   * Screen spots for notes: beside their node, never on another node. In
   * Columns the stretched row gap under a node is the first choice; on the
   * spatial canvas the side away from the stage edge.
   */
  private placeNotes(
    positions: Map<string, FleetMapPosition>,
    requests: BubbleRequest[]
  ): PlacedBubble[] {
    const s = this.scale;
    const nodes = new Map<string, BubbleRect>();
    for (const [id, p] of positions)
      nodes.set(id, {
        x: this.translateX + p.x * s,
        y: this.translateY + p.y * s,
        w: p.w * s,
        h: p.h * s,
      });
    const layout = this.lastLayout;
    const groups = layout?.groups ?? new Map<string, string[]>();
    let pitch = Number.POSITIVE_INFINITY;
    if (!this.spatial && layout?.columns.length) {
      // Column headers are part of the picture: a note never covers one.
      for (const column of layout.columns)
        nodes.set(`header:${column.type}`, {
          x: this.translateX + column.x * s,
          y: this.translateY,
          w: FLEET_MAP_NODE_WIDTH * s,
          h: FLEET_MAP_HEADER * s,
        });
      // A note fits its column and gutter, so it can sit under its node.
      if (layout.columns.length > 1)
        pitch = (layout.columns[1].x - layout.columns[0].x - 12) * s;
    }
    const panel =
      this.selected && !window.matchMedia('(max-width: 900px)').matches
        ? 364
        : 0;
    return placeBubbles(
      requests.map((request) => ({
        ...request,
        w: Math.min(request.w, Math.max(140, pitch)),
      })),
      {
        nodes,
        bounds: {
          x: 12,
          y: 12,
          w: Math.max(0, this.viewport.width - 24 - panel),
          h: Math.max(0, this.viewport.height - 64),
        },
        sides: this.spatial
          ? ['right', 'left', 'below', 'above']
          : ['below', 'above', 'right', 'left'],
        related: (node, other) =>
          !!groups.get(other)?.includes(node) ||
          !!groups.get(node)?.includes(other),
      }
    );
  }

  /** Screen boxes of the drawn nodes, in stage coordinates. */
  private screenBoxes(positions: Map<string, FleetMapPosition>): BubbleRect[] {
    const s = this.scale;
    return [...positions.values()].map((p) => ({
      x: this.translateX + p.x * s,
      y: this.translateY + p.y * s,
      w: p.w * s,
      h: p.h * s,
    }));
  }

  /** Put each toast in the free spot nearest the top right (B-08). */
  private positionToasts(): void {
    const els = [
      ...(this.shadowRoot?.querySelectorAll<HTMLElement>('[data-toast]') ?? []),
    ];
    const positions = (
      this.spatial ? (this.shownLayout ?? this.lastLayout) : this.lastLayout
    )?.positions;
    if (!els.length || !positions) return;
    const panel =
      this.selected && !window.matchMedia('(max-width: 900px)').matches
        ? 364
        : 0;
    const nodes = this.screenBoxes(positions);
    // Column headers are part of the picture, as they are for bubbles.
    const layout = this.lastLayout;
    if (!this.spatial && layout?.columns.length)
      for (const column of layout.columns)
        nodes.push({
          x: this.translateX + column.x * this.scale,
          y: this.translateY,
          w: FLEET_MAP_NODE_WIDTH * this.scale,
          h: FLEET_MAP_HEADER * this.scale,
        });
    // The canvas tools sit on the stage too.
    const tools = this.shadowRoot?.querySelector('.canvas-tools');
    const stage = this.shadowRoot?.querySelector('.stage');
    if (tools && stage) {
      const t = tools.getBoundingClientRect();
      const st = stage.getBoundingClientRect();
      nodes.push({
        x: t.left - st.left,
        y: t.top - st.top,
        w: t.width,
        h: t.height,
      });
    }
    // Measure every toast at the same origin, so wrapping cannot change it.
    for (const el of els) {
      el.style.left = '0px';
      el.style.top = '0px';
    }
    const spots = placeCornerNotes(
      els.map((el) => ({ w: el.offsetWidth || 200, h: el.offsetHeight || 26 })),
      {
        nodes,
        bounds: {
          x: 12,
          y: 12,
          w: Math.max(0, this.viewport.width - 24 - panel),
          h: Math.max(0, this.viewport.height - 24),
        },
      }
    );
    els.forEach((el, index) => {
      const spot = spots[index];
      el.style.visibility = spot ? 'visible' : 'hidden';
      if (!spot) return;
      el.style.left = `${spot.x}px`;
      el.style.top = `${spot.y}px`;
    });
  }

  private positionBubbles(): void {
    const els = [
      ...(this.shadowRoot?.querySelectorAll<HTMLElement>('[data-bubble]') ??
        []),
    ];
    const positions = (
      this.spatial ? (this.shownLayout ?? this.lastLayout) : this.lastLayout
    )?.positions;
    if (!els.length || !positions) return;
    const placed = this.placeNotes(
      positions,
      els.map((el) => ({
        node: el.dataset.bubble!,
        w: el.offsetWidth || 160,
        h: el.offsetHeight || 26,
      }))
    );
    for (const el of els) {
      const spot = placed.find((item) => item.node === el.dataset.bubble);
      // No free spot at this zoom: hide the note rather than leave it where
      // it was, possibly on a node, until the next render drops it.
      el.style.visibility = spot ? '' : 'hidden';
      if (!spot) continue;
      el.dataset.side = spot.side;
      el.style.left = `${spot.x}px`;
      el.style.top = `${spot.y}px`;
    }
  }

  private renderToolCount(node: FleetMapNode) {
    if (!this.instanceAdmin) return nothing;
    const count = this.eventLog
      .forNode(node.id, windowMinutes(this.activityWindow))
      .filter((event) => event.kind === 'tool').length;
    if (!count) return nothing;
    return html`<span
      class="tool-count"
      data-tools=${node.id}
      title="${count} tool calls in this window, since you opened the map${this.eventLog.truncated ? ' (bounded sample)' : ''}"
      ><sl-icon name="wrench" aria-hidden="true"></sl-icon>${count}</span
    >`;
  }

  private renderRecent(node: FleetMapNode) {
    if (!this.instanceAdmin) return nothing;
    const events = this.eventLog.forNode(
      node.id,
      windowMinutes(this.activityWindow)
    );
    if (!events.length) return nothing;
    const elapsed = Math.max(
      1,
      Math.min(
        (Date.now() - this.eventLog.openedAt) / 60_000,
        windowMinutes(this.activityWindow) ?? Infinity
      )
    );
    const tools = events.filter((event) => event.kind === 'tool').length;
    const calls = events.filter((event) => event.kind === 'response').length;
    const blocked = events.filter((event) => event.blocked).length;
    return html`<section data-testid="fleet-map-recent">
      <h4>Recent</h4>
      <p class="meta">
        ${this.eventLog.truncated ? 'Bounded sample, ' : ''}Since you opened the
        map, within the selected window
      </p>
      <div class="recent-stats">
        ${(tools / elapsed).toFixed(1)} tools/min ·
        ${(calls / elapsed).toFixed(1)} model calls/min<br />
        ${compactCount(events.reduce((sum, e) => sum + e.input, 0))} tokens in /
        ${compactCount(events.reduce((sum, e) => sum + e.output, 0))}
        out${blocked ? ` · ${blocked} blocked` : ''}
      </div>
      <ol class="recent-events">
        ${this.eventLog.recent(node.id, windowMinutes(this.activityWindow)).map(
          (event) =>
            html`<li>
              <sl-icon
                name=${iconName(event.icon)}
                src=${iconSrc(event.icon)}
                aria-hidden="true"
              ></sl-icon
              ><span
                >${event.text}${event.count > 1 ? ` ×${event.count}` : ''}<time
                  datetime=${new Date(event.at).toISOString()}
                  title=${new Date(event.at).toLocaleString()}
                  >${formatRelativeTime(new Date(event.at).toISOString())}</time
                ></span
              >
            </li>`
        )}
      </ol>
    </section>`;
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
    const edges = [...view.edges];
    if (!this.types.has('provider_account')) {
      const ids = new Set(view.nodes.map((node) => node.id));
      for (const model of view.nodes.filter((node) => node.type === 'model'))
        for (const provider of model.provider_ids) {
          if (
            !ids.has(provider) ||
            edges.some(
              (edge) => edge.source === model.id && edge.target === provider
            )
          )
            continue;
          // This visual summary follows the model's explicit provider facet.
          // It never participates in account-scoped blast-radius traversal.
          edges.push({
            id: `summary:${model.id}>${provider}`,
            source: model.id,
            target: provider,
            kind: 'provided_by',
            provenance: 'configured',
            impact_source: provider,
            impact_target: model.id,
            gaps: [],
          });
        }
    }
    // A precise server relation supersedes the older, generic story line.
    const pairs = new Set(
      edges
        .filter(isWorkRelation)
        .flatMap((edge) => [
          `${edge.source}>${edge.target}`,
          `${edge.target}>${edge.source}`,
        ])
    );
    for (const edge of edges) {
      if (edge.kind === 'caused' && pairs.has(`${edge.source}>${edge.target}`))
        continue;
      if (edge.kind === 'account_of_provider' && !this.spatial) continue;
      // A machine box already says what runs on it.
      if (
        this.spatial &&
        edge.kind === 'hosts' &&
        layout.groups.get(edge.source)?.includes(edge.target)
      )
        continue;
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
      // A PR row inside its issue box needs no line: containment says it.
      if (
        edge.kind === 'caused' &&
        (layout.groups.get(from)?.includes(to) ||
          layout.groups.get(to)?.includes(from))
      )
        continue;
      const key = `${isWorkRelation(edge) ? `${edge.kind}:` : ''}${from}>${to}`;
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
      <div class="panel-heading">
        ${this.renderGlyph(node, byId)}
        <div>
          <div class="eyebrow">
            ${node.work_kind === 'issue' ? 'Issue' : node.work_kind === 'pull_request' ? 'Pull request' : FLEET_MAP_TYPE_LABEL[node.type]}
          </div>
          <h3>${displayLabel(node)}</h3>
        </div>
      </div>
      <div class="chips">
        ${
          node.idle
            ? html`<sl-badge class="chip" pill variant="neutral"
                >Idle (configured)</sl-badge
              >`
            : node.status
              ? html`<sl-badge class="chip" pill variant="neutral"
                  >${node.status}</sl-badge
                >`
              : node.type === 'work'
                ? html`<sl-badge class="chip" pill variant="neutral"
                    >State unknown</sl-badge
                  >`
                : nothing
        }
      </div>
      ${
        node.href
          ? html`<div class="actions">
              <a
                href=${node.href}
                target=${node.type === 'work' ? '_blank' : nothing}
                rel=${node.type === 'work' ? 'noopener noreferrer' : nothing}
                >Open
                ${node.work_kind === 'issue' ? 'issue' : node.work_kind === 'pull_request' ? 'pull request' : FLEET_MAP_TYPE_LABEL[node.type].toLowerCase()}${node.type === 'work' ? html` <sl-icon name="box-arrow-up-right" aria-hidden="true"></sl-icon>` : nothing}</a
              >
            </div>`
          : nothing
      }
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
      ${this.renderUsage(node)} ${this.renderWorkDetails(node, graph)}
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
      ${this.renderRecent(node)}
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

  private roomCache?: {
    viewport: FleetMapViewport;
    k: number;
    room: FleetMapViewport;
  };

  /**
   * The stage the spatial layout packs for. Where the wheel pans, the stage
   * itself: shelves stack until the picture fits at the legible scale. On
   * the Overview card (B-08) a fleet too dense for that is packed for a
   * proportionally larger stage, so its shelves stack to the card's shape
   * and the contained fit shrinks the whole picture evenly into view.
   */
  private layoutRoom(count: number): FleetMapViewport {
    const viewport = this.viewport;
    if (this.wheelPans || !count) return viewport;
    const cell = (FLEET_MAP_NODE_WIDTH + 24) * (FLEET_MAP_NODE_HEIGHT + 24);
    const fits = Math.sqrt((viewport.width * viewport.height) / (count * cell));
    if (fits >= LEGIBLE_SPATIAL_SCALE) return viewport;
    // Quantised, so a node arriving does not repack the whole map.
    const k = Math.max(MIN_CONTAINED_FIT_SCALE, Math.floor(fits * 20) / 20);
    const cached = this.roomCache;
    if (cached?.viewport === viewport && cached.k === k) return cached.room;
    const room = {
      width: Math.round(viewport.width / k),
      height: Math.round(viewport.height / k),
    };
    this.roomCache = { viewport, k, room };
    return room;
  }

  /** The simulation reruns only when the shown graph or a position changes. */
  private spatialFor(view: FleetMapGraph): FleetMapLayout {
    // Keyed on what is shown, not on the object: focus mode rebuilds the
    // view on every render and must not re-run the layout each time.
    const shape =
      view.nodes.map((node) => node.id).join(',') +
      '|' +
      view.edges.map((edge) => edge.id).join(',');
    const key = [
      shape,
      this.points,
      this.dragSnapshot,
      this.collapsed,
      this.layoutRoom(view.nodes.length),
    ];
    const cached = this.spatialCache;
    if (cached && cached.key.every((value, index) => value === key[index]))
      return cached.layout;
    const seed = layoutGraph(view).positions;
    const clusters = clusterOf(view);
    // Fresh layouts repack their shelves whenever a node joins or leaves.
    // Once the user owns the layout, existing nodes stay put and arrivals
    // are placed beside what they link to.
    const groups =
      [...clusters]
        .map(([id, key]) => `${id}:${key}`)
        .sort()
        .join('|') +
      '|' +
      view.nodes
        .filter((node) => node.type === 'work')
        .map((node) => `${node.id}:${node.parent_id ?? ''}`)
        .sort()
        .join('|');
    if (groups !== this.settledGroups) {
      this.settledGroups = groups;
      if (!this.layoutOwned) this.settled.clear();
    }
    const settled: FleetMapPoints = {};
    for (const node of view.nodes) {
      const at = this.settled.get(node.id);
      if (at) settled[node.id] = at;
    }
    const layout = spatialLayout(
      view,
      seed,
      { ...settled, ...(this.dragSnapshot ?? {}), ...this.points },
      clusters,
      this.collapsed,
      this.layoutRoom(view.nodes.length)
    );
    for (const [id, position] of layout.positions)
      this.settled.set(id, { x: position.x, y: position.y, w: position.w });
    if (this.layoutOwned && !this.drag) {
      const next = { ...this.points };
      let changed = false;
      for (const [id, p] of layout.positions) {
        if (next[id]?.x !== p.x || next[id]?.y !== p.y || next[id]?.w !== p.w)
          changed = true;
        next[id] = { x: p.x, y: p.y, w: p.w };
      }
      if (changed) this.points = next;
      savePositions(this.userId, next, this.positionsScope, [
        ...layout.positions.keys(),
      ]);
    }
    this.spatialCache = { key, layout };
    return layout;
  }

  /** Where each node was last placed this session, so refreshes keep it. */
  private settled = new Map<string, { x: number; y: number; w?: number }>();
  private settledGroups = '';

  /** Lit relationships by drawn line, with the label of what travelled. */
  private activityByLine(
    view: FleetMapGraph,
    layout: FleetMapLayout
  ): Map<string, string> {
    const lines = new Map<string, string>();
    const story = this.story.labels();
    // Work lines say what the action was; a lit line with nothing specific
    // to say carries no text at all.
    for (const edge of view.edges) {
      const label =
        story.get(edge.id) ??
        (isWorkRelation(edge)
          ? (story.get(`story:${edge.source}>${edge.target}`) ??
            story.get(`story:${edge.target}>${edge.source}`))
          : undefined);
      if (label === undefined) continue;
      const from = layout.anchor(edge.source),
        to = layout.anchor(edge.target);
      if (from && to) lines.set(`${from}>${to}`, label);
    }
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
    const graph =
      this.graph &&
      this.withFlowModels(
        withWorkRelations(
          this.story.graph(
            withCodeFlows(this.graph, this.codeActivity),
            windowMinutes(this.activityWindow)
          ),
          this.codeActivity
        )
      );
    if (!graph) return nothing;
    if (!graph.nodes.length && this.bare) {
      this.lastLayout = null;
      return html`<div class="stage" data-testid="fleet-map"></div>`;
    }
    if (!graph.nodes.length) {
      this.lastLayout = null;
      const windowLabel = FLEET_MAP_WINDOWS.find(
        (item) => item.value === this.activityWindow
      )?.label;
      // Keep the window reachable: a narrow window must not strand the user.
      return html`${this.bare ? nothing : this.renderToolbar(graph, graph, new Set())}
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
          room: this.viewport.height
            ? { ...this.viewport, minScale: MIN_FIT_SCALE }
            : undefined,
        });
    if (
      !this.spatial &&
      this.lastLayout &&
      target.width > this.lastLayout.width &&
      Date.now() - this.userMovedAt >= HANDS_OFF_MS &&
      target.width * this.scale + this.translateX > this.viewport.width - 16
    )
      this.refit = true;
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
    // Line labels never cover a node or another label; one that cannot fit
    // is left to the line's title and the bubble.
    const boxes = [...layout.positions.values()];
    const labelRects: FleetMapPosition[] = [];
    const active = this.activityByLine(view, layout);
    // On an administrator's spatial canvas the lit node says where traffic
    // started; the line lights without a label, so a busy map stays legible.
    const quiet = this.instanceAdmin && this.spatial;
    return html`${this.bare ? nothing : this.renderToolbar(graph, filtered, hits)}
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
        this.codeActivity?.truncated ||
        this.codeActivity?.work_truncated ||
        this.codeActivity?.relations_truncated
          ? html`<p class="notice" role="status" data-testid="code-truncated">
              Work activity is capped for this window. Counts and relations may
              be incomplete. Choose a shorter window for more detail.
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
                        data-lane=${column.type}
                        style="left:${column.x}px;width:${216}px"
                      >
                        ${
                          column.groups && column.groups.length > 1
                            ? column.groups.map(
                                (group, index) =>
                                  html`${index ? html`<span class="amp">&amp;</span>` : nothing}${FLEET_MAP_COLUMN_LABEL[group.type]}<span
                                      class="n"
                                      data-lane-count=${group.type}
                                      >${group.count}</span
                                    >`
                              )
                            : html`${laneTitle(column)}<span class="n"
                                  >${column.count}</span
                                >`
                        }
                        ${this.renderShowFewer(
                          column.type,
                          column.groups?.[0]?.count ?? column.count
                        )}
                      </div>
                      ${(column.groups ?? [])
                        .slice(1)
                        .map(
                          (group) =>
                            html`<div
                              class="column-title lane-subheader"
                              data-lane-group=${group.type}
                              style="left:${column.x}px;top:${group.y}px;width:${216}px"
                            >
                              ${FLEET_MAP_COLUMN_LABEL[group.type]}<span
                                class="n"
                                >${group.count}</span
                              >${this.renderShowFewer(group.type, group.count)}
                            </div>`
                        )}`
                )}
                <svg
                  width=${layout.width}
                  height=${layout.height}
                  aria-hidden="true"
                >
                  ${this.drawnEdges(view, layout, lit).map(
                    ({ edge, from, to, accounts }) => {
                      // '' is a lit line with nothing specific to say.
                      const inRadius =
                        !!lit &&
                        lit.has(edge.impact_source) &&
                        lit.has(edge.impact_target);
                      const eventLabel =
                        active.get(`${from}>${to}`) ??
                        (isWorkRelation(edge)
                          ? active.get(`${to}>${from}`)
                          : undefined);
                      const live =
                        isWorkRelation(edge) &&
                        (eventLabel !== undefined || inRadius)
                          ? edge.kind === 'touched' && eventLabel
                            ? eventLabel
                            : workRelationLabel(edge, byId.get(edge.target))
                          : eventLabel;
                      const glowing = eventLabel !== undefined;
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
                      const focused =
                        !!this.focusGap &&
                        (na.gaps.includes(this.focusGap) ||
                          nb.gaps.includes(this.focusGap));
                      const cls = [
                        'edge',
                        edge.provenance,
                        edge.gaps.length ? 'gap' : '',
                        inRadius ? 'lit' : '',
                        glowing ? 'active' : '',
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
                      // Text only when the line has something specific to
                      // say; the spatial admin canvas stays quiet.
                      if (
                        !live ||
                        (quiet && !isWorkRelation(edge)) ||
                        labelRects.length >= 6
                      )
                        return path;
                      const caption =
                        live.length > 72 ? `${live.slice(0, 69)}...` : live;
                      const spot = labelSpot(
                        caption,
                        [a.x + a.w, y1],
                        [b.x, y2],
                        a.x <= b.x,
                        boxes,
                        labelRects
                      );
                      if (!spot) return path;
                      return svg`${path}<text class="activity-label" x=${spot.x} y=${spot.y} text-anchor="middle">${caption}</text>`;
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
                No nodes match these filters. Clear a filter to see more.
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
          <button
            type="button"
            class="zoom"
            aria-label="Reset zoom to 100 percent"
            @click=${() => this.zoom(1 / this.scale)}
          ></button>
          <button
            type="button"
            @click=${() => this.zoom(1.25)}
            aria-label="Zoom in"
          >
            +
          </button>
          ${
            // Arrange lives beside Fit (pass 4): icon only, canvas only, and
            // never on an empty map. Undo stays in the notice it raises.
            this.instanceAdmin && this.spatial && view.nodes.length
              ? html`<sl-tooltip
                  content="Arrange: repack the map (Undo restores it)"
                  placement="top"
                  hoist
                  ><button
                    type="button"
                    class="arrange"
                    data-testid="fleet-map-arrange-button"
                    aria-label="Arrange"
                    @click=${this.arrange}
                  >
                    <sl-icon
                      name="grid-1x2"
                      aria-hidden="true"
                    ></sl-icon></button
                ></sl-tooltip>`
              : nothing
          }
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
        ${
          !this.bare && this.compactLegend
            ? html`<button
                class="overlay legend-toggle"
                aria-label="Map legend"
                aria-expanded=${this.legendOpen}
                @click=${() => {
                  this.legendOpen = !this.legendOpen;
                }}
              >
                ?
              </button>`
            : nothing
        }
        <div
          class="overlay legend ${this.compactLegend ? 'raised' : ''}"
          ?hidden=${this.bare || (this.compactLegend && !this.legendOpen)}
        >
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
            >Select a node to see what depends on it.
            ${this.wheelMode === 'page' && !this.fullscreen ? 'Drag' : 'Scroll'}
            to pan, Ctrl+scroll to zoom.</span
          >
        </div>
        ${this.renderBubbles(layout.positions)}
        ${this.resetUndo ? html`<div class="reset-notice" role="status">${this.resetUndo.message}. <button type="button" @click=${this.undoResetLayout}>Undo</button></div>` : nothing}
        ${this.instanceAdmin ? this.renderToasts(new Map(view.nodes.map((node) => [node.id, node]))) : nothing}
        ${lit && reach && uses ? this.renderPanel(graph, reach, uses) : nothing}
      </div>`;
  }
}

/** Icons are Shoelace names, or an image path such as the flow mark. */
const iconSrc = (icon?: string) => (icon?.startsWith('/') ? icon : nothing);
const iconName = (icon: string) => (icon.startsWith('/') ? nothing : icon);

/**
 * Where a lit line's label goes: on the line between the two boxes, at the
 * middle or a third of the way along, wherever it covers no node and no
 * label already placed. Null when no spot is free.
 */

export function labelSpot(
  text: string,
  from: [number, number],
  to: [number, number],
  forward: boolean,
  boxes: FleetMapPosition[],
  placed: FleetMapPosition[]
): { x: number; y: number } | null {
  const [x1, y1] = forward ? from : to;
  const [x2, y2] = forward ? to : from;
  // 11px medium text runs about 6.6px a character; keep 6px clear of cards
  // and of the counters that rise 10px above them.
  const w = Math.ceil(text.length * 6.8) + 12;
  const h = 16;
  const MARGIN = 6;
  for (const t of [0.5, 0.33, 0.67]) {
    const x = x1 + (x2 - x1) * t;
    const y = y1 + (y2 - y1) * t - 6;
    const rect = { x: x - w / 2, y: y - h + 3, w, h };
    const hits = (b: FleetMapPosition) =>
      rect.x < b.x + b.w + MARGIN &&
      b.x - MARGIN < rect.x + rect.w &&
      rect.y < b.y + b.h + MARGIN &&
      b.y - MARGIN - 10 < rect.y + rect.h;
    if (boxes.some(hits) || placed.some(hits)) continue;
    placed.push(rect);
    return { x, y };
  }
  return null;
}

const BUBBLES_KEY = 'preloop:fleet-map:bubbles';
const TYPES_KEY = 'preloop:fleet-map:types';

/** The Show choice, per user and map; unknown or broken values fall back. */
function loadTypes(user: string, scope: string): Set<FleetMapNodeType> {
  try {
    const saved = JSON.parse(
      localStorage.getItem(`${TYPES_KEY}:${scope}:${user || 'anonymous'}`) ||
        'null'
    );
    // A saved list is the user's choice, even when it hides everything.
    if (Array.isArray(saved))
      return new Set(
        saved.filter((type): type is FleetMapNodeType =>
          (FLEET_MAP_TYPES as readonly string[]).includes(type)
        )
      );
  } catch {
    // Private mode or a hand-edited value: the default applies.
  }
  return new Set(DEFAULT_TYPES);
}

function saveTypes(
  user: string,
  scope: string,
  types: Set<FleetMapNodeType>
): void {
  try {
    localStorage.setItem(
      `${TYPES_KEY}:${scope}:${user || 'anonymous'}`,
      JSON.stringify([...types])
    );
  } catch {
    // Storage unavailable: the choice lasts for this page.
  }
}

function loadBubbles(user: string, scope: string, fallback = false): boolean {
  try {
    const value = localStorage.getItem(
      `${BUBBLES_KEY}:${scope}:${user || 'anonymous'}`
    );
    return value === null ? fallback : value === 'on';
  } catch {
    return false;
  }
}

function saveBubbles(user: string, on: boolean, scope: string): void {
  try {
    localStorage.setItem(
      `${BUBBLES_KEY}:${scope}:${user || 'anonymous'}`,
      on ? 'on' : 'off'
    );
  } catch {
    // A preference, not a requirement.
  }
}

/** Whether two layouts place every node in the same spot. */
function samePositions(a: FleetMapLayout, b: FleetMapLayout): boolean {
  if (a.positions.size !== b.positions.size) return false;
  for (const [id, p] of b.positions) {
    const q = a.positions.get(id);
    if (!q || q.x !== p.x || q.y !== p.y) return false;
  }
  return true;
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
