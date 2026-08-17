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

export function loadConfig(configPath?: string): ControlConfig {
  const resolvedPath = configPath ?? defaultConfigPath();
  const raw = JSON.parse(fs.readFileSync(resolvedPath, "utf8")) as Record<
    string,
    unknown
  >;
  // Accept either a flat file or a nested `control` block.
  const config = (raw.control ?? raw) as ControlConfig;
  return config;
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
