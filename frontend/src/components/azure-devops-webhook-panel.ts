import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import * as api from '../api';

const GUIDE_URL = 'https://docs.preloop.ai/guide/azure-devops-tracker/';

/**
 * Service hook setup for an Azure DevOps tracker: callback URL, secret
 * header, required events and registration state, with actions to generate
 * a secret and to register the subscriptions through the stored token.
 */
@customElement('azure-devops-webhook-panel')
export class AzureDevOpsWebhookPanel extends LitElement {
  @property({ type: String }) trackerId = '';

  /** Injectable for tests. */
  @property({ attribute: false }) apiClient: Pick<
    typeof api,
    | 'getAzureDevOpsWebhookStatus'
    | 'rotateAzureDevOpsWebhookSecret'
    | 'registerAzureDevOpsWebhook'
  > = api;

  @state() private status: api.AzureDevOpsWebhookStatus | null = null;
  @state() private secret = '';
  @state() private busy = false;
  @state() private error = '';

  static styles = css`
    :host {
      display: block;
      margin: 1rem 0;
    }
    dl {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 0.25rem 1rem;
      margin: 0.5rem 0;
    }
    dt {
      font-weight: var(--sl-font-weight-semibold);
    }
    dd {
      margin: 0;
      word-break: break-all;
    }
    .actions {
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-top: 0.5rem;
    }
  `;

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('trackerId') && this.trackerId) {
      void this.load(false);
    }
  }

  async load(check: boolean): Promise<void> {
    this.busy = true;
    this.error = '';
    try {
      this.status = await this.apiClient.getAzureDevOpsWebhookStatus(
        this.trackerId,
        check
      );
    } catch (error: any) {
      this.error = error?.message || 'Failed to read the service hook status';
    } finally {
      this.busy = false;
    }
  }

  private async rotate(): Promise<void> {
    this.busy = true;
    this.error = '';
    try {
      const result = await this.apiClient.rotateAzureDevOpsWebhookSecret(
        this.trackerId
      );
      this.secret = result.secret ?? '';
      this.status = result;
    } catch (error: any) {
      this.error = error?.message || 'Failed to generate the secret';
    } finally {
      this.busy = false;
    }
  }

  private async register(): Promise<void> {
    this.busy = true;
    this.error = '';
    try {
      this.status = await this.apiClient.registerAzureDevOpsWebhook(
        this.trackerId
      );
    } catch (error: any) {
      this.error = error?.message || 'Failed to register the service hooks';
    } finally {
      this.busy = false;
    }
  }

  private registrationLabel(): string {
    const registration = this.status?.registration;
    if (!registration) return 'Not checked';
    const status = String(registration.status || 'unknown').replace(/_/g, ' ');
    const ids = registration.ids?.length
      ? ` (${registration.ids.length} subscriptions)`
      : '';
    return `${status}${ids}`;
  }

  render() {
    const status = this.status;
    const header = status?.header_name || 'X-Preloop-Webhook-Secret';
    const ready =
      status?.signature === 'configured' &&
      status?.registration?.status === 'registered';
    return html`
      <sl-alert variant=${ready ? 'success' : 'neutral'} open>
        <sl-icon slot="icon" name="broadcast"></sl-icon>
        <strong>Azure DevOps service hooks</strong>
        ${
          this.error
            ? html`<p class="error" role="alert">${this.error}</p>`
            : nothing
        }
        ${
          status
            ? html`
                <dl>
                  <dt>Callback URL</dt>
                  <dd class="callback-url">
                    ${
                      status.callback_url
                        ? html`<code>${status.callback_url}</code>`
                        : 'Not configured (PRELOOP_URL is unset)'
                    }
                  </dd>
                  <dt>HTTP header</dt>
                  <dd class="header-name">
                    <code>${header}:&lt;secret&gt;</code>
                  </dd>
                  <dt>Secret</dt>
                  <dd class="signature">
                    ${
                      status.signature === 'configured'
                        ? 'Configured'
                        : 'Missing: generate a secret before registering'
                    }
                  </dd>
                  <dt>Events</dt>
                  <dd class="events">${status.required_events.join(', ')}</dd>
                  <dt>Registration</dt>
                  <dd class="registration">${this.registrationLabel()}</dd>
                  <dt>Last review label poll</dt>
                  <dd class="review-poll">
                    ${status.review_requests?.last_poll_at || 'Not polled yet'}
                    ${
                      status.review_requests?.poll_error
                        ? ' — polling failed; check tracker access and worker logs'
                        : nothing
                    }
                  </dd>
                </dl>
                <div class="review-requests" role="status">
                  ${(status.review_requests?.recent || []).map(
                    (request) =>
                      html`<p>
                        PR #${request.number} (${request.kind}):
                        ${request.reason.replace(/_/g, ' ')}.
                        ${request.flow_id ? html`Flow ${request.flow_id}.` : nothing}
                        <time datetime=${request.at}>${request.at}</time>
                      </p>`
                  )}
                </div>
                ${
                  status.last_ignored_reason
                    ? html`<p class="last-ignored" role="status">
                        Last event ignored:
                        ${
                          status.last_ignored_reason ===
                          'repository_not_imported'
                            ? 'repository not imported'
                            : status.last_ignored_reason ===
                                'repository_outside_scope'
                              ? 'repository outside selected scope'
                              : status.last_ignored_reason.replace(/_/g, ' ')
                        }.
                        Edit the tracker scope or the service hook repository
                        filter.
                      </p>`
                    : nothing
                }
                ${
                  status.instructions
                    ? html`<p class="instructions">${status.instructions}</p>`
                    : nothing
                }
              `
            : nothing
        }
        ${
          this.secret
            ? html`<p class="secret">
                Copy this secret now; it is shown only once:
                <code>${this.secret}</code>
              </p>`
            : nothing
        }
        <div class="actions">
          <sl-button
            size="small"
            class="rotate"
            ?disabled=${this.busy}
            @click=${() => this.rotate()}
            >${
              status?.signature === 'configured'
                ? 'Rotate secret'
                : 'Generate secret'
            }</sl-button
          >
          <sl-button
            size="small"
            variant="primary"
            class="register"
            ?disabled=${
              this.busy ||
              !status?.callback_configured ||
              status?.signature !== 'configured'
            }
            @click=${() => this.register()}
            >Register</sl-button
          >
          <sl-button
            size="small"
            variant="text"
            class="check"
            ?disabled=${this.busy}
            @click=${() => this.load(true)}
            >Check</sl-button
          >
          <sl-button
            size="small"
            variant="text"
            href=${GUIDE_URL}
            target="_blank"
            >Setup guide</sl-button
          >
        </div>
      </sl-alert>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'azure-devops-webhook-panel': AzureDevOpsWebhookPanel;
  }
}
