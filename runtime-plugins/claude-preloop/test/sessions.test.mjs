// SessionManager routing against a fake Agent SDK query factory.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionManager } from "../dist/sessions.js";

const baseConfig = {
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
  workspace_root: "/tmp/workspace",
  permission_mode: "default",
};

/**
 * Fake SDK: consumes the streaming-input queue and, per user turn, yields an
 * assistant message echoing the text plus a result message. Records the
 * options each query() was opened with and interrupt() calls.
 */
function makeFakeFactory(state) {
  return async ({ prompt, options }) => {
    const sessionId = options.resume ?? `session-${state.opened.length + 1}`;
    state.opened.push({ options, sessionId });
    const handle = {
      interrupted: 0,
      async interrupt() {
        this.interrupted += 1;
        state.interrupts.push(sessionId);
      },
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: sessionId };
        for await (const userMessage of prompt) {
          const text = userMessage.message.content;
          yield {
            type: "assistant",
            session_id: sessionId,
            message: { content: [{ type: "text", text: `echo: ${text}` }] },
          };
          yield {
            type: "result",
            subtype: "success",
            session_id: sessionId,
            result: `echo: ${text}`,
          };
        }
      },
    };
    return handle;
  };
}

function makeManager() {
  const state = { opened: [], interrupts: [] };
  const manager = new SessionManager(baseConfig, makeFakeFactory(state));
  return { manager, state };
}

test("send without a target starts a new session and returns the reply", async () => {
  const { manager, state } = makeManager();
  const reply = await manager.sendMessage({ text: "hello" });
  assert.equal(reply, "echo: hello");
  assert.equal(state.opened.length, 1);
  assert.equal(state.opened[0].options.cwd, "/tmp/workspace");
  assert.equal(state.opened[0].options.permissionMode, "default");
  // Filesystem settings must load so the Preloop PreToolUse hook fires.
  assert.deepEqual(state.opened[0].options.settingSources, [
    "user",
    "project",
    "local",
  ]);
  manager.stop();
});

test("second message to the same session reuses the streaming input", async () => {
  const { manager, state } = makeManager();
  const first = await manager.sendMessage({ text: "one" });
  const sessionId = manager.ownedSessionIds()[0];
  const second = await manager.sendMessage({
    text: "two",
    targetSessionId: sessionId,
  });
  assert.equal(first, "echo: one");
  assert.equal(second, "echo: two");
  assert.equal(state.opened.length, 1);
  manager.stop();
});

test("targeting an unknown session resumes it via the SDK", async () => {
  const { manager, state } = makeManager();
  const reply = await manager.sendMessage({
    text: "resume me",
    targetSessionId: "persisted-abc",
  });
  assert.equal(reply, "echo: resume me");
  assert.equal(state.opened.length, 1);
  assert.equal(state.opened[0].options.resume, "persisted-abc");
  manager.stop();
});

test("session_source_id is preferred for SDK resume over the Preloop UUID", async () => {
  const { manager, state } = makeManager();
  const reply = await manager.sendMessage({
    text: "resume native",
    targetSessionId: "preloop-uuid-1",
    resumeSessionId: "claude-session-abc",
  });
  assert.equal(reply, "echo: resume native");
  assert.equal(state.opened.length, 1);
  assert.equal(state.opened[0].options.resume, "claude-session-abc");
  manager.stop();
});

test("interrupt targets the owned session", async () => {
  const { manager, state } = makeManager();
  await manager.sendMessage({ text: "busy work" });
  const sessionId = manager.ownedSessionIds()[0];
  await manager.interrupt(sessionId);
  assert.deepEqual(state.interrupts, [sessionId]);
  manager.stop();
});

test("a hung turn rejects after turn_timeout_ms instead of blocking forever", async () => {
  // Factory that consumes input but never yields a result message.
  const hangingFactory = async ({ prompt }) => ({
    async interrupt() {},
    async *[Symbol.asyncIterator]() {
      yield { type: "system", subtype: "init", session_id: "hung-1" };
      for await (const _userMessage of prompt) {
        // Swallow the turn; no assistant/result messages ever come back.
      }
    },
  });
  const manager = new SessionManager(
    { ...baseConfig, turn_timeout_ms: 50 },
    hangingFactory,
  );
  await assert.rejects(
    () => manager.sendMessage({ text: "never answered" }),
    /timed out after 50ms/,
  );
  // The sidecar can still take new commands afterwards.
  assert.equal(manager.ownedSessionIds().length, 1);
  manager.stop();
});

test("interrupt with no owned sessions reports the honest limitation", async () => {
  const { manager } = makeManager();
  await assert.rejects(
    () => manager.interrupt("some-tui-session"),
    /owned by the sidecar/,
  );
  manager.stop();
});
