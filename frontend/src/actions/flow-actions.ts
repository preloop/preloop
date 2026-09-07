/**
 * What a flow offers, by enabled state.
 *
 * The list called the same two actions `run-now` and `edit`; the flow page
 * called them `test-run` and `edit-flow`, so nothing could line them up and
 * the flow page could not delete the flow it was showing. One declaration,
 * one set of ids.
 */
import type { ResourceAction } from '../components/resource-actions';
import { defineActions, type ActionContext } from './registry';

/** The little a flow action needs to know, from a list row or a full flow. */
export interface FlowActionResource {
  id: string;
  name?: string;
  /** False means paused: the flow exists but no trigger starts it. */
  is_enabled: boolean;
}

export interface FlowActionContext extends ActionContext {
  /** True while a call for this flow is in flight. */
  busy?: boolean;
  /** Offered on a list row, where the flow page is somewhere else. */
  includeOpen?: boolean;
  onRun?: (flow: FlowActionResource) => void;
  onToggleEnabled?: (flow: FlowActionResource) => void;
  onDelete?: (flow: FlowActionResource) => void;
  /** The flow page edits in place; the list links to the page in edit mode. */
  onEdit?: (flow: FlowActionResource) => void;
}

export function flowDetailUrl(flow: FlowActionResource): string {
  return `/console/flows/${encodeURIComponent(flow.id)}`;
}

export function flowActions(
  flow: FlowActionResource,
  ctx: FlowActionContext = {}
): ResourceAction[] {
  const busy = ctx.busy === true;
  return defineActions(
    flow,
    [
      ctx.includeOpen
        ? {
            id: 'open',
            label: 'Open',
            icon: 'box-arrow-up-right',
            href: flowDetailUrl(flow),
          }
        : null,
      // Paused keeps Run now on screen, disabled: an operator who paused the
      // flow this morning should read why it cannot run, not hunt for a
      // button that quietly left.
      {
        id: 'run-now',
        label: 'Run now',
        icon: 'play-circle',
        variant: 'primary',
        loading: busy,
        disabled: busy,
        available: (item) => item.is_enabled,
        visibleWhenUnavailable: true,
        unavailableTooltip: 'Resume the flow before running it',
        onClick: ctx.onRun ? () => ctx.onRun!(flow) : undefined,
      },
      {
        id: 'edit',
        label: 'Edit',
        icon: 'pencil',
        href: ctx.onEdit ? undefined : `${flowDetailUrl(flow)}?edit=true`,
        onClick: ctx.onEdit ? () => ctx.onEdit!(flow) : undefined,
      },
      // Two ids, not one toggle: a bulk bar over a mixed selection can then
      // say honestly that neither Pause nor Resume suits all of them.
      {
        id: 'pause',
        label: 'Pause',
        icon: 'pause-circle',
        variant: 'default',
        loading: busy,
        available: (item) => item.is_enabled,
        onClick: ctx.onToggleEnabled
          ? () => ctx.onToggleEnabled!(flow)
          : undefined,
      },
      {
        id: 'resume',
        label: 'Resume',
        icon: 'play-circle',
        variant: 'success',
        loading: busy,
        available: (item) => !item.is_enabled,
        onClick: ctx.onToggleEnabled
          ? () => ctx.onToggleEnabled!(flow)
          : undefined,
      },
      {
        id: 'delete',
        label: 'Delete',
        icon: 'trash',
        variant: 'danger',
        outline: true,
        separated: true,
        onClick: ctx.onDelete ? () => ctx.onDelete!(flow) : undefined,
      },
    ],
    ctx
  );
}
