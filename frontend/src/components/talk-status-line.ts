/**
 * The Talk window's one-line footer under the input, the web form of the
 * CLIs' status bar: model, working directory, control mode, connection and
 * pending approvals, in the console mono face.
 *
 * It only reads what the window already has (the agent summary, the open
 * session row, the realtime socket state, the session's approval list), so
 * it costs no request. A field the server has not reported is left out
 * rather than guessed. Light DOM, so the words are part of the page.
 */
import { LitElement, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import type { ManagedAgentSummary, RuntimeSessionSummary } from '../types';
import {
  getAgentControlSessionMode,
  getAgentControlState,
} from '../utils/agent-control';

const STYLE = `
talk-status-line { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 0 0.5rem; min-width: 0;
  padding: 0.25rem var(--sl-spacing-medium, 1rem)
    calc(0.25rem + env(safe-area-inset-bottom, 0px));
  font-family: var(--console-font-mono, 'JetBrains Mono', ui-monospace,
    SFMono-Regular, Menlo, Consolas, monospace);
  font-size: var(--console-text-eyebrow, 0.6875rem);
  color: var(--console-meta-color, var(--sl-color-neutral-600)); }
talk-status-line:empty { display: none; }
talk-status-line .talk-status-field { white-space: nowrap; }
talk-status-line .talk-status-field + .talk-status-field::before {
  content: '·'; margin-right: 0.5rem; color: var(--console-meta-color, var(--sl-color-neutral-600)); }
talk-status-line .talk-status-cwd { min-width: 0; overflow: hidden;
  text-overflow: ellipsis; max-width: 40ch; }
talk-status-line .talk-status-approvals { color: var(--sl-color-warning-700);
  font-weight: 600; }
`;

let sheet: CSSStyleSheet | null = null;

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

/** `/Users/me/src/app` reads `~/src/app`; long paths keep their tail. */
export function shortCwd(cwd: string): string {
  const home = cwd.match(/^\/(?:Users|home)\/[^/]+(\/.*)?$/);
  const path = home ? `~${home[1] ?? ''}` : cwd;
  const parts = path.split('/');
  return parts.length > 4 ? `…/${parts.slice(-3).join('/')}` : path;
}

const MODE_LABEL: Record<string, string> = {
  remote: 'remote control',
  local: 'local',
  queued: 'queued',
};

@customElement('talk-status-line')
export class TalkStatusLine extends LitElement {
  @property({ attribute: false }) agent: ManagedAgentSummary | null = null;
  @property({ attribute: false }) session: RuntimeSessionSummary | null = null;
  /** The realtime socket; null before the first state is known. */
  @property({ attribute: false }) connected: boolean | null = null;
  /** Approvals waiting on the open session. */
  @property({ type: Number }) approvals = 0;

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  connectedCallback(): void {
    super.connectedCallback();
    ensureStyle(this);
  }

  private connectionLabel(): string | null {
    if (this.connected === false) return 'reconnecting';
    if (!this.agent) return null;
    const control = getAgentControlState(this.agent);
    if (!control.enabled) return null;
    return control.online ? 'connected' : 'agent offline';
  }

  render() {
    const model = this.session?.latest_model_alias || null;
    const cwd = this.session?.cwd || null;
    const control = getAgentControlState(this.agent);
    const mode = control.enabled
      ? MODE_LABEL[getAgentControlSessionMode(this.agent)] || null
      : null;
    const connection = this.connectionLabel();
    const approvals = Math.max(0, this.approvals || 0);
    if (!model && !cwd && !mode && !connection && !approvals) return nothing;
    return html`${
      model
        ? html`<span class="talk-status-field" data-testid="talk-status-model"
            >${model}</span
          >`
        : nothing
    }${
      cwd
        ? html`<span
            class="talk-status-field talk-status-cwd"
            data-testid="talk-status-cwd"
            title=${cwd}
            >${shortCwd(cwd)}</span
          >`
        : nothing
    }${
      mode
        ? html`<span class="talk-status-field" data-testid="talk-status-mode"
            >${mode}</span
          >`
        : nothing
    }${
      connection
        ? html`<span
            class="talk-status-field"
            data-testid="talk-status-connection"
            >${connection}</span
          >`
        : nothing
    }${
      approvals
        ? html`<span
            class="talk-status-field talk-status-approvals"
            data-testid="talk-status-approvals"
            >${approvals === 1 ? '1 approval waiting' : `${approvals} approvals waiting`}</span
          >`
        : nothing
    }`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talk-status-line': TalkStatusLine;
  }
}
