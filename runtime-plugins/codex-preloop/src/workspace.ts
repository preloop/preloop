// Ported from runtime-plugins/claude-preloop/src/workspace.ts.
// Kept in step with that file. This sidecar does not create git worktrees,
// so the default worktree callback refuses spawn_worktree instead of calling
// createGitWorktree from the Claude session layer.

import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import type { ControlConfig } from "./config.js";

const execFileAsync = promisify(execFile);

export const DEFAULT_REPOSITORIES_MAX = 20;
export const DEFAULT_FETCH_TIMEOUT_MS = 120_000;

/** Checkout identity from a persistent send_message. No credentials. */
export type WorkspaceSpec = {
  mode?: string;
  repository_url?: string;
  repository_slug?: string;
  default_branch?: string;
  ref?: string;
  fetch_ref?: string;
  sha?: string;
  pr_number?: number | null;
};

export type GitRunResult = {
  stdout: string;
  stderr: string;
  code: number;
};

export type GitRunner = (
  args: string[],
  options: { cwd?: string; timeoutMs?: number },
) => Promise<GitRunResult>;

export class WorkspaceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WorkspaceError";
  }
}

async function refuseGitWorktree(_repoRoot: string): Promise<string> {
  throw new WorkspaceError("the Codex sidecar does not create git worktrees");
}

export function workspaceRepositoriesMax(config: ControlConfig): number {
  const value = config.workspace_repositories_max;
  if (typeof value === "number" && Number.isFinite(value) && value >= 1) {
    return Math.floor(value);
  }
  return DEFAULT_REPOSITORIES_MAX;
}

export function workspaceFetchTimeoutMs(config: ControlConfig): number {
  const value = config.workspace_fetch_timeout_ms;
  if (typeof value === "number" && Number.isFinite(value) && value >= 1) {
    return Math.floor(value);
  }
  return DEFAULT_FETCH_TIMEOUT_MS;
}

export async function defaultGitRunner(
  args: string[],
  options: { cwd?: string; timeoutMs?: number },
): Promise<GitRunResult> {
  const safeArgs = assertGitArgs(args);
  try {
    const { stdout, stderr } = await execFileAsync("git", safeArgs, {
      cwd: options.cwd,
      timeout: options.timeoutMs,
      maxBuffer: 8 * 1024 * 1024,
    });
    return { stdout: String(stdout), stderr: String(stderr), code: 0 };
  } catch (error) {
    const failed = error as {
      code?: number | string;
      stdout?: string;
      stderr?: string;
      message?: string;
      killed?: boolean;
    };
    if (failed.killed || failed.code === "ETIMEDOUT") {
      throw new WorkspaceError(
        `git ${args[0] ?? "command"} timed out after ${options.timeoutMs ?? 0}ms`,
      );
    }
    return {
      stdout: String(failed.stdout ?? ""),
      stderr: String(failed.stderr ?? failed.message ?? ""),
      code: typeof failed.code === "number" ? failed.code : 1,
    };
  }
}

function refuseUnsafeGitText(arg: string): void {
  if (arg.includes("upload-pack") || arg.includes("\n") || arg.includes("\0")) {
    throw new WorkspaceError("refusing unsafe git argument");
  }
}

/**
 * Guard a git argv list.
 *
 * `execFile` does not invoke a shell, so a space or backslash in a
 * manager-built path is not an injection vector. Newlines, NULs, and
 * `upload-pack` overrides are refused on every argument. The character
 * check for caller-supplied remotes and refs lives in `assertRemoteToken`.
 */
const GIT_TOKEN =
  /^(?:--|-c|-{1,2}[A-Za-z][A-Za-z0-9._:=/@+-]*|[A-Za-z0-9][A-Za-z0-9._:=/@+-]*)$/;
const SLUG_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._:=@+-]*$/;

export function assertGitArgs(args: string[]): string[] {
  return args.map((arg) => {
    refuseUnsafeGitText(arg);
    const matched = GIT_TOKEN.exec(arg);
    if (!matched) {
      throw new WorkspaceError("refusing unsafe git argument");
    }
    return matched[0];
  });
}

const REMOTE_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:@/+-]*$/;

function assertRemoteToken(value: string): string {
  refuseUnsafeGitText(value);
  if (!REMOTE_TOKEN.test(value)) {
    throw new WorkspaceError("refusing unsafe git argument");
  }
  return value;
}

function assertSafeSlug(slug: string): string {
  let text = slug.trim();
  while (text.startsWith("/")) {
    text = text.slice(1);
  }
  while (text.endsWith("/")) {
    text = text.slice(0, -1);
  }
  const parts = text.split("/");
  if (!text || parts.some((part) => part === ".." || part === "")) {
    throw new WorkspaceError(
      `repository_slug ${JSON.stringify(slug)} is not a safe checkout path`,
    );
  }
  if (parts.some((part) => !SLUG_SEGMENT.test(part))) {
    throw new WorkspaceError(
      "repository_slug contains characters not allowed in a git argument",
    );
  }
  return text;
}

function assertCloneUrl(url: string): void {
  refuseUnsafeGitText(url);
  const schemeSep = url.indexOf("://");
  if (schemeSep === -1) {
    return;
  }
  const scheme = url.slice(0, schemeSep).toLowerCase();
  const rest = url.slice(schemeSep + 3);
  const cut = rest.search(/[/?#]/);
  const authority = cut === -1 ? rest : rest.slice(0, cut);
  const at = authority.lastIndexOf("@");
  if (at !== -1 && authority.slice(0, at).includes(":")) {
    throw new WorkspaceError(
      "refusing to clone with a password in repository_url; the host uses its own git credentials",
    );
  }
  if (scheme && !["https", "http", "ssh", "git"].includes(scheme)) {
    throw new WorkspaceError(`refusing clone scheme ${scheme}`);
  }
}

/**
 * Host checkouts for persistent flow executions.
 *
 * One directory per repository under workspace_root. Git operations on a
 * directory are serialized. A dirty tree the sidecar did not just check out
 * clean is left alone: the command fails instead of reset or clean.
 */
export class WorkspaceManager {
  private readonly tails = new Map<string, Promise<void>>();
  /** Absolute checkout paths, oldest first. */
  private readonly lru: string[] = [];
  /** Directories currently inside prepare. Eviction skips them without locking. */
  private readonly preparing = new Set<string>();
  /** Directories whose turn is still running. Eviction skips them. */
  private readonly inUse = new Set<string>();
  /** Turn cwd to the repository directory eviction must also skip. */
  private readonly checkoutRoot = new Map<string, string>();

  constructor(
    private readonly config: ControlConfig,
    private readonly git: GitRunner = defaultGitRunner,
    private readonly worktree: (repoRoot: string) => Promise<string> = refuseGitWorktree,
  ) {}

  async prepare(spec: WorkspaceSpec, spawnWorktree = false): Promise<string> {
    if (spec.mode !== "persistent_checkout") {
      throw new WorkspaceError(
        `workspace mode ${String(spec.mode)} is not a persistent checkout`,
      );
    }
    const slug = assertSafeSlug(String(spec.repository_slug ?? ""));
    const root = this.config.workspace_root;
    if (!root) {
      throw new WorkspaceError(
        "workspace_root is not configured; cannot check out a persistent repository",
      );
    }
    const repoDir = path.resolve(root, slug);
    const rootResolved = path.resolve(root);
    if (!repoDir.startsWith(rootResolved + path.sep)) {
      throw new WorkspaceError(
        `repository_slug ${slug} escapes workspace_root`,
      );
    }
    // The Claude sidecar creates a worktree after checkout. This sidecar
    // refuses before clone or fetch so a rejected request does not touch git.
    if (spawnWorktree) {
      await this.worktree(repoDir);
    }
    this.preparing.add(repoDir);
    try {
      const cwd = await this.exclusive(repoDir, () =>
        this.prepareLocked(repoDir, spec, spawnWorktree),
      );
      await this.evict(repoDir);
      return cwd;
    } finally {
      this.preparing.delete(repoDir);
    }
  }

  /** Keep a checkout out of LRU eviction until the turn finishes. */
  hold(dir: string): void {
    this.inUse.add(dir);
    const root = this.checkoutRoot.get(dir);
    if (root) {
      this.inUse.add(root);
    }
  }

  release(dir: string): void {
    const root = this.checkoutRoot.get(dir);
    this.inUse.delete(dir);
    if (root) {
      this.inUse.delete(root);
    }
  }

  private exclusive<T>(key: string, fn: () => Promise<T>): Promise<T> {
    const previous = this.tails.get(key) ?? Promise.resolve();
    const run = previous.then(fn, fn);
    this.tails.set(
      key,
      run.then(
        () => undefined,
        () => undefined,
      ),
    );
    return run;
  }

  private async prepareLocked(
    repoDir: string,
    spec: WorkspaceSpec,
    spawnWorktree: boolean,
  ): Promise<string> {
    await fs.mkdir(path.dirname(repoDir), { recursive: true });
    const gitDir = path.join(repoDir, ".git");
    let exists = false;
    try {
      await fs.access(gitDir);
      exists = true;
    } catch {
      exists = false;
    }
    if (!exists) {
      await this.clone(repoDir, spec);
    }
    await this.refuseDirty(repoDir);
    await this.fetch(repoDir, spec);
    await this.checkout(repoDir, spec);
    await this.markManaged(repoDir);
    this.touch(repoDir);
    const cwd = spawnWorktree ? await this.worktree(repoDir) : repoDir;
    this.checkoutRoot.set(cwd, repoDir);
    return cwd;
  }

  private async clone(repoDir: string, spec: WorkspaceSpec): Promise<void> {
    const url = (spec.repository_url ?? "").trim();
    if (!url) {
      throw new WorkspaceError(
        `cannot clone ${spec.repository_slug}: repository_url is missing`,
      );
    }
    assertCloneUrl(url);
    const args = [
      "-c",
      "protocol.ext.allow=never",
      "-c",
      "protocol.file.allow=never",
      "clone",
    ];
    // Destination is the slug's final segment. workspace_root may contain
    // spaces, and those stay in cwd rather than argv so a path cannot be
    // read as a git option.
    args.push("--", url, path.basename(repoDir));
    const result = await this.git(args, {
      cwd: path.dirname(repoDir),
      timeoutMs: workspaceFetchTimeoutMs(this.config),
    });
    if (result.code !== 0) {
      throw new WorkspaceError(
        `git clone failed for ${spec.repository_slug}: ${result.stderr.trim() || "unknown error"}`,
      );
    }
  }

  private async fetch(repoDir: string, spec: WorkspaceSpec): Promise<void> {
    const candidates = [
      spec.fetch_ref,
      spec.sha,
      spec.ref,
      spec.default_branch,
    ]
      .map((value) => (value ?? "").trim())
      .filter((value) => value.length > 0);
    const unique = [...new Set(candidates)];
    if (unique.length === 0) {
      throw new WorkspaceError(
        `git fetch failed for ${spec.repository_slug}: no ref to fetch`,
      );
    }
    let lastError = "unknown error";
    for (const ref of unique) {
      assertRemoteToken(ref);
      const result = await this.git(
        ["-c", "protocol.ext.allow=never", "fetch", "origin", ref],
        {
          cwd: repoDir,
          timeoutMs: workspaceFetchTimeoutMs(this.config),
        },
      );
      if (result.code === 0) {
        return;
      }
      lastError = result.stderr.trim() || lastError;
    }
    throw new WorkspaceError(
      `git fetch failed for ${spec.repository_slug}: ${lastError}`,
    );
  }

  private async checkout(repoDir: string, spec: WorkspaceSpec): Promise<void> {
    const target = (spec.sha || spec.ref || spec.default_branch || "").trim();
    if (!target) {
      throw new WorkspaceError(
        `cannot check out ${spec.repository_slug}: no sha or ref`,
      );
    }
    await this.refuseDirty(repoDir);
    assertRemoteToken(target);
    const result = await this.git(["checkout", "--detach", target], {
      cwd: repoDir,
      timeoutMs: workspaceFetchTimeoutMs(this.config),
    });
    if (result.code !== 0) {
      throw new WorkspaceError(
        `git checkout failed for ${spec.repository_slug} at ${target}: ${result.stderr.trim() || "unknown error"}`,
      );
    }
  }

  private async markManaged(repoDir: string): Promise<void> {
    const result = await this.git(
      ["config", "preloop.managedcheckout", "1"],
      { cwd: repoDir },
    );
    if (result.code !== 0) {
      throw new WorkspaceError(
        `could not record managed checkout in ${repoDir}`,
      );
    }
  }

  private async isManaged(repoDir: string): Promise<boolean> {
    try {
      const result = await this.git(
        ["config", "--get", "preloop.managedcheckout"],
        { cwd: repoDir },
      );
      return result.code === 0 && result.stdout.trim() === "1";
    } catch {
      return false;
    }
  }

  private async refuseDirty(repoDir: string): Promise<void> {
    let stat: GitRunResult;
    try {
      stat = await this.git(["status", "--porcelain"], { cwd: repoDir });
    } catch (error) {
      throw new WorkspaceError(
        `checkout ${repoDir} could not be inspected: ${error instanceof Error ? error.message : "unknown error"}`,
      );
    }
    const dirty = stat.stdout.trim().length > 0 || stat.code !== 0;
    if (!dirty) {
      return;
    }
    const managed = await this.isManaged(repoDir);
    const reason = managed
      ? "a previous persistent turn left uncommitted changes (preloop.managedcheckout is set)"
      : "uncommitted changes the sidecar did not make";
    throw new WorkspaceError(
      `checkout ${repoDir} has ${reason}; refusing to reset or clean it. Paths: ${stat.stdout.trim() || "unreadable"}`,
    );
  }

  private touch(repoDir: string): void {
    const index = this.lru.indexOf(repoDir);
    if (index >= 0) {
      this.lru.splice(index, 1);
    }
    this.lru.push(repoDir);
  }

  private async evict(current: string): Promise<void> {
    const max = workspaceRepositoriesMax(this.config);
    while (this.lru.length > max) {
      let removed = false;
      for (const candidate of [...this.lru]) {
        if (candidate === current) {
          continue;
        }
        const deleted = await this.exclusive(candidate, async () => {
          if (
            this.inUse.has(candidate) ||
            this.preparing.has(candidate) ||
            (await this.isDirty(candidate))
          ) {
            return false;
          }
          const at = this.lru.indexOf(candidate);
          if (at < 0) {
            return false;
          }
          this.lru.splice(at, 1);
          await fs.rm(candidate, { recursive: true, force: true });
          return true;
        });
        if (!deleted) {
          continue;
        }
        removed = true;
        break;
      }
      if (!removed) {
        return;
      }
    }
  }

  private async isDirty(repoDir: string): Promise<boolean> {
    try {
      const stat = await this.git(["status", "--porcelain"], { cwd: repoDir });
      return stat.code !== 0 || stat.stdout.trim().length > 0;
    } catch {
      return true;
    }
  }
}
