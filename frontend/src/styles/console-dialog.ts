import { css } from 'lit';

/**
 * Dialogs centre over the content area, not over the whole window.
 *
 * A modal opened from a button in the middle of a page looks off by half a
 * sidebar when it centres on the viewport: the eye reads the page column, not
 * the window. `console-shell` publishes the sidebar's rendered width as
 * `--console-main-offset` (0px on mobile, where the sidebar overlays the page
 * instead of taking space from it), and this rule moves the dialog's
 * centring box right by that much.
 *
 * Shoelace's `base` part is `position: fixed; inset: 0` and does the
 * centring; the `overlay` part inside it is itself fixed with its own
 * `left: 0`, so the dim still covers the sidebar. `::part()` only matches
 * from the scope holding the `<sl-dialog>`, so every view and component that
 * renders one includes this snippet in its `static styles`.
 *
 * `sl-drawer` is deliberately untouched: it is anchored to a window edge.
 *
 * The same snippet puts the dialog panel on the raised rung of the surface
 * ladder (styles/console-surfaces.css): a modal floats above the page and the
 * cards, so it is the one thing that may be lighter than a card in dark.
 */
export const consoleDialogStyles = css`
  sl-dialog::part(base) {
    left: var(--console-main-offset, 0px);
  }

  sl-dialog::part(panel) {
    background-color: var(--console-surface-raised);
    border-radius: var(--console-card-radius);
    box-shadow: var(--console-raised-shadow);
  }
`;
