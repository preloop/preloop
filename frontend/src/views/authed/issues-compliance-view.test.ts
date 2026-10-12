import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './issues-compliance-view';
import type { IssuesComplianceView } from './issues-compliance-view';

const tick = (ms = 200) => new Promise((r) => setTimeout(r, ms));

const issue = {
  id: 'issue-1',
  key: 'ALP-1',
  title: 'Fix login',
  status: 'open',
  description: 'Search returns 500',
  project_id: 'p1',
  url: 'https://example.com/1',
};

interface StubOpts {
  complianceStatus?: number;
  compliance?: unknown;
  batchResults?: 'scored' | 'empty';
}

function stubFetch(opts: StubOpts = {}) {
  const { complianceStatus = 200, compliance, batchResults = 'scored' } = opts;
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), { status });
      if (url.includes('/api/v1/issue_compliance_prompts')) {
        return json([{ id: 'default', name: 'Definition of ready' }]);
      }
      const scored = compliance || {
        name: 'Definition of ready',
        compliance_factor: 0.6,
        reason: 'Missing acceptance criteria',
        suggestion: 'Add acceptance criteria',
      };
      if (url.includes('/api/v1/issue_compliance/batch')) {
        if (batchResults === 'empty') return json({ results: {} });
        return json({ results: { [issue.id]: scored } });
      }
      if (url.includes('/api/v1/issue_compliance/')) {
        if (complianceStatus !== 200) return json({}, complianceStatus);
        return json(scored);
      }
      if (url.includes('/api/v1/projects')) {
        return json([{ id: 'p1', name: 'Alpha', organization_id: 'org-1' }]);
      }
      if (url.includes('/api/v1/organizations')) {
        return json({ items: [{ id: 'org-1', name: 'Org' }] });
      }
      if (url.includes('/api/v1/search')) {
        return json({ results: [{ item: issue }] });
      }
      return json({});
    });
}

async function mount() {
  const el = (await fixture(
    html`<issues-compliance-view></issues-compliance-view>`
  )) as IssuesComplianceView;
  await tick(300);
  await el.updateComplete;
  return el;
}

describe('IssuesComplianceView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('offers Improve on a scored issue', async () => {
    fetchStub = stubFetch();
    const el = await mount();
    const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
    // One batch call for the page, no per-row model-backed GETs.
    expect(
      urls.filter((u) => u.includes('/issue_compliance/batch'))
    ).to.have.length(1);
    expect(
      urls.filter(
        (u) => u.includes('/issue_compliance/') && !u.includes('/batch')
      )
    ).to.have.length(0);
    const action = el.shadowRoot?.querySelector(
      'tbody sl-button'
    ) as HTMLElement;
    expect(action).to.exist;
    expect(action.textContent?.trim()).to.equal('Improve');
    expect(action.hasAttribute('outline')).to.be.true;
    expect(el.shadowRoot?.textContent).to.not.contain('Not scored');
  });

  it('says Not scored and offers Score when the score is missing', async () => {
    fetchStub = stubFetch({ batchResults: 'empty' });
    const el = await mount();
    expect(el.shadowRoot?.textContent).to.contain('Not scored');
    const action = el.shadowRoot?.querySelector(
      'tbody sl-button'
    ) as HTMLElement;
    expect(action.textContent?.trim()).to.equal('Score');

    const before = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/api/v1/issue_compliance/')
      ).length;
    action.click();
    await tick(200);
    const after = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/api/v1/issue_compliance/')
      ).length;
    expect(after).to.be.greaterThan(before);
  });

  it('pages with the shared pager and a chosen page size', async () => {
    fetchStub = stubFetch();
    const el = await mount();
    const calls = () =>
      fetchStub
        .getCalls()
        .map((call) => new URL(String(call.args[0]), window.location.origin))
        .filter((url) => url.pathname.includes('/api/v1/search'));
    await waitUntil(() => calls().length > 0, 'first page');
    (el as any)._onPageChange(
      new CustomEvent('page-change', { detail: { pageIndex: 2 } })
    );
    await waitUntil(
      () => calls().at(-1)!.searchParams.get('skip') === '20',
      'third page of ten'
    );
    (el as any)._onPageSizeChange(
      new CustomEvent('page-size-change', { detail: { pageSize: 25 } })
    );
    await waitUntil(
      () =>
        calls().at(-1)!.searchParams.get('limit') === '26' &&
        (calls().at(-1)!.searchParams.get('skip') ?? '0') === '0',
      'first page of 25'
    );
  });
});
