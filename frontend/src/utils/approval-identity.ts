export const APPROVAL_SOURCE_KEY = '_preloop_source';
export const APPROVAL_REPOSITORY_KEY = '_preloop_repository';

/**
 * The trusted repository observation the hook recorded from its own cwd.
 *
 * Only the hook sets this: it is stamped next to `_preloop_source` and must
 * never be read from caller-supplied tool arguments.
 */
export interface ApprovalRepository {
  /** Normalized `host/owner/repo`, empty when the work tree has no origin. */
  remote: string;
  toplevel?: string | null;
  relative_path?: string | null;
  source?: string | null;
  /** True when the work tree exists but has no `origin` remote. */
  no_remote: boolean;
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

/**
 * Read the repository marker, or null when there is none.
 *
 * A malformed marker (not an object, or no remote and no `no_remote` flag)
 * yields null so a surface renders nothing rather than an empty chip.
 */
export function getApprovalRepository(
  toolArgs: Record<string, unknown> | null | undefined
): ApprovalRepository | null {
  const raw = toolArgs?.[APPROVAL_REPOSITORY_KEY];
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const remote = cleanString(record.remote);
  const noRemote = record.no_remote === true;
  if (!remote && !noRemote) return null;
  return {
    remote,
    toplevel: cleanString(record.toplevel) || null,
    relative_path: cleanString(record.relative_path) || null,
    source: cleanString(record.source) || null,
    no_remote: noRemote,
  };
}

function cleanString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * The repository's short name: `owner/repo`, dropping the host and keeping
 * nested groups (`group/sub/repo`). Null when there is no remote to name.
 */
export function formatApprovalRepository(
  repository: ApprovalRepository | null | undefined
): string | null {
  if (!repository || repository.no_remote) return null;
  const remote = repository.remote.trim();
  if (!remote) return null;
  const slash = remote.indexOf('/');
  if (slash < 0) return remote;
  return remote.slice(slash + 1).replace(/^\/+|\/+$/g, '') || remote;
}

/** Label and tooltip for a repository chip, including a relative path. */
export function repositoryObservationChip(
  toolArgs: Record<string, unknown> | null | undefined
): { label: string; title: string } | null {
  const repository = getApprovalRepository(toolArgs);
  if (!repository) return null;
  const name = formatApprovalRepository(repository);
  const relative = (repository.relative_path || '').replace(/^\/+|\/+$/g, '');
  let label = name;
  if (label && relative) label = `${label} · ${relative}`;
  if (!label && repository.no_remote) label = 'no remote';
  if (!label) return null;
  const title =
    [repository.remote, repository.toplevel].filter(Boolean).join('\n') ||
    label;
  return { label, title };
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
