import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './subaccounts-view';
import type { SubaccountsView } from './subaccounts-view';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
} from '../../../test-helpers/capability-api';

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

  it('renders nothing and raises no toast when the endpoint is missing', async () => {
    api = mockApi();
    const before = toastCount();
    const el = await fixture<SubaccountsView>(
      html`<subaccounts-view></subaccounts-view>`
    );
    await waitUntil(() => api.callsTo(LIST).length === 1);
    await waitUntil(() => el.shadowRoot!.childElementCount === 0);
    expect(el.shadowRoot!.textContent!.trim()).to.equal('');
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
    expect(api.callsTo(`${LIST}/sub-sibling`, 'PATCH')[0].body).to.eql({
      name: 'Renamed',
      tags: {},
    });
    // The view stays (a missing item is not a missing capability).
    expect(el.shadowRoot!.querySelector('view-header')).to.exist;
    expect(toastCount()).to.equal(before);
  });
});
