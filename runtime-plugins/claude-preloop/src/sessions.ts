import { SessionRegistrar } from "./session-registration.js";
import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { randomUUID } from "node:crypto";

import type { ControlConfig } from "./config.js";
import { selectTurnTimeoutMs } from "./flow-workspace.js";
import { ClaudeExecutable, resolveClaudeExecutable } from "./claude-binary.js";

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
  /** Set on a result message when the turn ended on an error. */
  is_error?: boolean;
  errors?: string[];
  usage?: { input_tokens?: number; output_tokens?: number };
  total_cost_usd?: number;
  message?: {
    content?: Array<{
      type: string;
      text?: string;
      id?: string;
      name?: string;
    }>;
  };
};

/** Token and cost totals from one SDK result message. */
export type TurnUsage = {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens: number;
  total_cost_usd?: number;
};

export type TurnOutcome = {
  text: string;
  usage?: TurnUsage;
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

export type TurnProgress = {
  kind: "assistant" | "tool" | "summary";
  summary: string;
  call_id?: string;
  tool_name?: string;
};

type TurnWaiter = {
  onProgress?: (event: TurnProgress) => void;
  progressCount: number;
  resolve: (outcome: TurnOutcome) => void;
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
  /** Aborting it makes the SDK kill the Claude Code child process. */
  readonly abort = new AbortController();

  constructor(
    readonly key: string,
    private registrar: SessionRegistrar,
    private cwd?: string,
  ) {}

  /** Consume SDK messages, resolving one waiter per completed turn. */
  async run(handle: SdkQueryHandle): Promise<void> {
    this.handle = handle;
    try {
      for await (const message of handle) {
        this.lastActivity = Date.now();
        if (message.type === "system" && message.subtype === "init") {
          this.sessionId = message.session_id ?? this.sessionId;
          // Never block the message stream on the registration request.
          void this.registrar.register(this.sessionId, this.cwd);
          continue;
        }
        if (message.type === "assistant") {
          for (const block of message.message?.content ?? []) {
            if (block.type === "text" && block.text) {
              this.turnText.push(block.text);
              this.progress({
                kind: "assistant",
                summary: block.text.slice(0, 1000),
              });
            } else if (block.type === "tool_use" && block.id && block.name) {
              this.progress({
                kind: "tool",
                summary: `Tool: ${block.name.slice(0, 100)}`,
                call_id: block.id,
                tool_name: block.name.slice(0, 100),
              });
            }
          }
          continue;
        }
        if (message.type === "result") {
          const reply = message.result ?? this.turnText.join("\n").trim();
          this.turnText = [];
          if (resultIsError(message)) {
            this.failNextWaiter(
              new Error(`Claude Code turn failed: ${resultErrorText(message)}`),
            );
          } else {
            this.settleNextWaiter(reply ?? "", readTurnUsage(message));
          }
          // The turn's end time for the live activity board (#1889).
          void this.registrar.recordEvent(this.sessionId, "Stop");
        }
      }
    } catch (error) {
      if (!this.abort.signal.aborted) {
        this.endError =
          error instanceof Error ? error : new Error(String(error));
      }
    } finally {
      this.ended = true;
      void this.registrar.recordEvent(this.sessionId, "SessionEnd");
      const error = this.endError
        ? new Error(`Claude Code session ended: ${this.endError.message}`)
        : new Error("Claude Code session ended");
      for (const waiter of this.turnWaiters.splice(0)) {
        if (!waiter.settled) {
          waiter.settled = true;
          clearTimeout(waiter.timer);
          waiter.reject(error);
        }
      }
    }
  }

  private progress(event: TurnProgress): void {
    const waiter = this.turnWaiters[0];
    if (waiter && !waiter.settled && waiter.progressCount++ < 200) {
      waiter.onProgress?.(event);
    }
  }

  /** Consume exactly one result per waiter in FIFO order. If the front
   * waiter already timed out, drop this reply so a later waiter cannot
   * receive the previous turn's text. */
  private settleNextWaiter(reply: string, usage?: TurnUsage): void {
    const waiter = this.turnWaiters.shift();
    if (!waiter) {
      return;
    }
    if (waiter.settled) {
      return;
    }
    waiter.settled = true;
    clearTimeout(waiter.timer);
    waiter.resolve(usage ? { text: reply, usage } : { text: reply });
  }

  private endError?: Error;

  private failNextWaiter(error: Error): void {
    const waiter = this.turnWaiters.shift();
    if (!waiter || waiter.settled) {
      return;
    }
    waiter.settled = true;
    clearTimeout(waiter.timer);
    waiter.reject(error);
  }

  /** Stop the session for good: the SDK kills the Claude Code child. */
  terminate(): void {
    this.ended = true;
    this.input.close();
    this.abort.abort();
  }

  isLive(): boolean {
    return !this.ended;
  }

  /** Queue an operator turn and resolve with the assistant reply. */
  send(
    text: string,
    timeoutMs: number = DEFAULT_TURN_TIMEOUT_MS,
    onProgress?: (event: TurnProgress) => void,
  ): Promise<TurnOutcome> {
    if (this.ended) {
      return Promise.reject(new Error("Claude Code session ended"));
    }
    const reply = new Promise<TurnOutcome>((resolve, reject) => {
      const waiter: TurnWaiter = {
        resolve,
        reject,
        settled: false,
        onProgress,
        progressCount: 0,
      };
      waiter.timer = setTimeout(() => {
        if (!waiter.settled) {
          waiter.settled = true;
          reject(
            new Error(
              `Claude Code turn timed out after ${timeoutMs}ms; ` +
                "the session was stopped",
            ),
          );
          // A hung turn would otherwise keep the Claude Code child alive.
          this.terminate();
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

export type SessionManagerOptions = {
  /** Line logger; defaults to stderr. */
  log?: (line: string) => void;
  /** Which Claude Code binary to run; tests replace it. */
  resolveExecutable?: () => Promise<ClaudeExecutable>;
};

function finiteNonNegative(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return undefined;
  }
  return value;
}

/** Token and cost fields a flow execution can settle from one result. */
export function readTurnUsage(message: SdkMessage): TurnUsage | undefined {
  const input = finiteNonNegative(message.usage?.input_tokens);
  const output = finiteNonNegative(message.usage?.output_tokens);
  const cost = finiteNonNegative(message.total_cost_usd);
  if (input === undefined && output === undefined && cost === undefined) {
    return undefined;
  }
  const usage: TurnUsage = { total_tokens: (input ?? 0) + (output ?? 0) };
  if (input !== undefined) {
    usage.input_tokens = input;
  }
  if (output !== undefined) {
    usage.output_tokens = output;
  }
  if (cost !== undefined) {
    usage.total_cost_usd = cost;
  }
  return usage;
}

/** A result message that ended its turn on an error. */
function resultIsError(message: SdkMessage): boolean {
  return (
    message.is_error === true ||
    (typeof message.subtype === "string" && message.subtype.startsWith("error"))
  );
}

function resultErrorText(message: SdkMessage): string {
  const text =
    (typeof message.result === "string" && message.result.trim()) ||
    (Array.isArray(message.errors) && message.errors.join("; ")) ||
    message.subtype ||
    "unknown error";
  return text.slice(0, 2000);
}

export type SendMessageParams = {
  onProgress?: (event: TurnProgress) => void;
  text: string;
  targetSessionId?: string;
  /** Native Claude session id for SDK `resume` when the envelope has it. */
  resumeSessionId?: string;
  metadata?: Record<string, unknown>;
  /** Create a git worktree for a phone-started parallel session (G9). */
  spawnWorktree?: boolean;
  cwd?: string;
  /** Model for a new session. The host default applies when absent. */
  model?: string;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Flow execution a new session runs for. Exported to the Claude Code child
 * as PRELOOP_FLOW_EXECUTION_ID so the Preloop permission hook can apply that
 * flow's approval policy to this session only.
 */
export function flowExecutionId(
  metadata: Record<string, unknown> | undefined,
): string | undefined {
  if (metadata?.["source"] !== "flow_execution") {
    return undefined;
  }
  const value = metadata["flow_execution_id"];
  return typeof value === "string" && UUID_PATTERN.test(value)
    ? value
    : undefined;
}

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
  private registrar: SessionRegistrar;

  private readonly log: (line: string) => void;
  private readonly resolveExecutable: () => Promise<ClaudeExecutable>;
  private executable?: Promise<ClaudeExecutable>;

  constructor(
    private readonly config: ControlConfig,
    private readonly queryFactory: QueryFactory = sdkQueryFactory,
    options: SessionManagerOptions = {},
  ) {
    this.registrar = new SessionRegistrar(config, "claude_code");
    this.log =
      options.log ?? ((line: string) => process.stderr.write(`${line}\n`));
    this.resolveExecutable =
      options.resolveExecutable ??
      (queryFactory === sdkQueryFactory
        ? () => resolveClaudeExecutable(config)
        : async () =>
            config.claude_executable
              ? { path: config.claude_executable, source: "config" as const }
              : { source: "bundled" as const });
  }

  /** The Claude Code binary owned sessions run, resolved once. */
  claudeExecutable(): Promise<ClaudeExecutable> {
    // A bad claude_executable fails every turn with its reason instead of
    // silently running another binary; it is resolved again next time.
    this.executable ??= this.resolveExecutable().then(
      (executable) => {
        this.log(
          `claude binary=${executable.path ?? "bundled"} version=${executable.version ?? "unknown"} source=${executable.source}`,
        );
        if (executable.warning) {
          this.log(`warning: ${executable.warning}`);
        }
        return executable;
      },
      (error: unknown) => {
        this.executable = undefined;
        throw error;
      },
    );
    return this.executable;
  }

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
    model?: string,
    executable?: ClaudeExecutable,
    abortController?: AbortController,
    executionId?: string,
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
    if (model) {
      options.model = model;
    }
    if (executable?.path) {
      options.pathToClaudeCodeExecutable = executable.path;
    }
    if (abortController) {
      options.abortController = abortController;
    }
    const env: Record<string, string | undefined> = { ...process.env };
    delete env.PRELOOP_FLOW_EXECUTION_ID;
    if (executionId) {
      env.PRELOOP_FLOW_EXECUTION_ID = executionId;
    }
    options.env = env;
    return options;
  }

  private async open(
    resumeSessionId?: string,
    cwd?: string,
    model?: string,
    executionId?: string,
  ): Promise<OwnedSession> {
    const session = new OwnedSession(
      resumeSessionId ?? randomUUID(),
      this.registrar,
      cwd ?? this.config.workspace_root,
    );
    const executable = await this.claudeExecutable();
    const handle = await this.queryFactory({
      prompt: session.input,
      options: this.sdkOptions(
        resumeSessionId,
        cwd,
        model,
        executable,
        session.abort,
        executionId,
      ),
    });
    void session.run(handle).finally(() => {
      this.sessions.delete(session.key);
    });
    this.sessions.set(session.key, session);
    return session;
  }

  /** Deliver an operator message; returns the assistant reply text. */
  async sendMessage(params: SendMessageParams): Promise<string> {
    return (await this.completeTurn(params)).text;
  }

  /** Deliver one turn and return its text plus any SDK usage totals. */
  async completeTurn(params: SendMessageParams): Promise<TurnOutcome> {
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
      session = await this.open(
        resume,
        cwd,
        params.model,
        flowExecutionId(params.metadata),
      );
    }
    const timeoutMs = selectTurnTimeoutMs(
      this.config,
      params.metadata,
      DEFAULT_TURN_TIMEOUT_MS,
    );
    const executable = await this.claudeExecutable();
    const binary = `${executable.path ?? "bundled"} version=${executable.version ?? "unknown"}`;
    const started = Date.now();
    const owned = session;
    const label = () => owned.sessionId ?? owned.key;
    const modelLabel = params.model ?? "default";
    const executionLabel = flowExecutionId(params.metadata) ?? "-";
    this.log(
      `turn start session=${label()} model=${modelLabel} execution_id=${executionLabel} binary=${binary} timeout_ms=${timeoutMs}`,
    );
    try {
      const reply = await session.send(
        params.text,
        timeoutMs,
        params.onProgress,
      );
      params.onProgress?.({
        kind: "summary",
        summary: `Turn completed in ${Date.now() - started}ms; tokens: ${reply.usage?.total_tokens ?? "unknown"}`,
      });
      this.log(
        `turn end session=${label()} outcome=completed duration_ms=${Date.now() - started} model=${modelLabel} execution_id=${executionLabel} binary=${binary}`,
      );
      return reply;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const outcome = /timed out/.test(message) ? "timeout" : "failed";
      params.onProgress?.({
        kind: "summary",
        summary: `Turn ${outcome} after ${Date.now() - started}ms`,
      });
      this.log(
        `turn error session=${label()} outcome=${outcome} duration_ms=${Date.now() - started} model=${modelLabel} execution_id=${executionLabel} binary=${binary} error=${JSON.stringify(message.slice(0, 500))}`,
      );
      if (!owned.isLive()) {
        this.sessions.delete(owned.key);
      }
      throw error;
    }
  }

  /** Close owned SDK sessions so a local `claude --resume` can take the TTY. */
  async release(targetSessionId?: string): Promise<string | undefined> {
    const session = targetSessionId
      ? (this.findBySessionId(targetSessionId) ??
        this.sessions.get(targetSessionId))
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
