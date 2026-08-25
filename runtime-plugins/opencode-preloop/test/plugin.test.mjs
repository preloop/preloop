// Unit tests for the Preloop OpenCode plugin: config parsing, permission-ask
// bridging (approve/deny/timeout), dedupe, and Agent Control command
// handling. Uses Node's built-in test runner against the built output:
// `npm test` builds first, then runs `node --test`.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  PreloopOpenCodePlugin,
} from "../dist/index.js";
import {
  extractControlConfig,
  loadControlConfig,
  verifyConfig,
} from "../dist/config.js";

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

const askRequest = (id) => ({
  id,
  sessionID: "ses_example",
  permission: "bash",
  patterns: ["rm -rf /"],
});

function makeClient(replyLog) {
  return {
    permission: {
      reply: async (input) => {
        replyLog?.push({ id: input.path.id, response: input.body.response });
        return {};
      },
    },
  };
}

function jsonResponse(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

// ---------------------------------------------------------------------------
// Config parsing

test("extractControlConfig reads the nested preloop.control block", () => {
  const raw = {
    $schema: "https://opencode.ai/config.json",
    plugin: ["@preloop-ai/opencode-plugin"],
    preloop: { control: { ...baseConfig } },
  };
  const extracted = extractControlConfig(raw);
  assert.equal(extracted.source, "control-block");
  assert.equal(extracted.config.control_ws_url, baseConfig.control_ws_url);
});

test("extractControlConfig accepts a flat control file", () => {
  const extracted = extractControlConfig({ ...baseConfig });
  assert.equal(extracted.source, "flat");
  assert.equal(extracted.config.bearer_token, "secret-token");
});

test("extractControlConfig classifies an unrelated opencode config as empty", () => {
  const extracted = extractControlConfig({
    theme: "dark",
    plugin: ["something-else"],
  });
  assert.equal(extracted.source, "empty");
  assert.deepEqual(extracted.config, {});
});

test("verifyConfig rejects a missing or wrong-runtime config", () => {
  assert.throws(() => verifyConfig({}), /no preloop\.control settings/);
  assert.throws(
    () => verifyConfig({ ...baseConfig, runtime: "claude_code" }),
    /Expected runtime "opencode"/,
  );
  assert.throws(
    () =>
      verifyConfig({
        runtime: "opencode",
        control_ws_url: baseConfig.control_ws_url,
      }),
    /bearer_token is required/,
  );
  assert.doesNotThrow(() => verifyConfig({ ...baseConfig }));
});

test("loadControlConfig honors the PRELOOP_OPENCODE_CONTROL_CONFIG override", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-opencode-"));
  const filePath = path.join(dir, "control.json");
  try {
    fs.writeFileSync(filePath, JSON.stringify({ ...baseConfig }));
    const previous = process.env.PRELOOP_OPENCODE_CONTROL_CONFIG;
    process.env.PRELOOP_OPENCODE_CONTROL_CONFIG = filePath;
    try {
      assert.equal(loadControlConfig().runtime_principal_id, "principal-1");
    } finally {
      if (previous === undefined) {
        delete process.env.PRELOOP_OPENCODE_CONTROL_CONFIG;
      } else {
        process.env.PRELOOP_OPENCODE_CONTROL_CONFIG = previous;
      }
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// URL derivation and approval policy knobs

test("permissionCheckUrl derives https from the wss control URL", () => {
  const plugin = makePlugin();
  assert.equal(
    plugin.permissionCheckUrl(baseConfig),
    "https://example.preloop.ai/api/v1/agents/permission-check",
  );
});

test("permissionCheckUrl derives http from a ws control URL", () => {
  const plugin = makePlugin();
  const wsConfig = {
    ...baseConfig,
    control_ws_url: "ws://localhost:8000/api/v1/agents/control/ws",
  };
  assert.equal(
    plugin.permissionCheckUrl(wsConfig),
    "http://localhost:8000/api/v1/agents/permission-check",
  );
});

test("approval timeout falls back to the default when unset or invalid", () => {
  assert.equal(makePlugin().approvalTimeoutMs(), 310000);
  assert.equal(
    makePlugin(undefined, { approval_timeout_ms: 1500 }).approvalTimeoutMs(),
    1500,
  );
  assert.equal(
    makePlugin(undefined, { approval_timeout_ms: -5 }).approvalTimeoutMs(),
    310000,
  );
});

// ---------------------------------------------------------------------------
// Permission bridging: approve path

test("allow decision replies once through the SDK client", async () => {
  const replies = [];
  let captured;
  const plugin = makePlugin((url, init) => {
    captured = { url, init };
    return jsonResponse(200, { decision: "allow" });
  });
  plugin.setOpenCodeClient(makeClient(replies));

  const outcome = await plugin.handlePermissionAsked(askRequest("perm-1"));

  assert.equal(outcome.replied, true);
  assert.equal(outcome.response, "once");
  assert.deepEqual(replies, [{ id: "perm-1", response: "once" }]);
  const body = JSON.parse(captured.init.body);
  assert.equal(captured.init.method, "POST");
  assert.equal(
    captured.url,
    "https://example.preloop.ai/api/v1/agents/permission-check",
  );
  assert.equal(captured.init.headers.authorization, "Bearer secret-token");
  assert.equal(body.source, "opencode");
  assert.equal(body.tool_name, "bash");
  assert.deepEqual(body.tool_input.patterns, ["rm -rf /"]);
  assert.equal(body.session_id, "ses_example");
});

// ---------------------------------------------------------------------------
// Permission bridging: deny path

test("deny decision rejects the tool with the operator's reason", async () => {
  const replies = [];
  const plugin = makePlugin(() =>
    jsonResponse(200, {
      decision: "deny",
      reason: "Operator declined in Preloop",
      request_id: "req-7",
    }),
  );
  plugin.setOpenCodeClient(makeClient(replies));

  const outcome = await plugin.handlePermissionAsked(askRequest("perm-2"));

  assert.equal(outcome.replied, true);
  assert.equal(outcome.response, "reject");
  assert.equal(outcome.reason, "Operator declined in Preloop");
  assert.deepEqual(replies, [{ id: "perm-2", response: "reject" }]);
});

// ---------------------------------------------------------------------------
// Permission bridging: timeout fallback

test("timeout applies the fail-closed fallback (reject) by default", async () => {
  const replies = [];
  const plugin = makePlugin(
    () => new Promise(() => undefined),
    { approval_timeout_ms: 25 },
  );
  plugin.setOpenCodeClient(makeClient(replies));

  const outcome = await plugin.handlePermissionAsked(askRequest("perm-3"));

  assert.equal(outcome.response, "reject");
  assert.match(String(outcome.reason), /failing closed/);
  assert.deepEqual(replies, [{ id: "perm-3", response: "reject" }]);
});

test("timeout applies the configured fail-open fallback (once)", async () => {
  const replies = [];
  const plugin = makePlugin(
    () => new Promise(() => undefined),
    { approval_timeout_ms: 25, tool_approval_fail_open: true },
  );
  plugin.setOpenCodeClient(makeClient(replies));

  const outcome = await plugin.handlePermissionAsked(askRequest("perm-4"));

  assert.equal(outcome.response, "once");
  assert.match(String(outcome.reason), /failing open/);
  assert.deepEqual(replies, [{ id: "perm-4", response: "once" }]);
});

test("transport error fails closed by default and open when configured", async () => {
  const closed = makePlugin(() => Promise.reject(new Error("boom")));
  closed.setOpenCodeClient(makeClient([]));
  const closedOutcome = await closed.handlePermissionAsked(askRequest("perm-e1"));
  assert.equal(closedOutcome.response, "reject");

  const open = makePlugin(
    () => Promise.reject(new Error("boom")),
    { tool_approval_fail_open: true },
  );
  open.setOpenCodeClient(makeClient([]));
  const openOutcome = await open.handlePermissionAsked(askRequest("perm-e2"));
  assert.equal(openOutcome.response, "once");
});

// ---------------------------------------------------------------------------
// Dedupe of repeated message/request ids

test("repeated permission.asked events for one id produce one round trip", async () => {
  let fetchCount = 0;
  const replies = [];
  const plugin = makePlugin(() => {
    fetchCount += 1;
    return jsonResponse(200, { decision: "allow" });
  });
  plugin.setOpenCodeClient(makeClient(replies));

  await plugin.handlePermissionAsked(askRequest("perm-dup"));
  const second = await plugin.handlePermissionAsked(askRequest("perm-dup"));

  assert.equal(fetchCount, 1);
  assert.deepEqual(second, { replied: false, skipped: "duplicate" });
  assert.equal(replies.length, 1);
});

test("permission.replied clears the dedupe entry so later asks re-escalate", async () => {
  let fetchCount = 0;
  const plugin = makePlugin(() => {
    fetchCount += 1;
    return jsonResponse(200, { decision: "allow" });
  });
  plugin.setOpenCodeClient(makeClient([]));

  await plugin.handlePermissionAsked(askRequest("perm-clear"));
  plugin.handlePermissionReplied({
    type: "permission.replied",
    properties: { id: "perm-clear" },
  });
  await plugin.handlePermissionAsked(askRequest("perm-clear"));

  assert.equal(fetchCount, 2);
});

test("disabled approvals short-circuit without contacting Preloop", async () => {
  let called = false;
  const plugin = makePlugin(
    () => {
      called = true;
      return jsonResponse(200, { decision: "deny" });
    },
    { tool_approval_enabled: false },
  );

  const outcome = await plugin.handlePermissionAsked(askRequest("perm-off"));

  assert.deepEqual(outcome, { replied: false, skipped: "disabled" });
  assert.equal(called, false);
});

// ---------------------------------------------------------------------------
// Agent Control commands: message_id dedupe + session targeting

test("handleCommand executes each message_id once and flags replays", async () => {
  const chats = [];
  const plugin = new PreloopOpenCodePlugin();
  plugin.configure(baseConfig);
  plugin.setOpenCodeClient({
    session: {
      chat: async (input) => {
        chats.push({ id: input.path.id, text: input.body.parts[0].text });
        return {};
      },
    },
  });

  const command = {
    message_id: "cmd-1",
    type: "command",
    name: "send_message",
    payload: { text: "hello agent", target_session_id: "ses_target" },
  };

  const first = plugin.handleCommand(command);
  assert.equal(first.duplicate, false);
  await first.result;

  const replay = plugin.handleCommand(command);
  assert.equal(replay.duplicate, true);

  const second = plugin.handleCommand({ ...command, message_id: "cmd-2" });
  await second.result;

  assert.deepEqual(chats, [
    { id: "ses_target", text: "hello agent" },
    { id: "ses_target", text: "hello agent" },
  ]);
});

test("session targeting prefers explicit ids over the configured reference", () => {
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });

  assert.equal(plugin.resolveSessionId({ target_session_id: "ses_a" }), "ses_a");
  assert.equal(
    plugin.resolveSessionId({ session_reference: "ses_b" }),
    "ses_b",
  );
  assert.equal(plugin.resolveSessionId({ runtime_session_id: "ses_c" }), "ses_c");
  assert.equal(plugin.resolveSessionId({}), "ses_default");
  assert.throws(
    () => makePlugin().resolveSessionId({}),
    /no target session specified/,
  );
});

test("dispatch ignores non-command envelopes and reports a missing SDK surface", async () => {
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  assert.equal(
    await plugin.dispatch({ message_id: "x", type: "status", name: "heartbeat" }),
    undefined,
  );
  assert.equal(
    await plugin.dispatch({
      message_id: "z",
      type: "command",
      name: "request_takeover",
      payload: { text: "hi" },
    }),
    undefined,
  );
  const noSession = makePlugin(undefined, { session_reference: "ses_default" });
  noSession.setOpenCodeClient({ session: {} });
  await assert.rejects(
    noSession.dispatch({
      message_id: "y",
      type: "command",
      name: "send_message",
      payload: { text: "hi" },
    }),
    /client\.session\.chat \/ client\.session\.prompt is not available/,
  );
});

// ---------------------------------------------------------------------------
// Remote control: prompt fallback, timeout, stop/interrupt

test("send_message falls back to blocking session.prompt with the same payload", async () => {
  const prompts = [];
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({
    session: {
      prompt: async (input) => {
        prompts.push({ id: input.path.id, text: input.body.parts[0].text });
        return { info: { role: "assistant" }, parts: [] };
      },
    },
  });

  const result = await plugin.dispatch({
    message_id: "cmd-prompt",
    type: "command",
    name: "send_message",
    payload: { text: "run the checks", target_session_id: "ses_live" },
  });

  assert.deepEqual(prompts, [{ id: "ses_live", text: "run the checks" }]);
  assert.equal(result.info.role, "assistant");
});

test("a hanging session.prompt rejects after turn_timeout_ms", async () => {
  const plugin = makePlugin(undefined, {
    session_reference: "ses_default",
    turn_timeout_ms: 25,
  });
  plugin.setOpenCodeClient({
    session: {
      prompt: () => new Promise(() => undefined),
    },
  });

  await assert.rejects(
    plugin.dispatch({
      message_id: "cmd-hang",
      type: "command",
      name: "send_message",
      payload: { text: "hello" },
    }),
    /timed out after 25ms/,
  );
});

test("turnTimeoutMs falls back to the default when unset or invalid", () => {
  assert.equal(makePlugin().turnTimeoutMs(), 300000);
  assert.equal(
    makePlugin(undefined, { turn_timeout_ms: 4000 }).turnTimeoutMs(),
    4000,
  );
  assert.equal(
    makePlugin(undefined, { turn_timeout_ms: 0 }).turnTimeoutMs(),
    300000,
  );
});

test("payload.interrupt maps to session.abort on the targeted session", async () => {
  const aborts = [];
  const plugin = makePlugin();
  plugin.setOpenCodeClient({
    session: {
      abort: async (input) => {
        aborts.push(input.path.id);
        return true;
      },
    },
  });

  const result = await plugin.dispatch({
    message_id: "cmd-stop",
    type: "command",
    name: "send_message",
    payload: { text: "ignored", interrupt: true, target_session_id: "ses_run" },
  });

  assert.equal(result, "interrupted");
  assert.deepEqual(aborts, ["ses_run"]);
});

test("stop and interrupt command names map to session.abort", async () => {
  const aborts = [];
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({
    session: {
      abort: async (input) => {
        aborts.push(input.path.id);
        return true;
      },
    },
  });

  for (const name of ["stop", "interrupt"]) {
    const result = await plugin.dispatch({
      message_id: `cmd-${name}`,
      type: "command",
      name,
      payload: {},
    });
    assert.equal(result, "interrupted");
  }
  assert.deepEqual(aborts, ["ses_default", "ses_default"]);
});

test("a missing abort surface produces an actionable error", async () => {
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({ session: {} });
  await assert.rejects(
    plugin.dispatch({
      message_id: "cmd-noabort",
      type: "command",
      name: "stop",
      payload: {},
    }),
    /client\.session\.abort is not available/,
  );
});

// ---------------------------------------------------------------------------
// Remote control gating

test("remote_control_enabled=false blocks steering and dims capabilities", async () => {
  const chats = [];
  const config = { ...baseConfig, remote_control_enabled: false };
  const plugin = new PreloopOpenCodePlugin();
  plugin.configure(config);
  plugin.setOpenCodeClient({
    session: {
      chat: async (input) => {
        chats.push(input);
        return {};
      },
    },
  });

  await assert.rejects(
    plugin.dispatch({
      message_id: "cmd-gated",
      type: "command",
      name: "send_message",
      payload: { text: "hi", target_session_id: "ses_x" },
    }),
    /remote control is disabled/,
  );
  assert.equal(chats.length, 0);

  const presence = plugin.presenceMessage(config);
  assert.equal(presence.payload.capabilities.text, false);
  assert.equal(presence.payload.capabilities.interrupt, false);
  // Approvals are independent of remote steering.
  assert.equal(presence.payload.capabilities.tool_approval, true);
});

test("remoteControlEnabled defaults to on", () => {
  assert.equal(makePlugin().remoteControlEnabled(), true);
  assert.equal(
    makePlugin(undefined, { remote_control_enabled: false }).remoteControlEnabled(),
    false,
  );
});

// ---------------------------------------------------------------------------
// WS frame handling: ack sequence ordering and replay dedupe

function makeSocket() {
  return {
    frames: [],
    send(data) {
      this.frames.push(JSON.parse(data));
    },
  };
}

/** Yield so handleFrame's async result/ack chain can settle. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test("handleFrame emits one completed ack per command in order", async () => {
  const socket = makeSocket();
  let chatCount = 0;
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({
    session: {
      chat: async (input) => {
        chatCount += 1;
        return { reply_text: `ack ${chatCount}` };
      },
    },
  });

  await plugin.handleFrame(
    socket,
    JSON.stringify({
      message_id: "ws-1",
      type: "command",
      name: "send_message",
      payload: { text: "first" },
    }),
  );
  await flush();
  await plugin.handleFrame(
    socket,
    JSON.stringify({
      message_id: "ws-2",
      type: "command",
      name: "send_message",
      payload: { message: "second" },
    }),
  );
  await flush();

  assert.equal(socket.frames.length, 2);
  assert.deepEqual(
    socket.frames.map((frame) => [frame.name, frame.message_id]),
    [
      ["command_result", "ws-1"],
      ["command_result", "ws-2"],
    ],
  );
  assert.deepEqual(socket.frames[0].payload, {
    command_id: "ws-1",
    status: "completed",
    result: { reply_text: "ack 1" },
    reply_text: "ack 1",
  });
  assert.equal(chatCount, 2);
});

test("handleFrame acknowledges a replayed message_id without re-executing", async () => {
  const socket = makeSocket();
  let chatCount = 0;
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({
    session: {
      chat: async () => {
        chatCount += 1;
        return {};
      },
    },
  });
  const envelope = JSON.stringify({
    message_id: "ws-dup",
    type: "command",
    name: "send_message",
    payload: { text: "once only" },
  });

  await plugin.handleFrame(socket, envelope);
  await flush();
  await plugin.handleFrame(socket, envelope);
  await flush();

  assert.equal(chatCount, 1);
  assert.equal(socket.frames.length, 2);
  assert.deepEqual(socket.frames[1], {
    type: "status",
    name: "command_result",
    message_id: "ws-dup",
    payload: {
      command_id: "ws-dup",
      status: "completed",
      duplicate: true,
    },
  });
});

test("handleFrame reports failures as command_error with the command id", async () => {
  const socket = makeSocket();
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({
    session: {
      chat: async () => {
        throw new Error("session gone");
      },
    },
  });

  await plugin.handleFrame(
    socket,
    JSON.stringify({
      message_id: "ws-fail",
      type: "command",
      name: "send_message",
      payload: { text: "boom" },
    }),
  );
  await flush();

  assert.deepEqual(socket.frames, [
    {
      type: "status",
      name: "command_error",
      message_id: "ws-fail",
      payload: {
        command_id: "ws-fail",
        status: "failed",
        error: "session gone",
      },
    },
  ]);
});

test("handleFrame rejects empty turns and malformed JSON", async () => {
  const socket = makeSocket();
  const plugin = makePlugin(undefined, { session_reference: "ses_default" });
  plugin.setOpenCodeClient({
    session: { chat: async () => ({}) },
  });

  await plugin.handleFrame(
    socket,
    JSON.stringify({
      message_id: "ws-empty",
      type: "command",
      name: "send_message",
      payload: {},
    }),
  );
  await flush();
  await plugin.handleFrame(socket, "{not json");
  await flush();

  assert.deepEqual(
    socket.frames.map((frame) => [frame.name, frame.payload.status]),
    [
      ["command_error", "failed"],
      ["command_error", "failed"],
    ],
  );
  assert.match(socket.frames[0].payload.error, /requires non-empty text/);
  assert.match(socket.frames[1].payload.error, /^invalid_json:/);
});

// ---------------------------------------------------------------------------
// Presence envelope

test("presence advertises the opencode runtime and tool_approval capability", () => {
  const plugin = makePlugin();
  const presence = plugin.presenceMessage(baseConfig);
  assert.equal(presence.type, "presence");
  assert.equal(presence.name, "capabilities");
  const payload = presence.payload;
  assert.equal(payload.runtime, "opencode");
  assert.equal(payload.protocol, "preloop.agent_control.v1");
  assert.equal(payload.capabilities.tool_approval, true);
  assert.equal(payload.capabilities.voice, false);
});
