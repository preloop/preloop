/**
 * Long-form article styles, shared by every markdown-driven page (privacy,
 * terms, /about, /vs/<slug>, /resources/<slug>) and by the blog.
 *
 * These live in the light DOM as an inline <style> tag because the article is
 * slotted into <static-view-wrapper> and ::slotted() cannot style
 * descendants. Keep in lockstep with the `.text-section` styles in
 * views/public/static-view.ts.
 *
 * This module has no Node imports so the build plugin (`vite-plugin-brand.ts`)
 * and the browser tests render an article with the same styles.
 */
export const ARTICLE_STYLES = `
      article.container {
        font-size: 1.0625rem;
        line-height: 1.75;
        color: rgba(230, 237, 243, 0.9);
        font-feature-settings: 'liga', 'kern';
        text-rendering: optimizeLegibility;
        -webkit-font-smoothing: antialiased;
        word-wrap: break-word;
        overflow-wrap: break-word;
      }
      article.container h1 {
        font-size: clamp(2rem, 1.6rem + 1.6vw, 2.75rem);
        font-weight: 600;
        line-height: 1.15;
        letter-spacing: -0.02em;
        color: #e6edf3;
        margin: 0 0 1.25rem;
      }
      article.container h2 {
        font-size: clamp(1.5rem, 1.25rem + 1vw, 1.875rem);
        font-weight: 600;
        line-height: 1.25;
        letter-spacing: -0.015em;
        color: #e6edf3;
        margin: 3rem 0 1rem;
        padding-bottom: 0.5rem;
        border-bottom: 1px solid rgba(230, 237, 243, 0.08);
      }
      article.container h3 {
        font-size: 1.375rem;
        font-weight: 600;
        line-height: 1.3;
        letter-spacing: -0.01em;
        color: #e6edf3;
        margin: 2.25rem 0 0.75rem;
      }
      article.container h4 {
        font-size: 1.125rem;
        font-weight: 600;
        line-height: 1.35;
        color: #e6edf3;
        margin: 1.75rem 0 0.5rem;
      }
      article.container p {
        margin: 0 0 1.25em;
      }
      article.container p:last-child {
        margin-bottom: 0;
      }
      article.container strong {
        color: #e6edf3;
        font-weight: 600;
      }
      article.container em {
        font-style: italic;
      }
      article.container a {
        color: #58a6ff;
        text-decoration: underline;
        text-underline-offset: 2px;
        text-decoration-thickness: 1px;
        transition: color 0.15s ease;
      }
      article.container a:hover {
        color: #79b8ff;
        text-decoration-thickness: 2px;
      }
      article.container ul,
      article.container ol {
        margin: 0 0 1.25em;
        padding-left: 1.6em;
      }
      article.container li {
        margin-bottom: 0.4em;
      }
      article.container li > p {
        margin-bottom: 0.4em;
      }
      article.container ul ul,
      article.container ol ol,
      article.container ul ol,
      article.container ol ul {
        margin: 0.4em 0;
      }
      article.container ul li::marker {
        color: rgba(230, 237, 243, 0.45);
      }
      article.container code {
        font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo,
          Consolas, monospace;
        font-size: 0.9em;
        background: rgba(110, 118, 129, 0.18);
        color: #e6edf3;
        padding: 0.15em 0.4em;
        border-radius: 4px;
      }
      article.container pre {
        margin: 1.5em 0;
        padding: 1.1rem 1.25rem;
        background: #0d1117;
        border: 1px solid rgba(230, 237, 243, 0.08);
        border-radius: 8px;
        overflow-x: auto;
        line-height: 1.55;
        font-size: 0.9rem;
      }
      article.container pre code {
        padding: 0;
        background: transparent;
        font-size: inherit;
        color: inherit;
      }
      article.container blockquote {
        margin: 1.5em 0;
        padding: 0.5em 1.25em;
        border-left: 3px solid #58a6ff;
        background: rgba(88, 166, 255, 0.06);
        color: rgba(230, 237, 243, 0.85);
        border-radius: 0 6px 6px 0;
      }
      article.container blockquote > :first-child {
        margin-top: 0;
      }
      article.container blockquote > :last-child {
        margin-bottom: 0;
      }
      article.container table {
        width: 100%;
        margin: 1.75em 0;
        border-collapse: collapse;
        font-size: 0.95rem;
        line-height: 1.55;
        background: rgba(13, 17, 23, 0.5);
        border: 1px solid rgba(230, 237, 243, 0.08);
        border-radius: 8px;
        overflow: hidden;
        display: block;
        overflow-x: auto;
      }
      article.container thead {
        background: rgba(110, 118, 129, 0.12);
      }
      article.container th,
      article.container td {
        padding: 0.7em 1em;
        text-align: left;
        vertical-align: top;
        border-bottom: 1px solid rgba(230, 237, 243, 0.06);
      }
      article.container th {
        font-weight: 600;
        color: #e6edf3;
        white-space: nowrap;
      }
      article.container tr:last-child td {
        border-bottom: none;
      }
      article.container tr:hover td {
        background: rgba(88, 166, 255, 0.04);
      }
      article.container hr {
        border: none;
        border-top: 1px solid rgba(230, 237, 243, 0.08);
        margin: 2.5rem 0;
      }
      article.container img {
        max-width: 100%;
        height: auto;
        border-radius: 8px;
        margin: 1.5em 0;
      }
      /* Team section styles (used by /about) */
      article.container .team-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1.5rem;
        margin: 2rem 0;
      }
      article.container .team-member {
        text-align: center;
        padding: 1.5rem;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(230, 237, 243, 0.08);
      }
      article.container .team-photo {
        width: 140px;
        height: 140px;
        border-radius: 50%;
        object-fit: cover;
        margin-bottom: 1rem;
        border: 3px solid #58a6ff;
      }
      article.container .team-member h4 {
        font-size: 1.125rem;
        font-weight: 600;
        margin: 0.5rem 0 0.25rem;
        color: #e6edf3;
      }
      article.container .team-member p {
        font-size: 0.95rem;
        line-height: 1.6;
        text-align: left;
      }
      @media (max-width: 640px) {
        article.container {
          font-size: 1rem;
          line-height: 1.7;
        }
        article.container h2 {
          margin-top: 2.25rem;
        }
        article.container table {
          font-size: 0.875rem;
        }
        article.container th,
        article.container td {
          padding: 0.55em 0.7em;
        }
      }
`;
