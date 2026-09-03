/**
 * Bounded parallelism for per-item API calls.
 *
 * `Promise.all(items.map(fetch))` sends every request at once. On a page with
 * a long list that is a burst the API answers by checking out one database
 * connection per request; on 2026-09-03 a burst like that emptied the pool
 * and took the API's liveness probe down with it. Where a batch endpoint is
 * not available, a small cap keeps a long list from becoming a spike.
 */

/** Requests in flight at once for console fan-out. Deliberately small. */
export const DEFAULT_FETCH_CONCURRENCY = 4;

/**
 * Map over items with at most `limit` calls in flight, preserving order.
 *
 * @param items - Items to map over.
 * @param limit - Maximum concurrent calls; values below 1 are treated as 1.
 * @param fn - Async mapper, receives the item and its index.
 * @returns Results in the order of `items`.
 */
export async function mapWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>
): Promise<R[]> {
  const results = new Array<R>(items.length);
  if (items.length === 0) {
    return results;
  }

  const workerCount = Math.min(Math.max(1, Math.floor(limit)), items.length);
  let next = 0;

  const worker = async (): Promise<void> => {
    while (next < items.length) {
      const index = next++;
      results[index] = await fn(items[index], index);
    }
  };

  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return results;
}
