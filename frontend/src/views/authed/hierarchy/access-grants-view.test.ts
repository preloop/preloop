import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './access-grants-view';
import type { AccessGrantsView } from './access-grants-view';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
} from '../../../test-helpers/capability-api';

const GRANTS = '/api/v1/accounts/acc-root/access-grants';
const SUBS = '/api/v1/accounts/acc-root/subaccounts';

const baseRoutes = (grants: unknown[]) => [
  { path: GRANTS, body: { items: grants } },
  {
    path: SUBS,
    body: {
      items: [
        { id: 'sub-a', name: 'North', tags: {} },
        { id: 'sub-b', name: 'South', tags: {} },
      ],
    },
  },
  {
    path: '/api/v1/users',
    body: {
      users: [{ id: 'user-2', username: 'grace', email: 'g@example.com' }],
      total: 1,
    },
  },
  {
    path: '/api/v1/teams',
    body: { teams: [{ id: 'team-1', name: 'Ops' }], total: 1 },
  },
];

async function ready(el: AccessGrantsView) {
  await waitUntil(() => !el.shadowRoot!.querySelector('sl-spinner'));
  await el.updateComplete;
}

describe('access-grants-view', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('lists grants with names for subjects and subaccounts', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: baseRoutes([
        {
          id: 'g1',
          subject_type: 'user',
          subject_id: 'user-2',
          level: 'operate',
          target: 'selected',
          subaccount_ids: ['sub-b'],
        },
        {
          id: 'g2',
          subject_type: 'team',
          subject_id: 'team-1',
          level: 'read',
          target: 'all',
        },
      ]),
    });
    const el = await fixture<AccessGrantsView>(
      html`<access-grants-view></access-grants-view>`
    );
    await ready(el);
    const text = el.shadowRoot!.querySelector('tbody')!.textContent!;
    expect(text).to.contain('grace');
    expect(text).to.contain('South');
    expect(text).to.contain('Team: Ops');
    expect(text).to.contain('All subaccounts');
  });

  it('renders nothing and raises no toast when the endpoint is missing', async () => {
    api = mockApi();
    const before = toastCount();
    const el = await fixture<AccessGrantsView>(
      html`<access-grants-view></access-grants-view>`
    );
    await waitUntil(() => api.callsTo(GRANTS).length === 1);
    await waitUntil(() => el.shadowRoot!.childElementCount === 0);
    expect(toastCount()).to.equal(before);
    expect(api.callsTo('/api/v1/users')).to.have.length(0);
  });

  it('refuses a grant without a subject', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: baseRoutes([]),
    });
    const el = await fixture<AccessGrantsView>(
      html`<access-grants-view></access-grants-view>`
    );
    await ready(el);
    el.shadowRoot!.querySelector('form')!.requestSubmit();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.error')!.textContent).to.equal(
      'Choose a user or team.'
    );
    expect(api.callsTo(GRANTS, 'POST')).to.have.length(0);
  });

  it('says not found when revoking a grant of another account', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        ...baseRoutes([
          {
            id: 'g-foreign',
            subject_type: 'user',
            subject_id: 'user-2',
            level: 'read',
            target: 'all',
          },
        ]),
        { method: 'DELETE', path: `${GRANTS}/g-foreign`, status: 404 },
      ],
    });
    const el = await fixture<AccessGrantsView>(
      html`<access-grants-view></access-grants-view>`
    );
    await ready(el);
    const before = toastCount();
    const revoke = [...el.shadowRoot!.querySelectorAll('sl-button')].find(
      (b) => b.textContent?.trim() === 'Revoke'
    ) as HTMLElement;
    revoke.click();
    await waitUntil(() => el.shadowRoot!.querySelector('.error'));
    expect(el.shadowRoot!.querySelector('.error')!.textContent).to.equal(
      'That grant is not in this account.'
    );
    expect(el.shadowRoot!.querySelector('view-header')).to.exist;
    expect(toastCount()).to.equal(before);
  });
});
