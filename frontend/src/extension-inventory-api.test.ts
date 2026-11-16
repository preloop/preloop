import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { getExtensions, setExtensionApproval } from './extension-inventory-api';
import type { ExtensionKey } from './extension-inventory-api';

const key: ExtensionKey = {
  agent_kind: 'claude_code',
  extension_kind: 'plugin',
  extension_id: 'reviewer@community',
  approval_state: 'unknown',
};

describe('extension inventory API', () => {
  beforeEach(() => localStorage.setItem('accessToken', 'synthetic-token'));
  afterEach(() => localStorage.removeItem('accessToken'));
  afterEach(() => sinon.restore());
  // protects=REST context and filters; fails_when=device reads become account
  // aggregates; no_claim=fetch stub, backend tested separately.
  it('encodes device filters and requests account aggregates', async () => {
    const fetch = sinon.stub(window, 'fetch').callsFake(
      async () =>
        new Response(
          JSON.stringify({
            items: [],
            next_cursor: null,
            completeness: 'complete',
          })
        )
    );
    await getExtensions({
      fingerprint: 'a'.repeat(64),
      state: 'unknown',
      source_kind: 'git',
    });
    const device = new URL(String(fetch.firstCall.args[0]), location.origin);
    expect(device.pathname).to.equal('/api/v1/agents/discovery-extensions');
    expect(device.searchParams.get('fingerprint')).to.equal('a'.repeat(64));
    expect(device.searchParams.has('group')).to.equal(false);
    await getExtensions();
    expect(String(fetch.lastCall.args[0])).to.contain('group=extension');
  });
  // protects=existing version constraints and notes; fails_when=state change
  // clears another decision field; no_claim=fetch fixture and pagination.
  it('preserves the existing constraint and note when changing state', async () => {
    const fetch = sinon.stub(window, 'fetch');
    fetch
      .onFirstCall()
      .resolves(
        new Response(JSON.stringify({ items: [], next_cursor: 'next' }))
      );
    fetch.onSecondCall().resolves(
      new Response(
        JSON.stringify({
          items: [{ ...key, version_constraint: '1.4.*', note: 'Reviewed' }],
          next_cursor: null,
        })
      )
    );
    fetch.onThirdCall().resolves(new Response('{}'));
    await setExtensionApproval(key, 'unapproved');
    expect(String(fetch.secondCall.args[0])).to.contain('cursor=next');
    expect(JSON.parse(String(fetch.thirdCall.args[1]?.body))).to.deep.equal({
      approval_state: 'unapproved',
      version_constraint: '1.4.*',
      note: 'Reviewed',
    });
    expect(String(fetch.thirdCall.args[0])).to.contain('reviewer%40community');
  });
});
