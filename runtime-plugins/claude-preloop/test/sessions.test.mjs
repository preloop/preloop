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
  // The timed-out session is stopped (its Claude Code child is aborted), so
  // the next command starts or resumes a session instead of queueing
  // behind a turn that never ends.
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(manager.ownedSessionIds().length, 0);
  manager.stop();
});

test("a timeout stops the session, so a turn queued behind it fails too", async () => {
  // Behaviour change (#1679): a timed-out turn aborts the session, which
  // kills the Claude Code child. A second turn already queued on that
  // session cannot be answered and is rejected instead of waiting.
  let turn = 0;
  const factory = async ({ prompt, options }) => ({
    async interrupt() {},
    async *[Symbol.asyncIterator]() {
      yield { type: "system", subtype: "init", session_id: "align-1" };
      const aborted = new Promise((resolve) =>
        options.abortController.signal.addEventListener("abort", resolve),
      );
      for await (const _userMessage of prompt) {
        turn += 1;
        if (turn === 1) {
          await aborted;
          throw Object.assign(new Error("aborted"), { name: "AbortError" });
        }
      }
    },
  });
  const manager = new SessionManager(
    { ...baseConfig, turn_timeout_ms: 80 },
    factory,
  );
  const first = manager.sendMessage({ text: "A" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const second = manager.sendMessage({ text: "B", targetSessionId: "align-1" });
  await assert.rejects(() => first, /timed out after 80ms/);
  await assert.rejects(() => second, /session ended/);
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

test("spawn env registers an SDK session and records the final alias", async () => {
  const fs = await import("node:fs/promises");
  const os = await import("node:os");
  const path = await import("node:path");
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "preloop-1552-"));
  const original = {
    home: process.env.HOME,
    alias: process.env.PRELOOP_SESSION_ALIAS,
    parent: process.env.PRELOOP_PARENT_SESSION_ID,
    fetch: globalThis.fetch,
  };
  const requests = [];
  process.env.HOME = home;
  process.env.PRELOOP_SESSION_ALIAS = "brief";
  process.env.PRELOOP_PARENT_SESSION_ID =
    "11111111-2222-3333-4444-555555555555";
  globalThis.fetch = async (url, options) => {
    requests.push({ url: String(url), body: JSON.parse(options.body) });
    return new Response(
      JSON.stringify({
        runtime_session_id: "99999999-2222-3333-4444-555555555555",
        alias: "brief-2",
        parent_session_id: process.env.PRELOOP_PARENT_SESSION_ID,
      }),
      { status: 200 },
    );
  };
  await fs.mkdir(path.join(home, ".preloop/sessions"), { recursive: true });
  await fs.writeFile(
    path.join(home, ".preloop/sessions/session-1.json"),
    JSON.stringify({
      runtime_session_id: "99999999-2222-3333-4444-555555555555",
      alias: "brief-2",
      title_sent: true,
    }),
  );
  const { manager } = makeManager();
  try {
    await manager.sendMessage({ text: "hello" });
    const starts = requests.filter((r) => r.url.endsWith("/session-start"));
    assert.equal(starts.length, 1);
    assert.equal(requests[0].body.alias, "brief");
    assert.equal(
      requests[0].body.parent_session_id,
      process.env.PRELOOP_PARENT_SESSION_ID,
    );
    const file = JSON.parse(
      await fs.readFile(
        path.join(home, ".preloop/sessions/session-1.json"),
        "utf8",
      ),
    );
    assert.equal(file.alias, "brief-2");
    assert.equal(
      file.title_sent,
      true,
      "preserve the hook first-prompt marker",
    );
  } finally {
    manager.stop();
    globalThis.fetch = original.fetch;
    for (const [key, value] of [
      ["HOME", original.home],
      ["PRELOOP_SESSION_ALIAS", original.alias],
      ["PRELOOP_PARENT_SESSION_ID", original.parent],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await fs.rm(home, { recursive: true, force: true });
  }
});

test("turn progress forwards bounded assistant/tool output and final usage", async () => {
  const events = [];
  const factory = async ({ prompt }) => ({
    async interrupt() {},
    async *[Symbol.asyncIterator]() {
      yield { type: "system", subtype: "init", session_id: "progress-example" };
      for await (const _ of prompt) {
        yield {
          type: "assistant",
          message: {
            content: [
              { type: "text", text: "x".repeat(4000) },
              {
                type: "tool_use",
                id: "call-example",
                name: "Read",
                input: { secret: "excluded" },
              },
            ],
          },
        };
        yield {
          type: "result",
          result: "done",
          usage: { input_tokens: 3, output_tokens: 2 },
        };
      }
    },
  });
  const manager = new SessionManager(baseConfig, factory);
  try {
    await manager.completeTurn({
      text: "run",
      onProgress: (event) => events.push(event),
    });
    assert.equal(events[0].summary.length, 1000);
    assert.deepEqual(events[1], {
      kind: "tool",
      summary: "Tool: Read",
      call_id: "call-example",
      tool_name: "Read",
    });
    assert.match(events[2].summary, /Turn completed in .*ms; tokens: 5/);
    assert.equal(JSON.stringify(events).includes("excluded"), false);
  } finally {
    manager.stop();
  }
});


test("a finished turn records its end time and no turn text (#1889)", async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url: String(url), body: JSON.parse(options.body) });
    return new Response(null, { status: 204 });
  };
  const { manager } = makeManager();
  try {
    await manager.sendMessage({ text: "secret plan" });
    await new Promise((resolve) => setImmediate(resolve));
    const events = requests.filter((r) => r.url.endsWith("/session-event"));
    assert.equal(events.length, 1);
    assert.equal(
      events[0].url,
      "https://example.preloop.ai/api/v1/agents/session-event",
    );
    assert.deepEqual(events[0].body, {
      session_id: "session-1",
      hook_event_name: "Stop",
      source: "claude_code",
    });
    assert.ok(!JSON.stringify(requests).includes("secret plan"));
  } finally {
    manager.stop();
    await new Promise((resolve) => setImmediate(resolve));
    globalThis.fetch = original;
  }
});
