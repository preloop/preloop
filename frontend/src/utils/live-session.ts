/**
 * Shared live-session state for the console's session surfaces.
 *
 * Two questions, one answer each, for Talk, Conversation and Transcript:
 *
 *   1. *What is happening right now?* — `deriveLiveSessionState` reduces the
 *      observed events, activity rows and pending approvals into one status
 *      plus the evidence behind it.
 *   2. *What tools did it run?* — `normalizeToolCalls` turns gateway
 *      `tool_activity` entries and native `tool_call` rows into named rows
 *      with an explicit lifecycle state.
 *
 * Pure functions: no Lit, no fetch, no timers. Every value a surface renders
 * is derived here so the three surfaces cannot drift apart, and so the rules
 * (what counts as proof a tool ran, what counts as proof a model request is
 * still in flight) are testable without a browser.
 *
 * Honesty rules that the rest of the console depends on:
 *
 * - **A successful model request is not proof its tool succeeded.** A gateway
 *   `tool_calls` entry is a *request*; it only becomes `running` when something
 *   observed execution, and `completed`/`failed` only when a result exists.
 * - **Never invent a correlation.** Requests are paired by the gateway's
 *   `gateway_request_id`, never by arrival order. An observation with no id
 *   stays unpaired and contributes no lifecycle claim.
 * - **Absence of evidence is a state, not a blank.** `unknown` and
 *   `unavailable` exist so a redacted, legacy or paged-away record reads as
 *   "we do not know" rather than as "nothing happened".
 */

/** How long an unmatched `model_gateway_request_started` may claim "processing". */
export const MAX_INFLIGHT_REQUEST_MS = 10 * 60 * 1000;

/** Lifecycle states a tool row may report. Only evidence-backed ones are used. */
export type ToolCallState =
  'requested' | 'running' | 'completed' | 'failed' | 'unknown';

export const TOOL_CALL_STATE_LABELS: Record<ToolCallState, string> = {
  requested: 'requested',
  running: 'running',
  completed: 'completed',
  failed: 'failed',
  unknown: 'unknown',
};

/** One normalized tool invocation, ready to render as a card. */
export interface LiveToolCall {
  /**
   * Stable render key. Deduplicated by `id` when the producer gave us one,
   * scoped to its source row otherwise. Never derived from the arguments:
   * two identical calls in one turn are two calls.
   */
  key: string;
  /** Provider-assigned call id, or null when the producer supplied none. */
  id: string | null;
  /** Whether `id` provably identifies this call across rows and events. */
  stableId: boolean;
  name: string | null;
  serverName: string | null;
  state: ToolCallState;
  /** Short command / path / URL line shown under the header. */
  summary: string;
  /** Pretty-printed arguments, or null when not captured. */
  argumentsText: string | null;
  /** Result text, or null when not captured. */
  resultText: string | null;
  /** Duration in ms, only when a start AND an end were both observed. */
  durationMs: number | null;
  timestamp: string | null;
  redacted: boolean;
  truncated: boolean;
  /**
   * What kind of tool arguments this is, when the row may carry
   * `_preloop_repository`. Read-only: the chip that renders it fetches
   * nothing and executes nothing.
   */
  repositoryArgs: Record<string, unknown> | null;
}

/** One entry of `payload.tool_activity`, as emitted by the gateway. */
interface GatewayToolActivityEntry {
  id?: string | null;
  stable_id?: boolean;
  direction?: 'call' | 'result' | string;
  name?: string | null;
  dialect?: string | null;
  arguments?: string | null;
  result?: string | null;
  is_error?: boolean | null;
  redacted?: boolean;
  truncated?: boolean;
}

export interface GatewayToolActivity {
  entries?: GatewayToolActivityEntry[];
  truncated?: boolean;
  dialect?: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function parseTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

/** Argument keys that carry the one detail worth showing on a collapsed row. */
const SUMMARY_KEYS = [
  'command',
  'cmd',
  'script',
  'path',
  'file_path',
  'file',
  'url',
  'uri',
  'query',
  'pattern',
  'name',
  'prompt',
  'description',
];

const SUMMARY_MAX_CHARS = 160;

/**
 * A one-line "what did this actually do" preview.
 *
 * Reads structured arguments only. It never scans prose: a tool whose
 * arguments have no recognizable key gets the first key/value pair rendered as
 * text, and a tool with no arguments at all gets an empty summary rather than
 * a guess.
 */
export function summarizeToolArguments(
  argumentsText: string | null | undefined
): string {
  if (!argumentsText) return '';
  let parsed: unknown = argumentsText;
  const trimmed = argumentsText.trim();
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      // Malformed or truncated JSON is still worth showing verbatim.
      parsed = trimmed;
    }
  }
  if (typeof parsed === 'string') return firstLine(parsed);
  if (!isRecord(parsed)) return firstLine(String(parsed));

  for (const key of SUMMARY_KEYS) {
    const value = parsed[key];
    if (typeof value === 'string' && value.trim()) return clip(value);
    if (typeof value === 'number' || typeof value === 'boolean') {
      return clip(`${key}: ${String(value)}`);
    }
  }
  const [firstKey] = Object.keys(parsed);
  if (!firstKey) return '';
  const firstValue = parsed[firstKey];
  if (typeof firstValue === 'string') return clip(firstValue);
  if (typeof firstValue === 'number' || typeof firstValue === 'boolean') {
    return clip(`${firstKey}: ${String(firstValue)}`);
  }
  if (firstValue === null || firstValue === undefined) return '';
  return clip(`${firstKey}: ${JSON.stringify(firstValue)}`);
}

function firstLine(text: string): string {
  return clip(text.split('\n').find((line) => line.trim()) ?? '');
}

function clip(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > SUMMARY_MAX_CHARS
    ? `${flat.slice(0, SUMMARY_MAX_CHARS)}…`
    : flat;
}

/** Map a native activity row's free-text status onto a lifecycle state. */
export function toolStateFromActivityStatus(
  status: string | null | undefined
): ToolCallState {
  const normalized = (status || '').toLowerCase();
  if (!normalized) return 'unknown';
  if (/(fail|error|denied|rejected|timeout|timed_out)/.test(normalized)) {
    return 'failed';
  }
  if (/(success|complete|ok$|done|finished|passed)/.test(normalized)) {
    return 'completed';
  }
  if (
    /(running|in_?progress|started|detected|executing|streaming)/.test(
      normalized
    )
  ) {
    return 'running';
  }
  return 'unknown';
}

interface ToolAccumulator {
  call: LiveToolCall;
  hasCall: boolean;
  hasResult: boolean;
  isError: boolean | null;
  firstAt: number | null;
  lastAt: number | null;
}

function entryId(entry: GatewayToolActivityEntry): string | null {
  const id = entry.id;
  return typeof id === 'string' && id.trim() ? id.trim() : null;
}

/**
 * Normalize every observed tool invocation, oldest first.
 *
 * Two producers, merged only where identity is provable:
 *
 * - `payload.tool_activity` on gateway events. The gateway already read the
 *   structured wire fields; a call and its result share the provider's call
 *   id, which is the only identity we accept as proof they are the same call.
 * - native `tool_call` activity rows. These carry the MCP server's own
 *   `correlation_id`, which is a different namespace from the provider call
 *   id, so a row is never merged into a gateway entry on a name match. Both
 *   render; that is honest, and in practice a session has one or the other.
 *
 * Entries with no stable id are scoped to the row that produced them, so two
 * id-less calls in the same turn stay two rows instead of collapsing.
 */
export function normalizeToolCalls(
  events: Array<{
    id: string;
    type: string;
    timestamp: string | null;
    payload: Record<string, unknown>;
  }>,
  activity: Array<{
    activity_type: string;
    timestamp: string;
    tool_name: string | null;
    server_name: string | null;
    status: string | null;
    summary: string | null;
    metadata?: Record<string, unknown>;
  }> = []
): LiveToolCall[] {
  const byKey = new Map<string, ToolAccumulator>();

  const ensure = (
    key: string,
    seed: Partial<LiveToolCall> & Pick<LiveToolCall, 'name' | 'state'>
  ): ToolAccumulator => {
    const existing = byKey.get(key);
    if (existing) return existing;
    const created: ToolAccumulator = {
      call: {
        key,
        id: seed.id ?? null,
        stableId: seed.stableId ?? false,
        name: seed.name,
        serverName: seed.serverName ?? null,
        state: seed.state,
        summary: '',
        argumentsText: null,
        resultText: null,
        durationMs: null,
        timestamp: null,
        redacted: false,
        truncated: false,
        repositoryArgs: null,
      },
      hasCall: false,
      hasResult: false,
      isError: null,
      firstAt: null,
      lastAt: null,
    };
    byKey.set(key, created);
    return created;
  };

  const stamp = (bucket: ToolAccumulator, at: number | null): void => {
    if (at === null) return;
    if (bucket.firstAt === null || at < bucket.firstAt) bucket.firstAt = at;
    if (bucket.lastAt === null || at > bucket.lastAt) bucket.lastAt = at;
  };

  // Pass 1: gateway tool_activity, oldest event first so entry order within an
  // event is preserved (that is the order the model asked in).
  const orderedEvents = [...events].sort(
    (left, right) =>
      (parseTime(left.timestamp) ?? 0) - (parseTime(right.timestamp) ?? 0)
  );
  for (const event of orderedEvents) {
    const activityPayload = event.payload?.tool_activity;
    if (!isRecord(activityPayload)) continue;
    const entries = (activityPayload as GatewayToolActivity).entries;
    if (!Array.isArray(entries)) continue;
    const at = parseTime(event.timestamp);

    for (const [index, entry] of entries.entries()) {
      if (!isRecord(entry)) continue;
      const id = entryId(entry as GatewayToolActivityEntry);
      const stableId = id !== null;
      const key = stableId
        ? `gw:${id}`
        : `gw:${event.id}:${index}:${entry.direction ?? 'entry'}`;
      const bucket = ensure(key, {
        id,
        stableId,
        name: (entry.name as string) ?? null,
        state: 'requested',
        timestamp: event.timestamp,
      });
      const call = bucket.call;
      if (!call.timestamp && event.timestamp) call.timestamp = event.timestamp;
      stamp(bucket, at);
      if (entry.direction === 'result') {
        bucket.hasResult = true;
        if (typeof entry.result === 'string' && entry.result) {
          call.resultText = entry.result;
        }
        if (entry.is_error === true) bucket.isError = true;
        else if (entry.is_error === false && bucket.isError === null) {
          bucket.isError = false;
        }
      } else {
        bucket.hasCall = true;
        if (typeof entry.arguments === 'string' && entry.arguments) {
          call.argumentsText = entry.arguments;
        }
      }
      if (!call.name && entry.name) call.name = entry.name as string;
      if (entry.redacted) call.redacted = true;
      if (entry.truncated) call.truncated = true;
    }
  }

  // Pass 2: native tool_call activity rows.
  for (const [index, row] of activity.entries()) {
    if ((row.activity_type || '').toLowerCase() !== 'tool_call') continue;
    const metadata = isRecord(row.metadata) ? row.metadata : {};
    const correlationId =
      typeof metadata.correlation_id === 'string' &&
      metadata.correlation_id.trim()
        ? metadata.correlation_id.trim()
        : null;
    const key = correlationId
      ? `act:${correlationId}`
      : `act:${row.timestamp}:${index}:${row.tool_name ?? 'tool'}`;
    const state = toolStateFromActivityStatus(row.status);
    const startedAt = parseTime(
      typeof metadata.started_at === 'string' ? metadata.started_at : null
    );
    const endedAt = parseTime(row.timestamp);
    const bucket = ensure(key, {
      id: correlationId,
      stableId: correlationId !== null,
      name: row.tool_name,
      serverName: row.server_name,
      state,
      timestamp: row.timestamp,
    });
    const call = bucket.call;
    bucket.hasCall = true;
    stamp(bucket, endedAt);
    if (row.server_name && !call.serverName) call.serverName = row.server_name;
    if (!call.timestamp) call.timestamp = row.timestamp;
    if (state === 'failed') bucket.isError = true;
    else if (state === 'completed') bucket.hasResult = true;
    const argumentsSummary = metadata.arguments_summary;
    if (!call.summary && typeof argumentsSummary === 'string') {
      call.summary = clip(argumentsSummary);
    }
    if (!call.argumentsText && typeof row.summary === 'string') {
      call.summary = call.summary || clip(row.summary);
    }
    // A duration needs BOTH ends. A row without `started_at` reports none
    // rather than measuring the gap to the previous activity row.
    if (startedAt !== null && endedAt !== null) {
      call.durationMs = Math.max(0, endedAt - startedAt);
    }
    if (isRecord(metadata._preloop_repository)) {
      call.repositoryArgs = metadata as Record<string, unknown>;
    }
  }

  return Array.from(byKey.values()).map(
    ({ call, hasCall, hasResult, isError, firstAt, lastAt }) => {
      const resolved: LiveToolCall = { ...call };
      if (isError === true) resolved.state = 'failed';
      else if (hasResult) resolved.state = 'completed';
      else if (hasCall) resolved.state = 'requested';
      else
        resolved.state =
          resolved.state === 'unknown' ? 'unknown' : resolved.state;
      // A span needs both ends. A call with no observed result keeps `null`,
      // because "the model asked at T" is not a duration.
      if (firstAt !== null && lastAt !== null && lastAt > firstAt) {
        resolved.durationMs = lastAt - firstAt;
      }
      if (!resolved.summary) {
        resolved.summary =
          summarizeToolArguments(resolved.argumentsText) ||
          summarizeToolArguments(resolved.resultText);
      }
      return resolved;
    }
  );
}

/** The one-line session status shown above a conversation or transcript. */
export type LiveStatus =
  | 'model_processing'
  | 'running_tool'
  | 'waiting_for_approval'
  | 'waiting_for_agent'
  | 'idle'
  | 'ended'
  | 'unavailable';

/** Transport health is reported separately from agent work, never merged into it. */
export type TransportState = 'connected' | 'reconnecting' | 'unknown';

export interface LiveSessionState {
  status: LiveStatus;
  /** Short headline, e.g. `Model processing`. */
  label: string;
  /** Supporting fact: the model, the tool name, the pending count. */
  detail: string | null;
  /** Epoch ms the current status began, or null when not observed. */
  since: number | null;
  transport: TransportState;
  /** Epoch ms of the newest observation, for the `Reconnecting` line. */
  lastUpdateAt: number | null;
  /** How many pending approvals are blocking, when any. */
  pendingApprovalCount: number;
}

export interface LiveSessionInput {
  events: Array<{
    type: string;
    timestamp: string | null;
    payload: Record<string, unknown>;
  }>;
  activity?: Array<{
    activity_type: string;
    timestamp: string;
    status: string | null;
  }>;
  /** Requests from THIS session only; the caller is responsible for scoping. */
  pendingApprovals?: Array<{ status: string }>;
  connected?: boolean | null;
  /** True once the session itself reported that it ended. */
  ended?: boolean;
  now?: number;
}

const LABELS: Record<LiveStatus, string> = {
  model_processing: 'Model processing',
  running_tool: 'Tool running',
  waiting_for_approval: 'Waiting for approval',
  waiting_for_agent: 'Waiting for agent',
  idle: 'Idle',
  ended: 'Ended',
  unavailable: 'Activity unavailable',
};

const STARTED_EVENT_TYPE = 'model_gateway_request_started';

/**
 * Requests announced by `model_gateway_request_started` and not yet closed by
 * a `model_gateway_call` carrying the same `gateway_request_id`.
 *
 * Starts without an id cannot be paired at all and are ignored: matching them
 * by arrival order is exactly the bug this replaces, and a spinner that
 * outlasts its evidence is worse than no spinner. A start whose id never
 * arrives within `MAX_INFLIGHT_REQUEST_MS` stops claiming processing for the
 * same reason.
 */
export function inFlightGatewayRequests(
  events: LiveSessionInput['events'],
  now: number
): Array<{ id: string; model: string | null; since: number }> {
  const open = new Map<string, { model: string | null; since: number }>();
  const ordered = [...events].sort(
    (left, right) =>
      (parseTime(left.timestamp) ?? 0) - (parseTime(right.timestamp) ?? 0)
  );
  for (const event of ordered) {
    const payload = isRecord(event.payload) ? event.payload : {};
    const id = payload.gateway_request_id;
    if (typeof id !== 'string' || !id.trim()) continue;
    const key = id.trim();
    if (event.type === STARTED_EVENT_TYPE) {
      open.set(key, {
        model:
          typeof payload.model_alias === 'string' ? payload.model_alias : null,
        since: parseTime(event.timestamp) ?? now,
      });
    } else if (isModelGatewayCallType(event.type)) {
      open.delete(key);
    }
  }
  return Array.from(open.entries())
    .filter(([, value]) => now - value.since <= MAX_INFLIGHT_REQUEST_MS)
    .map(([id, value]) => ({ id, model: value.model, since: value.since }));
}

function isModelGatewayCallType(type: string): boolean {
  return type.startsWith('model_gateway') && type !== STARTED_EVENT_TYPE;
}

/**
 * Reduce observed facts into the single status line.
 *
 * Precedence, highest first: the session ended, nothing observed at all, an
 * unresolved approval, a tracked in-flight model request, a tool observed
 * running, a turn queued for the agent, idle.
 *
 * An unresolved approval outranks a running tool on purpose: it is the one
 * thing on this line the operator can act on, and the tool that is waiting is
 * usually the one that caused it.
 */
export function deriveLiveSessionState(
  input: LiveSessionInput
): LiveSessionState {
  const now = input.now ?? Date.now();
  const events = input.events ?? [];
  const activity = input.activity ?? [];
  const pending = (input.pendingApprovals ?? []).filter(
    (request) => (request.status || '').toLowerCase() === 'pending'
  );

  const observedTimes = [
    ...events.map((event) => parseTime(event.timestamp)),
    ...activity.map((row) => parseTime(row.timestamp)),
  ].filter((value): value is number => value !== null);
  const lastUpdateAt = observedTimes.length ? Math.max(...observedTimes) : null;

  const transport: TransportState =
    input.connected === true
      ? 'connected'
      : input.connected === false
        ? 'reconnecting'
        : 'unknown';

  const base = (
    status: LiveStatus,
    detail: string | null = null,
    since: number | null = lastUpdateAt
  ): LiveSessionState => ({
    status,
    label: LABELS[status],
    detail,
    since,
    transport,
    lastUpdateAt,
    pendingApprovalCount: pending.length,
  });

  if (input.ended) return base('ended');

  const hasObservation =
    events.length > 0 || activity.length > 0 || pending.length > 0;
  if (!hasObservation) return base('unavailable', null, null);

  if (pending.length) {
    const oldest = pending
      .map((request) =>
        parseTime((request as { requested_at?: string }).requested_at)
      )
      .filter((value): value is number => value !== null)
      .sort((left, right) => left - right)[0];
    return base(
      'waiting_for_approval',
      `${pending.length} pending`,
      oldest ?? lastUpdateAt
    );
  }

  const inFlight = inFlightGatewayRequests(events, now);
  if (inFlight.length) {
    const newest = inFlight[inFlight.length - 1];
    return base('model_processing', newest.model, newest.since);
  }

  const running = activity
    .filter(
      (row) =>
        (row.activity_type || '').toLowerCase() === 'tool_call' &&
        toolStateFromActivityStatus(row.status) === 'running'
    )
    .map((row) => parseTime(row.timestamp))
    .filter((value): value is number => value !== null)
    .sort((left, right) => right - left)[0];
  if (running !== undefined) return base('running_tool', null, running);

  const queued = activity
    .filter((row) => {
      const type = (row.activity_type || '').toLowerCase();
      const status = (row.status || '').toLowerCase();
      return (
        type === 'agent_control_message' &&
        (status === 'queued' || status === 'pending')
      );
    })
    .map((row) => parseTime(row.timestamp))
    .filter((value): value is number => value !== null)
    .sort((left, right) => right - left)[0];
  if (queued !== undefined) return base('waiting_for_agent', null, queued);

  return base('idle');
}

/** `12s`, `1.2s`, `4m 05s` — stable as `since` moves, so pass it the start. */
export function formatElapsed(
  since: number | null,
  now: number
): string | null {
  if (since === null) return null;
  const elapsed = Math.max(0, now - since);
  const seconds = elapsed / 1000;
  if (seconds < 1) return 'just now';
  if (seconds < 60)
    return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(Math.floor(seconds % 60)).padStart(2, '0')}s`;
}

/** `1.2s` for a tool card header; null when no duration was observed. */
export function formatDuration(durationMs: number | null): string | null {
  if (durationMs === null || !Number.isFinite(durationMs)) return null;
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  return `${(durationMs / 1000).toFixed(1)}s`;
}
