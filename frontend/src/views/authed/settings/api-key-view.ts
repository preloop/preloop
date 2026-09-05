import { LitElement, css, html, unsafeCSS, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/format-date/format-date.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';

import '../../../components/preloop-session-observer';
import '../../../components/budget-policy-editor';
import '../../../components/tools-editor-component';
import '../../../components/view-header';
import { confirmDialog, showToast } from '../../../components/confirm-dialog';

import {
  getApiKey,
  getApiKeyGovernance,
  deleteApiKey,
  updateApiKeyGovernance,
  getTools,
  getApprovalWorkflows,
  getFeatures,
  getMCPServers,
  getAIModels,
  getApiKeyGatewayUsageSummary,
} from '../../../api';
import type {
  SubjectGovernanceResponse,
  AIModel,
  ApiKeyGatewayUsageSummaryResponse,
} from '../../../types';

import consoleStyles from '../../../styles/console-styles.css?inline';

@customElement('api-key-view')
export class ApiKeyView extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        height: 100%;
        overflow-y: auto;
      }

      .container {
        padding: var(--sl-spacing-large);
        max-width: 1200px;
        margin: 0 auto;
      }

      .loading-container {
        display: flex;
        justify-content: center;
        align-items: center;
        height: 50vh;
      }

      .layout {
        display: grid;
        grid-template-columns: 1fr;
        gap: var(--sl-spacing-large);
      }

      @media (min-width: 1024px) {
        .layout {
          grid-template-columns: minmax(0, 2fr) minmax(300px, 1fr);
        }
      }

      .main-column {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .sidebar {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-large);
      }

      .details-card {
        margin-bottom: var(--sl-spacing-large);
      }

      .details-grid {
        display: grid;
        grid-template-columns: max-content 1fr;
        gap: var(--sl-spacing-medium) var(--sl-spacing-large);
        align-items: baseline;
      }

      .label {
        color: var(--sl-color-neutral-600);
        font-weight: var(--sl-font-weight-semibold);
      }

      .value {
        color: var(--sl-color-neutral-900);
        word-break: break-all;
      }

      .section-header {
        margin-top: 0;
        margin-bottom: var(--sl-spacing-medium);
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
      }

      .activity-table {
        width: 100%;
        border-collapse: collapse;
      }

      .activity-table th,
      .activity-table td {
        padding: var(--sl-spacing-small);
        text-align: left;
        border-bottom: 1px solid var(--sl-color-neutral-200);
      }

      .activity-table th {
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-600);
        background-color: var(--sl-color-neutral-50);
      }

      .empty-state {
        padding: var(--sl-spacing-large);
        text-align: center;
        color: var(--sl-color-neutral-500);
        font-style: italic;
      }

      sl-card::part(body) {
        padding: var(--sl-spacing-large);
      }

      .success-text {
        color: var(--sl-color-success-600);
      }

      .danger-text {
        color: var(--sl-color-danger-600);
      }
    `,
  ];

  @property({ type: Object }) location?: any;

  @state() private apiKey: any | null = null;
  @state() private governance: SubjectGovernanceResponse | null = null;
  @state() private usageSummary: ApiKeyGatewayUsageSummaryResponse | null =
    null;
  @state() private aiModels: AIModel[] = [];
  @state() private budgetTimeRange:
    'day' | 'week' | 'month' | 'year' | 'total' = 'total';
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private updatingGovernance = false;

  @state() private governanceAllowedModels = '';
  @state() private toolCatalog: any[] = [];
  @state() private mcpServers: any[] = [];
  @state() private approvalWorkflows: any[] = [];
  @state() private featureFlags: any = {};
  @state() private scopedToolRules: Record<string, any[]> = {};
  @state() private toolEnabledOverrides: Record<string, boolean> = {};

  get keyId(): string | undefined {
    return this.location?.params?.keyId as string | undefined;
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.keyId) {
      this.loadData();
    }
  }

  async loadData() {
    if (!this.keyId) return;

    this.loading = true;
    this.error = null;

    try {
      let startDate: string | undefined;
      const now = new Date();
      if (this.budgetTimeRange === 'day') {
        startDate = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString();
      } else if (this.budgetTimeRange === 'week') {
        startDate = new Date(
          now.getTime() - 7 * 24 * 60 * 60 * 1000
        ).toISOString();
      } else if (this.budgetTimeRange === 'month') {
        startDate = new Date(
          now.getTime() - 30 * 24 * 60 * 60 * 1000
        ).toISOString();
      } else if (this.budgetTimeRange === 'year') {
        startDate = new Date(
          now.getTime() - 365 * 24 * 60 * 60 * 1000
        ).toISOString();
      }

      const [
        keyData,
        governanceData,
        summaryData,
        tools,
        servers,
        workflows,
        features,
        modelsData,
      ] = await Promise.all([
        getApiKey(this.keyId),
        getApiKeyGovernance(this.keyId),
        getApiKeyGatewayUsageSummary(this.keyId, { startDate }).catch(
          () => null
        ),
        getTools().catch(() => []),
        getMCPServers().catch(() => []),
        getApprovalWorkflows().catch(() => []),
        getFeatures().catch(() => ({ features: {} })),
        getAIModels().catch(() => []),
      ]);

      this.apiKey = keyData;
      this.governance = governanceData;
      this.usageSummary = summaryData;
      this.aiModels = modelsData || [];

      this.toolCatalog = tools || [];
      this.mcpServers = servers || [];
      this.approvalWorkflows = workflows || [];
      this.featureFlags = features?.features || {};

      if (governanceData && governanceData.config) {
        this.governanceAllowedModels = (
          governanceData.config.allowed_models || []
        ).join(', ');
        this.scopedToolRules = governanceData.config.tool_rules || {};
        this.toolEnabledOverrides =
          governanceData.config.tool_enabled_overrides || {};
      }
    } catch (err: any) {
      console.error('Error loading API key data:', err);
      this.error = err.message || 'Failed to load API key details';
    } finally {
      this.loading = false;
    }
  }

  private async handleRevoke() {
    if (!this.keyId) return;

    const confirmed = await confirmDialog({
      title: 'Revoke API key',
      message: `Revoke "${this.apiKey?.name}"?`,
      detail:
        'Anything still authenticating with this key stops working immediately. This cannot be undone.',
      confirmLabel: 'Revoke key',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }

    try {
      await deleteApiKey(this.keyId);
      Router.go('/console/settings/api-keys');
    } catch (err: any) {
      console.error('Error revoking API key:', err);
      showToast(err.message || 'Failed to revoke API key', 'danger');
    }
  }

  private async handleGovernanceUpdate() {
    if (!this.keyId || !this.governance) return;

    this.updatingGovernance = true;
    try {
      const allowed_models = this.governanceAllowedModels
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);

      this.governance.config.allowed_models = allowed_models;
      this.governance.config.tool_rules = this.scopedToolRules;
      this.governance.config.tool_enabled_overrides = this.toolEnabledOverrides;

      this.governance = await updateApiKeyGovernance(
        this.keyId,
        this.governance.config
      );
    } catch (err: any) {
      console.error('Error updating governance:', err);
      alert(err.message || 'Failed to update governance policy');
    } finally {
      this.updatingGovernance = false;
    }
  }

  private handleAllowedModelToggle(modelName: string, checked: boolean) {
    if (!this.governance) return;

    let currentModels = this.governanceAllowedModels
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    if (checked && !currentModels.includes(modelName)) {
      currentModels.push(modelName);
    } else if (!checked && currentModels.includes(modelName)) {
      currentModels = currentModels.filter((m) => m !== modelName);
    }

    this.governanceAllowedModels = currentModels.join(', ');
    this.handleGovernanceUpdate();
  }

  private saveScopedToolRule(
    toolName: string,
    existingRule: any,
    formData: any
  ) {
    const rules = [...(this.scopedToolRules[toolName] || [])];
    if (existingRule) {
      const i = rules.findIndex((r) => r.id === existingRule.id);
      if (i >= 0) rules[i] = { ...existingRule, ...formData };
    } else {
      rules.push({
        id: 'rule_' + Math.random().toString(36).substring(2, 9),
        ...formData,
      });
    }
    this.scopedToolRules = { ...this.scopedToolRules, [toolName]: rules };
    this.handleGovernanceUpdate();
  }

  private deleteScopedToolRule(toolName: string, ruleId: string) {
    const rules = (this.scopedToolRules[toolName] || []).filter(
      (r) => r.id !== ruleId
    );
    this.scopedToolRules = { ...this.scopedToolRules, [toolName]: rules };
    this.handleGovernanceUpdate();
  }

  private reorderScopedToolRules(toolName: string, reorderedRules: any[]) {
    this.scopedToolRules = {
      ...this.scopedToolRules,
      [toolName]: reorderedRules,
    };
    this.handleGovernanceUpdate();
  }

  private toggleToolEnabledOverride(e: CustomEvent) {
    const { toolName, enabled } = e.detail;
    this.toolEnabledOverrides = {
      ...this.toolEnabledOverrides,
      [toolName]: enabled,
    };
    this.handleGovernanceUpdate();
  }

  private revertScopedTool(e: CustomEvent) {
    const { toolName } = e.detail;
    const rulesCopy = { ...this.scopedToolRules };
    delete rulesCopy[toolName];
    this.scopedToolRules = rulesCopy;

    const overridesCopy = { ...this.toolEnabledOverrides };
    delete overridesCopy[toolName];
    this.toolEnabledOverrides = overridesCopy;

    this.handleGovernanceUpdate();
  }

  render() {
    if (this.loading) {
      return html`
        <div class="loading-container">
          <sl-spinner style="font-size: 3rem;"></sl-spinner>
        </div>
      `;
    }

    if (this.error) {
      return html`
        <div class="container">
          <sl-alert variant="danger" open>
            <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
            <strong>Error loading API key</strong><br />
            ${this.error}
          </sl-alert>
          <div style="margin-top: var(--sl-spacing-medium)">
            <sl-button @click=${() => Router.go('/console/settings/api-keys')}>
              Back to API Keys
            </sl-button>
          </div>
        </div>
      `;
    }

    if (!this.apiKey) {
      return nothing;
    }

    return html`
      <div class="container">
        <!-- view-header takes headerText, and its slots are top / title-prefix
             / main-column / meta / description. Passing title, backUrl and
             backLabel rendered an empty band with no title, no way back and no
             Revoke button. -->
        <view-header headerText=${this.apiKey.name}>
          <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
            <sl-button
              variant="text"
              size="small"
              href="/console/settings/api-keys"
              style="margin-left: -12px;"
            >
              <sl-icon slot="prefix" name="arrow-left"></sl-icon>
              Back to API keys
            </sl-button>
          </div>
          <div slot="main-column">
            <sl-button variant="danger" outline @click=${this.handleRevoke}>
              <sl-icon slot="prefix" name="trash"></sl-icon>
              Revoke key
            </sl-button>
          </div>
        </view-header>

        <div class="layout">
          <div class="main-column">
            <sl-card class="details-card">
              <div
                style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-medium);"
              >
                <h2 class="section-header" style="margin: 0;">
                  Key Details & Spend
                </h2>
                <select
                  style="background: transparent; border: none; font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-600); cursor: pointer; outline: none;"
                  .value=${this.budgetTimeRange}
                  @change=${(e: Event) => {
                    this.budgetTimeRange = (e.target as HTMLSelectElement)
                      .value as any;
                    this.loadData();
                  }}
                >
                  <option value="day">24h</option>
                  <option value="week">7d</option>
                  <option value="month">30d</option>
                  <option value="year">1y</option>
                  <option value="total">All time</option>
                </select>
              </div>
              <div class="details-grid">
                <div class="label">ID</div>
                <div class="value"><code>${this.apiKey.id}</code></div>

                <div class="label">Status</div>
                <div class="value">
                  ${
                    this.apiKey.expires_at &&
                    new Date(this.apiKey.expires_at) < new Date()
                      ? html`<sl-badge class="chip" pill variant="neutral"
                          >Expired</sl-badge
                        >`
                      : html`<sl-badge class="chip" pill variant="success"
                          >Active</sl-badge
                        >`
                  }
                </div>

                <div class="label">Created</div>
                <div class="value">
                  <sl-format-date
                    date=${this.apiKey.created_at}
                    month="short"
                    day="numeric"
                    year="numeric"
                    hour="numeric"
                    minute="numeric"
                  ></sl-format-date>
                </div>

                <div class="label">Expires</div>
                <div class="value">
                  ${
                    this.apiKey.expires_at
                      ? html`<sl-format-date
                          date=${this.apiKey.expires_at}
                          month="short"
                          day="numeric"
                          year="numeric"
                        ></sl-format-date>`
                      : html`<i>Never</i>`
                  }
                </div>

                <div class="label">Last Used</div>
                <div class="value">
                  ${
                    this.apiKey.last_used_at
                      ? html`<sl-format-date
                          date=${this.apiKey.last_used_at}
                          month="short"
                          day="numeric"
                          year="numeric"
                          hour="numeric"
                          minute="numeric"
                        ></sl-format-date>`
                      : html`<i>Never</i>`
                  }
                </div>
                <div class="label">Total Spend (${this.budgetTimeRange})</div>
                <div class="value">
                  <span
                    style="font-size: 1.1em; font-weight: 600; color: var(--sl-color-primary-600);"
                  >
                    $${(this.usageSummary?.estimated_cost || 0).toFixed(6)}
                  </span>
                  <span
                    style="color: var(--sl-color-neutral-500); font-size: 0.9em; margin-left: 8px;"
                  >
                    (${this.usageSummary?.total_requests || 0} requests)
                  </span>
                </div>
              </div>
            </sl-card>

            <sl-card>
              <h2 class="section-header">Session Observer</h2>
              <preloop-session-observer
                scope="api_key"
                .scopeId=${this.keyId || ''}
                .sessions=${this.usageSummary?.usage_by_session || []}
                layout="embedded"
                defaultReplayMode="timeline"
                .features=${{
                  summaries: true,
                  optimization: this.featureFlags.session_optimization === true,
                  auditLinks: true,
                  liveFollow: true,
                }}
              ></preloop-session-observer>
            </sl-card>
          </div>

          <div class="sidebar">
            <sl-card>
              <h2 class="section-header">Allowed Models & Spend</h2>
              <div
                style="display: flex; flex-direction: column; gap: var(--sl-spacing-small); max-height: 300px; overflow-y: auto;"
              >
                ${this.aiModels.map((model) => {
                  const allowed = this.governanceAllowedModels
                    .split(',')
                    .map((s) => s.trim())
                    .filter(Boolean);
                  const isAllowed =
                    allowed.length === 0 || allowed.includes(model.name);
                  const modelUsage = this.usageSummary?.usage_by_model?.find(
                    (u) =>
                      u.model_alias === model.name || u.ai_model_id === model.id
                  );
                  return html`
                    <div
                      style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--sl-color-neutral-100); padding-bottom: 4px;"
                    >
                      <sl-checkbox
                        ?checked=${isAllowed}
                        @sl-change=${(e: Event) =>
                          this.handleAllowedModelToggle(
                            model.name,
                            (e.target as HTMLInputElement).checked
                          )}
                      >
                        ${model.name}
                      </sl-checkbox>
                      ${
                        modelUsage
                          ? html`
                              <div
                                style="font-size: 0.85rem; color: var(--sl-color-neutral-600);"
                              >
                                <span
                                  style="color: var(--sl-color-primary-600); font-weight: 500;"
                                  >$${(modelUsage.estimated_cost || 0).toFixed(
                                    4
                                  )}</span
                                >
                              </div>
                            `
                          : ''
                      }
                    </div>
                  `;
                })}
              </div>
              <div
                style="margin-top: var(--sl-spacing-medium); padding-top: var(--sl-spacing-medium); border-top: 1px solid var(--sl-color-neutral-200);"
              >
                <sl-input
                  label="Manual override"
                  placeholder="preloop/google/gemini-3.1-pro-preview, ..."
                  .value=${this.governanceAllowedModels}
                  @sl-change=${(e: Event) => {
                    this.governanceAllowedModels = (
                      e.target as HTMLInputElement
                    ).value;
                    this.handleGovernanceUpdate();
                  }}
                ></sl-input>
                <div
                  style="font-size: 0.8rem; color: var(--sl-color-neutral-500); margin-top: 4px;"
                >
                  Comma separated list. Leave empty to allow all.
                </div>
              </div>
            </sl-card>

            <sl-card>
              <h2 class="section-header">Budget Policy</h2>
              <budget-policy-editor
                subjectType="api_key"
                .subjectId=${this.keyId}
              ></budget-policy-editor>
            </sl-card>

            <sl-card>
              <h2 class="section-header">Tool Policies</h2>
              <tools-editor-component
                mode="scoped"
                ?collapseByDefault=${true}
                .tools=${this.toolCatalog}
                .mcpServers=${this.mcpServers}
                .scopedToolRules=${this.scopedToolRules}
                .toolEnabledOverrides=${this.toolEnabledOverrides}
                .approvalPolicies=${this.approvalWorkflows}
                .features=${this.featureFlags}
                @save-rule=${(e: CustomEvent) =>
                  this.saveScopedToolRule(
                    e.detail.tool.name,
                    e.detail.existingRule || e.detail.rule,
                    e.detail.formData
                  )}
                @delete-rule=${(e: CustomEvent) =>
                  this.deleteScopedToolRule(
                    e.detail.tool.name,
                    e.detail.rule.id
                  )}
                @reorder-rules=${(e: CustomEvent) =>
                  this.reorderScopedToolRules(
                    e.detail.tool.name,
                    e.detail.reorderedRules
                  )}
                @toggle-enabled=${this.toggleToolEnabledOverride}
                @revert-tool=${this.revertScopedTool}
                @policy-created=${() => this.loadData()}
              ></tools-editor-component>
            </sl-card>
          </div>
        </div>
      </div>
    `;
  }
}
