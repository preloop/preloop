import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './work-item-cost-view.ts';
import {
  WorkItemCostView,
  WORK_ITEM_PAGE_SIZE,
  formatWorkItemCost,
  groupMakeup,
  sliceShare,
  workItemKindLabel,
} from './work-item-cost-view';
import { invalidateApiCaches, type WorkItemCostReport } from '../../api';

const slice = (key: string, label: string, cost: number) => ({
  key,
  label,
  estimated_cost: cost,
  total_tokens: 1000,
  request_count: 2,
});

const report: WorkItemCostReport = {
  period_start: '2026-10-07T00:00:00Z',
  period_end: '2026-11-06T00:00:00Z',
  total_groups: 1,
  estimated_cost: 3,
  total_tokens: 3000,
  truncated: false,
  split_rule: 'Spend is split evenly.',
  groups: [
    {
      key: 'example-org/app#7',
      kind: 'issue',
      title: 'Retry flaky deploys',
      url: 'https://github.com/example-org/app/issues/7',
      estimated_cost: 3,
      partial: false,
      total_tokens: 3000,
      request_count: 4,
      execution_count: 1,
      session_count: 2,
      items: [
        {
          kind: 'issue',
          key: 'example-org/app#7',
          title: 'Retry flaky deploys',
          url: 'https://github.com/example-org/app/issues/7',
          state: null,
          estimated_cost: 1,
          total_tokens: 1000,
          request_count: 1,
        },
        {
          kind: 'pull_request',
          key: 'example-org/app#43',
          title: 'Retry deploys with backoff',
          url: 'https://github.com/example-org/app/pull/43',
          state: 'merged',
          estimated_cost: 2,
          total_tokens: 2000,
          request_count: 3,
        },
      ],
      agents: [slice('claude_code|x', 'Claude Code', 2)],
      flows: [slice('flow-1', 'Implement issue', 1)],
      models: [slice('gpt-5-codex', 'gpt-5-codex', 2)],
      users: [slice('user-1', 'Nyota Uhura', 2)],
    },
  ],
};

describe('WorkItemCostView', () => {
  let fetchStub: sinon.SinonStub;
  let requested: string[];

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    requested = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      requested.push(url);
      const body = url.includes('/cost/work-items') ? report : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.removeItem('accessToken');
  });

  it('formats amounts, kinds, shares and the group makeup', () => {
    expect(formatWorkItemCost(2.875)).to.equal('$2.88 est.');
    expect(formatWorkItemCost(null)).to.equal('');
    expect(workItemKindLabel('pull_request')).to.equal('PR');
    expect(workItemKindLabel('issue')).to.equal('Issue');
    expect(sliceShare(slice('a', 'a', 1), 4)).to.equal(25);
    expect(sliceShare(slice('a', 'a', 1), 0)).to.equal(0);
    expect(groupMakeup(report.groups[0])).to.equal('1 issue + 1 PR');
  });

  it('embedded on Spend: no header or range, reads the page window and follows it', async () => {
    const element = await fixture<WorkItemCostView>(
      html`<work-item-cost-view
        embedded
        .period=${{
          startDate: '2026-10-01T00:00:00Z',
          endDate: '2026-11-01T00:00:00Z',
        }}
      ></work-item-cost-view>`
    );
    await waitUntil(() => element.report !== null);
    expect(element.shadowRoot?.querySelector('view-header')).to.equal(null);
    expect(element.shadowRoot?.querySelector('time-range-select')).to.equal(
      null
    );
    const first = new URL(
      requested.find((url) => url.includes('/cost/work-items'))!,
      location.origin
    );
    expect(first.searchParams.get('start_date')).to.equal(
      '2026-10-01T00:00:00Z'
    );
    element.period = {
      startDate: '2026-09-01T00:00:00Z',
      endDate: '2026-10-01T00:00:00Z',
    };
    await waitUntil(() =>
      requested.some((url) => url.includes('start_date=2026-09-01'))
    );
  });

  it('asks for one page of the selected period', async () => {
    const element = await fixture<WorkItemCostView>(
      html`<work-item-cost-view></work-item-cost-view>`
    );
    await waitUntil(() => element.report !== null);
    const url = new URL(
      requested.find((u) => u.includes('/cost/work-items'))!,
      location.origin
    );
    expect(url.pathname).to.equal('/api/v1/cost/work-items');
    expect(url.searchParams.get('limit')).to.equal(String(WORK_ITEM_PAGE_SIZE));
    expect(url.searchParams.get('start_date')).to.be.a('string');
    element.remove();
  });

  it('lists groups and drills down into items, agents, flows, models and people', async () => {
    const element = await fixture<WorkItemCostView>(
      html`<work-item-cost-view></work-item-cost-view>`
    );
    await waitUntil(() => element.report !== null);
    await element.updateComplete;
    const root = element.shadowRoot!;
    const row = root.querySelector('tbody tr')!;
    expect(row.textContent).to.contain('example-org/app#7');
    expect(row.textContent).to.contain('1 issue + 1 PR');
    expect(row.textContent).to.contain('$3.00 est.');
    expect(root.querySelector('.members')).to.equal(null);
    const toggle = root.querySelector<HTMLButtonElement>('button.expand')!;
    expect(toggle.getAttribute('aria-expanded')).to.equal('false');
    toggle.click();
    await element.updateComplete;
    const detail = root.querySelector('.detail')!;
    expect(detail.textContent).to.contain('example-org/app#43');
    expect(detail.textContent).to.contain('merged');
    expect(detail.textContent).to.contain('Claude Code');
    expect(detail.textContent).to.contain('Implement issue');
    expect(detail.textContent).to.contain('gpt-5-codex');
    expect(detail.textContent).to.contain('Nyota Uhura');
    element.remove();
  });

  it('says so in one line when nothing worked on a work item', async () => {
    fetchStub.callsFake(async () => {
      return new Response(
        JSON.stringify({ ...report, groups: [], total_groups: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    });
    const element = await fixture<WorkItemCostView>(
      html`<work-item-cost-view></work-item-cost-view>`
    );
    await waitUntil(() => element.report !== null);
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('.empty')).to.not.equal(null);
    element.remove();
  });
});
