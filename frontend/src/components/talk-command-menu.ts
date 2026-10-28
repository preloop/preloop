/**
 * The Talk window's `/` command menu, the web form of the slash commands in
 * Claude Code and Codex CLI. The composer owns the keyboard (it keeps focus
 * in the input, as the CLIs do); this element only lists the commands that
 * match what was typed and which one Enter would run. Light DOM, so the
 * command names and descriptions are page text.
 */
import { LitElement, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

export type TalkCommandName =
  'new' | 'resume' | 'interrupt' | 'release' | 'status';

export interface TalkCommand {
  name: TalkCommandName;
  description: string;
}

/** Every command, in menu order. Availability is decided by the composer. */
export const TALK_COMMANDS: readonly TalkCommand[] = [
  { name: 'new', description: 'Start a new session' },
  { name: 'resume', description: 'Switch to another session' },
  { name: 'interrupt', description: 'Stop the running turn' },
  { name: 'release', description: 'Hand the session back to its terminal' },
  { name: 'status', description: 'Show where this message would go' },
];

/** Bubbled when a command is clicked: `{ name }`. */
export const TALK_COMMAND_PICK_EVENT = 'talk-command-pick';

/**
 * The slash query in `text`, or null when it is not a command being typed:
 * a lone `/word` with nothing after it. `/new please` is a message.
 */
export function slashQuery(text: string): string | null {
  const match = text.match(/^\/([a-z]*)$/i);
  return match ? match[1].toLowerCase() : null;
}

/** Commands among `available` whose name starts with the typed query. */
export function matchCommands(
  text: string,
  available: readonly TalkCommand[] = TALK_COMMANDS
): TalkCommand[] {
  const query = slashQuery(text);
  if (query === null) return [];
  return available.filter((command) => command.name.startsWith(query));
}

const STYLE = `
talk-command-menu { display: block; }
talk-command-menu .talk-command-list { list-style: none; margin: 0 0 0.25rem;
  padding: 0.25rem 0; border-radius: var(--sl-border-radius-medium, 4px);
  background: var(--console-surface-raised, var(--sl-color-neutral-0));
  box-shadow: var(--sl-shadow-small); }
talk-command-menu .talk-command { display: flex; gap: 0.75rem;
  align-items: baseline; padding: 0.2rem 0.75rem; cursor: pointer;
  font-size: var(--console-text-meta, 0.8125rem); }
talk-command-menu .talk-command[aria-selected='true'] {
  background: color-mix(in srgb, var(--sl-color-primary-600) 16%, transparent); }
talk-command-menu .talk-command-name {
  font-family: var(--console-font-mono, 'JetBrains Mono', ui-monospace,
    SFMono-Regular, Menlo, Consolas, monospace);
  min-width: 8ch; }
talk-command-menu .talk-command-description {
  color: var(--console-meta-color, var(--sl-color-neutral-600)); }
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

@customElement('talk-command-menu')
export class TalkCommandMenu extends LitElement {
  @property({ attribute: false }) commands: TalkCommand[] = [];
  @property({ type: Number }) activeIndex = 0;

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  connectedCallback(): void {
    super.connectedCallback();
    ensureStyle(this);
  }

  private pick(name: TalkCommandName): void {
    this.dispatchEvent(
      new CustomEvent(TALK_COMMAND_PICK_EVENT, {
        detail: { name },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    if (!this.commands.length) return nothing;
    return html`<ul
      class="talk-command-list"
      role="listbox"
      aria-label="Commands"
      data-testid="talk-command-menu"
    >
      ${this.commands.map(
        (command, index) =>
          html`<li
            class="talk-command"
            role="option"
            id=${`talk-command-${command.name}`}
            data-command=${command.name}
            aria-selected=${index === this.activeIndex ? 'true' : 'false'}
            @mousedown=${(event: Event) => event.preventDefault()}
            @click=${() => this.pick(command.name)}
          >
            <span class="talk-command-name">/${command.name}</span>
            <span class="talk-command-description">${command.description}</span>
          </li>`
      )}
    </ul>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talk-command-menu': TalkCommandMenu;
  }
}
