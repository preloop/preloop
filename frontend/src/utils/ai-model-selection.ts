import type { AIModel } from '../types';

/**
 * Whether Preloop can run its own server-side generation with this model.
 *
 * Principal-bound OAuth credentials (a Claude Code or Codex subscription
 * login) only authorize their owner's interactive traffic. They cannot serve
 * server-side generation, so a model backed by one must never be
 * auto-selected as a default — doing so makes the user's first "Optimize"
 * click fail.
 *
 * Defaults to `true` when the backend did not send the field, so an older API
 * response degrades to the previous behaviour rather than hiding every model.
 */
export function supportsServerSideGeneration(model: AIModel): boolean {
  return model.supports_server_side_generation !== false;
}

/**
 * Filter a model list down to those usable for server-side generation.
 */
export function selectableModels(models: AIModel[]): AIModel[] {
  return models.filter(supportsServerSideGeneration);
}

/**
 * Pick the model that should be preselected as the default.
 *
 * The account's flagged default wins when it is usable; otherwise the first
 * BYOK/API-key-backed model wins. Returns `null` when no model can serve
 * server-side generation — callers must surface that rather than silently
 * selecting a model that will fail.
 */
export function pickDefaultModel(models: AIModel[]): AIModel | null {
  const usable = selectableModels(models);
  return usable.find((model) => model.is_default) || usable[0] || null;
}
