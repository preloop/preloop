// Takeover / release commands and local-to-remote switch.
import assert from "node:assert/strict";
import { test } from "node:test";

import { PreloopClaudeSidecar } from "../dist/index.js";

const baseConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
  observer_enabled: false,
};

function makeEchoFactory(state) {
  return async ({ prompt }) => ({
    async interrupt() {
      state.interrupts += 1;
    },
    async *[Symbol.asyncIterator]() {
      yield { type: "system", subtype: "init", session_id: "session-1" };
      for await (const userMessage of prompt) {
        state.texts.push(userMessage.message.content);
        yield {
          type: "result",
          subtype: "success",
          session_id: "session-1",
          result: `ok: ${userMessage.message.content}`,
        };
      }
    },
  });
}

function makeSidecar() {
  const state = { texts: [], interrupts: 0 };
  const sidecar = new PreloopClaudeSidecar(undefined, makeEchoFactory(state));
  sidecar.configure(baseConfig);
  return { sidecar, state };
}

test("request_takeover marks the session remote", async () => {
  const { sidecar } = makeSidecar();
  const result = await sidecar.dispatch({
    type: "command",
    name: "request_takeover",
    payload: { session_source_id: "claude-native" },
  });
  assert.equal(result, "remote:claude-native");
  assert.equal(sidecar.currentMode(), "remote");
  sidecar.stop();
});

test("release after a remote turn returns local", async () => {
  const { sidecar } = makeSidecar();
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "hello from phone" },
  });
  const result = await sidecar.dispatch({
    type: "command",
    name: "release",
    payload: { session_source_id: "session-1" },
  });
  assert.match(String(result), /^local:/);
  sidecar.stop();
});

test("send_message still delivers after takeover", async () => {
  const { sidecar, state } = makeSidecar();
  await sidecar.dispatch({
    type: "command",
    name: "request_takeover",
    payload: {},
  });
  const reply = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "ship it" },
  });
  assert.equal(reply, "ok: ship it");
  assert.deepEqual(state.texts, ["ship it"]);
  sidecar.stop();
});
