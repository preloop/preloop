/**
 * Content-Security-Policy violation sweep (#2280).
 *
 * Loads every route of the console router plus the landing page, a blog
 * post, a `/vs/` page and a `/resources/` page from a production build, under
 * the policy `CSP_MODE=enforce` sends, with analytics off and on, and fails on
 * any `securitypolicyviolation` event or CSP console error. A third test
 * checks that the analytics script still loads and an event leaves the
 * browser under the enforced policy.
 *
 * Run: npm run test:csp (builds dist-csp-sweep, then runs this file).
 *
 * The API is mocked with page.route: every /api call answers 200 with an
 * empty JSON object, /api/v1/features reports every capability so the gated
 * routes are registered, and a synthetic access token is in localStorage.
 * Views therefore render their empty or error states, not real data.
 */
import { expect, test, type Page } from '@playwright/test';
import { fileURLToPath } from 'url';
import { consoleRoutes } from './console-routes';
import {
  enforcedPolicy,
  helmAnalyticsTags,
  startAnalyticsStub,
  startConsoleServer,
  type AnalyticsEvent,
  type RunningServer,
} from './csp-server';

const DIST = fileURLToPath(new URL('../../dist-csp-sweep', import.meta.url));
const ANALYTICS_DOMAIN = 'csp-sweep.example.test';

/** The four public page types; the slugs are the fixture content's. */
const PUBLIC_PAGES = [
  '/',
  '/blog/csp-sweep-post',
  '/vs/aws-agentcore',
  '/resources/csp-sweep-guide',
];

/**
 * Content routes whose page must exist: an unknown slug makes static-view
 * fetch the SPA shell as its "content" and render the app inside itself,
 * which is a separate bug, not a CSP one.
 */
const ROUTE_URLS: Record<string, string> = {
  '/vs/:slug': '/vs/aws-agentcore',
  '/blog/:slug': '/blog/csp-sweep-post',
};

const CAPABILITIES = [
  'fleet_coverage',
  'multi_account',
  'account_hierarchy',
  'resource_sharing',
  'resource_access',
  'abac_rules',
  'chat_connections',
  'cloud_support',
  'anthropic_inference_hooks',
  'governance_review',
  'weekly_work_value',
  'runner_sharing',
];

interface Violation {
  url: string;
  detail: string;
}

async function preparePage(page: Page, origin: string): Promise<Violation[]> {
  const violations: Violation[] = [];
  await page.addInitScript(() => {
    localStorage.setItem('accessToken', 'csp-sweep-synthetic-token');
    localStorage.setItem('dashboard_welcome_dismissed', 'true');
    const store: string[] = [];
    (window as unknown as { __cspViolations: string[] }).__cspViolations =
      store;
    document.addEventListener('securitypolicyviolation', (event) => {
      store.push(
        `${event.effectiveDirective} blocked ${event.blockedURI || '(inline)'} at ${event.sourceFile}:${event.lineNumber}`
      );
    });
  });
  page.on('console', (message) => {
    const text = message.text();
    if (process.env.CSP_SWEEP_DEBUG) process.stdout.write(`[page] ${text}\n`);
    if (/Content[- ]Security[- ]Policy/i.test(text)) {
      violations.push({ url: page.url(), detail: `console: ${text}` });
    }
  });
  await page.route(`${origin}/api/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/features') {
      await route.fulfill({
        json: {
          edition: 'oss',
          features: {
            registration: true,
            ...Object.fromEntries(CAPABILITIES.map((name) => [name, true])),
          },
        },
      });
      return;
    }
    await route.fulfill({ json: {} });
  });
  return violations;
}

async function visit(
  page: Page,
  origin: string,
  url: string,
  violations: Violation[]
): Promise<string> {
  if (process.env.CSP_SWEEP_DEBUG) process.stdout.write(`[visit] ${url}\n`);
  await page.goto(`${origin}${url}`, { waitUntil: 'load' });
  await page.waitForFunction(() => Boolean(customElements.get('lit-app')));
  // Lazy route chunks, Shoelace components and icons load after `load`.
  await page.waitForLoadState('networkidle').catch(() => undefined);
  await page.waitForTimeout(300);
  const recorded = await page.evaluate(
    () =>
      (window as unknown as { __cspViolations?: string[] }).__cspViolations ??
      []
  );
  for (const detail of recorded) violations.push({ url, detail });
  return new URL(page.url()).pathname;
}

function scriptSources(policy: string): string[] {
  const directive = policy
    .split(';')
    .map((part) => part.trim().split(/\s+/))
    .find((parts) => parts[0] === 'script-src');
  if (!directive) throw new Error(`no script-src in ${policy}`);
  return directive.slice(1);
}

const routes = consoleRoutes();

test('the route table reader finds the console router', () => {
  // protects=the sweep's coverage claim; fails_when=the reader silently
  // finds a fraction of the routes after a refactor of lit-app.ts;
  // no_claim=that every route is listed (spot checks only).
  const patterns = routes.map((route) => route.pattern);
  expect(patterns.length).toBeGreaterThan(60);
  for (const expected of [
    '/login',
    '/console',
    '/console/agents/:agentId',
    '/console/settings/api-keys',
    '/console/approval/:requestId',
    '/console/settings/subaccounts',
  ]) {
    expect(patterns).toContain(expected);
  }
});

for (const analytics of [false, true]) {
  test(`zero CSP violations on every route, analytics ${analytics ? 'on' : 'off'}`, async ({
    page,
  }) => {
    // protects=acceptance criterion 3 of #2280: the enforced policy blocks
    // nothing the console, landing, blog, /vs/ and /resources/ pages need;
    // fails_when=any route raises a securitypolicyviolation event or a CSP
    // console error, or a console route bounces to /login (the sweep would
    // then not have rendered it); no_claim=views with real data (the API is
    // mocked with empty objects), interactions after load (no clicks, so the
    // video embed iframe and form posts are not exercised), the API's own
    // HTML pages, browsers other than Chromium, and nginx itself (the policy
    // string is read from the nginx template and served by a Node server).
    test.setTimeout(15 * 60 * 1000);
    const servers: RunningServer[] = [];
    try {
      let policy = enforcedPolicy();
      let analyticsTags: string | undefined;
      if (analytics) {
        const stub = await startAnalyticsStub();
        servers.push(stub);
        policy = enforcedPolicy([stub.origin], [stub.origin]);
        analyticsTags = helmAnalyticsTags(
          ANALYTICS_DOMAIN,
          `${stub.origin}/js/script.js`
        );
      }
      const scriptSrc = scriptSources(policy);
      expect(scriptSrc).not.toContain("'unsafe-inline'");
      expect(scriptSrc.filter((source) => /^[a-z]+:$/.test(source))).toEqual(
        []
      );
      const site = await startConsoleServer({
        dist: DIST,
        policy,
        analyticsTags,
      });
      servers.push(site);

      const violations = await preparePage(page, site.origin);
      const bounced: string[] = [];
      const urls = [
        ...PUBLIC_PAGES,
        ...routes.map((route) => ROUTE_URLS[route.pattern] ?? route.url),
      ];
      for (const url of urls) {
        const landed = await visit(page, site.origin, url, violations);
        if (url.startsWith('/console') && landed.startsWith('/login')) {
          bounced.push(url);
        }
      }
      expect(bounced, 'console routes that redirected to /login').toEqual([]);
      expect(
        violations.map((v) => `${v.url}: ${v.detail}`),
        `CSP violations across ${urls.length} URLs`
      ).toEqual([]);
    } finally {
      for (const server of servers.reverse()) await server.close();
    }
  });
}

test('analytics script loads and an event leaves the browser under enforce', async ({
  page,
}) => {
  // protects=acceptance criterion 4 of #2280: with analytics on, the tracker
  // script runs under the enforced policy and its requests reach the
  // analytics origin, including events queued before it loaded;
  // fails_when=the queue stub or tracker is blocked, or no event request
  // arrives; no_claim=the real analytics platform (a local stub tracker on
  // another origin stands in) and Helm's derivation of the origin from
  // analytics.scriptUrl (backend Helm tests cover that).
  const stub = await startAnalyticsStub();
  const servers: RunningServer[] = [stub];
  try {
    const site = await startConsoleServer({
      dist: DIST,
      policy: enforcedPolicy([stub.origin], [stub.origin]),
      analyticsTags: helmAnalyticsTags(
        ANALYTICS_DOMAIN,
        `${stub.origin}/js/script.js`
      ),
    });
    servers.push(site);
    const violations = await preparePage(page, site.origin);
    await page.addInitScript(() => {
      // A custom event fired before the tracker loads: analytics-queue.js
      // must queue it and the tracker must replay it.
      document.addEventListener('DOMContentLoaded', () => {
        (window as unknown as { plausible: (name: string) => void }).plausible(
          'csp-sweep-queued'
        );
      });
    });
    await visit(page, site.origin, '/login', violations);
    const names = (events: AnalyticsEvent[]) => events.map((e) => e.name);
    await expect.poll(() => names(stub.events)).toContain('pageview');
    await expect.poll(() => names(stub.events)).toContain('csp-sweep-queued');
    expect(stub.events.every((e) => e.domain === ANALYTICS_DOMAIN)).toBe(true);
    expect(violations).toEqual([]);
  } finally {
    for (const server of servers.reverse()) await server.close();
  }
});
