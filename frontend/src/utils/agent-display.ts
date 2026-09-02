import { html, type TemplateResult } from 'lit';
import type { ManagedAgentSummary, RuntimeSessionSummary } from '../types';
import { getAgentKindPresentation } from './agent-kinds';

/**
 * Does a runtime session belong to this managed agent?
 *
 * Sessions reference an agent through the runtime principal or the session
 * source, and long-lived agents suffix those ids per run (`<base>:2`,
 * `<base>-cli`), so prefix matches count too. Shared by the Overview's active
 * agents card and the attention rules.
 */
export function sessionBelongsToAgent(
  session: Pick<
    RuntimeSessionSummary,
    'runtime_principal_id' | 'session_source_id'
  >,
  agent: Pick<ManagedAgentSummary, 'id' | 'session_source_id'>
): boolean {
  const base = agent.session_source_id;
  const candidates = [
    session.runtime_principal_id,
    session.session_source_id,
  ].filter((value): value is string => Boolean(value));
  return candidates.some(
    (id) =>
      id === base ||
      id === agent.id ||
      (Boolean(base) &&
        (id.startsWith(`${base}:`) || id.startsWith(`${base}-`)))
  );
}

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

/**
 * Spelled out on every Remove confirmation, on the list and on the detail
 * page: removing an agent revokes its credentials, which is not obvious from
 * the word "remove" alone.
 */
export const REMOVE_AGENT_CONSEQUENCE =
  "This also revokes the agent's Preloop credentials. If the agent is still onboarded on a machine, its gateway and MCP access stop working until you run `preloop agents onboard` again. To disconnect cleanly, run `preloop agents offboard` on that machine instead.";

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
