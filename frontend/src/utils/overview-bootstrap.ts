import { REST_API_PREFIX } from '../api-path';
/** A single Overview load, never a shared or persistent response cache. */
interface Result {
  url: string;
  status: number;
  body: string;
  headers: Record<string, string>;
}

function key(url: string): string {
  const parsed = new URL(url, window.location.origin);
  parsed.searchParams.sort();
  return parsed.pathname + '?' + parsed.searchParams.toString();
}

export interface OverviewLoad {
  read(url: string, token: string): Promise<Response | null>;
  close(): void;
}

let active: OverviewLoad | null = null;
let shell: OverviewLoad | null = null;

export function beginOverviewShell(): void {
  if (!['/console', '/console/'].includes(window.location.pathname)) return;
  if (shell || !localStorage.getItem('accessToken')) return;
  shell = createLoad(REST_API_PREFIX + '/overview/shell');
}

export function closeOverviewShell(): void {
  shell?.close();
  shell = null;
}

/** Writes and authentication changes make every snapshot a read of the past. */
export function invalidateOverviewLoads(): void {
  active?.close();
  closeOverviewShell();
}

export function readOverviewInput(
  url: string,
  token: string
): Promise<Response | null> | null {
  if (!['/console', '/console/'].includes(window.location.pathname)) {
    return null;
  }
  const parsed = new URL(url, window.location.origin);
  if (parsed.origin !== window.location.origin) return null;
  const path = parsed.pathname.replace(REST_API_PREFIX + '/', '');
  if (path === 'features' && !shell) beginOverviewShell();
  // These independent reads start immediately beside the bootstrap stream.
  if (['cost/timeseries', 'audit-logs/grouped'].includes(path)) return null;
  const currentShell = shell;
  const currentLoad = active;
  if (!currentShell && !currentLoad) return null;
  return (async () => {
    const shellResult = await currentShell?.read(url, token);
    if (shellResult) return shellResult;
    return currentLoad?.read(url, token) ?? null;
  })();
}

export function beginOverviewLoad(
  params: {
    start_date: string;
    prior_start: string;
    prior_end: string;
    now: string;
  },
  phase: 'full' | 'gateway' = 'full'
): OverviewLoad {
  active?.close();
  active = createLoad(
    REST_API_PREFIX +
      '/overview/bootstrap?' +
      new URLSearchParams({ ...params, phase })
  );
  return active;
}

function createLoad(endpoint: string): OverviewLoad {
  const token = localStorage.getItem('accessToken');
  const controller = new AbortController();
  const responses = new Map<string, Result>();
  const waiters = new Map<string, (result: Result | null) => void>();
  const pending = new Map<string, Promise<Result | null>>();
  const manifestKeys = new Set<string>();
  let closed = false;
  let resolveManifest!: () => void;
  const manifest = new Promise<void>((resolve) => {
    resolveManifest = resolve;
  });
  const finish = () => {
    closed = true;
    resolveManifest();
    for (const resolve of waiters.values()) resolve(null);
    waiters.clear();
  };
  const load: OverviewLoad = {
    async read(url, callerToken) {
      if (closed || !token || callerToken !== token) return null;
      await manifest;
      // Only inputs declared by this stream belong to its cancellation scope.
      // Keep keys (not response data) after close so pending callers can tell
      // unrelated ordinary reads from canceled snapshot reads.
      if (!manifestKeys.has(key(url))) return null;
      if (closed || callerToken !== localStorage.getItem('accessToken')) {
        throw new DOMException('Overview load canceled', 'AbortError');
      }
      const result = responses.get(key(url)) ?? (await pending.get(key(url)));
      if (closed || token !== localStorage.getItem('accessToken')) {
        throw new DOMException('Overview load canceled', 'AbortError');
      }
      return result
        ? new Response(result.body, {
            status: result.status,
            headers: result.headers,
          })
        : null;
    },
    close() {
      finish();
      controller.abort();
      responses.clear();
      pending.clear();
      if (active === load) active = null;
    },
  };
  void (async () => {
    try {
      const response = await fetch(endpoint, {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
        cache: 'no-store',
      });
      if (!response.ok || !response.body) return;
      const reader = response.body
        .pipeThrough(new TextDecoderStream())
        .getReader();
      let buffer = '';
      while (!closed) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += chunk.value;
        let newline: number;
        while ((newline = buffer.indexOf('\n')) !== -1) {
          const line = JSON.parse(buffer.slice(0, newline)) as Result & {
            paths?: string[];
          };
          buffer = buffer.slice(newline + 1);
          if (line.paths) {
            for (const path of line.paths) {
              manifestKeys.add(key(path));
              pending.set(
                key(path),
                new Promise((resolve) => waiters.set(key(path), resolve))
              );
            }
            resolveManifest();
          } else {
            responses.set(key(line.url), line);
            waiters.get(key(line.url))?.(line);
            waiters.delete(key(line.url));
          }
        }
      }
    } catch {
      // Older deployments and interrupted streams use ordinary route reads.
    } finally {
      resolveManifest();
      for (const resolve of waiters.values()) resolve(null);
      waiters.clear();
    }
  })();
  return load;
}
