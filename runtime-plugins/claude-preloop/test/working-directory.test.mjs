import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { resolveWorkingDirectory } from "../dist/workspace.js";

test("a selected directory must remain within the authorized real root", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "talk-directory-"));
  try {
    await fs.mkdir(path.join(root, "project"));
    await fs.symlink(os.tmpdir(), path.join(root, "escape"));
    const configPath = path.join(root, "runner.json");
    await fs.writeFile(configPath, JSON.stringify({ authorized_directories: [{ path: path.join(await fs.realpath(root), "project"), mode: "write" }] }));
    assert.equal(await resolveWorkingDirectory({ workspace_root: root, runner_config_path: configPath }, path.join(root, "project")), path.join(await fs.realpath(root), "project"));
    await assert.rejects(resolveWorkingDirectory({ workspace_root: root, runner_config_path: configPath }, path.join(root, "escape")), /authorized/);
    await assert.rejects(resolveWorkingDirectory({ workspace_root: root, runner_config_path: configPath }, "../outside"), /absolute/);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});


test("Talk respects an existing read-only authorized directory", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "talk-authorized-"));
  try {
    const directory = await fs.realpath(root);
    const configPath = path.join(root, "runner.json");
    await fs.writeFile(configPath, JSON.stringify({ authorized_directories: [{ path: directory, mode: "read_only" }] }));
    await assert.rejects(resolveWorkingDirectory({ workspace_root: root, runner_config_path: configPath }, directory), /authorized|writable/);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});
