/**
 * US dollar amounts for the console.
 *
 * One formatter instead of a `$${value.toFixed(2)}` per page, so large
 * spends get thousands separators ($12,345.67) and sub-cent amounts read
 * the same everywhere ("< $0.01") instead of $0.000123 on one page and
 * $0.0000 on another. Where a page showed an amount precisely before, it
 * keeps the exact value in a `title` via {@link formatUsdExact}.
 */

const CENTS = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const EXACT = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});

function toAmount(value: number | null | undefined): number {
  const amount = Number(value ?? 0);
  return Number.isFinite(amount) ? amount : 0;
}

/**
 * An amount in dollars and cents, with "< $0.01" for a positive amount
 * below one cent.
 *
 * @param value - The amount in USD; null, undefined and NaN count as zero
 * @returns The formatted amount, e.g. "$12,345.68"
 */
export function formatUsd(value: number | null | undefined): string {
  const amount = toAmount(value);
  if (amount > 0 && amount < 0.01) return '< $0.01';
  return CENTS.format(amount);
}

/**
 * The same amount with up to six decimals, for a tooltip beside a rounded
 * {@link formatUsd} value.
 *
 * @param value - The amount in USD; null, undefined and NaN count as zero
 * @returns The formatted amount, e.g. "$0.000123"
 */
export function formatUsdExact(value: number | null | undefined): string {
  return EXACT.format(toAmount(value));
}
