import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './subaccounts-view';
import type { SubaccountsView } from './subaccounts-view';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
} from '../../../test-helpers/capability-api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { answerConfirmDialog } from '../../../utils/test-confirm-dialog';

const LIST = '/api/v1/accounts/acc-root/subaccounts';

const rows = (el: SubaccountsView) =>
  [...el.shadowRoot!.querySelectorAll('tbody tr')].map((tr) =>
    tr.getAttribute('data-id')
  );

async function ready(el: SubaccountsView) {
  await waitUntil(
    () => !el.shadowRoot!.querySelector('sl-spinner'),
    'view did not finish loading'
  );
  await el.updateComplete;
}

function setValue(el: SubaccountsView, id: string, value: string) {
  const input = el.shadowRoot!.querySelector<HTMLInputElement>(`#${id}`)!;
  input.value = value;
}

function button(el: SubaccountsView, label: string, scope?: Element) {
  return [...(scope ?? el.shadowRoot!).querySelectorAll('sl-button')].find(
    (b) => b.textContent?.trim() === label
  ) as HTMLElement;
}

describe('subaccounts-view', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
    resetConfirmDialogForTests();
  });

  it('lists the subaccounts of the current account', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        {
          path: LIST,
          body: {
            items: [
              { id: 'sub-a', name: 'North', tags: { site: 'north' } },
              { id: 'sub-b', name: 'South', tags: {} },
            ],
          },
        },
      ],
    });
    const el = await fixture<SubaccountsView>(
      html`<subaccounts-view></subaccounts-view>`
    );
    await ready(el);
    expect(rows(el)).to.eql(['sub-a', 'sub-b']);
    expect(el.shadowRoot!.textContent).to.contain('site=north');
  });

  it('explains, without a toast, when the endpoint is missing', async () => {
    api = mockApi();
    const before = toastCount();
    const el = await fixture<SubaccountsView>(
      html`<subaccounts-view></subaccounts-view>`
    );
    await waitUntil(() => api.callsTo(LIST).length === 1);
    await waitUntil(() => el.shadowRoot!.querySelector('.off-state'));
    // A bookmarked link gets a title and a reason, not a blank page.
    expect(
      (el.shadowRoot!.querySelector('view-header') as any).headerText
    ).to.equal('Subaccounts');
    expect(el.shadowRoot!.querySelector('.off-state')!.textContent).to.contain(
      "aren't available on this deployment"
    );
    expect(el.shadowRoot!.querySelector('#new-name')).to.not.exist;
    expect(toastCount()).to.equal(before);
  });

  it('creates a subaccount with its tags', async () => {
    const created: unknown[] = [];
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        { path: LIST, body: { items: [] } },
        {
          method: 'POST',
          path: LIST,
          status: 201,
          body: (call) => {
            created.push(call.body);
            return { id: 'sub-new', name: 'North', tags: { site: 'north' } };
          },
        },
      ],
    });
    const el = await fixture<SubaccountsView>(
      html`<subaccounts-view></subaccounts-view>`
    );
    await ready(el);
    setValue(el, 'new-name', 'North');
    setValue(el, 'new-tags', 'site=north');
    button(el, 'Create subaccount').click();
    await waitUntil(() => api.callsTo(LIST, 'GET').length === 2);
    expect(created).to.eql([{ name: 'North', tags: { site: 'north' } }]);
  });

  it('says not found for a subaccount that is not in this account', async () => {
    // A sibling's id: the server answers 404 and returns nothing of it.
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        {
          path: LIST,
          body: { items: [{ id: 'sub-sibling', name: 'Stale', tags: {} }] },
        },
        { method: 'PATCH', path: `${LIST}/sub-sibling`, status: 404 },
      ],
    });
    const el = await fixture<SubaccountsView>(
      html`<subaccounts-view></subaccounts-view>`
    );
    await ready(el);
    const before = toastCount();
    button(el, 'Edit').click();
    await el.updateComplete;
    setValue(el, 'name-sub-sibling', 'Renamed');
    button(el, 'Save').click();
    await waitUntil(() => el.shadowRoot!.querySelector('.error'));
    expect(el.shadowRoot!.querySelector('.error')!.textContent).to.equal(
      'That subaccount is not in this account.'
    );
    expect(
      el.shadowRoot!.querySelector('.error')!.getAttribute('role')
    ).to.equal('alert');
    expect(api.callsTo(`${LIST}/sub-sibling`, 'PATCH')[0].body).to.eql({
      name: 'Renamed',
      tags: {},
    });
    // The view stays (a missing item is not a missing capability).
    expect(el.shadowRoot!.querySelector('view-header')).to.exist;
    expect(toastCount()).to.equal(before);
  });

  describe('as the Account page card (embedded)', () => {
    async function card(memberships: unknown[] | null) {
      api = mockApi({
        capabilities: ['account_hierarchy', 'multi_account'],
        routes: [
          ...(memberships
            ? [{ path: '/api/v1/me/memberships', body: memberships }]
            : []),
          {
            path: LIST,
            body: { items: [{ id: 'sub-a', name: 'North', tags: {} }] },
          },
          {
            method: 'POST',
            path: '/api/v1/auth/switch-account',
            body: { access_token: 'sub-access', refresh_token: 'sub-refresh' },
          },
        ],
      });
      const navigated: string[] = [];
      const el = await fixture<SubaccountsView>(
        html`<subaccounts-view
          embedded
          .navigate=${(url: string) => navigated.push(url)}
        ></subaccounts-view>`
      );
      await waitUntil(() => !(el as any).loading, 'card did not load');
      await el.updateComplete;
      return { el, navigated };
    }

    it('renders a Subaccounts card with create, list and a link to Access grants', async () => {
      const { el } = await card([
        { account_id: 'acc-root', account_name: 'Root' },
      ]);
      const sl = el.shadowRoot!.querySelector('sl-card#subaccounts');
      expect(sl).to.exist;
      expect(sl!.textContent).to.contain('Subaccounts');
      expect(el.shadowRoot!.querySelector('view-header')).to.equal(null);
      expect(rows(el)).to.eql(['sub-a']);
      expect(button(el, 'Create subaccount')).to.exist;
      for (const label of ['Open', 'Edit', 'Detach', 'Delete']) {
        expect(button(el, label), label).to.exist;
      }
      expect(
        el.shadowRoot!.querySelector(
          'a[href="/console/settings/access-grants"]'
        )?.textContent
      ).to.contain('Access grants');
    });

    it('treats the account as a root when memberships are unavailable', async () => {
      const { el } = await card(null);
      expect(el.shadowRoot!.querySelector('sl-card#subaccounts')).to.exist;
    });

    it('is absent on a subaccount', async () => {
      const { el } = await card([
        {
          account_id: 'acc-root',
          account_name: 'EU',
          parent_account_id: 'acc-parent',
        },
      ]);
      expect(el.shadowRoot!.querySelector('sl-card')).to.equal(null);
      expect(api.calls.some((c) => c.path === LIST)).to.equal(false);
    });

    it('is absent without the extension, and says nothing', async () => {
      const before = toastCount();
      api = mockApi({ capabilities: [] });
      const el = await fixture<SubaccountsView>(
        html`<subaccounts-view embedded></subaccounts-view>`
      );
      await waitUntil(() => !(el as any).loading, 'card did not load');
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('sl-card')).to.equal(null);
      expect(el.shadowRoot!.textContent?.trim()).to.equal('');
      expect(toastCount()).to.equal(before);
    });

    it('opens a subaccount by switching to it', async () => {
      const { el, navigated } = await card([
        { account_id: 'acc-root', account_name: 'Root' },
      ]);
      button(el, 'Open').click();
      await waitUntil(() => navigated.length === 1, 'did not navigate');
      const call = api.calls.find(
        (c) => c.path === '/api/v1/auth/switch-account'
      );
      expect(call?.body).to.eql({ account_id: 'sub-a' });
    });
  });
});
