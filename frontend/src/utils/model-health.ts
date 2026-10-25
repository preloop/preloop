/**
 * The health label a model carries on the Models list and on its detail page.
 *
 * One rule for both pages, so a model cannot read "Acknowledged" on the list
 * and "Healthy" on its own page for the same window (#1956).
 */

/**
 * Failure rate in the window at or above which a quiet model is not called
 * Healthy. An acknowledged failure keeps the model out of the attention
 * count, but "Healthy" over 50 failures in 73 requests is a claim the
 * numbers beside it contradict.
 */
export const HEALTHY_FAILURE_RATE = 0.05;

export type ModelHealthLabel =
  'Attention' | 'Idle' | 'Acknowledged' | 'Healthy';
export type ModelHealthVariant = 'success' | 'warning' | 'neutral';

/** The counts and the open claim the label is decided from. */
export interface ModelHealthInput {
  /** A failure or a missing price nobody has answered yet. */
  needsAttention: boolean;
  totalRequests: number | null | undefined;
  failedRequests: number | null | undefined;
}

/** True when the window's failure rate is too high to call the model Healthy. */
export function failsOften(
  failedRequests: number | null | undefined,
  totalRequests: number | null | undefined
): boolean {
  const total = Number(totalRequests || 0);
  const failed = Number(failedRequests || 0);
  return total > 0 && failed > 0 && failed / total >= HEALTHY_FAILURE_RATE;
}

/**
 * "Healthy" covers an acknowledged failure or an expected missing price too,
 * as long as the failures stay under the threshold: the operator said this
 * one is handled, and the marker is on the badge's title so the claim stays
 * checkable. Above the threshold the model reads "Acknowledged".
 */
export function modelHealthLabel(input: ModelHealthInput): ModelHealthLabel {
  if (input.needsAttention) return 'Attention';
  if (!Number(input.totalRequests || 0)) return 'Idle';
  if (failsOften(input.failedRequests, input.totalRequests)) {
    return 'Acknowledged';
  }
  return 'Healthy';
}

/** The badge variant for a label: only Healthy is green. */
export function modelHealthVariant(
  label: ModelHealthLabel
): ModelHealthVariant {
  if (label === 'Attention') return 'warning';
  if (label === 'Healthy') return 'success';
  return 'neutral';
}

/**
 * "N of M requests failed in <window>" when the failures are too many to call
 * the model Healthy, otherwise an empty string. Exact counts: it is a title.
 */
export function modelFailureShareText(
  failedRequests: number | null | undefined,
  totalRequests: number | null | undefined,
  windowLabel: string
): string {
  if (!failsOften(failedRequests, totalRequests)) return '';
  const format = (value: number | null | undefined) =>
    Intl.NumberFormat().format(Number(value || 0));
  const window = windowLabel ? ` in ${windowLabel}` : '';
  return `${format(failedRequests)} of ${format(totalRequests)} requests failed${window}`;
}
