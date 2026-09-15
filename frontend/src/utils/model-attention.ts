import type { AttentionDismissal } from '../api';
import {
  dismissalHidesFingerprint,
  modelAttentionFingerprint,
  modelAttentionItemId,
} from './attention';
import { parseUTCDate } from './date';

/**
 * The Models page and the model detail page say "needs attention" from a
 * usage summary, while the Overview and /console/attention say it from a list
 * of failed gateway calls. Both have to agree about which item a dismissal
 * refers to and whether that dismissal still applies, or an operator who
 * marked a model fixed on one page keeps being told about it on another.
 *
 * The inbox keeps one item per alias. A Models row is one configured model,
 * so it is Attention if any of that model's alias-group items is still
 * unacknowledged, and Healthy only when every matching item is dismissed
 * (or there is no failure). The item id, fingerprint and matching rule
 * still all come from `utils/attention.ts`.
 */

/** One inbox-grouped alias of failures for a Models row. */
export interface ModelAliasFailure {
  /**
   * The alias the failing calls carried. The gateway alias configured
   * today is the fallback: a model renamed at the gateway keeps failing
   * under the name the failures were recorded with.
   */
  failureAlias?: string | null;
  /** Newest failed request for this alias, the fingerprint's material. */
  lastFailureAt?: string | null;
  /** Failed requests for this alias in the page's window. */
  failedRequests: number;
  /**
   * Failures newer than the marker, when the caller asked the API for them
   * with `failed_since`. Null when it did not.
   */
  failedRequestsSince?: number | null;
}

/** What a Models row or a detail page knows about one model's failures. */
export interface ModelFailureSummary extends ModelAliasFailure {
  providerName?: string | null;
  /**
   * Per-alias groups, matching the inbox. When present and non-empty, the
   * row takes the worst of these instead of keying only the newest alias.
   */
  aliasFailures?: ModelAliasFailure[];
}

export type ModelAttentionStatus = 'quiet' | 'marked' | 'failing';

export interface ModelAttentionState {
  /** `model:<alias>`, the id a dismissal is stored under. */
  itemId: string;
  /** `last:<newest failure>`, what a dismissal is compared against. */
  fingerprint: string;
  /**
   * `quiet`: nothing failed in the window.
   * `marked`: an active dismissal still matches, so the row is not flagged.
   * `failing`: failures the operator has not acknowledged.
   */
  status: ModelAttentionStatus;
  /** The stored dismissal for this model, matching or not. */
  dismissal: AttentionDismissal | null;
  /** True while a dismissal of any reason is in force for this fingerprint. */
  marked: boolean;
  /**
   * Failures newer than the marker, when there is a stale marker and the API
   * was asked to split the count. Null when there is nothing to split.
   */
  failuresSinceMarker: number | null;
  /** The moment the marker was taken against, from its fingerprint. */
  markerFailureAt: string | null;
  /** "Marked fixed 3 Sep 2026", for a tooltip. */
  markerLabel: string | null;
  /** Is there an unacknowledged failure here to offer a Dismiss control for? */
  dismissable: boolean;
}

/** The three reasons the console offers, in one place. */
export type ModelDismissReason = 'expected' | 'snoozed' | 'fixed';

const REASON_VERB: Record<string, string> = {
  expected: 'Marked expected',
  snoozed: 'Snoozed',
  fixed: 'Marked fixed',
};

/** "since fix", "since snooze", "since it was marked expected". */
export function markerSinceLabel(reason: string | undefined): string {
  switch (reason) {
    case 'snoozed':
      return 'since snooze';
    case 'expected':
      return 'since marked expected';
    default:
      return 'since fix';
  }
}

/** The failure the marker was taken against, read back out of its fingerprint. */
export function markerFailureTimestamp(
  dismissal: AttentionDismissal | null | undefined
): string | null {
  const fingerprint = dismissal?.fingerprint || '';
  if (!fingerprint.startsWith('last:')) {
    return null;
  }
  return fingerprint.slice('last:'.length) || null;
}

function formatMarkerDate(value: string | undefined): string {
  if (!value) {
    return '';
  }
  return parseUTCDate(value).toLocaleDateString();
}

function fingerprintTime(state: ModelAttentionState): number {
  if (!state.fingerprint.startsWith('last:')) {
    return 0;
  }
  const ms = Date.parse(state.fingerprint.slice('last:'.length));
  return Number.isNaN(ms) ? 0 : ms;
}

function newestAliasState(
  left: ModelAttentionState,
  right: ModelAttentionState
): ModelAttentionState {
  return fingerprintTime(right) > fingerprintTime(left) ? right : left;
}

/**
 * Where one alias-group stands, by the Overview / inbox matching rule.
 */
function modelAttentionStateForAlias(
  summary: ModelFailureSummary,
  dismissals: AttentionDismissal[],
  now: Date
): ModelAttentionState {
  const itemId = modelAttentionItemId(
    summary.failureAlias,
    summary.providerName
  );
  const fingerprint = modelAttentionFingerprint(summary.lastFailureAt);
  const dismissal =
    dismissals.find((candidate) => candidate.item_id === itemId) || null;
  const failing = (summary.failedRequests || 0) > 0;
  const marked = Boolean(
    dismissal && dismissalHidesFingerprint(dismissal, fingerprint, now)
  );
  const markerFailureAt = marked ? null : markerFailureTimestamp(dismissal);
  return {
    itemId,
    fingerprint,
    status: !failing ? 'quiet' : marked ? 'marked' : 'failing',
    dismissal,
    marked,
    // Only a marker that no longer matches has anything "since" it: while it
    // matches, the count since it is zero by construction.
    failuresSinceMarker:
      !marked && markerFailureAt !== null
        ? (summary.failedRequestsSince ?? null)
        : null,
    markerFailureAt,
    markerLabel: dismissal
      ? `${REASON_VERB[dismissal.reason] || 'Dismissed'} ${formatMarkerDate(
          dismissal.created_at
        )}`.trim()
      : null,
    // Dismissing a model means "quiet until it fails again", so it needs a
    // failure to point at that is not already acknowledged. A server too old
    // to report one gets no controls rather than a dismissal the other pages
    // would never match.
    dismissable: failing && !marked && Boolean(summary.lastFailureAt),
  };
}

/**
 * Where one model stands: flagged, acknowledged, or quiet.
 *
 * When the summary carries per-alias groups, the row is Attention if any
 * group is unacknowledged. Healthy only when every matching alias item is
 * dismissed. Dismiss still writes the newest unacknowledged alias, which
 * is the inbox item a single-alias row already wrote.
 *
 * @param summary What the page knows about this model's failures.
 * @param dismissals The account's active dismissals, as the API returns them.
 * @param now Instant to judge a snooze against.
 * @returns The row's attention state.
 */
export function modelAttentionState(
  summary: ModelFailureSummary,
  dismissals: AttentionDismissal[],
  now: Date = new Date()
): ModelAttentionState {
  const groups =
    summary.aliasFailures && summary.aliasFailures.length > 0
      ? summary.aliasFailures.map((group) => ({
          failureAlias: group.failureAlias,
          providerName: summary.providerName,
          failedRequests: group.failedRequests,
          lastFailureAt: group.lastFailureAt,
          failedRequestsSince:
            group.failedRequestsSince ?? summary.failedRequestsSince,
        }))
      : [summary];
  const states = groups.map((group) =>
    modelAttentionStateForAlias(group, dismissals, now)
  );
  const failing = states.filter((state) => state.status === 'failing');
  if (failing.length > 0) {
    return failing.reduce(newestAliasState);
  }
  const marked = states.filter((state) => state.status === 'marked');
  if (marked.length > 0) {
    return marked.reduce(newestAliasState);
  }
  return states[0];
}
