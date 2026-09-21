import type { AttentionDismissal } from '../api';
import {
  dismissalHidesFingerprint,
  modelAttentionFingerprint,
  modelAttentionItemId,
  unpricedModelAttentionFingerprint,
  unpricedModelAttentionItemId,
  unpricedModelIsMarked,
} from './attention';
import { formatRelativeTime, parseUTCDate } from './date';

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
  modelAlias?: string | null;
  /**
   * Per-alias groups, matching the inbox. When present and non-empty, the
   * row takes the worst of these instead of keying only the newest alias.
   */
  aliasFailures?: ModelAliasFailure[];
  modelId?: string | null;
  credentialsStatus?: string | null;
  credentialsLastError?: string | null;
  credentialsLastErrorCode?: string | null;
  credentialsLastFailedAt?: string | null;
  credentialsLastVerifiedAt?: string | null;
  credentialType?: string | null;
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
  /** Description of the failure, e.g. credential refresh error and failed-at time. */
  reasonText?: string | null;
  /** Remediation instructions tailored to the credential type. */
  remediationText?: string | null;
  credentialsStatus?: string | null;
  credentialsLastError?: string | null;
  credentialsLastFailedAt?: string | null;
  credentialType?: string | null;
}

export function modelCredentialRemediation(
  credentialType: string | null | undefined
): string {
  switch (credentialType) {
    case 'oauth_openai_codex':
      return 'Re-sync from your local Codex login by re-onboarding Codex CLI (`preloop agents onboard "Codex CLI"`).';
    case 'oauth_anthropic_claude_code':
      return 'Re-onboard Claude Code (`preloop agents onboard "Claude Code"`) to refresh credentials.';
    default:
      return 'Rotate the API key for this model.';
  }
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
  const chosen = fingerprintTime(right) > fingerprintTime(left) ? right : left;
  return {
    ...chosen,
    reasonText: chosen.reasonText || left.reasonText || right.reasonText,
    remediationText:
      chosen.remediationText || left.remediationText || right.remediationText,
    credentialsStatus:
      chosen.credentialsStatus ||
      left.credentialsStatus ||
      right.credentialsStatus,
    credentialsLastError:
      chosen.credentialsLastError ||
      left.credentialsLastError ||
      right.credentialsLastError,
    credentialsLastFailedAt:
      chosen.credentialsLastFailedAt ||
      left.credentialsLastFailedAt ||
      right.credentialsLastFailedAt,
    credentialType:
      chosen.credentialType || left.credentialType || right.credentialType,
  };
}

/**
 * Where one alias-group stands, by the Overview / inbox matching rule.
 */
function modelAttentionStateForAlias(
  summary: ModelFailureSummary,
  dismissals: AttentionDismissal[],
  now: Date
): ModelAttentionState {
  const isCredentialFailure = summary.credentialsStatus === 'error';
  const alias = summary.failureAlias || summary.modelAlias;
  const itemId = modelAttentionItemId(alias, summary.providerName);

  let effectiveFailureAt = summary.lastFailureAt;
  if (isCredentialFailure && summary.credentialsLastFailedAt) {
    if (
      !effectiveFailureAt ||
      Date.parse(summary.credentialsLastFailedAt) >
        Date.parse(effectiveFailureAt)
    ) {
      effectiveFailureAt = summary.credentialsLastFailedAt;
    }
  }

  const fingerprint = modelAttentionFingerprint(effectiveFailureAt);
  const dismissal =
    dismissals.find((candidate) => candidate.item_id === itemId) || null;
  const failing = (summary.failedRequests || 0) > 0 || isCredentialFailure;
  const marked = Boolean(
    dismissal && dismissalHidesFingerprint(dismissal, fingerprint, now)
  );
  const markerFailureAt = marked ? null : markerFailureTimestamp(dismissal);

  let reasonText: string | null = null;
  let remediationText: string | null = null;
  if (isCredentialFailure) {
    const parts: string[] = [];
    if (summary.credentialsLastError) {
      parts.push(summary.credentialsLastError);
    }
    if (summary.credentialsLastFailedAt) {
      parts.push(
        `(last ${formatRelativeTime(summary.credentialsLastFailedAt, now)})`
      );
    }
    reasonText = parts.join(' ') || 'Credential refresh failed';
    remediationText = modelCredentialRemediation(summary.credentialType);
  }

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
    dismissable: failing && !marked && Boolean(effectiveFailureAt),
    reasonText,
    remediationText,
    credentialsStatus: summary.credentialsStatus,
    credentialsLastError: summary.credentialsLastError,
    credentialsLastFailedAt: summary.credentialsLastFailedAt,
    credentialType: summary.credentialType,
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
          modelAlias: summary.modelAlias,
          providerName: summary.providerName,
          failedRequests: group.failedRequests,
          lastFailureAt: group.lastFailureAt,
          failedRequestsSince:
            group.failedRequestsSince ?? summary.failedRequestsSince,
          credentialsStatus: summary.credentialsStatus,
          credentialsLastError: summary.credentialsLastError,
          credentialsLastErrorCode: summary.credentialsLastErrorCode,
          credentialsLastFailedAt: summary.credentialsLastFailedAt,
          credentialsLastVerifiedAt: summary.credentialsLastVerifiedAt,
          credentialType: summary.credentialType,
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

/**
 * The two answers offered for a model that has no price. "Fixed" is missing on
 * purpose: the fix for an unpriced model is a price, and a priced model does
 * not derive the item at all, so "fixed" would be a claim nobody can check.
 */
export type UnpricedDismissReason = 'expected' | 'snoozed';

/** What a page knows about one model's unpriced requests. */
export interface UnpricedModelSummary {
  /** Gateway alias the requests carried; the provider name is the fallback. */
  modelAlias?: string | null;
  providerName?: string | null;
  /** Requests in the window that carry no price at all. */
  unpricedRequests: number;
}

/**
 * `quiet`: every request this model served is priced.
 * `marked`: somebody said unpriced is expected here, and that still holds.
 * `unpriced`: unpriced requests nobody has acknowledged.
 */
export type UnpricedAttentionStatus = 'quiet' | 'marked' | 'unpriced';

export interface UnpricedAttentionState {
  /** `model-unpriced:<alias>`, the id a dismissal is stored under. */
  itemId: string;
  /** `unpriced:<alias>`: stable, so one more unpriced request changes nothing. */
  fingerprint: string;
  status: UnpricedAttentionStatus;
  /** The stored dismissal for this model's unpriced requests, active or not. */
  dismissal: AttentionDismissal | null;
  /** True while an active marker covers this model. */
  marked: boolean;
  /** "Unpriced requests marked expected 21 Sep 2026", for a tooltip. */
  markerLabel: string | null;
  /** Is there an unacknowledged unpriced model to offer a Dismiss control for? */
  dismissable: boolean;
  /** Is there an active marker to offer a Restore control for? */
  restorable: boolean;
  unpricedRequests: number;
}

/**
 * Where one model stands on price, by the rule the inbox uses.
 *
 * Deliberately separate from `modelAttentionState`: failures and missing
 * prices are independent facts with independent markers, so a row that is
 * both failing and unpriced needs both before it reads Healthy.
 *
 * @param summary What the page knows about this model's unpriced requests.
 * @param dismissals The account's active dismissals, as the API returns them.
 * @param now Instant to judge a snooze against.
 * @returns The model's unpriced state.
 */
export function unpricedAttentionState(
  summary: UnpricedModelSummary,
  dismissals: AttentionDismissal[],
  now: Date = new Date()
): UnpricedAttentionState {
  const alias = summary.modelAlias;
  const provider = summary.providerName;
  const itemId = unpricedModelAttentionItemId(alias, provider);
  const fingerprint = unpricedModelAttentionFingerprint(alias, provider);
  const dismissal =
    dismissals.find((candidate) => candidate.item_id === itemId) || null;
  const marked = unpricedModelIsMarked(dismissals, alias, provider, now);
  const unpricedRequests = summary.unpricedRequests || 0;
  const unpriced = unpricedRequests > 0;
  return {
    itemId,
    fingerprint,
    status: !unpriced ? 'quiet' : marked ? 'marked' : 'unpriced',
    dismissal,
    marked,
    markerLabel:
      marked && dismissal
        ? `Unpriced requests ${(
            REASON_VERB[dismissal.reason] || 'dismissed'
          ).toLowerCase()} ${formatMarkerDate(dismissal.created_at)}`.trim()
        : null,
    dismissable: unpriced && !marked,
    // Restore is offered wherever the marker is doing work, which is the only
    // place an operator can see that something is being hidden from them.
    restorable: marked,
    unpricedRequests,
  };
}
