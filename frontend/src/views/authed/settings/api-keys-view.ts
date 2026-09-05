import { LitElement, html, css, unsafeCSS } from 'lit';
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

  async connectedCallback() {
    super.connectedCallback();
    await Promise.all([
      this.fetchApiKeys(),
      this.fetchGovernanceEditorContext(),
    ]);
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

  private async openGovernanceDialog(key: ApiKey): Promise<void> {
    this.governanceError = null;
    this.governanceKeyId = key.id;
    this.governanceKeyName = key.name;
    try {
      const response = await getApiKeyGovernance(key.id);
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

  private _copyKey(e: Event) {
    const button = e.currentTarget as HTMLElement;
    const pre = button.previousElementSibling;
    if (pre && pre.tagName === 'PRE') {
      const code = pre.querySelector('code');
      if (code) {
        navigator.clipboard.writeText(code.innerText).then(() => {
          const originalHTML = button.innerHTML;
          button.innerHTML =
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-check" viewBox="0 0 16 16"><path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 1 1 1.06-1.06l2.094 2.093 3.473-4.425a.267.267 0 0 1 .02-.022z"/></svg>';
          setTimeout(() => {
            button.innerHTML = originalHTML;
          }, 2000);
        });
      }
    }
  }

  private _handleExpirySelect(e: CustomEvent) {
    const item = e.detail.item as SlMenuItem;
    this.newKeyExpiry = item.value;
    this.newKeyExpiryLabel = item.textContent?.trim() ?? 'Never';
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
              >Add an API Key</a
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
          <table class="styled-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Created</th>
                <th>Last activity</th>
                <th>Recent usage</th>
                <th>Expires</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${
                visibleKeys.length === 0
                  ? html`<tr>
                      <td colspan="7" class="empty-row">No active keys.</td>
                    </tr>`
                  : ''
              }
              ${repeat(
                visibleKeys,
                (key) => key.id,
                (key) => html`
                  <tr>
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
                          ? parseUTCDate(key.expires_at).toLocaleDateString()
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
                                    this.handleDeleteApiKey(key.id, key.name),
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
        headerText="API Keys"
        description='Credentials for the gateway and MCP endpoints. Keys labeled "Managed Agent" were minted automatically when an agent was onboarded, and "Flow Execution" keys are minted for a single flow run and revoked when it ends.'
        width="narrow"
      >
        <div slot="main-column">
          <sl-button
            variant="primary"
            @click=${() => {
              this.isCreateModalOpen = true;
            }}
            >Create New API Key</sl-button
          >
        </div>
      </view-header>
      <div class="column-layout narrow">
        <div class="main-column">${renderContent()}</div>
        <div class="side-column"></div>
      </div>

      <sl-dialog label="Create API Key" .open=${this.isCreateModalOpen}>
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
          label="Key Name"
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
        label="API Key Created"
        .open=${this.isShowKeyModalOpen && this.newlyCreatedKey}
        @sl-hide=${() => (this.isShowKeyModalOpen = false)}
      >
        <p>Here is your new API key:</p>
        <div class="code-container">
          <pre><code>${this.newlyCreatedKey?.key}</code></pre>
          <button class="copy-btn" @click=${this._copyKey}>
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="16"
              height="16"
              fill="currentColor"
              class="bi bi-clipboard"
              viewBox="0 0 16 16"
            >
              <path
                d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1v-1z"
              />
              <path
                d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0h-3z"
              />
            </svg>
          </button>
        </div>
        <div class="warning-text">
          <sl-icon name="exclamation-triangle"></sl-icon>
          <span>Please copy it now. You will not be able to see it again.</span>
        </div>
        <sl-button
          slot="footer"
          variant="primary"
          autofocus
          @click=${() => (this.isShowKeyModalOpen = false)}
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
      .code-container {
        position: relative;
        background-color: var(--sl-color-neutral-100);
        border-radius: var(--sl-border-radius-medium);
        margin: 1rem 0;
      }
      .code-container pre {
        margin: 0;
        padding: var(--sl-spacing-medium);
        white-space: pre-wrap;
        word-break: break-all;
      }
      .copy-btn {
        position: absolute;
        top: var(--sl-spacing-x-small);
        right: var(--sl-spacing-x-small);
        background: none;
        border: none;
        color: var(--sl-color-neutral-600);
        cursor: pointer;
        padding: var(--sl-spacing-2x-small);
        border-radius: var(--sl-border-radius-circle);
      }
      .copy-btn:hover {
        background-color: var(--sl-color-neutral-200);
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
