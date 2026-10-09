/** Replace list filters without losing deep links, unrelated parameters, or hash. */
export function replaceListFilters(
  filters: Record<string, string | string[] | null>
): void {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(filters)) {
    url.searchParams.delete(key);
    if (Array.isArray(value)) {
      for (const item of value) if (item) url.searchParams.append(key, item);
    } else if (value) {
      url.searchParams.set(key, value);
    }
  }
  window.history.replaceState(
    window.history.state,
    '',
    url.pathname + url.search + url.hash
  );
}

/** Ignore malformed URL dates before they reach API timestamp conversion. */
export function validFilterDate(value: string | null): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return '';
  const parsed = new Date(value + 'T00:00:00Z');
  return !Number.isNaN(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === value
    ? value
    : '';
}
