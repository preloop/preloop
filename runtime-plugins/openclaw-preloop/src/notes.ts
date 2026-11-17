/** Native note carriers audited against OpenClaw v2026.6.11 hook-types.ts. */
export type NoteContext = { sessionId?: string; runId?: string };
export type NoteInput = {
  sessionId: string;
  runId: string;
  prompt: string;
  systemPrompt?: string;
  historyMessages: unknown[];
};
export type NoteCall = NoteContext & {
  callId: string;
  outcome?: "completed" | "error";
};
export type NoteEnd = { sessionId: string; reason?: string };
export type NoteFetch = (
  input: string,
  init: {
    method: string;
    headers: Record<string, string>;
    body: string;
    signal: AbortSignal;
  },
) => Promise<{ ok: boolean; json(): Promise<unknown> }>;
type Claim = {
  text: string;
  ids: string[];
  run: string;
  confirmed: boolean;
  seen: boolean;
  calls: Set<string>;
};
type Session = { registered: boolean; claim?: Claim };

function contains(value: unknown, text: string): boolean {
  if (typeof value === "string") return value.includes(text);
  if (Array.isArray(value)) return value.some((item) => contains(item, text));
  if (value && typeof value === "object")
    return Object.values(value).some((item) => contains(item, text));
  return false;
}

/** Claim at prompt build; acknowledge witnessed context only after a real model call. */
export class OperatorNotes {
  private sessions = new Map<string, Session>();
  private endedSessions = new Set<string>();
  private calls = new Map<string, Promise<unknown>>();
  constructor(
    private readonly permissionCheckUrl: string,
    private readonly token: string,
    private readonly fetchImpl: NoteFetch,
  ) {}

  private async serial<T>(id: string, callback: () => Promise<T>): Promise<T> {
    const previous = this.calls.get(id) ?? Promise.resolve();
    const call = previous.catch(() => undefined).then(callback);
    this.calls.set(id, call);
    try {
      return await call;
    } finally {
      if (this.calls.get(id) === call) this.calls.delete(id);
    }
  }

  private async post(
    path: string,
    body: unknown,
  ): Promise<Record<string, unknown>> {
    const response = await this.fetchImpl(
      new URL(path, this.permissionCheckUrl).toString(),
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(3000),
      },
    );
    if (!response.ok) throw new Error("Note endpoint unavailable");
    const result = await response.json();
    if (!result || typeof result !== "object" || Array.isArray(result))
      throw new Error("Invalid note response");
    return result as Record<string, unknown>;
  }

  private state(id: string): Session | undefined {
    let session = this.sessions.get(id);
    if (!session) {
      if (this.sessions.size >= 256) {
        // An in-flight registration/pull may acquire a claim after yielding.
        // Keep its state reachable for the later model-call receipt hooks.
        const empty = [...this.sessions].find(
          ([key, item]) => !item.claim && !this.calls.has(key),
        );
        if (!empty) return undefined;
        this.sessions.delete(empty[0]);
      }
      session = { registered: false };
      this.sessions.set(id, session);
    }
    return session;
  }

  private async acknowledge(id: string, session: Session): Promise<void> {
    const claim = session.claim;
    if (!claim?.confirmed) return;
    await this.post("notes/ack", { session_id: id, note_ids: claim.ids });
    session.claim = undefined;
  }

  async build(
    ctx: NoteContext,
  ): Promise<{ prependContext: string } | undefined> {
    const id = ctx.sessionId?.trim();
    const run = ctx.runId?.trim();
    if (!id || id.length > 200 || !run) return undefined;
    return this.serial(id, async () => {
      try {
        const session = this.state(id);
        if (!session) return undefined;
        if (!session.registered) {
          const result = await this.post("session-start", {
            session_id: id,
            source: "openclaw",
          });
          if (
            typeof result.runtime_session_id !== "string" ||
            !result.runtime_session_id
          )
            return undefined;
          session.registered = true;
        }
        await this.acknowledge(id, session);
        if (session.claim && session.claim.run && session.claim.run !== run)
          return undefined;
        if (!session.claim) {
          const response = await this.post("notes/pending", {
            session_id: id,
            channel: "hook",
            event: "pre_tool_use",
            defer_delivery: true,
          });
          const ids = Array.isArray(response.notes)
            ? response.notes.flatMap((item) =>
                item &&
                typeof item === "object" &&
                typeof item.note_id === "string"
                  ? [item.note_id]
                  : [],
              )
            : [];
          if (
            typeof response.text !== "string" ||
            !response.text ||
            !ids.length
          )
            return undefined;
          session.claim = {
            text: response.text,
            ids: [...new Set(ids)],
            run,
            confirmed: false,
            seen: false,
            calls: new Set(),
          };
        }
        session.claim.run = run;
        return { prependContext: session.claim.text };
      } catch {
        return undefined;
      } // Note delivery must not stop a model turn.
    });
  }

  async input(event: NoteInput, ctx: NoteContext): Promise<void> {
    if (event.sessionId !== ctx.sessionId || event.runId !== ctx.runId) return;
    await this.serial(event.sessionId, async () => {
      const session = this.sessions.get(event.sessionId);
      const claim = session?.claim;
      if (!session || !claim || claim.run !== event.runId) return;
      // History can contain old notes; only the current admitted prompt carries this batch.
      // llm_input precedes preflight checks; it is a tentative witness only.
      claim.seen = contains([event.prompt, event.systemPrompt], claim.text);
    });
  }

  async started(event: NoteCall, ctx: NoteContext): Promise<void> {
    if (
      !event.sessionId ||
      event.sessionId !== ctx.sessionId ||
      event.runId !== ctx.runId ||
      !event.callId
    )
      return;
    await this.serial(event.sessionId, async () => {
      const claim = this.sessions.get(event.sessionId!)?.claim;
      if (claim && claim.run === event.runId && claim.seen && !claim.confirmed)
        claim.calls.add(event.callId);
    });
  }

  async completed(event: NoteCall, ctx: NoteContext): Promise<void> {
    if (
      !event.sessionId ||
      event.sessionId !== ctx.sessionId ||
      event.runId !== ctx.runId ||
      !event.callId
    )
      return;
    await this.serial(event.sessionId, async () => {
      const session = this.sessions.get(event.sessionId!);
      const claim = session?.claim;
      if (
        !session ||
        !claim ||
        claim.run !== event.runId ||
        !claim.calls.delete(event.callId)
      )
        return;
      if (event.outcome !== "completed") return;
      claim.confirmed = true;
      try {
        await this.acknowledge(event.sessionId!, session);
      } catch {
        /* Retry ACK without reinjecting. */
      }
    });
  }

  async turnEnded(ctx: NoteContext): Promise<void> {
    if (!ctx.sessionId || !ctx.runId) return;
    await this.serial(ctx.sessionId, async () => {
      const claim = this.sessions.get(ctx.sessionId!)?.claim;
      if (claim && claim.run === ctx.runId && !claim.confirmed) {
        claim.run = "";
        claim.seen = false;
        claim.calls.clear();
      }
    });
  }

  async ended(event: NoteEnd): Promise<void> {
    if (
      !event.sessionId ||
      event.sessionId.length > 200 ||
      !["new", "reset", "deleted"].includes(event.reason ?? "")
    )
      return;
    await this.serial(event.sessionId, async () => {
      if (this.endedSessions.has(event.sessionId!)) return;
      // Native terminal events remain authoritative after cache eviction.
      // The backend resolves external ids only within this bearer principal.
      try {
        const session = this.sessions.get(event.sessionId);
        if (session) await this.acknowledge(event.sessionId, session);
        await this.post("session-end", {
          external_session_id: event.sessionId,
          reason: event.reason,
        });
        this.sessions.delete(event.sessionId);
        this.endedSessions.add(event.sessionId);
        if (this.endedSessions.size > 256)
          this.endedSessions.delete(this.endedSessions.values().next().value!);
      } catch {
        /* TTL or another authority may terminalize an unreachable run. */
      }
    });
  }
}
