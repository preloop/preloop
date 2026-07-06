import { fixture, html, expect, oneEvent } from '@open-wc/testing';
import './session-request-timeline';
import type { SessionRequestTimeline } from './session-request-timeline';
import type { RuntimeSessionRequestItem } from '../types';

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
