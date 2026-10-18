/**
 * Full screen for a console component: a CSS-fixed cover (the host's own
 * `:host([fullscreen])` rule), not the Fullscreen API. While attached the
 * page behind it cannot scroll and Esc asks the host to leave. One rule for
 * every component that covers the viewport (Fleet Map, Activity board).
 */
export interface FullscreenOptions {
  /** Called on Esc. The host decides what Esc means (e.g. clear first). */
  onEscape: (event: KeyboardEvent) => void;
}

/**
 * Lock body scroll and listen for Esc until the returned disposer runs.
 *
 * @param _host - The element covering the viewport (kept for symmetry and debugging).
 * @param options - Esc handler.
 * @returns A disposer that restores body overflow and removes the listener.
 *   Calling it twice is harmless.
 */
export function attachFullscreen(
  _host: HTMLElement,
  options: FullscreenOptions
): () => void {
  const onKey = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    options.onEscape(event);
  };
  document.body.style.overflow = 'hidden';
  window.addEventListener('keydown', onKey);
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    window.removeEventListener('keydown', onKey);
    document.body.style.overflow = '';
  };
}
