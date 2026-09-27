import { html, nothing } from 'lit';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';

/** The part of a list row a parent's share adds. Absent everywhere else. */
export interface SharedFrom {
  account_id?: string;
  account_name: string;
}

export function sharedFrom(resource: unknown): SharedFrom | null {
  const value = (resource as { shared_from?: unknown } | null)?.shared_from;
  if (!value || typeof value !== 'object') return null;
  const name = (value as { account_name?: unknown }).account_name;
  return typeof name === 'string' && name ? (value as SharedFrom) : null;
}

/** Where a subaccount opens a resource its parent shared with it. */
export function sharedResourceHref(kind: string, id: string): string {
  return `/console/shared/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`;
}

/**
 * "Shared from <parent>" for a resource that a parent account shared. Renders
 * nothing for the account's own resources, which is every resource on a
 * deployment without the account hierarchy capability.
 */
export function sharedFromBadge(resource: unknown) {
  const from = sharedFrom(resource);
  if (!from) return nothing;
  return html`<sl-badge
    class="shared-from"
    variant="primary"
    pill
    data-testid="shared-from-badge"
    >Shared from ${from.account_name}</sl-badge
  >`;
}
