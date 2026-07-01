import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  BudgetPolicy,
  BudgetPolicyCreate,
  getBudgetPolicies,
  createBudgetPolicy,
  updateBudgetPolicy,
  deleteBudgetPolicy,
  getAIModels,
  getAccountAgents,
  getTeams,
  AIModel,
  ManagedAgentSummary,
  fetchWithAuth,
} from '../api.js';
import type { User, UserListResponse, Team } from '../types.js';
import './notify-recipients-field.ts';
import type { NotifyRecipientsValue } from './notify-recipients-field.ts';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

@customElement('budget-policy-editor')
export class BudgetPolicyEditor extends LitElement {
  @property({ type: String }) subjectType?: string;
  @property({ type: String }) subjectId?: string;
  /** Skip the features round-trip when the parent already knows billing is on. */
  @property({ type: Boolean }) billingEnabled = false;

  @state() private policies: BudgetPolicy[] = [];
  @state() private showAddForm = false;
  @state() private editingPolicyId: string | null = null;
  @state() private error = '';

  @state() private models: AIModel[] = [];
  @state() private agents: ManagedAgentSummary[] = [];
  @state() private teams: Team[] = [];
  @state() private loadingSubjects = false;
  @state() private loadingFeatures = false;
  @state() private loadingPolicies = false;
  @state() private saving = false;
  @state() private subjectsLoaded = false;
  @state() private features: Record<string, boolean> = {};
  @state() private availableUsers: User[] = [];

  // New Policy Form State
  @state() private newSubjectType = 'global';
  @state() private newSubjectId = 'global';
  @state() private newPeriod = 'monthly';
  @state() private newHardLimit = '';
  @state() private newSoftLimit = '';
  @state() private newNotifySoft = false;
  @state() private newNotifyHard = false;
  @state() private newNotifyUserIds: string[] = [];
  @state() private newNotifyTeamIds: string[] = [];
  @state() private newCustomEmails: string[] = [];

  static styles = css`
    :host {
      display: block;
      font-family: var(--sl-font-sans);
    }
    .policy-list {
      margin-top: var(--sl-spacing-medium);
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
    }
    .policy-item {
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--sl-panel-background-color);
      color: var(--sl-color-neutral-900);
    }
    .form-container {
      margin-top: var(--sl-spacing-medium);
      padding: var(--sl-spacing-medium);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-panel-background-color);
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }
    .form-row {
      display: flex;
      gap: var(--sl-spacing-medium);
    }
    .form-row > * {
      flex: 1;
    }
    .form-actions {
      display: flex;
      justify-content: flex-end;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-small);
    }
    .checkbox-group {
      display: flex;
      gap: var(--sl-spacing-medium);
      align-items: center;
    }
    .policy-actions {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
    }
    .readonly-field {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-3x-small);
      flex: 1;
    }
    .readonly-label {
      font-size: var(--sl-font-size-small);
      font-weight: var(--sl-font-weight-semibold);
      color: var(--sl-color-neutral-700);
    }
    .readonly-value {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-900);
    }
    .meta-text {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-500);
      margin-top: var(--sl-spacing-3x-small);
    }
    .loading-state {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: var(--sl-spacing-small);
      padding: var(--sl-spacing-large);
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
  `;

  async connectedCallback() {
    super.connectedCallback();
    this.loadingPolicies = true;
    try {
      if (this.billingEnabled) {
        this.features = { billing: true };
      } else {
        this.loadingFeatures = true;
        const featuresRes = await fetchWithAuth('/api/v1/features')
          .then((r) => (r.ok ? r.json() : null))
          .catch(() => null);
        this.features = featuresRes?.features || {};
        this.loadingFeatures = false;
      }
      if (this.features.billing !== true) {
        return;
      }
      await this.loadPolicies();
      if (this.needsSubjectMetadata()) {
        void this.loadSubjects();
      }
    } finally {
      this.loadingPolicies = false;
    }
  }

  private needsSubjectMetadata(): boolean {
    if (this.subjectType) {
      return false;
    }
    if (this.showAddForm) {
      return true;
    }
    return this.policies.some(
      (policy) => policy.subject_type !== 'global' && policy.subject_id
    );
  }

  private async ensureSubjectsLoaded(): Promise<void> {
    if (this.subjectType || this.subjectsLoaded || this.loadingSubjects) {
      return;
    }
    await this.loadSubjects();
  }

  private dispatchPoliciesChanged(): void {
    this.dispatchEvent(
      new CustomEvent('budget-policies-changed', {
        bubbles: true,
        composed: true,
        detail: { policies: [...this.policies] },
      })
    );
  }

  private getSubjectName(type: string, id: string): string {
    if (type === 'ai_model') {
      const model = this.models.find((m) => m.id === id);
      return model ? model.alias || model.id : id;
    }
    if (type === 'managed_agent') {
      const agent = this.agents.find((a) => a.id === id);
      return agent ? agent.display_name || agent.id : id;
    }
    return id;
  }

  async loadSubjects() {
    this.loadingSubjects = true;
    this.error = '';
    try {
      const [models, agentsResponse, userProfile, usersRes, teamsResponse] =
        await Promise.all([
          getAIModels(),
          getAccountAgents({ status: 'all', limit: 100 }),
          fetchWithAuth('/api/v1/auth/users/me').then((r) =>
            r.ok ? r.json() : null
          ),
          fetchWithAuth('/api/v1/users?limit=100').then((r) =>
            r.ok
              ? r.json()
              : ({
                  users: [],
                  total: 0,
                  skip: 0,
                  limit: 100,
                } satisfies UserListResponse)
          ),
          getTeams().catch(() => ({ teams: [] as Team[], total: 0 })),
        ]);
      this.models = models;
      this.agents = agentsResponse.items || [];
      this.availableUsers = (usersRes as UserListResponse).users || [];
      this.teams = teamsResponse.teams || [];
      if (
        userProfile &&
        this.newNotifyUserIds.length === 0 &&
        this.newNotifyTeamIds.length === 0 &&
        this.newCustomEmails.length === 0
      ) {
        this.newNotifyUserIds = [userProfile.id];
      }
      this.subjectsLoaded = true;
    } catch (e) {
      console.error('Failed to load subjects', e);
      this.error =
        'Failed to load budget policy subjects. Some selectors may be empty.';
      this.models = [];
      this.agents = [];
      this.availableUsers = [];
      this.teams = [];
    } finally {
      this.loadingSubjects = false;
    }
  }

  async loadPolicies() {
    try {
      const result = await getBudgetPolicies(this.subjectType, this.subjectId);
      if (Array.isArray(result)) {
        this.policies = result;
      } else {
        this.error =
          'Failed to load budget policies. Server returned an error.';
        this.policies = [];
        console.error('Non-array response:', result);
      }
    } catch (e: any) {
      this.error = 'Failed to load budget policies.';
      this.policies = [];
      console.error(e);
    }
  }

  private async handleDelete(id: string) {
    this.saving = true;
    this.error = '';
    try {
      await deleteBudgetPolicy(id);
      if (this.editingPolicyId === id) {
        this.cancelForm();
      }
      this.policies = this.policies.filter((policy) => policy.id !== id);
      this.dispatchPoliciesChanged();
    } catch (e: any) {
      this.error = 'Failed to delete policy.';
    } finally {
      this.saving = false;
    }
  }

  private startEdit(policy: BudgetPolicy) {
    this.showAddForm = false;
    this.editingPolicyId = policy.id;
    this.error = '';
    this.newSubjectType = policy.subject_type;
    this.newSubjectId = policy.subject_id || 'global';
    this.newPeriod = policy.period;
    this.newHardLimit =
      policy.hard_limit_usd != null ? String(policy.hard_limit_usd) : '';
    this.newSoftLimit =
      policy.soft_limit_usd != null ? String(policy.soft_limit_usd) : '';
    this.newNotifySoft = policy.notify_on_soft;
    this.newNotifyHard = policy.notify_on_hard;
    this.newNotifyUserIds = [...(policy.notification_user_ids || [])];
    this.newNotifyTeamIds = [...(policy.notification_team_ids || [])];
    this.newCustomEmails = [...(policy.notification_emails || [])];
  }

  private cancelForm() {
    this.showAddForm = false;
    this.editingPolicyId = null;
    this.resetForm();
    this.error = '';
  }

  private buildNotifyPayload() {
    return {
      notification_user_ids:
        this.newNotifyUserIds.length > 0 ? this.newNotifyUserIds : null,
      notification_team_ids:
        this.newNotifyTeamIds.length > 0 ? this.newNotifyTeamIds : null,
      notification_emails:
        this.newCustomEmails.length > 0 ? this.newCustomEmails : null,
    };
  }

  private handleNotifyRecipientsChange(
    event: CustomEvent<NotifyRecipientsValue>
  ) {
    this.newNotifyUserIds = event.detail.userIds;
    this.newNotifyTeamIds = event.detail.teamIds;
    this.newCustomEmails = event.detail.customEmails;
  }

  private async handleSave() {
    this.error = '';
    this.saving = true;
    const notifyPayload = this.buildNotifyPayload();
    const hardLimit = this.newHardLimit ? parseFloat(this.newHardLimit) : null;
    const softLimit = this.newSoftLimit ? parseFloat(this.newSoftLimit) : null;

    try {
      if (this.editingPolicyId) {
        const updated = await updateBudgetPolicy(this.editingPolicyId, {
          hard_limit_usd: hardLimit,
          soft_limit_usd: softLimit,
          notify_on_soft: this.newNotifySoft,
          notify_on_hard: this.newNotifyHard,
          ...notifyPayload,
        });
        this.policies = this.policies.map((policy) =>
          policy.id === updated.id ? updated : policy
        );
        this.cancelForm();
        this.dispatchPoliciesChanged();
        return;
      }

      const payload: BudgetPolicyCreate = {
        subject_type: this.subjectType || this.newSubjectType,
        subject_id: this.subjectType
          ? this.subjectId || 'global'
          : this.newSubjectId,
        model_alias: null,
        period: this.newPeriod,
        hard_limit_usd: hardLimit,
        soft_limit_usd: softLimit,
        notify_on_soft: this.newNotifySoft,
        notify_on_hard: this.newNotifyHard,
        ...notifyPayload,
      };

      const created = await createBudgetPolicy(payload);
      this.policies = [...this.policies, created];
      this.cancelForm();
      this.dispatchPoliciesChanged();
    } catch (err: any) {
      this.error =
        'Failed to save policy. ' + (err instanceof Error ? err.message : '');
    } finally {
      this.saving = false;
    }
  }

  private get isFormOpen(): boolean {
    return this.showAddForm || this.editingPolicyId !== null;
  }

  private get editingPolicy(): BudgetPolicy | undefined {
    return this.policies.find((policy) => policy.id === this.editingPolicyId);
  }

  private renderScopeSummary(policy?: BudgetPolicy) {
    const subjectType = policy?.subject_type || this.newSubjectType;
    const subjectId = policy?.subject_id || this.newSubjectId;
    const scopeLabel =
      subjectType === 'global'
        ? 'Global (Account-wide)'
        : subjectType === 'ai_model'
          ? 'AI Model'
          : subjectType === 'managed_agent'
            ? 'Agent'
            : subjectType;
    return html`
      <div class="form-row">
        <div class="readonly-field">
          <span class="readonly-label">Target Scope</span>
          <span class="readonly-value">${scopeLabel}</span>
        </div>
        ${
          subjectType !== 'global' && subjectId
            ? html`<div class="readonly-field">
                <span class="readonly-label">Subject</span>
                <span class="readonly-value"
                  >${this.getSubjectName(subjectType, subjectId)}</span
                >
              </div>`
            : html`<div style="flex: 1"></div>`
        }
      </div>
    `;
  }

  private resetForm() {
    this.newSubjectType = 'global';
    this.newSubjectId = 'global';
    this.newPeriod = 'monthly';
    this.newHardLimit = '';
    this.newSoftLimit = '';
    this.newNotifySoft = false;
    this.newNotifyHard = false;
    this.newNotifyUserIds = [];
    this.newNotifyTeamIds = [];
    this.newCustomEmails = [];
  }

  private formatNotifySummary(policy: BudgetPolicy): string {
    const parts: string[] = [];
    const userCount = policy.notification_user_ids?.length || 0;
    const teamCount = policy.notification_team_ids?.length || 0;
    const emailCount = policy.notification_emails?.length || 0;
    if (userCount > 0) {
      parts.push(`${userCount} user${userCount === 1 ? '' : 's'}`);
    }
    if (teamCount > 0) {
      parts.push(`${teamCount} team${teamCount === 1 ? '' : 's'}`);
    }
    if (emailCount > 0) {
      parts.push(`${emailCount} email${emailCount === 1 ? '' : 's'}`);
    }
    return parts.length > 0 ? parts.join(', ') : 'None';
  }

  render() {
    if (this.loadingFeatures) {
      return html`
        <div class="loading-state" role="status" aria-live="polite">
          <sl-spinner></sl-spinner>
          Loading budget settings…
        </div>
      `;
    }

    if (this.features.billing !== true) {
      return nothing;
    }

    return html`
      <div role="region" aria-labelledby="budget-policy-editor-title">
        <div
          style="display: flex; justify-content: space-between; align-items: center;"
        >
          <h4
            id="budget-policy-editor-title"
            style="margin: 0; font-size: var(--sl-font-size-large); font-weight: var(--sl-font-weight-semibold); color: var(--sl-color-neutral-900);"
          >
            Budget Policies
          </h4>
          ${
            !this.isFormOpen
              ? html`
                  <sl-button
                    size="small"
                    variant="primary"
                    @click=${() => {
                      this.cancelForm();
                      this.showAddForm = true;
                      void this.ensureSubjectsLoaded();
                    }}
                  >
                    <sl-icon slot="prefix" name="plus"></sl-icon>
                    Add Policy
                  </sl-button>
                `
              : ''
          }
        </div>

        ${
          this.error
            ? html`
                <sl-alert
                  variant="danger"
                  open
                  role="alert"
                  aria-live="assertive"
                  style="margin-top: var(--sl-spacing-medium);"
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.error}
                </sl-alert>
              `
            : ''
        }

        <div class="policy-list">
          ${
            this.loadingPolicies
              ? html`
                  <div class="loading-state" role="status" aria-live="polite">
                    <sl-spinner></sl-spinner>
                    Loading policies…
                  </div>
                `
              : this.policies.length === 0 && !this.isFormOpen
                ? html`<div
                    style="color: var(--sl-color-neutral-500); font-size: var(--sl-font-size-small);"
                  >
                    No budget policies configured.
                  </div>`
                : this.policies.map(
                    (p) => html`
                      <div class="policy-item">
                        <div>
                          <strong style="color: var(--sl-color-neutral-900);"
                            >${
                              p.period.charAt(0).toUpperCase() +
                              p.period.slice(1)
                            }</strong
                          >
                          ${
                            !this.subjectType
                              ? html`
                                  <sl-badge
                                    variant="neutral"
                                    style="margin-left: 8px;"
                                  >
                                    ${
                                      p.subject_type === 'global'
                                        ? 'Global'
                                        : p.subject_type === 'ai_model'
                                          ? 'Model'
                                          : p.subject_type === 'managed_agent'
                                            ? 'Agent'
                                            : p.subject_type
                                    }
                                  </sl-badge>
                                  <span
                                    style="font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-600); margin-left: 4px;"
                                  >
                                    ${
                                      p.subject_type !== 'global' &&
                                      p.subject_id
                                        ? this.getSubjectName(
                                            p.subject_type,
                                            p.subject_id
                                          )
                                        : ''
                                    }
                                  </span>
                                `
                              : ''
                          }
                          <br />
                          <span
                            style="font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-700);"
                            >Limit:</span
                          >
                          ${
                            p.hard_limit_usd
                              ? html`<span
                                  style="font-size: var(--sl-font-size-small);"
                                  >Hard: $${p.hard_limit_usd.toFixed(2)}</span
                                >`
                              : html`<span
                                  style="font-size: var(--sl-font-size-small);"
                                  >No Hard Limit</span
                                >`
                          }
                          ${
                            p.soft_limit_usd
                              ? html`<span
                                  style="font-size: var(--sl-font-size-small);"
                                  >| Soft: $${p.soft_limit_usd.toFixed(2)}</span
                                >`
                              : ''
                          }
                          ${
                            this.features?.['billing']
                              ? html`
                                  <div class="meta-text">
                                    Notify: ${this.formatNotifySummary(p)}
                                  </div>
                                `
                              : ''
                          }
                        </div>
                        <div class="policy-actions">
                          <sl-icon-button
                            name="pencil"
                            label="Edit policy"
                            ?disabled=${this.saving}
                            @click=${() => this.startEdit(p)}
                          ></sl-icon-button>
                          <sl-icon-button
                            name="trash"
                            label="Delete policy"
                            style="color: var(--sl-color-danger-600);"
                            ?disabled=${this.saving}
                            @click=${() => this.handleDelete(p.id)}
                          ></sl-icon-button>
                        </div>
                      </div>
                    `
                  )
          }
        </div>

        ${
          this.isFormOpen
            ? html`
                <div class="form-container">
                  ${
                    this.editingPolicyId
                      ? html`
                          ${this.renderScopeSummary(this.editingPolicy)}
                          <div class="readonly-field">
                            <span class="readonly-label">Period</span>
                            <span class="readonly-value"
                              >${
                                this.editingPolicy?.period
                                  ? this.editingPolicy.period
                                      .charAt(0)
                                      .toUpperCase() +
                                    this.editingPolicy.period.slice(1)
                                  : this.newPeriod
                              }</span
                            >
                          </div>
                        `
                      : !this.subjectType
                        ? html`
                            <div class="form-row">
                              <sl-select
                                label="Target Scope"
                                value=${this.newSubjectType}
                                @sl-change=${(e: any) => {
                                  this.newSubjectType = e.target.value;
                                  this.newSubjectId =
                                    this.newSubjectType === 'global'
                                      ? 'global'
                                      : '';
                                }}
                              >
                                <sl-option value="global"
                                  >Global (Account-wide)</sl-option
                                >
                                <sl-option value="ai_model">AI Model</sl-option>
                                <sl-option value="managed_agent"
                                  >Agent</sl-option
                                >
                              </sl-select>

                              ${
                                this.newSubjectType === 'ai_model'
                                  ? html`
                                      <sl-select
                                        label="Select Model"
                                        value=${this.newSubjectId}
                                        @sl-change=${(e: any) =>
                                          (this.newSubjectId = e.target.value)}
                                        ?disabled=${this.loadingSubjects}
                                      >
                                        ${this.models.map(
                                          (m) =>
                                            html`<sl-option value=${m.id}
                                              >${m.alias || m.id}</sl-option
                                            >`
                                        )}
                                      </sl-select>
                                    `
                                  : this.newSubjectType === 'managed_agent'
                                    ? html`
                                        <sl-select
                                          label="Select Agent"
                                          value=${this.newSubjectId}
                                          @sl-change=${(e: any) =>
                                            (this.newSubjectId =
                                              e.target.value)}
                                          ?disabled=${this.loadingSubjects}
                                        >
                                          ${this.agents.map(
                                            (a) =>
                                              html`<sl-option value=${a.id}
                                                >${a.display_name || a.id}</sl-option
                                              >`
                                          )}
                                        </sl-select>
                                      `
                                    : html`<div style="flex: 1"></div>`
                              }
                            </div>
                            <sl-select
                              label="Period"
                              value=${this.newPeriod}
                              @sl-change=${(e: any) =>
                                (this.newPeriod = e.target.value)}
                            >
                              <sl-option value="hourly">Hourly</sl-option>
                              <sl-option value="daily">Daily</sl-option>
                              <sl-option value="weekly">Weekly</sl-option>
                              <sl-option value="monthly">Monthly</sl-option>
                              <sl-option value="yearly">Yearly</sl-option>
                              <sl-option value="all_time">All Time</sl-option>
                            </sl-select>
                          `
                        : html`
                            <sl-select
                              label="Period"
                              value=${this.newPeriod}
                              @sl-change=${(e: any) =>
                                (this.newPeriod = e.target.value)}
                            >
                              <sl-option value="hourly">Hourly</sl-option>
                              <sl-option value="daily">Daily</sl-option>
                              <sl-option value="weekly">Weekly</sl-option>
                              <sl-option value="monthly">Monthly</sl-option>
                              <sl-option value="yearly">Yearly</sl-option>
                              <sl-option value="all_time">All Time</sl-option>
                            </sl-select>
                          `
                  }

                  <div class="form-row">
                    <sl-input
                      label="Hard Limit (USD)"
                      type="number"
                      step="0.0001"
                      .value=${this.newHardLimit}
                      @sl-input=${(e: any) =>
                        (this.newHardLimit = e.target.value)}
                    ></sl-input>
                    <sl-input
                      label="Soft Limit (USD)"
                      type="number"
                      step="0.0001"
                      .value=${this.newSoftLimit}
                      @sl-input=${(e: any) =>
                        (this.newSoftLimit = e.target.value)}
                    ></sl-input>
                  </div>

                  ${
                    this.features?.['billing']
                      ? html`
                          <notify-recipients-field
                            .users=${this.availableUsers}
                            .teams=${this.teams}
                            .userIds=${this.newNotifyUserIds}
                            .teamIds=${this.newNotifyTeamIds}
                            .customEmails=${this.newCustomEmails}
                            help-text="Recipients are notified via their email and mobile app preferences. Add custom emails when needed."
                            @notify-recipients-change=${this.handleNotifyRecipientsChange}
                          ></notify-recipients-field>

                          <div class="checkbox-group">
                            <sl-checkbox
                              ?checked=${this.newNotifySoft}
                              @sl-change=${(e: any) =>
                                (this.newNotifySoft = e.target.checked)}
                            >
                              Send email on Soft Limit
                            </sl-checkbox>
                            <sl-checkbox
                              ?checked=${this.newNotifyHard}
                              @sl-change=${(e: any) =>
                                (this.newNotifyHard = e.target.checked)}
                            >
                              Send email on Hard Limit
                            </sl-checkbox>
                          </div>
                        `
                      : ''
                  }

                  <div class="form-actions">
                    <sl-button
                      @click=${this.cancelForm}
                      ?disabled=${this.saving}
                      >Cancel</sl-button
                    >
                    <sl-button
                      variant="primary"
                      @click=${this.handleSave}
                      ?loading=${this.saving}
                      ?disabled=${this.saving}
                    >
                      ${this.editingPolicyId ? 'Update Policy' : 'Save Policy'}
                    </sl-button>
                  </div>
                </div>
              `
            : ''
        }
      </div>
    `;
  }
}
