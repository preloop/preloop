import { html, type TemplateResult } from 'lit';

/**
 * Agent-supplied text (an ask_user row's title, description or extra
 * fields) is plain text that may contain URLs. Render it with every http(s)
 * URL as a link and everything else as text: Lit escapes string parts, so
 * nothing in the input is ever parsed as markup, and only the http and https
 * schemes become an href (a `javascript:` or `data:` string stays text).
 */

const URL_PATTERN = /https?:\/\/[^\s<>"'`]+/gi;
const TRAILING_PUNCTUATION = /[.,;:!?'")\]}]+$/;

/** The URL when it is a well-formed http(s) URL, otherwise null. */
export function safeHttpUrl(candidate: string): string | null {
  try {
    const url = new URL(candidate);
    return url.protocol === 'http:' || url.protocol === 'https:'
      ? url.href
      : null;
  } catch {
    return null;
  }
}

/** Split text into string parts and link templates, in order. */
export function linkify(text: string): Array<string | TemplateResult> {
  const parts: Array<string | TemplateResult> = [];
  let last = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const start = match.index ?? 0;
    let raw = match[0];
    // "see https://x.io/a." keeps the full stop out of the link; a closing
    // bracket stays in only when the URL opened one (Wikipedia-style paths).
    const trailing = raw.match(TRAILING_PUNCTUATION)?.[0] ?? '';
    if (trailing) {
      raw = raw.slice(0, raw.length - trailing.length);
      if (trailing.startsWith(')') && raw.includes('(')) {
        raw += ')';
      }
    }
    const href = safeHttpUrl(raw);
    if (!href) continue;
    if (start > last) parts.push(text.slice(last, start));
    parts.push(
      html`<a href=${href} target="_blank" rel="noopener noreferrer">${raw}</a>`
    );
    last = start + raw.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}
