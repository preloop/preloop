/**
 * What a runtime session offers, by whether it has ended.
 *
 * The sessions list offered nothing at all: ending a session meant opening it
 * first, and the panel's one button stayed on screen after the session ended,
 * relabelled "Session ended", which is a status wearing a button's clothes.
 */
import type { ResourceAction } from '../components/resource-actions';
import { defineActions, type ActionContext } from './registry';

export interface RuntimeSessionActionResource {
  id: string;
  /** Set once the session is over: nothing left to end. */
  ended_at?: string | null;
  /**
   * Session summaries carry 'ended', 'active_now' or 'idle' (server side
   * crud/runtime_session.py, _row_to_summary). Only 'ended' closes a session,
   * an idle one can still be ended by hand.
   */
  status?: string | null;
  flow_execution_id?: string | null;
}

export interface RuntimeSessionActionContext extends ActionContext {
  busy?: boolean;
  /** Offered on a list row, where the session panel is somewhere else. */
  includeOpen?: boolean;
  onOpen?: (session: RuntimeSessionActionResource) => void;
  onEnd?: (session: RuntimeSessionActionResource) => void;
}

/**
 * The one rule for "there is nothing left to end", shared by the session
 * toolbar and by any list row that offers End session.
 */
export function isSessionEnded(session: RuntimeSessionActionResource): boolean {
  return session.status === 'ended' || Boolean(session.ended_at);
}

export function isSessionLive(session: RuntimeSessionActionResource): boolean {
  return !isSessionEnded(session);
}

export function runtimeSessionActions(
  session: RuntimeSessionActionResource,
  ctx: RuntimeSessionActionContext = {}
): ResourceAction[] {
  const busy = ctx.busy === true;
  return defineActions(
    session,
    [
      ctx.includeOpen
        ? {
            id: 'open',
            label: 'Open session',
            icon: 'box-arrow-up-right',
            onClick: ctx.onOpen ? () => ctx.onOpen!(session) : undefined,
          }
        : null,
      {
        id: 'view-flow-execution',
        label: 'View flow run',
        icon: 'diagram-3',
        available: (item) => Boolean(item.flow_execution_id),
        href: `/console/flows/executions/${encodeURIComponent(
          session.flow_execution_id || ''
        )}`,
      },
      // Ending a session cuts the agent off mid-conversation, so it is
      // destructive: outlined, last, and confirmed by the caller.
      {
        id: 'end-session',
        label: 'End session',
        icon: 'stop-circle',
        variant: 'danger',
        outline: true,
        separated: true,
        loading: busy,
        available: isSessionLive,
        onClick: ctx.onEnd ? () => ctx.onEnd!(session) : undefined,
      },
    ],
    ctx
  );
}
