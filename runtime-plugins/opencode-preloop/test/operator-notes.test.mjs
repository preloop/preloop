import assert from "node:assert/strict";
import { test } from "node:test";
import { PreloopOpenCodePlugin } from "../dist/index.js";

function setup(fail = false) {
  const calls = [];
  const plugin = new PreloopOpenCodePlugin(undefined, async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    if (fail) throw new Error("offline");
    return {
      ok: true,
      status: 200,
      json: async () => ({
        text: "<operator-notes>Run checks</operator-notes>",
        notes: [
          { note_id: "note_synthetic", envelope: {}, text: "Run checks" },
        ],
      }),
    };
  });
  plugin.configure({
    runtime: "opencode",
    control_ws_url: "wss://example.com/api/v1/agents/control/ws",
    bearer_token: "synthetic-token",
  });
  return { plugin, calls };
}

test("system transform registers session and appends notes before model request", async () => {
  const { plugin, calls } = setup();
  const output = { system: ["existing system prompt"] };
  await plugin.transformOperatorNotes({ sessionID: "ses_synthetic" }, output);
  assert.equal(calls.length, 3);
  assert.equal(calls[0].body.session_id, "ses_synthetic");
  assert.match(calls[0].url, /session-start$/);
  assert.match(calls[1].url, /notes\/pending$/);
  assert.equal(calls[1].body.session_id, "ses_synthetic");
  assert.equal(calls[1].body.defer_delivery, true);
  assert.match(calls[2].url, /notes\/ack$/);
  assert.deepEqual(calls[2].body.note_ids, ["note_synthetic"]);
  assert.deepEqual(output.system, [
    "existing system prompt",
    "<operator-notes>Run checks</operator-notes>",
  ]);
  await plugin.transformOperatorNotes({ sessionID: "ses_synthetic" }, output);
  assert.equal(calls.filter((c) => c.url.endsWith("session-start")).length, 1);
});

test("missing session never pulls; transport failure preserves model request", async () => {
  const { plugin, calls } = setup(true);
  const output = { system: ["existing"] };
  await plugin.transformOperatorNotes({}, output);
  assert.equal(calls.length, 0);
  await plugin.transformOperatorNotes({ sessionID: "ses_synthetic" }, output);
  assert.deepEqual(output.system, ["existing"]);
  await plugin.transformOperatorNotes({ sessionID: "ses_synthetic" }, output);
  assert.equal(calls.length, 2, "failed registration is retried");
});

test("deleted session posts authoritative end and resets registration", async () => {
  const { plugin, calls } = setup();
  await plugin.transformOperatorNotes(
    { sessionID: "ses_synthetic" },
    { system: [] },
  );
  await plugin.endNoteSession("ses_synthetic");
  assert.deepEqual(calls.at(-1).body, {
    external_session_id: "ses_synthetic",
    reason: "session_deleted",
  });
  await plugin.transformOperatorNotes(
    { sessionID: "ses_synthetic" },
    { system: [] },
  );
  assert.equal(calls.filter((c) => c.url.endsWith("session-start")).length, 2);
});

test("failed context mutation never acknowledges the claim", async () => {
  const { plugin, calls } = setup();
  await plugin.transformOperatorNotes(
    { sessionID: "ses_synthetic" },
    { system: Object.freeze([]) },
  );
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.defer_delivery, true);
  assert.equal(
    calls.some((c) => c.url.endsWith("notes/ack")),
    false,
  );
});

test("failed acknowledgment is retried before another pull without inserting the note twice", async () => {
  const calls = [];
  let ackAttempts = 0;
  const plugin = new PreloopOpenCodePlugin(undefined, async (url, init) => {
    calls.push(url);
    if (url.endsWith("notes/ack") && ++ackAttempts === 1)
      throw new Error("offline");
    return {
      ok: true,
      status: 200,
      json: async () => ({
        text:
          calls.filter((u) => u.endsWith("notes/pending")).length === 1
            ? "instruction"
            : null,
        notes: [
          { note_id: "note_synthetic", envelope: {}, text: "instruction" },
        ],
      }),
    };
  });
  plugin.configure({
    runtime: "opencode",
    control_ws_url: "wss://example.com/api/v1/agents/control/ws",
    bearer_token: "synthetic",
  });
  const output = { system: [] };
  await plugin.transformOperatorNotes({ sessionID: "ses_synthetic" }, output);
  await plugin.transformOperatorNotes({ sessionID: "ses_synthetic" }, output);
  assert.deepEqual(output.system, ["instruction"]);
  assert.equal(ackAttempts, 2);
  assert.match(calls[3], /notes\/ack$/);
});

test("concurrent transforms serialize acknowledgment before the next pull", async () => {
  const calls = [];
  const plugin = new PreloopOpenCodePlugin(undefined, async (url) => {
    calls.push(url.split("/agents/")[1]);
    await new Promise((resolve) => setTimeout(resolve, 1));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        text: "instruction",
        notes: [{ note_id: "note_synthetic" }],
      }),
    };
  });
  plugin.configure({
    runtime: "opencode",
    control_ws_url: "wss://example.com/api/v1/agents/control/ws",
    bearer_token: "synthetic",
  });
  await Promise.all([
    plugin.transformOperatorNotes(
      { sessionID: "ses_synthetic" },
      { system: [] },
    ),
    plugin.transformOperatorNotes(
      { sessionID: "ses_synthetic" },
      { system: [] },
    ),
  ]);
  assert.deepEqual(calls, [
    "session-start",
    "notes/pending",
    "notes/ack",
    "notes/pending",
    "notes/ack",
  ]);
});

test("duplicate terminal callbacks share one successful end", async () => {
  const { plugin, calls } = setup();
  await Promise.all(Array.from({ length: 10 }, () => plugin.endNoteSession("ses_synthetic")));
  await plugin.endNoteSession("ses_synthetic");
  assert.equal(calls.filter(call => call.url.endsWith("session-end")).length, 1);
});

test("failed terminal delivery remains retryable", async () => {
  const { plugin, calls } = setup(true);
  await plugin.endNoteSession("ses_synthetic");
  await plugin.endNoteSession("ses_synthetic");
  assert.equal(calls.filter(call => call.url.endsWith("session-end")).length, 2);
});
