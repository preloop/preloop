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
