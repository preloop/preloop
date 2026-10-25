import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { AgentStatus } from '../utils/agent-status';

/** Static glyph per runtime vocabulary, also the reduced-motion glyph. */
const STATIC_GLYPH = { codex: '•', claude: '✻' } as const;

/*
 * The glyph is the console's one status motion (DESIGN "Motion"): a CSS
 * `content` animation on a single span, so nothing else on the line moves.
 * Codex blinks `•`/`◦` every 600 ms; Claude Code style plays the dingbats
 * forward then back.
 */
const STYLE = `
agent-status-line { display: inline-flex; align-items: baseline; gap: 0.4rem;
  font-family: var(--console-font-mono, 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: var(--console-text-meta, 0.8125rem); font-variant-numeric: tabular-nums;
  color: var(--sl-color-neutral-700); min-width: 0; }
agent-status-line[tone='warning'] { color: var(--sl-color-warning-700); font-weight: 600; }
agent-status-line .agent-status-glyph { display: inline-block; width: 1ch; text-align: center; }
agent-status-line .agent-status-glyph.is-animated[data-runtime='codex']::before {
  content: '•'; animation: agent-status-codex 1.2s steps(1, end) infinite; }
agent-status-line .agent-status-glyph.is-animated[data-runtime='claude']::before {
  content: '·'; animation: agent-status-claude 1.2s steps(1, end) infinite; }
@keyframes agent-status-codex { 0% { content: '•'; } 50% { content: '◦'; } }
@keyframes agent-status-claude {
  0% { content: '·'; } 10% { content: '✢'; } 20% { content: '✳'; }
  30% { content: '✶'; } 40% { content: '✻'; } 50% { content: '✽'; }
  60% { content: '✻'; } 70% { content: '✶'; } 80% { content: '✳'; }
  90% { content: '✢'; } }
@media (prefers-reduced-motion: reduce) {
  agent-status-line .agent-status-glyph.is-animated::before { animation: none; }
}
agent-status-line .agent-status-text { overflow-wrap: anywhere; }
`;

let sheet: CSSStyleSheet | null = null;

/**
 * One shared constructed sheet, adopted once by each document or shadow root
 * the line is placed in, so no style text lands in the page content.
 */
function ensureStyle(element: HTMLElement): void {
  const root = element.getRootNode() as Document | ShadowRoot;
  const target: Document | ShadowRoot =
    root instanceof ShadowRoot ? root : document;
  if (!('adoptedStyleSheets' in target)) return;
  if (!sheet) {
    sheet = new CSSStyleSheet();
    sheet.replaceSync(STYLE);
  }
  if (!target.adoptedStyleSheets.includes(sheet))
    target.adoptedStyleSheets = [...target.adoptedStyleSheets, sheet];
}

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

/**
 * The live status line of a turn, in the runtime's own format:
 * `• Working (2m 38s)` for Codex, `✻ Mulling… (1m 15s · ↓ 4.1k tokens ·
 * thinking)` for Claude Code. Light DOM, so the words are part of the page
 * (search, copy, screen readers) wherever it is placed.
 */
@customElement('agent-status-line')
export class AgentStatusLine extends LitElement {
  @property({ attribute: false }) status: AgentStatus | null = null;
  @property({ type: String, reflect: true }) tone: 'neutral' | 'warning' =
    'neutral';
  @state() private reducedMotion = prefersReducedMotion();
  private motionQuery: MediaQueryList | null = null;

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  connectedCallback(): void {
    super.connectedCallback();
    ensureStyle(this);
    try {
      this.motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
      this.motionQuery.addEventListener?.('change', this.onMotionChange);
    } catch {
      this.motionQuery = null;
    }
  }

  disconnectedCallback(): void {
    this.motionQuery?.removeEventListener?.('change', this.onMotionChange);
    super.disconnectedCallback();
  }

  private onMotionChange = (): void => {
    this.reducedMotion = prefersReducedMotion();
  };

  protected willUpdate(): void {
    this.tone = this.status?.tone ?? 'neutral';
  }

  render() {
    const status = this.status;
    if (!status?.line) return nothing;
    const glyph = status.glyph;
    return html`${
        glyph
          ? this.reducedMotion
            ? html`<span
                class="agent-status-glyph"
                data-runtime=${glyph}
                aria-hidden="true"
                >${STATIC_GLYPH[glyph]}</span
              >`
            : html`<span
                class="agent-status-glyph is-animated"
                data-runtime=${glyph}
                data-animated
                aria-hidden="true"
              ></span>`
          : nothing
      }<span class="agent-status-text" data-testid="agent-status-text"
        >${status.line}</span
      >`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'agent-status-line': AgentStatusLine;
  }
}
