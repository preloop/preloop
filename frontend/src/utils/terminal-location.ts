import type { TerminalLocation } from '../types';
import { agentStateChip } from './agent-status';
import type { ObservedSession } from './session-observer';

/** Bounded, plain display text; Lit performs HTML escaping at render time. */
const label = (value: unknown): string =>
  typeof value === 'string'
    ? value.replace(/[\x00-\x1f\x7f]/g, '').slice(0, 256)
    : '';

export function terminalLocation(raw: unknown): TerminalLocation | null {
  const location = (raw as { terminal_location?: TerminalLocation } | null)
    ?.terminal_location;
  return location?.multiplexer === 'herdr' ? location : null;
}

export function terminalPlace(location: TerminalLocation): string {
  return [
    label(location.workspace_label) || label(location.workspace_id),
    label(location.tab_label) || label(location.tab_id),
  ]
    .filter(Boolean)
    .join(' / ');
}

export function paneClosed(location: TerminalLocation | null): boolean {
  return Boolean(
    location?.pane_closed_at ||
    location?.state === 'closed' ||
    location?.state === 'pane_closed'
  );
}

function ownSessionChip(session: ObservedSession) {
  const raw = (session.raw ?? {}) as Record<string, unknown>;
  const location = terminalLocation(raw);
  return agentStateChip({
    turn_state:
      location &&
      typeof raw.pending_approval_count === 'number' &&
      raw.pending_approval_count > 0
        ? 'waiting_for_approval'
        : typeof raw.turn_state === 'string'
          ? raw.turn_state
          : null,
    ended_at:
      session.endedAt ||
      (session.status === 'ended' && !paneClosed(location) ? 'ended' : null),
    last_activity_at: session.lastActivityAt,
  });
}

/** Own turn and approval evidence wins; terminal state supplies missing context. */
export function terminalSessionChip(session: ObservedSession) {
  const location = terminalLocation(session.raw);
  const own = ownSessionChip(session);
  if (!location) return { ...own, title: own.label };
  let state: string = own.label;
  let variant = own.variant;
  if (own.label !== 'Working' && own.label !== 'Needs approval') {
    if (paneClosed(location)) state = 'Pane closed';
    else if (location.state === 'blocked') {
      state = 'Waiting at a dialog in the terminal';
      variant = 'warning';
    } else if (own.label !== 'Ended') {
      state =
        (
          { working: 'Working', idle: 'Idle', done: 'Done' } as Record<
            string,
            string
          >
        )[location.state] || 'State unknown';
    }
  }
  const place = terminalPlace(location);
  return {
    ...own,
    label: [place, state].filter(Boolean).join(' · '),
    variant,
    title: [
      place,
      label(location.pane_label) ||
        label(location.terminal_title) ||
        label(location.pane_id),
      state,
    ]
      .filter(Boolean)
      .join(' · '),
  };
}

/** A closed pane ends inferred activity, but never overrides an observed turn. */
export function paneClosesActivity(session: ObservedSession | null): boolean {
  if (!session || !paneClosed(terminalLocation(session.raw))) return false;
  const chip = ownSessionChip(session);
  return chip.label !== 'Working' && chip.label !== 'Needs approval';
}
