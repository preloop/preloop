import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import './tool-output-filter-dialog';
import type {
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
  RuntimeSessionOptimizationAppliedAction,
  RuntimeSessionOptimizationResponse,
  SessionContextProfileSegment,
} from '../types';
import type {
  ObservedSession,
  SessionOptimizationSuggestion,
} from '../utils/session-observer';
import {
  formatCost,
  formatNumber,
  suggestSessionOptimizations,
} from '../utils/session-observer';

const SEGMENT_LABELS: Record<string, string> = {
  system_prompt: 'System prompt',
  tool_schemas: 'Tool schemas',
  tool_outputs: 'Tool outputs',
  conversation_history: 'Conversation history',
  user_messages: 'User messages',
  other: 'Other',
};

@customElement('session-optimization-panel')
export class SessionOptimizationPanel extends LitElement {
  @property({ type: Object })
  session: ObservedSession | null = null;

  @property({ type: Array })
  events: FlowGatewayEvent[] = [];

  @property({ type: Array })
  activity: RuntimeSessionActivityItem[] = [];

  @property({ type: Array })
  suggestions: SessionOptimizationSuggestion[] | null = null;

  @property({ type: Object })
  optimization: RuntimeSessionOptimizationResponse | null = null;

  @property({ type: Array })
  appliedActions: RuntimeSessionOptimizationAppliedAction[] = [];

  @property({ type: String })
  applyingSuggestionId: string | null = null;

  @state()
  private pendingApply: SessionOptimizationSuggestion | null = null;

  // Per-tool keep/drop selection for the rich scope_tools dialog. Keyed by
  // tool name; ``true`` means the tool is checked (will be disabled). Seeded
  // all-checked when the dialog opens.
  @state()
  private scopeToolSelection: Record<string, boolean> = {};

  // The active manage_output_filter suggestion whose dialog is open, or null.
  @state()
  private outputFilterSuggestion: SessionOptimizationSuggestion | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .panel {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
    }

    .suggestion {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
    }

    .suggestion-header {
      align-items: start;
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .title {
      color: var(--sl-color-neutral-900);
      font-weight: 700;
    }

    .description,
    .evidence,
    .savings {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
      line-height: 1.45;
      margin-top: var(--sl-spacing-2x-small);
    }

    .savings {
      color: var(--sl-color-success-700);
      font-weight: 600;
    }

    .savings-summary {
      border: 1px solid var(--sl-color-success-200);
      background: var(--sl-color-success-50);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      margin-bottom: var(--sl-spacing-small);
    }

    .savings-summary-headline {
      color: var(--sl-color-success-700);
      font-size: var(--sl-font-size-medium);
      font-weight: var(--sl-font-weight-semibold);
    }

    .savings-summary-headline strong {
      font-size: var(--sl-font-size-large);
    }

    .savings-pct {
      color: var(--sl-color-success-600);
      font-weight: var(--sl-font-weight-normal);
    }

    .savings-summary-detail {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      margin-top: var(--sl-spacing-2x-small);
      line-height: 1.45;
    }

    .actions {
      align-items: center;
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: flex-end;
      margin-top: var(--sl-spacing-small);
    }

    .waste-banner {
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
    }

    .segment-row {
      align-items: center;
      display: grid;
      gap: var(--sl-spacing-small);
      grid-template-columns: 170px 1fr 110px;
      margin-top: var(--sl-spacing-2x-small);
    }

    .segment-label {
      color: var(--sl-color-neutral-800);
      font-size: var(--sl-font-size-small);
    }

    .segment-bar {
      background: var(--sl-color-neutral-100);
      border-radius: 999px;
      height: 10px;
      overflow: hidden;
    }

    .segment-bar-fill {
      background: var(--sl-color-primary-500);
      border-radius: 999px;
      height: 100%;
      min-width: 2px;
    }

    .segment-bar-fill.tool_outputs {
      background: var(--sl-color-warning-500);
    }

    .segment-bar-fill.tool_schemas {
      background: var(--sl-color-violet-500);
    }

    .segment-bar-fill.system_prompt {
      background: var(--sl-color-sky-500);
    }

    .segment-value {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-x-small);
      text-align: right;
    }

    .history-item {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
    }

    .outcome {
      border-top: 1px dashed var(--sl-color-neutral-200);
      margin-top: var(--sl-spacing-x-small);
      padding-top: var(--sl-spacing-x-small);
    }

    .outcome-positive {
      color: var(--sl-color-success-700);
      font-weight: 600;
    }

    .outcome-negative {
      color: var(--sl-color-danger-700);
      font-weight: 600;
    }

    .transparency {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
      margin-top: var(--sl-spacing-x-small);
    }

    .empty {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      padding: var(--sl-spacing-medium);
      text-align: center;
    }

    .apply-summary {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      font-size: var(--sl-font-size-small);
      line-height: 1.5;
      margin-top: var(--sl-spacing-small);
      padding: var(--sl-spacing-small);
    }

    .tool-checklist {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
      margin-top: var(--sl-spacing-small);
      max-height: 320px;
      overflow-y: auto;
    }

    .governance-link {
      font-size: var(--sl-font-size-small);
      margin-top: var(--sl-spacing-medium);
    }

    .governance-link a {
      color: var(--sl-color-primary-700);
      text-decoration: none;
    }

    .governance-link a:hover {
      text-decoration: underline;
    }
  `;

  private getConfidenceVariant(confidence: string) {
    if (confidence === 'high') return 'success';
    if (confidence === 'medium') return 'primary';
    return 'neutral';
  }

  private emitSuggestion(suggestion: SessionOptimizationSuggestion): void {
    this.dispatchEvent(
      new CustomEvent('session-optimization-selected', {
        detail: { suggestion, session: this.session },
        bubbles: true,
        composed: true,
      })
    );
  }

  private isApplied(suggestion: SessionOptimizationSuggestion): boolean {
    return this.appliedActions.some(
      (item) =>
        item.suggestion_id === suggestion.id &&
        item.action_type === suggestion.action?.type
    );
  }

  private isServerApplyable(
    suggestion: SessionOptimizationSuggestion
  ): boolean {
    const type = suggestion.action?.type;
    return type === 'scope_tools' || type === 'set_budget';
  }

  private getScopeToolNames(
    suggestion: SessionOptimizationSuggestion
  ): string[] {
    const params = suggestion.action?.params || {};
    return Array.isArray(params.tool_names)
      ? (params.tool_names as unknown[]).filter(
          (name): name is string => typeof name === 'string'
        )
      : [];
  }

  private getCheckedScopeTools(
    suggestion: SessionOptimizationSuggestion
  ): string[] {
    return this.getScopeToolNames(suggestion).filter(
      (name) => this.scopeToolSelection[name]
    );
  }

  // Open the rich scope_tools dialog, seeding every advertised tool as checked
  // (the default is to disable all unused tools).
  private openScopeToolsDialog(
    suggestion: SessionOptimizationSuggestion
  ): void {
    const selection: Record<string, boolean> = {};
    for (const name of this.getScopeToolNames(suggestion)) {
      selection[name] = true;
    }
    this.scopeToolSelection = selection;
    this.pendingApply = suggestion;
  }

  private toggleScopeTool(name: string, checked: boolean): void {
    this.scopeToolSelection = { ...this.scopeToolSelection, [name]: checked };
  }

  private confirmApply(): void {
    const suggestion = this.pendingApply;
    if (!suggestion?.action) return;
    // For scope_tools, narrow the action's tool_names to the user's checked
    // subset so only the tools they kept selected get disabled. Other actions
    // (set_budget) are applied verbatim.
    let action = suggestion.action;
    if (action.type === 'scope_tools') {
      const checked = this.getCheckedScopeTools(suggestion);
      action = {
        ...action,
        params: { ...action.params, tool_names: checked },
      };
    }
    this.dispatchEvent(
      new CustomEvent('session-optimization-apply', {
        detail: {
          suggestionId: suggestion.id,
          suggestionTitle: suggestion.title,
          action,
        },
        bubbles: true,
        composed: true,
      })
    );
    this.pendingApply = null;
  }

  // Dispatch an inspect-events request the parent (session-replay-panel) listens
  // for to jump the transcript to the relevant requests.
  private emitInspectEvents(suggestion: SessionOptimizationSuggestion): void {
    const params = suggestion.action?.params || {};
    const fromParams = Array.isArray(params.event_ids)
      ? (params.event_ids as unknown[]).filter(
          (id): id is string => typeof id === 'string'
        )
      : null;
    const eventIds = fromParams ?? suggestion.evidenceEventIds ?? [];
    let mode = 'evidence';
    if (suggestion.id === 'stabilize-prefix') {
      mode = 'cache-breaking';
    } else if (
      suggestion.id.includes('failed') ||
      suggestion.id.includes('error')
    ) {
      mode = 'failed';
    }
    this.dispatchEvent(
      new CustomEvent('optimization-inspect-events', {
        detail: { eventIds, mode },
        bubbles: true,
        composed: true,
      })
    );
  }

  private getAgentDetailHref(
    suggestion: SessionOptimizationSuggestion
  ): string | null {
    const params = suggestion.action?.params || {};
    const subjectType =
      typeof params.subject_type === 'string' ? params.subject_type : null;
    const subjectId =
      typeof params.subject_id === 'string' ? params.subject_id : null;
    // Only managed agents have a Tools & Governance view we can deep-link to.
    if (subjectType !== 'managed_agent' || !subjectId) return null;
    return `/console/agents/${encodeURIComponent(subjectId)}?tab=tools`;
  }

  private describeAction(
    action: { type: string; params: Record<string, unknown> } | null | undefined
  ): string {
    if (!action) return '';
    const params = action.params || {};
    const subjectName =
      typeof params.subject_name === 'string' ? params.subject_name : 'agent';
    if (action.type === 'scope_tools') {
      const tools = Array.isArray(params.tool_names) ? params.tool_names : [];
      return `Disable ${tools.length} unused tool${
        tools.length === 1 ? '' : 's'
      } for ${subjectName}: ${tools.slice(0, 8).join(', ')}${
        tools.length > 8 ? '…' : ''
      }`;
    }
    if (action.type === 'set_budget') {
      const limit = Number(params.hard_limit_usd || 0);
      const period =
        typeof params.period === 'string' ? params.period : 'daily';
      return `Set a ${period} hard budget of ${formatCost(limit)} for ${subjectName}.`;
    }
    if (action.type === 'open_events') {
      const ids = Array.isArray(params.event_ids) ? params.event_ids : [];
      return `Jump the replay to ${ids.length} evidence event${
        ids.length === 1 ? '' : 's'
      }.`;
    }
    return '';
  }

  private renderScopeToolsDialog(suggestion: SessionOptimizationSuggestion) {
    const params = suggestion.action?.params || {};
    const subjectName =
      typeof params.subject_name === 'string' ? params.subject_name : 'agent';
    const toolNames = this.getScopeToolNames(suggestion);
    const checkedCount = this.getCheckedScopeTools(suggestion).length;
    const governanceHref = this.getAgentDetailHref(suggestion);
    return html`
      <sl-dialog
        label="Scope down advertised tools"
        open
        @sl-after-hide=${() => {
          this.pendingApply = null;
        }}
      >
        <div class="title">${suggestion.title}</div>
        <div class="description">
          Uncheck any tool you want to keep advertised to ${subjectName}. The
          rest are disabled for this agent and stop costing schema tokens on
          every request.
        </div>
        <div class="tool-checklist">
          ${toolNames.map(
            (name) => html`
              <sl-checkbox
                ?checked=${Boolean(this.scopeToolSelection[name])}
                @sl-change=${(e: Event) =>
                  this.toggleScopeTool(
                    name,
                    (e.target as HTMLInputElement).checked
                  )}
              >
                ${name}
              </sl-checkbox>
            `
          )}
        </div>
        ${
          governanceHref
            ? html`
                <div class="governance-link">
                  <a href=${governanceHref}>
                    Open ${subjectName}'s Tools &amp; Governance view
                  </a>
                </div>
              `
            : nothing
        }
        <sl-button
          slot="footer"
          size="small"
          @click=${() => {
            this.pendingApply = null;
          }}
        >
          Cancel
        </sl-button>
        <sl-button
          slot="footer"
          size="small"
          variant="primary"
          ?disabled=${checkedCount === 0}
          @click=${this.confirmApply}
        >
          <sl-icon slot="prefix" name="lightning-charge"></sl-icon>
          Disable ${checkedCount} unused tool${checkedCount === 1 ? '' : 's'}
          for this agent
        </sl-button>
      </sl-dialog>
    `;
  }

  private renderApplyDialog() {
    const suggestion = this.pendingApply;
    if (!suggestion?.action) return nothing;
    if (suggestion.action.type === 'scope_tools') {
      return this.renderScopeToolsDialog(suggestion);
    }
    return html`
      <sl-dialog
        label="Apply optimization"
        open
        @sl-after-hide=${() => {
          this.pendingApply = null;
        }}
      >
        <div class="title">${suggestion.title}</div>
        <div class="apply-summary">
          ${this.describeAction(suggestion.action)}
        </div>
        <div class="description">
          This change takes effect for future requests and is recorded in the
          optimization history with a measured before/after outcome.
        </div>
        <sl-button
          slot="footer"
          size="small"
          @click=${() => {
            this.pendingApply = null;
          }}
        >
          Cancel
        </sl-button>
        <sl-button
          slot="footer"
          size="small"
          variant="primary"
          @click=${this.confirmApply}
        >
          Apply
        </sl-button>
      </sl-dialog>
    `;
  }

  private renderOutputFilterDialog() {
    const suggestion = this.outputFilterSuggestion;
    if (!suggestion?.action) return nothing;
    const params = suggestion.action.params || {};
    const serverName =
      typeof params.server_name === 'string' ? params.server_name : null;
    const toolName =
      typeof params.tool_name === 'string' ? params.tool_name : '';
    const suggestedFields = Array.isArray(params.suggested_fields)
      ? (params.suggested_fields as unknown[]).filter(
          (field): field is string => typeof field === 'string'
        )
      : [];
    const managedAgentId =
      typeof params.managed_agent_id === 'string'
        ? params.managed_agent_id
        : null;
    return html`
      <tool-output-filter-dialog
        open
        .serverName=${serverName}
        .toolName=${toolName}
        .suggestedFields=${suggestedFields}
        .managedAgentId=${managedAgentId}
        @dialog-closed=${() => {
          this.outputFilterSuggestion = null;
        }}
      ></tool-output-filter-dialog>
    `;
  }

  private renderSuggestion(suggestion: SessionOptimizationSuggestion) {
    const applied = this.isApplied(suggestion);
    const applying = this.applyingSuggestionId === suggestion.id;
    const serverApplyable = this.isServerApplyable(suggestion);
    const actionType = suggestion.action?.type;
    const isOutputFilter = actionType === 'manage_output_filter';
    const isInspect = actionType === 'open_events';
    return html`
      <div class="suggestion">
        <div class="suggestion-header">
          <div>
            <div class="title">${suggestion.title}</div>
            <div class="description">${suggestion.description}</div>
          </div>
          <sl-badge
            variant=${this.getConfidenceVariant(suggestion.confidence)}
            pill
          >
            ${suggestion.confidence} confidence
          </sl-badge>
        </div>
        <div class="savings">
          Expected savings: ${formatNumber(suggestion.expectedSavingsTokens)}
          tokens · ${formatCost(suggestion.expectedSavingsUsd)}
        </div>
        ${
          suggestion.evidence.length
            ? html`
                <div class="evidence">
                  Evidence: ${suggestion.evidence.join(' · ')}
                </div>
              `
            : ''
        }
        <div class="actions">
          ${
            serverApplyable
              ? applied
                ? html`
                    <sl-badge variant="success" pill>
                      <sl-icon name="check-circle"></sl-icon>
                      Applied
                    </sl-badge>
                  `
                : html`
                    <sl-button
                      size="small"
                      variant="primary"
                      ?loading=${applying}
                      ?disabled=${Boolean(this.applyingSuggestionId)}
                      @click=${() => {
                        if (actionType === 'scope_tools') {
                          this.openScopeToolsDialog(suggestion);
                        } else {
                          this.pendingApply = suggestion;
                        }
                      }}
                    >
                      <sl-icon slot="prefix" name="lightning-charge"></sl-icon>
                      Apply
                    </sl-button>
                  `
              : nothing
          }
          ${
            isOutputFilter
              ? html`
                  <sl-button
                    size="small"
                    variant="primary"
                    @click=${() => {
                      this.outputFilterSuggestion = suggestion;
                    }}
                  >
                    <sl-icon slot="prefix" name="funnel"></sl-icon>
                    Filter tool output
                  </sl-button>
                `
              : nothing
          }
          ${
            isInspect
              ? html`
                  <sl-button
                    size="small"
                    @click=${() => this.emitInspectEvents(suggestion)}
                  >
                    <sl-icon slot="prefix" name="search"></sl-icon>
                    Inspect evidence
                  </sl-button>
                `
              : isOutputFilter || serverApplyable
                ? nothing
                : html`
                    <sl-button
                      size="small"
                      @click=${() => this.emitSuggestion(suggestion)}
                    >
                      <sl-icon slot="prefix" name="search"></sl-icon>
                      View evidence
                    </sl-button>
                  `
          }
        </div>
      </div>
    `;
  }

  private renderProfileTab() {
    const profile = this.optimization?.context_profile;
    if (!profile) {
      return html`<div class="empty">
        No measured context profile yet. Generate suggestions to analyze where
        this session's tokens went.
      </div>`;
    }
    const segments = (profile.segments || [])
      .filter((segment) => segment.estimated_tokens > 0)
      .sort((a, b) => b.estimated_tokens - a.estimated_tokens);
    const maxTokens = Math.max(
      ...segments.map((segment) => segment.estimated_tokens),
      1
    );
    const wasteScore = this.optimization?.waste_score;
    return html`
      <div class="waste-banner">
        ${
          wasteScore !== null && wasteScore !== undefined
            ? html`
                <sl-badge
                  variant=${
                    wasteScore >= 40
                      ? 'danger'
                      : wasteScore >= 15
                        ? 'warning'
                        : 'success'
                  }
                  pill
                >
                  Waste score ${wasteScore}%
                </sl-badge>
              `
            : nothing
        }
        <span class="evidence">
          ${formatNumber(profile.analyzed_event_count)} analyzed requests ·
          ${formatNumber(profile.total_prompt_tokens)} prompt tokens ·
          ${formatNumber(profile.total_completion_tokens)} completion tokens
        </span>
      </div>
      ${
        segments.length
          ? segments.map((segment: SessionContextProfileSegment) => {
              const label = SEGMENT_LABELS[segment.kind] || segment.kind;
              const width = Math.max(
                (segment.estimated_tokens / maxTokens) * 100,
                2
              );
              return html`
                <div class="segment-row">
                  <div class="segment-label">${label}</div>
                  <div class="segment-bar">
                    <div
                      class="segment-bar-fill ${segment.kind}"
                      style="width: ${width}%"
                    ></div>
                  </div>
                  <div class="segment-value">
                    ${formatNumber(segment.estimated_tokens)} ·
                    ${Math.round(segment.share * 100)}%
                  </div>
                </div>
              `;
            })
          : html`<div class="empty">No context segments measured.</div>`
      }
    `;
  }

  private formatOutcomeDelta(value: unknown, label: string) {
    if (typeof value !== 'number') return nothing;
    const positive = value < 0;
    return html`
      <span class=${positive ? 'outcome-positive' : 'outcome-negative'}>
        ${value > 0 ? '+' : ''}${value}% ${label}
      </span>
    `;
  }

  private renderHistoryTab() {
    if (!this.appliedActions.length) {
      return html`<div class="empty">
        Nothing applied yet. Apply a suggestion and Preloop will measure the
        before/after impact on this agent's sessions.
      </div>`;
    }
    return html`
      ${this.appliedActions.map((item) => {
        const outcome = item.outcome || null;
        const appliedAt = new Date(item.applied_at);
        return html`
          <div class="history-item">
            <div class="suggestion-header">
              <div>
                <div class="title">
                  ${item.suggestion_title || item.suggestion_id}
                </div>
                <div class="evidence">
                  ${this.describeAction({
                    type: item.action_type,
                    params: item.params,
                  })}
                </div>
              </div>
              <sl-badge variant="primary" pill>${item.action_type}</sl-badge>
            </div>
            <div class="evidence">
              Applied
              ${
                Number.isNaN(appliedAt.getTime())
                  ? item.applied_at
                  : appliedAt.toLocaleString()
              }
              ${item.applied_by ? html`by ${item.applied_by}` : nothing}
            </div>
            ${
              outcome
                ? html`
                    <div class="outcome">
                      <div class="evidence">
                        Measured since apply:
                        ${formatNumber(Number(outcome.requests || 0))} requests
                        ·
                        ${formatNumber(
                          Number(outcome.avg_total_tokens_per_request || 0)
                        )}
                        tokens/request ·
                        ${formatCost(Number(outcome.avg_cost_per_request || 0))}
                        /request
                      </div>
                      <div>
                        ${this.formatOutcomeDelta(
                          outcome.tokens_per_request_change_pct,
                          'tokens/request'
                        )}
                        ${this.formatOutcomeDelta(
                          outcome.cost_per_request_change_pct,
                          'cost/request'
                        )}
                      </div>
                    </div>
                  `
                : html`
                    <div class="outcome evidence">
                      No follow-up traffic measured yet for this agent.
                    </div>
                  `
            }
          </div>
        `;
      })}
    `;
  }

  private renderTransparency() {
    const optimization = this.optimization;
    if (!optimization) return nothing;
    const parts: string[] = [];
    parts.push(
      optimization.generated_by === 'model'
        ? `Generated by ${optimization.model_name || 'model'}`
        : 'Generated locally from measured session data'
    );
    if (
      optimization.generated_by !== 'model' &&
      optimization.llm_skipped_reason
    ) {
      const reasons: Record<string, string> = {
        daily_cap_reached: 'daily model budget reached',
        model_error: 'configured model could not run',
        model_empty_response: 'model returned no usable output',
      };
      parts.push(
        `model skipped (${
          reasons[optimization.llm_skipped_reason] ||
          optimization.llm_skipped_reason
        })`
      );
    }
    if (optimization.estimated_optimization_cost) {
      parts.push(
        `generation cost ${formatCost(optimization.estimated_optimization_cost)}`
      );
    }
    if (optimization.from_cache) parts.push('cached result');
    if (optimization.generated_at) {
      const generated = new Date(optimization.generated_at);
      if (!Number.isNaN(generated.getTime())) {
        parts.push(`generated ${generated.toLocaleString()}`);
      }
    }
    return html`<div class="transparency">${parts.join(' · ')}</div>`;
  }

  // Aggregate before/after savings across all suggestions — the demo's
  // "hard number": what the session cost vs. what it could cost if every
  // suggestion is applied. Savings are clamped to the session's own
  // cost/tokens (you can't save more than you spent).
  private renderSavingsSummary(suggestions: SessionOptimizationSuggestion[]) {
    if (!this.session || !suggestions.length) return '';
    // Anchor "before" to the analyzed scope when available — that's the
    // baseline the suggestions' savings are measured against. Fall back to the
    // whole-session totals for older cached responses / local-only suggestions.
    const opt = this.optimization;
    const scopeCost = opt?.analyzed_scope_estimated_cost;
    const scopeTokens = opt?.analyzed_scope_total_tokens;
    const before = Number((scopeCost ?? this.session.estimated_cost ?? 0) || 0);
    const beforeTokens = Number(
      (scopeTokens ?? this.session.token_usage?.total_tokens ?? 0) || 0
    );
    const rawUsd = suggestions.reduce(
      (sum, s) => sum + Number(s.expectedSavingsUsd || 0),
      0
    );
    const rawTokens = suggestions.reduce(
      (sum, s) => sum + Number(s.expectedSavingsTokens || 0),
      0
    );
    const savingsUsd = before > 0 ? Math.min(rawUsd, before) : rawUsd;
    const savingsTokens =
      beforeTokens > 0 ? Math.min(rawTokens, beforeTokens) : rawTokens;
    if (savingsUsd <= 0 && savingsTokens <= 0) return '';
    const after = Math.max(before - savingsUsd, 0);
    // Prefer a cost-based percentage; fall back to a token-based one when the
    // scope cost is unknown (0) so the headline still conveys the magnitude.
    const pct =
      before > 0
        ? Math.round((savingsUsd / before) * 100)
        : beforeTokens > 0
          ? Math.round((savingsTokens / beforeTokens) * 100)
          : 0;
    return html`
      <div class="savings-summary">
        <div class="savings-summary-headline">
          Potential savings: <strong>${formatCost(savingsUsd)}</strong>
          ${pct > 0 ? html`<span class="savings-pct">(${pct}%)</span>` : ''}
        </div>
        <div class="savings-summary-detail">
          Analyzed scope: ${formatCost(before)} · ${formatNumber(beforeTokens)}
          tokens → ~${formatCost(after)} after applying all
          ${suggestions.length} suggestion${suggestions.length === 1 ? '' : 's'}
          (${formatNumber(savingsTokens)} tokens saved).
        </div>
      </div>
    `;
  }

  render() {
    if (!this.session) return '';
    const suggestions =
      this.suggestions || suggestSessionOptimizations(this.session);
    const hasProfile = Boolean(this.optimization?.context_profile);

    return html`
      <div class="panel">
        <sl-tab-group>
          <sl-tab slot="nav" panel="suggestions">
            Suggestions (${suggestions.length})
          </sl-tab>
          <sl-tab slot="nav" panel="profile" ?disabled=${!hasProfile}>
            Profile
          </sl-tab>
          <sl-tab slot="nav" panel="history">
            History (${this.appliedActions.length})
          </sl-tab>
          <sl-tab-panel name="suggestions">
            <div class="panel">
              ${this.renderSavingsSummary(suggestions)}
              ${suggestions.map((suggestion) =>
                this.renderSuggestion(suggestion)
              )}
            </div>
          </sl-tab-panel>
          <sl-tab-panel name="profile">${this.renderProfileTab()}</sl-tab-panel>
          <sl-tab-panel name="history">${this.renderHistoryTab()}</sl-tab-panel>
        </sl-tab-group>
        ${this.renderTransparency()} ${this.renderApplyDialog()}
        ${this.renderOutputFilterDialog()}
      </div>
    `;
  }
}
