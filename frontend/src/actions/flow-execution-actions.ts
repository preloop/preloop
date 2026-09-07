/**
 * What a flow execution offers, by run status.
 *
 * The list and the execution page each carried their own copy of the retry
 * predicate and their own labels ("Retry run" against "Retry"), and the list
 * offered Open session on runs that never had one, which lands on a search
 * with no results.
 */
import type { ResourceAction } from '../components/resource-actions';
import { RUNNING_STATUSES } from '../utils/execution';
import { defineActions, type ActionContext } from './registry';

/** Statuses the retry endpoint accepts. */
export const RETRYABLE_EXECUTION_STATUSES = new Set([
  'FAILED',
  'STOPPED',
  'TIMEOUT',
  'CANCELLED',
]);

export interface FlowExecutionActionResource {
  id: string;
  status: string;
  /** Set once the run opened a runtime session worth linking to. */
  agent_session_reference?: string | null;
  flow_id?: string | null;
}

export interface FlowExecutionActionContext extends ActionContext {
  busy?: boolean;
  /** Offered on a list row, where the execution page is somewhere else. */
  includeOpen?: boolean;
  /** Offered on the execution page, which knows which flow it belongs to. */
  includeViewFlow?: boolean;
  /**
   * Where "Open session" goes. The execution page sends the operator to the
   * session record in the sessions list; a list row sends them to the run's
   * own transcript, which is the same conversation one page closer.
   */
  sessionHref?: (execution: FlowExecutionActionResource) => string;
  onCancel?: (execution: FlowExecutionActionResource) => void;
  onRetry?: (execution: FlowExecutionActionResource) => void;
}

export function isExecutionRunning(
  execution: FlowExecutionActionResource
): boolean {
  return RUNNING_STATUSES.has(execution.status);
}

export function canRetryExecution(
  execution: FlowExecutionActionResource
): boolean {
  return RETRYABLE_EXECUTION_STATUSES.has(execution.status);
}

export function executionDetailUrl(
  execution: FlowExecutionActionResource
): string {
  return `/console/flows/executions/${encodeURIComponent(execution.id)}`;
}

/** The session this run opened, found by id in the sessions list. */
export function executionSessionUrl(
  execution: FlowExecutionActionResource
): string {
  return `/console/runtime-sessions?query=${encodeURIComponent(execution.id)}`;
}

export function flowExecutionActions(
  execution: FlowExecutionActionResource,
  ctx: FlowExecutionActionContext = {}
): ResourceAction[] {
  const busy = ctx.busy === true;
  return defineActions(
    execution,
    [
      ctx.includeOpen
        ? {
            id: 'open',
            label: 'Open',
            icon: 'box-arrow-up-right',
            href: executionDetailUrl(execution),
          }
        : null,
      {
        id: 'retry',
        label: 'Retry run',
        icon: 'arrow-repeat',
        loading: busy,
        available: canRetryExecution,
        onClick: ctx.onRetry ? () => ctx.onRetry!(execution) : undefined,
      },
      {
        id: 'open-session',
        label: 'Open session',
        icon: 'chat-left-text',
        available: (item) => Boolean(item.agent_session_reference),
        href: ctx.sessionHref
          ? ctx.sessionHref(execution)
          : executionSessionUrl(execution),
      },
      ctx.includeViewFlow
        ? {
            id: 'view-flow',
            label: 'View flow',
            icon: 'diagram-3',
            available: (item) => Boolean(item.flow_id),
            href: `/console/flows/${encodeURIComponent(
              execution.flow_id || ''
            )}`,
          }
        : null,
      // Stopping a run destroys work in progress, so it is destructive and
      // sits apart from the everyday actions.
      {
        id: 'cancel',
        label: 'Cancel run',
        icon: 'x-circle',
        variant: 'danger',
        outline: true,
        separated: true,
        available: isExecutionRunning,
        onClick: ctx.onCancel ? () => ctx.onCancel!(execution) : undefined,
      },
    ],
    ctx
  );
}
