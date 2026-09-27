import { expect } from '@open-wc/testing';
import { getUserProfile } from './api';
import { isCapabilityOff } from './capabilities';
import {
  applyTokenPair,
  getSharedResource,
  getSubaccount,
  isNotFound,
  lastUsedAccounts,
  listSubaccounts,
  switchAccount,
  switchTarget,
} from './hierarchy-api';
import { mockApi, type MockApi } from './test-helpers/capability-api';

describe('hierarchy-api', () => {
  let api: MockApi | undefined;
  const originalPath = window.location.pathname;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'old-access');
    localStorage.setItem('refreshToken', 'old-refresh');
  });

  afterEach(() => {
    api?.restore();
    api = undefined;
    localStorage.clear();
    window.history.replaceState({}, '', originalPath);
  });

  describe('404 semantics', () => {
    it('reads a collection 404 as capability off', async () => {
      api = mockApi();
      const error = await listSubaccounts('acc-root').catch((e) => e);
      expect(isCapabilityOff(error)).to.equal(true);
    });

    it('reads an item 404 as not found, not capability off', async () => {
      api = mockApi();
      const error = await getSubaccount('acc-root', 'sub-sibling').catch(
        (e) => e
      );
      expect(isNotFound(error)).to.equal(true);
      expect(isCapabilityOff(error)).to.equal(false);
    });

    it('accepts a bare list or an items envelope', async () => {
      api = mockApi({
        routes: [
          {
            path: '/api/v1/accounts/acc-root/subaccounts',
            body: { items: [{ id: 's1', name: 'EU' }] },
          },
        ],
      });
      expect((await listSubaccounts('acc-root')).map((s) => s.id)).to.eql([
        's1',
      ]);
    });

    it('never returns a sibling account resource (isolation)', async () => {
      api = mockApi({
        routes: [
          {
            // The server answers another account's id with 404.
            path: '/api/v1/accounts/acc-root/shared-resources/ai_model/m-sibling',
            status: 404,
          },
        ],
      });
      const error = await getSharedResource(
        'acc-root',
        'ai_model',
        'm-sibling'
      ).catch((e) => e);
      expect(isNotFound(error)).to.equal(true);
    });
  });

  describe('switching accounts', () => {
    it('replaces both tokens and sends no request with the old token after the switch', async () => {
      window.history.replaceState({}, '', '/console/ai-models/m-1?tab=x');
      api = mockApi({
        routes: [
          {
            method: 'POST',
            path: '/api/v1/auth/switch-account',
            body: { access_token: 'new-access', refresh_token: 'new-refresh' },
          },
          { path: '/api/v1/ai-models/m-1', body: { id: 'm-1' } },
        ],
      });
      // Warm the profile cache under the old account.
      await getUserProfile();

      let navigatedTo = '';
      await switchAccount('acc-2', (url) => {
        navigatedTo = url;
      });

      expect(localStorage.getItem('accessToken')).to.equal('new-access');
      expect(localStorage.getItem('refreshToken')).to.equal('new-refresh');
      const switchCall = api.callsTo('/api/v1/auth/switch-account', 'POST')[0];
      expect(switchCall.body).to.eql({ account_id: 'acc-2' });
      expect(switchCall.authorization).to.equal('Bearer old-access');

      const after = api.calls.slice(api.calls.indexOf(switchCall) + 1);
      expect(after.length).to.be.greaterThan(0);
      for (const call of after) {
        expect(call.authorization, call.path).to.not.equal('Bearer old-access');
      }

      // The profile cache of the old account is gone: the next read refetches
      // with the new token.
      const before = api.callsTo('/api/v1/auth/users/me').length;
      await getUserProfile();
      const profileCalls = api.callsTo('/api/v1/auth/users/me');
      expect(profileCalls.length).to.equal(before + 1);
      expect(profileCalls[profileCalls.length - 1].authorization).to.equal(
        'Bearer new-access'
      );

      // The page's model exists in the new account, so the page stays.
      expect(navigatedTo).to.equal('/console/ai-models/m-1?tab=x');
      expect(lastUsedAccounts()[0]).to.equal('acc-2');
    });

    it('goes to the dashboard when the page resource is not in the new account', async () => {
      api = mockApi();
      expect(await switchTarget('/console/ai-models/m-1')).to.equal('/console');
      expect(await switchTarget('/console/agents/a-1')).to.equal('/console');
      expect(await switchTarget('/console/settings/profile')).to.equal(
        '/console/settings/profile'
      );
      expect(await switchTarget('/pricing')).to.equal('/console');
    });

    it('keeps both old tokens when the server returns only one', async () => {
      api = mockApi({
        routes: [
          {
            method: 'POST',
            path: '/api/v1/auth/switch-account',
            body: { access_token: 'new-access' },
          },
        ],
      });
      let navigated = false;
      const error = await switchAccount('acc-2', () => {
        navigated = true;
      }).catch((e) => e);
      expect(error).to.be.instanceOf(Error);
      expect(navigated).to.equal(false);
      expect(localStorage.getItem('accessToken')).to.equal('old-access');
      expect(localStorage.getItem('refreshToken')).to.equal('old-refresh');
    });

    it('announces the switch so listeners drop per-account state', () => {
      let heard = 0;
      const listener = () => heard++;
      window.addEventListener('auth-change', listener);
      applyTokenPair({ access_token: 'a', refresh_token: 'r' });
      window.removeEventListener('auth-change', listener);
      expect(heard).to.equal(1);
    });
  });
});
