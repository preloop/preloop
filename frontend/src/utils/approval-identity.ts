export const APPROVAL_SOURCE_KEY = '_preloop_source';

const SOURCE_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
  codex_cli: 'Codex CLI',
  cursor: 'Cursor',
  opencode: 'OpenCode',
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

/**
 * The requester name for a whole request, server-resolved name first.
 *
 * `managed_agent_name` is denormalized at creation time and older rows left
 * it empty even when they carried an agent id, which is how a named Claude
 * Code agent came to render as "AI agent". `agent.name` is resolved from the
 * id at read time, so it is right whenever the agent still exists.
 */
export function approvalRequesterName(
  request: {
    agent?: { name?: string | null } | null;
    managed_agent_name?: string | null;
    tool_args?: Record<string, unknown> | null;
  },
  fallback = 'AI agent'
): string {
  return formatApprovalRequester(
    request.agent?.name || request.managed_agent_name,
    request.tool_args,
    fallback
  );
}

export function withoutApprovalMetadata(
  toolArgs: Record<string, unknown>
): Record<string, unknown> {
  const { [APPROVAL_SOURCE_KEY]: _source, ...displayArgs } = toolArgs;
  return displayArgs;
}
