// Sidecar command dispatch: routing, voice-as-text, interrupt, target keys.
// dispatch() lazily builds its SessionManager from the configured config and
// injected query factory, so no websocket is needed here.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  PreloopClaudeSidecar,
  controlAuthHeaders,
  resolveResumeSessionId,
  resolveTargetSessionId,
} from "../dist/index.js";

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

test("start_new_session ignores a prior session id so the model is applied", async () => {
  const opened = [];
  const factory = async ({ prompt, options }) => {
    opened.push(options);
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: "fresh-1" };
        for await (const userMessage of prompt) {
          yield {
            type: "result",
            subtype: "success",
            result: `ok: ${userMessage.message.content}`,
            usage: { input_tokens: 1, output_tokens: 1 },
            total_cost_usd: 0.0001,
          };
        }
      },
    };
  };
  const sidecar = new PreloopClaudeSidecar(undefined, factory);
  sidecar.configure(baseConfig);
  const reply = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    message_id: "flow-1",
    payload: {
      text: "dream",
      start_new_session: true,
      runtime_session_id: "should-not-resume",
      metadata: { model: "anthropic/claude-opus-5-5" },
    },
  });
  assert.equal(reply, "ok: dream");
  assert.equal(opened.length, 1);
  assert.equal(opened[0].model, "anthropic/claude-opus-5-5");
  assert.equal("resume" in opened[0], false);
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

test("resolveResumeSessionId prefers session_source_id when present", () => {
  assert.equal(
    resolveResumeSessionId({
      target_session_id: "preloop-uuid",
      session_source_id: "claude-native",
    }),
    "claude-native",
  );
  assert.equal(
    resolveResumeSessionId({ target_session_id: "preloop-uuid" }),
    "preloop-uuid",
  );
  assert.equal(
    resolveResumeSessionId({
      metadata: { session_source_id: "from-meta" },
    }),
    "from-meta",
  );
});

test("controlAuthHeaders uses Authorization Bearer, not a query token", () => {
  const headers = controlAuthHeaders("agt_secret");
  assert.equal(headers.Authorization, "Bearer agt_secret");
});

function fakeSocket({ open = true } = {}) {
  const sent = [];
  return {
    sent,
    OPEN: 1,
    readyState: open ? 1 : 3,
    send(data) {
      if (!open) {
        throw new Error("WebSocket is not open: readyState 3 (CLOSED)");
      }
      sent.push(JSON.parse(data));
    },
    close() {},
  };
}

test("sendOn is a no-op when the socket is not OPEN", async () => {
  const { sidecar } = makeSidecar();
  const socket = fakeSocket({ open: false });
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "closed-1",
      payload: { text: "should not throw" },
    }),
  );
  assert.deepEqual(socket.sent, []);
  sidecar.stop();
});

test("reconnect replays only outcomes that missed delivery", async () => {
  const opened = [];
  const factory = async ({ prompt }) => {
    opened.push(true);
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: "s-1" };
        for await (const userMessage of prompt) {
          yield {
            type: "result",
            subtype: "success",
            result: `ok: ${userMessage.message.content}`,
            usage: { input_tokens: 4, output_tokens: 2 },
            total_cost_usd: 0.002,
          };
        }
      },
    };
  };
  const sidecar = new PreloopClaudeSidecar(undefined, factory);
  sidecar.configure(baseConfig);
  const closed = fakeSocket({ open: false });
  await sidecar.handleFrame(
    closed,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "missed-1",
      payload: { text: "while offline" },
    }),
  );
  assert.deepEqual(closed.sent, []);
  assert.equal(opened.length, 1);

  // A second completed command delivered on a live socket must not be
  // replayed later just because it sits in the dedupe map.
  const live = fakeSocket();
  sidecar.socket = live;
  await sidecar.handleFrame(
    live,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "live-1",
      payload: { text: "while online" },
    }),
  );
  assert.equal(live.sent.length, 2);
  assert.equal(live.sent[0].name, "command_progress");
  assert.equal(live.sent[0].payload.command_id, "live-1");
  assert.match(live.sent[0].payload.summary, /tokens: 6/);
  assert.equal(live.sent[1].name, "command_result");
  assert.equal(live.sent[1].payload.reply_text, "ok: while online");

  const reconnected = fakeSocket();
  sidecar.replayCommandOutcomes(reconnected);
  assert.equal(reconnected.sent.length, 1);
  assert.equal(reconnected.sent[0].name, "command_result");
  assert.equal(reconnected.sent[0].message_id, "missed-1");
  assert.equal(reconnected.sent[0].payload.reply_text, "ok: while offline");
  assert.equal(reconnected.sent[0].payload.usage.total_tokens, 6);
  sidecar.stop();
});

test("replay of a failed command resends the error, not a bare duplicate", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  const failed = JSON.stringify({
    type: "command",
    name: "send_message",
    message_id: "fail-1",
    payload: { text: "   " },
  });
  await sidecar.handleFrame(socket, failed);
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].name, "command_error");
  assert.match(socket.sent[0].payload.error, /non-empty text/);
  await sidecar.handleFrame(socket, failed);
  assert.equal(socket.sent.length, 2);
  assert.equal(socket.sent[1].name, "command_error");
  assert.match(socket.sent[1].payload.error, /non-empty text/);
  assert.notEqual(socket.sent[1].payload.status, "duplicate");
  assert.deepEqual(state.texts, []);
  sidecar.stop();
});

test("replay of a completed command resends the stored result", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  const completed = JSON.stringify({
    type: "command",
    name: "send_message",
    message_id: "ok-1",
    payload: { text: "once" },
  });
  await sidecar.handleFrame(socket, completed);
  await sidecar.handleFrame(socket, completed);
  assert.equal(socket.sent.length, 2);
  assert.equal(socket.sent[0].name, "command_result");
  assert.equal(socket.sent[1].name, "command_result");
  assert.equal(socket.sent[1].payload.result, "ok: once");
  assert.deepEqual(state.texts, ["once"]);
  sidecar.stop();
});

test("stop() clears sessions so a later dispatch recreates the manager", async () => {
  const { sidecar, state } = makeSidecar();
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "first" },
  });
  sidecar.stop();
  const result = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "after-stop" },
  });
  assert.equal(result, "ok: after-stop");
  assert.deepEqual(state.texts, ["first", "after-stop"]);
  sidecar.stop();
});

test("presence lists sdk-owned and launcher sessions as reachable", async () => {
  const { sidecar } = makeSidecar();
  assert.deepEqual(sidecar.reachableSessionIds(), []);
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "start work" },
  });
  assert.deepEqual(sidecar.reachableSessionIds(), ["session-1"]);

  // A launcher TUI held locally is reachable; a released one is not.
  sidecar.launcher.hasLauncher = () => true;
  sidecar.launcher.mode = "local";
  sidecar.launcher.lastSessionId = "launcher-tui";
  assert.deepEqual(sidecar.reachableSessionIds(), ["session-1", "launcher-tui"]);
  sidecar.launcher.mode = "offline";
  assert.deepEqual(sidecar.reachableSessionIds(), ["session-1"]);

  // The presence frame carries the list for the server.
  sidecar.launcher.mode = "local";
  const sent = [];
  sidecar.sendEnvelope = (envelope) => sent.push(envelope);
  sidecar.launcher.notifyStatus = () => {};
  sidecar.broadcastMode();
  assert.equal(sent[0].name, "session_mode");
  assert.deepEqual(sent[0].payload.reachable_session_ids, [
    "session-1",
    "launcher-tui",
  ]);
  sidecar.stop();
});
