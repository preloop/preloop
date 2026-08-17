#!/usr/bin/env node

// Preloop Agent Control sidecar for Claude Code.
//
// Claude Code has no in-process plugin registry (unlike Hermes/OpenClaw), so
// this runs as a long-lived sidecar daemon: it owns the Agent Control
// WebSocket and drives Claude Code through the Agent SDK for sessions it
// starts/resumes, while interactive terminal sessions are observed (presence
// + telemetry) via their JSONL transcripts. Tool approvals stay on the
// existing PreToolUse hook path installed by `preloop agents onboard
// --approvals`; the sidecar never reimplements them, so its absence never
// ungoverns anything.
//
// Design memo: factory/roadmap/claude-code-remote-control.md §4 (issue #131).

import { randomUUID } from "node:crypto";

import WebSocket from "ws";

import {
  ControlConfig,
  PROTOCOL,
  RUNTIME,
  defaultTranscriptDir,
  loadConfig,
  verifyConfig,
} from "./config.js";
import { QueryFactory, SessionManager, sdkQueryFactory } from "./sessions.js";
import { SessionActivity, TranscriptObserver } from "./observer.js";

export { ControlConfig, loadConfig, verifyConfig } from "./config.js";
export { SessionManager } from "./sessions.js";
export type { QueryFactory, SdkQueryHandle, SdkMessage, SdkUserMessage } from "./sessions.js";
export { TranscriptObserver } from "./observer.js";
export type { SessionActivity } from "./observer.js";

export type OperatorCommand = {
  message_id?: string;
  type?: string;
  name?: string;
  payload?: {
    text?: string;
    message?: string;
    input_mode?: string;
    metadata?: Record<string, unknown>;
    interrupt?: boolean;
    target_session_id?: string;
    session_source_id?: string;
    session_reference?: string;
    runtime_session_id?: string;
  };
};

/** Headers the Agent Control WS already accepts (Authorization: Bearer). */
export function controlAuthHeaders(token: string): { Authorization: string } {
  return { Authorization: `Bearer ${token}` };
}

/** Reconnect backoff bounds and heartbeat cadence (mirror the other plugins). */
const RECONNECT_BASE_DELAY_MS = 2_000;
const RECONNECT_MAX_DELAY_MS = 30_000;
const HEARTBEAT_INTERVAL_MS = 30_000;
/** Bound on the message_id dedupe memory. */
const DEDUPE_CAPACITY = 1_000;

export class PreloopClaudeSidecar {
  readonly runtime = RUNTIME;
  private controlConfig?: ControlConfig;
  private socket?: WebSocket;
  private sessions?: SessionManager;
  private observer?: TranscriptObserver;
  private stopped = false;
  private reconnectAttempts = 0;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private heartbeatTimer?: ReturnType<typeof setInterval>;
  private seenMessageIds = new Set<string>();
  private logger: (message: string) => void = () => {};

  constructor(
    private readonly configPath?: string,
    private readonly queryFactory: QueryFactory = sdkQueryFactory,
  ) {}

  setLogger(logger: (message: string) => void): void {
    this.logger = logger;
  }

  private log(message: string): void {
    this.logger(message);
  }

  configure(config: ControlConfig): void {
    this.controlConfig = config;
  }

  verify(): ControlConfig {
    const config = this.controlConfig ?? loadConfig(this.configPath);
    verifyConfig(config);
    this.controlConfig = config;
    return config;
  }

  async start(): Promise<void> {
    this.stopped = false;
    const config = this.verify();
    if (config.enabled === false) {
      throw new Error("preloop-control is disabled (enabled=false)");
    }
    this.sessions ??= new SessionManager(config, this.queryFactory);
    if (config.observer_enabled !== false) {
      this.observer = new TranscriptObserver(
        config.transcript_dir ?? defaultTranscriptDir(),
        (activity) => this.sendSessionActivity(activity),
        config.observer_poll_ms,
      );
      this.observer.start();
    }
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    this.stopHeartbeat();
    this.observer?.stop();
    this.observer = undefined;
    this.sessions?.stop();
    this.sessions = undefined;
    this.socket?.close();
    this.socket = undefined;
  }

  private connect(): void {
    if (this.stopped) {
      return;
    }
    const config = this.controlConfig!;
    const wsUrl = new URL(config.control_ws_url!);
    // Node's global WebSocket cannot set headers. The `ws` package can, so
    // the durable bearer token is sent as Authorization: Bearer on the HTTP
    // upgrade. That is the scheme Agent Control already prefers; the token
    // stays out of the URL and out of access-log query strings.

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl, {
        headers: controlAuthHeaders(config.bearer_token!),
      });
    } catch (error) {
      this.log(
        `Preloop Agent Control connect failed: ${errorMessage(error)}`,
      );
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.on("open", () => {
      this.reconnectAttempts = 0;
      this.sendEnvelope({
        type: "presence",
        name: "capabilities",
        message_id: randomUUID(),
        payload: {
          status: "online",
          protocol: PROTOCOL,
          runtime: this.runtime,
          capabilities: {
            new_session: true,
            existing_session: true,
            text: true,
            voice: true,
            interrupt: true,
            // Delegated to the PreToolUse permission hook, not the sidecar.
            tool_approval: true,
          },
          runtime_principal_id: config.runtime_principal_id,
          runtime_principal_name: config.runtime_principal_name,
        },
      });
      this.startHeartbeat(config);
    });

    socket.on("message", (data) => {
      void this.handleFrame(socket, websocketDataToString(data));
    });

    socket.on("close", () => {
      this.stopHeartbeat();
      if (this.socket === socket) {
        this.socket = undefined;
      }
      this.scheduleReconnect();
    });
    socket.on("error", () => {
      // 'error' is followed by 'close'; log and let close drive reconnect.
      this.log("Preloop Agent Control websocket error");
    });
  }

  private async handleFrame(socket: WebSocket, data: string): Promise<void> {
    let command: OperatorCommand;
    try {
      command = JSON.parse(data) as OperatorCommand;
    } catch (error) {
      this.sendOn(socket, {
        type: "status",
        name: "command_error",
        payload: {
          status: "failed",
          error: `invalid_json: ${errorMessage(error)}`,
        },
      });
      return;
    }
    // Redelivered commands (reconnect replay) must not run twice; the
    // persisted-command contract says dedupe on message_id and re-ack.
    if (command.message_id && this.seenMessageIds.has(command.message_id)) {
      this.sendOn(socket, {
        type: "status",
        name: "command_ack",
        message_id: command.message_id,
        payload: {
          command_id: command.message_id,
          status: "duplicate",
        },
      });
      return;
    }
    this.rememberMessageId(command.message_id);
    try {
      const result = await this.dispatch(command);
      this.sendOn(socket, {
        type: "status",
        name: "command_result",
        message_id: command.message_id,
        payload: {
          command_id: command.message_id,
          status: "completed",
          result,
          reply_text: typeof result === "string" ? result : "",
        },
      });
    } catch (error) {
      this.sendOn(socket, {
        type: "status",
        name: "command_error",
        message_id: command.message_id,
        payload: {
          command_id: command.message_id,
          status: "failed",
          error: errorMessage(error),
        },
      });
    }
  }

  /** Execute one operator command envelope. Exposed for tests. */
  async dispatch(command: OperatorCommand): Promise<unknown> {
    if (command.type !== "command" || command.name !== "send_message") {
      return undefined;
    }
    if (!this.sessions) {
      this.sessions = new SessionManager(this.verify(), this.queryFactory);
    }
    const payload = command.payload ?? {};
    const targetSessionId = resolveTargetSessionId(payload);
    const resumeSessionId = resolveResumeSessionId(payload);

    if (payload.interrupt) {
      await this.sessions.interrupt(targetSessionId ?? resumeSessionId);
      return "interrupted";
    }

    // Voice transcripts are delivered as text turns (same as hermes):
    // operator text is an auditable user turn, never a hidden system prompt.
    const text = payload.text ?? payload.message ?? "";
    if (!text.trim()) {
      throw new Error("send_message requires non-empty text");
    }
    return this.sessions.sendMessage({
      text,
      targetSessionId,
      resumeSessionId,
      metadata: payload.metadata,
    });
  }

  private rememberMessageId(messageId?: string): void {
    if (!messageId) {
      return;
    }
    this.seenMessageIds.add(messageId);
    if (this.seenMessageIds.size > DEDUPE_CAPACITY) {
      const oldest = this.seenMessageIds.values().next().value;
      if (oldest !== undefined) {
        this.seenMessageIds.delete(oldest);
      }
    }
  }

  private sendSessionActivity(activity: SessionActivity): void {
    this.sendEnvelope({
      type: "event",
      name: "session_activity",
      message_id: randomUUID(),
      payload: {
        ...activity,
        runtime: this.runtime,
        owned: this.sessions
          ? this.sessions.ownedSessionIds().includes(activity.session_id)
          : false,
      },
    });
  }

  private sendEnvelope(envelope: Record<string, unknown>): void {
    if (this.socket && this.socket.readyState === this.socket.OPEN) {
      this.sendOn(this.socket, envelope);
    }
  }

  private sendOn(socket: WebSocket, envelope: Record<string, unknown>): void {
    socket.send(JSON.stringify(envelope));
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) {
      return;
    }
    const delay = Math.min(
      RECONNECT_MAX_DELAY_MS,
      RECONNECT_BASE_DELAY_MS * 2 ** this.reconnectAttempts,
    );
    this.reconnectAttempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      this.connect();
    }, delay);
  }

  private startHeartbeat(config: ControlConfig): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.sendEnvelope({
        type: "status",
        name: "heartbeat",
        message_id: randomUUID(),
        payload: {
          status: "online",
          runtime_principal_id: config.runtime_principal_id,
        },
      });
    }, HEARTBEAT_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = undefined;
    }
  }
}

export function resolveTargetSessionId(
  payload: NonNullable<OperatorCommand["payload"]>,
): string | undefined {
  const metadata = payload.metadata ?? {};
  for (const candidate of [
    payload.target_session_id,
    payload.session_reference,
    payload.runtime_session_id,
    metadata["session_id"],
    metadata["runtime_session_id"],
    metadata["session_reference"],
  ]) {
    if (typeof candidate === "string" && candidate.trim() !== "") {
      return candidate;
    }
  }
  return undefined;
}

/** Native Claude session id for Agent SDK resume, when the envelope has it. */
export function resolveResumeSessionId(
  payload: NonNullable<OperatorCommand["payload"]>,
): string | undefined {
  const metadata = payload.metadata ?? {};
  for (const candidate of [
    payload.session_source_id,
    metadata["session_source_id"],
    resolveTargetSessionId(payload),
  ]) {
    if (typeof candidate === "string" && candidate.trim() !== "") {
      return candidate;
    }
  }
  return undefined;
}

function websocketDataToString(data: unknown): string {
  if (typeof data === "string") {
    return data;
  }
  if (Buffer.isBuffer(data)) {
    return data.toString("utf8");
  }
  if (Array.isArray(data) && data.every((part) => Buffer.isBuffer(part))) {
    return Buffer.concat(data).toString("utf8");
  }
  if (data instanceof ArrayBuffer) {
    return Buffer.from(data).toString("utf8");
  }
  return String(data);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parseArgs(): { command: string; configPath?: string } {
  const [, , command = "verify", ...rest] = process.argv;
  const configIndex = rest.indexOf("--config");
  return {
    command,
    configPath: configIndex >= 0 ? rest[configIndex + 1] : undefined,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = parseArgs();
  const sidecar = new PreloopClaudeSidecar(args.configPath);
  sidecar.setLogger((message) => console.error(message));
  if (args.command === "verify") {
    sidecar.verify();
    console.log("@preloop-ai/claude-plugin verified");
  } else if (args.command === "run") {
    void sidecar.start().catch((error: unknown) => {
      console.error(
        `@preloop-ai/claude-plugin failed to start: ${errorMessage(error)}`,
      );
      process.exitCode = 1;
    });
  } else {
    throw new Error(`Unknown command: ${args.command}`);
  }
}
