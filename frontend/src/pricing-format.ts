/**
 * The one place that turns a catalog price pair into the strings a pricing
 * card prints.
 *
 * Both the client component (`<pricing-card>`) and the server-side render in
 * `pricing-ssr.ts` call this, so a crawler and a visitor can never be shown a
 * different number for the same plan and period.
 *
 * Rules, applied generically from the catalog rather than hand-written per
 * plan:
 *   - Monthly always shows the monthly amount as `$N` with a `/mo` unit.
 *   - Yearly shows the effective monthly rate (`annual / 12`) when that
 *     division is exact, because "$10 /mo billed annually" is the number a
 *     buyer compares against the monthly price.
 *   - When the division is not exact the annual total becomes the headline
 *     instead: `$291.67 /mo` is an ugly, unmemorable price, and rounding it
 *     silently would understate or overstate what is charged.
 *   - `price_label` wins outright so a floor price ("from $30k/yr") is never
 *     rendered as if it were an exact amount.
 */

export interface PriceDisplay {
  /** The large number, already carrying its currency symbol. */
  headline: string;
  /** `/mo`, `/yr`, or empty for a quoted or zero price. */
  unit: string;
  /** The single line under the number. Empty means print nothing. */
  note: string;
}

export interface PricedPlan {
  id?: string;
  price_monthly?: number | null;
  price_annually?: number | null;
  price_label?: string;
  price_note?: string;
  price_note_annual?: string;
}

export type BillingInterval = 'month' | 'year';

/** Whole dollars when the amount is exact, cents when it is not. */
export function money(amount: number): string {
  const digits = Number.isInteger(amount) ? 0 : 2;
  return `$${amount.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

const MONTHS_PER_YEAR = 12;

function nullish(value: number | null | undefined): number | null {
  return value === null || value === undefined ? null : value;
}

export function formatPlanPrice(
  plan: PricedPlan,
  interval: BillingInterval
): PriceDisplay {
  if (plan.price_label) {
    return {
      headline: plan.price_label,
      unit: '',
      note: plan.price_note ?? '',
    };
  }

  const monthly = nullish(plan.price_monthly);
  const annual = nullish(plan.price_annually);

  // '$0', not 'Free': the plan is already named Free right above the number,
  // and no period applies to a price that is never charged.
  if (monthly === 0 && (annual === 0 || annual === null)) {
    return { headline: '$0', unit: '', note: plan.price_note ?? '' };
  }

  const monthlyDisplay = (): PriceDisplay =>
    monthly === null
      ? { headline: 'Custom', unit: '', note: plan.price_note ?? '' }
      : {
          headline: money(monthly),
          unit: '/mo',
          note: plan.price_note ?? 'billed monthly',
        };

  if (interval === 'month' || annual === null) return monthlyDisplay();

  const effective = annual / MONTHS_PER_YEAR;
  if (Number.isInteger(effective)) {
    return {
      headline: money(effective),
      unit: '/mo',
      note: plan.price_note_annual ?? `billed annually (${money(annual)}/yr)`,
    };
  }
  return {
    headline: money(annual),
    unit: '/yr',
    note:
      plan.price_note_annual ??
      `about ${money(Math.round(effective))}/mo, billed annually`,
  };
}

/** The same display flattened to one line, for crawler-visible markup. */
export function formatPlanPriceText(
  plan: PricedPlan,
  interval: BillingInterval
): string {
  const { headline, unit, note } = formatPlanPrice(plan, interval);
  const price = unit ? `${headline} ${unit}` : headline;
  return note ? `${price}, ${note}` : price;
}
