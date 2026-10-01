import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './issue-cost-view.ts';
import {
  IssueCostView,
  formatIssueAmount,
  formatIssueCost,
  formatIssueEstimate,
  formatIssueHours,
  issueCostTitle,
} from './issue-cost-view';
import { invalidateApiCaches, type IssueCostCoverage } from '../../api';

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
      cost_coverage: 'complete',
      known_cost_run_count: 3,
      unknown_cost_run_count: 0,
      attributed_cost_usd: 2.875,
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
    {
      id: 'rollup-2',
      tracker_id: 'tracker-1',
      tracker_name: 'GitHub',
      tracker_type: 'github',
      issue_key: 'example-org/example-repo#13',
      issue_id: null,
      title: 'Subscription-backed work',
      issue_url: 'https://github.com/example-org/example-repo/issues/13',
      pr_url: null,
      project_id: 'project-1',
      project_name: 'Example',
      estimated_cost: 0,
      cost_coverage: 'unknown',
      known_cost_run_count: 0,
      unknown_cost_run_count: 2,
      attributed_cost_usd: null,
      total_tokens: 800,
      run_count: 2,
      failed_run_count: 0,
      first_event_at: '2026-09-02T08:00:00Z',
      pr_opened_at: null,
      approved_at: null,
      merged_at: null,
      first_event_to_pr_opened_hours: null,
      pr_opened_to_approved_hours: null,
      approved_to_merged_hours: null,
      pr_opened_at_source: null,
      estimate_hours: null,
      estimate_hours_source: null,
      estimate_points: null,
      estimate_points_source: null,
    },
    {
      id: 'rollup-3',
      tracker_id: 'tracker-1',
      tracker_name: 'GitHub',
      tracker_type: 'github',
      issue_key: 'example-org/example-repo#14',
      issue_id: null,
      title: 'Mixed priced and subscription work',
      issue_url: 'https://github.com/example-org/example-repo/issues/14',
      pr_url: null,
      project_id: 'project-1',
      project_name: 'Example',
      estimated_cost: 2,
      cost_coverage: 'partial',
      known_cost_run_count: 1,
      unknown_cost_run_count: 1,
      attributed_cost_usd: null,
      total_tokens: 900,
      run_count: 2,
      failed_run_count: 0,
      first_event_at: '2026-09-03T08:00:00Z',
      pr_opened_at: null,
      approved_at: null,
      merged_at: null,
      first_event_to_pr_opened_hours: null,
      pr_opened_to_approved_hours: null,
      approved_to_merged_hours: null,
      pr_opened_at_source: null,
      estimate_hours: null,
      estimate_hours_source: null,
      estimate_points: null,
      estimate_points_source: null,
    },
    {
      id: 'rollup-4',
      tracker_id: 'tracker-1',
      tracker_name: 'GitHub',
      tracker_type: 'github',
      issue_key: 'example-org/example-repo#15',
      issue_id: null,
      title: 'Priced at zero',
      issue_url: 'https://github.com/example-org/example-repo/issues/15',
      pr_url: null,
      project_id: 'project-1',
      project_name: 'Example',
      estimated_cost: 0,
      cost_coverage: 'complete',
      known_cost_run_count: 1,
      unknown_cost_run_count: 0,
      attributed_cost_usd: 0,
      total_tokens: 10,
      run_count: 1,
      failed_run_count: 0,
      first_event_at: '2026-09-04T08:00:00Z',
      pr_opened_at: null,
      approved_at: null,
      merged_at: null,
      first_event_to_pr_opened_hours: null,
      pr_opened_to_approved_hours: null,
      approved_to_merged_hours: null,
      pr_opened_at_source: null,
      estimate_hours: null,
      estimate_hours_source: null,
      estimate_points: null,
      estimate_points_source: null,
    },
  ],
  by_project: [
    {
      id: 'project-1',
      name: 'Example',
      issue_count: 4,
      estimated_cost: 4.875,
      cost_coverage: 'partial',
      known_cost_run_count: 5,
      unknown_cost_run_count: 3,
      attributed_cost_usd: null,
      total_tokens: 8410,
      run_count: 8,
      failed_run_count: 1,
    },
  ],
  by_flow: [],
  unassigned: {
    estimated_cost: 0.03,
    cost_coverage: 'partial',
    known_cost_run_count: 1,
    unknown_cost_run_count: 1,
    attributed_cost_usd: null,
    total_tokens: 300,
    run_count: 2,
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

  it('reads every coverage state, and a known zero as priced', () => {
    const complete = report.issues[0];
    expect(formatIssueAmount(complete)).to.equal('$2.88');
    const zero = report.issues[3];
    expect(zero.cost_coverage).to.equal('complete');
    expect(zero.estimated_cost).to.equal(0);
    expect(formatIssueAmount(zero)).to.equal('$0.00');
    expect(issueCostTitle(zero)).to.contain('estimate');

    const partial = report.issues[2];
    expect(formatIssueAmount(partial)).to.equal(
      '$2.00 partial; 1 run without cost'
    );
    expect(
      formatIssueAmount({ ...partial, unknown_cost_run_count: 3 })
    ).to.equal('$2.00 partial; 3 runs without cost');
    expect(issueCostTitle(partial)).to.contain('not total spend');

    const unknown = report.issues[1];
    expect(formatIssueAmount(unknown)).to.equal('Unknown');
    // A wholly unpriced bucket is never rendered as free.
    expect(formatIssueAmount(unknown)).to.not.contain('$');
    expect(issueCostTitle(unknown)).to.contain('not a free ticket');

    // An older response without coverage is read as unpriced, not complete.
    expect(
      formatIssueAmount({
        estimated_cost: 2.875,
      } as unknown as IssueCostCoverage)
    ).to.equal('Unknown');
  });

  it('renders unknown, partial and a known zero in the cost column', async () => {
    const el = await fixture<IssueCostView>(
      html`<issue-cost-view></issue-cost-view>`
    );
    await waitUntil(() => el.report !== null, 'report loaded');
    await el.updateComplete;
    const root = el.shadowRoot!;
    const cost = (key: string) =>
      root.querySelector(`tr[data-issue="${key}"] td.cost`) as HTMLElement;

    expect(cost('example-org/example-repo#12').textContent!.trim()).to.equal(
      '$2.88'
    );
    const partial = cost('example-org/example-repo#14');
    expect(partial.textContent!.trim()).to.equal(
      '$2.00 partial; 1 run without cost'
    );
    expect(partial.title).to.contain('not total spend');
    const unknown = cost('example-org/example-repo#13');
    expect(unknown.textContent!.trim()).to.equal('Unknown');
    expect(unknown.title).to.contain('not a free ticket');
    expect(cost('example-org/example-repo#15').textContent!.trim()).to.equal(
      '$0.00'
    );
    // The summaries carry the same coverage, and the note names the sources.
    expect(
      root.querySelector('table[aria-label="By project"]')!.textContent
    ).to.contain('partial; 3 runs without cost');
    const note = root
      .querySelector('.cost-note')!
      .textContent!.replace(/\s+/g, ' ');
    expect(note).to.contain('daily GitHub Copilot import');
    expect(note).to.contain('premium-request count');
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
    expect(root.querySelector('.unassigned')!.textContent).to.contain('2 runs');
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

  it('keeps the unassigned panel hidden when hidden before the runs load', async () => {
    const el = await fixture<IssueCostView>(
      html`<issue-cost-view></issue-cost-view>`
    );
    await waitUntil(() => el.report !== null, 'report loaded');
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const plainFetch = fetchStub.wrappedMethod.bind(window);
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      requested.push(url);
      if (url.includes('/cost/by-issue/unassigned/executions')) {
        await gate;
        return new Response(JSON.stringify(unassignedExecutions), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return plainFetch(input);
    });

    const pending = el.toggleUnassigned();
    expect(el.unassignedRuns).to.equal('loading');
    await el.toggleUnassigned();
    expect(el.unassignedRuns).to.equal(null);
    release();
    await pending;
    await el.updateComplete;

    expect(el.unassignedRuns).to.equal(null);
    expect(
      el.shadowRoot!.querySelector('tr[data-execution="exec-9"]')
    ).to.equal(null);
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
