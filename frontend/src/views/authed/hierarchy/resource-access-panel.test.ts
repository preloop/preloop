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
    const boxes = [...el.shadowRoot!.querySelectorAll('sl-checkbox')].map(
      (b) => [
        b.getAttribute('data-subaccount'),
        (b as HTMLInputElement).checked,
      ]
    );
    expect(boxes).to.eql([
      ['sub-a', true],
      ['sub-b', false],
    ]);
    // No tag section without abac_rules.
    expect(q(el, 'tag-section')).to.be.null;
    expect(api.callsTo(TAGS)).to.have.length(0);
  });

  it('replaces the share when saved with a new target', async () => {
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
