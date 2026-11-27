import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs/promises";
import { WebSocketServer } from "ws";
import { PreloopCodexSidecar } from "../dist/index.js";

test("announces capabilities before a blocked checkout scan", async (t) => {
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  t.mock.method(fs, "realpath", () => blocked);
  const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise(resolve => server.on("listening", resolve));
  const frames = [];
  const connected = new Promise(resolve => server.on("connection", socket => {
    socket.on("message", data => frames.push(JSON.parse(String(data))));
    resolve();
  }));
  const sidecar = new PreloopCodexSidecar();
  sidecar.configure({
    enabled: true, protocol: "preloop.agent_control.v1", runtime: "codex",
    bearer_token: "example-token", runtime_principal_id: "example-host",
    control_ws_url: `ws://127.0.0.1:${server.address().port}/control`,
    observer_enabled: false, workspace_root: "/example/checkout",
  });
  try {
    sidecar.connect();
    await connected;
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(frames[0]?.name, "capabilities");
    assert.equal(frames[0].payload.capabilities.followup_prompts, true);
  } finally {
    sidecar.stop();
    release("/example/checkout");
    for (const socket of server.clients) socket.terminate();
    await new Promise(resolve => server.close(resolve));
  }
});
