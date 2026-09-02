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
import { budgetTrackStyles, renderBudgetTrack } from '../styles/budget-track';
import './notify-recipients-field.ts';
import type { NotifyRecipientsValue } from './notify-recipients-field.ts';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import { consoleDialogStyles } from '../styles/console-dialog';

/** List groups, in display order. Rendered only when they hold a limit. */
const SUBJECT_GROUPS: {
  types: string[];
  label: string;
  icon: string;
}[] = [
  { types: ['global', 'account'], label: 'Global', icon: 'globe' },
  { types: ['managed_agent'], label: 'Agents', icon: 'robot' },
  { types: ['ai_model'], label: 'Models', icon: 'cpu' },
  { types: ['user'], label: 'Users', icon: 'person' },
  { types: ['team'], label: 'Teams', icon: 'people' },
  { types: ['api_key'], label: 'API keys', icon: 'key' },
];

const PERIOD_LABELS: Record<string, string> = {
  hourly: 'Hourly',
  daily: 'Daily',
  weekly: 'Weekly',
  monthly: 'Monthly',
  yearly: 'Yearly',
  all_time: 'All time',
};

const SCOPE_CHOICES: { value: string; label: string }[] = [
  { value: 'global', label: 'Global' },
  { value: 'managed_agent', label: 'Agent' },
  { value: 'ai_model', label: 'Model' },
  { value: 'user', label: 'User' },
];

/** Above this many options the subject picker grows a search box. */
const SUBJECT_SEARCH_THRESHOLD = 8;

/**
 * Two steps in one component: a grouped list of the limits that exist, and a
 * form that replaces the list while an operator writes one. Embedded scoped
 * (agent, model, API key detail pages) it shows a flat list for that subject
 * only, which is why the public API stays `subjectType` / `subjectId` /
 * `billingEnabled` plus the `budget-policies-changed` event.
 */
@customElement('budget-policy-editor')
export class BudgetPolicyEditor extends LitElement {
  @property({ type: String }) subjectType?: string;
  @property({ type: String }) subjectId?: string;
  /** Skip the features round-trip when the parent already knows billing is on. */
  @property({ type: Boolean }) billingEnabled = false;

  @state() private policies: BudgetPolicy[] = [];
  @state() private step: 'list' | 'form' = 'list';
  @state() private editingPolicyId: string | null = null;
  @state() private error = '';
  @state() private formError = '';
  @state() private pendingDeleteId: string | null = null;

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
  @state() private subjectFilter = '';

  // Form state
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

  static styles = [
    consoleDialogStyles,
    budgetTrackStyles,
    css`
      :host {
        display: block;
        font-family: var(--sl-font-sans);
        color: var(--sl-color-neutral-900);
      }
      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-small);
      }
      h4 {
        margin: 0;
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
      }
      .group {
        margin-top: var(--sl-spacing-medium);
      }
      .group-label {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        font-weight: var(--sl-font-weight-semibold);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        margin-bottom: var(--sl-spacing-2x-small);
      }
      .limit-row {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-x-small) 0;
      }
      .limit-row + .limit-row {
        border-top: 1px solid var(--sl-color-neutral-100);
      }
      .row-main {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
        min-width: 0;
      }
      .row-name {
        font-weight: var(--sl-font-weight-semibold);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .row-limits {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        font-variant-numeric: tabular-nums;
      }
      .row-meter {
        margin-left: auto;
        width: 96px;
        flex-shrink: 0;
      }
      .row-notify {
        color: var(--sl-color-neutral-500);
        flex-shrink: 0;
      }
      .empty {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin: var(--sl-spacing-medium) 0;
      }
      .form {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }
      .form-top {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }
      .back-button::part(base) {
        padding-left: 0;
      }
      .form-row {
        display: flex;
        gap: var(--sl-spacing-medium);
      }
      .form-row > * {
        flex: 1;
      }
      .switch-row {
        display: flex;
        gap: var(--sl-spacing-large);
        align-items: center;
      }
      .field-error {
        color: var(--sl-color-danger-700);
        font-size: var(--sl-font-size-small);
      }
      .form-actions {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }
      .form-actions .spacer {
        margin-left: auto;
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
    `,
  ];

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
    if (this.step === 'form') {
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
    if (type === 'user') {
      const user = this.availableUsers.find((u) => u.id === id);
      return user ? user.username || user.email || user.id : id;
    }
    if (type === 'team') {
      const team = this.teams.find((t) => t.id === id);
      return team ? team.name || team.id : id;
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
        this.showList();
      }
      this.policies = this.policies.filter((policy) => policy.id !== id);
      this.dispatchPoliciesChanged();
    } catch (e: any) {
      this.error = 'Failed to delete limit.';
    } finally {
      this.pendingDeleteId = null;
      this.saving = false;
    }
  }

  private startAdd() {
    this.resetForm();
    this.editingPolicyId = null;
    this.error = '';
    this.formError = '';
    this.step = 'form';
    void this.ensureSubjectsLoaded();
  }

  private startEdit(policy: BudgetPolicy) {
    this.editingPolicyId = policy.id;
    this.error = '';
    this.formError = '';
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
    this.step = 'form';
    void this.ensureSubjectsLoaded();
  }

  private showList() {
    this.step = 'list';
    this.editingPolicyId = null;
    this.formError = '';
    this.subjectFilter = '';
    this.resetForm();
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

  /** Inline validation, so a typo never reaches the server as a 422. */
  private validate(soft: number | null, hard: number | null): string {
    if (soft === null && hard === null) {
      return 'Set a soft limit, a hard limit, or both.';
    }
    if (soft !== null && soft <= 0) {
      return 'The soft limit must be greater than zero.';
    }
    if (hard !== null && hard <= 0) {
      return 'The hard limit must be greater than zero.';
    }
    if (soft !== null && hard !== null && soft > hard) {
      return 'The soft limit must be at or below the hard limit.';
    }
    if (
      !this.subjectType &&
      this.newSubjectType !== 'global' &&
      !this.newSubjectId
    ) {
      return 'Choose what this limit applies to.';
    }
    return '';
  }

  private async handleSave() {
    const hardLimit = this.newHardLimit ? parseFloat(this.newHardLimit) : null;
    const softLimit = this.newSoftLimit ? parseFloat(this.newSoftLimit) : null;
    const validationError = this.validate(softLimit, hardLimit);
    if (validationError) {
      this.formError = validationError;
      return;
    }

    this.error = '';
    this.formError = '';
    this.saving = true;
    const notifyPayload = this.buildNotifyPayload();

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
        this.showList();
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
      this.showList();
      this.dispatchPoliciesChanged();
    } catch (err: any) {
      this.formError =
        'Failed to save limit. ' + (err instanceof Error ? err.message : '');
    } finally {
      this.saving = false;
    }
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

  private formatCurrency(value: number): string {
    const amount = Number(value || 0);
    if (amount > 0 && amount < 0.01) return `$${amount.toFixed(4)}`;
    return `$${amount.toFixed(2)}`;
  }

  private recipientCount(policy: BudgetPolicy): number {
    return (
      (policy.notification_user_ids?.length || 0) +
      (policy.notification_team_ids?.length || 0) +
      (policy.notification_emails?.length || 0)
    );
  }

  private policyRowName(policy: BudgetPolicy): string {
    if (policy.subject_type === 'global' || policy.subject_type === 'account') {
      return 'Global';
    }
    if (!policy.subject_id) {
      return policy.subject_type.replace(/_/g, ' ');
    }
    return this.getSubjectName(policy.subject_type, policy.subject_id);
  }

  private renderPolicyRow(policy: BudgetPolicy) {
    const soft = policy.soft_limit_usd || 0;
    const hard = policy.hard_limit_usd || 0;
    const spend = policy.current_spend_usd || 0;
    const limits = [
      soft > 0 ? `Soft ${this.formatCurrency(soft)}` : null,
      hard > 0 ? `Hard ${this.formatCurrency(hard)}` : null,
    ].filter(Boolean);
    const notifies = policy.notify_on_soft || policy.notify_on_hard;
    const recipients = this.recipientCount(policy);

    return html`
      <div class="limit-row">
        <div class="row-main">
          <span class="row-name">${this.policyRowName(policy)}</span>
          <sl-badge variant="neutral" pill
            >${PERIOD_LABELS[policy.period] || policy.period}</sl-badge
          >
          <span class="row-limits"
            >${limits.length ? limits.join(' · ') : 'No limit set'}</span
          >
        </div>
        <div class="row-meter">
          ${renderBudgetTrack({
            spend,
            softLimit: soft,
            hardLimit: hard,
            label: `${this.policyRowName(policy)} limit`,
          })}
        </div>
        ${
          notifies
            ? html`<sl-tooltip
                content=${
                  recipients > 0
                    ? `Notifies ${recipients} recipient${recipients === 1 ? '' : 's'}`
                    : 'Notifications on, no recipients yet'
                }
              >
                <sl-icon class="row-notify" name="bell"></sl-icon>
              </sl-tooltip>`
            : nothing
        }
        <sl-dropdown hoist>
          <sl-icon-button
            slot="trigger"
            name="three-dots-vertical"
            label="Limit actions"
            ?disabled=${this.saving}
          ></sl-icon-button>
          <sl-menu>
            <sl-menu-item @click=${() => this.startEdit(policy)}>
              <sl-icon slot="prefix" name="pencil"></sl-icon>
              Edit
            </sl-menu-item>
            <sl-menu-item @click=${() => (this.pendingDeleteId = policy.id)}>
              <sl-icon slot="prefix" name="trash"></sl-icon>
              Delete
            </sl-menu-item>
          </sl-menu>
        </sl-dropdown>
      </div>
    `;
  }

  private renderList() {
    if (this.loadingPolicies) {
      return html`
        <div class="loading-state" role="status" aria-live="polite">
          <sl-spinner></sl-spinner>
          Loading limits…
        </div>
      `;
    }

    if (this.policies.length === 0) {
      return html`
        <div class="empty">
          No limits yet. Start with a global monthly hard limit.
        </div>
      `;
    }

    // Scoped embeds already filter to one subject, so grouping would print a
    // single header over a single list.
    if (this.subjectType) {
      return html`<div class="group">
        ${this.policies.map((policy) => this.renderPolicyRow(policy))}
      </div>`;
    }

    const seen = new Set<string>();
    const groups = SUBJECT_GROUPS.map((group) => {
      const policies = this.policies.filter((policy) =>
        group.types.includes(policy.subject_type)
      );
      policies.forEach((policy) => seen.add(policy.id));
      return { ...group, policies };
    }).filter((group) => group.policies.length > 0);

    const other = this.policies.filter((policy) => !seen.has(policy.id));
    if (other.length > 0) {
      groups.push({
        types: [],
        label: 'Other',
        icon: 'sliders',
        policies: other,
      });
    }

    return html`
      ${groups.map(
        (group) => html`
          <div class="group">
            <div class="group-label">
              <sl-icon name=${group.icon}></sl-icon>
              ${group.label}
            </div>
            ${group.policies.map((policy) => this.renderPolicyRow(policy))}
          </div>
        `
      )}
    `;
  }

  private subjectOptions(): { value: string; label: string }[] {
    if (this.newSubjectType === 'ai_model') {
      return this.models.map((m) => ({ value: m.id, label: m.alias || m.id }));
    }
    if (this.newSubjectType === 'managed_agent') {
      return this.agents.map((a) => ({
        value: a.id,
        label: a.display_name || a.id,
      }));
    }
    if (this.newSubjectType === 'user') {
      return this.availableUsers.map((u) => ({
        value: u.id,
        label: u.username || u.email || u.id,
      }));
    }
    return [];
  }

  private renderSubjectField() {
    const options = this.subjectOptions();
    const filter = this.subjectFilter.trim().toLowerCase();
    const visible = filter
      ? options.filter((option) => option.label.toLowerCase().includes(filter))
      : options;
    const searchable = options.length > SUBJECT_SEARCH_THRESHOLD;
    const label =
      this.newSubjectType === 'ai_model'
        ? 'Model'
        : this.newSubjectType === 'managed_agent'
          ? 'Agent'
          : 'User';

    return html`
      <div>
        ${
          searchable
            ? html`<sl-input
                size="small"
                clearable
                placeholder=${`Search ${label.toLowerCase()}s`}
                aria-label=${`Search ${label.toLowerCase()}s`}
                .value=${this.subjectFilter}
                @sl-input=${(e: any) => (this.subjectFilter = e.target.value)}
                style="margin-bottom: var(--sl-spacing-2x-small);"
              >
                <sl-icon slot="prefix" name="search"></sl-icon>
              </sl-input>`
            : nothing
        }
        <sl-select
          label=${label}
          value=${this.newSubjectId}
          hoist
          ?disabled=${this.loadingSubjects || this.editingPolicyId !== null}
          help-text=${
            this.newSubjectType === 'user'
              ? 'Enforces across all agents owned by this user'
              : ''
          }
          @sl-change=${(e: any) => (this.newSubjectId = e.target.value)}
        >
          ${visible.map(
            (option) =>
              html`<sl-option value=${option.value}>${option.label}</sl-option>`
          )}
        </sl-select>
      </div>
    `;
  }

  private renderForm() {
    const editing = this.editingPolicyId !== null;

    return html`
      <div class="form">
        <div class="form-top">
          <sl-button
            class="back-button"
            variant="text"
            size="small"
            @click=${this.showList}
          >
            ← Limits
          </sl-button>
          <h4 id="budget-policy-editor-title">
            ${editing ? 'Edit limit' : 'New limit'}
          </h4>
        </div>

        ${
          this.subjectType
            ? nothing
            : html`
                <sl-radio-group
                  label="Scope"
                  value=${this.newSubjectType}
                  @sl-change=${(e: any) => {
                    this.newSubjectType = e.target.value;
                    this.newSubjectId =
                      this.newSubjectType === 'global' ? 'global' : '';
                    this.subjectFilter = '';
                  }}
                >
                  ${SCOPE_CHOICES.map(
                    (choice) => html`
                      <sl-radio-button
                        size="small"
                        value=${choice.value}
                        ?disabled=${editing}
                        >${choice.label}</sl-radio-button
                      >
                    `
                  )}
                </sl-radio-group>
                ${
                  this.newSubjectType === 'global'
                    ? nothing
                    : this.renderSubjectField()
                }
              `
        }

        <sl-select
          label="Period"
          value=${this.newPeriod}
          hoist
          ?disabled=${editing}
          @sl-change=${(e: any) => (this.newPeriod = e.target.value)}
        >
          ${Object.entries(PERIOD_LABELS).map(
            ([value, label]) =>
              html`<sl-option value=${value}>${label}</sl-option>`
          )}
        </sl-select>

        <div class="form-row">
          <sl-input
            label="Soft limit (USD)"
            type="number"
            step="0.0001"
            inputmode="decimal"
            help-text="Notifies recipients"
            .value=${this.newSoftLimit}
            @sl-input=${(e: any) => (this.newSoftLimit = e.target.value)}
          ></sl-input>
          <sl-input
            label="Hard limit (USD)"
            type="number"
            step="0.0001"
            inputmode="decimal"
            help-text="Blocks further model calls"
            .value=${this.newHardLimit}
            @sl-input=${(e: any) => (this.newHardLimit = e.target.value)}
          ></sl-input>
        </div>

        ${
          this.formError
            ? html`<div class="field-error" role="alert">
                ${this.formError}
              </div>`
            : nothing
        }

        <div class="switch-row">
          <sl-switch
            ?checked=${this.newNotifySoft}
            @sl-change=${(e: any) => (this.newNotifySoft = e.target.checked)}
          >
            Notify on soft
          </sl-switch>
          <sl-switch
            ?checked=${this.newNotifyHard}
            @sl-change=${(e: any) => (this.newNotifyHard = e.target.checked)}
          >
            Notify on hard
          </sl-switch>
        </div>

        ${
          this.newNotifySoft || this.newNotifyHard
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
              `
            : nothing
        }

        <div class="form-actions">
          ${
            editing
              ? html`<sl-button
                  variant="danger"
                  outline
                  size="small"
                  ?disabled=${this.saving}
                  @click=${() => (this.pendingDeleteId = this.editingPolicyId)}
                >
                  Delete
                </sl-button>`
              : nothing
          }
          <span class="spacer"></span>
          <sl-button
            size="small"
            ?disabled=${this.saving}
            @click=${this.showList}
            >Cancel</sl-button
          >
          <sl-button
            variant="primary"
            size="small"
            ?loading=${this.saving}
            ?disabled=${this.saving}
            @click=${this.handleSave}
          >
            Save limit
          </sl-button>
        </div>
      </div>
    `;
  }

  private renderDeleteConfirm() {
    const policy = this.policies.find(
      (candidate) => candidate.id === this.pendingDeleteId
    );
    return html`
      <sl-dialog
        label="Delete limit"
        ?open=${this.pendingDeleteId !== null}
        @sl-hide=${() => (this.pendingDeleteId = null)}
      >
        Delete the ${policy ? this.policyRowName(policy) : ''}
        ${
          policy
            ? (PERIOD_LABELS[policy.period] || policy.period).toLowerCase()
            : ''
        }
        limit? Spending will no longer be capped for that scope.
        <div slot="footer">
          <sl-button
            size="small"
            @click=${() => (this.pendingDeleteId = null)}
            ?disabled=${this.saving}
            >Cancel</sl-button
          >
          <sl-button
            variant="danger"
            size="small"
            ?loading=${this.saving}
            @click=${() =>
              this.pendingDeleteId && this.handleDelete(this.pendingDeleteId)}
            >Delete limit</sl-button
          >
        </div>
      </sl-dialog>
    `;
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
        ${
          this.step === 'list'
            ? html`
                <div class="header">
                  <h4 id="budget-policy-editor-title">Limits</h4>
                  <sl-button
                    size="small"
                    variant="primary"
                    @click=${this.startAdd}
                  >
                    <sl-icon slot="prefix" name="plus"></sl-icon>
                    Add limit
                  </sl-button>
                </div>
              `
            : nothing
        }
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
            : nothing
        }
        ${this.step === 'list' ? this.renderList() : this.renderForm()}
        ${this.renderDeleteConfirm()}
      </div>
    `;
  }
}
