import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import qrcode from 'qrcode-generator';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import { getFeatures } from '../api';
import { confirmDialog } from './confirm-dialog';
import { mfaMessage, mfaRequest, storeMfaTokens } from '../services/mfa';
import consoleStyles from '../styles/console-styles.css?inline';

@customElement('mfa-card')
export class MfaCard extends LitElement {
  @state() private enabled = false;
  @state() private enrollmentAvailable = true;
  @state() private remaining = 0;
  @state() private loaded = false;
  @state() private secret = '';
  @state() private qr = '';
  @state() private code = '';
  @state() private recovery = false;
  @state() private codes: string[] = [];
  @state() private saved = false;
  @state() private busy = false;
  @state() private message = '';

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .card-body {
        display: grid;
        gap: var(--sl-spacing-medium);
      }
      p {
        margin: 0;
      }
      img {
        width: 208px;
        max-width: 100%;
        image-rendering: pixelated;
      }
      code {
        overflow-wrap: anywhere;
      }
      ul {
        columns: 2;
        padding-left: var(--sl-spacing-large);
      }
      .actions {
        display: flex;
        gap: var(--sl-spacing-small);
        flex-wrap: wrap;
      }
      .remove {
        margin-top: var(--sl-spacing-large);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.secret = this.qr = this.code = '';
    this.codes = [];
  }

  private async load(): Promise<void> {
    try {
      const [data, features] = await Promise.all([
        mfaRequest('status'),
        getFeatures(),
      ]);
      this.enrollmentAvailable = features.features.mfa_totp !== false;
      this.enabled = data.totp_enabled;
      this.remaining = data.recovery_codes_remaining;
      this.loaded = true;
    } catch (error) {
      this.message = mfaMessage(error);
    }
  }

  private async run(action: () => Promise<void>): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.message = '';
    try {
      await action();
    } catch (error) {
      this.message = mfaMessage(error);
    } finally {
      this.busy = false;
      this.code = '';
    }
  }

  private start(): void {
    void this.run(async () => {
      const data = await mfaRequest('enroll', {});
      this.secret = data.secret;
      const qr = qrcode(0, 'M');
      qr.addData(data.otpauth_uri);
      qr.make();
      this.qr = qr.createDataURL(4, 8);
    });
  }

  private confirm(event: Event): void {
    event.preventDefault();
    void this.run(async () => {
      const data = await mfaRequest('enroll/confirm', { code: this.code });
      storeMfaTokens(data);
      this.secret = this.qr = '';
      this.enabled = true;
      this.remaining = 10;
      this.codes = data.recovery_codes;
      this.saved = false;
    });
  }

  private regenerate(): void {
    void this.run(async () => {
      const confirmed = await confirmDialog({
        title: 'Replace recovery codes',
        message: 'Your existing recovery codes will stop working.',
        confirmLabel: 'Replace codes',
      });
      if (!confirmed) return;
      const data = await mfaRequest('recovery-codes', { code: this.code });
      this.codes = data.recovery_codes;
      this.remaining = 10;
      this.saved = false;
    });
  }

  private disable(): void {
    void this.run(async () => {
      const confirmed = await confirmDialog({
        title: 'Turn off authenticator app',
        message:
          'Password sign-in will no longer ask for an authenticator code.',
        confirmLabel: 'Turn off',
        variant: 'danger',
      });
      if (!confirmed) return;
      await mfaRequest('disable', {
        [this.recovery ? 'recovery_code' : 'code']: this.code,
      });
      this.enabled = false;
      this.remaining = 0;
      this.recovery = false;
      this.message = 'Authenticator app turned off.';
    });
  }

  private download(): void {
    const url = URL.createObjectURL(
      new Blob(
        [
          `Preloop recovery codes\nKeep these private. Each code works once.\n\n${this.codes.join('\n')}\n`,
        ],
        { type: 'text/plain' }
      )
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = 'preloop-recovery-codes.txt';
    link.click();
    URL.revokeObjectURL(url);
  }

  private proofInput(): unknown {
    return html`<sl-input
      label=${this.recovery ? 'Recovery code' : 'Authenticator code'}
      autocomplete="one-time-code"
      inputmode=${this.recovery ? 'text' : 'numeric'}
      maxlength=${this.recovery ? '11' : '6'}
      .value=${this.code}
      @sl-input=${(e: Event) => {
        this.code = (e.target as HTMLInputElement).value;
      }}
      required
    ></sl-input>`;
  }

  render() {
    return html`<section class="card" aria-labelledby="mfa-title">
      <div class="card-header"><h3 id="mfa-title">Authenticator app</h3></div>
      <div class="card-body">
        ${this.message ? html`<p role="status">${this.message}</p>` : nothing}
        ${
          !this.loaded
            ? html`<p>Loading authenticator settings…</p>`
            : this.codes.length
              ? html`
                  <p>
                    Save these recovery codes somewhere private. Each works once
                    if you lose your phone. They will not be shown again.
                  </p>
                  <ul>
                    ${this.codes.map((code) => html`<li><code>${code}</code></li>`)}
                  </ul>
                  <div class="actions">
                    <sl-button
                      @click=${() =>
                        this.run(async () => {
                          await navigator.clipboard.writeText(
                            this.codes.join('\n')
                          );
                          this.message = 'Recovery codes copied.';
                        })}
                      >Copy codes</sl-button
                    ><sl-button @click=${this.download}
                      >Download codes</sl-button
                    >
                  </div>
                  <sl-checkbox
                    .checked=${this.saved}
                    @sl-change=${(e: Event) => {
                      this.saved = (e.target as HTMLInputElement).checked;
                    }}
                    >I saved them</sl-checkbox
                  >
                  <sl-button
                    variant="primary"
                    ?disabled=${!this.saved}
                    @click=${() => {
                      this.codes = [];
                      this.saved = false;
                    }}
                    >Done</sl-button
                  >
                `
              : this.secret
                ? html`
                    <p>
                      Scan this QR code with your authenticator app, or enter
                      the secret manually.
                    </p>
                    <img
                      src=${this.qr}
                      alt="Authenticator app enrollment QR code"
                    />
                    <code>${this.secret}</code>
                    <form @submit=${this.confirm}>
                      ${this.proofInput()}<sl-button
                        type="submit"
                        variant="primary"
                        ?loading=${this.busy}
                        >Confirm code</sl-button
                      >
                    </form>
                    <sl-button
                      ?disabled=${this.busy}
                      @click=${() => {
                        this.secret = this.qr = this.code = '';
                      }}
                      >Cancel</sl-button
                    >
                  `
                : this.enabled
                  ? html`
                      <p>
                        Authenticator app is on. ${this.remaining} recovery
                        codes remaining.
                      </p>
                      <p>
                        Enter a fresh code to replace recovery codes or turn off
                        the authenticator app.
                      </p>
                      ${this.proofInput()}
                      <sl-button
                        variant="text"
                        @click=${() => {
                          this.recovery = !this.recovery;
                          this.code = '';
                        }}
                        >${this.recovery ? 'Use an authenticator code' : 'Use a recovery code to turn off'}</sl-button
                      >
                      <sl-button
                        ?disabled=${this.recovery || this.busy || !this.code}
                        @click=${this.regenerate}
                        >Replace recovery codes</sl-button
                      >
                      <sl-button
                        class="remove"
                        variant="danger"
                        outline
                        ?disabled=${this.busy || !this.code}
                        @click=${this.disable}
                        >Turn off</sl-button
                      >
                    `
                  : !this.enrollmentAvailable
                    ? html`<p>
                        Authenticator enrollment is disabled on this server.
                      </p>`
                    : html`<p>
                          Add a code from your authenticator app after password
                          sign-in. Passkey and single sign-on continue to work.
                        </p>
                        <sl-button
                          variant="primary"
                          ?loading=${this.busy}
                          @click=${this.start}
                          >Set up authenticator app</sl-button
                        >`
        }
      </div>
    </section>`;
  }
}
