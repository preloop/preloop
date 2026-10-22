/**
 * One agent state and one status line, in the runtime's own words.
 *
 * Every surface that says what an agent is doing (the Talk header and live
 * turn, the session page, the Agents list, the agent detail header, the
 * sessions list and the Fleet Map) reads it from here, so the same session
 * never says "Active now" in one place and "Working" in another.
 *
 * Codex sessions use Codex's own status strings verbatim. Claude Code
 * sessions use Claude Code's status line format with a word list of our own.
 * Every other runtime reads `Working`.
 */

import { parseUTCDate } from './date';

export type AgentRuntime = 'codex' | 'claude' | 'other';

/** Which status vocabulary a session's source type speaks. */
export function runtimeForSource(
  sourceType: string | null | undefined
): AgentRuntime {
  const source = (sourceType || '').toLowerCase();
  if (source.startsWith('codex')) return 'codex';
  if (source.startsWith('claude')) return 'claude';
  return 'other';
}

/*
 * Status strings from openai/codex codex-rs/tui (Apache-2.0), tag
 * rust-v0.163.0: status_indicator_widget.rs (default header, elapsed
 * format, interrupt hint), chatwidget/compaction.rs,
 * chatwidget/safety_buffering.rs, chatwidget/command_lifecycle.rs,
 * chatwidget/mcp_startup.rs, exec_cell/render.rs and the turn footer.
 * Copyright 2025 OpenAI. Licensed under the Apache License, Version 2.0.
 */
export const CODEX_STATUS = {
  working: 'Working',
  compacting: 'Compacting context',
  safetyBuffering: 'Giving this request a little extra thought',
  backgroundTerminal: 'Waiting for background terminal',
  bootingMcpServer: 'Booting MCP server:',
  startingMcpServers: 'Starting MCP servers',
  interruptHint: 'esc to interrupt',
  interrupted: 'Conversation interrupted',
  workedFor: 'Worked for',
  running: 'Running',
  ran: 'Ran',
} as const;

/** Codex's activity indicator without truecolor: a 600 ms `•`/`◦` blink. */
export const CODEX_GLYPH_FRAMES = ['•', '◦'] as const;
export const CODEX_GLYPH_FRAME_MS = 600;

/**
 * Generic Unicode dingbats played forward then back, as Claude Code's
 * spinner does. The static glyph under reduced motion is `✻`.
 */
export const CLAUDE_GLYPH_FRAMES = ['·', '✢', '✳', '✶', '✻', '✽'] as const;
export const CLAUDE_GLYPH_FRAME_MS = 120;

/**
 * Our own present participles in the same playful register as Claude Code's
 * status words. Not copied from it: one exported constant, so a different
 * list is a one-line change.
 */
export const PRELOOP_CLAUDE_VERBS = [
  'Thinking',
  'Working',
  'Processing',
  'Mulling',
  'Musing',
  'Tinkering',
  'Puzzling',
  'Sketching',
  'Drafting',
  'Sifting',
  'Sorting',
  'Weaving',
  'Untangling',
  'Assembling',
  'Calibrating',
  'Composing',
  'Distilling',
  'Exploring',
  'Figuring',
  'Forging',
  'Gathering',
  'Hatching',
  'Honing',
  'Iterating',
  'Juggling',
  'Kneading',
  'Mapping',
  'Mending',
  'Noodling',
  'Orchestrating',
  'Piecing',
  'Plotting',
  'Polishing',
  'Reckoning',
  'Refining',
  'Rummaging',
  'Scheming',
  'Shaping',
  'Simmering',
  'Smoothing',
  'Stitching',
  'Surveying',
  'Tallying',
  'Threading',
  'Tidying',
  'Tracing',
  'Tuning',
  'Unpacking',
  'Unravelling',
  'Whittling',
  'Wrangling',
  'Chiselling',
  'Charting',
  'Combing',
  'Crafting',
  'Deciphering',
  'Fathoming',
  'Kindling',
  'Sculpting',
  'Steeping',
] as const;

/** Past tense for the turn footer, `<Word> for 3m 24s`. */
export const PRELOOP_CLAUDE_DONE_VERBS = [
  'Worked',
  'Cooked',
  'Brewed',
  'Baked',
  'Crunched',
] as const;

/** Claude Code's generic interrupted marker. */
export const CLAUDE_INTERRUPTED =
  'Interrupted · What should Claude do instead?';

/** `Ns`, then `Nm SSs`, then `Nh MMm SSs` (Codex `fmt_elapsed_compact`). */
export function fmtElapsedCompact(seconds: number): string {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  if (total < 60) return `${total}s`;
  if (total < 3600) {
    const minutes = Math.floor(total / 60);
    return `${minutes}m ${String(total % 60).padStart(2, '0')}s`;
  }
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${hours}h ${String(minutes).padStart(2, '0')}m ${String(total % 60).padStart(2, '0')}s`;
}

/** `512`, `1.2k`, `12k`, `1.4M`: the CLI token counter. */
export function fmtTokens(count: number): string {
  const n = Math.max(0, Math.round(Number.isFinite(count) ? count : 0));
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/**
 * A word that is stable for one turn: seeded by the turn id, so re-renders
 * and new model requests in the same turn never change it.
 */
export function pickStable<T>(seed: string, list: readonly T[]): T {
  let hash = 2166136261;
  for (let i = 0; i < seed.length; i += 1) {
    hash ^= seed.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return list[(hash >>> 0) % list.length];
}

export type AgentPhase =
  | 'thinking'
  | 'tool_running'
  | 'hook_running'
  | 'waiting_for_approval'
  | 'queued'
  | 'catching_up'
  | 'your_turn'
  | 'interrupted'
  | 'idle'
  | 'ended'
  | 'unknown';

export type AgentChipVariant = 'success' | 'warning' | 'neutral';

export interface AgentStateChip {
  label: 'Working' | 'Needs approval' | 'Your turn' | 'Idle' | 'Ended';
  variant: AgentChipVariant;
}

export interface AgentStatusInput {
  runtime: AgentRuntime;
  /** Seeds the verb; the turn start time works when there is no id. */
  turnId?: string | null;
  /** Epoch ms the turn started (the prompt), never a request start. */
  turnStartedAt?: number | null;
  /** Epoch ms the turn ended; null while it runs. */
  turnEndedAt?: number | null;
  interrupted?: boolean;
  ended?: boolean;
  /** Server-derived `turn_state`, when known. */
  turnState?: string | null;
  /** True while a model request of this turn is open. */
  modelBusy?: boolean;
  /** Name of a tool observed running. */
  runningTool?: string | null;
  /** A PreToolUse hook (policy check) is in flight. */
  hookRunning?: boolean;
  pendingApprovals?: number;
  /** Epoch ms the oldest pending approval was raised. */
  pendingSince?: number | null;
  /** A message is queued for the agent and nothing has picked it up. */
  queued?: boolean;
  /** Output tokens summed over the turn's gateway rows. */
  outputTokens?: number;
  /** Codex: the first bold title of the reasoning summary. */
  reasoningTitle?: string | null;
  /** The live channel is down; only whole events can arrive. */
  catchingUp?: boolean;
  lastEventAt?: number | null;
  /** Add the interrupt hint (only where Preloop can interrupt). */
  canInterrupt?: boolean;
  now: number;
}

export interface AgentStatus {
  phase: AgentPhase;
  chip: AgentStateChip;
  /** The live line, or null when there is no live turn. */
  line: string | null;
  /** Ambient glyph before the verb; null when nothing is running. */
  glyph: 'codex' | 'claude' | null;
  /** `warning` only while a person must act. */
  tone: 'neutral' | 'warning';
  /** Turn footer, e.g. `Worked for 3m 24s • 8:48 PM`. */
  footer: string | null;
  /** Interrupted marker in the runtime's words. */
  marker: string | null;
}

const CHIPS: Record<string, AgentStateChip> = {
  working: { label: 'Working', variant: 'success' },
  waiting_for_approval: { label: 'Needs approval', variant: 'warning' },
  your_turn: { label: 'Your turn', variant: 'neutral' },
  idle: { label: 'Idle', variant: 'neutral' },
  ended: { label: 'Ended', variant: 'neutral' },
};

/** `8:48 PM` in the viewer's time zone. */
export function fmtClock(at: number): string {
  return new Date(at).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  });
}

function interruptedMarker(runtime: AgentRuntime): string {
  if (runtime === 'codex') return CODEX_STATUS.interrupted;
  if (runtime === 'claude') return CLAUDE_INTERRUPTED;
  return 'Interrupted';
}

function footerFor(
  runtime: AgentRuntime,
  seed: string,
  startedAt: number | null,
  endedAt: number
): string | null {
  if (startedAt === null) return null;
  const duration = fmtElapsedCompact((endedAt - startedAt) / 1000);
  const clock = fmtClock(endedAt);
  if (runtime === 'codex')
    return `${CODEX_STATUS.workedFor} ${duration} • ${clock}`;
  if (runtime === 'claude')
    return `${pickStable(seed, PRELOOP_CLAUDE_DONE_VERBS)} for ${duration} · done ${clock}`;
  return `Worked for ${duration} • ${clock}`;
}

function thinkingLabel(seconds: number): string {
  if (seconds < 30) return 'thinking';
  if (seconds < 60) return 'thinking more';
  return 'still thinking';
}

/**
 * The phase table: one function decides the chip, the live line, the glyph
 * and the footer for every runtime.
 *
 * Precedence: ended, a pending approval, a hook, a running tool, an open
 * model request, a queued message, then the turn's end (your turn,
 * interrupted) and idle.
 */
export function deriveAgentStatus(input: AgentStatusInput): AgentStatus {
  const { runtime, now } = input;
  const startedAt = input.turnStartedAt ?? null;
  const seed =
    input.turnId || (startedAt !== null ? String(startedAt) : 'turn');
  const elapsed = fmtElapsedCompact(
    startedAt !== null ? (now - startedAt) / 1000 : 0
  );
  const tokens = input.outputTokens ?? 0;
  const result = (
    phase: AgentPhase,
    chip: keyof typeof CHIPS,
    extra: Partial<AgentStatus> = {}
  ): AgentStatus => ({
    phase,
    chip: CHIPS[chip],
    line: null,
    glyph: null,
    tone: 'neutral',
    footer: null,
    marker: null,
    ...extra,
  });

  if (input.ended || input.turnState === 'ended')
    return result('ended', 'ended');

  if ((input.pendingApprovals ?? 0) > 0) {
    const since = input.pendingSince ?? startedAt ?? now;
    return result('waiting_for_approval', 'waiting_for_approval', {
      line: `Waiting for approval (${fmtElapsedCompact((now - since) / 1000)})`,
      tone: 'warning',
    });
  }

  const turnOpen =
    input.turnEndedAt === null ||
    input.turnEndedAt === undefined ||
    (startedAt !== null && input.turnEndedAt < startedAt);
  const busy =
    Boolean(input.hookRunning) ||
    Boolean(input.runningTool) ||
    Boolean(input.modelBusy) ||
    (input.turnState === 'working' && turnOpen && startedAt !== null);

  if (busy) {
    if (input.catchingUp) {
      const ago =
        input.lastEventAt != null
          ? ` · last event ${fmtElapsedCompact((now - input.lastEventAt) / 1000)} ago`
          : '';
      return result('catching_up', 'working', {
        line: `Catching up${ago}`,
        glyph: runtime === 'claude' ? 'claude' : 'codex',
      });
    }
    const phase: AgentPhase = input.hookRunning
      ? 'hook_running'
      : input.runningTool
        ? 'tool_running'
        : 'thinking';
    if (runtime === 'claude') {
      const verb = pickStable(seed, PRELOOP_CLAUDE_VERBS);
      const parts = [elapsed];
      if (phase !== 'hook_running' && tokens > 0)
        parts.push(`↓ ${fmtTokens(tokens)} tokens`);
      if (phase === 'hook_running') parts.push('running PreToolUse hook');
      else if (phase === 'tool_running')
        parts.push(`running ${input.runningTool}`);
      else
        parts.push(
          thinkingLabel(startedAt !== null ? (now - startedAt) / 1000 : 0)
        );
      if (input.canInterrupt) parts.push('esc to interrupt');
      return result(phase, 'working', {
        line: `${verb}… (${parts.join(' · ')})`,
        glyph: 'claude',
      });
    }
    const header =
      runtime === 'codex' && input.reasoningTitle
        ? input.reasoningTitle
        : CODEX_STATUS.working;
    const hint = input.canInterrupt ? ` • ${CODEX_STATUS.interruptHint}` : '';
    const suffix =
      phase === 'hook_running'
        ? ' · Running hook'
        : phase === 'tool_running'
          ? ` · ${CODEX_STATUS.running} ${input.runningTool}`
          : '';
    return result(phase, 'working', {
      line: `${header} (${elapsed}${hint})${suffix}`,
      glyph: 'codex',
    });
  }

  if (input.queued) {
    return result('queued', 'working', { line: 'Waiting for agent' });
  }

  const endedAt = input.turnEndedAt ?? null;
  if (endedAt !== null && !turnOpen) {
    if (input.interrupted) {
      return result('interrupted', 'your_turn', {
        marker: interruptedMarker(runtime),
        footer: footerFor(runtime, seed, startedAt, endedAt),
      });
    }
    return result('your_turn', 'your_turn', {
      footer: footerFor(runtime, seed, startedAt, endedAt),
    });
  }
  if (input.turnState === 'your_turn') return result('your_turn', 'your_turn');
  if (input.turnState === 'waiting_for_approval')
    return result('waiting_for_approval', 'waiting_for_approval', {
      tone: 'warning',
    });
  return result('idle', 'idle');
}

/** How long a `working` state may go without any activity before it reads Idle. */
export const STALE_WORKING_MS = 10 * 60 * 1000;

export interface AgentStateSource {
  turn_state?: string | null;
  ended_at?: string | null;
  last_activity_at?: string | null;
  last_request_at?: string | null;
  activity_status?: string | null;
  is_active_now?: boolean | null;
}

/**
 * The chip for a list row or a header, from the server's `turn_state`.
 *
 * Without a turn state (sessions from before hooks reported turns) the row
 * is Idle: recency is "seen recently", shown in Last seen, never "Working".
 * A `working` state with no activity for `STALE_WORKING_MS` is Idle too, so
 * a harness that died mid-turn does not read as working forever.
 */
export function agentStateChip(
  source: AgentStateSource,
  now = Date.now()
): AgentStateChip {
  if (source.ended_at || source.turn_state === 'ended') return CHIPS.ended;
  const state = source.turn_state;
  if (state === 'waiting_for_approval') return CHIPS.waiting_for_approval;
  if (state === 'your_turn') return CHIPS.your_turn;
  if (state === 'working') {
    const seen = [source.last_activity_at, source.last_request_at]
      .map((value) => (value ? parseUTCDate(value).getTime() : NaN))
      .filter((value) => Number.isFinite(value));
    const last = seen.length ? Math.max(...seen) : null;
    if (last === null || now - last <= STALE_WORKING_MS) return CHIPS.working;
  }
  return CHIPS.idle;
}

/** Epoch ms from a server timestamp; an offset-less value is UTC. */
export function utcMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = parseUTCDate(value).getTime();
  return Number.isFinite(ms) ? ms : null;
}

/** The turn facts a session row (or the live socket) carries. */
export interface AgentTurnInfo {
  sourceType?: string | null;
  turnState?: string | null;
  turnStartedAt?: string | null;
  turnEndedAt?: string | null;
  turnInterrupted?: boolean | null;
  /** Tool reported running by an SDK session's progress events. */
  runningTool?: string | null;
}

/** Turn facts from a runtime session summary or agent session row. */
export function turnInfoFromSession(
  session: Record<string, unknown> | null | undefined
): AgentTurnInfo | null {
  if (!session) return null;
  const str = (value: unknown): string | null =>
    typeof value === 'string' && value ? value : null;
  return {
    sourceType:
      str(session.session_source_type) ||
      str(session.agent_kind) ||
      str(session.sourceType),
    turnState: str(session.turn_state),
    turnStartedAt: str(session.turn_started_at),
    turnEndedAt: str(session.turn_ended_at),
    turnInterrupted:
      typeof session.turn_interrupted === 'boolean'
        ? session.turn_interrupted
        : null,
  };
}

/**
 * Fold one realtime event into the turn facts: prompt, tool end, turn end,
 * wait for input and session end on `runtime_sessions`, and SDK command
 * progress on `agent_control`. Unrelated events return the input unchanged.
 */
export function applyTurnEvent(
  turn: AgentTurnInfo,
  message: {
    type?: string;
    timestamp?: string;
    payload?: Record<string, unknown>;
  }
): AgentTurnInfo {
  const payload = message.payload ?? {};
  const at =
    (typeof payload.timestamp === 'string' && payload.timestamp) ||
    message.timestamp ||
    new Date().toISOString();
  const state =
    typeof payload.turn_state === 'string' ? payload.turn_state : null;
  switch (message.type) {
    case 'runtime_session_prompt':
      return {
        ...turn,
        turnState: state ?? 'working',
        turnStartedAt: at,
        turnEndedAt: null,
        turnInterrupted: null,
        runningTool: null,
      };
    case 'runtime_session_tool_ended':
      return { ...turn, turnState: state ?? turn.turnState, runningTool: null };
    case 'runtime_session_turn_ended':
      return {
        ...turn,
        turnState: state ?? 'your_turn',
        turnEndedAt: at,
        turnInterrupted: payload.interrupted === true,
        runningTool: null,
      };
    case 'runtime_session_waiting_for_input':
      return {
        ...turn,
        turnState: state ?? 'your_turn',
        turnEndedAt: turn.turnEndedAt || at,
        runningTool: null,
      };
    case 'runtime_session_hook_ended':
      return { ...turn, turnState: 'ended', runningTool: null };
    case 'managed_agent_command_progress':
      return {
        ...turn,
        turnState: turn.turnState === 'ended' ? 'ended' : 'working',
        turnStartedAt:
          turn.turnEndedAt || !turn.turnStartedAt ? at : turn.turnStartedAt,
        turnEndedAt: null,
        runningTool:
          payload.kind === 'tool' && typeof payload.tool_name === 'string'
            ? payload.tool_name
            : null,
      };
    default:
      return turn;
  }
}

/** Output tokens of gateway rows at or after the turn start. */
export function turnOutputTokens(
  events: Array<{
    type?: string;
    timestamp?: string | null;
    payload?: unknown;
  }>,
  turnStartedAt: number | null
): number {
  let total = 0;
  for (const event of events) {
    const type = event.type || '';
    if (!type.startsWith('model_gateway') || type.endsWith('_started'))
      continue;
    const at = utcMs(event.timestamp ?? null);
    if (turnStartedAt !== null && (at === null || at < turnStartedAt)) continue;
    const payload = (event.payload ?? {}) as Record<string, unknown>;
    const usage = (payload.usage ?? {}) as Record<string, unknown>;
    const value =
      payload.completion_tokens ??
      usage.output_tokens ??
      usage.completion_tokens;
    if (typeof value === 'number' && Number.isFinite(value)) total += value;
  }
  return total;
}
