// Tests for the `tool.execute.before` native tool-call gate: tool-name
// mapping, request body, allow/deny/timeout, safe-read auto-allow, the
// native_tool_approvals switch, and dedupe against `permission.asked`.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { PreloopOpenCodePlugin, PreloopPlugin, plugin as sharedPlugin } from "../dist/index.js";
import {
  isPreloopMCPTool,
  isSafeReadShellCommand,
  mapOpenCodeToolName,
  normalizeToolArgs,
  MAX_TOOL_ARG_CHARS,
} from "../dist/tools.js";

const baseConfig = {
  runtime: "opencode",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "secret-token",
  runtime_principal_id: "principal-1",
};

function makePlugin(fetchImpl, overrides = {}) {
  const plugin = new PreloopOpenCodePlugin(undefined, fetchImpl);
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

function countingFetch(body) {
  const state = { count: 0, calls: [] };
  state.fetch = (url, init) => {
    state.count += 1;
    state.calls.push({ url, init });
    return jsonResponse(200, body);
  };
  return state;
}

const bashCall = (callID, command) => [
  { tool: "bash", sessionID: "ses_1", callID },
  { args: { command, description: "run it" } },
];

// ---------------------------------------------------------------------------
// Pure helpers

test("mapOpenCodeToolName maps built-ins to the Preloop vocabulary and passes MCP tools through", () => {
  assert.equal(mapOpenCodeToolName("bash"), "Bash");
  assert.equal(mapOpenCodeToolName("edit"), "Edit");
  assert.equal(mapOpenCodeToolName("write"), "Write");
  assert.equal(mapOpenCodeToolName("read"), "Read");
  assert.equal(mapOpenCodeToolName("glob"), "Glob");
  assert.equal(mapOpenCodeToolName("grep"), "Grep");
  assert.equal(mapOpenCodeToolName("list"), "List");
  assert.equal(mapOpenCodeToolName("webfetch"), "WebFetch");
  assert.equal(mapOpenCodeToolName("task"), "Task");
  assert.equal(mapOpenCodeToolName("Bash"), "Bash");
  assert.equal(mapOpenCodeToolName("github_create_issue"), "github_create_issue");
  assert.equal(mapOpenCodeToolName(""), "tool");
  assert.equal(isPreloopMCPTool("preloop_list_issues"), true);
  assert.equal(isPreloopMCPTool("bash"), false);
});

test("normalizeToolArgs adds snake_case aliases and clips oversized strings", () => {
  const out = normalizeToolArgs({
    filePath: "/tmp/a.ts",
    oldString: "a",
    newString: "b",
    content: "x".repeat(MAX_TOOL_ARG_CHARS + 10),
  });
  assert.equal(out.file_path, "/tmp/a.ts");
  assert.equal(out.filePath, "/tmp/a.ts");
  assert.equal(out.old_string, "a");
  assert.equal(out.new_string, "b");
  assert.match(out.content, /\[truncated 10 chars\]$/);
  assert.deepEqual(normalizeToolArgs(undefined), {});
  assert.deepEqual(normalizeToolArgs("raw"), { value: "raw" });
});

test("isSafeReadShellCommand mirrors the CLI allowlist", () => {
  for (const ok of [
    "ls -la",
    "cat package.json | head -20",
    "git status",
    "git log --oneline -5",
    "git branch -a",
    "find . -name '*.ts'",
    "env",
    "rg TODO src",
  ]) {
    assert.equal(isSafeReadShellCommand(ok), true, ok);
  }
  for (const bad of [
    "",
    "rm -rf /",
    "ls; rm -rf /",
    "cat a > b",
    "echo $(rm x)",
    "git push",
    "git branch -D main",
    "find . -delete",
    "env FOO=1 bash",
    "ls || rm x",
    "npm install",
  ]) {
    assert.equal(isSafeReadShellCommand(bad), false, bad);
  }
});

// ---------------------------------------------------------------------------
// Request body

test("buildToolExecuteRequestBody sends source opencode with the mapped tool and payload", () => {
  const plugin = makePlugin();
  plugin.setWorkingDirectory("/work/project");
  const body = plugin.buildToolExecuteRequestBody(
    { tool: "edit", sessionID: "ses_9", callID: "call_1" },
    { filePath: "/work/project/a.ts", oldString: "x", newString: "y" },
  );
  assert.equal(body.source, "opencode");
  assert.equal(body.tool_name, "Edit");
  assert.equal(body.session_id, "ses_9");
  assert.equal(body.cwd, "/work/project");
  assert.equal(body.tool_input.file_path, "/work/project/a.ts");
  assert.equal(body.tool_input.old_string, "x");
  assert.equal(body.agent_reasoning, undefined);

  const bash = plugin.buildToolExecuteRequestBody(
    { tool: "bash", sessionID: "ses_9", callID: "call_2" },
    { command: "npm test", description: "run the suite" },
  );
  assert.equal(bash.tool_name, "Bash");
  assert.equal(bash.tool_input.command, "npm test");
  assert.equal(bash.agent_reasoning, "run the suite");
});

// ---------------------------------------------------------------------------
// Allow / deny / timeout

test("allow decision lets the tool run after one permission-check round trip", async () => {
  const state = countingFetch({ decision: "allow" });
  const plugin = makePlugin(state.fetch);
  const outcome = await plugin.handleToolExecuteBefore(...bashCall("call_a", "npm test"));
  assert.equal(outcome.allowed, true);
  assert.equal(outcome.skipped, undefined);
  assert.equal(state.count, 1);
  const { url, init } = state.calls[0];
  assert.equal(url, "https://example.preloop.ai/api/v1/agents/permission-check");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.authorization, "Bearer secret-token");
  const body = JSON.parse(init.body);
  assert.equal(body.source, "opencode");
  assert.equal(body.tool_name, "Bash");
  assert.equal(body.tool_input.command, "npm test");
  assert.equal(body.session_id, "ses_1");
});

test("deny decision throws 'Preloop denied <tool>: <reason>'", async () => {
  const plugin = makePlugin(() =>
    jsonResponse(200, { decision: "deny", reason: "Operator declined in Preloop" }),
  );
  await assert.rejects(
    plugin.handleToolExecuteBefore(...bashCall("call_d", "rm -rf build")),
    /^Error: Preloop denied Bash: Operator declined in Preloop$/,
  );
  const edit = makePlugin(() => jsonResponse(200, { decision: "deny" }));
  await assert.rejects(
    edit.handleToolExecuteBefore(
      { tool: "edit", sessionID: "s", callID: "c" },
      { args: { filePath: "/etc/hosts" } },
    ),
    /Preloop denied Edit: Denied in Preloop\./,
  );
});

test("timeout fails closed (throws) by default and open when configured", async () => {
  const closed = makePlugin(() => new Promise(() => undefined), {
    approval_timeout_ms: 25,
  });
  await assert.rejects(
    closed.handleToolExecuteBefore(...bashCall("call_t1", "npm publish")),
    /Preloop denied Bash: Preloop approval unavailable \(failing closed\): operator decision timed out after 25ms/,
  );

  const open = makePlugin(() => new Promise(() => undefined), {
    approval_timeout_ms: 25,
    tool_approval_fail_open: true,
  });
  const outcome = await open.handleToolExecuteBefore(...bashCall("call_t2", "npm publish"));
  assert.equal(outcome.allowed, true);
  assert.match(String(outcome.reason), /failing open/);
});

test("transport errors and non-2xx responses fail closed", async () => {
  const boom = makePlugin(() => Promise.reject(new Error("ECONNREFUSED")));
  await assert.rejects(
    boom.handleToolExecuteBefore(...bashCall("call_e", "make deploy")),
    /failing closed\): ECONNREFUSED/,
  );
  const http500 = makePlugin(() => jsonResponse(500, {}));
  await assert.rejects(
    http500.handleToolExecuteBefore(...bashCall("call_f", "make deploy")),
    /permission-check returned HTTP 500/,
  );
});

// ---------------------------------------------------------------------------
// Switches and local shortcuts

test("native_tool_approvals=off and tool_approval_enabled=false skip the gate without a round trip", async () => {
  const off = countingFetch({ decision: "deny" });
  const plugin = makePlugin(off.fetch, { native_tool_approvals: "off" });
  const outcome = await plugin.handleToolExecuteBefore(...bashCall("call_o", "rm -rf /"));
  assert.deepEqual(outcome, { allowed: true, skipped: "disabled" });
  assert.equal(off.count, 0);

  const disabled = makePlugin(off.fetch, { tool_approval_enabled: false });
  assert.deepEqual(
    await disabled.handleToolExecuteBefore(...bashCall("call_o2", "rm -rf /")),
    { allowed: true, skipped: "disabled" },
  );
  assert.equal(off.count, 0);

  // Unset and "on" both gate (a hand-installed plugin gates by default).
  assert.equal(makePlugin().nativeToolApprovalsEnabled(), true);
  assert.equal(makePlugin(undefined, { native_tool_approvals: "on" }).nativeToolApprovalsEnabled(), true);
  assert.equal(makePlugin(undefined, { native_tool_approvals: "OFF" }).nativeToolApprovalsEnabled(), false);
});

test("read-only shell commands are auto-allowed locally unless safe_read_auto_allow is false", async () => {
  const state = countingFetch({ decision: "deny", reason: "nope" });
  const plugin = makePlugin(state.fetch);
  const outcome = await plugin.handleToolExecuteBefore(...bashCall("call_r1", "git status"));
  assert.equal(outcome.skipped, "safe-read");
  assert.equal(state.count, 0);

  // A mutating command still goes to Preloop.
  await assert.rejects(
    plugin.handleToolExecuteBefore(...bashCall("call_r2", "git push --force")),
    /Preloop denied Bash: nope/,
  );
  assert.equal(state.count, 1);

  const strict = makePlugin(state.fetch, { safe_read_auto_allow: false });
  await assert.rejects(
    strict.handleToolExecuteBefore(...bashCall("call_r3", "git status")),
    /Preloop denied Bash: nope/,
  );
  assert.equal(state.count, 2);
});

test("read-only tools (read, glob, grep, list) are decided by the backend, not locally", async () => {
  const state = countingFetch({ decision: "deny", reason: "blocked by rule" });
  const plugin = makePlugin(state.fetch);
  for (const [tool, args, expected] of [
    ["read", { filePath: "/etc/passwd" }, "Read"],
    ["glob", { pattern: "**/*.env" }, "Glob"],
    ["grep", { pattern: "SECRET" }, "Grep"],
    ["list", { path: "/" }, "List"],
  ]) {
    await assert.rejects(
      plugin.handleToolExecuteBefore(
        { tool, sessionID: "ses_1", callID: `call_${tool}` },
        { args },
      ),
      new RegExp(`Preloop denied ${expected}: blocked by rule`),
    );
  }
  assert.equal(state.count, 4);
  assert.deepEqual(
    state.calls.map((call) => JSON.parse(call.init.body).tool_name),
    ["Read", "Glob", "Grep", "List"],
  );

  const allowAll = countingFetch({ decision: "allow" });
  const permissive = makePlugin(allowAll.fetch);
  const outcome = await permissive.handleToolExecuteBefore(
    { tool: "read", sessionID: "ses_1", callID: "call_read_ok" },
    { args: { filePath: "/tmp/notes.md" } },
  );
  assert.equal(outcome.allowed, true);
  assert.equal(allowAll.count, 1);
});

test("Preloop MCP tools are not gated a second time", async () => {
  const state = countingFetch({ decision: "deny" });
  const plugin = makePlugin(state.fetch);
  const outcome = await plugin.handleToolExecuteBefore(
    { tool: "preloop_list_issues", sessionID: "ses_1", callID: "call_mcp" },
    { args: { project: "x" } },
  );
  assert.deepEqual(outcome, { allowed: true, skipped: "preloop-mcp-tool" });
  assert.equal(state.count, 0);
});

// ---------------------------------------------------------------------------
// Dedupe between tool.execute.before and permission.asked

test("a permission.asked for a call the gate already decided reuses the decision (one round trip)", async () => {
  const state = countingFetch({ decision: "allow" });
  const replies = [];
  const plugin = makePlugin(state.fetch);
  plugin.setOpenCodeClient({
    permission: {
      reply: async (input) => {
        replies.push({ id: input.path.id, response: input.body.response });
        return {};
      },
    },
  });

  await plugin.handleToolExecuteBefore(...bashCall("call_x", "npm test"));
  assert.equal(state.count, 1);

  const outcome = await plugin.handlePermissionAsked({
    id: "perm-x",
    sessionID: "ses_1",
    callID: "call_x",
    permission: "bash",
    patterns: ["npm test"],
  });
  assert.equal(outcome.replied, true);
  assert.equal(outcome.response, "once");
  assert.deepEqual(replies, [{ id: "perm-x", response: "once" }]);
  assert.equal(state.count, 1, "permission.asked must not contact Preloop again");
});

test("a gate call for a request permission.asked already decided reuses it, and a different call does not", async () => {
  const state = countingFetch({ decision: "allow" });
  const plugin = makePlugin(state.fetch);
  plugin.setOpenCodeClient({ permission: { reply: async () => ({}) } });

  await plugin.handlePermissionAsked({
    id: "perm-y",
    sessionID: "ses_2",
    callID: "call_y",
    permission: "bash",
  });
  assert.equal(state.count, 1);

  const outcome = await plugin.handleToolExecuteBefore(
    { tool: "bash", sessionID: "ses_2", callID: "call_y" },
    { args: { command: "npm test" } },
  );
  assert.equal(outcome.skipped, "cached");
  assert.equal(state.count, 1);

  // Same call id in another session is a different call.
  await plugin.handleToolExecuteBefore(
    { tool: "bash", sessionID: "ses_3", callID: "call_y" },
    { args: { command: "npm test" } },
  );
  assert.equal(state.count, 2);
  assert.equal(plugin.cachedToolDecision("ses_3", "call_y") !== undefined, true);
  assert.equal(plugin.cachedToolDecision("ses_3", undefined), undefined);
});

test("a permission.asked arriving while the gate's round trip is still pending shares it", async () => {
  let resolveFetch;
  let count = 0;
  const plugin = makePlugin(() => {
    count += 1;
    return new Promise((resolve) => {
      resolveFetch = () => resolve({ ok: true, status: 200, json: async () => ({ decision: "deny", reason: "no" }) });
    });
  });
  const replies = [];
  plugin.setOpenCodeClient({
    permission: { reply: async (input) => { replies.push(input.body.response); return {}; } },
  });

  const gate = plugin.handleToolExecuteBefore(...bashCall("call_p", "npm test"));
  const asked = plugin.handlePermissionAsked({ id: "perm-p", sessionID: "ses_1", callID: "call_p", permission: "bash" });
  resolveFetch();

  await assert.rejects(gate, /Preloop denied Bash: no/);
  const outcome = await asked;
  assert.equal(outcome.response, "reject");
  assert.deepEqual(replies, ["reject"]);
  assert.equal(count, 1);
});

// ---------------------------------------------------------------------------
// Plugin factory wiring

test("PreloopPlugin registers the tool.execute.before hook next to the event hook", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-opencode-gate-"));
  const filePath = path.join(dir, "opencode.json");
  fs.writeFileSync(
    filePath,
    JSON.stringify({
      plugin: ["@preloop-ai/opencode-plugin"],
      preloop: {
        control: {
          ...baseConfig,
          // Unroutable on purpose: the socket fails fast and the reconnect
          // timer is unref'd, so this cannot hang the test process.
          control_ws_url: "ws://127.0.0.1:1/api/v1/agents/control/ws",
          native_tool_approvals: "off",
        },
      },
    }),
  );
  const previous = process.env.PRELOOP_OPENCODE_CONTROL_CONFIG;
  process.env.PRELOOP_OPENCODE_CONTROL_CONFIG = filePath;
  try {
    const hooks = await PreloopPlugin({ directory: dir });
    assert.equal(typeof hooks["tool.execute.before"], "function");
    assert.equal(typeof hooks.event, "function");
    // With native_tool_approvals off the hook is a no-op and never throws.
    await hooks["tool.execute.before"](
      { tool: "bash", sessionID: "s", callID: "c" },
      { args: { command: "rm -rf /" } },
    );
  } finally {
    sharedPlugin.stop();
    if (previous === undefined) {
      delete process.env.PRELOOP_OPENCODE_CONTROL_CONFIG;
    } else {
      process.env.PRELOOP_OPENCODE_CONTROL_CONFIG = previous;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
