import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './resource-access-panel';
import type { ResourceAccessPanel } from './resource-access-panel';
import type { Capability } from '../../../capabilities';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
} from '../../../test-helpers/capability-api';

const SHARES = '/api/v1/accounts/acc-root/shares';
const SUBS = '/api/v1/accounts/acc-root/subaccounts';
const TAGS = '/api/v1/tags/ai_model/model-1';

async function mount(
  capabilities: Capability[],
  context: Record<string, unknown> = { kind: 'ai_model', resourceId: 'model-1' }
) {
  const el = await fixture<ResourceAccessPanel>(
    html`<resource-access-panel
      .capabilities=${new Set(capabilities)}
      .context=${context}
    ></resource-access-panel>`
  );
  return el;
}

const q = (el: ResourceAccessPanel, testid: string) =>
  el.shadowRoot!.querySelector(`[data-testid="${testid}"]`);

describe('resource-access-panel', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('shows the share toggle with the current target, and only shares of this resource', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        {
          path: SHARES,
          body: {
            items: [
              {
                id: 'sh-1',
                resource_type: 'ai_model',
                resource_id: 'model-1',
                target: { type: 'selected', subaccount_ids: ['sub-a'] },
              },
              {
                id: 'sh-other',
                resource_type: 'ai_model',
                resource_id: 'model-9',
                target: { type: 'all' },
              },
            ],
          },
        },
        {
          path: SUBS,
          body: {
            items: [
              { id: 'sub-a', name: 'North', tags: {} },
              { id: 'sub-b', name: 'South', tags: {} },
            ],
          },
        },
      ],
    });
    const el = await mount(['account_hierarchy']);
    await waitUntil(() => q(el, 'share-section'));
    expect(api.callsTo(SHARES)[0].search).to.equal(
      '?resource_type=ai_model&resource_id=model-1'
    );
    const toggle = q(el, 'share-toggle') as HTMLInputElement;
    expect(toggle.checked).to.equal(true);
    const listed = [
      ...el.shadowRoot!.querySelectorAll('[data-testid="share-list"] li'),
    ].map((li) => [li.getAttribute('data-share'), li.textContent!.trim()]);
    expect(listed).to.have.length(1);
    expect(listed[0][0]).to.equal('sh-1');
    expect(listed[0][1]).to.contain('North');
    // No tag section without abac_rules.
    expect(q(el, 'tag-section')).to.be.null;
    expect(api.callsTo(TAGS)).to.have.length(0);
  });

  it('creates a share when turned on', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        { path: SHARES, body: { items: [] } },
        { path: SUBS, body: { items: [] } },
        { method: 'POST', path: SHARES, status: 201, body: { id: 'sh-2' } },
      ],
    });
    const el = await mount(['account_hierarchy']);
    await waitUntil(() => q(el, 'share-section'));
    (q(el, 'share-toggle') as HTMLElement).click();
    await el.updateComplete;
    (q(el, 'share-save') as HTMLElement).click();
    await waitUntil(() => api.callsTo(SHARES, 'POST').length === 1);
    expect(api.callsTo(SHARES, 'POST')[0].body).to.eql({
      resource_type: 'ai_model',
      resource_id: 'model-1',
      target: { type: 'all' },
    });
  });

  it('compares selected share targets by id set, not by name', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        { method: 'POST', path: SHARES, status: 201, body: { id: 'sh-new' } },
        {
          path: SHARES,
          body: {
            items: [
              {
                id: 'sh-sel',
                resource_type: 'ai_model',
                resource_id: 'model-1',
                target: {
                  type: 'selected',
                  subaccount_ids: ['sub-b', 'sub-a'],
                },
              },
            ],
          },
        },
        {
          path: SUBS,
          body: {
            items: [
              { id: 'sub-a', name: 'Alpha' },
              { id: 'sub-b', name: 'Beta' },
              { id: 'sub-c', name: 'Alpha' },
            ],
          },
        },
      ],
    });
    const el = await mount(['account_hierarchy']);
    await waitUntil(() => q(el, 'share-list'));
    const draft = el as unknown as {
      draftTarget: string;
      draftSelected: Set<string>;
    };
    draft.draftTarget = 'selected';
    draft.draftSelected = new Set(['sub-a', 'sub-b']);
    await el.updateComplete;
    (q(el, 'share-save') as HTMLElement).click();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Already shared with')
    );
    expect(api.callsTo(SHARES, 'POST')).to.have.length(0);

    draft.draftSelected = new Set(['sub-c', 'sub-b']);
    await el.updateComplete;
    (q(el, 'share-save') as HTMLElement).click();
    await waitUntil(() => api.callsTo(SHARES, 'POST').length === 1);
  });

  describe('with several shares on one resource', () => {
    const TWO = [
      {
        id: 'sh-all',
        resource_type: 'ai_model',
        resource_id: 'model-1',
        target: { type: 'all' },
      },
      {
        id: 'sh-tag',
        resource_type: 'ai_model',
        resource_id: 'model-1',
        target: { type: 'tag', key: 'customer', value: 'acme' },
      },
    ];
    const routes = (extra: Parameters<typeof mockApi>[0]['routes'] = []) => [
      ...(extra ?? []),
      { path: SHARES, body: { items: TWO } },
      { path: SUBS, body: { items: [] } },
      { method: 'DELETE', path: /\/shares\/sh-/, status: 204 },
    ];
    const click = (el: ResourceAccessPanel, selector: string) =>
      (el.shadowRoot!.querySelector(selector) as HTMLElement).click();

    it('lists every share', async () => {
      api = mockApi({ capabilities: ['account_hierarchy'], routes: routes() });
      const el = await mount(['account_hierarchy']);
      await waitUntil(() => q(el, 'share-list'));
      const text = q(el, 'share-list')!.textContent!;
      expect(text).to.contain('All subaccounts');
      expect(text).to.contain('Subaccounts tagged customer=acme');
    });

    it('adds a share without deleting the others', async () => {
      api = mockApi({
        capabilities: ['account_hierarchy'],
        routes: routes([
          { method: 'POST', path: SHARES, status: 201, body: { id: 'sh-new' } },
        ]),
      });
      const el = await mount(['account_hierarchy']);
      await waitUntil(() => q(el, 'share-list'));
      const draft = el as unknown as { draftTarget: string; draftTag: string };
      draft.draftTarget = 'tag';
      draft.draftTag = 'customer=globex';
      await el.updateComplete;
      click(el, '[data-testid="share-save"]');
      await waitUntil(() => api.callsTo(SHARES, 'GET').length === 2);
      expect(api.callsTo(SHARES, 'POST')).to.have.length(1);
      expect(api.callsTo(/\/shares\//, 'DELETE')).to.have.length(0);
    });

    it('refuses to add a share whose target is already shared', async () => {
      api = mockApi({ capabilities: ['account_hierarchy'], routes: routes() });
      const el = await mount(['account_hierarchy']);
      await waitUntil(() => q(el, 'share-list'));
      click(el, '[data-testid="share-save"]');
      await waitUntil(() =>
        el.shadowRoot!.textContent!.includes(
          'Already shared with All subaccounts.'
        )
      );
      const draft = el as unknown as { draftTarget: string; draftTag: string };
      draft.draftTarget = 'tag';
      draft.draftTag = 'customer=acme';
      await el.updateComplete;
      click(el, '[data-testid="share-save"]');
      await waitUntil(() =>
        el.shadowRoot!.textContent!.includes(
          'Already shared with Subaccounts tagged customer=acme.'
        )
      );
      expect(api.callsTo(SHARES, 'POST')).to.have.length(0);
    });

    it('stops only the share asked for', async () => {
      api = mockApi({ capabilities: ['account_hierarchy'], routes: routes() });
      const el = await mount(['account_hierarchy']);
      await waitUntil(() => q(el, 'share-list'));
      click(el, 'li[data-share="sh-tag"] sl-button');
      await waitUntil(() => api.callsTo(SHARES, 'GET').length === 2);
      expect(api.callsTo(/\/shares\//, 'DELETE').map((c) => c.path)).to.eql([
        '/api/v1/accounts/acc-root/shares/sh-tag',
      ]);
    });

    it('stopping all removes exactly the listed shares', async () => {
      api = mockApi({ capabilities: ['account_hierarchy'], routes: routes() });
      const el = await mount(['account_hierarchy']);
      await waitUntil(() => q(el, 'share-list'));
      click(el, '[data-testid="share-toggle"]');
      await el.updateComplete;
      click(el, '[data-testid="share-stop"]');
      await waitUntil(() => api.callsTo(SHARES, 'GET').length === 2);
      expect(
        api.callsTo(/\/shares\//, 'DELETE').map((c) => c.path.split('/').pop())
      ).to.eql(['sh-all', 'sh-tag']);
    });

    it('rereads the shares after a failed add and deletes nothing', async () => {
      api = mockApi({
        capabilities: ['account_hierarchy'],
        routes: routes([
          {
            method: 'POST',
            path: SHARES,
            status: 500,
            body: { detail: 'boom' },
          },
        ]),
      });
      const el = await mount(['account_hierarchy']);
      await waitUntil(() => q(el, 'share-list'));
      const draft = el as unknown as { draftTarget: string; draftTag: string };
      draft.draftTarget = 'tag';
      draft.draftTag = 'customer=globex';
      await el.updateComplete;
      click(el, '[data-testid="share-save"]');
      await waitUntil(() => api.callsTo(SHARES, 'GET').length === 2);
      await el.updateComplete;
      expect(api.callsTo(/\/shares\//, 'DELETE')).to.have.length(0);
      expect(el.shadowRoot!.querySelector('.error')!.textContent).to.contain(
        'boom'
      );
      expect(
        el.shadowRoot!.querySelectorAll('[data-testid="share-list"] li')
      ).to.have.length(2);
    });
  });

  it('writes tags with the version read and reloads on a concurrent change', async () => {
    let version = 'v1';
    let tags: Record<string, string> = { env: 'prod' };
    api = mockApi({
      capabilities: ['abac_rules'],
      routes: [
        { path: TAGS, body: () => ({ tags, governed_keys: [], version }) },
        {
          method: 'PUT',
          path: TAGS,
          status: 409,
          body: { detail: 'version mismatch' },
        },
      ],
    });
    const el = await mount(['abac_rules']);
    await waitUntil(() => q(el, 'tag-section'));
    // Someone else writes owner=ops meanwhile.
    version = 'v2';
    tags = { env: 'prod', owner: 'ops' };
    el.shadowRoot!.querySelector<HTMLInputElement>('#new-tag')!.value =
      'tier=gold';
    (q(el, 'tag-add') as HTMLElement).click();
    await waitUntil(() => el.shadowRoot!.querySelector('.error'));
    expect(api.callsTo(TAGS, 'PUT')[0].body).to.eql({
      tags: { env: 'prod', tier: 'gold' },
      version: 'v1',
    });
    expect(el.shadowRoot!.querySelector('.error')!.textContent).to.contain(
      'changed while you were editing'
    );
    expect(el.shadowRoot!.querySelector('sl-tag[data-key="owner"]')).to.exist;
    expect(el.shadowRoot!.querySelector('sl-tag[data-key="tier"]')).to.be.null;
  });

  it('shows governed tag keys read-only and refuses to set them', async () => {
    api = mockApi({
      capabilities: ['abac_rules'],
      routes: [
        {
          path: TAGS,
          body: {
            tags: { customer: 'acme', env: 'prod' },
            governed_keys: ['customer'],
          },
        },
      ],
    });
    const el = await mount(['abac_rules']);
    await waitUntil(() => q(el, 'tag-section'));
    const tag = (key: string) =>
      el.shadowRoot!.querySelector(`sl-tag[data-key="${key}"]`)!;
    expect(tag('customer').hasAttribute('removable')).to.equal(false);
    expect(tag('customer').textContent).to.contain('set by parent');
    expect(tag('env').hasAttribute('removable')).to.equal(true);

    const input = el.shadowRoot!.querySelector<HTMLInputElement>('#new-tag')!;
    input.value = 'customer=other';
    (q(el, 'tag-add') as HTMLElement).click();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.error')!.textContent).to.contain(
      'customer'
    );
    expect(api.callsTo(TAGS, 'PUT')).to.have.length(0);
  });

  it('offers no sharing and no tag edits on a resource shared from a parent', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy', 'abac_rules'],
      routes: [{ path: TAGS, body: { tags: { env: 'prod' } } }],
    });
    const el = await mount(['account_hierarchy', 'abac_rules'], {
      kind: 'ai_model',
      resourceId: 'model-1',
      sharedFrom: { account_id: 'acc-parent', account_name: 'Parent' },
    });
    await waitUntil(() => q(el, 'tag-section'));
    expect(q(el, 'share-section')).to.be.null;
    expect(api.callsTo(SHARES)).to.have.length(0);
    expect(el.shadowRoot!.querySelector('#new-tag')).to.be.null;
    expect(
      el
        .shadowRoot!.querySelector('sl-tag[data-key="env"]')!
        .hasAttribute('removable')
    ).to.equal(false);
  });

  it('reports capability-off without a toast when both endpoints are missing', async () => {
    api = mockApi();
    const before = toastCount();
    let off = 0;
    const el = await fixture<ResourceAccessPanel>(
      html`<resource-access-panel
        @capability-off=${() => off++}
        .capabilities=${new Set<Capability>(['account_hierarchy', 'abac_rules'])}
        .context=${{ kind: 'ai_model', resourceId: 'model-1' }}
      ></resource-access-panel>`
    );
    await waitUntil(() => off === 1, 'no capability-off event');
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('sl-card')).to.be.null;
    expect(toastCount()).to.equal(before);
  });
});
