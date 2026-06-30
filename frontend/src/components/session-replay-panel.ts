import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/range/range.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type {
  AIModel,
  FlowGatewayConversationPreviewMessage,
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
  RuntimeSessionInteractionSummary,
  RuntimeSessionOptimizationAppliedAction,
  RuntimeSessionOptimizationResponse,
} from '../types';
import type {
  ObservedSession,
  SessionOptimizationSuggestion,
  SessionReplayMode,
} from '../utils/session-observer';
import {
  formatCost,
  formatNumber,
  getGatewayEventPreviewMessages,
  getGatewayEventUserRequest,
} from '../utils/session-observer';
import './preloop-gateway-event';
import './session-optimization-panel';

type ReplayMessage = FlowGatewayConversationPreviewMessage & {
  key: string;
  event: FlowGatewayEvent | null;
  eventMessageIndex: number | null;
  timestamp: string | null;
  title: string;
};

type ReplayEventMarker = {
  id: string;
  index: number;
  kind: ReplayMarkerKind;
  role: string;
  title: string;
  timestamp: string | null;
  failed: boolean;
};

type ReplayMarkerKind = 'user' | 'agent' | 'system' | 'developer' | 'tool';

type TranscriptFilter = 'all' | 'model' | 'tools' | 'costly';

type TranscriptRow =
  | { kind: 'event'; timestamp: string | null; event: FlowGatewayEvent }
  | {
      kind: 'tool';
      timestamp: string | null;
      item: RuntimeSessionActivityItem;
    };

const TRANSCRIPT_FILTERS: Array<{ id: TranscriptFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'model', label: 'Model calls' },
  { id: 'tools', label: 'Tool calls' },
  { id: 'costly', label: 'Costly first' },
];

// --- Unified sortable chat (turn/delta model) ---------------------------------
//
// Each gateway request is rendered as one "turn". Consecutive agentic requests
// re-send a growing message list, so for each turn we render only the messages
// it ADDED relative to the previous request (the delta) plus that request's
// assistant response. This deduplicates the conversation and makes each turn a
// self-contained, sortable unit.

type ChatSort = 'oldest' | 'newest' | 'costliest' | 'cheapest' | 'type';

type ChatTypeFilter = 'all' | 'messages' | 'tools';

type ChatThresholdMode = 'tokens' | 'cost';

// A single delta message belonging to a turn.
type ChatTurnMessage = FlowGatewayConversationPreviewMessage & {
  key: string;
  signature: string;
  isToolRelated: boolean;
};

// One turn = one gateway request (with only its delta messages) OR a standalone
// supporting-activity message (e.g. an operator/agent-control message) shown
// inline in the chat flow.
type ChatTurn = {
  id: string;
  index: number;
  event: FlowGatewayEvent | null;
  timestamp: string | null;
  title: string;
  deltaMessages: ChatTurnMessage[];
  totalTokens: number;
  promptTokens: number;
  completionTokens: number;
  // Prompt-cache hits read off the usage details (OpenAI cached_tokens /
  // Anthropic cache_read_input_tokens). null when the payload doesn't carry it.
  cachedTokens: number | null;
  estimatedCost: number;
  toolCallCount: number;
  failed: boolean;
  // Activity (operator/talk) turns are NOT gateway requests: they carry no real
  // token/cost/tool stats, so the header suppresses those meaningless zeros.
  isActivity: boolean;
};

const CHAT_SORTS: Array<{ id: ChatSort; label: string }> = [
  { id: 'oldest', label: 'Oldest first' },
  { id: 'newest', label: 'Newest first' },
  { id: 'costliest', label: 'Costliest first' },
  { id: 'cheapest', label: 'Cheapest first' },
  { id: 'type', label: 'By event type' },
];

const CHAT_TYPE_FILTERS: Array<{ id: ChatTypeFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'messages', label: 'Messages only' },
  { id: 'tools', label: 'Tool calls only' },
];

const REPLAY_MARKER_LEGEND: Array<{ kind: ReplayMarkerKind; label: string }> = [
  { kind: 'user', label: 'User' },
  { kind: 'agent', label: 'Agent' },
  { kind: 'system', label: 'System' },
  { kind: 'developer', label: 'Developer' },
  { kind: 'tool', label: 'Tool call' },
];
const REPLAY_MARKER_KINDS = REPLAY_MARKER_LEGEND.map((item) => item.kind);

const REPLAY_MESSAGE_WINDOW_BEFORE = 18;
const REPLAY_MESSAGE_WINDOW_AFTER = 24;
const ESTIMATED_REPLAY_MESSAGE_HEIGHT = 180;
const REPLAY_SCROLL_RESUME_DELAY_MS = 550;

@customElement('session-replay-panel')
export class SessionReplayPanel extends LitElement {
  @property({ type: Object })
  session: ObservedSession | null = null;

  @property({ type: Array })
  events: FlowGatewayEvent[] = [];

  @property({ type: Array })
  timelineEvents: FlowGatewayEvent[] = [];

  @property({ type: Array })
  activity: RuntimeSessionActivityItem[] = [];

  @property({ type: String })
  replayMode: SessionReplayMode = 'timeline';

  @property({ type: Boolean })
  loading = false;

  @property({ type: Boolean })
  rawPayloads = true;

  @property({ type: Object })
  eventDetails: Record<string, FlowGatewayEvent> = {};

  @property({ type: Object })
  loadingEventDetails: Set<string> = new Set();

  @property({ type: Object })
  interactionSummaries: Record<string, RuntimeSessionInteractionSummary> = {};

  @property({ type: Object })
  loadingInteractionSummaries: Set<string> = new Set();

  @property({ type: Boolean })
  summarizeVisibleContent = false;

  @property({ type: Boolean })
  hasMoreEvents = false;

  @property({ type: Boolean })
  loadingMoreEvents = false;

  @property({ type: Number })
  totalEvents: number | null = null;

  @property({ type: Array })
  optimizationSuggestions: SessionOptimizationSuggestion[] | null = null;

  @property({ type: Boolean })
  optimizationEnabled = false;

  @property({ type: Boolean })
  loadingOptimization = false;

  @property({ type: Array })
  availableModels: AIModel[] = [];

  @property({ type: Object })
  optimizationResult: RuntimeSessionOptimizationResponse | null = null;

  @property({ type: Array })
  optimizationAppliedActions: RuntimeSessionOptimizationAppliedAction[] = [];

  @property({ type: String })
  applyingOptimizationSuggestionId: string | null = null;

  @state()
  private visibleActivityCount = 20;

  @state()
  private expandedMessageKeys = new Set<string>();

  @state()
  private fullTextEventIds = new Set<string>();

  @state()
  private replayActive = false;

  @state()
  private replaySpeedMs = 1200;

  @state()
  private replayIndex = 0;

  @state()
  private replayReversed = false;

  @state()
  private optimizeControlsOpen = false;

  @state()
  private optimizeFromIndex = 0;

  @state()
  private optimizeToIndex = 0;

  @state()
  private optimizeSources = new Set<ReplayMarkerKind>(REPLAY_MARKER_KINDS);

  @state()
  private optimizeModelId: string | null = null;

  @state()
  private visibleReplayKinds = new Set<ReplayMarkerKind>(REPLAY_MARKER_KINDS);

  @state()
  private transcriptFilter: TranscriptFilter = 'all';

  // Unified sortable chat controls.
  @state()
  private chatSort: ChatSort = 'oldest';

  @state()
  private chatTypeFilter: ChatTypeFilter = 'all';

  @state()
  private chatThreshold = 0;

  @state()
  private chatThresholdMode: ChatThresholdMode = 'tokens';

  // Turns expanded into their full request context (lazy-loaded on expand).
  @state()
  private expandedTurnEventIds = new Set<string>();

  private replayTimer: number | null = null;
  private summaryObserver: IntersectionObserver | null = null;
  private eventPageObserver: IntersectionObserver | null = null;
  private replayDetailObserver: IntersectionObserver | null = null;
  private replayScrollSyncTimer: number | null = null;
  private autoScrollingReplay = false;
  private suppressNextReplayAutoScroll = false;
  private userScrollingReplay = false;
  private resumeReplayAfterScroll = false;

  static styles = css`
    :host {
      display: block;
      min-height: 0;
    }

    .panel {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
    }

    .empty,
    .loading {
      align-items: center;
      color: var(--sl-color-neutral-600);
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
      justify-content: center;
      padding: var(--sl-spacing-x-large);
      text-align: center;
    }

    .timeline-event,
    .chat-message,
    .activity-event {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
    }

    .activity-event {
      border-left: 3px solid var(--sl-color-neutral-400);
    }

    .event-header,
    .event-meta-row {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .event-title {
      color: var(--sl-color-neutral-900);
      font-weight: 700;
    }

    .event-meta,
    .preview,
    .segment-title {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      overflow-wrap: anywhere;
    }

    .preview {
      color: var(--sl-color-neutral-800);
      margin-top: var(--sl-spacing-small);
      max-height: min(28vh, 220px);
      overflow: auto;
      white-space: pre-wrap;
    }

    .message-list {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-small);
    }

    .chat-message.user {
      border-color: var(--sl-color-primary-200);
      margin-left: clamp(0px, 10%, 72px);
    }

    .chat-message.assistant {
      border-color: var(--sl-color-success-200);
      margin-right: clamp(0px, 10%, 72px);
    }

    .chat-message.failed {
      border-color: var(--sl-color-danger-300);
      box-shadow: inset 3px 0 0 var(--sl-color-danger-500);
    }

    .message-role {
      color: var(--sl-color-neutral-900);
      font-weight: 700;
      margin-bottom: var(--sl-spacing-2x-small);
    }

    .message-text {
      color: var(--sl-color-neutral-800);
      font-size: var(--sl-font-size-small);
      line-height: 1.5;
      max-height: min(40vh, 360px);
      overflow: auto;
      white-space: pre-wrap;
    }

    .message-footer {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
      margin-top: var(--sl-spacing-x-small);
      text-transform: none;
    }

    .message-metrics {
      align-items: center;
      color: var(--sl-color-neutral-600);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-2x-small);
      margin-top: var(--sl-spacing-x-small);
    }

    .metric-pill {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 999px;
      padding: 1px 7px;
    }

    .metric-pill.danger {
      background: var(--sl-color-danger-50);
      border-color: var(--sl-color-danger-200);
      color: var(--sl-color-danger-700);
    }

    .metric-pill.warning {
      background: var(--sl-color-warning-50);
      border-color: var(--sl-color-warning-200);
      color: var(--sl-color-warning-700);
    }

    .metric-pill.success {
      background: var(--sl-color-success-50);
      border-color: var(--sl-color-success-200);
      color: var(--sl-color-success-700);
    }

    .segment-grid {
      display: grid;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-small);
    }

    .segment {
      background: var(--sl-color-neutral-50);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-small);
    }

    .detail-actions {
      display: flex;
      justify-content: flex-end;
      margin-top: var(--sl-spacing-small);
    }

    .activity-group::part(base) {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      overflow: hidden;
    }

    .activity-group::part(header) {
      background: var(--sl-color-neutral-50);
      color: var(--sl-color-neutral-900);
      font-weight: 700;
    }

    .activity-group-summary {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
      width: 100%;
    }

    .activity-list {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
      max-height: min(55vh, 520px);
      overflow: auto;
      padding-right: var(--sl-spacing-2x-small);
    }

    .supporting-note {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      margin-bottom: var(--sl-spacing-small);
    }

    .raw-event-container {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      margin-top: var(--sl-spacing-small);
      max-height: min(65vh, 680px);
      overflow: auto;
    }

    .summary-card {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
    }

    .summary-text {
      color: var(--sl-color-neutral-800);
      line-height: 1.5;
      margin-top: var(--sl-spacing-small);
    }

    .summary-points {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
      margin-bottom: 0;
      margin-top: var(--sl-spacing-small);
      padding-left: var(--sl-spacing-large);
    }

    .replay-controls {
      align-items: center;
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
      padding: var(--sl-spacing-small);
    }

    .playback-bar {
      display: grid;
      gap: var(--sl-spacing-x-small);
      grid-template-columns: auto auto auto minmax(260px, 1fr);
      width: 100%;
    }

    .playback-bar sl-button-group,
    .playback-bar .speed-select-native,
    .playback-bar .reverse-button {
      white-space: nowrap;
    }

    .transport-button sl-icon {
      font-size: 1rem;
    }

    .speed-select-native {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-small);
      color: var(--sl-color-neutral-800);
      font: inherit;
      height: 30px;
      padding: 0 4px;
      width: 54px;
    }

    .reverse-button::part(base) {
      min-height: 30px;
      min-width: 30px;
      padding-inline: 0;
    }

    .timeline-wrap {
      display: grid;
      gap: var(--sl-spacing-2x-small);
      min-width: 0;
      position: relative;
      width: 100%;
    }

    .timeline-range {
      accent-color: var(--sl-color-primary-600);
      width: 100%;
    }

    .timeline-markers {
      height: 36px;
      position: relative;
    }

    .timeline-marker {
      background: var(--sl-color-primary-500);
      border: 1px solid var(--sl-color-neutral-0);
      border-radius: 999px;
      cursor: pointer;
      height: 16px;
      padding: 0;
      position: absolute;
      top: 0;
      transform: translateX(-50%);
      width: 4px;
    }

    .timeline-marker.current {
      background: var(--sl-color-warning-500);
      width: 8px;
    }

    .timeline-marker.failed {
      box-shadow: 0 0 0 2px var(--sl-color-danger-500);
    }

    .timeline-datetime-label {
      color: var(--sl-color-neutral-500);
      font-size: 0.62rem;
      position: absolute;
      top: 20px;
      transform: translateX(-50%);
      white-space: nowrap;
    }

    .timeline-marker.user,
    .legend-swatch.user {
      background: var(--sl-color-primary-500);
    }

    .timeline-marker.agent,
    .legend-swatch.agent {
      background: var(--sl-color-success-500);
    }

    .timeline-marker.system,
    .legend-swatch.system {
      background: var(--sl-color-neutral-500);
    }

    .timeline-marker.developer,
    .legend-swatch.developer {
      background: #8b5cf6;
    }

    .timeline-marker.tool,
    .legend-swatch.tool {
      background: var(--sl-color-warning-500);
    }

    .timeline-legend {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-2x-small);
      justify-content: flex-end;
    }

    .legend-item {
      align-items: center;
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 999px;
      color: var(--sl-color-neutral-600);
      display: inline-flex;
      font-size: var(--sl-font-size-x-small);
      gap: 4px;
      line-height: 1.4;
      padding: 2px 8px;
    }

    .legend-item.toggle {
      cursor: pointer;
    }

    .legend-item.off {
      opacity: 0.42;
    }

    .legend-swatch {
      border-radius: 999px;
      display: inline-block;
      height: 8px;
      width: 8px;
    }

    .timeline-label-row {
      align-items: center;
      display: flex;
      justify-content: space-between;
    }

    .replay-stage {
      display: grid;
      gap: var(--sl-spacing-large);
      padding-bottom: var(--sl-spacing-medium);
    }

    .replay-spacer {
      pointer-events: none;
    }

    .event-page-sentinel {
      height: 1px;
      pointer-events: none;
    }

    .summary-replacement {
      background: var(--sl-color-primary-50);
      border: 1px solid var(--sl-color-primary-200);
      border-radius: var(--sl-border-radius-medium);
      margin-top: var(--sl-spacing-small);
      padding: var(--sl-spacing-small);
    }

    sl-dialog.replay-dialog::part(panel) {
      --width: min(1120px, calc(100vw - 32px));
      max-height: calc(100vh - 32px);
    }

    sl-dialog.replay-dialog::part(body) {
      padding: 0;
    }

    .replay-dialog-body {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      max-height: min(82vh, 860px);
      min-height: min(72vh, 760px);
    }

    .replay-dialog-header {
      background: var(--sl-color-neutral-0);
      border-bottom: 1px solid var(--sl-color-neutral-200);
      display: grid;
      gap: var(--sl-spacing-small);
      padding: var(--sl-spacing-medium);
      position: sticky;
      top: 0;
      z-index: 2;
    }

    .replay-title-row {
      align-items: center;
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .replay-title {
      color: var(--sl-color-neutral-900);
      font-weight: 700;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .replay-dialog-actions {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: flex-end;
    }

    .replay-control-row {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .replay-control-cluster {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
    }

    .time-select {
      min-width: min(420px, 100%);
    }

    .replay-scrollport {
      background: linear-gradient(
        180deg,
        var(--sl-color-neutral-50),
        var(--sl-color-neutral-0)
      );
      min-height: 0;
      overflow: auto;
      padding: var(--sl-spacing-large);
    }

    .replay-detail-placeholder {
      align-items: center;
      color: var(--sl-color-neutral-600);
      display: flex;
      gap: var(--sl-spacing-small);
      min-height: 76px;
    }

    .transcript-filter-bar {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      justify-content: space-between;
    }

    .chat-control-bar {
      align-items: center;
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .chat-control-cluster {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
    }

    .chat-control-label {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
      font-weight: var(--sl-font-weight-semibold);
    }

    .chat-threshold-input {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-small);
      color: var(--sl-color-neutral-800);
      font: inherit;
      height: 30px;
      padding: 0 6px;
      width: 96px;
    }

    .chat-threshold-slider {
      --track-color-active: var(--sl-color-primary-500);
      min-width: 120px;
      width: clamp(120px, 18vw, 200px);
    }

    .chat-threshold-slider::part(form-control-label) {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
    }

    .chat-select {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-small);
      color: var(--sl-color-neutral-800);
      font: inherit;
      height: 30px;
      padding: 0 4px;
    }

    /* Whole-session cost picture: a quiet dashboard strip, not a banner. Muted
       secondary metadata in neutral tokens, consistent with the chat styling. */
    .chat-summary-bar {
      align-items: center;
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      color: var(--sl-color-neutral-600);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-x-small);
      gap: var(--sl-spacing-x-small) var(--sl-spacing-medium);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .chat-summary-item {
      align-items: baseline;
      display: inline-flex;
      gap: var(--sl-spacing-2x-small);
      white-space: nowrap;
    }

    .chat-summary-label {
      color: var(--sl-color-neutral-500);
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }

    .chat-summary-value {
      color: var(--sl-color-neutral-800);
      font-weight: var(--sl-font-weight-semibold);
    }

    .chat-summary-value.has-tools {
      color: var(--sl-color-warning-700);
    }

    .chat-summary-value.has-failures {
      color: var(--sl-color-danger-700);
    }

    .chat-summary-sub {
      color: var(--sl-color-neutral-500);
    }

    .chat-thread {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }

    .chat-turn {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
      padding: var(--sl-spacing-small);
    }

    .chat-turn.failed {
      border-color: var(--sl-color-danger-300);
      box-shadow: inset 3px 0 0 var(--sl-color-danger-500);
    }

    /* Most-expensive request turn: a subtle warning-toned left accent so the
       cost story is visible without sorting. Failed turns keep their danger
       accent (the .failed rule wins by source order if both apply). */
    .chat-turn.most-expensive {
      border-color: var(--sl-color-warning-300);
      box-shadow: inset 3px 0 0 var(--sl-color-warning-500);
    }

    .chat-turn-cost-chip {
      align-items: center;
      background: var(--sl-color-warning-50);
      border: 1px solid var(--sl-color-warning-200);
      border-radius: 999px;
      color: var(--sl-color-warning-700);
      display: inline-flex;
      font-size: var(--sl-font-size-x-small);
      font-weight: var(--sl-font-weight-semibold);
      gap: var(--sl-spacing-2x-small);
      padding: 1px 8px;
      white-space: nowrap;
    }

    .chat-turn-header {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      justify-content: space-between;
    }

    .chat-turn-header-main {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      min-width: 0;
    }

    .chat-turn-title {
      color: var(--sl-color-neutral-900);
      font-size: var(--sl-font-size-small);
      font-weight: var(--sl-font-weight-semibold);
    }

    .chat-turn-time {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
      font-weight: var(--sl-font-weight-normal);
    }

    .chat-turn-badges {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
    }

    /* Per-turn token/cost/tool counts are secondary metadata: quiet, muted,
       lighter weight and a touch smaller than the conversation text. */
    .chat-turn-stat {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
      font-weight: var(--sl-font-weight-normal);
      letter-spacing: 0.01em;
      white-space: nowrap;
    }

    .chat-turn-stat.has-tools {
      color: var(--sl-color-warning-700);
    }

    .chat-turn-bubbles {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
    }

    .chat-turn-empty {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
      font-style: italic;
    }

    .chat-turn-detail {
      border-top: 1px dashed var(--sl-color-neutral-200);
      display: grid;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-x-small);
      padding-top: var(--sl-spacing-small);
    }

    .tool-row {
      align-items: center;
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-left: 3px solid var(--sl-color-amber-400);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      justify-content: space-between;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .tool-row-main {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      min-width: 0;
    }

    .tool-row-name {
      font-weight: var(--sl-font-weight-semibold);
      font-size: var(--sl-font-size-small);
    }

    .tool-row-chips {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-2x-small);
    }

    .optimize-drawer {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: grid;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-small);
      padding: var(--sl-spacing-small);
    }

    .optimize-controls {
      background: var(--sl-color-neutral-50);
      border-radius: var(--sl-border-radius-medium);
      display: grid;
      gap: var(--sl-spacing-small);
      padding: var(--sl-spacing-small);
    }

    .optimize-control-row {
      align-items: end;
      display: grid;
      gap: var(--sl-spacing-small);
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    }

    .optimization-model-select {
      max-width: 320px;
      min-width: 220px;
      width: 100%;
    }

    .optimize-range {
      display: grid;
      gap: var(--sl-spacing-2x-small);
    }

    .optimize-range-dual {
      display: grid;
      gap: 2px;
    }

    .optimize-range-markers {
      height: 30px;
      position: relative;
    }

    .optimize-range-marker {
      background: var(--sl-color-primary-500);
      border: 1px solid var(--sl-color-neutral-0);
      border-radius: 999px;
      height: 12px;
      padding: 0;
      position: absolute;
      top: 0;
      transform: translateX(-50%);
      width: 4px;
    }

    .optimize-range-label {
      color: var(--sl-color-neutral-500);
      font-size: 0.58rem;
      position: absolute;
      top: 16px;
      transform: translateX(-50%);
      white-space: nowrap;
    }

    .optimize-range-marker.user {
      background: var(--sl-color-primary-500);
    }

    .optimize-range-marker.agent {
      background: var(--sl-color-success-500);
    }

    .optimize-range-marker.system {
      background: var(--sl-color-neutral-500);
    }

    .optimize-range-marker.developer {
      background: #8b5cf6;
    }

    .optimize-range-marker.tool {
      background: var(--sl-color-warning-500);
    }

    .optimize-range-marker.failed {
      box-shadow: 0 0 0 2px var(--sl-color-danger-500);
    }

    .source-toggle-row {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-2x-small);
    }

    .replay-transcript {
      display: grid;
      gap: var(--sl-spacing-large);
      margin: 0 auto;
      max-width: 920px;
    }

    .replay-message {
      opacity: 0.62;
      scroll-margin: var(--sl-spacing-2x-large);
      transition:
        opacity 120ms ease,
        transform 120ms ease;
    }

    .replay-message.played {
      opacity: 1;
    }

    .replay-message.current .chat-message,
    .replay-message.current .summary-card {
      box-shadow: 0 0 0 3px var(--sl-color-primary-100);
      transform: translateY(-1px);
    }

    .character-row {
      align-items: center;
      display: flex;
      gap: var(--sl-spacing-small);
      margin-bottom: var(--sl-spacing-x-small);
    }

    .character-avatar {
      align-items: center;
      background: var(--sl-color-neutral-100);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 999px;
      display: inline-flex;
      font-size: 0.72rem;
      font-weight: 800;
      height: 28px;
      justify-content: center;
      text-transform: uppercase;
      width: 28px;
    }

    @media (max-width: 560px) {
      .playback-bar {
        grid-template-columns: auto auto auto;
        overflow-x: auto;
      }

      .timeline-wrap {
        grid-column: 1 / -1;
        min-width: 260px;
      }
    }
  `;

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.stopReplay();
    this.summaryObserver?.disconnect();
    this.eventPageObserver?.disconnect();
    this.replayDetailObserver?.disconnect();
    if (this.replayScrollSyncTimer !== null) {
      window.clearTimeout(this.replayScrollSyncTimer);
    }
  }

  updated(changed: Map<string | number | symbol, unknown>): void {
    if (
      changed.has('summarizeVisibleContent') ||
      changed.has('events') ||
      changed.has('timelineEvents') ||
      changed.has('interactionSummaries')
    ) {
      this.updateSummaryObserver();
    }
    if (
      changed.has('events') ||
      changed.has('hasMoreEvents') ||
      changed.has('loadingMoreEvents') ||
      changed.has('replayMode')
    ) {
      this.updateEventPageObserver();
    }
    if (changed.has('replayIndex') || changed.has('replayMode')) {
      this.scrollReplayToCurrentMessage();
    }
    if (
      changed.has('replayMode') ||
      changed.has('timelineEvents') ||
      changed.has('events') ||
      changed.has('eventDetails') ||
      changed.has('loadingEventDetails')
    ) {
      this.updateReplayDetailObserver();
    }
    if (changed.has('replayMode')) {
      this.handleReplayModeChange();
    }
    if (changed.has('timelineEvents') && this.replayViewActive) {
      const messages = this.getVisibleReplayMessages();
      if (messages.length) {
        this.replayIndex = messages.length - 1;
        this.ensureOptimizationBounds(messages);
        this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
      }
    }
  }

  private formatTime(value: string | null | undefined): string {
    if (!value) return 'Unknown time';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleTimeString();
  }

  private formatDateTime(value: string | null | undefined): string {
    if (!value) return 'Unknown time';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  private formatTimelineLabel(value: string | null | undefined): string {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return `${parsed.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    })} ${parsed.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
    })}`;
  }

  private getOutcomeVariant(event: FlowGatewayEvent) {
    const outcome = event.payload?.outcome;
    if (outcome === 'error') return 'danger';
    if (outcome === 'budget_denied') return 'warning';
    if (outcome === 'success') return 'success';
    if (outcome === 'pending') return 'primary';
    return 'neutral';
  }

  private getEventTitle(event: FlowGatewayEvent): string {
    if (event.type.includes('model_gateway')) {
      return (
        event.payload?.model_alias ||
        event.payload?.requested_model ||
        'Model request'
      );
    }
    if (event.payload?.tool_name) {
      return `Tool: ${event.payload.tool_name}`;
    }
    return event.type.replace(/_/g, ' ');
  }

  private requestEventDetail(event: FlowGatewayEvent): void {
    this.dispatchEvent(
      new CustomEvent('session-event-detail-requested', {
        detail: { eventId: event.id },
        bubbles: true,
        composed: true,
      })
    );
  }

  private requestMoreEvents(): void {
    if (!this.hasMoreEvents || this.loadingMoreEvents) return;
    this.dispatchEvent(
      new CustomEvent('session-events-page-requested', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private requestReplayMetadata(): void {
    this.dispatchEvent(
      new CustomEvent('session-replay-metadata-requested', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private requestInteractionSummary(event: FlowGatewayEvent): void {
    this.dispatchEvent(
      new CustomEvent('session-interaction-summary-requested', {
        detail: { eventId: event.id },
        bubbles: true,
        composed: true,
      })
    );
  }

  private messageContentToText(value: unknown): string {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      return value
        .map((part) => {
          if (typeof part === 'string') return part;
          if (part && typeof part === 'object') {
            const record = part as Record<string, unknown>;
            return String(record.text || record.content || '');
          }
          return '';
        })
        .filter(Boolean)
        .join('\n');
    }
    return value ? String(value) : '';
  }

  private getRequestMessages(event: FlowGatewayEvent) {
    const request = event.payload?.request;
    const rawMessages =
      request && typeof request === 'object'
        ? (request as { messages?: unknown }).messages
        : null;
    if (!Array.isArray(rawMessages)) return [];
    return rawMessages
      .map((message, index) => {
        if (!message || typeof message !== 'object') return null;
        const record = message as Record<string, unknown>;
        const text = this.messageContentToText(record.content).trim();
        if (!text) return null;
        return {
          role: String(record.role || 'message'),
          text,
          key: `${event.id}:request:${index}`,
        };
      })
      .filter(Boolean) as Array<{ role: string; text: string; key: string }>;
  }

  private toggleMessage(key: string): void {
    const next = new Set(this.expandedMessageKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expandedMessageKeys = next;
  }

  private toggleEventFullText(eventId: string): void {
    const next = new Set(this.fullTextEventIds);
    if (next.has(eventId)) next.delete(eventId);
    else next.add(eventId);
    this.fullTextEventIds = next;
  }

  private showFullReplayMessage(message: ReplayMessage): void {
    const next = new Set(this.fullTextEventIds);
    next.add(message.key);
    this.fullTextEventIds = next;
    if (message.event && !this.eventDetails[message.event.id]) {
      this.requestEventDetail(message.event);
    }
  }

  private eventNeedsSummary(event: FlowGatewayEvent): boolean {
    const userRequest = getGatewayEventUserRequest(event) || '';
    const messages = getGatewayEventPreviewMessages(event);
    return (
      userRequest.length > 420 ||
      messages.some((message) => (message.text || '').length > 420) ||
      Number(event.payload?.total_tokens || 0) > 4000
    );
  }

  private updateSummaryObserver(): void {
    this.summaryObserver?.disconnect();
    this.summaryObserver = null;
    if (!this.summarizeVisibleContent) return;

    this.summaryObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const eventId = (entry.target as HTMLElement).dataset.eventId;
          const event = this.events.find(
            (candidate) => candidate.id === eventId
          );
          if (!event || !this.eventNeedsSummary(event)) continue;
          if (this.interactionSummaries[event.id]) continue;
          if (this.loadingInteractionSummaries.has(event.id)) continue;
          this.requestInteractionSummary(event);
        }
      },
      {
        root: null,
        rootMargin: '240px 0px',
        threshold: 0.01,
      }
    );

    this.updateComplete.then(() => {
      this.renderRoot
        .querySelectorAll<HTMLElement>('.summary-candidate[data-event-id]')
        .forEach((element) => this.summaryObserver?.observe(element));
    });
  }

  private updateEventPageObserver(): void {
    this.eventPageObserver?.disconnect();
    this.eventPageObserver = null;
    if (!this.hasMoreEvents || this.loadingMoreEvents) return;

    this.eventPageObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          this.requestMoreEvents();
        }
      },
      {
        root: null,
        rootMargin: '640px 0px',
        threshold: 0.01,
      }
    );

    this.updateComplete.then(() => {
      this.renderRoot
        .querySelectorAll<HTMLElement>('.event-page-sentinel')
        .forEach((element) => this.eventPageObserver?.observe(element));
    });
  }

  private get replayViewActive(): boolean {
    return this.replayMode === 'replay';
  }

  private handleReplayModeChange(): void {
    if (this.replayViewActive) {
      this.initializeReplayView();
      return;
    }
    this.pauseReplay();
    // Do NOT auto-generate optimization suggestions on entering the optimize
    // view. We only render previously cached results here; (re)generation
    // happens exclusively when the user clicks the explicit Generate /
    // Regenerate button.
  }

  private updateReplayDetailObserver(): void {
    this.replayDetailObserver?.disconnect();
    this.replayDetailObserver = null;
    if (!this.replayViewActive) return;

    this.replayDetailObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const eventId = (entry.target as HTMLElement).dataset.eventId;
          const event = this.getReplayEventById(eventId);
          if (!event || this.eventDetails[event.id]) continue;
          if (this.loadingEventDetails.has(event.id)) continue;
          this.requestEventDetail(event);
        }
      },
      {
        root: this.renderRoot.querySelector('.replay-scrollport'),
        rootMargin: '360px 0px',
        threshold: 0.01,
      }
    );

    this.updateComplete.then(() => {
      this.renderRoot
        .querySelectorAll<HTMLElement>(
          '.replay-detail-placeholder[data-event-id]'
        )
        .forEach((element) => this.replayDetailObserver?.observe(element));
    });
  }

  private getReplayEventById(
    eventId: string | undefined
  ): FlowGatewayEvent | null {
    if (!eventId) return null;
    return (
      this.eventDetails[eventId] ||
      this.events.find((event) => event.id === eventId) ||
      this.timelineEvents.find((event) => event.id === eventId) ||
      null
    );
  }

  private getReplayMessages(): ReplayMessage[] {
    const detailedEventIds = new Set([
      ...this.events.map((event) => event.id),
      ...Object.keys(this.eventDetails),
    ]);
    const replayEvents = this.mergeReplayEvents(
      this.timelineEvents.length ? this.timelineEvents : this.events,
      this.events,
      Object.values(this.eventDetails)
    );
    const eventMessages = replayEvents.flatMap((event): ReplayMessage[] => {
      const previewMessages = getGatewayEventPreviewMessages(event);
      if (previewMessages.length) {
        return previewMessages.map((message, index) => ({
          ...message,
          key: `${event.id}:replay:${index}`,
          event,
          eventMessageIndex: index,
          timestamp: event.timestamp,
          title: this.getEventTitle(event),
        }));
      }
      return [
        {
          role: 'message',
          source: 'metadata',
          text: detailedEventIds.has(event.id)
            ? 'No conversation preview captured for this event.'
            : null,
          truncated: !detailedEventIds.has(event.id),
          key: `${event.id}:replay:metadata`,
          event,
          eventMessageIndex: null,
          timestamp: event.timestamp,
          title: this.getEventTitle(event),
        },
      ];
    });
    const activityMessages: ReplayMessage[] =
      this.getAgentControlActivityMessages().map((message, index) => ({
        ...message,
        key: `activity:replay:${index}`,
        event: null,
        eventMessageIndex: null,
        timestamp: message.timestamp || null,
        title: 'Developer message',
      }));
    const messages: ReplayMessage[] = [
      ...eventMessages,
      ...activityMessages,
    ].sort(
      (left, right) =>
        new Date(left.timestamp || 0).getTime() -
        new Date(right.timestamp || 0).getTime()
    );
    return this.replayReversed ? messages.reverse() : messages;
  }

  private getVisibleReplayMessages(): ReplayMessage[] {
    return this.getReplayMessages().filter((message) => {
      if (!message.event) return this.visibleReplayKinds.has('developer');
      const kind = this.getReplayMarkerKind(
        message.event,
        message.role || message.source || 'message'
      );
      return this.visibleReplayKinds.has(kind);
    });
  }

  private mergeReplayEvents(
    ...eventLists: FlowGatewayEvent[][]
  ): FlowGatewayEvent[] {
    const byId = new Map<string, FlowGatewayEvent>();
    for (const event of eventLists.flat()) {
      byId.set(event.id, { ...byId.get(event.id), ...event });
    }
    return Array.from(byId.values()).sort(
      (left, right) =>
        new Date(left.timestamp || 0).getTime() -
        new Date(right.timestamp || 0).getTime()
    );
  }

  private getReplayEventMarkers(
    messages: ReplayMessage[]
  ): ReplayEventMarker[] {
    const markers: ReplayEventMarker[] = [];
    const seenEventIds = new Set<string>();
    messages.forEach((message, index) => {
      if (!message.event || seenEventIds.has(message.event.id)) return;
      seenEventIds.add(message.event.id);
      const role = message.role || message.source || 'message';
      markers.push({
        id: message.event.id,
        index,
        kind: this.getReplayMarkerKind(message.event, role),
        role,
        title: message.title,
        timestamp: message.timestamp,
        failed: this.eventIsFailure(message.event),
      });
    });
    this.getSupportingActivity()
      .filter(
        (item) =>
          item.activity_type.toLowerCase().includes('tool') ||
          item.title.toLowerCase().includes('tool')
      )
      .forEach((item) => {
        const timestamp = item.timestamp || null;
        const itemTime = new Date(timestamp || 0).getTime();
        let nearestIndex = 0;
        let nearestDistance = Number.POSITIVE_INFINITY;
        messages.forEach((message, index) => {
          const messageTime = new Date(message.timestamp || 0).getTime();
          const distance = Math.abs(messageTime - itemTime);
          if (distance < nearestDistance) {
            nearestDistance = distance;
            nearestIndex = index;
          }
        });
        markers.push({
          id: item.api_usage_id || `activity:${item.timestamp}:${item.title}`,
          index: nearestIndex,
          kind: 'tool',
          role: 'tool',
          title: item.title || 'Tool call',
          timestamp,
          failed: String(item.status || '')
            .toLowerCase()
            .includes('fail'),
        });
      });
    return markers;
  }

  private getReplayMarkerKind(
    event: FlowGatewayEvent,
    role: string
  ): ReplayMarkerKind {
    const type = event.type.toLowerCase();
    const payload = event.payload || {};
    if (payload.tool_name || type.includes('tool')) return 'tool';
    const normalized = role.toLowerCase();
    if (normalized.includes('system')) return 'system';
    if (normalized.includes('developer') || normalized.includes('tool')) {
      return 'developer';
    }
    if (normalized.includes('assistant') || normalized.includes('agent')) {
      return 'agent';
    }
    return 'user';
  }

  private stopReplay(): void {
    if (this.replayTimer !== null) {
      window.clearInterval(this.replayTimer);
      this.replayTimer = null;
    }
  }

  private startReplay(): void {
    this.requestReplayMetadata();
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) return;
    this.userScrollingReplay = false;
    this.resumeReplayAfterScroll = false;
    this.replayActive = true;
    this.replayIndex = Math.min(
      Math.max(this.replayIndex, 0),
      messages.length - 1
    );
    this.stopReplay();
    this.replayTimer = window.setInterval(() => {
      if (this.replayIndex >= messages.length - 1) {
        this.stopReplay();
        this.replayActive = false;
        return;
      }
      const nextIndex = this.replayIndex + 1;
      this.replayIndex = nextIndex;
      this.requestReplayCurrentEventDetail(messages[nextIndex]);
      if (this.summarizeVisibleContent) {
        const nextMessage = messages[nextIndex - 1];
        if (
          nextMessage?.event &&
          this.eventNeedsSummary(nextMessage.event) &&
          !this.interactionSummaries[nextMessage.event.id] &&
          !this.loadingInteractionSummaries.has(nextMessage.event.id)
        ) {
          this.requestInteractionSummary(nextMessage.event);
        }
      }
    }, this.replaySpeedMs);
  }

  private pauseReplay(): void {
    this.stopReplay();
    this.replayActive = false;
    this.resumeReplayAfterScroll = false;
  }

  private initializeReplayView(): void {
    this.requestReplayMetadata();
    const messages = this.getVisibleReplayMessages();
    this.replayIndex = this.getInitialReplayIndex(messages);
    this.optimizeFromIndex = 0;
    this.optimizeToIndex = Math.max(messages.length - 1, 0);
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
  }

  private stepReplay(delta: number): void {
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) return;
    if (delta < 0 && this.replayIndex === 0 && this.hasMoreEvents) {
      this.requestMoreEvents();
      return;
    }
    this.replayIndex = Math.min(
      Math.max(this.replayIndex + delta, 0),
      messages.length - 1
    );
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
  }

  private jumpReplayToBoundary(boundary: 'start' | 'end'): void {
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) return;
    this.replayIndex = boundary === 'start' ? 0 : messages.length - 1;
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
  }

  private jumpReplayTo(index: number): void {
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) return;
    this.replayIndex = Math.min(Math.max(index, 0), messages.length - 1);
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
    if (this.summarizeVisibleContent) {
      const message = messages[this.replayIndex];
      if (
        message?.event &&
        this.messageShouldUseEventSummary(message) &&
        !this.interactionSummaries[message.event.id] &&
        !this.loadingInteractionSummaries.has(message.event.id)
      ) {
        this.requestInteractionSummary(message.event);
      }
    }
  }

  private requestReplayCurrentEventDetail(message?: ReplayMessage): void {
    if (!message?.event || this.eventDetails[message.event.id]) return;
    if (getGatewayEventPreviewMessages(message.event).length) return;
    if (this.loadingEventDetails.has(message.event.id)) return;
    this.requestEventDetail(message.event);
  }

  private getInitialReplayIndex(messages: ReplayMessage[]): number {
    if (!messages.length) return 0;
    return this.replayReversed ? 0 : messages.length - 1;
  }

  private setReplayReversed(reversed: boolean): void {
    if (this.replayReversed === reversed) return;
    const currentMessage = this.getVisibleReplayMessages()[this.replayIndex];
    this.replayReversed = reversed;
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) {
      this.replayIndex = 0;
      return;
    }
    const nextIndex = currentMessage
      ? messages.findIndex((message) => message.key === currentMessage.key)
      : -1;
    this.replayIndex =
      nextIndex >= 0 ? nextIndex : this.getInitialReplayIndex(messages);
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
  }

  // The effective optimization scope. optimizeToIndex defaults to 0 (unset);
  // treat that as "the whole session" to match the end slider, which displays
  // `optimizeToIndex || lastIndex`. Without this the slider looks full but the
  // scope is just event 0, so Generate analyzes a single event and returns
  // nothing — the reason the optimize view appeared to do nothing by default.
  private getEffectiveOptimizeBounds(messages = this.getReplayMessages()): {
    fromIndex: number;
    toIndex: number;
  } {
    const lastIndex = Math.max(messages.length - 1, 0);
    const effectiveTo =
      this.optimizeToIndex > 0 ? this.optimizeToIndex : lastIndex;
    const lo = Math.min(this.optimizeFromIndex, effectiveTo);
    const hi = Math.max(this.optimizeFromIndex, effectiveTo);
    return {
      fromIndex: Math.min(Math.max(lo, 0), lastIndex),
      toIndex: Math.min(Math.max(hi, 0), lastIndex),
    };
  }

  private requestOptimization(regenerate = false): void {
    const messages = this.getReplayMessages();
    const { fromIndex, toIndex } = this.getEffectiveOptimizeBounds(messages);
    const sourceKinds = Array.from(this.optimizeSources);
    this.dispatchEvent(
      new CustomEvent('session-optimization-requested', {
        detail: {
          regenerate,
          modelId: this.getSelectedOptimizationModel()?.id || null,
          fromIndex,
          toIndex,
          sourceKinds,
          eventIds: this.getOptimizationEvents(messages).map(
            (event) => event.id
          ),
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  private getDefaultOptimizationModel(): AIModel | null {
    return (
      this.availableModels.find((model) => model.is_default) ||
      this.availableModels[0] ||
      null
    );
  }

  private getSelectedOptimizationModel(): AIModel | null {
    if (this.optimizeModelId) {
      const selected = this.availableModels.find(
        (model) => model.id === this.optimizeModelId
      );
      if (selected) return selected;
    }
    return this.getDefaultOptimizationModel();
  }

  private renderOptimizationRangeMarkers(messages: ReplayMessage[]) {
    const eventMarkers = this.getReplayEventMarkers(messages);
    return html`
      <div class="optimize-range-markers" aria-hidden="true">
        ${eventMarkers.map((marker, markerIndex) => {
          const markerPercent = this.getReplayPositionPercent(
            markerIndex,
            eventMarkers.length
          );
          return html`
            <span
              class="optimize-range-marker ${marker.kind} ${marker.failed
                ? 'failed'
                : ''}"
              style=${`left: ${markerPercent}%;`}
              title=${`${this.formatDateTime(marker.timestamp)} - ${marker.title}`}
            ></span>
            ${this.shouldShowTimelineLabel(markerIndex, eventMarkers.length)
              ? html`
                  <span
                    class="optimize-range-label"
                    style=${`left: ${markerPercent}%;`}
                    title=${this.formatDateTime(marker.timestamp)}
                  >
                    ${this.formatTimelineLabel(marker.timestamp)}
                  </span>
                `
              : nothing}
          `;
        })}
      </div>
    `;
  }

  private getOptimizationEvents(
    messages = this.getReplayMessages()
  ): FlowGatewayEvent[] {
    const { fromIndex, toIndex } = this.getEffectiveOptimizeBounds(messages);
    const byId = new Map<string, FlowGatewayEvent>();
    messages.slice(fromIndex, toIndex + 1).forEach((message) => {
      if (!message.event) return;
      const kind = this.getReplayMarkerKind(
        message.event,
        message.role || message.source || 'message'
      );
      if (!this.optimizeSources.has(kind)) return;
      byId.set(message.event.id, message.event);
    });
    return Array.from(byId.values());
  }

  private toggleReplaySource(kind: ReplayMarkerKind): void {
    const next = new Set(this.visibleReplayKinds);
    if (next.has(kind)) {
      next.delete(kind);
    } else {
      next.add(kind);
    }
    if (!next.size) {
      next.add(kind);
    }
    this.visibleReplayKinds = next;
    const messages = this.getVisibleReplayMessages();
    this.replayIndex = Math.min(
      this.replayIndex,
      Math.max(messages.length - 1, 0)
    );
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
  }

  private toggleOptimizationSource(kind: ReplayMarkerKind): void {
    const next = new Set(this.optimizeSources);
    if (next.has(kind)) {
      next.delete(kind);
    } else {
      next.add(kind);
    }
    if (!next.size) {
      next.add(kind);
    }
    this.optimizeSources = next;
  }

  private handleOptimizationSelected(
    suggestion: SessionOptimizationSuggestion
  ): void {
    // Wire the typed action buttons that the optimization panel emits. These
    // previously fell through to local replay tricks and did nothing useful.
    const action = suggestion.action;
    if (action?.type === 'open_events') {
      const params = action.params || {};
      const eventIds = Array.isArray(params.event_ids)
        ? (params.event_ids as string[])
        : suggestion.evidenceEventIds || [];
      const failedOnly = Boolean(params.failed_only ?? !eventIds.length);
      this.dispatchEvent(
        new CustomEvent('session-inspect-requests', {
          detail: { eventIds, failedOnly, suggestion },
          bubbles: true,
          composed: true,
        })
      );
      return;
    }
    if (action?.type === 'set_budget') {
      this.dispatchEvent(
        new CustomEvent('session-create-budget', {
          detail: { action, suggestion, session: this.session },
          bubbles: true,
          composed: true,
        })
      );
      return;
    }

    this.optimizeControlsOpen = true;
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) return;

    if (suggestion.evidenceEventIds?.length) {
      const evidenceIds = new Set(suggestion.evidenceEventIds);
      this.visibleReplayKinds = new Set(REPLAY_MARKER_KINDS);
      const allMessages = this.getReplayMessages();
      const evidenceIndex = allMessages.findIndex((message) =>
        evidenceIds.has(message.event?.id || '')
      );
      if (evidenceIndex >= 0) {
        this.replayIndex = evidenceIndex;
        this.requestReplayCurrentEventDetail(allMessages[evidenceIndex]);
        return;
      }
    }

    if (suggestion.id === 'fix-failures') {
      this.visibleReplayKinds = new Set(REPLAY_MARKER_KINDS);
      const allMessages = this.getReplayMessages();
      const failedIndex = allMessages.findIndex((message) =>
        this.eventIsFailure(message.event)
      );
      if (failedIndex >= 0) {
        this.replayIndex = failedIndex;
        this.requestReplayCurrentEventDetail(allMessages[failedIndex]);
      }
      return;
    }

    if (
      suggestion.id === 'trim-context' ||
      suggestion.actionLabel.toLowerCase().includes('context')
    ) {
      this.optimizeSources = new Set(['user', 'system', 'developer', 'tool']);
      this.visibleReplayKinds = new Set([
        'user',
        'system',
        'developer',
        'tool',
      ]);
      this.replayIndex = 0;
      this.requestReplayCurrentEventDetail(this.getVisibleReplayMessages()[0]);
      return;
    }

    if (suggestion.actionLabel.toLowerCase().includes('raw')) {
      this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
    }
  }

  private ensureOptimizationBounds(messages: ReplayMessage[]): void {
    if (!messages.length) {
      this.optimizeFromIndex = 0;
      this.optimizeToIndex = 0;
      return;
    }
    const lastIndex = messages.length - 1;
    this.optimizeFromIndex = Math.min(
      Math.max(this.optimizeFromIndex, 0),
      lastIndex
    );
    this.optimizeToIndex =
      this.optimizeToIndex > 0
        ? Math.min(Math.max(this.optimizeToIndex, 0), lastIndex)
        : lastIndex;
  }

  private getReplayPositionPercent(index: number, markerCount: number): number {
    if (markerCount <= 1) return 0;
    return (index / (markerCount - 1)) * 100;
  }

  private shouldShowTimelineLabel(index: number, markerCount: number): boolean {
    if (markerCount <= 3) return true;
    const interval = Math.max(1, Math.ceil(markerCount / 3));
    return index === 0 || index === markerCount - 1 || index % interval === 0;
  }

  private getReplayIndexFromScroll(
    scrollport: HTMLElement,
    messageCount: number
  ): number {
    if (messageCount <= 1) return 0;
    return Math.min(
      Math.max(
        Math.round(scrollport.scrollTop / ESTIMATED_REPLAY_MESSAGE_HEIGHT),
        0
      ),
      Math.max(messageCount - 1, 0)
    );
  }

  private scrollReplayToCurrentMessage(): void {
    if (!this.replayViewActive) return;
    if (this.userScrollingReplay) return;
    if (this.suppressNextReplayAutoScroll) {
      this.suppressNextReplayAutoScroll = false;
      return;
    }
    this.updateComplete.then(() => {
      const scrollport = this.renderRoot.querySelector(
        '.replay-scrollport'
      ) as HTMLElement | null;
      if (!scrollport) return;
      this.autoScrollingReplay = true;
      const targetTop = Math.max(
        0,
        this.replayIndex * ESTIMATED_REPLAY_MESSAGE_HEIGHT -
          scrollport.clientHeight * 0.35
      );
      scrollport.scrollTo({ top: targetTop, behavior: 'auto' });
      window.setTimeout(() => {
        this.autoScrollingReplay = false;
      }, 450);
    });
  }

  private syncReplayTimeFromScroll(): void {
    if (!this.replayViewActive || this.autoScrollingReplay) return;
    this.userScrollingReplay = true;
    if (this.replayActive) {
      this.resumeReplayAfterScroll = true;
    }
    this.stopReplay();
    this.replayActive = false;
    if (this.replayScrollSyncTimer !== null) {
      window.clearTimeout(this.replayScrollSyncTimer);
    }
    this.replayScrollSyncTimer = window.setTimeout(() => {
      this.replayScrollSyncTimer = null;
      const scrollport = this.renderRoot.querySelector(
        '.replay-scrollport'
      ) as HTMLElement | null;
      if (!scrollport) {
        this.userScrollingReplay = false;
        return;
      }
      if (scrollport.scrollTop < 480) {
        this.requestMoreEvents();
      }
      const messages = this.getVisibleReplayMessages();
      const closestIndex = this.getReplayIndexFromScroll(
        scrollport,
        messages.length
      );
      if (closestIndex !== this.replayIndex) {
        this.suppressNextReplayAutoScroll = true;
        this.jumpReplayTo(closestIndex);
      }
      this.userScrollingReplay = false;
      if (this.resumeReplayAfterScroll) {
        this.resumeReplayAfterScroll = false;
        this.startReplay();
      }
    }, REPLAY_SCROLL_RESUME_DELAY_MS);
  }

  private messageShouldUseEventSummary(message: ReplayMessage): boolean {
    if (!message.event) return false;
    return (
      (message.text || '').length > 420 || this.eventNeedsSummary(message.event)
    );
  }

  private renderProgressiveEvent(event: FlowGatewayEvent) {
    const messages = getGatewayEventPreviewMessages(event);
    const userRequest = getGatewayEventUserRequest(event);
    const detail = this.eventDetails[event.id];
    const fullRequestMessages = detail ? this.getRequestMessages(detail) : [];
    const expandedMessages = fullRequestMessages.length
      ? fullRequestMessages
      : detail
        ? getGatewayEventPreviewMessages(detail).map((message, index) => ({
            ...message,
            key: `${event.id}:detail:${index}`,
          }))
        : messages.map((message, index) => ({
            ...message,
            key: `${event.id}:preview:${index}`,
          }));
    const canLoadMore =
      this.rawPayloads &&
      !detail &&
      (Boolean(event.payload?.capture_policy?.conversation_preview_available) ||
        expandedMessages.length === 0);
    const summary = this.interactionSummaries[event.id];
    const showSummary =
      this.summarizeVisibleContent &&
      summary &&
      this.eventNeedsSummary(event) &&
      !this.fullTextEventIds.has(event.id);

    return html`
      <div
        class="timeline-event ${this.eventNeedsSummary(event)
          ? 'summary-candidate'
          : ''}"
        data-event-id=${event.id}
      >
        <div class="event-header">
          <div>
            <div class="event-title">${this.getEventTitle(event)}</div>
            <div class="event-meta">
              ${this.formatTime(event.timestamp)} ·
              ${formatNumber(event.payload?.total_tokens as number)} tokens ·
              ${formatCost(event.payload?.estimated_cost as number)}
            </div>
          </div>
          <sl-badge variant=${this.getOutcomeVariant(event)} pill>
            ${event.payload?.outcome || 'event'}
          </sl-badge>
        </div>
        ${showSummary
          ? html`
              <div class="summary-replacement">
                <div class="event-title">${summary.title}</div>
                <div class="summary-text">${summary.summary}</div>
                ${summary.key_points.length
                  ? html`
                      <ul class="summary-points">
                        ${summary.key_points.map(
                          (point) => html`<li>${point}</li>`
                        )}
                      </ul>
                    `
                  : nothing}
                <div class="detail-actions">
                  <sl-button
                    size="small"
                    @click=${() => this.toggleEventFullText(event.id)}
                  >
                    Show full text
                  </sl-button>
                </div>
              </div>
            `
          : html`
              ${userRequest
                ? html`<div class="preview">${userRequest}</div>`
                : canLoadMore
                  ? html`
                      <div class="preview">
                        Message preview is available on demand. Load details to
                        inspect the captured request without flooding the
                        timeline.
                      </div>
                    `
                  : html`<div class="preview">
                      No user-request preview captured.
                    </div>`}
              ${summary && this.fullTextEventIds.has(event.id)
                ? html`
                    <div class="detail-actions">
                      <sl-button
                        size="small"
                        @click=${() => this.toggleEventFullText(event.id)}
                      >
                        Show summary
                      </sl-button>
                    </div>
                  `
                : nothing}
            `}
        <div class="segment-grid">
          <sl-details>
            <div slot="summary" class="segment-title">
              ${expandedMessages.length
                ? `Request messages (${expandedMessages.length})`
                : 'Request messages available after loading details'}
            </div>
            ${expandedMessages.length
              ? html`
                  <div class="message-list">
                    ${expandedMessages.map((message) =>
                      this.renderMessage(message, 'chat', message.key)
                    )}
                  </div>
                `
              : html`
                  <div class="segment">
                    <div class="event-meta">
                      The compact event list keeps large request/response
                      payloads out of the initial render. Use the details button
                      below when you need the full captured conversation.
                    </div>
                  </div>
                `}
          </sl-details>
          <sl-details>
            <div slot="summary" class="segment-title">
              Token and transport details
            </div>
            <div class="segment">
              <div class="event-meta">
                Endpoint:
                ${event.payload?.endpoint_kind ||
                event.payload?.endpoint ||
                'n/a'}
              </div>
              <div class="event-meta">
                HTTP: ${event.payload?.method || 'POST'}
                ${event.payload?.status_code || ''}
              </div>
              <div class="event-meta">
                Prompt ${formatNumber(event.payload?.prompt_tokens as number)} ·
                Completion
                ${formatNumber(event.payload?.completion_tokens as number)}
              </div>
            </div>
          </sl-details>
        </div>
        ${this.rawPayloads
          ? html`
              <div class="detail-actions">
                <sl-button
                  size="small"
                  ?loading=${this.loadingEventDetails.has(event.id)}
                  @click=${() => this.requestEventDetail(event)}
                >
                  Load raw event
                </sl-button>
              </div>
              ${detail
                ? html`
                    <div class="raw-event-container">
                      <preloop-gateway-event
                        .event=${detail}
                        expanded
                      ></preloop-gateway-event>
                    </div>
                  `
                : nothing}
            `
          : nothing}
      </div>
    `;
  }

  private renderMessage(
    message: FlowGatewayConversationPreviewMessage,
    mode: 'chat',
    key = ''
  ) {
    const role = message.role || message.source || 'message';
    const fullText = this.normalizeMessageText(message);
    const channelLabel = this.getMessageChannelLabel(message);
    const displayText = fullText
      ? fullText
      : message.redacted
        ? 'Content redacted by capture policy.'
        : 'No text content captured.';
    const isLong = displayText.length > 1800;
    const isExpanded = key ? this.expandedMessageKeys.has(key) : false;
    const text =
      isLong && !isExpanded ? `${displayText.slice(0, 1800)}...` : displayText;
    const className = `${mode}-message ${role}`;
    return html`
      <div class=${className}>
        <div class="message-role">
          ${role}
          ${message.truncated
            ? html`<sl-badge variant="warning" pill>Truncated</sl-badge>`
            : nothing}
          ${message.redacted
            ? html`<sl-badge variant="warning" pill>Redacted</sl-badge>`
            : nothing}
        </div>
        <div class="message-text">${text}</div>
        ${channelLabel
          ? html`<div class="message-footer">${channelLabel}</div>`
          : nothing}
        ${isLong
          ? html`
              <div class="detail-actions">
                <sl-button size="small" @click=${() => this.toggleMessage(key)}>
                  ${isExpanded ? 'Collapse message' : 'Show full message'}
                </sl-button>
              </div>
            `
          : nothing}
      </div>
    `;
  }

  private normalizeMessageText(
    message: FlowGatewayConversationPreviewMessage
  ): string | null {
    const text = message.text?.trim();
    if (!text) return null;

    const parsed = this.tryParseJSON(text);
    if (!parsed) return text;

    const extracted = this.extractTextFromUnknown(parsed);
    return extracted || text;
  }

  private tryParseJSON(value: string): unknown | null {
    const trimmed = value.trim();
    if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null;
    try {
      return JSON.parse(trimmed);
    } catch {
      return null;
    }
  }

  private extractTextFromUnknown(value: unknown): string | null {
    if (typeof value === 'string') return value.trim() || null;
    if (!value || typeof value !== 'object') return null;
    if (Array.isArray(value)) {
      return value
        .map((item) => this.extractTextFromUnknown(item))
        .filter(Boolean)
        .join('\n\n')
        .trim();
    }

    const record = value as Record<string, unknown>;
    for (const key of [
      'text',
      'message',
      'body',
      'content',
      'command',
      'prompt',
      'input',
    ]) {
      const extracted = this.extractTextFromUnknown(record[key]);
      if (extracted) return extracted;
    }

    const post = record.post as Record<string, unknown> | undefined;
    const postText = this.extractTextFromUnknown(post?.message ?? post?.text);
    if (postText) return postText;

    return null;
  }

  private getMessageChannelLabel(
    message: FlowGatewayConversationPreviewMessage
  ): string | null {
    const record = message as Record<string, unknown>;
    const raw =
      record.channel ||
      record.channel_name ||
      record.channelName ||
      record.source_channel ||
      record.integration ||
      record.platform ||
      record.source;
    if (typeof raw !== 'string' || !raw.trim()) return null;
    const channel = raw.trim();
    if (channel === message.role) return null;
    return `via ${channel}`;
  }

  private getCharacterLabel(role: string): string {
    const normalized = role.toLowerCase();
    if (normalized.includes('developer') || normalized.includes('tool')) {
      return 'Developer';
    }
    if (normalized.includes('assistant') || normalized.includes('agent')) {
      return 'Agent';
    }
    if (normalized.includes('system')) return 'System';
    return 'User';
  }

  private getReplayMessageFullText(message: ReplayMessage): string | null {
    if (!message.event || message.eventMessageIndex === null) return null;
    const detail = this.eventDetails[message.event.id];
    if (!detail) return null;
    return (
      this.getRequestMessages(detail)[message.eventMessageIndex]?.text || null
    );
  }

  private getEventTotalTokens(event: FlowGatewayEvent | null): number {
    if (!event) return 0;
    return Number(event.payload?.total_tokens || 0);
  }

  private getEventEstimatedCost(event: FlowGatewayEvent | null): number {
    if (!event) return 0;
    return Number(event.payload?.estimated_cost || 0);
  }

  // Read prompt-cache hits robustly across providers. Checks, in order:
  // OpenAI usage_details.prompt_tokens_details.cached_tokens, the same nested
  // under the payload root, then Anthropic cache_read_input_tokens (nested then
  // root). Returns null when no cache field is present (older captures) so the
  // header can omit the annotation gracefully.
  private getEventCachedTokens(event: FlowGatewayEvent | null): number | null {
    if (!event) return null;
    const payload = (event.payload || {}) as Record<string, unknown>;
    const usageDetails = (payload.usage_details ?? null) as Record<
      string,
      unknown
    > | null;
    const candidates: Array<unknown> = [
      (usageDetails?.prompt_tokens_details as Record<string, unknown>)
        ?.cached_tokens,
      (payload.prompt_tokens_details as Record<string, unknown>)?.cached_tokens,
      usageDetails?.cache_read_input_tokens,
      payload.cache_read_input_tokens,
    ];
    for (const candidate of candidates) {
      if (candidate === null || candidate === undefined) continue;
      const value = Number(candidate);
      if (Number.isFinite(value) && value > 0) return value;
    }
    return null;
  }

  private getEventOutcome(event: FlowGatewayEvent | null): string {
    if (!event) return 'unknown';
    return String(event.payload?.outcome || event.payload?.status || 'unknown');
  }

  private getEventStatusCode(event: FlowGatewayEvent | null): number | null {
    if (!event) return null;
    const statusCode = Number(event.payload?.status_code);
    return Number.isFinite(statusCode) && statusCode > 0 ? statusCode : null;
  }

  private eventIsFailure(event: FlowGatewayEvent | null): boolean {
    const outcome = this.getEventOutcome(event).toLowerCase();
    const statusCode = this.getEventStatusCode(event);
    return (
      outcome.includes('fail') ||
      outcome.includes('error') ||
      outcome.includes('denied') ||
      Boolean(statusCode && statusCode >= 400)
    );
  }

  private renderMessageMetrics(message: ReplayMessage) {
    if (!message.event) return nothing;
    const totalTokens = this.getEventTotalTokens(message.event);
    const estimatedCost = this.getEventEstimatedCost(message.event);
    const outcome = this.getEventOutcome(message.event);
    const statusCode = this.getEventStatusCode(message.event);
    const apiUsageId =
      typeof message.event.payload?.api_usage_id === 'string'
        ? message.event.payload.api_usage_id
        : null;
    const upstreamRequestId =
      typeof message.event.payload?.upstream_request_id === 'string'
        ? message.event.payload.upstream_request_id
        : null;
    const gatewayAttempt = Number(message.event.payload?.gateway_attempt || 1);
    const isRetry = Boolean(message.event.payload?.is_retry);
    const retryOfApiUsageId =
      typeof message.event.payload?.retry_of_api_usage_id === 'string'
        ? message.event.payload.retry_of_api_usage_id
        : null;
    const outcomeClass = this.eventIsFailure(message.event)
      ? 'danger'
      : outcome === 'success'
        ? 'success'
        : 'warning';
    return html`
      <div class="message-metrics">
        <span class="metric-pill ${outcomeClass}">
          ${statusCode ? `${statusCode} ` : ''}${outcome}
        </span>
        <span class="metric-pill">${formatNumber(totalTokens)} tokens</span>
        <span class="metric-pill">${formatCost(estimatedCost)}</span>
        ${isRetry || gatewayAttempt > 1
          ? html`<span
              class="metric-pill warning"
              title=${retryOfApiUsageId
                ? `Retry of usage ${retryOfApiUsageId}`
                : 'Gateway retry attempt'}
              >retry #${gatewayAttempt}</span
            >`
          : nothing}
        ${apiUsageId
          ? html`<span class="metric-pill" title=${apiUsageId}
              >usage ${apiUsageId.slice(0, 8)}</span
            >`
          : nothing}
        ${upstreamRequestId
          ? html`<span class="metric-pill" title=${upstreamRequestId}
              >upstream ${upstreamRequestId.slice(0, 8)}</span
            >`
          : nothing}
      </div>
    `;
  }

  private renderReplayMessage(
    message: ReplayMessage,
    isCurrent: boolean,
    index: number
  ) {
    const role = message.role || message.source || 'message';
    const character = this.getCharacterLabel(role);
    const messageEvent = message.event;
    const isLazyMetadata =
      message.source === 'metadata' &&
      messageEvent !== null &&
      !this.eventDetails[messageEvent.id] &&
      !getGatewayEventPreviewMessages(messageEvent).length;
    if (isLazyMetadata && messageEvent) {
      const eventId = messageEvent.id;
      return html`
        <div
          class="replay-message ${index <= this.replayIndex
            ? 'played'
            : ''} ${isCurrent ? 'current' : ''}"
          data-replay-index=${String(index)}
        >
          <div
            class="chat-message metadata replay-detail-placeholder"
            data-event-id=${eventId}
          >
            <sl-spinner></sl-spinner>
            <div>
              <div class="message-role">Loading event content</div>
              <div class="event-meta">
                ${this.formatDateTime(message.timestamp)} · ${message.title}
              </div>
              ${this.renderMessageMetrics(message)}
            </div>
          </div>
        </div>
      `;
    }
    const detailedText = this.getReplayMessageFullText(message);
    let fullText = 'No text content captured.';
    if (detailedText) {
      fullText = detailedText;
    } else if (message.text) {
      fullText = message.text;
    } else if (message.redacted) {
      fullText = 'Content redacted by capture policy.';
    }
    const summary =
      message.event && this.messageShouldUseEventSummary(message)
        ? this.interactionSummaries[message.event.id]
        : null;
    const showFull = this.fullTextEventIds.has(message.key);
    const showSummary =
      summary &&
      this.summarizeVisibleContent &&
      !showFull &&
      this.messageShouldUseEventSummary(message);
    const isLong = fullText.length > 900;
    const shouldTruncate = isLong && !showSummary && !showFull;
    const visibleText = shouldTruncate
      ? `${fullText.slice(0, 900)}...`
      : fullText;
    const className = `chat-message ${role} ${this.eventIsFailure(message.event) ? 'failed' : ''}`;

    return html`
      <div
        class="replay-message ${index <= this.replayIndex
          ? 'played'
          : ''} ${isCurrent ? 'current' : ''}"
        data-replay-index=${String(index)}
      >
        ${showSummary
          ? html`
              <div class="summary-card">
                <div class="character-row">
                  <span class="character-avatar">${character.slice(0, 1)}</span>
                  <div>
                    <div class="event-title">${character}</div>
                    <div class="event-meta">
                      <span title=${this.formatDateTime(message.timestamp)}>
                        ${this.formatTime(message.timestamp)}
                      </span>
                      · ${message.title}
                    </div>
                    ${this.renderMessageMetrics(message)}
                  </div>
                </div>
                <div class="summary-text">${summary.summary}</div>
                <div class="detail-actions">
                  <sl-button
                    size="small"
                    @click=${() => this.showFullReplayMessage(message)}
                  >
                    Show full message
                  </sl-button>
                </div>
              </div>
            `
          : html`
              <div class=${className}>
                <div class="character-row">
                  <span class="character-avatar">${character.slice(0, 1)}</span>
                  <div>
                    <div class="message-role">
                      ${character}
                      ${shouldTruncate
                        ? html`<sl-badge variant="warning" pill
                            >Truncated</sl-badge
                          >`
                        : nothing}
                      ${summary && showFull
                        ? html`<sl-badge variant="primary" pill
                            >Full message</sl-badge
                          >`
                        : nothing}
                    </div>
                    <div class="event-meta">
                      <span title=${this.formatDateTime(message.timestamp)}>
                        ${this.formatTime(message.timestamp)}
                      </span>
                      · ${message.title}
                    </div>
                    ${this.renderMessageMetrics(message)}
                  </div>
                </div>
                <div class="message-text">${visibleText}</div>
                ${summary || shouldTruncate || showFull
                  ? html`
                      <div class="detail-actions">
                        ${summary && showFull
                          ? html`
                              <sl-button
                                size="small"
                                @click=${() =>
                                  this.toggleEventFullText(message.key)}
                              >
                                Show summary
                              </sl-button>
                            `
                          : nothing}
                        ${shouldTruncate
                          ? html`
                              <sl-button
                                size="small"
                                @click=${() =>
                                  this.showFullReplayMessage(message)}
                              >
                                Show full message
                              </sl-button>
                            `
                          : nothing}
                        ${showFull && !summary
                          ? html`
                              <sl-button
                                size="small"
                                @click=${() =>
                                  this.toggleEventFullText(message.key)}
                              >
                                Collapse message
                              </sl-button>
                            `
                          : nothing}
                      </div>
                    `
                  : nothing}
              </div>
            `}
      </div>
    `;
  }

  private getAgentControlActivityMessages(): Array<
    FlowGatewayConversationPreviewMessage & { timestamp?: string | null }
  > {
    return this.activity
      .filter(
        (item) =>
          item.activity_type === 'agent_control_message' && item.summary?.trim()
      )
      .sort(
        (left, right) =>
          new Date(left.timestamp || 0).getTime() -
          new Date(right.timestamp || 0).getTime()
      )
      .map((item) => {
        const metadata = item.metadata ?? {};
        const role =
          typeof metadata.role === 'string' && metadata.role.trim()
            ? metadata.role
            : 'user';
        return {
          role,
          source: 'agent_control',
          text: item.summary,
          timestamp: item.timestamp,
        };
      });
  }

  // Label an agent-control activity turn from its role/source so an operator
  // (talk) message is never shown as a "Developer message" gateway request.
  private getActivityTurnLabel(
    message: FlowGatewayConversationPreviewMessage
  ): string {
    const role = (message.role || '').toLowerCase();
    if (role.includes('assistant') || role.includes('agent')) {
      return 'Agent message';
    }
    if (role.includes('system')) return 'System message';
    if (role.includes('developer')) return 'Developer message';
    // agent_control messages default to operator/talk input.
    return 'Operator message';
  }

  private renderTimelineLegend() {
    return html`
      <div class="timeline-legend" aria-label="Timeline marker legend">
        ${REPLAY_MARKER_LEGEND.map(
          (item) => html`
            <button
              class="legend-item toggle ${this.visibleReplayKinds.has(item.kind)
                ? ''
                : 'off'}"
              type="button"
              aria-pressed=${this.visibleReplayKinds.has(item.kind)}
              @click=${() => this.toggleReplaySource(item.kind)}
            >
              <span class="legend-swatch ${item.kind}"></span>
              ${item.label}
            </button>
          `
        )}
      </div>
    `;
  }

  private renderEventPageSentinel() {
    return this.hasMoreEvents
      ? html`<div class="event-page-sentinel" aria-hidden="true"></div>`
      : nothing;
  }

  private renderReplayControls() {
    const messages = this.getVisibleReplayMessages();
    const eventMarkers = this.getReplayEventMarkers(messages);
    const currentMessage = messages[this.replayIndex];
    const currentMarkerId = currentMessage?.event?.id || null;
    return html`
      <div class="replay-controls">
        <div class="playback-bar">
          <sl-button-group>
            <sl-button
              class="transport-button"
              size="medium"
              title="Jump to start"
              ?disabled=${!messages.length || this.replayIndex <= 0}
              @click=${() => this.jumpReplayToBoundary('start')}
            >
              <sl-icon
                name="skip-backward-fill"
                label="Jump to start"
              ></sl-icon>
            </sl-button>
            <sl-button
              class="transport-button"
              size="medium"
              title="Previous event"
              ?disabled=${!messages.length ||
              (this.replayIndex <= 0 && !this.hasMoreEvents)}
              @click=${() => this.stepReplay(-1)}
            >
              <sl-icon name="chevron-left" label="Previous event"></sl-icon>
            </sl-button>
            <sl-button
              class="transport-button"
              size="medium"
              variant="primary"
              title=${this.replayActive ? 'Pause' : 'Play'}
              ?disabled=${messages.length === 0}
              @click=${() =>
                this.replayActive ? this.pauseReplay() : this.startReplay()}
            >
              <sl-icon
                name=${this.replayActive ? 'pause-fill' : 'play-fill'}
                label=${this.replayActive ? 'Pause' : 'Play'}
              ></sl-icon>
            </sl-button>
            <sl-button
              class="transport-button"
              size="medium"
              title="Next event"
              ?disabled=${!messages.length ||
              this.replayIndex >= messages.length - 1}
              @click=${() => this.stepReplay(1)}
            >
              <sl-icon name="chevron-right" label="Next event"></sl-icon>
            </sl-button>
            <sl-button
              class="transport-button"
              size="medium"
              title="Jump to end"
              ?disabled=${!messages.length ||
              this.replayIndex >= messages.length - 1}
              @click=${() => this.jumpReplayToBoundary('end')}
            >
              <sl-icon name="skip-forward-fill" label="Jump to end"></sl-icon>
            </sl-button>
          </sl-button-group>
          <select
            class="speed-select-native"
            aria-label="Playback speed"
            .value=${String(this.replaySpeedMs)}
            @pointerdown=${(event: Event) => event.stopPropagation()}
            @mousedown=${(event: Event) => event.stopPropagation()}
            @click=${(event: Event) => event.stopPropagation()}
            @keydown=${(event: Event) => event.stopPropagation()}
            @change=${(event: Event) => {
              event.stopPropagation();
              this.replaySpeedMs = Number(
                (event.target as HTMLSelectElement).value || 1200
              );
              if (this.replayActive) this.startReplay();
            }}
          >
            <option value="2400">0.5x</option>
            <option value="1200">1x</option>
            <option value="600">2x</option>
            <option value="250">4x</option>
          </select>
          <sl-button
            class="reverse-button"
            size="small"
            variant=${this.replayReversed ? 'primary' : 'default'}
            title="Reverse playback from newest to oldest"
            @click=${() => this.setReplayReversed(!this.replayReversed)}
          >
            <sl-icon name="arrow-left-right" label="Reverse playback"></sl-icon>
          </sl-button>
          <div class="timeline-wrap">
            <input
              class="timeline-range"
              type="range"
              min="0"
              max=${String(Math.max(messages.length - 1, 0))}
              .value=${String(this.replayIndex)}
              ?disabled=${messages.length === 0}
              @input=${(event: Event) => {
                this.pauseReplay();
                this.jumpReplayTo(
                  Number((event.target as HTMLInputElement).value)
                );
              }}
            />
            <div class="timeline-markers">
              ${eventMarkers.map((marker, markerIndex) => {
                const markerPercent = this.getReplayPositionPercent(
                  markerIndex,
                  eventMarkers.length
                );
                return html`
                  <button
                    class="timeline-marker ${marker.kind} ${marker.id ===
                    currentMarkerId
                      ? 'current'
                      : ''} ${marker.failed ? 'failed' : ''}"
                    style=${`left: ${markerPercent}%;`}
                    title=${`${this.formatDateTime(marker.timestamp)} - ${this.getCharacterLabel(marker.role)} - ${marker.title}`}
                    aria-label=${`Seek to ${this.formatDateTime(marker.timestamp)} ${marker.title}`}
                    @click=${() => {
                      this.pauseReplay();
                      this.jumpReplayTo(marker.index);
                    }}
                  ></button>
                  ${this.shouldShowTimelineLabel(
                    markerIndex,
                    eventMarkers.length
                  )
                    ? html`
                        <span
                          class="timeline-datetime-label"
                          style=${`left: ${markerPercent}%;`}
                          title=${this.formatDateTime(marker.timestamp)}
                        >
                          ${this.formatTimelineLabel(marker.timestamp)}
                        </span>
                      `
                    : nothing}
                `;
              })}
            </div>
            <div class="timeline-label-row">
              <span class="event-meta">Start</span>
              <span class="event-meta">
                ${currentMessage
                  ? html`${this.formatTime(currentMessage.timestamp)} ·
                    ${this.replayIndex + 1} / ${messages.length}`
                  : 'No replay messages'}
              </span>
              <span class="event-meta">End</span>
            </div>
            ${this.renderTimelineLegend()}
          </div>
        </div>
      </div>
    `;
  }

  private renderReplaySession() {
    const messages = this.getVisibleReplayMessages();
    const startIndex = Math.max(
      0,
      this.replayIndex - REPLAY_MESSAGE_WINDOW_BEFORE
    );
    const endIndex = Math.min(
      messages.length,
      this.replayIndex + REPLAY_MESSAGE_WINDOW_AFTER + 1
    );
    const visibleMessages = messages.slice(startIndex, endIndex);
    const topSpacerHeight = startIndex * ESTIMATED_REPLAY_MESSAGE_HEIGHT;
    const bottomSpacerHeight =
      (messages.length - endIndex) * ESTIMATED_REPLAY_MESSAGE_HEIGHT;
    return html`
      <div class="replay-stage">
        ${this.renderEventPageSentinel()}
        ${topSpacerHeight > 0
          ? html`<div
              class="replay-spacer"
              style=${`height: ${topSpacerHeight}px;`}
            ></div>`
          : nothing}
        ${visibleMessages.map((message, offset) =>
          this.renderReplayMessage(
            message,
            startIndex + offset === this.replayIndex,
            startIndex + offset
          )
        )}
        ${bottomSpacerHeight > 0
          ? html`<div
              class="replay-spacer"
              style=${`height: ${bottomSpacerHeight}px;`}
            ></div>`
          : nothing}
      </div>
    `;
  }

  private renderOptimizeView() {
    if (!this.optimizationEnabled || !this.session) {
      return html`<div class="empty">
        Optimization is not enabled for this view.
      </div>`;
    }
    const messages = this.getReplayMessages();
    const lastIndex = Math.max(messages.length - 1, 0);
    const scopedEvents = this.getOptimizationEvents(messages);
    const hasSuggestions = Boolean(this.optimizationSuggestions?.length);
    const showControls = this.optimizeControlsOpen || !hasSuggestions;
    const selectedModel = this.getSelectedOptimizationModel();
    const optimizationTokenUsage = this.optimizationResult?.token_usage;
    const optimizationCost =
      this.optimizationResult?.estimated_optimization_cost || 0;
    return html`
      <div class="optimize-drawer">
        <div class="event-meta-row">
          <div>
            <div class="event-title">Optimization Suggestions</div>
            <div class="event-meta">
              ${hasSuggestions
                ? html`
                    ${this.optimizationResult?.generated_by === 'model'
                      ? `Generated by ${this.optimizationResult.model_name || selectedModel?.name || 'selected model'}`
                      : 'Showing local suggestions for this session.'}
                    ${optimizationTokenUsage?.total_tokens
                      ? html`
                          · ${formatNumber(optimizationTokenUsage.total_tokens)}
                          generation tokens · ${formatCost(optimizationCost)}
                        `
                      : nothing}
                  `
                : html`
                    Generate suggestions with
                    ${selectedModel?.name || 'the account default model'}.
                  `}
            </div>
          </div>
          ${hasSuggestions
            ? html`
                <div class="replay-dialog-actions">
                  <sl-button
                    size="small"
                    @click=${() =>
                      (this.optimizeControlsOpen = !this.optimizeControlsOpen)}
                  >
                    Regenerate
                  </sl-button>
                </div>
              `
            : nothing}
        </div>
        ${showControls
          ? html`
              <div class="optimize-controls">
                <div class="optimize-control-row">
                  <label>
                    <div class="event-meta">Suggestion model</div>
                    <select
                      class="speed-select-native optimization-model-select"
                      .value=${selectedModel?.id || ''}
                      ?disabled=${!this.availableModels.length ||
                      this.loadingOptimization}
                      @change=${(event: Event) => {
                        this.optimizeModelId =
                          (event.target as HTMLSelectElement).value || null;
                      }}
                    >
                      ${this.availableModels.length
                        ? this.availableModels.map(
                            (model) => html`
                              <option value=${model.id}>
                                ${model.name}${model.is_default
                                  ? ' (default)'
                                  : ''}
                              </option>
                            `
                          )
                        : html`<option value="">Local fallback</option>`}
                    </select>
                  </label>
                </div>
                <div class="optimize-control-row">
                  <label class="optimize-range" style="flex: 1 1 100%;">
                    <div class="event-meta">
                      Optimization scope (events
                      ${this.getEffectiveOptimizeBounds(messages).fromIndex} –
                      ${this.getEffectiveOptimizeBounds(messages).toIndex} of
                      ${formatNumber(Math.max(messages.length, 1))})
                    </div>
                    <div class="optimize-range-dual">
                      <input
                        class="timeline-range"
                        type="range"
                        aria-label="Range start"
                        min="0"
                        max=${String(lastIndex)}
                        .value=${String(this.optimizeFromIndex)}
                        @input=${(event: Event) => {
                          const value = Number(
                            (event.target as HTMLInputElement).value
                          );
                          this.optimizeFromIndex = Math.min(
                            value,
                            this.optimizeToIndex || lastIndex
                          );
                        }}
                      />
                      <input
                        class="timeline-range"
                        type="range"
                        aria-label="Range end"
                        min="0"
                        max=${String(lastIndex)}
                        .value=${String(this.optimizeToIndex || lastIndex)}
                        @input=${(event: Event) => {
                          const value = Number(
                            (event.target as HTMLInputElement).value
                          );
                          this.optimizeToIndex = Math.max(
                            value,
                            this.optimizeFromIndex
                          );
                        }}
                      />
                    </div>
                    ${this.renderOptimizationRangeMarkers(messages)}
                  </label>
                </div>
                <div class="source-toggle-row">
                  ${REPLAY_MARKER_LEGEND.map(
                    (item) => html`
                      <button
                        class="legend-item toggle ${this.optimizeSources.has(
                          item.kind
                        )
                          ? ''
                          : 'off'}"
                        type="button"
                        aria-pressed=${this.optimizeSources.has(item.kind)}
                        @click=${() => this.toggleOptimizationSource(item.kind)}
                      >
                        <span class="legend-swatch ${item.kind}"></span>
                        ${item.label}
                      </button>
                    `
                  )}
                </div>
                <div class="event-meta">
                  Scope includes ${formatNumber(scopedEvents.length)}
                  event${scopedEvents.length === 1 ? '' : 's'}.
                </div>
                <div class="detail-actions">
                  <sl-button
                    size="small"
                    variant="primary"
                    ?loading=${this.loadingOptimization}
                    ?disabled=${this.loadingOptimization ||
                    scopedEvents.length === 0}
                    @click=${() => this.requestOptimization(hasSuggestions)}
                  >
                    ${hasSuggestions
                      ? 'Regenerate suggestions'
                      : 'Generate suggestions'}
                  </sl-button>
                </div>
              </div>
            `
          : nothing}
        ${hasSuggestions
          ? html`
              <session-optimization-panel
                .session=${this.session}
                .events=${scopedEvents.length ? scopedEvents : this.events}
                .activity=${this.activity}
                .suggestions=${this.optimizationSuggestions}
                .optimization=${this.optimizationResult}
                .appliedActions=${this.optimizationAppliedActions}
                .applyingSuggestionId=${this.applyingOptimizationSuggestionId}
                @session-optimization-selected=${(event: CustomEvent) => {
                  this.handleOptimizationSelected(event.detail.suggestion);
                }}
              ></session-optimization-panel>
            `
          : this.optimizationResult && !this.loadingOptimization
            ? html`
                <div class="empty">
                  No optimization opportunities found for the selected scope.
                  Widen the range, or run a longer / more tool-heavy session.
                </div>
              `
            : nothing}
      </div>
    `;
  }

  private renderReplayView() {
    const messages = this.getVisibleReplayMessages();
    if (!messages.length) {
      return html`<div class="empty">
        No replayable messages captured for this session.
      </div>`;
    }
    return html`
      <div class="replay-dialog-body replay-view">
        <div class="replay-dialog-header">${this.renderReplayControls()}</div>
        <div
          class="replay-scrollport"
          @scroll=${() => this.syncReplayTimeFromScroll()}
        >
          <div class="replay-transcript">${this.renderReplaySession()}</div>
        </div>
      </div>
    `;
  }

  private getLocalSummary(
    event: FlowGatewayEvent
  ): RuntimeSessionInteractionSummary {
    const userRequest = getGatewayEventUserRequest(event);
    const model =
      event.payload?.model_alias ||
      event.payload?.requested_model ||
      'Model request';
    const endpoint =
      event.payload?.endpoint_kind || event.payload?.endpoint || 'request';
    const outcome = event.payload?.outcome || 'event';
    return {
      event_id: event.id,
      title: `${model} ${outcome}`,
      summary: userRequest
        ? `Captured user request: ${userRequest.slice(0, 360)}`
        : 'No generated summary yet. Generate one to turn the prompt and response into a readable interaction summary.',
      key_points: [
        `${formatNumber(event.payload?.total_tokens as number)} tokens`,
        `${event.payload?.method || 'POST'} ${endpoint}`,
      ],
      risk_level:
        outcome === 'error' || Number(event.payload?.status_code || 0) >= 400
          ? 'high'
          : 'low',
      next_action: null,
      generated_by: 'local',
      model_name: null,
      estimated_summary_cost: 0,
    };
  }

  private renderSummaries() {
    if (!this.events.length) {
      return html`<div class="empty">No model interactions captured.</div>`;
    }
    return html`
      <div class="panel">
        <div class="supporting-note">
          AI summaries are generated on demand with the account default model.
          Use this mode when you want the interaction story first, then expand
          raw prompts only where needed.
        </div>
        ${this.events.map((event) => {
          const summary =
            this.interactionSummaries[event.id] || this.getLocalSummary(event);
          const generated = summary.generated_by === 'model';
          return html`
            <div class="summary-card">
              <div class="event-header">
                <div>
                  <div class="event-title">${summary.title}</div>
                  <div class="event-meta">
                    ${this.formatTime(event.timestamp)} ·
                    ${formatNumber(event.payload?.total_tokens as number)}
                    tokens
                    ${generated && summary.model_name
                      ? html`· summarized by ${summary.model_name}`
                      : ''}
                  </div>
                </div>
                <sl-badge
                  variant=${summary.risk_level === 'high'
                    ? 'danger'
                    : 'neutral'}
                  pill
                >
                  ${generated ? 'AI summary' : 'preview'}
                </sl-badge>
              </div>
              <div class="summary-text">${summary.summary}</div>
              ${summary.key_points.length
                ? html`
                    <ul class="summary-points">
                      ${summary.key_points.map(
                        (point) => html`<li>${point}</li>`
                      )}
                    </ul>
                  `
                : nothing}
              ${summary.next_action
                ? html`<div class="preview">Next: ${summary.next_action}</div>`
                : nothing}
              <div class="detail-actions">
                <sl-button
                  size="small"
                  ?loading=${this.loadingInteractionSummaries.has(event.id)}
                  ?disabled=${generated}
                  @click=${() => this.requestInteractionSummary(event)}
                >
                  ${generated ? 'Summary generated' : 'Generate AI summary'}
                </sl-button>
                <sl-button
                  size="small"
                  @click=${() => this.requestEventDetail(event)}
                >
                  Load full content
                </sl-button>
              </div>
              ${this.eventDetails[event.id]
                ? html`
                    <sl-details>
                      <div slot="summary" class="segment-title">
                        Full captured request messages
                      </div>
                      <div class="message-list">
                        ${this.getRequestMessages(
                          this.eventDetails[event.id]
                        ).map((message) =>
                          this.renderMessage(message, 'chat', message.key)
                        )}
                      </div>
                    </sl-details>
                  `
                : nothing}
            </div>
          `;
        })}
        ${this.renderEventPageSentinel()}
      </div>
    `;
  }

  private getSupportingActivity(): RuntimeSessionActivityItem[] {
    if (!this.events.length) return this.activity;
    return this.activity.filter((item) => {
      if (item.activity_type === 'model_interaction') return false;
      if (item.activity_type === 'model_gateway_call') return false;
      if (this.isToolCallActivity(item)) return false;
      return true;
    });
  }

  private isToolCallActivity(item: RuntimeSessionActivityItem): boolean {
    return item.activity_type === 'tool_call' || Boolean(item.tool_name);
  }

  private getToolCallActivity(): RuntimeSessionActivityItem[] {
    return this.activity.filter((item) => this.isToolCallActivity(item));
  }

  private getTranscriptRowCost(row: TranscriptRow): number {
    if (row.kind === 'event') {
      return Number(row.event.payload?.estimated_cost || 0);
    }
    return Number(row.item.estimated_cost || 0);
  }

  private getTranscriptRows(): TranscriptRow[] {
    const eventRows: TranscriptRow[] = this.events.map((event) => ({
      kind: 'event',
      timestamp: event.timestamp,
      event,
    }));
    const toolRows: TranscriptRow[] = this.getToolCallActivity().map(
      (item) => ({
        kind: 'tool',
        timestamp: item.timestamp || null,
        item,
      })
    );
    let rows: TranscriptRow[];
    switch (this.transcriptFilter) {
      case 'model':
        rows = eventRows;
        break;
      case 'tools':
        rows = toolRows;
        break;
      case 'costly':
        rows = [...eventRows, ...toolRows].filter(
          (row) => this.getTranscriptRowCost(row) > 0
        );
        return rows.sort(
          (left, right) =>
            this.getTranscriptRowCost(right) - this.getTranscriptRowCost(left)
        );
      default:
        rows = [...eventRows, ...toolRows];
    }
    return rows.sort(
      (left, right) =>
        new Date(left.timestamp || 0).getTime() -
        new Date(right.timestamp || 0).getTime()
    );
  }

  // --- Unified sortable chat (turn/delta model) -----------------------------

  // The chat-eligible gateway events, merged across sources and de-duplicated by
  // id, sorted oldest-first. This is the natural request order used as the basis
  // for delta computation; sorting for display happens afterwards.
  private getChatEvents(): FlowGatewayEvent[] {
    const merged = this.mergeReplayEvents(
      this.timelineEvents.length ? this.timelineEvents : this.events,
      this.events,
      Object.values(this.eventDetails)
    );
    return merged.filter((event) => {
      // Keep model gateway requests; drop pure non-request markers that carry no
      // conversation. Tool-only events still flow through as tool turns.
      if (event.type.includes('model_gateway')) return true;
      if (event.payload?.tool_name) return true;
      if (getGatewayEventPreviewMessages(event).length) return true;
      return false;
    });
  }

  // A stable signature for a preview message so the same message re-sent across
  // requests collapses to a single delta entry.
  private getMessageSignature(
    message: FlowGatewayConversationPreviewMessage
  ): string {
    const role = (message.role || message.source || 'message').toLowerCase();
    const text = (message.text || '').trim();
    return `${role}::${text}`;
  }

  private messageIsToolRelated(
    message: FlowGatewayConversationPreviewMessage
  ): boolean {
    const role = (message.role || message.source || '').toLowerCase();
    return role.includes('tool') || role.includes('function');
  }

  // Build turns: one per request, each holding only the messages it added
  // relative to the previous request (the growing-prefix delta) plus this
  // request's assistant response.
  private getChatTurns(): ChatTurn[] {
    const events = this.getChatEvents();
    const seenSignatures = new Set<string>();
    const eventTurns: ChatTurn[] = [];

    events.forEach((event) => {
      const previewMessages = getGatewayEventPreviewMessages(event);
      const deltaMessages: ChatTurnMessage[] = [];
      let toolCallCount = 0;

      previewMessages.forEach((message, messageIndex) => {
        const text = (message.text || '').trim();
        // Skip empty/metadata noise so turns stay readable.
        if (!text && !message.redacted) return;
        const signature = this.getMessageSignature(message);
        // Delta: only render a message the first time we encounter it across
        // the growing message lists.
        if (text && seenSignatures.has(signature)) return;
        if (text) seenSignatures.add(signature);
        const isToolRelated = this.messageIsToolRelated(message);
        if (isToolRelated) toolCallCount += 1;
        deltaMessages.push({
          ...message,
          key: `${event.id}:turn:${messageIndex}`,
          signature,
          isToolRelated,
        });
      });

      eventTurns.push({
        id: event.id,
        index: 0,
        event,
        timestamp: event.timestamp,
        title: this.getEventTitle(event),
        deltaMessages,
        totalTokens: this.getEventTotalTokens(event),
        promptTokens: Number(event.payload?.prompt_tokens || 0),
        completionTokens: Number(event.payload?.completion_tokens || 0),
        cachedTokens: this.getEventCachedTokens(event),
        estimatedCost: this.getEventEstimatedCost(event),
        toolCallCount,
        failed: this.eventIsFailure(event),
        isActivity: false,
      });
    });

    // Fold supporting agent-control messages (operator messages, etc.) inline so
    // the talk dialog's live chat keeps showing them in the flow.
    const activityTurns: ChatTurn[] =
      this.getAgentControlActivityMessages().map((message, index) => ({
        id: `activity:${message.timestamp || ''}:${index}`,
        index: 0,
        event: null,
        timestamp: message.timestamp || null,
        // An agent-control activity is an operator/talk message, NOT a gateway
        // request, so it must not be mislabeled "Developer message".
        title: this.getActivityTurnLabel(message),
        deltaMessages: [
          {
            ...message,
            key: `activity:turn:${index}`,
            signature: this.getMessageSignature(message),
            isToolRelated: this.messageIsToolRelated(message),
          },
        ],
        totalTokens: 0,
        promptTokens: 0,
        completionTokens: 0,
        cachedTokens: null,
        estimatedCost: 0,
        toolCallCount: 0,
        failed: false,
        // Suppress the meaningless 0 tok / $0 / 0 tools stats for activity turns.
        isActivity: true,
      }));

    const turns = [...eventTurns, ...activityTurns].sort(
      (left, right) =>
        new Date(left.timestamp || 0).getTime() -
        new Date(right.timestamp || 0).getTime()
    );
    // Re-index in natural chat order so sort/filter can reorder in place while
    // preserving a stable oldest-first ordering reference.
    turns.forEach((turn, index) => {
      turn.index = index;
    });
    return turns;
  }

  private turnPassesTypeFilter(turn: ChatTurn): boolean {
    if (this.chatTypeFilter === 'all') return true;
    if (this.chatTypeFilter === 'tools') return turn.toolCallCount > 0;
    // messages only: non-tool delta messages present.
    return turn.deltaMessages.some((message) => !message.isToolRelated);
  }

  // Sensible slider ceiling: the costliest/largest turn currently in the
  // thread (in the active unit), with a small floor so the slider is always
  // usable even when every turn is tiny.
  private getChatThresholdMax(turns = this.getChatTurns()): number {
    const requestTurns = turns.filter((turn) => !turn.isActivity);
    if (!requestTurns.length) {
      return this.chatThresholdMode === 'tokens' ? 1000 : 1;
    }
    if (this.chatThresholdMode === 'tokens') {
      const max = Math.max(...requestTurns.map((turn) => turn.totalTokens));
      return Math.max(Math.ceil(max), 100);
    }
    const max = Math.max(...requestTurns.map((turn) => turn.estimatedCost));
    return Math.max(Number(max.toFixed(4)), 0.01);
  }

  private turnPassesThreshold(turn: ChatTurn): boolean {
    if (!this.chatThreshold) return true;
    const value =
      this.chatThresholdMode === 'tokens'
        ? turn.totalTokens
        : turn.estimatedCost;
    return value >= this.chatThreshold;
  }

  // Apply filters then sort in place. Oldest-first is natural chat order.
  private getVisibleChatTurns(): ChatTurn[] {
    const turns = this.getChatTurns().filter(
      (turn) =>
        this.turnPassesTypeFilter(turn) && this.turnPassesThreshold(turn)
    );
    const sorted = [...turns];
    switch (this.chatSort) {
      case 'newest':
        sorted.sort((left, right) => right.index - left.index);
        break;
      case 'costliest':
        sorted.sort((left, right) => right.estimatedCost - left.estimatedCost);
        break;
      case 'cheapest':
        sorted.sort((left, right) => left.estimatedCost - right.estimatedCost);
        break;
      case 'type':
        sorted.sort((left, right) => {
          const leftType = left.toolCallCount > 0 ? 1 : 0;
          const rightType = right.toolCallCount > 0 ? 1 : 0;
          if (leftType !== rightType) return leftType - rightType;
          return left.index - right.index;
        });
        break;
      case 'oldest':
      default:
        sorted.sort((left, right) => left.index - right.index);
        break;
    }
    return sorted;
  }

  // Whole-session cost picture, aggregated from the same per-turn data the chat
  // already computes. Only request turns (gateway calls) carry real
  // tokens/cost/tools; activity turns are excluded from the numeric totals but
  // do not affect the request/outcome counts either.
  private getChatSummary(): {
    totalCost: number;
    totalTokens: number;
    promptTokens: number;
    cachedTokens: number;
    cachedPct: number | null;
    toolCallCount: number;
    requestCount: number;
    okCount: number;
    failedCount: number;
  } {
    const requestTurns = this.getChatTurns().filter((turn) => !turn.isActivity);
    let totalCost = 0;
    let totalTokens = 0;
    let promptTokens = 0;
    let cachedTokens = 0;
    let toolCallCount = 0;
    let okCount = 0;
    let failedCount = 0;
    for (const turn of requestTurns) {
      totalCost += turn.estimatedCost;
      totalTokens += turn.totalTokens;
      promptTokens += turn.promptTokens;
      cachedTokens += turn.cachedTokens ?? 0;
      toolCallCount += turn.toolCallCount;
      if (turn.failed) failedCount += 1;
      else okCount += 1;
    }
    const cachedPct =
      promptTokens > 0 ? Math.round((cachedTokens / promptTokens) * 100) : null;
    return {
      totalCost,
      totalTokens,
      promptTokens,
      cachedTokens,
      cachedPct,
      toolCallCount,
      requestCount: requestTurns.length,
      okCount,
      failedCount,
    };
  }

  // The single highest-cost request turn, used to mark the cost story without
  // forcing the user to sort. Only meaningful when there is real spend (>$0) and
  // more than one request turn to compare.
  private getMostExpensiveTurnId(): string | null {
    const requestTurns = this.getChatTurns().filter((turn) => !turn.isActivity);
    if (requestTurns.length < 2) return null;
    let topTurn: ChatTurn | null = null;
    for (const turn of requestTurns) {
      if (!topTurn || turn.estimatedCost > topTurn.estimatedCost) {
        topTurn = turn;
      }
    }
    if (!topTurn || topTurn.estimatedCost <= 0) return null;
    return topTurn.id;
  }

  private toggleTurnExpanded(eventId: string): void {
    const next = new Set(this.expandedTurnEventIds);
    if (next.has(eventId)) {
      next.delete(eventId);
    } else {
      next.add(eventId);
      // Lazy-load the full payload only when expanded.
      const event = this.getReplayEventById(eventId);
      if (event && !this.eventDetails[eventId]) {
        this.requestEventDetail(event);
      }
    }
    this.expandedTurnEventIds = next;
  }

  private getChatRoleKind(
    message: FlowGatewayConversationPreviewMessage
  ): ReplayMarkerKind {
    const role = (message.role || message.source || 'message').toLowerCase();
    if (role.includes('tool') || role.includes('function')) return 'tool';
    if (role.includes('system')) return 'system';
    if (role.includes('developer')) return 'developer';
    if (role.includes('assistant') || role.includes('agent')) return 'agent';
    return 'user';
  }

  private renderChatBubble(message: ChatTurnMessage) {
    const kind = this.getChatRoleKind(message);
    const role = message.role || message.source || 'message';
    const bubbleRole =
      kind === 'agent'
        ? 'assistant'
        : kind === 'tool'
          ? 'tool'
          : kind === 'user'
            ? 'user'
            : kind;
    const fullText = this.normalizeMessageText(message);
    const displayText = fullText
      ? fullText
      : message.redacted
        ? 'Content redacted by capture policy.'
        : 'No text content captured.';
    const isLong = displayText.length > 1800;
    // Per-message expand state, keyed by this bubble's stable unique key. Each
    // message toggles independently — read and write the SAME key so the right
    // bubble (and only it) expands/collapses.
    const isExpanded = this.expandedMessageKeys.has(message.key);
    const text =
      isLong && !isExpanded ? `${displayText.slice(0, 1800)}...` : displayText;
    return html`
      <div class="chat-message ${bubbleRole}">
        <div class="message-role">
          ${role}
          ${message.truncated
            ? html`<sl-badge variant="warning" pill>Truncated</sl-badge>`
            : nothing}
          ${message.redacted
            ? html`<sl-badge variant="warning" pill>Redacted</sl-badge>`
            : nothing}
        </div>
        <div class="message-text">${text}</div>
        ${isLong
          ? html`
              <div class="detail-actions">
                <sl-button
                  size="small"
                  @click=${() => this.toggleMessage(message.key)}
                >
                  ${isExpanded ? 'Collapse message' : 'Show full message'}
                </sl-button>
              </div>
            `
          : nothing}
      </div>
    `;
  }

  // Compact, secondary-weight cached-tokens annotation (e.g. "14.2k cached")
  // so prompt-cache savings are visible without dominating the header.
  private formatCachedTokens(cached: number): string {
    if (cached >= 1000) {
      return `${(cached / 1000).toFixed(1).replace(/\.0$/, '')}k cached`;
    }
    return `${formatNumber(cached)} cached`;
  }

  private renderChatTurnHeader(turn: ChatTurn, isMostExpensive = false) {
    return html`
      <div class="chat-turn-header">
        <div class="chat-turn-header-main">
          <span class="chat-turn-title">${turn.title}</span>
          ${isMostExpensive
            ? html`<span
                class="chat-turn-cost-chip"
                title="Highest-cost request in this session"
              >
                <sl-icon name="exclamation-triangle"></sl-icon>
                Most expensive
              </span>`
            : nothing}
          <span class="chat-turn-time">${this.formatTime(turn.timestamp)}</span>
        </div>
        <div class="chat-turn-badges">
          ${turn.isActivity
            ? nothing
            : html`
                <span class="chat-turn-stat">
                  ${formatNumber(turn.totalTokens)}
                  tok${turn.cachedTokens
                    ? html` (${this.formatCachedTokens(turn.cachedTokens)})`
                    : nothing}
                </span>
                <span class="chat-turn-stat"
                  >${formatCost(turn.estimatedCost)}</span
                >
                <span
                  class="chat-turn-stat ${turn.toolCallCount
                    ? 'has-tools'
                    : ''}"
                >
                  ${formatNumber(turn.toolCallCount)}
                  tool${turn.toolCallCount === 1 ? '' : 's'}
                </span>
              `}
          ${turn.failed
            ? html`<sl-badge variant="danger" pill>failed</sl-badge>`
            : nothing}
        </div>
      </div>
    `;
  }

  // Drill-down: full request context for an expanded turn. Uses the lazy
  // eventDetails fetch — the complete message list, model, tokens, finish
  // reason, retries, and tools with per-tool schema cost.
  // TODO: deeper nested-tree drill-down (per-message / per-tool sub-trees).
  private renderChatTurnDetail(turn: ChatTurn) {
    if (!turn.event) return nothing;
    const detail = this.eventDetails[turn.event.id];
    if (!detail) {
      const eventId = turn.event.id;
      return html`
        <div class="chat-turn-detail">
          <div class="replay-detail-placeholder" data-event-id=${eventId}>
            <sl-spinner></sl-spinner>
            <span class="event-meta">Loading full request context…</span>
          </div>
        </div>
      `;
    }
    const fullMessages = this.getRequestMessages(detail);
    const payload = detail.payload || {};
    const cachedTokens = this.getEventCachedTokens(detail);
    const tools = Array.isArray(
      (payload.request as Record<string, unknown>)?.tools
    )
      ? ((payload.request as Record<string, unknown>).tools as unknown[])
      : [];
    return html`
      <div class="chat-turn-detail">
        <div class="event-meta">
          Model: ${payload.model_alias || payload.requested_model || 'n/a'} ·
          Prompt ${formatNumber(Number(payload.prompt_tokens || 0))} ·
          Completion ${formatNumber(Number(payload.completion_tokens || 0))}
          ${cachedTokens
            ? html`· Cached ${formatNumber(cachedTokens)}`
            : nothing}
          · Finish: ${payload.finish_reason || 'n/a'}
          ${payload.is_retry || Number(payload.gateway_attempt || 1) > 1
            ? html`· retry #${Number(payload.gateway_attempt || 1)}`
            : nothing}
        </div>
        <sl-details>
          <div slot="summary" class="segment-title">
            Full request context (${fullMessages.length}
            message${fullMessages.length === 1 ? '' : 's'})
          </div>
          <div class="message-list">
            ${fullMessages.length
              ? fullMessages.map((message) =>
                  this.renderMessage(message, 'chat', message.key)
                )
              : html`<div class="event-meta">
                  No request messages captured for this event.
                </div>`}
          </div>
        </sl-details>
        ${tools.length
          ? html`
              <sl-details>
                <div slot="summary" class="segment-title">
                  Tools carried (${tools.length})
                </div>
                <div class="message-list">
                  ${tools.map((tool) => {
                    const record = (tool || {}) as Record<string, unknown>;
                    const fn = (record.function || record) as Record<
                      string,
                      unknown
                    >;
                    const name = String(fn.name || record.name || 'tool');
                    const schemaTokens = Math.ceil(
                      JSON.stringify(tool).length / 4
                    );
                    return html`
                      <div class="tool-row">
                        <div class="tool-row-main">
                          <sl-icon name="wrench-adjustable"></sl-icon>
                          <span class="tool-row-name">${name}</span>
                        </div>
                        <div class="tool-row-chips">
                          <span class="metric-pill"
                            >~${formatNumber(schemaTokens)} schema tok</span
                          >
                        </div>
                      </div>
                    `;
                  })}
                </div>
              </sl-details>
            `
          : nothing}
        ${this.rawPayloads
          ? html`
              <div class="raw-event-container">
                <preloop-gateway-event
                  .event=${detail}
                  expanded
                ></preloop-gateway-event>
              </div>
            `
          : nothing}
      </div>
    `;
  }

  private renderChatTurn(
    turn: ChatTurn,
    mostExpensiveTurnId: string | null = null
  ) {
    const event = turn.event;
    const isMostExpensive = Boolean(
      mostExpensiveTurnId && turn.id === mostExpensiveTurnId
    );
    const expanded = event ? this.expandedTurnEventIds.has(event.id) : false;
    // Lazy AI-summary support: mark turns whose events warrant a summary so the
    // summary IntersectionObserver can request one, and swap bubbles for the
    // summary when one is available and summarisation is enabled.
    const needsSummary = Boolean(event && this.eventNeedsSummary(event));
    const summary =
      event && needsSummary ? this.interactionSummaries[event.id] : null;
    const showSummary =
      this.summarizeVisibleContent &&
      summary &&
      Boolean(event) &&
      !this.fullTextEventIds.has(event?.id || '');
    return html`
      <div
        class="chat-turn ${turn.failed ? 'failed' : ''} ${isMostExpensive
          ? 'most-expensive'
          : ''} ${needsSummary ? 'summary-candidate' : ''}"
        data-event-id=${event ? event.id : nothing}
      >
        ${this.renderChatTurnHeader(turn, isMostExpensive)}
        ${showSummary && summary
          ? html`
              <div class="summary-card">
                <div class="summary-text">${summary.summary}</div>
                ${summary.key_points.length
                  ? html`
                      <ul class="summary-points">
                        ${summary.key_points.map(
                          (point) => html`<li>${point}</li>`
                        )}
                      </ul>
                    `
                  : nothing}
                <div class="detail-actions">
                  <sl-button
                    size="small"
                    @click=${() => event && this.toggleEventFullText(event.id)}
                  >
                    Show full text
                  </sl-button>
                </div>
              </div>
            `
          : html`
              <div class="chat-turn-bubbles">
                ${turn.deltaMessages.length
                  ? repeat(
                      turn.deltaMessages,
                      (message) => message.key,
                      (message) => this.renderChatBubble(message)
                    )
                  : html`<div class="chat-turn-empty">
                      No new messages in this turn (resent context only).
                    </div>`}
              </div>
            `}
        ${event
          ? html`
              <div class="detail-actions">
                <sl-button
                  size="small"
                  ?loading=${this.loadingEventDetails.has(event.id)}
                  @click=${() => this.toggleTurnExpanded(event.id)}
                >
                  ${expanded ? 'Hide full context' : 'Expand full context'}
                </sl-button>
              </div>
              ${expanded ? this.renderChatTurnDetail(turn) : nothing}
            `
          : nothing}
      </div>
    `;
  }

  // Clamp + store the "hide below" threshold. Shared by the number input and
  // the slider so the two stay in sync (and never exceed the slider ceiling).
  private setChatThreshold(value: number): void {
    if (!Number.isFinite(value) || value <= 0) {
      this.chatThreshold = 0;
      return;
    }
    this.chatThreshold = Math.min(value, this.getChatThresholdMax());
  }

  // Whole-session cost picture at a glance: a quiet dashboard line at the top of
  // the chat. Renders in both the talk dialog and the observer (shared
  // component). Suppressed when there are no request turns to summarise.
  private renderChatSummaryBar() {
    const summary = this.getChatSummary();
    if (!summary.requestCount) return nothing;
    return html`
      <div
        class="chat-summary-bar"
        role="group"
        aria-label="Session cost summary"
      >
        <span class="chat-summary-item">
          <span class="chat-summary-label">Cost</span>
          <span class="chat-summary-value"
            >${formatCost(summary.totalCost)}</span
          >
        </span>
        <span class="chat-summary-item">
          <span class="chat-summary-label">Tokens</span>
          <span class="chat-summary-value"
            >${formatNumber(summary.totalTokens)}</span
          >
          ${summary.cachedTokens > 0
            ? html`<span class="chat-summary-sub"
                >(${formatNumber(summary.cachedTokens)}
                cached${summary.cachedPct !== null
                  ? html` · ${summary.cachedPct}%`
                  : nothing})</span
              >`
            : nothing}
        </span>
        <span class="chat-summary-item">
          <span class="chat-summary-label">Tools</span>
          <span
            class="chat-summary-value ${summary.toolCallCount
              ? 'has-tools'
              : ''}"
            >${formatNumber(summary.toolCallCount)}</span
          >
        </span>
        <span class="chat-summary-item">
          <span class="chat-summary-label">Requests</span>
          <span class="chat-summary-value"
            >${formatNumber(summary.requestCount)}</span
          >
        </span>
        <span class="chat-summary-item">
          <span class="chat-summary-label">Outcome</span>
          <span
            class="chat-summary-value ${summary.failedCount
              ? 'has-failures'
              : ''}"
            >${formatNumber(summary.okCount)} ok /
            ${formatNumber(summary.failedCount)} failed</span
          >
        </span>
      </div>
    `;
  }

  private renderChatControlBar(turnCount: number) {
    const thresholdMax = this.getChatThresholdMax();
    const isTokens = this.chatThresholdMode === 'tokens';
    // Token thresholds are integers (step 1 keeps slider + number input exactly
    // in sync); cost steps finely so cents are reachable on the slider.
    const thresholdStep = isTokens ? 1 : 0.01;
    const thresholdMaxLabel = isTokens
      ? `${formatNumber(thresholdMax)} tok`
      : formatCost(thresholdMax);
    return html`
      <div class="chat-control-bar">
        <div class="chat-control-cluster">
          <span class="chat-control-label">Sort</span>
          <select
            class="chat-select"
            aria-label="Sort turns"
            .value=${this.chatSort}
            @change=${(event: Event) => {
              this.chatSort = (event.target as HTMLSelectElement)
                .value as ChatSort;
            }}
          >
            ${CHAT_SORTS.map(
              (option) => html`
                <option
                  value=${option.id}
                  ?selected=${this.chatSort === option.id}
                >
                  ${option.label}
                </option>
              `
            )}
          </select>
          <span class="chat-control-label">Show</span>
          <select
            class="chat-select"
            aria-label="Filter by type"
            .value=${this.chatTypeFilter}
            @change=${(event: Event) => {
              this.chatTypeFilter = (event.target as HTMLSelectElement)
                .value as ChatTypeFilter;
            }}
          >
            ${CHAT_TYPE_FILTERS.map(
              (option) => html`
                <option
                  value=${option.id}
                  ?selected=${this.chatTypeFilter === option.id}
                >
                  ${option.label}
                </option>
              `
            )}
          </select>
        </div>
        <div class="chat-control-cluster">
          <span class="chat-control-label">Hide below</span>
          <input
            class="chat-threshold-input"
            type="number"
            min="0"
            step=${thresholdStep}
            aria-label="Threshold"
            .value=${String(this.chatThreshold || '')}
            @input=${(event: Event) => {
              const value = Number((event.target as HTMLInputElement).value);
              this.setChatThreshold(value);
            }}
          />
          <sl-range
            class="chat-threshold-slider"
            aria-label="Threshold slider"
            label=${`up to ${thresholdMaxLabel}`}
            min="0"
            max=${thresholdMax}
            step=${thresholdStep}
            .value=${Math.min(this.chatThreshold, thresholdMax)}
            .tooltip=${'none'}
            @sl-input=${(event: Event) => {
              const value = Number(
                (event.target as HTMLInputElement & { value: number }).value
              );
              this.setChatThreshold(value);
            }}
          ></sl-range>
          <select
            class="chat-select"
            aria-label="Threshold unit"
            .value=${this.chatThresholdMode}
            @change=${(event: Event) => {
              this.chatThresholdMode = (event.target as HTMLSelectElement)
                .value as ChatThresholdMode;
              // Re-clamp the threshold to the new unit's slider ceiling.
              this.setChatThreshold(this.chatThreshold);
            }}
          >
            <option
              value="tokens"
              ?selected=${this.chatThresholdMode === 'tokens'}
            >
              tokens
            </option>
            <option value="cost" ?selected=${this.chatThresholdMode === 'cost'}>
              $
            </option>
          </select>
          <span class="event-meta">
            ${formatNumber(turnCount)} turn${turnCount === 1 ? '' : 's'}
          </span>
        </div>
      </div>
    `;
  }

  private renderChatView() {
    const turns = this.getVisibleChatTurns();
    const mostExpensiveTurnId = this.getMostExpensiveTurnId();
    return html`
      <div class="panel">
        ${this.renderChatSummaryBar()}
        ${this.renderChatControlBar(turns.length)}
        ${turns.length
          ? html`
              <div class="chat-thread">
                ${repeat(
                  turns,
                  (turn) => turn.id,
                  (turn) => this.renderChatTurn(turn, mostExpensiveTurnId)
                )}
              </div>
            `
          : html`<div class="empty">No turns match the current filters.</div>`}
        ${this.renderEventPageSentinel()}
      </div>
    `;
  }

  private renderTranscriptFilterBar(rowCount: number) {
    return html`
      <div class="transcript-filter-bar">
        <sl-button-group>
          ${TRANSCRIPT_FILTERS.map(
            (filter) => html`
              <sl-button
                size="small"
                variant=${this.transcriptFilter === filter.id
                  ? 'primary'
                  : 'default'}
                @click=${() => (this.transcriptFilter = filter.id)}
              >
                ${filter.label}
              </sl-button>
            `
          )}
        </sl-button-group>
        <span class="event-meta">
          ${formatNumber(rowCount)} row${rowCount === 1 ? '' : 's'}
        </span>
      </div>
    `;
  }

  private renderToolCallRow(item: RuntimeSessionActivityItem) {
    const failed = String(item.status || '')
      .toLowerCase()
      .includes('fail');
    return html`
      <div class="tool-row">
        <div class="tool-row-main">
          <sl-icon name="wrench-adjustable"></sl-icon>
          <span class="tool-row-name">
            ${item.tool_name || item.title || 'Tool call'}
          </span>
          ${item.server_name
            ? html`<span class="event-meta">${item.server_name}</span>`
            : nothing}
          <span class="event-meta">${this.formatTime(item.timestamp)}</span>
        </div>
        <div class="tool-row-chips">
          ${item.total_tokens
            ? html`<span class="metric-pill">
                ${formatNumber(item.total_tokens)} tok
              </span>`
            : nothing}
          ${item.estimated_cost
            ? html`<span class="metric-pill">
                ${formatCost(item.estimated_cost)}
              </span>`
            : nothing}
          ${item.status
            ? html`<sl-badge variant=${failed ? 'danger' : 'neutral'} pill>
                ${item.status}
              </sl-badge>`
            : nothing}
        </div>
      </div>
    `;
  }

  private renderActivityItems() {
    const activity = this.getSupportingActivity();
    if (!activity.length) return nothing;
    const visibleActivity = activity.slice(0, this.visibleActivityCount);
    const hiddenCount = activity.length - visibleActivity.length;
    return html`
      <sl-details class="activity-group">
        <div slot="summary" class="activity-group-summary">
          <span>Supporting activity (${activity.length})</span>
          <span class="event-meta">Session lifecycle and tool activity</span>
        </div>
        <div class="supporting-note">
          Model requests are shown below as replay events. This section only
          keeps supporting activity so the timeline is not duplicated.
        </div>
        <div class="activity-list">
          ${visibleActivity.map(
            (item) => html`
              <div class="activity-event">
                <div class="event-header">
                  <div>
                    <div class="event-title">${item.title}</div>
                    <div class="event-meta">
                      ${this.formatTime(item.timestamp)} ·
                      ${item.activity_type.replace(/_/g, ' ')}
                    </div>
                  </div>
                  ${item.status
                    ? html`<sl-badge pill>${item.status}</sl-badge>`
                    : nothing}
                </div>
                ${item.summary
                  ? html`<div class="preview">${item.summary}</div>`
                  : ''}
              </div>
            `
          )}
        </div>
        ${hiddenCount > 0
          ? html`
              <div class="detail-actions">
                <sl-button
                  size="small"
                  @click=${() => {
                    this.visibleActivityCount += 20;
                  }}
                >
                  Show ${Math.min(hiddenCount, 20)} more
                </sl-button>
              </div>
            `
          : nothing}
      </sl-details>
    `;
  }

  render() {
    if (this.loading) {
      return html`
        <div class="loading">
          <sl-spinner></sl-spinner>
          <div>Loading session replay...</div>
        </div>
      `;
    }

    if (!this.session) {
      return html`<div class="empty">Select a session to inspect it.</div>`;
    }

    if (!this.events.length && !this.activity.length) {
      return html`<div class="empty">
        No interactions captured for this session.
      </div>`;
    }

    if (this.replayMode === 'replay') return this.renderReplayView();
    if (this.replayMode === 'optimize') return this.renderOptimizeView();

    // Unified sortable chat: the 'timeline'/transcript render (observer) and the
    // talk dialog's 'chat' mode both resolve here so both callers get the chat.
    return this.renderChatView();
  }
}
