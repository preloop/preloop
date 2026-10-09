import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import './tools-editor-component';
import {
  getAIModels,
  getApprovalWorkflows,
  getFeatures,
  getFlowGovernance,
  getMCPServers,
  getTools,
  resetFlowGovernance,
  updateFlowGovernance,
} from '../api';
import type {
  AccountGovernanceDefaults,
  FlowGovernanceResponse,
  SubjectGovernanceConfig,
} from '../types';
import type { AccessRuleSummary } from './governance-rule-set-editor';
import {
  normalizeScopedToolRules,
  serializeScopedToolRules,
  type ScopedToolRules,
} from '../utils/scoped-governance';

type RuleFormData = Omit<AccessRuleSummary, 'id' | 'priority'>;

const EMPTY_CONFIG: SubjectGovernanceConfig = {
  allowed_models: [],
  model_budgets: {},
  tool_rules: {},
  tool_enabled_overrides: {},
  approval_workflow_id: null,
  native_tool_approvals: null,
};

/**
 * Governance override for one flow (subject type "flows").
 *
 * Reuses the agent governance building blocks (scoped tools editor, native
 * approval and workflow selectors) and shows what the flow inherits from the
 * account until an override is saved. The flow's Allowed MCP Tools decide
 * which tools the agent sees; this card decides how those calls are governed.
 */
@customElement('flow-governance-card')
export class FlowGovernanceCard extends LitElement {
  @property({ type: String }) flowId = '';
  /**
   * Tools the flow exposes (its allowed_mcp_tools). Matches the backend: an
   * empty list means the flow sees no MCP tools; null shows the full catalog.
   */
  @property({ attribute: false }) allowedToolNames: string[] | null = null;
  /**
   * True when the flow runs as a managed agent (employee flow): unset values
   * then fall back to that agent's settings before the account default.
   */
  @property({ type: Boolean }) inheritsFromAgent = false;

  /** Saves run one at a time so responses cannot land out of order. */
  private saveChain: Promise<void> = Promise.resolve();

  @state() private config: SubjectGovernanceConfig = { ...EMPTY_CONFIG };
  @state() private hasOverride = false;
  @state() private accountDefaults: AccountGovernanceDefaults = {};
  @state() private scopedToolRules: ScopedToolRules = {};
  @state() private toolEnabledOverrides: Record<string, boolean> = {};
  @state() private toolCatalog: any[] = [];
  @state() private mcpServers: any[] = [];
  @state() private workflows: any[] = [];
  @state() private models: any[] = [];
  @state() private features: Record<string, any> = {};
  @state() private loading = true;
  @state() private saving = false;
  @state() private error: string | null = null;

  static styles = css`
    :host {
      display: block;
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--sl-spacing-medium);
      flex-wrap: wrap;
      margin-bottom: var(--sl-spacing-medium);
    }
    .label {
      font-weight: var(--sl-font-weight-semibold);
      color: var(--sl-color-neutral-900);
    }
    .meta {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-600);
    }
    .header {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-x-small);
      width: 100%;
    }
    .header .spacer {
      flex: 1;
    }
    sl-select {
      min-width: 260px;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    if (this.flowId) {
      void this.load();
    }
  }

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('flowId') && changed.get('flowId') !== undefined) {
      void this.load();
    }
  }

  private applyResponse(response: FlowGovernanceResponse): void {
    this.config = { ...EMPTY_CONFIG, ...response.config };
    this.hasOverride = response.has_override;
    this.accountDefaults = response.account_defaults || {};
    this.scopedToolRules = normalizeScopedToolRules(response.config.tool_rules);
    this.toolEnabledOverrides = response.config.tool_enabled_overrides || {};
  }

  async load(): Promise<void> {
    if (!this.flowId) return;
    this.loading = true;
    this.error = null;
    try {
      const governance = await getFlowGovernance(this.flowId);
      this.applyResponse(governance);
      // Catalog data only feeds the editors; a failure there must not hide
      // the override state that already loaded.
      const [tools, servers, workflows, models, features] =
        await Promise.allSettled([
          getTools(),
          getMCPServers(),
          getApprovalWorkflows(),
          getAIModels(),
          getFeatures(),
        ]);
      const value = <T>(r: PromiseSettledResult<T>, fallback: T): T =>
        r.status === 'fulfilled' && r.value ? r.value : fallback;
      this.toolCatalog = value(tools, [] as any[]);
      this.mcpServers = value(servers, [] as any[]);
      this.workflows = value(workflows, [] as any[]);
      this.models = value(models, [] as any[]);
      this.features = value(features, { features: {} } as any).features || {};
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to load flow governance';
    } finally {
      this.loading = false;
    }
  }

  /** Tools shown in the editor: the flow's allowed tools, or all. */
  private visibleTools(): any[] {
    if (this.allowedToolNames === null) return this.toolCatalog;
    const allowed = new Set(this.allowedToolNames);
    return this.toolCatalog.filter((tool) => allowed.has(tool.name));
  }

  save(patch: Partial<SubjectGovernanceConfig> = {}): Promise<void> {
    // Apply the edit locally right away, then queue the write behind any
    // in-flight one. Each queued write sends the latest local state.
    this.config = { ...this.config, ...patch };
    this.saveChain = this.saveChain.then(() => this.persist());
    return this.saveChain;
  }

  private async persist(): Promise<void> {
    if (!this.flowId) return;
    this.saving = true;
    this.error = null;
    try {
      const payload: SubjectGovernanceConfig = {
        ...this.config,
        tool_rules: serializeScopedToolRules(this.scopedToolRules),
        tool_enabled_overrides: this.toolEnabledOverrides,
      };
      this.applyResponse(await updateFlowGovernance(this.flowId, payload));
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to save flow governance';
    } finally {
      this.saving = false;
    }
  }

  reset(): Promise<void> {
    this.saveChain = this.saveChain.then(() => this.persistReset());
    return this.saveChain;
  }

  private async persistReset(): Promise<void> {
    if (!this.flowId) return;
    this.saving = true;
    this.error = null;
    try {
      this.applyResponse(await resetFlowGovernance(this.flowId));
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to reset flow governance';
    } finally {
      this.saving = false;
    }
  }

  private async saveRule(
    toolName: string,
    existing: AccessRuleSummary | null,
    formData: RuleFormData,
    settlement?: { resolve?: () => void; reject?: (message: string) => void }
  ): Promise<void> {
    const previous = this.scopedToolRules[toolName];
    const current = [...(this.scopedToolRules[toolName] || [])].sort(
      (a, b) => a.priority - b.priority
    );
    const next = existing
      ? current.map((rule) =>
          rule.id === existing.id ? { ...rule, ...formData } : rule
        )
      : [
          ...current,
          {
            id: `scoped:${toolName}:${Date.now()}:${current.length}`,
            priority: current.length,
            ...formData,
          },
        ];
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: next.map((rule, index) => ({ ...rule, priority: index })),
    };
    await this.save();
    if (this.error) {
      const restored = { ...this.scopedToolRules };
      if (previous) restored[toolName] = previous;
      else delete restored[toolName];
      this.scopedToolRules = restored;
      settlement?.reject?.(this.error);
    } else {
      settlement?.resolve?.();
    }
  }

  private deleteRule(toolName: string, ruleId: string): void {
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: (this.scopedToolRules[toolName] || [])
        .filter((rule) => rule.id !== ruleId)
        .map((rule, index) => ({ ...rule, priority: index })),
    };
    void this.save();
  }

  private reorderRules(
    toolName: string,
    reordered: { id: string; priority: number }[]
  ): void {
    const priorities = new Map(reordered.map((r) => [r.id, r.priority]));
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: [...(this.scopedToolRules[toolName] || [])]
        .map((rule) => ({
          ...rule,
          priority: priorities.get(rule.id) ?? rule.priority,
        }))
        .sort((a, b) => a.priority - b.priority)
        .map((rule, index) => ({ ...rule, priority: index })),
    };
    void this.save();
  }

  private toggleEnabled(e: CustomEvent): void {
    const { tool, isEnabled } = e.detail;
    this.toolEnabledOverrides = {
      ...this.toolEnabledOverrides,
      [tool.name]: isEnabled,
    };
    void this.save();
  }

  private revertTool(e: CustomEvent): void {
    const { tool } = e.detail;
    const rules = { ...this.scopedToolRules };
    delete rules[tool.name];
    const overrides = { ...this.toolEnabledOverrides };
    delete overrides[tool.name];
    this.scopedToolRules = rules;
    this.toolEnabledOverrides = overrides;
    void this.save();
  }

  private inheritedApprovalsLabel(): string {
    const account =
      this.accountDefaults.native_tool_approvals === 'off' ? 'Off' : 'Enforce';
    return this.inheritsFromAgent
      ? `agent setting, else account default: ${account}`
      : `account default: ${account}`;
  }

  private inheritedWorkflowLabel(): string {
    const pinned = this.accountDefaults.approval_workflow_id;
    const workflow = pinned
      ? this.workflows.find((w) => w.id === pinned)
      : this.workflows.find((w) => w.is_default);
    const account = workflow?.name || 'account default';
    return this.inheritsFromAgent ? `agent pin, else ${account}` : account;
  }

  render() {
    return html`
      <sl-card data-flow-governance>
        <div slot="header" class="header">
          <sl-icon name="shield-lock"></sl-icon>
          Governance
          <sl-badge
            id="flow-governance-state"
            pill
            variant=${this.hasOverride ? 'primary' : 'neutral'}
            >${
              this.hasOverride ? 'Flow override' : 'Inherits account defaults'
            }</sl-badge
          >
          <span class="spacer"></span>
          <sl-button
            id="flow-governance-reset"
            size="small"
            ?disabled=${!this.hasOverride || this.saving}
            @click=${() => this.reset()}
          >
            <sl-icon slot="prefix" name="arrow-counterclockwise"></sl-icon>
            Reset to account defaults
          </sl-button>
        </div>
        ${
          this.error
            ? html`<sl-alert variant="danger" open>${this.error}</sl-alert>`
            : nothing
        }
        <p class="meta">
          Allowed MCP Tools decide which tools this flow's agent can see. The
          settings below decide how this flow's model and tool calls are
          governed. Anything left unset follows the account policy.
        </p>
        ${this.loading ? html`<p class="meta">Loading…</p>` : this.renderBody()}
      </sl-card>
    `;
  }

  private renderBody() {
    return html`
      <div class="row">
        <div>
          <div class="label">Native tool approvals</div>
          <div class="meta">
            Human approval for the agent's native tool calls, and which workflow
            decides.
          </div>
        </div>
        <div class="row" style="margin: 0;">
          <sl-select
            id="flow-native-tool-approvals"
            size="small"
            hoist
            .value=${this.config.native_tool_approvals ?? ''}
            @sl-change=${(e: Event) => {
              const value = (e.target as HTMLSelectElement).value;
              void this.save({
                native_tool_approvals:
                  value === 'enforce' || value === 'off' ? value : null,
              });
            }}
          >
            <sl-option value=""
              >Inherit (${this.inheritedApprovalsLabel()})</sl-option
            >
            <sl-option value="enforce"
              >Enforce: always require approval</sl-option
            >
            <sl-option value="off">Off: auto-approve (recorded)</sl-option>
          </sl-select>
          <sl-select
            id="flow-approval-workflow"
            size="small"
            hoist
            .value=${this.config.approval_workflow_id ?? ''}
            @sl-change=${(e: Event) => {
              const value = (e.target as HTMLSelectElement).value;
              void this.save({ approval_workflow_id: value || null });
            }}
          >
            <sl-option value=""
              >Inherit (${this.inheritedWorkflowLabel()})</sl-option
            >
            ${this.workflows.map(
              (w) => html`<sl-option value=${w.id}>${w.name}</sl-option>`
            )}
          </sl-select>
        </div>
      </div>
      <div class="row">
        <div>
          <div class="label">Allowed models</div>
          <div class="meta">
            Gateway models this flow may call. Empty inherits the account (every
            enabled model).
          </div>
        </div>
        <sl-select
          id="flow-allowed-models"
          size="small"
          hoist
          multiple
          clearable
          placeholder="All models (inherited)"
          .value=${this.config.allowed_models || []}
          @sl-change=${(e: Event) => {
            const raw = (e.target as any).value;
            const value: string[] = Array.isArray(raw) ? raw : [];
            void this.save({ allowed_models: value });
          }}
        >
          ${this.models.map(
            (m) => html`<sl-option value=${m.id}>${m.name}</sl-option>`
          )}
        </sl-select>
      </div>
      <tools-editor-component
        mode="scoped"
        ?collapseByDefault=${true}
        .tools=${this.visibleTools()}
        .mcpServers=${this.mcpServers}
        .scopedToolRules=${this.scopedToolRules}
        .toolEnabledOverrides=${this.toolEnabledOverrides}
        .approvalPolicies=${this.workflows}
        .features=${this.features}
        @save-rule=${(e: CustomEvent) =>
          this.saveRule(
            e.detail.tool.name,
            e.detail.existingRule || e.detail.rule || null,
            e.detail.formData,
            e.detail
          )}
        @delete-rule=${(e: CustomEvent) =>
          this.deleteRule(e.detail.tool.name, e.detail.rule.id)}
        @reorder-rules=${(e: CustomEvent) =>
          this.reorderRules(e.detail.tool.name, e.detail.reorderedRules)}
        @toggle-enabled=${this.toggleEnabled}
        @revert-tool=${this.revertTool}
        @policy-created=${() => this.load()}
      ></tools-editor-component>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'flow-governance-card': FlowGovernanceCard;
  }
}
