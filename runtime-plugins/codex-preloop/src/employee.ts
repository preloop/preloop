// Employee subprocesses must not inherit broad provider/MCP/approval credentials.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import type { ControlConfig } from "./config.js";

export function employeeClientOptionsFor(
  config: ControlConfig,
  probe: (binary: string) => string = binary => execFileSync(binary,
    ["agents", "permission-hook", "--help"], {encoding: "utf8", timeout: 10000}),
): {codexPathOverride?: string; apiKey: string; baseUrl: string;
    env: Record<string, string>; config: Record<string, unknown>} {
  const token = config.codex_gateway_api_key;
  const gateway = config.codex_gateway_base_url;
  if (!token || !gateway || !gateway.replace(/\/$/, "").endsWith("/openai/v1")) {
    throw new Error("Employee gateway requires an execution credential and Preloop URL");
  }
  const cli = config.preloop_cli_path ?? "preloop";
  if (!probe(cli).includes("--require-flow-credential")) {
    throw new Error("Update Preloop CLI: execution-scoped approval hooks are required");
  }
  const base = (config.codex_employee_api_url ?? gateway.replace(/\/$/, "").slice(0, -"/openai/v1".length)).replace(/\/$/, "");
  const control = config.control_ws_url ? new URL(config.control_ws_url) : undefined;
  if (control && (new URL(base).host !== control.host
      || new URL(base).protocol !== (control.protocol === "wss:" ? "https:" : "http:"))) {
    throw new Error("Employee API URL must match the enrolled control origin");
  }
  const env: Record<string, string> = {};
  for (const name of ["PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SHELL"]) {
    if (process.env[name]) env[name] = process.env[name]!;
  }
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-codex-employee-"));
  fs.chmodSync(home, 0o700);
  env.CODEX_HOME = home;
  env.PRELOOP_FLOW_CREDENTIAL_REQUIRED = "1";
  env.PRELOOP_FLOW_TOKEN = token;
  env.PRELOOP_FLOW_API_URL = base;
  const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
  const hook = `${quote(cli)} agents permission-hook --source codex_cli --require-flow-credential`;
  const sourceHome = process.env.CODEX_HOME ?? path.join(os.homedir(), ".codex");
  const sourceHooks = path.join(sourceHome, "hooks.json");
  const hooks: Record<string, unknown[]> = fs.existsSync(sourceHooks)
    ? JSON.parse(fs.readFileSync(sourceHooks, "utf8")).hooks ?? {} : {};
  // Keep independent native hooks while replacing old Preloop credential gates.
  for (const phase of ["PreToolUse", "PermissionRequest"]) {
    const retained = (hooks[phase] ?? []).flatMap((entry: any) => {
      const commands = (entry.hooks ?? []).filter((command: any) =>
        !String(command.command ?? "").includes("agents permission-hook"));
      return commands.length ? [{...entry, hooks: commands}] : [];
    });
    hooks[phase] = [...retained, {matcher: "*", hooks: [{type: "command",
      command: hook + (phase === "PreToolUse" ? " --hook-event PreToolUse" : ""), timeout: 310}]}];
  }
  fs.writeFileSync(path.join(home, "hooks.json"), JSON.stringify({hooks}), {mode: 0o600});
  // Preserve the human's curated global role guide; repository AGENTS and skills
  // remain discoverable in the configured workspace. Never copy auth/config.
  const role = path.join(sourceHome, "AGENTS.md");
  if (fs.existsSync(role)) fs.copyFileSync(role, path.join(home, "AGENTS.md"));
  const runtimeConfig: Record<string, unknown> = {
    model_provider: "preloop_employee",
    model_providers: {preloop_employee: {name: "Preloop employee", base_url: gateway,
      env_key: "PRELOOP_FLOW_TOKEN", wire_api: "responses"}},
  };
  const nativeConfig = path.join(sourceHome, "config.toml");
  if (fs.existsSync(nativeConfig)) {
    const rootConfig = fs.readFileSync(nativeConfig, "utf8").split(/^\s*\[/m)[0];
    const policy = rootConfig.match(/^\s*approval_policy\s*=\s*["'](untrusted|on-request|on-failure|never)["']/m);
    if (policy) runtimeConfig.approval_policy = policy[1];
  }
  if (config.codex_employee_mcp_enabled) runtimeConfig.mcp_servers = {
    preloop: {url: base + "/mcp/v1", bearer_token_env_var: "PRELOOP_FLOW_TOKEN", tool_timeout_sec: 310},
  };
  return { ...(config.codex_path ? {codexPathOverride: config.codex_path} : {}),
    apiKey: token, baseUrl: gateway, env, config: runtimeConfig };
}
