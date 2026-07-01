import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getUserProfile, updateUserProfile } from '../../../api';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import consoleStyles from '../../../styles/console-styles.css?inline';

@customElement('profile-view')
export class ProfileView extends LitElement {
  @state()
  private user: { username: string; email: string; full_name: string } | null =
    null;

  @state()
  private fullName = '';

  @state()
  private updateProfileMessage = '';

  async connectedCallback() {
    super.connectedCallback();
    await this.loadAccountDetails();
  }

  async loadAccountDetails() {
    try {
      this.user = await getUserProfile();
      this.fullName = this.user?.full_name || '';
    } catch (error) {
      console.error('Failed to load account details', error);
      this.updateProfileMessage = 'Failed to load account details.';
    }
  }

  async handleUpdateProfile(event: Event) {
    event.preventDefault();
    try {
      await updateUserProfile({ full_name: this.fullName });
      this.updateProfileMessage = 'Profile updated successfully.';
    } catch (error) {
      this.updateProfileMessage = 'Failed to update profile.';
    }
  }

  render() {
    return html`
      <view-header headerText="Profile" width="narrow"> </view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <div class="card">
            <div class="card-body">
              <form @submit="${this.handleUpdateProfile}">
                <sl-input
                  label="Username"
                  .value="${this.user?.username || ''}"
                  readonly
                ></sl-input>
                <sl-input
                  label="Email"
                  .value="${this.user?.email || ''}"
                  readonly
                ></sl-input>
                <sl-input
                  label="Full Name"
                  .value="${this.fullName}"
                  @sl-input="${(e: Event) =>
                    (this.fullName = (e.target as HTMLInputElement).value)}"
                ></sl-input>
                <sl-button variant="primary" type="submit"
                  >Update Profile</sl-button
                >
                ${
                  this.updateProfileMessage
                    ? html`<p>${this.updateProfileMessage}</p>`
                    : ''
                }
              </form>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      h2,
      h3 {
        margin: 0;
      }
      form {
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }

      sl-input[readonly]::part(base) {
        background-color: var(--sl-color-neutral-100);
        color: var(--sl-color-neutral-500);
      }

      sl-input[readonly]::part(input) {
        cursor: not-allowed;
      }

      sl-button {
        width: 12em;
      }
    `,
  ];
}
