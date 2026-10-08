import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './account-switcher';
import type { AccountSwitcher } from './account-switcher';
import { groupMemberships, type MembershipGroup } from './membership-order';
import type { Membership } from '../../../hierarchy-api';
import {
  mockApi,
  toastCount,
  type MockApi,
} from '../../../test-helpers/capability-api';

const m = (
  id: string,
  name: string,
  parent: string | null = null,
  last_used_at: string | null = null
): Membership => ({
  account_id: id,
  account_name: name,
  slug: id,
  parent_account_id: parent,
  last_used_at,
});

const names = (groups: MembershipGroup[]) =>
  groups.map((g) => [
    g.root.account_name,
    ...g.children.map((c) => c.account_name),
  ]);

describe('groupMemberships', () => {
  it('groups by tree, at any depth', () => {
    const groups = groupMemberships([
      m('eu', 'EU', 'root'),
      m('root', 'Root'),
      m('eu-de', 'EU DE', 'eu'),
      m('other', 'Other'),
    ]);
    expect(names(groups)).to.eql([['Other'], ['Root', 'EU', 'EU DE']]);
    const root = groups.find((g) => g.root.account_id === 'root')!;
    expect(root.depth.get('eu')).to.equal(1);
    expect(root.depth.get('eu-de')).to.equal(2);
  });

  it('puts the last used first: local history, then server recency', () => {
    const list = [
      m('a', 'A', null, '2026-09-01T00:00:00Z'),
      m('b', 'B', null, '2026-09-20T00:00:00Z'),
      m('c', 'C'),
    ];
    expect(names(groupMemberships(list))).to.eql([['B'], ['A'], ['C']]);
    expect(names(groupMemberships(list, ['c']))).to.eql([['C'], ['B'], ['A']]);
  });

  it('ranks a tree by its most recent member', () => {
    const groups = groupMemberships(
      [m('x', 'X'), m('root', 'Root'), m('sub', 'Sub', 'root')],
      ['sub']
    );
    expect(groups[0].root.account_id).to.equal('root');
  });

  it('lets a subaccount without its parent head its own group', () => {
    expect(names(groupMemberships([m('sub', 'Sub', 'hidden')]))).to.eql([
      ['Sub'],
    ]);
  });
});

describe('account-switcher', () => {
  let api: MockApi | undefined;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'old-access');
    localStorage.setItem('refreshToken', 'old-refresh');
  });

  afterEach(() => {
    api?.restore();
    api = undefined;
    localStorage.clear();
  });

  async function render(memberships: Membership[] | null) {
    api = mockApi({
      capabilities: ['multi_account'],
      routes: [
        ...(memberships
          ? [{ path: '/api/v1/me/memberships', body: memberships }]
          : []),
        {
          method: 'POST',
          path: '/api/v1/auth/switch-account',
          body: { access_token: 'new-access', refresh_token: 'new-refresh' },
        },
      ],
    });
    const navigated: string[] = [];
    const el = await fixture<AccountSwitcher>(
      html`<account-switcher
        .navigate=${(url: string) => navigated.push(url)}
      ></account-switcher>`
    );
    await waitUntil(() => (el as any).loaded, 'switcher did not load');
    await el.updateComplete;
    return { el, navigated };
  }

  it('gives a single membership a menu: the account and New account', async () => {
    const { el } = await render([m('acc-root', 'Root')]);
    expect(
      el.shadowRoot!.querySelector('[data-testid="account-trigger"]')
        ?.textContent
    ).to.contain('Root');
    const menu = el.shadowRoot!.querySelector('sl-dropdown sl-menu')!;
    const children = [...menu.children].map((c) =>
      c.tagName === 'SL-DIVIDER'
        ? 'divider'
        : ((c as HTMLElement).dataset.account ??
          (c as HTMLElement).dataset.testid)
    );
    expect(children).to.eql(['acc-root', 'divider', 'new-account']);
    expect(
      menu.querySelector('[data-testid="new-account"]')?.textContent
    ).to.contain('New account…');
  });

  it('puts New account last, after a divider, below several accounts', async () => {
    const { el } = await render([m('acc-root', 'Root'), m('acc-2', 'Two')]);
    const menu = el.shadowRoot!.querySelector('sl-dropdown sl-menu')!;
    const last = menu.lastElementChild as HTMLElement;
    expect(last.dataset.testid).to.equal('new-account');
    expect(last.previousElementSibling?.tagName).to.equal('SL-DIVIDER');
  });

  async function openNewAccount(el: AccountSwitcher) {
    (
      el.shadowRoot!.querySelector('[data-testid="new-account"]') as HTMLElement
    ).click();
    await el.updateComplete;
    const dialog = el.shadowRoot!.querySelector('sl-dialog.new-account') as
      (HTMLElement & { open: boolean }) | null;
    await waitUntil(() => dialog?.open, 'dialog did not open');
    return dialog!;
  }

  async function typeName(el: AccountSwitcher, name: string) {
    const input = el.shadowRoot!.querySelector(
      '[data-testid="new-account-name"]'
    ) as HTMLInputElement;
    input.value = name;
    input.dispatchEvent(new Event('sl-input'));
    await el.updateComplete;
  }

  it('creates an account and switches to it', async () => {
    const { el, navigated } = await render([m('acc-root', 'Root')]);
    api!.restore();
    api = mockApi({
      capabilities: ['multi_account'],
      routes: [
        { path: '/api/v1/me/memberships', body: [m('acc-root', 'Root')] },
        {
          method: 'POST',
          path: '/api/v1/me/accounts',
          status: 201,
          body: {
            account: { id: 'acc-new', name: 'Side project' },
            membership: m('acc-new', 'Side project'),
          },
        },
        {
          method: 'POST',
          path: '/api/v1/auth/switch-account',
          body: { access_token: 'new-access', refresh_token: 'new-refresh' },
        },
      ],
    });
    await openNewAccount(el);
    await typeName(el, '  Side project ');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="create-account"]'
      ) as HTMLElement
    ).click();

    await waitUntil(() => navigated.length === 1, 'did not navigate');
    const create = api!.calls.find((c) => c.path === '/api/v1/me/accounts');
    expect(create?.method).to.equal('POST');
    expect(create?.body).to.eql({ name: 'Side project' });
    const switched = api!.calls.find(
      (c) => c.path === '/api/v1/auth/switch-account'
    );
    expect(switched?.body).to.eql({ account_id: 'acc-new' });
    expect(localStorage.getItem('accessToken')).to.equal('new-access');
  });

  it('asks for a name before creating anything', async () => {
    const { el } = await render([m('acc-root', 'Root')]);
    await openNewAccount(el);
    await typeName(el, '   ');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="create-account"]'
      ) as HTMLElement
    ).click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('sl-dialog.new-account [role="alert"]')
        ?.textContent
    ).to.contain('Give the account a name');
    expect(api!.calls.some((c) => c.path === '/api/v1/me/accounts')).to.equal(
      false
    );
  });

  it('retries only the switch when it fails after the account was created', async () => {
    const { el, navigated } = await render([m('acc-root', 'Root')]);
    api!.restore();
    let switches = 0;
    api = mockApi({
      capabilities: ['multi_account'],
      routes: [
        {
          method: 'POST',
          path: '/api/v1/me/accounts',
          status: 201,
          body: {
            account: { id: 'acc-new', name: 'Side project' },
            membership: m('acc-new', 'Side project'),
          },
        },
        {
          method: 'POST',
          path: '/api/v1/auth/switch-account',
          status: 200,
          body: () =>
            ++switches === 1
              ? {}
              : { access_token: 'new-access', refresh_token: 'new-refresh' },
        },
      ],
    });
    await openNewAccount(el);
    await typeName(el, 'Side project');
    const create = () =>
      (
        el.shadowRoot!.querySelector(
          '[data-testid="create-account"]'
        ) as HTMLElement
      ).click();
    create();
    await waitUntil(
      () =>
        el.shadowRoot!.querySelector('sl-dialog.new-account [role="alert"]'),
      'no error shown'
    );
    expect(
      el.shadowRoot!.querySelector('sl-dialog.new-account [role="alert"]')
        ?.textContent
    ).to.contain('The account was created');
    // The new account is in the menu already.
    expect(el.shadowRoot!.querySelector('sl-menu-item[data-account="acc-new"]'))
      .to.exist;
    expect(
      el.shadowRoot!.querySelector('[data-testid="create-account"]')
        ?.textContent
    ).to.contain('Switch to it');

    create();
    await waitUntil(() => navigated.length === 1, 'did not navigate');
    const creates = api!.calls.filter((c) => c.path === '/api/v1/me/accounts');
    expect(creates).to.have.length(1);
    expect(switches).to.equal(2);
  });

  it('keeps the dialog open with the reason when the server refuses', async () => {
    const { el, navigated } = await render([m('acc-root', 'Root')]);
    api!.restore();
    api = mockApi({
      capabilities: ['multi_account'],
      routes: [
        {
          method: 'POST',
          path: '/api/v1/me/accounts',
          status: 403,
          body: {
            detail: 'Verify your email address before creating an account',
          },
        },
      ],
    });
    const dialog = await openNewAccount(el);
    await typeName(el, 'Side project');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="create-account"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        el.shadowRoot!.querySelector('sl-dialog.new-account [role="alert"]'),
      'no error shown'
    );
    expect(
      el.shadowRoot!.querySelector('sl-dialog.new-account [role="alert"]')
        ?.textContent
    ).to.contain('Verify your email address');
    expect(dialog.open).to.equal(true);
    expect(navigated).to.eql([]);
    expect(
      api!.calls.some((c) => c.path === '/api/v1/auth/switch-account')
    ).to.equal(false);
  });

  it('lists accounts grouped by tree with the current one checked', async () => {
    const { el } = await render([
      m('acc-root', 'Root'),
      m('acc-eu', 'EU', 'acc-root'),
      m('acc-other', 'Other'),
    ]);
    const items = [
      ...el.shadowRoot!.querySelectorAll('sl-menu-item[data-account]'),
    ] as HTMLElement[];
    expect(items.map((i) => i.dataset.account)).to.have.members([
      'acc-root',
      'acc-eu',
      'acc-other',
    ]);
    const eu = items.find((i) => i.dataset.account === 'acc-eu')!;
    expect(eu.classList.contains('child')).to.equal(true);
    const root = items.find((i) => i.dataset.account === 'acc-root')!;
    expect(root.hasAttribute('checked')).to.equal(true);
    expect(
      el.shadowRoot!.querySelector('[data-testid="account-search"]')
    ).to.equal(null);
  });

  it('adds a search above eight accounts', async () => {
    const many = Array.from({ length: 9 }, (_, i) =>
      m(i === 0 ? 'acc-root' : `acc-${i}`, `Account ${i}`)
    );
    const { el } = await render(many);
    const search = el.shadowRoot!.querySelector(
      '[data-testid="account-search"]'
    ) as HTMLInputElement;
    expect(search).to.exist;
    search.value = 'account 7';
    search.dispatchEvent(new Event('sl-input'));
    await el.updateComplete;
    const shown = [
      ...el.shadowRoot!.querySelectorAll('sl-menu-item[data-account]'),
    ].map((i) => (i as HTMLElement).dataset.account);
    expect(shown).to.eql(['acc-7']);
  });

  it('switching replaces both tokens and later requests carry only the new one', async () => {
    const { el, navigated } = await render([
      m('acc-root', 'Root'),
      m('acc-2', 'Two'),
    ]);
    const item = el.shadowRoot!.querySelector(
      'sl-menu-item[data-account="acc-2"]'
    ) as HTMLElement;
    item.click();
    await waitUntil(() => navigated.length === 1, 'did not navigate');
    expect(localStorage.getItem('accessToken')).to.equal('new-access');
    expect(localStorage.getItem('refreshToken')).to.equal('new-refresh');
    const switchIndex = api!.calls.findIndex(
      (c) => c.path === '/api/v1/auth/switch-account'
    );
    for (const call of api!.calls.slice(switchIndex + 1)) {
      expect(call.authorization, call.path).to.equal('Bearer new-access');
    }
  });

  it('hides without a toast when the memberships endpoint is missing', async () => {
    const before = toastCount();
    let off = 0;
    api = mockApi({ capabilities: ['multi_account'] });
    const el = await fixture<AccountSwitcher>(
      html`<account-switcher @capability-off=${() => off++}></account-switcher>`
    );
    await waitUntil(() => (el as any).loaded, 'switcher did not load');
    await el.updateComplete;
    expect(off).to.equal(1);
    expect(el.shadowRoot!.childElementCount).to.equal(0);
    expect(toastCount()).to.equal(before);
  });
});
