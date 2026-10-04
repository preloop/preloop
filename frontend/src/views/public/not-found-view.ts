import { LitElement, css, html } from 'lit';
import { customElement } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

import { pageTitle } from '../../utils/page-title';

/** Read storage without throwing where it is blocked (private windows). */
function safeGetItem(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * `/console` itself or a path under `/console/`; `/consoles` is public. The
 * same boundary lit-app uses to decide when to restore the served title.
 */
function isConsolePath(pathname: string): boolean {
  return pathname === '/console' || pathname.startsWith('/console/');
}

/**
 * Where the one button on the 404 should go. Inside the console it renders
 * within the shell, so the way back is the Overview. A signed-in visitor on a
 * public path is offered the console; an anonymous one the home page, rather
 * than a sign-in screen they did not ask for.
 */
export function notFoundAction(
  pathname: string,
  signedIn: boolean
): { href: string; label: string } {
  if (isConsolePath(pathname)) {
    return { href: '/console', label: 'Back to Overview' };
  }
  if (signedIn) return { href: '/console', label: 'Go to the console' };
  return { href: '/', label: 'Go to the home page' };
}

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

  connectedCallback(): void {
    super.connectedCallback();
    // Inside the console every page titles the tab through its view-header,
    // which this page does not render; without this the tab kept the name
    // of the page the reader came from. A public 404 keeps the served title,
    // which lit-app restores on public paths.
    if (isConsolePath(window.location.pathname)) {
      document.title = pageTitle('Page not found');
    }
  }

  render() {
    const cta = notFoundAction(
      window.location.pathname,
      Boolean(safeGetItem('accessToken'))
    );
    return html`
      <div class="wrapper">
        <sl-icon name="compass"></sl-icon>
        <h1>Page not found</h1>
        <p>
          The page you asked for does not exist. It may have moved, or the link
          may be out of date.
        </p>
        <sl-button variant="primary" href=${cta.href}> ${cta.label} </sl-button>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'not-found-view': NotFoundView;
  }
}
