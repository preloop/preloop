/**
 * Persisted filters for the flow executions list.
 *
 * The page remounts when an operator opens a run or uses the sidebar, and
 * the query string is gone with it. A stored choice comes back. Explicit
 * URL params still win on entry. Invalid storage is ignored and removed.
 * Private mode and quota errors must not take the page down.
 */

export const FLOW_EXECUTION_FILTERS_KEY = 'preloop.flow-executions.filters';

/** Status values `applyQueryParams` already accepts from `?status=`. */
export const FLOW_EXECUTION_STATUSES = [
  'all',
  'RUNNING',
  'PENDING',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
] as const;

/** Range values the executions range control offers. */
export const FLOW_EXECUTION_RANGES = [
  'day',
  'week',
  'month',
  'year',
  'all',
] as const;

/** Search text stored with the filters. Longer input is cut here. */
export const FLOW_EXECUTION_QUERY_MAX = 200;

export type FlowExecutionStatus = (typeof FLOW_EXECUTION_STATUSES)[number];
export type FlowExecutionRange = (typeof FLOW_EXECUTION_RANGES)[number];

export interface FlowExecutionListFilters {
  status: FlowExecutionStatus;
  flow: string | null;
  range: FlowExecutionRange;
  q: string;
}

export const DEFAULT_FLOW_EXECUTION_FILTERS: FlowExecutionListFilters = {
  status: 'all',
  flow: null,
  range: 'month',
  q: '',
};

export function isFlowExecutionStatus(
  value: unknown
): value is FlowExecutionStatus {
  return (
    typeof value === 'string' &&
    (FLOW_EXECUTION_STATUSES as readonly string[]).includes(value)
  );
}

export function isFlowExecutionRange(
  value: unknown
): value is FlowExecutionRange {
  return (
    typeof value === 'string' &&
    (FLOW_EXECUTION_RANGES as readonly string[]).includes(value)
  );
}

/** True when every field is the page default. */
export function isDefaultFlowExecutionFilters(
  filters: FlowExecutionListFilters
): boolean {
  return (
    filters.status === DEFAULT_FLOW_EXECUTION_FILTERS.status &&
    filters.flow === DEFAULT_FLOW_EXECUTION_FILTERS.flow &&
    filters.range === DEFAULT_FLOW_EXECUTION_FILTERS.range &&
    filters.q === DEFAULT_FLOW_EXECUTION_FILTERS.q
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Keep known fields and drop the rest.
 *
 * Returns null when nothing in the value can be a filter blob, so the
 * caller can remove it. A long `q` is capped rather than rejected.
 */
export function sanitizeFlowExecutionFilters(
  value: unknown
): FlowExecutionListFilters | null {
  if (!isRecord(value)) return null;
  const filters: FlowExecutionListFilters = {
    ...DEFAULT_FLOW_EXECUTION_FILTERS,
  };
  if (isFlowExecutionStatus(value.status)) {
    filters.status = value.status;
  }
  if (typeof value.flow === 'string' && value.flow.trim() !== '') {
    filters.flow = value.flow.trim().slice(0, FLOW_EXECUTION_QUERY_MAX);
  }
  if (isFlowExecutionRange(value.range)) {
    filters.range = value.range;
  }
  if (typeof value.q === 'string') {
    filters.q = value.q.slice(0, FLOW_EXECUTION_QUERY_MAX);
  }
  return filters;
}

function readRaw(): string | null {
  try {
    return localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY);
  } catch {
    return null;
  }
}

/** Remove the stored filters. Failures are ignored. */
export function clearFlowExecutionFilters(): void {
  try {
    localStorage.removeItem(FLOW_EXECUTION_FILTERS_KEY);
  } catch {
    // Storage is a preference, not a requirement.
  }
}

/**
 * Read the stored filters.
 *
 * Missing storage returns null so the page can keep its defaults. Garbage
 * and unknown fields are removed. A blob that is not an object is cleared.
 */
export function loadFlowExecutionFilters(): FlowExecutionListFilters | null {
  const raw = readRaw();
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    clearFlowExecutionFilters();
    return null;
  }
  const filters = sanitizeFlowExecutionFilters(parsed);
  if (!filters) {
    clearFlowExecutionFilters();
    return null;
  }
  if (storedShapeDiffers(parsed as Record<string, unknown>, filters)) {
    saveFlowExecutionFilters(filters);
  }
  return filters;
}

/** True when the stored object is not exactly the sanitized filters. */
function storedShapeDiffers(
  parsed: Record<string, unknown>,
  filters: FlowExecutionListFilters
): boolean {
  const keys = Object.keys(parsed);
  if (keys.some((key) => !['status', 'flow', 'range', 'q'].includes(key))) {
    return true;
  }
  if (parsed.status !== filters.status) return true;
  const storedFlow = parsed.flow === undefined ? null : parsed.flow;
  if (storedFlow !== filters.flow) return true;
  if (parsed.range !== filters.range) return true;
  if (parsed.q !== filters.q) return true;
  return false;
}

/**
 * Persist a filter set. Unknown status or range is dropped. An all-default
 * set is removed so the next visit is a clean page. Failures are ignored.
 */
export function saveFlowExecutionFilters(
  filters: FlowExecutionListFilters
): void {
  const sanitized = sanitizeFlowExecutionFilters(filters);
  if (!sanitized || isDefaultFlowExecutionFilters(sanitized)) {
    clearFlowExecutionFilters();
    return;
  }
  try {
    localStorage.setItem(FLOW_EXECUTION_FILTERS_KEY, JSON.stringify(sanitized));
  } catch {
    // Same as load: storage is a preference, not a requirement.
  }
}
