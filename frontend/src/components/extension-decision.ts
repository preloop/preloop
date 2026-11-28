/**
 * What an approver reads on one extension row: its purpose, where it came
 * from, and what it can do. Every line is derived from reported metadata;
 * when a fact is missing the slot stays empty rather than saying so.
 */
import type {
  ExtensionAggregate,
  ExtensionCapabilityKey,
  ExtensionKey,
  ExtensionSource,
  InstalledExtension,
} from '../extension-inventory-api';

type Row = InstalledExtension | ExtensionAggregate;

export const EXTENSION_KIND_LABELS: Record<
  ExtensionKey['extension_kind'],
  string
> = {
  plugin: 'Plugin',
  skill: 'Skill',
  hook: 'Hook',
  command: 'Command',
  mcp_server: 'MCP server',
};

export const SOURCE_LABELS: Record<ExtensionSource, string> = {
  official_marketplace: 'Official marketplace',
  third_party_marketplace: 'Community marketplace',
  git: 'Git URL',
  local: 'Local file',
  builtin: 'Built in',
  unknown: 'Unknown source',
};

export const CAPABILITY_FILTER_LABELS: Record<ExtensionCapabilityKey, string> =
  {
    shell: 'Runs shell commands',
    files_read: 'Reads files',
    files_write: 'Writes files',
    network: 'Network access',
    hooks: 'Registers hooks',
    mcp: 'MCP tools',
    scripts: 'Executes scripts',
    unknown: 'Unknown',
  };

const list = (values: string[]): string =>
  values.length <= 2
    ? values.join(' and ')
    : `${values.slice(0, -1).join(', ')} and ${values[values.length - 1]}`;

/**
 * The one-line purpose. Authored or derived descriptions first; hooks and
 * MCP servers say what they run; built-in plugins say where they come from.
 * Returns an empty string when nothing is known.
 */
export function extensionPurpose(row: Row): string {
  const details = row.details;
  if (details?.description) return details.description;
  if (row.extension_kind === 'hook') {
    const executables = [
      ...new Set(
        (details?.hook_matchers ?? []).flatMap((matcher) =>
          matcher.commands.map((command) =>
            command.preloop_managed ? 'Preloop' : command.executable
          )
        )
      ),
    ].filter((name) => name && name !== 'unknown');
    return executables.length
      ? `Runs ${list(executables)} on ${row.extension_id}`
      : '';
  }
  if (row.extension_kind === 'mcp_server') {
    if (details?.transport === 'stdio')
      return details.command
        ? `Starts ${details.command} on the device (stdio)`
        : 'Starts a local program (stdio)';
    if (details?.transport === 'http' || details?.transport === 'sse')
      return details.url_host
        ? `Connects to ${details.url_host} (${details.transport.toUpperCase()})`
        : `Connects to a remote server (${details.transport.toUpperCase()})`;
    return '';
  }
  if (sourceKinds(row).includes('builtin')) return 'Ships with Claude Code';
  return '';
}

/**
 * The name to read: the declared name, else the identifier. A marketplace
 * plugin drops its "@marketplace" suffix, which the Source column states.
 */
export function displayName(row: Row): string {
  if (row.details?.name) return row.details.name;
  if (row.extension_kind === 'plugin' && row.extension_id.includes('@'))
    return row.extension_id.slice(0, row.extension_id.lastIndexOf('@'));
  return row.extension_id;
}

export function sourceKinds(row: Row): ExtensionSource[] {
  return 'source_kinds' in row ? row.source_kinds : [row.source_kind];
}

/**
 * Where it came from, as a trust hint: the source kind in words, then the
 * marketplace and publisher when they were reported.
 */
export function extensionSource(row: Row): { label: string; detail: string } {
  const kinds = sourceKinds(row);
  const label = kinds.map((kind) => SOURCE_LABELS[kind] ?? kind).join(', ');
  const parts: string[] = [];
  const details = row.details;
  if (details?.marketplace && !kinds.includes('builtin'))
    parts.push(details.marketplace);
  if (details?.author) parts.push(`by ${details.author}`);
  else if (details?.plugin) parts.push(`in ${details.plugin}`);
  return { label, detail: parts.join(' · ') };
}

/** "2 skills, 3 commands" — what a plugin bundles, in counts. */
export function bundleSummary(row: Row): string {
  const bundles = row.details?.bundles;
  if (!bundles) return '';
  const parts = (
    [
      [bundles.skills, 'skill'],
      [bundles.commands, 'command'],
      [bundles.agents, 'agent'],
      [bundles.hooks, 'hook'],
      [bundles.mcp_servers, 'MCP server'],
    ] as Array<[number, string]>
  )
    .filter(([count]) => count > 0)
    .map(([count, noun]) => `${count} ${noun}${count === 1 ? '' : 's'}`);
  return parts.length
    ? parts.join(', ')
    : 'No skills, commands, hooks or MCP servers';
}

/**
 * Nest a plugin's bundled skills under the plugin when both are on the page,
 * so they read as its contents instead of separate unexplained rows.
 */
export function groupBundled<T extends Row>(
  rows: T[]
): { top: T[]; children: Map<string, T[]> } {
  const pluginKey = (agent: string, id: string): string =>
    JSON.stringify([agent, id]);
  const plugins = new Set(
    rows
      .filter((row) => row.extension_kind === 'plugin')
      .map((row) => pluginKey(row.agent_kind, row.extension_id))
  );
  const children = new Map<string, T[]>();
  const top: T[] = [];
  for (const row of rows) {
    const parent = row.details?.plugin
      ? pluginKey(row.agent_kind, row.details.plugin)
      : '';
    if (row.extension_kind !== 'plugin' && parent && plugins.has(parent)) {
      children.set(parent, [...(children.get(parent) ?? []), row]);
    } else {
      top.push(row);
    }
  }
  return { top, children };
}

export function childrenOf<T extends Row>(
  children: Map<string, T[]>,
  row: Row
): T[] {
  return row.extension_kind === 'plugin'
    ? (children.get(JSON.stringify([row.agent_kind, row.extension_id])) ?? [])
    : [];
}
