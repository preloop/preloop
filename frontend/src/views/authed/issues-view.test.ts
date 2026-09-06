import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './issues-view';
import { IssuesView } from './issues-view';

const tick = (ms = 200) => new Promise((r) => setTimeout(r, ms));

function makePair(n: number) {
  return {
    issue1: {
      id: `i${n}a`,
      key: `PRJ-${n}A`,
      title: `Issue ${n}A`,
      status: 'opened',
      url: 'https://example.com/a',
      meta_data: {},
    },
    issue2: {
      id: `i${n}b`,
      key: `PRJ-${n}B`,
      title: `Issue ${n}B`,
      status: 'opened',
      url: 'https://example.com/b',
      meta_data: {},
    },
    similarity: 0.95,
  };
}

interface StubOpts {
  projects?: unknown[];
  duplicates?: unknown[];
  duplicatesStatus?: number;
  aiStatus?: { configured: boolean; model_name: string | null };
  checkHandler?: (url: string) => Promise<Response> | Response;
}

function stubFetch(opts: StubOpts = {}) {
  const {
    projects = [],
    duplicates = [],
    duplicatesStatus = 200,
    aiStatus = { configured: true, model_name: 'gpt-test' },
    checkHandler,
  } = opts;
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), { status });
      if (url.includes('/api/v1/projects')) return json(projects);
      if (url.includes('/api/v1/organizations')) return json({ items: [] });
      if (url.includes('/api/v1/issue-duplicates/ai-status')) {
        return json(aiStatus);
      }
      // check must be matched before the broader issue-duplicates route.
      if (url.includes('/api/v1/issue-duplicates/check')) {
        if (checkHandler) return checkHandler(url);
        return json({ decision: 'duplicate', reason: 'same bug' });
      }
      if (url.includes('/api/v1/issue-duplicates')) {
        if (duplicatesStatus !== 200) return json({}, duplicatesStatus);
        return json({ duplicates });
      }
      return json({});
    });
}

describe('IssuesView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('renders the Similar issues header with a filter button', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick();
    await el.updateComplete;
    const header = el.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Similar issues');
    expect(header?.getAttribute('description')).to.equal(
      'Find overlapping issues and resolve duplicates'
    );
    expect(el.shadowRoot?.textContent).to.contain('Filter');
  });

  it('shows a no-projects empty state', async () => {
    fetchStub = stubFetch({ projects: [] });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain('No projects found');
  });

  it('renders a row for each duplicate pair', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1), makePair(2)],
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick();
    await el.updateComplete;
    const rows = el.shadowRoot?.querySelectorAll('tbody tr.clickable-row');
    expect(rows?.length).to.equal(2);
    expect(el.shadowRoot?.textContent).to.contain('PRJ-1A');
  });

  it('renders an error state when duplicates fail to load', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicatesStatus: 500,
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick();
    await el.updateComplete;
    expect((el as any)._error).to.be.a('string');
    expect(el.shadowRoot?.querySelector('.error')).to.exist;
  });

  it('renders verdict per row while another request is pending', async () => {
    let resolveSlow: ((value: Response) => void) | undefined;
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1), makePair(2)],
      checkHandler: (url) => {
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), { status: 200 });
        if (url.includes('i1a')) {
          return json({ decision: 'duplicate', reason: 'same' });
        }
        return new Promise((resolve) => {
          resolveSlow = resolve;
        });
      },
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    const verdicts = (
      el as unknown as { _verdicts: Record<string, { state: string }> }
    )._verdicts;
    expect(verdicts['i1a-i1b']?.state).to.equal('done');
    expect(verdicts['i2a-i2b']?.state).to.equal('checking');
    const firstCell = el.shadowRoot?.querySelector('#verdict-i1a-i1b');
    expect(firstCell, 'first verdict cell').to.exist;
    expect(firstCell?.textContent).to.contain('Duplicate');
    resolveSlow?.(
      new Response(JSON.stringify({ decision: 'overlapping' }), {
        status: 200,
      })
    );
  });

  it('shows no-model state and makes zero verdict calls when ai-status is unconfigured', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1), makePair(2)],
      aiStatus: { configured: false, model_name: null },
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    const checkCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/issue-duplicates/check')
      );
    expect(checkCalls.length).to.equal(0);
    const verdicts = (
      el as unknown as { _verdicts: Record<string, { state: string }> }
    )._verdicts;
    expect(verdicts['i1a-i1b']?.state).to.equal('no_model');
    expect(el.shadowRoot?.textContent).to.contain('No AI model');
  });

  it('shows failed state with a Review action after timeout', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1)],
      checkHandler: () => {
        const timeoutError = new Error('Request timed out');
        timeoutError.name = 'TimeoutError';
        return Promise.reject(timeoutError);
      },
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    const verdicts = (
      el as unknown as { _verdicts: Record<string, { state: string }> }
    )._verdicts;
    expect(verdicts['i1a-i1b']?.state).to.equal('timeout');
    const retry = el.shadowRoot?.querySelector(
      '#verdict-i1a-i1b sl-button'
    ) as HTMLElement;
    expect(retry).to.exist;
    expect(retry.textContent).to.contain('Review');
    expect(retry.textContent).to.not.contain('Retry');
    retry.click();
    await tick(100);
    const checkCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/issue-duplicates/check')
      );
    expect(checkCalls.length).to.be.greaterThan(1);
  });

  it('leads with Resolve as an outline action and a chip status', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1)],
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    const action = el.shadowRoot?.querySelector(
      '.actions-container sl-button'
    ) as HTMLElement;
    expect(action).to.exist;
    expect(action.textContent).to.contain('Resolve');
    expect(action.hasAttribute('outline')).to.be.true;
    const chip = el.shadowRoot?.querySelector('tbody sl-badge.chip');
    expect(chip).to.exist;
    expect(chip?.textContent?.trim()).to.equal('Opened');
  });

  it('has no info banner and no per-project chart', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1)],
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('duplicate-stats-chart')).to.not.exist;
    expect(el.shadowRoot?.textContent).to.not.contain(
      'Identify similar and potential duplicate issues'
    );
  });

  it('opens on 50% similarity rather than everything from 10% up', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1)],
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    const first = fetchStub
      .getCalls()
      .map((entry) => String(entry.args[0]))
      .filter((url) => url.includes('/api/v1/issue-duplicates?'))
      .shift();
    expect(first).to.contain('similarity_threshold=0.5');
    const select = el.shadowRoot?.querySelector(
      'sl-select.threshold-filter'
    ) as HTMLElement & { value: string };
    expect(select.value).to.equal('0.5');
  });

  it('refetches with the similarity threshold chosen in the bar', async () => {
    fetchStub = stubFetch({
      projects: [{ id: 'p1-xxxx', name: 'Project 1', key: 'P1' }],
      duplicates: [makePair(1)],
    });
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick(300);
    await el.updateComplete;
    const select = el.shadowRoot?.querySelector(
      'sl-select.threshold-filter'
    ) as HTMLElement & { value: string };
    expect(select).to.exist;
    select.value = '0.8';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await tick(200);
    const call = fetchStub
      .getCalls()
      .map((entry) => String(entry.args[0]))
      .filter(
        (url) =>
          url.includes('/api/v1/issue-duplicates?') ||
          url.includes('/api/v1/issue-duplicates&')
      )
      .pop();
    expect(call).to.contain('similarity_threshold=0.8');
  });

  it('opens the filter modal when the Filter button is clicked', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(html`<issues-view></issues-view>`)) as IssuesView;
    await tick();
    await el.updateComplete;
    (el as any)._openFilterModal();
    await el.updateComplete;
    expect((el as any)._isFilterModalOpen).to.be.true;
  });
});
