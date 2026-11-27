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

test("without runner.json Talk refuses the home and credential directories", async () => {
  const home = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), "talk-home-")));
  const previousHome = process.env.HOME;
  process.env.HOME = home;
  try {
    await fs.mkdir(path.join(home, ".ssh"));
    await fs.mkdir(path.join(home, ".cursor"));
    await fs.mkdir(path.join(home, "project"));
    const config = { workspace_root: home };
    assert.equal(await resolveWorkingDirectory(config, "~/project"), path.join(home, "project"));
    assert.equal(await resolveWorkingDirectory(config, path.join(home, "project")), path.join(home, "project"));
    await assert.rejects(resolveWorkingDirectory(config, home), /home directory/);
    await assert.rejects(resolveWorkingDirectory(config, path.join(home, ".ssh")), /credentials/);
    await assert.rejects(resolveWorkingDirectory(config, path.join(home, ".cursor")), /credentials/);
    await assert.rejects(
      resolveWorkingDirectory({ workspace_root: path.join(home, "missing") }, path.join(home, "project")),
      /workspace_root does not exist/,
    );
  } finally {
    process.env.HOME = previousHome;
    await fs.rm(home, { recursive: true, force: true });
  }
});
