export const APPROVAL_SOURCE_KEY = '_preloop_source';
export const APPROVAL_REPOSITORY_KEY = '_preloop_repository';

/** Trusted hook observation stored beside the source marker. */
export interface RepositoryContext {
  remote?: string;
  toplevel?: string;
  relative_path?: string;
  source?: string;
  no_remote?: boolean;
}

/** Compact label and tooltip for a repository chip. */
export interface RepositoryChip {
  label: string;
  title: string;
}

const SOURCE_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  codex: 'Codex',
  codex_cli: 'Codex CLI',
  cursor: 'Cursor',
  opencode: 'OpenCode',
  pi: 'Pi',
  deepseek: 'DeepSeek Harness',
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
 *
 * When nothing names the agent but an id exists (deleted agent, or a server
 * that predates the resolved summary), the eight character id is the answer,
 * the same one `attributionParts` gives: the chip beside the attribution line
 * must not say "AI agent" while the line says "Agent 3f2a9c14".
 */
export function approvalRequesterName(
  request: {
    agent?: { id?: string | null; name?: string | null } | null;
    managed_agent_id?: string | null;
    managed_agent_name?: string | null;
    tool_args?: Record<string, unknown> | null;
  },
  fallback = 'AI agent'
): string {
  const agentId = (request.agent?.id || request.managed_agent_id || '').trim();
  return formatApprovalRequester(
    request.agent?.name ||
      request.managed_agent_name ||
      (agentId ? agentId.slice(0, 8) : null),
    request.tool_args,
    fallback
  );
}

function asRepositoryContext(value: unknown): RepositoryContext | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as RepositoryContext;
}

/**
 * Read the hook's repository observation from tool args or activity metadata.
 *
 * The marker is not a tool argument. Callers that render arguments use
 * `withoutApprovalMetadata` so it never appears there.
 */
export function getRepositoryContext(
  record: Record<string, unknown> | null | undefined
): RepositoryContext | null {
  if (!record) return null;
  const direct = asRepositoryContext(record[APPROVAL_REPOSITORY_KEY]);
  if (direct) return direct;
  const nested = record.tool_args;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    return asRepositoryContext(
      (nested as Record<string, unknown>)[APPROVAL_REPOSITORY_KEY]
    );
  }
  return null;
}

/** `owner/repo` plus a relative path, or "no remote" when the work tree has none. */
export function formatRepositoryChip(
  context: RepositoryContext | null | undefined
): RepositoryChip | null {
  if (!context) return null;
  const remote = (context.remote || '').trim();
  const toplevel = (context.toplevel || '').trim();
  const relative = (context.relative_path || '').trim();
  const noRemote = context.no_remote === true || (!remote && Boolean(toplevel));
  const title = [remote, toplevel].filter(Boolean).join('\n');
  if (noRemote) {
    return { label: 'no remote', title: title || 'No origin remote' };
  }
  if (!remote) return null;
  const parts = remote.split('/').filter(Boolean);
  const ownerRepo = parts.length > 1 ? parts.slice(1).join('/') : remote;
  const label = relative ? `${ownerRepo} · ${relative}` : ownerRepo;
  return { label, title: title || remote };
}

export function withoutApprovalMetadata(
  toolArgs: Record<string, unknown>
): Record<string, unknown> {
  const {
    [APPROVAL_SOURCE_KEY]: _source,
    [APPROVAL_REPOSITORY_KEY]: _repository,
    ...displayArgs
  } = toolArgs;
  return displayArgs;
}
