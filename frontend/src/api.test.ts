import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { Router } from '@vaadin/router';
import { fetchWithAuth, AuthedElement, getFlowExecutions } from './api.js';
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
});
