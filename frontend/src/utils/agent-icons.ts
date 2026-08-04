import { html, type TemplateResult } from 'lit';

export function renderAgentIcon(
  sourceType: string | null | undefined,
  style: string = ''
): TemplateResult {
  const renderIcon = (name: string, src?: string) => {
    return src
      ? html`<sl-icon src="${src}" style="${style}"></sl-icon>`
      : html`<sl-icon name="${name}" style="${style}"></sl-icon>`;
  };

  switch (sourceType?.toLowerCase()) {
    case 'claude_code':
      return renderIcon('', '/images/logos/claude.svg');
    case 'claude_desktop':
      return renderIcon('display');
    case 'openclaw':
      return renderIcon('', '/images/logos/openclaw.svg');
    case 'codex':
      return renderIcon('', '/images/logos/codex.svg?v=2');
    case 'gemini_cli':
    case 'gemini-cli':
    case 'geminicli':
      return renderIcon('', '/images/logos/gemini-cli.svg');
    case 'opencode':
      return renderIcon('', '/images/logos/opencode.svg');
    case 'hermes':
      return renderIcon('', '/images/logos/hermes.svg');
    case 'cursor':
      return renderIcon('', '/images/logos/cursor.svg');
    case 'windsurf':
      return renderIcon('', '/images/logos/Windsurf-black-symbol.svg');
    case 'vscode':
      return renderIcon('', '/images/logos/vscode.svg');
    case 'antigravity':
      return renderIcon('rocket');
    case 'devin':
      return renderIcon('robot');
    case 'desktop_agent':
      return renderIcon('pc-display');
    case 'custom':
      return renderIcon('robot');
    default:
      return renderIcon('robot');
  }
}
