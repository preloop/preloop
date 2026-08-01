// Transcript observer: polling scan over ~/.claude/projects-style JSONL trees.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { TranscriptObserver } from "../dist/observer.js";

function makeTree() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-projects-"));
  const project = path.join(root, "-Users-founder-code-myapp");
  fs.mkdirSync(project);
  return { root, project };
}

function writeRecords(file, records) {
  fs.writeFileSync(
    file,
    records.map((record) => JSON.stringify(record)).join("\n") + "\n",
  );
}

test("emits activity for a fresh transcript and again on growth", () => {
  const { root, project } = makeTree();
  const transcript = path.join(project, "abc-123.jsonl");
  writeRecords(transcript, [
    { sessionId: "abc-123", type: "user", cwd: "/Users/founder/code/myapp" },
  ]);

  const events = [];
  const observer = new TranscriptObserver(root, (event) => events.push(event));
  observer.scanOnce();
  assert.equal(events.length, 1);
  assert.equal(events[0].session_id, "abc-123");
  assert.equal(events[0].cwd, "/Users/founder/code/myapp");
  assert.equal(events[0].last_role, "user");
  assert.equal(events[0].transcript_path, transcript);

  // No growth -> no new event.
  observer.scanOnce();
  assert.equal(events.length, 1);

  // Growth -> one more event reflecting the newest record.
  fs.appendFileSync(
    transcript,
    JSON.stringify({ sessionId: "abc-123", type: "assistant" }) + "\n",
  );
  observer.scanOnce();
  assert.equal(events.length, 2);
  assert.equal(events[1].last_role, "assistant");
  observer.stop();
});

test("falls back to the filename for the session id", () => {
  const { root, project } = makeTree();
  const transcript = path.join(project, "def-456.jsonl");
  writeRecords(transcript, [{ type: "user" }]);

  const events = [];
  const observer = new TranscriptObserver(root, (event) => events.push(event));
  observer.scanOnce();
  assert.equal(events.length, 1);
  assert.equal(events[0].session_id, "def-456");
  observer.stop();
});

test("initial scan skips long-idle transcripts", () => {
  const { root, project } = makeTree();
  const stale = path.join(project, "stale.jsonl");
  writeRecords(stale, [{ sessionId: "stale", type: "user" }]);
  const past = new Date(Date.now() - 60 * 60 * 1000);
  fs.utimesSync(stale, past, past);

  const events = [];
  const observer = new TranscriptObserver(root, (event) => events.push(event));
  observer.scanOnce();
  assert.equal(events.length, 0);

  // But growth after startup is always reported.
  fs.appendFileSync(
    stale,
    JSON.stringify({ sessionId: "stale", type: "assistant" }) + "\n",
  );
  observer.scanOnce();
  assert.equal(events.length, 1);
  observer.stop();
});

test("a missing root directory is tolerated", () => {
  const events = [];
  const observer = new TranscriptObserver(
    path.join(os.tmpdir(), "does-not-exist-" + Date.now()),
    (event) => events.push(event),
  );
  observer.scanOnce();
  assert.equal(events.length, 0);
  observer.stop();
});

test("corrupt trailing lines do not break the summary", () => {
  const { root, project } = makeTree();
  const transcript = path.join(project, "ghi-789.jsonl");
  fs.writeFileSync(
    transcript,
    JSON.stringify({ sessionId: "ghi-789", type: "user" }) +
      "\n{ this is not json",
  );

  const events = [];
  const observer = new TranscriptObserver(root, (event) => events.push(event));
  observer.scanOnce();
  assert.equal(events.length, 1);
  assert.equal(events[0].session_id, "ghi-789");
  assert.equal(events[0].last_role, "user");
  observer.stop();
});
