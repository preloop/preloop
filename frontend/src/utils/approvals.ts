import type { ApprovalRequest } from '../types';
import { parseUTCDate } from './date';

/**
 * One reading of an approval request's state, shared by the list and the
 * detail page.
 *
 * The backend expires a request with a sweeper, so a row can sit in the
 * database as `pending` long after its `expires_at`. The list already
 * normalised that; the detail page did not, and showed a request that timed
 * out weeks ago as PENDING with live Approve and Deny buttons. Both surfaces
 * now read the clock through these helpers so they cannot disagree.
 */

/** Requests past this margin get the amber countdown: a decision is urgent. */
export const EXPIRING_SOON_MS = 5 * 60 * 1000;

/**
 * The approval-requests list endpoint caps here. A count at this size is a
 * floor, not a total — there may be more waiting past the page.
 */
export const APPROVAL_REQUESTS_PAGE_LIMIT = 100;

/**
 * Label for the post-decision "next waiting" link. When the pending page is
 * full the number is a floor (`100+`), not an authoritative total.
 */
export function formatNextWaitingLabel(
  waitingCount: number,
  fetchedCount: number,
  pageLimit: number = APPROVAL_REQUESTS_PAGE_LIMIT
): string {
  const count =
    fetchedCount >= pageLimit ? `${pageLimit}+` : String(waitingCount);
  return `Next waiting (${count})`;
}

/** True when the request is pending and its expiry (if any) is still ahead. */
export function isUnexpiredPendingRequest(
  request: ApprovalRequest,
  now: number = Date.now()
): boolean {
  if (request.status !== 'pending') {
    return false;
  }
  if (!request.expires_at) {
    return true;
  }
  return parseUTCDate(request.expires_at).getTime() > now;
}

/**
 * Returns the request as it should be rendered: a pending request whose
 * expiry has passed reads as timed out, resolved at its expiry.
 */
export function normalizeApprovalRequest(
  request: ApprovalRequest,
  now: number = Date.now()
): ApprovalRequest {
  if (request.status !== 'pending' || isUnexpiredPendingRequest(request, now)) {
    return request;
  }
  return {
    ...request,
    status: 'expired',
    resolved_at: request.resolved_at || request.expires_at,
  };
}

/** Milliseconds until the request expires; null when it never does. */
export function millisUntilExpiry(
  request: ApprovalRequest,
  now: number = Date.now()
): number | null {
  if (!request.expires_at) return null;
  return parseUTCDate(request.expires_at).getTime() - now;
}

/** True when the request expires within five minutes (amber countdown). */
export function isExpiringSoon(
  request: ApprovalRequest,
  now: number = Date.now()
): boolean {
  const remaining = millisUntilExpiry(request, now);
  return remaining !== null && remaining > 0 && remaining <= EXPIRING_SOON_MS;
}

/**
 * Splits a list into the requests that still need a decision and everything
 * else. The waiting group is ordered by how soon it expires, so the row that
 * will be lost first sits at the top; requests without an expiry come last.
 * History keeps the newest-first order it arrived in.
 */
export function partitionApprovalRequests(
  requests: ApprovalRequest[],
  now: number = Date.now()
): { waiting: ApprovalRequest[]; history: ApprovalRequest[] } {
  const waiting: ApprovalRequest[] = [];
  const history: ApprovalRequest[] = [];
  for (const request of requests) {
    if (isUnexpiredPendingRequest(request, now)) {
      waiting.push(request);
    } else {
      history.push(request);
    }
  }
  waiting.sort((a, b) => {
    const aExpiry = a.expires_at
      ? parseUTCDate(a.expires_at).getTime()
      : Number.POSITIVE_INFINITY;
    const bExpiry = b.expires_at
      ? parseUTCDate(b.expires_at).getTime()
      : Number.POSITIVE_INFINITY;
    return aExpiry - bExpiry;
  });
  return { waiting, history };
}

/** Sentence-case label for a status, with `declined` read as denied. */
export function approvalStatusLabel(status: string): string {
  switch (status) {
    case 'pending':
      return 'Pending';
    case 'approved':
      return 'Approved';
    case 'declined':
      return 'Denied';
    case 'expired':
      return 'Timed out';
    case 'cancelled':
      return 'Cancelled';
    default:
      return status;
  }
}
