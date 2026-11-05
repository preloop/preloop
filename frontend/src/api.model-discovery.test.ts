import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { getAvailableModelsForProvider } from './api';
import { Router } from './router';

describe('provider discovery authentication contract', () => {
  let sandbox: sinon.SinonSandbox;
  let fetchStub: sinon.SinonStub;
  let navigate: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'synthetic-access');
    localStorage.setItem('refreshToken', 'synthetic-refresh');
    sandbox = sinon.createSandbox();
    fetchStub = sandbox.stub(window, 'fetch');
    navigate = sandbox.stub(Router, 'go');
  });
  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('keeps a healthy access-only session after a provider rejects its key', async () => {
    localStorage.removeItem('refreshToken');
    fetchStub.resolves(
      new Response(
        JSON.stringify({
          detail: 'Invalid provider key. Check the key and try again.',
        }),
        { status: 400 }
      )
    );
    try {
      await getAvailableModelsForProvider('openai', 'synthetic-key');
      expect.fail('expected provider rejection');
    } catch (error) {
      expect((error as Error).message).to.contain('Invalid provider key');
    }
    expect(fetchStub.callCount).to.equal(1);
    expect(localStorage.getItem('accessToken')).to.equal('synthetic-access');
    expect(navigate.called).to.equal(false);
  });

  it('refreshes genuine session expiry and retries discovery with the same cancellation signal', async () => {
    const controller = new AbortController();
    fetchStub.onFirstCall().resolves(
      new Response(JSON.stringify({ detail: 'Token expired' }), {
        status: 401,
      })
    );
    fetchStub.onSecondCall().resolves(
      new Response(
        JSON.stringify({
          access_token: 'renewed-access',
          refresh_token: 'renewed-refresh',
        })
      )
    );
    fetchStub
      .onThirdCall()
      .resolves(
        new Response(
          JSON.stringify({ models: ['example-model'], source: 'live' })
        )
      );
    const result = await getAvailableModelsForProvider(
      'openai',
      'synthetic-key',
      'llm',
      undefined,
      undefined,
      undefined,
      controller.signal
    );
    expect(result.models).to.deep.equal(['example-model']);
    expect(fetchStub.callCount).to.equal(3);
    expect(String(fetchStub.secondCall.args[0])).to.contain('/auth/refresh');
    expect(fetchStub.thirdCall.args[1].signal).to.equal(controller.signal);
    expect(navigate.called).to.equal(false);
  });

  it('ends a revoked console session during discovery', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({ detail: 'Token revoked' }), {
          status: 401,
        })
    );
    try {
      await getAvailableModelsForProvider('openai', 'synthetic-key');
      expect.fail('expected session rejection');
    } catch (error) {
      expect((error as Error).message).to.contain('Failed to refresh token');
    }
    expect(localStorage.getItem('accessToken')).to.equal(null);
    expect(localStorage.getItem('refreshToken')).to.equal(null);
    expect(navigate.calledWith('/login')).to.equal(true);
  });
});
