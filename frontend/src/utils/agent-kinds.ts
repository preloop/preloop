/**
 * Presentation metadata for durable agent kinds (issue #123).
 *
 * An agent's kind records which product it is (`cursor`), as opposed to its
 * session source type, which records how it connects (`desktop_agent`).
 * Several products share the generic desktop transport, so the console keys
 * its label and icon off the kind.
 *
 * Label and icon live in one table so a newly supported product cannot end up
 * named in one view and unnamed in another.
 */

export interface AgentKindPresentation {
  /** Human-readable product name. */
  label: string;
  /** Shoelace icon name, used when no logo asset exists. */
  icon: string;
  /** Path to a logo asset, preferred over the icon when present. */
  logo?: string;
}

export const AGENT_KIND_PRESENTATION: Record<string, AgentKindPresentation> = {
  claude_code: {
    label: 'Claude Code',
    icon: '',
    logo: '/images/logos/claude.svg',
  },
  claude_desktop: { label: 'Claude Desktop', icon: 'display' },
  codex: { label: 'Codex', icon: '', logo: '/images/logos/codex.svg?v=2' },
  openclaw: { label: 'OpenClaw', icon: '', logo: '/images/logos/openclaw.svg' },
  gemini_cli: {
    label: 'Gemini CLI',
    icon: '',
    logo: '/images/logos/gemini-cli.svg',
  },
  // Older rows recorded the kind without a separator.
  geminicli: {
    label: 'Gemini CLI',
    icon: '',
    logo: '/images/logos/gemini-cli.svg',
  },
  opencode: { label: 'OpenCode', icon: '', logo: '/images/logos/opencode.svg' },
  hermes: { label: 'Hermes', icon: '', logo: '/images/logos/hermes.svg' },
  cursor: { label: 'Cursor', icon: '', logo: '/images/logos/cursor.svg' },
  windsurf: {
    label: 'Windsurf',
    icon: '',
    logo: '/images/logos/Windsurf-black-symbol.svg',
  },
  vscode: { label: 'VS Code', icon: '', logo: '/images/logos/vscode.svg' },
  antigravity: { label: 'Antigravity', icon: 'rocket' },
  devin: { label: 'Devin', icon: 'robot' },
  desktop_agent: { label: 'Desktop Agent', icon: 'pc-display' },
  custom: { label: 'Custom Agent', icon: 'robot' },
};

/**
 * Kinds the CLI discovers on a machine and can onboard.
 *
 * `preloop agents onboard <name>` scans the local machine for a known product
 * and enrolls it. These are the kinds it can produce (see
 * `managedAgentKindForAgent` in the CLI). Everything else - `custom` agents,
 * LangGraph graphs, anything wired through an SDK - is started by whoever
 * wrote it, so telling its owner to run the onboarding command is wrong: the
 * CLI has nothing to find.
 */
export const CLI_ONBOARDABLE_AGENT_KINDS: ReadonlySet<string> = new Set([
  'claude_code',
  'claude_desktop',
  'codex',
  'openclaw',
  'gemini_cli',
  'geminicli',
  'opencode',
  'hermes',
  'cursor',
  'windsurf',
  'vscode',
  'antigravity',
  'devin',
  'desktop_agent',
]);

/**
 * True when `preloop agents onboard` is the right instruction for this kind.
 *
 * An unknown kind answers false on purpose: printing a command that cannot
 * work is worse than printing none.
 */
export function isCliOnboardableAgentKind(
  kind: string | null | undefined
): boolean {
  return CLI_ONBOARDABLE_AGENT_KINDS.has(normalizeAgentKind(kind));
}

/**
 * Fold a raw kind into the canonical form used as a table key.
 *
 * Mirrors the server-side normalization so `Gemini CLI`, `gemini-cli`, and
 * `gemini_cli` all resolve to the same entry.
 */
export function normalizeAgentKind(kind: string | null | undefined): string {
  return (kind || '').trim().toLowerCase().replace(/[\s-]/g, '_');
}

/** Look up presentation metadata for a kind, or undefined if unknown. */
export function getAgentKindPresentation(
  kind: string | null | undefined
): AgentKindPresentation | undefined {
  return AGENT_KIND_PRESENTATION[normalizeAgentKind(kind)];
}
