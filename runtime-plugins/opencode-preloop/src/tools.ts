/**
 * Native tool-call gating helpers for the `tool.execute.before` hook.
 *
 * OpenCode names its built-in tools in lower case (`bash`, `edit`, `read`,
 * ...) and passes camelCase arguments (`filePath`, `oldString`). Preloop's
 * permission-check endpoint and the account's native tool rules use the
 * vocabulary the CLI hook adapters send for Claude Code (`Bash`, `Edit`,
 * `Read`, ... with `file_path`, `old_string`), so one set of rules governs
 * every runtime. These helpers translate between the two.
 */

/** OpenCode built-in tool id -> Preloop native tool name. */
export const OPENCODE_TOOL_NAMES: Readonly<Record<string, string>> = {
  bash: "Bash",
  edit: "Edit",
  multiedit: "MultiEdit",
  write: "Write",
  patch: "Patch",
  read: "Read",
  glob: "Glob",
  grep: "Grep",
  list: "List",
  webfetch: "WebFetch",
  websearch: "WebSearch",
  codesearch: "CodeSearch",
  task: "Task",
  todowrite: "TodoWrite",
  todoread: "TodoRead",
  question: "Question",
  skill: "Skill",
  lsp: "LSP",
};

/**
 * Prefix OpenCode gives tools served by the Preloop MCP server (OpenCode
 * names MCP tools `<server>_<tool>`; `preloop agents onboard` registers the
 * server as `preloop`). Those calls are already governed by Preloop's MCP
 * approvals server-side, so gating them again would ask twice.
 */
export const PRELOOP_MCP_TOOL_PREFIX = "preloop_";

/** Longest string argument forwarded to Preloop, in characters. */
export const MAX_TOOL_ARG_CHARS = 16_384;

/**
 * Map an OpenCode tool id onto the Preloop native tool vocabulary. Unknown
 * ids (MCP tools, custom tools) pass through unchanged so account rules can
 * still name them.
 */
export function mapOpenCodeToolName(tool: string): string {
  const trimmed = (tool ?? "").trim();
  if (trimmed === "") {
    return "tool";
  }
  return OPENCODE_TOOL_NAMES[trimmed.toLowerCase()] ?? trimmed;
}

export function isPreloopMCPTool(tool: string): boolean {
  return (tool ?? "").trim().toLowerCase().startsWith(PRELOOP_MCP_TOOL_PREFIX);
}

const SNAKE_CASE_ALIASES: Readonly<Record<string, string>> = {
  filePath: "file_path",
  oldString: "old_string",
  newString: "new_string",
  replaceAll: "replace_all",
  subagentType: "subagent_type",
};

/**
 * Copy the hook's `output.args` into the `tool_input` Preloop stores as
 * approval `tool_args`. Known camelCase keys gain the snake_case alias the
 * Claude Code adapter sends (the original key is kept so nothing is lost),
 * and oversized strings are clipped so a large file write cannot bloat the
 * approval row.
 */
export function normalizeToolArgs(
  args: unknown,
): Record<string, unknown> {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    return args === undefined || args === null ? {} : { value: clip(args) };
  }
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(args as Record<string, unknown>)) {
    const clipped = clip(value);
    out[key] = clipped;
    const alias = SNAKE_CASE_ALIASES[key];
    if (alias && out[alias] === undefined) {
      out[alias] = clipped;
    }
  }
  return out;
}

function clip(value: unknown): unknown {
  if (typeof value === "string" && value.length > MAX_TOOL_ARG_CHARS) {
    return (
      value.slice(0, MAX_TOOL_ARG_CHARS) +
      `... [truncated ${value.length - MAX_TOOL_ARG_CHARS} chars]`
    );
  }
  return value;
}

/**
 * Read-only shell commands that never need a human. Port of
 * `safeReadShellCommands` in the Preloop CLI (cli/internal/cmd/
 * cursor_permission_policy.go) so the plugin and the hook adapters agree.
 */
const SAFE_READ_SHELL_COMMANDS: ReadonlySet<string> = new Set([
  "ls",
  "pwd",
  "cat",
  "head",
  "tail",
  "less",
  "wc",
  "echo",
  "which",
  "whoami",
  "printenv",
  "grep",
  "rg",
  "stat",
  "file",
  "du",
  "df",
  "uname",
  "date",
]);

/**
 * True when a shell command consists solely of obviously read-only commands:
 * a single safe command or a plain `|` pipeline whose every stage leads with
 * one. Anything that could chain or hide a mutation (`;`, `&`, redirections,
 * backticks, `$(`, `<(`, newlines) is rejected. Conservative by design: when
 * in any doubt return false and let the call escalate to Preloop.
 */
export function isSafeReadShellCommand(command: string): boolean {
  const trimmed = (command ?? "").trim();
  if (trimmed === "") {
    return false;
  }
  for (const meta of [";", "&", ">", "<", "`", "$(", "\n", "\r"]) {
    if (trimmed.includes(meta)) {
      return false;
    }
  }
  return trimmed.split("|").every(isSafeReadPipelineStage);
}

function isSafeReadPipelineStage(stage: string): boolean {
  const fields = stage.trim().split(/\s+/).filter(Boolean);
  if (fields.length === 0) {
    return false;
  }
  const [head, ...args] = fields;
  switch (head) {
    case "env":
      // "env" alone prints the environment; with args it runs a command.
      return args.length === 0;
    case "find":
      return !args.some((arg) =>
        ["-delete", "-exec", "-execdir", "-ok", "-okdir"].includes(arg),
      );
    case "git":
      return isSafeReadGitArgs(args);
    default:
      return SAFE_READ_SHELL_COMMANDS.has(head);
  }
}

const MUTATING_GIT_BRANCH_FLAGS: ReadonlySet<string> = new Set([
  "-d",
  "-D",
  "-m",
  "-M",
  "-c",
  "-C",
  "-f",
  "--force",
  "--delete",
  "--move",
  "--copy",
  "--edit-description",
  "--set-upstream-to",
  "--unset-upstream",
  "-u",
  "-t",
  "--track",
]);

function isSafeReadGitArgs(args: string[]): boolean {
  if (args.length === 0) {
    return false;
  }
  const [sub, ...rest] = args;
  switch (sub) {
    case "status":
    case "log":
    case "diff":
    case "show":
      return !rest.some((arg) => arg.startsWith("--output"));
    case "branch":
      return rest.every(
        (arg) => arg.startsWith("-") && !MUTATING_GIT_BRANCH_FLAGS.has(arg),
      );
    default:
      return false;
  }
}
