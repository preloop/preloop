// Unit tests for gateway model-list refresh (models.ts).
// Run: npm run build && node --test test/models.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  PreloopOpenClawPlugin,
  gatewayModelsUrl,
  fetchGatewayModels,
} from "../dist/index.js";

const baseConfig = {
  runtime: "openclaw",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "secret-token",
  runtime_principal_id: "principal-1",
};

function jsonResponse(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

// ---------------------------------------------------------------------------
// gatewayModelsUrl
// ---------------------------------------------------------------------------

test("gatewayModelsUrl derives https from a wss control URL", () => {
  assert.equal(
    gatewayModelsUrl(baseConfig),
    "https://example.preloop.ai/openai/v1/models",
  );
});

test("gatewayModelsUrl derives http from a ws control URL", () => {
  assert.equal(
    gatewayModelsUrl({
      control_ws_url: "ws://localhost:8000/api/v1/agents/control/ws",
    }),
    "http://localhost:8000/openai/v1/models",
  );
});

// ---------------------------------------------------------------------------
// fetchGatewayModels
// ---------------------------------------------------------------------------

test("fetchGatewayModels returns the data array", async () => {
  const models = [
    { id: "claude-sonnet-4", object: "model", created: 1, owned_by: "preloop" },
  ];
  const result = await fetchGatewayModels(baseConfig, () =>
    jsonResponse(200, { object: "list", data: models }),
  );
  assert.deepEqual(result, models);
});

test("fetchGatewayModels throws on HTTP error", async () => {
  await assert.rejects(
    fetchGatewayModels(baseConfig, () => jsonResponse(403, {})),
    /HTTP 403/,
  );
});

// ---------------------------------------------------------------------------
// Plugin: refreshGatewayModels
// ---------------------------------------------------------------------------

test("refreshGatewayModels stores fetched models on the plugin instance", async () => {
  const models = [
    { id: "model-a", object: "model" },
    { id: "model-b", object: "model" },
  ];
  const plugin = new PreloopOpenClawPlugin(undefined, () =>
    jsonResponse(200, { data: models }),
  );
  plugin.configure(baseConfig);
  const result = await plugin.refreshGatewayModels();
  assert.deepEqual(result, models);
  assert.deepEqual(plugin.lastGatewayModels, models);
});

test("refreshGatewayModels returns empty on fetch failure", async () => {
  const plugin = new PreloopOpenClawPlugin(undefined, () =>
    Promise.reject(new Error("offline")),
  );
  plugin.configure(baseConfig);
  const result = await plugin.refreshGatewayModels();
  assert.deepEqual(result, []);
  assert.deepEqual(plugin.lastGatewayModels, []);
});

// ---------------------------------------------------------------------------
// register() wires model refresh to gateway_start
// ---------------------------------------------------------------------------

test("register fetches the gateway model list on gateway_start", async () => {
  let gatewayStartHandler;
  const api = {
    pluginConfig: baseConfig,
    on: (name, handler) => {
      if (name === "gateway_start") {
        gatewayStartHandler = handler;
      }
    },
    logger: { info: () => {}, warn: () => {}, error: () => {} },
  };

  const { register } = await import("../dist/index.js");
  register(api);
  assert.equal(typeof gatewayStartHandler, "function");

  // register() builds its own plugin instance, so observe the refresh
  // through global fetch rather than reaching into the instance.
  const realFetch = globalThis.fetch;
  const requested = [];
  globalThis.fetch = (url) => {
    requested.push(String(url));
    return jsonResponse(200, { data: [{ id: "model-a", object: "model" }] });
  };
  try {
    gatewayStartHandler();
    // Let the best-effort refresh promise settle.
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(
      requested.includes("https://example.preloop.ai/openai/v1/models"),
      `expected a gateway models fetch, saw ${JSON.stringify(requested)}`,
    );
  } finally {
    globalThis.fetch = realFetch;
  }
});
