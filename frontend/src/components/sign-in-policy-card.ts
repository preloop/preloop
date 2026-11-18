import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  fetchWithAuth,
  getFeatures,
  getUserProfile,
  invalidateApiCaches,
  ApiError,
} from '../api';
import { REST_API_PREFIX } from '../api-path';
import { storeMfaTokens } from '../services/mfa';
import { confirmDialog } from './confirm-dialog';
import consoleStyles from '../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';

interface Member {
  user_id: string;
  email: string;
  role: string;
  passkey: boolean;
  totp: boolean;
  /** Enrolled, but this server's key cannot read it. */
  totp_unusable?: boolean;
  sso: boolean;
  reset_allowed: boolean;
}

interface Policy {
  policy: 'off' | 'admins' | 'all';
  step_up_window_minutes: number;
  members_total: number;
  members_pending_total: number;
  members_offset: number;
  members_limit: number;
  members_with_factor: number;
  members_with_totp: number;
  members_sso: number;
  members_pending: Member[];
  members: Member[];
}

@customElement('sign-in-policy-card')
export class SignInPolicyCard extends LitElement {
  @state() private available = false;
  @state() private data: Policy | null = null;
  @state() private policy: Policy['policy'] = 'off';
  @state() private windowMinutes = 0;
  @state() private busy = false;
  @state() private message = '';
  @state() private resetting: Member | null = null;
  @state() private reason = '';
  private accountId = '';

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(offset = this.data?.members_offset ?? 0): Promise<void> {
    this.busy = true;
    try {
      const features = await getFeatures();
      if (features.features.strong_sign_in_policy !== true) return;
      const user = await getUserProfile();
      this.accountId = user.account_id;
      const response = await fetchWithAuth(
        `${this.url()}?offset=${offset}&limit=50`,
        { passive: true }
      );
      if (response.status === 403 || response.status === 404) return;
      this.available = true;
      if (!response.ok)
        throw new Error('Could not load account sign-in policy.');
      this.setData(await response.json());
    } catch (error) {
      this.message =
        error instanceof Error
          ? error.message
          : 'Could not load account sign-in policy.';
    } finally {
      this.busy = false;
    }
  }

  private url(): string {
    return `${REST_API_PREFIX}/accounts/${encodeURIComponent(this.accountId)}/sign-in-policy`;
  }

  private setData(data: Policy): void {
    this.data = data;
    this.policy = data.policy;
    this.windowMinutes = data.step_up_window_minutes;
  }

  private async checked(response: Response): Promise<any> {
    if (response.status === 204) return null;
    const data = await response.json();
    if (!response.ok) {
      const messages: Record<string, string> = {
        strong_sign_in_required:
          'Confirm your sign-in with a passkey, authenticator app, or SSO before enabling this policy.',
        strong_sign_in_policy_required:
          'Enable a sign-in policy before setting a step-up window.',
        member_not_managed:
          'This member belongs to another account and is not managed by your verified domain.',
        cannot_reset_self:
          'Ask another administrator to reset your sign-in, or use server-side recovery.',
      };
      throw new ApiError(
        messages[data.detail?.code] || 'Could not save this change.',
        response.status,
        data.detail?.code,
        data.detail
      );
    }
    return data;
  }

  private async save(event: Event): Promise<void> {
    event.preventDefault();
    if (this.busy) return;
    if (
      this.policy !== 'off' &&
      this.windowMinutes !== 0 &&
      (!Number.isInteger(this.windowMinutes) ||
        this.windowMinutes < 5 ||
        this.windowMinutes > 720)
    ) {
      this.message = 'Use 0 to turn off step-up, or 5 to 720 minutes.';
      return;
    }
    this.busy = true;
    this.message = '';
    try {
      const data = await this.checked(
        await fetchWithAuth(this.url(), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            policy: this.policy,
            step_up_window_minutes:
              this.policy === 'off' ? 0 : this.windowMinutes,
          }),
        })
      );
      if (data?.access_token && data?.refresh_token) storeMfaTokens(data);
      invalidateApiCaches();
      if (data) this.setData(data);
      else await this.load();
      this.message = 'Account sign-in policy saved.';
    } catch (error) {
      this.message =
        error instanceof Error ? error.message : 'Could not save the policy.';
    } finally {
      this.busy = false;
    }
  }

  private async reset(event: Event): Promise<void> {
    event.preventDefault();
    const member = this.resetting;
    if (!member || !this.reason.trim() || this.busy) return;
    if (
      !(await confirmDialog({
        title: 'Reset member sign-in',
        message: `Remove the passkeys and authenticator app for ${member.email}?`,
        detail:
          'Verify their identity first. Their sessions will end and they will need to enroll again. Passkeys are shared across their accounts.',
        confirmLabel: 'Reset sign-in',
        variant: 'danger',
      }))
    )
      return;
    this.busy = true;
    try {
      await this.checked(
        await fetchWithAuth(
          `${REST_API_PREFIX}/accounts/${this.accountId}/members/${member.user_id}/reset-sign-in`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: this.reason.trim() }),
          }
        )
      );
      this.resetting = null;
      this.reason = '';
      await this.load();
      this.message =
        'Member sign-in reset. Ask them to sign in and enroll again.';
    } catch (error) {
      this.message =
        error instanceof Error ? error.message : 'Could not reset sign-in.';
    } finally {
      this.busy = false;
    }
  }

  render() {
    if (!this.available) return nothing;
    return html`<section class="card" aria-labelledby="sign-in-policy-title">
      <h3 id="sign-in-policy-title">Account sign-in policy</h3>
      <p>
        Require members to use an authenticator app, passkey, or single sign-on.
      </p>
      ${this.message ? html`<p role="status">${this.message}</p>` : nothing}
      ${
        this.data
          ? html`<form @submit=${this.save}>
                <sl-select
                  label="Require strong sign-in"
                  .value=${this.policy}
                  @sl-change=${(event: Event) => {
                    this.policy = (event.target as HTMLSelectElement)
                      .value as Policy['policy'];
                  }}
                >
                  <sl-option value="off">Off</sl-option
                  ><sl-option value="admins">Owners and admins</sl-option
                  ><sl-option value="all">All members</sl-option>
                </sl-select>
                <sl-input
                  type="number"
                  label="Step-up window (minutes)"
                  min="0"
                  max="720"
                  step="1"
                  .value=${String(this.windowMinutes)}
                  ?disabled=${this.policy === 'off'}
                  @sl-input=${(event: Event) => {
                    this.windowMinutes = Number(
                      (event.target as HTMLInputElement).value
                    );
                  }}
                ></sl-input>
                <p class="help">
                  Use 0 for off, or 5–720 minutes. Covered members confirm their
                  identity again before console approval decisions, API key
                  changes, and sensitive account settings.
                </p>
                <p class="help">
                  Mobile, watch, Slack, and Mattermost approval channels use
                  device pairing and channel identity. They do not require
                  console step-up.
                </p>
                <sl-button type="submit" variant="primary" ?loading=${this.busy}
                  >Save policy</sl-button
                >
              </form>
              <p>
                ${this.data.members_with_factor} of ${this.data.members_total}
                members have a local factor; ${this.data.members_with_totp} use
                an authenticator app. ${this.data.members_sso} use enforced SSO.
                ${this.data.members_pending_total} have no factor.
              </p>
              <div class="coverage">
                <table>
                  <caption>
                    Member sign-in coverage
                  </caption>
                  <thead>
                    <tr>
                      <th>Member</th>
                      <th>Role</th>
                      <th>Factors</th>
                      <th>Recovery</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this.data.members.map(
                      (member) =>
                        html`<tr>
                          <td>${member.email}</td>
                          <td>${member.role}</td>
                          <td>
                            ${[member.passkey && 'Passkey', member.totp && 'Authenticator app', member.totp_unusable && 'Authenticator app (unusable, needs to be set up again)', member.sso && 'SSO'].filter(Boolean).join(', ') || 'Enrollment needed'}
                          </td>
                          <td>
                            ${
                              member.reset_allowed
                                ? html`<sl-button
                                    size="small"
                                    ?disabled=${this.busy}
                                    @click=${() => {
                                      this.resetting = member;
                                      this.reason = '';
                                    }}
                                    >Reset sign-in</sl-button
                                  >`
                                : nothing
                            }
                          </td>
                        </tr>`
                    )}
                  </tbody>
                </table>
              </div>
              <nav aria-label="Member coverage pages">
                <sl-button
                  ?disabled=${this.busy || this.data.members_offset === 0}
                  @click=${() => {
                    this.resetting = null;
                    void this.load(
                      Math.max(
                        0,
                        this.data!.members_offset - this.data!.members_limit
                      )
                    );
                  }}
                  >Previous</sl-button
                >
                <span
                  >${this.data.members_total ? this.data.members_offset + 1 : 0}–${this.data.members_offset + this.data.members.length}
                  of ${this.data.members_total}</span
                >
                <sl-button
                  ?disabled=${this.busy || this.data.members_offset + this.data.members_limit >= this.data.members_total}
                  @click=${() => {
                    this.resetting = null;
                    void this.load(
                      this.data!.members_offset + this.data!.members_limit
                    );
                  }}
                  >Next</sl-button
                >
              </nav>
              ${
                this.resetting
                  ? html`<form @submit=${this.reset}>
                      <p>Reset sign-in for ${this.resetting.email}</p>
                      <sl-input
                        label="Reason for reset"
                        required
                        maxlength="1000"
                        .value=${this.reason}
                        @sl-input=${(event: Event) => {
                          this.reason = (
                            event.target as HTMLInputElement
                          ).value;
                        }}
                      ></sl-input>
                      <sl-button
                        type="submit"
                        variant="danger"
                        ?disabled=${!this.reason.trim() || this.busy}
                        >Reset member sign-in</sl-button
                      >
                      <sl-button
                        @click=${() => {
                          this.resetting = null;
                        }}
                        >Cancel</sl-button
                      >
                    </form>`
                  : nothing
              }
              <p class="help">
                If the last administrator loses every factor, a server operator
                can disable this policy and reset the lost factor using the
                recovery scripts.
              </p>`
          : nothing
      }
    </section>`;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      h3 {
        font-size: var(--console-text-card-title);
        margin-top: 0;
      }
      form {
        display: grid;
        gap: var(--sl-spacing-medium);
      }
      .help {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .coverage {
        overflow-x: auto;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      caption {
        text-align: left;
        font-weight: 600;
        margin-bottom: var(--sl-spacing-small);
      }
      th,
      td {
        text-align: left;
        padding: var(--sl-spacing-small);
        border-bottom: 1px solid var(--console-hairline);
      }
    `,
  ];
}
