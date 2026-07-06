import { html, fixture, expect, oneEvent } from '@open-wc/testing';
import './budget-health-card.ts';
import type { BudgetHealthCard } from './budget-health-card';
import type { BudgetPolicy } from '../api';
import type { AccountGatewayUsageSummaryResponse } from '../types';

describe('BudgetHealthCard', () => {
  const summary: AccountGatewayUsageSummaryResponse = {
    period_start: '2026-03-01T00:00:00Z',
    period_end: '2026-03-31T00:00:00Z',
    total_requests: 10,
    successful_requests: 9,
    failed_requests: 1,
    token_usage: {
      prompt_tokens: 100,
      completion_tokens: 50,
      total_tokens: 150,
    },
    estimated_cost: 12.5,
    budget: {
      monthly_limit_usd: 100,
      soft_limit_usd: 80,
      current_spend_usd: 25,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [],
    usage_by_flow: [],
    usage_by_session: [],
  };

  const policies: BudgetPolicy[] = [
    {
      id: 'policy-1',
      subject_type: 'global',
      subject_id: 'global',
      model_alias: null,
      period: 'monthly',
      hard_limit_usd: 100,
      soft_limit_usd: 80,
      notify_on_soft: true,
      notify_on_hard: true,
      notification_emails: ['ops@example.com'],
    },
  ];

  it('renders budget health region with progress bars', async () => {
    const element = (await fixture(html`
      <budget-health-card
        .summary=${summary}
        .policies=${policies}
        .configurable=${true}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    const region = element.shadowRoot?.querySelector('[role="region"]');
    expect(region).to.exist;

    const progressBars = element.shadowRoot?.querySelectorAll(
      '[role="progressbar"]'
    );
    expect(progressBars?.length).to.be.greaterThan(0);
    expect(progressBars?.[0]?.getAttribute('aria-valuemin')).to.equal('0');
    expect(progressBars?.[0]?.getAttribute('aria-valuemax')).to.equal('100');
  });

  it('shows green and warning fill when soft limit is reached', async () => {
    const softLimitSummary: AccountGatewayUsageSummaryResponse = {
      ...summary,
      budget: {
        monthly_limit_usd: 100,
        soft_limit_usd: 80,
        current_spend_usd: 90,
        soft_limit_exceeded: true,
        hard_limit_exceeded: false,
      },
    };
    const softLimitPolicies: BudgetPolicy[] = [
      {
        ...policies[0],
        period: 'daily',
        hard_limit_usd: 120,
        soft_limit_usd: 80,
      },
    ];

    const element = (await fixture(html`
      <budget-health-card
        .summary=${softLimitSummary}
        .policies=${softLimitPolicies}
        .timeRange=${'day'}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    expect(
      element.shadowRoot?.querySelector(
        '.budget-track-fill:not(.warning):not(.danger)'
      )
    ).to.exist;
    expect(element.shadowRoot?.querySelector('.budget-track-fill.warning')).to
      .exist;
    expect(element.shadowRoot?.querySelector('.budget-track-fill.danger')).to
      .not.exist;
  });

  it('shows red styling when a hard limit is exceeded', async () => {
    const exceededSummary: AccountGatewayUsageSummaryResponse = {
      ...summary,
      budget: {
        monthly_limit_usd: 100,
        soft_limit_usd: 80,
        current_spend_usd: 105,
        soft_limit_exceeded: true,
        hard_limit_exceeded: true,
      },
    };

    const element = (await fixture(html`
      <budget-health-card
        .summary=${exceededSummary}
        .policies=${policies}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.title.exceeded')).to.exist;
    expect(element.shadowRoot?.querySelector('.row-value.exceeded')).to.exist;
    expect(element.shadowRoot?.querySelector('.budget-track-fill.danger')).to
      .exist;
    expect(
      element.shadowRoot?.querySelector('.limit-status')?.textContent
    ).to.contain('Hard limit exceeded');
  });

  it('dispatches configure when limits button is clicked', async () => {
    const element = (await fixture(html`
      <budget-health-card
        .summary=${summary}
        .policies=${policies}
        .configurable=${true}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    setTimeout(() => {
      element.shadowRoot
        ?.querySelector<HTMLElement>(
          'sl-button[aria-label="Configure budget limits"]'
        )
        ?.click();
    });

    const event = await oneEvent(element, 'configure');
    expect(event).to.exist;
  });
});
