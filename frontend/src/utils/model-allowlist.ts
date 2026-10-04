/**
 * Console copy of the gateway's model allowlist matching contract.
 *
 * Source of truth: ``backend/preloop/services/model_allowlist.py``
 * (``allowlist_entry_matches_model``) and
 * ``backend/preloop/services/model_runtime_resolver.py``
 * (``gateway_model_alias_candidates``). Keep the three in step: a stored
 * entry that the gateway honours must read as allowed here, and one it
 * ignores must not, or the console shows a policy that is not the one
 * enforced.
 *
 * A stored entry names a model when, after trimming, it equals:
 *
 * - the model id (case-insensitive),
 * - the display name (case-insensitive), or
 * - exactly one of the gateway alias spellings for the model:
 *   - the configured ``meta_data.gateway.model_alias``,
 *   - the bare ``model_identifier``,
 *   - the default ``provider/model_identifier`` alias (provider lower-cased,
 *     ``openai`` when unset),
 *   - for a gateway-enabled model with neither identifier nor configured
 *     alias, the bare provider (the runtime alias the gateway then uses),
 *   - the bare tail of any of the above that carries a ``/``.
 *
 * Not mirrored: the gateway also accepts the raw wire ``model`` string a
 * client sent when it is listed verbatim (``requested_spellings``). That is a
 * property of one request, not of a model, so there is nothing for the
 * console to compare it with. ``AIModel.alias`` is not part of the contract:
 * the API does not return it and the gateway does not read it.
 */

/** The subset of an account AI model the allowlist matcher reads. */
export interface AllowlistModel {
  id: string | number;
  name?: string | null;
  provider_name?: string | null;
  model_identifier?: string | null;
  meta_data?: Record<string, unknown> | null;
}

function gatewayConfig(model: AllowlistModel): Record<string, unknown> {
  const meta = model.meta_data;
  if (!meta || typeof meta !== 'object') return {};
  const gateway = (meta as Record<string, unknown>).gateway;
  return gateway && typeof gateway === 'object' && !Array.isArray(gateway)
    ? (gateway as Record<string, unknown>)
    : {};
}

function configuredAlias(model: AllowlistModel): string {
  const alias = gatewayConfig(model).model_alias;
  return typeof alias === 'string' ? alias.trim() : '';
}

/** ``provider/model_identifier``, mirroring ``_build_default_gateway_alias``. */
function defaultGatewayAlias(model: AllowlistModel): string {
  const provider = (model.provider_name || 'openai').trim().toLowerCase();
  const identifier = (model.model_identifier || '').trim();
  return identifier ? `${provider}/${identifier}` : provider;
}

/**
 * The gateway alias a model answers to and the key the console writes:
 * the configured ``meta_data.gateway.model_alias`` when set, otherwise the
 * default ``provider/model_identifier`` spelling.
 */
export function gatewayAliasForModel(model: AllowlistModel): string {
  return configuredAlias(model) || defaultGatewayAlias(model);
}

/**
 * Every alias spelling that addresses ``model`` on the gateway; mirrors
 * ``gateway_model_alias_candidates``.
 */
export function gatewayAliasCandidates(model: AllowlistModel): Set<string> {
  const candidates = new Set<string>();
  const configured = configuredAlias(model);
  const identifier = (model.model_identifier || '').trim();

  if (gatewayConfig(model).enabled) {
    // The runtime alias of a gateway-enabled model (effective_gateway_alias).
    candidates.add(gatewayAliasForModel(model));
  }
  if (configured) candidates.add(configured);
  if (identifier) {
    candidates.add(identifier);
    candidates.add(defaultGatewayAlias(model));
  }
  for (const alias of [...candidates]) {
    const slash = alias.indexOf('/');
    if (slash === -1) continue;
    const tail = alias.slice(slash + 1).trim();
    if (tail) candidates.add(tail);
  }
  candidates.delete('');
  return candidates;
}

/**
 * Whether one stored allowlist entry names ``model``; mirrors
 * ``allowlist_entry_matches_model``. Aliases compare exactly (they are wire
 * strings); ids and display names compare case-insensitively.
 */
export function allowlistEntryMatchesModel(
  entry: unknown,
  model: AllowlistModel
): boolean {
  if (typeof entry !== 'string') return false;
  const text = entry.trim();
  if (!text) return false;
  const folded = text.toLowerCase();
  if (String(model.id).toLowerCase() === folded) return true;
  const name = (model.name || '').trim();
  if (name && name.toLowerCase() === folded) return true;
  return gatewayAliasCandidates(model).has(text);
}

/**
 * The trimmed, non-empty string entries of a stored allowlist, first
 * occurrence kept; mirrors ``normalize_allowed_models``. Non-strings are
 * dropped rather than stringified so ``[null]`` cannot become a deny-all.
 */
export function normalizeAllowedModels(
  entries: readonly unknown[] | null | undefined
): string[] {
  const normalized: string[] = [];
  for (const item of entries || []) {
    if (typeof item !== 'string') continue;
    const text = item.trim();
    if (text && !normalized.includes(text)) normalized.push(text);
  }
  return normalized;
}

/**
 * The single inventory model one stored entry refers to, for rewriting the
 * entry to that model's alias. Exact alias, then id, then display name win
 * outright. Any other spelling (a bare identifier, an alias tail, the default
 * alias of a model with a configured one) only resolves when exactly one
 * model answers to it: the gateway honours such an entry for every model it
 * names, so rewriting it to one alias would silently narrow the policy.
 */
export function findModelForAllowedEntry<T extends AllowlistModel>(
  entry: string,
  models: readonly T[]
): T | null {
  const needle = entry.trim();
  if (!needle) return null;
  const folded = needle.toLowerCase();
  const byAlias = models.find(
    (model) => gatewayAliasForModel(model) === needle
  );
  if (byAlias) return byAlias;
  const byId = models.find(
    (model) => String(model.id).toLowerCase() === folded
  );
  if (byId) return byId;
  const byName = models.find(
    (model) => (model.name || '').trim().toLowerCase() === folded
  );
  if (byName) return byName;
  const others = models.filter((model) =>
    gatewayAliasCandidates(model).has(needle)
  );
  return others.length === 1 ? others[0] : null;
}
