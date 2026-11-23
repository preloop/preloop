import '../../../components/chat-setup-wizard';
import { getFlows } from '../../../api';
import type { Flow } from '../../../types';
import { ConsoleStatus } from '../../../controllers/console-status';
import { parseUTCDate } from '../../../utils/date';
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
  createChatLinkCode,
  listChatConnections,
  listChatDeliveries,
  setChatConnectionEnabled,
  setChatDefaultFlow,
  unlinkChatIdentity,
} from '../../../services/chat-api';
import type {
  ChatConnection,
  ChatDelivery,
  ChatLinkCode,
} from '../../../services/chat-api';

@customElement('chat-connections-view')
export class ChatConnectionsView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private chatFlows: Flow[] = [];
  @state() private connections: ChatConnection[] = [];
  @state() private canManage = false;
  @state() private loading = true;
  @state() private busy = false;
  @state() private error = '';
  @state() private showForm = false;
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
      if (this.canManage) {
        const flows = await getFlows({ limit: 1000 });
        if (!this.isConnected || generation !== this.generation) return;
        this.chatFlows = Array.isArray(flows)
          ? flows.filter(
              (flow) =>
                flow.chat_alias &&
                flow.is_enabled &&
                flow.git_clone_config?.publication_mode !== 'read_only'
            )
          : [];
      }
      if (!this.canManage) {
        this.showForm = false;
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

  private async setDefault(connection: ChatConnection, flowId: string) {
    const generation = this.generation;
    await this.act(async () => {
      const updated = await setChatDefaultFlow(
        connection.id,
        flowId === 'none' ? null : flowId
      );
      if (!this.isConnected || generation !== this.generation) return;
      this.connections = this.connections.map((row) =>
        row.id === updated.id ? updated : row
      );
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

  private renderForm() {
    return html`<chat-setup-wizard
      @chat-connection-created=${(event: CustomEvent<ChatConnection>) => {
        this.connections = [...this.connections, event.detail];
        this.showForm = false;
      }}
      @chat-setup-cancel=${() => {
        this.showForm = false;
      }}
    ></chat-setup-wizard>`;
  }

  private renderConnection(connection: ChatConnection) {
    const ingressUrl = new URL(connection.ingress_url, window.location.origin)
      .href;
    const code =
      this.linkCode?.connectionId === connection.id ? this.linkCode : null;
    const linkingCommand = code
      ? `${connection.provider === 'mattermost' ? (connection.transport === 'direct_message' ? 'link' : '/preloop /link') : connection.provider === 'discord' ? '/preloop message:/link' : 'link'} ${code.code}`
      : '';
    return html`<article aria-label=${connection.name}>
      <div class="heading">
        <h2>${connection.name}</h2>
        <sl-badge variant=${connection.enabled ? 'success' : 'neutral'}
          >${connection.enabled ? 'Enabled' : 'Disabled'}</sl-badge
        >
      </div>
      <p>${connection.provider} · ${connection.workspace_id}</p>
      <p role="status">
        Configured ·
        ${connection.worker_ready ? 'Worker ready' : connection.worker_status === 'reconnecting' ? 'Worker reconnecting' : 'Waiting for worker'}
        ·
        ${connection.callback_seen ? 'Provider event received' : connection.transport === 'direct_message' ? 'Direct messages' : 'Awaiting provider callback'}
      </p>
      <p>
        ${connection.linked ? `Your account is linked (${connection.external_user_id}).` : 'Your account is not linked yet.'}
      </p>
      ${this.canManage ? html`<p class="endpoint">Request URL: <code>${ingressUrl}</code><sl-copy-button value=${ingressUrl} copy-label="Copy request URL"></sl-copy-button></p>` : nothing}
      ${
        this.canManage
          ? html` <sl-select
              label="Default chat flow"
              help-text="Used for ask TEXT without an alias. Plain messages keep their existing behavior."
              data-field="default-flow"
              .value=${connection.default_flow_id || 'none'}
              ?disabled=${this.busy}
              @sl-change=${(e: Event) => this.setDefault(connection, (e.target as HTMLSelectElement).value)}
            >
              <sl-option value="none">No default</sl-option>
              ${this.chatFlows.map((flow) => html`<sl-option value=${flow.id!}>${flow.name} (${flow.chat_alias})</sl-option>`)}
            </sl-select>`
          : nothing
      }
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
                Expires ${parseUTCDate(code.expires_at).toLocaleTimeString()}.
                Keep this code private.
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
                      ${this.deliveries.map((delivery) => html`<li>${delivery.status} · ${parseUTCDate(delivery.created_at).toLocaleString()}${delivery.last_error ? html`<span role="status"> — ${delivery.last_error}</span>` : nothing}</li>`)}
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
      a {
        color: var(--sl-color-primary-600);
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
