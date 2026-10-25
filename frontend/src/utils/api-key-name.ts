/** Name given to keys the flow runtime creates for each run. */
const FLOW_RUN_KEY =
  /^Flow Execution ([0-9a-f]{8})(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

export interface ApiKeyDisplayName {
  /** Name to show as the heading or link text. */
  name: string;
  /** Full run id for an auto-created flow key, for the meta line. */
  fullId: string | null;
}

/**
 * Readable name for an API key. Auto-created flow keys are named
 * "Flow Execution <uuid>"; show them as "Flow run <short id>" instead.
 */
export function apiKeyDisplayName(name: string): ApiKeyDisplayName {
  const match = FLOW_RUN_KEY.exec(name.trim());
  if (!match) return { name, fullId: null };
  return {
    name: `Flow run ${match[1].toLowerCase()}`,
    fullId: name.trim().slice('Flow Execution '.length).toLowerCase(),
  };
}

/**
 * Server search matches the stored name, so a search typed from the displayed
 * "Flow run <id>" is mapped back to "Flow Execution <id>".
 */
export function apiKeySearchQuery(search: string): string {
  return search.replace(/^(\s*)flow run(?=\s|$)/i, '$1Flow Execution');
}
