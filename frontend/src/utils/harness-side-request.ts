import type { FlowGatewayEvent } from '../types';

/**
 * Side requests a coding-agent harness sends through the gateway on its own
 * behalf (a session title, a 3-5 word status line). They share the session
 * but are neither the operator's words nor the agent's work, so the
 * transcript folds them and the live status ignores them. Prefix match only,
 * at the start of the message: a prompt that merely mentions describing an
 * action stays a prompt.
 */
export const HARNESS_SIDE_REQUEST_PREFIXES: readonly string[] = [
  'Describe your most recent action in 3-5 words',
  'Please write a 5-10 word title for the following conversation',
  'Analyze if this message indicates a new conversation topic',
];

export function isHarnessSideRequest(text: string): boolean {
  const trimmed = text.trimStart();
  return HARNESS_SIDE_REQUEST_PREFIXES.some((prefix) =>
    trimmed.startsWith(prefix)
  );
}

/** True when a gateway event's request is a harness side request. */
export function isHarnessSideRequestEvent(event: FlowGatewayEvent): boolean {
  const messages = event.payload?.conversation_preview?.messages;
  if (!Array.isArray(messages)) return false;
  return messages.some(
    (message) =>
      (message.source || 'request').toLowerCase() !== 'response' &&
      (message.role || '').toLowerCase() === 'user' &&
      isHarnessSideRequest(message.text || '')
  );
}
