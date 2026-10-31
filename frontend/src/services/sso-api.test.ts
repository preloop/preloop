import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { discoverSso, localSsoUrl, ssoApi } from './sso-api';
import { invalidateApiCaches } from '../api';

describe('SSO API safety', () => {
  afterEach(() => {
    sinon.restore();
    invalidateApiCaches();
    localStorage.clear();
  });
  it('rejects cross-origin, script, credentialed and non-SSO redirects', () => {
    for (const value of [
      'https://evil.example/api/v1/auth/sso/start',
      'javascript:alert(1)',
      '//evil.example/api/v1/auth/sso/start',
      '/console',
      '/api/v1/auth/sso/a/start#token',
      `${location.protocol}//user:password@${location.host}/api/v1/auth/sso/a/start`,
    ])
      expect(localSsoUrl(value)).to.equal(null);
    expect(localSsoUrl('/api/v1/auth/sso/a/start')).to.equal(
      `${location.origin}/api/v1/auth/sso/a/start`
    );
  });
  it('discovers without sending a stored bearer credential', async () => {
    localStorage.setItem('accessToken', 'synthetic-stored-token');
    const fetch = sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ sso_url: '/api/v1/auth/sso/a/start' }), {
        status: 200,
      })
    );
    expect(await discoverSso('person@example.com')).to.equal(
      `${location.origin}/api/v1/auth/sso/a/start`
    );
    expect(fetch.firstCall.args[1]).to.include({
      credentials: 'same-origin',
      method: 'POST',
      cache: 'no-store',
    });
    expect(fetch.firstCall.args[1]!.headers).to.deep.equal({
      'Content-Type': 'application/json',
    });
  });
  it('uses identical fallback for unavailable or unknown-domain discovery', async () => {
    const fetch = sinon.stub(window, 'fetch');
    fetch.onFirstCall().resolves(new Response('{}', { status: 404 }));
    fetch
      .onSecondCall()
      .resolves(
        new Response(JSON.stringify({ sso_url: null }), { status: 200 })
      );
    expect(await discoverSso('unknown@example.com')).to.equal(null);
    expect(await discoverSso('known@example.com')).to.equal(null);
  });
  it('POSTs authenticated test sign-in and validates the returned start URL', async () => {
    localStorage.setItem('accessToken', 'synthetic-token');
    const fetch = sinon
      .stub(window, 'fetch')
      .resolves(
        new Response(
          JSON.stringify({ sso_url: '/api/v1/auth/sso/a/start?test=proof' })
        )
      );
    expect(await ssoApi.testSignIn('account-a', 'a')).to.equal(
      `${location.origin}/api/v1/auth/sso/a/start?test=proof`
    );
    expect(String(fetch.firstCall.args[0])).to.equal(
      '/api/v1/accounts/account-a/sso/connections/a/test-sign-in'
    );
    expect(fetch.firstCall.args[1]!.method).to.equal('POST');
    fetch.resolves(
      new Response(JSON.stringify({ sso_url: 'https://evil.example/' }))
    );
    let rejected = false;
    try {
      await ssoApi.testSignIn('account-a', 'a');
    } catch {
      rejected = true;
    }
    expect(rejected).to.equal(true);
  });

  it('requires a fresh proof on a rejected enforcement update without reflecting its payload', async () => {
    localStorage.setItem('accessToken', 'synthetic-token');
    sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ detail: 'synthetic-private-input' }), {
        status: 422,
      })
    );
    let error: unknown;
    try {
      await ssoApi.savePolicy('account-a', {
        enforce: true,
        break_glass_owner_ids: null,
        session_max_age_seconds: null,
      });
    } catch (caught) {
      error = caught;
    }
    expect((error as Error).message).to.contain('fresh test sign-in');
    expect((error as Error).message).not.to.contain('synthetic-private-input');
  });

  it('never reflects a validation body containing a secret into an error', async () => {
    localStorage.setItem('accessToken', 'synthetic-token');
    sinon
      .stub(window, 'fetch')
      .resolves(
        new Response(
          JSON.stringify({ detail: [{ input: 'synthetic-private-key' }] }),
          { status: 422 }
        )
      );
    try {
      await ssoApi.saveConnection('account-a', null, {
        saml_sp_signing_key: 'synthetic-private-key',
      });
      throw new Error('Expected rejection');
    } catch (error) {
      expect((error as Error).message).to.equal(
        'The change could not be saved. Check the configuration and try again.'
      );
    }
  });
});
