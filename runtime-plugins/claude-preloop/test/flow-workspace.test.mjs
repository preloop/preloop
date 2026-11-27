// Per-flow workspace, model, and turn timeout on the persistent path.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  PreloopClaudeSidecar,
  flowModel,
  resolveFlowWorkspace,
  selectTurnTimeoutMs,
} from "../dist/index.js";

function tempRoot() {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "flow-ws-")));
}

function setup({ entries, dirty = false, gitCode = 0 } = {}) {
  const root = tempRoot();
  const home = path.join(root, "home");
  const project = path.join(home, "code", "project");
  fs.mkdirSync(project, { recursive: true });
  const runnerPath = path.join(root, "runner.json");
  const list = entries ?? [
    { id: "dir_project", path: project, mode: "write", harnesses: "all" },
  ];
  fs.writeFileSync(runnerPath, JSON.stringify({ authorized_directories: list }));
  const calls = [];
  const git = async (args, options) => {
    calls.push({ args, cwd: options.cwd });
    return { stdout: dirty ? " M file.txt\n" : "", stderr: "", code: gitCode };
  };
  const config = { runner_config_path: runnerPath };
  return { root, home, project, runnerPath, git, calls, config };
}

test("authorized directory id resolves to its realpath", async () => {
  const s = setup();
  const dir = await resolveFlowWorkspace(
    { mode: "authorized_directory", kind: "authorized_directory", id: "dir_project" },
    s.config,
    { git: s.git, homeDir: s.home },
  );
  assert.equal(dir, s.project);
  assert.deepEqual(s.calls[0].args, ["status", "--porcelain", "--untracked-files=normal"]);
});

test("a listed path resolves; an unlisted path is refused", async () => {
  const s = setup();
  const dir = await resolveFlowWorkspace(
    { kind: "path", path: s.project },
    s.config,
    { git: s.git, homeDir: s.home },
  );
  assert.equal(dir, s.project);
  const other = path.join(s.home, "code", "other");
  fs.mkdirSync(other);
  await assert.rejects(
    resolveFlowWorkspace({ kind: "path", path: other }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /not an authorized directory/,
  );
});

test("unknown id and missing runner config are refused", async () => {
  const s = setup();
  await assert.rejects(
    resolveFlowWorkspace({ kind: "authorized_directory", id: "dir_nope" }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /not an authorized directory/,
  );
  await assert.rejects(
    resolveFlowWorkspace(
      { kind: "authorized_directory", id: "dir_project" },
      { runner_config_path: path.join(s.root, "missing.json") },
      { git: s.git, homeDir: s.home },
    ),
    /runner dirs add/,
  );
  await assert.rejects(
    resolveFlowWorkspace({ kind: "authorized_directory", id: "../x" }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /id is invalid/,
  );
});

test("dirty tree is refused unless allow_dirty", async () => {
  const s = setup({ dirty: true });
  await assert.rejects(
    resolveFlowWorkspace({ kind: "authorized_directory", id: "dir_project" }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /uncommitted changes/,
  );
  const dir = await resolveFlowWorkspace(
    { kind: "authorized_directory", id: "dir_project", allow_dirty: true },
    s.config,
    { git: s.git, homeDir: s.home },
  );
  assert.equal(dir, s.project);
});

test("git failure inside a repository fails closed", async () => {
  const s = setup({ gitCode: 128 });
  fs.mkdirSync(path.join(s.project, ".git"));
  await assert.rejects(
    resolveFlowWorkspace({ kind: "authorized_directory", id: "dir_project" }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /cannot be ruled out/,
  );
});

test("home, root, credential dirs, read_only and other harnesses are refused", async () => {
  const base = setup();
  const claudeDir = path.join(base.home, ".claude");
  fs.mkdirSync(claudeDir);
  const cases = [
    [{ id: "dir_home", path: base.home, mode: "write" }, /home directory/],
    [{ id: "dir_root", path: "/", mode: "write" }, /filesystem root/],
    [{ id: "dir_claude", path: claudeDir, mode: "write" }, /holds credentials/],
    [{ id: "dir_ro", path: base.project, mode: "read_only" }, /not writable/],
    [
      { id: "dir_cop", path: base.project, mode: "write", harnesses: ["copilot_cli"] },
      /does not allow harness/,
    ],
  ];
  for (const [entry, pattern] of cases) {
    fs.writeFileSync(
      base.runnerPath,
      JSON.stringify({ authorized_directories: [entry] }),
    );
    await assert.rejects(
      resolveFlowWorkspace({ kind: "authorized_directory", id: entry.id }, base.config, {
        git: base.git,
        homeDir: base.home,
      }),
      pattern,
    );
  }
  fs.writeFileSync(
    base.runnerPath,
    JSON.stringify({
      authorized_directories: [
        { id: "dir_ok", path: base.project, mode: "write", harnesses: ["claude_code"] },
      ],
    }),
  );
  assert.equal(
    await resolveFlowWorkspace({ kind: "authorized_directory", id: "dir_ok" }, base.config, {
      git: base.git,
      homeDir: base.home,
    }),
    base.project,
  );
});

test("an authorized path replaced by a symlink is refused", async () => {
  const s = setup();
  const elsewhere = path.join(s.root, "elsewhere");
  fs.mkdirSync(elsewhere);
  fs.rmSync(s.project, { recursive: true });
  fs.symlinkSync(elsewhere, s.project);
  await assert.rejects(
    resolveFlowWorkspace({ kind: "authorized_directory", id: "dir_project" }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /now resolves to/,
  );
  assert.equal(s.calls.length, 0);
});

test("a duplicated id is refused", async () => {
  const base = setup();
  const entry = { id: "dir_project", path: base.project, mode: "write" };
  const s = setup({ entries: [entry, { ...entry }] });
  await assert.rejects(
    resolveFlowWorkspace({ kind: "authorized_directory", id: "dir_project" }, s.config, {
      git: s.git,
      homeDir: s.home,
    }),
    /more than once/,
  );
});

test("turn timeout follows the flow, bounded by turn_timeout_ms", () => {
  const def = 300_000;
  assert.equal(selectTurnTimeoutMs({}, undefined, def), def);
  assert.equal(selectTurnTimeoutMs({ turn_timeout_ms: 60_000 }, {}, def), 60_000);
  assert.equal(
    selectTurnTimeoutMs({}, { run_limits: { max_duration_seconds: 3600 } }, def),
    3_600_000,
  );
  assert.equal(selectTurnTimeoutMs({}, { timeout_seconds: 1800 }, def), 1_800_000);
  assert.equal(
    selectTurnTimeoutMs(
      { turn_timeout_ms: 600_000 },
      { run_limits: { max_duration_seconds: 3600 } },
      def,
    ),
    600_000,
  );
  assert.equal(
    selectTurnTimeoutMs({}, { run_limits: { max_duration_seconds: 10 ** 9 } }, def),
    24 * 60 * 60 * 1000,
  );
  assert.equal(
    selectTurnTimeoutMs({}, { run_limits: { max_duration_seconds: null } }, def),
    def,
  );
});

test("flowModel accepts model names and drops malformed values", () => {
  assert.equal(flowModel({ model: "claude-opus-5-5" }), "claude-opus-5-5");
  assert.equal(flowModel({ model: "anthropic/claude-opus-5-5" }), "anthropic/claude-opus-5-5");
  // gateway.model is not a fallback: that would bypass the Claude-only guard.
  assert.equal(
    flowModel({ gateway: { model: "openai/gpt-5.4" } }),
    undefined,
  );
  assert.equal(flowModel({ model: "--dangerous flag" }), undefined);
  assert.equal(flowModel({ model: 5 }), undefined);
  assert.equal(flowModel(undefined), undefined);
});

const baseConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
  observer_enabled: false,
  workspace_root: "/tmp/default-root",
};

function makeSidecar(config) {
  const opened = [];
  const factory = async ({ prompt, options }) => {
    opened.push(options);
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: "s-1" };
        for await (const message of prompt) {
          yield { type: "result", user_message_uuids: [message.uuid], subtype: "success", result: `ok: ${message.message.content}` };
        }
      },
    };
  };
  const sidecar = new PreloopClaudeSidecar(undefined, factory);
  sidecar.configure({ ...baseConfig, ...config });
  return { sidecar, opened };
}

test("dispatch runs in the authorized directory with the flow model", async () => {
  const s = setup();
  const { sidecar, opened } = makeSidecar(s.config);
  sidecar.flowWorkspaceGit = s.git;
  // homeDir is the real home here; the project lives under a temp dir.
  const reply = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    message_id: "m-1",
    payload: {
      text: "nightly",
      metadata: {
        model: "claude-opus-5-5",
        workspace: {
          mode: "authorized_directory",
          kind: "authorized_directory",
          id: "dir_project",
        },
      },
    },
  });
  assert.equal(reply, "ok: nightly");
  assert.equal(opened[0].cwd, s.project);
  assert.equal(opened[0].model, "claude-opus-5-5");
  sidecar.stop();
});

test("dispatch ignores a raw metadata cwd and omits model when absent", async () => {
  const { sidecar, opened } = makeSidecar({});
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "hi", cwd: "/etc", metadata: { cwd: "/etc" } },
  });
  assert.equal(opened[0].cwd, "/tmp/default-root");
  assert.equal("model" in opened[0], false);
  sidecar.stop();
});

test("dispatch refuses an unauthorized flow directory", async () => {
  const s = setup();
  const { sidecar, opened } = makeSidecar(s.config);
  sidecar.flowWorkspaceGit = s.git;
  await assert.rejects(
    sidecar.dispatch({
      type: "command",
      name: "send_message",
      payload: {
        text: "hi",
        metadata: { workspace: { mode: "authorized_directory", kind: "path", path: "/etc" } },
      },
    }),
    /not an authorized directory/,
  );
  assert.equal(opened.length, 0);
  sidecar.stop();
});

test("a flow session exports PRELOOP_FLOW_EXECUTION_ID; others do not", async () => {
  const { sidecar, opened } = makeSidecar({});
  const executionId = "8a1f2c3d-1111-4222-8333-944455556666";
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: {
      text: "nightly",
      metadata: { source: "flow_execution", flow_execution_id: executionId },
    },
  });
  assert.equal(opened[0].env.PRELOOP_FLOW_EXECUTION_ID, executionId);
  sidecar.stop();

  const other = makeSidecar({});
  process.env.PRELOOP_FLOW_EXECUTION_ID = executionId;
  try {
    await other.sidecar.dispatch({
      type: "command",
      name: "send_message",
      payload: {
        text: "hi",
        metadata: { source: "operator", flow_execution_id: executionId },
      },
    });
  } finally {
    delete process.env.PRELOOP_FLOW_EXECUTION_ID;
  }
  assert.equal("PRELOOP_FLOW_EXECUTION_ID" in other.opened[0].env, false);
  other.sidecar.stop();

  const bad = makeSidecar({});
  await bad.sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: {
      text: "hi",
      metadata: { source: "flow_execution", flow_execution_id: "x; rm -rf /" },
    },
  });
  assert.equal("PRELOOP_FLOW_EXECUTION_ID" in bad.opened[0].env, false);
  bad.sidecar.stop();
});
