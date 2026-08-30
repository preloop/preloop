/**
 * Gateway model-list refresh for the OpenCode plugin.
 *
 * Derives the gateway ``GET /models`` URL from the Agent Control WS URL
 * already in the plugin config, fetches the current model list, and
 * patches the local ``opencode.json`` provider models map so the
 * in-session model picker reflects console edits without a restart.
 *
 * Design notes:
 * - The gateway models endpoint is ``/openai/v1/models`` on the same
 *   origin as the control WS (``/api/v1/agents/control/ws``).
 * - Auth: the same durable bearer token Agent Control already holds.
 * - Trigger: on WebSocket connect (plugin start + reconnect).  A
 *   periodic timer is intentionally avoided to keep the plugin
 *   lightweight; reconnects already fire on network changes, and the
 *   operator can force a reconnect from the console.
 * - File write: only the ``models`` map of a provider pointing at the
 *   gateway is touched.  It is reconciled to the gateway list (added and
 *   removed ids both apply); unrelated keys (MCP, permissions,
 *   preloop.control) and providers on other origins are left alone.  The
 *   file is replaced temp-file-then-rename so an interrupted write cannot
 *   truncate the operator's config.
 */

import fs from "node:fs";
import type { ControlConfig } from "./config.js";

type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    signal?: AbortSignal;
  },
) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;

/** OpenAI-compatible model entry returned by the gateway. */
export type GatewayModel = {
  id: string;
  object?: string;
  created?: number;
  owned_by?: string;
};

/** Shape of the ``GET /models`` response. */
export type GatewayModelsResponse = {
  object?: string;
  data?: GatewayModel[];
  models?: GatewayModel[];
};

/**
 * Derive the gateway ``GET /models`` URL from the Agent Control WS URL.
 *
 * ```
 * wss://app.example.ai/api/v1/agents/control/ws
 *  ->  https://app.example.ai/openai/v1/models
 * ```
 */
export function gatewayModelsUrl(config: ControlConfig): string {
  const wsUrl = new URL(config.control_ws_url!);
  const httpProtocol = wsUrl.protocol === "wss:" ? "https:" : "http:";
  return `${httpProtocol}//${wsUrl.host}/openai/v1/models`;
}

/**
 * Fetch the current model list from the Preloop gateway.
 *
 * Returns the array of model objects (``data`` field of the response).
 * Throws on network/HTTP errors.
 */
export async function fetchGatewayModels(
  config: ControlConfig,
  fetchImpl?: FetchLike,
): Promise<GatewayModel[]> {
  const doFetch = fetchImpl ?? (fetch as unknown as FetchLike);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await doFetch(gatewayModelsUrl(config), {
      method: "GET",
      headers: {
        authorization: `Bearer ${config.bearer_token}`,
        accept: "application/json",
      },
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`GET /models returned HTTP ${response.status}`);
    }
    const body = (await response.json()) as GatewayModelsResponse;
    return body.data ?? body.models ?? [];
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Read-modify-write the OpenCode config so that every provider whose
 * ``baseURL`` points at the Preloop gateway origin lists exactly the
 * models the gateway currently advertises.
 *
 * The matched provider blocks are reconciled, not merely extended: ids
 * the gateway no longer advertises are removed so the picker cannot
 * offer a model that the gateway will reject at request time. Providers
 * on any other origin are left untouched, as are unrelated keys inside
 * the matched provider (``npm``, ``options``, ...).
 *
 * Returns the number of provider sections that were updated, or -1 if
 * the config file could not be processed (missing, unparseable, etc.).
 */
export function patchConfigModels(
  configPath: string,
  gatewayOrigin: string,
  models: GatewayModel[],
): number {
  let raw: Record<string, unknown>;
  try {
    raw = JSON.parse(fs.readFileSync(configPath, "utf8")) as Record<
      string,
      unknown
    >;
  } catch {
    return -1;
  }

  const providers = raw["provider"] as
    | Record<string, Record<string, unknown>>
    | undefined;
  if (!providers || typeof providers !== "object") {
    return 0;
  }

  const modelsMap: Record<string, { name: string }> = {};
  for (const model of models) {
    modelsMap[model.id] = { name: model.id };
  }
  if (Object.keys(modelsMap).length === 0) {
    // Empty model list: nothing to patch (avoid wiping valid entries).
    return 0;
  }

  let patched = 0;
  let changed = false;
  for (const [, providerConfig] of Object.entries(providers)) {
    if (!providerConfig || typeof providerConfig !== "object") {
      continue;
    }
    const options = providerConfig["options"] as
      | Record<string, unknown>
      | undefined;
    const baseURL =
      typeof options?.["baseURL"] === "string"
        ? (options["baseURL"] as string)
        : undefined;
    if (!baseURL) {
      continue;
    }
    // Match: provider baseURL origin equals the gateway origin.
    try {
      const providerOrigin = new URL(baseURL).origin;
      if (providerOrigin !== gatewayOrigin) {
        continue;
      }
    } catch {
      continue;
    }
    // Reconcile to the gateway list: a model the gateway stopped
    // advertising is no longer usable, so leaving it in the picker only
    // defers the failure to request time.  Existing entries for ids the
    // gateway still advertises keep their local shape.
    const existing = (providerConfig["models"] ?? {}) as Record<
      string,
      unknown
    >;
    const reconciled: Record<string, unknown> = {};
    for (const id of Object.keys(modelsMap)) {
      reconciled[id] = existing[id] ?? modelsMap[id];
    }
    if (!sameModelIds(existing, reconciled)) {
      changed = true;
    }
    providerConfig["models"] = reconciled;
    patched += 1;
  }

  if (changed) {
    writeConfigAtomically(configPath, JSON.stringify(raw, null, 2) + "\n");
  }
  return patched;
}

/** True when both model maps carry the same set of ids. */
function sameModelIds(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
): boolean {
  const beforeIds = Object.keys(before);
  const afterIds = Object.keys(after);
  if (beforeIds.length !== afterIds.length) {
    return false;
  }
  return beforeIds.every((id) => id in after);
}

/**
 * Replace *configPath* without ever leaving a truncated file behind.
 *
 * This runs on every gateway connect and reconnect, so a plain
 * ``writeFileSync`` would put the operator's ``opencode.json`` at risk on
 * every crash or full disk.  The temp file is created in the same
 * directory so the rename stays on one filesystem and is atomic.
 */
function writeConfigAtomically(configPath: string, contents: string): void {
  const tempPath = `${configPath}.preloop-${process.pid}.tmp`;
  try {
    fs.writeFileSync(tempPath, contents, "utf8");
    fs.renameSync(tempPath, configPath);
  } catch (error) {
    try {
      fs.rmSync(tempPath, { force: true });
    } catch {
      // Best-effort cleanup only.
    }
    throw error;
  }
}

/**
 * High-level: fetch models from the gateway and patch the OpenCode
 * config file.  Swallows all errors (the feature is best-effort).
 *
 * @returns A human-readable summary string for logging.
 */
export async function refreshModels(
  config: ControlConfig,
  configPath: string,
  fetchImpl?: FetchLike,
  logger?: (message: string) => void,
): Promise<string> {
  try {
    const models = await fetchGatewayModels(config, fetchImpl);
    const origin = new URL(gatewayModelsUrl(config)).origin;
    const patched = patchConfigModels(configPath, origin, models);
    const summary =
      patched > 0
        ? `refreshed ${models.length} model(s) in ${patched} provider(s)`
        : patched === 0
          ? `${models.length} model(s) fetched but no matching provider in config`
          : "config file could not be read";
    logger?.(summary);
    return summary;
  } catch (error) {
    const message = `model refresh failed: ${
      error instanceof Error ? error.message : String(error)
    }`;
    logger?.(message);
    return message;
  }
}
