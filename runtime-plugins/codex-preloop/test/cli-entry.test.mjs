import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const entry = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "dist",
  "index.js",
);

const validConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "codex",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "codex-1",
};

function writeTempConfig(value) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-codex-cli-"));
  const file = path.join(dir, "preloop-control.json");
  fs.writeFileSync(file, JSON.stringify(value));
  return { dir, file };
}

test("verify exits 0 with a valid config", () => {
  const { file } = writeTempConfig(validConfig);
  const result = spawnSync(process.execPath, [entry, "verify", "--config", file], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /@preloop-ai\/codex-plugin verified/);
});

test("verify exits 0 when invoked through a bin-style symlink", () => {
  const { dir, file } = writeTempConfig(validConfig);
  const link = path.join(dir, "preloop-codex-plugin");
  fs.symlinkSync(entry, link);
  const result = spawnSync(process.execPath, [link, "verify", "--config", file], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /verified/);
});

test("verify exits non-zero with a clear message for a bad config", () => {
  const { file } = writeTempConfig({ runtime: "codex" });
  const result = spawnSync(process.execPath, [entry, "verify", "--config", file], {
    encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /control_ws_url is required|no usable control settings/);
});

test("verify exits non-zero when the runtime is not codex", () => {
  const { file } = writeTempConfig({ ...validConfig, runtime: "openclaw" });
  const result = spawnSync(process.execPath, [entry, "verify", "--config", file], {
    encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Expected runtime "codex"/);
});
