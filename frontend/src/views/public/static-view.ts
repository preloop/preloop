import { LitElement, html, css, PropertyValues, unsafeCSS } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { customElement, property, state } from 'lit/decorators.js';
import { marked } from 'marked';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import landingStyles from '../../styles/landing.css?inline';

@customElement('static-view')
export class StaticView extends LitElement {
  @property({ type: String }) src = '';
  @state() private content: string | null = null;
  @state() private error: string | null = null;

  static styles = [
    unsafeCSS(landingStyles),
    css`
      :host {
        display: flex;
        flex-direction: column;
        min-height: 100vh;
      }
      /* The padding is part of the 100% width: as a content-box the column
         measured viewport + 2 * padding and clipped the right edge of every
         line on a phone. The max-width includes the same padding so the
         reading column stays 760px on a desktop. */
      main {
        flex: 1;
        box-sizing: border-box;
        --reading-column-width: 760px;
        --page-gutter: 1.5rem;
        padding: 3.5rem var(--page-gutter) 5rem;
        max-width: calc(var(--reading-column-width) + 2 * var(--page-gutter));
        margin: 0 auto;
        width: 100%;
      }

      @media (max-width: 640px) {
        main {
          padding: 2rem 1.25rem 3.5rem;
        }
      }

      /* ----------------------------------------------------------------
       * Long-form typography for markdown- and HTML-driven static pages
       * (privacy, terms, about, /vs/<slug>, /resources/<slug>, etc.)
       * ----------------------------------------------------------------
       */
      .text-section {
        font-size: 1.0625rem;
        line-height: 1.75;
        color: rgba(230, 237, 243, 0.9);
        font-feature-settings: 'liga', 'kern';
        text-rendering: optimizeLegibility;
        -webkit-font-smoothing: antialiased;
        word-wrap: break-word;
        overflow-wrap: break-word;
      }

      /* Headings ----------------------------------------------------- */
      .text-section h1 {
        font-size: clamp(2rem, 1.6rem + 1.6vw, 2.75rem);
        font-weight: 600;
        line-height: 1.15;
        letter-spacing: -0.02em;
        color: #e6edf3;
        margin: 0 0 1.25rem;
      }
      .text-section h2 {
        font-size: clamp(1.5rem, 1.25rem + 1vw, 1.875rem);
        font-weight: 600;
        line-height: 1.25;
        letter-spacing: -0.015em;
        color: #e6edf3;
        margin: 3rem 0 1rem;
        padding-bottom: 0.5rem;
        border-bottom: 1px solid rgba(230, 237, 243, 0.08);
      }
      .text-section h3 {
        font-size: 1.375rem;
        font-weight: 600;
        line-height: 1.3;
        letter-spacing: -0.01em;
        color: #e6edf3;
        margin: 2.25rem 0 0.75rem;
      }
      .text-section h4 {
        font-size: 1.125rem;
        font-weight: 600;
        line-height: 1.35;
        color: #e6edf3;
        margin: 1.75rem 0 0.5rem;
      }

      /* Body text ---------------------------------------------------- */
      .text-section p {
        margin: 0 0 1.25em;
      }
      .text-section p:last-child {
        margin-bottom: 0;
      }
      .text-section strong {
        color: #e6edf3;
        font-weight: 600;
      }
      .text-section em {
        font-style: italic;
      }

      /* Links -------------------------------------------------------- */
      .text-section a {
        color: #58a6ff;
        text-decoration: underline;
        text-underline-offset: 2px;
        text-decoration-thickness: 1px;
        transition: color 0.15s ease;
      }
      .text-section a:hover {
        color: #79b8ff;
        text-decoration-thickness: 2px;
      }

      /* Lists -------------------------------------------------------- */
      .text-section ul,
      .text-section ol {
        margin: 0 0 1.25em;
        padding-left: 1.6em;
      }
      .text-section li {
        margin-bottom: 0.4em;
      }
      .text-section li > p {
        margin-bottom: 0.4em;
      }
      .text-section ul ul,
      .text-section ol ol,
      .text-section ul ol,
      .text-section ol ul {
        margin: 0.4em 0;
      }
      .text-section ul li::marker {
        color: rgba(230, 237, 243, 0.45);
      }

      /* Inline code & code blocks ----------------------------------- */
      .text-section code {
        font-family:
          'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas,
          monospace;
        font-size: 0.9em;
        background: rgba(110, 118, 129, 0.18);
        color: #e6edf3;
        padding: 0.15em 0.4em;
        border-radius: 4px;
      }
      .text-section pre {
        margin: 1.5em 0;
        padding: 1.1rem 1.25rem;
        background: #0d1117;
        border: 1px solid rgba(230, 237, 243, 0.08);
        border-radius: 8px;
        overflow-x: auto;
        line-height: 1.55;
        font-size: 0.9rem;
      }
      .text-section pre code {
        padding: 0;
        background: transparent;
        font-size: inherit;
        color: inherit;
      }

      /* Blockquotes -------------------------------------------------- */
      .text-section blockquote {
        margin: 1.5em 0;
        padding: 0.5em 1.25em;
        border-left: 3px solid #58a6ff;
        background: rgba(88, 166, 255, 0.06);
        color: rgba(230, 237, 243, 0.85);
        border-radius: 0 6px 6px 0;
      }
      .text-section blockquote > :first-child {
        margin-top: 0;
      }
      .text-section blockquote > :last-child {
        margin-bottom: 0;
      }

      /* Tables ------------------------------------------------------- */
      .text-section table {
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
      .text-section thead {
        background: rgba(110, 118, 129, 0.12);
      }
      .text-section th,
      .text-section td {
        padding: 0.7em 1em;
        text-align: left;
        vertical-align: top;
        border-bottom: 1px solid rgba(230, 237, 243, 0.06);
      }
      .text-section th {
        font-weight: 600;
        color: #e6edf3;
        white-space: nowrap;
      }
      .text-section tr:last-child td {
        border-bottom: none;
      }
      .text-section tr:hover td {
        background: rgba(88, 166, 255, 0.04);
      }

      /* Misc --------------------------------------------------------- */
      .text-section hr {
        border: none;
        border-top: 1px solid rgba(230, 237, 243, 0.08);
        margin: 2.5rem 0;
      }
      .text-section img {
        max-width: 100%;
        height: auto;
        border-radius: 8px;
        margin: 1.5em 0;
      }

      /* Team section (used by /about) ------------------------------- */
      .text-section .team-grid,
      .team-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1.5rem;
        margin: 2rem 0;
      }
      .text-section .team-member,
      .team-member {
        text-align: center;
        padding: 1.5rem;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(230, 237, 243, 0.08);
      }
      .text-section .team-photo,
      .team-photo {
        width: 140px;
        height: 140px;
        border-radius: 50%;
        object-fit: cover;
        margin-bottom: 1rem;
        border: 3px solid #58a6ff;
      }
      .text-section .team-member p,
      .team-member p {
        font-size: 0.95rem;
        line-height: 1.6;
        text-align: left;
      }

      @media (max-width: 640px) {
        .text-section {
          font-size: 1rem;
          line-height: 1.7;
        }
        .text-section h2 {
          margin-top: 2.25rem;
        }
        .text-section table {
          font-size: 0.875rem;
        }
        .text-section th,
        .text-section td {
          padding: 0.55em 0.7em;
        }
      }
    `,
  ];

  updated(changedProperties: PropertyValues) {
    if (changedProperties.has('src') && this.src) {
      this.fetchContent();
    }
  }

  async fetchContent() {
    this.content = null;
    this.error = null;
    try {
      const response = await fetch(this.src);
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const text = await response.text();

      // Check if the file is HTML or markdown based on extension
      if (this.src.endsWith('.html')) {
        // HTML file - use as-is
        this.content = text;
      } else {
        // Markdown file - parse with marked
        this.content = marked(text) as string;
      }
    } catch (error: any) {
      console.error('Error fetching static content:', error);
      this.error = 'There was an issue fetching the content.';
    }
  }

  render() {
    return html`
      <app-header></app-header>
      <main>
        <div class="text-section">
          <!-- Slotted skeleton content for SEO - visible without JavaScript -->
          <slot name="static-content"> ${this._renderDynamicContent()} </slot>
        </div>
      </main>
      <app-footer></app-footer>
    `;
  }

  private _renderDynamicContent() {
    if (this.error) {
      return html`
        <sl-alert variant="danger" open>
          <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
          <strong>Error loading content.</strong><br />
          ${this.error}
        </sl-alert>
      `;
    } else if (this.content === null) {
      return html`<sl-spinner></sl-spinner>`;
    } else {
      return html`${unsafeHTML(this.content)}`;
    }
  }
}
