import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './issue-cost-view.ts';
import {
  IssueCostView,
  formatIssueCost,
  formatIssueEstimate,
  formatIssueHours,
} from './issue-cost-view';
import { invalidateApiCaches } from '../../api';

const report = {
  start: null,
  end: null,
  project_id: null,
  flow_id: null,
  issues: [
    {
      id: 'rollup-1',
      tracker_id: 'tracker-1',
      tracker_name: 'GitHub',
      tracker_type: 'github',
      issue_key: 'example-org/example-repo#12',
      issue_id: null,
      title: 'Add the export button',
      issue_url: 'https://github.com/example-org/example-repo/issues/12',
      pr_url: 'https://github.com/example-org/example-repo/pull/40',
      project_id: 'project-1',
      project_name: 'Example',
      estimated_cost: 2.875,
      total_tokens: 6700,
      run_count: 3,
      failed_run_count: 1,
      first_event_at: '2026-09-01T08:00:00Z',
      pr_opened_at: '2026-09-01T10:00:00Z',
      approved_at: null,
      merged_at: null,
      first_event_to_pr_opened_hours: 2,
      pr_opened_to_approved_hours: null,
      approved_to_merged_hours: null,
      pr_opened_at_source: 'bind',
      estimate_hours: 6,
      estimate_hours_source: 'label:estimate:',
      estimate_points: null,
      estimate_points_source: null,
    },
  ],
  by_project: [
    {
      id: 'project-1',
      name: 'Example',
      issue_count: 1,
      estimated_cost: 2.875,
      total_tokens: 6700,
      run_count: 3,
      failed_run_count: 1,
    },
  ],
  by_flow: [],
  unassigned: {
    estimated_cost: 0.03,
    total_tokens: 300,
    run_count: 1,
    failed_run_count: 0,
    executions: [],
  },
  truncated: false,
};

const executions = [
  {
    execution_id: 'exec-1',
    flow_id: 'flow-1',
    flow_name: 'implement',
    status: 'SUCCEEDED',
    link: 'trigger_issue',
    pr_url: null,
    estimated_cost: 2.25,
    total_tokens: 5000,
    start_time: '2026-09-01T09:00:00Z',
    end_time: '2026-09-01T10:30:00Z',
  },
];

const unassignedExecutions = [
  {
    execution_id: 'exec-9',
    flow_id: 'flow-2',
    flow_name: 'nightly audit',
    status: 'SUCCEEDED',
    link: 'unassigned',
    pr_url: null,
    estimated_cost: 0.03,
    total_tokens: 300,
    start_time: '2026-09-01T11:00:00Z',
    end_time: '2026-09-01T11:05:00Z',
  },
];

describe('IssueCostView', () => {
  let fetchStub: sinon.SinonStub;
  let requested: string[];

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    requested = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      requested.push(url);
      let body: unknown = [];
      if (url.includes('/cost/by-issue/rollup-1/executions')) body = executions;
      else if (url.includes('/cost/by-issue/unassigned/executions')) {
        body = unassignedExecutions;
      } else if (url.includes('/cost/by-issue/export')) {
        return new Response('issue_key\n', {
          status: 200,
          headers: { 'Content-Type': 'text/csv' },
        });
      } else if (url.includes('/cost/by-issue')) body = report;
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

  it('formats cost and leaves missing intervals blank', () => {
    expect(formatIssueCost(2.875)).to.equal('$2.88');
    expect(formatIssueCost(0.0042)).to.equal('$0.0042');
    expect(formatIssueHours(null)).to.equal('');
    expect(formatIssueHours(2)).to.equal('2.0 h');
  });

  it('shows the tracker estimate and leaves a missing one blank', () => {
    const row = report.issues[0];
    expect(formatIssueEstimate(row)).to.equal('6 h');
    expect(
      formatIssueEstimate({ ...row, estimate_hours: null, estimate_points: 3 })
    ).to.equal('3 pts');
    expect(
      formatIssueEstimate({ ...row, estimate_hours: 2.5, estimate_points: 3 })
    ).to.equal('2.5 h / 3 pts');
    expect(
      formatIssueEstimate({
        ...row,
        estimate_hours: null,
        estimate_points: null,
      })
    ).to.equal('');
  });

  it('renders issue rows, the unassigned bucket and summaries', async () => {
    const el = await fixture<IssueCostView>(
      html`<issue-cost-view></issue-cost-view>`
    );
    await waitUntil(() => el.report !== null, 'report loaded');
    await el.updateComplete;
    const root = el.shadowRoot!;
    const row = root.querySelector(
      'tr[data-issue="example-org/example-repo#12"]'
    )!;
    expect(row).to.exist;
    const cells = [...row.querySelectorAll('td')].map((cell) =>
      cell.textContent!.trim()
    );
    expect(cells[3]).to.equal('$2.88');
    expect(cells[5]).to.contain('3');
    expect(cells[5]).to.contain('1 failed');
    expect(cells[6]).to.equal('2.0 h');
    expect(cells[7]).to.equal('');
    expect(cells[9]).to.equal('6 h');
    const estimate = row.querySelector('td.estimate') as HTMLElement;
    expect(estimate.title).to.equal('From label:estimate:');
    expect((row.querySelectorAll('td')[6] as HTMLElement).title).to.contain(
      'approximate'
    );
    expect(root.querySelector('.unassigned')!.textContent).to.contain('1 runs');
    expect(requested.some((url) => url.includes('start_date='))).to.be.true;
  });

  it('expands a row into its contributing executions', async () => {
    const el = await fixture<IssueCostView>(
      html`<issue-cost-view></issue-cost-view>`
    );
    await waitUntil(() => el.report !== null, 'report loaded');
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('button.expand') as HTMLButtonElement
    ).click();
    await waitUntil(
      () => el.shadowRoot!.querySelector('tr[data-execution="exec-1"]'),
      'executions rendered'
    );
    const text = el.shadowRoot!.querySelector(
      'tr[data-execution="exec-1"]'
    )!.textContent!;
    expect(text).to.contain('implement');
    expect(text).to.contain('$2.25');
  });

  it('drills into the unassigned bucket with the current filter', async () => {
    const el = await fixture<IssueCostView>(
      html`<issue-cost-view></issue-cost-view>`
    );
    await waitUntil(() => el.report !== null, 'report loaded');
    el.flowId = 'flow-2';
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('sl-button.show-unassigned') as HTMLElement
    ).click();
    await waitUntil(
      () => el.shadowRoot!.querySelector('tr[data-execution="exec-9"]'),
      'unassigned executions rendered'
    );
    const text = el.shadowRoot!.querySelector(
      'tr[data-execution="exec-9"]'
    )!.textContent!;
    expect(text).to.contain('nightly audit');
    expect(text).to.contain('unassigned');
    const url = requested.find((item) =>
      item.includes('/unassigned/executions')
    )!;
    expect(url).to.contain('flow_id=flow-2');
    expect(url).to.contain('start_date=');
  });

  it('exports with the current filter', async () => {
    const el = await fixture<IssueCostView>(
      html`<issue-cost-view></issue-cost-view>`
    );
    await waitUntil(() => el.report !== null, 'report loaded');
    el.flowId = 'flow-1';
    const click = sinon.stub(HTMLAnchorElement.prototype, 'click');
    try {
      await el.download('csv');
    } finally {
      click.restore();
    }
    const exportUrl = requested.find((url) => url.includes('/export'))!;
    expect(exportUrl).to.contain('format=csv');
    expect(exportUrl).to.contain('flow_id=flow-1');
  });
});
