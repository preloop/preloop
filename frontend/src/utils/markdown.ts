import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Markdown as the trackers write it.
 *
 * Issue bodies arrive as GitHub, GitLab or Jira Markdown: checklists, fenced
 * code, links. Printing the source verbatim asks a reader to parse `###` and
 * `- [ ]` in their head, so the console renders it and sanitises the result
 * before it reaches the DOM.
 */
export function renderMarkdown(source: string | null | undefined): string {
  const text = (source || '').trim();
  if (!text) return '';
  const parsed = marked.parse(text, { async: false, gfm: true, breaks: true });
  return DOMPurify.sanitize(typeof parsed === 'string' ? parsed : '');
}

const UNTRUSTED_TAGS = [
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'p',
  'br',
  'hr',
  'blockquote',
  'ul',
  'ol',
  'li',
  'pre',
  'code',
  'strong',
  'em',
  'del',
  'a',
  'table',
  'thead',
  'tbody',
  'tr',
  'th',
  'td',
];

/**
 * Markdown written by a third party, for example an extension author.
 *
 * Stricter than {@link renderMarkdown}. The text is attacker-controlled and
 * is read by someone with approval rights, so the output is reduced to an
 * allowlist of structural tags:
 *
 * - no images, media, frames, forms, styles or SVG, so opening a document
 *   cannot fetch a remote URL or restyle the console;
 * - no attributes except `href` on links, and only http, https and mailto
 *   targets, so there are no event handlers, ids, classes or script URLs;
 * - links open in a new tab without an opener or referrer.
 *
 * DOMPurify removes anything outside the allowlist, raw HTML in the
 * Markdown included.
 */
export function renderUntrustedMarkdown(
  source: string | null | undefined
): string {
  return renderUntrustedMarkdownChecked(source).html;
}

/** Attributes `marked` itself writes and the allowlist drops: no text lost. */
const BENIGN_DROPPED_ATTRIBUTES = new Set(['class', 'align', 'start']);

/**
 * Render third-party Markdown and count what the rendering left out.
 *
 * `omitted` counts every node and attribute the sanitiser removed that could
 * carry text a reader does not see: HTML comments, raw HTML elements, images
 * with their alt text, link titles. An agent that loads the file reads all of
 * it, so a viewer must not present the rendered form as the whole file when
 * this is above zero.
 */
export function renderUntrustedMarkdownChecked(
  source: string | null | undefined
): { html: string; omitted: number } {
  const text = source || '';
  if (!text.trim()) return { html: '', omitted: 0 };
  const parsed = marked.parse(text, { async: false, gfm: true, breaks: false });
  const fragment = DOMPurify.sanitize(
    typeof parsed === 'string' ? parsed : '',
    {
      ALLOWED_TAGS: UNTRUSTED_TAGS,
      ALLOWED_ATTR: ['href'],
      ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
      ALLOW_DATA_ATTR: false,
      ALLOW_ARIA_ATTR: false,
      RETURN_DOM_FRAGMENT: true,
    }
  );
  let omitted = 0;
  for (const entry of DOMPurify.removed as Array<{
    element?: Node;
    attribute?: Attr | null;
  }>) {
    if (entry.attribute) {
      if (!BENIGN_DROPPED_ATTRIBUTES.has(entry.attribute.name.toLowerCase()))
        omitted++;
    } else if (entry.element) {
      const element = entry.element as Element;
      // A task-list checkbox is the one element `marked` writes that the
      // allowlist drops; it has no text of its own.
      const checkbox =
        element.nodeName === 'INPUT' &&
        element.getAttribute?.('type') === 'checkbox' &&
        !element.getAttribute?.('value');
      // The sanitiser reports its own document wrapper as removed.
      const wrapper =
        ['BODY', 'HTML', 'HEAD'].includes(element.nodeName) &&
        !(element.attributes?.length > 0);
      if (!checkbox && !wrapper) omitted++;
    }
  }
  const holder = document.createElement('div');
  holder.appendChild(fragment);
  for (const link of Array.from(holder.querySelectorAll('a'))) {
    if (!link.getAttribute('href')) {
      // The target was not http, https or mailto: the reader would not see it.
      omitted++;
      link.replaceWith(...Array.from(link.childNodes));
      continue;
    }
    link.setAttribute('target', '_blank');
    link.setAttribute('rel', 'noopener noreferrer nofollow');
  }
  return { html: holder.innerHTML, omitted };
}

/**
 * Prose styling for a rendered body.
 *
 * No card, no inner scroller: the body is the page's content, and a box with
 * its own scrollbar inside a page that already scrolls is the second depth
 * level the console does not have (DESIGN.md, "Depth limit: two").
 */
export const markdownBodyCss = `
  .markdown-body {
    color: var(--sl-color-neutral-800);
    font-size: var(--console-text-body);
    line-height: 1.6;
    overflow-wrap: break-word;
  }
  .markdown-body > :first-child {
    margin-top: 0;
  }
  .markdown-body > :last-child {
    margin-bottom: 0;
  }
  .markdown-body h1,
  .markdown-body h2,
  .markdown-body h3,
  .markdown-body h4 {
    font-size: var(--console-text-body);
    font-weight: var(--sl-font-weight-semibold);
    margin: var(--sl-spacing-medium) 0 var(--sl-spacing-2x-small);
  }
  .markdown-body p,
  .markdown-body ul,
  .markdown-body ol,
  .markdown-body pre {
    margin: 0 0 var(--sl-spacing-small);
  }
  .markdown-body ul,
  .markdown-body ol {
    padding-left: var(--sl-spacing-large);
  }
  .markdown-body code {
    background: var(--sl-color-neutral-100);
    border-radius: var(--sl-border-radius-small);
    font-family: var(--sl-font-mono);
    font-size: var(--console-text-meta);
    padding: 1px 4px;
  }
  .markdown-body pre {
    background: var(--sl-color-neutral-50);
    border-radius: var(--sl-border-radius-medium);
    overflow-x: auto;
    padding: var(--sl-spacing-small);
  }
  .markdown-body pre code {
    background: none;
    padding: 0;
  }
  .markdown-body a {
    color: var(--console-link-color);
  }
  .markdown-body img {
    max-width: 100%;
  }
  .markdown-body table {
    border-collapse: collapse;
  }
  .markdown-body th,
  .markdown-body td {
    border-bottom: 1px solid var(--console-hairline);
    padding: 4px 8px;
    text-align: left;
  }
`;
