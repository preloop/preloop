import { html, nothing, type TemplateResult } from 'lit';
import type { SubscriptionUsageSummary } from '../types';
import { formatUsd, formatUsdExact } from './money';

/**
 * "Without subscriptions": what an agent's spend would have been had its
 * subscription-routed requests been billed at pay-per-use API prices, i.e.
 * the metered spend plus the API-equivalent cost of those requests (D-05).
 *
 * It is an estimate, never a bill, and it follows the Cost page's coverage
 * semantics (#1401): a subscription request without an API-equivalent
 * figure is not counted as $0. When only some requests carry one the sum is
 * a lower bound ("at least"); when none do there is no estimate at all.
 */
export interface SubscriptionEstimate {
  /** Metered spend plus the covered API-equivalent cost; null: no estimate. */
  value: number | null;
  /** True when some subscription requests carry no API-equivalent figure. */
  partial: boolean;
  /** The figure as a cell shows it: "$4.20", "at least $4.20", "No estimate". */
  text: string;
  /** The sentence behind the figure, for a `title`. */
  title: string;
}

const plural = (count: number, noun: string) =>
  `${count.toLocaleString('en-US')} ${noun}${count === 1 ? '' : 's'}`;

export function subscriptionEstimate(
  spend: number | null | undefined,
  block: SubscriptionUsageSummary | null | undefined
): SubscriptionEstimate {
  const metered = Number(spend) || 0;
  if (!block?.request_count)
    return {
      value: metered,
      partial: false,
      text: formatUsd(metered),
      title: 'No subscription requests: the same as the metered spend',
    };
  const requests = block.request_count;
  const covered = block.covered_requests ?? 0;
  if (!covered)
    return {
      value: null,
      partial: true,
      text: 'No estimate',
      title: `None of ${plural(requests, 'subscription request')} carries an API-equivalent price, so there is no estimate`,
    };
  const value = metered + (block.api_equivalent_cost || 0);
  const partial = covered < requests;
  const basis = `Estimate, not a bill: metered spend ${formatUsdExact(metered)} plus the API-equivalent cost of ${partial ? `${covered.toLocaleString('en-US')} of ` : ''}${plural(requests, 'subscription request')}`;
  return {
    value,
    partial,
    text: partial ? `at least ${formatUsd(value)}` : formatUsd(value),
    title: partial
      ? `${basis}. The rest carry no API-equivalent price, so the real figure is higher.`
      : `${basis}.`,
  };
}

/**
 * The estimate as a muted second line under a spend figure, for an agent
 * with subscription traffic; nothing otherwise (the figure would only repeat
 * the spend). The full sentence, coverage included, is in its `title`.
 */
export function renderWithoutSubscriptionsLine(
  estimate: SubscriptionEstimate | null | undefined,
  subscription: SubscriptionUsageSummary | number | null | undefined
): TemplateResult | typeof nothing {
  const requests =
    typeof subscription === 'number'
      ? subscription
      : (subscription?.request_count ?? 0);
  if (!estimate || !requests) return nothing;
  // Compact for a table cell: the "$ est." header already says estimate,
  // and the title carries the full sentence and the coverage.
  const figure =
    estimate.value === null
      ? 'No estimate'
      : `${estimate.partial ? '≥ ' : ''}${formatUsd(estimate.value)}`;
  return html`<span
    class="without-subscriptions"
    data-testid="without-subscriptions"
    title=${`Without subscriptions. ${estimate.title}`}
    style="display:block;color:var(--sl-color-neutral-600);font-size:var(--console-text-meta, 12px);white-space:nowrap"
    >${figure} w/o subs.</span
  >`;
}
