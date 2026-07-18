import { css } from 'lit';

/**
 * Global prefers-reduced-motion guard (DESIGN.md D19).
 *
 * Lit components render into shadow roots, so a document-level stylesheet
 * cannot reach their keyframe animations. Every component that ships an
 * animation appends this block to its static styles; the same rules also live
 * in `main.css` / `landing.css` for light-DOM surfaces. New animation ships
 * with this guard (or an explicit `prefers-reduced-motion` fallback) in the
 * same commit.
 */
export const reducedMotionStyles = css`
  @media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
      scroll-behavior: auto !important;
    }
  }
`;

/**
 * Runtime check for the user's reduced-motion preference, for imperative
 * animation decisions (e.g. skipping an entry fade).
 */
export function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}
