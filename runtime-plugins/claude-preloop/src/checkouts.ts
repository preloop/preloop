import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ControlConfig } from "./config.js";

const exec = promisify(execFile);
export type Checkout = { path: string; remote: string };

/** Normalize a git origin without sending credentials, query strings or fragments. */
export function remoteIdentity(remote: string): string | undefined {
  const value = remote.trim();
  try {
    const url = new URL(value.includes("://") ? value : value.replace(/^([^@]+@)?([^:]+):/, "ssh://$2/"));
    if (!["https:", "http:", "ssh:"].includes(url.protocol)) return undefined;
    const repository = url.pathname.replace(/^\//, "").replace(/\/$/, "").replace(/\.git$/, "");
    if (!url.hostname || !repository.includes("/")) return undefined;
    return `${url.host}/${repository}`.toLowerCase();
  } catch { return undefined; }
}

/** Observe only known directories. Never scan or clone arbitrary repositories. */
export async function knownCheckouts(config: ControlConfig, recent: string[] = []): Promise<Checkout[]> {
  const directories = new Set(recent);
  if (config.workspace_root) directories.add(config.workspace_root);
  try {
    const runner = JSON.parse(await fs.readFile(config.runner_config_path ?? path.join(os.homedir(), ".preloop", "runner.json"), "utf8"));
    for (const entry of runner.authorized_directories ?? []) {
      if (typeof entry.path === "string") directories.add(entry.path);
    }
  } catch { /* No runner directory inventory. */ }
  const checkouts: Checkout[] = [];
  for (const directory of [...directories].slice(0, 50)) {
    try {
      const cwd = await fs.realpath(directory);
      const options = { cwd, timeout: 500, maxBuffer: 4096 };
      const root = (await exec("git", ["rev-parse", "--show-toplevel"], options)).stdout.trim();
      const remote = remoteIdentity((await exec("git", ["remote", "get-url", "origin"], options)).stdout);
      if (remote && !checkouts.some(item => item.path === root)) checkouts.push({ path: root, remote });
    } catch { /* An absent checkout is not a project choice. */ }
  }
  return checkouts;
}
