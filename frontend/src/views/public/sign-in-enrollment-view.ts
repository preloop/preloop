import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  getUserProfile,
  getFeatures,
  invalidateApiCaches,
  signOut,
} from '../../api';
import { Router } from '../../router';
import {
  registerPasskey,
  signInWithPasskey,
  passkeysSupported,
} from '../../services/passkeys';
import { storeMfaTokens } from '../../services/mfa';
import consoleStyles from '../../styles/console-styles.css?inline';
import '../../components/mfa-card';
import '@shoelace-style/shoelace/dist/components/button/button.js';

@customElement('sign-in-enrollment-view')
export class SignInEnrollmentView extends LitElement {
  @state() private passkeys = false;
  @state() private busy = false;
  @state() private error = '';
  private accountId: string | undefined;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const [user, features] = await Promise.all([
        getUserProfile(),
        getFeatures(),
      ]);
      this.accountId = user.account_id;
      this.passkeys =
        features.features.passkeys !== false && passkeysSupported();
      if (!user.enroll_only) this.complete();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Sign in again to continue.';
    }
  }

  private complete(): void {
    invalidateApiCaches();
    window.dispatchEvent(new CustomEvent('auth-change'));
    Router.go('/console');
  }

  private async passkey(register: boolean): Promise<void> {
    this.busy = true;
    this.error = '';
    try {
      if (register) await registerPasskey();
      storeMfaTokens(await signInWithPasskey(this.accountId));
      this.complete();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Passkey sign-in failed.';
    } finally {
      this.busy = false;
    }
  }

  render() {
    return html`<main>
      <h1>Protect your sign-in</h1>
      <p>
        Your account requires a strong sign-in method. Set up an authenticator
        app or a passkey to continue.
      </p>
      ${this.error ? html`<p role="status">${this.error}</p>` : nothing}
      <mfa-card @mfa-enrollment-complete=${this.complete}></mfa-card>
      ${
        this.passkeys
          ? html`<section class="card">
              <h2>Passkey</h2>
              <p>
                Use your device’s screen lock, fingerprint, or security key.
              </p>
              <sl-button
                variant="primary"
                ?loading=${this.busy}
                @click=${() => this.passkey(true)}
                >Add a passkey</sl-button
              >
              <sl-button
                ?disabled=${this.busy}
                @click=${() => this.passkey(false)}
                >Use an existing passkey</sl-button
              >
            </section>`
          : nothing
      }
      <p>If you cannot set up either, ask an account administrator for help.</p>
      <p><a href="/login">Use another sign-in method</a></p>
      <sl-button @click=${() => signOut()}>Sign out</sl-button>
    </main>`;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      main {
        max-width: 40rem;
        margin: var(--sl-spacing-2x-large) auto;
        padding: var(--sl-spacing-large);
      }
      h1 {
        font-size: var(--console-text-h1);
      }
      h2 {
        font-size: var(--console-text-card-title);
      }
      .card {
        margin-top: var(--sl-spacing-large);
      }
      sl-button {
        margin: var(--sl-spacing-x-small);
      }
      a {
        color: var(--console-link-color);
      }
    `,
  ];
}
