// Unit tests for the native-tool approval gating (before_tool_call hook).
// Uses Node's built-in test runner against the built output: run `npm run build`
// first, then `npm test` (node --test).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  PreloopOpenClawPlugin,
  resolveOpenClawClientDecision,
} from "../dist/index.js";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const baseConfig = {
  runtime: "openclaw",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "secret-token",
  runtime_principal_id: "principal-1",
};

function makePlugin(fetchImpl, overrides = {}) {
  const plugin = new PreloopOpenClawPlugin(undefined, fetchImpl);
  plugin.configure({ ...baseConfig, ...overrides });
  return plugin;
}

function jsonResponse(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

test("permissionCheckUrl derives https base from the wss control URL", () => {
  const plugin = makePlugin();
  assert.equal(
    plugin.permissionCheckUrl(baseConfig),
    "https://example.preloop.ai/api/v1/agents/permission-check",
  );
});

test("permissionCheckUrl derives http base from a ws control URL", () => {
  const plugin = makePlugin(undefined, {
    control_ws_url: "ws://localhost:8000/api/v1/agents/control/ws",
  });
  assert.equal(
    plugin.permissionCheckUrl({
      control_ws_url: "ws://localhost:8000/api/v1/agents/control/ws",
    }),
    "http://localhost:8000/api/v1/agents/permission-check",
  );
});

test("deny decision blocks the tool with the supplied reason", async () => {
  let captured;
  const plugin = makePlugin((url, init) => {
    captured = { url, init };
    return jsonResponse(200, {
      decision: "deny",
      reason: "Operator declined",
      request_id: "req-1",
    });
  });

  const result = await plugin.checkToolPermission(
    { toolName: "exec", params: { cmd: "rm -rf /" } },
    { sessionId: "sess-1" },
  );

  assert.deepEqual(result, { block: true, blockReason: "Operator declined" });
  assert.equal(
    captured.url,
    "https://example.preloop.ai/api/v1/agents/permission-check",
  );
  assert.equal(captured.init.method, "POST");
  assert.equal(captured.init.headers.authorization, "Bearer secret-token");
  const body = JSON.parse(captured.init.body);
  assert.equal(body.source, "openclaw");
  assert.equal(body.tool_name, "exec");
  assert.deepEqual(body.tool_input, { cmd: "rm -rf /" });
  assert.equal(body.session_id, "sess-1");
});

test("allow decision lets the tool run (returns undefined)", async () => {
  const plugin = makePlugin(() => jsonResponse(200, { decision: "allow" }));
  const result = await plugin.checkToolPermission(
    { toolName: "Read", params: { path: "/tmp/x" } },
    { sessionKey: "sess-2" },
  );
  assert.equal(result, undefined);
});

test("network error fails closed (blocks) by default", async () => {
  const plugin = makePlugin(() => Promise.reject(new Error("boom")));
  const result = await plugin.checkToolPermission(
    { toolName: "exec", params: {} },
    {},
  );
  assert.equal(result.block, true);
  assert.match(result.blockReason, /failing closed/);
});

test("network error fails open when configured", async () => {
  const plugin = makePlugin(() => Promise.reject(new Error("boom")), {
    tool_approval_fail_open: true,
  });
  const result = await plugin.checkToolPermission(
    { toolName: "exec", params: {} },
    {},
  );
  assert.equal(result, undefined);
});

test("disabled approval short-circuits to allow", async () => {
  let called = false;
  const plugin = makePlugin(
    () => {
      called = true;
      return jsonResponse(200, { decision: "deny" });
    },
    { tool_approval_enabled: false },
  );
  const result = await plugin.checkToolPermission(
    { toolName: "exec", params: {} },
    {},
  );
  assert.equal(result, undefined);
  assert.equal(called, false);
});

test("deny decision request includes client_decision ask by default", async () => {
  let captured;
  const plugin = makePlugin((url, init) => {
    captured = { url, init };
    return jsonResponse(200, { decision: "allow" });
  });
  await plugin.checkToolPermission(
    { toolName: "Read", params: { path: "/tmp/x" } },
    { sessionId: "sess-ask" },
  );
  const body = JSON.parse(captured.init.body);
  assert.equal(body.client_decision, "ask");
});

test("resolveOpenClawClientDecision honors exec-approvals security deny", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-oc-"));
  const prevHome = process.env.HOME;
  process.env.HOME = home;
  try {
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.writeFileSync(
      path.join(home, ".openclaw", "exec-approvals.json"),
      JSON.stringify({ version: 1, defaults: { security: "deny", ask: "off" } }),
    );
    assert.equal(
      resolveOpenClawClientDecision("exec", { command: "ls" }),
      "deny",
    );
  } finally {
    process.env.HOME = prevHome;
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("resolveOpenClawClientDecision allows when ask is off and security full", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-oc-"));
  const prevHome = process.env.HOME;
  process.env.HOME = home;
  try {
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.writeFileSync(
      path.join(home, ".openclaw", "exec-approvals.json"),
      JSON.stringify({ version: 1, defaults: { security: "full", ask: "off" } }),
    );
    assert.equal(
      resolveOpenClawClientDecision("exec", { command: "ls" }),
      "allow",
    );
  } finally {
    process.env.HOME = prevHome;
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("local auto-deny skips the permission-check HTTP call", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-oc-"));
  const prevHome = process.env.HOME;
  process.env.HOME = home;
  let called = false;
  try {
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.writeFileSync(
      path.join(home, ".openclaw", "exec-approvals.json"),
      JSON.stringify({ version: 1, defaults: { security: "deny", ask: "off" } }),
    );
    const plugin = makePlugin(() => {
      called = true;
      return jsonResponse(200, { decision: "allow" });
    });
    const result = await plugin.checkToolPermission(
      { toolName: "exec", params: { command: "rm -rf /" } },
      {},
    );
    assert.deepEqual(result, {
      block: true,
      blockReason: "Denied by OpenClaw exec-approvals policy.",
    });
    assert.equal(called, false);
  } finally {
    process.env.HOME = prevHome;
    fs.rmSync(home, { recursive: true, force: true });
  }
});
