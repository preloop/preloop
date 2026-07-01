import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import './tool-rule-editor';
import type { ApprovalWorkflow } from './tool-card';
import type { AccessRule } from '../api';
import type { RuleFormData } from './tool-rule-editor';

export interface AccessRuleSummary {
  id: string;
  action: string;
  condition_expression: string | null;
  condition_type: string;
  priority: number;
  description: string | null;
  is_enabled: boolean;
  approval_workflow_id: string | null;
}

@customElement('governance-rule-set-editor')
export class GovernanceRuleSetEditor extends LitElement {
  @property({ type: String }) toolName = '';
  @property({ type: Object }) toolSchema: Record<string, unknown> | null = null;
  @property({ type: Array }) rules: AccessRuleSummary[] = [];
  @property({ type: Array }) workflows: ApprovalWorkflow[] = [];
  @property({ type: Object }) features: { [key: string]: boolean | string[] } =
    {};
  @property({ type: String }) emptyMessage = 'No scoped rules configured yet.';
  @property({ type: String }) addButtonLabel = 'Add Rule';

  @state() private _showRuleEditor = false;
  @state() private _editingRule: AccessRule | null = null;
  @state() private _dragIndex: number | null = null;
  @state() private _dragOverIndex: number | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .rules-list {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .rule-item {
      display: flex;
      align-items: flex-start;
      gap: var(--sl-spacing-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
      border-radius: var(--sl-border-radius-small);
      font-size: var(--sl-font-size-small);
      transition: background 0.1s ease;
    }

    .rule-item:hover {
      background: var(--sl-color-neutral-100);
    }

    .rule-item.disabled-rule {
      opacity: 0.5;
    }

    .rule-item[draggable='true'] {
      cursor: grab;
    }

    .rule-item.dragging {
      opacity: 0.4;
      cursor: grabbing;
    }

    .rule-item.drag-over-top {
      border-top: 2px solid var(--sl-color-primary-500);
      padding-top: calc(var(--sl-spacing-x-small) - 2px);
    }

    .rule-item.drag-over-bottom {
      border-bottom: 2px solid var(--sl-color-primary-500);
      padding-bottom: calc(var(--sl-spacing-x-small) - 2px);
    }

    .drag-handle {
      color: var(--sl-color-neutral-400);
      cursor: grab;
      flex-shrink: 0;
      font-size: 0.85rem;
      padding-top: 2px;
    }

    .drag-handle:hover {
      color: var(--sl-color-neutral-600);
    }

    .rule-priority {
      font-size: var(--sl-font-size-x-small);
      font-weight: var(--sl-font-weight-semibold);
      color: var(--sl-color-neutral-500);
      min-width: 20px;
      text-align: center;
      padding-top: 2px;
    }

    .rule-action-icon {
      flex-shrink: 0;
      font-size: 1rem;
      padding-top: 1px;
    }

    .rule-action-icon.deny {
      color: var(--sl-color-danger-600);
    }

    .rule-action-icon.approval {
      color: var(--sl-color-primary-600);
    }

    .rule-action-icon.allow {
      color: var(--sl-color-success-600);
    }

    .rule-details {
      flex: 1;
      min-width: 0;
    }

    .rule-action-label {
      font-weight: var(--sl-font-weight-semibold);
      font-size: var(--sl-font-size-small);
    }

    .rule-action-label.deny {
      color: var(--sl-color-danger-700);
    }

    .rule-action-label.approval {
      color: var(--sl-color-primary-700);
    }

    .rule-action-label.allow {
      color: var(--sl-color-success-700);
    }

    .rule-condition {
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-600);
      font-family: var(--sl-font-mono);
      margin-top: 2px;
      word-break: break-all;
    }

    .rule-description {
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-500);
      font-style: italic;
      margin-top: 2px;
    }

    .rule-actions {
      display: flex;
      gap: var(--sl-spacing-2x-small);
      flex-shrink: 0;
    }

    .rules-footer {
      display: flex;
      justify-content: flex-start;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-small);
    }

    .empty-rules {
      text-align: center;
      padding: var(--sl-spacing-medium);
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-small);
      border: 1px dashed var(--sl-color-neutral-300);
      border-radius: var(--sl-border-radius-medium);
      background: var(--sl-color-neutral-50);
    }
  `;

  private _getActionIconName(action: string): string {
    switch (action) {
      case 'deny':
        return 'x-octagon-fill';
      case 'require_approval':
        return 'shield-lock-fill';
      case 'allow':
        return 'check-circle-fill';
      default:
        return 'question-circle';
    }
  }

  private _getActionColorClass(action: string): string {
    switch (action) {
      case 'deny':
        return 'deny';
      case 'require_approval':
        return 'approval';
      case 'allow':
        return 'allow';
      default:
        return '';
    }
  }

  private _getActionLabel(action: string): string {
    switch (action) {
      case 'deny':
        return 'DENY';
      case 'require_approval':
        return 'REQUIRE APPROVAL';
      case 'allow':
        return 'ALLOW';
      default:
        return action.toUpperCase();
    }
  }

  private _openRuleEditor(rule: AccessRuleSummary | null = null) {
    this._editingRule = rule as AccessRule | null;
    this._showRuleEditor = true;
  }

  private _closeRuleEditor() {
    this._showRuleEditor = false;
    this._editingRule = null;
  }

  private _handleSaveRule(e: CustomEvent) {
    e.stopPropagation();
    const { rule, formData } = e.detail as {
      rule: AccessRuleSummary | null;
      formData: RuleFormData;
    };
    this.dispatchEvent(
      new CustomEvent('save-rule', {
        detail: {
          toolName: this.toolName,
          existingRule: rule,
          formData,
        },
        bubbles: true,
        composed: true,
      })
    );
    this._closeRuleEditor();
  }

  private _handleWorkflowCreated() {
    this.dispatchEvent(
      new CustomEvent('workflow-created', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private _handleDeleteRule(rule: AccessRuleSummary) {
    this.dispatchEvent(
      new CustomEvent('delete-rule', {
        detail: {
          toolName: this.toolName,
          rule,
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _handleDragStart(index: number, e: DragEvent) {
    this._dragIndex = index;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', index.toString());
    }
  }

  private _handleDragOver(index: number, e: DragEvent) {
    e.preventDefault();
    if (this._dragIndex === null || this._dragIndex === index) {
      return;
    }
    this._dragOverIndex = index;
    if (e.dataTransfer) {
      e.dataTransfer.dropEffect = 'move';
    }
  }

  private _handleDragLeave() {
    this._dragOverIndex = null;
  }

  private _handleDrop(index: number, e: DragEvent) {
    e.preventDefault();
    if (this._dragIndex === null || this._dragIndex === index) {
      this._dragIndex = null;
      this._dragOverIndex = null;
      return;
    }

    const sortedRules = [...this.rules].sort((a, b) => a.priority - b.priority);
    const draggedRule = sortedRules[this._dragIndex];
    if (!draggedRule) {
      this._dragIndex = null;
      this._dragOverIndex = null;
      return;
    }

    const newRules = [...sortedRules];
    newRules.splice(this._dragIndex, 1);
    newRules.splice(index, 0, draggedRule);

    const reorderedRules = newRules.map((rule, newIndex) => ({
      id: rule.id,
      priority: newIndex,
    }));

    this.dispatchEvent(
      new CustomEvent('reorder-rules', {
        detail: {
          toolName: this.toolName,
          reorderedRules,
        },
        bubbles: true,
        composed: true,
      })
    );

    this._dragIndex = null;
    this._dragOverIndex = null;
  }

  private _handleDragEnd() {
    this._dragIndex = null;
    this._dragOverIndex = null;
  }

  private _renderRuleItem(rule: AccessRuleSummary, index: number) {
    const workflow = rule.approval_workflow_id
      ? this.workflows.find((p) => p.id === rule.approval_workflow_id)
      : null;
    const colorClass = this._getActionColorClass(rule.action);
    const isDragging = this._dragIndex === index;
    const isDragOver = this._dragOverIndex === index;
    const dragPosition =
      isDragOver && this._dragIndex !== null
        ? index > this._dragIndex
          ? 'drag-over-bottom'
          : 'drag-over-top'
        : '';

    return html`
      <div
        class="rule-item ${!rule.is_enabled ? 'disabled-rule' : ''} ${
          isDragging ? 'dragging' : ''
        } ${dragPosition}"
        draggable="true"
        @dragstart=${(e: DragEvent) => this._handleDragStart(index, e)}
        @dragover=${(e: DragEvent) => this._handleDragOver(index, e)}
        @dragleave=${() => this._handleDragLeave()}
        @drop=${(e: DragEvent) => this._handleDrop(index, e)}
        @dragend=${() => this._handleDragEnd()}
      >
        <sl-icon class="drag-handle" name="grip-vertical"></sl-icon>
        <span class="rule-priority">${index + 1}.</span>
        <sl-icon
          class="rule-action-icon ${colorClass}"
          name=${this._getActionIconName(rule.action)}
        ></sl-icon>
        <div class="rule-details">
          <span class="rule-action-label ${colorClass}">
            ${this._getActionLabel(rule.action)}${
              rule.action === 'require_approval' && workflow
                ? html` <span
                    style="font-weight: normal; font-size: var(--sl-font-size-x-small);"
                    >(${workflow.name})</span
                  >`
                : ''
            }
          </span>
          ${
            rule.condition_expression
              ? html`<div class="rule-condition">
                  when ${rule.condition_expression}
                </div>`
              : html`<div class="rule-condition">(always)</div>`
          }
          ${
            rule.description
              ? html`<div class="rule-description">
                  ${rule.action === 'deny' ? '\u2192 ' : ''}${rule.description}
                </div>`
              : ''
          }
        </div>
        <div class="rule-actions">
          <sl-tooltip content="Edit rule">
            <sl-icon-button
              name="pencil"
              @click=${() => this._openRuleEditor(rule)}
            ></sl-icon-button>
          </sl-tooltip>
          <sl-tooltip content="Delete rule">
            <sl-icon-button
              name="trash"
              @click=${() => this._handleDeleteRule(rule)}
            ></sl-icon-button>
          </sl-tooltip>
        </div>
      </div>
    `;
  }

  render() {
    const sortedRules = [...this.rules].sort((a, b) => a.priority - b.priority);

    return html`
      <div class="rules-list">
        ${
          sortedRules.length === 0
            ? html`<div class="empty-rules">${this.emptyMessage}</div>`
            : sortedRules.map((rule, index) =>
                this._renderRuleItem(rule, index)
              )
        }
      </div>

      <div class="rules-footer">
        <sl-button
          size="small"
          variant="default"
          @click=${() => this._openRuleEditor(null)}
        >
          <sl-icon slot="prefix" name="plus-lg"></sl-icon>
          ${this.addButtonLabel}
        </sl-button>
      </div>

      <tool-rule-editor
        ?open=${this._showRuleEditor}
        .rule=${this._editingRule}
        .toolName=${this.toolName}
        .workflows=${this.workflows}
        .features=${this.features}
        .toolSchema=${this.toolSchema}
        @save-rule=${this._handleSaveRule}
        @workflow-created=${this._handleWorkflowCreated}
        @close=${this._closeRuleEditor}
      ></tool-rule-editor>
    `;
  }
}
