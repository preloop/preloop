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
  for (const status of [200, 404, 500]) {
    it(`reads each visible constraint once and preserves inventory when the decision returns ${status}`, async () => {
      const fetch = sinon.stub(window, 'fetch').callsFake(async (input) => {
        if (String(input).includes('/discovery-extensions?')) {
          return new Response(
            JSON.stringify({
              items: [key, key],
              next_cursor: null,
              completeness: 'complete',
            })
          );
        }
        return new Response(
          JSON.stringify({ ...key, version_constraint: '2.*' }),
          { status }
        );
      });
      const page = await getExtensions();
      expect(page.items).to.have.length(2);
      expect(fetch.callCount).to.equal(2);
      expect(String(fetch.lastCall.args[0])).to.contain(
        '/extension-approvals/claude_code/plugin/reviewer%40community'
      );
      for (const row of page.items) {
        expect(row.version_constraint).to.equal(
          status === 200 ? '2.*' : status === 404 ? null : undefined
        );
      }
    });
  }
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
  // clears another decision field or scans unrelated approvals;
  // no_claim=fetch fixture, not server authorization.
  it('preserves the existing constraint and note when changing state', async () => {
    const fetch = sinon.stub(window, 'fetch');
    fetch.onFirstCall().resolves(
      new Response(
        JSON.stringify({
          ...key,
          version_constraint: '1.4.*',
          note: 'Reviewed',
        })
      )
    );
    fetch.onSecondCall().resolves(new Response('{}'));
    await setExtensionApproval(key, 'unapproved');
    expect(fetch.callCount).to.equal(2);
    expect(fetch.firstCall.args[0]).to.equal(fetch.secondCall.args[0]);
    expect(JSON.parse(String(fetch.secondCall.args[1]?.body))).to.deep.equal({
      approval_state: 'unapproved',
      version_constraint: '1.4.*',
      note: 'Reviewed',
    });
    expect(String(fetch.firstCall.args[0])).to.contain('reviewer%40community');
  });
  // protects=bounded first approval; fails_when=missing decisions require a
  // list scan or failed reads overwrite metadata; no_claim=synthetic HTTP.
  for (const status of [404, 403, 500]) {
    it(`handles a decision lookup returning ${status}`, async () => {
      const fetch = sinon.stub(window, 'fetch');
      fetch.onFirstCall().resolves(new Response('{}', { status }));
      fetch.onSecondCall().resolves(new Response('{}'));
      let error: unknown;
      try {
        await setExtensionApproval(key, 'approved');
      } catch (caught) {
        error = caught;
      }
      expect(fetch.callCount).to.equal(status === 404 ? 2 : 1);
      if (status === 404) {
        expect(error).to.equal(undefined);
        expect(
          JSON.parse(String(fetch.secondCall.args[1]?.body))
        ).to.deep.equal({
          approval_state: 'approved',
          version_constraint: null,
          note: null,
        });
      } else expect(error).to.be.instanceOf(Error);
    });
  }
});
