import { LitElement, html, css, unsafeCSS } from 'lit';
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
      :host {
        display: block;
        /* The header owns the gap to the page content below it. Pages must
           not add their own spacers or negative margins to compensate. */
        margin-bottom: var(--sl-spacing-large);
      }
      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-medium);
      }
      h1 {
        margin: 0;
      }
      .description,
      ::slotted([slot='description']) {
        margin: var(--sl-spacing-2x-small) 0 0;
        color: var(--sl-color-neutral-500);
        font-size: 0.9rem;
      }
      ::slotted([slot='title-suffix']) {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
        font-weight: 400;
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
              <slot name="title-prefix"></slot>${this.headerText}<slot
                name="title-suffix"
              ></slot>
            </h1>
            <slot name="main-column"></slot>
          </div>
          ${
            this.description
              ? html`<p class="description">${this.description}</p>`
              : html`<slot name="description"></slot>`
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
