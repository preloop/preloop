// Sidecar observability: the log must never stay empty across a real run.
// Startup, config source, and WS connect attempts/results are all logged
// (never the bearer token), and a config with no usable settings warns
// loudly instead of idling silently. The WS side runs against a loopback
// fake server; no live network.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import path from "node:path";

import { WebSocketServer } from "ws";

import { PreloopClaudeSidecar } from "../dist/index.js";

const TOKEN = "agt_fake_token_for_tests";

function writeTempConfig(value) {
  const dir = fs.mkdtempSync("/tmp/plc-");
  const file = path.join(dir, "preloop-control.json");
  fs.writeFileSync(file, JSON.stringify(value));
  return { dir, file };
}

function baseConfig(overrides = {}) {
  return {
    enabled: true,
    protocol: "preloop.agent_control.v1",
    runtime: "claude_code",
    control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
    bearer_token: TOKEN,
    runtime_principal_id: "claude-code-1",
    observer_enabled: false,
    ...overrides,
  };
}

test("verify logs the config path and nested schema source", () => {
  const { file } = writeTempConfig({ control: baseConfig() });
  const lines = [];
  const sidecar = new PreloopClaudeSidecar(file);
  sidecar.setLogger((message) => lines.push(message));
  sidecar.verify();
  const joined = lines.join("\n");
  assert.match(joined, /config: .*preloop-control\.json/);
  assert.match(joined, /nested "control" block/);
  assert.ok(!joined.includes(TOKEN), "log must never contain the token");
});

test("verify logs the flat schema source", () => {
  const { file } = writeTempConfig(baseConfig());
  const lines = [];
  const sidecar = new PreloopClaudeSidecar(file);
  sidecar.setLogger((message) => lines.push(message));
  sidecar.verify();
  assert.match(lines.join("\n"), /flat schema/);
});

test("a config with no usable settings warns loudly on logger and stderr", () => {
  const { file } = writeTempConfig({ unrelated: true });
  const lines = [];
  const stderrLines = [];
  const originalError = console.error;
  console.error = (message) => stderrLines.push(String(message));
  try {
    const sidecar = new PreloopClaudeSidecar(file);
    sidecar.setLogger((message) => lines.push(message));
    assert.throws(() => sidecar.verify(), /Expected runtime/);
  } finally {
    console.error = originalError;
  }
  const joined = lines.join("\n");
  assert.match(joined, /no usable control settings/);
  assert.match(joined, /preloop agents onboard/);
  assert.match(stderrLines.join("\n"), /no usable control settings/);
});

test("start logs startup, socket listen, and WS connect results against a fake server", async () => {
  // Keep the launcher IPC socket out of the real HOME.
  const scratchHome = fs.mkdtempSync("/tmp/plc-home-");
  const originalHome = process.env.HOME;
  process.env.HOME = scratchHome;

  const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise((resolve) => server.on("listening", resolve));
  const port = server.address().port;
  const firstFrame = new Promise((resolve) => {
    server.on("connection", (socket) => {
      socket.on("message", (data) => resolve(JSON.parse(String(data))));
    });
  });

  const { file } = writeTempConfig({
    control: baseConfig({
      control_ws_url: `ws://127.0.0.1:${port}/control`,
    }),
  });
  const lines = [];
  const sidecar = new PreloopClaudeSidecar(file, async () => {
    throw new Error("no sessions in this test");
  });
  sidecar.setLogger((message) => lines.push(message));
  try {
    await sidecar.start();
    const frame = await firstFrame;
    assert.equal(frame.type, "presence");
    assert.equal(frame.name, "capabilities");

    const deadline = Date.now() + 5000;
    while (
      !lines.some((line) => line.includes("Agent Control: connected")) &&
      Date.now() < deadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const joined = lines.join("\n");
    assert.match(joined, /sidecar starting \(pid \d+/);
    assert.match(joined, /nested "control" block/);
    assert.match(joined, /launcher control socket listening/);
    assert.match(joined, new RegExp(`connecting to ws://127\\.0\\.0\\.1:${port}/control`));
    assert.match(joined, /Agent Control: connected/);
    assert.ok(!joined.includes(TOKEN), "log must never contain the token");
  } finally {
    sidecar.stop();
    server.close();
    process.env.HOME = originalHome;
  }
});

test("a refused WS connection logs the failure and schedules a reconnect", async () => {
  const scratchHome = fs.mkdtempSync("/tmp/plc-home-");
  const originalHome = process.env.HOME;
  process.env.HOME = scratchHome;

  // Grab a port with nothing listening on it.
  const probe = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise((resolve) => probe.on("listening", resolve));
  const deadPort = probe.address().port;
  await new Promise((resolve) => probe.close(resolve));

  const { file } = writeTempConfig({
    control: baseConfig({
      control_ws_url: `ws://127.0.0.1:${deadPort}/control`,
    }),
  });
  const lines = [];
  const sidecar = new PreloopClaudeSidecar(file, async () => {
    throw new Error("no sessions in this test");
  });
  sidecar.setLogger((message) => lines.push(message));
  try {
    await sidecar.start();
    const deadline = Date.now() + 5000;
    while (
      !lines.some((line) => line.includes("reconnecting in")) &&
      Date.now() < deadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const joined = lines.join("\n");
    assert.match(joined, /websocket error: .*ECONNREFUSED/);
    assert.match(joined, /connection closed/);
    assert.match(joined, /reconnecting in \d+ms/);
  } finally {
    sidecar.stop();
    process.env.HOME = originalHome;
  }
});
