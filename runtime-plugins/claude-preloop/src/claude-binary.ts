import { execFile } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { promisify } from "node:util";

import type { ControlConfig } from "./config.js";

const execFileAsync = promisify(execFile);

/**
 * Oldest Claude Code verified to report prompt UUIDs for streaming results.
 * This matches the bundled binary tested with this plugin.
 * The plugin tests fail when the bundled Claude Code is older than this.
 */
export const MIN_CLAUDE_CODE_VERSION = "2.1.330";

export type ClaudeExecutable = {
  /** Absolute path passed as pathToClaudeCodeExecutable; unset = bundled. */
  path?: string;
  version?: string;
  source: "config" | "path" | "bundled";
  /** Set when the binary is older than MIN_CLAUDE_CODE_VERSION. */
  warning?: string;
};

/** Compare dotted numeric versions; missing parts count as 0. */
export function compareVersions(a: string, b: string): number {
  const left = a.split(".").map((part) => Number.parseInt(part, 10) || 0);
  const right = b.split(".").map((part) => Number.parseInt(part, 10) || 0);
  for (let i = 0; i < Math.max(left.length, right.length); i += 1) {
    const diff = (left[i] ?? 0) - (right[i] ?? 0);
    if (diff !== 0) {
      return diff < 0 ? -1 : 1;
    }
  }
  return 0;
}

/** Claude Code version shipped with the installed Agent SDK. */
export function bundledClaudeCodeVersion(): string | undefined {
  try {
    const require = createRequire(import.meta.url);
    const entry = require.resolve("@anthropic-ai/claude-agent-sdk");
    const pkg = JSON.parse(
      fs.readFileSync(path.join(path.dirname(entry), "package.json"), "utf8"),
    ) as { claudeCodeVersion?: unknown };
    return typeof pkg.claudeCodeVersion === "string"
      ? pkg.claudeCodeVersion
      : undefined;
  } catch {
    return undefined;
  }
}

/** `claude --version` of an executable, e.g. "2.1.300". */
export async function probeClaudeVersion(
  executable: string,
): Promise<string | undefined> {
  try {
    const { stdout } = await execFileAsync(executable, ["--version"], {
      timeout: 5_000,
    });
    return /(\d+\.\d+\.\d+)/.exec(String(stdout))?.[1];
  } catch {
    return undefined;
  }
}

/** Whether a path is an existing executable file. */
export function isExecutableFile(candidate: string): boolean {
  try {
    if (!fs.statSync(candidate).isFile()) {
      return false;
    }
    if (process.platform !== "win32") {
      fs.accessSync(candidate, fs.constants.X_OK);
    }
    return true;
  } catch {
    return false;
  }
}

/** First executable named `command` on PATH (PATHEXT on Windows). */
export function findOnPath(
  command: string,
  searchPath: string = process.env.PATH ?? "",
  platform: NodeJS.Platform = process.platform,
  pathExt: string = process.env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD",
): string | undefined {
  const delimiter = platform === "win32" ? ";" : ":";
  const extensions =
    platform === "win32"
      ? ["", ...pathExt.split(";").filter(Boolean).map((ext) => ext.toLowerCase())]
      : [""];
  for (const dir of searchPath.split(delimiter)) {
    if (!dir || !(platform === "win32" ? path.win32 : path.posix).isAbsolute(dir)) {
      continue;
    }
    for (const ext of extensions) {
      const candidate = (platform === "win32" ? path.win32 : path.posix).join(
        dir,
        command + ext,
      );
      if (isExecutableFile(candidate)) {
        return candidate;
      }
    }
  }
  return undefined;
}

function requirePromptCorrelation(version: string | undefined): void {
  if (!version || compareVersions(version, MIN_CLAUDE_CODE_VERSION) < 0) {
    throw new Error(`Cannot verify streaming prompt correlation for Claude Code ${version ?? "unknown"}; update Claude Code to ${MIN_CLAUDE_CODE_VERSION} or newer, or remove claude_executable to use the bundled binary`);
  }
}

function olderThanMinimum(version: string | undefined): string | undefined {
  if (version && compareVersions(version, MIN_CLAUDE_CODE_VERSION) < 0) {
    return `Claude Code ${version} is older than ${MIN_CLAUDE_CODE_VERSION}; the model upstream may reject it`;
  }
  return undefined;
}

export type ClaudeExecutableDeps = {
  findClaude?: () => string | undefined;
  isExecutable?: (candidate: string) => boolean;
  probe?: (executable: string) => Promise<string | undefined>;
  bundledVersion?: () => string | undefined;
};

/**
 * The Claude Code binary owned sessions run.
 *
 * `claude_executable` from the control config wins. Otherwise the `claude`
 * on PATH is used when it is newer than the SDK's bundled Claude Code, so a
 * host that keeps Claude Code current is not held back by an older plugin
 * install. Else the bundled binary runs.
 */
export async function resolveClaudeExecutable(
  config: ControlConfig,
  deps: ClaudeExecutableDeps = {},
): Promise<ClaudeExecutable> {
  const probe = deps.probe ?? probeClaudeVersion;
  const bundled = (deps.bundledVersion ?? bundledClaudeCodeVersion)();
  if (config.claude_executable) {
    const configured = config.claude_executable;
    if (
      !path.isAbsolute(configured) ||
      !(deps.isExecutable ?? isExecutableFile)(configured)
    ) {
      throw new Error(
        `claude_executable ${configured} is not an absolute path to an executable file`,
      );
    }
    const version = await probe(configured);
    requirePromptCorrelation(version);
    return {
      path: configured,
      version,
      source: "config",
      warning: olderThanMinimum(version),
    };
  }
  const onPath = (deps.findClaude ?? (() => findOnPath("claude")))();
  if (onPath) {
    const version = await probe(onPath);
    if (version && compareVersions(version, MIN_CLAUDE_CODE_VERSION) >= 0 && (!bundled || compareVersions(version, bundled) > 0)) {
      return { path: onPath, version, source: "path" };
    }
  }
  requirePromptCorrelation(bundled);
  return { version: bundled, source: "bundled", warning: olderThanMinimum(bundled) };
}
