/**
 * One entry point for "what can this resource do?".
 *
 * A list row calls `actionsFor('agent', row, ctx)`, the bulk bar calls it for
 * every selected row and intersects the results, and the detail page calls it
 * with the same type. Adding a type is adding a module and a line here.
 */
import type { ResourceAction } from '../components/resource-actions';
import type { ApprovalRequest, ManagedAgentSummary } from '../types';
import { agentActions, type AgentActionContext } from './agent-actions';
import {
  flowActions,
  type FlowActionContext,
  type FlowActionResource,
} from './flow-actions';
import {
  flowExecutionActions,
  type FlowExecutionActionContext,
  type FlowExecutionActionResource,
} from './flow-execution-actions';
import {
  runtimeSessionActions,
  type RuntimeSessionActionContext,
  type RuntimeSessionActionResource,
} from './runtime-session-actions';
import {
  approvalActions,
  type ApprovalActionContext,
} from './approval-actions';

/** The types that declare their actions in `src/actions/`. */
export interface ResourceActionTypes {
  agent: { resource: ManagedAgentSummary; ctx: AgentActionContext };
  flow: { resource: FlowActionResource; ctx: FlowActionContext };
  'flow-execution': {
    resource: FlowExecutionActionResource;
    ctx: FlowExecutionActionContext;
  };
  'runtime-session': {
    resource: RuntimeSessionActionResource;
    ctx: RuntimeSessionActionContext;
  };
  approval: { resource: ApprovalRequest; ctx: ApprovalActionContext };
}

export type ResourceType = keyof ResourceActionTypes;

const REGISTRY: {
  [K in ResourceType]: (
    resource: ResourceActionTypes[K]['resource'],
    ctx: ResourceActionTypes[K]['ctx']
  ) => ResourceAction[];
} = {
  agent: agentActions,
  flow: flowActions,
  'flow-execution': flowExecutionActions,
  'runtime-session': runtimeSessionActions,
  approval: approvalActions,
};

export function actionsFor<K extends ResourceType>(
  type: K,
  resource: ResourceActionTypes[K]['resource'],
  ctx: ResourceActionTypes[K]['ctx'] = {}
): ResourceAction[] {
  return REGISTRY[type](resource, ctx);
}

export * from './registry';
export * from './agent-actions';
export * from './flow-actions';
export * from './flow-execution-actions';
export * from './runtime-session-actions';
export * from './approval-actions';
