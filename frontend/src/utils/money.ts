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

/** Provider invoices can be denominated in a currency other than USD. */
export function formatCurrencyAmount(value: number, currency = 'USD'): string {
  if (currency.toUpperCase() === 'USD') return formatUsd(value);
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(toAmount(value));
  } catch (error) {
    if (error instanceof RangeError) return 'Unavailable';
    throw error;
  }
}

/** Exact provider amount, preserving the invoice currency. */
export function formatCurrencyAmountExact(
  value: number,
  currency = 'USD'
): string {
  if (currency.toUpperCase() === 'USD') return formatUsdExact(value);
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 6,
    }).format(toAmount(value));
  } catch (error) {
    if (error instanceof RangeError) return 'Unavailable';
    throw error;
  }
}

/** Billing providers publish cents; missing amounts are unavailable. */
export function formatCurrencyCents(
  cents: number | null | undefined,
  currency = 'USD'
): string {
  if (cents == null || !Number.isFinite(cents)) return 'Unavailable';
  return formatCurrencyAmount(cents / 100, currency);
}

/** Exact cents-based invoice amount for a tooltip. */
export function formatCurrencyCentsExact(
  cents: number | null | undefined,
  currency = 'USD'
): string {
  if (cents == null || !Number.isFinite(cents)) return 'Unavailable';
  return formatCurrencyAmountExact(cents / 100, currency);
}
