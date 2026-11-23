import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth } from '../api';
import type { FlowGatewayEvent } from '../types';

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

export function conversationLabel(
  conversation: FlowConversation,
  now = Date.now()
): string {
  const labels = {
    starting: 'Starting (cold start)',
    working: 'Working',
    waiting: 'Waiting for you',
    stopped: 'Stopped (resumable)',
    ended: 'Ended',
    failed: 'Failed',
  };
  const remaining = conversation.idle_deadline
    ? Math.max(
        0,
        Math.ceil((Date.parse(conversation.idle_deadline) - now) / 1000)
      )
    : null;
  return (
    labels[conversation.state] +
    (conversation.state === 'waiting' && remaining !== null
      ? ` · container stops in ${remaining}s`
      : '')
  );
}

/** Feed durable conversation turns through the existing Talk transcript renderer. */
export function conversationEvents(
  conversation: FlowConversation
): FlowGatewayEvent[] {
  return conversation.messages.map((message) => ({
    id: message.id,
    execution_id: message.execution_id || '',
    timestamp: null,
    type: 'conversation_turn',
    payload: {
      conversation_preview: {
        messages: [
          {
            source: 'request',
            role: 'user',
            text: message.text || 'Start the flow’s initial prompt.',
          },
          ...(message.answer
            ? [{ source: 'response', role: 'assistant', text: message.answer }]
            : []),
        ],
      },
    },
  }));
}
