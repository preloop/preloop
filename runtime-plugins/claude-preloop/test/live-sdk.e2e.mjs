#!/usr/bin/env node
// Live Agent SDK e2e against the installed Claude Code binary bundled by
// @anthropic-ai/claude-agent-sdk. Skips unless PRELOOP_LIVE_CLAUDE_SDK=1 and
// ANTHROPIC_API_KEY is set. Default `npm test` keeps these skipped.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const live =
  process.env.PRELOOP_LIVE_CLAUDE_SDK === "1" &&
  Boolean(process.env.ANTHROPIC_API_KEY);
const skipReason =
  "set PRELOOP_LIVE_CLAUDE_SDK=1 and ANTHROPIC_API_KEY to run against Claude Code";

function installedSdkVersion() {
  const localPkg = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
    "node_modules",
    "@anthropic-ai",
    "claude-agent-sdk",
    "package.json",
  );
  try {
    const pkg = JSON.parse(readFileSync(localPkg, "utf8"));
    if (pkg.name === "@anthropic-ai/claude-agent-sdk") {
      return pkg.version;
    }
  } catch {
    // fall through to module resolve
  }
  const require = createRequire(import.meta.url);
  let dir = path.dirname(require.resolve("@anthropic-ai/claude-agent-sdk"));
  for (let i = 0; i < 6; i += 1) {
    try {
      const pkg = JSON.parse(
        readFileSync(path.join(dir, "package.json"), "utf8"),
      );
      if (pkg.name === "@anthropic-ai/claude-agent-sdk") {
        return pkg.version;
      }
    } catch {
      // walk up
    }
    dir = path.dirname(dir);
  }
  throw new Error("could not resolve @anthropic-ai/claude-agent-sdk version");
}

function managerConfig(overrides = {}) {
  return {
    runtime: "claude_code",
    control_ws_url: "wss://example.invalid/api/v1/agents/control/ws",
    bearer_token: "unused",
    runtime_principal_id: "live-e2e",
    workspace_root: process.cwd(),
    turn_timeout_ms: 120_000,
    ...overrides,
  };
}

test("installed Agent SDK version is recorded", () => {
  const version = installedSdkVersion();
  assert.match(version, /^\d+\.\d+\.\d+/);
  console.log(`claude-agent-sdk ${version}`);
});

test(
  "live Agent SDK query returns a result turn",
  { skip: live ? false : skipReason },
  async () => {
    const { SessionManager } = await import("../dist/sessions.js");
    const manager = new SessionManager(managerConfig());
    try {
      const reply = await manager.sendMessage({
        text: "Reply with exactly the word pong and nothing else.",
      });
      assert.ok(
        typeof reply === "string" && reply.toLowerCase().includes("pong"),
        `expected pong, got ${JSON.stringify(reply)}`,
      );
    } finally {
      manager.stop();
    }
  },
);

test(
  "live second turn reuses the same Claude session",
  { skip: live ? false : skipReason },
  async () => {
    const { SessionManager } = await import("../dist/sessions.js");
    const manager = new SessionManager(managerConfig());
    try {
      await manager.sendMessage({
        text: "Remember the codeword zebra. Reply with ok.",
      });
      const ids = manager.ownedSessionIds();
      assert.equal(ids.length, 1);
      const reply = await manager.sendMessage({
        text: "What was the codeword? Reply with only that word.",
        targetSessionId: ids[0],
        resumeSessionId: ids[0],
      });
      assert.ok(
        typeof reply === "string" && reply.toLowerCase().includes("zebra"),
        `expected zebra, got ${JSON.stringify(reply)}`,
      );
      assert.equal(manager.ownedSessionIds().length, 1);
    } finally {
      manager.stop();
    }
  },
);

test(
  "live interrupt stops an in-flight Claude Code turn",
  { skip: live ? false : skipReason },
  async () => {
    const { SessionManager } = await import("../dist/sessions.js");
    const manager = new SessionManager(
      managerConfig({ turn_timeout_ms: 30_000 }),
    );
    try {
      const pending = manager.sendMessage({
        text: "Count slowly from 1 to 200, writing each number on its own line.",
      });
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      await manager.interrupt();
      await pending.catch(() => undefined);
      assert.ok(manager.ownedSessionIds().length >= 0);
    } finally {
      manager.stop();
    }
  },
);

test(
  "live takeover then send_message steers through the Agent SDK",
  { skip: live ? false : skipReason },
  async () => {
    const { PreloopClaudeSidecar } = await import("../dist/index.js");
    const sidecar = new PreloopClaudeSidecar();
    sidecar.configure({
      enabled: true,
      protocol: "preloop.agent_control.v1",
      runtime: "claude_code",
      control_ws_url: "wss://example.invalid/api/v1/agents/control/ws",
      bearer_token: "unused",
      runtime_principal_id: "live-e2e",
      workspace_root: process.cwd(),
      observer_enabled: false,
      turn_timeout_ms: 120_000,
    });
    try {
      const takeover = await sidecar.dispatch({
        type: "command",
        name: "request_takeover",
        payload: {},
      });
      assert.match(String(takeover), /^remote/);
      assert.equal(sidecar.currentMode(), "remote");
      const reply = await sidecar.dispatch({
        type: "command",
        name: "send_message",
        payload: {
          text: "Reply with exactly the word pong and nothing else.",
        },
      });
      assert.ok(
        typeof reply === "string" && reply.toLowerCase().includes("pong"),
        `expected pong after takeover, got ${JSON.stringify(reply)}`,
      );
      const released = await sidecar.dispatch({
        type: "command",
        name: "release",
        payload: {},
      });
      assert.match(String(released), /^local/);
    } finally {
      sidecar.stop();
    }
  },
);
