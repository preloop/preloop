import { ConsoleStatus } from '../../../controllers/console-status';
import { parseUTCDate } from '../../../utils/date';
import { LitElement, html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getAllFlows, listProjects } from '../../../api';
import type { Flow, Project } from '../../../types';
import {
  CI_ACTIONS,
  ciAdministration,
  completeCiCapabilities,
  type CiAction,
  type CiCapabilities,
  type CiGrant,
  type CiIdentity,
  type CiKey,
  type CiPreview,
} from '../../../ci-administration-api';

@customElement('ci-identities-view')
export class CiIdentitiesView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private loading = true;
  @state() private busy = false;
  @state() private capabilities: CiCapabilities | null = null;
  @state() private identities: CiIdentity[] = [];
  @state() private projects: Project[] = [];
  @state() private flows: Flow[] = [];
  @state() private error = '';
  @state() private name = '';
  @state() private projectId = '';
  @state() private flowId = '';
  @state() private actions: CiAction[] = [...CI_ACTIONS];
  @state() private expires = '';
  @state() private preview: CiPreview | null = null;
  @state() private secret = '';
  @state() private secretLabel = '';
  @state() private secretError = '';
  @state() private editId = '';
  @state() private editActions: CiAction[] = [];
  @state() private subscriptionId = '';
  @state() private anchorId = '';
  @state() private callbackUrl = '';
  @state() private callbackDescription = '';
  private lifecycle = 0;

  protected createRenderRoot(): HTMLElement {
    return this;
  }
  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }
  disconnectedCallback(): void {
    this.lifecycle++;
    this.busy = false;
    this.clearSecret();
    super.disconnectedCallback();
  }
  protected updated(): void {
    const dialog = this.querySelector<HTMLDialogElement>('dialog');
    if (this.secret && dialog && !dialog.open) dialog.showModal();
  }

  private get ready(): boolean {
    return (
      completeCiCapabilities(this.capabilities) &&
      this.capabilities?.can_view === true
    );
  }
  private get manage(): boolean {
    return this.ready && this.capabilities?.can_manage === true;
  }
  private async load(): Promise<void> {
    const lifecycle = this.lifecycle;
    this.loading = true;
    this.preview = null;
    this.error = '';
    try {
      const capabilities = await ciAdministration.capabilities();
      if (lifecycle !== this.lifecycle) return;
      this.capabilities = capabilities;
      if (!this.ready) {
        this.identities = [];
        return;
      }
      const identities = await ciAdministration.list();
      if (lifecycle !== this.lifecycle) return;
      this.identities = identities;
      if (this.manage) {
        const [projects, flowList] = await Promise.all([
          listProjects(),
          getAllFlows(),
        ]);
        if (lifecycle !== this.lifecycle) return;
        this.projects = projects;
        this.flows = flowList.flows;
      }
    } catch {
      if (lifecycle === this.lifecycle)
        this.error =
          'Restricted CI setup could not be loaded. Refresh to check current permissions.';
    } finally {
      if (lifecycle === this.lifecycle) this.loading = false;
    }
  }
  private dedicatedFlows(): Flow[] {
    return this.flows.filter(
      (flow) =>
        flow.id &&
        flow.account_id &&
        flow.is_enabled === true &&
        flow.runner_pool === 'server' &&
        flow.trigger_project_ids?.length === 1 &&
        flow.trigger_project_ids[0] === this.projectId &&
        flow.git_clone_config?.enabled === true &&
        flow.git_clone_config.repositories?.length === 1 &&
        flow.git_clone_config.repositories[0].project_id === this.projectId
    );
  }
  private grant(): CiGrant {
    return {
      version: 1,
      project_id: this.projectId,
      flow_id: this.flowId,
      actions: [...this.actions],
    };
  }
  private expiry(value = this.expires): string | undefined {
    if (!value) return undefined;
    const date = parseUTCDate(value);
    if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now())
      throw new Error('Invalid expiry');
    return date.toISOString();
  }
  private clearSecret(): void {
    this.secret = '';
    this.secretLabel = '';
    this.secretError = '';
  }
  private disclose(value: string, label: string, lifecycle: number): void {
    if (this.isConnected && lifecycle === this.lifecycle) {
      this.secret = value;
      this.secretLabel = label;
    }
  }
  private async run(
    operation: (lifecycle: number) => Promise<void>
  ): Promise<void> {
    if (!this.manage || this.busy || this.secret) return;
    const lifecycle = this.lifecycle;
    this.busy = true;
    this.error = '';
    try {
      const capabilities = await ciAdministration.capabilities();
      if (lifecycle !== this.lifecycle) return;
      this.capabilities = capabilities;
      if (!this.manage) throw new Error('Unavailable');
      await operation(lifecycle);
    } catch {
      if (lifecycle === this.lifecycle)
        this.error =
          'The request could not be completed. Refresh permissions and verify the dedicated flow, grant and credential lifetime.';
    } finally {
      if (lifecycle === this.lifecycle) this.busy = false;
    }
  }
  private async refreshRows(): Promise<void> {
    const lifecycle = this.lifecycle;
    const rows = await ciAdministration.list();
    if (this.isConnected && lifecycle === this.lifecycle)
      this.identities = rows;
  }
  private previewGrant(): void {
    void this.run(async (lifecycle) => {
      const grant = this.grant();
      const preview = await ciAdministration.preview(grant);
      if (
        lifecycle === this.lifecycle &&
        JSON.stringify(grant) === JSON.stringify(this.grant())
      )
        this.preview = preview;
    });
  }
  private createIdentity(): void {
    if (
      !this.preview ||
      JSON.stringify(this.preview.grant) !== JSON.stringify(this.grant())
    )
      return;
    const name = this.name.trim();
    const grant = this.grant();
    const expires = this.expires;
    void this.run(async (lifecycle) => {
      const issued = await ciAdministration.create(
        name,
        grant,
        this.expiry(expires)
      );
      this.disclose(issued.token, 'CI token', lifecycle);
      this.preview = null;
      await this.refreshRows();
    });
  }
  private keyOperation(
    identity: CiIdentity,
    key: CiKey | null,
    operation: 'issue' | 'rotate' | 'revoke'
  ): void {
    void this.run(async (lifecycle) => {
      if (operation === 'revoke' && key)
        await ciAdministration.revoke(identity.id, key.id);
      else {
        const issued =
          operation === 'rotate' && key
            ? await ciAdministration.rotate(identity.id, key.id, this.expiry())
            : await ciAdministration.issue(identity.id, this.expiry());
        this.disclose(issued.token, 'CI token', lifecycle);
      }
      await this.refreshRows();
    });
  }
  private toggleIdentity(identity: CiIdentity): void {
    void this.run(async () => {
      await ciAdministration.change(identity.id, {
        enabled: !identity.is_active,
      });
      await this.refreshRows();
    });
  }
  private narrow(identity: CiIdentity): void {
    if (!this.editActions.length) return;
    void this.run(async () => {
      await ciAdministration.change(identity.id, {
        grant: { ...identity.grant, actions: [...this.editActions] },
      });
      this.editId = '';
      await this.refreshRows();
    });
  }
  private subscribe(): void {
    void this.run(async (lifecycle) => {
      const subscription = await ciAdministration.subscribe(
        this.subscriptionId,
        this.anchorId,
        this.callbackUrl,
        this.callbackDescription
      );
      this.disclose(subscription.secret, 'Callback signing secret', lifecycle);
      this.subscriptionId = '';
      this.callbackUrl = '';
      this.callbackDescription = '';
    });
  }
  private retired(key: CiKey): boolean {
    if (!key.is_active) return true;
    if (!key.expires_at) return false;
    const date = parseUTCDate(key.expires_at);
    return !Number.isFinite(date.getTime()) || date.getTime() <= Date.now();
  }
  private actionChoices(
    selected: CiAction[],
    options: readonly CiAction[],
    change: (actions: CiAction[]) => void
  ): TemplateResult {
    return html`<fieldset>
      <legend>Allowed actions</legend>
      ${options.map(
        (action) =>
          html`<label class="ci-check"
            ><input
              type="checkbox"
              .checked=${selected.includes(action)}
              ?disabled=${this.busy}
              @change=${(event: Event) => {
                change(
                  (event.target as HTMLInputElement).checked
                    ? [...selected, action]
                    : selected.filter((item) => item !== action)
                );
              }}
            />${action}</label
          >`
      )}
    </fieldset>`;
  }
  private controls(identity: CiIdentity): TemplateResult {
    const disabled = this.busy || !!this.secret;
    return html`<div>
      <button
        ?disabled=${disabled}
        @click=${() => this.toggleIdentity(identity)}
      >
        ${identity.is_active ? 'Disable identity' : 'Reenable identity'}</button
      ><button
        ?disabled=${disabled || !identity.is_active}
        @click=${() => this.keyOperation(identity, null, 'issue')}
      >
        Issue replacement key</button
      ><button
        ?disabled=${disabled}
        @click=${() => {
          this.editId = identity.id;
          this.editActions = [...identity.grant.actions];
        }}
      >
        Narrow grant</button
      ><button
        ?disabled=${disabled || !identity.is_active || !identity.keys.length || !identity.grant.actions.includes('subscription:create')}
        @click=${() => {
          this.subscriptionId = identity.id;
          this.anchorId = identity.keys[0].id;
        }}
      >
        Set up completion callback
      </button>
    </div>`;
  }
  render(): TemplateResult {
    return html`<style>
        ci-identities-view {
          display: block;
          padding: var(--sl-spacing-large);
          color: var(--sl-color-neutral-900);
        }
        ci-identities-view .ci-card {
          border: 1px solid var(--sl-color-neutral-200);
          border-radius: var(--sl-border-radius-medium);
          padding: 1.25rem;
          margin: 1rem 0;
          background: var(--sl-panel-background-color);
        }
        ci-identities-view label {
          display: block;
          margin: 0.6rem 0;
        }
        ci-identities-view input:not([type='checkbox']),
        ci-identities-view select {
          display: block;
          padding: 0.5rem;
          width: 100%;
          max-width: 32rem;
          border: 1px solid var(--sl-color-neutral-300);
          border-radius: 0.25rem;
          color: inherit;
          background: var(--sl-input-background-color);
        }
        ci-identities-view button {
          padding: 0.5rem 0.8rem;
          margin: 0.25rem 0.4rem 0.25rem 0;
          border: 1px solid var(--sl-color-neutral-300);
          border-radius: 0.25rem;
          color: inherit;
          background: var(--sl-color-neutral-50);
          cursor: pointer;
        }
        ci-identities-view button:disabled {
          opacity: 0.5;
          cursor: default;
        }
        ci-identities-view fieldset {
          border: 1px solid var(--sl-color-neutral-200);
          margin: 1rem 0;
        }
        ci-identities-view .ci-check {
          display: inline-flex;
          gap: 0.4rem;
          margin: 0.35rem 1rem 0.35rem 0;
        }
        ci-identities-view table {
          width: 100%;
          border-collapse: collapse;
        }
        ci-identities-view td,
        ci-identities-view th {
          text-align: left;
          padding: 0.6rem;
          border-bottom: 1px solid var(--sl-color-neutral-200);
        }
        ci-identities-view dialog {
          color: inherit;
          background: var(--sl-panel-background-color);
          border: 1px solid var(--sl-color-neutral-300);
          border-radius: 0.5rem;
          max-width: min(38rem, 90vw);
          margin: auto;
          position: fixed;
          top: 20vh;
          z-index: 1000;
        }
        ci-identities-view dialog::backdrop {
          background: #0007;
        }
        ci-identities-view code {
          overflow-wrap: anywhere;
        }
        ci-identities-view .ci-secret {
          user-select: all;
          display: block;
          padding: 1rem;
        }
      </style>
      <h1>Restricted CI identities</h1>
      <p>
        Each identity owns one project's dedicated hosted flow and its own
        executions and completion callbacks. Rotating a key preserves that
        ownership.
      </p>
      ${this.loading ? html`<p data-loading role="status">Loading restricted CI setup…</p>` : nothing}
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
      ${!this.loading && !this.ready ? html`<p role="status">Restricted CI setup is unavailable or you do not have permission to view it.</p>` : nothing}
      <button
        ?disabled=${this.busy || !!this.secret}
        @click=${() => void this.load()}
      >
        Refresh permissions
      </button>
      ${
        this.ready
          ? html`${
              !this.manage
                ? html`<p>
                    View only. Ask an account administrator to manage identities
                    and credentials.
                  </p>`
                : html`<section class="ci-card" aria-label="Create CI identity">
                    <h2>Create identity</h2>
                    <label
                      >Name<input
                        id="ci-name"
                        maxlength="100"
                        .value=${this.name}
                        @input=${(event: Event) => (this.name = (event.target as HTMLInputElement).value)}
                    /></label>
                    <label
                      >Project<select
                        id="ci-project"
                        .value=${this.projectId}
                        @change=${(event: Event) => {
                          this.projectId = (
                            event.target as HTMLSelectElement
                          ).value;
                          this.flowId = '';
                          this.preview = null;
                        }}
                      >
                        <option value="">Select project</option>
                        ${this.projects.map((project) => html`<option value=${project.id}>${project.name}</option>`)}
                      </select></label
                    >
                    <label
                      >Dedicated hosted flow<select
                        id="ci-flow"
                        .value=${this.flowId}
                        @change=${(event: Event) => {
                          this.flowId = (
                            event.target as HTMLSelectElement
                          ).value;
                          this.preview = null;
                        }}
                      >
                        <option value="">Select flow</option>
                        ${this.dedicatedFlows().map((flow) => html`<option value=${flow.id!}>${flow.name}</option>`)}
                      </select></label
                    >
                    ${this.projectId && !this.dedicatedFlows().length ? html`<p>Configure an enabled server-hosted flow bound to this project and its single repository first.</p>` : nothing}
                    ${this.actionChoices(
                      this.actions,
                      CI_ACTIONS,
                      (actions) => {
                        this.actions = actions;
                        this.preview = null;
                      }
                    )}
                    <label
                      >New/replacement key expires at (local time; blank means
                      no expiry)<input
                        id="ci-expiry"
                        type="datetime-local"
                        .value=${this.expires}
                        @input=${(event: Event) => (this.expires = (event.target as HTMLInputElement).value)}
                    /></label>
                    <p>
                      CI cannot administer identities, keys, projects, flows,
                      policies or runners. Other principals' executions, logs,
                      artifacts and sessions are denied.
                    </p>
                    <button
                      id="ci-preview"
                      ?disabled=${this.busy || !!this.secret || !this.projectId || !this.flowId || !this.actions.length}
                      @click=${this.previewGrant}
                    >
                      Preview grant
                    </button>
                    ${this.preview ? html`<p id="ci-preview-summary">Validated ${this.preview.tracker_type} repository ${this.preview.repository_slug}; hosted flow ${this.preview.grant.flow_id}.</p>` : nothing}
                    <button
                      id="ci-create"
                      ?disabled=${this.busy || !!this.secret || !this.name.trim() || !this.preview}
                      @click=${this.createIdentity}
                    >
                      Create identity and key
                    </button>
                  </section>`
            }
            ${
              this.identities.length
                ? this.identities.map(
                    (identity) =>
                      html`<section
                        class="ci-card"
                        data-principal=${identity.id}
                      >
                        <h2>${identity.name}</h2>
                        <p>
                          ${identity.is_active ? 'Enabled' : 'Disabled'} ·
                          ${identity.tracker_type}: ${identity.repository_slug}
                        </p>
                        <p>
                          Identity: <code>${identity.id}</code><br />Project:
                          <code>${identity.grant.project_id}</code><br />Flow:
                          <code>${identity.grant.flow_id}</code>
                        </p>
                        <p>Actions: ${identity.grant.actions.join(', ')}</p>
                        ${this.manage ? this.controls(identity) : nothing}
                        ${this.manage && this.editId === identity.id ? html`${this.actionChoices(this.editActions, identity.grant.actions, (actions) => (this.editActions = actions))}<button ?disabled=${this.busy || !this.editActions.length} @click=${() => this.narrow(identity)}>Save narrower grant</button><button @click=${() => (this.editId = '')}>Cancel narrowing</button>` : nothing}
                        <div class="table-scroll">
                          <table>
                            <thead>
                              <tr>
                                <th>Key audit ID</th>
                                <th>Status</th>
                                <th>Expires (UTC)</th>
                                <th>Actions</th>
                              </tr>
                            </thead>
                            <tbody>
                              ${identity.keys.map(
                                (key) =>
                                  html`<tr data-key=${key.id}>
                                    <td><code>${key.id}</code></td>
                                    <td>
                                      ${!key.is_active ? 'Revoked' : this.retired(key) ? 'Expired' : 'Active'}
                                    </td>
                                    <td>${key.expires_at ?? 'No expiry'}</td>
                                    <td>
                                      ${key.actions.join(', ')}${this.manage ? html`<div><button ?disabled=${this.busy || !!this.secret || this.retired(key) || !identity.is_active} @click=${() => this.keyOperation(identity, key, 'rotate')}>Rotate key</button><button ?disabled=${this.busy || !!this.secret || !key.is_active} @click=${() => this.keyOperation(identity, key, 'revoke')}>Revoke key</button></div>` : nothing}
                                    </td>
                                  </tr>`
                              )}
                            </tbody>
                          </table>
                        </div>
                      </section>`
                  )
                : html`<p>No restricted CI identities.</p>`
            }
            ${
              this.manage && this.subscriptionId
                ? html`<section
                    class="ci-card"
                    aria-label="Completion callback"
                  >
                    <h2>Completion callback</h2>
                    <p>
                      Only flow.execution.finished for this identity and flow.
                      Completion does not prove that a review was published.
                    </p>
                    <label
                      >Key audit anchor<select
                        id="ci-anchor"
                        .value=${this.anchorId}
                        @change=${(event: Event) => (this.anchorId = (event.target as HTMLSelectElement).value)}
                      >
                        ${this.identities.find((identity) => identity.id === this.subscriptionId)?.keys.map((key) => html`<option value=${key.id}>${key.id}${key.is_active ? '' : ' (revoked)'}</option>`)}
                      </select></label
                    ><label
                      >Callback URL<input
                        id="ci-callback-url"
                        type="url"
                        .value=${this.callbackUrl}
                        @input=${(event: Event) => (this.callbackUrl = (event.target as HTMLInputElement).value)} /></label
                    ><label
                      >Description<input
                        id="ci-callback-description"
                        .value=${this.callbackDescription}
                        @input=${(event: Event) => (this.callbackDescription = (event.target as HTMLInputElement).value)} /></label
                    ><button
                      ?disabled=${this.busy || !!this.secret || !this.anchorId || !this.callbackUrl}
                      @click=${this.subscribe}
                    >
                      Create completion callback</button
                    ><button @click=${() => (this.subscriptionId = '')}>
                      Cancel callback
                    </button>
                  </section>`
                : nothing
            }`
          : nothing
      }
      ${
        this.secret
          ? html`<dialog
              aria-modal="true"
              aria-label=${this.secretLabel}
              @close=${this.clearSecret}
              @cancel=${(event: Event) => {
                event.preventDefault();
                this.clearSecret();
              }}
            >
              <h2>${this.secretLabel}</h2>
              <p>
                Shown once. Store in your CI secret store or a protected
                credential file. Keep it out of logs, URLs, command history and
                source control.
              </p>
              <code class="ci-secret">${this.secret}</code>
              ${
                this.secretError
                  ? html`<p role="alert">${this.secretError}</p>`
                  : nothing
              }
              <button
                @click=${async () => {
                  try {
                    await navigator.clipboard.writeText(this.secret);
                    this.secretError = '';
                  } catch {
                    this.secretError =
                      'Copy failed. Select the secret and copy it manually.';
                  }
                }}
              >
                Copy secret</button
              ><button id="ci-secret-close" @click=${this.clearSecret}>
                Close and forget secret
              </button>
            </dialog>`
          : nothing
      }`;
  }
}
