import { expect } from '@open-wc/testing';
import {
  renderWithoutSubscriptionsLine,
  subscriptionEstimate,
} from './subscription-estimate';
import type { SubscriptionUsageSummary } from '../types';

const block = (
  overrides: Partial<SubscriptionUsageSummary>
): SubscriptionUsageSummary => ({
  request_count: 3,
  prompt_tokens: 3000,
  completion_tokens: 300,
  total_tokens: 3300,
  api_equivalent_cost: 1.75,
  api_equivalent_cost_is_estimate: true,
  covered_requests: 3,
  coverage: 1,
  billed: null,
  billed_available: false,
  ...overrides,
});

describe('subscriptionEstimate (D-05)', () => {
  it('adds the API-equivalent cost to the metered spend', () => {
    const estimate = subscriptionEstimate(0.16, block({}));
    expect(estimate.value).to.be.closeTo(1.91, 1e-9);
    expect(estimate.partial).to.equal(false);
    expect(estimate.text).to.equal('$1.91');
    expect(estimate.title).to.contain('Estimate, not a bill');
  });

  it('is a lower bound when some subscription requests have no estimate', () => {
    const estimate = subscriptionEstimate(
      0,
      block({
        covered_requests: 1,
        coverage: 0.5,
        request_count: 2,
        api_equivalent_cost: 0.5,
      })
    );
    expect(estimate.value).to.equal(0.5);
    expect(estimate.partial).to.equal(true);
    expect(estimate.text).to.equal('at least $0.50');
    expect(estimate.title).to.contain('1 of 2 subscription requests');
  });

  it('says there is no estimate rather than $0 when none is covered', () => {
    const estimate = subscriptionEstimate(
      0.2,
      block({ covered_requests: 0, coverage: 0, api_equivalent_cost: 0 })
    );
    expect(estimate.value).to.equal(null);
    expect(estimate.text).to.equal('No estimate');
  });

  it('renders the table line only for agents with subscription use', async () => {
    const { render, html } = await import('lit');
    const host = document.createElement('div');
    render(
      html`${renderWithoutSubscriptionsLine(
        subscriptionEstimate(0, block({ covered_requests: 0, coverage: 0 })),
        3
      )}`,
      host
    );
    expect(host.querySelector('summary')?.textContent?.trim()).to.equal(
      'w/o subs. unknown'
    );
    expect(host.querySelector('details')?.textContent).to.contain(
      'None of 3 subscription requests'
    );
    render(
      html`${renderWithoutSubscriptionsLine(subscriptionEstimate(0.5, null), 0)}`,
      host
    );
    expect(host.textContent?.trim()).to.equal('');
  });

  it('equals the spend for an agent without subscription use', () => {
    expect(subscriptionEstimate(0.72, null).text).to.equal('$0.72');
  });
});
