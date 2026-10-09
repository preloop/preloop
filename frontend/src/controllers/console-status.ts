import type { LitElement, ReactiveController } from 'lit';

/** A persistent, hidden status region for asynchronous authenticated views. */
export class ConsoleStatus implements ReactiveController {
  private region: HTMLSpanElement | null = null;
  private lastState = '';
  private pendingMessage: string | null = null;

  constructor(private readonly host: LitElement) {
    host.addController(this);
  }

  announce(message: string): void {
    this.pendingMessage = message;
    this.host.requestUpdate();
  }

  hostUpdated(): void {
    const root = this.host.renderRoot;
    const visibleContent = [...root.children].some(
      (child) => child.tagName !== 'STYLE' && child !== this.region
    );
    if (!visibleContent) {
      this.region?.remove();
      this.region = null;
      return;
    }

    if (!this.region?.isConnected) {
      this.region = document.createElement('span');
      this.region.dataset.consoleStatus = '';
      this.region.setAttribute('role', 'status');
      this.region.setAttribute('aria-live', 'polite');
      this.region.setAttribute('aria-atomic', 'true');
      this.region.style.cssText =
        'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0';
      root.appendChild(this.region);
    }
    const view = this.host as unknown as Record<string, unknown>;
    const declaredKeys = [
      ...(this.host.constructor as typeof LitElement).elementProperties.keys(),
    ].filter((key): key is string => typeof key === 'string');
    const keys = [...new Set([...Object.keys(view), ...declaredKeys])];
    const hasValue = (value: unknown): boolean =>
      value instanceof Set || value instanceof Map
        ? value.size > 0
        : Array.isArray(value)
          ? value.length > 0
          : value && typeof value === 'object' && !(value instanceof Error)
            ? Object.values(value).some(Boolean)
            : typeof value !== 'function' && !!value;
    // Lit state may live behind prototype accessors, so include the declared
    // reactive keys as well as ordinary fields. Accept the repository's
    // prefix/suffix conventions without treating preserveLoadingState as busy.
    const loading = keys.some(
      (key) =>
        /(?:^_?(?:is)?[Ll]oading(?:[A-Z]|$)|[Ll]oading$|^_?(?:is)?[Ss]aving(?:[A-Z]|$)|[Ss]aving$|^_?[Bb]usy(?:[A-Z]|$)|[Bb]usy$)/.test(
          key
        ) && hasValue(view[key])
    );
    const error = keys.some(
      (key) =>
        /(?:^_?(?:error|loadError|dialogError)$|(?:Error|Errors|ErrorMessage)$|^errorMessage$)/.test(
          key
        ) && hasValue(view[key])
    );
    const state = error ? 'error' : loading ? 'loading' : 'ready';
    const pending = this.pendingMessage;
    this.pendingMessage = null;
    if (pending) {
      this.region.textContent = pending;
    } else if (state !== this.lastState) {
      const title =
        (root.querySelector('view-header') as { headerText?: string } | null)
          ?.headerText || 'Page';
      this.region.textContent =
        state === 'loading'
          ? 'Loading updates.'
          : state === 'error'
            ? 'Could not complete the update. See the error on this page.'
            : `${title} ready.`;
    }
    this.lastState = state;
    this.host.dispatchEvent(
      new CustomEvent('console-view-updated', { bubbles: true, composed: true })
    );
  }

  hostDisconnected(): void {
    this.region?.remove();
    this.region = null;
    this.lastState = '';
  }
}
