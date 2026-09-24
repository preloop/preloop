import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import type { ControlConfig } from "./config.js";
import { createGitWorktree } from "./sessions.js";

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
  sha?: string;
  pr_number?: number | null;
  clone_depth?: number | null;
  submodules?: boolean;
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
  try {
    const { stdout, stderr } = await execFileAsync("git", args, {
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

function assertSafeSlug(slug: string): string {
  const text = slug.trim().replace(/^\/+|\/+$/g, "");
  if (!text || text.split("/").some((part) => part === ".." || part === "")) {
    throw new WorkspaceError(
      `repository_slug ${JSON.stringify(slug)} is not a safe checkout path`,
    );
  }
  return text;
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
  /** Directories this process cloned or checked out clean. */
  private readonly sidecarClean = new Set<string>();

  constructor(
    private readonly config: ControlConfig,
    private readonly git: GitRunner = defaultGitRunner,
    private readonly worktree: (repoRoot: string) => Promise<string> = createGitWorktree,
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
    return this.exclusive(repoDir, () =>
      this.prepareLocked(repoDir, spec, spawnWorktree),
    );
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
    } else {
      await this.refuseForeignDirty(repoDir);
      await this.fetch(repoDir, spec);
    }
    await this.checkout(repoDir, spec);
    this.touch(repoDir);
    await this.evict();
    if (spawnWorktree) {
      return this.worktree(repoDir);
    }
    return repoDir;
  }

  private async clone(repoDir: string, spec: WorkspaceSpec): Promise<void> {
    const url = (spec.repository_url ?? "").trim();
    if (!url) {
      throw new WorkspaceError(
        `cannot clone ${spec.repository_slug}: repository_url is missing`,
      );
    }
    if (/^[a-z+]+:\/\/[^/]*@/i.test(url)) {
      throw new WorkspaceError(
        "refusing to clone with credentials in repository_url; the host uses its own git credentials",
      );
    }
    const args = ["clone"];
    if (typeof spec.clone_depth === "number" && spec.clone_depth > 0) {
      args.push("--depth", String(spec.clone_depth));
    }
    if (spec.submodules) {
      args.push("--recurse-submodules");
    }
    args.push("--", url, repoDir);
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
    const ref = (spec.ref || spec.default_branch || "HEAD").trim();
    const result = await this.git(["fetch", "origin", ref], {
      cwd: repoDir,
      timeoutMs: workspaceFetchTimeoutMs(this.config),
    });
    if (result.code !== 0) {
      throw new WorkspaceError(
        `git fetch failed for ${spec.repository_slug} ref ${ref}: ${result.stderr.trim() || "unknown error"}`,
      );
    }
    if (spec.sha) {
      const shaFetch = await this.git(["fetch", "origin", spec.sha], {
        cwd: repoDir,
        timeoutMs: workspaceFetchTimeoutMs(this.config),
      });
      if (shaFetch.code !== 0) {
        throw new WorkspaceError(
          `git fetch failed for ${spec.repository_slug} sha ${spec.sha}: ${shaFetch.stderr.trim() || "unknown error"}`,
        );
      }
    }
  }

  private async checkout(repoDir: string, spec: WorkspaceSpec): Promise<void> {
    const target = (spec.sha || spec.ref || spec.default_branch || "").trim();
    if (!target) {
      throw new WorkspaceError(
        `cannot check out ${spec.repository_slug}: no sha or ref`,
      );
    }
    await this.refuseForeignDirty(repoDir);
    const result = await this.git(["checkout", "--detach", target], {
      cwd: repoDir,
      timeoutMs: workspaceFetchTimeoutMs(this.config),
    });
    if (result.code !== 0) {
      throw new WorkspaceError(
        `git checkout failed for ${spec.repository_slug} at ${target}: ${result.stderr.trim() || "unknown error"}`,
      );
    }
    this.sidecarClean.add(repoDir);
  }

  private async refuseForeignDirty(repoDir: string): Promise<void> {
    let stat: GitRunResult;
    try {
      stat = await this.git(["status", "--porcelain"], { cwd: repoDir });
    } catch (error) {
      if (!this.sidecarClean.has(repoDir)) {
        throw new WorkspaceError(
          `checkout ${repoDir} could not be inspected and was not created by the sidecar`,
        );
      }
      throw error;
    }
    const dirty = stat.stdout.trim().length > 0 || stat.code !== 0;
    if (!dirty) {
      return;
    }
    if (this.sidecarClean.has(repoDir)) {
      return;
    }
    throw new WorkspaceError(
      `checkout ${repoDir} has uncommitted changes the sidecar did not make; refusing to reset or clean it`,
    );
  }

  private touch(repoDir: string): void {
    const index = this.lru.indexOf(repoDir);
    if (index >= 0) {
      this.lru.splice(index, 1);
    }
    this.lru.push(repoDir);
  }

  private async evict(): Promise<void> {
    const max = workspaceRepositoriesMax(this.config);
    while (this.lru.length > max) {
      let removed = false;
      for (let index = 0; index < this.lru.length; index += 1) {
        const candidate = this.lru[index];
        if (await this.isDirty(candidate)) {
          continue;
        }
        this.lru.splice(index, 1);
        this.sidecarClean.delete(candidate);
        await fs.rm(candidate, { recursive: true, force: true });
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
