import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './delivery-report-view';
import './delivery-reports-view';
import { ACTIONS_UNAVAILABLE, anchorId } from './delivery-report-view';
import type { DeliveryReportView } from './delivery-report-view';
import type { DeliveryReportsView } from './delivery-reports-view';
import type {
  DeliveryReportDetail,
  DeliveryReportRow,
} from '../../delivery-reports-api';
import {
  denominators,
  periodLabel,
  verdictChip,
  verdictCounts,
} from '../../utils/delivery-report';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function row(
  id: string,
  extra: Partial<DeliveryReportRow> = {}
): DeliveryReportRow {
  return {
    id,
    flow_id: 'flow-1',
    flow_name: 'Delivery Report',
    status: 'SUCCEEDED',
    report_status: 'success',
    started_at: '2026-10-24T07:00:00Z',
    ended_at: '2026-10-24T07:04:00Z',
    window: { from: '2026-10-17T07:00:00Z', to: '2026-10-24T07:00:00Z' },
    projects: ['example/app'],
    shipped: 3,
    closed: 2,
    closed_unverified: 1,
    proposals: 2,
    cost_usd: 4.12,
    ...extra,
  };
}

function detail(
  extra: Partial<DeliveryReportDetail> = {}
): DeliveryReportDetail {
  return {
    ...row('run-1'),
    stale_reopen_requests: {},
    actions: { open_issue: false, reopen: false, dismiss: false },
    report: {
      status: 'success',
      summary: {
        shipped: 3,
        closed: 2,
        linkage: { linked: 2, total: 3 },
        criteria_availability: { with_criteria: 1, total: 2 },
        evidence_availability: { with_evidence: 1, total: 4 },
        closed_verdicts: {
          covered: 1,
          partial: 0,
          unverified: 1,
          no_criteria: 0,
        },
      },
      shipped: [
        {
          ref: 'example/app#10',
          title: 'Add the export button',
          url: 'https://example.com/example/app/pull/10',
          project: 'example/app',
          area: 'frontend',
          issues: ['example/app#7'],
          purpose: {
            verdict: 'unverified',
            source: 'report',
            criteria: [
              { text: 'Export finishes under 2s', met: null, evidence: [] },
            ],
          },
          risk_flags: [
            { flag: 'ui_without_screenshot', evidence: ['src/export.ts'] },
          ],
          evidence_gaps: ['p95 not measured'],
        },
        {
          ref: 'example/app#11',
          title: 'Fix the login copy',
          project: 'example/app',
          purpose: { verdict: 'covered', source: 'audit', criteria: [] },
        },
        {
          ref: 'example/api#3',
          title: 'Bump a dependency',
          project: 'example/api',
          purpose: { verdict: 'no_criteria', source: 'metadata' },
        },
      ],
      closed_issues: [
        {
          ref: 'example/app#7',
          title: 'Export is slow',
          url: 'https://example.com/example/app/issues/7',
          closed_by: ['example/app#10'],
          purpose: { verdict: 'unverified', source: 'report', criteria: [] },
        },
        {
          ref: 'example/app#8',
          title: 'Login copy',
          purpose: { verdict: 'covered', source: 'audit', criteria: [] },
        },
      ],
      reopen_requests: [
        {
          ref: 'example/app#7',
          title: 'Export is slow',
          revision: 'abcdef1234567890',
          reason: 'The latest comment measures 3.4s',
          evidence: ['comment by Jane Doe'],
        },
      ],
      follow_ups: [{ title: 'Add a screenshot check to the export view' }],
      open_past_window: [
        {
          kind: 'pull_request',
          ref: 'example/app#12',
          title: 'Old PR',
          age_days: 9,
        },
      ],
      delta: { previous_report: null, open_questions: [] },
      open_questions: [{ question: 'Is the export measured in CI?' }],
      spend: { cost_usd: 4.12, total_tokens: 120000, limit_usd: 5 },
    },
    ...extra,
  };
}

describe('delivery report presentation rules', () => {
  it('never paints unverified red: it is a warning tint called an evidence gap', () => {
    const chip = verdictChip('unverified');
    expect(chip.variant).to.equal('warning');
    expect(chip.title).to.match(/evidence gap/i);
    for (const v of ['covered', 'partial', 'unverified', 'no_criteria', 'x']) {
      expect(verdictChip(v).variant).to.not.equal('danger');
    }
    expect(verdictChip('covered').variant).to.equal('success');
    expect(verdictChip('partial').variant).to.equal('warning');
    expect(verdictChip('no_criteria').variant).to.equal('neutral');
  });

  it('keeps each denominator separate', () => {
    const summary = detail().report.summary;
    expect(
      denominators(summary).map((d) => `${d.part}/${d.total}`)
    ).to.deep.equal(['2/3', '1/2', '1/4']);
    expect(verdictCounts(summary).map((v) => v.count)).to.deep.equal([
      1, 0, 1, 0,
    ]);
  });

  it('labels periods within and across months', () => {
    expect(
      periodLabel({ from: '2026-10-17T07:00:00Z', to: '2026-10-24T07:00:00Z' })
    ).to.equal('17 to 24 Oct');
    expect(
      periodLabel({ from: '2026-09-26T07:00:00Z', to: '2026-10-03T07:00:00Z' })
    ).to.equal('26 Sep to 3 Oct');
    expect(anchorId('project', 'example/app')).to.equal('project-example-app');
  });
});

describe('delivery-report-view', () => {
  let fetchStub: sinon.SinonStub;
  let payload: DeliveryReportDetail;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    payload = detail();
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = new URL(String(input), window.location.origin);
      if (url.pathname === '/api/v1/delivery-reports/run-1')
        return json(payload);
      return json({});
    });
  });

  afterEach(() => {
    fetchStub.restore();
    sinon.restore();
  });

  async function mount(): Promise<DeliveryReportView> {
    const el = await fixture<DeliveryReportView>(
      html`<delivery-report-view reportId="run-1"></delivery-report-view>`
    );
    await waitUntil(() =>
      el.shadowRoot?.querySelector('[data-testid="summary-strip"]')
    );
    return el;
  }

  it('renders the four numbers and every section with anchors', async () => {
    const el = await mount();
    const root = el.shadowRoot!;
    const text = (id: string) =>
      root
        .querySelector(`[data-testid="${id}"]`)
        ?.textContent?.replace(/\s+/g, ' ');
    expect(text('denominator-linkage')).to.contain('2 of 3');
    expect(text('denominator-criteria')).to.contain('1 of 2');
    expect(text('denominator-evidence')).to.contain('1 of 4');
    expect(text('verdict-counts')).to.contain('Covered 1');
    expect(text('verdict-counts')).to.contain('Unverified 1');
    for (const id of [
      'needs-you',
      'shipped',
      'closed-issues',
      'open-past-window',
      'delta',
      'open-questions',
      'spend',
    ]) {
      expect(root.getElementById(id), id).to.exist;
    }
    expect(root.getElementById('project-example-app')).to.exist;
    expect(root.getElementById('project-example-api')).to.exist;
    expect(
      root.querySelectorAll('[data-testid="shipped-change"]').length
    ).to.equal(3);
    expect(
      root.querySelectorAll('[data-testid="closed-issue"]').length
    ).to.equal(2);
  });

  it('renders no danger chip for unverified items', async () => {
    const el = await mount();
    const chips = [...el.shadowRoot!.querySelectorAll('[data-testid="chip"]')];
    const unverified = chips.filter(
      (c) => c.textContent?.trim() === 'Unverified'
    );
    expect(unverified.length).to.be.greaterThan(0);
    for (const chip of unverified) {
      expect(chip.getAttribute('data-variant')).to.equal('warning');
    }
    expect(chips.some((c) => c.getAttribute('data-variant') === 'danger')).to.be
      .false;
  });

  it('renders proposal actions disabled while the server cannot act on them', async () => {
    const el = await mount();
    const root = el.shadowRoot!;
    const reopen = root.querySelector('[data-testid="action-reopen"]')!;
    const open = root.querySelector('[data-testid="action-open-issue"]')!;
    expect(reopen.hasAttribute('disabled')).to.be.true;
    expect(open.hasAttribute('disabled')).to.be.true;
    expect(open.getAttribute('title')).to.equal(ACTIONS_UNAVAILABLE);
  });

  it('enables an action the server reports, except on a stale reopen request', async () => {
    payload = detail({
      actions: { open_issue: true, reopen: true, dismiss: true },
    });
    let el = await mount();
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="action-open-issue"]')!
        .hasAttribute('disabled')
    ).to.be.false;
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="action-reopen"]')!
        .hasAttribute('disabled')
    ).to.be.false;

    payload = detail({
      actions: { open_issue: true, reopen: true, dismiss: true },
      stale_reopen_requests: {
        'example/app#7': 'The issue was edited after this assessment.',
      },
    });
    el = await mount();
    const request = el.shadowRoot!.querySelector(
      '[data-testid="reopen-request"]'
    )!;
    expect(request.getAttribute('data-stale')).to.equal('true');
    expect(request.textContent).to.contain('Stale');
    expect(request.textContent).to.contain('edited after this assessment');
    expect(
      request
        .querySelector('[data-testid="action-reopen"]')!
        .hasAttribute('disabled')
    ).to.be.true;
  });

  it('expands evidence in place', async () => {
    const el = await mount();
    const root = el.shadowRoot!;
    const toggle = root.querySelector<HTMLButtonElement>(
      '[data-testid="shipped-change"] button.toggle'
    )!;
    expect(toggle.getAttribute('aria-expanded')).to.equal('false');
    toggle.click();
    await el.updateComplete;
    expect(toggle.getAttribute('aria-expanded')).to.equal('true');
    const body = root.getElementById(toggle.getAttribute('aria-controls')!)!;
    expect(body.textContent).to.contain('Export finishes under 2s');
    expect(body.textContent).to.contain('evidence gap');
    expect(body.textContent).to.contain('p95 not measured');
  });

  it('says when assessments were skipped for budget', async () => {
    payload = detail();
    payload.report.summary!.assessments_skipped_for_budget = true;
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-testid="budget-notice"]')).to
      .exist;
  });

  it('passes an axe check', async () => {
    const el = await mount();
    await expect(el).to.be.accessible();
  });
});

describe('delivery-reports-view', () => {
  let fetchStub: sinon.SinonStub;
  let calls: URL[];
  const total = 30;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.removeItem('preloop.delivery_reports.view_mode');
    calls = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = new URL(String(input), window.location.origin);
      calls.push(url);
      if (url.pathname === '/api/v1/delivery-reports') {
        const offset = Number(url.searchParams.get('offset'));
        const items = Array.from(
          { length: Math.min(25, total - offset) },
          (_, i) =>
            row(
              `run-${offset + i}`,
              i === 0 ? { projects: ['example/api'] } : {}
            )
        );
        return json({ items, total });
      }
      return json({});
    });
  });

  afterEach(() => {
    fetchStub.restore();
    sinon.restore();
  });

  async function mount(): Promise<DeliveryReportsView> {
    const el = await fixture<DeliveryReportsView>(
      html`<delivery-reports-view></delivery-reports-view>`
    );
    await waitUntil(() =>
      el.shadowRoot?.querySelector('[data-testid="report-row"]')
    );
    return el;
  }

  it('counts and pages with the server total', async () => {
    const el = await mount();
    const root = el.shadowRoot!;
    expect(
      root.querySelector('[data-testid="report-count"]')!.textContent
    ).to.equal('30 reports');
    expect(root.querySelectorAll('[data-testid="report-row"]').length).to.equal(
      25
    );
    expect(root.querySelector('[data-testid="pager"]')!.textContent).to.contain(
      '1–25 of 30'
    );
    const next = [
      ...root.querySelectorAll('[data-testid="pager"] sl-button'),
    ].find((b) => b.textContent?.trim() === 'Next') as HTMLElement;
    next.click();
    await waitUntil(() =>
      calls.some((c) => c.searchParams.get('offset') === '25')
    );
    await waitUntil(
      () => root.querySelectorAll('[data-testid="report-row"]').length === 5
    );
  });

  it('links each row to its report and shows unknown spend as a dash', async () => {
    const el = await mount();
    const link = el.shadowRoot!.querySelector<HTMLAnchorElement>(
      '[data-testid="report-row"] a'
    )!;
    expect(link.getAttribute('href')).to.equal(
      '/console/delivery-reports/run-0'
    );
    expect(link.textContent).to.contain('Run of 24 Oct');
  });

  it('passes an axe check', async () => {
    const el = await mount();
    // The filter labels are visually hidden, as on the Flows page; the
    // best-practice rule wants them printed.
    await expect(el).to.be.accessible({ ignoredRules: ['label-title-only'] });
  });
});
