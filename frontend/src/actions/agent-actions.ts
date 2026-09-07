/**
 * What a managed agent offers, by lifecycle state.
 *
 * The agents list and the agent page each kept their own array, and they
 * drifted: the list offered Decommission, the page did not; Change owner
 * needed a loaded user list in one place and only a feature flag in the
 * other. Both now call this.
 */
import type { TemplateResult } from 'lit';
import type { ManagedAgentSummary } from '../types';
import { getAgentControlState } from '../utils/agent-control';
import type { ResourceAction } from '../components/resource-actions';
import { defineActions, type ActionContext } from './registry';

/** The lifecycle moves an agent can be asked to make. */
export type AgentLifecycleMove = 'suspend' | 'resume' | 'decommission';

/** The action id each lifecycle move is offered under. */
export const AGENT_LIFECYCLE_ACTION_IDS: Record<string, AgentLifecycleMove> = {
  pause: 'suspend',
  resume: 'resume',
  decommission: 'decommission',
};

export interface AgentActionContext extends ActionContext {
  /** True while a call for this agent is in flight. */
  busy?: boolean;
  /** `user_management` is on and there is somebody to hand the agent to. */
  canChangeOwner?: boolean;
  /**
   * The agents table has no room for a Talk button per row, so its kebab
   * carries the action; cards and the canvas draw their own button and would
   * otherwise offer it twice.
   */
  onTalk?: (agent: ManagedAgentSummary) => void;
  /** The agent page renders Talk as its own element instead. */
  renderTalk?: (agent: ManagedAgentSummary) => TemplateResult;
  onRename?: (agent: ManagedAgentSummary) => void;
  onEditTags?: (agent: ManagedAgentSummary) => void;
  onChangeOwner?: (agent: ManagedAgentSummary) => void;
  onLifecycle?: (agent: ManagedAgentSummary, move: AgentLifecycleMove) => void;
  onRemove?: (agent: ManagedAgentSummary) => void;
}

/** Paused or offboarded: the states Resume applies to. */
export function isAgentStopped(agent: ManagedAgentSummary): boolean {
  return (
    agent.lifecycle_state === 'suspended' ||
    agent.lifecycle_state === 'decommissioned'
  );
}

export function agentActions(
  agent: ManagedAgentSummary,
  ctx: AgentActionContext = {}
): ResourceAction[] {
  const control = getAgentControlState(agent);
  const busy = ctx.busy === true;

  return defineActions(
    agent,
    [
      // Talk leads: it is what people come to an agent for.
      control.visible && ctx.renderTalk
        ? {
            id: 'talk',
            label: 'Talk',
            render: () => ctx.renderTalk!(agent),
          }
        : control.visible && ctx.onTalk
          ? {
              id: 'talk',
              label: 'Talk',
              icon: 'chat-dots',
              disabled: false,
              // Runs inside the click handler, so the window still opens on
              // the user gesture.
              onClick: () => ctx.onTalk!(agent),
              available: () => getAgentControlState(agent).enabled,
              visibleWhenUnavailable: true,
              unavailableTooltip: control.detail,
            }
          : null,
      {
        id: 'rename',
        label: 'Rename',
        icon: 'pencil',
        loading: busy,
        onClick: ctx.onRename ? () => ctx.onRename!(agent) : undefined,
      },
      {
        id: 'edit-tags',
        label: 'Edit tags',
        icon: 'tags',
        loading: busy,
        onClick: ctx.onEditTags ? () => ctx.onEditTags!(agent) : undefined,
      },
      {
        id: 'change-owner',
        label: 'Change owner',
        icon: 'person-gear',
        loading: busy,
        available: () => ctx.canChangeOwner === true,
        onClick: ctx.onChangeOwner
          ? () => ctx.onChangeOwner!(agent)
          : undefined,
      },
      // Pause is an everyday, reversible control, so it stays neutral: amber
      // read as a warning next to Talk and pulled the eye away from it.
      {
        id: 'pause',
        label: 'Pause',
        icon: 'pause-fill',
        variant: 'default',
        loading: busy,
        tooltip:
          'Pause this agent. Requests are blocked while paused; Resume restores it without re-onboarding.',
        available: (item) => !isAgentStopped(item),
        onClick: ctx.onLifecycle
          ? () => ctx.onLifecycle!(agent, 'suspend')
          : undefined,
      },
      {
        id: 'resume',
        label: 'Resume',
        icon: 'play-fill',
        variant: 'success',
        loading: busy,
        tooltip:
          'Resume this agent. Its existing credentials start working again immediately.',
        available: (item) => isAgentStopped(item),
        onClick: ctx.onLifecycle
          ? () => ctx.onLifecycle!(agent, 'resume')
          : undefined,
      },
      // Decommission is the reversible offboard: credentials are revoked but
      // the agent and its history stay. Remove deletes the record, so the two
      // are not the same action and both belong here.
      {
        id: 'decommission',
        label: 'Decommission',
        icon: 'box-arrow-right',
        variant: 'danger',
        outline: true,
        loading: busy,
        available: (item) => item.lifecycle_state !== 'decommissioned',
        onClick: ctx.onLifecycle
          ? () => ctx.onLifecycle!(agent, 'decommission')
          : undefined,
      },
      // Last, outlined, after a gap (DESIGN.md "Destructive actions"): the
      // one action here that cannot be undone.
      {
        id: 'remove',
        label: 'Remove',
        icon: 'trash',
        variant: 'danger',
        outline: true,
        separated: true,
        loading: busy,
        onClick: ctx.onRemove ? () => ctx.onRemove!(agent) : undefined,
      },
    ],
    ctx
  );
}
