import {
  LitElement,
  css,
  html,
  nothing,
  unsafeCSS,
  type PropertyValues,
  type TemplateResult,
} from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import {
  createManagedAgentCredential,
  getUserProfile,
  listManagedAgentCredentials,
  revokeManagedAgentCredential,
  type ManagedAgentCredentialCreateResult,
  type ManagedAgentCredentialSummary,
} from '../api';
import { hasPermission } from '../permissions';
import { relativeTime } from '../utils/relative-time';
import { parseUTCDate } from '../utils/date';
import { confirmDialog } from './confirm-dialog';
import { consoleDialogStyles } from '../styles/console-dialog';
import consoleStyles from '../styles/console-styles.css?inline';

/** Durable agent keys. Full keys live only in the open creation result. */
@customElement('agent-credentials-card')
export class AgentCredentialsCard extends LitElement {
  @property() agentId = '';
  @property() lifecycleState = 'active';
  @state() private credentials: ManagedAgentCredentialSummary[] = [];
  @state() private canView = false;
  @state() private canManage = false;
  @state() private loading = true;
  @state() private error = '';
  @state() private dialogOpen = false;
  @state() private keyName = '';
  @state() private expiry = '365';
  @state() private token = '';
  @state() private createError = '';
  @state() private busy = false;
  @state() private revokingId = '';
  private generation = 0;
  private resolveCreate:
    ((result: ManagedAgentCredentialCreateResult | null) => void) | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
        margin: var(--sl-spacing-medium) 0;
      }
      sl-card {
        width: 100%;
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        min-height: 0;
        margin-bottom: 0;
      }
      h2 {
        margin: 0;
      }
      p {
        color: var(--console-meta-color);
      }
      table {
        width: 100%;
        border-collapse: collapse;
        text-align: left;
      }
      th {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        font-weight: 500;
      }
      th,
      td {
        padding: 12px 8px;
        border-bottom: 1px solid var(--console-hairline);
      }
      td:first-child {
        overflow-wrap: anywhere;
      }
      code {
        font-family: var(--sl-font-mono);
        overflow-wrap: anywhere;
      }
      sl-input,
      sl-select {
        margin-bottom: 1rem;
      }
      .key {
        display: flex;
        align-items: center;
        gap: 1rem;
      }
      .key code {
        min-width: 0;
        flex: 1;
        user-select: all;
      }
      sl-alert {
        margin-bottom: 1rem;
      }
      @media (max-width: 700px) {
        thead {
          display: none;
        }
        tr {
          display: block;
          padding: 12px 0;
          border-bottom: 1px solid var(--console-hairline);
        }
        td {
          display: flex;
          justify-content: space-between;
          gap: 12px;
          border: 0;
          padding: 4px 0;
        }
        td::before {
          content: attr(data-label);
          color: var(--console-meta-color);
        }
      }
    `,
  ];

  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('agentId')) {
      this.closeCreate();
      void this.load();
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    ++this.generation;
    this.closeCreate();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.loading = true;
    this.error = '';
    this.credentials = [];
    this.revokingId = '';
    this.canView = false;
    this.canManage = false;
    if (!this.agentId) {
      this.loading = false;
      return;
    }
    try {
      const profile = await getUserProfile();
      if (generation !== this.generation) return;
      this.canView = hasPermission(profile.permissions, 'view_agents');
      this.canManage = hasPermission(profile.permissions, 'manage_agents');
      if (!this.canView) return;
      const credentials = await listManagedAgentCredentials(this.agentId);
      if (generation === this.generation) this.credentials = credentials;
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not load credentials.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  /**
   * Open the shared create action, including from a manual setup panel.
   * Resolves once with the creation result, or null on dismissal/unavailability.
   * Callers must keep the returned token in memory only and clear it on close.
   */
  openCreate(): Promise<ManagedAgentCredentialCreateResult | null> {
    if (
      !this.canView ||
      !this.canManage ||
      this.loading ||
      this.lifecycleState !== 'active' ||
      this.dialogOpen
    )
      return Promise.resolve(null);
    this.keyName = '';
    this.expiry = '365';
    this.token = '';
    this.createError = '';
    this.dialogOpen = true;
    return new Promise((resolve) => {
      this.resolveCreate = resolve;
    });
  }

  private closeCreate(): void {
    this.dialogOpen = false;
    this.token = '';
    this.keyName = '';
    this.createError = '';
    this.busy = false;
    this.resolveCreate?.(null);
    this.resolveCreate = null;
  }

  private async create(): Promise<void> {
    if (this.busy || !this.canManage || this.lifecycleState !== 'active')
      return;
    const name = this.keyName.trim();
    if (!name || name.length > 100) {
      this.createError = 'Enter a key name between 1 and 100 characters.';
      return;
    }
    const generation = this.generation;
    this.busy = true;
    this.createError = '';
    try {
      const result = await createManagedAgentCredential(this.agentId, {
        name,
        expires_in_days: this.expiry === 'never' ? null : Number(this.expiry),
      });
      if (generation !== this.generation || !this.isConnected) return;
      this.credentials = [result.credential, ...this.credentials];
      this.token = result.token;
      this.resolveCreate?.(result);
      this.resolveCreate = null;
    } catch (error) {
      if (generation === this.generation)
        this.createError =
          error instanceof Error
            ? error.message
            : 'Could not create credential.';
    } finally {
      if (generation === this.generation) this.busy = false;
    }
  }

  private async revoke(
    credential: ManagedAgentCredentialSummary
  ): Promise<void> {
    if (!this.canManage || this.revokingId) return;
    const generation = this.generation;
    const confirmed = await confirmDialog({
      title: 'Revoke agent key',
      message: `Revoke "${credential.name}"?`,
      detail:
        'The agent will no longer be able to authenticate with this key. This cannot be undone.',
      confirmLabel: 'Revoke key',
      variant: 'danger',
    });
    if (!confirmed || generation !== this.generation || !this.isConnected)
      return;
    this.revokingId = credential.id;
    this.error = '';
    try {
      const updated = await revokeManagedAgentCredential(
        this.agentId,
        credential.id
      );
      if (generation === this.generation)
        this.credentials = this.credentials.map((item) =>
          item.id === updated.id ? updated : item
        );
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not revoke credential.';
    } finally {
      if (generation === this.generation) this.revokingId = '';
    }
  }

  private status(credential: ManagedAgentCredentialSummary): string {
    if (credential.status === 'revoked') return 'Revoked';
    if (
      credential.expires_at &&
      parseUTCDate(credential.expires_at).getTime() <= Date.now()
    )
      return 'Expired';
    return credential.status === 'active' ? 'Active' : credential.status;
  }

  render(): TemplateResult | typeof nothing {
    if (!this.loading && !this.canView && !this.error) return nothing;
    return html`
      <sl-card>
        <div slot="header" class="header">
          <h2>Credentials</h2>
          ${this.canView && this.canManage ? html`<sl-button data-create-open size="small" variant="primary" ?disabled=${this.loading || this.lifecycleState !== 'active'} @click=${() => void this.openCreate()}>Create key</sl-button>` : nothing}
        </div>
        <p>Keys that let this agent authenticate with Preloop.</p>
        ${this.canManage && this.lifecycleState !== 'active' ? html`<p>Only active agents can receive new keys.</p>` : nothing}
        ${this.error ? html`<sl-alert open variant="danger" role="alert">${this.error} <sl-button size="small" @click=${() => void this.load()}>Retry</sl-button></sl-alert>` : nothing}
        ${
          this.loading
            ? html`<p role="status">Loading credentials…</p>`
            : this.canView
              ? html`
                  ${
                    this.credentials.length
                      ? html`<table aria-label="Agent credentials">
                          <thead>
                            <tr>
                              <th>Name</th>
                              <th>Prefix</th>
                              <th>Created</th>
                              <th>Last used</th>
                              <th>Expiry</th>
                              <th>Status</th>
                              ${this.canManage ? html`<th>Actions</th>` : nothing}
                            </tr>
                          </thead>
                          <tbody>
                            ${this.credentials.map(
                              (credential) =>
                                html`<tr>
                                  <td data-label="Name">${credential.name}</td>
                                  <td data-label="Prefix">
                                    <code
                                      >${credential.key_prefix || 'Unavailable'}</code
                                    >
                                  </td>
                                  <td data-label="Created">
                                    ${relativeTime(credential.created_at, 'Unknown')}
                                  </td>
                                  <td data-label="Last used">
                                    ${relativeTime(credential.last_used_at, credential.last_used_at === undefined ? 'Unavailable' : 'Never used')}
                                  </td>
                                  <td data-label="Expiry">
                                    ${relativeTime(credential.expires_at, 'Never')}
                                  </td>
                                  <td data-label="Status">
                                    ${this.status(credential) === 'Active' ? html`<sl-badge class="chip" pill variant="success">Active</sl-badge>` : html`<sl-badge class="chip" pill variant="neutral">${this.status(credential)}</sl-badge>`}
                                  </td>
                                  ${this.canManage ? html`<td data-label="Actions">${credential.status !== 'revoked' ? html`<sl-button data-revoke size="small" variant="danger" outline ?disabled=${!!this.revokingId} ?loading=${this.revokingId === credential.id} @click=${() => void this.revoke(credential)}>Revoke</sl-button>` : nothing}</td>` : nothing}
                                </tr>`
                            )}
                          </tbody>
                        </table>`
                      : !this.error
                        ? html`<p>No credentials yet.</p>`
                        : nothing
                  }
                `
              : nothing
        }
      </sl-card>
      ${
        this.dialogOpen
          ? html`<sl-dialog
              open
              label=${this.token ? 'Save your agent key' : 'Create agent key'}
              @sl-request-close=${(event: Event) => {
                if (event.target !== event.currentTarget) return;
                if (this.busy) event.preventDefault();
              }}
              @sl-hide=${(event: Event) => {
                if (event.target === event.currentTarget) this.closeCreate();
              }}
            >
              ${
                this.token
                  ? html`
                      <p role="status">
                        Copy this key now and store it securely. You will not be
                        able to see it again after closing this dialog.
                      </p>
                      <div class="key">
                        <code>${this.token}</code
                        ><sl-copy-button
                          .value=${this.token}
                          copy-label="Copy agent key"
                        ></sl-copy-button>
                      </div>
                      <sl-button
                        slot="footer"
                        data-key-done
                        variant="primary"
                        @click=${this.closeCreate}
                        >Done</sl-button
                      >
                    `
                  : html`
                      ${this.createError ? html`<sl-alert open variant="danger" role="alert">${this.createError}</sl-alert>` : nothing}
                      <sl-input
                        label="Key name"
                        maxlength="100"
                        required
                        .value=${this.keyName}
                        ?disabled=${this.busy}
                        @sl-input=${(event: Event) => {
                          this.keyName = (
                            event.target as HTMLInputElement
                          ).value;
                        }}
                      ></sl-input>
                      <sl-select
                        label="Expiry"
                        .value=${this.expiry}
                        ?disabled=${this.busy}
                        @sl-change=${(event: Event) => {
                          this.expiry = (
                            event.target as HTMLSelectElement
                          ).value;
                        }}
                      >
                        <sl-option value="30">30 days</sl-option
                        ><sl-option value="90">90 days</sl-option
                        ><sl-option value="365">1 year</sl-option
                        ><sl-option value="never">Never</sl-option>
                      </sl-select>
                      <sl-button
                        slot="footer"
                        ?disabled=${this.busy}
                        @click=${this.closeCreate}
                        >Cancel</sl-button
                      >
                      <sl-button
                        slot="footer"
                        data-create-submit
                        variant="primary"
                        ?loading=${this.busy}
                        ?disabled=${!this.keyName.trim()}
                        @click=${() => void this.create()}
                        >Create key</sl-button
                      >
                    `
              }
            </sl-dialog>`
          : nothing
      }
    `;
  }
}
