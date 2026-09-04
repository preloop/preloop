/**
 * What the Overview costs to open.
 *
 * The console's first screen had grown to roughly forty requests before it
 * showed a number, and every websocket event replayed most of them. This spec
 * is the measurement that keeps that from coming back: it counts what the page
 * asks for, how much of it is the same question twice, how deep the request
 * chain goes, what a burst of gateway traffic costs, and how long the main
 * thread is blocked while all of that happens.
 *
 * ---------------------------------------------------------------------------
 * HOW TO RUN
 * ---------------------------------------------------------------------------
 * Same local harness as tests/e2e/custom-agent-session.spec.ts:
 *
 *   1. Postgres.
 *   2. cd preloop/backend
 *      python -m tests.e2e_support.fake_upstream --port 8081
 *   3. INIT_TEST_DATA=true TESTING=true python -m preloop.server        # :8000
 *      PRELOOP_E2E_FAKE_UPSTREAM=http://127.0.0.1:8081/v1 \
 *        python -m tests.e2e_support.seed_gateway_model
 *   4. Give the account something to show. An empty account renders empty
 *      cards and measures nothing: loop a few hundred completions through the
 *      gateway with the `preloop-fake` alias, and run a few flows.
 *   5. cd preloop/frontend && npx playwright test --project=e2e-ci \
 *        tests/e2e/overview-perf.spec.ts
 *
 * The frontend dev server on :5173 is started by Playwright's `webServer`.
 *
 * Set PRELOOP_PERF_RUNS to change the sample size (default 3; the report in
 * factory/reports uses 5). Numbers are printed per run and as a median so a
 * regression can be read off the console output, not just the pass or fail.
 */

import { test, expect, type Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const IS_STAGING = process.env.PRELOOP_E2E_TARGET === 'staging';
const RUNS = Number(process.env.PRELOOP_PERF_RUNS || 3);
const GATEWAY_MODEL = process.env.PRELOOP_E2E_MODEL || 'preloop-fake';

/**
 * Budgets. These are the numbers section 4 of the Overview performance report
 * asks for; a change that crosses one of them is a product decision, not an
 * accident, so it should move this line deliberately.
 */
const BUDGET = {
  firstUsableMs: 1000,
  coldRequests: 20,
  duplicateUrls: 0,
  longestChain: 2,
  requestsAfterBurst: 12,
  longTaskTotalMs: 200,
};

interface Creds {
  username: string;
  password: string;
}

function resolveCreds(): Creds {
  const envUser = process.env.PRELOOP_E2E_USERNAME;
  const envPass = process.env.PRELOOP_E2E_PASSWORD;
  if (envUser && envPass) {
    return { username: envUser, password: envPass };
  }
  const credsPath = path.resolve(process.cwd(), 'tmp', 'staging-creds.json');
  if (fs.existsSync(credsPath)) {
    const raw = JSON.parse(fs.readFileSync(credsPath, 'utf-8'));
    if (raw.username && raw.password) {
      return { username: raw.username, password: raw.password };
    }
  }
  return { username: 'admin', password: 'admin' };
}

/** Log in through the real /login UI and land on /console. */
async function login(page: Page, creds: Creds): Promise<void> {
  await page.goto('/login');
  const username = page.locator('sl-input[name="username"]');
  const password = page.locator('sl-input[name="password"]');
  await username.waitFor({ state: 'visible' });
  await username.click();
  await page.keyboard.type(creds.username);
  await password.click();
  await page.keyboard.type(creds.password);
  await page.locator('sl-button[type="submit"]').click();
  await page.waitForURL(/\/console/, { timeout: 30_000 });
  await expect
    .poll(
      async () => page.evaluate(() => localStorage.getItem('accessToken')),
      {
        timeout: 30_000,
      }
    )
    .not.toBeNull();
}

interface ApiCall {
  url: string;
  start: number;
  end: number;
}

interface RunResult {
  firstUsableMs: number;
  inventoryReadyMs: number;
  requests: number;
  duplicates: string[];
  longestChain: number;
  longTaskCount: number;
  longTaskTotalMs: number;
}

/** Strip the moving parts so two asks for the same thing compare equal. */
function normalise(url: string): string {
  const parsed = new URL(url);
  const params = new URLSearchParams(parsed.search);
  // Range boundaries are computed per call and differ by milliseconds; the
  // question is still the same question.
  for (const key of ['start_date', 'end_date', '_']) {
    if (params.has(key)) {
      params.set(key, 'T');
    }
  }
  params.sort();
  return `${parsed.pathname}?${params.toString()}`;
}

/**
 * The longest chain of requests that had to happen one after another: how many
 * round trips deep the load is. A request counts as a link when it started
 * after another one finished.
 */
function longestChain(calls: ApiCall[]): number {
  const ordered = [...calls].sort((a, b) => a.start - b.start);
  const depth: number[] = [];
  let longest = 0;
  ordered.forEach((call, index) => {
    let best = 1;
    for (let prior = 0; prior < index; prior += 1) {
      if (ordered[prior].end <= call.start) {
        best = Math.max(best, depth[prior] + 1);
      }
    }
    depth[index] = best;
    longest = Math.max(longest, best);
  });
  return longest;
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function p95(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[
    Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)
  ];
}

/** Watch the main thread and the API traffic for one load of /console. */
async function measureLoad(page: Page, cold: boolean): Promise<RunResult> {
  const calls: ApiCall[] = [];
  const started = new Map<string, number>();
  const onRequest = (request: { url: () => string }) => {
    if (!request.url().includes('/api/v1')) return;
    started.set(request.url(), Date.now());
  };
  const onResponse = (response: { url: () => string }) => {
    const url = response.url();
    if (!url.includes('/api/v1')) return;
    const start = started.get(url);
    if (start === undefined) return;
    calls.push({ url, start, end: Date.now() });
  };
  page.on('request', onRequest);
  page.on('response', onResponse);

  await page.addInitScript(() => {
    const store = window as unknown as { __longTasks: number[] };
    store.__longTasks = [];
    try {
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          store.__longTasks.push(entry.duration);
        }
      }).observe({ entryTypes: ['longtask'] });
    } catch {
      // Long tasks are not observable in this browser; the rest still runs.
    }
  });

  if (cold) {
    await page.evaluate(() => sessionStorage.clear());
    await page.goto('/console');
  } else {
    await page.reload();
  }

  // First usable: the gateway card shows a request count instead of a
  // skeleton. Read the page's own clock so navigation is the zero point.
  const firstUsableMs = await page
    .waitForFunction(
      () => {
        const view = document
          .querySelector('lit-app')
          ?.shadowRoot?.querySelector('dashboard-view');
        const stats = view?.shadowRoot?.querySelector('.plane-stats');
        const text = stats?.textContent || '';
        return /\d[\d.,KM]*\s+requests/.test(text) ? performance.now() : false;
      },
      undefined,
      { timeout: 30_000 }
    )
    .then((handle) => handle.jsonValue() as Promise<number>);

  const inventoryReadyMs = await page
    .waitForFunction(
      () => {
        const view = document
          .querySelector('lit-app')
          ?.shadowRoot?.querySelector('dashboard-view');
        const card = view?.shadowRoot?.querySelector('inventory-card');
        const rows =
          card?.shadowRoot?.querySelectorAll('tbody tr:not(.skeleton-row)') ||
          [];
        return rows.length > 0 ? performance.now() : false;
      },
      undefined,
      { timeout: 30_000 }
    )
    .then((handle) => handle.jsonValue() as Promise<number>);

  // Let the deferred pass finish before counting: a load that defers work is
  // only cheaper if the deferred work is cheaper too.
  await page.waitForTimeout(3000);

  const longTasks = await page.evaluate(
    () => (window as unknown as { __longTasks: number[] }).__longTasks || []
  );

  page.off('request', onRequest);
  page.off('response', onResponse);

  const seen = new Map<string, number>();
  for (const call of calls) {
    const key = normalise(call.url);
    seen.set(key, (seen.get(key) || 0) + 1);
  }
  const duplicates = [...seen.entries()]
    .filter(([, count]) => count > 1)
    .map(([key, count]) => `${key} x${count}`);

  return {
    firstUsableMs,
    inventoryReadyMs,
    requests: calls.length,
    duplicates,
    longestChain: longestChain(calls),
    longTaskCount: longTasks.length,
    longTaskTotalMs: longTasks.reduce((sum, value) => sum + value, 0),
  };
}

test.describe('Overview performance', () => {
  test.skip(IS_STAGING, 'Timings are only meaningful against the local stack.');
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(240_000);

  test('opens quickly, without asking twice', async ({ page }) => {
    await login(page, resolveCreds());

    const cold: RunResult[] = [];
    const warm: RunResult[] = [];
    for (let run = 0; run < RUNS; run += 1) {
      cold.push(await measureLoad(page, true));
      warm.push(await measureLoad(page, false));
    }

    const report = (label: string, runs: RunResult[]) => {
      const usable = runs.map((r) => r.firstUsableMs);
      const inventory = runs.map((r) => r.inventoryReadyMs);
      console.log(
        `${label}: first usable median ${Math.round(median(usable))}ms ` +
          `p95 ${Math.round(p95(usable))}ms, inventory median ` +
          `${Math.round(median(inventory))}ms, requests median ` +
          `${median(runs.map((r) => r.requests))}, chain ` +
          `${Math.max(...runs.map((r) => r.longestChain))}, long tasks ` +
          `${Math.round(median(runs.map((r) => r.longTaskTotalMs)))}ms`
      );
      for (const run of runs) {
        if (run.duplicates.length) {
          console.log(`${label} duplicates: ${run.duplicates.join(', ')}`);
        }
      }
    };
    report('cold', cold);
    report('warm', warm);

    expect(median(cold.map((r) => r.firstUsableMs))).toBeLessThan(
      BUDGET.firstUsableMs
    );
    expect(median(cold.map((r) => r.requests))).toBeLessThanOrEqual(
      BUDGET.coldRequests
    );
    expect(cold.flatMap((r) => r.duplicates)).toHaveLength(
      BUDGET.duplicateUrls
    );
    expect(Math.max(...cold.map((r) => r.longestChain))).toBeLessThanOrEqual(
      BUDGET.longestChain
    );
    expect(median(cold.map((r) => r.longTaskTotalMs))).toBeLessThan(
      BUDGET.longTaskTotalMs
    );
  });

  test('stays quiet while the gateway is busy', async ({ page, request }) => {
    await login(page, resolveCreds());
    await measureLoad(page, true);

    const key = await page.evaluate(() => localStorage.getItem('accessToken'));
    // Twenty model calls over ten seconds: each one publishes a
    // gateway_activity event to the open page.
    const burst = (async () => {
      for (let call = 0; call < 20; call += 1) {
        await request.post('/openai/v1/chat/completions', {
          headers: { Authorization: `Bearer ${key}` },
          data: {
            model: GATEWAY_MODEL,
            messages: [{ role: 'user', content: 'perf burst' }],
          },
        });
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    })();

    let count = 0;
    const onRequest = (req: { url: () => string }) => {
      if (req.url().includes('/api/v1')) count += 1;
    };
    page.on('request', onRequest);
    await burst;
    // The refresh floor is ten seconds per topic; watch three of them.
    await page.waitForTimeout(30_000);
    page.off('request', onRequest);

    console.log(`burst: ${count} API requests over the 40s window`);
    expect(count).toBeLessThanOrEqual(BUDGET.requestsAfterBurst);
  });
});
