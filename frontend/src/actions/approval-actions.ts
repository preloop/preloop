/**
 * What an approval request offers, by status and expiry.
 *
 * The list row, the bulk bar and the request page each spelled out "pending,
 * not expired, not a question" in their own words. The predicate lives here
 * now; the surfaces keep their own rendering (the request page decides with a
 * comment box and an "always allow" checkbox, which is a form, not a button
 * row) but they all ask this module what is on offer.
 */
import type { ApprovalRequest } from '../types';
import { isUnexpiredPendingRequest } from '../utils/approvals';
import type { ResourceAction } from '../components/resource-actions';
import { defineActions, type ActionContext } from './registry';

export interface ApprovalActionContext extends ActionContext {
  /** True while this request's decision is being posted. */
  busy?: boolean;
  /** Re-read at click time, so a row cannot decide a request that expired. */
  now?: number;
  onApprove?: (request: ApprovalRequest) => void;
  onDeny?: (request: ApprovalRequest) => void;
  /** List rows link to the request page; the page itself does not. */
  includeDetails?: boolean;
}

/** A question is answered in its own panel, not approved or denied. */
export function isApprovalQuestion(request: ApprovalRequest): boolean {
  return request.is_question === true;
}

/** Pending, unexpired and not a question: the requests a person can decide. */
export function isDecidableRequest(
  request: ApprovalRequest,
  now: number = Date.now()
): boolean {
  return (
    isUnexpiredPendingRequest(request, now) && !isApprovalQuestion(request)
  );
}

export function approvalDetailUrl(request: ApprovalRequest): string {
  return `/console/approval/${encodeURIComponent(request.id)}`;
}

export function approvalActions(
  request: ApprovalRequest,
  ctx: ApprovalActionContext = {}
): ResourceAction[] {
  const now = ctx.now ?? Date.now();
  const busy = ctx.busy === true;
  return defineActions(
    request,
    [
      {
        id: 'approve',
        label: 'Approve',
        icon: 'check-lg',
        variant: 'success',
        loading: busy,
        disabled: busy,
        available: (item) => isDecidableRequest(item, now),
        onClick: ctx.onApprove ? () => ctx.onApprove!(request) : undefined,
      },
      // Denying stops the agent, so it confirms first (DESIGN.md).
      {
        id: 'deny',
        label: 'Deny',
        icon: 'x-lg',
        variant: 'danger',
        outline: true,
        disabled: busy,
        available: (item) => isDecidableRequest(item, now),
        onClick: ctx.onDeny ? () => ctx.onDeny!(request) : undefined,
      },
      ctx.includeDetails
        ? {
            id: 'details',
            // A waiting request is opened to decide it, a settled one to read
            // it, and the link says which.
            label: isUnexpiredPendingRequest(request, now) ? 'Details' : 'View',
            href: approvalDetailUrl(request),
          }
        : null,
    ],
    ctx
  );
}
