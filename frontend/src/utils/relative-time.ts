import { html, type TemplateResult } from 'lit';
import { formatAbsoluteTime, formatRelativeOrFuture, parseUtc } from './date';

/**
 * Renders a timestamp the way the console shows every time: relative
 * ("6w ago", "in 3d") with the absolute value in `title`.
 *
 * @param value - Datetime string from backend
 * @param fallback - Text when the value is missing or unparseable
 * @param now - Instant to measure against
 * @returns A span with the relative text and the absolute title
 */
export function relativeTime(
  value: string | null | undefined,
  fallback = '',
  now: Date = new Date()
): TemplateResult {
  if (parseUtc(value) === null) return html`${fallback}`;
  return html`<span title=${formatAbsoluteTime(value)}
    >${formatRelativeOrFuture(value as string, now)}</span
  >`;
}
