// Sidecar command dispatch against a fake Codex client. No websocket.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  PreloopCodexSidecar,
  controlAuthHeaders,
  resolveResumeSessionId,
  resolveTargetSessionId,
} from "../dist/index.js";
import { WorkspaceManager } from "../dist/workspace.js";

const baseConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "codex",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "codex-1",
  workspace_root: "/tmp/workspace",
  observer_enabled: false,
};

function abortError() {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

function makeEchoFactory(state) {
  let next = 0;
  return () => ({
    startThread(options) {
      next += 1;
      const id = `thread-${next}`;
      state.starts.push({ id, options });
      return makeThread(state, id);
    },
    resumeThread(id, options) {
      state.resumes.push({ id, options });
      return makeThread(state, id);
    },
  });
}

function makeThread(state, id) {
  return {
    id,
    async run(input) {
      state.runs += 1;
      state.texts.push(input);
      return {
        finalResponse: `ok: ${input}`,
        usage: { input_tokens: 2, output_tokens: 4 },
      };
    },
  };
}

function makeSidecar() {
  const state = { texts: [], runs: 0, starts: [], resumes: [] };
  const sidecar = new PreloopCodexSidecar(undefined, makeEchoFactory(state));
  sidecar.configure(baseConfig);
  return { sidecar, state };
}

test("send_message with start_new_session returns reply_text as command_result", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "m-1",
      payload: { text: "deploy the fix", start_new_session: true },
    }),
  );
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].name, "command_result");
  assert.equal(socket.sent[0].payload.reply_text, "ok: deploy the fix");
  assert.equal(socket.sent[0].payload.status, "completed");
  assert.equal(socket.sent[0].payload.session_id, "thread-1");
  assert.deepEqual(socket.sent[0].payload.metadata.usage, {
    input_tokens: 2,
    output_tokens: 4,
  });
  assert.equal(state.starts.length, 1);
  assert.equal(state.starts[0].options.workingDirectory, "/tmp/workspace");
  assert.equal(state.runs, 1);
  sidecar.stop();
});

test("a redelivered command id replays the outcome and does not call the SDK", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  const frame = JSON.stringify({
    type: "command",
    name: "send_message",
    message_id: "ok-1",
    payload: { text: "once", start_new_session: true },
  });
  await sidecar.handleFrame(socket, frame);
  await sidecar.handleFrame(socket, frame);
  assert.equal(socket.sent.length, 2);
  assert.equal(socket.sent[0].name, "command_result");
  assert.equal(socket.sent[1].name, "command_result");
  assert.equal(socket.sent[1].payload.reply_text, "ok: once");
  assert.equal(state.runs, 1);
  assert.equal(state.starts.length, 1);
  sidecar.stop();
});

test("a second message to the same session id does not start another thread", async () => {
  const { sidecar, state } = makeSidecar();
  const first = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "one", start_new_session: true },
  });
  const second = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "two", target_session_id: first.session_id },
  });
  assert.equal(first.reply_text, "ok: one");
  assert.equal(second.reply_text, "ok: two");
  assert.equal(second.session_id, "thread-1");
  assert.equal(state.starts.length, 1);
  assert.equal(state.resumes.length, 0);
  assert.equal(state.runs, 2);
  sidecar.stop();
});

test("a message naming an unknown session id resumes it", async () => {
  const { sidecar, state } = makeSidecar();
  const outcome = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: {
      text: "continue",
      metadata: { session_id: "persisted-thread" },
    },
  });
  assert.equal(outcome.reply_text, "ok: continue");
  assert.equal(outcome.session_id, "persisted-thread");
  assert.equal(state.resumes.length, 1);
  assert.equal(state.resumes[0].id, "persisted-thread");
  assert.equal(state.starts.length, 0);
  sidecar.stop();
});

test("interrupt aborts a running turn", async () => {
  let notifyStarted;
  const started = new Promise((resolve) => {
    notifyStarted = resolve;
  });
  const state = { runs: 0, aborts: 0 };
  const sidecar = new PreloopCodexSidecar(undefined, () => ({
    startThread() {
      return {
        id: "thread-live",
        run(_input, options) {
          state.runs += 1;
          notifyStarted();
          return new Promise((_resolve, reject) => {
            const fail = () => {
              state.aborts += 1;
              reject(abortError());
            };
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
  }));
  sidecar.configure(baseConfig);
  const pending = sidecar.dispatch({
    type: "command",
    name: "send_message",
    message_id: "run-1",
    payload: { text: "go", start_new_session: true },
  });
  await started;
  const marker = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { interrupt: true, target_session_id: "thread-live" },
  });
  assert.equal(marker, "interrupted");
  const outcome = await pending;
  assert.equal(outcome.stopped, true);
  assert.equal(state.runs, 1);
  assert.equal(state.aborts, 1);
  sidecar.stop();
});

test("empty text is rejected", async () => {
  const { sidecar } = makeSidecar();
  await assert.rejects(
    () =>
      sidecar.dispatch({
        type: "command",
        name: "send_message",
        payload: { text: "   ", start_new_session: true },
      }),
    /non-empty text/,
  );
  sidecar.stop();
});

test("replay of a failed command resends the error and does not call the SDK", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  const failed = JSON.stringify({
    type: "command",
    name: "send_message",
    message_id: "fail-1",
    payload: { text: "   " },
  });
  await sidecar.handleFrame(socket, failed);
  await sidecar.handleFrame(socket, failed);
  assert.equal(socket.sent.length, 2);
  assert.equal(socket.sent[1].name, "command_error");
  assert.match(socket.sent[1].payload.error, /non-empty text/);
  assert.equal(state.runs, 0);
  sidecar.stop();
});

test("resolveTargetSessionId checks payload then metadata keys", () => {
  assert.equal(
    resolveTargetSessionId({ target_session_id: "a", session_reference: "b" }),
    "a",
  );
  assert.equal(
    resolveTargetSessionId({ metadata: { session_id: "from-meta" } }),
    "from-meta",
  );
  assert.equal(resolveTargetSessionId({ metadata: {} }), undefined);
});

test("resolveResumeSessionId prefers session_source_id when present", () => {
  assert.equal(
    resolveResumeSessionId({
      target_session_id: "preloop-uuid",
      session_source_id: "codex-native",
    }),
    "codex-native",
  );
});

test("controlAuthHeaders uses Authorization Bearer, not a query token", () => {
  assert.equal(controlAuthHeaders("agt_secret").Authorization, "Bearer agt_secret");
});

test("persistent_checkout runs in the prepared checkout and reports workspace_path", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "preloop-codex-ws-"));
  const state = { texts: [], runs: 0, starts: [], resumes: [] };
  const gitState = { dirty: new Set() };
  const manager = new WorkspaceManager(
    { workspace_root: root },
    fakeGit(gitState),
  );
  const sidecar = new PreloopCodexSidecar(
    undefined,
    makeEchoFactory(state),
    undefined,
    manager,
  );
  sidecar.configure({ ...baseConfig, workspace_root: root });
  const socket = fakeSocket();
  const checkout = path.join(root, "example", "repo");
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "ws-1",
      payload: {
        text: "review the change",
        start_new_session: true,
        metadata: {
          workspace: {
            mode: "persistent_checkout",
            repository_url: "https://github.com/example/repo.git",
            repository_slug: "example/repo",
            default_branch: "main",
            ref: "feature",
            sha: "a".repeat(40),
          },
        },
      },
    }),
  );
  assert.equal(state.starts.length, 1);
  assert.equal(state.starts[0].options.workingDirectory, checkout);
  assert.equal(socket.sent.length, 2);
  assert.equal(socket.sent[0].name, "command_result");
  assert.equal(socket.sent[0].payload.metadata.workspace_path, checkout);
  assert.deepEqual(socket.sent[0].payload.metadata.usage, {
    input_tokens: 2,
    output_tokens: 4,
  });
  assert.equal(socket.sent[1].type, "event");
  assert.equal(socket.sent[1].name, "session_activity");
  assert.equal(socket.sent[1].payload.workspace_path, checkout);
  assert.equal(socket.sent[1].payload.cwd, checkout);
  assert.equal(socket.sent[1].payload.runtime, "codex");
  sidecar.stop();
});

test("clone_less keeps workingDirectory on workspace_root", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "ws-clone-less",
      payload: {
        text: "review the diff",
        start_new_session: true,
        metadata: { workspace: { mode: "clone_less" } },
      },
    }),
  );
  assert.equal(state.starts.length, 1);
  assert.equal(state.starts[0].options.workingDirectory, "/tmp/workspace");
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].name, "command_result");
  assert.equal(socket.sent[0].payload.metadata.workspace_path, undefined);
  sidecar.stop();
});

test("spawn_worktree on a persistent checkout is command_error and does not run git", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "preloop-codex-ws-"));
  const state = { texts: [], runs: 0, starts: [], resumes: [] };
  const gitState = { dirty: new Set(), calls: 0 };
  const manager = new WorkspaceManager(
    { workspace_root: root },
    async (args, options) => {
      gitState.calls += 1;
      return fakeGit(gitState)(args, options);
    },
  );
  const sidecar = new PreloopCodexSidecar(
    undefined,
    makeEchoFactory(state),
    undefined,
    manager,
  );
  sidecar.configure({ ...baseConfig, workspace_root: root });
  const socket = fakeSocket();
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "ws-worktree",
      payload: {
        text: "review the change",
        start_new_session: true,
        spawn_worktree: true,
        metadata: {
          workspace: {
            mode: "persistent_checkout",
            repository_url: "https://github.com/example/repo.git",
            repository_slug: "example/repo",
            default_branch: "main",
            sha: "a".repeat(40),
          },
        },
      },
    }),
  );
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].name, "command_error");
  assert.match(socket.sent[0].payload.error, /does not create git worktrees/);
  assert.equal(state.starts.length, 0);
  assert.equal(gitState.calls, 0);
  sidecar.stop();
});

test("spawn_worktree without a persistent checkout is command_error", async () => {
  const { sidecar, state } = makeSidecar();
  const socket = fakeSocket();
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "ws-worktree-clone-less",
      payload: {
        text: "review the diff",
        start_new_session: true,
        metadata: {
          workspace: { mode: "clone_less" },
          spawn_worktree: true,
        },
      },
    }),
  );
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].name, "command_error");
  assert.match(socket.sent[0].payload.error, /does not create git worktrees/);
  assert.equal(state.starts.length, 0);
  sidecar.stop();
});

test("a dirty persistent checkout fails as command_error and does not start a thread", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "preloop-codex-ws-"));
  const checkout = path.join(root, "example", "repo");
  await fs.mkdir(path.join(checkout, ".git"), { recursive: true });
  const state = { texts: [], runs: 0, starts: [], resumes: [] };
  const manager = new WorkspaceManager(
    { workspace_root: root },
    fakeGit({ dirty: new Set([checkout]) }),
  );
  const sidecar = new PreloopCodexSidecar(
    undefined,
    makeEchoFactory(state),
    undefined,
    manager,
  );
  sidecar.configure({ ...baseConfig, workspace_root: root });
  const socket = fakeSocket();
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "ws-dirty",
      payload: {
        text: "review the change",
        start_new_session: true,
        metadata: {
          workspace: {
            mode: "persistent_checkout",
            repository_url: "https://github.com/example/repo.git",
            repository_slug: "example/repo",
            default_branch: "main",
            sha: "a".repeat(40),
          },
        },
      },
    }),
  );
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].name, "command_error");
  assert.match(socket.sent[0].payload.error, /uncommitted changes/);
  assert.equal(state.starts.length, 0);
  sidecar.stop();
});

function fakeGit(state) {
  return async (args, options) => {
    const command = gitSubcommand(args);
    if (command === "clone") {
      const dest = path.resolve(options.cwd ?? "", args[args.length - 1]);
      await fs.mkdir(path.join(dest, ".git"), { recursive: true });
      return { stdout: "", stderr: "", code: 0 };
    }
    if (command === "status") {
      const dirty = state.dirty.has(options.cwd);
      return { stdout: dirty ? " M README.md\n" : "", stderr: "", code: 0 };
    }
    if (command === "config" && args.includes("--get")) {
      return { stdout: "", stderr: "", code: 1 };
    }
    return { stdout: "", stderr: "", code: 0 };
  };
}

function gitSubcommand(args) {
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "-c" || arg === "-C") {
      index += 1;
      continue;
    }
    if (arg === "--") {
      break;
    }
    if (arg.startsWith("-")) {
      continue;
    }
    return arg;
  }
  return args[0];
}

function fakeSocket({ open = true } = {}) {
  const sent = [];
  return {
    sent,
    OPEN: 1,
    readyState: open ? 1 : 3,
    send(data) {
      sent.push(JSON.parse(data));
    },
  };
}
