export const APPROVAL_SOURCE_KEY = '_preloop_source';

const SOURCE_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
  codex_cli: 'Codex CLI',
  cursor: 'Cursor',
  openclaw: 'OpenClaw',
  hermes: 'Hermes',
};

export function getApprovalSource(
  toolArgs: Record<string, unknown> | null | undefined
): string | null {
  const source = toolArgs?.[APPROVAL_SOURCE_KEY];
  return typeof source === 'string' && source.trim() ? source.trim() : null;
}

export function formatApprovalSource(source: string | null): string | null {
  if (!source) return null;
  return (
    SOURCE_LABELS[source.toLowerCase()] ??
    source
      .split(/[_-]/)
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ')
  );
}

export function formatApprovalRequester(
  managedAgentName: string | null | undefined,
  toolArgs: Record<string, unknown> | null | undefined,
  fallback = 'AI agent'
): string {
  const agentName = managedAgentName?.trim() || null;
  const source = formatApprovalSource(getApprovalSource(toolArgs));

  if (!agentName) return source || fallback;
  if (!source || source.toLowerCase() === agentName.toLowerCase()) {
    return agentName;
  }
  return `${agentName} via ${source}`;
}

export function withoutApprovalMetadata(
  toolArgs: Record<string, unknown>
): Record<string, unknown> {
  const { [APPROVAL_SOURCE_KEY]: _source, ...displayArgs } = toolArgs;
  return displayArgs;
}
