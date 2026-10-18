/**
 * Which session a note was aimed at, in words and as a link.
 *
 * A busy agent runs several sessions at once, and a list of notes with no
 * session on them cannot answer the one question an operator brings to it:
 * did the note reach the conversation I meant. So every note names its
 * session, and the name links to that session's log at the turn the note
 * landed on.
 */
import type { OperatorNote, RuntimeSessionSummary } from '../types';

/** The fields of a session row a label needs. */
export type NoteSessionRow = Pick<RuntimeSessionSummary, 'id' | 'started_at'> &
  Partial<Pick<RuntimeSessionSummary, 'alias' | 'title'>> &
  Partial<
    Pick<
      RuntimeSessionSummary,
      'session_source_type' | 'runtime_principal_name'
    >
  >;

/** A short session id: enough to tell two apart, short enough for a row. */
export function shortSessionId(id: string): string {
  return id.slice(0, 8);
}

/** The session's own name: its alias, its title, or its short id. */
export function sessionName(
  session: Pick<NoteSessionRow, 'id' | 'alias' | 'title'>
): string {
  return (
    session.alias?.trim() ||
    session.title?.trim() ||
    `Session ${shortSessionId(session.id)}`
  );
}

/** The harness that runs the session, when the row says. */
export function sessionHarness(session: NoteSessionRow): string {
  return (
    session.runtime_principal_name?.trim() ||
    (session.session_source_type || '').replace(/_/g, ' ')
  );
}

/** One line for a session picker: name, harness and when it started. */
export function sessionOptionLabel(session: NoteSessionRow): string {
  const parts = [sessionName(session)];
  const harness = sessionHarness(session);
  if (harness) parts.push(harness);
  if (session.started_at) {
    parts.push(`started ${new Date(session.started_at).toLocaleString()}`);
  }
  return parts.join(' · ');
}

/**
 * Link to a session's log, focused on one note's turn when given.
 *
 * The runtime sessions page reads `turn` as the focus id, and the replay
 * matches it against the note id on the delivery row.
 */
export function sessionLogHref(sessionId: string, noteId?: string): string {
  const query = new URLSearchParams({ sessionId });
  if (noteId) query.set('turn', noteId);
  return `/console/runtime-sessions?${query.toString()}`;
}

/** The target of a note that has no session yet, in words. */
export function unboundNoteTarget(note: OperatorNote): string {
  return note.next_session ? 'Next session' : 'Current or next session';
}

/**
 * The label for the session a note is on.
 *
 * Falls back to the short id when the session is older than the page of
 * sessions the caller loaded, so a note never loses its session.
 */
export function noteSessionLabel(
  note: OperatorNote,
  sessions: ReadonlyMap<string, NoteSessionRow>
): string {
  const id = note.runtime_session_id;
  if (!id) return unboundNoteTarget(note);
  const session = sessions.get(id);
  return session ? sessionName(session) : `Session ${shortSessionId(id)}`;
}
