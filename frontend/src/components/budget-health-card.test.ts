import { html, fixture, expect, oneEvent } from '@open-wc/testing';
import './budget-health-card.ts';
import './usage-card.ts';
import type { UsageCard } from './usage-card';
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

  it('forecasts where the period lands and names rows as the Overview does', async () => {
    // A month that is 60% gone with $120 spent lands at $200: over the soft
    // limit, under the hard one, so the line reads as a warning.
    const now = new Date();
    // Anchor the window to now, not the calendar month: early in a month
    // less than MIN_ELAPSED_FRACTION has elapsed and no forecast renders.
    const day = 24 * 60 * 60 * 1000;
    const start = new Date(now.getTime() - 18 * day);
    const end = new Date(start.getTime() + 30 * day);
    const forecastPolicies = [
      {
        ...policies[0],
        period: 'monthly',
        hard_limit_usd: 300,
        soft_limit_usd: 100,
        current_spend_usd: 120,
        period_start: start.toISOString(),
        period_end: end.toISOString(),
      },
    ] as unknown as BudgetPolicy[];

    const element = (await fixture(html`
      <budget-health-card
        .summary=${summary}
        .policies=${forecastPolicies}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    const forecast = element.shadowRoot?.querySelector('.budget-forecast');
    expect(forecast, 'budget rows forecast the period end').to.exist;
    expect(forecast?.textContent?.replace(/\s+/g, ' ')).to.contain(
      'On track for $'
    );

    // One name per budget: "Monthly budget · Sep", not "Global spend · 30d".
    const label = element.shadowRoot?.querySelector('.row-label');
    const month = now.toLocaleDateString(undefined, { month: 'short' });
    expect(label?.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
      `Monthly budget · ${month}`
    );
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

describe('BudgetHealthCard period-aligned spend', () => {
  const summary = {
    period_start: '2026-07-01T00:00:00Z',
    period_end: '2026-07-31T00:00:00Z',
    total_requests: 10,
    successful_requests: 10,
    failed_requests: 0,
    token_usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    estimated_cost: 91.83,
    budget: {
      monthly_limit_usd: null,
      soft_limit_usd: null,
      current_spend_usd: 91.83,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [],
    usage_by_flow: [],
    usage_by_session: [],
  } as unknown as AccountGatewayUsageSummaryResponse;

  it('prefers the policy current_spend_usd over the summary window', async () => {
    // Regression: a daily and a monthly global policy used to both render
    // the summary-window spend. With server-provided period-aligned spend
    // they must differ.
    const periodPolicies = [
      {
        id: 'daily-policy',
        subject_type: 'global',
        subject_id: 'global',
        model_alias: null,
        period: 'daily',
        hard_limit_usd: 120,
        soft_limit_usd: 80,
        notify_on_soft: false,
        notify_on_hard: false,
        notification_emails: null,
        current_spend_usd: 3.25,
      },
      {
        id: 'monthly-policy',
        subject_type: 'global',
        subject_id: 'global',
        model_alias: null,
        period: 'monthly',
        hard_limit_usd: 300,
        soft_limit_usd: 200,
        notify_on_soft: false,
        notify_on_hard: false,
        notification_emails: null,
        current_spend_usd: 91.83,
      },
    ] as unknown as BudgetPolicy[];

    const element = (await fixture(html`
      <budget-health-card
        .summary=${summary}
        .policies=${periodPolicies}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    const text = element.shadowRoot?.textContent || '';
    expect(text).to.include('$3.25');
    expect(text).to.include('$91.83');
  });
  for (const scenario of [
    { range: 'month', windowSpend: 150, policySpend: 40 },
    { range: 'month', windowSpend: 20, policySpend: 125 },
    { range: 'year', windowSpend: 500, policySpend: 0 },
    { range: 'day', windowSpend: 5, policySpend: 120 },
  ]) {
    it(`agrees with Overview for ${scenario.range} analytics ($${scenario.windowSpend}) and monthly spend ($${scenario.policySpend})`, async () => {
      const windowSummary = {
        ...summary,
        estimated_cost: scenario.windowSpend,
        budget: {
          ...summary.budget!,
          current_spend_usd: scenario.windowSpend,
          hard_limit_exceeded: scenario.windowSpend > 100,
        },
      };
      const periodPolicies = [
        {
          id: 'monthly-policy',
          subject_type: 'account',
          subject_id: null,
          model_alias: null,
          period: 'monthly',
          hard_limit_usd: 100,
          soft_limit_usd: 80,
          notify_on_soft: false,
          notify_on_hard: false,
          notification_emails: [],
          current_spend_usd: scenario.policySpend,
          period_start: '2026-03-01T00:00:00Z',
          period_end: '2026-04-01T00:00:00Z',
        },
      ];
      const overview = await fixture<UsageCard>(html`
        <usage-card
          .summary=${windowSummary}
          .policies=${periodPolicies}
          .timeRange=${scenario.range}
        ></usage-card>
      `);
      // Cost's monthly budget remains independent of its analytics selector.
      const cost = await fixture<BudgetHealthCard>(html`
        <budget-health-card
          .summary=${windowSummary}
          .policies=${periodPolicies}
          .timeRange=${'month'}
        ></budget-health-card>
      `);
      await Promise.all([overview.updateComplete, cost.updateComplete]);
      const shownSpend = `$${scenario.policySpend.toFixed(2)}`;
      expect(
        overview.shadowRoot!.querySelector('.budget-row-value')!.textContent
      ).to.include(shownSpend);
      expect(
        cost.shadowRoot!.querySelector('.row-value')!.textContent
      ).to.include(shownSpend);
      expect(
        Boolean(cost.shadowRoot!.querySelector('.title.exceeded'))
      ).to.equal(scenario.policySpend >= 100);
      expect(
        Boolean(cost.shadowRoot!.querySelector('.row-value.exceeded'))
      ).to.equal(scenario.policySpend >= 100);
    });
  }

  it('keeps a soft-only account policy independent of legacy hard limits', async () => {
    const element = await fixture<BudgetHealthCard>(html`
      <budget-health-card
        .summary=${{
          ...summary,
          budget: {
            ...summary.budget!,
            monthly_limit_usd: 10,
            hard_limit_exceeded: true,
          },
        }}
        .policies=${[
          {
            id: 'soft-only',
            subject_type: 'account',
            subject_id: null,
            model_alias: null,
            period: 'monthly',
            hard_limit_usd: 0,
            soft_limit_usd: 100,
            current_spend_usd: 40,
            notify_on_soft: false,
            notify_on_hard: false,
            notification_emails: [],
          },
        ]}
      ></budget-health-card>
    `);
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector('.row-value')!.textContent
    ).to.include('/ $100.00');
    expect(element.shadowRoot!.querySelector('.title.exceeded')).to.not.exist;
  });

  it('still warns for an exceeded secondary daily policy', async () => {
    const element = await fixture<BudgetHealthCard>(html`
      <budget-health-card
        .summary=${summary}
        .policies=${[
          {
            id: 'monthly',
            subject_type: 'account',
            period: 'monthly',
            hard_limit_usd: 100,
            current_spend_usd: 40,
          },
          {
            id: 'daily',
            subject_type: 'account',
            period: 'daily',
            hard_limit_usd: 10,
            current_spend_usd: 12,
          },
        ]}
      ></budget-health-card>
    `);
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('.title.exceeded')).to.exist;
    const rows = element.shadowRoot!.querySelectorAll('.row-value');
    expect(rows[0].classList.contains('exceeded')).to.equal(false);
    expect(rows[1].classList.contains('exceeded')).to.equal(true);
  });

  it('names the team on a team budget row', async () => {
    const teamPolicy: BudgetPolicy = {
      model_alias: null,
      period: 'monthly',
      hard_limit_usd: 10,
      soft_limit_usd: null,
      notify_on_soft: false,
      notify_on_hard: false,
      id: 'policy-team',
      subject_type: 'team',
      subject_id: 'team-1',
      current_spend_usd: 4,
    } as BudgetPolicy;
    const element = (await fixture(html`
      <budget-health-card
        .summary=${summary}
        .policies=${[teamPolicy]}
        .teamNames=${{ 'team-1': 'Platform' }}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('Team Platform');
  });

  it('labels a team row without a known name as Team', async () => {
    const teamPolicy: BudgetPolicy = {
      model_alias: null,
      period: 'monthly',
      hard_limit_usd: 10,
      soft_limit_usd: null,
      notify_on_soft: false,
      notify_on_hard: false,
      id: 'policy-team',
      subject_type: 'team',
      subject_id: 'team-2',
    } as BudgetPolicy;
    const element = (await fixture(html`
      <budget-health-card
        .summary=${summary}
        .policies=${[teamPolicy]}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;
    const text = element.shadowRoot?.textContent || '';
    expect(text).to.contain('Team');
    expect(text).to.not.contain('Team Platform');
  });

  it('reads a budget meter as dollars, with thousands separators', async () => {
    const bigPolicies = [
      {
        id: 'big-policy',
        subject_type: 'global',
        subject_id: 'global',
        model_alias: null,
        period: 'monthly',
        hard_limit_usd: 25000,
        soft_limit_usd: null,
        notify_on_soft: false,
        notify_on_hard: false,
        notification_emails: null,
        current_spend_usd: 12345.678,
      },
    ] as unknown as BudgetPolicy[];
    const element = (await fixture(html`
      <budget-health-card
        .summary=${{
          ...summary,
          budget: { ...summary.budget!, current_spend_usd: 12345.678 },
        }}
        .policies=${bigPolicies}
      ></budget-health-card>
    `)) as BudgetHealthCard;
    await element.updateComplete;

    const meter = element.shadowRoot!.querySelector('[role="progressbar"]')!;
    expect(meter.getAttribute('aria-valuetext')).to.equal(
      '$12,345.68 of $25,000.00'
    );
    const value = element.shadowRoot!.querySelector('.row-value')!;
    expect(value.textContent).to.contain('$12,345.68');
    expect(value.getAttribute('title')).to.equal('$12,345.678');
  });
});
