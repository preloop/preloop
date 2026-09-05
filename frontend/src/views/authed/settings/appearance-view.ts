import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { DEFAULT_THEME, Theme } from '../../../theme';

@customElement('appearance-view')
export class AppearanceView extends LitElement {
  @state()
  private selectedTheme: Theme = DEFAULT_THEME;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      sl-card {
        width: 100%;
      }
      h1 {
        font-size: var(--sl-font-size-x-large);
        font-weight: var(--sl-font-weight-bold);
        margin-bottom: var(--sl-spacing-large);
      }
      .description {
        margin-bottom: var(--sl-spacing-large);
        color: var(--sl-color-neutral-600);
      }
      sl-radio-group::part(label) {
        font-size: var(--sl-font-size-medium);
        font-weight: var(--sl-font-weight-semibold);
        margin-bottom: var(--sl-spacing-medium);
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    const storedTheme = localStorage.getItem('theme') as Theme | null;
    this.selectedTheme = storedTheme || DEFAULT_THEME;
  }

  private handleThemeChange(event: CustomEvent) {
    const target = event.target as any;
    const newTheme = target.value as Theme;
    this.selectedTheme = newTheme;
    localStorage.setItem('theme', newTheme);
    window.dispatchEvent(
      new CustomEvent('theme-change', { detail: { theme: newTheme } })
    );
  }

  render() {
    return html`
      <view-header
        headerText="Appearance"
        description="Customize the look and feel of the Preloop console."
        width="narrow"
      ></view-header>
      <div class="column-layout narrow">
        <div class="main-column">
          <sl-card>
            <sl-radio-group
              label="Theme Preference"
              value=${this.selectedTheme}
              @sl-change=${this.handleThemeChange}
            >
              <sl-radio-button value="light">
                <sl-icon slot="prefix" name="sun"></sl-icon>
                Light
              </sl-radio-button>
              <sl-radio-button value="dark">
                <sl-icon slot="prefix" name="moon"></sl-icon>
                Dark
              </sl-radio-button>
              <sl-radio-button value="system">
                <sl-icon slot="prefix" name="display"></sl-icon>
                System
              </sl-radio-button>
            </sl-radio-group>
          </sl-card>
        </div>
        <div class="side-column"></div>
      </div>
    `;
  }
}
