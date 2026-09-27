/**
 * Development-only diagnostics.
 *
 * Vite replaces `import.meta.env.DEV` with `false` in a production build
 * and drops this branch, so the console call is not in the shipped bundle.
 * Use this instead of `console.log` or `console.debug`. Keep `console.error`
 * for failures an operator can act on.
 */
export function debugLog(...args: unknown[]): void {
  if (import.meta.env.DEV) {
    console.log(...args);
  }
}
