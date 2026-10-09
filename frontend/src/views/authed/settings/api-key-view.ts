import { LitElement, css, html, unsafeCSS, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '../../../router';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/format-date/format-date.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';

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
import { parseUTCDate } from '../../../utils/date';
import { formatUsd, formatUsdExact } from '../../../utils/money';
import {
  allowlistEntryMatchesModel,
  gatewayAliasForModel,
} from '../../../utils/model-allowlist';

type SpendRange = 'day' | 'week' | 'month' | 'year' | 'total';

const SPEND_RANGE_LABELS: Record<SpendRange, string> = {
  day: 'last 24 hours',
  week: 'last 7 days',
  month: 'last 30 days',
  year: 'last year',
  total: 'all time',
};

function splitAllowedModels(value: string): string[] {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

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

      /* No page geometry here: the shell owns the width and the side inset
         (styles/console-styles.css, "The page box"), so .container keeps the
         shared flex column and adds nothing. */

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

      .range-select {
        background: transparent;
        border: none;
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        cursor: pointer;
      }

      .range-select:focus-visible {
        outline: var(--sl-focus-ring);
        outline-offset: var(--sl-focus-ring-offset);
      }

      .allowlist-note {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        margin: var(--sl-spacing-x-small) 0 var(--sl-spacing-small);
      }

      .allowlist-warning {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-warning-700);
        margin: 0 0 var(--sl-spacing-small);
      }

      .retired-alert {
        margin-bottom: var(--sl-spacing-large);
      }
    `,
  ];

  @property({ type: Object }) location?: any;

  @state() private apiKey: any | null = null;
  @state() private governance: SubjectGovernanceResponse | null = null;
  @state() private usageSummary: ApiKeyGatewayUsageSummaryResponse | null =
    null;
  @state() private aiModels: AIModel[] = [];
  @state() private budgetTimeRange: SpendRange = 'total';
  @state() private loading = true;
  @state() private error: string | null = null;
  @state() private updatingGovernance = false;

  @state() private governanceAllowedModels = '';
  /**
   * The operator switched "Restrict to selected models" on but has not
   * picked a model yet. Nothing is saved until they do, because an empty
   * allowlist means every model is allowed.
   */
  @state() private restrictDraft = false;
  /** Why the last allowlist change was refused, shown under the list. */
  @state() private allowlistNotice = '';
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
      showToast(err.message || 'Failed to update governance policy', 'danger');
    } finally {
      this.updatingGovernance = false;
    }
  }

  private isRevoked(): boolean {
    return this.apiKey?.activity_status === 'revoked';
  }

  private isExpired(): boolean {
    const expiresAt = this.apiKey?.expires_at;
    return (
      Boolean(expiresAt) && parseUTCDate(expiresAt).getTime() <= Date.now()
    );
  }

  /** A key that can no longer sign a request: there is nothing to revoke. */
  private isRetired(): boolean {
    return this.isRevoked() || this.isExpired();
  }

  /**
   * Whether an allowlist entry names this model, by the gateway's own rules
   * (see utils/model-allowlist.ts): display name or id, the configured
   * gateway alias, provider/identifier, the bare identifier, or the bare tail
   * of an alias. A checkbox reads as checked exactly when the gateway would
   * let this key call the model.
   */
  private entryMatchesModel(entry: string, model: AIModel): boolean {
    return allowlistEntryMatchesModel(entry, model);
  }

  private allowedEntries(): string[] {
    return splitAllowedModels(this.governanceAllowedModels);
  }

  private isRestricted(): boolean {
    return this.allowedEntries().length > 0 || this.restrictDraft;
  }

  private async confirmAllowAll(): Promise<boolean> {
    return confirmDialog({
      title: 'Allow every model?',
      message: `"${this.apiKey?.name}" will be able to call any model on this account.`,
      detail:
        'An empty allowlist means no model restriction. To block a model instead, keep at least one other model selected.',
      confirmLabel: 'Allow every model',
      variant: 'danger',
    });
  }

  private async handleRestrictToggle(event: Event) {
    const target = event.target as HTMLInputElement;
    this.allowlistNotice = '';
    if (target.checked) {
      this.restrictDraft = true;
      return;
    }
    if (this.allowedEntries().length === 0) {
      this.restrictDraft = false;
      return;
    }
    if (!(await this.confirmAllowAll())) {
      target.checked = true;
      return;
    }
    this.restrictDraft = false;
    this.governanceAllowedModels = '';
    await this.handleGovernanceUpdate();
  }

  private handleAllowedModelToggle(model: AIModel, event: Event) {
    if (!this.governance) return;
    const target = event.target as HTMLInputElement;
    const checked = target.checked;
    const current = this.allowedEntries();
    this.allowlistNotice = '';

    let next: string[];
    if (checked) {
      next = current.some((entry) => this.entryMatchesModel(entry, model))
        ? current
        : [...current, gatewayAliasForModel(model)];
    } else {
      // Remove every stored entry that names this model, in whatever form
      // it was stored, so the gateway stops honouring it.
      next = current.filter((entry) => !this.entryMatchesModel(entry, model));
      // A removed entry may also have named other models (a bare
      // identifier shared by two providers). Keep those allowed under
      // their own alias rather than narrowing them as a side effect.
      for (const other of this.aiModels) {
        if (
          other === model ||
          this.entryMatchesModel(gatewayAliasForModel(other), model)
        ) {
          continue;
        }
        const wasAllowed = current.some((e) =>
          this.entryMatchesModel(e, other)
        );
        const stillAllowed = next.some((e) => this.entryMatchesModel(e, other));
        if (wasAllowed && !stillAllowed) next.push(gatewayAliasForModel(other));
      }
      if (next.length === 0) {
        // Saving [] would silently widen the key to every model.
        target.checked = true;
        this.allowlistNotice =
          'Keep at least one model selected. To allow every model, turn off "Restrict to selected models".';
        return;
      }
    }

    this.restrictDraft = false;
    this.governanceAllowedModels = next.join(', ');
    void this.handleGovernanceUpdate();
  }

  private async handleManualAllowlistChange(event: Event) {
    const target = event.target as HTMLInputElement;
    const value = target.value;
    this.allowlistNotice = '';
    if (
      splitAllowedModels(value).length === 0 &&
      this.allowedEntries().length > 0
    ) {
      if (!(await this.confirmAllowAll())) {
        target.value = this.governanceAllowedModels;
        return;
      }
      this.restrictDraft = false;
    }
    this.governanceAllowedModels = value;
    await this.handleGovernanceUpdate();
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

  private renderAllowlist() {
    const entries = this.allowedEntries();
    const restricted = this.isRestricted();
    return html`
      <sl-switch
        class="restrict-switch"
        .checked=${restricted}
        ?disabled=${this.updatingGovernance}
        @sl-change=${this.handleRestrictToggle}
        >Restrict to selected models</sl-switch
      >
      <p class="allowlist-note" aria-live="polite">
        ${
          !restricted
            ? 'This key can call any model on the account.'
            : entries.length === 0
              ? 'Select the models this key may call. Until you select one, it can still call any model.'
              : `This key can call ${entries.length} selected model${entries.length === 1 ? '' : 's'}.`
        }
        ${this.updatingGovernance ? ' Saving…' : nothing}
      </p>
      ${
        this.allowlistNotice
          ? html`<p class="allowlist-warning" role="alert">
              ${this.allowlistNotice}
            </p>`
          : nothing
      }
      <div
        style="display: flex; flex-direction: column; gap: var(--sl-spacing-small); max-height: 300px; overflow-y: auto;"
      >
        ${this.aiModels.map((model) => {
          const isAllowed =
            !restricted ||
            entries.some((entry) => this.entryMatchesModel(entry, model));
          const modelUsage = this.usageSummary?.usage_by_model?.find(
            (u) => u.model_alias === model.name || u.ai_model_id === model.id
          );
          return html`
            <div
              style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--sl-color-neutral-100); padding-bottom: 4px;"
            >
              <sl-checkbox
                .checked=${isAllowed}
                ?disabled=${!restricted}
                @sl-change=${(e: Event) =>
                  this.handleAllowedModelToggle(model, e)}
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
                          title=${formatUsdExact(modelUsage.estimated_cost)}
                          >${formatUsd(modelUsage.estimated_cost)}</span
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
          @sl-change=${this.handleManualAllowlistChange}
        ></sl-input>
        <div
          style="font-size: 0.8rem; color: var(--sl-color-neutral-500); margin-top: 4px;"
        >
          Comma-separated model names, ids or gateway aliases. Empty allows
          every model.
        </div>
      </div>
    `;
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
              Back to API keys
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
          ${
            this.isRetired()
              ? nothing
              : html`<div slot="main-column">
                  <sl-button
                    variant="danger"
                    outline
                    @click=${this.handleRevoke}
                  >
                    <sl-icon slot="prefix" name="trash"></sl-icon>
                    Revoke key
                  </sl-button>
                </div>`
          }
        </view-header>

        ${
          this.isRevoked()
            ? html`<sl-alert class="retired-alert" variant="neutral" open>
                <sl-icon slot="icon" name="slash-circle"></sl-icon>
                This key was revoked. Requests signed with it are rejected.
              </sl-alert>`
            : this.isExpired()
              ? html`<sl-alert class="retired-alert" variant="neutral" open>
                  <sl-icon slot="icon" name="clock-history"></sl-icon>
                  This key has expired. Requests signed with it are rejected.
                </sl-alert>`
              : nothing
        }

        <div class="layout">
          <div class="main-column">
            <sl-card class="details-card">
              <div
                style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sl-spacing-medium);"
              >
                <h2 class="section-header" style="margin: 0;">
                  Key details and spend
                </h2>
                <select
                  class="range-select"
                  aria-label="Spend time range"
                  .value=${this.budgetTimeRange}
                  @change=${(e: Event) => {
                    this.budgetTimeRange = (e.target as HTMLSelectElement)
                      .value as SpendRange;
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
                    this.isRevoked()
                      ? html`<sl-badge class="chip" pill variant="danger"
                          >Revoked</sl-badge
                        >`
                      : this.isExpired()
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

                <div class="label">Last used</div>
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
                <div class="label">
                  Spend (${SPEND_RANGE_LABELS[this.budgetTimeRange]})
                </div>
                <div class="value">
                  <span
                    class="spend-total"
                    style="font-size: 1.1em; font-weight: 600; color: var(--sl-color-primary-600);"
                    title=${formatUsdExact(this.usageSummary?.estimated_cost)}
                  >
                    ${formatUsd(this.usageSummary?.estimated_cost)}
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
              <h2 class="section-header">Session observer</h2>
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
              <h2 class="section-header">Allowed models and spend</h2>
              ${this.renderAllowlist()}
            </sl-card>

            <sl-card>
              <h2 class="section-header">Budget policy</h2>
              <budget-policy-editor
                subjectType="api_key"
                .subjectId=${this.keyId}
              ></budget-policy-editor>
            </sl-card>

            <sl-card>
              <h2 class="section-header">Tool policies</h2>
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
