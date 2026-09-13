import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { RepriceJobStatusElement } from './reprice-job-status';

describe('reprice-job-status', () => {
  let fetchStub: sinon.SinonStub;
  let payload: Record<string, unknown>;
  const defaults = {
    interval: RepriceJobStatusElement.POLL_INTERVAL_MS,
    attempts: RepriceJobStatusElement.POLL_MAX_ATTEMPTS,
  };
  const timing = RepriceJobStatusElement as unknown as {
    POLL_INTERVAL_MS: number;
    POLL_MAX_ATTEMPTS: number;
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    timing.POLL_INTERVAL_MS = 1;
    timing.POLL_MAX_ATTEMPTS = 3;
    payload = {
      id: 'job-1',
      status: 'queued',
      rows_examined: null,
      rows_updated: null,
      rows_skipped: null,
    };
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify(payload)));
  });

  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    timing.POLL_INTERVAL_MS = defaults.interval;
    timing.POLL_MAX_ATTEMPTS = defaults.attempts;
  });

  const content = (element: RepriceJobStatusElement) =>
    element.shadowRoot!.textContent!;
  const mount = () =>
    fixture<RepriceJobStatusElement>(
      html`<reprice-job-status jobId="job-1"></reprice-job-status>`
    );

  it('shows queued and running states, then the actual completion counters', async () => {
    timing.POLL_INTERVAL_MS = 100;
    const element = await mount();
    await waitUntil(() => content(element).includes('queued'));
    expect(content(element)).to.contain('job-1');
    expect(content(element)).not.to.contain('0 of 0');
    payload = { ...payload, status: 'running' };
    await waitUntil(() => content(element).includes('running'));
    payload = {
      ...payload,
      status: 'succeeded',
      rows_examined: 5,
      rows_updated: 3,
      rows_skipped: 2,
    };
    await waitUntil(() => content(element).includes('succeeded'));
    expect(content(element)).to.contain('3 of 5 requests updated, 2 skipped');
    expect(element.shadowRoot!.querySelector('sl-button')).not.to.exist;
    expect(fetchStub.callCount).to.equal(3);
    expect(String(fetchStub.firstCall.args[0])).to.equal(
      '/api/v1/billing/cost/reprice/job-1'
    );
  });

  it('explains why some provider actuals remain unavailable', async () => {
    payload = {
      ...payload,
      status: 'succeeded',
      rows_examined: 5,
      rows_updated: 1,
      rows_skipped: 4,
      provider_lookup: {
        recovered: 1,
        missing_id: 1,
        unavailable: 1,
        deferred: 1,
        ambiguous_cost: 1,
      },
    };
    const element = await mount();
    await waitUntil(() => content(element).includes('succeeded'));
    expect(content(element)).to.contain('Provider cost lookup: 1 recovered');
    expect(content(element)).to.contain('1 missing generation IDs');
    expect(content(element)).to.contain('1 unavailable');
    expect(content(element)).to.contain('1 deferred by limits');
    expect(content(element)).to.contain('1 ambiguous charges');
  });

  it('labels stalled jobs as overdue and leaves completion unconfirmed', async () => {
    payload = { ...payload, status: 'running', stalled: true };
    const element = await mount();
    await waitUntil(() =>
      content(element).includes('Automatic checks stopped')
    );
    expect(content(element)).to.contain(
      'Worker heartbeat overdue; completion unconfirmed'
    );
    expect(content(element)).not.to.contain('succeeded');
  });

  it('identifies counters from a later worker attempt', async () => {
    payload = {
      ...payload,
      status: 'succeeded',
      attempts: 2,
      rows_examined: 2,
      rows_updated: 2,
      rows_skipped: 0,
    };
    const element = await mount();
    await waitUntil(() => content(element).includes('succeeded'));
    expect(content(element)).to.contain('2 of 2 requests updated');
    expect(content(element)).to.contain(
      'Attempt 2; counts are for this attempt'
    );
    expect(content(element)).to.contain(
      'Earlier attempts may have updated some requests'
    );
  });

  it('reports a dry run as a preview without claiming historical writes', async () => {
    payload = {
      ...payload,
      status: 'succeeded',
      dry_run: true,
      rows_examined: 5,
      rows_updated: 3,
      rows_skipped: 2,
    };
    const element = await mount();
    await waitUntil(() => content(element).includes('preview succeeded'));
    expect(content(element)).to.contain('3 of 5 requests would be updated');
    expect(content(element)).not.to.contain('requests updated');
  });

  it('reports a worker failure and stops polling', async () => {
    payload = {
      ...payload,
      status: 'failed',
      error: 'Worker could not finish.',
    };
    const element = await mount();
    await waitUntil(() => content(element).includes('failed'));
    expect(content(element)).to.contain('Worker could not finish.');
    expect(element.shadowRoot!.querySelector('[role="alert"]')).to.exist;
    expect(fetchStub.callCount).to.equal(1);
  });

  it('stops automatic checks while queued and allows one explicit status refresh', async () => {
    const paused: unknown[] = [];
    const element = await mount();
    element.addEventListener('reprice-paused', (event) => paused.push(event));
    await waitUntil(() =>
      content(element).includes('Automatic checks stopped')
    );
    expect(content(element)).to.contain('Completion is unconfirmed');
    expect(content(element)).not.to.contain('succeeded');
    expect(fetchStub.callCount).to.equal(3);
    expect(paused).to.have.length(1);
    payload = {
      ...payload,
      status: 'succeeded',
      rows_examined: 5,
      rows_updated: 3,
      rows_skipped: 2,
    };
    (element.shadowRoot!.querySelector('sl-button') as HTMLElement).click();
    await waitUntil(() => content(element).includes('succeeded'));
    expect(fetchStub.callCount).to.equal(4);
  });

  it('does not convert a status endpoint failure into worker failure or success', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({ detail: 'Status service unavailable' }), {
          status: 503,
        })
    );
    const element = await mount();
    await waitUntil(() =>
      content(element).includes('Automatic checks stopped')
    );
    expect(content(element)).to.contain('Status service unavailable');
    expect(content(element)).to.contain('Completion is unconfirmed');
    expect(content(element)).not.to.contain('Repricing failed');
    expect(content(element)).not.to.contain('succeeded');
    expect(fetchStub.callCount).to.equal(3);
  });

  it('stops issuing status requests after leaving the view', async () => {
    timing.POLL_INTERVAL_MS = 20;
    const element = await mount();
    await waitUntil(() => content(element).includes('queued'));
    const calls = fetchStub.callCount;
    element.remove();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fetchStub.callCount).to.equal(calls);
  });

  it('resumes observation when the same pending element reconnects', async () => {
    const element = await mount();
    await waitUntil(() =>
      content(element).includes('Automatic checks stopped')
    );
    const parent = element.parentElement!;
    element.remove();
    payload = {
      ...payload,
      status: 'succeeded',
      rows_examined: 1,
      rows_updated: 1,
      rows_skipped: 0,
    };
    parent.append(element);
    await waitUntil(() => content(element).includes('1 of 1'));
    expect(fetchStub.callCount).to.equal(4);
  });

  it('ignores an old job response after the displayed job changes', async () => {
    let resolveFirst!: (response: Response) => void;
    fetchStub.onFirstCall().returns(
      new Promise<Response>((resolve) => {
        resolveFirst = resolve;
      })
    );
    const element = await mount();
    element.jobId = 'job-2';
    payload = {
      ...payload,
      id: 'job-2',
      status: 'succeeded',
      rows_examined: 2,
      rows_updated: 2,
      rows_skipped: 0,
    };
    await waitUntil(() => content(element).includes('2 of 2'));
    resolveFirst(
      new Response(
        JSON.stringify({
          ...payload,
          id: 'job-1',
          status: 'failed',
          error: 'Old failure',
        })
      )
    );
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(content(element)).not.to.contain('Old failure');
    expect(content(element)).to.contain('job-2');
  });
});
