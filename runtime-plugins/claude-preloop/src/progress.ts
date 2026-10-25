import type { LiveProgress } from "./sessions.js";

/** Longest a merged reply delta waits before it is sent. */
export const PROGRESS_FLUSH_MS = 250;

type Delta = Extract<LiveProgress, { kind: "assistant_delta" }>;

/**
 * Turns live session progress into `session_progress` envelopes.
 *
 * Reply deltas arrive per token, so consecutive deltas of one item are merged
 * and sent at most every PROGRESS_FLUSH_MS; any other event of the session
 * flushes them first so the order is kept. Every envelope carries a
 * per-session `seq`, so a viewer that sees a jump knows it missed one and
 * re-reads the thread.
 */
export class SessionProgressForwarder {
  private pending = new Map<string, Delta>();
  private seq = new Map<string, number>();
  private timer?: ReturnType<typeof setTimeout>;

  constructor(
    private readonly send: (payload: Record<string, unknown>) => void,
    private readonly runtime: string,
    private readonly flushMs: number = PROGRESS_FLUSH_MS,
  ) {}

  push(sessionId: string, event: LiveProgress): void {
    if (event.kind === "assistant_delta") {
      const pending = this.pending.get(sessionId);
      if (
        pending &&
        pending.turn_id === event.turn_id &&
        pending.item_id === event.item_id
      ) {
        pending.delta += event.delta;
        return;
      }
      this.flush(sessionId);
      this.pending.set(sessionId, { ...event });
      this.timer ??= setTimeout(() => {
        this.timer = undefined;
        for (const id of [...this.pending.keys()]) this.flush(id);
      }, this.flushMs);
      return;
    }
    this.flush(sessionId);
    this.emit(sessionId, event);
  }

  stop(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    this.pending.clear();
  }

  private flush(sessionId: string): void {
    const pending = this.pending.get(sessionId);
    if (!pending) return;
    this.pending.delete(sessionId);
    this.emit(sessionId, pending);
  }

  private emit(sessionId: string, event: LiveProgress): void {
    const seq = (this.seq.get(sessionId) ?? 0) + 1;
    this.seq.set(sessionId, seq);
    this.send({
      ...event,
      session_source_id: sessionId,
      runtime: this.runtime,
      seq,
    });
  }
}
