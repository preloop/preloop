// Delivered by the server alongside the launch, including with older plugins.
import { readFileSync, writeFileSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

export function configure(runtime, root, timeoutMs) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return;
  if (runtime === "pi") {
    const path = join(root, "settings.json");
    let settings = {};
    try {
      settings = JSON.parse(readFileSync(path, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    settings.httpIdleTimeoutMs = timeoutMs;
    settings.retry = {
      ...settings.retry,
      provider: { ...settings.retry?.provider, timeoutMs },
    };
    writeFileSync(path, JSON.stringify(settings), { mode: 0o600 });
  } else {
    const path = join(root, "cordis.patch.yml");
    const patch = JSON.parse(readFileSync(path, "utf8"));
    const provider = patch.find((row) => row.id === "llm-pi-ai").config
      .providers.preloop;
    Object.assign(provider, { timeoutMs, streamIdleTimeoutMs: timeoutMs });
    patch.push({
      insert: [
        { id: "preloop-model-timeouts", name: fileURLToPath(import.meta.url) },
      ],
    });
    writeFileSync(path, JSON.stringify(patch), { mode: 0o600 });
  }
}

export function gatewayTimeoutFetch(provider, transport) {
  const timeout = provider?.timeoutMs;
  if (!Number.isFinite(timeout) || timeout <= 0) return () => {};
  const base = new URL(provider.baseUrl);
  const prefix = base.pathname.replace(/\/$/, "") + "/";
  const dispatcher = new transport.EnvHttpProxyAgent({
    headersTimeout: timeout,
    bodyTimeout: timeout,
  });
  const original = globalThis.fetch;
  const wrapped = (input, init) => {
    const url = new URL(
      typeof input === "string" || input instanceof URL ? input : input.url,
    );
    if (url.origin === base.origin && url.pathname.startsWith(prefix)) {
      return transport.fetch(input, { ...init, dispatcher });
    }
    return original(input, init);
  };
  globalThis.fetch = wrapped;
  return () => {
    if (globalThis.fetch === wrapped) globalThis.fetch = original;
    void dispatcher.close().catch(() => {});
  };
}

export const name = "preloop-model-timeouts";
export function apply(ctx) {
  // Resolve the runtime's pinned Undici, not a dependency of an older plugin.
  const require = createRequire(realpathSync(process.env.PRELOOP_HARNESS_BIN));
  const provider = JSON.parse(process.env.PRELOOP_HARNESS_MODEL);
  provider.baseUrl = provider.baseUrl.replace(
    "${PRELOOP_URL}",
    process.env.PRELOOP_URL || "",
  );
  ctx.on("dispose", gatewayTimeoutFetch(provider, require("undici")));
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  const runtime = process.env.PRELOOP_HARNESS;
  configure(
    runtime,
    runtime === "pi" ? process.env.PI_CODING_AGENT_DIR : process.env.DSH_HOME,
    JSON.parse(process.env.PRELOOP_HARNESS_MODEL).timeoutMs,
  );
}
