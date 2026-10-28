import type {
  DeliveryReportDetail,
  DeliveryReportRow,
} from '../../delivery-reports-api';

export function row(
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

export function detail(
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
        closed_unverified: 1,
        follow_ups_proposed: 1,
        reopen_requests: 1,
        linkage: { linked: 2, total: 3 },
        criteria_availability: { with_criteria: 1, total: 2 },
        evidence_availability: { with_evidence: 1, total: 4 },
        closed_verdicts: {
          covered: 0,
          partial: 0,
          unverified: 1,
          no_criteria: 1,
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
          issues: ['example/app#8'],
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
          purpose: {
            verdict: 'unverified',
            source: 'report',
            criteria: [
              {
                text: 'Export control is labelled',
                met: true,
                evidence: ['src/export.test.ts'],
              },
              { text: 'Export finishes under 2s', met: null, evidence: [] },
              { text: 'Phone layout at 375px', met: null, evidence: [] },
              { text: 'Export includes all rows', met: null, evidence: [] },
            ],
          },
        },
        {
          ref: 'example/app#8',
          title: 'Login copy',
          purpose: { verdict: 'no_criteria', source: 'audit', criteria: [] },
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
