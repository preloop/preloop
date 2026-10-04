import { expect } from '@open-wc/testing';
import {
  CI_ACTIONS,
  ciAdministration,
  completeCiCapabilities,
  type CiGrant,
} from './ci-administration-api';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from './test-helpers/capability-api';
const BASE = '/api/v1/ci-identities';
const grant: CiGrant = {
  version: 1,
  project_id: 'project-a',
  flow_id: 'flow-a',
  actions: [...CI_ACTIONS],
};
describe('restricted CI human administration client', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });
  it('calls actual closed lifecycle paths with JSON bodies and no secret cache or query strings', async () => {
    api = mockApi({ fallback: { status: 200, body: {} } });
    await ciAdministration.capabilities();
    await ciAdministration.list();
    await ciAdministration.get('principal-a');
    await ciAdministration.preview(grant);
    await ciAdministration.create('Review', grant, '2030-01-01T00:00:00Z');
    await ciAdministration.change('principal-a', { enabled: false });
    await ciAdministration.issue('principal-a');
    await ciAdministration.rotate('principal-a', 'key-a');
    await ciAdministration.revoke('principal-a', 'key-a');
    await ciAdministration.subscribe(
      'principal-a',
      'key-a',
      'https://example.com/completed',
      'Review'
    );
    expect(api.calls.map((call) => [call.method, call.path, call.body])).to.eql(
      [
        ['GET', BASE + '/capabilities', undefined],
        ['GET', BASE, undefined],
        ['GET', BASE + '/principal-a', undefined],
        ['POST', BASE + '/preview', { grant }],
        [
          'POST',
          BASE,
          { name: 'Review', grant, expires_at: '2030-01-01T00:00:00Z' },
        ],
        ['PATCH', BASE + '/principal-a', { enabled: false }],
        ['POST', BASE + '/principal-a/keys', {}],
        ['POST', BASE + '/principal-a/keys/key-a/rotate', {}],
        ['DELETE', BASE + '/principal-a/keys/key-a', undefined],
        [
          'POST',
          BASE + '/principal-a/subscriptions',
          {
            key_id: 'key-a',
            url: 'https://example.com/completed',
            description: 'Review',
            event_types: ['flow.execution.finished'],
          },
        ],
      ]
    );
    for (const call of api.calls) {
      expect(call.search).to.equal('');
      expect(call.authorization).to.equal('Bearer test-access');
    }
    for (const call of api.stub.getCalls()) {
      const init = call.args[1] as RequestInit;
      expect(init.cache).to.equal('no-store');
      if (init.body)
        expect(new Headers(init.headers).get('Content-Type')).to.equal(
          'application/json'
        );
    }
  });
  it('does not copy server errors into exceptions or storage', async () => {
    api = mockApi({
      fallback: { status: 400, body: { detail: 'synthetic-private-response' } },
    });
    let failure = '';
    try {
      await ciAdministration.issue('principal-a');
    } catch (error) {
      failure = String(error);
    }
    expect(failure).to.contain('Restricted CI request');
    expect(failure).not.to.contain('synthetic-private-response');
    expect(JSON.stringify(localStorage)).not.to.contain(
      'synthetic-private-response'
    );
  });
  it('requires literal availability and entire unique action catalogue', () => {
    const on = {
      available: true,
      can_view: true,
      can_manage: true,
      supported_actions: [...CI_ACTIONS],
    };
    expect(completeCiCapabilities(on)).to.equal(true);
    expect(completeCiCapabilities(null)).to.equal(false);
    expect(completeCiCapabilities({ ...on, available: false })).to.equal(false);
    expect(
      completeCiCapabilities({ ...on, supported_actions: CI_ACTIONS.slice(1) })
    ).to.equal(false);
    expect(
      completeCiCapabilities({
        ...on,
        supported_actions: [...CI_ACTIONS.slice(1), CI_ACTIONS[1]],
      })
    ).to.equal(false);
    expect(
      completeCiCapabilities({
        ...on,
        available: 'true',
      } as unknown as typeof on)
    ).to.equal(false);
  });
});
