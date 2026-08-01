// Sidecar command dispatch: routing, voice-as-text, interrupt, target keys.
// dispatch() lazily builds its SessionManager from the configured config and
// injected query factory, so no websocket is needed here.
import assert from "node:assert/strict";
import { test } from "node:test";

import { PreloopClaudeSidecar, resolveTargetSessionId } from "../dist/index.js";

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

test("non-command envelopes are ignored", async () => {
  const { sidecar } = makeSidecar();
  const result = await sidecar.dispatch({ type: "status", name: "heartbeat" });
  assert.equal(result, undefined);
});

test("send_message text is delivered and reply text returned", async () => {
  const { sidecar, state } = makeSidecar();
  const result = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    message_id: "m-1",
    payload: { text: "deploy the fix" },
  });
  assert.equal(result, "ok: deploy the fix");
  assert.deepEqual(state.texts, ["deploy the fix"]);
  sidecar.stop();
});

test("voice transcripts are delivered as auditable text turns", async () => {
  const { sidecar, state } = makeSidecar();
  const result = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { input_mode: "voice_transcript", message: "spoken words" },
  });
  assert.equal(result, "ok: spoken words");
  assert.deepEqual(state.texts, ["spoken words"]);
  sidecar.stop();
});

test("empty text is rejected", async () => {
  const { sidecar } = makeSidecar();
  await assert.rejects(
    () =>
      sidecar.dispatch({
        type: "command",
        name: "send_message",
        payload: { text: "   " },
      }),
    /non-empty text/,
  );
  sidecar.stop();
});

test("interrupt payload interrupts the owned session", async () => {
  const { sidecar, state } = makeSidecar();
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "start work" },
  });
  const result = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { interrupt: true, target_session_id: "session-1" },
  });
  assert.equal(result, "interrupted");
  assert.equal(state.interrupts, 1);
  sidecar.stop();
});

test("interrupting an unowned (TUI) session fails honestly", async () => {
  const { sidecar } = makeSidecar();
  await assert.rejects(
    () =>
      sidecar.dispatch({
        type: "command",
        name: "send_message",
        payload: { interrupt: true, target_session_id: "tui-session" },
      }),
    /owned by the sidecar/,
  );
  sidecar.stop();
});

test("resolveTargetSessionId checks payload then metadata keys", () => {
  assert.equal(
    resolveTargetSessionId({ target_session_id: "a", session_reference: "b" }),
    "a",
  );
  assert.equal(resolveTargetSessionId({ session_reference: "b" }), "b");
  assert.equal(
    resolveTargetSessionId({ metadata: { runtime_session_id: "c" } }),
    "c",
  );
  assert.equal(resolveTargetSessionId({ metadata: {} }), undefined);
  assert.equal(resolveTargetSessionId({ target_session_id: "  " }), undefined);
});
