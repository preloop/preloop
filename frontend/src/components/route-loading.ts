import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { LoadingRenderer, LoadingSlot } from '../router';

/**
 * How long a route's chunk may take before the wait is worth mentioning.
 *
 * A warm cache resolves in single-digit milliseconds, so drawing anything
 * immediately would be a flicker on almost every navigation, and a flicker
 * reads as a fault. Past this point the page looks stuck instead, so it says
 * something. 150ms is the bottom of DESIGN.md's "short" band.
 */
export const PENDING_DELAY_MS = 150;

/**
 * The quiet line shown while a route's chunk is in flight.
 *
 * Text, not a spinner: the console's motion budget is two ambient animations
 * and neither of them is this. The only movement is a 150ms opacity fade,
 * which reduced motion drops entirely (D19).
 */
@customElement('route-loading')
export class RouteLoading extends LitElement {
  /** Set when the outlet has no shell around it paying the page padding. */
  @property({ type: Boolean, reflect: true }) standalone = false;

  static styles = css`
    :host {
      display: block;
      padding: 1.5rem 0;
      opacity: 0;
      animation: appear 150ms ease-out forwards;
    }

    :host([standalone]) {
      padding: 1.5rem 2rem;
    }

    p {
      margin: 0;
      font-size: 0.8125rem;
      color: var(--sl-color-neutral-500);
    }

    @keyframes appear {
      to {
        opacity: 1;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      :host {
        animation: none;
        opacity: 1;
      }
    }
  `;

  render() {
    return html`<p role="status" aria-live="polite">Loading…</p>`;
  }
}

/**
 * What a route that never arrived looks like.
 *
 * The usual cause is a deploy that replaced the hashed chunk this tab was
 * still pointing at. The one recovery offered is a reload, because a reload is
 * the only thing that works: once an ES module URL fails, the browser records
 * the failure in the module map and a second import() of the same specifier is
 * rejected from memory without touching the network. A "try again" button next
 * to it would replay the same error every time it was pressed, so this panel
 * does not offer one. A blank outlet would explain none of it.
 */
@customElement('route-load-error')
export class RouteLoadError extends LitElement {
  @property({ type: Boolean, reflect: true }) standalone = false;

  /**
   * How the page is fetched again. A seam for tests, which cannot let a real
   * reload tear the test runner's page down.
   */
  @property({ attribute: false }) recover: () => void = () =>
    window.location.reload();

  static styles = css`
    :host {
      display: block;
      padding: 2rem 0;
    }

    :host([standalone]) {
      padding: 2rem;
    }

    .panel {
      max-width: 34rem;
      padding: 1.25rem 1.5rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-large);
      background: var(--sl-color-neutral-0);
    }

    h2 {
      margin: 0 0 0.5rem;
      font-size: 0.9375rem;
      font-weight: 600;
      color: var(--sl-color-neutral-900);
    }

    p {
      margin: 0 0 1rem;
      font-size: 0.8125rem;
      line-height: 1.5;
      color: var(--sl-color-neutral-600);
    }

    .actions {
      display: flex;
      gap: 0.5rem;
    }

    button {
      font: inherit;
      font-size: 0.8125rem;
      padding: 0.375rem 0.875rem;
      border-radius: var(--sl-border-radius-medium);
      border: 1px solid var(--sl-color-neutral-300);
      background: var(--sl-color-neutral-0);
      color: var(--sl-color-neutral-800);
      cursor: pointer;
      transition: background-color 100ms ease-out;
    }

    button.primary {
      border-color: var(--sl-color-primary-600);
      background: var(--sl-color-primary-600);
      color: var(--sl-color-neutral-0);
    }

    button:hover {
      background: var(--sl-color-neutral-100);
    }

    button.primary:hover {
      background: var(--sl-color-primary-500);
    }

    @media (prefers-reduced-motion: reduce) {
      button {
        transition: none;
      }
    }
  `;

  render() {
    return html`
      <div class="panel" role="alert">
        <h2>This page did not load</h2>
        <p>
          Its code could not be fetched. That usually means the connection
          dropped, or a new version of the console was deployed while this tab
          was open. Reloading picks up the current version.
        </p>
        <div class="actions">
          <button class="primary" @click=${() => this.recover()}>
            Reload the page
          </button>
        </div>
      </div>
    `;
  }
}

/**
 * The console's answer to a route module that is slow or missing. Handed to
 * the router by `lit-app`; the router itself stays free of design decisions.
 */
export const routeLoadingRenderer: LoadingRenderer = {
  pending({ parent, atOutlet, isCurrent }: LoadingSlot) {
    let element: RouteLoading | undefined;
    const timer = window.setTimeout(() => {
      if (isCurrent && !isCurrent()) return;
      element = document.createElement('route-loading') as RouteLoading;
      element.standalone = atOutlet;
      parent.replaceChildren(element);
    }, PENDING_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      element?.remove();
    };
  },

  failed({ parent, atOutlet }: LoadingSlot) {
    const element = document.createElement(
      'route-load-error'
    ) as RouteLoadError;
    element.standalone = atOutlet;
    parent.replaceChildren(element);
  },
};

declare global {
  interface HTMLElementTagNameMap {
    'route-loading': RouteLoading;
    'route-load-error': RouteLoadError;
  }
}
