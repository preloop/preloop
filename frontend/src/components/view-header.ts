import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import consoleStyles from '../styles/console-styles.css?inline';

@customElement('view-header')
export class ViewHeader extends LitElement {
  @property({ type: String })
  headerText = '';

  @property({ type: String })
  description = '';

  @property({ type: String })
  width = '';

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-medium);
      }
      h1 {
        margin: 0;
      }
      .description {
        margin: var(--sl-spacing-2x-small) 0 0;
        color: var(--sl-color-neutral-500);
        font-size: 0.9rem;
      }
    `,
  ];

  render() {
    return html`
      <div class="column-layout ${this.width}">
        <div class="main-column">
          <slot name="top"></slot>
          <div class="header">
            <h1 style="display: flex; align-items: center; gap: 12px;">
              <slot name="title-prefix"></slot>${this.headerText}
            </h1>
            <slot name="main-column"></slot>
          </div>
          ${
            this.description
              ? html`<p class="description">${this.description}</p>`
              : nothing
          }
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'view-header': ViewHeader;
  }
}
