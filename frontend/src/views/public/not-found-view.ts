import { LitElement, css, html } from 'lit';
import { customElement } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

/**
 * Catch-all page. Without it an unknown path (`/agents` instead of
 * `/console/agents`, a stale bookmark, a typo) rendered a blank document.
 */
@customElement('not-found-view')
export class NotFoundView extends LitElement {
  static styles = css`
    :host {
      display: block;
    }

    .wrapper {
      align-items: center;
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
      margin: 0 auto;
      max-width: 480px;
      padding: var(--sl-spacing-3x-large) var(--sl-spacing-large);
      text-align: center;
    }

    sl-icon {
      color: var(--sl-color-neutral-400);
      font-size: 3rem;
    }

    h1 {
      font-size: var(--sl-font-size-2x-large);
      margin: 0;
    }

    p {
      color: var(--sl-color-neutral-600);
      margin: 0;
    }
  `;

  render() {
    return html`
      <div class="wrapper">
        <sl-icon name="compass"></sl-icon>
        <h1>Page not found</h1>
        <p>
          The page you asked for does not exist. It may have moved, or the link
          may be out of date.
        </p>
        <sl-button variant="primary" href="/console">
          Go to the console
        </sl-button>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'not-found-view': NotFoundView;
  }
}
