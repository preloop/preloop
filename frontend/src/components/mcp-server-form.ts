import { REST_API_PREFIX } from '../api-path';
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
import { consoleDialogStyles } from '../styles/console-dialog';
import './capability-extension';

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
  private toolPrefix = '';

  @state()
  private isLoading = false;

  @state()
  private errorMessage = '';

  @state() private oauthClientId = '';
  @state() private oauthClientSecret = '';
  @state() private oauthIssuer = '';
  @state() private oauthScope = '';
  @state() private oauthConnecting = false;

  static styles = [
    consoleDialogStyles,
    css`
      .error {
        color: var(--sl-color-danger-700);
        margin-top: 1rem;
      }
      sl-input,
      sl-select,
      sl-textarea {
        margin-bottom: 1rem;
      }
      .warnings {
        color: var(--sl-color-warning-700);
        background: var(--sl-color-warning-50);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-small);
        margin-bottom: 1rem;
        font-size: 0.875rem;
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
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    if (this.server) {
      this.serverName = this.server.name;
      this.serverUrl = this.server.url;
      this.transport = this.server.transport || 'http-streaming';
      this.authType = this.server.auth_type || 'none';
      this.bearerToken = this.server.auth_config?.token || '';
      this.toolPrefix = this.server.tool_prefix || '';
      this.oauthClientId = this.server.auth_config?.client_id || '';
      this.oauthIssuer = this.server.auth_config?.issuer || '';
      const scope =
        this.server.auth_config?.scope || this.server.auth_config?.scopes || '';
      this.oauthScope = Array.isArray(scope) ? scope.join(' ') : scope;
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
          The base URL of your MCP server. Servers that use the Streamable HTTP
          transport are supported.
        </div>

        ${
          this.server?.warnings?.length
            ? html`<div
                class="warnings"
                role="note"
                data-testid="mcp-server-warnings"
              >
                <strong>Tool name collisions</strong>
                ${this.server.warnings.map(
                  (warning: string) => html`<div>${warning}</div>`
                )}
              </div>`
            : ''
        }

        <sl-input
          label="Tool prefix (optional)"
          name="tool_prefix"
          .value=${this.toolPrefix}
          @sl-input=${(e: any) => (this.toolPrefix = e.target.value)}
          placeholder="e.g., crm"
          maxlength="32"
        ></sl-input>
        <div class="help-text">
          When set, this server's tools are exposed to agents as prefix_tool.
          Use it only when another server exposes the same tool names. Lowercase
          letters, digits and underscores, at most 32 characters. Changing it
          renames the tools agents see.
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
                <sl-input
                  label="Client ID (optional)"
                  .value=${this.oauthClientId}
                  @sl-input=${(e: any) => (this.oauthClientId = e.target.value)}
                  help-text="Leave empty to use public client registration when supported."
                ></sl-input>
                <sl-input
                  label="Client secret (optional)"
                  type="password"
                  .value=${this.oauthClientSecret}
                  @sl-input=${(e: any) => (this.oauthClientSecret = e.target.value)}
                  help-text="Leave empty to keep a saved secret. Public clients do not need one."
                ></sl-input>
                <sl-input
                  label="Authorization server issuer (optional)"
                  .value=${this.oauthIssuer}
                  @sl-input=${(e: any) => (this.oauthIssuer = e.target.value)}
                  help-text="Required when the MCP server advertises several authorization servers."
                ></sl-input>
                <sl-input
                  label="Scopes (optional)"
                  .value=${this.oauthScope}
                  @sl-input=${(e: any) => (this.oauthScope = e.target.value)}
                ></sl-input>
                <p role="status">
                  ${this.oauthConnecting ? 'Connecting…' : ({ disconnected: 'Disconnected', connecting: 'Connecting…', connected: 'Connected', reconnect_required: 'Reconnect required' } as Record<string, string>)[this.server?.connection_state || 'disconnected']}
                </p>
                ${
                  this.server
                    ? html`
                        <sl-button
                          variant="primary"
                          outline
                          ?disabled=${this.oauthConnecting}
                          @click=${() => this.handleSave(true)}
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
            ? html`<p class="error" role="alert">${this.errorMessage}</p>`
            : ''
        }
        ${
          // Sharing and tags of an existing server, where the deployment
          // reports the capability; nothing otherwise.
          this.server?.id
            ? html`<capability-extension
                name="resource-access"
                style="display: contents;"
                .context=${{
                  kind: 'mcp_server',
                  resourceId: this.server.id,
                  sharedFrom: this.server.is_shared
                    ? (this.server.owner_name ?? 'parent account')
                    : undefined,
                  readOnly: this.server.read_only || this.server.is_shared,
                }}
              ></capability-extension>`
            : ''
        }

        <div slot="footer">
          <sl-button @click=${() => this.closeModal()}>Cancel</sl-button>
          <sl-button
            variant="primary"
            @click=${() => this.handleSave()}
            .loading=${this.isLoading}
          >
            ${this.server ? 'Save' : 'Add'}
          </sl-button>
        </div>
      </sl-dialog>
    `;
  }

  async handleSave(connect = false) {
    const connectRequested = connect === true;
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

    const prefix = this.toolPrefix.trim();
    if (prefix && !/^[a-z0-9_]{1,32}$/.test(prefix)) {
      this.errorMessage =
        'Tool prefix must be lowercase letters, digits and underscores, at most 32 characters';
      this.isLoading = false;
      return;
    }

    // Build auth config
    let authConfigObj = null;
    if (this.authType === 'bearer' && this.bearerToken.trim()) {
      authConfigObj = { token: this.bearerToken };
    } else if (this.authType === 'oauth') {
      authConfigObj = {
        ...(this.server?.auth_config || {}),
        client_id: this.oauthClientId.trim(),
        scope: this.oauthScope.trim(),
      };
      if (this.oauthIssuer.trim())
        authConfigObj.issuer = this.oauthIssuer.trim();
      else delete authConfigObj.issuer;
      if (this.oauthClientSecret)
        authConfigObj.client_secret = this.oauthClientSecret;
    }

    const serverData = {
      name: this.serverName,
      url: this.serverUrl,
      transport: this.transport,
      auth_type: this.authType,
      auth_config: authConfigObj,
      status: 'active', // Always active when created/updated
      tool_prefix: prefix || null,
    };

    try {
      if (this.server) {
        const updatedServer = await this._api.updateMCPServer(
          this.server.id,
          serverData
        );
        // If editing an OAuth server, redirect immediately (keep modal open)
        if (
          this.authType === 'oauth' &&
          (connectRequested || updatedServer.connection_state !== 'connected')
        ) {
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
    this.oauthConnecting = true;
    try {
      const response = await this._api.fetchWithAuth(
        `${REST_API_PREFIX}/mcp-servers/${id}/oauth/authorize-token`,
        { method: 'POST' }
      );
      if (!response.ok) {
        this.oauthConnecting = false;
        this.errorMessage = 'Could not start the OAuth flow. Please try again.';
        return;
      }
      const { authorize_token: authorizeToken } = await response.json();
      window.location.href = `${REST_API_PREFIX}/mcp-servers/${id}/oauth/authorize?code=${encodeURIComponent(
        authorizeToken
      )}`;
    } catch (error) {
      this.oauthConnecting = false;
      this.errorMessage =
        'Could not connect. Check your sign-in and try again.';
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
