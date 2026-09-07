/**
 * The one place that decides which actions a resource offers.
 *
 * Every list row, every bulk bar and every detail page asked the same
 * question ("what can I do with this thing?") and answered it with its own
 * hand-written array, so the answers drifted: the agents list offered
 * Decommission and the agent page did not, the flows list called an action
 * `run-now` and the flow page called the same action `test-run`, and the bulk
 * bars offered Resume for agents that were already running.
 *
 * A type declares its actions once, as candidates carrying a state predicate
 * (`available`), and this module turns candidates into the plain
 * `ResourceAction[]` that `resource-actions` renders. Nothing here renders.
 */
import type { ResourceAction } from '../components/resource-actions';
import {
  hasAnyPermission,
  hasPermission,
  humanizePermission,
  type UserPermissions,
} from '../permissions';

/** What every action context carries. Types extend it with their handlers. */
export interface ActionContext {
  /** From `/auth/users/me`; null or undefined means RBAC is off. */
  permissions?: UserPermissions;
}

/**
 * One action a type can offer, before the state of a particular resource is
 * taken into account.
 */
export interface ActionCandidate<T> extends ResourceAction {
  /**
   * Whether this resource, in the state it is in, can be asked to do this.
   * Omitted means "always", which is the honest answer for Rename or Open.
   */
  available?: (resource: T) => boolean;
  /**
   * Keep an unavailable action on screen, disabled, instead of dropping it.
   * Use it where the absence would be a puzzle ("where did Run now go?") and
   * the tooltip can name the state that has to change first.
   */
  visibleWhenUnavailable?: boolean;
  /** The tooltip shown while the action is unavailable. */
  unavailableTooltip?: string;
  /** Permission (or any of several) the operator needs to be offered this. */
  permission?: string | string[];
}

/** Candidates may be written inline as `condition && {...}` or `null`. */
export type ActionCandidateInput<T> =
  ActionCandidate<T> | null | undefined | false;

const REGISTRY_KEYS = [
  'available',
  'visibleWhenUnavailable',
  'unavailableTooltip',
  'permission',
] as const;

function toResourceAction<T>(candidate: ActionCandidate<T>): ResourceAction {
  const action = { ...candidate } as ActionCandidate<T> &
    Record<string, unknown>;
  for (const key of REGISTRY_KEYS) {
    delete action[key];
  }
  return action as ResourceAction;
}

function permissionAllowed<T>(
  candidate: ActionCandidate<T>,
  permissions: UserPermissions
): boolean {
  if (!candidate.permission) return true;
  return Array.isArray(candidate.permission)
    ? hasAnyPermission(permissions, candidate.permission)
    : hasPermission(permissions, candidate.permission);
}

function permissionTooltip<T>(candidate: ActionCandidate<T>): string {
  const required = Array.isArray(candidate.permission)
    ? candidate.permission
    : [candidate.permission as string];
  return `You need ${required
    .map((permission) => humanizePermission(permission))
    .join(' or ')} to do this.`;
}

/**
 * An action with no way to run is not an action. A view that wires only some
 * of a type's handlers (the agents table has no Talk of its own, the card
 * grid has no Rename) gets the subset it wired, with the same ids as
 * everywhere else, rather than a button that does nothing.
 */
function isWired<T>(candidate: ActionCandidate<T>): boolean {
  return Boolean(candidate.onClick || candidate.href || candidate.render);
}

/**
 * Turn a type's candidates into the actions this resource actually offers.
 *
 * Order is the declared order, except that `separated` (destructive) actions
 * are moved to the end: DESIGN.md "Destructive actions" says Remove sits
 * last, after a gap, and a type module should not have to remember to declare
 * it last to get that.
 */
export function defineActions<T>(
  resource: T,
  candidates: ActionCandidateInput<T>[],
  ctx: ActionContext = {}
): ResourceAction[] {
  const resolved: ResourceAction[] = [];

  for (const candidate of candidates) {
    if (!candidate) continue;
    if (!isWired(candidate)) continue;

    const allowed = permissionAllowed(candidate, ctx.permissions);
    const available = candidate.available
      ? candidate.available(resource)
      : true;

    if (allowed && available) {
      resolved.push(toResourceAction(candidate));
      continue;
    }
    if (!candidate.visibleWhenUnavailable) continue;

    resolved.push({
      ...toResourceAction(candidate),
      disabled: true,
      tooltip: allowed
        ? candidate.unavailableTooltip || candidate.tooltip
        : permissionTooltip(candidate),
    });
  }

  const everyday = resolved.filter((action) => !action.separated);
  const destructive = resolved.filter((action) => action.separated);
  return [...everyday, ...destructive];
}

/**
 * What a whole selection can be asked to do: an action survives only if every
 * selected resource offers it, and offers it enabled.
 *
 * This is the same rule `resource-actions` applies to `actionsSets`, kept
 * here as well because the bulk bar is a different component with its own
 * button shape and cannot take a set of sets.
 */
export function intersectActions(
  sets: readonly ResourceAction[][]
): ResourceAction[] {
  if (sets.length === 0) return [];
  const [first, ...rest] = sets;
  return first
    .filter((action) => !action.disabled)
    .filter((action) =>
      rest.every((set) =>
        set.some((other) => other.id === action.id && !other.disabled)
      )
    );
}

/** Ids only, which is what tests and bulk handlers compare. */
export function actionIds(actions: readonly ResourceAction[]): string[] {
  return actions.map((action) => action.id);
}

/** Whether a resolved set offers this action, enabled. */
export function offersAction(
  actions: readonly ResourceAction[],
  id: string
): boolean {
  return actions.some((action) => action.id === id && !action.disabled);
}
