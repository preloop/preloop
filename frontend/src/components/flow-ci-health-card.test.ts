import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';
import {
  formatRate,
  formatSeconds,
  isCiHealthView,
  rateDelta,
  secondsDelta,
  sparklinePoints,
} from './flow-ci-health-card';
import './flow-ci-health-card';

const week = {
  start: '2026-01-08T12:00:00+00:00',
  end: '2026-01-15T12:00:00+00:00',
  runs: { total: 3, succeeded: 2, failed: 1, skipped: 4 },
  tokens: 450000,
  cost_usd_est: 4.5,
  pull_requests_opened: 2,
  pull_requests_merged: 1,
  failures_fixed: 1,
  failures_regressed: 0,
  runner_minutes_saved_per_week_est: 90,
  cost_per_merged_pull_request_usd_est: 4.5,
  tokens_per_merged_pull_request: 450000,
};

const point = (rate: number, queue: number) => ({
  at: '2026-01-14T06:00:00+00:00',
  success_rate: rate,
  queue_p95_s: queue,
  slowest_gate: 'test',
  slowest_gate_p95_s: 900,
  runner_minutes: 400,
  rerun_rate: 0.1,
});

const view = {
  generated_at: '2026-01-15T12:00:00+00:00',
  days: 7,
  first_run_at: '2025-12-26T12:00:00+00:00',
  last_run_at: '2026-01-15T06:00:00+00:00',
  ledger: { counts: { open: 2, regressed: 1 }, flaky: 1 },
  repositories: {
    'github:example-org/app': {
      latest: point(0.95, 60),
      prior: point(0.85, 240),
      series: [point(0.8, 300), point(0.85, 240), point(0.95, 60)],
    },
  },
  top_flakes: [
    {
      id: 'cccccccccccc',
      framework: 'pytest',
      test: 'tests/api/test_orders.py::test_retry',
      error: 'AssertionError',
      count: 4,
      flaky: true,
      status: 'regressed',
      first_seen: '2026-01-02T08:00:00+00:00',
      last_seen: '2026-01-14T08:00:00+00:00',
      linked: ['https://github.com/example-org/app/issues/12'],
      repositories: ['github:example-org/app'],
      jobs: ['test'],
    },
  ],
  week,
  prior_week: { ...week, pull_requests_merged: 0 },
  pull_requests: [],
};

describe('flow-ci-health-card', () => {
  beforeEach(() => localStorage.setItem('accessToken', 'test-access-token'));
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });

  it('formats durations, rates and grey deltas', () => {
    expect(formatSeconds(45)).to.equal('45s');
    expect(formatSeconds(250)).to.equal('4m 10s');
    expect(formatSeconds(3720)).to.equal('1h 02m');
    expect(formatSeconds(null)).to.equal('—');
    expect(formatRate(0.953)).to.equal('95%');
    expect(rateDelta(0.95, 0.85, 7)).to.equal('▲ 10 pts vs 7d ago');
    expect(rateDelta(0.85, 0.85, 7)).to.equal('No change vs 7d ago');
    expect(rateDelta(0.85, null, 7)).to.equal('');
    expect(secondsDelta(60, 240, 7)).to.equal('▼ 3m 00s vs 7d ago');
    expect(sparklinePoints([0, null, 1], 10, 10)).to.equal('0.0,10.0 10.0,0.0');
    expect(sparklinePoints([0.5])).to.equal('');
    expect(isCiHealthView(view)).to.equal(true);
    expect(isCiHealthView({ id: 'flow' })).to.equal(false);
    expect(isCiHealthView(null)).to.equal(false);
    expect(isCiHealthView({ ...view, days: undefined })).to.equal(false);
  });

  it('renders health, the failures worth a look and the value strip', async () => {
    const fetch = sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify(view)));
    const card = await fixture<any>(
      html`<flow-ci-health-card .flowId=${'f1'}></flow-ci-health-card>`
    );
    await waitUntil(() =>
      card.shadowRoot.querySelector('[data-testid="ci-health-card"]')
    );
    expect(String(fetch.firstCall.args[0])).to.contain(
      '/api/v1/flows/f1/ci-health?days=7'
    );
    expect(card.hidden).to.equal(false);
    const root = card.shadowRoot as ShadowRoot;
    const repo = root.querySelector('[data-testid="ci-health-repositories"]')!;
    expect(repo.textContent).to.contain('example-org/app');
    expect(repo.textContent).not.to.contain('github:');
    expect(repo.textContent).to.contain('95%');
    expect(repo.textContent).to.contain('▲ 10 pts vs 7d ago');
    expect(repo.querySelector('svg polyline')).to.not.equal(null);
    const flakes = root.querySelector('[data-testid="ci-health-flakes"]')!;
    expect(flakes.textContent).to.contain('Regressed');
    expect(flakes.querySelector('sl-badge')!.getAttribute('variant')).to.equal(
      'warning'
    );
    expect(flakes.querySelector('[data-testid="flaky-tag"]')).to.not.equal(
      null
    );
    const link = flakes.querySelector('a')!;
    expect(link.getAttribute('target')).to.equal('_blank');
    const value = root.querySelector('[data-testid="ci-health-value"]')!;
    expect(value.textContent).to.contain('1 merged');
    expect(value.textContent).to.contain('4 skipped');
    expect(
      root.querySelector('[data-testid="minutes-saved"]')!.textContent
    ).to.contain('90 est.');
    expect(
      root.querySelector('[data-testid="cost-per-pr"]')!.textContent
    ).to.contain('$4.50 est.');
  });

  it('stays hidden for a flow without a CI health ledger', async () => {
    sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response('', { status: 404 }));
    const card = await fixture<any>(
      html`<flow-ci-health-card .flowId=${'f2'}></flow-ci-health-card>`
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(card.hidden).to.equal(true);
    expect(card.shadowRoot.querySelector('sl-card')).to.equal(null);
  });

  it('drops a stale answer for the previous flow', async () => {
    let release: (value: Response) => void = () => {};
    sinon.stub(window, 'fetch').callsFake(async (url) => {
      if (String(url).includes('/flows/slow/')) {
        return new Promise<Response>((resolve) => (release = resolve));
      }
      return new Response(
        JSON.stringify({
          ...view,
          repositories: {
            'github:example-org/b': view.repositories['github:example-org/app'],
          },
        })
      );
    });
    const card = await fixture<any>(
      html`<flow-ci-health-card .flowId=${'slow'}></flow-ci-health-card>`
    );
    card.flowId = 'fast';
    await waitUntil(() =>
      card.shadowRoot.querySelector('[data-testid="ci-health-card"]')
    );
    release(new Response(JSON.stringify(view)));
    await new Promise((resolve) => setTimeout(resolve, 20));
    const text = card.shadowRoot.querySelector(
      '[data-testid="ci-health-repositories"]'
    )!.textContent;
    expect(text).to.contain('example-org/b');
    expect(text).not.to.contain('example-org/app');
  });
});
