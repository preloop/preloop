import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from '../test-helpers/capability-api';
import { WeeklyReportList, withoutReportFilters } from './weekly-report-list';
import { LOCATION_CHANGED } from '../router';
import { WeeklyReportMode } from '../controllers/weekly-report-mode';
import type { ReactiveControllerHost } from 'lit';

const path = '/api/v1/cost/report-items';
const account = '00000000-0000-4000-8000-000000000001';
const start = '2026-11-02T00:00:00.123456Z';
const end = '2026-11-09T00:00:00.123456Z';
const link = `/console/cost/spend?kind=cache&account_id=${account}&start_date=${start}&end_date=${end}`;
const result = {
  title: 'Prompt cache recommendations',
  total: 6,
  items: [
    {
      label: '<unsafe> prefix',
      value: '2 breaks',
      meta: 'Example agent',
      detail: 'Stored evidence',
      url: '/console/runtime-sessions?sessionId=example',
    },
  ],
  notes: ['Five busiest sessions in this week.'],
};

describe('weekly report filtered lists', () => {
  let api: MockApi;
  let original: string;
  beforeEach(() => {
    original = window.location.href;
    signInForTest();
    window.history.replaceState({}, '', link);
  });
  afterEach(() => {
    api.restore();
    window.history.replaceState({}, '', original);
  });

  it('shows the counted list, exact week, active filters and a clear action', async () => {
    api = mockApi({ routes: [{ path, body: result }] });
    const el = await fixture<WeeklyReportList>(
      html`<weekly-report-list></weekly-report-list>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('tbody'));
    const text = el.shadowRoot!.textContent!;
    expect(text).to.include('6 items');
    expect(text).to.include(start);
    expect(text).to.include(end);
    expect(text).to.include('kind: cache');
    expect(text).to.include('<unsafe> prefix');
    expect(el.shadowRoot!.querySelector('unsafe')).to.equal(null);
    expect(
      el.shadowRoot!.querySelector('.filters a')?.getAttribute('href')
    ).to.equal('/console/cost/spend');
    const query = new URLSearchParams(api.callsTo(path)[0].search);
    expect(query.get('start_date')).to.equal(start);
    expect(query.get('account_id')).to.equal(account);
  });

  it('preserves model, agent, flow and status filters on reload and back navigation', async () => {
    window.history.replaceState(
      {},
      '',
      link +
        '&model=example%2Fmodel&agent=Example&flow=example-flow&status=pending'
    );
    api = mockApi({ routes: [{ path, body: result }] });
    const el = await fixture<WeeklyReportList>(
      html`<weekly-report-list></weekly-report-list>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('tbody'));
    const query = new URLSearchParams(api.callsTo(path)[0].search);
    for (const [key, value] of Object.entries({
      model: 'example/model',
      agent: 'Example',
      flow: 'example-flow',
      status: 'pending',
    }))
      expect(query.get(key)).to.equal(value);
    window.history.replaceState({}, '', link + '&kind=models');
    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitUntil(() => !!el.shadowRoot?.querySelector('[role="alert"]'));
    expect(el.shadowRoot!.querySelector('tbody')).to.equal(null);
  });

  for (const status of [403, 409, 500]) {
    it(`shows an explicit HTTP ${status} state without a misleading list`, async () => {
      api = mockApi({ routes: [{ path, status }] });
      const el = await fixture<WeeklyReportList>(
        html`<weekly-report-list></weekly-report-list>`
      );
      await waitUntil(() => !!el.shadowRoot?.querySelector('[role="alert"]'));
      expect(el.shadowRoot!.querySelector('tbody')).to.equal(null);
      if (status === 409)
        expect(el.shadowRoot!.textContent).to.include('Switch accounts');
    });
  }

  it('does not request an invalid or incomplete report window', async () => {
    api = mockApi({ routes: [{ path, body: result }] });
    window.history.replaceState({}, '', '/console/cost/spend?kind=cache');
    const el = await fixture<WeeklyReportList>(
      html`<weekly-report-list></weekly-report-list>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('[role="alert"]'));
    expect(api.callsTo(path)).to.have.length(0);
  });

  it('keeps unrelated parameters and hash when filters are cleared', () => {
    api = mockApi();
    expect(
      withoutReportFilters(link + '&model=example&panel=pricing#detail')
    ).to.equal('/console/cost/spend?panel=pricing#detail');
  });

  it('paginates without losing the report context', async () => {
    api = mockApi({ routes: [{ path, body: { ...result, total: 55 } }] });
    const el = await fixture<WeeklyReportList>(
      html`<weekly-report-list></weekly-report-list>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('tbody'));
    (
      el.shadowRoot!.querySelector(
        '.pagination sl-button:last-child'
      ) as HTMLElement
    ).click();
    await waitUntil(() => api.callsTo(path).length === 2);
    const query = new URLSearchParams(api.callsTo(path)[1].search);
    expect(query.get('offset')).to.equal('50');
    expect(query.get('account_id')).to.equal(account);
    expect(query.get('start_date')).to.equal(start);
  });

  it('refreshes reused views after in-app navigation and avoids double loading', async () => {
    api = mockApi({ routes: [{ path, body: result }] });
    const el = await fixture<WeeklyReportList>(
      html`<weekly-report-list></weekly-report-list>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('tbody'));
    window.history.replaceState(
      {},
      '',
      link.replace('kind=cache', 'kind=models')
    );
    window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
    await waitUntil(() => api.callsTo(path).length === 2);
    expect(
      new URLSearchParams(api.callsTo(path)[1].search).get('kind')
    ).to.equal('models');
    window.dispatchEvent(new PopStateEvent('popstate'));
    window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
    expect(api.callsTo(path)).to.have.length(2);
  });

  it('clears report mode and restores the ordinary page after in-app navigation', () => {
    api = mockApi();
    const host: ReactiveControllerHost = {
      addController: () => {},
      removeController: () => {},
      requestUpdate: () => {},
      updateComplete: Promise.resolve(true),
    };
    let restored = 0;
    const mode = new WeeklyReportMode(host, () => {
      ++restored;
    });
    mode.hostConnected();
    expect(mode.active).to.equal(true);
    window.history.replaceState({}, '', '/console/cost/spend');
    window.dispatchEvent(new CustomEvent(LOCATION_CHANGED));
    expect(mode.active).to.equal(false);
    expect(restored).to.equal(1);
    mode.hostDisconnected();
  });
});
