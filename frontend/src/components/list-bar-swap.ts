import { LitElement, css, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';

/**
 * One row, two occupants: what a list already keeps on that row, and the bulk
 * bar that takes the row over while something is selected.
 *
 * A bulk bar that is inserted when the first checkbox is ticked pushes the
 * table down by its own height, so the row the operator aimed at moves under
 * the cursor at the exact moment they picked it. This element removes the
 * insertion: both occupants are always in the layout, stacked in the same
 * CSS grid cell, and selecting only decides which one is visible. The cell is
 * as tall as the taller occupant at every width, so its height does not
 * depend on the selection and nothing below it can move.
 *
 * `visibility: hidden` (not `display: none`) is what keeps the geometry: the
 * hidden layer is still measured, still out of the accessibility tree, and
 * `inert` keeps its search field and buttons out of the tab order. The swap
 * is an opacity change under the console's motion budget, with the
 * reduced-motion guard in the same rule.
 *
 * Pages use it through `list-toolbar` (which stacks its own filter row) or
 * directly, around any row that can stand in for a toolbar.
 */
@customElement('list-bar-swap')
export class ListBarSwap extends LitElement {
  /** True while a selection exists: the bulk layer shows, the base hides. */
  @property({ type: Boolean, reflect: true }) selecting = false;

  static styles = css`
    :host {
      display: block;
      width: 100%;
    }
    .stack {
      display: grid;
      /* One cell, two items: the row is as tall as the taller of the two and
         stays that height whether or not anything is selected. */
      align-items: center;
    }
    .layer {
      grid-area: 1 / 1;
      min-width: 0;
      transition: opacity 120ms ease-out;
    }
    .layer[data-hidden] {
      visibility: hidden;
      opacity: 0;
      pointer-events: none;
    }
    @media (prefers-reduced-motion: reduce) {
      .layer {
        transition: none;
      }
    }
  `;

  render() {
    return html`
      <div class="stack">
        <div
          class="layer base"
          part="base"
          ?data-hidden=${this.selecting}
          ?inert=${this.selecting}
        >
          <slot></slot>
        </div>
        <div
          class="layer bulk"
          part="bulk"
          ?data-hidden=${!this.selecting}
          ?inert=${!this.selecting}
        >
          <slot name="bulk"></slot>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'list-bar-swap': ListBarSwap;
  }
}
