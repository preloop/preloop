import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import { consoleDialogStyles } from '../styles/console-dialog';

export interface MCPServer {
  id: string;
  name: string;
  url: string;
  transport: string;
  auth_type: string;
  status: string;
  created_at: string;
  updated_at: string;
  tool_count?: number;
}

@customElement('mcp-server-card')
export class MCPServerCard extends LitElement {
  @property({ type: Object })
  server?: MCPServer;

  @state()
  private isConfirmingDelete = false;

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      .server-card {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      .card-content {
        padding-bottom: var(--sl-spacing-small);
      }

      .server-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--sl-spacing-small);
      }

      .server-name {
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
        margin: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .server-url {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-600);
        margin: 0 0 var(--sl-spacing-x-small) 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-family: monospace;
      }

      .server-meta {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-700);
        margin-top: var(--sl-spacing-2x-small);
      }

      sl-card::part(footer) {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        padding: var(--sl-spacing-medium);
        border-top: 1px solid var(--sl-color-neutral-200);
      }

      .footer-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
      }

      .footer-actions {
        display: flex;
        gap: var(--sl-spacing-x-small);
        flex: 1;
      }

      sl-button {
        flex: 1;
      }

      .enable-control {
        display: flex;
        align-items: center;
        justify-content: space-between;
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
        margin-bottom: var(--sl-spacing-medium);
        padding-top: var(--sl-spacing-2x-small);
      }

      sl-card {
        height: 100%;
      }
    `,
  ];

  private requestDeleteConfirmation() {
    this.isConfirmingDelete = true;
  }

  private cancelDelete() {
    this.isConfirmingDelete = false;
  }

  private confirmDelete() {
    if (!this.server) return;
    this.dispatchEvent(
      new CustomEvent('server-deleted', {
        detail: { id: this.server.id },
        bubbles: true,
        composed: true,
      })
    );
    this.isConfirmingDelete = false;
  }

  private handleEdit() {
    if (!this.server) return;
    this.dispatchEvent(
      new CustomEvent('server-edit', {
        detail: { server: this.server },
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleScan() {
    if (!this.server) return;
    this.dispatchEvent(
      new CustomEvent('server-scan', {
        detail: { id: this.server.id },
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleToggleEnabled() {
    if (!this.server) return;
    this.dispatchEvent(
      new CustomEvent('server-toggle-enabled', {
        detail: {
          id: this.server.id,
          enabled: this.server.status !== 'active',
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    if (!this.server) {
      return html``;
    }

    return html`
      <sl-dialog
        label="Confirm Deletion"
        ?open=${this.isConfirmingDelete}
        @sl-hide=${this.cancelDelete}
      >
        Are you sure you want to delete the MCP server "${this.server?.name}"?
        This will also remove all discovered tools from this server.
        <sl-button slot="footer" @click=${this.cancelDelete}>Cancel</sl-button>
        <sl-button slot="footer" variant="danger" @click=${this.confirmDelete}>
          Delete
        </sl-button>
      </sl-dialog>

      <sl-card class="server-card">
        <div class="card-content">
          <div class="server-header">
            <h3 class="server-name" title=${this.server.name}>
              ${this.server.name}
            </h3>
            <sl-badge
              variant=${this.server.status === 'active' ? 'success' : 'danger'}
              size="small"
            >
              ${this.server.status}
            </sl-badge>
          </div>
          <p class="server-url" title=${this.server.url}>${this.server.url}</p>
          <div class="server-meta">
            ${
              this.server.tool_count !== undefined
                ? `${this.server.tool_count} tool${this.server.tool_count !== 1 ? 's' : ''}`
                : 'No tools discovered'
            }
          </div>
        </div>
        <div slot="footer">
          <div class="enable-control">
            <span>Enabled</span>
            <sl-switch
              ?checked=${this.server.status === 'active'}
              @sl-change=${this.handleToggleEnabled}
            ></sl-switch>
          </div>
          <div class="footer-actions">
            <sl-button size="small" @click=${this.handleScan}>
              Refresh
            </sl-button>
            <sl-button size="small" @click=${this.handleEdit}> Edit </sl-button>
            <sl-button
              size="small"
              variant="danger"
              @click=${this.requestDeleteConfirmation}
            >
              Delete
            </sl-button>
          </div>
        </div>
      </sl-card>
    `;
  }
}
