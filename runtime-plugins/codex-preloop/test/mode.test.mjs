// Takeover / release, ported from claude-preloop/test/mode.test.mjs, plus
// the launcher socket at ~/.preloop/codex-control.sock.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import { PreloopCodexSidecar } from "../dist/index.js";
import { LauncherBridge, defaultSocketPath } from "../dist/mode.js";

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

function makeEchoFactory(state) {
  return () => ({
    startThread() {
      return {
        id: "thread-1",
        async run(input) {
          state.texts.push(input);
          return { finalResponse: `ok: ${input}`, usage: null };
        },
      };
    },
    resumeThread(id) {
      return {
        id,
        async run(input) {
          state.texts.push(input);
          return { finalResponse: `ok: ${input}`, usage: null };
        },
      };
    },
  });
}

function makeSidecar() {
  const state = { texts: [] };
  const sidecar = new PreloopCodexSidecar(undefined, makeEchoFactory(state));
  sidecar.configure(baseConfig);
  return { sidecar, state };
}

test("default socket path is the Codex control socket", () => {
  assert.equal(path.basename(defaultSocketPath()), "codex-control.sock");
});

test("request_takeover marks the session remote", async () => {
  const { sidecar } = makeSidecar();
  const result = await sidecar.dispatch({
    type: "command",
    name: "request_takeover",
    payload: { session_source_id: "codex-native" },
  });
  assert.equal(result, "remote:codex-native");
  assert.equal(sidecar.currentMode(), "remote");
  sidecar.stop();
});

test("release after a remote turn returns local", async () => {
  const { sidecar } = makeSidecar();
  await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "hello from phone", start_new_session: true },
  });
  const result = await sidecar.dispatch({
    type: "command",
    name: "release",
    payload: { session_source_id: "thread-1" },
  });
  assert.match(String(result), /^local:/);
  sidecar.stop();
});

test("send_message still delivers after takeover", async () => {
  const { sidecar, state } = makeSidecar();
  await sidecar.dispatch({
    type: "command",
    name: "request_takeover",
    payload: {},
  });
  const reply = await sidecar.dispatch({
    type: "command",
    name: "send_message",
    payload: { text: "ship it", start_new_session: true },
  });
  assert.equal(reply.reply_text, "ok: ship it");
  assert.deepEqual(state.texts, ["ship it"]);
  sidecar.stop();
});

test("launcher hello, switch, and switched round-trip on the Codex socket", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-mode-"));
  const bridge = new LauncherBridge(
    path.join(home, ".preloop", "codex-control.sock"),
  );
  await bridge.listen();
  const client = net.createConnection(bridgePath(home));
  const lines = [];
  let buffer = "";
  client.setEncoding("utf8");
  client.on("data", (chunk) => {
    buffer += chunk;
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      lines.push(buffer.slice(0, newline));
      buffer = buffer.slice(newline + 1);
      newline = buffer.indexOf("\n");
    }
  });
  await new Promise((resolve) => client.once("connect", resolve));
  client.write(
    `${JSON.stringify({ type: "hello", session_id: "local-1", cwd: "/tmp/workspace" })}\n`,
  );
  await waitFor(() => bridge.mode === "local");
  assert.equal(bridge.lastSessionId, "local-1");
  assert.equal(bridge.requestSwitch(), true);
  await waitFor(() => lines.some((line) => line.includes('"switch"')));
  client.write(`${JSON.stringify({ type: "switched" })}\n`);
  await waitFor(() => bridge.mode === "remote");
  client.end();
  bridge.stop();
});

test("send_message errors when a local launcher does not switch", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "codex-mode-timeout-"));
  const socketPath = path.join(home, "codex-control.sock");
  const state = { texts: [] };
  const sidecar = new PreloopCodexSidecar(
    undefined,
    makeEchoFactory(state),
    socketPath,
  );
  sidecar.configure(baseConfig);
  try {
    await sidecar.start();
    const client = net.createConnection(socketPath);
    await new Promise((resolve, reject) => {
      client.once("connect", resolve);
      client.once("error", reject);
    });
    client.write(`${JSON.stringify({ type: "hello", session_id: "local-9" })}\n`);
    await waitFor(() => sidecar.currentMode() === "local");
    const started = Date.now();
    await assert.rejects(
      () =>
        sidecar.dispatch({
          type: "command",
          name: "send_message",
          payload: { text: "from the phone", start_new_session: true },
        }),
      /timed out waiting for launcher switch/,
    );
    assert.ok(Date.now() - started >= 7000);
    client.end();
  } finally {
    sidecar.stop();
  }
});

function bridgePath(home) {
  return path.join(home, ".preloop", "codex-control.sock");
}

function waitFor(predicate) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (predicate()) {
        resolve();
        return;
      }
      if (Date.now() - started > 2000) {
        reject(new Error("timed out"));
        return;
      }
      setTimeout(tick, 20);
    };
    tick();
  });
}
