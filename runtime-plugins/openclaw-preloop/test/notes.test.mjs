import assert from "node:assert/strict";
import { test } from "node:test";
import { OperatorNotes } from "../dist/notes.js";

function fixture() {
  const calls = [];
  let ackFails = false;
  const text =
    '<operator-notes><operator-note id="note-1">Synthetic steer.</operator-note></operator-notes>';
  const notes = new OperatorNotes(
    "https://example.com/api/v1/agents/permission-check",
    "agt_synthetic",
    async (url, init) => {
      const body = JSON.parse(init.body);
      const route = url.split("/agents/")[1];
      calls.push({ route, body, auth: init.headers.Authorization });
      if (route === "notes/ack" && ackFails) throw new Error("offline");
      return {
        ok: true,
        json: async () =>
          route === "session-start"
            ? { runtime_session_id: "runtime-synthetic" }
            : route === "notes/pending"
              ? { text, notes: [{ note_id: "note-1" }] }
              : { ok: true },
      };
    },
  );
  const ctx = { sessionId: "session-1", runId: "run-1" };
  const input = {
    ...ctx,
    prompt: text + "\n\nUser prompt.",
    historyMessages: [],
  };
  const confirm = async (context = ctx) => {
    const call = { ...context, callId: "call-1" };
    await notes.started(call, context);
    await notes.completed({ ...call, outcome: "completed" }, context);
  };
  return {
    calls,
    notes,
    text,
    ctx,
    input,
    confirm,
    failAck: (fail) => {
      ackFails = fail;
    },
  };
}

test("deferred claim registers own session and ACKs only admitted current context", async () => {
  const f = fixture();
  assert.deepEqual(await f.notes.build(f.ctx), { prependContext: f.text });
  assert.deepEqual(
    f.calls.map((c) => c.route),
    ["session-start", "notes/pending"],
  );
  assert.deepEqual(f.calls[0].body, {
    session_id: "session-1",
    source: "openclaw",
  });
  assert.equal(f.calls[1].body.defer_delivery, true);
  assert.equal(f.calls[1].auth, "Bearer agt_synthetic");
  await f.notes.input(
    { ...f.input, prompt: "stripped", historyMessages: [{ content: f.text }] },
    f.ctx,
  );
  await f.notes.input({ ...f.input, runId: "another-run" }, f.ctx);
  await f.notes.input({ ...f.input, sessionId: "another-session" }, f.ctx);
  assert.equal(f.calls.length, 2);
  await f.notes.input(f.input, f.ctx);
  assert.equal(f.calls.length, 2);
  await f.confirm();
  assert.deepEqual(f.calls[2].body, {
    session_id: "session-1",
    note_ids: ["note-1"],
  });
});

test("failed admission can retry after matching agent_end without terminalizing", async () => {
  const f = fixture();
  await f.notes.build(f.ctx);
  const next = { ...f.ctx, runId: "run-2" };
  assert.equal(await f.notes.build(next), undefined);
  await f.notes.turnEnded({ ...f.ctx, runId: "wrong-run" });
  assert.equal(await f.notes.build(next), undefined);
  await f.notes.turnEnded(f.ctx);
  assert.deepEqual(await f.notes.build(next), { prependContext: f.text });
  assert.equal(f.calls.length, 2);
  await f.notes.input({ ...f.input, runId: next.runId }, next);
  await f.confirm(next);
  assert.equal(f.calls.at(-1).route, "notes/ack");
});

test("failed ACK retries without reinjecting or claiming another batch", async () => {
  const f = fixture();
  await f.notes.build(f.ctx);
  f.failAck(true);
  await f.notes.input(f.input, f.ctx);
  await f.confirm();
  assert.equal(await f.notes.build({ ...f.ctx, runId: "run-2" }), undefined);
  assert.equal(f.calls.filter((c) => c.route === "notes/pending").length, 1);
  f.failAck(false);
  await f.notes.build({ ...f.ctx, runId: "run-2" });
  assert.equal(f.calls.filter((c) => c.route === "notes/pending").length, 2);
});

test("session end accepts only authoritative terminal reasons and uses external id", async () => {
  const f = fixture();
  await f.notes.build(f.ctx);
  for (const reason of [
    undefined,
    "unknown",
    "idle",
    "daily",
    "compaction",
    "shutdown",
    "restart",
  ])
    await f.notes.ended({ sessionId: f.ctx.sessionId, reason });
  assert.equal(f.calls.length, 2);
  await f.notes.ended({ sessionId: f.ctx.sessionId, reason: "deleted" });
  assert.deepEqual(f.calls[2].body, {
    external_session_id: "session-1",
    reason: "deleted",
  });
});

test("concurrent callbacks claim once and keep sessions isolated", async () => {
  const f = fixture();
  await Promise.all([f.notes.build(f.ctx), f.notes.build(f.ctx)]);
  assert.equal(f.calls.filter((c) => c.route === "notes/pending").length, 1);
  await f.notes.input(
    { ...f.input, sessionId: "session-2" },
    { ...f.ctx, sessionId: "session-2" },
  );
  assert.equal(f.calls.length, 2);
});

test("missing identity, invalid registration and unavailable server never claim", async () => {
  const f = fixture();
  for (const ctx of [{}, { sessionId: "s" }, { runId: "r" }])
    assert.equal(await f.notes.build(ctx), undefined);
  assert.equal(f.calls.length, 0);
  for (const fetchImpl of [
    async () => {
      throw new Error("offline");
    },
    async () => ({ ok: true, json: async () => ({}) }),
  ]) {
    const notes = new OperatorNotes(
      "https://example.com/api/v1/agents/permission-check",
      "agt_synthetic",
      fetchImpl,
    );
    assert.equal(await notes.build(f.ctx), undefined);
  }
});

test("authoritative end is posted even after registered-session cache eviction", async () => {
  const calls = [];
  const notes = new OperatorNotes(
    "https://example.com/api/v1/agents/permission-check",
    "agt_synthetic",
    async (url, init) => {
      const route = url.split("/agents/")[1];
      calls.push({ route, body: JSON.parse(init.body) });
      return {
        ok: true,
        json: async () =>
          route === "session-start"
            ? { runtime_session_id: "runtime" }
            : { text: null, notes: [] },
      };
    },
  );
  for (let i = 0; i < 257; i++)
    await notes.build({ sessionId: `session-${i}`, runId: "run" });
  await notes.ended({ sessionId: "session-0", reason: "reset" });
  assert.deepEqual(calls.at(-1), {
    route: "session-end",
    body: { external_session_id: "session-0", reason: "reset" },
  });
});

for (const heldRoute of ["session-start", "notes/pending"]) {
  test(`cache overflow preserves a build awaiting ${heldRoute}`, async () => {
    const calls = [];
    const text = "<operator-notes>Concurrent steer.</operator-notes>";
    let release;
    let entered;
    const hold = new Promise((resolve) => {
      release = resolve;
    });
    const started = new Promise((resolve) => {
      entered = resolve;
    });
    const notes = new OperatorNotes(
      "https://example.com/api/v1/agents/permission-check",
      "agt_synthetic",
      async (url, init) => {
        const route = url.split("/agents/")[1];
        const body = JSON.parse(init.body);
        calls.push({ route, body });
        if (body.session_id === "active" && route === heldRoute) {
          entered();
          await hold;
        }
        return {
          ok: true,
          json: async () =>
            route === "session-start"
              ? { runtime_session_id: "runtime" }
              : route === "notes/pending" && body.session_id === "active"
                ? { text, notes: [{ note_id: "active-note" }] }
                : { text: null, notes: [] },
        };
      },
    );
    const ctx = { sessionId: "active", runId: "run" };
    const build = notes.build(ctx);
    await started;
    for (let i = 0; i < 256; i++)
      await notes.build({ sessionId: `idle-${i}`, runId: "run" });
    release();
    assert.deepEqual(await build, { prependContext: text });
    await notes.input({ ...ctx, prompt: text, historyMessages: [] }, ctx);
    const call = { ...ctx, callId: "active-call" };
    await notes.started(call, ctx);
    await notes.completed({ ...call, outcome: "completed" }, ctx);
    assert.deepEqual(calls.at(-1), {
      route: "notes/ack",
      body: { session_id: "active", note_ids: ["active-note"] },
    });
  });
}

test("cache stays bounded when every registered build is active", async () => {
  let release;
  const hold = new Promise((resolve) => {
    release = resolve;
  });
  let registrations = 0;
  let filled;
  const ready = new Promise((resolve) => {
    filled = resolve;
  });
  const notes = new OperatorNotes(
    "https://example.com/api/v1/agents/permission-check",
    "agt_synthetic",
    async (url) => {
      if (url.endsWith("/session-start")) {
        if (++registrations === 256) filled();
        await hold;
      }
      return {
        ok: true,
        json: async () =>
          url.endsWith("/session-start")
            ? { runtime_session_id: "runtime" }
            : { text: null, notes: [] },
      };
    },
  );
  const builds = Array.from({ length: 256 }, (_, i) =>
    notes.build({ sessionId: `active-${i}`, runId: "run" }),
  );
  await ready;
  try {
    const overflow = notes.build({ sessionId: "overflow", runId: "run" });
    const result = await Promise.race([
      overflow,
      new Promise((resolve) =>
        setImmediate(() => resolve("unexpected pending registration")),
      ),
    ]);
    assert.equal(result, undefined);
    assert.equal(registrations, 256);
  } finally {
    release();
    await Promise.all(builds);
  }
});

test("preflight rejection and changed subsequent input never ACK a tentative witness", async () => {
  const f = fixture();
  await f.notes.build(f.ctx);
  await f.notes.input(f.input, f.ctx);
  assert.equal(f.calls.length, 2); // Preflight may reject before any actual call.
  await f.notes.input(
    { ...f.input, prompt: "context stripped by retry" },
    f.ctx,
  );
  await f.confirm();
  assert.equal(f.calls.length, 2);
});

test("only matching completed actual call confirms the witnessed batch", async () => {
  const f = fixture();
  await f.notes.build(f.ctx);
  await f.notes.input(f.input, f.ctx);
  const call = { ...f.ctx, callId: "actual-call" };
  await f.notes.started(call, f.ctx);
  await f.notes.completed(
    { ...call, callId: "unknown", outcome: "completed" },
    f.ctx,
  );
  await f.notes.completed({ ...call, outcome: "error" }, f.ctx);
  assert.equal(f.calls.length, 2);
  await f.notes.completed({ ...call, outcome: "completed" }, f.ctx);
  assert.equal(f.calls.length, 2); // Error already closed this call id.
  await f.notes.started({ ...call, callId: "retry-call" }, f.ctx);
  await f.notes.completed(
    { ...call, callId: "retry-call", outcome: "completed" },
    f.ctx,
  );
  assert.equal(f.calls.at(-1).route, "notes/ack");
});

test("terminal end retries confirmed ACK first and avoids a false undeliverable receipt", async () => {
  const f = fixture();
  await f.notes.build(f.ctx);
  await f.notes.input(f.input, f.ctx);
  f.failAck(true);
  await f.confirm();
  await f.notes.ended({ sessionId: f.ctx.sessionId, reason: "deleted" });
  assert.equal(
    f.calls.some((call) => call.route === "session-end"),
    false,
  );
  f.failAck(false);
  await f.notes.ended({ sessionId: f.ctx.sessionId, reason: "deleted" });
  assert.deepEqual(
    f.calls.slice(-2).map((call) => call.route),
    ["notes/ack", "session-end"],
  );
});


test("repeated and concurrent terminal callbacks post one successful end", async () => {
  const f = fixture();
  await Promise.all(Array.from({ length: 10 }, () =>
    f.notes.ended({ sessionId: f.ctx.sessionId, reason: "deleted" })));
  await f.notes.ended({ sessionId: f.ctx.sessionId, reason: "deleted" });
  assert.equal(f.calls.filter(call => call.route === "session-end").length, 1);
});
