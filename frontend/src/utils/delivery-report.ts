/**
 * Presentation rules for Delivery Reports, shared by the collection page and
 * the report page.
 *
 * The four numbers a report states are separate denominators and never one
 * score. "Unverified" is an evidence gap: a warning tint described as such,
 * never danger and never counted as a failure.
 */
import type {
  PurposeVerdict,
  Ratio,
  ReportSummary,
} from '../delivery-reports-api';

export interface VerdictChip {
  variant: 'success' | 'warning' | 'neutral';
  label: string;
  title: string;
}

const VERDICT_CHIPS: Record<PurposeVerdict, VerdictChip> = {
  covered: {
    variant: 'success',
    label: 'Covered',
    title: 'Every stated criterion is met, each with cited evidence',
  },
  partial: {
    variant: 'warning',
    label: 'Partial',
    title: 'Some stated criteria are met with evidence',
  },
  unverified: {
    variant: 'warning',
    label: 'Unverified',
    title:
      'Evidence gap: the available evidence cannot decide the criteria. Not a failure.',
  },
  no_criteria: {
    variant: 'neutral',
    label: 'No criteria',
    title: 'The issue states no criteria to check against',
  },
};

/** The chip for a purpose verdict; an unknown or missing verdict is neutral. */
export function verdictChip(verdict: string | null | undefined): VerdictChip {
  if (verdict && verdict in VERDICT_CHIPS) {
    return VERDICT_CHIPS[verdict as PurposeVerdict];
  }
  return { variant: 'neutral', label: 'Not assessed', title: 'Not assessed' };
}

const SOURCE_LABELS: Record<string, string> = {
  audit: 'from the merge audit',
  report: 'assessed by this report',
  cache: 'unchanged since the previous report',
  metadata: 'reported from metadata',
  none: 'not assessed',
};

export function sourceLabel(source: string | null | undefined): string {
  return (source && SOURCE_LABELS[source]) || 'not assessed';
}

export interface Denominator {
  key: string;
  label: string;
  part: number;
  total: number;
  title: string;
}

function count(value: number | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function denominator(
  key: string,
  label: string,
  ratio: Ratio | undefined,
  partKey: 'linked' | 'with_criteria' | 'with_evidence',
  title: string
): Denominator {
  return {
    key,
    label,
    part: count(ratio?.[partKey]),
    total: count(ratio?.total),
    title,
  };
}

/** The three availability ratios, each with its own denominator. */
export function denominators(
  summary: ReportSummary | undefined
): Denominator[] {
  return [
    denominator(
      'linkage',
      'Linked to an issue',
      summary?.linkage,
      'linked',
      'Shipped changes that link an issue, of all shipped changes'
    ),
    denominator(
      'criteria',
      'Closed issues with criteria',
      summary?.criteria_availability,
      'with_criteria',
      'Closed issues that state criteria, of all closed issues'
    ),
    denominator(
      'evidence',
      'Criteria with evidence',
      summary?.evidence_availability,
      'with_evidence',
      'Criteria of closed issues with checkable evidence cited, of all those criteria'
    ),
  ];
}

export const VERDICT_ORDER: readonly PurposeVerdict[] = [
  'covered',
  'partial',
  'unverified',
  'no_criteria',
];

/** Closed-issue verdict counts in a fixed order, zeros included. */
export function verdictCounts(
  summary: ReportSummary | undefined
): { verdict: PurposeVerdict; count: number }[] {
  return VERDICT_ORDER.map((verdict) => ({
    verdict,
    count: count(summary?.closed_verdicts?.[verdict]),
  }));
}

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function day(date: Date, withMonth: boolean): string {
  return withMonth
    ? `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]}`
    : String(date.getUTCDate());
}

/** "17 to 24 Oct", "26 Sep to 3 Oct", or "to 24 Oct" with no start. */
export function periodLabel(
  window: { from?: string | null; to?: string | null } | undefined
): string {
  const start = parse(window?.from);
  const end = parse(window?.to);
  if (!end) return start ? `from ${day(start, true)}` : 'Unknown period';
  if (!start) return `to ${day(end, true)}`;
  const sameMonth =
    start.getUTCMonth() === end.getUTCMonth() &&
    start.getUTCFullYear() === end.getUTCFullYear();
  return `${day(start, !sameMonth)} to ${day(end, true)}`;
}

/** "Run of 24 Oct", named after the end of its window, else when it started. */
export function runLabel(
  window: { to?: string | null } | undefined,
  startedAt: string | null | undefined
): string {
  const date = parse(window?.to) ?? parse(startedAt);
  return date ? `Run of ${day(date, true)}` : 'Run';
}

const FLAG_LABELS: Record<string, string> = {
  sensitive_path: 'Sensitive path',
  hot_path_without_tests: 'Hot path without tests',
  public_api_without_version_note: 'Public API without a version note',
  ui_without_screenshot: 'UI changed without a screenshot',
  migration_without_downgrade: 'Migration without a downgrade',
  file_list_incomplete: 'Incomplete file list',
};

export function flagLabel(flag: string): string {
  if (FLAG_LABELS[flag]) return FLAG_LABELS[flag];
  const words = flag.replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Risk';
}
