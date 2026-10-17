import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import type SlSelect from '@shoelace-style/shoelace/dist/components/select/select.js';
import { consentOption, type ConsentOption } from '../utils/owner-consent';
import consoleStyles from '../styles/console-styles.css?inline';

/**
 * One owner consent setting; the host renders the visible label. The owner gets a select with the chosen
 * option's help under it; everyone else reads the value and who can change
 * it. Emits `consent-change` with `{ value }`; the host saves.
 */
@customElement('owner-consent-control')
export class OwnerConsentControl extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      sl-select {
        max-width: 16em;
      }
      .help {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        margin-top: var(--sl-spacing-2x-small);
        max-width: 36em;
      }
      .error {
        color: var(--sl-color-danger-700);
        font-size: var(--console-text-meta);
      }
    `,
  ];

  @property() label = '';
  @property() value = '';
  @property({ attribute: false }) options: ConsentOption<string>[] = [];
  @property({ type: Boolean }) editable = false;
  @property({ type: Boolean }) saving = false;
  /** Read-only explanation, e.g. "Only the runner owner can change this." */
  @property() ownerOnly = '';
  @property() error = '';
  /** Hide the help line, for tight table cells. */
  @property({ type: Boolean }) compact = false;

  render() {
    const current = consentOption(this.options, this.value);
    if (!this.editable) {
      if (this.compact) {
        const title = [current.help, this.ownerOnly].filter(Boolean).join(' ');
        return html`<span title=${title}>${current.label}</span>`;
      }
      return html`<span>${current.label}</span>
        <div class="help">${current.help}</div>
        ${this.ownerOnly ? html`<div class="help">${this.ownerOnly}</div>` : nothing}`;
    }
    return html`
      <sl-select
        size="small"
        aria-label=${this.label}
        value=${current.value}
        title=${current.help}
        ?disabled=${this.saving}
        @sl-change=${(event: Event) => {
          const value = (event.currentTarget as SlSelect).value as string;
          if (value === current.value) return;
          this.dispatchEvent(
            new CustomEvent('consent-change', {
              detail: { value },
              bubbles: true,
              composed: true,
            })
          );
        }}
      >
        ${this.options.map(
          (option) =>
            html`<sl-option value=${option.value}>${option.label}</sl-option>`
        )}
      </sl-select>
      ${this.compact ? nothing : html`<div class="help">${current.help}</div>`}
      ${this.error ? html`<div class="error" role="alert">${this.error}</div>` : nothing}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'owner-consent-control': OwnerConsentControl;
  }
}
