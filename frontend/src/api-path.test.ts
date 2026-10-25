import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import {
  fetchWithAuth,
  getAccountRuntimeSessions,
  listRuntimeSessionArtifacts,
} from './api';
import { sessionArtifactPath } from './utils/session-artifacts';

describe('REST URL construction', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'example-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.resolves(new Response('{"items":[]}', { status: 200 }));
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('preserves the REST prefix, encoded identifier and query', async () => {
    await listRuntimeSessionArtifacts('example/session #1');
    expect(fetchStub.firstCall.args[0]).to.equal(
      '/api/v1/runtime-sessions/example%2Fsession%20%231/artifacts?limit=200'
    );
  });

  it('preserves query encoding for collection filters', async () => {
    await getAccountRuntimeSessions({ ownerUserId: 'Jane Doe&admin=false' });
    const url = new URL(fetchStub.firstCall.args[0], location.origin);
    expect(url.pathname).to.equal('/api/v1/runtime-sessions');
    expect(url.searchParams.get('owner_user_id')).to.equal(
      'Jane Doe&admin=false'
    );
    expect(url.searchParams.has('admin')).to.equal(false);
  });

  it('preserves artifact download paths outside the main API client', () => {
    expect(sessionArtifactPath('example/session', 'example?artifact')).to.equal(
      '/api/v1/runtime-sessions/example%2Fsession/artifacts/example%3Fartifact'
    );
  });

  for (const path of ['/oauth/token', '/mcp', '/openai/v1/models']) {
    it(`leaves the explicit non-REST path ${path} unchanged`, async () => {
      await fetchWithAuth(path);
      expect(fetchStub.firstCall.args[0]).to.equal(path);
      const headers = new Headers(fetchStub.firstCall.args[1]?.headers);
      expect(headers.has('Preloop-Server-Version')).to.equal(false);
    });
  }
});
