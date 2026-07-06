import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import type { ToolOutputFilter } from '../types';
import {
  createToolOutputFilter,
  deleteToolOutputFilter,
  listToolOutputFilters,
} from '../api';

/**
 * Dialog for managing tool output filters. Opened from the session
 * optimization panel for a ``manage_output_filter`` suggestion. The dialog is
 * prefilled with a single tool's name + suggested droppable fields and lets the
 * user choose which fields to drop. It also lists existing filters with a
 * remove control.
 *
 * The host component controls visibility via the ``open`` property and listens
 * for the ``filter-saved`` / ``sl-after-hide`` events. The dialog performs its
 * own API calls (create / list / delete) so the host only needs to supply the
 * prefill context.
 */
@customElement('tool-output-filter-dialog')
export class ToolOutputFilterDialog extends LitElement {
  /** Controls dialog visibility. */
  @property({ type: Boolean })
  open = false;

  /** MCP server name the tool belongs to (null when unknown / payload tool). */
  @property({ type: String })
  serverName: string | null = null;

  /** Tool whose output should be filtered. */
  @property({ type: String })
  toolName = '';

  /** Fields suggested for dropping; all pre-checked by default. */
  @property({ type: Array })
  suggestedFields: string[] = [];

  /** Optional managed agent scope; null applies the filter account-wide. */
  @property({ type: String })
  managedAgentId: string | null = null;

  @state()
  private checkedFields: Record<string, boolean> = {};

  @state()
  private existingFilters: ToolOutputFilter[] = [];

  @state()
  private saving = false;

  @state()
  private loadingFilters = false;

  @state()
  private removingId: string | null = null;

  @state()
  private errorMessage: string | null = null;

  static styles = css`
    :host {
      display: contents;
    }

    .field-list {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
      margin-top: var(--sl-spacing-x-small);
    }

    .section-title {
      color: var(--sl-color-neutral-900);
      font-weight: 600;
      margin-top: var(--sl-spacing-medium);
    }

    .hint {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      line-height: 1.45;
      margin-top: var(--sl-spacing-2x-small);
    }

    .scope {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
    }

    .existing {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
      margin-top: var(--sl-spacing-x-small);
    }

    .existing-item {
      align-items: center;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    }

    .existing-meta {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-x-small);
    }

    .existing-fields {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-2x-small);
      margin-top: var(--sl-spacing-2x-small);
    }

    .empty {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }

    .error {
      color: var(--sl-color-danger-700);
      font-size: var(--sl-font-size-small);
      margin-top: var(--sl-spacing-x-small);
    }
  `;

  willUpdate(changed: Map<string, unknown>): void {
    // Re-seed the checklist whenever the dialog is (re)opened or the suggested
    // fields change, so the prefill always reflects the current suggestion.
    if (changed.has('open') && this.open) {
      this.seedCheckedFields();
      this.errorMessage = null;
      void this.loadExistingFilters();
    } else if (changed.has('suggestedFields') && this.open) {
      this.seedCheckedFields();
    }
  }

  private seedCheckedFields(): void {
    const next: Record<string, boolean> = {};
    for (const field of this.suggestedFields) {
      next[field] = true;
    }
    this.checkedFields = next;
  }

  private async loadExistingFilters(): Promise<void> {
    this.loadingFilters = true;
    try {
      this.existingFilters = await listToolOutputFilters();
      this.errorMessage = null;
    } catch (error) {
      console.error('Failed to load tool output filters:', error);
      this.existingFilters = [];
      this.errorMessage =
        'Could not load existing filters. You can still create a new one.';
    } finally {
      this.loadingFilters = false;
    }
  }

  private getCheckedFields(): string[] {
    return this.suggestedFields.filter((field) => this.checkedFields[field]);
  }

  private toggleField(field: string, checked: boolean): void {
    this.checkedFields = { ...this.checkedFields, [field]: checked };
  }

  private close(): void {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('dialog-closed', { bubbles: true, composed: true })
    );
  }

  private async confirm(): Promise<void> {
    const droppedFields = this.getCheckedFields();
    if (!this.toolName || droppedFields.length === 0) {
      this.errorMessage = 'Select at least one field to drop.';
      return;
    }
    this.saving = true;
    this.errorMessage = null;
    try {
      const filter = await createToolOutputFilter({
        server_name: this.serverName ?? null,
        tool_name: this.toolName,
        dropped_fields: droppedFields,
        managed_agent_id: this.managedAgentId ?? null,
      });
      this.dispatchEvent(
        new CustomEvent('filter-saved', {
          detail: { filter },
          bubbles: true,
          composed: true,
        })
      );
      await this.loadExistingFilters();
    } catch (error) {
      this.errorMessage =
        error instanceof Error
          ? error.message
          : 'Failed to create tool output filter';
    } finally {
      this.saving = false;
    }
  }

  private async removeFilter(id: string): Promise<void> {
    this.removingId = id;
    try {
      await deleteToolOutputFilter(id);
      this.existingFilters = this.existingFilters.filter(
        (filter) => filter.id !== id
      );
      this.dispatchEvent(
        new CustomEvent('filter-removed', {
          detail: { id },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.errorMessage =
        error instanceof Error
          ? error.message
          : 'Failed to delete tool output filter';
    } finally {
      this.removingId = null;
    }
  }

  private renderExistingFilters() {
    if (this.loadingFilters) {
      return html`<div class="empty"><sl-spinner></sl-spinner> Loading…</div>`;
    }
    if (!this.existingFilters.length) {
      return html`<div class="empty">No output filters configured yet.</div>`;
    }
    return html`
      <div class="existing">
        ${this.existingFilters.map((filter) => {
          const label = filter.server_name
            ? `${filter.server_name} · ${filter.tool_name}`
            : filter.tool_name;
          return html`
            <div class="existing-item">
              <div>
                <div>${label}</div>
                <div class="existing-meta">
                  ${
                    filter.managed_agent_id
                      ? 'Scoped to one agent'
                      : 'Account-wide'
                  }
                  ${filter.enabled ? '' : ' · disabled'}
                </div>
                <div class="existing-fields">
                  ${filter.dropped_fields.map(
                    (field) =>
                      html`<sl-tag size="small" variant="neutral"
                        >${field}</sl-tag
                      >`
                  )}
                </div>
              </div>
              <sl-icon-button
                name="trash"
                label="Remove filter"
                ?disabled=${this.removingId === filter.id}
                @click=${() => this.removeFilter(filter.id)}
              ></sl-icon-button>
            </div>
          `;
        })}
      </div>
    `;
  }

  render() {
    const scopeLabel = this.serverName
      ? `${this.serverName} · ${this.toolName}`
      : this.toolName;
    const checkedCount = this.getCheckedFields().length;
    return html`
      <sl-dialog
        label="Filter tool output"
        ?open=${this.open}
        @sl-after-hide=${this.close}
      >
        <div class="scope">
          Drop noisy fields from
          <strong>${scopeLabel || 'this tool'}</strong>'s output
          ${this.managedAgentId ? ' for this agent.' : ' across this account.'}
        </div>
        <div class="hint">
          Filtered fields are stripped from the tool's response before it enters
          the model's context window — keeping the useful fields the agent
          actually reads.
        </div>

        <div class="section-title">Fields to drop</div>
        ${
          this.suggestedFields.length
            ? html`
                <div class="field-list">
                  ${this.suggestedFields.map(
                    (field) => html`
                      <sl-checkbox
                        ?checked=${Boolean(this.checkedFields[field])}
                        @sl-change=${(e: Event) =>
                          this.toggleField(
                            field,
                            (e.target as HTMLInputElement).checked
                          )}
                      >
                        ${field}
                      </sl-checkbox>
                    `
                  )}
                </div>
              `
            : html`<div class="empty">
                No fields were suggested for this tool.
              </div>`
        }
        ${
          this.errorMessage
            ? html`<div class="error">${this.errorMessage}</div>`
            : nothing
        }

        <sl-divider></sl-divider>
        <div class="section-title">Existing filters</div>
        ${this.renderExistingFilters()}

        <sl-button slot="footer" size="small" @click=${this.close}>
          Close
        </sl-button>
        <sl-button
          slot="footer"
          size="small"
          variant="primary"
          ?loading=${this.saving}
          ?disabled=${this.saving || checkedCount === 0}
          @click=${this.confirm}
        >
          <sl-icon slot="prefix" name="funnel"></sl-icon>
          Drop ${checkedCount} field${checkedCount === 1 ? '' : 's'}
        </sl-button>
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'tool-output-filter-dialog': ToolOutputFilterDialog;
  }
}
