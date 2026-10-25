// Live reply progress: stream events become numbered, merged session_progress.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionManager } from "../dist/sessions.js";
import { SessionProgressForwarder } from "../dist/progress.js";

const baseConfig = {
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
  workspace_root: "/tmp/workspace",
};

function streamingFactory(state) {
  return async ({ prompt, options }) => {
    state.options = options;
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: "claude-1" };
        for await (const userMessage of prompt) {
          yield { type: "stream_event", event: { type: "message_start" } };
          for (const text of ["Hel", "lo"]) {
            yield {
              type: "stream_event",
              event: {
                type: "content_block_delta",
                index: 0,
                delta: { type: "text_delta", text },
              },
            };
          }
          yield {
            type: "stream_event",
            event: {
              type: "content_block_delta",
              index: 1,
              delta: { type: "input_json_delta", partial_json: "{}" },
            },
          };
          yield {
            type: "assistant",
            message: { content: [{ type: "text", text: "Hello" }] },
          };
          yield { type: "result", subtype: "success", result: `Hello ${userMessage.message.content}` };
        }
      },
    };
  };
}

test("an owned session streams its reply as live progress", async () => {
  const state = {};
  const live = [];
  const manager = new SessionManager(baseConfig, streamingFactory(state), {
    log: () => {},
    onLiveProgress: (sessionId, event) => live.push({ sessionId, ...event }),
  });
  try {
    const turn = await manager.completeTurn({ text: "there" });
    assert.equal(turn.text, "Hello there", "the stored reply still comes from the result");
    assert.equal(state.options.includePartialMessages, true);
    assert.deepEqual(
      live.map((event) => [event.sessionId, event.kind, event.delta ?? null]),
      [
        ["claude-1", "turn_started", null],
        ["claude-1", "assistant_delta", "Hel"],
        ["claude-1", "assistant_delta", "lo"],
        ["claude-1", "turn_completed", null],
      ],
    );
    assert.equal(live[1].item_id, live[2].item_id);
    assert.equal(live[3].status, "completed");
  } finally {
    manager.stop();
  }
});

test("the forwarder merges deltas, keeps order and numbers events per session", async () => {
  const sent = [];
  const forwarder = new SessionProgressForwarder((payload) => sent.push(payload), "claude_code", 20);
  forwarder.push("s1", { kind: "turn_started", turn_id: "t1" });
  forwarder.push("s1", { kind: "assistant_delta", turn_id: "t1", item_id: "a", delta: "Hel" });
  forwarder.push("s1", { kind: "assistant_delta", turn_id: "t1", item_id: "a", delta: "lo" });
  assert.equal(sent.length, 1, "per-token deltas are merged, not sent one by one");
  forwarder.push("s1", { kind: "turn_completed", turn_id: "t1", status: "completed" });
  assert.deepEqual(
    sent.map((payload) => [payload.kind, payload.delta ?? null, payload.seq]),
    [
      ["turn_started", null, 1],
      ["assistant_delta", "Hello", 2],
      ["turn_completed", null, 3],
    ],
  );
  assert.equal(sent[1].session_source_id, "s1");
  assert.equal(sent[1].runtime, "claude_code");
  forwarder.push("s2", { kind: "assistant_delta", turn_id: "t9", item_id: "b", delta: "Hi" });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(sent[3].delta, "Hi", "a lone delta is flushed by the timer");
  assert.equal(sent[3].seq, 1, "the counter is per session");
  forwarder.stop();
});

test("a command turn tags its live progress with the command id", async () => {
  const live = [];
  const manager = new SessionManager(baseConfig, streamingFactory({}), {
    log: () => {},
    onLiveProgress: (sessionId, event) => live.push(event),
  });
  try {
    await manager.completeTurn({ text: "there", commandId: "cmd-1" });
    assert.ok(live.length > 0);
    assert.ok(live.every((event) => event.command_id === "cmd-1"));
  } finally {
    manager.stop();
  }
});

test("a send that throws in the flush timer does not crash the sidecar", async () => {
  const forwarder = new SessionProgressForwarder(() => {
    throw new Error("socket closed");
  }, "claude_code", 5);
  forwarder.push("s1", { kind: "assistant_delta", turn_id: "t", item_id: "a", delta: "x" });
  await new Promise((resolve) => setTimeout(resolve, 30));
  forwarder.stop();
});
