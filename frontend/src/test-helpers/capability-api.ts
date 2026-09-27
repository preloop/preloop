/**
 * Test support for the capability-gated account views (issue #988): a
 * `/features` fixture that turns capabilities on, and a fetch mock that
 * serves a route table and records every request with its bearer token.
 */
import sinon from 'sinon';
import type { Capability } from '../capabilities';

export function featuresFixture(capabilities: readonly Capability[] = []) {
  const features: Record<string, boolean> = {
    multi_account: false,
    account_hierarchy: false,
    abac_rules: false,
  };
  for (const capability of capabilities) features[capability] = true;
  return { plugins: [], features };
}

export interface MockRoute {
  method?: string;
  /** A string matches the pathname exactly; a RegExp is tested against it. */
  path: string | RegExp;
  status?: number;
  body?: unknown | ((call: RecordedCall) => unknown);
}

export interface RecordedCall {
  method: string;
  path: string;
  search: string;
  authorization: string | null;
  body: unknown;
}

export interface MockApi {
  calls: RecordedCall[];
  stub: sinon.SinonStub;
  /** Requests whose pathname matches. */
  callsTo(path: string | RegExp, method?: string): RecordedCall[];
  restore(): void;
}

export const TEST_PROFILE = {
  id: 'user-1',
  username: 'ada',
  email: 'ada@example.com',
  email_verified: true,
  account_id: 'acc-root',
  permissions: null,
};

function json(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body ?? {}), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Replaces window.fetch. `/api/v1/features` and `/api/v1/auth/users/me` are
 * served from the fixture unless a route overrides them; anything else that
 * no route matches answers 404, which is how a server without the extension
 * plugin answers the account endpoints.
 */
export function mockApi(
  options: {
    capabilities?: readonly Capability[];
    routes?: MockRoute[];
    profile?: Record<string, unknown>;
    /** Answer for unmatched requests; default 404. */
    fallback?: { status: number; body: unknown };
  } = {}
): MockApi {
  const calls: RecordedCall[] = [];
  const routes: MockRoute[] = [
    ...(options.routes ?? []),
    { path: '/api/v1/features', body: featuresFixture(options.capabilities) },
    { path: '/api/v1/auth/users/me', body: options.profile ?? TEST_PROFILE },
  ];
  const stub = sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(raw, window.location.origin);
      const method = (init?.method ?? 'GET').toUpperCase();
      const headers = new Headers(init?.headers ?? {});
      let body: unknown = undefined;
      if (typeof init?.body === 'string') {
        try {
          body = JSON.parse(init.body);
        } catch {
          body = init.body;
        }
      }
      const call: RecordedCall = {
        method,
        path: url.pathname,
        search: url.search,
        authorization: headers.get('Authorization'),
        body,
      };
      calls.push(call);
      const route = routes.find(
        (r) =>
          (r.method ?? 'GET').toUpperCase() === method &&
          (typeof r.path === 'string'
            ? r.path === url.pathname
            : r.path.test(url.pathname))
      );
      if (!route) {
        return options.fallback
          ? json(options.fallback.status, options.fallback.body)
          : json(404, { detail: 'Not Found' });
      }
      const payload =
        typeof route.body === 'function'
          ? (route.body as (c: RecordedCall) => unknown)(call)
          : route.body;
      return json(route.status ?? 200, payload);
    });
  return {
    calls,
    stub,
    callsTo(path, method) {
      return calls.filter(
        (c) =>
          (typeof path === 'string' ? c.path === path : path.test(c.path)) &&
          (!method || c.method === method.toUpperCase())
      );
    },
    restore() {
      stub.restore();
    },
  };
}

/** Number of toasts currently in the page. */
export function toastCount(): number {
  return document.body.querySelectorAll('sl-alert').length;
}

/** Stores a session so fetchWithAuth sends requests; call in beforeEach. */
export function signInForTest(
  access = 'test-access',
  refresh = 'test-refresh'
) {
  localStorage.setItem('accessToken', access);
  localStorage.setItem('refreshToken', refresh);
}
