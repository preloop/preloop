import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { gatewayTimeoutFetch } from "../../../backend/preloop/agents/harness_model_timeouts.mjs";

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
