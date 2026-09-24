import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { loadConfig, loadConfigDetailed, verifyConfig } from "../dist/config.js";

const validConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "codex",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "codex-1",
  runtime_principal_name: "Codex CLI",
  workspace_root: "/tmp/workspace",
};

function writeTempConfig(value) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-codex-"));
  const file = path.join(dir, "preloop-control.json");
  fs.writeFileSync(file, JSON.stringify(value));
  return file;
}

test("loads a flat config file", () => {
  const file = writeTempConfig(validConfig);
  const config = loadConfig(file);
  assert.equal(config.runtime, "codex");
  assert.equal(config.bearer_token, "agt_secret");
});

test("loads a nested control block", () => {
  const file = writeTempConfig({ control: validConfig });
  const config = loadConfig(file);
  assert.equal(config.runtime_principal_id, "codex-1");
});

test("verify passes for a valid config", () => {
  verifyConfig(validConfig);
});

test("verify rejects a foreign runtime", () => {
  assert.throws(
    () => verifyConfig({ ...validConfig, runtime: "claude_code" }),
    /Expected runtime "codex"/,
  );
});

test("verify rejects missing required keys", () => {
  for (const key of ["control_ws_url", "bearer_token", "runtime_principal_id"]) {
    const broken = { ...validConfig };
    delete broken[key];
    assert.throws(() => verifyConfig(broken), new RegExp(key));
  }
});

test("verify rejects danger-full-access without the opt-in flag", () => {
  assert.throws(
    () =>
      verifyConfig({
        ...validConfig,
        codex_sandbox_mode: "danger-full-access",
      }),
    /codex_allow_full_access/,
  );
  verifyConfig({
    ...validConfig,
    codex_sandbox_mode: "danger-full-access",
    codex_allow_full_access: true,
  });
  verifyConfig({ ...validConfig, codex_sandbox_mode: "read-only" });
});

test("loadConfigDetailed flags a config with no usable settings as empty", () => {
  const file = writeTempConfig({ something_else: true });
  assert.equal(loadConfigDetailed(file).source, "empty");
});

test("loadConfigDetailed reports the nested control-block source", () => {
  const file = writeTempConfig({ control: validConfig });
  const loaded = loadConfigDetailed(file);
  assert.equal(loaded.source, "control-block");
  assert.equal(loaded.config.runtime, "codex");
});
