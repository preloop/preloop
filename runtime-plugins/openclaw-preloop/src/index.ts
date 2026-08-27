#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

type ControlConfig = {
  enabled?: boolean;
  protocol?: string;
  runtime?: string;
  control_ws_url?: string;
  bearer_token?: string;
  runtime_principal_id?: string;
  runtime_principal_name?: string;
  session_reference?: string;
  /** Gate the native-tool approval hook. Defaults to enabled. */
  tool_approval_enabled?: boolean;
  /**
   * When the permission-check endpoint is unreachable, allow the tool to run
   * instead of blocking. Defaults to false (fail closed / block on error).
   */
  tool_approval_fail_open?: boolean;
  /** Override for the permission-check endpoint (derived from the WS URL otherwise). */
  permission_check_url?: string;
};

// Mirror of OpenClaw's `before_tool_call` plugin hook contract
// (src/plugins/types.ts: PluginHookBeforeToolCallEvent / PluginHookToolContext /
// PluginHookBeforeToolCallResult). Declared locally so the plugin keeps a zero
// runtime dependency on the OpenClaw SDK.
type BeforeToolCallEvent = {
  toolName: string;
  params: Record<string, unknown>;
  runId?: string;
  toolCallId?: string;
};

type ToolHookContext = {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  runId?: string;
  toolName: string;
  toolCallId?: string;
};

type BeforeToolCallResult = {
  params?: Record<string, unknown>;
  block?: boolean;
  blockReason?: string;
};

type PermissionDecision = {
  decision: "allow" | "deny";
  reason?: string;
  request_id?: string;
};

type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal;
  },
) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;

/** Upper bound for the blocking permission check; backend blocks up to ~300s. */
const PERMISSION_CHECK_TIMEOUT_MS = 310_000;

type OpenClawRuntime = {
  sendPrompt?: (
    message: string,
    metadata?: Record<string, unknown>,
  ) => Promise<unknown>;
  sendVoiceTranscript?: (
    transcript: string,
    metadata?: Record<string, unknown>,
  ) => Promise<unknown>;
  interrupt?: (metadata?: Record<string, unknown>) => Promise<unknown>;
  subagent?: {
    run: (params: {
      sessionKey: string;
      message: string;
      deliver?: boolean;
      idempotencyKey?: string;
    }) => Promise<unknown>;
  };
};

type OperatorCommand = {
  message_id?: string;
  type?: string;
  name?: string;
  payload?: {
    text?: string;
    message?: string;
    input_mode?: string;
    metadata?: Record<string, unknown>;
    voice?: Record<string, unknown>;
    interrupt?: boolean;
    target_session_id?: string;
    session_reference?: string;
    runtime_session_id?: string;
  };
};

/** Reconnect backoff bounds and heartbeat cadence (mirror the Python client). */
const RECONNECT_BASE_DELAY_MS = 2_000;
const RECONNECT_MAX_DELAY_MS = 30_000;
const HEARTBEAT_INTERVAL_MS = 30_000;

export class PreloopOpenClawPlugin {
  runtime = "openclaw";
  private controlConfig?: ControlConfig;
  private socket?: WebSocket;
  private openclawRuntime?: OpenClawRuntime;
  private stopped = false;
  private reconnectAttempts = 0;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private heartbeatTimer?: ReturnType<typeof setInterval>;
  private logger?: (message: string) => void;

  constructor(
    private readonly configPath?: string,
    private readonly fetchImpl?: FetchLike,
  ) {}

  setLogger(logger: (message: string) => void): void {
    this.logger = logger;
  }

  private log(message: string): void {
    if (this.logger) {
      this.logger(message);
    }
  }

  configure(config: ControlConfig): void {
    this.controlConfig = config;
  }

  loadConfig(): ControlConfig {
    const resolvedPath = this.configPath ?? defaultConfigPath();
    const raw = JSON.parse(fs.readFileSync(resolvedPath, "utf8"));
    const config =
      raw.plugins?.entries?.["preloop-plugin"]?.config ??
      raw.plugins?.entries?.["openclaw-plugin"]?.config ??
      raw.plugins?.entries?.["@preloop-ai/openclaw-plugin"]?.config ??
      raw.plugins?.entries?.["@preloop/openclaw-plugin"]?.config ??
      raw.preloop?.control ??
      raw.control ??
      raw;
    this.controlConfig = config;
    return config;
  }

  verify(): void {
    const config = this.loadConfig();
    if (config.runtime !== this.runtime) {
      throw new Error(
        `Expected OpenClaw runtime config, got ${String(config.runtime)}`,
      );
    }
    for (const key of [
      "control_ws_url",
      "bearer_token",
      "runtime_principal_id",
    ]) {
      if (!config[key as keyof ControlConfig]) {
        throw new Error(`preloop.control.${key} is required`);
      }
    }
  }

  async start(openclawRuntime?: OpenClawRuntime): Promise<void> {
    this.stopped = false;
    this.openclawRuntime = openclawRuntime;
    // Resolve config once up front so a bad config surfaces to the caller
    // (register() logs it) instead of being retried forever.
    this.controlConfig = this.controlConfig ?? this.loadConfig();
    this.connect();
  }

  private connect(): void {
    if (this.stopped) {
      return;
    }
    const config = this.controlConfig ?? this.loadConfig();
    const wsUrl = new URL(config.control_ws_url!);
    // Note: Node's global WebSocket has no header option, so the durable
    // bearer token is passed as a query param. The backend accepts this form.
    wsUrl.searchParams.set("token", config.bearer_token!);

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl);
    } catch (error) {
      this.log(
        `Preloop Agent Control connect failed: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.reconnectAttempts = 0;
      socket.send(
        JSON.stringify({
          type: "presence",
          name: "capabilities",
          message_id: randomUUID(),
          payload: {
            status: "online",
            protocol: "preloop.agent_control.v1",
            runtime: this.runtime,
            capabilities: {
              new_session: true,
              existing_session: true,
              text: true,
              voice: true,
              interrupt: true,
              tool_approval: this.toolApprovalEnabled(config),
            },
            runtime_principal_id: config.runtime_principal_id,
            runtime_principal_name: config.runtime_principal_name,
          },
        }),
      );
      this.startHeartbeat(config);
    });

    socket.addEventListener("message", async (event) => {
      let command: OperatorCommand;
      try {
        command = JSON.parse(String(event.data)) as OperatorCommand;
      } catch (error) {
        // Malformed frame: report it rather than throwing an unhandled
        // rejection out of the async listener.
        socket.send(
          JSON.stringify({
            type: "status",
            name: "command_error",
            payload: {
              status: "failed",
              error: `invalid_json: ${
                error instanceof Error ? error.message : String(error)
              }`,
            },
          }),
        );
        return;
      }
      try {
        const result = await this.dispatch(this.openclawRuntime, command);
        socket.send(
          JSON.stringify({
            type: "status",
            name: "command_result",
            message_id: command.message_id,
            payload: {
              command_id: command.message_id,
              status: "completed",
              result,
              reply_text: this.resultToText(result),
            },
          }),
        );
      } catch (error) {
        socket.send(
          JSON.stringify({
            type: "status",
            name: "command_error",
            message_id: command.message_id,
            payload: {
              command_id: command.message_id,
              status: "failed",
              error: error instanceof Error ? error.message : String(error),
            },
          }),
        );
      }
    });

    const onClose = (event: { code?: number; reason?: string }): void => {
      this.stopHeartbeat();
      if (this.socket === socket) {
        this.socket = undefined;
      }
      if (event.code === 4000) {
        this.log(
          `Agent Control: evicted by server (${event.reason || "superseded by newer connection"}); will not reconnect`,
        );
        this.stopped = true;
        return;
      }
      this.scheduleReconnect();
    };
    socket.addEventListener("close", onClose);
    socket.addEventListener("error", () => {
      // 'error' is followed by 'close' in the WS lifecycle; log and let
      // onClose drive the reconnect so we don't schedule twice.
      this.log("Preloop Agent Control websocket error");
    });
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
    // Don't keep the process alive solely for the reconnect timer.
    (this.reconnectTimer as { unref?: () => void }).unref?.();
  }

  private startHeartbeat(config: ControlConfig): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (!this.socket || this.socket.readyState !== this.socket.OPEN) {
        return;
      }
      this.socket.send(
        JSON.stringify({
          type: "status",
          name: "heartbeat",
          message_id: randomUUID(),
          payload: {
            status: "online",
            runtime_principal_id: config.runtime_principal_id,
          },
        }),
      );
    }, HEARTBEAT_INTERVAL_MS);
    (this.heartbeatTimer as { unref?: () => void }).unref?.();
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = undefined;
    }
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    this.stopHeartbeat();
    this.socket?.close();
    this.socket = undefined;
  }

  toolApprovalEnabled(config?: ControlConfig): boolean {
    const resolved = config ?? this.controlConfig;
    return resolved?.tool_approval_enabled !== false;
  }

  toolApprovalFailOpen(config?: ControlConfig): boolean {
    const resolved = config ?? this.controlConfig;
    return resolved?.tool_approval_fail_open === true;
  }

  /**
   * Derive the REST API base URL from the Agent Control WS URL, e.g.
   * `wss://host/api/v1/agents/control/ws` -> `https://host`.
   */
  permissionCheckUrl(config: ControlConfig): string {
    if (config.permission_check_url) {
      return config.permission_check_url;
    }
    const wsUrl = new URL(config.control_ws_url!);
    const httpProtocol = wsUrl.protocol === "wss:" ? "https:" : "http:";
    return `${httpProtocol}//${wsUrl.host}/api/v1/agents/permission-check`;
  }

  /**
   * Gate a native OpenClaw tool call through Preloop's approval system.
   *
   * Returns a `before_tool_call` hook result: `{ block: true, blockReason }`
   * when the operator denies (or the check fails while failing closed), or
   * `undefined` to allow execution.
   *
   * Per plan §4, honor OpenClaw's own exec-approvals policy when present:
   * auto-allow / auto-deny locally via `client_decision`, and only escalate
   * would-prompt cases to Preloop.
   */
  async checkToolPermission(
    event: BeforeToolCallEvent,
    ctx: ToolHookContext,
  ): Promise<BeforeToolCallResult | undefined> {
    const config = this.controlConfig ?? this.loadConfig();
    if (!this.toolApprovalEnabled(config)) {
      return undefined;
    }
    const params = event.params ?? {};
    const cwd =
      typeof params["cwd"] === "string" ? (params["cwd"] as string) : process.cwd();
    const sessionId =
      ctx.sessionId ?? ctx.sessionKey ?? config.session_reference ?? undefined;
    const clientDecision = resolveOpenClawClientDecision(
      event.toolName,
      params,
      ctx.agentId,
    );
    // Honor local auto-allow/deny without round-tripping to Preloop.
    if (clientDecision === "allow") {
      return undefined;
    }
    if (clientDecision === "deny") {
      return {
        block: true,
        blockReason: "Denied by OpenClaw exec-approvals policy.",
      };
    }
    const requestBody: Record<string, unknown> = {
      source: "openclaw",
      tool_name: event.toolName,
      tool_input: params,
      session_id: sessionId,
      cwd,
      client_decision: clientDecision,
    };
    const reasoning = firstString(
      params["description"],
      params["reason"],
      params["prompt"],
    );
    if (reasoning) {
      requestBody.agent_reasoning = reasoning;
    }

    const doFetch = this.fetchImpl ?? (fetch as unknown as FetchLike);
    const controller = new AbortController();
    const timer = setTimeout(
      () => controller.abort(),
      PERMISSION_CHECK_TIMEOUT_MS,
    );
    try {
      const response = await doFetch(this.permissionCheckUrl(config), {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${config.bearer_token}`,
        },
        body: JSON.stringify(requestBody),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`permission-check returned HTTP ${response.status}`);
      }
      const decision = (await response.json()) as PermissionDecision;
      if (decision.decision === "deny") {
        return {
          block: true,
          blockReason:
            decision.reason ?? "Tool call denied by Preloop approval.",
        };
      }
      // allow (or any non-deny decision) -> let the tool run.
      return undefined;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (this.toolApprovalFailOpen(config)) {
        return undefined;
      }
      // Fail closed: block when the approval service is unreachable.
      return {
        block: true,
        blockReason: `Preloop approval unavailable (failing closed): ${message}`,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  async dispatch(
    openclawRuntime: OpenClawRuntime | undefined,
    command: OperatorCommand,
  ): Promise<unknown> {
    if (command.type !== "command" || command.name !== "send_message") {
      return undefined;
    }
    const payload = command.payload ?? {};
    const message = payload.text ?? payload.message ?? "";
    const metadata = payload.metadata ?? {};

    if (payload.interrupt) {
      if (!openclawRuntime?.interrupt) {
        throw new Error("OpenClaw interrupt hook is not available");
      }
      return openclawRuntime.interrupt(metadata);
    }

    if (payload.input_mode === "voice_transcript") {
      if (openclawRuntime?.sendVoiceTranscript) {
        return openclawRuntime.sendVoiceTranscript(message, metadata);
      }
      if (openclawRuntime?.sendPrompt) {
        return openclawRuntime.sendPrompt(message, metadata);
      }
      if (openclawRuntime?.subagent?.run) {
        return openclawRuntime.subagent.run({
          sessionKey: this.resolveSessionKey(payload, metadata),
          message,
          deliver: true,
          idempotencyKey: command.message_id,
        });
      }
      throw new Error("OpenClaw voice hook is not available");
    }

    if (openclawRuntime?.sendPrompt) {
      return openclawRuntime.sendPrompt(message, metadata);
    }
    if (openclawRuntime?.subagent?.run) {
      return openclawRuntime.subagent.run({
        sessionKey: this.resolveSessionKey(payload, metadata),
        message,
        deliver: true,
        idempotencyKey: command.message_id,
      });
    }
    throw new Error("OpenClaw sendPrompt hook is not available");
  }

  private resolveSessionKey(
    payload: NonNullable<OperatorCommand["payload"]>,
    metadata: Record<string, unknown>,
  ): string {
    const configured = this.controlConfig?.session_reference;
    for (const candidate of [
      payload.target_session_id,
      payload.session_reference,
      payload.runtime_session_id,
      metadata["session_key"],
      metadata["session_id"],
      metadata["runtime_session_id"],
      metadata["session_reference"],
      configured,
    ]) {
      if (typeof candidate === "string" && candidate.trim() !== "") {
        return candidate;
      }
    }
    return "preloop-agent-control";
  }

  private resultToText(result: unknown): string {
    if (typeof result === "string") return result;
    if (result && typeof result === "object") {
      const record = result as Record<string, unknown>;
      for (const key of ["reply_text", "text", "message", "output"]) {
        const value = record[key];
        if (typeof value === "string" && value.trim()) {
          return value;
        }
      }
    }
    return "";
  }
}

export const plugin = new PreloopOpenClawPlugin();

export const definition = {
  id: "preloop-plugin",
  name: "Preloop",
  version: "0.1.1",
  description: "Expose OpenClaw to Preloop Agent Control.",
};

export function register(api: {
  pluginConfig?: Record<string, unknown>;
  runtime?: OpenClawRuntime;
  registrationMode?: string;
  logger?: {
    info?: (message: string) => void;
    warn?: (message: string) => void;
    error?: (message: string) => void;
  };
  on?: {
    (
      hookName: "gateway_start" | "gateway_stop",
      handler: () => void | Promise<void>,
    ): void;
    (
      hookName: "before_tool_call",
      handler: (
        event: BeforeToolCallEvent,
        ctx: ToolHookContext,
      ) => BeforeToolCallResult | void | Promise<BeforeToolCallResult | void>,
      opts?: { priority?: number },
    ): void;
  };
}): void {
  const instance = new PreloopOpenClawPlugin();
  if (api.logger?.warn || api.logger?.error) {
    instance.setLogger((message) =>
      (api.logger?.warn ?? api.logger?.error)?.(message),
    );
  }
  if (api.pluginConfig && Object.keys(api.pluginConfig).length > 0) {
    instance.configure(api.pluginConfig as ControlConfig);
  }
  let started = false;
  const start = (): void => {
    if (started) {
      return;
    }
    started = true;
    void instance.start(api.runtime).catch((error: unknown) => {
      started = false;
      const message = error instanceof Error ? error.message : String(error);
      api.logger?.error?.(`Preloop Agent Control failed to start: ${message}`);
    });
  };

  api.on?.("gateway_start", start);
  api.on?.("gateway_stop", () => {
    started = false;
    instance.stop();
  });

  // Gate native OpenClaw tool calls through Preloop's approval system so they
  // can be approved/denied on mobile/watch. `before_tool_call` returning
  // `{ block: true, blockReason }` is terminal and stops the tool execution.
  api.on?.(
    "before_tool_call",
    async (event: BeforeToolCallEvent, ctx: ToolHookContext) => {
      try {
        return await instance.checkToolPermission(event, ctx);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        api.logger?.error?.(
          `Preloop tool approval check failed: ${message}`,
        );
        // checkToolPermission already handles its own fail-open/closed policy;
        // an error escaping here is unexpected, so block to stay safe.
        return { block: true, blockReason: `Preloop approval error: ${message}` };
      }
    },
  );
  if (process.argv.includes("gateway")) {
    start();
  }
  api.logger?.info?.("Preloop Agent Control plugin registered.");
}

function defaultConfigPath(): string {
  return path.join(process.env.HOME ?? ".", ".openclaw", "openclaw.json");
}

function firstString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.trim() !== "") {
      return value.trim();
    }
  }
  return undefined;
}

type ExecAsk = "off" | "on-miss" | "always";
type ExecSecurity = "deny" | "allowlist" | "full";

type ExecApprovalsFile = {
  defaults?: { security?: string; ask?: string };
  agents?: Record<string, { security?: string; ask?: string; allowlist?: unknown[] }>;
};

/**
 * Map OpenClaw's `~/.openclaw/exec-approvals.json` policy onto the shared
 * `client_decision` field. Defaults to `"ask"` (escalate) when the file is
 * missing or the tool is not an exec-style call — matching plan §4's safe
 * choice for hooks that fire on every tool call.
 *
 * Heuristic (mirrors OpenClaw's `requiresExecApproval` defaults):
 * - `security: deny` → deny
 * - `ask: off` (or security full with ask off) → allow
 * - `ask: always` → ask
 * - `ask: on-miss` + allowlist security → ask (we cannot fully evaluate the
 *   allowlist without OpenClaw's command analyzer; escalate is safe)
 */
export function resolveOpenClawClientDecision(
  toolName: string,
  params: Record<string, unknown>,
  agentId?: string,
): "allow" | "deny" | "ask" {
  const isExecTool = isOpenClawExecTool(toolName, params);
  if (!isExecTool) {
    // Non-exec tools are not covered by exec-approvals; escalate so Preloop
    // can still gate them when the plugin is loaded.
    return "ask";
  }
  const policy = loadOpenClawExecApprovals(agentId);
  if (!policy) {
    return "ask";
  }
  if (policy.security === "deny") {
    return "deny";
  }
  if (policy.ask === "always") {
    return "ask";
  }
  if (policy.ask === "off") {
    // ask=off means OpenClaw would not prompt; honor that as allow unless
    // security is deny (already handled) or allowlist-miss (escalate).
    if (policy.security === "allowlist") {
      return "ask";
    }
    return "allow";
  }
  // ask=on-miss (default for allowlist) → escalate.
  return "ask";
}

function isOpenClawExecTool(
  toolName: string,
  params: Record<string, unknown>,
): boolean {
  const name = toolName.trim().toLowerCase();
  if (
    name === "exec" ||
    name === "bash" ||
    name === "shell" ||
    name === "system.run" ||
    name === "system_run"
  ) {
    return true;
  }
  return (
    typeof params["command"] === "string" ||
    typeof params["cmd"] === "string" ||
    Array.isArray(params["argv"])
  );
}

function loadOpenClawExecApprovals(
  agentId?: string,
): { security: ExecSecurity; ask: ExecAsk } | null {
  const filePath = path.join(
    process.env.HOME ?? ".",
    ".openclaw",
    "exec-approvals.json",
  );
  try {
    if (!fs.existsSync(filePath)) {
      return null;
    }
    const raw = JSON.parse(fs.readFileSync(filePath, "utf8")) as ExecApprovalsFile;
    const defaults = raw.defaults ?? {};
    const agentKey = agentId?.trim() || "main";
    const agent = raw.agents?.[agentKey] ?? raw.agents?.["*"] ?? {};
    const security = normalizeExecSecurity(agent.security ?? defaults.security);
    const ask = normalizeExecAsk(agent.ask ?? defaults.ask);
    return { security, ask };
  } catch {
    return null;
  }
}

function normalizeExecSecurity(value: unknown): ExecSecurity {
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (normalized === "deny" || normalized === "allowlist" || normalized === "full") {
    return normalized;
  }
  return "full";
}

function normalizeExecAsk(value: unknown): ExecAsk {
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (normalized === "off" || normalized === "on-miss" || normalized === "always") {
    return normalized;
  }
  return "off";
}

function parseArgs(): {
  command: string;
  configPath?: string;
} {
  const [, , command = "verify", ...rest] = process.argv;
  const configIndex = rest.indexOf("--config");
  return {
    command,
    configPath: configIndex >= 0 ? rest[configIndex + 1] : undefined,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const args = parseArgs();
  const instance = new PreloopOpenClawPlugin(args.configPath);
  if (args.command === "verify") {
    instance.verify();
    console.log("@preloop-ai/openclaw-plugin verified");
  } else if (args.command === "run") {
    void instance.start();
  } else {
    throw new Error(`Unknown command: ${args.command}`);
  }
}
