import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import './governance-rule-set-editor';
import type { Tool, ApprovalWorkflow } from './tool-card';
import type { AccessRuleSummary } from './governance-rule-set-editor';
import type { GatewayUsageByTool } from '../types';
import { consoleDialogStyles } from '../styles/console-dialog';

@customElement('tool-list-item')
export class ToolListItem extends LitElement {
  @property({ type: Object }) tool!: Tool;
  @property({ type: Array }) accessRules: AccessRuleSummary[] = [];
  @property({ type: Array }) policies: ApprovalWorkflow[] = [];
  @property({ type: Object }) features: { [key: string]: boolean | string[] } =
    {};
  @property({ type: Boolean }) expanded = false;
  @property({ type: String }) mode: 'global' | 'scoped' = 'global';
  @property({ type: Boolean }) rulesInherited = false;
  @property({ type: Object }) usageStat: GatewayUsageByTool | null = null;
  /**
   * Account default for native tool calls ("Ask a human before running").
   * A native row with no rules of its own is governed by this, so the row
   * has to say so instead of claiming every call is allowed.
   */
  @property({ type: Boolean }) accountAsksByDefault = false;

  @state() private _showJustificationDialog = false;
  @state() private _justificationMode: string = 'disabled';

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: block;
      }

      .tool-row {
        border-radius: var(--sl-border-radius-medium);
        overflow: hidden;
        transition: background 0.15s ease;
      }

      .tool-row.expanded {
        background: var(--sl-color-neutral-50);
      }

      .tool-row.disabled {
        opacity: 0.65;
      }

      .tool-header {
        display: flex;
        align-items: center;
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-medium);
        cursor: pointer;
        user-select: none;
        gap: var(--sl-spacing-small);
        min-height: 36px;
      }

      .tool-header:hover {
        background: var(--sl-color-neutral-50);
      }

      .expand-icon {
        color: var(--sl-color-neutral-500);
        transition: transform 0.2s ease;
        flex-shrink: 0;
      }

      .expand-icon.open {
        transform: rotate(90deg);
      }

      .tool-name {
        font-weight: var(--sl-font-weight-semibold);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-900);
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .tool-description {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-500);
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
      }

      .tool-badges {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        flex-shrink: 0;
      }

      .usage-stat {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        white-space: nowrap;
        flex-shrink: 0;
      }

      .schema-tokens {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        white-space: nowrap;
        flex-shrink: 0;
        font-variant-numeric: tabular-nums;
      }

      .rule-summary {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        flex-shrink: 0;
        font-size: var(--sl-font-size-x-small);
      }

      .rule-summary .rule-count {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        padding: 2px 8px;
        border-radius: var(--sl-border-radius-pill);
        font-weight: 500;
      }

      .rule-count.deny {
        background: var(--sl-color-danger-100);
        color: var(--sl-color-danger-700);
      }

      .rule-count.approval {
        background: var(--sl-color-primary-100);
        color: var(--sl-color-primary-700);
      }

      .rule-count.allow {
        background: var(--sl-color-success-100);
        color: var(--sl-color-success-700);
      }

      .no-rules {
        color: var(--sl-color-neutral-400);
        font-size: var(--sl-font-size-x-small);
      }

      .tool-toggle {
        display: flex;
        align-items: center;
        gap: 6px;
        flex-shrink: 0;
        margin-top: -3px;
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
      }

      /* Expanded content */
      .tool-content {
        padding: var(--sl-spacing-small) var(--sl-spacing-medium)
          var(--sl-spacing-medium);
      }

      .unsupported-overlay {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        font-style: italic;
      }

      /* Phone: the row header is one non-wrapping flex line, so the agent
         tags squeezed the tool name out of a 390px row (B-T4). The name
         keeps its content width and everything else wraps beneath it. */
      @media (max-width: 480px) {
        .tool-header {
          flex-wrap: wrap;
          row-gap: var(--sl-spacing-2x-small);
        }
        .expand-icon {
          order: 0;
        }
        .tool-name {
          order: 1;
          flex: 0 0 auto;
          max-width: calc(100% - 120px);
        }
        .tool-toggle {
          order: 2;
          margin-left: auto;
        }
        /* MCP rows carry a three-dots menu after the toggle. Without an
           order it would default to 0 and sort ahead of the name. */
        .tool-menu {
          order: 2;
        }
        .tool-badges {
          order: 3;
          flex-basis: 100%;
          flex-wrap: wrap;
        }
        .tool-description {
          order: 4;
          flex-basis: 100%;
          white-space: normal;
        }
        .rule-summary {
          order: 5;
          flex-basis: 100%;
          flex-wrap: wrap;
        }
      }
    `,
  ];

  private _getRuleSummary() {
    const rules = this.accessRules.filter((r) => r.is_enabled);
    const deny = rules.filter((r) => r.action === 'deny').length;
    const approval = rules.filter(
      (r) => r.action === 'require_approval'
    ).length;
    const allow = rules.filter((r) => r.action === 'allow').length;
    return { deny, approval, allow, total: rules.length };
  }

  private _toggleExpanded() {
    this.dispatchEvent(
      new CustomEvent('toggle-expand', {
        detail: { tool: this.tool },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _isNativeTool(): boolean {
    return this.tool.source === 'agent';
  }

  private _toolSchema(): Record<string, unknown> | null {
    const schema = this.tool.schema;
    if (schema && typeof schema === 'object' && schema.properties) {
      return schema;
    }
    if (this.tool.parameters) {
      return { type: 'object', properties: this.tool.parameters };
    }
    return schema || null;
  }

  private _handleToggleEnabled(e: Event) {
    e.stopPropagation();
    const checked = (e.target as HTMLInputElement).checked;
    // Native rows use a Blocked switch (inverse of is_enabled).
    const isEnabled = this._isNativeTool() ? !checked : checked;
    this.dispatchEvent(
      new CustomEvent('toggle-enabled', {
        detail: { tool: this.tool, isEnabled },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _revertToGlobal() {
    this.dispatchEvent(
      new CustomEvent('revert-tool', {
        detail: { tool: this.tool },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _handleSaveRule(e: CustomEvent) {
    e.stopPropagation();
    const { existingRule, formData } = e.detail;
    this.dispatchEvent(
      new CustomEvent('save-rule', {
        detail: {
          tool: this.tool,
          existingRule,
          formData,
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _handleWorkflowCreated() {
    // Bubble up to tools-view to refresh the policies list
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
          tool: this.tool,
          rule,
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  /**
   * What happens to a call when the tool carries no rule of its own. Native
   * tools fall through to the account default, so "No rules" alone (or
   * "allow all") states the wrong policy whenever that default is on.
   */
  private _noRulesEffect(): string {
    if (!this.tool.is_enabled) {
      return 'blocked';
    }
    if (this._isNativeTool() && this.accountAsksByDefault) {
      return 'asks a human (account default)';
    }
    return 'allowed';
  }

  private _renderRuleSummaryBadges() {
    const summary = this._getRuleSummary();

    if (summary.total === 0) {
      if (this._isNativeTool()) {
        return html`<span class="no-rules"
          >No rules · ${this._noRulesEffect()}</span
        >`;
      }
      if (this.tool.is_enabled) {
        return html`<span class="no-rules">No rules (allow all)</span>`;
      }
      return html`<span class="no-rules">No rules</span>`;
    }

    return html`
      ${
        summary.deny > 0
          ? html`<span class="rule-count deny"
              ><sl-icon
                name="x-octagon-fill"
                style="font-size: 0.8em;"
              ></sl-icon>
              ${summary.deny} deny</span
            >`
          : ''
      }
      ${
        summary.approval > 0
          ? html`<span class="rule-count approval"
              ><sl-icon
                name="shield-lock-fill"
                style="font-size: 0.8em;"
              ></sl-icon>
              ${summary.approval} approval</span
            >`
          : ''
      }
      ${
        summary.allow > 0
          ? html`<span class="rule-count allow"
              ><sl-icon
                name="check-circle-fill"
                style="font-size: 0.8em;"
              ></sl-icon>
              ${summary.allow} allow</span
            >`
          : ''
      }
    `;
  }

  private _openJustificationDialog() {
    this._justificationMode = this.tool.justification_mode || 'disabled';
    this._showJustificationDialog = true;
  }

  private async _saveJustificationMode() {
    try {
      const { updateToolConfiguration, createToolConfiguration } =
        await import('../api');
      let configId = this.tool.config_id;
      if (!configId) {
        await createToolConfiguration({
          tool_name: this.tool.name,
          tool_source: this.tool.source || 'builtin',
          is_enabled: this.tool.is_enabled !== false,
          justification_mode:
            this._justificationMode === 'disabled'
              ? null
              : this._justificationMode,
        });
      } else {
        await updateToolConfiguration(configId, {
          justification_mode:
            this._justificationMode === 'disabled'
              ? null
              : this._justificationMode,
        });
      }
      this._showJustificationDialog = false;
      this.dispatchEvent(
        new CustomEvent('tool-updated', { bubbles: true, composed: true })
      );
    } catch (error) {
      console.error('Failed to save justification mode:', error);
    }
  }

  private _emptyRulesMessage(): string {
    if (!this.tool.is_enabled) {
      return 'No access rules configured. All calls to this tool are blocked (tool disabled).';
    }
    if (this._isNativeTool() && this.accountAsksByDefault) {
      return 'No access rules configured. Calls to this tool ask a human first, from the account default.';
    }
    return 'No access rules configured. All calls to this tool are allowed.';
  }

  private _renderExpandedContent() {
    return html`
      <div class="tool-content">
        ${
          this.mode === 'scoped'
            ? this.rulesInherited
              ? html`
                  <sl-alert
                    variant="primary"
                    open
                    style="margin-bottom: var(--sl-spacing-medium);"
                  >
                    <sl-icon slot="icon" name="info-circle"></sl-icon>
                    <strong>Inherited Configuration</strong><br />
                    These rules are inherited from the global API Catalog.
                    Saving any changes or toggling this tool will create an
                    override specific to this agent.
                  </sl-alert>
                `
              : html`
                  <sl-button
                    variant="warning"
                    outline
                    size="small"
                    @click=${this._revertToGlobal}
                    style="margin-bottom: var(--sl-spacing-medium);"
                  >
                    <sl-icon
                      slot="prefix"
                      name="arrow-counterclockwise"
                    ></sl-icon>
                    Restore global settings
                  </sl-button>
                `
            : ''
        }
        ${
          this.tool.description
            ? html`<div
                style="font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-600); margin-bottom: var(--sl-spacing-small);"
              >
                ${this.tool.description}
              </div>`
            : ''
        }

        <governance-rule-set-editor
          .toolName=${this.tool.name}
          .toolSchema=${this._toolSchema()}
          .rules=${this.accessRules}
          .workflows=${this.policies}
          .features=${this.features}
          .emptyMessage=${this._emptyRulesMessage()}
          @save-rule=${this._handleSaveRule}
          @delete-rule=${(event: CustomEvent) =>
            this._handleDeleteRule(event.detail.rule)}
          @reorder-rules=${(event: CustomEvent) =>
            this.dispatchEvent(
              new CustomEvent('reorder-rules', {
                detail: {
                  tool: this.tool,
                  reorderedRules: event.detail.reorderedRules,
                },
                bubbles: true,
                composed: true,
              })
            )}
          @workflow-created=${this._handleWorkflowCreated}
        ></governance-rule-set-editor>
      </div>
    `;
  }

  render() {
    const isUnsupported = this.tool.is_supported === false;

    return html`
      <div
        class="tool-row ${this.expanded ? 'expanded' : ''} ${
          !this.tool.is_enabled ? 'disabled' : ''
        }"
      >
        <div class="tool-header" @click=${this._toggleExpanded}>
          <sl-icon
            class="expand-icon ${this.expanded ? 'open' : ''}"
            name="chevron-right"
          ></sl-icon>

          <span class="tool-name">${this.tool.name}</span>

          <div class="tool-badges">
            ${
              this._isNativeTool()
                ? (this.tool.adapters || []).map(
                    (adapter) =>
                      html`<sl-badge variant="neutral" pill
                        >${adapter}</sl-badge
                      >`
                  )
                : ''
            }
            ${
              typeof this.tool.schema_tokens_estimate === 'number' &&
              this.tool.schema_tokens_estimate > 0
                ? html`<sl-tooltip
                    content="Estimated schema tokens added to every agent request that advertises this tool (includes justification parameters when configured)"
                  >
                    <span class="schema-tokens"
                      >~${this.tool.schema_tokens_estimate.toLocaleString()}
                      tokens/request</span
                    >
                  </sl-tooltip>`
                : ''
            }
            ${
              this.usageStat &&
              (this.usageStat.invocation_count > 0 ||
                this.usageStat.estimated_schema_cost > 0)
                ? html`<sl-tooltip
                    content=${`${this.usageStat.invocation_count} invocations · ${this.usageStat.estimated_schema_cost.toFixed(4)} schema cost (30d)`}
                  >
                    <span class="usage-stat"
                      >${this.usageStat.invocation_count} calls ·
                      $${this.usageStat.estimated_schema_cost >= 0.01 ? this.usageStat.estimated_schema_cost.toFixed(2) : this.usageStat.estimated_schema_cost.toFixed(4)}</span
                    >
                  </sl-tooltip>`
                : ''
            }
            ${
              isUnsupported
                ? html`<sl-tooltip
                    content=${
                      this.tool.unsupported_reason ||
                      'This tool is currently unavailable'
                    }
                  >
                    <sl-badge variant="neutral" pill>Unavailable</sl-badge>
                  </sl-tooltip>`
                : ''
            }
          </div>

          <span class="tool-description">${this.tool.description}</span>

          <div class="rule-summary">
            ${
              this.rulesInherited
                ? html`<sl-badge
                    variant="neutral"
                    style="font-size: 0.7em; margin-right: 4px;"
                    >Inherited</sl-badge
                  >`
                : ''
            }
            ${this._renderRuleSummaryBadges()}
          </div>

          <div class="tool-toggle" @click=${(e: Event) => e.stopPropagation()}>
            <sl-switch
              size="small"
              ?checked=${
                this._isNativeTool()
                  ? !this.tool.is_enabled
                  : this.tool.is_enabled
              }
              ?disabled=${isUnsupported}
              @sl-change=${this._handleToggleEnabled}
              >${this._isNativeTool() ? 'Block' : ''}</sl-switch
            >
          </div>

          ${
            this._isNativeTool()
              ? ''
              : html`
                  <div
                    class="tool-menu"
                    @click=${(e: Event) => e.stopPropagation()}
                  >
                    <sl-dropdown>
                      <sl-icon-button
                        slot="trigger"
                        name="three-dots-vertical"
                        label="Tool settings"
                        style="font-size: 1.2rem;"
                      ></sl-icon-button>
                      <sl-menu>
                        <sl-menu-item
                          @click=${() => this._openJustificationDialog()}
                        >
                          <sl-icon slot="prefix" name="shield-shaded"></sl-icon>
                          Justification settings
                        </sl-menu-item>
                      </sl-menu>
                    </sl-dropdown>
                  </div>
                `
          }
        </div>

        ${this.expanded ? this._renderExpandedContent() : ''}
      </div>

      <sl-dialog
        label="Justification Settings"
        ?open=${this._showJustificationDialog}
        @sl-after-hide=${() => {
          this._showJustificationDialog = false;
        }}
      >
        <p style="margin-top: 0; color: var(--sl-color-neutral-600);">
          Configure whether agents must provide justification when calling this
          tool. Justification is used for auditing and approval workflows.
        </p>
        <sl-radio-group
          label="Justification requirement"
          value=${this._justificationMode}
          @sl-change=${(e: Event) => {
            this._justificationMode = (e.target as any).value;
          }}
        >
          <sl-radio value="disabled">Disabled</sl-radio>
          <sl-radio value="optional">Optional</sl-radio>
          <sl-radio value="required">Required</sl-radio>
        </sl-radio-group>
        <div slot="footer">
          <sl-button
            variant="primary"
            @click=${() => this._saveJustificationMode()}
          >
            Save
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }
}
