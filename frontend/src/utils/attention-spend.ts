/**
 * Spend outlier cards (#960) for the Attention inbox.
 *
 * The server evaluates the rules once a day (and sessions periodically) and
 * records each finding once. This module only turns those findings into
 * attention items, so the rules and the "fires once" guarantee live in one
 * place.
 *
 * Dismissal follows the rest of the inbox, with one addition. The item id is
 * stable per rule and user (`spend:<rule>:<user id>`, or the session id for
 * the session rule) and the fingerprint names the UTC day. A dismissal hides
 * the card while the fingerprint is unchanged, so a user who is still an
 * outlier the next day gets a new card. A snooze, unlike other kinds, keeps
 * the card hidden until it runs out even when the day moves on: that is what
 * "remind me in a week" means for a standing spend pattern.
 */
import type { SpendOutlierFinding } from '../spend-outliers-api';
import type { AttentionItem } from './attention';

/** What the expanded row shows. */
export interface AttentionSpendEvidence {
  rule: SpendOutlierFinding['rule'];
  ruleLabel: string;
  userName: string;
  day: string;
  spendUsd: number | null;
  medianUsd: number | null;
  multiple: number | null;
  thresholdMultiple: number | null;
  model: string | null;
  share: number | null;
  previousShare: number | null;
  thresholdShare: number | null;
  thresholdUsd: number | null;
  sessionId: string | null;
  sessionTitle: string | null;
  importedUsd: number;
  importedSources: string[];
}

function money(value: number | null | undefined): string {
  return `$${Number(value || 0).toFixed(2)}`;
}

function percent(value: number | null | undefined): string {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** "includes $15.00 of imported copilot spend, not metered by the gateway" */
export function importedSpendNote(finding: SpendOutlierFinding): string {
  const imported = Number(finding.details?.imported_usd || 0);
  if (!(imported > 0)) {
    return '';
  }
  const sources = (finding.details?.imported_sources || []).join(', ');
  return `includes ${money(imported)} of imported ${
    sources || 'usage'
  } spend, not metered by the gateway`;
}

function spendDetail(finding: SpendOutlierFinding): string {
  const details = finding.details || {};
  const day = `${finding.day} (UTC)`;
  let text: string;
  if (finding.rule === 'daily_spend') {
    text = `${money(details.spend_usd)} on ${day}, ${Number(
      details.multiple || 0
    ).toFixed(1)}x the 28-day median of ${money(details.median_usd)}`;
  } else if (finding.rule === 'model_mix') {
    text = `${details.model || 'A top-tier model'} was ${percent(
      details.share
    )} of spend on ${day} and ${percent(details.previous_share)} the day before`;
  } else {
    const session = finding.session_title
      ? `Session "${finding.session_title}"`
      : 'One session';
    text = `${session} cost ${money(details.spend_usd)}, over the ${money(
      details.threshold_usd
    )} threshold (${day})`;
  }
  const note = importedSpendNote(finding);
  return note ? `${text} · ${note}` : text;
}

function spendHref(finding: SpendOutlierFinding): string {
  if (finding.rule === 'session_cost' && finding.runtime_session_id) {
    return `/console/runtime-sessions?sessionId=${encodeURIComponent(
      finding.runtime_session_id
    )}`;
  }
  return '/console/cost';
}

/** One card per open finding: who, which rule, the numbers and the day. */
export function spendOutlierItems(
  findings: readonly SpendOutlierFinding[]
): AttentionItem[] {
  return findings.map((finding) => {
    const details = finding.details || {};
    const evidence: AttentionSpendEvidence = {
      rule: finding.rule,
      ruleLabel: finding.rule_label,
      userName: finding.user_name,
      day: finding.day,
      spendUsd: numberOrNull(details.spend_usd),
      medianUsd: numberOrNull(details.median_usd),
      multiple: numberOrNull(details.multiple),
      thresholdMultiple: numberOrNull(details.threshold_multiple),
      model: details.model || null,
      share: numberOrNull(details.share),
      previousShare: numberOrNull(details.previous_share),
      thresholdShare: numberOrNull(details.threshold_share),
      thresholdUsd: numberOrNull(details.threshold_usd),
      sessionId: finding.runtime_session_id,
      sessionTitle: finding.session_title,
      importedUsd: Number(details.imported_usd || 0),
      importedSources: details.imported_sources || [],
    };
    return {
      id: finding.item_id,
      kind: 'spend' as const,
      severity: 'warning' as const,
      title: `${finding.user_name} · ${finding.rule_label}`,
      detail: spendDetail(finding),
      href: spendHref(finding),
      at: finding.detected_at || null,
      fingerprint: finding.fingerprint,
      dismissable: true,
      snoozeHidesNewFingerprints: true,
      evidence: { spendOutlier: evidence },
    };
  });
}
