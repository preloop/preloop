import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";

import type { ControlConfig } from "./config.js";

const execFileAsync = promisify(execFile);

/**
 * Minimal structural view of the Claude Agent SDK surface the sidecar uses.
 * Declared locally so unit tests can inject a fake and the module stays
 * loadable without the SDK installed (the real factory imports it lazily).
 */
export type SdkUserMessage = {
  type: "user";
  message: { role: "user"; content: string };
  parent_tool_use_id: null;
  session_id: string;
};

export type SdkMessage = {
  type: string;
  subtype?: string;
  session_id?: string;
  result?: string;
  message?: { content?: Array<{ type: string; text?: string }> };
};

export type SdkQueryHandle = AsyncIterable<SdkMessage> & {
  interrupt: () => Promise<void>;
};

export type QueryFactory = (params: {
  prompt: AsyncIterable<SdkUserMessage>;
  options: Record<string, unknown>;
}) => Promise<SdkQueryHandle>;

/** Default factory: lazily import the real Agent SDK. */
export const sdkQueryFactory: QueryFactory = async ({ prompt, options }) => {
  const sdk = (await import("@anthropic-ai/claude-agent-sdk")) as {
    query: (params: {
      prompt: AsyncIterable<SdkUserMessage>;
      options: Record<string, unknown>;
    }) => SdkQueryHandle;
  };
  return sdk.query({ prompt, options });
};

/** Unbounded async FIFO used as the SDK streaming-input channel. */
class AsyncQueue<T> implements AsyncIterable<T> {
  private values: T[] = [];
  private resolvers: Array<(value: IteratorResult<T>) => void> = [];
  private closed = false;

  push(value: T): void {
    if (this.closed) {
      throw new Error("queue is closed");
    }
    const resolver = this.resolvers.shift();
    if (resolver) {
      resolver({ value, done: false });
    } else {
      this.values.push(value);
    }
  }

  close(): void {
    this.closed = true;
    for (const resolver of this.resolvers.splice(0)) {
      resolver({ value: undefined as never, done: true });
    }
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return {
      next: (): Promise<IteratorResult<T>> => {
        if (this.values.length > 0) {
          return Promise.resolve({ value: this.values.shift()!, done: false });
        }
        if (this.closed) {
          return Promise.resolve({ value: undefined as never, done: true });
        }
        return new Promise((resolve) => this.resolvers.push(resolve));
      },
    };
  }
}

type TurnWaiter = {
  resolve: (replyText: string) => void;
  reject: (error: Error) => void;
  settled: boolean;
  timer?: ReturnType<typeof setTimeout>;
};

/** Upper bound on a single remote turn; prevents one hung session from
 * blocking the sidecar forever. Overridable via `turn_timeout_ms`. */
export const DEFAULT_TURN_TIMEOUT_MS = 5 * 60 * 1000;

/** One SDK-owned Claude Code session (started or resumed by the sidecar). */
class OwnedSession {
  readonly input = new AsyncQueue<SdkUserMessage>();
  /** Claude Code session id, learned from the init message. */
  sessionId?: string;
  handle?: SdkQueryHandle;
  lastActivity = Date.now();
  private turnWaiters: TurnWaiter[] = [];
  private turnText: string[] = [];
  private ended = false;

  constructor(readonly key: string) {}

  /** Consume SDK messages, resolving one waiter per completed turn. */
  async run(handle: SdkQueryHandle): Promise<void> {
    this.handle = handle;
    try {
      for await (const message of handle) {
        this.lastActivity = Date.now();
        if (message.type === "system" && message.subtype === "init") {
          this.sessionId = message.session_id ?? this.sessionId;
          continue;
        }
        if (message.type === "assistant") {
          for (const block of message.message?.content ?? []) {
            if (block.type === "text" && block.text) {
              this.turnText.push(block.text);
            }
          }
          continue;
        }
        if (message.type === "result") {
          const reply =
            message.result ?? this.turnText.join("\n").trim();
          this.turnText = [];
          this.settleNextWaiter(reply ?? "");
        }
      }
    } finally {
      this.ended = true;
      const error = new Error("Claude Code session ended");
      for (const waiter of this.turnWaiters.splice(0)) {
        if (!waiter.settled) {
          waiter.settled = true;
          clearTimeout(waiter.timer);
          waiter.reject(error);
        }
      }
    }
  }

  /** Consume exactly one result per waiter in FIFO order. If the front
   * waiter already timed out, drop this reply so a later waiter cannot
   * receive the previous turn's text. */
  private settleNextWaiter(reply: string): void {
    const waiter = this.turnWaiters.shift();
    if (!waiter) {
      return;
    }
    if (waiter.settled) {
      return;
    }
    waiter.settled = true;
    clearTimeout(waiter.timer);
    waiter.resolve(reply);
  }

  isLive(): boolean {
    return !this.ended;
  }

  /** Queue an operator turn and resolve with the assistant's reply text. */
  send(text: string, timeoutMs: number = DEFAULT_TURN_TIMEOUT_MS): Promise<string> {
    if (this.ended) {
      return Promise.reject(new Error("Claude Code session ended"));
    }
    const reply = new Promise<string>((resolve, reject) => {
      const waiter: TurnWaiter = { resolve, reject, settled: false };
      waiter.timer = setTimeout(() => {
        if (!waiter.settled) {
          waiter.settled = true;
          reject(
            new Error(
              `Claude Code turn timed out after ${timeoutMs}ms; ` +
                "the session may still be running",
            ),
          );
        }
      }, timeoutMs);
      // Deliberately NOT unref'd: an in-flight turn is real pending work and
      // the timeout must fire even if nothing else keeps the loop alive.
      this.turnWaiters.push(waiter);
    });
    this.input.push({
      type: "user",
      message: { role: "user", content: text },
      parent_tool_use_id: null,
      session_id: this.sessionId ?? "",
    });
    return reply;
  }

  async interrupt(): Promise<void> {
    if (!this.handle) {
      throw new Error("session has no active SDK handle");
    }
    await this.handle.interrupt();
  }

  close(): void {
    this.input.close();
  }
}

export type SendMessageParams = {
  text: string;
  targetSessionId?: string;
  /** Native Claude session id for SDK `resume` when the envelope has it. */
  resumeSessionId?: string;
  metadata?: Record<string, unknown>;
  /** Create a git worktree for a phone-started parallel session (G9). */
  spawnWorktree?: boolean;
  cwd?: string;
};

/**
 * Registry of sidecar-owned Claude Code sessions.
 *
 * Routing per the design memo (claude-code-remote-control.md §4):
 * - target names an owned live session -> push into its streaming input;
 * - target names a persisted-but-idle session -> resume it via the SDK;
 * - no target -> start a new session in the configured workspace.
 *
 * Interactive TUI sessions become steerable after an explicit takeover:
 * `preloop claude` exits local mode and this manager resumes the same
 * Claude session id through the Agent SDK.
 */
export class SessionManager {
  private sessions = new Map<string, OwnedSession>();

  constructor(
    private readonly config: ControlConfig,
    private readonly queryFactory: QueryFactory = sdkQueryFactory,
  ) {}

  private findBySessionId(sessionId: string): OwnedSession | undefined {
    for (const session of this.sessions.values()) {
      if (
        (session.sessionId === sessionId || session.key === sessionId) &&
        session.isLive()
      ) {
        return session;
      }
    }
    return undefined;
  }

  private sdkOptions(
    resumeSessionId?: string,
    cwd?: string,
  ): Record<string, unknown> {
    const options: Record<string, unknown> = {
      cwd: cwd ?? this.config.workspace_root ?? os.homedir(),
      // Load filesystem settings so the Preloop PreToolUse approval hook
      // installed by `preloop agents onboard --approvals` fires inside owned
      // sessions exactly as it does in the founder's terminal. Approvals are
      // delegated to that hook, NOT reimplemented here.
      settingSources: ["user", "project", "local"],
    };
    if (this.config.permission_mode) {
      options.permissionMode = this.config.permission_mode;
    }
    if (resumeSessionId) {
      options.resume = resumeSessionId;
    }
    return options;
  }

  private async open(
    resumeSessionId?: string,
    cwd?: string,
  ): Promise<OwnedSession> {
    const session = new OwnedSession(resumeSessionId ?? randomUUID());
    const handle = await this.queryFactory({
      prompt: session.input,
      options: this.sdkOptions(resumeSessionId, cwd),
    });
    void session.run(handle).finally(() => {
      this.sessions.delete(session.key);
    });
    this.sessions.set(session.key, session);
    return session;
  }

  /** Deliver an operator message; returns the assistant reply text. */
  async sendMessage(params: SendMessageParams): Promise<string> {
    const target = params.targetSessionId;
    const resume = params.resumeSessionId ?? target;
    let session = target ? this.findBySessionId(target) : undefined;
    if (!session && resume && resume !== target) {
      session = this.findBySessionId(resume);
    }
    if (!session) {
      let cwd = params.cwd;
      if (params.spawnWorktree) {
        cwd = await createGitWorktree(
          cwd ?? this.config.workspace_root ?? process.cwd(),
        );
      }
      // Prefer the native Claude session id for SDK resume when present
      // (G2 envelope field). Fall back to the Preloop UUID.
      session = await this.open(resume, cwd);
    }
    return session.send(
      params.text,
      this.config.turn_timeout_ms ?? DEFAULT_TURN_TIMEOUT_MS,
    );
  }

  /** Close owned SDK sessions so a local `claude --resume` can take the TTY. */
  async release(targetSessionId?: string): Promise<string | undefined> {
    const session = targetSessionId
      ? this.findBySessionId(targetSessionId) ?? this.sessions.get(targetSessionId)
      : this.mostRecentSession();
    const sessionId = session?.sessionId ?? session?.key;
    if (session) {
      try {
        await session.interrupt();
      } catch {
        // already idle
      }
      session.close();
      this.sessions.delete(session.key);
    }
    return sessionId;
  }

  /** Interrupt an owned session. Observed TUI sessions are not interruptible. */
  async interrupt(targetSessionId?: string): Promise<void> {
    const session = targetSessionId
      ? this.findBySessionId(targetSessionId)
      : this.mostRecentSession();
    if (!session) {
      throw new Error(
        "interrupt is only supported for sessions owned by the sidecar; " +
          "interactive terminal sessions must be interrupted locally",
      );
    }
    await session.interrupt();
  }

  private mostRecentSession(): OwnedSession | undefined {
    let latest: OwnedSession | undefined;
    for (const session of this.sessions.values()) {
      if (!session.isLive()) continue;
      if (!latest || session.lastActivity > latest.lastActivity) {
        latest = session;
      }
    }
    return latest;
  }

  ownedSessionIds(): string[] {
    return [...this.sessions.values()]
      .filter((session) => session.isLive())
      .map((session) => session.sessionId ?? session.key);
  }

  stop(): void {
    for (const session of this.sessions.values()) {
      session.close();
    }
    this.sessions.clear();
  }
}

/** G9: isolate a phone-started session in its own git worktree. */
export async function createGitWorktree(repoRoot: string): Promise<string> {
  const dest = path.join(
    os.tmpdir(),
    `preloop-claude-${Date.now()}-${randomUUID().slice(0, 8)}`,
  );
  await execFileAsync("git", ["worktree", "add", "--detach", dest, "HEAD"], {
    cwd: repoRoot,
  });
  return dest;
}
