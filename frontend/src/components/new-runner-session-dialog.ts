import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';

import {
  RunnerSessionApiError,
  getRunnerSessionOptions,
  startRunnerSession,
  type RunnerRecord,
  type RunnerSessionOptions,
  type RunnerSessionStarted,
  type RunnerSessionWorkspace,
} from '../api';
import { consoleDialogStyles } from '../styles/console-dialog';

type WorkspaceKind = 'authorized_directory' | 'tracker_checkout';

/** Sentences for the refusal codes of the remote session API (contract C). */
export const RUNNER_SESSION_ERROR_MESSAGES: Record<string, string> = {
  not_runner_owner:
    'Only the runner owner or an account admin can start sessions on this runner.',
  runner_offline:
    'The runner is offline. Start it on the host (preloop runner start) and try again.',
  harness_not_enabled_for_sessions:
    'Remote sessions are not enabled for this harness on the host. On the host run: preloop runner sessions enable <harness>.',
  harness_signed_out:
    'The harness is signed out on the host. Sign in to it there and try again.',
  harness_disabled: 'This harness is disabled in the runner configuration.',
  max_concurrent_reached:
    'This runner already runs the maximum number of remote sessions. Stop one first.',
  workspace_not_authorized:
    'This directory is not authorized for the selected harness on the host.',
  workspace_dirty: 'The directory has uncommitted changes the runner refused.',
  checkout_not_available:
    'Repository checkouts are not available yet. Pick an authorized directory.',
  checkout_requires_app_or_oauth:
    'Checkouts need a GitHub App tracker. Personal access tokens are never sent to a host.',
  checkout_requires_oauth:
    'Checkouts need a Bitbucket Cloud OAuth tracker. Access tokens are never sent to a host.',
  checkout_source_not_supported:
    'Checkouts work with GitHub and Bitbucket Cloud trackers.',
  checkout_failed: 'The runner could not check out the repository.',
  sessions_disabled_on_host: 'Remote sessions are turned off on this host.',
  remote_sessions_unavailable:
    'Remote sessions are not available on this server yet.',
};

export function runnerSessionErrorMessage(error: unknown): string {
  if (error instanceof RunnerSessionApiError) {
    const known = error.code
      ? RUNNER_SESSION_ERROR_MESSAGES[error.code]
      : undefined;
    return known || error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/**
 * Start a session with a harness installed on one of my runners (#1483).
 *
 * The dialog asks the server what the runner offers (`session-options`):
 * harnesses with sessions enabled on the host, the directories the host
 * owner authorized, and tracker repositories it can check out. It never
 * shows host paths; directories are labels chosen on the host.
 *
 * Fires `runner-session-started` with the API response on success.
 */
@customElement('new-runner-session-dialog')
export class NewRunnerSessionDialog extends LitElement {
  @property({ type: Boolean, reflect: true }) open = false;
  /** Runners to choose from. Omit when `runnerId` is fixed. */
  @property({ attribute: false }) runners: RunnerRecord[] = [];
  /** Preselected runner (per-runner button). */
  @property({ attribute: 'runner-id' }) runnerId = '';

  @state() private selectedRunnerId = '';
  @state() private options: RunnerSessionOptions | null = null;
  @state() private loading = false;
  @state() private submitting = false;
  @state() private error = '';
  @state() private harness = '';
  @state() private model = '';
  @state() private workspaceKind: WorkspaceKind = 'authorized_directory';
  @state() private directoryId = '';
  @state() private trackerId = '';
  @state() private repository = '';
  @state() private ref = '';
  @state() private prompt = '';

  private loadToken = 0;

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: contents;
      }
      form {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }
      .hint {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin: 0;
      }
      sl-dialog::part(panel) {
        width: min(36rem, 96vw);
      }
    `,
  ];

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('open') && this.open) {
      this.reset();
      const initial =
        this.runnerId ||
        this.runners.find((r) => this.isOnline(r))?.id ||
        this.runners[0]?.id ||
        '';
      if (initial) void this.selectRunner(initial);
    }
  }

  private reset(): void {
    this.error = '';
    this.options = null;
    this.harness = '';
    this.model = '';
    this.workspaceKind = 'authorized_directory';
    this.directoryId = '';
    this.trackerId = '';
    this.repository = '';
    this.ref = '';
    this.prompt = '';
    this.submitting = false;
  }

  private isOnline(runner: RunnerRecord): boolean {
    return runner.status === 'online' || runner.status === 'busy';
  }

  async selectRunner(runnerId: string): Promise<void> {
    this.selectedRunnerId = runnerId;
    this.options = null;
    this.error = '';
    this.loading = true;
    const token = ++this.loadToken;
    try {
      const options = await getRunnerSessionOptions(runnerId);
      if (token !== this.loadToken) return;
      this.options = options;
      const firstHarness = options.harnesses.find((h) => h.available !== false);
      this.setHarness(firstHarness?.harness || '');
      if (
        this.directoriesFor(this.harness).length === 0 &&
        options.checkout_sources.length > 0
      ) {
        this.workspaceKind = 'tracker_checkout';
      }
    } catch (error) {
      if (token !== this.loadToken) return;
      this.error = runnerSessionErrorMessage(error);
    } finally {
      if (token === this.loadToken) this.loading = false;
    }
  }

  private setHarness(harness: string): void {
    this.harness = harness;
    const entry = this.options?.harnesses.find((h) => h.harness === harness);
    this.model = entry?.models[0]?.id || '';
    const dirs = this.directoriesFor(harness);
    if (!dirs.some((d) => d.id === this.directoryId)) {
      this.directoryId = dirs[0]?.id || '';
    }
  }

  private directoriesFor(harness: string) {
    return (this.options?.authorized_directories || []).filter(
      (d) =>
        !d.harnesses ||
        d.harnesses === 'all' ||
        (Array.isArray(d.harnesses) && d.harnesses.includes(harness))
    );
  }

  private get selectedSource() {
    return this.options?.checkout_sources.find(
      (s) => s.tracker_id === this.trackerId
    );
  }

  private workspace(): RunnerSessionWorkspace | null {
    if (this.workspaceKind === 'authorized_directory') {
      return this.directoryId
        ? { kind: 'authorized_directory', id: this.directoryId }
        : null;
    }
    if (!this.trackerId || !this.repository) return null;
    return {
      kind: 'tracker_checkout',
      tracker_id: this.trackerId,
      repository: this.repository,
      ref: this.ref.trim() || null,
    };
  }

  get canSubmit(): boolean {
    return Boolean(
      !this.submitting &&
      this.options?.online &&
      this.options?.sessions_available !== false &&
      this.harness &&
      this.workspace()
    );
  }

  async submit(event?: Event): Promise<void> {
    event?.preventDefault();
    const workspace = this.workspace();
    if (!this.canSubmit || !workspace) return;
    this.submitting = true;
    this.error = '';
    try {
      const started: RunnerSessionStarted = await startRunnerSession(
        this.selectedRunnerId,
        {
          harness: this.harness,
          model: this.model || null,
          workspace,
          first_prompt: this.prompt.trim() || null,
        }
      );
      this.dispatchEvent(
        new CustomEvent<RunnerSessionStarted>('runner-session-started', {
          detail: started,
          bubbles: true,
          composed: true,
        })
      );
      this.close();
    } catch (error) {
      this.error = runnerSessionErrorMessage(error);
    } finally {
      this.submitting = false;
    }
  }

  private close(): void {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('runner-session-dialog-hide', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private renderStatus() {
    const options = this.options;
    if (!options) return nothing;
    if (options.sessions_available === false) {
      return html`<sl-alert class="unavailable" variant="neutral" open>
        <sl-icon slot="icon" name="info-circle"></sl-icon>
        ${RUNNER_SESSION_ERROR_MESSAGES.remote_sessions_unavailable}
      </sl-alert>`;
    }
    if (!options.online) {
      return html`<sl-alert class="offline" variant="warning" open>
        <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
        ${RUNNER_SESSION_ERROR_MESSAGES.runner_offline}
      </sl-alert>`;
    }
    if (options.harnesses.length === 0) {
      return html`<p class="hint no-harness">
        This runner reports no harness that supports remote sessions. On the
        host, enable one with
        <code>preloop runner sessions enable copilot_cli</code>.
      </p>`;
    }
    return html`<p class="hint limits">
      ${options.limits.active} of ${options.limits.max_concurrent} remote
      sessions in use on this runner.
    </p>`;
  }

  private renderWorkspace() {
    const options = this.options;
    if (!options) return nothing;
    const dirs = this.directoriesFor(this.harness);
    const source = this.selectedSource;
    return html`
      <sl-radio-group
        class="workspace-kind"
        label="Workspace"
        value=${this.workspaceKind}
        @sl-change=${(e: Event) =>
          (this.workspaceKind = (e.target as HTMLInputElement)
            .value as WorkspaceKind)}
      >
        <sl-radio-button value="authorized_directory"
          >Authorized directory</sl-radio-button
        >
        <sl-radio-button value="tracker_checkout"
          >Repository checkout</sl-radio-button
        >
      </sl-radio-group>
      ${
        this.workspaceKind === 'authorized_directory'
          ? dirs.length === 0
            ? html`<p class="hint no-directory">
                No directory on this host is authorized for this harness. On the
                host run <code>preloop runner dirs add &lt;path&gt;</code>.
              </p>`
            : html`<sl-select
                class="directory"
                label="Directory"
                value=${this.directoryId}
                @sl-change=${(e: Event) =>
                  (this.directoryId = (e.target as HTMLSelectElement).value)}
              >
                ${dirs.map(
                  (d) =>
                    html`<sl-option value=${d.id}
                      >${d.label}${
                        d.mode === 'read_only' ? ' (read only)' : ''
                      }</sl-option
                    >`
                )}
              </sl-select>`
          : options.checkout_sources.length === 0
            ? html`<p class="hint no-tracker">
                No GitHub or Bitbucket Cloud tracker is connected to this
                account.
              </p>`
            : html`
                <sl-select
                  class="tracker"
                  label="Tracker"
                  value=${this.trackerId}
                  @sl-change=${(e: Event) => {
                    this.trackerId = (e.target as HTMLSelectElement).value;
                    this.repository = '';
                  }}
                >
                  ${options.checkout_sources.map(
                    (s) =>
                      html`<sl-option value=${s.tracker_id}
                        >${s.tracker_name || s.provider}</sl-option
                      >`
                  )}
                </sl-select>
                <sl-select
                  class="repository"
                  label="Repository"
                  value=${this.repository}
                  ?disabled=${!source}
                  @sl-change=${(e: Event) =>
                    (this.repository = (e.target as HTMLSelectElement).value)}
                >
                  ${(source?.repositories || []).map(
                    (r) =>
                      html`<sl-option value=${r.full_name}
                        >${r.full_name}</sl-option
                      >`
                  )}
                </sl-select>
                <sl-input
                  class="ref"
                  label="Branch or ref (optional)"
                  placeholder=${
                    source?.repositories.find(
                      (r) => r.full_name === this.repository
                    )?.default_branch || 'default branch'
                  }
                  value=${this.ref}
                  @sl-input=${(e: Event) =>
                    (this.ref = (e.target as HTMLInputElement).value)}
                ></sl-input>
              `
      }
    `;
  }

  render() {
    const options = this.options;
    const harness = options?.harnesses.find((h) => h.harness === this.harness);
    return html`
      <sl-dialog
        label="New session"
        ?open=${this.open}
        @sl-hide=${(e: Event) => {
          if (e.target === e.currentTarget) this.close();
        }}
      >
        <form @submit=${(e: Event) => void this.submit(e)}>
          ${
            this.runnerId
              ? nothing
              : html`<sl-select
                  class="runner"
                  label="Runner"
                  value=${this.selectedRunnerId}
                  @sl-change=${(e: Event) =>
                    void this.selectRunner(
                      (e.target as HTMLSelectElement).value
                    )}
                >
                  ${this.runners.map(
                    (r) =>
                      html`<sl-option value=${r.id}
                        >${r.name}${r.hostname ? ` (${r.hostname})` : ''}${
                          this.isOnline(r) ? '' : ' - offline'
                        }</sl-option
                      >`
                  )}
                </sl-select>`
          }
          ${this.loading ? html`<sl-spinner></sl-spinner>` : this.renderStatus()}
          ${
            options && options.harnesses.length > 0
              ? html`
                  <sl-select
                    class="harness"
                    label="Agent"
                    value=${this.harness}
                    @sl-change=${(e: Event) =>
                      this.setHarness((e.target as HTMLSelectElement).value)}
                  >
                    ${options.harnesses.map(
                      (h) =>
                        html`<sl-option
                          value=${h.harness}
                          ?disabled=${h.available === false}
                          >${h.display_name}${
                            h.available === false && h.unavailable_reason
                              ? ` (${h.unavailable_reason.replace(/_/g, ' ')})`
                              : ''
                          }</sl-option
                        >`
                    )}
                  </sl-select>
                  ${
                    harness && harness.models.length > 0
                      ? html`<sl-select
                          class="model"
                          label="Model"
                          value=${this.model}
                          @sl-change=${(e: Event) =>
                            (this.model = (
                              e.target as HTMLSelectElement
                            ).value)}
                        >
                          ${harness.models.map(
                            (m) =>
                              html`<sl-option value=${m.id}>${m.id}</sl-option>`
                          )}
                        </sl-select>`
                      : nothing
                  }
                  ${this.renderWorkspace()}
                  <sl-textarea
                    class="prompt"
                    label="First message (optional)"
                    rows="3"
                    maxlength="32000"
                    value=${this.prompt}
                    @sl-input=${(e: Event) =>
                      (this.prompt = (e.target as HTMLTextAreaElement).value)}
                  ></sl-textarea>
                  <p class="hint">
                    The host shows a notification when the session starts, and
                    the start is recorded in the audit log.
                  </p>
                `
              : nothing
          }
          ${
            this.error
              ? html`<sl-alert class="error" variant="danger" open role="alert">
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.error}
                </sl-alert>`
              : nothing
          }
        </form>
        <sl-button slot="footer" class="cancel" @click=${() => this.close()}
          >Cancel</sl-button
        >
        <sl-button
          slot="footer"
          class="start"
          variant="primary"
          ?disabled=${!this.canSubmit}
          ?loading=${this.submitting}
          @click=${() => void this.submit()}
          >Start session</sl-button
        >
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'new-runner-session-dialog': NewRunnerSessionDialog;
  }
}
