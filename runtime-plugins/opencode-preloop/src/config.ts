import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * Control configuration, written by `preloop agents enroll` into OpenCode's
 * own config file (`~/.config/opencode/opencode.json`) under the
 * `preloop.control` key. A flat file containing only the control block is
 * also accepted so a config can be staged by hand or pointed at via
 * `PRELOOP_OPENCODE_CONTROL_CONFIG`.
 */
export type ControlConfig = {
  enabled?: boolean;
  protocol?: string;
  runtime?: string;
  control_ws_url?: string;
  bearer_token?: string;
  runtime_principal_id?: string;
  runtime_principal_name?: string;
  /** Preloop session to target when the operator does not specify one. */
  session_reference?: string;
  /** Gate the permission-ask bridging. Defaults to enabled. */
  tool_approval_enabled?: boolean;
  /**
   * Override for Preloop's permission-check endpoint. When set, approval
   * round trips POST here instead of deriving
   * `<origin>/api/v1/agents/permission-check` from `control_ws_url`.
   */
  permission_check_url?: string;
  /**
   * When Preloop cannot be reached (or the operator has not decided within
   * the timeout), allow the tool to run instead of rejecting it. Defaults to
   * false (fail closed / reject on error).
   */
  tool_approval_fail_open?: boolean;
  /**
   * How long to wait for the operator decision before applying the fallback
   * (fail open or fail closed). Defaults to 310000 ms — the backend blocks
   * the permission-check request for up to ~300 s.
   */
  approval_timeout_ms?: number;
  /**
   * Gate remote steering (operator `send_message` / `stop` commands arriving
   * on the Agent Control WebSocket). Defaults to enabled.
   */
  remote_control_enabled?: boolean;
  /**
   * Upper bound for waiting on OpenCode's blocking `session.prompt` surface
   * before reporting a timeout for an operator turn. The session itself keeps
   * running. Defaults to 300000 ms (mirrors the claude sidecar).
   */
  turn_timeout_ms?: number;
};

export const PROTOCOL = "preloop.agent_control.v1";
export const RUNTIME = "opencode";

/** Upper bound for the blocking permission check; backend blocks up to ~300s. */
export const DEFAULT_APPROVAL_TIMEOUT_MS = 310_000;

/** Upper bound for one remote operator turn (mirrors the claude sidecar). */
export const DEFAULT_TURN_TIMEOUT_MS = 300_000;

const USABLE_KEYS: (keyof ControlConfig)[] = [
  "enabled",
  "protocol",
  "runtime",
  "control_ws_url",
  "bearer_token",
  "runtime_principal_id",
  "permission_check_url",
];

export function defaultConfigPath(): string {
  return path.join(os.homedir(), ".config", "opencode", "opencode.json");
}

/**
 * Extract the `preloop.control` block from an already-parsed OpenCode config.
 *
 * Accepted shapes, first match wins:
 * - `preloop.control` (what the Preloop CLI writes)
 * - a flat object that itself carries usable control keys
 */
export function extractControlConfig(raw: unknown): {
  config: ControlConfig;
  source: "control-block" | "flat" | "empty";
} {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const record = raw as Record<string, unknown>;
    const nested = record["preloop"] as Record<string, unknown> | undefined;
    if (
      nested &&
      typeof nested === "object" &&
      !Array.isArray(nested) &&
      nested["control"] &&
      typeof nested["control"] === "object"
    ) {
      const block = nested["control"] as ControlConfig;
      if (USABLE_KEYS.some((key) => block[key] !== undefined)) {
        return { config: block, source: "control-block" };
      }
    }
    const flat = raw as ControlConfig;
    if (USABLE_KEYS.some((key) => flat[key] !== undefined)) {
      return { config: flat, source: "flat" };
    }
  }
  return { config: {}, source: "empty" };
}

export function loadControlConfig(configPath?: string): ControlConfig {
  const resolvedPath =
    process.env.PRELOOP_OPENCODE_CONTROL_CONFIG ?? configPath ?? defaultConfigPath();
  const raw = JSON.parse(fs.readFileSync(resolvedPath, "utf8")) as unknown;
  return extractControlConfig(raw).config;
}

export function verifyConfig(config: ControlConfig): void {
  if (!USABLE_KEYS.some((key) => config[key] !== undefined)) {
    throw new Error("no preloop.control settings found in opencode config");
  }
  if (config.runtime !== RUNTIME) {
    throw new Error(
      `Expected runtime "${RUNTIME}", got ${String(config.runtime)}`,
    );
  }
  for (const key of [
    "control_ws_url",
    "bearer_token",
    "runtime_principal_id",
  ] as const) {
    if (!config[key]) {
      throw new Error(`preloop.control.${key} is required`);
    }
  }
  if (config.protocol && config.protocol !== PROTOCOL) {
    throw new Error(
      `Unsupported protocol ${String(config.protocol)}; expected ${PROTOCOL}`,
    );
  }
}
