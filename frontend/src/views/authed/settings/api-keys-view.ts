import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getApiKeys,
  createApiKey,
  deleteApiKey,
  getApprovalWorkflows,
  getApiKeyGovernance,
  getFeatures,
  getTools,
  updateApiKeyGovernance,
} from '../../../api';
import type { ApiKey, SubjectGovernanceConfig } from '../../../types';
import type { AccessRuleSummary } from '../../../components/governance-rule-set-editor';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import type SlMenuItem from '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '../../../components/governance-rule-set-editor.ts';
import '../../../components/budget-policy-editor.ts';
import '../../../components/resource-actions.ts';
import '../../../components/list-selection.ts';
import {
  ListSelectionController,
  confirmBulkAction,
  type BulkAction,
} from '../../../components/list-selection';
import { confirmDialog } from '../../../components/confirm-dialog';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { parseUTCDate } from '../../../utils/date';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import {
  normalizeScopedToolRules,
  serializeScopedToolRules,
  type ScopedToolRules,
} from '../../../utils/scoped-governance';
import { consoleDialogStyles } from '../../../styles/console-dialog';

interface GovernanceToolDefinition {
  name: string;
  description?: string;
  schema?: Record<string, unknown>;
}

@customElement('api-keys-view')
export class ApiKeysView extends LitElement {
  @state()
  private apiKeys: ApiKey[] = [];

  @state()
  private isLoading = true;

  @state()
  private error: string | null = null;

  @state()
  private isCreateModalOpen = false;

  @state()
  private isShowKeyModalOpen = false;

  @state()
  private newKeyName = '';

  @state()
  private newKeyExpiry = 'never';

  @state()
  private newKeyExpiryLabel = 'Never';

  @state()
  private newlyCreatedKey: ApiKey | null = null;

  /** Feedback for the one-time key dialog's Copy button. */
  @state()
  private keyCopyStatus: 'idle' | 'copied' | 'manual' = 'idle';

  @state()
  private isSelectOpen = false;

  @state()
  private createError: string | null = null;

  @state()
  private governanceKeyId: string | null = null;

  @state()
  private governanceKeyName = '';

  @state()
  private governanceAllowedModels = '';

  @state()
  private governanceModelBudgets = '{}';

  @state()
  private governanceToolRules = '{}';

  @state()
  private scopedToolRules: ScopedToolRules = {};

  @state()
  private toolCatalog: GovernanceToolDefinition[] = [];

  @state()
  private approvalWorkflows: any[] = [];

  @state()
  private featureFlags: { [key: string]: boolean | string[] } = {};

  @state()
  private governanceToolToAdd = '';

  @state()
  private governanceCustomToolName = '';

  @state()
  private governanceError: string | null = null;

  /**
   * Revoked and expired keys are history, not credentials. An account that
   * mints a key per flow run accumulates dozens of them and they push the
   * long-lived keys off the first screen, so they stay behind a footer until
   * the operator asks for them.
   */
  @state()
  private showAllKeys = false;

  @state()
  private liveActivity: Record<
    string,
    { modelCalls: number; toolCalls: number; lastActivityAt: string | null }
  > = {};

  private unsubscribeRealtime?: () => void;

  /**
   * Multi-select for the key table. Only keys that can still sign a request
   * are selectable: a revoked key has nothing left to revoke.
   */
  readonly selection = new ListSelectionController<ApiKey>(this, {
    idOf: (key) => key.id,
    selectable: (key) => !this.isRetired(key),
  });

  async connectedCallback() {
    super.connectedCallback();
    await this.fetchApiKeys();
    this.connectRealtime();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.unsubscribeRealtime?.();
  }

  async fetchApiKeys() {
    this.isLoading = true;
    this.error = null;
    try {
      this.apiKeys = await getApiKeys();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to fetch API keys';
    } finally {
      this.isLoading = false;
    }
  }

  private async fetchGovernanceEditorContext(): Promise<void> {
    try {
      const [tools, workflows, features] = await Promise.all([
        getTools(),
        getApprovalWorkflows(),
        getFeatures(),
      ]);
      this.toolCatalog = (tools || []).map((tool: any) => ({
        name: tool.name,
        description: tool.description,
        schema:
          tool.schema && typeof tool.schema === 'object'
            ? tool.schema
            : undefined,
      }));
      this.approvalWorkflows = workflows || [];
      this.featureFlags = features?.features || {};
    } catch (error) {
      console.error('Failed to load governance editor context:', error);
    }
  }

  private connectRealtime(): void {
    const unsubscribe = unifiedWebSocketManager.subscribe(
      'gateway_activity',
      (message) => this.handleGatewayActivity(message)
    );
    this.unsubscribeRealtime = () => unsubscribe();
    void unifiedWebSocketManager.connect();
  }

  private handleGatewayActivity(message: any): void {
    const payload = message?.payload ?? {};
    const keyId = payload.api_key_id;
    if (!keyId || !this.apiKeys.some((key) => key.id === keyId)) {
      return;
    }
    const type = message?.type;
    const previous = this.liveActivity[keyId] ?? {
      modelCalls: 0,
      toolCalls: 0,
      lastActivityAt: null,
    };
    const next = {
      modelCalls: previous.modelCalls + (type === 'model_gateway_call' ? 1 : 0),
      toolCalls: previous.toolCalls + (type === 'mcp_call' ? 1 : 0),
      lastActivityAt:
        payload.timestamp ??
        payload.last_activity_at ??
        previous.lastActivityAt ??
        new Date().toISOString(),
    };
    this.liveActivity = {
      ...this.liveActivity,
      [keyId]: next,
    };
    this.apiKeys = this.apiKeys.map((key) =>
      key.id !== keyId
        ? key
        : {
            ...key,
            activity_status: 'active_now',
            last_activity_at: next.lastActivityAt,
            last_used_at: next.lastActivityAt ?? key.last_used_at,
            recent_model_calls:
              (key.recent_model_calls ?? 0) +
              (type === 'model_gateway_call' ? 1 : 0),
            recent_tool_calls:
              (key.recent_tool_calls ?? 0) + (type === 'mcp_call' ? 1 : 0),
          }
    );
  }

  private isRevoked(key: ApiKey): boolean {
    return key.activity_status === 'revoked';
  }

  private isExpired(key: ApiKey): boolean {
    if (!key.expires_at) return false;
    return parseUTCDate(key.expires_at).getTime() <= Date.now();
  }

  /** A key that can no longer sign a request: nothing to do with it but read it. */
  private isRetired(key: ApiKey): boolean {
    return this.isRevoked(key) || this.isExpired(key);
  }

  private getActivityVariant(key: ApiKey): string {
    if (this.isRevoked(key)) return 'danger';
    if (this.isExpired(key)) return 'neutral';
    if (key.activity_status === 'active_now') return 'success';
    if (key.activity_status === 'recently_active') return 'primary';
    return 'neutral';
  }

  private getActivityLabel(key: ApiKey): string {
    if (this.isRevoked(key)) return 'Revoked';
    if (this.isExpired(key)) return 'Expired';
    if (key.activity_status === 'active_now') return 'Active now';
    if (key.activity_status === 'recently_active') return 'Recently active';
    return 'Idle';
  }

  private visibleKeys(): ApiKey[] {
    if (this.showAllKeys) return this.apiKeys;
    return this.apiKeys.filter((key) => !this.isRetired(key));
  }

  private hiddenKeyCount(): number {
    return this.apiKeys.filter((key) => this.isRetired(key)).length;
  }

  /**
   * Prunes the selection to the keys this pass paints, before the bulk bar is
   * built. Doing it here rather than inside `render` keeps the bar's count and
   * the table's checkboxes from ever disagreeing by one pass.
   */
  protected willUpdate(): void {
    this.selection.setItems(
      this.isLoading || this.error ? [] : this.visibleKeys()
    );
  }

  async handleCreateApiKey() {
    if (!this.newKeyName) {
      return;
    }

    this.createError = null;
    const trimmedName = this.newKeyName.trim();
    if (!trimmedName) {
      this.createError = 'Please enter a name for your key.';
      return;
    }

    const existingNames = new Set(
      this.apiKeys.map((k) => k.name.trim().toLowerCase())
    );
    if (existingNames.has(trimmedName.toLowerCase())) {
      this.createError = 'API key with this name already exists.';
      return;
    }

    let expires_at: string | null = null;
    if (this.newKeyExpiry !== 'never') {
      const now = new Date();
      const days = parseInt(this.newKeyExpiry.replace('days', ''));
      now.setDate(now.getDate() + days);
      expires_at = now.toISOString();
    }

    try {
      const newKey = await createApiKey(trimmedName, expires_at);
      this.newlyCreatedKey = newKey;
      this.keyCopyStatus = 'idle';
      this.isCreateModalOpen = false;
      this.isShowKeyModalOpen = true;
      this.newKeyName = ''; // Reset for next time
      this.newKeyExpiry = 'never'; // Reset for next time
      this.newKeyExpiryLabel = 'Never'; // Reset for next time
      await this.fetchApiKeys();
    } catch (error) {
      this.createError =
        error instanceof Error ? error.message : 'Failed to create API key';
    }
  }

  async handleDeleteApiKey(keyId: string, keyName?: string) {
    const confirmed = await confirmDialog({
      title: 'Revoke API key',
      message: keyName ? `Revoke "${keyName}"?` : 'Revoke this API key?',
      detail:
        'Anything still authenticating with this key stops working immediately. This cannot be undone.',
      confirmLabel: 'Revoke key',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }
    try {
      await deleteApiKey(keyId);
      await this.fetchApiKeys();
    } catch (error) {
      console.error('Failed to delete API key:', error);
    }
  }

  /** The only bulk action a key has: revoke. */
  private get bulkActions(): BulkAction[] {
    return [
      {
        id: 'revoke',
        label: 'Revoke',
        icon: 'trash',
        variant: 'danger',
      },
    ];
  }

  /**
   * Revokes every selected key, one DELETE each with the shared bound.
   *
   * There is no batch endpoint and a transaction would be the wrong shape
   * here: each key is independent, and a single failure must not silently
   * un-revoke the keys that already went.
   */
  private async handleBulkRevoke(): Promise<void> {
    const keys = this.selection.selectedItems;
    if (keys.length === 0) return;
    const confirmed = await confirmBulkAction({
      title: keys.length === 1 ? 'Revoke API key' : 'Revoke API keys',
      message: `Revoke ${keys.length} ${keys.length === 1 ? 'key' : 'keys'}?`,
      names: keys.map((key) => key.name),
      detail:
        'Anything still authenticating with these keys stops working immediately. This cannot be undone.',
      confirmLabel: keys.length === 1 ? 'Revoke key' : 'Revoke keys',
      variant: 'danger',
    });
    if (!confirmed) return;

    await this.selection.run(
      'revoke',
      keys.map((key) => ({ id: key.id, name: key.name })),
      (key) => deleteApiKey(key.id),
      { verb: 'revoke', verbPast: 'revoked', noun: 'key' }
    );
    await this.fetchApiKeys();
  }

  private async openGovernanceDialog(key: ApiKey): Promise<void> {
    this.governanceError = null;
    this.governanceKeyId = key.id;
    this.governanceKeyName = key.name;
    try {
      const [response] = await Promise.all([
        getApiKeyGovernance(key.id),
        this.fetchGovernanceEditorContext(),
      ]);
      this.governanceAllowedModels = response.config.allowed_models.join(', ');
      this.governanceModelBudgets = JSON.stringify(
        response.config.model_budgets || {},
        null,
        2
      );
      this.scopedToolRules = normalizeScopedToolRules(
        response.config.tool_rules
      );
      this.governanceToolRules = JSON.stringify(
        response.config.tool_rules || {},
        null,
        2
      );
    } catch (error) {
      this.governanceError =
        error instanceof Error
          ? error.message
          : 'Failed to load API key governance';
    }
  }

  private async saveGovernance(): Promise<void> {
    if (!this.governanceKeyId) {
      return;
    }
    this.governanceError = null;
    try {
      const config: SubjectGovernanceConfig = {
        allowed_models: this.governanceAllowedModels
          .split(',')
          .map((value) => value.trim())
          .filter(Boolean),
        model_budgets: JSON.parse(this.governanceModelBudgets || '{}'),
        tool_rules: serializeScopedToolRules(this.scopedToolRules),
      };
      const response = await updateApiKeyGovernance(
        this.governanceKeyId,
        config
      );
      this.governanceAllowedModels = response.config.allowed_models.join(', ');
      this.governanceModelBudgets = JSON.stringify(
        response.config.model_budgets || {},
        null,
        2
      );
      this.scopedToolRules = normalizeScopedToolRules(
        response.config.tool_rules
      );
      this.governanceToolRules = JSON.stringify(
        response.config.tool_rules || {},
        null,
        2
      );
    } catch (error) {
      this.governanceError =
        error instanceof Error
          ? error.message
          : 'Failed to update API key governance';
    }
  }

  private getGovernanceTool(toolName: string): GovernanceToolDefinition | null {
    return (
      this.toolCatalog.find((tool) => tool.name === toolName.trim()) ?? null
    );
  }

  private getAvailableGovernanceTools(): GovernanceToolDefinition[] {
    const configured = new Set(Object.keys(this.scopedToolRules));
    return this.toolCatalog.filter((tool) => !configured.has(tool.name));
  }

  private addGovernanceToolScope(): void {
    const toolName = (
      this.governanceCustomToolName.trim() || this.governanceToolToAdd.trim()
    ).trim();
    if (!toolName || this.scopedToolRules[toolName]) {
      return;
    }
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: [],
    };
    this.governanceToolToAdd = '';
    this.governanceCustomToolName = '';
    this.governanceToolRules = JSON.stringify(
      serializeScopedToolRules(this.scopedToolRules),
      null,
      2
    );
  }

  private removeGovernanceToolScope(toolName: string): void {
    const nextRules = { ...this.scopedToolRules };
    delete nextRules[toolName];
    this.scopedToolRules = nextRules;
    this.governanceToolRules = JSON.stringify(
      serializeScopedToolRules(nextRules),
      null,
      2
    );
  }

  private saveScopedToolRule(
    toolName: string,
    existingRule: AccessRuleSummary | null,
    formData: {
      action: 'allow' | 'deny' | 'require_approval';
      condition_expression: string | null;
      condition_type: 'simple' | 'cel';
      description: string | null;
      is_enabled: boolean;
      approval_workflow_id: string | null;
    }
  ): void {
    const currentRules = [...(this.scopedToolRules[toolName] || [])].sort(
      (left, right) => left.priority - right.priority
    );
    const nextRules = existingRule
      ? currentRules.map((rule) =>
          rule.id === existingRule.id ? { ...rule, ...formData } : rule
        )
      : [
          ...currentRules,
          {
            id: `scoped:${toolName}:${Date.now()}:${currentRules.length}`,
            priority: currentRules.length,
            ...formData,
          },
        ];
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: nextRules.map((rule, index) => ({
        ...rule,
        priority: index,
      })),
    };
    this.governanceToolRules = JSON.stringify(
      serializeScopedToolRules(this.scopedToolRules),
      null,
      2
    );
  }

  private deleteScopedToolRule(toolName: string, ruleId: string): void {
    const nextRules = (this.scopedToolRules[toolName] || [])
      .filter((rule) => rule.id !== ruleId)
      .map((rule, index) => ({
        ...rule,
        priority: index,
      }));
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: nextRules,
    };
    this.governanceToolRules = JSON.stringify(
      serializeScopedToolRules(this.scopedToolRules),
      null,
      2
    );
  }

  private reorderScopedToolRules(
    toolName: string,
    reorderedRules: { id: string; priority: number }[]
  ): void {
    const priorities = new Map(
      reorderedRules.map((rule) => [rule.id, rule.priority] as const)
    );
    const nextRules = [...(this.scopedToolRules[toolName] || [])]
      .map((rule) => ({
        ...rule,
        priority: priorities.get(rule.id) ?? rule.priority,
      }))
      .sort((left, right) => left.priority - right.priority)
      .map((rule, index) => ({
        ...rule,
        priority: index,
      }));
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: nextRules,
    };
    this.governanceToolRules = JSON.stringify(
      serializeScopedToolRules(this.scopedToolRules),
      null,
      2
    );
  }

  private async refreshGovernanceWorkflows(): Promise<void> {
    try {
      this.approvalWorkflows = await getApprovalWorkflows();
    } catch (error) {
      console.error('Failed to refresh approval workflows:', error);
      this.governanceError =
        error instanceof Error
          ? error.message
          : 'Failed to refresh approval workflows';
    }
  }

  /**
   * `navigator.clipboard` only exists in secure contexts, so a self-hosted
   * console served over plain HTTP has none.
   */
  private _clipboardAvailable(): boolean {
    return typeof navigator.clipboard?.writeText === 'function';
  }

  private _keyField(): (HTMLElement & { select: () => void }) | null {
    return this.renderRoot.querySelector('sl-input.key-field');
  }

  private async _copyKey() {
    const key = this.newlyCreatedKey?.key;
    if (!key) return;
    if (this._clipboardAvailable()) {
      try {
        await navigator.clipboard.writeText(key);
        this.keyCopyStatus = 'copied';
        return;
      } catch (error) {
        console.warn('Clipboard write failed, falling back:', error);
      }
    }
    // Select the key so Ctrl/Cmd+C copies it, and try the legacy copy
    // command, which still works without a secure context.
    const field = this._keyField();
    field?.focus();
    field?.select();
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    this.keyCopyStatus = copied ? 'copied' : 'manual';
  }

  private _closeKeyDialog() {
    this.isShowKeyModalOpen = false;
    this.keyCopyStatus = 'idle';
  }

  /** The key is shown once: only the deliberate close controls dismiss it. */
  private _guardKeyDialogClose(event: CustomEvent<{ source: string }>) {
    if (event.detail?.source !== 'close-button') {
      event.preventDefault();
    }
  }

  private _handleExpirySelect(e: CustomEvent) {
    const item = e.detail.item as SlMenuItem;
    this.newKeyExpiry = item.value;
    this.newKeyExpiryLabel = item.textContent?.trim() ?? 'Never';
  }

  /** The header checkbox: selects or clears every key on the page. */
  private renderSelectAll() {
    return html`<list-select-checkbox
      label="Select all keys"
      ?checked=${this.selection.allSelected}
      ?indeterminate=${this.selection.someSelected}
      ?disabled=${this.selection.busy}
      @selection-toggle=${this.selection.handleToggleEvent}
    ></list-select-checkbox>`;
  }

  /**
   * The bulk bar, over the table's own header row.
   *
   * This page has no filter bar to hand over, so it takes the fallback the
   * mail clients use: the header row is where the bar goes. The bar is laid
   * out on top of that row (`position: absolute` inside the `thead`), never
   * inserted into the table, so the row keeps its exact height and no key
   * moves under the pointer that just picked it. The column labels and the
   * select-all box go `visibility: hidden` underneath, which keeps their
   * geometry and takes them out of the tab order while they are covered.
   */
  private renderBulkBar() {
    return html`<div
      class="head-bulk-bar"
      ?data-hidden=${this.selection.count === 0}
      ?inert=${this.selection.count === 0}
    >
      <list-bulk-bar
        docked
        label="API key bulk actions"
        .count=${this.selection.count}
        .total=${this.selection.order.length}
        .actions=${this.bulkActions}
        .running=${this.selection.running}
        .progressDone=${this.selection.progressDone}
        .progressTotal=${this.selection.progressTotal}
        @bulk-action=${() => void this.handleBulkRevoke()}
        @selection-select-all=${() => this.selection.toggleAll(true)}
        @selection-clear=${() => this.selection.clear()}
      ></list-bulk-bar>
    </div>`;
  }

  render() {
    const renderContent = () => {
      if (this.isLoading) {
        return html`<div class="loading-indicator">
          <sl-spinner></sl-spinner>
        </div>`;
      }
      if (this.error) {
        return html`
          <sl-alert variant="danger" open>
            <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
            <strong>Error:</strong> ${this.error}
          </sl-alert>
        `;
      }

      if (this.apiKeys.length === 0) {
        return html`
          <sl-alert variant="primary" open>
            <sl-icon slot="icon" name="info-circle"></sl-icon>
            No API keys created yet.
            <a
              href="#"
              @click=${(e: Event) => {
                e.preventDefault();
                this.isCreateModalOpen = true;
              }}
              >Add an API key</a
            >
          </sl-alert>
        `;
      }

      const visibleKeys = this.visibleKeys();
      const hiddenCount = this.hiddenKeyCount();
      const hiddenLabel = `${hiddenCount} ${
        hiddenCount === 1 ? 'key is' : 'keys are'
      } revoked or expired and hidden`;
      const shownLabel = `Showing ${this.apiKeys.length} keys, including ${hiddenCount} revoked or expired`;

      return html`
        <sl-card class="table-card">
          <div class="table-shell">
            ${this.renderBulkBar()}
            <div class="table-scroll">
              <table
                class="styled-table"
                role="grid"
                aria-multiselectable="true"
                aria-label="API keys"
              >
                <thead class=${this.selection.count > 0 ? 'selecting' : ''}>
                  <tr>
                    <th class="select-cell">${this.renderSelectAll()}</th>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Last activity</th>
                    <th>Recent usage</th>
                    <th>Expires</th>
                    <th class="actions-cell">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  ${
                    visibleKeys.length === 0
                      ? html`<tr>
                          <td colspan="8" class="empty-row">No active keys.</td>
                        </tr>`
                      : ''
                  }
                  ${repeat(
                    visibleKeys,
                    (key) => key.id,
                    (key) => html`
                      <tr
                        data-selection-id=${key.id}
                        aria-selected=${
                          this.selection.isSelected(key.id) ? 'true' : 'false'
                        }
                      >
                        <td class="select-cell">
                          ${
                            this.isRetired(key)
                              ? nothing
                              : html`<list-select-checkbox
                                  item-id=${key.id}
                                  label=${`Select ${key.name}`}
                                  ?checked=${this.selection.isSelected(key.id)}
                                  ?disabled=${this.selection.busy}
                                  @selection-toggle=${
                                    this.selection.handleToggleEvent
                                  }
                                ></list-select-checkbox>`
                          }
                        </td>
                        <td>
                          <div
                            style="display: flex; align-items: center; gap: var(--sl-spacing-2x-small); flex-wrap: wrap;"
                          >
                            <a
                              href="/console/settings/api-keys/${key.id}"
                              style="font-weight: 600; text-decoration: none; color: var(--sl-color-primary-600);"
                            >
                              ${key.name}
                            </a>
                            ${
                              key.managed_agent_id
                                ? html`<sl-badge variant="neutral" size="small"
                                    >Agent</sl-badge
                                  >`
                                : ''
                            }
                          </div>
                        </td>
                        <td>
                          <sl-badge
                            class="chip"
                            pill
                            variant=${this.getActivityVariant(key)}
                          >
                            ${this.getActivityLabel(key)}
                          </sl-badge>
                        </td>
                        <td>
                          ${parseUTCDate(key.created_at).toLocaleDateString()}
                        </td>
                        <td>
                          ${
                            key.last_activity_at || key.last_used_at
                              ? parseUTCDate(
                                  key.last_activity_at || key.last_used_at || ''
                                ).toLocaleDateString()
                              : 'Never'
                          }
                        </td>
                        <td>
                          ${
                            (key.recent_model_calls ?? 0) +
                            (key.recent_tool_calls ?? 0)
                          }
                          (${key.recent_model_calls ?? 0} model /
                          ${key.recent_tool_calls ?? 0} tool)
                        </td>
                        <td>
                          ${
                            key.expires_at
                              ? parseUTCDate(
                                  key.expires_at
                                ).toLocaleDateString()
                              : 'Never'
                          }
                        </td>
                        <td class="actions-cell">
                          <!-- A revoked or expired key cannot be revoked again, so
                           it carries no actions at all. -->
                          <resource-actions
                            menu-only
                            .actions=${
                              this.isRetired(key)
                                ? []
                                : [
                                    {
                                      id: 'revoke',
                                      label: 'Revoke key',
                                      icon: 'trash',
                                      variant: 'danger' as const,
                                      onClick: () =>
                                        this.handleDeleteApiKey(
                                          key.id,
                                          key.name
                                        ),
                                    },
                                  ]
                            }
                          ></resource-actions>
                        </td>
                      </tr>
                    `
                  )}
                </tbody>
              </table>
            </div>
          </div>
          ${
            hiddenCount > 0 && !this.showAllKeys
              ? html`<div class="table-footnote">
                  <span>${hiddenLabel}</span>
                  <span aria-hidden="true">·</span>
                  <button
                    type="button"
                    class="link-button"
                    @click=${() => {
                      this.showAllKeys = true;
                    }}
                  >
                    Show all
                  </button>
                </div>`
              : ''
          }
          ${
            this.showAllKeys && hiddenCount > 0
              ? html`<div class="table-footnote">
                  <span>${shownLabel}</span>
                  <span aria-hidden="true">·</span>
                  <button
                    type="button"
                    class="link-button"
                    @click=${() => {
                      this.showAllKeys = false;
                    }}
                  >
                    Hide them
                  </button>
                </div>`
              : ''
          }
        </sl-card>
      `;
    };

    return html`
      <view-header
        headerText="API keys"
        description='Credentials for the gateway and MCP endpoints. Keys labeled "Managed Agent" were minted automatically when an agent was onboarded, and "Flow Execution" keys are minted for a single flow run and revoked when it ends.'
        width="narrow"
      >
        <div slot="main-column">
          <sl-button
            variant="primary"
            @click=${() => {
              this.isCreateModalOpen = true;
            }}
            >Create API key</sl-button
          >
        </div>
      </view-header>
      <div class="column-layout narrow">
        <div class="main-column">${renderContent()}</div>
        <div class="side-column"></div>
      </div>

      <sl-dialog label="Create API key" .open=${this.isCreateModalOpen}>
        ${
          this.createError
            ? html`<sl-alert variant="danger" open style="margin-bottom: 1rem;">
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                <strong>Error:</strong> ${this.createError}
              </sl-alert>`
            : null
        }
        <sl-input
          autofocus
          style="margin-bottom: 1rem;"
          label="Key name"
          placeholder="Enter a name for your key"
          .value=${this.newKeyName}
          @sl-input=${(e: Event) =>
            (this.newKeyName = (e.target as HTMLInputElement).value)}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter' && this.newKeyName) {
              this.handleCreateApiKey();
            }
          }}
        ></sl-input>
        <label class="form-label">Key Expiry</label>
        <sl-dropdown class="expiry-dropdown">
          <sl-button slot="trigger" caret>${this.newKeyExpiryLabel}</sl-button>
          <sl-menu @sl-select=${this._handleExpirySelect}>
            <sl-menu-item value="never">Never</sl-menu-item>
            <sl-menu-item value="7days">7 Days</sl-menu-item>
            <sl-menu-item value="30days">30 Days</sl-menu-item>
            <sl-menu-item value="90days">90 Days</sl-menu-item>
          </sl-menu>
        </sl-dropdown>
        <sl-button
          slot="footer"
          @click=${() => {
            this.isCreateModalOpen = false;
            this.createError = null;
          }}
          >Cancel</sl-button
        >
        <sl-button
          slot="footer"
          variant="primary"
          @click=${this.handleCreateApiKey}
          .disabled=${!this.newKeyName}
          >Create</sl-button
        >
      </sl-dialog>

      <sl-dialog
        label="API key created"
        .open=${this.isShowKeyModalOpen && this.newlyCreatedKey}
        @sl-request-close=${this._guardKeyDialogClose}
        @sl-hide=${(e: Event) => {
          if (e.target === e.currentTarget) this._closeKeyDialog();
        }}
      >
        <div class="key-row">
          <sl-input
            class="key-field"
            label="API key"
            readonly
            .value=${this.newlyCreatedKey?.key ?? ''}
            @sl-focus=${(e: Event) =>
              (e.target as HTMLElement & { select: () => void }).select()}
          ></sl-input>
          <sl-button class="copy-key" size="medium" @click=${this._copyKey}>
            <sl-icon
              slot="prefix"
              name=${this.keyCopyStatus === 'copied' ? 'check' : 'clipboard'}
            ></sl-icon>
            ${this.keyCopyStatus === 'copied' ? 'Copied' : 'Copy key'}
          </sl-button>
        </div>
        <div class="copy-status" role="status" aria-live="polite">
          ${
            this.keyCopyStatus === 'copied'
              ? 'API key copied to clipboard.'
              : this.keyCopyStatus === 'manual'
                ? "Couldn't copy automatically. The key is selected: press Ctrl+C (Cmd+C on Mac)."
                : nothing
          }
        </div>
        ${
          this.keyCopyStatus === 'idle' && !this._clipboardAvailable()
            ? html`<p class="copy-hint">
                Copying isn't available on this connection. Click the key and
                press Ctrl+C (Cmd+C on Mac).
              </p>`
            : nothing
        }
        <p class="usage-hint">
          Send it in the <code>Authorization: Bearer &lt;key&gt;</code> header.
        </p>
        <div class="warning-text">
          <sl-icon name="exclamation-triangle"></sl-icon>
          <span>Please copy it now. You will not be able to see it again.</span>
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          autofocus
          @click=${this._closeKeyDialog}
          >I have copied my key</sl-button
        >
      </sl-dialog>
    `;
  }

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      .loading-indicator {
        display: flex;
        justify-content: center;
        align-items: center;
        height: 200px;
      }
      .form-label {
        font-size: var(--sl-input-label-font-size-medium);
        display: inline-block;
        color: var(--sl-input-label-color);
        margin-bottom: var(--sl-spacing-3x-small);
      }
      .expiry-dropdown {
        display: block;
        margin-bottom: 1rem;
      }
      .expiry-dropdown::part(trigger) {
        width: 100%;
      }
      .expiry-dropdown sl-button {
        width: 100%;
        text-align: left;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      /* The bulk bar's containing block. It wraps the table rather than
         being the table head: WebKit does not make a positioned table section
         a containing block, so a bar anchored to the head escaped to the
         viewport in Safari. The wrapper is a plain block, which every engine
         positions against, and the bar still covers only the header row
         because it is anchored to the top and sized by its own content. */
      .table-shell {
        position: relative;
      }
      /* Eight columns do not fit a phone: the table scrolls sideways inside
         its card instead of pushing the page (and the Revoke action) off
         screen. */
      .table-scroll {
        overflow-x: auto;
      }
      .head-bulk-bar {
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        display: flex;
        align-items: center;
        /* The same padding the header cells carry, so the bar's content
           starts on the line the column labels start on. */
        padding: var(--sl-spacing-medium);
        background: var(--sl-panel-background-color);
        /* Visible inside a header the selection has hidden. */
        visibility: visible;
        transition: opacity 120ms ease-out;
      }
      .head-bulk-bar[data-hidden] {
        visibility: hidden;
        opacity: 0;
        pointer-events: none;
      }
      @media (prefers-reduced-motion: reduce) {
        .head-bulk-bar {
          transition: none;
        }
      }
      thead.selecting th {
        visibility: hidden;
      }
      th,
      td {
        padding: var(--sl-spacing-medium);
        text-align: left;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }
      th {
        background-color: var(--sl-color-neutral-50);
        font-weight: var(--sl-font-weight-semibold);
      }
      tr:last-child td {
        border-bottom: none;
      }
      td.actions-cell {
        text-align: right;
        width: 1%;
        white-space: nowrap;
      }
      /* The Actions header sits over a right-aligned column, so it is
         right-aligned too. */
      th.actions-cell {
        text-align: right;
      }
      .empty-row {
        color: var(--console-meta-color, var(--sl-color-neutral-600));
        font-size: var(--sl-font-size-small);
      }
      /* A hairline footer, not a card: the count of what is not on screen and
         the one control that reveals it. */
      .table-footnote {
        border-top: 1px solid
          var(--console-hairline, var(--sl-color-neutral-200));
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        color: var(--console-meta-color, var(--sl-color-neutral-600));
        font-size: var(--sl-font-size-small);
      }
      .link-button {
        background: none;
        border: none;
        padding: 0;
        font: inherit;
        color: var(--sl-color-primary-600);
        cursor: pointer;
      }
      .link-button:hover {
        text-decoration: underline;
      }
      .key-row {
        display: flex;
        align-items: flex-end;
        gap: var(--sl-spacing-x-small);
      }
      .key-field {
        flex: 1;
        min-width: 0;
      }
      .key-field::part(input) {
        font-family: var(--sl-font-mono);
      }
      .copy-status {
        margin-top: var(--sl-spacing-x-small);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
      }
      .copy-hint,
      .usage-hint {
        margin: var(--sl-spacing-x-small) 0 0;
        font-size: var(--sl-font-size-small);
        color: var(--console-meta-color, var(--sl-color-neutral-600));
      }
      .warning-text {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        color: var(--sl-color-neutral-600);
        margin-top: var(--sl-spacing-medium);
        font-size: var(--sl-font-size-small);
      }
    `,
  ];
}
