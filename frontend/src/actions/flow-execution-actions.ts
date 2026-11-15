/**
 * What a flow execution offers, by run status.
 *
 * The list and the execution page each carried their own copy of the retry
 * predicate and their own labels ("Retry run" against "Retry"), and the list
 * offered Open session on runs that never had one, which lands on a search
 * with no results.
 */
import { confirmDialog } from '../components/confirm-dialog';
import type { ResourceAction } from '../components/resource-actions';
import { RUNNING_STATUSES } from '../utils/execution';
import {
  failureCategoryLabel,
  failureCategoryTooltip,
} from '../utils/failure-category';
import { defineActions, type ActionContext } from './registry';

/**
 * Statuses the stop command accepts.
 *
 * The same four `POST /flows/executions/{id}/command` treats as stoppable,
 * including PENDING: a run that is queued and has no runtime yet is exactly
 * the one an operator wants to call off, and the backend completes that stop
 * durably (`cancel_unstarted_stop`), so it can never be claimed later.
 */
export const STOPPABLE_EXECUTION_STATUSES: ReadonlySet<string> =
  RUNNING_STATUSES;

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
  runtime_session_id?: string | null;
  trigger_event_details?: Record<string, unknown> | null;
  resume_of?: string | null;
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

/** Whether the stop command will act on this run. */
export function canStopExecution(
  execution: FlowExecutionActionResource
): boolean {
  return STOPPABLE_EXECUTION_STATUSES.has(execution.status);
}

/**
 * The one confirmation asked before a run is stopped.
 *
 * Stopping destroys work in progress and cannot be undone from the console,
 * so every surface that offers it asks the same question in the same words.
 */
export function confirmStopExecution(execution: {
  flow_name?: string | null;
}): Promise<boolean> {
  return confirmDialog({
    title: 'Cancel run',
    message: `Stop the run of "${execution.flow_name || 'this flow'}"?`,
    detail:
      'The agent stops where it is. Work already done is kept in the run, but the run does not finish, and it cannot be resumed \u2014 only retried from the start.',
    confirmLabel: 'Cancel run',
    cancelLabel: 'Keep running',
    variant: 'danger',
  });
}

/**
 * Confirmation asked before a run is retried.
 *
 * States the model and harness the retry will use (re-resolved from current flow).
 */
export interface RetryConfirmOptions {
  flow_name?: string | null;
  agent_type?: string | null;
  model_name?: string | null;
  /** The failed run's stored category, to warn when a retry cannot help. */
  failure_category?: string | null;
}

/**
 * Failures a plain retry repeats exactly: a credential, a billing account or
 * a model setting has to change first. The confirmation says so instead of
 * implying another attempt might work.
 */
export const NON_TRANSIENT_FAILURE_CATEGORIES: ReadonlySet<string> = new Set([
  'repository_access',
  'model_auth',
  'provider_billing',
  'model_config',
  'hook_binding_invalid',
]);

/** The body of the retry confirmation. */
export function retryConfirmDetail(options: RetryConfirmOptions): string {
  const harness = options.agent_type || 'default harness';
  const model = options.model_name || 'default model';
  const base = `The retry will run with ${harness} using ${model}, re-resolved from the current flow definition.`;
  const category = (options.failure_category || '').trim().toLowerCase();
  if (!NON_TRANSIENT_FAILURE_CATEGORIES.has(category)) return base;
  return `${base} This run failed with "${failureCategoryLabel(
    category
  )}", which a retry repeats until the cause is fixed: ${failureCategoryTooltip(
    category
  )}`;
}

export function confirmRetryExecution(
  options: RetryConfirmOptions
): Promise<boolean> {
  return confirmDialog({
    title: 'Retry run',
    message: `Retry the run of "${options.flow_name || 'this flow'}"?`,
    detail: retryConfirmDetail(options),
    confirmLabel: 'Retry run',
    cancelLabel: 'Cancel',
    variant: 'primary',
  });
}

export function canRetryExecution(
  execution: FlowExecutionActionResource
): boolean {
  const resume = execution.trigger_event_details?._resume;
  const controllerOwned =
    resume &&
    typeof resume === 'object' &&
    'thread_id' in resume &&
    resume.thread_id;
  return (
    RETRYABLE_EXECUTION_STATUSES.has(execution.status) &&
    !controllerOwned &&
    !execution.resume_of
  );
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
  const binding = execution.trigger_event_details?._agent_control;
  const session =
    binding && typeof binding === 'object'
      ? (binding as Record<string, unknown>).history_session_id ||
        (binding as Record<string, unknown>).runtime_session_id
      : null;
  return `/console/runtime-sessions?query=${encodeURIComponent(
    execution.runtime_session_id ||
      (typeof session === 'string' ? session : execution.id)
  )}`;
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
        available: canStopExecution,
        onClick: ctx.onCancel ? () => ctx.onCancel!(execution) : undefined,
      },
    ],
    ctx
  );
}
