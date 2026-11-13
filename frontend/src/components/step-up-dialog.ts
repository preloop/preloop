import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import { consoleDialogStyles } from '../styles/console-dialog';
import { mfaRequest, mfaMessage, storeMfaTokens } from '../services/mfa';
import { signInWithPasskey } from '../services/passkeys';
import { fetchWithAuth } from '../api';
import { REST_API_PREFIX } from '../api-path';
import { Router } from '../router';

@customElement('step-up-dialog')
export class StepUpDialog extends LitElement {
  @state() private methods: string[] = [];
  @state() private open = false;
  @state() private busy = false;
  @state() private code = '';
  @state() private error = '';
  private resolve: ((verified: boolean) => void) | null = null;
  private generation = 0;

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: contents;
      }
      form {
        display: grid;
        gap: var(--sl-spacing-medium);
      }
      p {
        color: var(--console-meta-color);
      }
    `,
  ];

  ask(methods: string[]): Promise<boolean> {
    this.finish(false);
    this.methods = methods;
    this.open = true;
    this.error = this.code = '';
    return new Promise((resolve) => {
      this.resolve = resolve;
    });
  }

  private finish(verified: boolean): void {
    this.generation++;
    this.open = false;
    this.code = '';
    this.resolve?.(verified);
    this.resolve = null;
  }

  private async verify(method: 'totp' | 'passkey'): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.error = '';
    const generation = this.generation;
    try {
      let data;
      if (method === 'totp') {
        data = await mfaRequest('step-up', { code: this.code });
      } else {
        const response = await fetchWithAuth(
          `${REST_API_PREFIX}/auth/users/me`
        );
        if (!response.ok)
          throw new Error('Could not read the current account.');
        const user = await response.json();
        data = await signInWithPasskey(user.account_id, true);
      }
      if (generation !== this.generation) return;
      storeMfaTokens(data);
      this.finish(true);
    } catch (error) {
      if (generation === this.generation) this.error = mfaMessage(error);
    } finally {
      this.busy = false;
      this.code = '';
    }
  }

  private sso(): void {
    const token = localStorage.getItem('accessToken');
    try {
      const claims = JSON.parse(
        atob((token?.split('.')[1] || '').replace(/-/g, '+').replace(/_/g, '/'))
      );
      const connection = claims.sso_connection_id;
      if (typeof connection !== 'string' || !/^[\da-f-]{36}$/i.test(connection))
        throw new Error();
      localStorage.setItem(
        'loginRedirect',
        location.pathname + location.search
      );
      window.location.assign(
        `${REST_API_PREFIX}/auth/sso/${encodeURIComponent(connection)}/start`
      );
    } catch {
      this.error = 'Sign in again through your identity provider.';
    }
  }

  render() {
    return html`<sl-dialog
      label="Confirm it’s you"
      ?open=${this.open}
      @sl-after-hide=${(event: Event) => {
        if (event.target === event.currentTarget) this.finish(false);
      }}
    >
      <p>Confirm your identity to continue this action.</p>
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
      ${
        this.methods.includes('totp')
          ? html`<form
              @submit=${(e: Event) => {
                e.preventDefault();
                void this.verify('totp');
              }}
            >
              <sl-input
                label="Authenticator code"
                autocomplete="one-time-code"
                inputmode="numeric"
                maxlength="6"
                .value=${this.code}
                @sl-input=${(e: Event) => {
                  this.code = (e.target as HTMLInputElement).value;
                }}
                required
              ></sl-input
              ><sl-button type="submit" variant="primary" ?loading=${this.busy}
                >Verify code</sl-button
              >
            </form>`
          : nothing
      }
      ${this.methods.includes('passkey') ? html`<sl-button ?disabled=${this.busy} @click=${() => this.verify('passkey')}>Use a passkey</sl-button>` : nothing}
      ${this.methods.includes('sso') ? html`<sl-button @click=${this.sso}>Continue with single sign-on</sl-button>` : nothing}
      ${
        this.methods.includes('enroll')
          ? html`<p>
                Set up an authenticator app or passkey in Security settings
                first.
              </p>
              <sl-button
                @click=${() => {
                  this.finish(false);
                  Router.go('/console/settings/security');
                }}
                >Open Security settings</sl-button
              >`
          : nothing
      }
      <sl-button slot="footer" @click=${() => this.finish(false)}
        >Cancel</sl-button
      >
    </sl-dialog>`;
  }
}

let pending: Promise<boolean> | null = null;

/** One shared prompt; callers retry their own request once after it succeeds. */
export function requestStepUp(methods: string[]): Promise<boolean> {
  if (pending) return pending;
  let dialog = document.querySelector<StepUpDialog>('step-up-dialog');
  if (!dialog) {
    dialog = document.createElement('step-up-dialog') as StepUpDialog;
    document.body.append(dialog);
  }
  pending = dialog.ask(methods).finally(() => {
    pending = null;
  });
  return pending;
}
