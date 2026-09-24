// Config schema matches runtime-plugins/claude-preloop/src/config.ts
// (same key names and meanings) plus the Codex-specific keys below.
// `~/.codex/config.toml` and `~/.codex/auth.json` stay reserved for Codex.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * Sidecar configuration, written by `preloop agents onboard` (or by hand for
 * the prototype) to `~/.codex/preloop-control.json`.
 *
 * The config deliberately lives in its OWN file: `~/.codex/config.toml`
 * stays reserved for Codex.
 */
export type ControlConfig = {
  enabled?: boolean;
  protocol?: string;
  runtime?: string;
  control_ws_url?: string;
  bearer_token?: string;
  managed_agent_id?: string;
  runtime_principal_id?: string;
  runtime_principal_name?: string;
  /** Default cwd for sessions started remotely. Defaults to the home dir. */
  workspace_root?: string;
  /** Accepted for Claude-schema parity; never applied to a Codex thread. */
  permission_mode?: string;
  /** Root of Codex rollout transcripts. Defaults to ~/.codex/sessions. */
  transcript_dir?: string;
  /** Gate the transcript observer. Defaults to enabled. */
  observer_enabled?: boolean;
  /** Observer poll cadence in milliseconds. Defaults to 5000. */
  observer_poll_ms?: number;
  /** Per-turn reply timeout in milliseconds. Defaults to 5 minutes. */
  turn_timeout_ms?: number;
  /** Optional model passed to `startThread` / `resumeThread`. */
  codex_model?: string;
  /**
   * Sandbox passed to the SDK. Defaults to `workspace-write`.
   * `read-only` is allowed. `danger-full-access` is refused unless
   * `codex_allow_full_access` is also true.
   */
  codex_sandbox_mode?: string;
  /** Explicit opt-in for `codex_sandbox_mode: "danger-full-access"`. */
  codex_allow_full_access?: boolean;
  /** Optional override of the `codex` binary the SDK spawns. */
  codex_path?: string;
};

export const PROTOCOL = "preloop.agent_control.v1";
export const RUNTIME = "codex";

export const DEFAULT_SANDBOX_MODE = "workspace-write";
const SANDBOX_MODES = new Set([
  "read-only",
  "workspace-write",
  "danger-full-access",
]);

export function defaultConfigPath(): string {
  return path.join(os.homedir(), ".codex", "preloop-control.json");
}

export function defaultTranscriptDir(): string {
  return path.join(os.homedir(), ".codex", "sessions");
}

/**
 * Where the usable settings were found. `preloop agents onboard` writes a
 * nested `{"control": {...}}` block; the README long documented the flat
 * shape. Both are accepted on read, and the source is reported so a config
 * that yields NOTHING usable is loudly distinguishable from a good one
 * (a silently-empty config idles the sidecar forever).
 */
export type ConfigSource = "control-block" | "flat" | "empty";

export type LoadedConfig = {
  config: ControlConfig;
  source: ConfigSource;
  path: string;
};

/** Keys that mark a config object as carrying real control settings. */
const USABLE_KEYS: (keyof ControlConfig)[] = [
  "enabled",
  "protocol",
  "runtime",
  "control_ws_url",
  "bearer_token",
  "runtime_principal_id",
];

export function loadConfigDetailed(configPath?: string): LoadedConfig {
  const resolvedPath = configPath ?? defaultConfigPath();
  const raw = JSON.parse(fs.readFileSync(resolvedPath, "utf8")) as Record<
    string,
    unknown
  >;
  // Accept either a flat file or a nested `control` block.
  const nested = raw.control;
  if (nested && typeof nested === "object" && !Array.isArray(nested)) {
    const nestedCfg = nested as ControlConfig;
    // A control block with no usable keys is the same ambiguous case as an
    // empty flat file: classify it "empty" so the loud warning fires.
    const nestedUsable = USABLE_KEYS.some(
      (key) => nestedCfg[key] !== undefined,
    );
    return {
      config: nestedCfg,
      source: nestedUsable ? "control-block" : "empty",
      path: resolvedPath,
    };
  }
  const flat = raw as ControlConfig;
  const usable = USABLE_KEYS.some((key) => flat[key] !== undefined);
  return { config: flat, source: usable ? "flat" : "empty", path: resolvedPath };
}

export function loadConfig(configPath?: string): ControlConfig {
  return loadConfigDetailed(configPath).config;
}

/**
 * Sandbox the SDK should use. Missing means `workspace-write`.
 * `danger-full-access` is refused unless the operator also set
 * `codex_allow_full_access`.
 */
export function resolveSandboxMode(config: ControlConfig): string {
  const raw = config.codex_sandbox_mode ?? DEFAULT_SANDBOX_MODE;
  if (!SANDBOX_MODES.has(raw)) {
    throw new Error(
      `Unsupported codex_sandbox_mode ${JSON.stringify(raw)}; ` +
        "expected read-only, workspace-write, or danger-full-access",
    );
  }
  if (raw === "danger-full-access" && config.codex_allow_full_access !== true) {
    throw new Error(
      'codex_sandbox_mode "danger-full-access" is refused unless ' +
        "codex_allow_full_access is true",
    );
  }
  return raw;
}

export function verifyConfig(config: ControlConfig): void {
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
      throw new Error(`preloop-control.${key} is required`);
    }
  }
  if (config.protocol && config.protocol !== PROTOCOL) {
    throw new Error(
      `Unsupported protocol ${String(config.protocol)}; expected ${PROTOCOL}`,
    );
  }
  resolveSandboxMode(config);
}
