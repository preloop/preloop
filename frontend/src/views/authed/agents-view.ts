import { LitElement, css, html, unsafeCSS, nothing } from 'lit';
import { Router } from '@vaadin/router';
import { styleMap } from 'lit/directives/style-map.js';
import { customElement, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '../../components/view-header.ts';
import '../../components/preloop-agent-deployer.ts';
import '../../components/preloop-deploy-wizard.ts';
import '../../components/resource-actions.ts';
import '../../components/agent-talk-composer.ts';
import type { ResourceAction } from '../../components/resource-actions.ts';
import {
  fetchWithAuth,
  getAccountAgents,
  removeAccountAgent,
  getAccountGatewayUsageSummary,
  getFlows,
  getAIModels,
  getFeatures,
  updateAccountAgent,
  getUserProfile,
  type ManagedAgentListParams,
} from '../../api';
import type {
  AccountManagedAgentListResponse,
  ManagedAgentSummary,
  AccountGatewayUsageSummaryResponse,
  AIModel,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import { reducedMotionStyles } from '../../styles/reduced-motion';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { getAgentControlState } from '../../utils/agent-control';
import { renderAgentIcon } from '../../utils/agent-icons';

const AVAILABLE_AGENT_KINDS = [
  { value: 'openclaw', label: 'OpenClaw' },
  { value: 'opencode', label: 'OpenCode' },
  { value: 'claude_code', label: 'Claude Code' },
  { value: 'claude_desktop', label: 'Claude Desktop' },
  { value: 'codex', label: 'Codex CLI' },
  { value: 'gemini_cli', label: 'Gemini CLI' },
  { value: 'hermes', label: 'Hermes' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'windsurf', label: 'Windsurf' },
  { value: 'desktop_agent', label: 'Desktop Agent' },
  { value: 'custom', label: 'Custom' },
  { value: 'flows', label: 'Flows' },
];

const AGENT_KIND_VALUES = AVAILABLE_AGENT_KINDS.map((k) => k.value);

const DEFAULT_AGENT_KINDS = AGENT_KIND_VALUES.filter((k) => k !== 'flows');

// Filter selections persist as a *hidden* list so agent kinds added in later
// releases stay visible by default — a kind absent from storage means
// "unknown at save time", never "deselected by the user". (A `claude_desktop`
// agent was once invisible because the persisted selected-list predated it.)
const HIDDEN_AGENT_KINDS_KEY = 'preloopAgentKindsHidden';
const LEGACY_AGENT_KINDS_KEY = 'preloopAgentKinds';

function loadInitialAgentKinds(): string[] {
  try {
    const hidden = localStorage.getItem(HIDDEN_AGENT_KINDS_KEY);
    if (hidden) {
      const hiddenKinds: string[] = JSON.parse(hidden);
      return AGENT_KIND_VALUES.filter((v) => !hiddenKinds.includes(v));
    }
    const legacy = localStorage.getItem(LEGACY_AGENT_KINDS_KEY);
    if (legacy) {
      const selected: string[] = JSON.parse(legacy);
      // Legacy selected-list saves predate `claude_desktop`; its absence
      // there means "unknown at save time", not "deselected by the user".
      return AGENT_KIND_VALUES.filter(
        (v) => selected.includes(v) || v === 'claude_desktop'
      );
    }
  } catch (e) {}
  return DEFAULT_AGENT_KINDS;
}

function persistAgentKinds(selected: string[]): void {
  try {
    const hidden = AGENT_KIND_VALUES.filter((v) => !selected.includes(v));
    localStorage.setItem(HIDDEN_AGENT_KINDS_KEY, JSON.stringify(hidden));
    localStorage.removeItem(LEGACY_AGENT_KINDS_KEY);
  } catch (e) {}
}

const CANVAS_LAYOUT_VERSION = 'polygon-rings-v1';
const CANVAS_CARD_HALF_WIDTH = 160;
const CANVAS_CARD_HALF_HEIGHT = 118;
const CANVAS_CARD_GAP = 48;

// Account-wide gateway totals on the Agents canvas — OK if briefly stale.
// Cache in sessionStorage and refresh in the background so agent list paint
// never waits on the summary endpoint.
const GATEWAY_SUMMARY_CACHE_TTL_MS = 90_000;
const GATEWAY_SUMMARY_CACHE_KEY = 'preloop.agents.gateway_summary.v1';

type CachedGatewaySummary = {
  cachedAt: number;
  data: AccountGatewayUsageSummaryResponse;
};

function readCachedGatewaySummary(options?: {
  allowStale?: boolean;
}): AccountGatewayUsageSummaryResponse | null {
  try {
    const raw = sessionStorage.getItem(GATEWAY_SUMMARY_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedGatewaySummary;
    if (!parsed?.data || typeof parsed.cachedAt !== 'number') {
      return null;
    }
    const isStale = Date.now() - parsed.cachedAt > GATEWAY_SUMMARY_CACHE_TTL_MS;
    // Stale-while-revalidate: still paint expired cache while a background
    // refresh runs (callers pass allowStale for first paint).
    if (isStale && !options?.allowStale) {
      return null;
    }
    return parsed.data;
  } catch {
    return null;
  }
}

function writeCachedGatewaySummary(
  data: AccountGatewayUsageSummaryResponse
): void {
  try {
    const payload: CachedGatewaySummary = { cachedAt: Date.now(), data };
    sessionStorage.setItem(GATEWAY_SUMMARY_CACHE_KEY, JSON.stringify(payload));
  } catch {
    // ignore quota / private-mode failures
  }
}

function gatewaySummaryDays(
  summary: AccountGatewayUsageSummaryResponse
): number {
  // Prefer timeseries length when present; otherwise derive from the period
  // window so include_breakdown=false still yields a sensible tokens/day.
  if (summary.requests_by_day?.length > 0) {
    return summary.requests_by_day.length;
  }
  const start = Date.parse(summary.period_start);
  const end = Date.parse(summary.period_end);
  if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
    return Math.max(1, Math.ceil((end - start) / (24 * 60 * 60 * 1000)));
  }
  return 1;
}

@customElement('agents-view')
export class AgentsView extends LitElement {
  @state() private agents: AccountManagedAgentListResponse | null = null;
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private searchQuery = '';
  @state() private agentKinds: string[] = loadInitialAgentKinds();
  @state() private lastSeenAfter = 'all';
  @state() private flows: any[] = [];
  @state() private aiModels: AIModel[] = [];
  @state() private availableUsers: Array<{
    id: string;
    username: string;
    email: string;
  }> = [];
  @state() private featureFlags: { [key: string]: boolean | string[] } = {};

  @state() private actionAgentId: string | null = null;
  @state() private liveActivity: Record<
    string,
    {
      modelCalls: number;
      toolCalls: number;
      lastActivityAt: string | null;
      lastMessagePreview?: string;
      lastMessageSource?: string;
      currentBubble?: { text: string; source: string; timestamp: number };
      messageQueue?: { text: string; source: string; timestamp: number }[];
      processTimeoutId?: any;
    }
  > = {};

  @state() private gatewaySummary: AccountGatewayUsageSummaryResponse | null =
    null;

  @state() private showOnboardingDialog = false;
  // Used to track agents count from the last fetch to detect new registrations
  private previousAgentCount = -1;
  // Used to track the exact set of agent IDs to detect additions/removals for layout resets
  private previousAgentIds: string[] | null = null;
  // Tracks if the onboarding dialog was automatically opened at least once upon page load
  private hasAutoOpenedOnboarding = false;

  // Switcher state
  @state() private currentView: 'cards' | 'canvas' = 'canvas';

  // VM Provisioning state variables
  @state() private computeFeatureEnabled = false;
  @state() private isEnterprise = false;
  @state() private isAdmin = false;
  @state() private showDeployDialog = false;

  // Canvas Viewport State
  @state() private scale = 1;
  @state() private translateX = 0;
  @state() private translateY = 0;

  // Node Dragging State
  @state() private nodePositions: Record<string, { x: number; y: number }> = {};
  @state() private nodeAnimationState: Record<string, 'entering' | 'exiting'> =
    {};
  private draggingNodeId: string | null = null;
  private nodeStartX = 0;
  private nodeStartY = 0;
  private dragHasMoved = false;
  private exitingCanvasItems = new Map<string, any>();
  private nodeAnimationTimers = new Map<string, number>();

  // Viewport Dragging State
  private isDragging = false;
  private startX = 0;
  private startY = 0;
  private initialPinchDistance = 0;
  private initialPinchScale = 1;
  private activePointers = new Map<number, PointerEvent>();
  private hasLoadedPositions = false;
  private resizeObserver = new ResizeObserver(() => {
    if (Object.keys(this.nodePositions).length > 0) {
      this.fitViewportToPositions(this.nodePositions);
    } else {
      const bounds = this.shadowRoot
        ?.querySelector('.canvas-viewport')
        ?.getBoundingClientRect();
      if (bounds && bounds.width > 0) {
        this.scale = 1;
        this.translateX = bounds.width / 2;
        this.translateY = bounds.height / 2;
      }
    }
  });

  private unsubscribeRealtime?: () => void;
  private refreshTimer: number | null = null;

  static styles = [
    reducedMotionStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        height: 100%;
      }
      .canvas-bubbles-overlay {
        position: absolute;
        inset: 0;
        pointer-events: none;
        z-index: 1000;
        overflow: visible;
      }
      .canvas-bubbles-overlay .agent-speech-bubble {
        bottom: 107px;
        left: 0;
        transform: translateX(-50%);
        z-index: 1000;
      }
      .page {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
        height: 100%;
        overflow-y: auto;
      }
      .filters {
        display: flex;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
        align-items: end;
      }
      .filters sl-input,
      .filters sl-select {
        min-width: 180px;
      }
      .filters sl-input {
        flex: 1 1 280px;
      }
      .agents-toolbar {
        display: flex;
        align-items: end;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
        width: 100%;
      }
      .agents-toolbar .filters {
        flex: 1 1 520px;
        min-width: 0;
      }
      .view-switcher-group {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-medium);
        margin: auto;
      }
      .view-switcher-group sl-radio-group {
        white-space: nowrap;
      }
      .toolbar-divider {
        width: 1px;
        height: 32px;
        background: var(--sl-color-neutral-300);
      }
      @media (max-width: 900px) {
        .view-switcher-group {
          margin-left: 0;
          width: 100%;
          justify-content: flex-end;
        }
        .toolbar-divider {
          display: none;
        }
      }
      .cards {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        gap: var(--sl-spacing-large);
        padding: 1rem 1rem 0 2rem;
      }
      .deploy-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-large);
        margin-bottom: var(--sl-spacing-large);
      }
      @media (max-width: 768px) {
        .deploy-grid {
          grid-template-columns: 1fr;
        }
      }
      .agent-card::part(base) {
        height: 100%;
      }
      .agent-card {
        max-width: 400px;
        cursor: pointer;
      }
      .agent-card:focus-visible::part(base) {
        outline: 2px solid var(--sl-color-primary-500);
        outline-offset: 2px;
      }
      .agent-card.live::part(base) {
        border-color: var(--sl-color-primary-500);
        box-shadow: 0 0 15px rgba(var(--sl-color-primary-500-rgb), 0.2);
      }
      @keyframes glow-pulse {
        0% {
          box-shadow: 0 0 25px 5px rgba(var(--sl-color-success-500-rgb), 0.6);
          border-color: var(--sl-color-success-500);
        }
        100% {
          box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
          border-color: var(--sl-color-neutral-200);
        }
      }
      .agent-card.glowing::part(base) {
        animation: glow-pulse 1.5s ease-out;
      }
      .card-stack {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
        position: relative;
      }
      .title-row,
      .metric-row {
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        align-items: center;
      }
      .title-row {
        align-items: start;
        border-bottom: 1px solid var(--sl-color-neutral-200);
        padding-bottom: var(--sl-spacing-small);
        padding-right: 44px;
      }
      .agent-name {
        font-weight: 700;
        font-size: 1.15rem;
        letter-spacing: -0.01em;
      }
      .agent-meta {
        opacity: 0.7;
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-3x-small);
        overflow-wrap: anywhere;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .label {
        opacity: 0.7;
        font-size: 0.85rem;
        font-weight: 500;
      }
      .value {
        font-weight: 600;
        font-size: 0.95rem;
        text-align: right;
      }
      .card-actions {
        position: absolute;
        top: -8px;
        right: -8px;
        z-index: 2;
      }
      .identity-stack {
        min-width: 0;
      }
      .identity-badges {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
        margin-top: var(--sl-spacing-x-small);
      }
      .identity-badges sl-badge {
        max-width: 100%;
      }
      .model-traffic-failing {
        background: var(--sl-color-danger-50);
        border-left: 3px solid var(--sl-color-danger-600);
        border-radius: 4px;
        color: var(--sl-color-danger-700);
        font-size: 0.85rem;
        margin-bottom: 12px;
        padding: 6px 10px;
      }
      .model-traffic-failing a {
        color: inherit;
        text-decoration: underline;
      }
      .top-action {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        flex-shrink: 0;
      }
      .agent-control-strip {
        border: 1px solid var(--sl-color-primary-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-small);
        background: var(--sl-color-primary-50);
        display: flex;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
        align-items: center;
      }
      .agent-control-copy {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-2x-small);
        min-width: 0;
      }
      .agent-control-title {
        color: var(--sl-color-neutral-900);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-semibold);
      }
      .agent-control-detail {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
      }
      .empty-state {
        border: 1px dashed var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-large);
        color: var(--sl-color-neutral-600);
        background: var(--sl-color-neutral-0);
      }
      :host(:host-context(.sl-theme-dark)) .title-row {
        border-color: var(--sl-color-neutral-800);
      }

      .agent-speech-bubble {
        position: absolute;
        bottom: calc(100% + 12px);
        left: 50%;
        transform: translateX(-50%);
        background: var(--sl-color-neutral-900);
        color: var(--sl-color-neutral-0);
        padding: 8px 12px;
        border-radius: var(--sl-border-radius-medium);
        font-size: var(--sl-font-size-small);
        width: max-content;
        max-width: 280px;
        box-shadow: var(--sl-shadow-large);
        pointer-events: none;
        opacity: 0;
        transition: opacity 0.5s ease;
        z-index: 250;
      }
      .agent-speech-bubble::after {
        content: '';
        position: absolute;
        top: 100%;
        left: 50%;
        margin-left: -6px;
        border-width: 6px;
        border-style: solid;
        border-color: var(--sl-color-neutral-900) transparent transparent
          transparent;
      }
      .agent-speech-bubble.visible {
        opacity: 1;
        animation: bubble-bounce 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
      }
      @media (prefers-color-scheme: dark) {
        .flow-icon {
          filter: invert(0.8) hue-rotate(180deg);
        }
      }

      @keyframes bubble-bounce {
        0% {
          transform: translateX(-50%) translateY(10px) scale(0.9);
          opacity: 0;
        }
        100% {
          transform: translateX(-50%) translateY(0) scale(1);
          opacity: 1;
        }
      }
      .speech-source {
        font-size: 0.7rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        opacity: 0.7;
        margin-bottom: 2px;
      }
      .speech-text {
        overflow: hidden;
        text-overflow: ellipsis;
        display: -webkit-box;
        -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
        line-height: 1.4;
      }
      .agent-speech-bubble.tool-bubble {
        background: var(--sl-color-neutral-100);
        color: var(--sl-color-neutral-800);
        border: 1px solid var(--sl-color-neutral-300);
        box-shadow: 0 4px 12px rgba(var(--sl-color-warning-500-rgb), 0.15);
      }
      .agent-speech-bubble.tool-bubble::after {
        border-color: var(--sl-color-neutral-300) transparent transparent
          transparent;
      }
      .agent-speech-bubble.tool-bubble .speech-source {
        color: var(--sl-color-warning-600);
      }

      /* Canvas specific styles */
      .section-container {
        max-width: 1400px;
        margin: 0 auto;
        padding: 0 2rem;
      }
      .content-bounds {
        width: 100%;
        max-width: 80rem;
        margin: 0 auto;
        padding: 1rem 1rem 0 2rem;
        box-sizing: border-box;
      }
      .page-canvas-wrapper .content-bounds {
        /* Any overrides for canvas wrapper */
      }
      .page-canvas-wrapper {
        display: flex;
        flex-direction: column;
        height: 100%;
        width: 100%;
        position: relative;
        overflow: hidden;
      }
      .canvas-container {
        flex: 1;
        min-height: 500px;
        position: relative;
        overflow: hidden;
        background-color: transparent;
        border-radius: var(--sl-border-radius-medium);
      }
      .canvas-viewport {
        width: 100%;
        height: 100%;
        touch-action: none;
        user-select: none;
        position: absolute;
        inset: 0;
        cursor: grab;
      }
      .canvas-viewport:active {
        cursor: grabbing;
      }
      .canvas-content {
        position: absolute;
        inset: 0;
        transform-origin: 0 0;
        will-change: transform;
      }
      .gateway-node {
        position: absolute;
        left: 0;
        top: 0;
        transform: translate(-50%, -50%);
        display: flex;
        flex-direction: column;
        align-items: center;
        z-index: 10;
        pointer-events: none;
      }
      .gateway-icon {
        width: 80px;
        height: 80px;
        border-radius: 50%;
        background-color: var(--sl-color-primary-600);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 40px;
        box-shadow: var(--sl-shadow-large);
        position: relative;
      }
      .gateway-icon.pulsing::after {
        content: '';
        position: absolute;
        inset: -10px;
        border-radius: 50%;
        border: 2px solid var(--sl-color-primary-500);
        animation: gateway-pulse 2s infinite;
      }
      @keyframes gateway-pulse {
        0% {
          transform: scale(0.8);
          opacity: 0.8;
        }
        100% {
          transform: scale(1.5);
          opacity: 0;
        }
      }
      .gateway-label {
        position: absolute;
        top: calc(100% + 12px);
        background-color: var(
          --sl-panel-background-color,
          var(--sl-color-neutral-0)
        );
        padding: 4px 12px;
        border-radius: 20px;
        font-weight: bold;
        color: var(--sl-color-primary-600);
        box-shadow: var(--sl-shadow-medium);
        border: 1px solid var(--sl-color-neutral-200);
        letter-spacing: 1px;
        width: max-content;
      }
      .agent-node {
        position: absolute;
        transform: translate(-50%, -50%);
        z-index: 5;
        width: 300px;
        touch-action: none;
        cursor: pointer;
        opacity: 1;
        transition: opacity 220ms ease;
      }
      .agent-node.has-bubble {
        z-index: 150;
      }
      .agent-node.entering {
        animation: node-fade-in 240ms ease-out both;
      }
      .agent-node.exiting {
        opacity: 0;
        pointer-events: none;
      }
      .agent-node.dragging {
        z-index: 100;
        cursor: grabbing;
      }
      @keyframes node-fade-in {
        from {
          opacity: 0;
        }
        to {
          opacity: 1;
        }
      }
      .agent-node sl-card {
        width: 100%;
        pointer-events: auto;
        transition:
          transform 0.2s,
          box-shadow 0.2s;
      }
      .agent-node:not(.dragging) sl-card:hover {
        transform: translateY(-4px);
        box-shadow: var(--sl-shadow-large);
      }
      .controls-overlay {
        position: absolute;
        bottom: 24px;
        right: 24px;
        z-index: 20;
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: var(--sl-panel-background-color, var(--sl-color-neutral-0));
        padding: 8px;
        border-radius: var(--sl-border-radius-large);
        box-shadow: var(--sl-shadow-large);
        border: 1px solid var(--sl-color-neutral-200);
      }
      .connection-line {
        position: absolute;
        left: 0;
        top: 0;
        overflow: visible;
        pointer-events: none;
        transform: translate(-50%, -50%);
        width: 1px;
        height: 1px;
        transition: opacity 220ms ease;
      }
      .connection-line.entering {
        animation: node-fade-in 240ms ease-out both;
      }
      .connection-line.exiting {
        opacity: 0;
      }
      @media (prefers-color-scheme: dark) {
        .canvas-container {
          border-color: var(--sl-color-neutral-800);
        }
        .gateway-label {
          border-color: var(--sl-color-neutral-700);
          color: var(--sl-color-primary-400);
        }
        .controls-overlay {
          border-color: var(--sl-color-neutral-400);
        }
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();

    // Restore saved view preference
    const savedView = localStorage.getItem('preloop.agents.view_mode');
    if (savedView === 'cards' || savedView === 'canvas') {
      this.currentView = savedView;
    }

    // Restore saved node positions
    try {
      const savedPositions = localStorage.getItem(
        'preloop.agents.canvas_positions'
      );
      const savedLayoutVersion = localStorage.getItem(
        'preloop.agents.canvas_layout_version'
      );
      if (savedPositions && savedLayoutVersion === CANVAS_LAYOUT_VERSION) {
        this.nodePositions = JSON.parse(savedPositions);
      }
    } catch (e) {
      console.warn('Failed to parse saved canvas positions', e);
    }

    // Hydrate gateway totals from cache immediately (including stale); network
    // refresh runs after the agent list paints.
    if (!this.gatewaySummary) {
      const cached = readCachedGatewaySummary({ allowStale: true });
      if (cached) {
        this.gatewaySummary = cached;
      }
    }

    void this.loadAgents();
    void this.fetchAdminStatus();
    this.connectRealtime();
    requestAnimationFrame(() => {
      this.resizeObserver.observe(this);
    });
  }

  private async fetchAdminStatus() {
    try {
      const user = await getUserProfile();
      this.isAdmin = user?.is_superuser || false;
    } catch (error) {
      console.error('Failed to fetch user profile:', error);
      this.isAdmin = false;
    }
  }

  private async refreshGatewaySummary(): Promise<void> {
    try {
      // Agents canvas only shows totals (tokens + tokens/day) — skip heavy
      // model/session/tool breakdowns.
      const gatewayData = await getAccountGatewayUsageSummary({
        includeBreakdown: false,
      });
      this.gatewaySummary = gatewayData;
      writeCachedGatewaySummary(gatewayData);
    } catch (error) {
      console.error('Failed to refresh gateway usage summary:', error);
    }
  }

  updated(changedProperties: Map<string, unknown>) {
    super.updated?.(changedProperties);
    if (changedProperties.has('currentView')) {
      this.dispatchEvent(
        new CustomEvent('request-full-bleed', {
          detail: this.currentView === 'canvas',
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
    this.resizeObserver.disconnect();
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
    for (const timer of this.nodeAnimationTimers.values()) {
      window.clearTimeout(timer);
    }
    this.nodeAnimationTimers.clear();
  }

  onBeforeLeave() {
    this.dispatchEvent(
      new CustomEvent('request-full-bleed', {
        detail: false,
        bubbles: true,
        composed: true,
      })
    );
  }

  private connectRealtime(): void {
    const scheduleRefresh = () => this.scheduleRefresh();
    const unsubscribers = [
      unifiedWebSocketManager.subscribe('managed_agents', scheduleRefresh),
      unifiedWebSocketManager.subscribe('agent_control', scheduleRefresh),
      unifiedWebSocketManager.subscribe('runtime_sessions', scheduleRefresh),
      unifiedWebSocketManager.subscribe('gateway_activity', (message) =>
        this.handleGatewayActivity(message)
      ),
    ];
    this.unsubscribeRealtime = () => {
      for (const unsubscribe of unsubscribers) {
        unsubscribe();
      }
    };
    void unifiedWebSocketManager.connect();
  }

  private scheduleRefresh(): void {
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.loadAgents();
    }, 250);
  }

  private async fetchUsers(): Promise<
    Array<{ id: string; username: string; email: string }>
  > {
    const response = await fetchWithAuth('/api/v1/users');
    if (!response.ok) {
      return [];
    }
    const data = await response.json();
    return data.users || [];
  }

  private async loadAgents(): Promise<void> {
    this.loading = true;
    this.error = null;

    const params: ManagedAgentListParams = {
      limit: 50,
    };

    const selectedAgentKinds = this.agentKinds.filter((k) => k !== 'flows');
    const allAgentKindsSelected = AVAILABLE_AGENT_KINDS.every(
      (k) => k.value === 'flows' || selectedAgentKinds.includes(k.value)
    );
    let skipAgentsFetch = false;
    if (allAgentKindsSelected) {
      // Send no kind filter so the backend returns everything — including
      // agent kinds this UI does not know about yet. Sending an explicit
      // allowlist here is what once made `claude_desktop` agents invisible.
    } else if (selectedAgentKinds.length > 0) {
      params.agentKind = selectedAgentKinds.join(',');
    } else {
      // Every agent kind is hidden — nothing can match, so skip the agents
      // API call entirely and render the filtered-empty state locally.
      skipAgentsFetch = true;
    }
    // We handle the 'flows' display separately in frontend
    const includeFlows = this.agentKinds.includes('flows');
    const previousCanvasItems = this.getCanvasItems({ includeExiting: false });

    if (this.lastSeenAfter !== 'all') {
      const now = Date.now();
      let ms = 0;
      switch (this.lastSeenAfter) {
        case 'last_10_minutes':
          ms = 10 * 60 * 1000;
          break;
        case 'last_1_hour':
          ms = 60 * 60 * 1000;
          break;
        case 'last_24_hours':
          ms = 24 * 60 * 60 * 1000;
          break;
        case 'last_7_days':
          ms = 7 * 24 * 60 * 60 * 1000;
          break;
      }
      if (ms > 0) {
        params.lastSeenAfter = new Date(now - ms).toISOString();
      }
    }

    let queryPart = this.searchQuery.trim();
    if (queryPart) {
      const tags: Record<string, string> = {};
      let ownerUsername: string | undefined;

      const tagRegex = /tags?:([\w-]+(?:=[\w-]+)?)/g;
      const ownerRegex = /owner:([\w.-]+)/g;

      let match;
      while ((match = tagRegex.exec(queryPart)) !== null) {
        const parts = match[1].split('=');
        tags[parts[0]] = parts[1] || 'true';
      }
      queryPart = queryPart.replace(tagRegex, '').trim();

      while ((match = ownerRegex.exec(queryPart)) !== null) {
        ownerUsername = match[1];
      }
      queryPart = queryPart.replace(ownerRegex, '').trim();

      if (queryPart) params.query = queryPart;
      if (Object.keys(tags).length > 0) params.tags = JSON.stringify(tags);
      if (ownerUsername) params.ownerUsername = ownerUsername;
    }

    try {
      // Agent list first — gateway summary is refreshed separately so it never
      // blocks first paint (cached value may already be showing).
      const emptyAgentsData: AccountManagedAgentListResponse = {
        query: params.query ?? null,
        agent_kind: null,
        last_seen_after: params.lastSeenAfter ?? null,
        status: 'all',
        total: 0,
        limit: params.limit ?? 50,
        offset: 0,
        items: [],
      };
      const [agentsData, flowsData, modelsData, featuresData, users] =
        await Promise.all([
          skipAgentsFetch
            ? Promise.resolve(emptyAgentsData)
            : getAccountAgents(params),
          getFlows(),
          getAIModels().catch(() => [] as AIModel[]),
          getFeatures().catch(() => ({ features: {}, plugins: [] })),
          this.fetchUsers().catch(() => []),
        ]);
      this.aiModels = modelsData;
      void this.refreshGatewaySummary();

      // Check if a new agent was registered while the dialog is open
      if (
        this.showOnboardingDialog &&
        this.previousAgentCount !== -1 &&
        agentsData.items.length > this.previousAgentCount
      ) {
        this.showOnboardingDialog = false;

        // Show success toast
        const alertEl = Object.assign(document.createElement('sl-alert'), {
          variant: 'success',
          duration: 4000,
          closable: true,
          innerHTML: `<sl-icon slot="icon" name="check2-circle"></sl-icon> <strong>Success</strong><br>A new agent was successfully registered!`,
        });
        document.body.append(alertEl);
        alertEl.toast();
      }

      this.agents = agentsData;
      this.previousAgentCount = agentsData.items.length;
      this.featureFlags = featuresData?.features || {};
      this.computeFeatureEnabled = !!this.featureFlags['compute'];
      this.isEnterprise =
        Array.isArray((featuresData as { plugins?: unknown[] })?.plugins) &&
        ((featuresData as { plugins?: unknown[] }).plugins?.length ?? 0) > 0;
      this.availableUsers = users;

      if (!this.hasAutoOpenedOnboarding && this.previousAgentCount === 0) {
        this.showOnboardingDialog = true;
        this.hasAutoOpenedOnboarding = true;
      }

      // Filter flows locally if lastSeenAfter is set (since backend getFlows doesn't support it)
      let activeFlows = includeFlows
        ? Array.isArray(flowsData)
          ? flowsData
          : (flowsData as any).items || []
        : [];

      // Filter flows locally by query
      if (params.query) {
        const lowerQuery = params.query.toLowerCase();
        activeFlows = activeFlows.filter(
          (f: any) =>
            (f.name && f.name.toLowerCase().includes(lowerQuery)) ||
            (f.description && f.description.toLowerCase().includes(lowerQuery))
        );
      }

      if (this.lastSeenAfter !== 'all') {
        const now = Date.now();
        let ms = 0;
        switch (this.lastSeenAfter) {
          case 'last_10_minutes':
            ms = 10 * 60 * 1000;
            break;
          case 'last_1_hour':
            ms = 60 * 60 * 1000;
            break;
          case 'last_24_hours':
            ms = 24 * 60 * 60 * 1000;
            break;
          case 'last_7_days':
            ms = 7 * 24 * 60 * 60 * 1000;
            break;
        }
        this.flows = activeFlows.filter((f: any) => {
          const t = new Date(
            f.execution_stats?.last_seen_at || f.created_at
          ).getTime();
          return now - t <= ms;
        });
      } else {
        this.flows = activeFlows;
      }

      this.updateCanvasItemTransitions(
        this.getCanvasItems({ includeExiting: false }),
        previousCanvasItems
      );

      const items = this.getCanvasItems({ includeExiting: false });
      const currentAgentIds = items.map((item) => item.id).sort();
      const hasMembershipChanged =
        this.previousAgentIds !== null &&
        (currentAgentIds.length !== this.previousAgentIds.length ||
          JSON.stringify(currentAgentIds) !==
            JSON.stringify(this.previousAgentIds));

      if (hasMembershipChanged) {
        // Membership changed (agent added/removed). Trigger a full view reset.
        this.resetView();
      } else {
        this.initializeNodePositions(false);
      }
      this.previousAgentIds = currentAgentIds;
    } catch (error) {
      console.error('Failed to load managed agents or gateway summary:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to load managed agents or gateway summary';
    } finally {
      this.loading = false;
    }
  }

  private initializeNodePositions(forceReset = false) {
    if (!this.agents) return;
    const items = this.getCanvasItems({ includeExiting: false });

    // Detect if agents were added or removed to trigger a layout reset
    const currentAgentIds = [...items.map((item) => item.id)].sort();
    const hasMembershipChanged =
      this.previousAgentIds !== null &&
      (currentAgentIds.length !== this.previousAgentIds.length ||
        JSON.stringify(currentAgentIds) !==
          JSON.stringify(this.previousAgentIds));

    if (hasMembershipChanged) {
      forceReset = true;
    }

    // Always update the tracking list
    this.previousAgentIds = currentAgentIds;

    // Sort items by last active timestamp descending so active nodes get closer slots
    items.sort((a, b) => {
      const aTime = new Date(
        a.execution_stats?.last_seen_at || a.last_seen_at || a.created_at || 0
      ).getTime();
      const bTime = new Date(
        b.execution_stats?.last_seen_at || b.last_seen_at || b.created_at || 0
      ).getTime();
      if (bTime !== aTime) {
        return bTime - aTime;
      }
      return a.id.localeCompare(b.id);
    });
    let newPositions = forceReset ? {} : { ...this.nodePositions };

    // Clean up any stale positions for items that are no longer active
    const activeIds = new Set(items.map((item) => item.id));
    let cleanedAny = false;
    for (const key of Object.keys(newPositions)) {
      if (!activeIds.has(key)) {
        delete newPositions[key];
        cleanedAny = true;
      }
    }

    if (cleanedAny) {
      this.nodePositions = newPositions;
      localStorage.setItem(
        'preloop.agents.canvas_positions',
        JSON.stringify(newPositions)
      );
    }

    const compactStaleLayout = false;
    const lopsidedCompactLayout = false;
    // Compact-layout resets are currently disabled; keep the flags for a
    // future opt-in path without leaving a permanently-false branch.
    void compactStaleLayout;
    void lopsidedCompactLayout;

    const unpositionedAgents = items.filter((a) => !newPositions[a.id]);

    // If nothing to position and not forcing reset, do nothing
    if (!forceReset && unpositionedAgents.length === 0) {
      if (!this.hasLoadedPositions) {
        requestAnimationFrame(() => {
          const success = this.fitViewportToPositions(newPositions);
          if (success) {
            this.hasLoadedPositions = true;
          }
        });
      }
      return;
    }

    const isFirstTime = Object.keys(newPositions).length === 0;
    const isFullLayout = forceReset || isFirstTime;

    if (isFullLayout) {
      // Recompute the entire layout to fit the canvas viewport.
      newPositions = this.computeFittedLayout(items);
    } else {
      unpositionedAgents.forEach((agent) => {
        newPositions[agent.id] = this.findBestBlankCanvasPosition(newPositions);
      });
    }

    localStorage.setItem(
      'preloop.agents.canvas_positions',
      JSON.stringify(newPositions)
    );
    localStorage.setItem(
      'preloop.agents.canvas_layout_version',
      CANVAS_LAYOUT_VERSION
    );
    this.animateNodePositions(newPositions);

    requestAnimationFrame(() => {
      const success = this.fitViewportToPositions(newPositions);
      if (success) {
        this.hasLoadedPositions = true;
      }
    });
  }

  private getCanvasItems(options: { includeExiting?: boolean } = {}) {
    const currentItems = [
      ...(this.agents?.items || []).filter(
        (agent: any) =>
          !this.flows.some((flow: any) => flow.id === agent.session_source_id)
      ),
      ...this.flows,
    ];
    if (!options.includeExiting || this.exitingCanvasItems.size === 0) {
      return currentItems;
    }
    const currentIds = new Set(currentItems.map((item: any) => item.id));
    return [
      ...currentItems,
      ...Array.from(this.exitingCanvasItems.values()).filter(
        (item: any) => !currentIds.has(item.id)
      ),
    ];
  }

  private updateCanvasItemTransitions(
    currentItems: any[],
    previousItems: any[]
  ) {
    const currentIds = new Set(currentItems.map((item: any) => item.id));
    const previousItemsById = new Map(
      previousItems.map((item: any) => [item.id, item])
    );
    const previousIds = new Set(previousItemsById.keys());

    for (const item of currentItems) {
      if (!previousIds.has(item.id)) {
        this.markNodeAnimation(item.id, 'entering', 280);
      }
      this.exitingCanvasItems.delete(item.id);
    }

    for (const previousId of previousIds) {
      if (
        currentIds.has(previousId) ||
        this.exitingCanvasItems.has(previousId)
      ) {
        continue;
      }
      const previousItem = previousItemsById.get(previousId);
      if (!previousItem) continue;
      this.exitingCanvasItems.set(previousId, previousItem);
      this.markNodeAnimation(previousId, 'exiting', 260, () => {
        this.exitingCanvasItems.delete(previousId);
        const { [previousId]: _removed, ...rest } = this.nodeAnimationState;
        this.nodeAnimationState = rest;
        this.requestUpdate();
      });
    }
  }

  private markNodeAnimation(
    id: string,
    state: 'entering' | 'exiting',
    durationMs: number,
    onDone?: () => void
  ) {
    const existing = this.nodeAnimationTimers.get(id);
    if (existing !== undefined) {
      window.clearTimeout(existing);
    }
    this.nodeAnimationState = { ...this.nodeAnimationState, [id]: state };
    const timer = window.setTimeout(() => {
      this.nodeAnimationTimers.delete(id);
      if (onDone) {
        onDone();
        return;
      }
      const { [id]: _removed, ...rest } = this.nodeAnimationState;
      this.nodeAnimationState = rest;
    }, durationMs);
    this.nodeAnimationTimers.set(id, timer);
  }

  private findBestBlankCanvasPosition(
    positions: Record<string, { x: number; y: number }>
  ): { x: number; y: number } {
    const stepX = CANVAS_CARD_HALF_WIDTH * 2 + CANVAS_CARD_GAP;
    const stepY = CANVAS_CARD_HALF_HEIGHT * 2 + CANVAS_CARD_GAP;
    const occupied = Object.values(positions);
    const candidates = this.getCompactCanvasCandidates(
      occupied.length + 12,
      occupied.length + 1
    ).sort(
      (a, b) =>
        this.canvasCandidateScore(a, occupied) -
        this.canvasCandidateScore(b, occupied)
    );

    return (
      candidates.find(
        (candidate) =>
          !occupied.some(
            (pos) =>
              Math.abs(pos.x - candidate.x) < stepX &&
              Math.abs(pos.y - candidate.y) < stepY
          )
      ) || { x: 360 + occupied.length * 42, y: 280 }
    );
  }

  private canvasCandidateScore(
    candidate: { x: number; y: number },
    occupied: Array<{ x: number; y: number }>
  ): number {
    const distanceFromGateway = Math.hypot(candidate.x, candidate.y);
    if (occupied.length === 0) return distanceFromGateway;
    const nearest = Math.min(
      ...occupied.map((pos) =>
        Math.hypot(pos.x - candidate.x, pos.y - candidate.y)
      )
    );
    return distanceFromGateway - nearest * 0.08;
  }

  private getCompactCanvasCandidates(
    desiredCount: number,
    visibleCount = desiredCount
  ): Array<{ x: number; y: number }> {
    const candidates: Array<{ x: number; y: number }> = [];
    const minDx = 2 * CANVAS_CARD_HALF_WIDTH + CANVAS_CARD_GAP;
    const minDy = 2 * CANVAS_CARD_HALF_HEIGHT + CANVAS_CARD_GAP;
    const aspect = minDx / minDy;

    const minRFromGateway = 240; // Safe distance to avoid overlapping gateway

    let remainingVisible = visibleCount > 0 ? visibleCount : desiredCount;
    let layerIdx = 0;
    let prevR = 0;

    while (candidates.length < desiredCount) {
      let N = remainingVisible > 0 ? Math.min(8, remainingVisible) : 8;
      if (layerIdx === 0 && visibleCount > 1) {
        N = Math.max(6, N);
      }
      remainingVisible -= N;

      let R = layerIdx === 0 ? minRFromGateway : prevR + minDy;

      while (true) {
        let overlap = false;
        const currentLayerPts: Array<{ x: number; y: number }> = [];
        const thetaOffset = -Math.PI / 2;

        for (let i = 0; i < N; i++) {
          const theta = thetaOffset + (2 * Math.PI * i) / N;
          currentLayerPts.push({
            x: Math.round(R * aspect * Math.cos(theta)),
            y: Math.round(R * Math.sin(theta)),
          });
        }

        // Same-layer overlap check
        for (let i = 0; i < N; i++) {
          for (let j = i + 1; j < N; j++) {
            if (
              Math.abs(currentLayerPts[i].x - currentLayerPts[j].x) < minDx &&
              Math.abs(currentLayerPts[i].y - currentLayerPts[j].y) < minDy
            ) {
              overlap = true;
              break;
            }
          }
          if (overlap) break;
        }

        // Cross-layer overlap check
        if (!overlap) {
          for (const pt of currentLayerPts) {
            for (const cand of candidates) {
              if (
                Math.abs(cand.x - pt.x) < minDx &&
                Math.abs(cand.y - pt.y) < minDy
              ) {
                overlap = true;
                break;
              }
            }
            if (overlap) break;
          }
        }

        if (!overlap) {
          for (const pt of currentLayerPts) {
            candidates.push(pt);
          }
          prevR = R;
          break;
        }

        R += 10;
      }

      layerIdx++;
    }

    // If we have fewer items than candidates in the first layer, distribute them better
    if (layerIdx === 1 && candidates.length > desiredCount) {
      const distributed: Array<{ x: number; y: number }> = [];
      const step = candidates.length / desiredCount;
      for (let i = 0; i < desiredCount; i++) {
        distributed.push(candidates[Math.floor(i * step)]);
      }
      return distributed;
    }

    return candidates.slice(0, desiredCount);
  }

  private shouldCompactCanvasLayout(
    items: Array<{ id: string }>,
    positions: Record<string, { x: number; y: number }>
  ): boolean {
    if (items.length === 0) return false;
    const positionedItems = items.filter((item) => positions[item.id]);
    if (positionedItems.length === 0) return false;

    let maxDistance = 0;
    for (const item of positionedItems) {
      const pos = positions[item.id];
      maxDistance = Math.max(maxDistance, Math.hypot(pos.x, pos.y));
    }

    const compactCandidates = this.getCompactCanvasCandidates(
      positionedItems.length,
      positionedItems.length
    );
    const expectedMaxDistance = Math.max(
      ...compactCandidates
        .slice(0, Math.max(positionedItems.length, 1))
        .map((pos) => Math.hypot(pos.x, pos.y))
    );

    return maxDistance > expectedMaxDistance + 220;
  }

  private shouldRebalanceCompactCanvasLayout(
    items: Array<{ id: string }>,
    positions: Record<string, { x: number; y: number }>
  ): boolean {
    if (items.length < 4) return false;
    const positionedItems = items.filter((item) => positions[item.id]);
    if (positionedItems.length !== items.length) return false;

    let leftCount = 0;
    let rightCount = 0;
    let topCount = 0;
    let bottomCount = 0;
    let maxDistance = 0;
    for (const item of positionedItems) {
      const pos = positions[item.id];
      if (pos.x < -40) leftCount += 1;
      if (pos.x > 40) rightCount += 1;
      if (pos.y < -40) topCount += 1;
      if (pos.y > 40) bottomCount += 1;
      maxDistance = Math.max(maxDistance, Math.hypot(pos.x, pos.y));
    }

    // Only auto-rebalance compact generated layouts. Very distant layouts are
    // likely hand-arranged by the user and should be left alone.
    return (
      (Math.abs(leftCount - rightCount) > 1 ||
        Math.abs(topCount - bottomCount) > 2) &&
      maxDistance < 1400
    );
  }

  private computeFittedLayout(
    items: Array<{ id: string }>
  ): Record<string, { x: number; y: number }> {
    const positions: Record<string, { x: number; y: number }> = {};
    if (items.length === 0) return positions;

    const minDx = 2 * CANVAS_CARD_HALF_WIDTH + CANVAS_CARD_GAP;
    const minDy = 2 * CANVAS_CARD_HALF_HEIGHT + CANVAS_CARD_GAP;
    const candidates = this.getCompactCanvasCandidates(
      items.length + 16,
      items.length
    );

    for (const item of items) {
      const occupied = Object.values(positions);
      const candidate =
        candidates.find(
          (pos) =>
            !occupied.some(
              (occupiedPos) =>
                Math.abs(occupiedPos.x - pos.x) < minDx &&
                Math.abs(occupiedPos.y - pos.y) < minDy
            )
        ) || this.findBestBlankCanvasPosition(positions);
      positions[item.id] = candidate;
    }

    return positions;
  }

  /**
   * Adjust the viewport's scale/translate so every node in `positions`
   * is visible with a comfortable margin. Allow a modest auto-zoom on
   * roomy canvases so small fleets stay readable in marketing captures
   * and day-to-day use.
   */
  private fitViewportToPositions(
    positions: Record<string, { x: number; y: number }>
  ): boolean {
    const viewport = this.shadowRoot?.querySelector(
      '.canvas-viewport'
    ) as HTMLElement | null;
    const bounds = viewport?.getBoundingClientRect();
    if (!bounds || bounds.width === 0 || bounds.height === 0) return false;

    const items = this.getCanvasItems({ includeExiting: false });
    const itemMap = new Map(items.map((item) => [item.id, item]));
    const ids = Object.keys(positions).filter((id) => itemMap.has(id));

    if (ids.length === 0) {
      this.scale = 1;
      this.translateX = bounds.width / 2;
      this.translateY = bounds.height / 2;
      return true;
    }

    const getTimestamp = (id: string) => {
      const item = itemMap.get(id);
      if (!item) return 0;
      return new Date(
        item.execution_stats?.last_seen_at ||
          item.last_seen_at ||
          item.created_at ||
          0
      ).getTime();
    };

    const sortedIds = ids
      .filter((id) => positions[id])
      .sort((a, b) => getTimestamp(b) - getTimestamp(a));

    const MIN_READABLE_SCALE = 0.75;
    const MIN_ITEMS_TO_FIT = Math.min(sortedIds.length, 6);

    let targetScale = 0.5;
    let finalMinY = 0;
    let finalMaxY = 0;

    for (let k = sortedIds.length; k >= MIN_ITEMS_TO_FIT; k--) {
      const subsetIds = sortedIds.slice(0, k);

      let minX = 0;
      let maxX = 0;
      let minY = 0;
      let maxY = 0;
      for (const id of subsetIds) {
        const pos = positions[id];
        if (pos.x < minX) minX = pos.x;
        if (pos.x > maxX) maxX = pos.x;
        if (pos.y < minY) minY = pos.y;
        if (pos.y > maxY) maxY = pos.y;
      }

      const cardHalfW = CANVAS_CARD_HALF_WIDTH;
      const cardHalfH = CANVAS_CARD_HALF_HEIGHT;
      const sideMargin = 56;
      const topMargin = 44;
      const bottomMargin = 44;

      const paddedMinX = minX - (cardHalfW + sideMargin);
      const paddedMaxX = maxX + (cardHalfW + sideMargin);
      const paddedMinY = minY - (cardHalfH + topMargin);
      const paddedMaxY = maxY + (cardHalfH + bottomMargin);

      const halfWidth = Math.max(Math.abs(paddedMinX), Math.abs(paddedMaxX), 1);
      const scaleX = bounds.width / 2 / halfWidth;
      const contentHeight = Math.max(paddedMaxY - paddedMinY, 1);
      const scaleY = bounds.height / contentHeight;

      const maxAutoScale = 1.25;
      const currentScale = Math.min(scaleX, scaleY, maxAutoScale);

      if (currentScale >= MIN_READABLE_SCALE || k === MIN_ITEMS_TO_FIT) {
        targetScale = currentScale;
        finalMinY = paddedMinY;
        finalMaxY = paddedMaxY;
        break;
      }
    }

    this.scale = targetScale;
    this.translateX = bounds.width / 2;
    const contentCenterY = (finalMinY + finalMaxY) / 2;
    this.translateY = bounds.height / 2 - contentCenterY * targetScale;
    return true;
  }

  private animatePositionFrameId: number | null = null;

  private animateNodePositions(
    targetPositions: Record<string, { x: number; y: number }>
  ) {
    if (this.animatePositionFrameId) {
      cancelAnimationFrame(this.animatePositionFrameId);
    }

    const startPositions = { ...this.nodePositions };
    const startTime = performance.now();
    const duration = 600;

    const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3);

    const animate = (time: number) => {
      const elapsed = time - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const ease = easeOutCubic(progress);

      const currentPositions: Record<string, { x: number; y: number }> = {};
      let allDone = progress >= 1;

      for (const id in targetPositions) {
        const target = targetPositions[id];
        const start = startPositions[id] || target;
        currentPositions[id] = {
          x: start.x + (target.x - start.x) * ease,
          y: start.y + (target.y - start.y) * ease,
        };
      }

      this.nodePositions = currentPositions;

      if (!allDone) {
        this.animatePositionFrameId = requestAnimationFrame(animate);
      } else {
        this.animatePositionFrameId = null;
        this.nodePositions = targetPositions; // ensure exact final values
      }
    };

    this.animatePositionFrameId = requestAnimationFrame(animate);
  }

  private handleSearchInput(event: Event): void {
    const target = event.target as HTMLInputElement;
    this.searchQuery = target.value;

    // Add debounce for search query filtering
    if ((this as any)._searchTimeout)
      clearTimeout((this as any)._searchTimeout);
    (this as any)._searchTimeout = setTimeout(() => {
      void this.loadAgents();
    }, 400);
  }

  private handleSearchSubmit(event: Event): void {
    event.preventDefault();
    void this.loadAgents();
  }

  private handleAgentKindChange(kind: string, checked: boolean): void {
    if (kind === 'all') {
      this.agentKinds = checked
        ? AVAILABLE_AGENT_KINDS.map((k) => k.value)
        : [];
    } else {
      let updated = [...this.agentKinds];
      if (checked) {
        if (!updated.includes(kind)) updated.push(kind);
      } else {
        updated = updated.filter((k) => k !== kind);
      }
      this.agentKinds = updated;
    }

    persistAgentKinds(this.agentKinds);
    void this.loadAgents();
  }

  private handleLastSeenAfterChange(event: Event): void {
    const target = event.target as HTMLSelectElement;
    this.lastSeenAfter = target.value || 'all';
    void this.loadAgents();
  }

  private getSourceLabel(sourceType: string | null | undefined): string {
    switch (sourceType) {
      case 'claude_code':
        return 'Claude Code';
      case 'claude_desktop':
        return 'Claude Desktop';
      case 'codex':
        return 'Codex';
      case 'openclaw':
        return 'OpenClaw';
      case 'gemini_cli':
        return 'Gemini CLI';
      case 'opencode':
        return 'OpenCode';
      case 'hermes':
        return 'Hermes';
      case 'cursor':
        return 'Cursor';
      case 'windsurf':
        return 'Windsurf';
      case 'vscode':
        return 'VS Code';
      case 'antigravity':
        return 'Antigravity';
      case 'devin':
        return 'Devin';
      case 'desktop_agent':
        return 'Desktop Agent';
      case 'custom':
        return 'Custom Agent';
      default:
        return sourceType || 'Unknown';
    }
  }

  private formatMoney(amount: number | null | undefined): string {
    return `$${(amount || 0).toFixed(2)}`;
  }

  private formatDateTime(value: string | null | undefined): string {
    if (!value) return 'None';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  private getLifecycleVariant(agent: ManagedAgentSummary): string {
    if (agent.lifecycle_state === 'decommissioned') return 'danger';
    if (agent.lifecycle_state === 'suspended') return 'warning';
    if (agent.activity_status === 'active_now') return 'success';
    if (agent.activity_status === 'recently_active') return 'primary';
    if (agent.ended_at) return 'neutral';
    return 'primary';
  }

  private getLifecycleLabel(agent: ManagedAgentSummary): string {
    if (agent.lifecycle_state === 'decommissioned') return 'Decommissioned';
    if (agent.lifecycle_state === 'suspended') return 'Suspended';
    if (agent.activity_status === 'active_now') return 'Active now';
    if (agent.activity_status === 'recently_active') return 'Recently active';
    if (agent.ended_at) return 'Ended';
    return 'Idle';
  }

  private getOnboardingVariant(agent: ManagedAgentSummary): string {
    if (agent.onboarding_state === 'fully_onboarded') return 'success';
    if (agent.onboarding_state === 'mcp_proxy_only') return 'warning';
    if (agent.onboarding_state === 'gateway_only') return 'warning';
    return 'neutral';
  }

  private getOnboardingLabel(agent: ManagedAgentSummary): string {
    if (agent.onboarding_state === 'fully_onboarded') return 'Fully onboarded';
    if (agent.onboarding_state === 'mcp_proxy_only') return 'MCP only';
    if (agent.onboarding_state === 'gateway_only') return 'Gateway only';
    return 'Incomplete';
  }

  private getOnboardingDescription(agent: ManagedAgentSummary): string {
    if (agent.total_requests > 0) return '';
    if (agent.onboarding_state === 'fully_onboarded') {
      return 'Tool calls and model traffic both flow through Preloop.';
    }
    if (agent.onboarding_state === 'mcp_proxy_only') {
      return 'Tool calls flow through Preloop, but model traffic is still direct.';
    }
    if (agent.onboarding_state === 'gateway_only') {
      return 'Model traffic flows through Preloop, but MCP tool traffic is still direct.';
    }
    return 'This agent is not fully managed by Preloop yet.';
  }

  private isMcpConfigured(agent: ManagedAgentSummary): boolean {
    return !!agent.mcp_proxy_configured;
  }

  private isModelConfigured(agent: ManagedAgentSummary): boolean {
    return !!agent.model_gateway_configured;
  }

  private getLiveValidationVariant(agent: ManagedAgentSummary): string {
    if (!agent.live_validation_supported) return 'neutral';
    if (agent.live_validation_status === 'passed') return 'success';
    if (agent.live_validation_status === 'failed') return 'danger';
    if (agent.live_validation_status === 'throttled') return 'warning';
    if (agent.live_validation_status === 'not_run') return 'neutral';
    return 'warning';
  }

  private getLiveValidationLabel(agent: ManagedAgentSummary): string {
    if (!agent.live_validation_supported) return 'No live check';
    if (agent.live_validation_status === 'passed') return 'Live validated';
    if (agent.live_validation_status === 'failed') return 'Live check failed';
    if (agent.live_validation_status === 'throttled')
      return 'Live check throttled — unverified';
    if (agent.live_validation_status === 'upstream_unavailable')
      return 'Upstream refused — unverified';
    // ``not_run`` means the CLI was never invoked with ``--live-validate`` —
    // it's an opt-in step, not a check that's currently in flight.
    if (agent.live_validation_status === 'not_run') return 'Live check not run';
    return 'Live check pending';
  }

  private extractPreviewFromRequest(
    request: any
  ): { text: string; source: string } | null {
    console.log(
      '[Canvas] extractPreviewFromRequest parsing API JSON Payload',
      request
    );
    let text = '';

    const messages = request.messages || request.input || [];
    if (Array.isArray(messages) && messages.length > 0) {
      const lastItem: any = messages[messages.length - 1];
      if (lastItem.role === 'assistant' && Array.isArray(lastItem.content)) {
        const toolUsePart = lastItem.content.find(
          (part: any) => part.type === 'tool_use' || part.type === 'tool_call'
        );
        if (toolUsePart) {
          console.log(
            '[Canvas] extractPreviewFromRequest found tool use:',
            toolUsePart.name
          );
          return { text: `Running: ${toolUsePart.name}`, source: 'Tool' };
        }
      }

      // Find the last non-assistant message or just the last message
      const userMsg = [...messages]
        .reverse()
        .find((m: any) => m.role === 'user');
      const lastMsg: any = userMsg || lastItem;
      if (lastMsg.content) {
        if (Array.isArray(lastMsg.content)) {
          const textPart = lastMsg.content.find(
            (part: any) => part.type === 'text' || part.type === 'input_text'
          );
          text = textPart ? textPart.text : JSON.stringify(lastMsg.content);
        } else if (typeof lastMsg.content === 'string') {
          text = lastMsg.content;
        } else {
          text = JSON.stringify(lastMsg.content);
        }
      }
    } else if (request.prompt) {
      text =
        typeof request.prompt === 'string'
          ? request.prompt
          : JSON.stringify(request.prompt);
    }

    if (text) {
      console.log(
        '[Canvas] extractPreviewFromRequest resolving bubble display:',
        text.substring(0, 50) + '...'
      );
      return { text: text.substring(0, 300), source: 'User' };
    }
    return null;
  }

  private extractPreviewFromGatewayResponse(
    response: any
  ): { text: string; source: string } | null {
    if (!response || typeof response !== 'object') return null;

    const choiceMessage = response.choices?.[0]?.message;
    const choiceText =
      typeof choiceMessage?.content === 'string'
        ? choiceMessage.content
        : this.extractTextFromContentParts(choiceMessage?.content);
    if (choiceText) {
      return { text: choiceText.substring(0, 300), source: 'AI Model' };
    }

    if (
      typeof response.output_text === 'string' &&
      response.output_text.trim()
    ) {
      return {
        text: response.output_text.substring(0, 300),
        source: 'AI Model',
      };
    }

    const outputText = this.extractTextFromContentParts(response.output);
    if (outputText) {
      return { text: outputText.substring(0, 300), source: 'AI Model' };
    }

    return null;
  }

  private extractTextFromContentParts(content: any): string {
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';

    const fragments: string[] = [];
    for (const item of content) {
      if (typeof item === 'string') {
        fragments.push(item);
      } else if (item && typeof item === 'object') {
        const itemRecord = item as Record<string, any>;
        if (typeof itemRecord.text === 'string') {
          fragments.push(itemRecord.text);
        } else if (typeof itemRecord.content === 'string') {
          fragments.push(itemRecord.content);
        } else if (Array.isArray(itemRecord.content)) {
          const nested = this.extractTextFromContentParts(itemRecord.content);
          if (nested) fragments.push(nested);
        }
      }
    }
    return fragments.join('\n').trim();
  }

  private enqueueBubble(agentId: string, text: string, source: string) {
    if (!text || !text.trim()) return;

    const state = this.liveActivity[agentId] || {
      modelCalls: 0,
      toolCalls: 0,
      lastActivityAt: null,
    };

    const isVisible =
      state.currentBubble && Date.now() - state.currentBubble.timestamp < 6000;
    const isDuplicate =
      (isVisible && state.currentBubble?.text === text) ||
      state.messageQueue?.some((b: any) => b.text === text);
    if (isDuplicate) return;

    const bubble = { text, source, timestamp: Date.now() };

    const nextState = {
      ...state,
      messageQueue: [...(state.messageQueue || []), bubble],
    };
    this.liveActivity = { ...this.liveActivity, [agentId]: nextState };

    this.processBubbleQueue(agentId);
  }

  private processBubbleQueue(agentId: string) {
    const state = this.liveActivity[agentId];
    if (!state) return;

    if (
      state.currentBubble &&
      Date.now() - state.currentBubble.timestamp < 2400
    ) {
      if ((state.messageQueue || []).length > 0 && !state.processTimeoutId) {
        const timeoutId = setTimeout(() => {
          this.liveActivity[agentId].processTimeoutId = null;
          this.processBubbleQueue(agentId);
        }, 2500);
        this.liveActivity = {
          ...this.liveActivity,
          [agentId]: { ...state, processTimeoutId: timeoutId as any },
        };
      }
      return;
    }

    const queue = state.messageQueue || [];
    if (queue.length > 0) {
      const nextBubble = { ...queue[0], timestamp: Date.now() };
      const nextState = {
        ...state,
        currentBubble: nextBubble,
        messageQueue: queue.slice(1),
        processTimeoutId: null,
      };

      // Set the next state
      this.liveActivity = { ...this.liveActivity, [agentId]: nextState };

      // Schedule clearing/next item
      const timeoutId = setTimeout(() => {
        this.liveActivity[agentId].processTimeoutId = null;
        this.processBubbleQueue(agentId);
      }, 2500);
      this.liveActivity[agentId].processTimeoutId = timeoutId as any;

      setTimeout(() => this.requestUpdate(), 6000);
    }
  }

  private handleGatewayActivity(message: any): void {
    const payload = message?.payload ?? {};
    const type = message?.type;

    console.log(`[Canvas/Dashboard] Raw Event received: ${type}`, message);

    let agentId = payload.managed_agent_id || payload.flow_id;
    const sessionId =
      payload.session_id ||
      payload.runtime_session_id ||
      message.runtime_session_id;

    if (!agentId && payload.execution_id) {
      const flowExec = this.flows.find(
        (f: any) => f.id === payload.flow_id || f.id === payload.execution_id
      );
      if (flowExec) agentId = flowExec.id;
    }
    if (!agentId && sessionId) {
      const agentWithSession = (this.agents?.items || []).find(
        (a: any) => a.runtime_session_id === sessionId
      );
      if (agentWithSession) agentId = agentWithSession.id;
    }

    if (!agentId) {
      console.log(
        '[Canvas] Cannot resolve agentId for event. managed: ',
        payload.managed_agent_id,
        ' session: ',
        sessionId
      );
      return;
    }

    let preview: { text: string; source: string } | undefined = undefined;

    if (type === 'model_gateway_request_started' && payload.request) {
      preview = this.extractPreviewFromRequest(payload.request) || undefined;
    } else if (type === 'model_gateway_call_started' && payload.request) {
      preview = this.extractPreviewFromRequest(payload.request) || undefined;
    } else if (type === 'flow_execution_started') {
      preview = {
        text: payload.resolved_input_prompt || 'User triggered flow...',
        source: 'User',
      };
    } else if (
      type === 'model_gateway_call' ||
      type === 'model_gateway_call_completed'
    ) {
      preview =
        this.extractPreviewFromGatewayResponse(payload.response) || undefined;
      if (!preview && payload.conversation_preview?.messages?.length > 0) {
        const messages = payload.conversation_preview.messages;
        const last = messages[messages.length - 1];
        preview = {
          text: last.text || '(No text)',
          source: last.source === 'request' ? 'Agent' : 'AI Model',
        };
      }
    } else if (
      (payload.messages &&
        Array.isArray(payload.messages) &&
        payload.messages.length > 0) ||
      (payload.input &&
        Array.isArray(payload.input) &&
        payload.input.length > 0)
    ) {
      preview = this.extractPreviewFromRequest(payload) || undefined;
    } else if (
      type === 'mcp_call' ||
      type === 'mcp_call_started' ||
      type === 'tool_execution_started' ||
      type === 'mcp_tool_call' ||
      type === 'mcp_gateway_call_started' ||
      type === 'mcp_gateway_call' ||
      (type && (type.includes('tool') || type.includes('mcp')))
    ) {
      const toolName =
        payload.tool_name || payload.name || payload.action || 'Tool';
      const serverName = payload.server_name ? payload.server_name + '/' : '';
      const status = payload.status === 'failed' ? 'Failed: ' : 'Running: ';
      preview = {
        text: `${status}${serverName}${toolName}`,
        source: 'Tool',
      };
    }

    if (preview) {
      this.enqueueBubble(agentId, preview.text, preview.source);
    }

    const previous = this.liveActivity[agentId] ?? {
      modelCalls: 0,
      toolCalls: 0,
      lastActivityAt: null,
    };

    const current = this.liveActivity[agentId] ?? previous;
    const t = type || '';
    const isModelCall =
      t.includes('model_gateway_call') || preview?.source === 'AI Model';
    const isToolCall =
      t.includes('mcp') || t.includes('tool') || preview?.source === 'Tool';

    const next = {
      ...current,
      modelCalls: previous.modelCalls + (isModelCall ? 1 : 0),
      toolCalls: previous.toolCalls + (isToolCall ? 1 : 0),
      lastActivityAt:
        payload.timestamp ??
        payload.last_activity_at ??
        previous.lastActivityAt ??
        new Date().toISOString(),
      lastMessagePreview: preview?.text ?? previous.lastMessagePreview,
      lastMessageSource: preview?.source ?? previous.lastMessageSource,
    };

    this.liveActivity = { ...this.liveActivity, [agentId]: next };
    this.agents = {
      ...(this.agents as any),
      items: this.agents!.items.map((agent) =>
        agent.id !== agentId
          ? agent
          : {
              ...agent,
              activity_status: 'active_now',
              last_seen_at: next.lastActivityAt ?? agent.last_seen_at,
              last_activity_at: next.lastActivityAt ?? agent.last_activity_at,
              last_request_at:
                type === 'model_gateway_call'
                  ? (next.lastActivityAt ?? agent.last_request_at)
                  : agent.last_request_at,
            }
      ),
    };
  }

  private async removeAgent(agent: ManagedAgentSummary): Promise<void> {
    if (
      !window.confirm(
        `Remove ${agent.display_name} from the managed agents list?\n\nThis also revokes the agent's Preloop credentials: if this agent is still onboarded on a machine, its gateway and MCP access will stop working until you run \`preloop agents onboard\` again. To disconnect cleanly, run \`preloop agents offboard\` on that machine instead.`
      )
    )
      return;
    this.actionAgentId = agent.id;
    try {
      await removeAccountAgent(agent.id);
      await this.loadAgents();
    } catch (error) {
      console.error('Failed to remove managed agent:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to remove managed agent';
    } finally {
      this.actionAgentId = null;
    }
  }

  private async updateAgent(
    agent: ManagedAgentSummary,
    payload: Parameters<typeof updateAccountAgent>[1]
  ): Promise<void> {
    this.actionAgentId = agent.id;
    try {
      await updateAccountAgent(agent.id, payload);
      await this.loadAgents();
    } catch (error) {
      console.error('Failed to update managed agent:', error);
      this.error =
        error instanceof Error
          ? error.message
          : 'Failed to update managed agent';
    } finally {
      this.actionAgentId = null;
    }
  }

  private promptRenameAgent(agent: ManagedAgentSummary): void {
    const newName = window.prompt(
      'Enter the new name for this agent:',
      agent.display_name
    );
    if (newName !== null && newName.trim() !== '') {
      void this.updateAgent(agent, { display_name: newName.trim() });
    }
  }

  private promptEditAgentTags(agent: ManagedAgentSummary): void {
    const currentTags = Object.entries(agent.tags || {})
      .map(([key, value]) =>
        value && value !== 'true' ? `${key}=${value}` : key
      )
      .join(' ');
    const input = window.prompt(
      'Edit tags as space-separated key or key=value entries:',
      currentTags
    );
    if (input === null) return;

    const tags: Record<string, string> = {};
    input.split(/\s+/).forEach((tag) => {
      if (!tag) return;
      const [key, ...valueParts] = tag.split('=');
      tags[key] = valueParts.length > 0 ? valueParts.join('=') : 'true';
    });
    void this.updateAgent(agent, { tags });
  }

  private promptChangeAgentOwner(agent: ManagedAgentSummary): void {
    if (!this.availableUsers.length) return;
    const currentOwner = agent.owner_username || agent.owner_email || '';
    const input = window.prompt(
      'Enter owner username or email. Leave blank to clear owner.',
      currentOwner
    );
    if (input === null) return;

    const trimmed = input.trim();
    if (!trimmed) {
      void this.updateAgent(agent, { owner_user_id: null });
      return;
    }

    const selected = this.availableUsers.find(
      (user) => user.username === trimmed || user.email === trimmed
    );
    if (!selected) {
      window.alert('No user matched that username or email.');
      return;
    }
    void this.updateAgent(agent, { owner_user_id: selected.id });
  }

  private async updateAgentLifecycle(
    agent: ManagedAgentSummary,
    lifecycleAction: 'suspend' | 'resume'
  ): Promise<void> {
    const label = lifecycleAction === 'suspend' ? 'halt' : 'resume';
    if (
      !window.confirm(
        `Are you sure you want to ${label} ${agent.display_name}?`
      )
    ) {
      return;
    }
    await this.updateAgent(agent, {
      lifecycle_action: lifecycleAction,
      reason:
        lifecycleAction === 'suspend'
          ? 'Manually halted from managed agents view'
          : 'Manually resumed from managed agents view',
    });
  }

  // --- CANVAS VIEWPORT LOGIC ---
  private handleWheel(e: WheelEvent) {
    if (this.currentView !== 'canvas') return;
    e.preventDefault();
    const zoomSensitivity = 0.001;
    const delta = -e.deltaY * zoomSensitivity;
    this.zoom(delta, e.clientX, e.clientY);
  }

  private zoom(delta: number, clientX: number, clientY: number) {
    const minScale = 0.2;
    const maxScale = 3;
    const newScale = Math.min(
      Math.max(this.scale + this.scale * delta, minScale),
      maxScale
    );

    const bounds = this.shadowRoot
      ?.querySelector('.canvas-viewport')
      ?.getBoundingClientRect();
    if (bounds) {
      const offsetX = clientX - bounds.left;
      const offsetY = clientY - bounds.top;
      this.translateX =
        offsetX - (offsetX - this.translateX) * (newScale / this.scale);
      this.translateY =
        offsetY - (offsetY - this.translateY) * (newScale / this.scale);
    }
    this.scale = newScale;
  }

  private handlePointerDown(e: PointerEvent) {
    this.activePointers.set(e.pointerId, e);
    const canvasViewport = this.shadowRoot?.querySelector(
      '.canvas-viewport'
    ) as HTMLElement;
    if (canvasViewport) {
      canvasViewport.setPointerCapture(e.pointerId);
    }

    if (this.activePointers.size === 1) {
      this.isDragging = false;
      this.startX = e.clientX - this.translateX;
      this.startY = e.clientY - this.translateY;
    } else if (this.activePointers.size === 2) {
      this.isDragging = false;
      const pointers = Array.from(this.activePointers.values());
      this.initialPinchDistance = Math.hypot(
        pointers[0].clientX - pointers[1].clientX,
        pointers[0].clientY - pointers[1].clientY
      );
      this.initialPinchScale = this.scale;
    }
  }

  private handlePointerMove(e: PointerEvent) {
    if (!this.activePointers.has(e.pointerId)) return;
    this.activePointers.set(e.pointerId, e);

    if (this.activePointers.size === 1) {
      if (
        Math.abs(e.clientX - this.startX - this.translateX) > 3 ||
        Math.abs(e.clientY - this.startY - this.translateY) > 3
      ) {
        this.isDragging = true;
      }
      if (this.isDragging) {
        this.translateX = e.clientX - this.startX;
        this.translateY = e.clientY - this.startY;
      }
    } else if (this.activePointers.size === 2) {
      const pointers = Array.from(this.activePointers.values());
      const currentDistance = Math.hypot(
        pointers[0].clientX - pointers[1].clientX,
        pointers[0].clientY - pointers[1].clientY
      );
      const centerX = (pointers[0].clientX + pointers[1].clientX) / 2;
      const centerY = (pointers[0].clientY + pointers[1].clientY) / 2;

      const scaleDelta = currentDistance / this.initialPinchDistance - 1;
      this.scale = this.initialPinchScale;
      this.zoom(scaleDelta, centerX, centerY);

      this.initialPinchDistance = currentDistance;
      this.initialPinchScale = this.scale;
    }
  }

  private handlePointerUp(e: PointerEvent) {
    this.activePointers.delete(e.pointerId);
    if (this.activePointers.size < 2) {
      this.initialPinchDistance = 0;
    }
    if (this.activePointers.size === 1) {
      const remainingPointer = Array.from(this.activePointers.values())[0];
      this.startX = remainingPointer.clientX - this.translateX;
      this.startY = remainingPointer.clientY - this.translateY;
      this.isDragging = true;
    } else if (this.activePointers.size === 0) {
      this.isDragging = false;
    }
    const canvasViewport = this.shadowRoot?.querySelector(
      '.canvas-viewport'
    ) as HTMLElement;
    if (canvasViewport) {
      canvasViewport.releasePointerCapture(e.pointerId);
    }
  }

  private resetView() {
    const items = this.getCanvasItems({ includeExiting: false });
    const bounds = this.shadowRoot
      ?.querySelector('.canvas-viewport')
      ?.getBoundingClientRect();

    if (!bounds || bounds.width === 0) {
      this.scale = 1;
      this.translateX = bounds ? bounds.width / 2 : window.innerWidth / 2;
      this.translateY = bounds ? bounds.height / 2 : window.innerHeight / 2;
      return;
    }

    if (items.length === 0) {
      this.scale = 1;
      this.translateX = bounds.width / 2;
      this.translateY = bounds.height / 2;
      return;
    }

    this.initializeNodePositions(true);
  }

  firstUpdated() {
    setTimeout(() => {
      if (Object.keys(this.nodePositions).length > 0) {
        this.fitViewportToPositions(this.nodePositions);
      } else {
        this.resetView();
      }
    }, 50);
  }

  // --- NODE DRAG LOGIC ---
  private handleNodePointerDown(e: PointerEvent, id: string) {
    e.stopPropagation(); // prevent canvas drag
    this.draggingNodeId = id;
    this.nodeStartX = e.clientX;
    this.nodeStartY = e.clientY;
    this.dragHasMoved = false;

    const nodeEl = e.currentTarget as HTMLElement;
    nodeEl.setPointerCapture(e.pointerId);
  }

  private handleNodePointerMove(e: PointerEvent, id: string) {
    if (this.draggingNodeId === id) {
      e.stopPropagation();
      const dx = (e.clientX - this.nodeStartX) / this.scale;
      const dy = (e.clientY - this.nodeStartY) / this.scale;

      if (
        Math.abs(e.clientX - this.nodeStartX) > 3 ||
        Math.abs(e.clientY - this.nodeStartY) > 3
      ) {
        this.dragHasMoved = true;
      }

      this.nodeStartX = e.clientX;
      this.nodeStartY = e.clientY;

      const pos = this.nodePositions[id] || { x: 0, y: 0 };
      this.nodePositions = {
        ...this.nodePositions,
        [id]: {
          x: pos.x + dx,
          y: pos.y + dy,
        },
      };
    }
  }

  private handleNodePointerUp(e: PointerEvent, id: string) {
    if (this.draggingNodeId === id) {
      e.stopPropagation();
      this.draggingNodeId = null;
      const nodeEl = e.currentTarget as HTMLElement;
      nodeEl.releasePointerCapture(e.pointerId);

      // If we didn't drag it, it's a click to route.
      if (!this.dragHasMoved) {
        const isFlow = this.flows.some((f: any) => f.id === id);
        if (isFlow) {
          Router.go(`/console/flows/${encodeURIComponent(id)}`);
        } else {
          Router.go(`/console/agents/${encodeURIComponent(id)}`);
        }
      } else {
        localStorage.setItem(
          'preloop.agents.canvas_positions',
          JSON.stringify(this.nodePositions)
        );
      }
    }
  }

  private setCurrentView(view: 'cards' | 'canvas') {
    this.currentView = view;
    localStorage.setItem('preloop.agents.view_mode', view);
  }

  // --- RENDERING ---
  private navigateToCardTarget(url: string) {
    Router.go(url);
  }

  private handleCardKeydown(event: KeyboardEvent, url: string) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    this.navigateToCardTarget(url);
  }

  private getCardActions(item: any): ResourceAction[] {
    const isFlow =
      'flow_status' in item || ('name' in item && !('display_name' in item));
    if (isFlow) {
      return [];
    }

    const agent = item as ManagedAgentSummary;
    const actions: ResourceAction[] = [
      {
        id: 'rename',
        label: 'Rename',
        icon: 'pencil',
        loading: this.actionAgentId === agent.id,
        onClick: () => this.promptRenameAgent(agent),
      },
      {
        id: 'edit-tags',
        label: 'Edit tags',
        icon: 'tags',
        loading: this.actionAgentId === agent.id,
        onClick: () => this.promptEditAgentTags(agent),
      },
    ];

    if (this.featureFlags.user_management && this.availableUsers.length > 0) {
      actions.push({
        id: 'change-owner',
        label: 'Change owner',
        icon: 'person-gear',
        loading: this.actionAgentId === agent.id,
        onClick: () => this.promptChangeAgentOwner(agent),
      });
    }

    const isSuspendedOrDecommissioned =
      agent.lifecycle_state === 'suspended' ||
      agent.lifecycle_state === 'decommissioned';
    actions.push(
      isSuspendedOrDecommissioned
        ? {
            id: 'resume',
            label: 'Resume',
            icon: 'plug',
            variant: 'success',
            loading: this.actionAgentId === agent.id,
            onClick: () => {
              void this.updateAgentLifecycle(agent, 'resume');
            },
          }
        : {
            id: 'halt',
            label: 'Halt',
            icon: 'power',
            variant: 'danger',
            loading: this.actionAgentId === agent.id,
            onClick: () => {
              void this.updateAgentLifecycle(agent, 'suspend');
            },
          }
    );

    actions.push({
      id: 'remove',
      label: 'Remove',
      icon: 'trash',
      variant: 'danger',
      loading: this.actionAgentId === agent.id,
      onClick: () => {
        void this.removeAgent(agent);
      },
    });

    return actions;
  }

  /**
   * Whether the live-validation badge belongs on the list card. Quiet chrome:
   * only failure states surface here (badges mean "look here"); passed and
   * not-run stay off the card and remain visible on the detail page.
   */
  private shouldShowValidationBadge(agent: ManagedAgentSummary): boolean {
    if (!agent.live_validation_supported) return false;
    return ['failed', 'throttled', 'upstream_unavailable'].includes(
      agent.live_validation_status
    );
  }

  /**
   * An agent whose gateway traffic is failing across the board (≥5 requests,
   * zero successes) gets a red strip on its card — the silent-failure case
   * where a broken agent otherwise renders as a healthy green one.
   */
  private isModelTrafficFailing(agent: ManagedAgentSummary): boolean {
    const total = agent.total_requests || 0;
    const failed = agent.failed_requests ?? 0;
    return total >= 5 && failed === total;
  }

  private renderAgentIdentityBadges(agent: ManagedAgentSummary) {
    const tags = Object.entries(agent.tags || {});
    return html`
      <div class="identity-badges">
        <sl-badge variant="${this.getLifecycleVariant(agent)}" pill>
          ${this.getLifecycleLabel(agent)}
        </sl-badge>
        ${
          this.shouldShowValidationBadge(agent)
            ? html`<sl-badge
                class="validation-badge"
                variant="${this.getLiveValidationVariant(agent)}"
                pill
              >
                ${this.getLiveValidationLabel(agent)}
              </sl-badge>`
            : null
        }
        ${
          agent.owner_username
            ? html`<sl-badge variant="neutral" pill title="Owner">
                <sl-icon
                  name="person"
                  style="margin-right: 3px; opacity: 0.7;"
                ></sl-icon
                >${agent.owner_username}</sl-badge
              >`
            : null
        }
        ${tags.map(
          ([key, value]) => html`
            <sl-badge variant="neutral" pill>
              <span style="opacity: 0.7">${key}</span>${
                value && value !== 'true'
                  ? html`<span style="opacity: 0.4; margin: 0 4px;">=</span
                      >${value}`
                  : ''
              }
            </sl-badge>
          `
        )}
      </div>
    `;
  }

  private renderAgentTalkButton(
    agent: ManagedAgentSummary | null,
    sourceContext: string
  ) {
    const controlState = agent ? getAgentControlState(agent) : null;
    if (!agent || !controlState?.visible) {
      return null;
    }
    return html`
      <div
        class="top-action"
        @click=${(event: Event) => event.stopPropagation()}
        @keydown=${(event: Event) => event.stopPropagation()}
        @pointerdown=${(event: Event) => event.stopPropagation()}
      >
        <agent-talk-composer
          .agent=${agent}
          .sessions=${[]}
          sourceContext=${sourceContext}
          compact
          @agent-control-sent=${() => this.loadAgents()}
        ></agent-talk-composer>
      </div>
    `;
  }

  private renderAgentCard(item: any) {
    const isFlow =
      'flow_status' in item || ('name' in item && !('display_name' in item));
    const agent = isFlow ? null : (item as ManagedAgentSummary);
    const flowNode = isFlow ? (item as any) : null;
    const itemId = isFlow ? item.id : agent?.id;
    const detailUrl = isFlow
      ? `/console/flows/${encodeURIComponent(item.id)}`
      : `/console/agents/${encodeURIComponent(agent!.id)}`;
    const actions = this.getCardActions(item);
    const liveActivity = this.liveActivity[itemId];
    const liveTotal =
      (liveActivity?.modelCalls || 0) + (liveActivity?.toolCalls || 0);

    const isGlowing =
      liveActivity?.lastActivityAt &&
      Date.now() - new Date(liveActivity.lastActivityAt).getTime() < 2000;

    const displayName = isFlow ? item.name : agent?.display_name;
    const agentKind = isFlow
      ? 'flow'
      : agent?.agent_kind || agent?.session_source_type;
    const sessionSourceId = isFlow
      ? flowNode?.description || ''
      : agent?.session_source_id;
    const totalRequests = isFlow
      ? flowNode?.execution_stats?.total_execs || 0
      : agent?.total_requests;
    const estimatedCost = isFlow
      ? flowNode?.execution_stats?.estimated_cost || 0
      : agent?.estimated_cost || 0;
    const lastSeen = isFlow
      ? flowNode?.execution_stats?.last_seen_at
      : agent?.last_seen_at;
    return html`
      <sl-card
        class="agent-card ${liveTotal > 0 ? 'live' : ''} ${
          isGlowing ? 'glowing' : ''
        }"
        role="link"
        tabindex="0"
        @click=${() => this.navigateToCardTarget(detailUrl)}
        @keydown=${(event: KeyboardEvent) =>
          this.handleCardKeydown(event, detailUrl)}
      >
        <div class="card-stack">
          ${
            actions.length
              ? html`
                  <div
                    class="card-actions"
                    @click=${(event: Event) => event.stopPropagation()}
                    @keydown=${(event: Event) => event.stopPropagation()}
                  >
                    <resource-actions
                      .actions=${actions}
                      menu-only
                    ></resource-actions>
                  </div>
                `
              : null
          }
          <div class="title-row">
            <div style="display: flex; gap: 12px; align-items: flex-start;">
              ${
                isFlow
                  ? html`<img
                      src="/images/flow.svg"
                      class="flow-icon"
                      style="width: 24px; height: 24px; flex-shrink: 0; margin-top: 2px;"
                      alt="Flow"
                    />`
                  : renderAgentIcon(
                      agentKind,
                      'font-size: 24px; color: var(--sl-color-neutral-800); margin-top: 2px;'
                    )
              }
              <div class="identity-stack">
                <div class="agent-name">${displayName}</div>
                <div
                  class="agent-meta"
                  title="${sessionSourceId ? sessionSourceId : ''}"
                >
                  ${isFlow ? 'Flow' : this.getSourceLabel(agentKind)}
                  ${sessionSourceId ? ` · ${sessionSourceId}` : ''}
                </div>
                ${
                  !isFlow && agent
                    ? this.renderAgentIdentityBadges(agent)
                    : null
                }
              </div>
            </div>
            <div class="badges">
              ${
                !isFlow
                  ? this.renderAgentTalkButton(agent, 'agents-card')
                  : html`
                      <sl-badge
                        variant=${
                          !isFlow
                            ? ''
                            : flowNode?.flow_status === 'active'
                              ? 'success'
                              : 'neutral'
                        }
                        >${
                          !isFlow
                            ? ''
                            : flowNode?.flow_status === 'active'
                              ? 'Active'
                              : 'Inactive'
                        }</sl-badge
                      >
                    `
              }
              ${
                liveTotal
                  ? html`<sl-badge variant="primary"
                      >Live ${liveTotal}</sl-badge
                    >`
                  : null
              }
            </div>
          </div>

          ${
            isFlow && flowNode?.agent_type
              ? html`
                  <div
                    style="font-size: 0.85rem; color: var(--sl-color-neutral-700); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;"
                  >
                    ${renderAgentIcon(
                      flowNode.agent_type,
                      'color: var(--sl-color-primary-500); width: 14px; height: 14px;'
                    )}
                    <strong>Agent Type:</strong> ${flowNode.agent_type}
                  </div>
                `
              : ''
          }
          ${
            (isFlow && flowNode?.ai_model_id) ||
            (!isFlow &&
              ((agent as any)?.ai_model_id ||
                (agent as any)?.configured_model_alias ||
                (agent as any)?.latest_model_alias))
              ? html`
                  <div
                    style="font-size: 0.85rem; color: var(--sl-color-neutral-700); margin-bottom: 8px; display: flex; align-items: center; gap: 6px;"
                  >
                    <sl-icon
                      name="cpu"
                      style="color: var(--sl-color-primary-500);"
                    ></sl-icon>
                    <strong>Model:</strong>
                    <a
                      href="/console/ai-models/${encodeURIComponent(
                        isFlow
                          ? flowNode!.ai_model_id
                          : (agent as any)?.ai_model_id ||
                              (agent as any)?.configured_model_id ||
                              'unknown'
                      )}"
                      style="color: inherit; text-decoration: underline; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px;"
                      @pointerdown="${(e: Event) => e.stopPropagation()}"
                      @click="${(e: Event) => e.stopPropagation()}"
                    >
                      ${(() => {
                        if (isFlow) return flowNode!.ai_model_id;
                        const mId = (agent as any)?.ai_model_id;
                        if (mId) {
                          const model = this.aiModels.find((m) => m.id === mId);
                          if (model && model.name) return model.name;
                        }
                        return (
                          (agent as any)?.ai_model_name ||
                          (agent as any)?.configured_model_alias ||
                          (agent as any)?.latest_model_alias ||
                          mId
                        );
                      })()}
                    </a>
                  </div>
                `
              : ''
          }
          <div
            style="font-size: 0.85rem; color: var(--sl-color-neutral-600); margin-bottom: 12px;"
          >
            ${isFlow ? '' : this.getOnboardingDescription(agent!)}
          </div>

          ${
            !isFlow && agent && this.isModelTrafficFailing(agent)
              ? html`
                  <div class="model-traffic-failing">
                    Model traffic failing —
                    <a
                      href=${
                        agent.runtime_session_id
                          ? `/console/runtime-sessions?sessionId=${encodeURIComponent(
                              agent.runtime_session_id
                            )}`
                          : `/console/agents/${encodeURIComponent(agent.id)}`
                      }
                      @pointerdown=${(e: Event) => e.stopPropagation()}
                      @click=${(e: Event) => e.stopPropagation()}
                      >see latest session</a
                    >
                  </div>
                `
              : ''
          }
          ${
            liveActivity?.lastMessagePreview
              ? html`
                  <div
                    style="background: var(--sl-color-neutral-100); padding: 8px 12px; border-radius: var(--sl-border-radius-medium); margin-bottom: 12px; font-size: 0.85rem;"
                  >
                    <div
                      style="font-weight: 600; font-size: 0.75rem; text-transform: uppercase; color: var(--sl-color-neutral-500); margin-bottom: 4px;"
                    >
                      Latest from ${liveActivity.lastMessageSource || 'Agent'}
                    </div>
                    <div
                      style="color: var(--sl-color-neutral-800); overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;"
                    >
                      ${liveActivity.lastMessagePreview}
                    </div>
                  </div>
                `
              : ''
          }
          ${
            !isFlow
              ? html`
                  <div class="metric-row">
                    <span class="label">Preloop MCP Proxy</span>
                    <span class="value"
                      >${
                        agent!.mcp_proxy_configured ? 'Configured' : 'Missing'
                      }</span
                    >
                  </div>
                  <div class="metric-row">
                    <span class="label">Preloop Model Gateway</span>
                    <span class="value"
                      >${
                        agent!.model_gateway_configured
                          ? 'Configured'
                          : 'Missing'
                      }</span
                    >
                  </div>
                `
              : ''
          }

          <div class="metric-row">
            <span class="label">Estimated Cost</span>
            <span class="value">${this.formatMoney(estimatedCost!)}</span>
          </div>

          <div class="metric-row">
            <span class="label">${isFlow ? 'Executions' : 'Requests'}</span>
            <span class="value">${totalRequests}</span>
          </div>

          <div class="metric-row">
            <span class="label">Last Seen</span>
            <span class="value"
              >${this.formatDateTime(
                liveActivity?.lastActivityAt || lastSeen
              )}</span
            >
          </div>
        </div>
      </sl-card>
    `;
  }

  private renderCanvas() {
    const items = this.getCanvasItems({ includeExiting: false });
    return html`
      <div
        style="position: relative; flex: 1; min-height: 500px; display: flex; flex-direction: column; overflow: visible; z-index: 10;"
      >
        <div
          class="canvas-container"
          style="flex: 1; width: 100%; height: 100%;"
        >
          ${
            this.loading && !this.agents
              ? html`
                  <div
                    style="position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 50; background: var(--sl-panel-background-color); backdrop-filter: blur(4px);"
                  >
                    <sl-spinner style="font-size: 2rem;"></sl-spinner>
                    <div
                      style="margin-top: 16px; font-family: monospace; color: var(--sl-color-neutral-600);"
                    >
                      Loading topology data...
                    </div>
                  </div>
                `
              : ''
          }

          <div class="controls-overlay">
            <sl-tooltip content="Zoom In" placement="left">
              <sl-button
                size="medium"
                circle
                @click=${() =>
                  this.zoom(0.2, window.innerWidth / 2, window.innerHeight / 2)}
              >
                <sl-icon name="plus"></sl-icon>
              </sl-button>
            </sl-tooltip>
            <sl-tooltip content="Reset View" placement="left">
              <sl-button size="medium" circle @click=${() => this.resetView()}>
                <sl-icon name="arrows-collapse"></sl-icon>
              </sl-button>
            </sl-tooltip>
            <sl-tooltip content="Zoom Out" placement="left">
              <sl-button
                size="medium"
                circle
                @click=${() =>
                  this.zoom(
                    -0.2,
                    window.innerWidth / 2,
                    window.innerHeight / 2
                  )}
              >
                <sl-icon name="dash"></sl-icon>
              </sl-button>
            </sl-tooltip>
          </div>

          <div
            style="position: absolute; left: 20px; bottom: 20px; z-index: 20; background: color-mix(in srgb, var(--sl-panel-background-color) 92%, transparent); border: 1px solid var(--sl-color-neutral-200); border-radius: var(--sl-border-radius-medium); padding: 10px 12px; font-size: 0.8rem; color: var(--sl-color-neutral-700); display: flex; gap: 16px;"
          >
            <div style="display: flex; align-items: center; gap: 8px;">
              <span
                style="display: inline-block; width: 20px; height: 0; border-top: 2px solid var(--sl-color-primary-500);"
              ></span>
              <span>Model traffic</span>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span
                style="display: inline-block; width: 20px; height: 0; border-top: 2px dashed var(--sl-color-warning-500);"
              ></span>
              <span>Tool traffic</span>
            </div>
          </div>

          <div
            class="canvas-viewport"
            @wheel=${this.handleWheel}
            @pointerdown=${this.handlePointerDown}
            @pointermove=${this.handlePointerMove}
            @pointerup=${this.handlePointerUp}
            @pointercancel=${this.handlePointerUp}
            @pointerleave=${this.handlePointerUp}
          >
            <div
              style=${styleMap({
                transform: `translate(${this.translateX}px, ${this.translateY}px) scale(${this.scale})`,
              })}
              class="canvas-content"
            >
              <div class="gateway-node">
                <div
                  class="gateway-icon ${
                    Object.values(this.liveActivity).some(
                      (v) =>
                        v.lastActivityAt &&
                        Date.now() - new Date(v.lastActivityAt).getTime() < 2000
                    )
                      ? 'pulsing'
                      : ''
                  }"
                >
                  <sl-icon
                    src="/assets/preloop-badge.svg"
                    style="margin-left: -5px;margin-bottom: -4px;"
                  ></sl-icon>
                </div>
                <div class="gateway-label" style="text-align: center;">
                  <div>PRELOOP GATEWAY</div>
                  ${
                    this.gatewaySummary
                      ? html`
                          <div
                            style="font-size: 0.75rem; color: var(--sl-color-primary-500); margin-top: 4px; font-weight: 500; font-family: monospace;"
                          >
                            ${this.gatewaySummary.token_usage.total_tokens.toLocaleString()}
                            Tokens ·
                            ${(
                              this.gatewaySummary.token_usage.total_tokens /
                              gatewaySummaryDays(this.gatewaySummary)
                            ).toFixed(0)}
                            / day
                          </div>
                        `
                      : ''
                  }
                </div>
              </div>

              ${this.getCanvasItems({ includeExiting: true }).map(
                (item: any) => {
                  const isFlow =
                    'flow_status' in item ||
                    ('name' in item && !('display_name' in item));
                  const agent = isFlow ? null : (item as ManagedAgentSummary);
                  const flowName = isFlow ? item.name : '';
                  const flowNode = isFlow ? (item as any) : null;
                  const totalExecs = isFlow
                    ? item.execution_stats?.total_execs || 0
                    : 0;
                  const totalSpend = isFlow
                    ? item.execution_stats?.estimated_cost || 0
                    : agent?.estimated_cost || 0;
                  const lastSeenFlow = isFlow
                    ? item.execution_stats?.last_seen_at
                    : null;
                  const pos = this.nodePositions[item.id] || { x: 250, y: 250 };
                  const liveActivity = this.liveActivity[item.id];
                  const liveTotal = liveActivity
                    ? liveActivity.modelCalls + liveActivity.toolCalls
                    : 0;
                  const mcpEnabled = isFlow
                    ? true
                    : this.isMcpConfigured(agent as any);
                  const modelEnabled = isFlow
                    ? true
                    : this.isModelConfigured(agent as any);
                  const modelActive = !!(
                    liveActivity?.modelCalls &&
                    liveActivity?.lastActivityAt &&
                    Date.now() -
                      new Date(liveActivity.lastActivityAt).getTime() <
                      2000
                  );
                  const toolActive = !!(
                    liveActivity?.toolCalls &&
                    liveActivity?.lastActivityAt &&
                    Date.now() -
                      new Date(liveActivity.lastActivityAt).getTime() <
                      2000
                  );
                  const distance = Math.max(
                    Math.sqrt(pos.x * pos.x + pos.y * pos.y),
                    1
                  );
                  const offsetX = (-pos.y / distance) * 8;
                  const offsetY = (pos.x / distance) * 8;

                  return html`
                    <svg
                      class="connection-line ${
                        this.nodeAnimationState[item.id] || ''
                      } ${this.draggingNodeId === item.id ? 'dragging' : ''}"
                      xmlns="http://www.w3.org/2000/svg"
                    >
                      <line
                        x1="${offsetX}"
                        y1="${offsetY}"
                        x2="${pos.x + offsetX}"
                        y2="${pos.y + offsetY}"
                        stroke="${
                          isFlow
                            ? 'var(--sl-color-primary-500)'
                            : modelEnabled
                              ? modelActive
                                ? 'var(--sl-color-success-500)'
                                : 'var(--sl-color-primary-500)'
                              : 'var(--sl-color-neutral-300)'
                        }"
                        stroke-width="${
                          isFlow
                            ? '2'
                            : modelActive
                              ? '3'
                              : modelEnabled
                                ? '2'
                                : '1.25'
                        }"
                        stroke-dasharray="${modelEnabled ? '0' : '6 6'}"
                        opacity="${modelEnabled ? '1' : '0.55'}"
                      />
                      <line
                        x1="${-offsetX}"
                        y1="${-offsetY}"
                        x2="${pos.x - offsetX}"
                        y2="${pos.y - offsetY}"
                        stroke="${
                          mcpEnabled
                            ? toolActive
                              ? 'var(--sl-color-warning-300)'
                              : 'var(--sl-color-warning-500)'
                            : 'var(--sl-color-neutral-300)'
                        }"
                        stroke-width="${
                          toolActive ? '3' : mcpEnabled ? '2' : '1.25'
                        }"
                        stroke-dasharray="${mcpEnabled ? '5 4' : '6 6'}"
                        opacity="${mcpEnabled ? '1' : '0.55'}"
                      />
                    </svg>

                    <div
                      class="agent-node ${
                        this.nodeAnimationState[item.id] || ''
                      } ${this.draggingNodeId === item.id ? 'dragging' : ''} ${
                        liveActivity?.currentBubble &&
                        Date.now() - liveActivity.currentBubble.timestamp < 6000
                          ? 'has-bubble'
                          : ''
                      }"
                      style=${styleMap({
                        left: `${pos.x}px`,
                        top: `${pos.y}px`,
                      })}
                      @pointerdown=${(e: PointerEvent) =>
                        this.handleNodePointerDown(e, item.id)}
                      @pointermove=${(e: PointerEvent) =>
                        this.handleNodePointerMove(e, item.id)}
                      @pointerup=${(e: PointerEvent) =>
                        this.handleNodePointerUp(e, item.id)}
                      @pointercancel=${(e: PointerEvent) =>
                        this.handleNodePointerUp(e, item.id)}
                    >
                      ${html`
                        <sl-card>
                          <div
                            slot="header"
                            style="display: flex; justify-content: space-between; align-items: center;"
                          >
                            <div
                              style="display: flex; gap: 8px; overflow: hidden;"
                            >
                              ${
                                isFlow
                                  ? html`<img
                                      src="/images/flow.svg"
                                      class="flow-icon"
                                      style="width: 20px; height: 20px; flex-shrink: 0;"
                                      alt="Flow"
                                    />`
                                  : renderAgentIcon(
                                      agent?.agent_kind ||
                                        agent?.session_source_type,
                                      'flex-shrink: 0; color: var(--sl-color-neutral-900); width: 20px; height: 20px;'
                                    )
                              }
                              <strong
                                style="font-size: 1rem; word-break: break-word; line-height: 1.2;"
                                >${
                                  isFlow ? flowName : agent?.display_name
                                }</strong
                              >
                            </div>
                            ${
                              !isFlow
                                ? this.renderAgentTalkButton(
                                    agent,
                                    'agents-canvas'
                                  )
                                : liveTotal > 0
                                  ? html`<sl-badge variant="success" pulse
                                      >Live</sl-badge
                                    >`
                                  : isFlow
                                    ? html`<sl-badge variant="success"
                                        >Active</sl-badge
                                      >`
                                    : ''
                            }
                          </div>
                          <div
                            style="font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-500); margin-bottom: 8px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis; word-break: break-word;"
                            title="${
                              isFlow
                                ? flowNode?.description || ''
                                : agent?.session_source_id || ''
                            }"
                          >
                            ${
                              isFlow
                                ? flowNode?.description || ''
                                : agent?.session_source_id
                            }
                          </div>
                          ${
                            !isFlow && agent
                              ? this.renderAgentIdentityBadges(agent)
                              : null
                          }
                          ${
                            !isFlow &&
                            agent &&
                            this.isModelTrafficFailing(agent)
                              ? html`
                                  <div class="model-traffic-failing">
                                    Model traffic failing —
                                    <a
                                      href=${
                                        agent.runtime_session_id
                                          ? `/console/runtime-sessions?sessionId=${encodeURIComponent(
                                              agent.runtime_session_id
                                            )}`
                                          : `/console/agents/${encodeURIComponent(
                                              agent.id
                                            )}`
                                      }
                                      @pointerdown=${(e: Event) =>
                                        e.stopPropagation()}
                                      @click=${(e: Event) =>
                                        e.stopPropagation()}
                                      >see latest session</a
                                    >
                                  </div>
                                `
                              : ''
                          }
                          ${
                            isFlow && flowNode?.agent_type
                              ? html` <div
                                  style="font-size: 0.75rem; color: var(--sl-color-neutral-600); margin-bottom: 6px; display: flex; align-items: center; gap: 4px;"
                                >
                                  ${renderAgentIcon(
                                    flowNode.agent_type,
                                    'color: var(--sl-color-primary-500); width: 14px; height: 14px;'
                                  )}
                                  ${flowNode.agent_type}
                                </div>`
                              : ''
                          }
                          ${
                            !isFlow && (agent as any)?.ai_model_id
                              ? html` <div
                                  style="font-size: 0.75rem; color: var(--sl-color-neutral-600); margin-bottom: 6px; display: flex; align-items: center; gap: 4px;"
                                >
                                  <sl-icon
                                    name="cpu"
                                    style="color: var(--sl-color-primary-500);"
                                  ></sl-icon>
                                  <a
                                    href="/console/ai-models/${encodeURIComponent(
                                      (agent as any).ai_model_id
                                    )}"
                                    style="max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: inherit; text-decoration: underline;"
                                    @click=${(e: Event) => e.stopPropagation()}
                                    >${(agent as any).ai_model_id}</a
                                  >
                                </div>`
                              : ''
                          }
                          ${
                            isFlow && flowNode?.ai_model_id
                              ? html` <div
                                  style="font-size: 0.75rem; color: var(--sl-color-neutral-600); margin-bottom: 6px; display: flex; align-items: center; gap: 4px;"
                                >
                                  <sl-icon
                                    name="cpu"
                                    style="color: var(--sl-color-primary-500);"
                                  ></sl-icon>
                                  <a
                                    href="/console/ai-models/${encodeURIComponent(
                                      flowNode.ai_model_id
                                    )}"
                                    style="max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: inherit; text-decoration: underline;"
                                    @click=${(e: Event) => e.stopPropagation()}
                                    >${flowNode.ai_model_id}</a
                                  >
                                </div>`
                              : ''
                          }
                          ${
                            !isFlow &&
                            agent?.tags &&
                            Object.keys(agent.tags).length > 0
                              ? html` <div
                                  style="display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px;"
                                >
                                  ${Object.entries(agent.tags)
                                    .slice(0, 3)
                                    .map(
                                      ([k, v]) => html`
                                        <div
                                          style="font-size: 0.65rem; background: var(--sl-color-neutral-100); padding: 2px 6px; border-radius: 10px; color: var(--sl-color-neutral-700); max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                                        >
                                          <span style="opacity: 0.7">${k}</span
                                          >${
                                            v && v !== 'true'
                                              ? html`<span
                                                    style="opacity: 0.4; margin: 0 2px;"
                                                    >=</span
                                                  >${v}`
                                              : ''
                                          }
                                        </div>
                                      `
                                    )}
                                  ${
                                    Object.keys(agent.tags).length > 3
                                      ? html`<div
                                          style="font-size: 0.65rem; padding: 2px;"
                                        >
                                          +${Object.keys(agent.tags).length - 3}
                                        </div>`
                                      : ''
                                  }
                                </div>`
                              : ''
                          }
                          ${
                            !isFlow && agent
                              ? html` <div
                                  style="font-size: 0.78rem; color: var(--sl-color-neutral-600); margin-bottom: 8px;"
                                >
                                  ${this.getOnboardingDescription(agent)}
                                </div>`
                              : ''
                          }
                          <div
                            style="display: flex; justify-content: space-between; margin-top: 12px; font-size: 0.85rem; border-top: 1px solid var(--sl-color-neutral-200); padding-top: 8px;"
                          >
                            <div style="display: flex; flex-direction: column;">
                              <span
                                style="opacity: 0.7; font-size: 0.75rem; text-transform: uppercase;"
                                >${isFlow ? 'Execs' : 'Reqs'}</span
                              >
                              <strong
                                >${
                                  isFlow ? totalExecs : agent?.total_requests
                                }</strong
                              >
                            </div>
                            <div
                              style="display: flex; flex-direction: column; text-align: center;"
                            >
                              <span
                                style="opacity: 0.7; font-size: 0.75rem; text-transform: uppercase;"
                                >Spend</span
                              >
                              <strong
                                >${this.formatMoney(
                                  isFlow
                                    ? totalSpend
                                    : agent?.estimated_cost || 0
                                )}</strong
                              >
                            </div>
                            <div
                              style="display: flex; flex-direction: column; text-align: right;"
                            >
                              <span
                                style="opacity: 0.7; font-size: 0.75rem; text-transform: uppercase;"
                                >Last Seen</span
                              >
                              <span style="font-weight: 600;"
                                >${new Date(
                                  liveActivity?.lastActivityAt ||
                                    (isFlow
                                      ? lastSeenFlow
                                      : agent?.last_seen_at) ||
                                    0
                                ).toLocaleTimeString([], {
                                  hour: '2-digit',
                                  minute: '2-digit',
                                })}</span
                              >
                            </div>
                          </div>
                        </sl-card>
                      `}
                    </div>
                  `;
                }
              )}
            </div>
          </div>
        </div>

        <!-- Speech bubbles overlay container outside the overflow-hidden boundary -->
        <div class="canvas-bubbles-overlay">
          <div
            style=${styleMap({
              position: 'absolute',
              inset: '0',
              transform: `translate(${this.translateX}px, ${this.translateY}px) scale(${this.scale})`,
              transformOrigin: '0 0',
              overflow: 'visible',
            })}
          >
            ${items.map((item) => {
              const pos = this.nodePositions[item.id];
              if (!pos) return '';
              const liveActivity = this.liveActivity[item.id];
              const isVisible =
                liveActivity?.currentBubble &&
                Date.now() - liveActivity.currentBubble.timestamp < 6000;
              if (!isVisible) return '';

              return html`
                <div
                  style=${styleMap({
                    position: 'absolute',
                    left: `${pos.x}px`,
                    top: `${pos.y}px`,
                    width: '0',
                    height: '0',
                    overflow: 'visible',
                  })}
                >
                  <div
                    class="agent-speech-bubble visible ${
                      liveActivity?.currentBubble?.source === 'Tool'
                        ? 'tool-bubble'
                        : ''
                    }"
                  >
                    <div class="speech-source">
                      ${liveActivity?.currentBubble?.source || 'Agent'}
                    </div>
                    <div class="speech-text">
                      ${liveActivity?.currentBubble?.text || ''}
                    </div>
                  </div>
                </div>
              `;
            })}
          </div>
        </div>
      </div>
    `;
  }

  private renderOnboardingDialog() {
    return html`
      <sl-dialog
        label="Onboard Agents"
        ?open=${this.showOnboardingDialog}
        @sl-after-hide=${(e: Event) => {
          if (e.target === e.currentTarget) {
            this.showOnboardingDialog = false;
          }
        }}
        style="--width: 760px;"
      >
        ${
          this.showOnboardingDialog
            ? html`
                <preloop-deploy-wizard
                  initial-path="govern"
                  hide-step-title
                  .aiModels=${this.aiModels}
                  .computeFeatureEnabled=${this.computeFeatureEnabled}
                  .isEnterprise=${this.isEnterprise}
                  .isAdmin=${this.isAdmin}
                  @deploy-agent-success=${this.handleDeployAgentSuccess}
                  @deploy-wizard-done=${() => {
                    this.showOnboardingDialog = false;
                    void this.loadAgents();
                  }}
                  @deploy-cancel=${() => {
                    this.showOnboardingDialog = false;
                  }}
                ></preloop-deploy-wizard>
              `
            : nothing
        }
      </sl-dialog>
    `;
  }

  private handleDeployAgentSuccess(event: CustomEvent): void {
    const mockAgent = event.detail.agent;
    if (this.agents) {
      this.agents = {
        ...this.agents,
        items: [mockAgent, ...this.agents.items],
        total: this.agents.total + 1,
      };
    } else {
      this.agents = {
        query: null,
        agent_kind: null,
        last_seen_after: null,
        status: 'all',
        items: [mockAgent],
        total: 1,
        limit: 50,
        offset: 0,
      };
    }
    this.requestUpdate();
  }

  render() {
    return html`
      <div
        class="page ${
          this.currentView === 'canvas' ? 'page-canvas-wrapper' : ''
        }"
      >
        ${this.renderOnboardingDialog()}

        <sl-dialog
          label="Deploy Governed Agent"
          ?open=${this.showDeployDialog}
          @sl-after-hide=${(e: Event) => {
            if (e.target === e.currentTarget) {
              this.showDeployDialog = false;
            }
          }}
          style="--width: 650px;"
        >
          <preloop-agent-deployer
            .aiModels=${this.aiModels}
            .computeFeatureEnabled=${this.computeFeatureEnabled}
            .isEnterprise=${this.isEnterprise}
            .isAdmin=${this.isAdmin}
            hide-back-button
            @deploy-agent-success=${(e: CustomEvent) => {
              this.handleDeployAgentSuccess(e);
              this.showDeployDialog = false;
            }}
            @deploy-cancel=${() => {
              this.showDeployDialog = false;
            }}
          ></preloop-agent-deployer>
        </sl-dialog>

        <div class="content-bounds">
          <view-header
            headerText="Agents"
            description="Agents connected to Preloop — their gateway credentials, MCP access, and live status. Onboard agents you already run with the CLI, or deploy new ones."
            width="extra-wide"
          >
            <div
              slot="main-column"
              style="display: flex; gap: var(--sl-spacing-small); align-items: center;"
            >
              <sl-button
                variant="default"
                @click=${() => {
                  this.showDeployDialog = true;
                }}
              >
                <sl-icon slot="prefix" name="cloud-arrow-up"></sl-icon>
                Deploy Agent
              </sl-button>
              <sl-button
                variant="primary"
                @click=${() => (this.showOnboardingDialog = true)}
              >
                <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                Onboard Agents
              </sl-button>
            </div>
          </view-header>

          <div class="agents-toolbar">
            <form class="filters" @submit=${this.handleSearchSubmit}>
              <sl-input
                placeholder="Search name, tags:env=prod, owner:username"
                clearable
                .value=${this.searchQuery}
                @sl-input=${this.handleSearchInput}
              >
                <sl-icon name="search" slot="prefix"></sl-icon>
              </sl-input>

              <sl-dropdown stay-open-on-select>
                <sl-button slot="trigger" caret variant="default">
                  Agent Kinds
                  (${
                    this.agentKinds.length === AVAILABLE_AGENT_KINDS.length
                      ? 'All'
                      : this.agentKinds.length
                  })
                </sl-button>
                <div
                  style="padding: var(--sl-spacing-medium); background: var(--sl-panel-background-color); border: solid 1px var(--sl-panel-border-color); border-radius: var(--sl-border-radius-medium); box-shadow: var(--sl-shadow-large); display: flex; flex-direction: column; gap: var(--sl-spacing-small); min-width: 200px;"
                >
                  <sl-checkbox
                    .checked=${
                      this.agentKinds.length === AVAILABLE_AGENT_KINDS.length
                    }
                    .indeterminate=${
                      this.agentKinds.length > 0 &&
                      this.agentKinds.length < AVAILABLE_AGENT_KINDS.length
                    }
                    @sl-change=${(e: any) =>
                      this.handleAgentKindChange('all', e.target.checked)}
                  >
                    Select All
                  </sl-checkbox>
                  <sl-divider
                    style="margin: var(--sl-spacing-x-small) 0;"
                  ></sl-divider>
                  ${AVAILABLE_AGENT_KINDS.map(
                    (kind) => html`
                      <sl-checkbox
                        .checked=${this.agentKinds.includes(kind.value)}
                        @sl-change=${(e: any) =>
                          this.handleAgentKindChange(
                            kind.value,
                            e.target.checked
                          )}
                      >
                        ${kind.label}
                      </sl-checkbox>
                    `
                  )}
                </div>
              </sl-dropdown>

              <sl-select
                value=${this.lastSeenAfter}
                @sl-change=${this.handleLastSeenAfterChange}
              >
                <sl-option value="all">All Time</sl-option>
                <sl-option value="last_10_minutes">Last 10 minutes</sl-option>
                <sl-option value="last_1_hour">Last 1 hour</sl-option>
                <sl-option value="last_24_hours">Last 24 hours</sl-option>
                <sl-option value="last_7_days">Last 7 days</sl-option>
              </sl-select>
            </form>

            <div class="view-switcher-group">
              <span class="toolbar-divider" aria-hidden="true"></span>
              <sl-radio-group
                value=${this.currentView}
                @sl-change=${(e: any) => this.setCurrentView(e.target.value)}
                size="small"
              >
                <sl-radio-button value="cards" title="Cards View">
                  <sl-icon name="grid"></sl-icon>
                </sl-radio-button>
                <sl-radio-button value="canvas" title="Canvas View">
                  <sl-icon name="share"></sl-icon>
                </sl-radio-button>
              </sl-radio-group>
            </div>
          </div>
          ${
            this.error
              ? html`<sl-alert open variant="danger" class="mx-6 mb-4"
                  >${this.error}</sl-alert
                >`
              : null
          }
        </div>

        ${
          this.currentView === 'canvas'
            ? this.renderCanvas()
            : html`
                <div class="cards">
                  ${
                    (!this.agents ||
                      (this.agents.items.length === 0 &&
                        this.flows.length === 0)) &&
                    !this.loading
                      ? html`
                          <div class="empty-state">
                            No agents or flows found matching your query.
                          </div>
                        `
                      : [...(this.agents?.items || []), ...this.flows].map(
                          (item) => this.renderAgentCard(item)
                        )
                  }
                </div>
              `
        }
      </div>
    `;
  }
}
