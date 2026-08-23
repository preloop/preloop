// Config loading/verification. Run `npm run build` first, then node --test.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { loadConfig, loadConfigDetailed, verifyConfig } from "../dist/config.js";

const validConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
  runtime_principal_name: "Claude Code",
};

function writeTempConfig(value) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-claude-"));
  const file = path.join(dir, "preloop-control.json");
  fs.writeFileSync(file, JSON.stringify(value));
  return file;
}

test("loads a flat config file", () => {
  const file = writeTempConfig(validConfig);
  const config = loadConfig(file);
  assert.equal(config.runtime, "claude_code");
  assert.equal(config.bearer_token, "agt_secret");
});

test("loads a nested control block", () => {
  const file = writeTempConfig({ control: validConfig });
  const config = loadConfig(file);
  assert.equal(config.runtime_principal_id, "claude-code-1");
});

test("verify passes for a valid config", () => {
  verifyConfig(validConfig);
});

test("verify rejects a foreign runtime", () => {
  assert.throws(
    () => verifyConfig({ ...validConfig, runtime: "openclaw" }),
    /Expected runtime "claude_code"/,
  );
});

test("verify rejects missing required keys", () => {
  for (const key of [
    "control_ws_url",
    "bearer_token",
    "runtime_principal_id",
  ]) {
    const broken = { ...validConfig };
    delete broken[key];
    assert.throws(() => verifyConfig(broken), new RegExp(key));
  }
});

test("verify rejects an unknown protocol", () => {
  assert.throws(
    () => verifyConfig({ ...validConfig, protocol: "preloop.v999" }),
    /Unsupported protocol/,
  );
});

test("loadConfigDetailed reports the nested control-block source", () => {
  const file = writeTempConfig({ control: validConfig });
  const loaded = loadConfigDetailed(file);
  assert.equal(loaded.source, "control-block");
  assert.equal(loaded.path, file);
  assert.equal(loaded.config.runtime, "claude_code");
});

test("loadConfigDetailed reports the flat source", () => {
  const file = writeTempConfig(validConfig);
  const loaded = loadConfigDetailed(file);
  assert.equal(loaded.source, "flat");
  assert.equal(loaded.config.bearer_token, "agt_secret");
});

test("loadConfigDetailed flags a config with no usable settings as empty", () => {
  // The silent-idle bug: a file that parses fine but carries no control
  // settings under either shape must be distinguishable from a good one.
  const file = writeTempConfig({ something_else: true });
  const loaded = loadConfigDetailed(file);
  assert.equal(loaded.source, "empty");
});

test("a nested control block that is an array is not treated as settings", () => {
  const file = writeTempConfig({ control: ["nope"] });
  const loaded = loadConfigDetailed(file);
  assert.equal(loaded.source, "empty");
});
