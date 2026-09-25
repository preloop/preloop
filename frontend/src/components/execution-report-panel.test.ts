import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './execution-report-panel';
import type { ExecutionReportPanel } from './execution-report-panel';
import { invalidateApiCaches } from '../api';

const EXECUTION = '33333333-3333-4333-8333-333333333333';

const RESULT = {
  verdict: 'pass_with_findings',
  findings_summary: {
    counts_by_severity: { medium: 1, low: 1, high: 0 },
  },
  artifacts: {
    report: 'evidence/report.md',
    findings: 'evidence/findings.json',
  },
  register: {
    items: [
      { module: 'cli', lens: 'quality', status: 'met', note: 'clean' },
      { module: 'api', lens: 'correctness', status: 'gap', note: 'missing' },
      { module: 'api', lens: 'quality', status: 'partial', note: 'thin' },
    ],
  },
};

const FINDINGS = {
  findings: [
    {
      id: 'health:quality:a',
      severity: 'low',
      category: 'quality',
      file: 'src/a.ts',
      line: 4,
      recommendation: 'Quiet log',
    },
    {
      id: 'health:correctness:b',
      severity: 'medium',
      category: 'correctness',
      file: 'src/b.ts',
      line: 9,
      recommendation: 'Hold the task',
      status: 'open',
    },
  ],
};

const EVIDENCE = {
  status: 'available',
  sha256: 'abc123def456abc123def456abc123def456',
  integrity: 'not_checked',
  integrity_note: 'Availability only.',
  legal_hold: false,
  object_lock: false,
};

describe('ExecutionReportPanel', () => {
  let fetchStub: sinon.SinonStub;

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  function install(options?: {
    findings?: unknown;
    report?: string;
    membersStatus?: number;
    memberStatus?: number;
    legalHold?: boolean;
  }) {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      if (url.includes('/auth/users/me')) {
        return json({
          username: 'operator',
          email: 'operator@example.com',
          email_verified: true,
          permissions: null,
        });
      }
      if (url.includes('path=')) {
        if (options?.memberStatus) {
          return json({ detail: 'member missing' }, options.memberStatus);
        }
        if (url.includes('findings.json')) {
          return new Response(JSON.stringify(options?.findings ?? FINDINGS), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(
          options?.report ??
            '# What we checked\n\n## What you should do next\n',
          {
            status: 200,
            headers: { 'Content-Type': 'text/markdown' },
          }
        );
      }
      if (url.includes('/evidence/members')) {
        return json(
          {
            members: [
              {
                path: 'evidence/report.md',
                size_bytes: 40,
                sha256: 'aaa',
                content_type: 'text/markdown',
              },
            ],
            sha256: EVIDENCE.sha256,
            integrity: 'verified',
            legal_hold: false,
          },
          options?.membersStatus ?? 200
        );
      }
      return json({});
    });
  }

  async function mount(
    evidence: Record<string, unknown> | null = EVIDENCE,
    result: Record<string, unknown> | null = RESULT
  ) {
    const el = await fixture<ExecutionReportPanel>(html`
      <execution-report-panel
        execution-id=${EXECUTION}
        .result=${result}
        .evidence=${evidence}
      ></execution-report-panel>
    `);
    return el;
  }

  it('renders the report, findings and gap-first register', async () => {
    install();
    const el = await mount();
    await waitUntil(
      () => (el.shadowRoot?.textContent || '').includes('What we checked'),
      'report did not render'
    );
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('What you should do next');
    expect(
      el.shadowRoot!.querySelector('[data-testid="report-outline"]')!
        .textContent
    ).to.contain('What we checked');
    expect(text).to.contain('2 findings: 1 medium, 1 low');
    expect(text).to.contain('src/b.ts:9');
    const statuses = Array.from(
      el.shadowRoot!.querySelectorAll('[data-testid="register-table"] tbody tr')
    ).map((row) => row.children[2].textContent?.trim());
    expect(statuses?.slice(0, 2)).to.eql(['gap', 'partial']);
    expect(el.shadowRoot!.textContent).to.contain('not_checked');
    expect(el.shadowRoot!.textContent).to.contain('abc123');
  });

  it('filters findings by severity', async () => {
    install();
    const el = await mount();
    await waitUntil(() =>
      (el.shadowRoot?.textContent || '').includes('src/b.ts:9')
    );
    const select = el.shadowRoot!.querySelector(
      '[data-testid="severity-filter"]'
    ) as HTMLSelectElement;
    select.value = 'low';
    select.dispatchEvent(new Event('change'));
    await el.updateComplete;
    const ids = Array.from(
      el.shadowRoot!.querySelectorAll('[data-testid="findings-table"] tbody tr')
    ).map((row) => row.textContent || '');
    expect(ids).to.have.length(1);
    expect(ids[0]).to.contain('quality');
    expect(ids[0]).to.not.contain('correctness');
    select.value = '';
    select.dispatchEvent(new Event('change'));
    const lens = el.shadowRoot!.querySelector(
      '[data-testid="lens-filter"]'
    ) as HTMLSelectElement;
    lens.value = 'correctness';
    lens.dispatchEvent(new Event('change'));
    await el.updateComplete;
    const remaining = Array.from(
      el.shadowRoot!.querySelectorAll('[data-testid="findings-table"] tbody tr')
    ).map((row) => row.textContent || '');
    expect(remaining).to.have.length(1);
    expect(remaining[0]).to.contain('correctness');
  });

  it('shows a member error inline', async () => {
    install({ memberStatus: 404 });
    const el = await mount();
    await waitUntil(
      () => !!el.shadowRoot!.querySelector('[data-testid="report-error"]'),
      'error did not render'
    );
    expect(el.shadowRoot!.textContent).to.contain('member missing');
    expect(el.shadowRoot!.querySelector('[data-testid="execution-report"]')).to
      .exist;
  });

  it('explains an expired pack without a blank panel', async () => {
    install();
    const el = await mount({
      ...EVIDENCE,
      status: 'expired',
      integrity_note: 'Retention elapsed.',
    });
    await el.updateComplete;
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('Expired');
    expect(text).to.contain('Retention elapsed.');
    expect(el.shadowRoot!.querySelector('[data-testid="findings-table"]')).to
      .not.exist;
  });

  it('uses package columns for vulnerability findings', async () => {
    install({
      findings: {
        findings: [
          {
            id: 'CVE-2024-0001',
            pkg: 'libexample',
            severity: 'high',
            cvss: 8.1,
            kev: false,
            fix_version: '1.5.0',
            vex_status: 'not_affected',
          },
        ],
      },
    });
    const el = await mount();
    await waitUntil(() =>
      (el.shadowRoot?.textContent || '').includes('libexample')
    );
    const headers = Array.from(
      el.shadowRoot!.querySelectorAll('[data-testid="findings-table"] th')
    ).map((cell) => cell.textContent?.trim());
    expect(headers).to.include('Package');
    expect(headers).to.include('VEX');
    expect(el.shadowRoot!.textContent).to.contain('not_affected');
  });

  it('shows the held badge from the same status the records card uses', async () => {
    install();
    const el = await mount({ ...EVIDENCE, legal_hold: true });
    await waitUntil(() => (el.shadowRoot?.textContent || '').includes('Held'));
    expect(el.shadowRoot!.textContent).to.contain('Held');
  });
});
