// Rollout observer against synthetic Codex JSONL (no local Codex install).
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { TranscriptObserver, summarizeRolloutRecords } from "../dist/observer.js";

const SESSION = "11111111-1111-4111-8111-111111111111";

function rolloutLines() {
  return [
    {
      type: "session_meta",
      timestamp: "2026-01-01T00:00:00Z",
      payload: {
        id: SESSION,
        session_id: SESSION,
        cwd: "/tmp/workspace",
      },
    },
    {
      type: "event_msg",
      timestamp: "2026-01-01T00:00:01Z",
      payload: { type: "task_started", turn_id: "t1" },
    },
    {
      type: "response_item",
      timestamp: "2026-01-01T00:00:02Z",
      payload: { type: "message", role: "user" },
    },
    {
      type: "event_msg",
      timestamp: "2026-01-01T00:00:03Z",
      payload: { type: "task_started", turn_id: "t2" },
    },
    {
      type: "response_item",
      timestamp: "2026-01-01T00:00:04Z",
      payload: { type: "message", role: "assistant" },
    },
  ];
}

function writeJsonl(file, records) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(
    file,
    records.map((record) => JSON.stringify(record)).join("\n") + "\n",
  );
}

test("summarizeRolloutRecords reads session id, cwd, role, and turn count", () => {
  const summary = summarizeRolloutRecords(
    rolloutLines(),
    `rollout-2026-01-01T00-00-00-${SESSION}.jsonl`,
  );
  assert.equal(summary.session_id, SESSION);
  assert.equal(summary.cwd, "/tmp/workspace");
  assert.equal(summary.last_role, "assistant");
  assert.equal(summary.turn_count, 2);
});

test("a filename UUID is the session id when session_meta is absent", () => {
  const summary = summarizeRolloutRecords(
    [
      {
        type: "turn_context",
        payload: { cwd: "/tmp/other" },
      },
      {
        type: "event_msg",
        payload: { type: "item_completed", thread_id: "ignored-if-we-prefer-nothing" },
      },
    ],
    "rollout-2026-01-02T00-00-00-22222222-2222-4222-8222-222222222222.jsonl",
  );
  // thread_id on an event is used when present; this fixture sets one.
  assert.equal(summary.session_id, "ignored-if-we-prefer-nothing");
  assert.equal(summary.cwd, "/tmp/other");
  assert.equal(summary.turn_count, 0);
});

test("filename UUID is used when no id field exists", () => {
  const summary = summarizeRolloutRecords(
    [{ type: "event_msg", payload: { type: "token_count" } }],
    "rollout-2026-01-02T00-00-00-22222222-2222-4222-8222-222222222222.jsonl",
  );
  assert.equal(summary.session_id, "22222222-2222-4222-8222-222222222222");
  assert.equal(summary.turn_count, 0);
});

test("observer emits activity for a nested rollout and again on growth", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sessions-"));
  const file = path.join(
    root,
    "2026",
    "01",
    "01",
    `rollout-2026-01-01T00-00-00-${SESSION}.jsonl`,
  );
  writeJsonl(file, rolloutLines().slice(0, 3));
  const events = [];
  const observer = new TranscriptObserver(root, (event) => events.push(event));
  observer.scanOnce();
  assert.equal(events.length, 1);
  assert.equal(events[0].session_id, SESSION);
  assert.equal(events[0].cwd, "/tmp/workspace");
  assert.equal(events[0].last_role, "user");
  assert.equal(events[0].turn_count, 1);
  assert.equal(events[0].transcript_path, file);
  assert.equal(events[0].text, undefined);

  observer.scanOnce();
  assert.equal(events.length, 1);

  fs.appendFileSync(
    file,
    JSON.stringify({
      type: "response_item",
      payload: { type: "message", role: "assistant" },
    }) + "\n",
  );
  observer.scanOnce();
  assert.equal(events.length, 2);
  assert.equal(events[1].last_role, "assistant");
  observer.stop();
});

test("a missing sessions directory is tolerated", () => {
  const events = [];
  const observer = new TranscriptObserver(
    path.join(os.tmpdir(), "codex-missing-" + Date.now()),
    (event) => events.push(event),
  );
  observer.scanOnce();
  assert.equal(events.length, 0);
  observer.stop();
});

test("corrupt trailing lines do not hide an earlier record", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sessions-"));
  const file = path.join(root, "2026", "01", "02", `rollout-${SESSION}.jsonl`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(
    file,
    JSON.stringify({
      type: "session_meta",
      payload: { id: SESSION, cwd: "/tmp/workspace" },
    }) + "\n{ this is not json\n",
  );
  const events = [];
  const observer = new TranscriptObserver(root, (event) => events.push(event));
  observer.scanOnce();
  assert.equal(events.length, 1);
  assert.equal(events[0].session_id, SESSION);
  assert.equal(events[0].cwd, "/tmp/workspace");
  observer.stop();
});
