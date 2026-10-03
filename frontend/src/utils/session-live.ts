import { parseUTCDate } from './date';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';

export type ToolPhase =
  'requested' | 'running' | 'completed' | 'failed' | 'unknown';
export interface SessionTool {
  id: string;
  callId?: string;
  name: string;
  timestamp: string | null;
  phase: ToolPhase;
  arguments?: string;
  result?: string;
  redacted: boolean;
  truncated: boolean;
  durationMs?: number;
  repositoryArgs?: Record<string, unknown>;
}
const record = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
const text = (v: unknown): string =>
  typeof v === 'string' ? v : JSON.stringify(v ?? null);

/** Only captured structured evidence creates tools. Text equality is never identity. */
export function sessionTools(
  events: FlowGatewayEvent[],
  activity: RuntimeSessionActivityItem[] = []
): SessionTool[] {
  const tools = new Map<string, SessionTool>();
  for (const event of [...events].sort(
    (a, b) => Date.parse(a.timestamp || '') - Date.parse(b.timestamp || '')
  )) {
    const entries: Record<string, unknown>[] = [];
    if (Array.isArray(event.payload?.tools)) {
      for (const item of event.payload.tools) {
        const r = record(item);
        if (r) entries.push(r);
      }
    } else if (
      event.payload?.capture_policy?.content_capture_enabled !== false
    ) {
      // Legacy bounded raw bodies are already capture-policy sanitized server side.
      const scan = (items: unknown): void => {
        if (!Array.isArray(items)) return;
        for (const item of items.slice(-256)) {
          const r = record(item);
          if (!r) continue;
          const kind =
            r.type === 'function_call' || r.type === 'tool_use'
              ? 'call'
              : r.type === 'function_call_output' ||
                  r.type === 'tool_result' ||
                  r.role === 'tool'
                ? 'result'
                : null;
          if (kind)
            entries.push({
              kind,
              call_id: r.call_id || r.tool_call_id || r.tool_use_id || r.id,
              name: r.name,
              text: text(
                kind === 'call'
                  ? (r.arguments ?? r.input)
                  : (r.output ?? r.content)
              ),
              is_error: r.is_error,
            });
          if (Array.isArray(r.tool_calls))
            for (const call of r.tool_calls) {
              const c = record(call),
                f = record(c?.function);
              if (c && f)
                entries.push({
                  kind: 'call',
                  call_id: c.id,
                  name: f.name,
                  text: text(f.arguments),
                });
            }
          scan(r.content);
        }
      };
      const req = record(event.payload?.request),
        res = record(event.payload?.response);
      scan(req?.messages);
      scan(req?.input);
      scan(res?.output);
      scan(res?.content);
      if (Array.isArray(res?.choices))
        for (const choice of res.choices) scan([record(choice)?.message]);
    }
    entries.forEach((entry, index) => {
      const callId =
        typeof entry.call_id === 'string' &&
        entry.call_id &&
        entry.call_id.length <= 256
          ? entry.call_id
          : undefined;
      const id = callId ? `tool:${callId}` : `tool:${event.id}:${index}`;
      const previous = tools.get(id);
      const tool: SessionTool = previous ?? {
        id,
        callId,
        name: 'Unknown tool',
        timestamp: event.timestamp,
        phase: 'unknown',
        redacted: false,
        truncated: false,
      };
      if (typeof entry.name === 'string' && entry.name) tool.name = entry.name;
      const content =
        typeof entry.text === 'string' ? entry.text.slice(0, 32768) : undefined;
      if (entry.kind === 'call') {
        tool.arguments = content;
        if (!previous?.result && tool.phase === 'unknown')
          tool.phase = 'requested';
      }
      if (entry.kind === 'result') {
        tool.result = content;
        tool.phase = entry.is_error === true ? 'failed' : 'completed';
      }
      tool.redacted ||= entry.redacted === true;
      tool.truncated ||=
        entry.truncated === true ||
        (typeof entry.text === 'string' && entry.text.length > 32768);
      tools.set(id, tool);
    });
  }
  activity.forEach((item, index) => {
    if (item.activity_type !== 'tool_call') return;
    const m = record(item.metadata),
      callId = typeof m?.tool_call_id === 'string' ? m.tool_call_id : undefined;
    const id = callId
      ? `tool:${callId}`
      : `native:${item.activity_id || `${item.timestamp}:${index}`}`;
    const tool = tools.get(id) ?? {
      id,
      callId,
      name: item.tool_name || item.title || 'Unknown tool',
      timestamp: item.timestamp,
      phase: 'unknown' as ToolPhase,
      redacted: false,
      truncated: false,
    };
    const status = item.status?.toLowerCase();
    if (
      (status === 'running' || status === 'started') &&
      tool.phase !== 'completed' &&
      tool.phase !== 'failed'
    )
      tool.phase = 'running';
    else if (status === 'completed' || status === 'success')
      tool.phase = 'completed';
    else if (status === 'failed' || status === 'error') tool.phase = 'failed';
    tool.arguments ??= item.summary || undefined;
    tool.repositoryArgs = m ?? undefined;
    if (typeof m?.duration_ms === 'number') tool.durationMs = m.duration_ms;
    tools.set(id, tool);
  });
  return [...tools.values()];
}

export interface LiveWork {
  label: string;
  startedAt?: number;
  model?: string;
  count?: number;
}
/** Concurrent completion is correlated only by the producer's stable request id. */
export function sessionWork(
  events: FlowGatewayEvent[],
  ended: boolean,
  now = Date.now()
): LiveWork {
  if (ended) return { label: 'Ended' };
  const requests = new Map<string, FlowGatewayEvent>();
  const finished = new Set<string>();
  for (const event of events) {
    const id = event.payload?.request_id;
    if (typeof id !== 'string' || !id) continue;
    if (event.type === 'model_gateway_request_started') requests.set(id, event);
    else if (event.type.startsWith('model_gateway')) finished.add(id);
  }
  const active = [...requests].filter(
    ([id, event]) =>
      !finished.has(id) && now - Date.parse(event.timestamp || '') < 120000
  );
  if (active.length)
    return {
      label: 'Model processing',
      count: active.length,
      model: String(active[0][1].payload?.model_alias || ''),
      startedAt: Math.min(
        ...active.map(([, event]) => Date.parse(event.timestamp || ''))
      ),
    };
  if ([...requests].some(([id]) => !finished.has(id)))
    return { label: 'Activity unavailable' };
  return { label: events.length ? 'Waiting for agent' : 'Idle' };
}

export function readablePayload(value: string | undefined): string {
  if (value === undefined) return 'No content captured.';
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

/** Short, readable captured command/path/url; never interpreted as executable content. */
export function toolSummary(tool: SessionTool): string {
  const value = tool.arguments || tool.result || '';
  try {
    const parsed = JSON.parse(value);
    const args = record(parsed);
    if (args) {
      for (const key of ['command', 'cmd', 'path', 'file_path', 'url', 'query'])
        if (typeof args[key] === 'string')
          return String(args[key]).replace(/\s+/g, ' ').slice(0, 180);
      return Object.entries(args)
        .slice(0, 3)
        .map(
          ([key, item]) =>
            `${key}: ${typeof item === 'string' ? item : JSON.stringify(item)}`
        )
        .join(' · ')
        .slice(0, 180);
    }
  } catch {
    /* Malformed or plain multiline captures remain readable text. */
  }
  return value.replace(/\s+/g, ' ').slice(0, 180);
}

export function sessionTimelineTime(
  timestamp: string | null | undefined
): number {
  return timestamp ? parseUTCDate(timestamp).getTime() || 0 : 0;
}
