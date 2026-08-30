import { expect } from '@open-wc/testing';

import type { AccountGatewayUsageSummaryResponse } from '../types';
import {
  compareByUsageMetric,
  mergeGatewaySummaryPreservingBreakdown,
  previewWindow,
  usageSortValue,
} from './top-models-overview';

function summary(
  overrides: Partial<AccountGatewayUsageSummaryResponse> = {}
): AccountGatewayUsageSummaryResponse {
  return {
    period_start: '2026-08-01T00:00:00Z',
    period_end: '2026-08-31T00:00:00Z',
    total_requests: 10,
    successful_requests: 9,
    failed_requests: 1,
    token_usage: {
      prompt_tokens: 1,
      completion_tokens: 1,
      total_tokens: 2,
    },
    estimated_cost: 1.25,
    budget: {
      monthly_limit_usd: null,
      soft_limit_usd: null,
      current_spend_usd: 1.25,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [],
    usage_by_flow: [],
    usage_by_session: [],
    ...overrides,
  };
}

describe('mergeGatewaySummaryPreservingBreakdown', () => {
  it('keeps previous session rows when the refresh has no breakdown', () => {
    const previous = summary({
      total_requests: 10,
      usage_by_model: [
        {
          ai_model_id: 'm1',
          model_alias: 'deepseek-v4-pro',
          provider_name: 'deepseek',
          request_count: 8,
          token_usage: {
            prompt_tokens: 1,
            completion_tokens: 1,
            total_tokens: 2,
          },
          estimated_cost: 4,
        },
      ],
      usage_by_session: [
        {
          flow_execution_id: null,
          flow_id: null,
          flow_name: null,
          session_reference: 's1',
          runtime_session_id: 's1',
          model_alias: 'deepseek-v4-pro',
          provider_name: 'deepseek',
          request_count: 8,
          token_usage: {
            prompt_tokens: 1,
            completion_tokens: 1,
            total_tokens: 2,
          },
          estimated_cost: 4,
          last_request_at: '2026-08-29T17:00:00Z',
        },
      ],
    });
    const incoming = summary({
      total_requests: 12,
      estimated_cost: 1.5,
    });

    const merged = mergeGatewaySummaryPreservingBreakdown(previous, incoming);
    expect(merged?.total_requests).to.equal(12);
    expect(merged?.estimated_cost).to.equal(1.5);
    expect(merged?.usage_by_session).to.equal(previous.usage_by_session);
    expect(merged?.usage_by_model).to.equal(previous.usage_by_model);
  });

  it('replaces the previous summary when the incoming payload has a breakdown', () => {
    const previous = summary({
      usage_by_session: [
        {
          flow_execution_id: null,
          flow_id: null,
          flow_name: null,
          session_reference: 'old',
          runtime_session_id: 'old',
          model_alias: 'm',
          provider_name: 'p',
          request_count: 1,
          token_usage: {
            prompt_tokens: 1,
            completion_tokens: 0,
            total_tokens: 1,
          },
          estimated_cost: 1,
          last_request_at: null,
        },
      ],
    });
    const incoming = summary({
      usage_by_model: [
        {
          ai_model_id: null,
          model_alias: 'm',
          provider_name: 'p',
          request_count: 2,
          token_usage: {
            prompt_tokens: 2,
            completion_tokens: 0,
            total_tokens: 2,
          },
          estimated_cost: 2,
        },
      ],
    });

    const merged = mergeGatewaySummaryPreservingBreakdown(previous, incoming);
    expect(merged).to.equal(incoming);
  });
});

describe('previewWindow', () => {
  it('returns the first N items and the overflow count when collapsed', () => {
    const { visible, overflow } = previewWindow([1, 2, 3, 4, 5], 4, false);
    expect(visible).to.deep.equal([1, 2, 3, 4]);
    expect(overflow).to.equal(1);
  });

  it('returns every item when expanded', () => {
    const { visible, overflow } = previewWindow([1, 2, 3, 4, 5], 4, true);
    expect(visible).to.deep.equal([1, 2, 3, 4, 5]);
    expect(overflow).to.equal(1);
  });

  it('has no overflow when the list fits the limit', () => {
    const { visible, overflow } = previewWindow(['a', 'b'], 4, false);
    expect(visible).to.deep.equal(['a', 'b']);
    expect(overflow).to.equal(0);
  });
});

describe('usageSortValue / compareByUsageMetric', () => {
  it('sorts spend by estimated_cost and usage by request count', () => {
    const cheap = { estimated_cost: 1, request_count: 50 };
    const pricey = { estimated_cost: 9, request_count: 2 };
    expect(usageSortValue(cheap, 'spend')).to.equal(1);
    expect(usageSortValue(pricey, 'usage')).to.equal(2);
    expect(compareByUsageMetric(cheap, pricey, 'spend')).to.be.greaterThan(0);
    expect(compareByUsageMetric(cheap, pricey, 'usage')).to.be.lessThan(0);
  });
});
