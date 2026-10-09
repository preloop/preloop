/**
 * Capability gate for the multi-account, account hierarchy and access rule
 * views.
 *
 * Every edition builds the console from this source. The views for these
 * features call endpoints that exist only when an extension plugin is mounted,
 * and each view renders only when `GET /api/v1/features` reports its
 * capability as `true`. The gate is a plain data check on that payload: no
 * build flag, no plugin module. With none reported (the open-source default)
 * nothing gated renders and none of its code is downloaded.
 *
 * This module ships in the main bundle, so it holds names and checks only.
 * The views themselves live in lazy chunks.
 */
import { getFeatures, type FeaturesResponse, type Edition } from './api';

/** Unknown or older servers safely default to the open-source edition. */
export function editionOf(
  payload: Partial<FeaturesResponse> | null | undefined
): Edition {
  return payload?.edition === 'cloud' || payload?.edition === 'enterprise'
    ? payload.edition
    : 'oss';
}

export const CAPABILITIES = [
  'multi_account',
  'account_hierarchy',
  'abac_rules',
  'chat_connections',
] as const;

export type Capability = (typeof CAPABILITIES)[number];

export type CapabilitySet = ReadonlySet<Capability>;

export const NO_CAPABILITIES: CapabilitySet = new Set<Capability>();

/** The capabilities a `/features` payload turns on. Only a literal `true` counts. */
export function capabilitiesOf(
  features: FeaturesResponse['features'] | null | undefined
): CapabilitySet {
  const on = new Set<Capability>();
  if (!features) return on;
  for (const name of CAPABILITIES) {
    if (features[name] === true) on.add(name);
  }
  return on;
}

export function hasCapability(
  features: FeaturesResponse['features'] | null | undefined,
  capability: Capability
): boolean {
  return features?.[capability] === true;
}

/**
 * The capabilities of this deployment. A failed `/features` read means none:
 * hiding a view is always safe, showing one against a missing API is not.
 */
export async function loadCapabilities(): Promise<CapabilitySet> {
  try {
    return capabilitiesOf((await getFeatures()).features);
  } catch {
    return NO_CAPABILITIES;
  }
}

/**
 * Thrown by a gated API client when the endpoint answered 404 for the
 * collection itself: the plugin that serves it is not mounted, so the view
 * hides. It never becomes an error message.
 */
export class CapabilityOffError extends Error {
  constructor(path: string) {
    super(`Capability endpoint not available: ${path}`);
    this.name = 'CapabilityOffError';
  }
}

export function isCapabilityOff(error: unknown): error is CapabilityOffError {
  return error instanceof CapabilityOffError;
}
