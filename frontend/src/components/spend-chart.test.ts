import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './spend-chart';
import { bucketStarts, niceMax, type SpendChart } from './spend-chart';

const NOW = new Date('2026-03-31T15:00:00Z');

describe('spend-chart', () => {
  let fetchStub: sinon.SinonStub;
  let urls: URL[];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    urls = [];
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = new URL(String(input), location.origin);
        if (url.pathname.endsWith('/cost/timeseries')) urls.push(url);
        return new Response(
          JSON.stringify({
            period_start: url.searchParams.get('start_date'),
            period_end: url.searchParams.get('end_date'),
            granularity: url.searchParams.get('granularity'),
            buckets: [
              { date: '2026-03-30', request_count: 3, estimated_cost: 4.5 },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  async function chart(): Promise<SpendChart> {
    const el = await fixture<SpendChart>(
      html`<spend-chart .now=${() => NOW}></spend-chart>`
    );
    await waitUntil(() => el.shadowRoot!.querySelector('svg'));
    return el;
  }

  const last = () => urls[urls.length - 1];

  it('loads the last 30 UTC days by day and zero-fills empty buckets', async () => {
    const el = await chart();
    expect(last().pathname).to.equal('/api/v1/cost/timeseries');
    expect(last().searchParams.get('granularity')).to.equal('day');
    expect(last().searchParams.get('start_date')).to.equal(
      '2026-03-02T00:00:00.000Z'
    );
    expect(last().searchParams.get('end_date')).to.equal(
      '2026-04-01T00:00:00.000Z'
    );
    expect(el.shadowRoot!.querySelectorAll('rect.bar')).to.have.length(30);
    expect(el.shadowRoot!.textContent).to.contain('$4.50');
    // Axis ticks are labelled in dollars.
    expect(el.shadowRoot!.querySelector('.y-axis')!.textContent).to.contain(
      '$5.00'
    );
  });

  it('switches granularity and steps back and forward by one window', async () => {
    const el = await chart();
    const forward = el.shadowRoot!.querySelector(
      'sl-button.step-forward'
    ) as HTMLElement;
    expect(forward.hasAttribute('disabled')).to.equal(true);

    (
      Array.from(el.shadowRoot!.querySelectorAll('sl-button')).find(
        (b) => b.textContent!.trim() === 'Week'
      ) as HTMLElement
    ).click();
    await waitUntil(() => last().searchParams.get('granularity') === 'week');

    (
      el.shadowRoot!.querySelector('sl-button.step-back') as HTMLElement
    ).click();
    await waitUntil(
      () => last().searchParams.get('end_date') === '2026-03-02T00:00:00.000Z'
    );
    expect(last().searchParams.get('start_date')).to.equal(
      '2026-01-31T00:00:00.000Z'
    );
    await el.updateComplete;
    expect(forward.hasAttribute('disabled')).to.equal(false);
    forward.click();
    await waitUntil(
      () => last().searchParams.get('end_date') === '2026-04-01T00:00:00.000Z'
    );
  });

  it('shows a tooltip with the bucket, spend and requests on hover', async () => {
    const el = await chart();
    const hits = el.shadowRoot!.querySelectorAll('rect.hit');
    hits[28].dispatchEvent(new MouseEvent('mouseenter'));
    await el.updateComplete;
    const tip = el
      .shadowRoot!.querySelector('.tooltip')!
      .textContent!.replace(/\s+/g, ' ');
    expect(tip).to.contain('Mar 30, 2026');
    expect(tip).to.contain('$4.50');
    expect(tip).to.contain('3 requests');
  });

  it('draws exactly the page window while on Same as page, and follows it', async () => {
    const el = await fixture<SpendChart>(
      html`<spend-chart
        .now=${() => NOW}
        .period=${{
          startDate: '2026-03-01T15:00:00Z',
          endDate: '2026-03-31T15:00:00Z',
        }}
      ></spend-chart>`
    );
    await waitUntil(() => el.shadowRoot!.querySelector('svg'));
    // One request on mount, not one from connectedCallback and another from
    // the first update.
    expect(urls).to.have.length(1);
    expect(last().searchParams.get('start_date')).to.equal(
      '2026-03-01T15:00:00.000Z'
    );
    expect(last().searchParams.get('end_date')).to.equal(
      '2026-03-31T15:00:00.000Z'
    );
    // One range per page: the page's range select drives the chart, which
    // offers no range of its own, only the bucket size.
    expect(el.shadowRoot!.querySelector('sl-select')).to.not.exist;
    expect(el.shadowRoot!.querySelector('.step-back')).to.not.exist;
    expect(el.shadowRoot!.querySelector('sl-button-group[label="Bucket size"]'))
      .to.exist;
    expect(
      el.shadowRoot!.querySelector('.meta')!.textContent!.trim()
    ).to.not.equal('');
    el.period = {
      startDate: '2026-03-24T15:00:00Z',
      endDate: '2026-03-31T15:00:00Z',
    };
    await waitUntil(
      () => last().searchParams.get('start_date') === '2026-03-24T15:00:00.000Z'
    );
  });

  it('does not ask again when the page restates the same window', async () => {
    const el = await fixture<SpendChart>(
      html`<spend-chart
        .now=${() => NOW}
        .period=${{
          startDate: '2026-03-01T15:00:00Z',
          endDate: '2026-03-31T15:00:00Z',
        }}
      ></spend-chart>`
    );
    await waitUntil(() => el.shadowRoot!.querySelector('svg'));
    // The page first hands the window it asked for, then the one the server
    // echoed back, spelled differently. Same instants, one request.
    el.period = {
      startDate: '2026-03-01T15:00:00.000+00:00',
      endDate: '2026-03-31T15:00:00.000+00:00',
    };
    await el.updateComplete;
    expect(urls).to.have.length(1);
  });

  it('keeps its own range and stepping when no page range is given', async () => {
    const el = await chart();
    expect(el.shadowRoot!.querySelector('sl-select')).to.exist;
    expect(el.shadowRoot!.querySelector('.step-back')).to.exist;
  });

  it('builds ISO-week and month bucket starts', () => {
    const start = new Date('2026-03-04T00:00:00Z');
    const end = new Date('2026-04-10T00:00:00Z');
    expect(bucketStarts(start, end, 'week')[0]).to.equal('2026-03-02');
    expect(bucketStarts(start, end, 'month')).to.deep.equal([
      '2026-03-01',
      '2026-04-01',
    ]);
    expect(niceMax(3.2)).to.equal(5);
    expect(niceMax(0)).to.equal(1);
  });
});
