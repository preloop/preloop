/**
 * Counts as DESIGN.md "Numbers" states them: whole under 1000, compact at or
 * above (`70.2K`, `1.2M`), with the exact figure for a `title`. Raw counts
 * such as "272950 requests" were printed on half a dozen pages because each
 * page formatted its own numbers.
 */
export function formatCount(value: number | null | undefined): string {
  const amount = Number(value ?? 0);
  if (!Number.isFinite(amount)) return '0';
  if (Math.abs(amount) < 1000) return String(Math.round(amount));
  return new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(amount);
}

/** The exact count with thousands separators, for `title`. */
export function formatCountExact(value: number | null | undefined): string {
  const amount = Number(value ?? 0);
  if (!Number.isFinite(amount)) return '0';
  return new Intl.NumberFormat('en-US').format(Math.round(amount));
}
