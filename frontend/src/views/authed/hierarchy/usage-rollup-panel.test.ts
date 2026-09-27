import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './usage-rollup-panel';
import type { UsageRollupPanel } from './usage-rollup-panel';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
  type RecordedCall,
} from '../../../test-helpers/capability-api';

const USAGE = '/api/v1/accounts/acc-root/usage/rollup';
const ATTENTION = '/api/v1/accounts/acc-root/attention/rollup';

const ROWS = [
  {
    subaccount_id: 'sub-a',
    subaccount_name: 'North',
    model: 'claude',
    day: '2026-09-01',
    requests: 3,
    cost_usd: 1.25,
  },
  {
    subaccount_id: 'sub-b',
    subaccount_name: 'South',
    model: 'gpt',
    day: '2026-09-01',
    requests: 2,
    cost_usd: 0.5,
  },
];

const usageBody = (call: RecordedCall) => {
  const only = new URLSearchParams(call.search).get('subaccount_id');
  return { rows: only ? ROWS.filter((r) => r.subaccount_id === only) : ROWS };
};

const q = (el: UsageRollupPanel, testid: string) =>
  el.shadowRoot!.querySelector(`[data-testid="${testid}"]`);

describe('usage-rollup-panel', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('shows every subaccount with a column, a total and the attention rollup', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        { path: USAGE, body: usageBody },
        {
          path: ATTENTION,
          body: {
            items: [
              { subaccount_id: 'sub-b', subaccount_name: 'South', count: 4 },
            ],
          },
        },
      ],
    });
    const el = await fixture<UsageRollupPanel>(
      html`<usage-rollup-panel .context=${{}}></usage-rollup-panel>`
    );
    await waitUntil(() => q(el, 'rollup-table'));
    const cells = [
      ...el.shadowRoot!.querySelectorAll(
        'tbody tr[data-subaccount] td:first-child'
      ),
    ].map((td) => td.textContent);
    expect(cells).to.eql(['North', 'South']);
    expect(q(el, 'rollup-total')!.textContent).to.equal('$1.75');
    expect(q(el, 'attention-rollup')!.textContent).to.contain('South');
    expect(q(el, 'attention-rollup')!.textContent).to.contain('4');
    expect((q(el, 'subaccount-filter') as HTMLSelectElement).value).to.equal(
      '__all__'
    );
  });

  it('filters by one subaccount and keeps the others selectable', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        { path: USAGE, body: usageBody },
        { path: ATTENTION, body: { items: [] } },
      ],
    });
    const el = await fixture<UsageRollupPanel>(
      html`<usage-rollup-panel .context=${{}}></usage-rollup-panel>`
    );
    await waitUntil(() => q(el, 'rollup-table'));
    const select = q(el, 'subaccount-filter') as HTMLSelectElement;
    select.value = 'sub-a';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() => api.callsTo(USAGE).length === 2);
    expect(api.callsTo(USAGE)[1].search).to.equal('?subaccount_id=sub-a');
    await waitUntil(() => q(el, 'rollup-total')!.textContent === '$1.25');
    const options = [...el.shadowRoot!.querySelectorAll('sl-option')].map((o) =>
      o.getAttribute('value')
    );
    expect(options).to.eql(['__all__', 'sub-a', 'sub-b']);
  });

  it('hides and reports capability-off without a toast when the endpoint is missing', async () => {
    api = mockApi();
    const before = toastCount();
    let off = 0;
    const el = await fixture<UsageRollupPanel>(
      html`<usage-rollup-panel
        .context=${{}}
        @capability-off=${() => off++}
      ></usage-rollup-panel>`
    );
    await waitUntil(() => off === 1);
    expect(el.shadowRoot!.querySelector('sl-card')).to.be.null;
    expect(toastCount()).to.equal(before);
  });
});
