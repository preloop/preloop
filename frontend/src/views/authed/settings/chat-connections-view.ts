import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '../../../components/view-header';
import consoleStyles from '../../../styles/console-styles.css?inline';
import {
  createChatConnection,
  createChatLinkCode,
  listChatConnections,
  listChatDeliveries,
  setChatConnectionEnabled,
  unlinkChatIdentity,
} from '../../../services/chat-api';
import type {
  ChatConnection,
  ChatConnectionCreate,
  ChatDelivery,
  ChatLinkCode,
  ChatProvider,
} from '../../../services/chat-api';

const emptyForm = (): ChatConnectionCreate => ({
  provider: 'slack',
  workspace_id: '',
  name: '',
  verification_secret: '',
  bot_token: '',
  bot_user_id: '',
  base_url: '',
});

@customElement('chat-connections-view')
export class ChatConnectionsView extends LitElement {
  @state() private connections: ChatConnection[] = [];
  @state() private canManage = false;
  @state() private loading = true;
  @state() private busy = false;
  @state() private error = '';
  @state() private showForm = false;
  @state() private form = emptyForm();
  @state() private linkCode: (ChatLinkCode & { connectionId: string }) | null =
    null;
  @state() private deliveries: ChatDelivery[] = [];
  @state() private deliveryConnectionId = '';
  private generation = 0;
  private expiryTimer: ReturnType<typeof setTimeout> | undefined;

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.generation++;
    this.canManage = false;
    this.busy = false;
    this.showForm = false;
    this.error = '';
    this.deliveryConnectionId = '';
    this.clearCode();
    this.form = emptyForm();
    this.connections = [];
    this.deliveries = [];
  }

  private clearCode() {
    clearTimeout(this.expiryTimer);
    this.linkCode = null;
  }

  private async load() {
    const generation = this.generation;
    this.loading = true;
    this.error = '';
    try {
      const result = await listChatConnections();
      if (!this.isConnected || generation !== this.generation) return;
      this.connections = result.connections;
      this.canManage = result.can_manage;
      if (!this.canManage) {
        this.showForm = false;
        this.form = emptyForm();
      }
    } catch (error) {
      if (this.isConnected && generation === this.generation)
        this.error = this.errorText(error);
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  private errorText(error: unknown): string {
    return error instanceof Error
      ? error.message
      : 'Unable to update chat connections.';
  }

  private async act(action: () => Promise<void>) {
    const generation = this.generation;
    this.busy = true;
    this.error = '';
    try {
      await action();
      if (generation !== this.generation) return;
    } catch (error) {
      if (this.isConnected && generation === this.generation)
        this.error = this.errorText(error);
    } finally {
      if (generation === this.generation) this.busy = false;
    }
  }

  private async create(event: Event) {
    const generation = this.generation;
    event.preventDefault();
    const inputs = this.renderRoot.querySelectorAll('sl-input');
    if ([...inputs].some((input) => !input.reportValidity())) return;
    await this.act(async () => {
      const created = await createChatConnection(this.form);
      if (!this.isConnected || generation !== this.generation) return;
      this.form = emptyForm();
      this.connections = [...this.connections, created];
      this.showForm = false;
    });
  }

  private async link(connection: ChatConnection) {
    const generation = this.generation;
    this.clearCode();
    await this.act(async () => {
      const code = await createChatLinkCode(connection.id);
      if (!this.isConnected || generation !== this.generation) return;
      const remaining = Date.parse(code.expires_at) - Date.now();
      if (!Number.isFinite(remaining) || remaining <= 0) {
        throw new Error('This linking code has expired. Generate a new code.');
      }
      this.linkCode = { ...code, connectionId: connection.id };
      this.expiryTimer = setTimeout(
        () => this.clearCode(),
        Math.min(remaining, 2_147_483_647)
      );
    });
  }

  private async unlink(connection: ChatConnection) {
    const generation = this.generation;
    await this.act(async () => {
      await unlinkChatIdentity(connection.id);
      if (!this.isConnected || generation !== this.generation) return;
      this.clearCode();
      await this.load();
    });
  }

  private async toggle(connection: ChatConnection) {
    const generation = this.generation;
    await this.act(async () => {
      const updated = await setChatConnectionEnabled(
        connection.id,
        !connection.enabled
      );
      if (!this.isConnected || generation !== this.generation) return;
      this.connections = this.connections.map((row) =>
        row.id === updated.id ? updated : row
      );
    });
  }

  private async showDeliveries(connection: ChatConnection) {
    const generation = this.generation;
    await this.act(async () => {
      const result = await listChatDeliveries(connection.id);
      if (!this.isConnected || generation !== this.generation) return;
      this.deliveries = result.deliveries;
      this.deliveryConnectionId = connection.id;
    });
  }

  private field(key: keyof ChatConnectionCreate, event: Event) {
    const value = (event.target as HTMLInputElement).value;
    this.form = { ...this.form, [key]: value };
  }

  private renderForm() {
    const provider = this.form.provider;
    return html`<form
      @submit=${this.create}
      aria-label="Connect a chat service"
    >
      <h2>Connect a chat service</h2>
      <p>
        Use a bot created for your team. Credentials are saved securely and
        aren't shown again.
      </p>
      <sl-select
        label="Service"
        .value=${provider}
        @sl-change=${(event: Event) => {
          this.form = {
            ...emptyForm(),
            provider: (event.target as HTMLSelectElement).value as ChatProvider,
          };
        }}
      >
        <sl-option value="slack">Slack</sl-option>
        <sl-option value="mattermost">Mattermost</sl-option>
        <sl-option value="discord">Discord</sl-option>
      </sl-select>
      <sl-input
        label="Connection name"
        required
        .value=${this.form.name}
        @sl-input=${(e: Event) => this.field('name', e)}
      ></sl-input>
      <sl-input
        label=${provider === 'discord' ? 'Server ID' : 'Workspace or team ID'}
        required
        .value=${this.form.workspace_id}
        @sl-input=${(e: Event) => this.field('workspace_id', e)}
      ></sl-input>
      <sl-input
        label="Bot token"
        type="password"
        autocomplete="new-password"
        required
        .value=${this.form.bot_token}
        @sl-input=${(e: Event) => this.field('bot_token', e)}
      ></sl-input>
      <sl-input
        label=${provider === 'discord' ? 'Application public key' : provider === 'slack' ? 'Signing secret' : 'Command verification token'}
        type=${provider === 'discord' ? 'text' : 'password'}
        autocomplete="off"
        required
        .value=${this.form.verification_secret}
        @sl-input=${(e: Event) => this.field('verification_secret', e)}
      ></sl-input>
      <sl-input
        label="Bot user ID"
        ?required=${provider === 'mattermost'}
        .value=${this.form.bot_user_id}
        @sl-input=${(e: Event) => this.field('bot_user_id', e)}
      ></sl-input>
      ${provider === 'mattermost' ? html`<sl-input label="Mattermost address" placeholder="https://chat.example.com" type="url" required .value=${this.form.base_url} @sl-input=${(e: Event) => this.field('base_url', e)}></sl-input>` : nothing}
      <div class="actions">
        <sl-button type="submit" variant="primary" ?loading=${this.busy}
          >Save connection</sl-button
        >
        <sl-button
          ?disabled=${this.busy}
          @click=${() => {
            this.showForm = false;
            this.form = emptyForm();
          }}
          >Cancel</sl-button
        >
      </div>
    </form>`;
  }

  private renderConnection(connection: ChatConnection) {
    const ingressUrl = new URL(connection.ingress_url, window.location.origin)
      .href;
    const code =
      this.linkCode?.connectionId === connection.id ? this.linkCode : null;
    const linkingCommand = code
      ? `${connection.provider === 'mattermost' ? '/preloop /link' : connection.provider === 'discord' ? '/preloop message:/link' : 'link'} ${code.code}`
      : '';
    return html`<article aria-label=${connection.name}>
      <div class="heading">
        <h2>${connection.name}</h2>
        <sl-badge variant=${connection.enabled ? 'success' : 'neutral'}
          >${connection.enabled ? 'Enabled' : 'Disabled'}</sl-badge
        >
      </div>
      <p>${connection.provider} · ${connection.workspace_id}</p>
      <p>
        ${connection.linked ? `Your account is linked (${connection.external_user_id}).` : 'Your account is not linked yet.'}
      </p>
      ${this.canManage ? html`<p class="endpoint">Request URL: <code>${ingressUrl}</code><sl-copy-button value=${ingressUrl} copy-label="Copy request URL"></sl-copy-button></p>` : nothing}
      <div class="actions">
        ${
          connection.linked
            ? html`<sl-button
                data-action="unlink"
                ?disabled=${this.busy}
                @click=${() => this.unlink(connection)}
                >Unlink my account</sl-button
              >`
            : html`<sl-button
                data-action="link"
                ?disabled=${this.busy || !connection.enabled}
                @click=${() => this.link(connection)}
                >Link my account</sl-button
              >`
        }
        <sl-button
          data-action="deliveries"
          ?disabled=${this.busy}
          @click=${() => this.showDeliveries(connection)}
          >My delivery history</sl-button
        >
        ${this.canManage ? html`<sl-button data-action="toggle" ?disabled=${this.busy} @click=${() => this.toggle(connection)}>${connection.enabled ? 'Disable' : 'Enable'} connection</sl-button>` : nothing}
      </div>
      ${
        code
          ? html`<div class="link-code" role="status">
              <p>${code.instruction}</p>
              <code>${linkingCommand}</code
              ><sl-copy-button
                value=${linkingCommand}
                copy-label="Copy linking command"
              ></sl-copy-button>
              <p>
                Expires ${new Date(code.expires_at).toLocaleTimeString()}. Keep
                this code private.
              </p>
              <sl-button
                @click=${() => {
                  this.clearCode();
                  void this.load();
                }}
                >I've sent the code — refresh</sl-button
              >
            </div>`
          : nothing
      }
      ${
        this.deliveryConnectionId === connection.id
          ? html`<div aria-label="My delivery history">
              ${
                this.deliveries.length
                  ? html`<ul>
                      ${this.deliveries.map((delivery) => html`<li>${delivery.status} · ${new Date(delivery.created_at).toLocaleString()}${delivery.last_error ? html`<span role="status"> — ${delivery.last_error}</span>` : nothing}</li>`)}
                    </ul>`
                  : html`<p>No deliveries yet.</p>`
              }
            </div>`
          : nothing
      }
    </article>`;
  }

  render() {
    return html`<view-header headerText="Chat connections"></view-header>
      <main>
        <p>
          Ask Preloop about your agents and spend, receive approval requests,
          and send notes to running sessions from your team's chat.
        </p>
        <p>
          Link your own account to use your existing permissions. Answers and
          approval notices arrive privately.
        </p>
        ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
        ${
          this.loading
            ? html`<p role="status">Loading chat connections…</p>`
            : html`
                <div class="actions">
                  <sl-button ?disabled=${this.busy} @click=${this.load}
                    >Refresh</sl-button
                  >${
                    this.canManage
                      ? html`<sl-button
                          data-action="create"
                          variant="primary"
                          ?disabled=${this.busy}
                          @click=${() => {
                            this.showForm = true;
                          }}
                          >Add connection</sl-button
                        >`
                      : nothing
                  }
                </div>
                ${this.showForm && this.canManage ? this.renderForm() : nothing}
                ${this.connections.length ? this.connections.map((row) => this.renderConnection(row)) : html`<p>No chat services are connected.${!this.canManage ? ' Ask an administrator to add one.' : ''}</p>`}
              `
        }
      </main>`;
  }

  static styles = [
    css`
      ${unsafeCSS(consoleStyles)}
    `,
    css`
      :host {
        display: block;
      }
      main {
        max-width: 850px;
        padding: var(--sl-spacing-large);
      }
      article,
      form {
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-large);
        margin-top: var(--sl-spacing-large);
      }
      h2 {
        font-size: var(--sl-font-size-large);
        margin: 0;
      }
      .heading,
      .actions {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
        align-items: center;
      }
      sl-input,
      sl-select {
        margin-bottom: var(--sl-spacing-medium);
      }
      .link-code {
        margin-top: var(--sl-spacing-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
      }
      code,
      .endpoint {
        overflow-wrap: anywhere;
      }
      [role='alert'] {
        color: var(--sl-color-danger-700);
      }
    `,
  ];
}
