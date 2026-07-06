import { LitElement, html, css, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getUsers,
  getTeams,
  getAIModels,
  createApprovalWorkflow,
  updateApprovalWorkflow,
} from '../api';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/range/range.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import './add-ai-model-modal';
import './notify-recipients-field.ts';
import type { NotifyRecipientsValue } from './notify-recipients-field.ts';

export interface ApprovalWorkflow {
  id: string;
  name: string;
  description?: string;
  approval_type: string;
  channel?: string;
  user?: string;
  approval_config?: {
    webhook_url?: string;
  };
  is_default?: boolean;
  approver_user_ids?: string[];
  approver_team_ids?: string[];
  approvals_required?: number;
  timeout_seconds?: number;
  escalation_user_ids?: string[];
  escalation_team_ids?: string[];
  ai_model?: string;
  ai_guidelines?: string;
  ai_confidence_threshold?: number;
  ai_fallback_behavior?: 'escalate' | 'approve' | 'deny';
  escalation_workflow_id?: string;
  async_approval_enabled?: boolean;
}

interface AIModel {
  id: string;
  name: string;
  provider_name: string;
  model_identifier: string;
}

interface User {
  id: string;
  username: string;
  email: string;
}

interface Team {
  id: string;
  name: string;
}

@customElement('approval-workflow-dialog')
export class ApprovalWorkflowDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ type: Object }) policy: ApprovalWorkflow | null = null;
  @property({ type: Array }) existingPolicies: ApprovalWorkflow[] = [];
  @property({ type: Object }) features: { [key: string]: boolean | string[] } =
    {};

  /**
   * Check if advanced approvals feature is enabled (EE only).
   * This gates multi-user approvers and AI-driven approvals.
   */
  private _hasAdvancedApprovals(): boolean {
    return this.features['advanced_approvals'] === true;
  }

  @state() private _loading = false;
  @state() private _error: string | null = null;
  @state() private _users: User[] = [];
  @state() private _teams: Team[] = [];
  @state() private _aiModels: AIModel[] = [];
  @state() private _loadingModels = false;
  @state() private _showAddModelModal = false;

  // Form state
  @state() private _name = '';
  @state() private _description = '';
  @state() private _approvalType = 'standard';
  @state() private _timeoutSeconds = 300;
  @state() private _isDefault = false;
  @state() private _asyncApprovalEnabled = false;

  // Standard type fields
  @state() private _approverUserIds: string[] = [];
  @state() private _approverTeamIds: string[] = [];
  @state() private _approvalsRequired = 1;

  // AI-driven type fields
  @state() private _aiModel = '';
  @state() private _aiGuidelines = '';
  @state() private _aiConfidenceThreshold = 0.8;
  @state() private _aiFallbackBehavior: 'escalate' | 'approve' | 'deny' =
    'escalate';
  @state() private _escalationWorkflowId = '';

  // Slack/Mattermost fields
  @state() private _channel = '';

  // Webhook fields
  @state() private _webhookUrl = '';

  static styles = css`
    :host {
      display: block;
    }

    sl-dialog::part(panel) {
      max-width: 650px;
    }

    .form-field {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-2x-small);
      margin-bottom: var(--sl-spacing-medium);
    }

    .form-label {
      font-size: var(--sl-font-size-small);
      font-weight: var(--sl-font-weight-semibold);
      color: var(--sl-color-neutral-700);
    }

    .form-label.required::after {
      content: ' *';
      color: var(--sl-color-danger-600);
    }

    .form-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: var(--sl-spacing-medium);
    }

    .type-section {
      padding: var(--sl-spacing-medium);
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      margin-bottom: var(--sl-spacing-medium);
    }

    .type-section-header {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
      color: var(--sl-color-neutral-700);
      font-weight: 500;
      margin-bottom: var(--sl-spacing-medium);
    }

    .ai-section {
      background: var(--sl-color-primary-50);
      border-color: var(--sl-color-primary-200);
    }

    .ai-section .type-section-header {
      color: var(--sl-color-primary-700);
    }

    .dialog-footer {
      display: flex;
      justify-content: flex-end;
      gap: var(--sl-spacing-small);
    }

    .threshold-display {
      display: flex;
      justify-content: space-between;
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-500);
      margin-top: var(--sl-spacing-2x-small);
    }

    .add-model-link {
      display: inline-flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-primary-600);
      cursor: pointer;
      margin-top: var(--sl-spacing-x-small);
    }

    .add-model-link:hover {
      text-decoration: underline;
    }

    sl-divider {
      --spacing: var(--sl-spacing-medium);
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    this._loadData();
  }

  willUpdate(changedProperties: PropertyValues<this>) {
    if (changedProperties.has('policy')) {
      this._populateForm();
    }
    if (changedProperties.has('open') && this.open) {
      this._populateForm();
      this._loadData();
    }
  }

  private async _loadData() {
    try {
      // Users, teams, and AI models are only needed for multi-user approver
      // selection and AI-driven approvals (EE features).  In OSS the
      // endpoints don't exist, so skip them to avoid 404s.
      if (this._hasAdvancedApprovals()) {
        const [usersResponse, teamsResponse] = await Promise.all([
          getUsers(),
          getTeams(),
        ]);
        this._users = usersResponse.users || [];
        this._teams = teamsResponse.teams || [];

        await this._loadAIModels();
      }
    } catch (error) {
      console.error('Failed to load data:', error);
    }
  }

  private async _loadAIModels() {
    this._loadingModels = true;
    try {
      const models = await getAIModels();
      this._aiModels = models || [];
    } catch (error) {
      console.error('Failed to load AI models:', error);
      this._aiModels = [];
    } finally {
      this._loadingModels = false;
    }
  }

  private _populateForm() {
    if (this.policy) {
      this._name = this.policy.name || '';
      this._description = this.policy.description || '';
      // Fall back to standard only for truly EE-only types.
      const eeTypes = ['ai_driven'];
      // Older default workflows were stored with approval_type="manual"
      // (a legacy synonym for the in-UI human-approval flow). The dialog's
      // dropdown only renders "standard" for that case, so normalise here
      // — otherwise the type field appears blank in the editor.
      const rawType = this.policy.approval_type || 'standard';
      const policyType = rawType === 'manual' ? 'standard' : rawType;
      if (eeTypes.includes(policyType) && !this._hasAdvancedApprovals()) {
        this._approvalType = 'standard';
      } else {
        this._approvalType = policyType;
      }
      this._timeoutSeconds = this.policy.timeout_seconds || 300;
      this._isDefault = this.policy.is_default || false;
      this._asyncApprovalEnabled = this.policy?.async_approval_enabled ?? false;

      // Standard fields
      this._approverUserIds = this.policy.approver_user_ids || [];
      this._approverTeamIds = this.policy.approver_team_ids || [];
      this._approvalsRequired = this.policy.approvals_required || 1;

      // AI-driven fields
      this._aiModel = this.policy.ai_model || '';
      this._aiGuidelines = this.policy.ai_guidelines || '';
      this._aiConfidenceThreshold = this.policy.ai_confidence_threshold ?? 0.8;
      this._aiFallbackBehavior = this.policy.ai_fallback_behavior || 'escalate';
      this._escalationWorkflowId = this.policy.escalation_workflow_id || '';

      // Slack/Mattermost/Webhook
      this._channel = this.policy.channel || '';
      this._webhookUrl = this.policy.approval_config?.webhook_url || '';
    } else {
      this._resetForm();
    }
  }

  private _resetForm() {
    this._name = '';
    this._description = '';
    this._approvalType = 'standard';
    this._timeoutSeconds = 300;
    this._isDefault = false;
    this._asyncApprovalEnabled = false;
    this._approverUserIds = [];
    this._approverTeamIds = [];
    this._approvalsRequired = 1;
    this._aiModel = '';
    this._aiGuidelines = '';
    this._aiConfidenceThreshold = 0.8;
    this._aiFallbackBehavior = 'escalate';
    this._escalationWorkflowId = '';
    this._channel = '';
    this._webhookUrl = '';
    this._error = null;
  }

  private _handleClose() {
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true })
    );
  }

  private _isFormValid(): boolean {
    if (!this._name.trim()) return false;

    switch (this._approvalType) {
      case 'standard':
        // In EE mode, approvers are required; in OSS (single-user) they are not
        if (this._hasAdvancedApprovals()) {
          return (
            this._approverUserIds.length > 0 || this._approverTeamIds.length > 0
          );
        }
        return true;
      case 'ai_driven':
        // AI model is required
        return !!this._aiModel;
      case 'slack':
      case 'mattermost':
      case 'webhook':
        // Incoming webhook URL is required
        return !!this._webhookUrl.trim();
      default:
        return true;
    }
  }

  private async _handleSave() {
    if (!this._isFormValid()) {
      this._error = 'Please fill in all required fields';
      return;
    }

    this._loading = true;
    this._error = null;

    try {
      const policyData: any = {
        name: this._name.trim(),
        description: this._description.trim() || null,
        approval_type: this._approvalType,
        timeout_seconds: this._timeoutSeconds,
        is_default: this._isDefault,
        async_approval_enabled: this._asyncApprovalEnabled,
      };

      // Type-specific fields
      switch (this._approvalType) {
        case 'standard':
          // Only send multi-user approval fields in EE mode
          if (this._hasAdvancedApprovals()) {
            policyData.approver_user_ids = this._approverUserIds;
            policyData.approver_team_ids = this._approverTeamIds;
            policyData.approvals_required = this._approvalsRequired;
          }
          break;

        case 'ai_driven':
          policyData.ai_model = this._aiModel;
          policyData.ai_guidelines = this._aiGuidelines || null;
          policyData.ai_confidence_threshold = this._aiConfidenceThreshold;
          policyData.ai_fallback_behavior = this._aiFallbackBehavior;
          if (
            this._aiFallbackBehavior === 'escalate' &&
            this._escalationWorkflowId
          ) {
            policyData.escalation_workflow_id = this._escalationWorkflowId;
          }
          break;

        case 'slack':
        case 'mattermost':
          policyData.approval_config = {
            webhook_url: this._webhookUrl.trim(),
          };
          policyData.channel = this._channel.trim();
          break;

        case 'webhook':
          policyData.approval_config = {
            webhook_url: this._webhookUrl.trim(),
          };
          break;
      }

      let savedPolicy;
      if (this.policy) {
        savedPolicy = await updateApprovalWorkflow(this.policy.id, policyData);
      } else {
        savedPolicy = await createApprovalWorkflow(policyData);
      }

      this.dispatchEvent(
        new CustomEvent('saved', {
          detail: { policy: savedPolicy },
          bubbles: true,
          composed: true,
        })
      );
      this._handleClose();
    } catch (error: any) {
      this._error = error.message || 'Failed to save approval workflow';
    } finally {
      this._loading = false;
    }
  }

  private _handleAddModel() {
    this._showAddModelModal = true;
  }

  private _handleAddModelModalClose() {
    this._showAddModelModal = false;
  }

  private async _handleModelCreated() {
    this._showAddModelModal = false;
    // Refresh the AI models list so the newly created model appears
    await this._loadAIModels();
  }

  private _renderTypeSpecificFields() {
    switch (this._approvalType) {
      case 'standard':
        return this._renderStandardFields();
      case 'ai_driven':
        return this._renderAIDrivenFields();
      case 'slack':
      case 'mattermost':
        return this._renderChannelFields();
      case 'webhook':
        return this._renderWebhookFields();
      default:
        return null;
    }
  }

  private _renderStandardFields() {
    // In open-source (single-user) mode, there's no need to select
    // approvers or require multiple approvals — the sole user approves.
    if (!this._hasAdvancedApprovals()) {
      return null;
    }

    return html`
      <div class="type-section">
        <div class="type-section-header">
          <sl-icon name="people"></sl-icon>
          Human Approval Settings
        </div>

        <notify-recipients-field
          label="Approvers"
          .required=${true}
          .showCustomEmails=${false}
          .users=${this._users}
          .teams=${this._teams}
          .userIds=${this._approverUserIds}
          .teamIds=${this._approverTeamIds}
          helpText="Select one or more users or teams who can approve requests."
          @notify-recipients-change=${this._handleApproverRecipientsChange}
        ></notify-recipients-field>

        <div class="form-field">
          <label class="form-label">Approvals Required</label>
          <sl-input
            type="number"
            min="1"
            .value=${String(this._approvalsRequired)}
            @sl-input=${(e: any) =>
              (this._approvalsRequired = parseInt(e.target.value) || 1)}
          ></sl-input>
          <small style="color: var(--sl-color-neutral-500);">
            Number of approvals needed before the action can proceed.
          </small>
        </div>
      </div>
    `;
  }

  private _handleApproverRecipientsChange(
    event: CustomEvent<NotifyRecipientsValue>
  ) {
    this._approverUserIds = event.detail.userIds;
    this._approverTeamIds = event.detail.teamIds;
  }

  private _renderAIDrivenFields() {
    const standardPolicies = this.existingPolicies.filter(
      (p) => p.approval_type === 'standard' && p.id !== this.policy?.id
    );

    return html`
      <div class="type-section ai-section">
        <div class="type-section-header">
          <sl-icon name="robot"></sl-icon>
          AI Approval Settings
        </div>

        <div class="form-field">
          <label class="form-label required">AI Model</label>
          <sl-select
            hoist
            placeholder=${
              this._loadingModels
                ? 'Loading models...'
                : 'Select an AI model...'
            }
            .value=${this._aiModel}
            @sl-change=${(e: any) => (this._aiModel = e.target.value)}
            ?disabled=${this._loadingModels}
          >
            ${this._aiModels.map(
              (model) => html`
                <sl-option value=${model.model_identifier}>
                  ${model.name} (${model.provider_name})
                </sl-option>
              `
            )}
          </sl-select>
          ${
            this._aiModels.length === 0 && !this._loadingModels
              ? html`
                  <div class="add-model-link" @click=${this._handleAddModel}>
                    <sl-icon name="plus-circle"></sl-icon>
                    Configure an AI model first
                  </div>
                `
              : html`
                  <div class="add-model-link" @click=${this._handleAddModel}>
                    <sl-icon name="plus-circle"></sl-icon>
                    Add new model
                  </div>
                `
          }
        </div>

        <div class="form-field">
          <label class="form-label">Guidelines</label>
          <sl-textarea
            .value=${this._aiGuidelines}
            @sl-input=${(e: any) => (this._aiGuidelines = e.target.value)}
            placeholder="APPROVE if:
- Read-only operations
- Non-production environments

DENY if:
- Production data modifications
- Credential access"
            rows="6"
          ></sl-textarea>
          <small style="color: var(--sl-color-neutral-500);">
            Instructions for the AI to determine when to approve or deny.
          </small>
        </div>

        <div class="form-field">
          <label class="form-label">
            Confidence Threshold:
            ${Math.round(this._aiConfidenceThreshold * 100)}%
          </label>
          <sl-range
            .value=${this._aiConfidenceThreshold * 100}
            @sl-input=${(e: any) =>
              (this._aiConfidenceThreshold =
                (parseFloat(e.target.value) || 80) / 100)}
            min="0"
            max="100"
            step="5"
          ></sl-range>
          <div class="threshold-display">
            <span>0% (always escalate)</span>
            <span>100% (very confident)</span>
          </div>
        </div>

        <div class="form-field">
          <label class="form-label">When Uncertain</label>
          <sl-radio-group
            .value=${this._aiFallbackBehavior}
            @sl-change=${(e: any) =>
              (this._aiFallbackBehavior = e.target.value)}
          >
            <sl-radio value="escalate">Escalate to human approvers</sl-radio>
            <sl-radio value="approve">Approve automatically</sl-radio>
            <sl-radio value="deny">Deny automatically</sl-radio>
          </sl-radio-group>
        </div>

        ${
          this._aiFallbackBehavior === 'escalate'
            ? html`
                <div class="form-field">
                  <label class="form-label">Escalation Workflow</label>
                  <sl-select
                    hoist
                    .value=${this._escalationWorkflowId}
                    @sl-change=${(e: any) =>
                      (this._escalationWorkflowId = e.target.value)}
                    placeholder="Select a workflow for escalation..."
                    clearable
                  >
                    ${standardPolicies.map(
                      (p) => html`
                        <sl-option value=${p.id}>${p.name}</sl-option>
                      `
                    )}
                  </sl-select>
                  <small style="color: var(--sl-color-neutral-500);">
                    The approval workflow to use when AI confidence is below
                    threshold.
                  </small>
                </div>
              `
            : ''
        }
      </div>
    `;
  }

  private _renderChannelFields() {
    const typeName = this._approvalType === 'slack' ? 'Slack' : 'Mattermost';

    return html`
      <div class="type-section">
        <div class="type-section-header">
          <sl-icon name="chat-square-text"></sl-icon>
          ${typeName} Settings
        </div>

        <div class="form-field">
          <label class="form-label required">Incoming Webhook URL</label>
          <sl-input
            type="url"
            .value=${this._webhookUrl}
            @sl-input=${(e: any) => (this._webhookUrl = e.target.value)}
            placeholder=${
              this._approvalType === 'slack'
                ? 'https://hooks.slack.com/services/...'
                : 'https://your-mattermost.com/hooks/...'
            }
          ></sl-input>
          <small style="color: var(--sl-color-neutral-500);">
            Approval requests will be posted to this ${typeName} incoming
            webhook.
          </small>
        </div>

        <div class="form-field">
          <label class="form-label">Channel (Optional)</label>
          <sl-input
            .value=${this._channel}
            @sl-input=${(e: any) => (this._channel = e.target.value)}
            placeholder="#approval-requests"
          ></sl-input>
          <small style="color: var(--sl-color-neutral-500);">
            Optional display target for your own bookkeeping.
          </small>
        </div>
      </div>
    `;
  }

  private _renderWebhookFields() {
    return html`
      <div class="type-section">
        <div class="type-section-header">
          <sl-icon name="broadcast"></sl-icon>
          Webhook Settings
        </div>

        <div class="form-field">
          <label class="form-label required">Webhook URL</label>
          <sl-input
            type="url"
            .value=${this._webhookUrl}
            @sl-input=${(e: any) => (this._webhookUrl = e.target.value)}
            placeholder="https://your-service.com/approval-webhook"
          ></sl-input>
          <small style="color: var(--sl-color-neutral-500);">
            Approval requests will be sent to this URL. The response should
            include an approval link.
          </small>
        </div>
      </div>
    `;
  }

  render() {
    return html`
      <sl-dialog
        label=${
          this.policy ? 'Edit Approval Workflow' : 'Create Approval Workflow'
        }
        ?open=${this.open}
        @sl-request-close=${this._handleClose}
      >
        ${
          this._error
            ? html`
                <sl-alert variant="danger" open closable>
                  ${this._error}
                </sl-alert>
              `
            : ''
        }

        <div class="form-field">
          <label class="form-label required">Name</label>
          <sl-input
            .value=${this._name}
            @sl-input=${(e: any) => (this._name = e.target.value)}
            placeholder="e.g., Production Safeguards"
          ></sl-input>
        </div>

        <div class="form-field">
          <label class="form-label">Description</label>
          <sl-textarea
            .value=${this._description}
            @sl-input=${(e: any) => (this._description = e.target.value)}
            placeholder="Optional description"
            rows="2"
          ></sl-textarea>
        </div>

        <div class="form-row">
          <div class="form-field">
            <label class="form-label">Type</label>
            <sl-select
              hoist
              .value=${this._approvalType}
              @sl-change=${(e: any) => (this._approvalType = e.target.value)}
            >
              <sl-option value="standard">Standard Human Approval</sl-option>
              <sl-option value="slack">Slack</sl-option>
              <sl-option value="mattermost">Mattermost</sl-option>
              <sl-option value="webhook">Webhook</sl-option>
              ${
                this._hasAdvancedApprovals()
                  ? html`
                      <sl-option value="ai_driven"
                        >AI-Driven Approval</sl-option
                      >
                    `
                  : ''
              }
            </sl-select>
          </div>

          <div class="form-field">
            <label class="form-label">Timeout (seconds)</label>
            <sl-input
              type="number"
              min="30"
              .value=${String(this._timeoutSeconds)}
              @sl-input=${(e: any) =>
                (this._timeoutSeconds = parseInt(e.target.value) || 300)}
            ></sl-input>
          </div>
        </div>

        <sl-divider></sl-divider>

        ${this._renderTypeSpecificFields()}

        <div class="form-field">
          <div
            style="display: flex; justify-content: space-between; align-items: center;"
          >
            <label class="form-label">Enable Async Approvals</label>
            <sl-switch
              ?checked=${this._asyncApprovalEnabled}
              @sl-change=${(e: Event) => {
                this._asyncApprovalEnabled = (e.target as any).checked;
              }}
            ></sl-switch>
          </div>
          <small style="color: var(--sl-color-neutral-500);">
            When enabled, tool calls return immediately and agents poll for
            approval status. Recommended for CLI clients (Claude Code, Codex
            CLI) to avoid timeouts.
          </small>
        </div>

        <div class="form-field">
          <div
            style="display: flex; justify-content: space-between; align-items: center;"
          >
            <label class="form-label">Set as Default</label>
            <sl-switch
              ?checked=${this._isDefault}
              @sl-change=${(e: any) => (this._isDefault = e.target.checked)}
            ></sl-switch>
          </div>
          <small style="color: var(--sl-color-neutral-500);">
            The default policy is used when no specific policy is assigned.
          </small>
        </div>

        <div slot="footer" class="dialog-footer">
          <sl-button @click=${this._handleClose}>Cancel</sl-button>
          <sl-button
            variant="primary"
            @click=${this._handleSave}
            ?loading=${this._loading}
            ?disabled=${!this._isFormValid()}
          >
            ${this.policy ? 'Save Changes' : 'Create Policy'}
          </sl-button>
        </div>
      </sl-dialog>

      <add-ai-model-modal
        ?open=${this._showAddModelModal}
        @close-modal=${this._handleAddModelModalClose}
        @model-created=${this._handleModelCreated}
      ></add-ai-model-modal>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'approval-workflow-dialog': ApprovalWorkflowDialog;
  }
}
