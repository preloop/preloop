import { fixture, html, expect, oneEvent } from '@open-wc/testing';
import './session-request-timeline';
import type { SessionRequestTimeline } from './session-request-timeline';
import type {
  RuntimeSessionCacheSummary,
  RuntimeSessionRequestItem,
} from '../types';

function makeRequest(
  overrides: Partial<RuntimeSessionRequestItem>
): RuntimeSessionRequestItem {
  return {
    id: 'r1',
    timestamp: '2026-03-09T20:00:00Z',
    model_alias: 'gpt-4o',
    provider_name: 'openai',
    status_code: 200,
    is_error: false,
    finish_reason: 'stop',
    is_retry: false,
    prompt_tokens: 500,
    completion_tokens: 500,
    total_tokens: 1000,
    estimated_cost: 0.02,
    endpoint: '/v1/chat/completions',
    tools: [],
    tools_total_schema_tokens: 0,
    ...overrides,
  };
}

describe('SessionRequestTimeline', () => {
  const requests: RuntimeSessionRequestItem[] = [
    makeRequest({
      id: 'cheap',
      estimated_cost: 0.01,
      total_tokens: 100,
      timestamp: '2026-03-09T20:00:00Z',
      tools: [
        {
          name: 'search_issues',
          source: 'github',
          schema_tokens_estimate: 120,
          stripped: false,
        },
      ],
      tools_total_schema_tokens: 120,
    }),
    makeRequest({
      id: 'pricey',
      estimated_cost: 0.5,
      total_tokens: 5000,
      timestamp: '2026-03-09T20:05:00Z',
    }),
    makeRequest({
      id: 'failed',
      estimated_cost: 0.0,
      total_tokens: 10,
      status_code: 500,
      is_error: true,
      timestamp: '2026-03-09T20:10:00Z',
    }),
  ];

  it('renders one merged stream with tokens, cost and tools', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${requests}
        .total=${3}
        .failedCount=${1}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.include('search_issues');
    expect(text.replace(/\s+/g, ' ')).to.include('schema tokens');
    expect(text.replace(/\s+/g, ' ')).to.include('3 total requests');
    expect(el.shadowRoot?.querySelectorAll('.request-row').length).to.equal(3);
  });

  it('sorts costliest first when selected', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${requests}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    const select = el.shadowRoot?.querySelector('select') as HTMLSelectElement;
    select.value = 'costliest';
    select.dispatchEvent(new Event('change'));
    await el.updateComplete;
    const firstRow = el.shadowRoot?.querySelector('.request-row');
    expect(firstRow?.textContent).to.include('$0.50');
  });

  it('filters out events below the cost threshold', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${requests}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    const numberInput = el.shadowRoot?.querySelector(
      'input[type="number"]'
    ) as HTMLInputElement;
    numberInput.value = '0.1';
    numberInput.dispatchEvent(new Event('input'));
    await el.updateComplete;
    // Only the $0.50 request survives a $0.10 threshold.
    expect(el.shadowRoot?.querySelectorAll('.request-row').length).to.equal(1);
    expect(
      el.shadowRoot?.querySelector('.request-row')?.textContent
    ).to.include('$0.50');
  });

  it('emits a failed-only event when the toggle is clicked', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${requests}
        .failedCount=${1}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    const button = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((b) => b.textContent?.includes('Failed requests only'));
    expect(button).to.exist;
    setTimeout(() => button!.click());
    const event = await oneEvent(el, 'request-timeline-failed-only');
    expect(event.detail.failedOnly).to.be.true;
  });
});

function makeCacheSummary(
  overrides: Partial<RuntimeSessionCacheSummary> = {}
): RuntimeSessionCacheSummary {
  return {
    requests_total: 4,
    requests_with_cache_data: 3,
    requests_without_cache_data: 1,
    covered_prompt_tokens: 3000,
    uncovered_prompt_tokens: 2000,
    cached_prompt_tokens: 2400,
    uncached_prompt_tokens: 300,
    cache_write_tokens: 300,
    cache_hit_ratio: 0.8889,
    estimated_cache_savings_usd: 0.0125,
    savings_basis: 'catalog_exact',
    savings_omitted_reason: null,
    ...overrides,
  };
}

describe('SessionRequestTimeline cache accounting', () => {
  const plainRequests: RuntimeSessionRequestItem[] = [
    makeRequest({ id: 'a' }),
    makeRequest({ id: 'b' }),
  ];

  it('renders read/write/miss for a call the provider reported on', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${[
          makeRequest({
            id: 'cached',
            prompt_tokens: 10000,
            cache: {
              cache_read_tokens: 7000,
              cache_creation_tokens: 2000,
              cache_miss_tokens: 1000,
              cache_miss_source: 'derived',
              has_cache_data: true,
              usage_source: 'provider',
            },
          }),
        ]}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const line = el.shadowRoot?.querySelector(
      '[data-testid="request-cache-line"]'
    );
    expect(line).to.exist;
    const text = (line?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.include('read 7,000');
    expect(text).to.include('write 2,000');
    expect(text).to.include('miss 1,000');
  });

  it('says "not reported" instead of zero for an absent write count', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${[
          makeRequest({
            id: 'openai',
            cache: {
              cache_read_tokens: 1024,
              cache_creation_tokens: null,
              cache_miss_tokens: 476,
              cache_miss_source: 'derived',
              has_cache_data: true,
              usage_source: 'provider',
            },
          }),
        ]}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const text = (
      el.shadowRoot?.querySelector('[data-testid="request-cache-line"]')
        ?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(text).to.include('write not reported');
    expect(text).to.not.include('write 0');
  });

  it('omits the cache line entirely when the provider reported nothing', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${[
          makeRequest({
            id: 'blind',
            cache: {
              cache_read_tokens: null,
              cache_creation_tokens: null,
              cache_miss_tokens: null,
              cache_miss_source: null,
              has_cache_data: false,
              usage_source: 'provider',
            },
          }),
        ]}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('[data-testid="request-cache-line"]'))
      .to.not.exist;
  });

  it('renders the session rollup with ratio, writes and savings', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${plainRequests}
        .cacheSummary=${makeCacheSummary()}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const block = el.shadowRoot?.querySelector(
      '[data-testid="session-cache-summary"]'
    );
    expect(block).to.exist;
    expect(
      el.shadowRoot?.querySelector('[data-testid="cache-hit-ratio"]')
        ?.textContent
    ).to.include('88.9%');
    expect(
      el.shadowRoot?.querySelector('[data-testid="cache-write-tokens"]')
        ?.textContent
    ).to.include('300');
    expect(
      el.shadowRoot?.querySelector('[data-testid="cache-savings"]')?.textContent
    ).to.include('$0.0125');
  });

  it('states coverage when some requests reported no cache data', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${plainRequests}
        .cacheSummary=${makeCacheSummary()}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const text = (
      el.shadowRoot?.querySelector('[data-testid="cache-coverage"]')
        ?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(text).to.include('3 of 4 requests');
    expect(text).to.include('not counted as misses');
  });

  it('shows the reason instead of a number when savings are unpriced', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${plainRequests}
        .cacheSummary=${makeCacheSummary({
          estimated_cache_savings_usd: null,
          savings_basis: null,
          savings_omitted_reason: 'no_catalog_cache_price',
        })}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const text =
      el.shadowRoot?.querySelector('[data-testid="cache-savings"]')
        ?.textContent || '';
    expect(text).to.include('no exact catalog price');
    expect(text).to.not.include('$');
  });

  it('renders cache writes as "not reported" when no provider reports them', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${plainRequests}
        .cacheSummary=${makeCacheSummary({ cache_write_tokens: null })}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    expect(
      el.shadowRoot?.querySelector('[data-testid="cache-write-tokens"]')
        ?.textContent
    ).to.include('not reported');
  });

  it('does not round a sub-cent savings figure down to $0.01', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${plainRequests}
        .cacheSummary=${makeCacheSummary({
          estimated_cache_savings_usd: 0.0125,
        })}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    const text =
      el.shadowRoot?.querySelector('[data-testid="cache-savings"]')
        ?.textContent || '';
    expect(text).to.include('$0.0125');
  });

  it('renders no rollup when no request carried cache data', async () => {
    const el = (await fixture(
      html`<session-request-timeline
        .requests=${plainRequests}
        .cacheSummary=${makeCacheSummary({
          requests_with_cache_data: 0,
          cached_prompt_tokens: 0,
          uncached_prompt_tokens: 0,
          cache_hit_ratio: null,
        })}
      ></session-request-timeline>`
    )) as SessionRequestTimeline;
    await el.updateComplete;
    expect(
      el.shadowRoot?.querySelector('[data-testid="session-cache-summary"]')
    ).to.not.exist;
  });
});
