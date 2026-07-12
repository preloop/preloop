import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import * as api from '../api';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';

@customElement('mcp-server-form')
export class MCPServerForm extends LitElement {
  @property({ type: Object })
  server: any = null;

  /**
   * @internal
   */
  _api = api;

  @property({ type: Boolean })
  opened = true;

  @state()
  private serverName = '';

  @state()
  private serverUrl = '';

  @state()
  private transport = 'http-streaming';

  @state()
  private authType = 'none';

  @state()
  private bearerToken = '';

  @state()
  private isLoading = false;

  @state()
  private errorMessage = '';

  static styles = css`
    .error {
      color: var(--sl-color-danger-700);
      margin-top: 1rem;
    }
    sl-input,
    sl-select,
    sl-textarea {
      margin-bottom: 1rem;
    }
    .help-text {
      font-size: 0.875rem;
      color: var(--sl-color-neutral-600);
      margin-top: 0.25rem;
      margin-bottom: 1rem;
    }
    sl-textarea::part(base) {
      min-height: 120px;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    if (this.server) {
      this.serverName = this.server.name;
      this.serverUrl = this.server.url;
      this.transport = this.server.transport || 'http-streaming';
      this.authType = this.server.auth_type || 'none';
      this.bearerToken = this.server.auth_config?.token || '';
    }
  }

  updated(changedProperties: Map<string, unknown>) {
    if (changedProperties.has('opened') && this.opened) {
      requestAnimationFrame(() => {
        const input = this.shadowRoot?.querySelector<SlInput>('sl-input');
        input?.focus();
      });
    }
  }

  render() {
    return html`
      <sl-dialog
        label="${this.server ? 'Edit' : 'Add'} MCP Server"
        ?open=${this.opened}
        @sl-request-close=${() => this.closeModal()}
      >
        <sl-input
          label="Server Name"
          name="name"
          .value=${this.serverName}
          @sl-input=${(e: any) => (this.serverName = e.target.value)}
          required
          placeholder="e.g., My MCP Server"
        ></sl-input>

        <sl-input
          label="Server URL"
          name="url"
          .value=${this.serverUrl}
          @sl-input=${(e: any) => (this.serverUrl = e.target.value)}
          required
          placeholder="e.g., http://localhost:8001"
        ></sl-input>
        <div class="help-text">
          Enter the base URL of your MCP server (e.g., http://localhost:8001)<br />
          Phase 1B supports HTTP Streaming transport only.
        </div>

        <sl-input
          label="Transport"
          name="transport"
          .value=${'http-streaming'}
          disabled
          readonly
        ></sl-input>
        <div class="help-text">
          Only HTTP Streaming (streamable-http) transport is currently
          supported.
        </div>

        <sl-select
          label="Authentication Type"
          name="auth_type"
          .value=${this.authType}
          @sl-change=${(e: any) => (this.authType = e.target.value)}
        >
          <sl-option value="none">None</sl-option>
          <sl-option value="bearer">Bearer Token</sl-option>
          <sl-option value="oauth">OAuth 2.0</sl-option>
        </sl-select>

        ${
          this.authType === 'bearer'
            ? html`
                <sl-input
                  label="Bearer Token"
                  name="bearer_token"
                  type="password"
                  .value=${this.bearerToken}
                  @sl-input=${(e: any) => (this.bearerToken = e.target.value)}
                  placeholder="Enter your bearer token"
                  password-toggle
                ></sl-input>
                <div class="help-text">
                  Enter the bearer token required to authenticate with this MCP
                  server
                </div>
              `
            : ''
        }
        ${
          this.authType === 'oauth'
            ? html`
                <div class="help-text">
                  OAuth credentials will be obtained automatically via the MCP
                  standard discovery flow. After saving, click "Connect OAuth
                  Account" to authorize.
                </div>
                ${
                  this.server
                    ? html`
                        <sl-button
                          variant="primary"
                          outline
                          @click=${() => this._startOAuthFlow()}
                        >
                          <sl-icon
                            slot="prefix"
                            name="box-arrow-up-right"
                          ></sl-icon>
                          Connect OAuth Account
                        </sl-button>
                      `
                    : ''
                }
              `
            : ''
        }
        ${
          this.errorMessage
            ? html`<p class="error">${this.errorMessage}</p>`
            : ''
        }

        <div slot="footer">
          <sl-button @click=${() => this.closeModal()}>Cancel</sl-button>
          <sl-button
            variant="primary"
            @click=${this.handleSave}
            .loading=${this.isLoading}
          >
            ${this.server ? 'Save' : 'Add'}
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }

  async handleSave() {
    this.isLoading = true;
    this.errorMessage = '';

    // Validate required fields
    if (!this.serverName.trim()) {
      this.errorMessage = 'Server name is required';
      this.isLoading = false;
      return;
    }

    if (!this.serverUrl.trim()) {
      this.errorMessage = 'Server URL is required';
      this.isLoading = false;
      return;
    }

    if (this.authType === 'bearer' && !this.bearerToken.trim()) {
      this.errorMessage =
        'Bearer token is required when using bearer authentication';
      this.isLoading = false;
      return;
    }

    // Build auth config
    let authConfigObj = null;
    if (this.authType === 'bearer' && this.bearerToken.trim()) {
      authConfigObj = { token: this.bearerToken };
    } else if (this.authType === 'oauth' && this.server?.auth_config) {
      // Preserve existing OAuth auth_config (tokens, client_id, etc.)
      authConfigObj = this.server.auth_config;
    }

    const serverData = {
      name: this.serverName,
      url: this.serverUrl,
      transport: this.transport,
      auth_type: this.authType,
      auth_config: authConfigObj,
      status: 'active', // Always active when created/updated
    };

    try {
      if (this.server) {
        const updatedServer = await this._api.updateMCPServer(
          this.server.id,
          serverData
        );
        // If editing an OAuth server, redirect immediately (keep modal open)
        if (this.authType === 'oauth') {
          this._startOAuthFlow(updatedServer.id);
          return;
        }
        this.dispatchEvent(
          new CustomEvent('server-updated', {
            detail: { server: updatedServer },
          })
        );
      } else {
        const newServer = await this._api.createMCPServer(serverData);
        // For new OAuth servers, redirect immediately (keep modal open)
        if (this.authType === 'oauth') {
          this._startOAuthFlow(newServer.id);
          return;
        }
        this.dispatchEvent(
          new CustomEvent('server-added', {
            detail: { server: newServer },
          })
        );
      }
      this.closeModal(true);
    } catch (error: any) {
      this.errorMessage = error.message;
    } finally {
      this.isLoading = false;
    }
  }

  private async _startOAuthFlow(serverId?: string) {
    const id = serverId || this.server?.id;
    if (!id) return;
    // Exchange the session for a short-lived, server-scoped authorize token so
    // the reusable access token never lands in the URL (browser history, server
    // logs, Referer). The browser then does a full-page redirect that the
    // backend 302s to the external consent form.
    try {
      const response = await api.fetchWithAuth(
        `/api/v1/mcp-servers/${id}/oauth/authorize-token`,
        { method: 'POST' }
      );
      if (!response.ok) {
        this.errorMessage = 'Could not start the OAuth flow. Please try again.';
        return;
      }
      const { authorize_token: authorizeToken } = await response.json();
      window.location.href = `/api/v1/mcp-servers/${id}/oauth/authorize?code=${encodeURIComponent(
        authorizeToken
      )}`;
    } catch (error) {
      this.errorMessage = 'Not authenticated. Please log in again.';
    }
  }

  closeModal(success = false) {
    if (typeof success !== 'boolean') {
      success = false;
    }
    const event = new CustomEvent('close-modal', {
      bubbles: true,
      composed: true,
      detail: { success },
    });
    this.dispatchEvent(event);
    this.opened = false;
  }
}
