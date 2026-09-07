/**
 * Test helpers for the bulk bar.
 *
 * The bar renders its actions through `resource-actions`, the same renderer
 * the row kebab uses, so an action button lives one shadow root deeper than
 * the bar's own Clear and Select all. These two helpers keep that detail in
 * one place instead of in every collection page's test.
 */

interface Updatable {
  updateComplete: Promise<unknown>;
}

/** The bar's action button by id ("suspend", "delete"), once it is rendered. */
export async function bulkActionButton(
  bar: Element,
  id: string
): Promise<HTMLElement | null> {
  await (bar as unknown as Updatable).updateComplete;
  const actions = bar.shadowRoot?.querySelector('resource-actions');
  if (!actions) return null;
  await (actions as unknown as Updatable).updateComplete;
  const button = actions.shadowRoot?.querySelector<HTMLElement>(
    `sl-button[data-action="${id}"]`
  );
  if (!button) return null;
  await (button as unknown as Updatable).updateComplete;
  return button;
}

/** "3 selected", as the bar's live region reads it. */
export function bulkCountText(bar: Element): string {
  return (
    bar.shadowRoot
      ?.querySelector('[data-testid="bulk-count"]')
      ?.textContent?.trim() ?? ''
  );
}
