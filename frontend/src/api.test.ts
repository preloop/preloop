import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { Router } from './router';
import {
  fetchWithAuth,
  performLocalSignOut,
  invalidateApiCaches,
  AuthedElement,
  getFlowExecutions,
  getFlows,
  getAllFlows,
  uniqueFlowsById,
  FLOW_LIST_MAX_PAGES,
  createFlow,
  updateFlow,
  listProjectsForOrg,
  uploadAvatar,
  validateTrackerToken,
  startCheckout,
  startAnonymousCheckout,
  getPlanChoice,
  recordFreePlanChoice,
  coalesceKey,
  getUsageNudges,
  getAccountGatewayUsageSummary,
  getAccountRuntimeSessionActivityTimeline,
  getCostAnalyticsSummary,
  BILLING_SUBSCRIPTION_CHANGED,
  NO_USAGE_NUDGES,
} from './api.js';
import {
  HISTORY_UNAVAILABLE_CODE,
  isHistoryUnavailable,
} from './utils/history-window.js';
import { customElement } from 'lit/decorators.js';

// Minimal test element that exposes fetchData for testing
@customElement('test-authed-element')
class TestAuthedElement extends AuthedElement {
  async fetchDataForTest(url: string, options?: RequestInit) {
    return this.fetchData(url, options);
  }
}

describe('api', () => {
  let fetchStub: sinon.SinonStub;
  let routerGoStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    routerGoStub = sinon.stub(Router, 'go');
  });

  afterEach(() => {
    fetchStub.restore();
    routerGoStub.restore();
    localStorage.clear();
  });

  describe('performLocalSignOut', () => {
    it('clears tokens, navigates home, and hits /logout', () => {
      const navigate = sinon.stub();
      performLocalSignOut(navigate);

      expect(localStorage.getItem('accessToken')).to.equal(null);
      expect(localStorage.getItem('refreshToken')).to.equal(null);
      expect(navigate).to.have.been.calledWith('/');
      const logoutCall = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]) === '/logout');
      expect(logoutCall, 'expected GET /logout').to.exist;
    });
  });

  describe('fetchWithAuth', () => {
    it('includes Authorization header with access token', async () => {
      fetchStub.resolves(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      await fetchWithAuth('/api/v1/test');

      expect(fetchStub).to.have.been.calledOnce;
      const [url, options] = fetchStub.firstCall.args;
      expect(url).to.equal('/api/v1/test');
      expect(options?.headers).to.be.instanceOf(Headers);
      expect((options?.headers as Headers).get('Authorization')).to.equal(
        'Bearer test-access-token'
      );
    });

    it('redirects to login when no access token', async () => {
      localStorage.removeItem('accessToken');
      localStorage.removeItem('refreshToken');

      let threw = false;
      try {
        await fetchWithAuth('/api/v1/test');
      } catch (e: unknown) {
        threw = true;
        expect((e as Error).message).to.include('Not authenticated');
      }
      expect(threw).to.be.true;
      expect(routerGoStub).to.have.been.calledWith('/login');
    });

    it('refreshes token and retries on 401', async () => {
      const successResponse = new Response(JSON.stringify({ data: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });

      let callCount = 0;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        callCount++;
        const url = typeof input === 'string' ? input : input.toString();
        if (callCount === 1) {
          return new Response(JSON.stringify({}), {
            status: 401,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/auth/refresh')) {
          return new Response(
            JSON.stringify({
              access_token: 'new-access-token',
              refresh_token: 'new-refresh-token',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return successResponse;
      });

      const response = await fetchWithAuth('/api/v1/test');

      expect(response?.status).to.equal(200);
      expect(fetchStub).to.have.been.calledThrice; // initial 401, refresh, retry
      const refreshCall = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/api/v1/auth/refresh'));
      expect(refreshCall).to.exist;
      const retryCall = fetchStub
        .getCalls()
        .find(
          (c) =>
            String(c.args[0]) === '/api/v1/test' &&
            (c.args[1] as RequestInit)?.headers &&
            (c.args[1] as RequestInit).headers instanceof Headers &&
            ((c.args[1] as RequestInit).headers as Headers).get(
              'Authorization'
            ) === 'Bearer new-access-token'
        );
      expect(retryCall).to.exist;
    });

    it('redirects to login when refresh fails on 401', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/auth/refresh')) {
          return new Response(
            JSON.stringify({ detail: 'Invalid refresh token' }),
            { status: 401, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response(JSON.stringify({}), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      let threw = false;
      try {
        await fetchWithAuth('/api/v1/test');
      } catch (e: unknown) {
        threw = true;
        expect((e as Error).message).to.include(
          'Failed to refresh token, redirecting to login.'
        );
      }
      expect(threw).to.be.true;
      expect(routerGoStub).to.have.been.calledWith('/login');
    });

    it('clears tokens when refresh is definitively rejected', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/auth/refresh')) {
          return new Response(
            JSON.stringify({ detail: 'Invalid refresh token' }),
            { status: 401, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response(JSON.stringify({}), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      try {
        await fetchWithAuth('/api/v1/test');
      } catch {
        // expected
      }
      expect(localStorage.getItem('accessToken')).to.be.null;
      expect(localStorage.getItem('refreshToken')).to.be.null;
    });

    it('keeps session and retries once when refresh fails transiently', async () => {
      let refreshCalls = 0;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/auth/refresh')) {
          refreshCalls++;
          // Simulate a deploy blip: both refresh attempts return 502.
          return new Response('Bad Gateway', { status: 502 });
        }
        return new Response(JSON.stringify({}), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const response = await fetchWithAuth('/api/v1/test');

      // Transient failure: original 401 response is returned, session intact.
      expect(response.status).to.equal(401);
      expect(refreshCalls).to.equal(2); // initial attempt + one retry
      expect(localStorage.getItem('accessToken')).to.equal('test-access-token');
      expect(localStorage.getItem('refreshToken')).to.equal(
        'test-refresh-token'
      );
      expect(routerGoStub).to.not.have.been.calledWith('/login');
    });

    it('recovers when the transient retry succeeds', async () => {
      let refreshCalls = 0;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/auth/refresh')) {
          refreshCalls++;
          if (refreshCalls === 1) {
            return new Response('Bad Gateway', { status: 502 });
          }
          return new Response(
            JSON.stringify({
              access_token: 'recovered-access-token',
              refresh_token: 'recovered-refresh-token',
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        const headers =
          (fetchStub.lastCall?.args[1] as RequestInit | undefined)?.headers ??
          null;
        const auth =
          headers instanceof Headers ? headers.get('Authorization') : null;
        if (auth === 'Bearer recovered-access-token') {
          return new Response(JSON.stringify({ data: 'ok' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({}), {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const response = await fetchWithAuth('/api/v1/test');

      expect(response.status).to.equal(200);
      expect(refreshCalls).to.equal(2);
      expect(localStorage.getItem('accessToken')).to.equal(
        'recovered-access-token'
      );
      expect(localStorage.getItem('refreshToken')).to.equal(
        'recovered-refresh-token'
      );
    });

    it('asks once when two callers want the same GET at the same time', async () => {
      let release = () => {};
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(async () => {
        await gate;
        return new Response(JSON.stringify({ users: [{ id: 'user-1' }] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const both = Promise.all([
        fetchWithAuth('/api/v1/users?skip=0&limit=100'),
        fetchWithAuth('/api/v1/users?skip=0&limit=100'),
      ]);
      release();
      const [first, second] = await both;

      expect(fetchStub.callCount, 'one network request').to.equal(1);
      // Each caller reads its own body: a shared response consumed once
      // would leave the second caller with a locked stream.
      expect(await first.json()).to.eql({ users: [{ id: 'user-1' }] });
      expect(await second.json()).to.eql({ users: [{ id: 'user-1' }] });
    });

    it('asks again once the shared request has settled', async () => {
      fetchStub.resolves(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      await fetchWithAuth('/api/v1/features');
      await fetchWithAuth('/api/v1/features');

      // A coalescer, not a cache: nothing is remembered after a response
      // lands, so nobody can read a stale body.
      expect(fetchStub.callCount).to.equal(2);
    });

    it('does not join a GET that was in flight when caches were invalidated', async () => {
      let release = () => {};
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(async () => {
        await gate;
        return new Response('stale', { status: 404 });
      });

      const first = fetchWithAuth('/api/v1/users?skip=0&limit=100');
      invalidateApiCaches();
      fetchStub.callsFake(async () => {
        return new Response(JSON.stringify({ users: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      const second = fetchWithAuth('/api/v1/users?skip=0&limit=100');
      release();
      const [stale, fresh] = await Promise.all([first, second]);

      expect(stale.status).to.equal(404);
      expect(fresh.status).to.equal(200);
      expect(await fresh.json()).to.eql({ users: [] });
    });

    it('never joins two writes to the same URL', async () => {
      fetchStub.resolves(
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      await Promise.all([
        fetchWithAuth('/api/v1/flows', { method: 'POST', body: '{}' }),
        fetchWithAuth('/api/v1/flows', { method: 'POST', body: '{}' }),
      ]);

      expect(fetchStub.callCount).to.equal(2);
    });
  });

  describe('passive requests (the paywall is a user action)', () => {
    const upgradeRequired = () =>
      new Response(
        JSON.stringify({
          detail: { code: 'upgrade_required', feature: 'price_overrides' },
        }),
        { status: 402, headers: { 'Content-Type': 'application/json' } }
      );

    /** Collect every upgrade dialog request raised while `run` runs. */
    async function modalEvents(run: () => Promise<unknown>) {
      const seen: CustomEvent[] = [];
      const listener = (event: Event) => seen.push(event as CustomEvent);
      window.addEventListener('show-upgrade-modal', listener);
      try {
        await run();
      } finally {
        window.removeEventListener('show-upgrade-modal', listener);
      }
      return seen;
    }

    it('still opens the dialog for a 402 nobody marked passive', async () => {
      // The founder decision narrows where the dialog appears, not whether a
      // click on a gated feature still explains itself.
      fetchStub.resolves(upgradeRequired());

      const seen = await modalEvents(() =>
        fetchWithAuth('/api/v1/billing/cost/pricing-overrides')
      );

      expect(seen).to.have.length(1);
      expect(seen[0].detail).to.eql({
        code: 'upgrade_required',
        feature: 'price_overrides',
      });
    });

    it('leaves a passive 402 to the caller, with no dialog', async () => {
      fetchStub.resolves(upgradeRequired());

      let status = 0;
      const seen = await modalEvents(async () => {
        const response = await fetchWithAuth(
          '/api/v1/billing/cost/pricing-overrides',
          { passive: true }
        );
        status = response.status;
      });

      expect(seen, 'no dialog from a background read').to.have.length(0);
      // The answer is still the answer: the caller decides what to render.
      expect(status).to.equal(402);
    });

    it('says nothing about a passive rate limit either', async () => {
      // 429 opens the same dialog, for the same reason: it is an answer about
      // the plan. A background read that hits it stays quiet.
      fetchStub.resolves(
        new Response('{"detail":"slow down"}', { status: 429 })
      );

      const passiveSeen = await modalEvents(() =>
        fetchWithAuth('/api/v1/agents', { passive: true })
      );
      const activeSeen = await modalEvents(() =>
        fetchWithAuth('/api/v1/agents')
      );

      expect(passiveSeen, 'silent in the background').to.have.length(0);
      expect(activeSeen, 'answers the reader who asked').to.have.length(1);
    });

    it('never sends the flag to the server', async () => {
      fetchStub.resolves(
        new Response('{}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      await fetchWithAuth('/api/v1/agents', { passive: true });

      const [, options] = fetchStub.firstCall.args;
      expect(options).to.not.have.property('passive');
    });

    it("keeps a passive read out of an active caller's coalesced request", async () => {
      // Sharing one in-flight GET would hand the active caller's answer to a
      // passive one, or worse, silence the dialog for the reader who clicked.
      let release = () => {};
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(async () => {
        await gate;
        return upgradeRequired();
      });

      const seen = await modalEvents(async () => {
        const both = Promise.all([
          fetchWithAuth('/api/v1/billing/cost/pricing-overrides', {
            passive: true,
          }),
          fetchWithAuth('/api/v1/billing/cost/pricing-overrides'),
        ]);
        release();
        await both;
      });

      expect(fetchStub.callCount, 'two separate requests').to.equal(2);
      expect(seen, 'one dialog, for the caller who asked').to.have.length(1);
    });

    it('reads the usage nudges without ever interrupting the page', async () => {
      // The nudge banner loads itself on every console page, so it is the
      // definition of a request nobody asked for. A 429 there used to raise
      // the rate-limit dialog over whatever the reader was doing.
      fetchStub.resolves(
        new Response('{"detail":"slow down"}', { status: 429 })
      );

      let nudges: unknown;
      const seen = await modalEvents(async () => {
        nudges = await getUsageNudges();
      });

      expect(seen, 'no dialog from the banner').to.have.length(0);
      expect(nudges).to.eql(NO_USAGE_NUDGES);
      const [, options] = fetchStub.firstCall.args;
      expect(options).to.not.have.property('passive');
    });

    it('namespaces the passive key with printable characters only', () => {
      // An invisible separator (a NUL or any other control byte) works at
      // runtime and passes type checking, but it makes this file "binary" for
      // grep, ripgrep, git grep and editor search. Keep the separator
      // readable, and fail here if anyone retypes it as something invisible.
      const key = coalesceKey('/api/v1/billing/cost/pricing-overrides', true);

      expect(key).to.equal('passive|/api/v1/billing/cost/pricing-overrides');
      // eslint-disable-next-line no-control-regex
      expect(
        /[\x00-\x1f\x7f]/.test(key),
        `control byte in ${JSON.stringify(key)}`
      ).to.be.false;
      expect(key).to.match(/^[\x20-\x7e]+$/);
      // The active key is the bare URL, so a passive read can never take an
      // active caller's slot.
      expect(coalesceKey('/api/v1/agents')).to.equal('/api/v1/agents');
      expect(coalesceKey('/api/v1/agents', false)).to.equal('/api/v1/agents');
      expect(coalesceKey('/api/v1/agents', true)).to.not.equal(
        coalesceKey('/api/v1/agents')
      );
    });

    it('does not coalesce passive reads of two different gated features', async () => {
      // Two background legs of the same boot-time `Promise.allSettled` ask
      // about different features: one answer must never be handed to the
      // other, whatever the separator in the key is.
      fetchStub.callsFake(
        async (input: RequestInfo | URL) =>
          new Response(JSON.stringify({ url: String(input) }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
      );

      const [overrides, analytics] = await Promise.all([
        fetchWithAuth('/api/v1/billing/cost/pricing-overrides', {
          passive: true,
        }),
        fetchWithAuth('/api/v1/analytics/summary', { passive: true }),
      ]);

      expect(fetchStub.callCount, 'one request per feature').to.equal(2);
      expect(await overrides.json()).to.eql({
        url: '/api/v1/billing/cost/pricing-overrides',
      });
      expect(await analytics.json()).to.eql({
        url: '/api/v1/analytics/summary',
      });
      expect(
        coalesceKey('/api/v1/billing/cost/pricing-overrides', true)
      ).to.not.equal(coalesceKey('/api/v1/analytics/summary', true));
    });

    it('changes nothing where no plan gate exists (self-hosted default)', async () => {
      // Without the billing plugin there is no capability gate and no 402, so
      // a passive call is an ordinary call and returns the same body.
      fetchStub.resolves(
        new Response(JSON.stringify([{ id: 'override-1' }]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      const seen = await modalEvents(async () => {
        const passive = await fetchWithAuth(
          '/api/v1/billing/cost/pricing-overrides',
          { passive: true }
        );
        expect(passive.status).to.equal(200);
        expect(await passive.json()).to.eql([{ id: 'override-1' }]);
      });

      expect(seen).to.have.length(0);
    });
  });

  describe('getFlowExecutions', () => {
    it('passes bounded filter params for lightweight list requests', async () => {
      fetchStub.resolves(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      await getFlowExecutions({
        limit: 21,
        skip: 20,
        flowId: 'flow-1',
        status: ['RUNNING', 'PENDING'],
      });

      const url = new URL(fetchStub.firstCall.args[0], window.location.origin);
      expect(url.pathname).to.equal('/api/v1/flows/executions');
      expect(url.searchParams.get('limit')).to.equal('21');
      expect(url.searchParams.get('skip')).to.equal('20');
      expect(url.searchParams.get('flow_id')).to.equal('flow-1');
      expect(url.searchParams.getAll('status')).to.deep.equal([
        'RUNNING',
        'PENDING',
      ]);
    });
  });

  describe('getFlows', () => {
    const ok = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });

    it('passes skip and limit for a paged list', async () => {
      fetchStub.resolves(ok([]));

      await getFlows({ skip: 100, limit: 100 });

      const url = new URL(fetchStub.firstCall.args[0], window.location.origin);
      expect(url.pathname).to.equal('/api/v1/flows');
      expect(url.searchParams.get('skip')).to.equal('100');
      expect(url.searchParams.get('limit')).to.equal('100');
    });

    it('throws when the list request fails', async () => {
      fetchStub.resolves(new Response('error', { status: 500 }));
      let message = '';
      try {
        await getFlows();
      } catch (e: unknown) {
        message = (e as Error).message;
      }
      expect(message).to.equal('Failed to fetch flows');
    });
  });

  describe('getAllFlows', () => {
    const ok = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });

    it('pages until a short page', async () => {
      fetchStub.onCall(0).resolves(
        ok([
          { id: '1', name: 'A' },
          { id: '2', name: 'B' },
        ])
      );
      fetchStub.onCall(1).resolves(ok([{ id: '3', name: 'C' }]));

      const result = await getAllFlows({ pageSize: 2 });

      expect(result.truncated).to.be.false;
      expect(
        result.flows.map((flow: { name: string }) => flow.name)
      ).to.deep.equal(['A', 'B', 'C']);
      expect(fetchStub.callCount).to.equal(2);
      const first = new URL(
        fetchStub.firstCall.args[0],
        window.location.origin
      );
      const second = new URL(
        fetchStub.secondCall.args[0],
        window.location.origin
      );
      expect(first.searchParams.get('skip')).to.equal('0');
      expect(first.searchParams.get('limit')).to.equal('2');
      expect(second.searchParams.get('skip')).to.equal('2');
      expect(second.searchParams.get('limit')).to.equal('2');
    });

    it('does not treat a failed page as an empty account', async () => {
      fetchStub.resolves(new Response('error', { status: 500 }));
      let message = '';
      try {
        await getAllFlows({ pageSize: 2 });
      } catch (e: unknown) {
        message = (e as Error).message;
      }
      expect(message).to.equal('Failed to fetch flows');
    });

    it('stops at the page cap and reports truncated', async () => {
      let n = 0;
      fetchStub.callsFake(async () => {
        n += 1;
        return ok([{ id: String(n), name: 'A' }]);
      });

      const result = await getAllFlows({ pageSize: 1 });

      expect(result.truncated).to.be.true;
      expect(result.flows).to.have.lengthOf(FLOW_LIST_MAX_PAGES);
      expect(fetchStub.callCount).to.equal(FLOW_LIST_MAX_PAGES);
    });

    it('keeps one row when the same id appears on two pages', async () => {
      fetchStub.onCall(0).resolves(
        ok([
          { id: '1', name: 'A' },
          { id: '2', name: 'B' },
        ])
      );
      fetchStub.onCall(1).resolves(
        ok([
          { id: '2', name: 'B-renamed' },
          { id: '3', name: 'C' },
        ])
      );
      fetchStub.onCall(2).resolves(ok([]));

      const result = await getAllFlows({ pageSize: 2 });

      expect(result.truncated).to.be.false;
      expect(result.flows.map((flow: { id: string }) => flow.id)).to.deep.equal(
        ['1', '2', '3']
      );
      expect(
        result.flows.map((flow: { name: string }) => flow.name)
      ).to.deep.equal(['A', 'B', 'C']);
    });
  });

  describe('uniqueFlowsById', () => {
    it('keeps the first name when the same id repeats', () => {
      expect(
        uniqueFlowsById([
          { id: '1', name: 'A' },
          { id: '1', name: 'A-renamed' },
          { id: '2', name: 'B' },
        ])
      ).to.deep.equal([
        { id: '1', name: 'A' },
        { id: '2', name: 'B' },
      ]);
    });
  });

  describe('AuthedElement.fetchData', () => {
    it('returns parsed JSON on success', async () => {
      const testData = { id: '1', name: 'Test' };
      fetchStub.resolves(
        new Response(JSON.stringify(testData), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      const el = document.createElement(
        'test-authed-element'
      ) as TestAuthedElement;
      document.body.appendChild(el);
      await el.updateComplete;

      const result = await el.fetchDataForTest('/api/v1/test');

      expect(result).to.deep.equal(testData);
      document.body.removeChild(el);
    });

    it('returns null on HTTP error', async () => {
      fetchStub.resolves(
        new Response(JSON.stringify({ detail: 'Not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      const el = document.createElement(
        'test-authed-element'
      ) as TestAuthedElement;
      document.body.appendChild(el);
      await el.updateComplete;

      const result = await el.fetchDataForTest('/api/v1/test');

      expect(result).to.be.null;
      document.body.removeChild(el);
    });

    it('rethrows PermissionError on 403 so views can show a denied state', async () => {
      const { PermissionError } = await import('./permissions');
      fetchStub.resolves(
        new Response(
          JSON.stringify({
            detail: 'Insufficient permissions. Required: view_cost',
          }),
          {
            status: 403,
            headers: { 'Content-Type': 'application/json' },
          }
        )
      );

      const el = document.createElement(
        'test-authed-element'
      ) as TestAuthedElement;
      document.body.appendChild(el);
      await el.updateComplete;

      let thrown: unknown;
      try {
        await el.fetchDataForTest('/api/v1/test');
      } catch (error) {
        thrown = error;
      }

      expect(thrown).to.be.instanceOf(PermissionError);
      expect((thrown as InstanceType<typeof PermissionError>).status).to.equal(
        403
      );
      expect(
        (thrown as InstanceType<typeof PermissionError>).requiredPermission
      ).to.equal('view_cost');
      document.body.removeChild(el);
    });

    it('returns null when fetchWithAuth throws (e.g. auth failure)', async () => {
      localStorage.removeItem('accessToken');
      localStorage.removeItem('refreshToken');

      const el = document.createElement(
        'test-authed-element'
      ) as TestAuthedElement;
      document.body.appendChild(el);
      await el.updateComplete;

      const result = await el.fetchDataForTest('/api/v1/test');

      expect(result).to.be.null;
      document.body.removeChild(el);
    });
  });

  describe('uploadAvatar', () => {
    const file = new File(['avatar'], 'photo.png', { type: 'image/png' });

    it('uses the API detail string when present', async () => {
      fetchStub.resolves(
        new Response(JSON.stringify({ detail: 'Not an image.' }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      let message = '';
      try {
        await uploadAvatar(file);
      } catch (e: unknown) {
        message = (e as Error).message;
      }
      expect(message).to.equal('Not an image.');
    });

    it('explains a 413 HTML body as too large', async () => {
      fetchStub.resolves(
        new Response('<html>413 Request Entity Too Large</html>', {
          status: 413,
          headers: { 'Content-Type': 'text/html' },
        })
      );

      let message = '';
      try {
        await uploadAvatar(file);
      } catch (e: unknown) {
        message = (e as Error).message;
      }
      expect(message).to.equal('Image too large to upload.');
    });

    it('includes the HTTP status when the body is not JSON', async () => {
      fetchStub.resolves(
        new Response('<html>Bad Gateway</html>', {
          status: 502,
          headers: { 'Content-Type': 'text/html' },
        })
      );

      let message = '';
      try {
        await uploadAvatar(file);
      } catch (e: unknown) {
        message = (e as Error).message;
      }
      expect(message).to.equal('Failed to upload avatar (502)');
    });
  });

  describe('tracker connection helpers', () => {
    const errorResponse = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });

    const messageOf = async (call: Promise<unknown>) => {
      try {
        await call;
      } catch (e: unknown) {
        return (e as Error).message;
      }
      return '';
    };

    it('validateTrackerToken surfaces the FastAPI detail string', async () => {
      fetchStub.resolves(
        errorResponse({
          detail: 'Tracker is bound to an installation that no longer exists.',
        })
      );

      expect(
        await messageOf(validateTrackerToken('github', 'unchanged'))
      ).to.equal('Tracker is bound to an installation that no longer exists.');
    });

    it('validateTrackerToken falls back to message, then a generic error', async () => {
      fetchStub.resolves(errorResponse({ message: 'Custom message' }));
      expect(
        await messageOf(validateTrackerToken('github', 'unchanged'))
      ).to.equal('Custom message');

      fetchStub.resolves(errorResponse({}));
      expect(
        await messageOf(validateTrackerToken('github', 'unchanged'))
      ).to.equal('Failed to validate token');
    });

    it('listProjectsForOrg surfaces the FastAPI detail string', async () => {
      fetchStub.resolves(
        errorResponse({ detail: 'Tracker not found or access denied' })
      );

      expect(
        await messageOf(listProjectsForOrg('github', 'unchanged', '9001'))
      ).to.equal('Tracker not found or access denied');

      fetchStub.resolves(errorResponse({}));
      expect(
        await messageOf(listProjectsForOrg('github', 'unchanged', '9001'))
      ).to.equal('Failed to list projects for organization');
    });
  });
  describe('flow write refusals', () => {
    const refusal = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 422,
        headers: { 'Content-Type': 'application/json' },
      });

    const messageOf = async (call: Promise<unknown>) => {
      try {
        await call;
      } catch (e: unknown) {
        return (e as Error).message;
      }
      return '';
    };

    it('surfaces the detail string, which names the entry refused', async () => {
      fetchStub.resolves(
        refusal({
          detail:
            "callable_flows entry 'Child flow' does not name a flow in this account",
        })
      );

      expect(await messageOf(updateFlow('flow-1', {}))).to.equal(
        "callable_flows entry 'Child flow' does not name a flow in this account"
      );
    });

    it('flattens a field error list instead of printing [object Object]', async () => {
      fetchStub.resolves(
        refusal({
          detail: [
            {
              loc: ['body', 'callable_flows'],
              msg: "Value error, callable_flows has a duplicate entry for 'Child flow'",
              type: 'value_error',
            },
          ],
        })
      );

      const message = await messageOf(createFlow({}));
      expect(message).to.include("'Child flow'");
      expect(message).to.include('callable_flows');
      expect(message).to.not.include('object Object');
    });

    it('falls back when the body carries no reason', async () => {
      fetchStub.resolves(refusal({}));
      expect(await messageOf(createFlow({}))).to.equal('Failed to create flow');

      fetchStub.resolves(refusal({ detail: [] }));
      expect(await messageOf(updateFlow('flow-1', {}))).to.equal(
        'Failed to update flow'
      );
    });
  });
  describe('startCheckout', () => {
    /**
     * Every answer the server can give, and what the person who clicked sees.
     *
     * The console modal has no other source of words: whatever this function
     * resolves or throws is what appears on screen. A body it cannot read used
     * to become "Unexpected checkout response", which named neither the
     * problem nor a way out.
     */
    const answer = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });

    const messageOf = async (call: Promise<unknown>) => {
      try {
        await call;
      } catch (e: unknown) {
        return (e as Error).message;
      }
      return '';
    };

    it('resolves a refresh answer with the reason instead of throwing', async () => {
      fetchStub.resolves(
        answer({
          action: 'refresh',
          code: 'subscription_exists',
          message:
            'Your account already has a Pro subscription (status: active), so there is nothing to check out.',
        })
      );

      const outcome = await startCheckout('pro', 'month');

      expect(outcome?.action).to.equal('refresh');
      expect(outcome?.code).to.equal('subscription_exists');
      expect(outcome?.message).to.contain('already has a Pro subscription');
    });

    it('fills a speakable sentence when a refresh answer has no message', async () => {
      // Older EE returns bare {action: "refresh"}. The modal assigns
      // outcome.message to role=status; an empty string would render nothing.
      fetchStub.resolves(answer({ action: 'refresh' }));

      const outcome = await startCheckout('pro', 'month');

      expect(outcome?.action).to.equal('refresh');
      expect(outcome?.message).to.equal(
        'Your subscription is already up to date. Nothing was charged.'
      );
    });

    it('asks the billing views to re-read the summary on refresh', async () => {
      // The whole point of a refresh answer: the screen is stale, not the
      // account. Nothing reloads the summary unless this event is dispatched.
      fetchStub.resolves(
        answer({
          action: 'refresh',
          code: 'subscription_exists',
          message: 'Already subscribed.',
        })
      );
      let seen = 0;
      const listener = () => {
        seen += 1;
      };
      window.addEventListener(BILLING_SUBSCRIPTION_CHANGED, listener);

      try {
        await startCheckout('pro', 'month');
      } finally {
        window.removeEventListener(BILLING_SUBSCRIPTION_CHANGED, listener);
      }

      expect(seen).to.equal(1);
    });

    it('navigates and reports the outcome for a redirect', async () => {
      // A hash keeps the assignment inside this page: a real Stripe URL would
      // navigate the test runner away.
      fetchStub.resolves(
        answer({
          action: 'redirect',
          code: 'checkout_session_created',
          url: '#stripe-checkout',
          message: 'Opening secure checkout for Pro.',
        })
      );
      const originalHash = window.location.hash;

      try {
        const outcome = await startCheckout('pro', 'month');
        expect(outcome?.action).to.equal('redirect');
        expect(outcome?.message).to.equal('Opening secure checkout for Pro.');
        expect(window.location.hash).to.equal('#stripe-checkout');
      } finally {
        window.location.hash = originalHash;
      }
    });

    it('surfaces the server sentence for an action it cannot perform', async () => {
      fetchStub.resolves(
        answer({
          action: 'contact_sales',
          code: 'sales_contact_required',
          message: 'Enterprise is not sold through self-serve checkout.',
        })
      );

      expect(await messageOf(startCheckout('enterprise', 'month'))).to.equal(
        'Enterprise is not sold through self-serve checkout.'
      );
    });

    it('never falls back to an opaque phrase when the body says nothing', async () => {
      fetchStub.resolves(answer({}));

      const message = await messageOf(startCheckout('pro', 'month'));

      expect(message).to.not.contain('Unexpected checkout response');
      expect(message).to.contain('Checkout could not be started');
    });

    it('passes a deployment refusal through in the server words', async () => {
      // catalog_not_synced means the image shipped without a synced catalog.
      // Rewording it hides the one instruction an operator needs.
      fetchStub.resolves(
        answer(
          {
            detail: {
              code: 'catalog_not_synced',
              message:
                'Pro is not available for purchase yet. Ask an administrator to sync the plan catalog.',
            },
          },
          503
        )
      );

      expect(await messageOf(startCheckout('pro', 'month'))).to.equal(
        'Pro is not available for purchase yet. Ask an administrator to sync the plan catalog.'
      );
    });
  });

  describe('startAnonymousCheckout', () => {
    const answer = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });

    it('sends no Authorization header and never diverts to login', async () => {
      // The whole point of the Stripe-first path: the caller has no account
      // yet. Routing this through fetchWithAuth would read the missing token
      // as a dead session and send the visitor to /login, which is the detour
      // this flow removes.
      localStorage.removeItem('accessToken');
      localStorage.removeItem('refreshToken');
      fetchStub.resolves(
        answer({
          action: 'redirect',
          code: 'checkout_session_created',
          url: '#stripe-anon',
          message: 'Opening secure checkout for Pro.',
        })
      );
      const originalHash = window.location.hash;

      try {
        const outcome = await startAnonymousCheckout('pro', 'year');
        expect(outcome?.code).to.equal('checkout_session_created');
        expect(window.location.hash).to.equal('#stripe-anon');
        expect(routerGoStub.called, 'no redirect to login').to.equal(false);
        const [url, options] = fetchStub.firstCall.args;
        expect(String(url)).to.contain('/billing/create-checkout-session');
        const headers = new Headers((options as RequestInit)?.headers);
        expect(headers.get('Authorization')).to.equal(null);
        expect(
          JSON.parse(String((options as RequestInit)?.body))
        ).to.deep.equal({ plan_id: 'pro', interval: 'year', return_to: null });
      } finally {
        window.location.hash = originalHash;
      }
    });

    it('reports a refusal in the server words', async () => {
      localStorage.removeItem('accessToken');
      fetchStub.resolves(
        answer(
          {
            detail: {
              code: 'catalog_not_synced',
              message: 'Pro is not available for purchase yet.',
            },
          },
          503
        )
      );

      let message = '';
      try {
        await startAnonymousCheckout('pro', 'month');
      } catch (e: unknown) {
        message = (e as Error).message;
      }
      expect(message).to.equal('Pro is not available for purchase yet.');
    });
  });

  describe('plan choice', () => {
    beforeEach(() => {
      // The durable "no" is remembered per tab, which in a test file is per
      // module. Each case starts from a console that has not asked yet.
      invalidateApiCaches();
    });

    it('reads the decision the server made', async () => {
      fetchStub.resolves(
        new Response(
          JSON.stringify({ show: true, reason: 'eligible', trial_days: 14 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      );

      const decision = await getPlanChoice();

      expect(decision.show).to.equal(true);
      expect(decision.reason).to.equal('eligible');
      expect(decision.trial_days).to.equal(14);
    });

    it('asks nothing of a console with no billing plugin', async () => {
      // OSS: the endpoint does not exist, so a 404 is a normal answer here,
      // not an error, and it means "there is no plan to choose".
      fetchStub.resolves(new Response('Not Found', { status: 404 }));

      const decision = await getPlanChoice();

      expect(decision.show).to.equal(false);
      expect(decision.reason).to.equal('unavailable');
    });

    it('asks passively, so the question never raises a dialog', async () => {
      // The shell asks this on load. A rate limit on a question nobody typed
      // must not put the paywall dialog over the page (#770's rule).
      fetchStub.resolves(
        new Response('{"detail":"slow down"}', { status: 429 })
      );
      const seen: CustomEvent[] = [];
      const listener = (event: Event) => seen.push(event as CustomEvent);
      window.addEventListener('show-upgrade-modal', listener);

      let decision;
      try {
        decision = await getPlanChoice();
      } finally {
        window.removeEventListener('show-upgrade-modal', listener);
      }

      expect(seen, 'no dialog from a background question').to.have.length(0);
      expect(decision.show).to.equal(false);
      const [, options] = fetchStub.firstCall.args;
      expect(options).to.not.have.property('passive');
    });

    it('asks again after an unreachable plugin, and only once after a no', async () => {
      // Two different kinds of "false". A 404 or a 500 is the plugin being
      // absent or broken, and the question has to heal itself when it comes
      // back. A reasoned refusal (a member who cannot buy, an account that
      // already subscribes) writes nothing down on the core profile, so
      // without this memo those people would re-ask on every route change
      // for the life of the account, to be told the same thing.
      fetchStub.resolves(new Response('boom', { status: 500 }));
      expect((await getPlanChoice()).reason).to.equal('unavailable');
      expect((await getPlanChoice()).reason).to.equal('unavailable');
      expect(fetchStub.callCount).to.equal(2);

      fetchStub.resolves(
        new Response(
          JSON.stringify({
            show: false,
            reason: 'not_billing_actor',
            trial_days: 14,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      );
      expect((await getPlanChoice()).reason).to.equal('not_billing_actor');
      expect(fetchStub.callCount).to.equal(3);

      const again = await getPlanChoice();
      expect(again.reason).to.equal('not_billing_actor');
      expect(again.trial_days).to.equal(14);
      expect(fetchStub.callCount, 'answered from the memo').to.equal(3);
    });

    it('keeps asking while the answer is still yes', async () => {
      // An open question is not settled, so nothing is remembered: the screen
      // has to be able to appear on a later load.
      fetchStub.resolves(
        new Response(
          JSON.stringify({ show: true, reason: 'eligible', trial_days: 14 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      );

      await getPlanChoice();
      await getPlanChoice();

      expect(fetchStub.callCount).to.equal(2);
    });

    it('forgets the answer when the session does', async () => {
      // Signing out and in as somebody else must not inherit the previous
      // person's settled question.
      fetchStub.resolves(
        new Response(
          JSON.stringify({
            show: false,
            reason: 'subscription_exists',
            trial_days: 14,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      );
      await getPlanChoice();
      await getPlanChoice();
      expect(fetchStub.callCount).to.equal(1);

      invalidateApiCaches();
      await getPlanChoice();
      expect(fetchStub.callCount).to.equal(2);
    });

    it('posts the free choice and throws when the write is refused', async () => {
      fetchStub.resolves(
        new Response(
          JSON.stringify({ show: false, reason: 'answered', trial_days: 14 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      );
      await recordFreePlanChoice();
      const [url, options] = fetchStub.firstCall.args;
      expect(String(url)).to.contain('/api/v1/billing/plan-choice');
      expect(options.method).to.equal('POST');

      // A screen that came down over a write that did not happen would ask
      // again on the next load, which reads as a bug, so this one throws.
      fetchStub.resolves(new Response('nope', { status: 500 }));
      let message = '';
      try {
        await recordFreePlanChoice();
      } catch (e) {
        message = (e as Error).message;
      }
      expect(message).to.contain('Could not record your choice');
    });
  });

  describe('getUsageNudges', () => {
    /**
     * Chrome, not content. Every failure has to read as "no nudges", because
     * the OSS console's whole guarantee is that a missing billing plugin
     * changes nothing on screen, and a console page must never fail over a
     * banner it was only going to be helpful with.
     */
    function body(payload: unknown, status = 200): Response {
      return new Response(JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    it('reads the envelope: nudges, window and ladder', async () => {
      fetchStub.resolves(
        body({
          nudges: [
            {
              key: 'max_agents',
              ratio: 0.67,
              used: 2,
              limit: 3,
              unit: 'agents',
              plan_id: 'free',
              unlocks_at_plan: 'pro',
            },
          ],
          analytics_window: {
            days: 90,
            unlocks_at_plan: 'team',
            unlocks_at_plan_name: 'Team',
          },
          threshold: 0.5,
          bands: [0.5, 0.8, 1.0],
        })
      );

      const payload = await getUsageNudges();

      expect(payload.nudges).to.have.length(1);
      expect(payload.nudges[0].key).to.equal('max_agents');
      expect(payload.analytics_window?.days).to.equal(90);
      expect(payload.analytics_window?.unlocks_at_plan_name).to.equal('Team');
      expect(payload.threshold).to.equal(0.5);
      expect(payload.bands).to.deep.equal([0.5, 0.8, 1.0]);
    });

    it('answers with nothing on OSS, where the route does not exist', async () => {
      fetchStub.resolves(body({ detail: 'Not Found' }, 404));
      expect(await getUsageNudges()).to.deep.equal(NO_USAGE_NUDGES);
    });

    it('answers with nothing when a server has no such method', async () => {
      fetchStub.resolves(body({ detail: 'Method Not Allowed' }, 405));
      expect(await getUsageNudges()).to.deep.equal(NO_USAGE_NUDGES);
    });

    it('answers with nothing when the network is gone', async () => {
      fetchStub.rejects(new TypeError('Failed to fetch'));
      expect(await getUsageNudges()).to.deep.equal(NO_USAGE_NUDGES);
    });

    it('answers with nothing when the body is not the contract', async () => {
      for (const payload of ['a string', 42, null]) {
        fetchStub.resolves(body(payload));
        expect(await getUsageNudges()).to.deep.equal(NO_USAGE_NUDGES);
      }

      fetchStub.resolves(new Response('not json', { status: 200 }));
      expect(await getUsageNudges()).to.deep.equal(NO_USAGE_NUDGES);
    });

    it('drops envelope fields that are the wrong shape', async () => {
      fetchStub.resolves(
        body({
          nudges: 'many',
          analytics_window: 90,
          threshold: 'half',
          bands: 3,
        })
      );
      expect(await getUsageNudges()).to.deep.equal(NO_USAGE_NUDGES);
    });

    it('still reads a server that answers with the list alone', async () => {
      // The shape this endpoint carried before the window and the ladder
      // joined it, so the order the two repositories deploy in cannot
      // silently empty the banner.
      fetchStub.resolves(
        body([
          {
            key: 'max_agents',
            ratio: 0.9,
            used: 9,
            limit: 10,
            unit: 'agents',
            plan_id: 'free',
            unlocks_at_plan: 'pro',
          },
        ])
      );

      const payload = await getUsageNudges();

      expect(payload.nudges).to.have.length(1);
      expect(payload.analytics_window).to.equal(null);
      expect(payload.threshold).to.equal(null);
    });
  });

  describe('a period outside the plan analytics window', () => {
    /**
     * The 403 the reporting endpoints answer with is a plan fact, not a
     * failure, and only the typed error lets a caller tell the two apart
     * (one is worth offering an upgrade for, the other is a bug report).
     */
    function refused(): Response {
      return new Response(
        JSON.stringify({
          detail: {
            code: HISTORY_UNAVAILABLE_CODE,
            available_from: '2026-03-18T00:00:00+00:00',
            message: "This period is outside your plan's analytics history.",
          },
        }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      );
    }

    async function caught(promise: Promise<unknown>): Promise<unknown> {
      try {
        await promise;
        return null;
      } catch (error) {
        return error;
      }
    }

    it('is a typed refusal on the gateway usage summary', async () => {
      fetchStub.resolves(refused());

      const error = await caught(getAccountGatewayUsageSummary({}));

      expect(isHistoryUnavailable(error)).to.equal(true);
      expect((error as Error).message).to.equal(
        "This period is outside your plan's analytics history."
      );
      expect((error as { availableFrom: string }).availableFrom).to.equal(
        '2026-03-18T00:00:00+00:00'
      );
    });

    it('is a typed refusal on the cost summary and the session timeline', async () => {
      fetchStub.resolves(refused());
      expect(
        isHistoryUnavailable(await caught(getCostAnalyticsSummary({})))
      ).to.equal(true);

      fetchStub.resolves(refused());
      expect(
        isHistoryUnavailable(
          await caught(getAccountRuntimeSessionActivityTimeline('session-1'))
        )
      ).to.equal(true);
    });

    it('leaves a plain 403 as the generic failure it is', async () => {
      // A permission problem is not something to sell a plan for.
      fetchStub.resolves(
        new Response(JSON.stringify({ detail: 'Forbidden' }), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        })
      );

      const error = await caught(getAccountGatewayUsageSummary({}));

      expect(isHistoryUnavailable(error)).to.equal(false);
      expect((error as Error).message).to.equal(
        'Failed to fetch account gateway usage summary'
      );
    });
  });
});
