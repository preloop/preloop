// Codex session layer. The WebSocket sidecar in index.ts is ported from
// runtime-plugins/claude-preloop/src/index.ts; this file is the runtime-
// specific half and is the only place that talks to @openai/codex-sdk.
//
// `CodexClient` is the seam a future `codex app-server` driver can replace.
// The SDK implementation below is the only one shipped today.

import os from "node:os";

import type { ControlConfig } from "./config.js";
import { resolveSandboxMode } from "./config.js";

/** Upper bound on a single remote turn. Overridable via `turn_timeout_ms`. */
export const DEFAULT_TURN_TIMEOUT_MS = 5 * 60 * 1000;

export type CodexUsage = {
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
};

export type CodexTurn = {
  finalResponse: string;
  usage: CodexUsage | null;
};

export type CodexThreadOptions = {
  workingDirectory: string;
  skipGitRepoCheck: boolean;
  sandboxMode: string;
  model?: string;
};

export type CodexRunOptions = {
  signal?: AbortSignal;
};

/** One Codex thread. `id` may be null until the first turn starts. */
export interface CodexThread {
  readonly id: string | null;
  run(input: string, options?: CodexRunOptions): Promise<CodexTurn>;
}

/**
 * What the sidecar needs from a Codex driver.
 * `@openai/codex-sdk` (`Codex.startThread` / `resumeThread` / `Thread.run`)
 * is the current implementation. `codex app-server` can replace it later.
 */
export interface CodexClient {
  startThread(options: CodexThreadOptions): CodexThread;
  resumeThread(id: string, options: CodexThreadOptions): CodexThread;
}

export type CodexClientFactory = (config: ControlConfig) => CodexClient;

export type TurnOutcome = {
  reply_text: string;
  session_id: string;
  usage?: CodexUsage | null;
  /** Set when the in-flight run was aborted by interrupt. */
  stopped?: boolean;
};

export type SendMessageParams = {
  text: string;
  targetSessionId?: string;
  resumeSessionId?: string;
  metadata?: Record<string, unknown>;
  cwd?: string;
  /** Force `startThread` even when the envelope names a session. */
  startNewSession?: boolean;
};

type OwnedThread = {
  key: string;
  thread: CodexThread;
  controller?: AbortController;
  lastActivity: number;
  /** Prior turn on this thread. The next turn waits so two runs cannot overlap. */
  tail?: Promise<void>;
};

/** Options passed to `new Codex(...)`. Approval policy is intentionally absent. */
export function clientOptionsFor(
  config: ControlConfig,
): { codexPathOverride?: string } {
  if (config.codex_path && config.codex_path.trim() !== "") {
    return { codexPathOverride: config.codex_path };
  }
  return {};
}

export function threadOptionsFor(
  config: ControlConfig,
  cwd?: string,
): CodexThreadOptions {
  const options: CodexThreadOptions = {
    workingDirectory: cwd ?? config.workspace_root ?? os.homedir(),
    skipGitRepoCheck: true,
    sandboxMode: resolveSandboxMode(config),
  };
  if (config.codex_model && config.codex_model.trim() !== "") {
    options.model = config.codex_model;
  }
  return options;
}

type LoadableClient = CodexClient & {
  load?: () => Promise<CodexClient>;
};

/** Default factory: lazily import `@openai/codex-sdk`. */
export const sdkCodexClientFactory: CodexClientFactory = (config) => {
  let client: CodexClient | undefined;
  let pending: Promise<CodexClient> | undefined;
  const holder: LoadableClient = {
    startThread(options) {
      if (!client) {
        throw new Error("Codex SDK client is not loaded");
      }
      return client.startThread(options);
    },
    resumeThread(id, options) {
      if (!client) {
        throw new Error("Codex SDK client is not loaded");
      }
      return client.resumeThread(id, options);
    },
    load() {
      pending ??= (async () => {
        if (client) {
          return client;
        }
        const sdk = (await import("@openai/codex-sdk")) as {
          Codex: new (options?: { codexPathOverride?: string }) => {
            startThread(options?: Record<string, unknown>): SdkThread;
            resumeThread(
              id: string,
              options?: Record<string, unknown>,
            ): SdkThread;
          };
        };
        const codex = new sdk.Codex(clientOptionsFor(config));
        client = {
          startThread(options) {
            return wrapThread(codex.startThread(toSdkThreadOptions(options)));
          },
          resumeThread(id, options) {
            return wrapThread(
              codex.resumeThread(id, toSdkThreadOptions(options)),
            );
          },
        };
        return client;
      })();
      return pending;
    },
  };
  return holder;
};

type SdkThread = {
  readonly id: string | null;
  run(
    input: string,
    options?: { signal?: AbortSignal },
  ): Promise<{ finalResponse: string; usage: CodexUsage | null }>;
};

function toSdkThreadOptions(
  options: CodexThreadOptions,
): Record<string, unknown> {
  // Do not set approvalPolicy. Codex's own approval behaviour, including
  // the hook at ~/.codex/hooks.json, stays in force.
  const sdkOptions: Record<string, unknown> = {
    workingDirectory: options.workingDirectory,
    skipGitRepoCheck: options.skipGitRepoCheck,
    sandboxMode: options.sandboxMode,
  };
  if (options.model) {
    sdkOptions.model = options.model;
  }
  return sdkOptions;
}

function wrapThread(thread: SdkThread): CodexThread {
  return {
    get id() {
      return thread.id;
    },
    run(input, options) {
      return thread.run(input, options);
    },
  };
}

/**
 * Load the real SDK client. The sync `CodexClient` surface is what
 * `SessionManager` calls; this awaits the dynamic import once.
 */
export async function ensureSdkClient(
  factory: CodexClientFactory,
  config: ControlConfig,
): Promise<CodexClient> {
  const created = factory(config) as LoadableClient;
  if (typeof created.load === "function") {
    return created.load();
  }
  return created;
}

export class SessionManager {
  private sessions = new Map<string, OwnedThread>();
  private client?: CodexClient;
  private clientPromise?: Promise<CodexClient>;

  constructor(
    private readonly config: ControlConfig,
    private readonly clientFactory: CodexClientFactory = sdkCodexClientFactory,
  ) {}

  private async clientOrLoad(): Promise<CodexClient> {
    if (this.client) {
      return this.client;
    }
    this.clientPromise ??= ensureSdkClient(this.clientFactory, this.config);
    this.client = await this.clientPromise;
    return this.client;
  }

  private find(sessionId: string): OwnedThread | undefined {
    const direct = this.sessions.get(sessionId);
    if (direct) {
      return direct;
    }
    for (const session of this.sessions.values()) {
      if (session.thread.id === sessionId || session.key === sessionId) {
        return session;
      }
    }
    return undefined;
  }

  private remember(session: OwnedThread): void {
    const id = session.thread.id;
    if (id && id !== session.key) {
      this.sessions.delete(session.key);
      session.key = id;
    }
    this.sessions.set(session.key, session);
  }

  /** Deliver an operator message. Returns reply text plus optional usage. */
  async sendMessage(params: SendMessageParams): Promise<TurnOutcome> {
    const target = params.targetSessionId;
    const resume = params.resumeSessionId ?? target;
    const cwd = params.cwd;
    let session: OwnedThread | undefined;
    if (!params.startNewSession && target) {
      session = this.find(target);
    }
    if (!session && !params.startNewSession && resume && resume !== target) {
      session = this.find(resume);
    }
    if (!session) {
      const client = await this.clientOrLoad();
      const options = threadOptionsFor(this.config, cwd);
      const resumeId =
        !params.startNewSession && resume && !this.find(resume)
          ? resume
          : undefined;
      const thread = resumeId
        ? client.resumeThread(resumeId, options)
        : client.startThread(options);
      session = {
        key: thread.id ?? resumeId ?? `pending-${this.sessions.size + 1}`,
        thread,
        lastActivity: Date.now(),
      };
      this.remember(session);
    }
    const timeoutMs = this.config.turn_timeout_ms ?? DEFAULT_TURN_TIMEOUT_MS;
    // One run at a time per thread. A second send_message waits instead of
    // replacing session.controller (which would hide the first turn from
    // interrupt) or calling thread.run twice on a live SDK thread.
    const previous = session.tail ?? Promise.resolve();
    const outcome = previous.then(
      () => this.runTurn(session, params.text, timeoutMs),
      () => this.runTurn(session, params.text, timeoutMs),
    );
    session.tail = outcome.then(
      () => undefined,
      () => undefined,
    );
    return outcome;
  }

  private async runTurn(
    session: OwnedThread,
    text: string,
    timeoutMs: number,
  ): Promise<TurnOutcome> {
    const controller = new AbortController();
    session.controller = controller;
    session.lastActivity = Date.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    try {
      const turn = await new Promise<CodexTurn>((resolve, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          controller.abort();
          reject(
            new Error(
              `Codex turn timed out after ${timeoutMs}ms; ` +
                "the thread may still be running",
            ),
          );
        }, timeoutMs);
        session.thread.run(text, { signal: controller.signal }).then(
          (value) => resolve(value),
          (error: unknown) => reject(error),
        );
      });
      this.remember(session);
      return {
        reply_text: turn.finalResponse ?? "",
        session_id: session.thread.id ?? session.key,
        usage: turn.usage ?? null,
      };
    } catch (error) {
      if (timedOut) {
        throw new Error(
          `Codex turn timed out after ${timeoutMs}ms; ` +
            "the thread may still be running",
        );
      }
      if (controller.signal.aborted && isAbortError(error)) {
        this.remember(session);
        return {
          reply_text: "",
          session_id: session.thread.id ?? session.key,
          stopped: true,
        };
      }
      throw error;
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
      if (session.controller === controller) {
        session.controller = undefined;
      }
    }
  }

  /** Abort the in-flight run on an owned thread. */
  async interrupt(targetSessionId?: string): Promise<void> {
    const session = targetSessionId
      ? this.find(targetSessionId)
      : this.mostRecent();
    if (!session || !session.controller) {
      throw new Error(
        "interrupt is only supported for sessions owned by the sidecar; " +
          "interactive terminal sessions must be interrupted locally",
      );
    }
    session.controller.abort();
  }

  /** Stop an owned thread so a local Codex TUI can resume it. */
  async release(targetSessionId?: string): Promise<string | undefined> {
    const session = targetSessionId
      ? this.find(targetSessionId)
      : this.mostRecent();
    const sessionId = session?.thread.id ?? session?.key;
    if (session) {
      session.controller?.abort();
      this.sessions.delete(session.key);
      if (session.thread.id) {
        this.sessions.delete(session.thread.id);
      }
    }
    return sessionId;
  }

  private mostRecent(): OwnedThread | undefined {
    let latest: OwnedThread | undefined;
    for (const session of this.sessions.values()) {
      if (!latest || session.lastActivity > latest.lastActivity) {
        latest = session;
      }
    }
    return latest;
  }

  ownedSessionIds(): string[] {
    const ids = new Set<string>();
    for (const session of this.sessions.values()) {
      ids.add(session.thread.id ?? session.key);
    }
    return [...ids];
  }

  stop(): void {
    for (const session of this.sessions.values()) {
      session.controller?.abort();
    }
    this.sessions.clear();
  }
}

function isAbortError(error: unknown): boolean {
  if (error instanceof Error) {
    return (
      error.name === "AbortError" ||
      error.message.toLowerCase().includes("abort")
    );
  }
  return false;
}
