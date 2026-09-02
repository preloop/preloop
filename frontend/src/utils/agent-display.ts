import { html, type TemplateResult } from 'lit';
import type { ManagedAgentSummary } from '../types';
import { getAgentKindPresentation } from './agent-kinds';

export function getAgentSourceLabel(
  sourceType: string | null | undefined
): string {
  return getAgentKindPresentation(sourceType)?.label || sourceType || 'Unknown';
}

export function getAgentLifecycleVariant(agent: ManagedAgentSummary): string {
  if (agent.lifecycle_state === 'decommissioned') return 'danger';
  if (agent.lifecycle_state === 'suspended') return 'warning';
  if (agent.activity_status === 'active_now') return 'success';
  if (agent.activity_status === 'recently_active') return 'primary';
  if (agent.ended_at) return 'neutral';
  return 'primary';
}

export function getAgentLifecycleLabel(agent: ManagedAgentSummary): string {
  if (agent.lifecycle_state === 'decommissioned') return 'Decommissioned';
  if (agent.lifecycle_state === 'suspended') return 'Paused';
  if (agent.activity_status === 'active_now') return 'Active now';
  if (agent.activity_status === 'recently_active') return 'Recently active';
  if (agent.ended_at) return 'Ended';
  return 'Idle';
}

export interface AgentStatusChip {
  label: string;
  variant: 'success' | 'neutral' | 'warning' | 'danger';
  /** Render as an outlined chip rather than a solid one. */
  outline?: boolean;
}

/** Onboarding states that still leave part of the agent ungoverned. */
const INCOMPLETE_ONBOARDING_STATES = [
  'incomplete',
  'gateway_only',
  'mcp_proxy_only',
];

/**
 * The single status taxonomy for an agent, shared by the agents list rows,
 * the agent cards, the canvas nodes and the agent detail header so the same
 * agent never reads as two different things in two places.
 *
 * Conditions are evaluated in order and the first match wins: lifecycle beats
 * health, health beats activity. Amber means "you have something to fix",
 * green means "working right now", neutral means "nothing to do".
 */
export function getAgentStatusChip(
  agent: ManagedAgentSummary
): AgentStatusChip {
  if (agent.lifecycle_state === 'decommissioned') {
    return { label: 'Decommissioned', variant: 'neutral' };
  }
  if (agent.lifecycle_state === 'suspended') {
    return { label: 'Paused', variant: 'neutral' };
  }
  if (agent.live_validation_status === 'failed') {
    return { label: 'Live check failed', variant: 'warning' };
  }
  if (INCOMPLETE_ONBOARDING_STATES.includes(agent.onboarding_state)) {
    return { label: 'Setup incomplete', variant: 'warning' };
  }
  if (agent.is_active_now) {
    return { label: 'Active now', variant: 'success' };
  }
  if (agent.activity_status === 'recently_active') {
    return { label: 'Recently active', variant: 'success', outline: true };
  }
  return { label: 'Idle', variant: 'neutral' };
}

/**
 * Tags in the reserved `identity.*` namespace are written by the server
 * (re-keying records `identity.previous_ids=...`). They are bookkeeping, not
 * operator labels, so they stay out of the default chip rows and out of the
 * tag editor — while still being preserved on save.
 */
export function isSystemAgentTag(key: string): boolean {
  return key.startsWith('identity.');
}

/** Operator-authored tags, i.e. everything outside the system namespaces. */
export function getVisibleAgentTags(
  tags: Record<string, string> | null | undefined
): [string, string][] {
  return Object.entries(tags || {}).filter(([key]) => !isSystemAgentTag(key));
}

/** Server-owned tags that must survive an operator tag edit. */
export function getSystemAgentTags(
  tags: Record<string, string> | null | undefined
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(tags || {}).filter(([key]) => isSystemAgentTag(key))
  );
}

export function renderAgentIdentityBadges(
  agent: ManagedAgentSummary
): TemplateResult {
  const tags = getVisibleAgentTags(agent.tags);
  return html`
    <div class="agent-identity-badges">
      <sl-badge variant="${getAgentLifecycleVariant(agent)}" pill>
        ${getAgentLifecycleLabel(agent)}
      </sl-badge>
      ${
        agent.owner_username
          ? html`<sl-badge variant="neutral" pill title="Owner">
              <sl-icon
                name="person"
                style="margin-right: 3px; opacity: 0.7;"
              ></sl-icon
              >${agent.owner_username}</sl-badge
            >`
          : null
      }
      ${tags.map(
        ([key, value]) => html`
          <sl-badge variant="neutral" pill>
            <span style="opacity: 0.7">${key}</span>${
              value && value !== 'true'
                ? html`<span style="opacity: 0.4; margin: 0 4px;">=</span
                    >${value}`
                : ''
            }
          </sl-badge>
        `
      )}
    </div>
  `;
}
