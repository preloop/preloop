// Unit tests for gateway model-list refresh (models.ts).
// Run: npm run build && node --test test/models.test.mjs
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  gatewayModelsUrl,
  fetchGatewayModels,
  patchConfigModels,
  refreshModels,
} from "../dist/models.js";

const baseConfig = {
  runtime: "opencode",
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
  const config = {
    ...baseConfig,
    control_ws_url: "ws://localhost:8000/api/v1/agents/control/ws",
  };
  assert.equal(
    gatewayModelsUrl(config),
    "http://localhost:8000/openai/v1/models",
  );
});

// ---------------------------------------------------------------------------
// fetchGatewayModels
// ---------------------------------------------------------------------------

test("fetchGatewayModels returns the data array from a successful response", async () => {
  const models = [
    { id: "claude-sonnet-4", object: "model", created: 1, owned_by: "preloop" },
    { id: "gpt-5", object: "model", created: 2, owned_by: "preloop" },
  ];
  let captured;
  const result = await fetchGatewayModels(baseConfig, (url, init) => {
    captured = { url, init };
    return jsonResponse(200, { object: "list", data: models });
  });
  assert.deepEqual(result, models);
  assert.equal(captured.url, "https://example.preloop.ai/openai/v1/models");
  assert.equal(captured.init.headers.authorization, "Bearer secret-token");
});

test("fetchGatewayModels falls back to models array when data is absent", async () => {
  const models = [{ id: "test-model", object: "model" }];
  const result = await fetchGatewayModels(baseConfig, () =>
    jsonResponse(200, { object: "list", models }),
  );
  assert.deepEqual(result, models);
});

test("fetchGatewayModels returns empty array when response has neither data nor models", async () => {
  const result = await fetchGatewayModels(baseConfig, () =>
    jsonResponse(200, { object: "list" }),
  );
  assert.deepEqual(result, []);
});

test("fetchGatewayModels throws on non-OK HTTP status", async () => {
  await assert.rejects(
    fetchGatewayModels(baseConfig, () => jsonResponse(401, {})),
    /HTTP 401/,
  );
});

// ---------------------------------------------------------------------------
// patchConfigModels
// ---------------------------------------------------------------------------

function writeTempConfig(config) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-oc-models-"));
  const file = path.join(dir, "opencode.json");
  fs.writeFileSync(file, JSON.stringify(config, null, 2));
  return { file, dir };
}

test("patchConfigModels updates the matching provider models map", () => {
  const { file, dir } = writeTempConfig({
    model: "preloop/claude-sonnet-4",
    provider: {
      preloop: {
        npm: "@ai-sdk/openai-compatible",
        options: { baseURL: "https://example.preloop.ai/openai/v1" },
        models: { "claude-sonnet-4": { name: "claude-sonnet-4" } },
      },
    },
  });
  try {
    const models = [
      { id: "claude-sonnet-4", object: "model" },
      { id: "gpt-5", object: "model" },
    ];
    const patched = patchConfigModels(
      file,
      "https://example.preloop.ai",
      models,
    );
    assert.equal(patched, 1);
    const updated = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.deepEqual(updated.provider.preloop.models, {
      "claude-sonnet-4": { name: "claude-sonnet-4" },
      "gpt-5": { name: "gpt-5" },
    });
    // No temp file left behind by the atomic write.
    assert.deepEqual(fs.readdirSync(dir), ["opencode.json"]);
    // Unrelated keys must be preserved.
    assert.equal(updated.model, "preloop/claude-sonnet-4");
    assert.equal(updated.provider.preloop.npm, "@ai-sdk/openai-compatible");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels skips providers on a different origin", () => {
  const { file, dir } = writeTempConfig({
    provider: {
      other: {
        options: { baseURL: "https://other.host/v1" },
        models: { "some-model": { name: "some-model" } },
      },
    },
  });
  try {
    const patched = patchConfigModels(
      file,
      "https://example.preloop.ai",
      [{ id: "new-model", object: "model" }],
    );
    assert.equal(patched, 0);
    // File should not have been rewritten.
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.deepEqual(raw.provider.other.models, {
      "some-model": { name: "some-model" },
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels returns -1 for a missing file", () => {
  const result = patchConfigModels("/no/such/file.json", "https://x", []);
  assert.equal(result, -1);
});

test("patchConfigModels returns 0 when the config has no provider section", () => {
  const { file, dir } = writeTempConfig({ model: "test/model" });
  try {
    const result = patchConfigModels(file, "https://x", [
      { id: "m", object: "model" },
    ]);
    assert.equal(result, 0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels does not wipe models on an empty gateway response", () => {
  const { file, dir } = writeTempConfig({
    provider: {
      preloop: {
        options: { baseURL: "https://example.preloop.ai/openai/v1" },
        models: { existing: { name: "existing" } },
      },
    },
  });
  try {
    const patched = patchConfigModels(
      file,
      "https://example.preloop.ai",
      [],
    );
    assert.equal(patched, 0);
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.deepEqual(raw.provider.preloop.models, {
      existing: { name: "existing" },
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels reconciles to the gateway list, dropping stale ids", () => {
  const { file, dir } = writeTempConfig({
    provider: {
      preloop: {
        options: { baseURL: "https://gw.preloop.ai/openai/v1" },
        models: {
          "old-model": { name: "old-model" },
          "shared-model": { name: "Shared (local label)" },
        },
      },
    },
  });
  try {
    const models = [
      { id: "shared-model", object: "model" },
      { id: "new-model", object: "model" },
    ];
    patchConfigModels(file, "https://gw.preloop.ai", models);
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    // old-model is gone (the gateway would reject it); new-model added.
    assert.deepEqual(Object.keys(raw.provider.preloop.models).sort(), [
      "new-model",
      "shared-model",
    ]);
    // A still-advertised model keeps its local entry as-is.
    assert.deepEqual(raw.provider.preloop.models["shared-model"], {
      name: "Shared (local label)",
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels leaves providers on other origins untouched", () => {
  const { file, dir } = writeTempConfig({
    provider: {
      preloop: {
        options: { baseURL: "https://gw.preloop.ai/openai/v1" },
        models: { "old-model": { name: "old-model" } },
      },
      ollama: {
        options: { baseURL: "http://localhost:11434/v1" },
        models: { "llama-3": { name: "llama-3" } },
      },
    },
  });
  try {
    const patched = patchConfigModels(file, "https://gw.preloop.ai", [
      { id: "new-model", object: "model" },
    ]);
    assert.equal(patched, 1);
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.deepEqual(Object.keys(raw.provider.preloop.models), ["new-model"]);
    assert.deepEqual(raw.provider.ollama.models, {
      "llama-3": { name: "llama-3" },
    });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels does not rewrite the file when the ids already match", () => {
  const { file, dir } = writeTempConfig({
    provider: {
      preloop: {
        options: { baseURL: "https://gw.preloop.ai/openai/v1" },
        models: { "model-a": { name: "model-a" } },
      },
    },
  });
  try {
    const before = fs.statSync(file).mtimeMs;
    const patched = patchConfigModels(file, "https://gw.preloop.ai", [
      { id: "model-a", object: "model" },
    ]);
    assert.equal(patched, 1);
    assert.equal(fs.statSync(file).mtimeMs, before);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("patchConfigModels writes atomically via a temp file in the same dir", () => {
  const { file, dir } = writeTempConfig({
    provider: {
      preloop: {
        options: { baseURL: "https://gw.preloop.ai/openai/v1" },
        models: {},
      },
    },
  });
  const realRename = fs.renameSync;
  const renames = [];
  fs.renameSync = (from, to) => {
    renames.push({ from, to });
    return realRename(from, to);
  };
  try {
    patchConfigModels(file, "https://gw.preloop.ai", [
      { id: "model-a", object: "model" },
    ]);
    assert.equal(renames.length, 1);
    assert.equal(renames[0].to, file);
    assert.equal(path.dirname(renames[0].from), path.dirname(file));
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.ok("model-a" in raw.provider.preloop.models);
  } finally {
    fs.renameSync = realRename;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// refreshModels (end-to-end)
// ---------------------------------------------------------------------------

test("refreshModels fetches and patches in one call", async () => {
  const { file, dir } = writeTempConfig({
    provider: {
      preloop: {
        options: { baseURL: "https://example.preloop.ai/openai/v1" },
        models: {},
      },
    },
  });
  try {
    const logs = [];
    const summary = await refreshModels(
      baseConfig,
      file,
      () =>
        jsonResponse(200, {
          data: [{ id: "model-a", object: "model" }],
        }),
      (msg) => logs.push(msg),
    );
    assert.match(summary, /refreshed 1 model/);
    assert.ok(logs.length > 0);
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.ok("model-a" in raw.provider.preloop.models);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("refreshModels swallows fetch errors gracefully", async () => {
  const { file, dir } = writeTempConfig({});
  try {
    const summary = await refreshModels(
      baseConfig,
      file,
      () => Promise.reject(new Error("network down")),
    );
    assert.match(summary, /model refresh failed/);
    assert.match(summary, /network down/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
