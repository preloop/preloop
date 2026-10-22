// Driver for interactive Codex TUI sessions that run on the shared
// `codex app-server` daemon (Codex CLI 0.163.0 and later).
//
// A plain `codex` attaches to the daemon's control socket when it exists
// (and 0.163.0 starts the daemon itself unless `--no-daemon` is given).
// Any second client on that socket can resume a running thread, which
// atomically subscribes it to the thread's updates, and can then start a
// turn on an idle thread, steer the active turn, or interrupt it. The TUI is
// subscribed to the same thread, so it renders the injected input and the
// reply as if they had been typed. See docs/app-server-spike.md.
//
// Wire facts this file depends on (verified on 0.163.0):
// - `codex app-server proxy` is a raw byte pipe to the control socket, and
//   the socket speaks WebSocket. Frames written before the server's
//   `101 Switching Protocols` are dropped, so the client waits for it.
// - `initialize` must opt in to `experimentalApi` for the v2 thread methods.
// - `thread/loaded/list` also lists ephemeral helper threads (title
//   generation); only `thread.ephemeral === false` threads are sessions.
// - A thread that is not loaded is never resumed here: resuming it would
//   load the rollout from disk and make this sidecar a second writer.
//
// Approval requests are fanned out by the daemon to every subscriber. The
// driver never answers them: the TUI shows its own prompt, and Preloop's
// approval workflow stays the authority through the Codex hooks.

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import type {
  CodexClient,
  CodexRunOptions,
  CodexThread,
  CodexThreadOptions,
  CodexTurn,
  CodexUsage,
} from "./sessions.js";

/** Oldest daemon this driver speaks to. Older builds lack `turn/steer`. */
export const MIN_APP_SERVER_VERSION = "0.163.0";
export const DEFAULT_APP_SERVER_POLL_MS = 5_000;
const REQUEST_TIMEOUT_MS = 15_000;

/** A JSON text channel to the daemon (one JSON-RPC message per call). */
export interface AppServerTransport {
  send(text: string): void;
  onMessage(listener: (text: string) => void): void;
  onClose(listener: (reason: string) => void): void;
  close(): void;
}

export type AppServerTransportFactory = () => Promise<AppServerTransport>;

/** Events forwarded to Preloop for a subscribed TUI thread. */
export type AppServerEvent =
  | { kind: "turn_started"; turn_id: string }
  | {
      kind: "turn_completed";
      turn_id: string;
      status: string;
      duration_ms: number | null;
    }
  | { kind: "assistant_delta"; turn_id: string; item_id: string; delta: string }
  | { kind: "status_title"; turn_id: string; delta: string }
  | {
      kind: "tool_start";
      turn_id: string;
      call_id: string;
      tool_name: string;
      command: string;
    }
  | {
      kind: "tool_end";
      turn_id: string;
      call_id: string;
      tool_name: string;
      exit_code: number | null;
      status: string;
      duration_ms: number | null;
    }
  | { kind: "usage"; turn_id: string; usage: CodexUsage };

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toUsage(breakdown: JsonObject | undefined): CodexUsage | undefined {
  if (!breakdown) return undefined;
  return {
    input_tokens: asNumber(breakdown.inputTokens) ?? 0,
    cached_input_tokens: asNumber(breakdown.cachedInputTokens) ?? 0,
    cache_write_input_tokens: asNumber(breakdown.cacheWriteInputTokens) ?? 0,
    output_tokens: asNumber(breakdown.outputTokens) ?? 0,
    reasoning_output_tokens: asNumber(breakdown.reasoningOutputTokens) ?? 0,
  };
}

/**
 * Map one app-server notification to the Preloop event it carries, if any.
 * Returns the thread id with the event so the caller can scope it.
 */
export function mapNotification(
  method: string,
  params: unknown,
): { threadId: string; event: AppServerEvent } | undefined {
  const p = asObject(params);
  const threadId = asString(p?.threadId);
  if (!p || !threadId) return undefined;
  const turnId = asString(p.turnId) ?? asString(asObject(p.turn)?.id) ?? "";
  switch (method) {
    case "turn/started":
      return { threadId, event: { kind: "turn_started", turn_id: turnId } };
    case "turn/completed": {
      const turn = asObject(p.turn);
      return {
        threadId,
        event: {
          kind: "turn_completed",
          turn_id: turnId,
          status: asString(turn?.status) ?? "completed",
          duration_ms: asNumber(turn?.durationMs),
        },
      };
    }
    case "item/agentMessage/delta":
      return {
        threadId,
        event: {
          kind: "assistant_delta",
          turn_id: turnId,
          item_id: asString(p.itemId) ?? "",
          delta: asString(p.delta) ?? "",
        },
      };
    case "item/reasoning/summaryTextDelta":
      return {
        threadId,
        event: { kind: "status_title", turn_id: turnId, delta: asString(p.delta) ?? "" },
      };
    case "item/started":
    case "item/completed": {
      const item = asObject(p.item);
      if (!item || item.type !== "commandExecution") return undefined;
      const callId = asString(item.id) ?? "";
      if (method === "item/started") {
        return {
          threadId,
          event: {
            kind: "tool_start",
            turn_id: turnId,
            call_id: callId,
            tool_name: "exec_command",
            command: asString(item.command) ?? "",
          },
        };
      }
      return {
        threadId,
        event: {
          kind: "tool_end",
          turn_id: turnId,
          call_id: callId,
          tool_name: "exec_command",
          exit_code: asNumber(item.exitCode),
          status: asString(item.status) ?? "completed",
          duration_ms: asNumber(item.durationMs),
        },
      };
    }
    case "thread/tokenUsage/updated": {
      const usage = toUsage(asObject(asObject(p.tokenUsage)?.last));
      if (!usage) return undefined;
      return { threadId, event: { kind: "usage", turn_id: turnId, usage } };
    }
    default:
      return undefined;
  }
}

/** `CODEX_HOME/app-server-control/app-server-control.sock`. */
export function appServerSocketPath(codexHome?: string): string {
  const home = codexHome ?? process.env.CODEX_HOME ?? path.join(os.homedir(), ".codex");
  return path.join(home, "app-server-control", "app-server-control.sock");
}

/** Compare dotted versions numerically. */
export function versionAtLeast(version: string, minimum: string): boolean {
  const parse = (value: string) => value.split(".").map((part) => parseInt(part, 10) || 0);
  const a = parse(version);
  const b = parse(minimum);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const left = a[index] ?? 0;
    const right = b[index] ?? 0;
    if (left !== right) return left > right;
  }
  return true;
}

/**
 * Default transport: `codex app-server proxy` with a minimal WebSocket
 * client over its stdio (text frames only, client frames masked).
 */
export function proxyTransportFactory(
  codexPath = "codex",
  codexHome?: string,
): AppServerTransportFactory {
  return () =>
    new Promise<AppServerTransport>((resolve, reject) => {
      const env = { ...process.env };
      if (codexHome) env.CODEX_HOME = codexHome;
      const child = spawn(codexPath, ["app-server", "proxy"], {
        stdio: ["pipe", "pipe", "ignore"],
        env,
      });
      const messageListeners: ((text: string) => void)[] = [];
      const closeListeners: ((reason: string) => void)[] = [];
      let buffer = Buffer.alloc(0);
      let upgraded = false;
      let settled = false;
      let fragments: Buffer[] = [];
      const fail = (reason: string) => {
        if (!settled) {
          settled = true;
          reject(new Error(reason));
        }
        closeListeners.forEach((listener) => listener(reason));
      };
      const transport: AppServerTransport = {
        send(text) {
          child.stdin.write(encodeClientFrame(text));
        },
        onMessage(listener) {
          messageListeners.push(listener);
        },
        onClose(listener) {
          closeListeners.push(listener);
        },
        close() {
          child.kill();
        },
      };
      child.on("error", (error) => fail(`codex app-server proxy: ${error.message}`));
      child.on("exit", (code) => fail(`codex app-server proxy exited (${String(code)})`));
      child.stdout.on("data", (chunk: Buffer) => {
        buffer = Buffer.concat([buffer, chunk]);
        if (!upgraded) {
          const end = buffer.indexOf("\r\n\r\n");
          if (end < 0) return;
          const head = buffer.subarray(0, end).toString();
          buffer = buffer.subarray(end + 4);
          if (!/^HTTP\/1\.1 101/.test(head)) {
            fail("app-server control socket refused the WebSocket upgrade");
            child.kill();
            return;
          }
          upgraded = true;
          settled = true;
          resolve(transport);
        }
        for (;;) {
          const frame = decodeServerFrame(buffer);
          if (!frame) return;
          buffer = buffer.subarray(frame.consumed);
          if (frame.opcode === 0x8) {
            child.kill();
            return;
          }
          if (frame.opcode === 0x9 || frame.opcode === 0xa) continue;
          fragments.push(frame.payload);
          if (!frame.fin) continue;
          const text = Buffer.concat(fragments).toString("utf8");
          fragments = [];
          messageListeners.forEach((listener) => listener(text));
        }
      });
      const key = randomBytes(16).toString("base64");
      child.stdin.write(
        "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n" +
          `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\n` +
          "Sec-WebSocket-Version: 13\r\n\r\n",
      );
    });
}

export function encodeClientFrame(text: string): Buffer {
  const payload = Buffer.from(text, "utf8");
  const mask = randomBytes(4);
  let header: Buffer;
  if (payload.length < 126) {
    header = Buffer.from([0x81, 0x80 | payload.length]);
  } else if (payload.length < 65_536) {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(payload.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = 0x80 | 127;
    header.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  const masked = Buffer.alloc(payload.length);
  for (let index = 0; index < payload.length; index += 1) {
    masked[index] = payload[index] ^ mask[index % 4];
  }
  return Buffer.concat([header, mask, masked]);
}

export function decodeServerFrame(
  buffer: Buffer,
): { fin: boolean; opcode: number; payload: Buffer; consumed: number } | undefined {
  if (buffer.length < 2) return undefined;
  const fin = (buffer[0] & 0x80) !== 0;
  const opcode = buffer[0] & 0x0f;
  const masked = (buffer[1] & 0x80) !== 0;
  let length = buffer[1] & 0x7f;
  let offset = 2;
  if (length === 126) {
    if (buffer.length < 4) return undefined;
    length = buffer.readUInt16BE(2);
    offset = 4;
  } else if (length === 127) {
    if (buffer.length < 10) return undefined;
    length = Number(buffer.readBigUInt64BE(2));
    offset = 10;
  }
  const maskKey = masked ? buffer.subarray(offset, offset + 4) : undefined;
  if (masked) offset += 4;
  if (buffer.length < offset + length) return undefined;
  const payload = Buffer.from(buffer.subarray(offset, offset + length));
  if (maskKey) {
    for (let index = 0; index < payload.length; index += 1) {
      payload[index] ^= maskKey[index % 4];
    }
  }
  return { fin, opcode, payload, consumed: offset + length };
}

type Pending = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};

type TurnWaiter = {
  turnId: string;
  resolve: (turn: CodexTurn) => void;
  reject: (error: Error) => void;
};

type ThreadState = {
  subscribed: boolean;
  activeTurnId?: string;
  waiters: TurnWaiter[];
  /** Reply text and usage seen per turn, so a fast turn is never lost. */
  turns: Map<string, { text: string; usage: CodexUsage | null; done: boolean }>;
};

const MAX_REMEMBERED_TURNS = 20;

export type AppServerDriverOptions = {
  transportFactory?: AppServerTransportFactory;
  socketExists?: () => boolean;
  codexPath?: string;
  codexHome?: string;
  pollMs?: number;
  onEvent?: (threadId: string, event: AppServerEvent) => void;
  onReachableChanged?: (ids: string[]) => void;
  log?: (message: string) => void;
};

/**
 * One connection to the shared daemon, the set of TUI threads it can reach,
 * and turn delivery into them. Implements `CodexClient` so the sidecar can
 * treat a reachable TUI thread like any other thread.
 */
export class AppServerDriver implements CodexClient {
  private transport?: AppServerTransport;
  private connecting?: Promise<AppServerTransport>;
  private nextId = 0;
  private pending = new Map<number, Pending>();
  private threads = new Map<string, ThreadState>();
  private reachable: string[] = [];
  private pollTimer?: ReturnType<typeof setInterval>;
  private readonly transportFactory: AppServerTransportFactory;
  private readonly socketExists: () => boolean;
  serverVersion?: string;

  constructor(private readonly options: AppServerDriverOptions = {}) {
    this.transportFactory =
      options.transportFactory ?? proxyTransportFactory(options.codexPath, options.codexHome);
    const socketPath = appServerSocketPath(options.codexHome);
    this.socketExists = options.socketExists ?? (() => fs.existsSync(socketPath));
  }

  /** Late-bind the sidecar callbacks (the driver may be built first). */
  setListeners(listeners: Pick<AppServerDriverOptions, "onEvent" | "onReachableChanged">): void {
    if (listeners.onEvent) this.options.onEvent = listeners.onEvent;
    if (listeners.onReachableChanged) this.options.onReachableChanged = listeners.onReachableChanged;
  }

  /** Native thread ids of TUI sessions this driver can type into. */
  reachableThreadIds(): string[] {
    return [...this.reachable];
  }

  isReachable(threadId: string | undefined): boolean {
    return !!threadId && this.reachable.includes(threadId);
  }

  /** Whether the thread has a turn in flight (as last seen on the wire). */
  activeTurnId(threadId: string): string | undefined {
    return this.threads.get(threadId)?.activeTurnId;
  }

  start(): void {
    if (this.pollTimer) return;
    void this.refresh();
    this.pollTimer = setInterval(
      () => void this.refresh(),
      this.options.pollMs ?? DEFAULT_APP_SERVER_POLL_MS,
    );
  }

  stop(): void {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = undefined;
    this.disconnect("stopped");
  }

  /** Re-list loaded, non-ephemeral threads. Never throws. */
  async refresh(): Promise<string[]> {
    let ids: string[] = [];
    try {
      if (this.socketExists()) {
        ids = await this.listSessionThreads();
      } else {
        this.disconnect("daemon socket missing");
      }
    } catch (error) {
      this.options.log?.(`codex app-server: ${errorText(error)}`);
      this.disconnect(errorText(error));
      ids = [];
    }
    this.setReachable(ids);
    return ids;
  }

  private setReachable(ids: string[]): void {
    const changed =
      ids.length !== this.reachable.length || ids.some((id, i) => id !== this.reachable[i]);
    this.reachable = ids;
    if (changed) this.options.onReachableChanged?.([...ids]);
  }

  private async listSessionThreads(): Promise<string[]> {
    const ids: string[] = [];
    let cursor: string | null = null;
    do {
      const page = asObject(
        await this.request("thread/loaded/list", cursor ? { cursor } : {}),
      );
      const data = Array.isArray(page?.data) ? page.data : [];
      for (const id of data) {
        if (typeof id !== "string") continue;
        const read = asObject(await this.request("thread/read", { threadId: id }));
        const thread = asObject(read?.thread);
        if (thread && thread.ephemeral === false) ids.push(id);
      }
      cursor = asString(page?.nextCursor) ?? null;
    } while (cursor);
    return ids;
  }

  private async connection(): Promise<AppServerTransport> {
    if (this.transport) return this.transport;
    this.connecting ??= (async () => {
      const transport = await this.transportFactory();
      transport.onMessage((text) => this.handleMessage(text));
      transport.onClose((reason) => {
        if (this.transport === transport) this.disconnect(reason);
      });
      this.transport = transport;
      try {
        const init = asObject(
          await this.request("initialize", {
            clientInfo: { name: "preloop_codex_sidecar", title: null, version: "0.1.0" },
            capabilities: { experimentalApi: true },
          }),
        );
        const agent = asString(init?.userAgent) ?? "";
        const match = /\/(\d+\.\d+\.\d+)/.exec(agent);
        this.serverVersion = match?.[1];
        if (!this.serverVersion || !versionAtLeast(this.serverVersion, MIN_APP_SERVER_VERSION)) {
          throw new Error(
            `codex app-server ${this.serverVersion ?? "unknown"} is older than ${MIN_APP_SERVER_VERSION}`,
          );
        }
        transport.send(JSON.stringify({ jsonrpc: "2.0", method: "initialized", params: {} }));
        return transport;
      } catch (error) {
        this.disconnect(errorText(error));
        throw error;
      }
    })().finally(() => {
      this.connecting = undefined;
    });
    return this.connecting;
  }

  private disconnect(reason: string): void {
    const transport = this.transport;
    this.transport = undefined;
    transport?.close();
    for (const [, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error(reason));
    }
    this.pending.clear();
    for (const state of this.threads.values()) {
      state.subscribed = false;
      for (const waiter of state.waiters) waiter.reject(new Error(reason));
      state.waiters = [];
    }
  }

  private async request(method: string, params: unknown): Promise<unknown> {
    const transport =
      method === "initialize" ? this.transport! : await this.connection();
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, REQUEST_TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timer });
      transport.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }

  private handleMessage(text: string): void {
    let message: JsonObject;
    try {
      message = JSON.parse(text) as JsonObject;
    } catch {
      return;
    }
    const method = asString(message.method);
    if (!method && typeof message.id === "number") {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      const error = asObject(message.error);
      if (error) pending.reject(new Error(asString(error.message) ?? "app-server error"));
      else pending.resolve(message.result);
      return;
    }
    // Server requests (approvals) carry an id and a method. They are left to
    // the TUI and the Codex hooks; see the header comment.
    if (!method || message.id !== undefined) return;
    this.trackTurns(method, message.params);
    const mapped = mapNotification(method, message.params);
    if (mapped && this.threads.get(mapped.threadId)?.subscribed) {
      this.collect(mapped.threadId, mapped.event);
      this.options.onEvent?.(mapped.threadId, mapped.event);
    }
  }

  private trackTurns(method: string, params: unknown): void {
    const p = asObject(params);
    const threadId = asString(p?.threadId);
    if (!threadId) return;
    const state = this.threads.get(threadId);
    if (!state) return;
    if (method === "turn/started") {
      state.activeTurnId = asString(asObject(p?.turn)?.id);
    } else if (method === "turn/completed") {
      state.activeTurnId = undefined;
    } else if (method === "item/completed") {
      const item = asObject(p?.item);
      const turnId = asString(p?.turnId);
      if (item?.type === "agentMessage" && turnId) {
        this.turnRecord(state, turnId).text = asString(item.text) ?? "";
      }
    }
  }

  private turnRecord(state: ThreadState, turnId: string) {
    let record = state.turns.get(turnId);
    if (!record) {
      record = { text: "", usage: null, done: false };
      state.turns.set(turnId, record);
      while (state.turns.size > MAX_REMEMBERED_TURNS) {
        state.turns.delete(state.turns.keys().next().value as string);
      }
    }
    return record;
  }

  private collect(threadId: string, event: AppServerEvent): void {
    const state = this.threads.get(threadId);
    if (!state) return;
    if (event.kind === "usage") {
      this.turnRecord(state, event.turn_id).usage = event.usage;
    } else if (event.kind === "turn_completed") {
      const record = this.turnRecord(state, event.turn_id);
      record.done = true;
      const done = state.waiters.filter((waiter) => waiter.turnId === event.turn_id);
      state.waiters = state.waiters.filter((waiter) => waiter.turnId !== event.turn_id);
      for (const waiter of done) {
        waiter.resolve({ finalResponse: record.text, usage: record.usage });
      }
    }
  }

  /** Subscribe to a loaded thread once (`thread/resume` of a running thread). */
  async subscribe(threadId: string): Promise<void> {
    if (!this.isReachable(threadId)) {
      throw new Error(
        `Codex thread ${threadId} is not loaded on the shared app-server daemon`,
      );
    }
    const state: ThreadState =
      this.threads.get(threadId) ?? { subscribed: false, waiters: [], turns: new Map() };
    this.threads.set(threadId, state);
    if (state.subscribed) return;
    const result = asObject(
      await this.request("thread/resume", { threadId, excludeTurns: true }),
    );
    const status = asObject(asObject(result?.thread)?.status);
    if (status?.type !== "active") state.activeTurnId = undefined;
    state.subscribed = true;
  }

  /**
   * Deliver one line to a TUI thread: `turn/steer` when a turn is running,
   * `turn/start` when it is idle. Resolves when that turn completes.
   */
  async sendTurn(
    threadId: string,
    text: string,
    signal?: AbortSignal,
  ): Promise<CodexTurn & { turnId: string; steered: boolean }> {
    await this.subscribe(threadId);
    const state = this.threads.get(threadId)!;
    const input = [{ type: "text", text, text_elements: [] }];
    let turnId: string;
    let steered = false;
    if (state.activeTurnId) {
      const result = asObject(
        await this.request("turn/steer", {
          threadId,
          input,
          expectedTurnId: state.activeTurnId,
        }),
      );
      turnId = asString(result?.turnId) ?? state.activeTurnId;
      steered = true;
    } else {
      const result = asObject(await this.request("turn/start", { threadId, input }));
      turnId = asString(asObject(result?.turn)?.id) ?? "";
      if (!turnId) throw new Error("turn/start returned no turn id");
      state.activeTurnId = turnId;
    }
    const turn = await new Promise<CodexTurn>((resolve, reject) => {
      const seen = state.turns.get(turnId);
      if (seen?.done) {
        resolve({ finalResponse: seen.text, usage: seen.usage });
        return;
      }
      state.waiters.push({ turnId, resolve, reject });
      signal?.addEventListener(
        "abort",
        () => {
          void this.interrupt(threadId).catch(() => undefined);
        },
        { once: true },
      );
    });
    return { ...turn, turnId, steered };
  }

  /** Interrupt the active turn of a TUI thread. */
  async interrupt(threadId: string): Promise<void> {
    await this.subscribe(threadId);
    const turnId = this.threads.get(threadId)?.activeTurnId;
    if (!turnId) throw new Error("the Codex session has no turn in progress");
    await this.request("turn/interrupt", { threadId, turnId });
  }

  startThread(_options: CodexThreadOptions): CodexThread {
    throw new Error("the app-server driver only drives running TUI threads");
  }

  resumeThread(id: string, _options: CodexThreadOptions): CodexThread {
    return {
      id,
      run: async (input: string, runOptions?: CodexRunOptions) => {
        const turn = await this.sendTurn(id, input, runOptions?.signal);
        return { finalResponse: turn.finalResponse, usage: turn.usage };
      },
    };
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
