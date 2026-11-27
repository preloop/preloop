// Failed and hung turns end the command; the Claude Code binary is chosen
// and logged per turn.
import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionManager } from "../dist/sessions.js";
import { PreloopClaudeSidecar } from "../dist/index.js";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  findOnPath,
  MIN_CLAUDE_CODE_VERSION,
  bundledClaudeCodeVersion,
  compareVersions,
  resolveClaudeExecutable,
} from "../dist/claude-binary.js";

const config = {
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
  workspace_root: "/tmp/workspace",
};

function errorResultFactory(state) {
  return async ({ prompt, options }) => {
    state.options = options;
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: "s-err" };
        for await (const _message of prompt) {
          yield {
            type: "result", user_message_uuids: [_message.uuid],
            subtype: "success",
            is_error: true,
            session_id: "s-err",
            result: "API Error: 400 version 2.1.251 or newer is required",
          };
        }
      },
    };
  };
}

function hangingFactory(state) {
  return async ({ prompt, options }) => {
    state.options = options;
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        yield { type: "system", subtype: "init", session_id: "s-hang" };
        // Consume the prompt but never answer, until aborted.
        for await (const _message of prompt) {
          await new Promise((resolve) => {
            options.abortController.signal.addEventListener("abort", resolve);
          });
          state.aborted = options.abortController.signal.aborted;
          throw Object.assign(new Error("aborted"), { name: "AbortError" });
        }
      },
    };
  };
}

test("an error result fails the turn with the error text", async () => {
  const state = {};
  const lines = [];
  const manager = new SessionManager(config, errorResultFactory(state), {
    log: (line) => lines.push(line),
    resolveExecutable: async () => ({ source: "bundled", version: "2.1.330" }),
  });
  await assert.rejects(
    manager.sendMessage({ text: "go" }),
    /Claude Code turn failed: API Error: 400 version 2\.1\.251/,
  );
  assert.ok(lines.some((line) => line.startsWith("turn start session=")));
  assert.ok(
    lines.some(
      (line) =>
        line.startsWith("turn error") &&
        line.includes("outcome=failed") &&
        line.includes("version=2.1.330"),
    ),
    lines.join("\n"),
  );
  manager.stop();
});

test("turn logs carry the effective model and flow execution id", async () => {
  const lines = [];
  const factory = async ({ prompt, options }) => ({
    async interrupt() {},
    async *[Symbol.asyncIterator]() {
      yield { type: "system", subtype: "init", session_id: "s-log" };
      for await (const _message of prompt) {
        assert.equal(options.model, "anthropic/claude-opus-5-5");
        yield {
          type: "result", user_message_uuids: [_message.uuid],
          subtype: "success",
          result: "ok",
          usage: { input_tokens: 3, output_tokens: 2 },
          total_cost_usd: 0.001,
        };
      }
    },
  });
  const manager = new SessionManager(config, factory, {
    log: (line) => lines.push(line),
    resolveExecutable: async () => ({ source: "bundled", version: "2.1.330" }),
  });
  const executionId = "6fdedd35-1111-2222-3333-444444444444";
  const outcome = await manager.completeTurn({
    text: "go",
    model: "anthropic/claude-opus-5-5",
    metadata: {
      source: "flow_execution",
      flow_execution_id: executionId,
    },
  });
  assert.equal(outcome.text, "ok");
  assert.equal(outcome.usage?.total_tokens, 5);
  assert.ok(
    lines.some(
      (line) =>
        line.startsWith("turn end") &&
        line.includes("outcome=completed") &&
        line.includes(`model=anthropic/claude-opus-5-5`) &&
        line.includes(`execution_id=${executionId}`),
    ),
    lines.join("\n"),
  );
  manager.stop();
});

test("error subtypes without is_error also fail the turn", async () => {
  const factory = async ({ prompt }) => ({
    async interrupt() {},
    async *[Symbol.asyncIterator]() {
      yield { type: "system", subtype: "init", session_id: "s-max" };
      for await (const _message of prompt) {
        yield { type: "result", user_message_uuids: [_message.uuid], subtype: "error_max_turns", errors: ["max turns"] };
      }
    },
  });
  const manager = new SessionManager(config, factory, { log: () => {} });
  await assert.rejects(manager.sendMessage({ text: "go" }), /max turns/);
  manager.stop();
});

test("a hung turn times out and aborts the Claude Code child", async () => {
  const state = {};
  const lines = [];
  const manager = new SessionManager(
    { ...config, turn_timeout_ms: 30 },
    hangingFactory(state),
    { log: (line) => lines.push(line) },
  );
  await assert.rejects(manager.sendMessage({ text: "go" }), /timed out after 30ms/);
  assert.equal(state.options.abortController.signal.aborted, true);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(state.aborted, true);
  assert.deepEqual(manager.ownedSessionIds(), []);
  assert.ok(lines.some((line) => line.includes("outcome=timeout")), lines.join("\n"));
  manager.stop();
});

test("sidecar reports a failed first turn as command_error", async () => {
  const state = {};
  const sidecar = new PreloopClaudeSidecar(undefined, errorResultFactory(state));
  sidecar.configure({
    ...config,
    enabled: true,
    protocol: "preloop.agent_control.v1",
    observer_enabled: false,
  });
  const sent = [];
  const socket = { readyState: 1, OPEN: 1, send: (data) => sent.push(JSON.parse(data)) };
  await sidecar.handleFrame(
    socket,
    JSON.stringify({
      type: "command",
      name: "send_message",
      message_id: "m-err",
      payload: { text: "go" },
    }),
  );
  const frame = sent.find((item) => item.message_id === "m-err");
  assert.equal(frame?.name, "command_error", JSON.stringify(sent));
  assert.equal(frame.payload.status, "failed");
  assert.match(frame.payload.error, /API Error: 400/);
  sidecar.stop();
});

test("claude_executable is passed as pathToClaudeCodeExecutable", async () => {
  const state = {};
  const factory = async ({ prompt, options }) => {
    state.options = options;
    return {
      async interrupt() {},
      async *[Symbol.asyncIterator]() {
        for await (const _message of prompt) {
          yield { type: "result", user_message_uuids: [_message.uuid], subtype: "success", result: "ok" };
        }
      },
    };
  };
  const manager = new SessionManager(
    { ...config, claude_executable: "/opt/claude/bin/claude" },
    factory,
    { log: () => {} },
  );
  assert.equal(await manager.sendMessage({ text: "go" }), "ok");
  assert.equal(state.options.pathToClaudeCodeExecutable, "/opt/claude/bin/claude");
  manager.stop();
});

test("resolveClaudeExecutable prefers config, then a newer PATH claude", async () => {
  const probe = async (exe) => (exe === "/usr/local/bin/claude" ? "2.1.400" : exe === "/opt/claude" ? "2.1.330" : "2.1.100");
  assert.deepEqual(
    await resolveClaudeExecutable(
      { claude_executable: "/opt/claude" },
      {
        probe,
        bundledVersion: () => "2.1.330",
        findClaude: () => "/usr/local/bin/claude",
        isExecutable: () => true,
      },
    ),
    {
      path: "/opt/claude",
      version: "2.1.330",
      source: "config",
      warning: undefined,
    },
  );
  await assert.rejects(
    resolveClaudeExecutable(
      { claude_executable: "/opt/missing/claude" },
      { probe, isExecutable: () => false },
    ),
    /not an absolute path to an executable file/,
  );
  await assert.rejects(
    resolveClaudeExecutable({ claude_executable: "claude" }, { probe, isExecutable: () => true }),
    /not an absolute path/,
  );
  assert.deepEqual(
    await resolveClaudeExecutable(
      {},
      { probe, bundledVersion: () => "2.1.330", findClaude: () => "/usr/local/bin/claude" },
    ),
    { path: "/usr/local/bin/claude", version: "2.1.400", source: "path" },
  );
  assert.deepEqual(
    await resolveClaudeExecutable(
      {},
      { probe, bundledVersion: () => "2.1.330", findClaude: () => "/usr/bin/claude" },
    ),
    { version: "2.1.330", source: "bundled", warning: undefined },
  );
  assert.deepEqual(
    await resolveClaudeExecutable(
      {},
      { probe, bundledVersion: () => "2.1.330", findClaude: () => undefined },
    ),
    { version: "2.1.330", source: "bundled", warning: undefined },
  );
});

test("compareVersions orders dotted versions numerically", () => {
  assert.equal(compareVersions("2.1.251", "2.1.96"), 1);
  assert.equal(compareVersions("2.1.96", "2.1.251"), -1);
  assert.equal(compareVersions("2.1", "2.1.0"), 0);
});

test("bundled Claude Code meets the upstream minimum version", () => {
  const bundled = bundledClaudeCodeVersion();
  assert.ok(bundled, "the Agent SDK should declare claudeCodeVersion");
  assert.ok(
    compareVersions(bundled, MIN_CLAUDE_CODE_VERSION) >= 0,
    `bundled Claude Code ${bundled} is older than ${MIN_CLAUDE_CODE_VERSION}; bump @anthropic-ai/claude-agent-sdk`,
  );
});

test("findOnPath honours PATHEXT on Windows and X_OK elsewhere", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "claude-bin-"));
  fs.writeFileSync(path.join(dir, "claude.cmd"), "@echo off\r\n");
  if (process.platform === "win32") {
    assert.match(findOnPath("claude", dir) ?? "", /claude\.cmd$/i);
  } else {
    assert.equal(findOnPath("claude", dir), undefined);
    const exe = path.join(dir, "claude");
    fs.writeFileSync(exe, "#!/bin/sh\n", { mode: 0o755 });
    assert.equal(findOnPath("claude", `relative:${dir}`), exe);
    fs.chmodSync(exe, 0o644);
    assert.equal(findOnPath("claude", dir), undefined);
  }
});

test("a bad claude_executable fails the turn with its reason", async () => {
  const factory = async () => {
    throw new Error("factory must not run");
  };
  const manager = new SessionManager(config, factory, {
    log: () => {},
    resolveExecutable: async () => {
      throw new Error("claude_executable /nope is not an absolute path to an executable file");
    },
  });
  await assert.rejects(manager.sendMessage({ text: "go" }), /claude_executable \/nope/);
  manager.stop();
});


test("rejects configured binaries without verified prompt UUID support", async () => {
  for (const version of ["2.1.251", undefined]) {
    await assert.rejects(resolveClaudeExecutable(
      { claude_executable: "/opt/claude" },
      { probe: async () => version, isExecutable: () => true, bundledVersion: () => "2.1.330" },
    ), /update Claude Code.*2\.1\.330/i);
  }
});
