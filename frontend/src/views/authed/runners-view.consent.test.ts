import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './runners-view';
import type { RunnersView } from './runners-view';
import { invalidateApiCaches } from '../../api';

const OWNER = '00000000-0000-4000-8000-000000000001';

function runner(id: string, owner: string, consent = 'ask_me') {
  return {
    id,
    name: `runner-${id}`,
    status: 'online',
    registered_by_user_id: owner,
    registered_by_email: 'jane@example.com',
    flow_execution_consent: consent,
    labels: [],
  };
}

describe('RunnersView flow consent', () => {
  let fetchStub: sinon.SinonStub;
  let patched: unknown = null;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    invalidateApiCaches();
    patched = null;
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.includes('/auth/users/me')) return json({ id: OWNER });
        if (url.includes('/consent')) {
          patched = JSON.parse(String(init?.body || '{}'));
          return json({ ...runner('a', OWNER), ...(patched as object) });
        }
        if (url.includes('/api/v1/runners')) {
          return json([
            runner('a', OWNER),
            runner('b', 'someone-else', 'always'),
          ]);
        }
        return json({});
      });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('gives the owner a select and everyone else plain text', async () => {
    const el = await fixture<RunnersView>(html`<runners-view></runners-view>`);
    await waitUntil(
      () =>
        el.shadowRoot!.querySelectorAll('owner-consent-control').length === 2
    );
    const [mine, theirs] = Array.from(
      el.shadowRoot!.querySelectorAll('owner-consent-control')
    );
    await waitUntil(() => mine.editable);
    expect(theirs.editable).to.equal(false);
    expect(theirs.ownerOnly).to.contain('runner owner');

    mine.dispatchEvent(
      new CustomEvent('consent-change', { detail: { value: 'never' } })
    );
    await waitUntil(() => patched !== null);
    expect(patched).to.deep.equal({ flow_execution_consent: 'never' });
    await waitUntil(() => mine.value === 'never');
  });
});
