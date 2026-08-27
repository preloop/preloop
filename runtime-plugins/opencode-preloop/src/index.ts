import { randomUUID } from "node:crypto";

import WebSocket from "ws";

import {
  DEFAULT_APPROVAL_TIMEOUT_MS,
  DEFAULT_TURN_TIMEOUT_MS,
  PROTOCOL,
  RUNTIME,
  loadControlConfig,
  verifyConfig,
  type ControlConfig,
} from "./config.js";

/**
 * Structural mirror of OpenCode's permission-ask request
 * (packages/opencode/src/permission/next.ts: PermissionNext.Request, delivered
 * to plugins through the generic `event` hook as `{ type:
 * "permission.asked", properties }`). Declared locally so the plugin keeps a
 * zero runtime dependency on the `@opencode-ai/plugin` SDK.
 *
 * NOTE: OpenCode's documented `permission.ask` plugin hook is defined in the
 * SDK types but never triggered by the permission system (anomalyco/opencode
 * issues #7006 and #9229); the supported surface is the `permission.asked`
 * bus event plus a reply through the SDK client.
 */
export type PermissionRequest = {
  id: string;
  sessionID?: string;
  /** Permission action that matched an "ask" rule, e.g. "bash". */
  permission?: string;
  /** Resource patterns the tool proposed (e.g. command prefixes). */
  patterns?: string[];
  metadata?: Record<string, unknown>;
  title?: string;
  callID?: string;
};

export type PermissionEvent = {
  type: "permission.asked" | "permission.replied";
  properties?: Partial<PermissionRequest> & { id?: string };
};

/** Reply values accepted by OpenCode's permission reply endpoint. */
export type PermissionResponse = "once" | "always" | "reject";

/**
 * Minimal structural view of the OpenCode SDK client handed to plugins. The
 * real client is generated; only the members used here are declared.
 */
export type OpenCodeClient = {
  permission?: {
    reply: (input: {
      path: { id: string };
      body: { response: PermissionResponse };
    }) => Promise<unknown>;
  };
  session?: {
    /**
     * Async prompt surface: enqueues the user message and resolves as soon
     * as OpenCode accepts it. Present on recent SDK builds.
     */
    chat?: (input: {
      path: { id: string };
      body: { parts: Array<{ type: "text"; text: string }> };
    }) => Promise<unknown>;
    /**
     * Blocking prompt surface (https://opencode.ai/docs/sdk/): resolves with
     * the assistant's reply once the turn finishes.
     */
    prompt?: (input: {
      path: { id: string };
      body: { parts: Array<{ type: "text"; text: string }> };
    }) => Promise<unknown>;
    /** Abort a running session (https://opencode.ai/docs/sdk/). */
    abort?: (input: { path: { id: string } }) => Promise<unknown>;
  };
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

export type PermissionDecision = {
  decision: "allow" | "deny";
  reason?: string;
  request_id?: string;
};

export type OperatorCommand = {
  message_id?: string;
  type?: string;
  name?: string;
  payload?: {
    text?: string;
    message?: string;
    interrupt?: boolean;
    target_session_id?: string;
    session_reference?: string;
    runtime_session_id?: string;
    metadata?: Record<string, unknown>;
  };
};

/**
 * Close code the server sends when evicting a superseded WebSocket.  Must
 * match `EVICTION_CLOSE_CODE` on the server and in the Python client.
 */
const EVICTION_CLOSE_CODE = 4000;

/** Reconnect backoff bounds and heartbeat cadence (mirror the Python client). */
const RECONNECT_BASE_DELAY_MS = 2_000;
const RECONNECT_MAX_DELAY_MS = 30_000;
const HEARTBEAT_INTERVAL_MS = 30_000;

/** Upper bound on remembered command ids / pending approval ids. */
const DEDUPE_MAX_ENTRIES = 512;

export class PreloopOpenCodePlugin {
  runtime = RUNTIME;
  private controlConfig?: ControlConfig;
  private socket?: WebSocket;
  private client?: OpenCodeClient;
  private stopped = false;
  private reconnectAttempts = 0;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private heartbeatTimer?: ReturnType<typeof setInterval>;

  /**
   * Dedupe for inbound Agent Control commands. The backend persists commands
   * before delivery and replays undelivered ones with their original ids on
   * reconnect, so every envelope must be applied at most once.
   */
  private seenCommandIds: Set<string>;

  /**
   * Dedupe for OpenCode permission requests: repeated `permission.asked`
   * events for the same request id must produce exactly one operator round
   * trip and one reply.
   */
  private pendingApprovals: Map<string, PermissionRequest>;

  private logger?: (message: string) => void;

  constructor(
    private readonly configPath?: string,
    private readonly fetchImpl?: FetchLike,
  ) {
    this.seenCommandIds = new Set<string>();
    this.pendingApprovals = new Map<string, PermissionRequest>();
  }

  setLogger(logger: (message: string) => void): void {
    this.logger = logger;
  }

  setOpenCodeClient(client: OpenCodeClient): void {
    this.client = client;
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
    this.controlConfig = loadControlConfig(this.configPath);
    return this.controlConfig;
  }

  verify(): void {
    verifyConfig(this.loadConfig());
  }

  async start(client?: OpenCodeClient): Promise<void> {
    this.stopped = false;
    if (client) {
      this.client = client;
    }
    // Resolve config once up front so a bad config surfaces to the caller
    // instead of being retried forever.
    this.controlConfig = this.controlConfig ?? this.loadConfig();
    this.connect();
  }

  private connect(): void {
    if (this.stopped) {
      return;
    }
    const config = this.controlConfig ?? this.loadConfig();
    const wsUrl = new URL(config.control_ws_url!);
    // Node's global WebSocket cannot set headers, but the `ws` package can.
    // The durable bearer token is therefore sent as `Authorization: Bearer`
    // on the HTTP upgrade — the scheme Agent Control already prefers — so it
    // never appears in proxy/access-log query strings. The URL is loggable;
    // the token itself must never be logged.

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl, {
        headers: { authorization: `Bearer ${config.bearer_token!}` },
      });
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
      socket.send(JSON.stringify(this.presenceMessage(config)));
      this.startHeartbeat(config);
    });

    socket.addEventListener("message", (event) => {
      void this.handleFrame(socket, String(event.data));
    });

    const onClose = (event: { code?: number; reason?: string | Buffer }): void => {
      this.stopHeartbeat();
      if (this.socket === socket) {
        this.socket = undefined;
      }
      if (event.code === EVICTION_CLOSE_CODE) {
        this.log(
          `Agent Control: evicted by server (${String(event.reason || "superseded by newer connection")}); will not reconnect`,
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

  /**
   * Steering capabilities actually available through the connected SDK
   * client. Advertised truthfully in presence so the console never offers an
   * operator a control the plugin cannot perform.
   */
  private steeringCapabilities(config: ControlConfig): {
    text: boolean;
    interrupt: boolean;
  } {
    if (!this.remoteControlEnabled(config)) {
      return { text: false, interrupt: false };
    }
    const session = this.client?.session;
    return {
      text:
        typeof session?.chat === "function" ||
        typeof session?.prompt === "function",
      interrupt: typeof session?.abort === "function",
    };
  }

  presenceMessage(config: ControlConfig): Record<string, unknown> {
    const steering = this.steeringCapabilities(config);
    return {
      type: "presence",
      name: "capabilities",
      message_id: randomUUID(),
      payload: {
        status: "online",
        protocol: PROTOCOL,
        runtime: this.runtime,
        capabilities: {
          // OpenCode sessions are addressed by their native session id; the
          // plugin steers existing sessions rather than creating new ones.
          new_session: false,
          existing_session: true,
          text: steering.text,
          voice: false,
          interrupt: steering.interrupt,
          tool_approval: this.toolApprovalEnabled(config),
        },
        runtime_principal_id: config.runtime_principal_id,
        runtime_principal_name: config.runtime_principal_name,
      },
    };
  }

  /**
   * Process one inbound control frame. Exposed for tests.
   *
   * Emission mirrors the openclaw plugin exactly: a `command_result` frame on
   * success (which the backend also accepts as the command ack) and a
   * `command_error` frame on failure. Replayed `message_id`s are acknowledged
   * as completed duplicates without re-executing.
   */
  async handleFrame(socket: WebSocket, data: string): Promise<void> {
    let command: OperatorCommand;
    try {
      command = JSON.parse(data) as OperatorCommand;
    } catch (error) {
      this.sendStatus(socket, {
        type: "status",
        name: "command_error",
        payload: {
          status: "failed",
          error: `invalid_json: ${
            error instanceof Error ? error.message : String(error)
          }`,
        },
      });
      return;
    }
    const outcome = this.handleCommand(command);
    if (outcome.duplicate) {
      // Already applied in a previous connection; acknowledge without
      // re-executing so the backend can mark it delivered.
      this.sendStatus(socket, {
        type: "status",
        name: "command_result",
        message_id: command.message_id,
        payload: {
          command_id: command.message_id,
          status: "completed",
          duplicate: true,
        },
      });
      return;
    }
    void Promise.resolve(outcome.result)
      .then((result) => {
        this.sendStatus(socket, {
          type: "status",
          name: "command_result",
          message_id: command.message_id,
          payload: {
            command_id: command.message_id,
            status: "completed",
            result,
            reply_text: this.resultToText(result),
          },
        });
      })
      .catch((error: unknown) => {
        this.sendStatus(socket, {
          type: "status",
          name: "command_error",
          message_id: command.message_id,
          payload: {
            command_id: command.message_id,
            status: "failed",
            error: error instanceof Error ? error.message : String(error),
          },
        });
      });
  }

  private sendStatus(socket: WebSocket, frame: Record<string, unknown>): void {
    try {
      socket.send(JSON.stringify(frame));
    } catch (error) {
      this.log(
        `Failed to send status frame: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
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
      if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
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

  approvalTimeoutMs(config?: ControlConfig): number {
    const resolved = config ?? this.controlConfig;
    const value = resolved?.approval_timeout_ms;
    return typeof value === "number" && value > 0
      ? value
      : DEFAULT_APPROVAL_TIMEOUT_MS;
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
   * Apply one inbound Agent Control command at most once.
   *
   * Returns `{ duplicate: true }` when `message_id` was already applied (the
   * backend replays undelivered commands on reconnect), otherwise the result
   * promise from {@link dispatch}.
   *
   * The id is remembered only while the dispatch is in flight and after it
   * succeeds; a failed turn (chat/prompt unavailable, remote control
   * disabled, ...) forgets the id so a backend replay re-executes instead of
   * being acked as a completed duplicate.
   */
  handleCommand(command: OperatorCommand): {
    duplicate: boolean;
    result?: unknown;
  } {
    const messageId =
      typeof command.message_id === "string" && command.message_id.trim() !== ""
        ? command.message_id
        : randomUUID();
    if (this.seenCommandIds.has(messageId)) {
      return { duplicate: true };
    }
    // Reserve the id synchronously so concurrent deliveries of the same
    // command cannot double-execute while the first dispatch is pending.
    this.seenCommandIds.add(messageId);
    while (this.seenCommandIds.size > DEDUPE_MAX_ENTRIES) {
      const oldest = this.seenCommandIds.values().next().value;
      if (oldest === undefined) {
        break;
      }
      if (oldest === messageId) {
        break;
      }
      this.seenCommandIds.delete(oldest);
    }
    return {
      duplicate: false,
      result: Promise.resolve(this.dispatch(command)).catch((error) => {
        // Release the reservation so reconnect replays of the same
        // message_id re-execute instead of being dropped as duplicates.
        this.seenCommandIds.delete(messageId);
        throw error;
      }),
    };
  }

  async dispatch(command: OperatorCommand): Promise<unknown> {
    if (command.type !== "command") {
      return undefined;
    }
    const payload = command.payload ?? {};
    const isStop =
      command.name === "stop" ||
      command.name === "interrupt" ||
      payload.interrupt === true;
    if (!isStop && command.name !== "send_message") {
      return undefined;
    }
    if (!this.remoteControlEnabled()) {
      throw new Error(
        "Preloop remote control is disabled (preloop.control.remote_control_enabled)",
      );
    }
    if (isStop) {
      return this.abortSession(payload);
    }
    const text = payload.text ?? payload.message ?? "";
    if (!text.trim()) {
      throw new Error("send_message requires non-empty text");
    }
    return this.sendOperatorTurn(text, payload);
  }

  /**
   * Forward an operator turn into the targeted OpenCode session.
   *
   * Prefers the async `session.chat` surface (resolves once OpenCode accepts
   * the user message); falls back to the documented blocking
   * `session.prompt` (https://opencode.ai/docs/sdk/), bounded by
   * `turn_timeout_ms` so a hung session cannot stall the command ack forever.
   */
  private async sendOperatorTurn(
    text: string,
    payload: NonNullable<OperatorCommand["payload"]>,
  ): Promise<unknown> {
    const sessionId = this.resolveSessionId(payload);
    const chat = this.client?.session?.chat;
    if (chat) {
      return chat({
        path: { id: sessionId },
        body: { parts: [{ type: "text", text }] },
      });
    }
    const prompt = this.client?.session?.prompt;
    if (prompt) {
      return this.withTurnTimeout(
        prompt({
          path: { id: sessionId },
          body: { parts: [{ type: "text", text }] },
        }),
      );
    }
    throw new Error(
      "OpenCode SDK client.session.chat / client.session.prompt is not available",
    );
  }

  /** Map stop/interrupt commands onto OpenCode's `session.abort` API. */
  private async abortSession(
    payload: NonNullable<OperatorCommand["payload"]>,
  ): Promise<"interrupted"> {
    const sessionId = this.resolveSessionId(payload);
    const abort = this.client?.session?.abort;
    if (!abort) {
      throw new Error("OpenCode SDK client.session.abort is not available");
    }
    await abort({ path: { id: sessionId } });
    return "interrupted";
  }

  private withTurnTimeout<T>(promise: Promise<T>): Promise<T> {
    const timeoutMs = this.turnTimeoutMs();
    let timer: ReturnType<typeof setTimeout> | undefined;
    return Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () =>
            reject(
              new Error(
                `OpenCode turn timed out after ${timeoutMs}ms; ` +
                  "the session may still be running",
              ),
            ),
          timeoutMs,
        );
      }),
    ]).finally(() => {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    });
  }

  remoteControlEnabled(config?: ControlConfig): boolean {
    const resolved = config ?? this.controlConfig;
    return resolved?.remote_control_enabled !== false;
  }

  turnTimeoutMs(config?: ControlConfig): number {
    const resolved = config ?? this.controlConfig;
    const value = resolved?.turn_timeout_ms;
    return typeof value === "number" && value > 0
      ? value
      : DEFAULT_TURN_TIMEOUT_MS;
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

  resolveSessionId(payload: NonNullable<OperatorCommand["payload"]>): string {
    const configured = this.controlConfig?.session_reference;
    for (const candidate of [
      payload.target_session_id,
      payload.session_reference,
      payload.runtime_session_id,
      configured,
    ]) {
      if (typeof candidate === "string" && candidate.trim() !== "") {
        return candidate.trim();
      }
    }
    throw new Error(
      "no target session specified and no preloop.control.session_reference configured",
    );
  }

  /**
   * Bridge an OpenCode `permission.asked` event to Preloop's approval system.
   *
   * Repeated events for the same request id are collapsed into a single
   * round trip (and a single reply). The decision is applied by replying
   * `"once"` (approved) or `"reject"` (denied) through the SDK client —
   * OpenCode holds tool execution open until a reply arrives, so awaiting
   * the remote operator here genuinely blocks the tool call.
   *
   * Returns the reply that was sent, or why nothing was sent.
   */
  async handlePermissionAsked(request: PermissionRequest): Promise<{
    replied: boolean;
    response?: PermissionResponse;
    skipped?: "disabled" | "duplicate" | "missing-id" | "no-reply-channel";
    reason?: string;
  }> {
    const config = this.controlConfig ?? this.loadConfig();
    if (!this.toolApprovalEnabled(config)) {
      return { replied: false, skipped: "disabled" };
    }
    if (!request.id || request.id.trim() === "") {
      // Not a duplicate — there is nothing to dedupe on. Log loudly so a
      // silently-dropped escalation is visible.
      this.log(
        "Preloop approval skipped: permission.asked event carried no request id",
      );
      return {
        replied: false,
        skipped: "missing-id",
        reason: "permission.asked event had no request id",
      };
    }
    if (this.pendingApprovals.has(request.id)) {
      return { replied: false, skipped: "duplicate" };
    }
    this.pendingApprovals.set(request.id, request);
    while (this.pendingApprovals.size > DEDUPE_MAX_ENTRIES) {
      const oldest = this.pendingApprovals.keys().next().value;
      if (oldest === undefined || oldest === request.id) {
        break;
      }
      this.pendingApprovals.delete(oldest);
    }

    try {
      const decision = await this.requestOperatorDecision(request);
      let response: PermissionResponse;
      if (decision.decision === "deny") {
        response = "reject";
      } else {
        response = "once";
      }

      const reply = this.client?.permission?.reply;
      if (!reply) {
        this.log(
          `Preloop decision "${decision.decision}" for ${request.id} could not be applied: no SDK reply channel`,
        );
        return { replied: false, skipped: "no-reply-channel", response };
      }
      await reply({ path: { id: request.id }, body: { response } });
      return { replied: true, response, reason: decision.reason };
    } finally {
      // The round trip has settled (replied, rejected for lack of a reply
      // channel, or errored): forget the reservation so the map cannot leak
      // entries. Late duplicate asks after settlement re-escalate rather
      // than being silently dropped.
      this.pendingApprovals.delete(request.id);
    }
  }

  /**
   * Observe a locally-settled permission request (operator answered in the
   * TUI or another client). Clears the dedupe entry so late plugin activity
   * cannot double-reply, and forgets the request.
   */
  handlePermissionReplied(event: PermissionEvent): void {
    const id = event.properties?.id;
    if (id) {
      this.pendingApprovals.delete(id);
    }
  }

  buildPermissionRequestBody(
    request: PermissionRequest,
    cwd: string,
  ): Record<string, unknown> {
    const config = this.controlConfig ?? this.loadConfig();
    const sessionId =
      request.sessionID ??
      config.session_reference ??
      request.callID ??
      undefined;
    const body: Record<string, unknown> = {
      source: this.runtime,
      tool_name: request.permission ?? "tool",
      tool_input: {
        patterns: request.patterns ?? [],
        title: request.title ?? null,
        metadata: request.metadata ?? {},
      },
      session_id: sessionId,
      cwd,
    };
    const reasoning = request.title ?? request.patterns?.join(", ");
    if (reasoning) {
      body.agent_reasoning = reasoning;
    }
    return body;
  }

  /**
   * Blocking round trip to Preloop's permission-check endpoint. The backend
   * parks the HTTP request until an operator decides (or its own ~300 s
   * budget expires), so this promise resolves with the human's decision.
   * On timeout or transport error the configured fallback applies: fail
   * closed (deny, the default) or fail open (allow).
   */
  async requestOperatorDecision(
    request: PermissionRequest,
  ): Promise<PermissionDecision> {
    const config = this.controlConfig ?? this.loadConfig();
    const doFetch = this.fetchImpl ?? (fetch as unknown as FetchLike);
    const timeoutMs = this.approvalTimeoutMs(config);
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      // Race the round trip against the configured budget instead of relying
      // solely on AbortSignal: the backend parks the request until the
      // operator decides, so the deadline must be enforced locally.
      const decision = await Promise.race([
        doFetch(this.permissionCheckUrl(config), {
          method: "POST",
          headers: {
            "content-type": "application/json",
            authorization: `Bearer ${config.bearer_token}`,
          },
          body: JSON.stringify(
            this.buildPermissionRequestBody(request, process.cwd()),
          ),
          signal: controller.signal,
        }).then(async (response) => {
          if (!response.ok) {
            throw new Error(
              `permission-check returned HTTP ${response.status}`,
            );
          }
          return (await response.json()) as PermissionDecision;
        }),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(`operator decision timed out after ${timeoutMs}ms`),
              ),
            timeoutMs,
          );
        }),
      ]);
      if (decision.decision === "deny") {
        return { ...decision, reason: decision.reason ?? "Denied in Preloop." };
      }
      return { ...decision, decision: "allow" };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (this.toolApprovalFailOpen(config)) {
        return {
          decision: "allow",
          reason: `Preloop approval unavailable (failing open): ${message}`,
        };
      }
      return {
        decision: "deny",
        reason: `Preloop approval unavailable (failing closed): ${message}`,
      };
    } finally {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      controller.abort();
    }
  }
}

export const plugin = new PreloopOpenCodePlugin();

/**
 * OpenCode plugin entry point.
 *
 * Register the package in `opencode.json`:
 *
 * ```json
 * { "plugin": ["@preloop-ai/opencode-plugin"] }
 * ```
 *
 * or drop a shim into `.opencode/plugins/preloop.ts`. OpenCode calls the
 * exported function once per project with an SDK context; we keep the Agent
 * Control websocket alive, bridge `permission.asked` events to Preloop, and
 * forward operator turns and stop commands from Preloop into OpenCode.
 */
export const PreloopPlugin = async (ctx: {
  client?: OpenCodeClient;
  directory?: string;
}): Promise<{
  event: (input: { event: { type: string; properties?: unknown } }) => Promise<void>;
}> => {
  const instance = plugin;
  if (ctx.client) {
    instance.setOpenCodeClient(ctx.client);
  }
  instance.configure(loadControlConfig());
  await instance.start(ctx.client);
  return {
    event: async ({ event }) => {
      if (
        event.type === "permission.asked" ||
        event.type === "permission.replied"
      ) {
        const properties =
          (event.properties as Partial<PermissionRequest> | undefined) ?? {};
        if (event.type === "permission.asked") {
          await instance.handlePermissionAsked({
            ...properties,
            id: properties.id ?? "",
          });
        } else {
          instance.handlePermissionReplied({ type: event.type, properties });
        }
      }
    },
  };
};

export default PreloopPlugin;
