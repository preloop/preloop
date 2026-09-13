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
      JSON.stringify({
        version: 1,
        defaults: { security: "deny", ask: "off" },
      }),
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
      JSON.stringify({
        version: 1,
        defaults: { security: "full", ask: "off" },
      }),
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
      JSON.stringify({
        version: 1,
        defaults: { security: "deny", ask: "off" },
      }),
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

for (const decision of ["allow", "deny"]) {
  test(`local allow reaches central policy and obeys ${decision}`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-policy-"));
    const previousHome = process.env.HOME;
    process.env.HOME = directory;
    try {
      fs.mkdirSync(path.join(directory, ".openclaw"));
      fs.writeFileSync(
        path.join(directory, ".openclaw", "exec-approvals.json"),
        JSON.stringify({ defaults: { security: "full", ask: "off" } }),
      );
      let captured;
      const plugin = makePlugin((url, init) => {
        captured = JSON.parse(init.body);
        return jsonResponse(200, { decision, reason: "Central policy" });
      });
      const result = await plugin.checkToolPermission(
        { toolName: "exec", params: { command: "pwd" } },
        {},
      );
      assert.equal(captured?.client_decision, "allow");
      assert.equal(result?.block, decision === "deny" ? true : undefined);
    } finally {
      if (previousHome === undefined) delete process.env.HOME;
      else process.env.HOME = previousHome;
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });
}

for (const body of [{}, { decision: "ask" }, [], null]) {
  test(`malformed response fails closed: ${JSON.stringify(body)}`, async () => {
    const result = await makePlugin(() =>
      jsonResponse(200, body),
    ).checkToolPermission({ toolName: "Read", params: {} }, {});
    assert.equal(result?.block, true);
  });
}

test("expiry deny cannot be widened by fail-open", async () => {
  const result = await makePlugin(
    () =>
      jsonResponse(200, {
        decision: "deny",
        reason: "Approval expired",
        timed_out: true,
      }),
    { tool_approval_fail_open: true },
  ).checkToolPermission({ toolName: "Read", params: {} }, {});
  assert.equal(result?.block, true);
});

test("workflow wait budget defaults to 24h with 15s HTTP headroom", () => {
  assert.equal(makePlugin().permissionCheckTimeoutSeconds(), 86415);
  assert.equal(
    makePlugin(undefined, {
      tool_approval_timeout_seconds: 1800,
    }).permissionCheckTimeoutSeconds(),
    1815,
  );
});

for (const value of [0, 29, 86401, 30.5, "300", true, null]) {
  test(`invalid workflow wait budget blocks: ${JSON.stringify(value)}`, async () => {
    let called = false;
    const plugin = makePlugin(
      () => {
        called = true;
        return jsonResponse(200, { decision: "allow" });
      },
      { tool_approval_timeout_seconds: value },
    );
    const result = await plugin.checkToolPermission(
      { toolName: "Read", params: {} },
      {},
    );
    assert.equal(result?.block, true);
    assert.equal(called, false);
  });
}

for (const failOpen of [false, true]) {
  test(`HTTP deadline abort follows explicit fail-open=${failOpen}`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const plugin = makePlugin(
      (url, init) =>
        new Promise((resolve, reject) => {
          init.signal.addEventListener("abort", () =>
            reject(new Error("request aborted")),
          );
        }),
      { tool_approval_timeout_seconds: 30, tool_approval_fail_open: failOpen },
    );
    const pending = plugin.checkToolPermission(
      { toolName: "Read", params: {} },
      {},
    );
    t.mock.timers.tick(45000);
    const result = await pending;
    assert.equal(result?.block, failOpen ? undefined : true);
  });
}

test("real HTTP adapter request waits for synthetic approval decision", async () => {
  const { createServer } = await import("node:http");
  let received;
  const requestReceived = new Promise((resolve) => {
    received = resolve;
  });
  let finish;
  const server = createServer(async (req, res) => {
    assert.equal(req.url, "/api/v1/agents/permission-check");
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    assert.equal(body.source, "openclaw");
    finish = (decision) => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ decision, request_id: "synthetic-approval" }));
    };
    received();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const plugin = makePlugin(undefined, {
      permission_check_url: `http://127.0.0.1:${server.address().port}/api/v1/agents/permission-check`,
    });
    const pending = plugin.checkToolPermission(
      { toolName: "Read", params: {} },
      {},
    );
    await requestReceived;
    finish("deny");
    assert.equal((await pending)?.block, true);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

for (const status of [400, 401, 403, 404, 429]) {
  test(`HTTP ${status} is terminal even with fail-open`, async () => {
    const plugin = makePlugin(
      () => jsonResponse(status, { detail: "Synthetic rejection" }),
      { tool_approval_fail_open: true },
    );
    assert.equal(
      (await plugin.checkToolPermission({ toolName: "Read", params: {} }, {}))
        .block,
      true,
    );
  });
}
for (const body of [
  null,
  [],
  {},
  { decision: "unknown" },
  { decision: "allow", timed_out: true },
  { decision: "deny", reason: {} },
  { decision: "allow", timed_out: "false" },
]) {
  test(`invalid reply is terminal with fail-open: ${JSON.stringify(body)}`, async () => {
    const plugin = makePlugin(() => jsonResponse(200, body), {
      tool_approval_fail_open: true,
    });
    assert.equal(
      (await plugin.checkToolPermission({ toolName: "Read", params: {} }, {}))
        .block,
      true,
    );
  });
}
for (const config of [
  { tool_approval_enabled: 0 },
  { tool_approval_enabled: "false" },
  { tool_approval_fail_open: "false" },
  { tool_approval_timeout_seconds: "300", tool_approval_fail_open: true },
]) {
  test(`invalid config never opts into fail-open: ${JSON.stringify(config)}`, async () => {
    let fetched = false;
    const plugin = makePlugin(() => {
      fetched = true;
      return Promise.reject(new Error("Synthetic outage"));
    }, config);
    assert.equal(
      (await plugin.checkToolPermission({ toolName: "Read", params: {} }, {}))
        .block,
      true,
    );
    assert.equal(fetched, false);
  });
}

for (const failOpen of [false, true]) {
  test(`HTTP 503 availability honors fail-open=${failOpen}`, async () => {
    const plugin = makePlugin(
      () => jsonResponse(503, { detail: "Synthetic outage" }),
      { tool_approval_fail_open: failOpen },
    );
    const result = await plugin.checkToolPermission(
      { toolName: "Read", params: {} },
      {},
    );
    assert.equal(result === undefined, failOpen);
  });
}
test("invalid JSON cannot fail open", async () => {
  const plugin = makePlugin(
    () =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.reject(new SyntaxError("invalid JSON")),
      }),
    { tool_approval_fail_open: true },
  );
  assert.equal(
    (await plugin.checkToolPermission({ toolName: "Read", params: {} }, {}))
      .block,
    true,
  );
});

test("invalid URL credentials are configuration errors, not fail-open transport", async () => {
  let fetched = false;
  const plugin = makePlugin(
    () => {
      fetched = true;
      return Promise.reject(new TypeError("fetch failed"));
    },
    {
      tool_approval_fail_open: true,
      permission_check_url: "http://synthetic:synthetic@127.0.0.1/check",
    },
  );
  assert.equal(
    (await plugin.checkToolPermission({ toolName: "Read", params: {} }, {}))
      .block,
    true,
  );
  assert.equal(fetched, false);
});
