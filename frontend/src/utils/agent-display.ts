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
  if (agent.lifecycle_state === 'suspended') return 'Suspended';
  if (agent.activity_status === 'active_now') return 'Active now';
  if (agent.activity_status === 'recently_active') return 'Recently active';
  if (agent.ended_at) return 'Ended';
  return 'Idle';
}

export function renderAgentIdentityBadges(
  agent: ManagedAgentSummary
): TemplateResult {
  const tags = Object.entries(agent.tags || {});
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
