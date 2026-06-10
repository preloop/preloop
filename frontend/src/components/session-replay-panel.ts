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
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type {
  AIModel,
  FlowGatewayConversationPreviewMessage,
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
  RuntimeSessionInteractionSummary,
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

  @state()
  private visibleActivityCount = 20;

  @state()
  private expandedMessageKeys = new Set<string>();

  @state()
  private fullTextEventIds = new Set<string>();

  @state()
  private replayActive = false;

  @state()
  private replayDialogOpen = false;

  @state()
  private replaySpeedMs = 1200;

  @state()
  private replayIndex = 0;

  @state()
  private replayReversed = false;

  @state()
  private optimizeOpen = false;

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
      align-items: center;
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
      changed.has('replayDialogOpen')
    ) {
      this.updateEventPageObserver();
    }
    if (changed.has('replayIndex') || changed.has('replayDialogOpen')) {
      this.scrollReplayToCurrentMessage();
    }
    if (
      changed.has('replayDialogOpen') ||
      changed.has('timelineEvents') ||
      changed.has('events') ||
      changed.has('eventDetails') ||
      changed.has('loadingEventDetails')
    ) {
      this.updateReplayDetailObserver();
    }
    if (changed.has('timelineEvents') && this.replayDialogOpen) {
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

  private updateReplayDetailObserver(): void {
    this.replayDetailObserver?.disconnect();
    this.replayDetailObserver = null;
    if (!this.replayDialogOpen) return;

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
    const eventMessages = replayEvents.flatMap((event) => {
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
    const activityMessages = this.getAgentControlActivityMessages().map(
      (message, index) => ({
        ...message,
        key: `activity:replay:${index}`,
        event: null,
        eventMessageIndex: null,
        timestamp: message.timestamp || null,
        title: 'Developer message',
      })
    );
    const messages = [...eventMessages, ...activityMessages].sort(
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
          id: item.id,
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
    this.replayDialogOpen = true;
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

  private openReplayDialog(): void {
    this.requestReplayMetadata();
    const messages = this.getVisibleReplayMessages();
    this.replayDialogOpen = true;
    this.replayIndex = this.getInitialReplayIndex(messages);
    this.optimizeFromIndex = 0;
    this.optimizeToIndex = Math.max(messages.length - 1, 0);
    this.requestReplayCurrentEventDetail(messages[this.replayIndex]);
  }

  private closeReplayDialog(): void {
    this.pauseReplay();
    this.replayDialogOpen = false;
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

  private requestOptimization(regenerate = false): void {
    const messages = this.getReplayMessages();
    const fromIndex = Math.min(this.optimizeFromIndex, this.optimizeToIndex);
    const toIndex = Math.max(this.optimizeFromIndex, this.optimizeToIndex);
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

  private toggleOptimizeOpen(): void {
    const nextOpen = !this.optimizeOpen;
    this.optimizeOpen = nextOpen;
    if (nextOpen && !this.optimizationSuggestions?.length) {
      this.requestOptimization(false);
    }
  }

  private getOptimizationEvents(
    messages = this.getReplayMessages()
  ): FlowGatewayEvent[] {
    const fromIndex = Math.min(this.optimizeFromIndex, this.optimizeToIndex);
    const toIndex = Math.max(this.optimizeFromIndex, this.optimizeToIndex);
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
    if (!this.replayDialogOpen) return;
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
    if (!this.replayDialogOpen || this.autoScrollingReplay) return;
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
    const isLazyMetadata =
      message.source === 'metadata' &&
      message.event &&
      !this.eventDetails[message.event.id] &&
      !getGatewayEventPreviewMessages(message.event).length;
    if (isLazyMetadata) {
      const eventId = message.event.id;
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

  private renderChat() {
    const messages = [
      ...this.events.flatMap(getGatewayEventPreviewMessages),
      ...this.getAgentControlActivityMessages(),
    ];
    if (!messages.length) {
      return html`<div class="empty">No conversation preview captured.</div>`;
    }
    return html`
      <div class="message-list">
        ${messages.map((message, index) =>
          this.renderMessage(message, 'chat', `chat:${index}`)
        )}
        ${this.renderEventPageSentinel()}
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

  private renderOptimizationDrawer() {
    if (!this.optimizeOpen || !this.optimizationEnabled || !this.session) {
      return nothing;
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
                  <label class="optimize-range">
                    <div class="event-meta">From event</div>
                    <input
                      class="timeline-range"
                      type="range"
                      min="0"
                      max=${String(lastIndex)}
                      .value=${String(this.optimizeFromIndex)}
                      @input=${(event: Event) => {
                        this.optimizeFromIndex = Number(
                          (event.target as HTMLInputElement).value
                        );
                      }}
                    />
                    ${this.renderOptimizationRangeMarkers(messages)}
                  </label>
                  <label class="optimize-range">
                    <div class="event-meta">To event</div>
                    <input
                      class="timeline-range"
                      type="range"
                      min="0"
                      max=${String(lastIndex)}
                      .value=${String(this.optimizeToIndex || lastIndex)}
                      @input=${(event: Event) => {
                        this.optimizeToIndex = Number(
                          (event.target as HTMLInputElement).value
                        );
                      }}
                    />
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
                @session-optimization-selected=${(event: CustomEvent) => {
                  this.handleOptimizationSelected(event.detail.suggestion);
                }}
              ></session-optimization-panel>
            `
          : nothing}
      </div>
    `;
  }

  private renderReplayDialog() {
    if (!this.replayDialogOpen) return nothing;
    return html`
      <sl-dialog
        class="replay-dialog"
        label="Replay session"
        ?open=${this.replayDialogOpen}
        @sl-hide=${() => this.closeReplayDialog()}
      >
        <div class="replay-dialog-body">
          <div class="replay-dialog-header">
            <div class="replay-title-row">
              <div class="replay-title">
                ${this.session
                  ? `Replay ${this.session.title}`
                  : 'Replay session'}
              </div>
              <div class="replay-dialog-actions">
                ${this.optimizationEnabled
                  ? html`
                      <sl-button
                        size="small"
                        variant=${this.optimizeOpen ? 'primary' : 'default'}
                        @click=${() => this.toggleOptimizeOpen()}
                      >
                        <sl-icon slot="prefix" name="magic"></sl-icon>
                        Optimize
                      </sl-button>
                    `
                  : nothing}
              </div>
            </div>
            ${this.renderOptimizationDrawer()} ${this.renderReplayControls()}
          </div>
          <div
            class="replay-scrollport"
            @scroll=${() => this.syncReplayTimeFromScroll()}
          >
            <div class="replay-transcript">${this.renderReplaySession()}</div>
          </div>
        </div>
      </sl-dialog>
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

  private renderDebug() {
    return html`
      <div class="panel">
        ${this.events.map(
          (event) =>
            html`<preloop-gateway-event
              .event=${this.eventDetails[event.id] || event}
            ></preloop-gateway-event>`
        )}
        ${this.renderEventPageSentinel()}
      </div>
    `;
  }

  private getSupportingActivity(): RuntimeSessionActivityItem[] {
    if (!this.events.length) return this.activity;
    return this.activity.filter((item) => {
      if (item.activity_type === 'model_interaction') return false;
      if (item.activity_type === 'model_gateway_call') return false;
      return true;
    });
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

    if (this.replayMode === 'chat') return this.renderChat();
    if (this.replayMode === 'debug') return this.renderDebug();

    return html`
      <div class="panel">
        <div class="replay-controls">
          <div class="event-meta">
            Replay this session as a timed chat transcript.
          </div>
          <sl-button
            size="small"
            variant="primary"
            ?disabled=${this.getVisibleReplayMessages().length === 0}
            @click=${() => this.openReplayDialog()}
          >
            Open replay
          </sl-button>
        </div>
        ${this.renderReplayDialog()} ${this.renderActivityItems()}
        ${repeat(
          this.events,
          (event) => event.id,
          (event) => this.renderProgressiveEvent(event)
        )}
        ${this.renderEventPageSentinel()}
      </div>
    `;
  }
}
