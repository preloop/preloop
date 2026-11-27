import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import {
  apply,
  gatewayTimeoutFetch,
} from "../../../backend/preloop/agents/harness_model_timeouts.mjs";

test("production plugin resolves the runtime's transport and restores fetch on disposal", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "preloop-runtime-transport-"));
  const executable = join(root, "runtime", "bin", "dsh.js");
  const transportDir = join(root, "runtime", "node_modules", "undici");
  mkdirSync(join(root, "runtime", "bin"), { recursive: true });
  mkdirSync(transportDir, { recursive: true });
  writeFileSync(executable, "");
  writeFileSync(
    join(transportDir, "index.js"),
    `exports.calls = [];
exports.EnvHttpProxyAgent = class {
  constructor(options) { exports.calls.push(options); }
  async close() { exports.calls.push("closed"); }
};
exports.fetch = async (url, options) => {
  exports.calls.push({ url, options });
  return new Response("runtime transport");
};`,
  );
  const binary = join(root, "dsh");
  symlinkSync(executable, binary);
  const previousEnv = { ...process.env };
  const originalFetch = globalThis.fetch;
  let dispose;
  t.after(() => {
    dispose?.();
    globalThis.fetch = originalFetch;
    process.env = previousEnv;
    rmSync(root, { recursive: true, force: true });
  });
  process.env.PRELOOP_HARNESS_BIN = binary;
  process.env.PRELOOP_URL = "https://example.com";
  process.env.PRELOOP_HARNESS_MODEL = JSON.stringify({
    baseUrl: "${PRELOOP_URL}/openai/v1",
    timeoutMs: 7215000,
  });
  const fallback = async () => new Response("unrelated");
  globalThis.fetch = fallback;
  apply({
    on(event, callback) {
      assert.equal(event, "dispose");
      dispose = callback;
    },
  });
  assert.notEqual(globalThis.fetch, fallback);
  const transport = createRequire(executable)("undici");
  const controller = new AbortController();
  const url = "https://example.com/openai/v1/chat/completions";
  assert.equal(
    await (await fetch(url, { signal: controller.signal })).text(),
    "runtime transport",
  );
  assert.deepEqual(transport.calls[0], {
    headersTimeout: 7215000,
    bodyTimeout: 7215000,
  });
  assert.equal(transport.calls[1].options.signal, controller.signal);
  assert.ok(
    transport.calls[1].options.dispatcher instanceof
      transport.EnvHttpProxyAgent,
  );
  assert.equal(
    await (await fetch("https://example.com/api/v1/agents")).text(),
    "unrelated",
  );
  dispose();
  dispose = undefined;
  assert.equal(globalThis.fetch, fallback);
  assert.equal(transport.calls.at(-1), "closed");
});

for (const runtime of ["pi", "deepseek"]) {
  test(`${runtime} gateway client allows the flow's model wait`, () => {
    const root = mkdtempSync(join(tmpdir(), "preloop-timeout-"));
    try {
      const env = {
        ...process.env,
        PRELOOP_DISABLE_TELEMETRY: "true",
        PRELOOP_HARNESS: runtime,
        PI_CODING_AGENT_DIR: root,
        DSH_HOME: root,
        PRELOOP_HARNESS_MODEL: JSON.stringify({
          api: "openai-completions",
          baseUrl: "https://example.com/openai/v1",
          models: [{ id: "example-model" }],
          timeoutMs: 7215000,
        }),
      };
      for (const file of [
        "../bootstrap.mjs",
        "../../../backend/preloop/agents/harness_model_timeouts.mjs",
      ]) {
        execFileSync(
          process.execPath,
          [new URL(file, import.meta.url).pathname],
          { env },
        );
      }
      if (runtime === "pi") {
        const settings = JSON.parse(readFileSync(join(root, "settings.json")));
        assert.equal(settings.httpIdleTimeoutMs, 7215000);
        assert.equal(settings.retry.provider.timeoutMs, 7215000);
      } else {
        const patch = JSON.parse(readFileSync(join(root, "cordis.patch.yml")));
        const provider = patch.find((row) => row.id === "llm-pi-ai").config
          .providers.preloop;
        assert.equal(provider.timeoutMs, 7215000);
        assert.equal(provider.streamIdleTimeoutMs, 7215000);
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
}

test("gateway fetch carries the wait, preserves cancellation and scopes its override", async () => {
  const server = createServer((_req, res) => {
    setTimeout(() => {
      res.writeHead(200);
      res.end("ok");
    }, 100);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${server.address().port}/v1`;
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response("unrelated");
  const restore = gatewayTimeoutFetch(
    { baseUrl, timeoutMs: 500 },
    {
      EnvHttpProxyAgent: class {
        constructor(options) {
          assert.deepEqual(options, { headersTimeout: 500, bodyTimeout: 500 });
        }
        async close() {}
      },
      fetch: (input, init) => {
        assert.ok(init.dispatcher);
        const { dispatcher, ...options } = init;
        return original(input, options);
      },
    },
  );
  try {
    assert.equal(
      await (await fetch(baseUrl + "/chat/completions")).text(),
      "ok",
    );
    assert.equal(await (await fetch(baseUrl + "-other")).text(), "unrelated");
    await assert.rejects(
      fetch(baseUrl + "/chat/completions", { signal: AbortSignal.timeout(10) }),
    );
  } finally {
    restore();
    globalThis.fetch = original;
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});
