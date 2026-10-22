// AppServerDriver against a fake JSON-RPC peer standing in for the shared
// `codex app-server` daemon. No codex binary, no socket.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AppServerDriver,
  decodeServerFrame,
  encodeClientFrame,
  mapNotification,
} from "../dist/app-server-client.js";

const TUI = "thread-tui";
const HELPER = "thread-title-helper";

/**
 * A fake daemon. `threads` maps id -> { ephemeral, active }. Every request is
 * recorded; `notify` pushes a server notification to the driver.
 */
function fakeDaemon({ threads = { [TUI]: { ephemeral: false }, [HELPER]: { ephemeral: true } }, version = "0.163.0" } = {}) {
  const calls = [];
  let onMessage = () => {};
  let turnSeq = 0;
  const peer = {
    calls,
    threads,
    notify(method, params) {
      onMessage(JSON.stringify({ method, params }));
    },
    serverRequest(id, method, params) {
      onMessage(JSON.stringify({ id, method, params }));
    },
    replies: [],
  };
  const reply = (id, result) => setImmediate(() => onMessage(JSON.stringify({ id, result })));
  const fail = (id, message) => setImmediate(() => onMessage(JSON.stringify({ id, error: { code: -32600, message } })));
  const transport = {
    send(text) {
      const message = JSON.parse(text);
      if (message.method === undefined) { peer.replies.push(message); return; }
      calls.push(message);
      const { id, method, params } = message;
      if (id === undefined) return;
      switch (method) {
        case "initialize":
          return reply(id, { userAgent: `codex-tui/${version} (Mac OS; arm64) test` });
        case "thread/loaded/list":
          return reply(id, { data: Object.keys(threads), nextCursor: null });
        case "thread/read":
          return reply(id, { thread: { id: params.threadId, ephemeral: threads[params.threadId]?.ephemeral ?? true } });
        case "thread/resume":
          return reply(id, { thread: { id: params.threadId, status: { type: threads[params.threadId]?.active ? "active" : "idle" } } });
        case "thread/turns/list": {
          const active = threads[params.threadId]?.active;
          return reply(id, { data: active ? [{ id: active, status: "inProgress" }] : [{ id: "old", status: "completed" }], nextCursor: null });
        }
        case "turn/start": {
          turnSeq += 1;
          return reply(id, { turn: { id: `turn-${turnSeq}`, status: "inProgress" } });
        }
        case "turn/steer":
          return reply(id, { turnId: params.expectedTurnId });
        case "turn/interrupt":
          return reply(id, {});
        default:
          return fail(id, `unexpected ${method}`);
      }
    },
    onMessage(listener) { onMessage = listener; },
    onClose() {},
    close() { peer.closed = true; },
  };
  return { peer, factory: async () => transport };
}

function makeDriver(daemon, extra = {}) {
  const events = [];
  const driver = new AppServerDriver({
    transportFactory: daemon.factory,
    socketExists: () => true,
    onEvent: (threadId, event) => events.push({ threadId, event }),
    ...extra,
  });
  return { driver, events };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
async function until(predicate) {
  for (let i = 0; i < 200; i += 1) {
    if (predicate()) return;
    await tick();
  }
  throw new Error("condition not reached");
}

test("resume subscribes then turn/start on idle thread", async () => {
  const daemon = fakeDaemon();
  const { driver } = makeDriver(daemon);
  await driver.refresh();
  const pending = driver.sendTurn(TUI, "say hi");
  await until(() => daemon.peer.calls.some((c) => c.method === "turn/start"));
  const methods = daemon.peer.calls.map((c) => c.method);
  assert.ok(methods.indexOf("thread/resume") < methods.indexOf("turn/start"));
  const start = daemon.peer.calls.find((c) => c.method === "turn/start");
  assert.equal(start.params.threadId, TUI);
  assert.deepEqual(start.params.input, [{ type: "text", text: "say hi", text_elements: [] }]);
  assert.ok(daemon.peer.calls.some((c) => c.method === "initialized"));
  assert.equal(daemon.peer.calls[0].params.capabilities.experimentalApi, true);

  daemon.peer.notify("turn/started", { threadId: TUI, turn: { id: "turn-1" } });
  daemon.peer.notify("item/completed", { threadId: TUI, turnId: "turn-1", item: { type: "agentMessage", id: "m1", text: "hi" } });
  daemon.peer.notify("thread/tokenUsage/updated", { threadId: TUI, turnId: "turn-1", tokenUsage: { last: { inputTokens: 3, outputTokens: 2, cachedInputTokens: 0, cacheWriteInputTokens: 0, reasoningOutputTokens: 0, totalTokens: 5 } } });
  daemon.peer.notify("turn/completed", { threadId: TUI, turn: { id: "turn-1", status: "completed", durationMs: 40 } });
  const turn = await pending;
  assert.equal(turn.finalResponse, "hi");
  assert.equal(turn.steered, false);
  assert.equal(turn.usage.input_tokens, 3);
  assert.equal(daemon.peer.calls.filter((c) => c.method === "thread/resume").length, 1);
  driver.stop();
});

test("steer sends expectedTurnId of the active turn", async () => {
  const daemon = fakeDaemon();
  const { driver } = makeDriver(daemon);
  await driver.refresh();
  await driver.subscribe(TUI);
  daemon.peer.notify("turn/started", { threadId: TUI, turn: { id: "turn-typed-in-tui" } });
  assert.equal(driver.activeTurnId(TUI), "turn-typed-in-tui");
  const pending = driver.sendTurn(TUI, "also check the tests");
  await until(() => daemon.peer.calls.some((c) => c.method === "turn/steer"));
  const steer = daemon.peer.calls.find((c) => c.method === "turn/steer");
  assert.equal(steer.params.expectedTurnId, "turn-typed-in-tui");
  assert.equal(daemon.peer.calls.some((c) => c.method === "turn/start"), false);
  daemon.peer.notify("turn/completed", { threadId: TUI, turn: { id: "turn-typed-in-tui", status: "completed" } });
  const turn = await pending;
  assert.equal(turn.steered, true);
  assert.equal(driver.activeTurnId(TUI), undefined);
  driver.stop();
});

test("interrupt maps to turn/interrupt", async () => {
  const daemon = fakeDaemon();
  const { driver } = makeDriver(daemon);
  await driver.refresh();
  await driver.subscribe(TUI);
  daemon.peer.notify("turn/started", { threadId: TUI, turn: { id: "turn-9" } });
  await driver.interrupt(TUI);
  const call = daemon.peer.calls.find((c) => c.method === "turn/interrupt");
  assert.deepEqual(call.params, { threadId: TUI, turnId: "turn-9" });
  daemon.peer.notify("turn/completed", { threadId: TUI, turn: { id: "turn-9", status: "interrupted" } });
  await assert.rejects(driver.interrupt(TUI), /no turn in progress/);
  driver.stop();
});

test("loaded thread ids are reported reachable", async () => {
  const daemon = fakeDaemon();
  const seen = [];
  const { driver } = makeDriver(daemon, { onReachableChanged: (ids) => seen.push(ids) });
  const ids = await driver.refresh();
  // The ephemeral title-generation helper is not a session.
  assert.deepEqual(ids, [TUI]);
  assert.deepEqual(driver.reachableThreadIds(), [TUI]);
  assert.equal(driver.isReachable(HELPER), false);
  assert.deepEqual(seen, [[TUI]]);
  await driver.refresh();
  assert.equal(seen.length, 1, "unchanged lists do not re-announce");
  // A thread that is not loaded is never resumed (it would fork the rollout).
  await assert.rejects(driver.subscribe("thread-on-disk-only"), /not loaded/);
  assert.equal(daemon.peer.calls.some((c) => c.method === "thread/resume"), false);
  driver.stop();
});

test("item/completed commandExecution maps to tool_end with exit code", async () => {
  const mapped = mapNotification("item/completed", {
    threadId: TUI,
    turnId: "turn-1",
    item: { type: "commandExecution", id: "call_1", command: "npm test", status: "failed", exitCode: 2, durationMs: 812 },
  });
  assert.deepEqual(mapped, {
    threadId: TUI,
    event: { kind: "tool_end", turn_id: "turn-1", call_id: "call_1", tool_name: "exec_command", exit_code: 2, status: "failed", duration_ms: 812 },
  });
  const started = mapNotification("item/started", {
    threadId: TUI, turnId: "turn-1", item: { type: "commandExecution", id: "call_1", command: "npm test" },
  });
  assert.equal(started.event.kind, "tool_start");
  assert.equal(started.event.command, "npm test");
  assert.equal(mapNotification("item/completed", { threadId: TUI, item: { type: "userMessage" } }), undefined);

  // Only subscribed threads are forwarded.
  const daemon = fakeDaemon();
  const { driver, events } = makeDriver(daemon);
  await driver.refresh();
  daemon.peer.notify("item/completed", { threadId: TUI, turnId: "t", item: { type: "commandExecution", id: "c0", exitCode: 0 } });
  assert.equal(events.length, 0);
  await driver.subscribe(TUI);
  daemon.peer.notify("item/completed", { threadId: TUI, turnId: "t", item: { type: "commandExecution", id: "c1", exitCode: 0 } });
  assert.equal(events.length, 1);
  assert.equal(events[0].event.exit_code, 0);
  driver.stop();
});

test("agentMessage delta maps to assistant_delta", async () => {
  const daemon = fakeDaemon();
  const { driver, events } = makeDriver(daemon);
  await driver.refresh();
  await driver.subscribe(TUI);
  daemon.peer.notify("item/agentMessage/delta", { threadId: TUI, turnId: "turn-1", itemId: "m1", delta: "Hel" });
  daemon.peer.notify("item/reasoning/summaryTextDelta", { threadId: TUI, turnId: "turn-1", itemId: "r1", delta: "Reading files", summaryIndex: 0 });
  assert.deepEqual(events[0], { threadId: TUI, event: { kind: "assistant_delta", turn_id: "turn-1", item_id: "m1", delta: "Hel" } });
  assert.deepEqual(events[1].event, { kind: "status_title", turn_id: "turn-1", delta: "Reading files" });
  driver.stop();
});

test("daemon socket missing reports no reachable TUI threads", async () => {
  let spawned = 0;
  const driver = new AppServerDriver({
    socketExists: () => false,
    transportFactory: async () => { spawned += 1; throw new Error("should not connect"); },
  });
  assert.deepEqual(await driver.refresh(), []);
  assert.deepEqual(driver.reachableThreadIds(), []);
  assert.equal(spawned, 0);

  // A daemon that drops away clears a previously reachable list.
  const daemon = fakeDaemon();
  let present = true;
  const { driver: live } = makeDriver(daemon, { socketExists: () => present });
  assert.deepEqual(await live.refresh(), [TUI]);
  present = false;
  assert.deepEqual(await live.refresh(), []);
  live.stop();
});

test("an app-server older than the pinned protocol is refused", async () => {
  const daemon = fakeDaemon({ version: "0.150.0" });
  const { driver } = makeDriver(daemon);
  assert.deepEqual(await driver.refresh(), []);
  assert.equal(daemon.peer.calls.some((c) => c.method === "thread/loaded/list"), false);
  driver.stop();
});

test("approval requests are left to the TUI, never answered", async () => {
  const daemon = fakeDaemon();
  const { driver } = makeDriver(daemon);
  await driver.refresh();
  await driver.subscribe(TUI);
  daemon.peer.serverRequest(7, "item/commandExecution/requestApproval", { threadId: TUI, itemId: "call_1" });
  await tick();
  assert.deepEqual(daemon.peer.replies, []);
  driver.stop();
});

test("websocket frames round-trip through the proxy codec", () => {
  for (const size of [5, 300, 70_000]) {
    const text = "x".repeat(size);
    const frame = encodeClientFrame(text);
    const decoded = decodeServerFrame(frame);
    assert.equal(decoded.payload.toString(), text);
    assert.equal(decoded.consumed, frame.length);
    assert.equal(decoded.fin, true);
  }
  assert.equal(decodeServerFrame(Buffer.from([0x81])), undefined);
});

test("a thread resumed mid-turn is steered, not started", async () => {
  const daemon = fakeDaemon({ threads: { [TUI]: { ephemeral: false, active: "turn-typed-before-subscribe" } } });
  const { driver } = makeDriver(daemon);
  await driver.refresh();
  const pending = driver.sendTurn(TUI, "and add a test");
  await until(() => daemon.peer.calls.some((c) => c.method === "turn/steer"));
  const steer = daemon.peer.calls.find((c) => c.method === "turn/steer");
  assert.equal(steer.params.expectedTurnId, "turn-typed-before-subscribe");
  assert.equal(daemon.peer.calls.some((c) => c.method === "turn/start"), false);
  daemon.peer.notify("turn/completed", { threadId: TUI, turn: { id: "turn-typed-before-subscribe", status: "completed" } });
  assert.equal((await pending).steered, true);
  driver.stop();
});

test("a turn that completes before the turn/start reply is not left active", async () => {
  const daemon = fakeDaemon();
  const { driver } = makeDriver(daemon);
  await driver.refresh();
  await driver.subscribe(TUI);
  // The completion is pushed before the turn/start reply (setImmediate) lands.
  const original = daemon.peer.notify;
  const pending = driver.sendTurn(TUI, "quick");
  await until(() => daemon.peer.calls.some((c) => c.method === "turn/start"));
  original("turn/started", { threadId: TUI, turn: { id: "turn-1" } });
  original("item/completed", { threadId: TUI, turnId: "turn-1", item: { type: "agentMessage", id: "m1", text: "done" } });
  original("turn/completed", { threadId: TUI, turn: { id: "turn-1", status: "completed" } });
  const turn = await pending;
  assert.equal(turn.finalResponse, "done");
  assert.equal(driver.activeTurnId(TUI), undefined);
  const next = driver.sendTurn(TUI, "again");
  await until(() => daemon.peer.calls.filter((c) => c.method === "turn/start").length === 2);
  assert.equal(daemon.peer.calls.some((c) => c.method === "turn/steer"), false);
  daemon.peer.notify("turn/completed", { threadId: TUI, turn: { id: "turn-2", status: "completed" } });
  await next;
  driver.stop();
});

test("a dropped daemon connection forgets the active turn", async () => {
  let close;
  const daemon = fakeDaemon();
  const factory = async () => {
    const transport = await daemon.factory();
    const onClose = transport.onClose;
    transport.onClose = (listener) => { close = listener; onClose(listener); };
    return transport;
  };
  const { driver } = makeDriver({ factory });
  await driver.refresh();
  await driver.subscribe(TUI);
  daemon.peer.notify("turn/started", { threadId: TUI, turn: { id: "turn-5" } });
  assert.equal(driver.activeTurnId(TUI), "turn-5");
  close("daemon restarted");
  assert.equal(driver.activeTurnId(TUI), undefined);
  driver.stop();
});
