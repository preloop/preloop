import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { User, Team } from '../types.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';

export type NotifyRecipientsValue = {
  userIds: string[];
  teamIds: string[];
  customEmails: string[];
};

@customElement('notify-recipients-field')
export class NotifyRecipientsField extends LitElement {
  @property({ type: Array }) users: User[] = [];
  @property({ type: Array }) teams: Team[] = [];
  @property({ type: Array }) userIds: string[] = [];
  @property({ type: Array }) teamIds: string[] = [];
  @property({ type: Array }) customEmails: string[] = [];
  @property({ type: String }) label = 'Notify';
  @property({ type: Boolean }) showCustomEmails = true;
  @property({ type: Boolean }) required = false;
  @property({ type: String }) helpText =
    'Recipients are notified using their email and mobile app preferences.';

  @state() private customEmailInput = '';
  @state() private customEmailError = '';

  private static readonly EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  private isValidEmail(value: string): boolean {
    return NotifyRecipientsField.EMAIL_PATTERN.test(value);
  }

  static styles = css`
    :host {
      display: block;
    }
    .form-field {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-3x-small);
    }
    .form-label {
      font-size: var(--sl-font-size-small);
      font-weight: var(--sl-font-weight-semibold);
      color: var(--sl-color-neutral-700);
    }
    .form-label.required::after {
      content: ' *';
      color: var(--sl-color-danger-600);
    }
    .help-text {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-500);
    }
    .custom-email-row {
      display: flex;
      gap: var(--sl-spacing-x-small);
      align-items: flex-start;
    }
    .custom-email-row sl-input {
      flex: 1;
    }
    .email-badges {
      display: flex;
      gap: var(--sl-spacing-2x-small);
      flex-wrap: wrap;
      margin-top: var(--sl-spacing-2x-small);
    }
  `;

  private dispatchChange() {
    this.dispatchEvent(
      new CustomEvent('notify-recipients-change', {
        bubbles: true,
        composed: true,
        detail: {
          userIds: [...this.userIds],
          teamIds: [...this.teamIds],
          customEmails: [...this.customEmails],
        } satisfies NotifyRecipientsValue,
      })
    );
  }

  private handleRecipientChange(event: Event) {
    const select = event.target as HTMLSelectElement & { value: string[] };
    const values = select.value || [];
    this.userIds = values
      .filter((value) => value.startsWith('user:'))
      .map((value) => value.replace('user:', ''));
    this.teamIds = values
      .filter((value) => value.startsWith('team:'))
      .map((value) => value.replace('team:', ''));
    this.dispatchChange();
  }

  private addCustomEmail() {
    const email = this.customEmailInput.trim();
    if (!email) {
      this.customEmailError = '';
      return;
    }
    if (!this.isValidEmail(email)) {
      this.customEmailError = 'Enter a valid email address.';
      return;
    }
    if (this.customEmails.includes(email)) {
      this.customEmailError = 'That email is already listed.';
      return;
    }
    this.customEmails = [...this.customEmails, email];
    this.customEmailInput = '';
    this.customEmailError = '';
    this.dispatchChange();
  }

  private removeCustomEmail(email: string) {
    this.customEmails = this.customEmails.filter((entry) => entry !== email);
    this.dispatchChange();
  }

  render() {
    return html`
      <div class="form-field">
        <label class="form-label${this.required ? ' required' : ''}"
          >${this.label}</label
        >
        <sl-select
          placeholder="Select users or teams..."
          multiple
          clearable
          hoist
          .value=${[
            ...this.userIds.map((id) => `user:${id}`),
            ...this.teamIds.map((id) => `team:${id}`),
          ]}
          @sl-change=${this.handleRecipientChange}
        >
          ${this.users.map(
            (user) => html`
              <sl-option value=${`user:${user.id}`}>
                ${user.username} (${user.email})
              </sl-option>
            `
          )}
          ${
            this.users.length > 0 && this.teams.length > 0
              ? html`<sl-divider></sl-divider>`
              : null
          }
          ${this.teams.map(
            (team) => html`
              <sl-option value=${`team:${team.id}`}>${team.name}</sl-option>
            `
          )}
        </sl-select>
        <div class="help-text">${this.helpText}</div>

        ${
          this.showCustomEmails
            ? html`
                <div class="custom-email-row">
                  <sl-input
                    placeholder="Add custom email"
                    .value=${this.customEmailInput}
                    @sl-input=${(event: Event) => {
                      this.customEmailInput = (
                        event.target as HTMLInputElement
                      ).value;
                      if (this.customEmailError) {
                        this.customEmailError = '';
                      }
                    }}
                    @keydown=${(event: KeyboardEvent) => {
                      if (event.key === 'Enter') {
                        event.preventDefault();
                        this.addCustomEmail();
                      }
                    }}
                  ></sl-input>
                  <sl-button @click=${this.addCustomEmail}>Add</sl-button>
                </div>
                ${
                  this.customEmailError
                    ? html`<div
                        class="help-text"
                        style="color: var(--sl-color-danger-600);"
                      >
                        ${this.customEmailError}
                      </div>`
                    : null
                }
                ${
                  this.customEmails.length > 0
                    ? html`
                        <div class="email-badges">
                          ${this.customEmails.map(
                            (email) => html`
                              <sl-badge
                                variant="neutral"
                                style="cursor: pointer;"
                                @click=${() => this.removeCustomEmail(email)}
                              >
                                ${email} ×
                              </sl-badge>
                            `
                          )}
                        </div>
                      `
                    : null
                }
              `
            : null
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'notify-recipients-field': NotifyRecipientsField;
  }
}
