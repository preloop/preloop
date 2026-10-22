/**
 * Terminal-ordered turn model for the session Conversation and Talk views.
 *
 * Pure functions only: no Lit, no fetch. The input is the chronological item
 * list the chat view already builds (transcript messages, step groups,
 * dividers, captured tools, live tool calls and approvals). The output reads
 * like a CLI scrollback: an optional Session context disclosure, then one
 * turn per real prompt holding the agent prose and one row per tool call,
 * closed by a "Worked for" footer when the turn ended.
 *
 * Rules:
 * - One row per provider call id. A hook row or an approval-created card
 *   without a call id merges into the gateway row for the same command.
 * - A user-role message that only carries a tool result belongs to the tool
 *   row, never to "You".
 * - Developer, system, AGENTS.md and environment messages collapse into one
 *   Session context disclosure.
 * - Hook wrapper tags such as `<session>` are stripped from display.
 * - System and injected segment groups inside one turn fold into one group;
 *   agent steps keep their place in time.
 */

import type { ApprovalRequest, RuntimeSessionActivityItem } from '../types';
import type { LiveToolCall } from './live-session';
import type { SessionTool, ToolPhase } from './session-live';
import { sessionTimelineTime } from './session-live';
import type {
  TranscriptItem,
  TranscriptMessageItem,
  TranscriptStepGroupItem,
} from './transcript';
import { formatElapsedCompact } from './duration';

export type SessionRuntime = 'codex' | 'claude' | 'other';

/** Lines shown before the `+ N lines` disclosure, as in both CLIs. */
export const TOOL_PREVIEW_LINES = 3;

export type InputItem =
  | TranscriptItem
  | { type: 'tool'; key: string; tool: SessionTool }
  | { type: 'approval'; key: string; request: ApprovalRequest };

export interface DiffLine {
  kind: 'add' | 'del' | 'ctx' | 'hunk';
  text: string;
}

export interface ToolRow {
  type: 'row';
  key: string;
  callId: string | null;
  stableId: boolean;
  runtime: SessionRuntime;
  name: string;
  /** Leading glyph: `•` (Codex), `⏺` (Claude), `•` otherwise. */
  glyph: string;
  /** Codex verb (`Ran`, `Edited`, `Explored`, `Called`, `Failed (exit 1)`). */
  verb: string;
  /** The deciding argument: command, path, url or query. */
  target: string;
  /** Codex `└ Read a.ts` style detail line, when one applies. */
  detail: string | null;
  state: ToolPhase;
  exitCode: number | null;
  failed: boolean;
  argumentsText: string | null;
  resultText: string | null;
  previewLines: string[];
  hiddenLineCount: number;
  diff: DiffLine[] | null;
  durationMs: number | null;
  timestamp: string | null;
  redacted: boolean;
  truncated: boolean;
  serverName: string | null;
  repositoryArgs: Record<string, unknown> | null;
  /** Keys of the rows merged into this one (hook row, approval card). */
  mergedKeys: string[];
  /** Whether this row came from a native hook row without a call id. */
  native: boolean;
}

export interface TurnFooter {
  key: string;
  text: string;
  durationMs: number;
  endedAt: string;
  interrupted: boolean;
}

export type TurnItem =
  | TranscriptMessageItem
  | TranscriptStepGroupItem
  | ToolRow
  | Exclude<
      InputItem,
      TranscriptMessageItem | TranscriptStepGroupItem | { type: 'tool' }
    >;

export interface SessionTurn {
  key: string;
  prompt: TranscriptMessageItem | null;
  items: TurnItem[];
  footer: TurnFooter | null;
}

export interface SessionTurnsResult {
  context: TranscriptMessageItem[];
  turns: SessionTurn[];
  toolRowCount: number;
}

export interface BuildTurnsOptions {
  activity?: RuntimeSessionActivityItem[];
  runtime?: SessionRuntime;
}

const SESSION_TAG = /<\/?session(?:\s[^>]*)?>/gi;

/** Remove hook wrapper tags such as `<session>` from displayed text. */
export function stripWrapperTags(text: string): string {
  return text.replace(SESSION_TAG, '').trim();
}

const CONTEXT_PATTERNS: RegExp[] = [
  /^you are codex\b/i,
  /^#\s*agents\.md instructions\b/i,
  /^<(environment_context|user_instructions|permissions instructions|developer_instructions|system)\b/i,
  /^<instructions>/i,
];

/** Developer, system, AGENTS.md or environment text a person did not type. */
export function isSessionContextMessage(item: TranscriptMessageItem): boolean {
  const role = (item.role || '').toLowerCase();
  if (role === 'developer' || role === 'system') return true;
  if (item.kind !== 'user_prompt') return false;
  const text = stripWrapperTags(item.text || '');
  return CONTEXT_PATTERNS.some((pattern) => pattern.test(text));
}

const NO_OUTPUT_RESULT = /^\(\w[\w .-]* completed with no output\)$/i;

/** Infer the runtime from hook metadata on the activity rows. */
export function inferRuntime(
  activity: RuntimeSessionActivityItem[]
): SessionRuntime {
  for (const item of activity) {
    const source = item.metadata?.source;
    if (typeof source !== 'string') continue;
    const value = source.toLowerCase();
    if (value.includes('codex')) return 'codex';
    if (value.includes('claude')) return 'claude';
  }
  return 'other';
}

function parseArgs(text: string | null | undefined): Record<string, unknown> {
  if (!text) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
      return parsed as Record<string, unknown>;
  } catch {
    /* Plain text arguments stay as the target below. */
  }
  return {};
}

function commandOf(args: Record<string, unknown>): string | null {
  const value = args.command ?? args.cmd;
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    const parts = value.filter((v): v is string => typeof v === 'string');
    // Codex wraps commands as ["bash", "-lc", "<script>"].
    if (parts.length >= 3 && /^-l?c$/.test(parts[1]))
      return parts.slice(2).join(' ');
    return parts.join(' ');
  }
  return null;
}

function targetOf(args: Record<string, unknown>, raw: string): string {
  const command = commandOf(args);
  if (command) return command.replace(/\s+/g, ' ').trim();
  for (const key of ['file_path', 'path', 'url', 'pattern', 'query']) {
    if (typeof args[key] === 'string') return String(args[key]);
  }
  if (Object.keys(args).length === 0)
    return raw.replace(/\s+/g, ' ').trim().slice(0, 180);
  return '';
}

const EDIT_TOOLS = new Set([
  'edit',
  'multiedit',
  'write',
  'apply_patch',
  'str_replace_based_edit_tool',
]);
const EXEC_TOOLS = new Set([
  'bash',
  'shell',
  'exec_command',
  'local_shell',
  'terminal',
  'container.exec',
  'unified_exec',
]);

/** Exit code from structured metadata or the captured result text. */
export function parseExitCode(result: string | null): number | null {
  if (!result) return null;
  const match =
    /(?:exit(?:ed)?(?: with)? code:?\s*|"exit_code"\s*:\s*)(-?\d+)/i.exec(
      result
    );
  return match ? Number(match[1]) : null;
}

/** Diff lines for Edit, Write and apply_patch arguments. */
export function toolDiff(
  name: string,
  argsText: string | null
): DiffLine[] | null {
  const lower = name.toLowerCase();
  if (!EDIT_TOOLS.has(lower)) return null;
  const args = parseArgs(argsText);
  const lines = (value: unknown): string[] =>
    typeof value === 'string' ? value.replace(/\n$/, '').split('\n') : [];
  if (
    typeof args.old_string === 'string' ||
    typeof args.new_string === 'string'
  ) {
    return [
      ...lines(args.old_string).map((text) => ({ kind: 'del' as const, text })),
      ...lines(args.new_string).map((text) => ({ kind: 'add' as const, text })),
    ];
  }
  if (Array.isArray(args.edits)) {
    const out: DiffLine[] = [];
    for (const edit of args.edits) {
      const e = (edit ?? {}) as Record<string, unknown>;
      out.push({ kind: 'hunk', text: '@@' });
      out.push(
        ...lines(e.old_string).map((text) => ({ kind: 'del' as const, text }))
      );
      out.push(
        ...lines(e.new_string).map((text) => ({ kind: 'add' as const, text }))
      );
    }
    return out;
  }
  if (typeof args.content === 'string') {
    return lines(args.content).map((text) => ({ kind: 'add' as const, text }));
  }
  const patch =
    typeof args.input === 'string'
      ? args.input
      : typeof args.patch === 'string'
        ? args.patch
        : argsText && argsText.includes('*** Begin Patch')
          ? argsText
          : null;
  if (!patch) return null;
  const out: DiffLine[] = [];
  for (const line of patch.split('\n')) {
    if (/^\*\*\* (Begin|End) Patch/.test(line)) continue;
    if (line.startsWith('*** ') || line.startsWith('@@'))
      out.push({ kind: 'hunk', text: line });
    else if (line.startsWith('+'))
      out.push({ kind: 'add', text: line.slice(1) });
    else if (line.startsWith('-'))
      out.push({ kind: 'del', text: line.slice(1) });
    else if (line.startsWith(' '))
      out.push({ kind: 'ctx', text: line.slice(1) });
  }
  return out.length ? out : null;
}

function editTarget(
  args: Record<string, unknown>,
  argsText: string | null
): string {
  for (const key of ['file_path', 'path'])
    if (typeof args[key] === 'string') return String(args[key]);
  const patch = typeof args.input === 'string' ? args.input : argsText || '';
  const match = /\*\*\* (?:Update|Add|Delete) File: (.+)/.exec(patch);
  return match ? match[1].trim() : '';
}

const READ_COMMANDS = /^(cat|sed|head|tail|nl|less)\b/;
const SEARCH_COMMANDS = /^(rg|grep|find|fd|ls)\b/;

function exploredDetail(command: string): string | null {
  const first = command.trim();
  if (READ_COMMANDS.test(first)) {
    const files = first
      .split(/\s+/)
      .slice(1)
      .filter(
        (part) =>
          !part.startsWith('-') && /[./]/.test(part) && !/^\d+,\d+p$/.test(part)
      );
    return files.length ? `Read ${files.join(', ')}` : null;
  }
  if (SEARCH_COMMANDS.test(first))
    return first.startsWith('ls')
      ? `List ${first.slice(2).trim() || '.'}`
      : `Search ${first}`;
  return null;
}

interface RowSeed {
  key: string;
  callId: string | null;
  stableId: boolean;
  name: string;
  state: ToolPhase;
  argumentsText: string | null;
  resultText: string | null;
  durationMs: number | null;
  timestamp: string | null;
  redacted: boolean;
  truncated: boolean;
  serverName: string | null;
  repositoryArgs: Record<string, unknown> | null;
  summary: string | null;
  native: boolean;
}

/** Finish a row: runtime verb, glyph, preview lines, diff, failure. */
export function finishRow(seed: RowSeed, runtime: SessionRuntime): ToolRow {
  const args = parseArgs(seed.argumentsText);
  const lower = seed.name.toLowerCase();
  const isEdit = EDIT_TOOLS.has(lower);
  const metaExit = seed.repositoryArgs?.exit_code;
  const exitCode =
    typeof metaExit === 'number' ? metaExit : parseExitCode(seed.resultText);
  const failed =
    seed.state === 'failed' || (exitCode !== null && exitCode !== 0);
  let target = isEdit
    ? editTarget(args, seed.argumentsText)
    : targetOf(args, seed.argumentsText || '');
  if (!target && seed.summary)
    target = seed.summary.replace(/\s+/g, ' ').trim();
  let verb = 'Called';
  let detail: string | null = null;
  if (isEdit) verb = 'Edited';
  else if (EXEC_TOOLS.has(lower) || commandOf(args)) {
    detail = runtime === 'codex' ? exploredDetail(target) : null;
    verb = detail ? 'Explored' : 'Ran';
  }
  if (failed) verb = exitCode !== null ? `Failed (exit ${exitCode})` : 'Failed';
  const result = (seed.resultText ?? '').replace(/\s+$/, '');
  const allLines = result ? result.split('\n') : [];
  return {
    type: 'row',
    key: seed.key,
    callId: seed.callId,
    stableId: seed.stableId,
    runtime,
    name: seed.name,
    glyph: runtime === 'claude' ? '⏺' : '•',
    verb,
    target,
    detail,
    state: failed ? 'failed' : seed.state,
    exitCode,
    failed,
    argumentsText: seed.argumentsText,
    resultText: seed.resultText,
    previewLines: allLines.slice(0, TOOL_PREVIEW_LINES),
    hiddenLineCount: Math.max(0, allLines.length - TOOL_PREVIEW_LINES),
    diff: toolDiff(seed.name, seed.argumentsText),
    durationMs: seed.durationMs && seed.durationMs > 0 ? seed.durationMs : null,
    timestamp: seed.timestamp,
    redacted: seed.redacted,
    truncated: seed.truncated,
    serverName: seed.serverName,
    repositoryArgs: seed.repositoryArgs,
    mergedKeys: [],
    native: seed.native,
  };
}

function seedFromCall(call: LiveToolCall, key: string): RowSeed {
  return {
    key,
    callId: call.id,
    stableId: call.stableId,
    name: call.name || 'tool',
    state: call.state,
    argumentsText: call.argumentsText,
    resultText: call.resultText,
    durationMs: call.durationMs,
    timestamp: call.timestamp,
    redacted: call.redacted,
    truncated: call.truncated,
    serverName: call.serverName,
    repositoryArgs: call.repositoryArgs ?? null,
    summary: call.summary || null,
    native: !call.stableId,
  };
}

function seedFromTool(tool: SessionTool, key: string): RowSeed {
  return {
    key,
    callId: tool.callId ?? null,
    stableId: Boolean(tool.callId),
    name: tool.name,
    state: tool.phase,
    argumentsText: tool.arguments ?? null,
    resultText: tool.result ?? null,
    durationMs: tool.durationMs ?? null,
    timestamp: tool.timestamp,
    redacted: tool.redacted,
    truncated: tool.truncated,
    serverName: null,
    repositoryArgs: tool.repositoryArgs ?? null,
    summary: null,
    native: tool.id.startsWith('native:'),
  };
}

const APPROVAL_CARD = /^created from approval\b/i;
/** How far apart a hook row and its gateway call may be and still merge. */
const MERGE_WINDOW_MS = 10 * 60 * 1000;

function normalizeCommand(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

/**
 * Merge a native hook row (no call id) into the gateway row for the same
 * call. Matching is by command text, or for an approval-created placeholder
 * by proximity; an unmatched native row stays its own row.
 */
function mergeNativeRows(rows: ToolRow[]): ToolRow[] {
  const out: ToolRow[] = [];
  const natives: ToolRow[] = [];
  for (const row of rows) (row.native && !row.callId ? natives : out).push(row);
  for (const native of natives) {
    const time = sessionTimelineTime(native.timestamp);
    const target = normalizeCommand(native.target);
    const placeholder = APPROVAL_CARD.test(native.target);
    let best: ToolRow | null = null;
    let bestGap = Number.POSITIVE_INFINITY;
    for (const row of out) {
      if (row.native) continue;
      const gap = Math.abs(sessionTimelineTime(row.timestamp) - time);
      if (gap > MERGE_WINDOW_MS) continue;
      const rowTarget = normalizeCommand(row.target);
      const matches =
        placeholder ||
        (target !== '' &&
          rowTarget !== '' &&
          (rowTarget === target ||
            rowTarget.includes(target) ||
            target.includes(rowTarget)));
      if (matches && gap < bestGap) {
        best = row;
        bestGap = gap;
      }
    }
    if (!best) {
      if (!placeholder) out.push(native);
      continue;
    }
    best.mergedKeys.push(native.key, ...native.mergedKeys);
    best.durationMs ??= native.durationMs;
    best.repositoryArgs ??= native.repositoryArgs;
    if (native.failed && !best.failed) {
      best.failed = true;
      best.state = 'failed';
      best.verb =
        best.exitCode !== null ? `Failed (exit ${best.exitCode})` : 'Failed';
    } else if (best.state === 'requested' || best.state === 'unknown') {
      best.state = native.state === 'unknown' ? best.state : native.state;
    }
  }
  return out;
}

function sortKey(item: InputItem | TurnItem): number {
  if (item.type === 'row') return sessionTimelineTime(item.timestamp);
  if (item.type === 'tool') {
    return sessionTimelineTime(
      'tool' in item ? item.tool.timestamp : item.call.timestamp
    );
  }
  if (item.type === 'approval')
    return sessionTimelineTime(item.request.requested_at);
  if (item.type === 'steps')
    return sessionTimelineTime(item.steps[0]?.timestamp);
  return sessionTimelineTime(item.timestamp);
}

/** Codex: `Worked for 3m 24s • 8:48 PM`; Claude: `Worked for 3m 24s · done 8:48 PM`. */
export function footerText(
  runtime: SessionRuntime,
  durationMs: number,
  endedAt: string,
  interrupted: boolean
): string {
  if (interrupted) {
    return runtime === 'codex' ? 'Conversation interrupted' : 'Interrupted';
  }
  const clock = new Date(sessionTimelineTime(endedAt)).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  });
  const elapsed = formatElapsedCompact(durationMs);
  return runtime === 'claude'
    ? `Worked for ${elapsed} · done ${clock}`
    : `Worked for ${elapsed} • ${clock}`;
}

/** A step group holding only system or injected segments (CLI noise). */
function isSegmentGroup(item: TranscriptStepGroupItem): boolean {
  return item.steps.every(
    (step) => step.kind === 'system' || step.kind === 'injected'
  );
}

/** Build the turn model from the chat view's chronological items. */
export function buildSessionTurns(
  input: InputItem[],
  options: BuildTurnsOptions = {}
): SessionTurnsResult {
  const activity = options.activity ?? [];
  const runtime = options.runtime ?? inferRuntime(activity);
  const context: TranscriptMessageItem[] = [];

  // 1. Tool rows first, so tool-result-only messages can find their row.
  const rawRows: ToolRow[] = [];
  const rest: InputItem[] = [];
  for (const item of input) {
    if (item.type === 'tool') {
      rawRows.push(
        finishRow(
          'tool' in item
            ? seedFromTool(item.tool, item.key)
            : seedFromCall(item.call, item.key),
          runtime
        )
      );
    } else rest.push(item);
  }
  const rows = mergeNativeRows(rawRows);
  const resultTexts = new Map<string, ToolRow>();
  for (const row of rows) {
    if (row.resultText) resultTexts.set(row.resultText.trim(), row);
  }

  // 2. Messages: strip wrappers, pull out context and tool results.
  const items: Array<InputItem | ToolRow> = [...rows];
  for (const item of rest) {
    if (item.type !== 'message') {
      items.push(item);
      continue;
    }
    const text = stripWrapperTags(item.text || '');
    const message: TranscriptMessageItem =
      text === item.text ? item : { ...item, text };
    if (isSessionContextMessage(message)) {
      context.push(message);
      continue;
    }
    if (message.kind === 'user_prompt') {
      const owner = resultTexts.get(text);
      if (owner) continue;
      if (NO_OUTPUT_RESULT.test(text)) {
        const time = sessionTimelineTime(message.timestamp);
        const previous = rows
          .filter(
            (row) =>
              !row.resultText && sessionTimelineTime(row.timestamp) <= time
          )
          .pop();
        if (previous) {
          previous.resultText = '(No output)';
          previous.previewLines = ['(No output)'];
        }
        continue;
      }
    }
    items.push(message);
  }
  items.sort((a, b) => sortKey(a) - sortKey(b));

  // 3. Split into turns at each real prompt; fold step groups per turn.
  const turns: SessionTurn[] = [];
  let current: SessionTurn = {
    key: 'turn:start',
    prompt: null,
    items: [],
    footer: null,
  };
  let stepGroup: TranscriptStepGroupItem | null = null;
  const pushTurn = (): void => {
    if (current.prompt || current.items.length) turns.push(current);
  };
  for (const item of items) {
    if (item.type === 'message' && item.kind === 'user_prompt') {
      pushTurn();
      current = {
        key: `turn:${item.key}`,
        prompt: item,
        items: [],
        footer: null,
      };
      stepGroup = null;
      continue;
    }
    if (item.type === 'steps' && isSegmentGroup(item)) {
      if (stepGroup) {
        stepGroup.steps.push(...item.steps);
        continue;
      }
      stepGroup = { ...item, steps: [...item.steps] };
      current.items.push(stepGroup);
      continue;
    }
    current.items.push(item as TurnItem);
  }
  pushTurn();

  // 4. Footers from turn_end activity, measured from the turn start.
  const turnEnds = activity
    .filter(
      (row) =>
        row.activity_type === 'turn_end' && row.metadata?.subagent !== true
    )
    .sort(
      (a, b) =>
        sessionTimelineTime(a.timestamp) - sessionTimelineTime(b.timestamp)
    );
  const starts = turns.map((turn) =>
    turn.prompt
      ? sessionTimelineTime(turn.prompt.timestamp)
      : turn.items.length
        ? sortKey(turn.items[0])
        : 0
  );
  for (const end of turnEnds) {
    const at = sessionTimelineTime(end.timestamp);
    let index = -1;
    for (let i = 0; i < turns.length; i += 1) if (starts[i] <= at) index = i;
    if (index < 0 || turns[index].footer) continue;
    const durationMs = Math.max(0, at - starts[index]);
    const interrupted = end.metadata?.interrupted === true;
    turns[index].footer = {
      key: `footer:${end.activity_id ?? end.timestamp}`,
      text: footerText(runtime, durationMs, end.timestamp, interrupted),
      durationMs,
      endedAt: end.timestamp,
      interrupted,
    };
  }

  return { context, turns, toolRowCount: rows.length };
}

/** Count tool calls the way the transcript shows them: one per row. */
export function countToolRows(
  input: InputItem[],
  options: BuildTurnsOptions = {}
): number {
  return buildSessionTurns(input, options).toolRowCount;
}
