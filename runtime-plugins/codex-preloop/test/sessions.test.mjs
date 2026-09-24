// SessionManager routing against a fake Codex client (no SDK, no network).
import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionManager, threadOptionsFor } from "../dist/sessions.js";

const baseConfig = {
  runtime: "codex",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "codex-1",
  workspace_root: "/tmp/workspace",
  codex_model: "gpt-test",
};

function makeFakeFactory(state) {
  return () => ({
    startThread(options) {
      const id = `thread-${state.threads.length + 1}`;
      const thread = makeThread(state, id, options);
      state.starts.push({ id, options });
      return thread;
    },
    resumeThread(id, options) {
      state.resumes.push({ id, options });
      return makeThread(state, id, options);
    },
  });
}

function makeThread(state, id, options) {
  const thread = {
    id,
    options,
    runs: 0,
    async run(input, runOptions) {
      thread.runs += 1;
      state.runs.push({ id, input, options });
      if (runOptions?.signal?.aborted) {
        throw abortError();
      }
      return {
        finalResponse: `echo: ${input}`,
        usage: { input_tokens: 3, output_tokens: 5, cached_input_tokens: 0, cache_write_input_tokens: 0, reasoning_output_tokens: 1 },
      };
    },
  };
  state.threads.push(thread);
  return thread;
}

function abortError() {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

function makeManager(config = baseConfig) {
  const state = { threads: [], starts: [], resumes: [], runs: [] };
  const manager = new SessionManager(config, makeFakeFactory(state));
  return { manager, state };
}

test("start_new_session starts a thread in workspace_root and returns the reply", async () => {
  const { manager, state } = makeManager();
  const outcome = await manager.sendMessage({
    text: "hello",
    startNewSession: true,
  });
  assert.equal(outcome.reply_text, "echo: hello");
  assert.equal(outcome.session_id, "thread-1");
  assert.equal(outcome.usage.output_tokens, 5);
  assert.equal(state.starts.length, 1);
  assert.equal(state.starts[0].options.workingDirectory, "/tmp/workspace");
  assert.equal(state.starts[0].options.sandboxMode, "workspace-write");
  assert.equal(state.starts[0].options.skipGitRepoCheck, true);
  assert.equal(state.starts[0].options.model, "gpt-test");
  assert.equal(state.starts[0].options.approvalPolicy, undefined);
  manager.stop();
});

test("a second message to the same session id runs on the same thread", async () => {
  const { manager, state } = makeManager();
  const first = await manager.sendMessage({ text: "one", startNewSession: true });
  const second = await manager.sendMessage({
    text: "two",
    targetSessionId: first.session_id,
  });
  assert.equal(second.reply_text, "echo: two");
  assert.equal(second.session_id, first.session_id);
  assert.equal(state.starts.length, 1);
  assert.equal(state.resumes.length, 0);
  assert.equal(state.runs.length, 2);
  manager.stop();
});

test("an unknown session id is resumed", async () => {
  const { manager, state } = makeManager();
  const outcome = await manager.sendMessage({
    text: "resume me",
    targetSessionId: "persisted-abc",
  });
  assert.equal(outcome.reply_text, "echo: resume me");
  assert.equal(outcome.session_id, "persisted-abc");
  assert.equal(state.starts.length, 0);
  assert.equal(state.resumes.length, 1);
  assert.equal(state.resumes[0].id, "persisted-abc");
  assert.equal(state.resumes[0].options.workingDirectory, "/tmp/workspace");
  manager.stop();
});

test("cwd overrides workspace_root", async () => {
  const { manager, state } = makeManager();
  await manager.sendMessage({
    text: "there",
    startNewSession: true,
    cwd: "/tmp/other",
  });
  assert.equal(state.starts[0].options.workingDirectory, "/tmp/other");
  manager.stop();
});

test("danger-full-access is refused unless codex_allow_full_access is set", () => {
  assert.throws(
    () =>
      threadOptionsFor({
        ...baseConfig,
        codex_sandbox_mode: "danger-full-access",
      }),
    /codex_allow_full_access/,
  );
  const allowed = threadOptionsFor({
    ...baseConfig,
    codex_sandbox_mode: "danger-full-access",
    codex_allow_full_access: true,
  });
  assert.equal(allowed.sandboxMode, "danger-full-access");
  const readOnly = threadOptionsFor({
    ...baseConfig,
    codex_sandbox_mode: "read-only",
  });
  assert.equal(readOnly.sandboxMode, "read-only");
});

test("interrupt aborts a running turn and the outcome is stopped", async () => {
  let notifyStarted;
  const started = new Promise((resolve) => {
    notifyStarted = resolve;
  });
  const state = { runs: 0 };
  const factory = () => ({
    startThread() {
      return {
        id: "thread-live",
        run(_input, options) {
          state.runs += 1;
          notifyStarted();
          return new Promise((_resolve, reject) => {
            const fail = () => reject(abortError());
            if (options?.signal?.aborted) {
              fail();
              return;
            }
            options?.signal?.addEventListener("abort", fail, { once: true });
          });
        },
      };
    },
    resumeThread() {
      throw new Error("unexpected resume");
    },
  });
  const manager = new SessionManager(baseConfig, factory);
  const pending = manager.sendMessage({ text: "go", startNewSession: true });
  await started;
  await manager.interrupt("thread-live");
  const outcome = await pending;
  assert.equal(outcome.stopped, true);
  assert.equal(outcome.reply_text, "");
  assert.equal(outcome.session_id, "thread-live");
  assert.equal(state.runs, 1);
  manager.stop();
});

test("a second turn waits until the in-flight run finishes", async () => {
  let releaseFirst;
  const gate = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  let runs = 0;
  const factory = () => ({
    startThread() {
      return {
        id: "thread-serial",
        async run(input) {
          runs += 1;
          if (runs === 1) {
            await gate;
          }
          return { finalResponse: `echo: ${input}`, usage: null };
        },
      };
    },
    resumeThread() {
      throw new Error("unexpected resume");
    },
  });
  const manager = new SessionManager(baseConfig, factory);
  const first = manager.sendMessage({ text: "one", startNewSession: true });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const second = manager.sendMessage({
    text: "two",
    targetSessionId: "thread-serial",
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(runs, 1);
  releaseFirst();
  assert.equal((await first).reply_text, "echo: one");
  assert.equal((await second).reply_text, "echo: two");
  assert.equal(runs, 2);
  manager.stop();
});

test("interrupt with no running turn reports the honest limitation", async () => {
  const { manager } = makeManager();
  await assert.rejects(
    () => manager.interrupt("some-tui-session"),
    /owned by the sidecar/,
  );
  manager.stop();
});

test("a hung turn rejects after turn_timeout_ms", async () => {
  const factory = () => ({
    startThread() {
      return {
        id: "thread-hung",
        run() {
          return new Promise(() => {});
        },
      };
    },
    resumeThread() {
      throw new Error("unexpected resume");
    },
  });
  const manager = new SessionManager(
    { ...baseConfig, turn_timeout_ms: 40 },
    factory,
  );
  await assert.rejects(
    () => manager.sendMessage({ text: "never", startNewSession: true }),
    /timed out after 40ms/,
  );
  manager.stop();
});
