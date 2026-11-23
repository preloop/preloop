import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth } from '../api';
import type { FlowGatewayEvent } from '../types';
import { formatElapsedCompact } from './duration';

export interface FlowConversation {
  id: string;
  flow_id: string;
  owner_user_id: string;
  execution_id: string | null;
  state: 'starting' | 'working' | 'waiting' | 'stopped' | 'ended' | 'failed';
  notice: string;
  can_write: boolean;
  flow_name?: string;
  turns: number;
  max_turns: number;
  idle_deadline: string | null;
  messages: Array<{
    id: string;
    turn: number;
    text: string;
    answer: string;
    status: string;
    execution_id: string | null;
  }>;
}

export async function conversationRequest<T>(
  path: string,
  body?: unknown
): Promise<T> {
  const result = await fetchWithAuth(
    `${REST_API_PREFIX}/${path}`,
    body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }
  );
  if (!result.ok) {
    const error = await result.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === 'string'
        ? error.detail
        : 'The conversation request failed. Try again.'
    );
  }
  return result.json();
}

const STATE_LABELS: Record<FlowConversation['state'], string> = {
  starting: 'Starting (cold start)',
  working: 'Working',
  waiting: 'Waiting for you',
  stopped: 'Stopped (resumable)',
  ended: 'Ended',
  failed: 'Failed',
};

/** The state's name from the user guide, nothing else. */
export function conversationLabel(conversation: FlowConversation): string {
  return STATE_LABELS[conversation.state];
}

/** Seconds left before the idle container stops, or null without a deadline. */
export function secondsUntilIdleStop(
  conversation: FlowConversation,
  now = Date.now()
): number | null {
  if (conversation.state !== 'waiting' || !conversation.idle_deadline)
    return null;
  const deadline = Date.parse(conversation.idle_deadline);
  if (!Number.isFinite(deadline)) return null;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

/** `9:05` for 545 seconds; hours roll into minutes (`72:00`). */
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export interface ConversationNotice {
  text: string;
  /** Only a failure is an error; every other state is calm. */
  tone: 'neutral' | 'danger';
}

/**
 * The one line under the thread for the current state, or null when the
 * in-thread activity indicator already says it (Starting, Working).
 */
export function conversationNotice(
  conversation: FlowConversation,
  now = Date.now()
): ConversationNotice | null {
  switch (conversation.state) {
    case 'starting':
    case 'working':
      return null;
    case 'waiting': {
      const left = secondsUntilIdleStop(conversation, now);
      if (left === null) return { text: 'Waiting for you.', tone: 'neutral' };
      if (left === 0)
        return {
          text: 'The idle time is up. Saving the conversation so it can resume.',
          tone: 'neutral',
        };
      return {
        text: `Waiting for you. The container stops in ${formatCountdown(left)} without a reply; a later message resumes it.`,
        tone: 'neutral',
      };
    }
    case 'stopped':
      return {
        text:
          conversation.notice ||
          'The container has stopped. Send a message to resume this conversation.',
        tone: 'neutral',
      };
    case 'ended':
      return {
        text:
          conversation.notice ||
          'This conversation has ended. Start a new conversation to continue.',
        tone: 'neutral',
      };
    case 'failed':
      return {
        text:
          conversation.notice ||
          'This conversation failed. Start a new conversation.',
        tone: 'danger',
      };
  }
}

/** Composer placeholder for a hosted conversation, per state. */
export function conversationPlaceholder(
  conversation: FlowConversation
): string {
  if (!conversation.can_write)
    return 'Only the person who started this conversation can send messages';
  switch (conversation.state) {
    case 'starting':
      return 'Wait for the first answer. Messages are not queued while the container starts';
    case 'working':
      return 'Wait for the answer. Messages are not queued while the agent works';
    case 'waiting':
      return `Message ${conversation.flow_name || 'the agent'}`;
    case 'stopped':
      return 'Send a message to resume this conversation';
    case 'ended':
      return 'This conversation has ended. Start a new one from the flow';
    case 'failed':
      return 'This conversation cannot continue. Start a new one from the flow';
  }
}

/** Whether the composer accepts a message in this state. */
export function conversationCanSend(conversation: FlowConversation): boolean {
  return (
    conversation.can_write &&
    (conversation.state === 'waiting' || conversation.state === 'stopped')
  );
}

/**
 * The words of the in-thread activity indicator while the agent has the
 * turn, with the elapsed time the window has watched it for.
 */
export function conversationActivityLine(
  conversation: FlowConversation,
  elapsedSeconds: number
): string | null {
  const elapsed = formatElapsedCompact(elapsedSeconds * 1000);
  if (conversation.state === 'starting')
    return `Starting a container for this conversation (${elapsed}). The first answer can take a minute or more.`;
  if (conversation.state === 'working') return `Working (${elapsed})`;
  return null;
}

/** Shown in place of a user bubble when the turn carried no typed message. */
export const INITIAL_PROMPT_LINE =
  'Conversation started with the flow’s initial prompt';

function openMessage(
  conversation: FlowConversation
): FlowConversation['messages'][number] | null {
  const last = conversation.messages[conversation.messages.length - 1];
  if (!last) return null;
  if (conversation.state !== 'starting' && conversation.state !== 'working')
    return null;
  return last.status === 'completed' ? null : last;
}

/** The open turn's answer snapshot while it is still being written. */
export function conversationLiveReply(conversation: FlowConversation): string {
  return openMessage(conversation)?.answer || '';
}

/** Feed durable conversation turns through the existing Talk transcript renderer. */
export function conversationEvents(
  conversation: FlowConversation
): FlowGatewayEvent[] {
  const open = openMessage(conversation);
  return conversation.messages.map((message) => ({
    id: message.id,
    execution_id: message.execution_id || '',
    timestamp: null,
    type: 'conversation_turn',
    payload: {
      conversation_preview: {
        messages: [
          // A turn started without a message is the flow's own prompt, not
          // something the user said: the view draws a system line for it.
          ...(message.text
            ? [
                {
                  source: 'request',
                  position: message.turn * 2,
                  role: 'user',
                  text: message.text,
                },
              ]
            : []),
          // The open turn's partial answer streams in the live bubble.
          ...(message.answer && message !== open
            ? [
                {
                  source: 'response',
                  position: message.turn * 2 + 1,
                  role: 'assistant',
                  text: message.answer,
                },
              ]
            : []),
        ],
      },
    },
  }));
}

/** The system line for a conversation whose first turn had no typed text. */
export function conversationIntro(conversation: FlowConversation): string {
  const first = conversation.messages[0];
  return first && first.turn === 1 && !first.text ? INITIAL_PROMPT_LINE : '';
}
