import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * Sidecar configuration, written by `preloop agents onboard` (or by hand for
 * the prototype) to `~/.claude/preloop-control.json`.
 *
 * The config deliberately lives in its OWN file: `~/.claude/settings.json`
 * stays reserved for Claude Code's own schema (S18 ruling, non-negotiable).
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
  /** Claude Code permission mode for owned sessions (e.g. "default"). */
  permission_mode?: string;
  /** Root of Claude Code transcripts. Defaults to ~/.claude/projects. */
  transcript_dir?: string;
  /** Gate the transcript observer. Defaults to enabled. */
  observer_enabled?: boolean;
  /** Observer poll cadence in milliseconds. Defaults to 5000. */
  observer_poll_ms?: number;
  /** Per-turn reply timeout in milliseconds. Defaults to 5 minutes. */
  turn_timeout_ms?: number;
};

export const PROTOCOL = "preloop.agent_control.v1";
export const RUNTIME = "claude_code";

export function defaultConfigPath(): string {
  return path.join(os.homedir(), ".claude", "preloop-control.json");
}

export function defaultTranscriptDir(): string {
  return path.join(os.homedir(), ".claude", "projects");
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
}
