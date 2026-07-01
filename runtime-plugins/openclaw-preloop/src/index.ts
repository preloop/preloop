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

export class PreloopOpenClawPlugin {
  runtime = "openclaw";
  private controlConfig?: ControlConfig;
  private socket?: WebSocket;

  constructor(
    private readonly configPath?: string,
    private readonly fetchImpl?: FetchLike,
  ) {}

  configure(config: ControlConfig): void {
    this.controlConfig = config;
  }

  loadConfig(): ControlConfig {
    const resolvedPath = this.configPath ?? defaultConfigPath();
    const raw = JSON.parse(fs.readFileSync(resolvedPath, "utf8"));
    const config =
      raw.plugins?.entries?.["openclaw-plugin"]?.config ??
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
    const config = this.controlConfig ?? this.loadConfig();
    const wsUrl = new URL(config.control_ws_url!);
    wsUrl.searchParams.set("token", config.bearer_token!);
    this.socket = new WebSocket(wsUrl);

    this.socket.addEventListener("open", () => {
      this.socket?.send(
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
    });

    this.socket.addEventListener("message", async (event) => {
      const command = JSON.parse(String(event.data)) as OperatorCommand;
      try {
        const result = await this.dispatch(openclawRuntime, command);
        this.socket?.send(
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
        this.socket?.send(
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
  }

  stop(): void {
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
    const requestBody = {
      source: "openclaw",
      tool_name: event.toolName,
      tool_input: params,
      session_id: sessionId,
      cwd,
    };

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
  id: "openclaw-plugin",
  name: "Preloop",
  version: "0.1.0",
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
    console.log("@preloop/openclaw-plugin verified");
  } else if (args.command === "run") {
    void instance.start();
  } else {
    throw new Error(`Unknown command: ${args.command}`);
  }
}
