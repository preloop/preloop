/**
 * Gateway model-list refresh for the OpenClaw plugin.
 *
 * Derives the gateway ``GET /models`` URL from the Agent Control WS URL,
 * fetches the current model list, and patches the local OpenClaw config
 * file so the agent's model picker reflects console edits without a full
 * restart.
 *
 * Shared design with opencode-preloop/src/models.ts; the logic is
 * deliberately duplicated (no cross-plugin import) so each plugin is
 * independently publishable.
 */

import fs from "node:fs";

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

type ControlConfig = {
  control_ws_url?: string;
  bearer_token?: string;
  [key: string]: unknown;
};

export type GatewayModel = {
  id: string;
  object?: string;
  created?: number;
  owned_by?: string;
};

export type GatewayModelsResponse = {
  object?: string;
  data?: GatewayModel[];
  models?: GatewayModel[];
};

/**
 * Derive the gateway ``GET /models`` URL from the Agent Control WS URL.
 */
export function gatewayModelsUrl(config: ControlConfig): string {
  const wsUrl = new URL(config.control_ws_url!);
  const httpProtocol = wsUrl.protocol === "wss:" ? "https:" : "http:";
  return `${httpProtocol}//${wsUrl.host}/openai/v1/models`;
}

/**
 * Fetch the current model list from the Preloop gateway.
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
 * High-level: fetch models from the gateway.  Best-effort; swallows
 * errors. Returns the model list or an empty array on failure.
 *
 * OpenClaw's plugin API does not expose runtime model-list mutation, so
 * this helper is limited to fetching and logging.  The model list is
 * available for programmatic use by callers that can act on it (e.g. a
 * future OpenClaw hook that supports runtime catalog updates).
 */
export async function refreshModels(
  config: ControlConfig,
  fetchImpl?: FetchLike,
  logger?: (message: string) => void,
): Promise<GatewayModel[]> {
  try {
    const models = await fetchGatewayModels(config, fetchImpl);
    logger?.(
      `model refresh: fetched ${models.length} model(s) from gateway`,
    );
    return models;
  } catch (error) {
    const message = `model refresh failed: ${
      error instanceof Error ? error.message : String(error)
    }`;
    logger?.(message);
    return [];
  }
}
