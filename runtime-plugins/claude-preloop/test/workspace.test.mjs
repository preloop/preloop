import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { WorkspaceManager } from "../dist/workspace.js";

function spec(slug, overrides = {}) {
  return {
    mode: "persistent_checkout",
    repository_url: `https://github.com/${slug}.git`,
    repository_slug: slug,
    default_branch: "main",
    ref: "feature",
    sha: "a".repeat(40),
    pr_number: 1,
    clone_depth: null,
    submodules: false,
    ...overrides,
  };
}

function gitSubcommand(args) {
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "-c" || arg === "-C") {
      index += 1;
      continue;
    }
    if (arg === "--") {
      break;
    }
    if (arg.startsWith("-")) {
      continue;
    }
    return arg;
  }
  return args[0];
}

function makeGit(state) {
  let inFlight = 0;
  return async (args, options) => {
    inFlight += 1;
    state.maxInFlight = Math.max(state.maxInFlight, inFlight);
    state.calls.push({ args: [...args], cwd: options.cwd });
    await new Promise((resolve) => setTimeout(resolve, state.delayMs ?? 0));
    inFlight -= 1;
    const command = gitSubcommand(args);
    if (command === "clone") {
      const dest = args[args.length - 1];
      await fs.mkdir(path.join(dest, ".git"), { recursive: true });
      return { stdout: "", stderr: "", code: 0 };
    }
    if (command === "config") {
      state.config ??= new Map();
      if (args.includes("--get")) {
        const key = args[args.length - 1];
        const value = state.config.get(`${options.cwd}:${key}`) ?? "";
        return {
          stdout: value ? `${value}\n` : "",
          stderr: "",
          code: value ? 0 : 1,
        };
      }
      const value = args[args.length - 1];
      const name = args[args.length - 2];
      state.config.set(`${options.cwd}:${name}`, value);
      return { stdout: "", stderr: "", code: 0 };
    }
    if (command === "status") {
      const cwd = options.cwd;
      const dirty = state.dirty.has(cwd);
      return { stdout: dirty ? " M README.md\n" : "", stderr: "", code: 0 };
    }
    if (args.includes("reset") || args.includes("clean")) {
      state.destructive.push(args);
    }
    return { stdout: "", stderr: "", code: 0 };
  };
}

async function tempRoot() {
  return fs.mkdtemp(path.join(os.tmpdir(), "preloop-ws-"));
}

test("first execution clones once; second fetches and checks out only", async () => {
  const root = await tempRoot();
  const state = { calls: [], dirty: new Set(), destructive: [], maxInFlight: 0 };
  const manager = new WorkspaceManager(
    { workspace_root: root, workspace_repositories_max: 20 },
    makeGit(state),
  );
  const first = await manager.prepare(spec("example/repo"));
  const second = await manager.prepare(spec("example/repo"));
  assert.equal(first, second);
  const clones = state.calls.filter((call) => call.args.includes("clone"));
  const fetches = state.calls.filter((call) => call.args[0] === "fetch" || call.args.includes("fetch"));
  const checkouts = state.calls.filter((call) => call.args.includes("checkout"));
  assert.equal(clones.length, 1);
  assert.ok(clones[0].args.includes("protocol.ext.allow=never"));
  const firstCheckout = state.calls.findIndex((call) => call.args.includes("checkout"));
  const fetchBeforeCheckout = state.calls
    .slice(0, firstCheckout)
    .some((call) => call.args.includes("fetch"));
  assert.equal(fetchBeforeCheckout, true);
  assert.ok(fetches.length >= 1);
  assert.ok(checkouts.length >= 2);
  assert.equal(state.destructive.length, 0);
});

test("concurrent commands on one repository serialize git operations", async () => {
  const root = await tempRoot();
  const state = {
    calls: [],
    dirty: new Set(),
    destructive: [],
    maxInFlight: 0,
    delayMs: 20,
  };
  const manager = new WorkspaceManager(
    { workspace_root: root },
    makeGit(state),
  );
  await Promise.all([
    manager.prepare(spec("example/repo")),
    manager.prepare(spec("example/repo", { sha: "b".repeat(40) })),
  ]);
  assert.equal(state.maxInFlight, 1);
});

test("dirty checkout the sidecar did not create fails without reset or clean", async () => {
  const root = await tempRoot();
  const repo = path.join(root, "example", "repo");
  await fs.mkdir(path.join(repo, ".git"), { recursive: true });
  const state = { calls: [], dirty: new Set([repo]), destructive: [], maxInFlight: 0 };
  const manager = new WorkspaceManager(
    { workspace_root: root },
    makeGit(state),
  );
  await assert.rejects(
    () => manager.prepare(spec("example/repo")),
    /uncommitted changes the sidecar did not make/,
  );
  assert.equal(state.destructive.length, 0);
  assert.equal(
    state.calls.some((call) => call.args.includes("reset") || call.args.includes("clean")),
    false,
  );
});

test("LRU eviction skips dirty directories", async () => {
  const root = await tempRoot();
  const state = { calls: [], dirty: new Set(), destructive: [], maxInFlight: 0 };
  const manager = new WorkspaceManager(
    { workspace_root: root, workspace_repositories_max: 2 },
    makeGit(state),
  );
  const oldest = await manager.prepare(spec("example/old"));
  const dirty = await manager.prepare(spec("example/dirty"));
  state.dirty.add(dirty);
  const newest = await manager.prepare(spec("example/new"));
  await assert.rejects(() => fs.access(path.join(oldest, ".git")));
  await fs.access(path.join(dirty, ".git"));
  await fs.access(path.join(newest, ".git"));
});

test("ssh git user is allowed and a password is refused", async () => {
  const root = await tempRoot();
  const state = { calls: [], dirty: new Set(), destructive: [], maxInFlight: 0 };
  const manager = new WorkspaceManager({ workspace_root: root }, makeGit(state));
  const checkedOut = await manager.prepare(
    spec("example/repo", {
      repository_url: "ssh://git@github.com/example/repo.git",
    }),
  );
  assert.equal(checkedOut, path.join(root, "example", "repo"));
  await assert.rejects(
    () =>
      manager.prepare(
        spec("example/other", {
          repository_url: "ssh://git:secret@github.com/example/other.git",
        }),
      ),
    /password in repository_url/,
  );
});

test("managed dirty checkout fails without reset or clean", async () => {
  const root = await tempRoot();
  const state = { calls: [], dirty: new Set(), destructive: [], maxInFlight: 0 };
  const manager = new WorkspaceManager({ workspace_root: root }, makeGit(state));
  const repo = await manager.prepare(spec("example/repo"));
  state.dirty.add(repo);
  await assert.rejects(
    () => manager.prepare(spec("example/repo")),
    /preloop.managedcheckout/,
  );
  assert.equal(state.destructive.length, 0);
});
