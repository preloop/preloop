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
        font-size: var(--console-text-h1);
        font-weight: 600;
        letter-spacing: -0.01em;
      }
      .description,
      ::slotted([slot='description']) {
        margin: var(--sl-spacing-2x-small) 0 0;
        color: var(--sl-color-neutral-500);
        font-size: var(--console-text-meta);
      }
      /* Page-level meta ("Updated just now") sits opposite the title, in the
         meta register: it says when, not what. */
      ::slotted([slot='meta']) {
        color: var(--sl-color-neutral-500);
        font-size: var(--console-text-meta);
        font-variant-numeric: tabular-nums;
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
            <slot name="meta"></slot>
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
