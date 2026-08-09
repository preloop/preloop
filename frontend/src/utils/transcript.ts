/**
 * Conversation reconstruction for the chat-style session transcript.
 *
 * Pure functions only: no Lit, no fetch. The builder turns stored gateway
 * events (each carrying the full accumulated message history as a
 * conversation preview, plus the capped raw request body) and activity rows
 * into a chat-shaped item list where ONLY top-level user prompts and final
 * agent responses are expanded; tool calls, tool results, system/injected
 * segments and intermediate agent output are collapsed step groups.
 *
 * Classification honesty rules (binding, see
 * factory/briefs/2026-08-06-transcript-redesign-spec.md section 2):
 * - Anthropic-style tool results arrive as role "user". They are detected
 *   EXACTLY from the raw request body's content-block structure
 *   (`type: "tool_result"` / role "tool" / Responses `function_call_output`)
 *   when that body was captured, and only heuristically (role substring)
 *   otherwise. Events without a usable raw body are counted in
 *   `stats.eventsWithoutRawBody` so the UI can say so instead of guessing.
 * - Injected pseudo-user segments (system reminders, [Preloop] question
 *   notices, local-command wrappers, compaction summaries) are matched by a
 *   deliberately conservative pattern list. When in doubt a user-role
 *   message STAYS a visible prompt: hiding a real prompt is worse than
 *   showing a misclassified reminder.
 */

import type {
  FlowGatewayConversationPreviewMessage,
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
} from '../types';

export type TranscriptStepKind =
  'tool_call' | 'tool_result' | 'system' | 'injected' | 'intermediate';

export interface TranscriptStep {
  key: string;
  kind: TranscriptStepKind;
  label: string;
  text: string;
  timestamp: string | null;
  /** Which injection pattern matched (kind === 'injected' only). */
  injectedPatternId?: string;
  redacted?: boolean;
  truncated?: boolean;
  toolName?: string | null;
  serverName?: string | null;
  status?: string | null;
  /** True when the classification came from exact structure, not a heuristic. */
  detectionExact: boolean;
}

export interface TranscriptMessageItem {
  type: 'message';
  key: string;
  kind: 'user_prompt' | 'agent_response' | 'operator';
  role: string;
  text: string;
  timestamp: string | null;
  event: FlowGatewayEvent | null;
  redacted?: boolean;
  truncated?: boolean;
  failed?: boolean;
}

export interface TranscriptStepGroupItem {
  type: 'steps';
  key: string;
  steps: TranscriptStep[];
}

export interface TranscriptDividerItem {
  type: 'divider';
  key: string;
  label: string;
  timestamp: string | null;
}

export type TranscriptItem =
  TranscriptMessageItem | TranscriptStepGroupItem | TranscriptDividerItem;

export interface TranscriptStats {
  promptCount: number;
  responseCount: number;
  stepCount: number;
  toolResultCount: number;
  injectedCount: number;
  toolCallCount: number;
  /** Gateway events whose raw request body was unavailable for exact
   *  tool-result detection. When > 0 the UI must disclose that some
   *  user-role bubbles may actually be tool results. */
  eventsWithoutRawBody: number;
  totalEvents: number;
}

export interface TranscriptBuildResult {
  items: TranscriptItem[];
  stats: TranscriptStats;
}

/**
 * Conservative harness-injection pattern list. Every entry is a known,
 * verifiable convention; anything not matched stays a visible prompt.
 */
export const INJECTED_SEGMENT_PATTERNS: Array<{
  id: string;
  label: string;
  test: (text: string) => boolean;
}> = [
  {
    // Preloop's own in-session question/approval notice
    // (backend services/ask_user_inband.py format_question_notice).
    id: 'preloop_notice',
    label: 'Preloop question notice',
    test: (text) => text.startsWith('[Preloop]'),
  },
  {
    id: 'system_reminder',
    label: 'System reminder',
    test: (text) => text.includes('<system-reminder>'),
  },
  {
    id: 'caveat',
    label: 'Harness caveat',
    test: (text) => text.startsWith('Caveat: The messages below'),
  },
  {
    id: 'local_command',
    label: 'Local command',
    test: (text) =>
      text.includes('<command-name>') ||
      text.includes('<local-command-stdout>'),
  },
  {
    id: 'compaction',
    label: 'Compaction summary',
    test: (text) =>
      text.startsWith(
        'This session is being continued from a previous conversation'
      ),
  },
  {
    id: 'interrupted',
    label: 'Interrupt marker',
    test: (text) => text.startsWith('[Request interrupted'),
  },
];

/**
 * Exact-prefix gateway event-type check. The backend emits
 * `model_gateway_call` (services/model_gateway_events.py); prefix (not
 * substring) matching keeps hypothetical namespaced variants like
 * `model_gateway_call_v2` while excluding unrelated types that merely
 * contain the words somewhere inside.
 */
export function isModelGatewayEventType(type: string): boolean {
  return type.startsWith('model_gateway');
}

/** First-match injected pattern for a user-role text, or null. */
export function matchInjectedSegment(
  text: string
): { id: string; label: string } | null {
  const trimmed = text.trimStart();
  for (const pattern of INJECTED_SEGMENT_PATTERNS) {
    if (pattern.test(trimmed)) return { id: pattern.id, label: pattern.label };
  }
  return null;
}

// Prefix length used to match preview text against raw-body tool-result
// text. The two are capped at different lengths server-side (preview
// 32768/message, activity body strings 8192), so equality can only be
// asserted on a common prefix.
const TOOL_RESULT_MATCH_PREFIX = 400;

function normalizeForMatch(text: string): string {
  return text.replace(/\s+/g, ' ').trim().slice(0, TOOL_RESULT_MATCH_PREFIX);
}

/** Mirror of the backend's _content_to_preview_text flattening. */
function contentToText(content: unknown): string {
  if (content === null || content === undefined) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((item) => contentToText(item))
      .filter(Boolean)
      .join('\n');
  }
  if (typeof content === 'object') {
    const record = content as Record<string, unknown>;
    if (typeof record.text === 'string') return record.text;
    if (record.content !== undefined) return contentToText(record.content);
    if (typeof record.output === 'string') return record.output;
    return '';
  }
  return String(content);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Exact tool-result texts from the raw request body, as normalized match
 * prefixes. Detects OpenAI `role: "tool"` messages, Anthropic user messages
 * whose content array carries `type: "tool_result"` blocks, and Responses
 * API `function_call_output` items.
 *
 * Returns null when exact detection is IMPOSSIBLE, so callers can degrade
 * honestly: either no raw message array was captured (capture off, older
 * rows), or tool-result-shaped items were present but none yielded usable
 * text (unrecognized structure). An EMPTY set is a positive claim: the raw
 * body was parsed and contains no tool results at all.
 */
export function collectRawToolResultPrefixes(
  event: FlowGatewayEvent
): Set<string> | null {
  const request = isRecord(event.payload?.request)
    ? (event.payload!.request as Record<string, unknown>)
    : null;
  if (!request) return null;
  const arrays: Array<readonly unknown[]> = [];
  if (Array.isArray(request.messages)) arrays.push(request.messages);
  if (Array.isArray(request.input)) arrays.push(request.input);
  if (!arrays.length) return null;

  const prefixes = new Set<string>();
  let sawToolResultShape = false;
  for (const rawMessages of arrays) {
    for (const raw of rawMessages) {
      if (!isRecord(raw)) continue;
      const role = String(raw.role || '').toLowerCase();
      const itemType = String(raw.type || '').toLowerCase();
      if (role === 'tool' || itemType === 'function_call_output') {
        sawToolResultShape = true;
        const text = contentToText(raw.content ?? raw.output ?? raw);
        if (text.trim()) prefixes.add(normalizeForMatch(text));
        continue;
      }
      const content = raw.content;
      if (!Array.isArray(content)) continue;
      for (const block of content) {
        if (!isRecord(block)) continue;
        if (String(block.type || '').toLowerCase() === 'tool_result') {
          sawToolResultShape = true;
          const text = contentToText(block.content ?? block);
          if (text.trim()) prefixes.add(normalizeForMatch(text));
        }
      }
    }
  }
  // Tool-result items existed but none produced matchable text: exact
  // detection would silently misclassify every one of them as a prompt, so
  // report "no usable raw body" instead and let the heuristics (and the
  // stats disclosure) take over.
  if (sawToolResultShape && !prefixes.size) return null;
  return prefixes;
}

function previewMessages(
  event: FlowGatewayEvent
): FlowGatewayConversationPreviewMessage[] {
  const messages = event.payload?.conversation_preview?.messages;
  return Array.isArray(messages) ? messages : [];
}

function eventIsFailure(event: FlowGatewayEvent): boolean {
  const outcome = String(event.payload?.outcome || '').toLowerCase();
  if (outcome === 'error' || outcome === 'budget_denied') return true;
  const statusCode = Number(event.payload?.status_code || 0);
  return statusCode >= 400;
}

// Internal chronological atom before grouping.
type Atom =
  | { type: 'message'; item: TranscriptMessageItem; order: number }
  | { type: 'step'; step: TranscriptStep; order: number }
  | { type: 'divider'; item: TranscriptDividerItem; order: number };

function atomTime(atom: Atom): number {
  const timestamp =
    atom.type === 'message'
      ? atom.item.timestamp
      : atom.type === 'step'
        ? atom.step.timestamp
        : atom.item.timestamp;
  const parsed = new Date(timestamp || 0).getTime();
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * Build the chat-shaped transcript. `events` may arrive in any order (the
 * observer stores them newest-first); `activity` rows supply tool calls,
 * operator messages and lifecycle markers.
 */
export function buildConversation(
  events: FlowGatewayEvent[],
  activity: RuntimeSessionActivityItem[] = []
): TranscriptBuildResult {
  const stats: TranscriptStats = {
    promptCount: 0,
    responseCount: 0,
    stepCount: 0,
    toolResultCount: 0,
    injectedCount: 0,
    toolCallCount: 0,
    eventsWithoutRawBody: 0,
    totalEvents: 0,
  };

  const gatewayEvents = events
    .filter(
      (event) =>
        isModelGatewayEventType(event.type) || previewMessages(event).length
    )
    .sort(
      (left, right) =>
        new Date(left.timestamp || 0).getTime() -
        new Date(right.timestamp || 0).getTime()
    );
  stats.totalEvents = gatewayEvents.length;

  const atoms: Atom[] = [];
  let order = 0;
  const seenSignatures = new Set<string>();

  for (const event of gatewayEvents) {
    const toolResultPrefixes = collectRawToolResultPrefixes(event);
    if (toolResultPrefixes === null) stats.eventsWithoutRawBody += 1;
    const failed = eventIsFailure(event);

    previewMessages(event).forEach((message, messageIndex) => {
      const text = (message.text || '').trim();
      if (!text && !message.redacted) return;
      const role = (message.role || 'message').toLowerCase();
      const source = (message.source || 'request').toLowerCase();
      // Growing-prefix delta: render a message only the first time it
      // appears across the session's accumulated request histories. The
      // position index is part of the signature so a genuinely repeated
      // identical text at a DIFFERENT conversation point (e.g. the user
      // sending "continue" twice) is not collapsed into one bubble, while
      // the same message replayed at the same position in later requests
      // still deduplicates — including an event's response, which the next
      // request replays as history at that same position. (An event-id
      // component would defeat the dedup outright: every request replays
      // the whole history.)
      const signature = `${messageIndex}::${role}::${text}`;
      if (text && seenSignatures.has(signature)) return;
      if (text) seenSignatures.add(signature);

      const key = `${event.id}:m:${messageIndex}`;
      const base = {
        key,
        text,
        timestamp: event.timestamp || null,
        redacted: Boolean(message.redacted),
        truncated: Boolean(message.truncated),
      };

      if (source === 'response') {
        // Provisional agent response; the grouping pass demotes all but the
        // final response of each exchange to an 'intermediate' step.
        atoms.push({
          type: 'message',
          order: order++,
          item: {
            type: 'message',
            kind: 'agent_response',
            role: message.role || 'assistant',
            event,
            failed,
            ...base,
          },
        });
        return;
      }

      if (role === 'system') {
        atoms.push({
          type: 'step',
          order: order++,
          step: {
            kind: 'system',
            label: 'System prompt',
            detectionExact: true,
            ...base,
          },
        });
        return;
      }

      if (role.includes('tool') || role.includes('function')) {
        stats.toolResultCount += 1;
        atoms.push({
          type: 'step',
          order: order++,
          step: {
            kind: 'tool_result',
            label: 'Tool result',
            detectionExact: true,
            ...base,
          },
        });
        return;
      }

      if (role === 'assistant' || role.includes('agent')) {
        // Assistant text replayed inside a request body is conversation
        // history, not a new response: collapse it.
        atoms.push({
          type: 'step',
          order: order++,
          step: {
            kind: 'intermediate',
            label: 'Assistant (history)',
            detectionExact: true,
            ...base,
          },
        });
        return;
      }

      // User-role request message: exact tool-result check first.
      if (
        text &&
        toolResultPrefixes !== null &&
        toolResultPrefixes.has(normalizeForMatch(text))
      ) {
        stats.toolResultCount += 1;
        atoms.push({
          type: 'step',
          order: order++,
          step: {
            kind: 'tool_result',
            label: 'Tool result',
            detectionExact: true,
            ...base,
          },
        });
        return;
      }

      const injected = text ? matchInjectedSegment(text) : null;
      if (injected) {
        stats.injectedCount += 1;
        atoms.push({
          type: 'step',
          order: order++,
          step: {
            kind: 'injected',
            label: injected.label,
            injectedPatternId: injected.id,
            detectionExact: false,
            ...base,
          },
        });
        return;
      }

      atoms.push({
        type: 'message',
        order: order++,
        item: {
          type: 'message',
          kind: 'user_prompt',
          role: message.role || 'user',
          event,
          ...base,
        },
      });
    });
  }

  for (const [index, item] of activity.entries()) {
    const activityType = (item.activity_type || '').toLowerCase();
    const key = `activity:${index}:${item.timestamp || ''}`;
    if (activityType === 'tool_call') {
      stats.toolCallCount += 1;
      atoms.push({
        type: 'step',
        order: order++,
        step: {
          key,
          kind: 'tool_call',
          label: item.tool_name || item.title || 'Tool call',
          text: item.summary || '',
          timestamp: item.timestamp || null,
          toolName: item.tool_name,
          serverName: item.server_name,
          status: item.status,
          detectionExact: true,
        },
      });
      continue;
    }
    if (activityType === 'agent_control_message') {
      const metadata = (item.metadata || {}) as Record<string, unknown>;
      const role =
        typeof metadata.role === 'string' && metadata.role.trim()
          ? metadata.role
          : 'operator';
      atoms.push({
        type: 'message',
        order: order++,
        item: {
          type: 'message',
          key,
          kind: 'operator',
          role,
          text: item.summary || item.title || '',
          timestamp: item.timestamp || null,
          event: null,
        },
      });
      continue;
    }
    if (
      activityType === 'session_started' ||
      activityType === 'session_ended'
    ) {
      atoms.push({
        type: 'divider',
        order: order++,
        item: {
          type: 'divider',
          key,
          label:
            activityType === 'session_started'
              ? 'Session started'
              : 'Session ended',
          timestamp: item.timestamp || null,
        },
      });
    }
  }

  atoms.sort((left, right) => {
    const delta = atomTime(left) - atomTime(right);
    return delta !== 0 ? delta : left.order - right.order;
  });

  // Demote every agent response except the LAST one of each exchange (the
  // stretch between consecutive user prompts) to a collapsed step.
  const responseIndexes: number[] = [];
  const finalResponses = new Set<number>();
  const flush = () => {
    const last = responseIndexes[responseIndexes.length - 1];
    if (last !== undefined) finalResponses.add(last);
    responseIndexes.length = 0;
  };
  atoms.forEach((atom, index) => {
    if (atom.type === 'message' && atom.item.kind === 'user_prompt') flush();
    if (atom.type === 'message' && atom.item.kind === 'agent_response') {
      responseIndexes.push(index);
    }
  });
  flush();

  const items: TranscriptItem[] = [];
  let currentSteps: TranscriptStep[] = [];
  const closeSteps = () => {
    if (!currentSteps.length) return;
    items.push({
      type: 'steps',
      key: `steps:${currentSteps[0].key}`,
      steps: currentSteps,
    });
    stats.stepCount += currentSteps.length;
    currentSteps = [];
  };

  atoms.forEach((atom, index) => {
    if (atom.type === 'step') {
      currentSteps.push(atom.step);
      return;
    }
    if (atom.type === 'divider') {
      closeSteps();
      items.push(atom.item);
      return;
    }
    if (atom.item.kind === 'agent_response' && !finalResponses.has(index)) {
      currentSteps.push({
        key: atom.item.key,
        kind: 'intermediate',
        label: 'Agent (working)',
        text: atom.item.text,
        timestamp: atom.item.timestamp,
        redacted: atom.item.redacted,
        truncated: atom.item.truncated,
        detectionExact: true,
      });
      return;
    }
    closeSteps();
    if (atom.item.kind === 'user_prompt') stats.promptCount += 1;
    if (atom.item.kind === 'agent_response') stats.responseCount += 1;
    items.push(atom.item);
  });
  closeSteps();

  return { items, stats };
}
