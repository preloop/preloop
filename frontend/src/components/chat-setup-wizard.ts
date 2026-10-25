import { REST_API_PREFIX } from '../api-path';
import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import { fetchWithAuth } from '../api';
import type { ChatProvider } from '../services/chat-api';

interface SetupDraft {
  id: string;
  name: string;
  provider: ChatProvider;
  bot_username?: string;
  expires_at: string;
  callback_url: string;
  workspaces: { id: string; name: string }[];
  install_url?: string;
  callback_conflict?: boolean;
  signing_secret_required: boolean;
  manifest?: object;
}

@customElement('chat-setup-wizard')
export class ChatSetupWizard extends LitElement {
  @state() private provider: ChatProvider = 'mattermost';
  @state() private draft?: SetupDraft;
  @state() private token = '';
  @state() private secret = '';
  @state() private origin = '';
  @state() private teamHint = '';
  @state() private workspace = '';
  @state() private name = '';
  @state() private busy = false;
  @state() private error = '';
  @state() private replaceCallback = false;
  @state() private oauth = false;
  private generation = 0;

  connectedCallback(): void {
    super.connectedCallback();
    const generation = this.generation;
    void this.request('/options')
      .then((result) => {
        if (this.isConnected && this.generation === generation)
          this.oauth = result.slack_oauth === true;
      })
      .catch(() => {});
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.generation++;
    this.token = this.secret = '';
    this.draft = undefined;
  }

  private async request(path: string, body?: unknown, method = 'POST') {
    const response = await fetchWithAuth(
      `${REST_API_PREFIX}/chat/setup${path}`,
      {
        method: body === undefined && method === 'POST' ? 'GET' : method,
        cache: 'no-store',
        ...(body === undefined
          ? {}
          : {
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            }),
      }
    );
    const data = await response.json();
    if (!response.ok)
      throw new Error(
        typeof data.detail === 'string'
          ? data.detail
          : 'Unable to complete setup.'
      );
    return data;
  }

  private async act(action: () => Promise<void>) {
    this.busy = true;
    this.error = '';
    try {
      await action();
    } catch (error) {
      if (this.isConnected)
        this.error = error instanceof Error ? error.message : 'Setup failed.';
    } finally {
      if (this.isConnected) this.busy = false;
    }
  }

  private setDraft(draft: SetupDraft) {
    if (!this.isConnected) return;
    this.draft = draft;
    this.workspace =
      draft.workspaces.length === 1 ? draft.workspaces[0].id : '';
    this.name = draft.name;
  }

  private discover = (event: Event) => {
    event.preventDefault();
    if (
      ![...this.renderRoot.querySelectorAll('sl-input')].every((input) =>
        input.reportValidity()
      )
    )
      return;
    void this.act(async () => {
      const draft = await this.request('', {
        provider: this.provider,
        bot_token: this.token,
        verification_secret: this.secret,
        base_url: this.origin,
        team_hint: this.teamHint,
      });
      this.token = this.secret = '';
      this.setDraft(draft);
    });
  };

  private refresh = () =>
    void this.act(async () => {
      if (this.draft)
        this.setDraft(await this.request(`/${this.draft.id}/refresh`, {}));
    });

  private finish = () =>
    void this.act(async () => {
      if (!this.draft) return;
      if (this.draft.signing_secret_required) {
        if (!this.secret.trim())
          throw new Error('Enter the Slack Signing Secret.');
        this.setDraft(
          await this.request(
            `/${this.draft.id}/signing-secret`,
            { verification_secret: this.secret },
            'PUT'
          )
        );
        this.secret = '';
      }
      const connection = await this.request(`/${this.draft!.id}/finish`, {
        workspace_id: this.workspace,
        name: this.name,
        replace_callback: this.replaceCallback,
      });
      if (this.isConnected)
        window.dispatchEvent(new Event('preloop-support-conversion'));
      if (this.isConnected)
        this.dispatchEvent(
          new CustomEvent('chat-connection-created', {
            detail: connection,
            bubbles: true,
            composed: true,
          })
        );
    });

  render() {
    const draft = this.draft;
    return html`<section aria-label="Connect a chat service">
      <h2>Connect a chat service</h2>
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
      ${this.busy ? html`<p role="status">Checking provider…</p>` : nothing}
      ${
        draft
          ? html`
              <h3>2. Confirm ${draft.name}</h3>
              <p>
                Credentials verified. Finish installation, then link your own
                Preloop account. Worker readiness appears after saving.
              </p>
              ${
                draft.workspaces.length
                  ? html`<sl-select
                      label=${this.provider === 'mattermost' ? 'Team' : this.provider === 'discord' ? 'Server' : 'Workspace'}
                      .value=${this.workspace}
                      @sl-change=${(event: Event) => (this.workspace = (event.target as HTMLSelectElement).value)}
                    >
                      ${draft.workspaces.map((workspace) => html`<sl-option value=${workspace.id}>${workspace.name}</sl-option>`)}
                    </sl-select>`
                  : this.provider === 'mattermost'
                    ? html`<div role="status">
                        <p>
                          This
                          bot${draft.bot_username ? html` (<strong>@${draft.bot_username}</strong>)` : nothing}
                          is not a member of any team. Add it to the team whose
                          members should use Preloop:
                        </p>
                        <ol>
                          <li>
                            In Mattermost, open that team's menu →
                            <strong>Invite People</strong> →
                            <strong>Invite Member</strong>.
                          </li>
                          <li>
                            Find the bot in
                            <strong>Add or Invite People</strong>, then select
                            <strong>Invite Members</strong>.
                          </li>
                        </ol>
                        <p>
                          If invitations are restricted, ask a Mattermost
                          administrator to open
                          <strong
                            >System Console → User Management → Teams</strong
                          >, select the team, then
                          <strong>Members → Add Members</strong>, select the
                          bot, and <strong>Add → Save</strong>.
                        </p>
                        <p>
                          Return here and select
                          <strong>Refresh installation</strong>. Your encrypted
                          setup is kept for 30 minutes from discovery; you do
                          not need to enter the token again. Finish becomes
                          available after a team is found and selected.
                        </p>
                        <a
                          href="https://docs.mattermost.com/developers/integrate/reference/bot-accounts/"
                          target="_blank"
                          rel="noopener noreferrer"
                          >Mattermost bot invitation instructions</a
                        >
                      </div>`
                    : html`<p role="status">
                        ${this.provider === 'slack' ? 'Complete Slack authorization in the opened tab, then refresh.' : 'Install the bot in your server, then refresh.'}
                      </p>`
              }
              ${draft.install_url ? html`<a href=${draft.install_url} target="_blank" rel="noopener noreferrer">Install in Discord</a>` : nothing}
              ${this.provider !== 'mattermost' ? html`<p>Callback URL: <code>${draft.callback_url}</code><sl-copy-button value=${draft.callback_url} copy-label="Copy callback URL"></sl-copy-button></p>` : html`<p>Send the bot a direct message. No slash command or command token is needed.</p>`}
              ${
                draft.manifest
                  ? html`<p>
                        Slack uses direct messages and app mentions; no slash
                        command is needed.
                      </p>
                      <details>
                        <summary>Own Slack app manifest</summary>
                        <pre>${JSON.stringify(draft.manifest, null, 2)}</pre>
                        <sl-copy-button
                          value=${JSON.stringify(draft.manifest, null, 2)}
                          copy-label="Copy Slack manifest"
                        ></sl-copy-button>
                      </details>
                      <p>
                        <a
                          href="https://api.slack.com/apps"
                          target="_blank"
                          rel="noopener noreferrer"
                          >Open Slack app settings</a
                        >. Install the app to your workspace, enable Events with
                        this callback and enable the Messages tab. Basic
                        Information → App Credentials contains the Signing
                        Secret; your bot token cannot reveal it.
                      </p>
                      ${draft.signing_secret_required ? html`<sl-input label="Signing Secret" type="password" autocomplete="new-password" .value=${this.secret} @sl-input=${(event: Event) => (this.secret = (event.target as HTMLInputElement).value)}></sl-input>` : nothing}`
                  : nothing
              }
              ${
                draft.callback_conflict
                  ? html`<p role="alert">
                        This Discord app already has a different interactions
                        callback. Replacing it affects its existing integration.
                      </p>
                      <label
                        ><input
                          type="checkbox"
                          .checked=${this.replaceCallback}
                          @change=${(event: Event) => (this.replaceCallback = (event.target as HTMLInputElement).checked)}
                        />Replace the existing callback</label
                      >`
                  : nothing
              }
              <details>
                <summary>Advanced settings</summary>
                <sl-input
                  label="Connection name"
                  .value=${this.name}
                  @sl-input=${(event: Event) => (this.name = (event.target as HTMLInputElement).value)}
                ></sl-input>
              </details>
              <sl-button ?disabled=${this.busy} @click=${this.refresh}
                >Refresh installation</sl-button
              >
              <sl-button
                variant="primary"
                ?disabled=${this.busy || !this.workspace || (!!draft.callback_conflict && !this.replaceCallback)}
                @click=${this.finish}
                >Finish${this.provider === 'discord' ? ' and install /preloop' : ''}</sl-button
              >
            `
          : html`<form @submit=${this.discover}>
              <h3>1. Choose and connect</h3>
              <sl-select
                label="Service"
                .value=${this.provider}
                @sl-change=${(event: Event) => {
                  this.provider = (event.target as HTMLSelectElement)
                    .value as ChatProvider;
                  this.token = this.secret = this.origin = this.teamHint = '';
                }}
              >
                <sl-option value="mattermost">Mattermost</sl-option
                ><sl-option value="slack">Slack</sl-option
                ><sl-option value="discord">Discord</sl-option>
              </sl-select>
              ${
                this.provider === 'slack' && this.oauth
                  ? html`<sl-button
                      @click=${() =>
                        void this.act(async () => {
                          const result = await this.request('/slack/oauth', {});
                          this.draft = {
                            id: result.id,
                            provider: 'slack',
                            name: 'Slack authorization',
                            expires_at: '',
                            callback_url: '',
                            workspaces: [],
                            signing_secret_required: false,
                          };
                          window.open(
                            result.authorization_url,
                            '_blank',
                            'noopener,noreferrer'
                          );
                        })}
                      >Add to Slack</sl-button
                    >`
                  : nothing
              }
              ${this.provider === 'mattermost' ? html`<sl-input label="Mattermost address" type="url" required placeholder="https://chat.example.com" .value=${this.origin} @sl-input=${(event: Event) => (this.origin = (event.target as HTMLInputElement).value)}></sl-input>` : nothing}
              <sl-input
                label=${this.provider === 'mattermost' ? 'Bot access token' : 'Bot token'}
                type="password"
                autocomplete="new-password"
                required
                help-text=${this.provider === 'mattermost' ? 'Paste the token secret, not the Token ID. Use a bot account, not a system-admin token.' : 'Use the bot token from your provider developer console. Identity and workspace are discovered automatically.'}
                .value=${this.token}
                @sl-input=${(event: Event) => (this.token = (event.target as HTMLInputElement).value)}
              ></sl-input>
              ${this.provider === 'slack' ? html`<sl-input label="Signing Secret (can add after discovery)" type="password" autocomplete="new-password" .value=${this.secret} @sl-input=${(event: Event) => (this.secret = (event.target as HTMLInputElement).value)}></sl-input>` : nothing}
              ${
                this.provider === 'mattermost'
                  ? html`<details>
                      <summary>Advanced team lookup</summary>
                      <sl-input
                        label="Team name or normal team URL"
                        help-text="Optional. Invite and channel links cannot identify a team."
                        .value=${this.teamHint}
                        @sl-input=${(event: Event) => (this.teamHint = (event.target as HTMLInputElement).value)}
                      ></sl-input>
                    </details>`
                  : nothing
              }
              <p>
                <a
                  href=${`https://docs.preloop.ai/chat-connections/#${this.provider}-setup`}
                  target="_blank"
                  rel="noopener noreferrer"
                  >Provider setup guide</a
                >
              </p>
              <sl-button type="submit" variant="primary" ?loading=${this.busy}
                >Discover bot and workspace</sl-button
              >
            </form>`
      }
      <sl-button
        ?disabled=${this.busy}
        @click=${() => this.dispatchEvent(new CustomEvent('chat-setup-cancel', { bubbles: true, composed: true }))}
        >Cancel</sl-button
      >
    </section>`;
  }

  static styles = css`
    :host {
      display: block;
    }
    section {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 8px;
      padding: 1.5rem;
      margin-top: 1rem;
    }
    sl-input,
    sl-select {
      display: block;
      margin: 1rem 0;
    }
    sl-button {
      margin: 0.5rem 0.5rem 0.5rem 0;
    }
    code,
    pre {
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    a {
      color: var(--sl-color-primary-600);
    }
    [role='alert'] {
      color: var(--sl-color-danger-700);
    }
  `;
}
