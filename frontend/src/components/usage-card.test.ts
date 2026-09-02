import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import './usage-card.ts';
import { USAGE_UNIT_STORAGE_KEY } from './usage-card';
import type { UsageCard } from './usage-card';
import type { BudgetPolicy } from '../api';
import type { AccountGatewayUsageSummaryResponse } from '../types';

function summaryFixture(
  overrides: Partial<AccountGatewayUsageSummaryResponse> = {}
): AccountGatewayUsageSummaryResponse {
  return {
    period_start: '2026-08-03T00:00:00Z',
    period_end: '2026-09-02T00:00:00Z',
    total_requests: 19500000,
    successful_requests: 19400000,
    failed_requests: 100000,
    token_usage: {
      prompt_tokens: 412100000,
      completion_tokens: 172200000,
      total_tokens: 584300000,
    },
    estimated_cost: 33.57,
    budget: {
      monthly_limit_usd: null,
      soft_limit_usd: null,
      current_spend_usd: 33.57,
      soft_limit_exceeded: false,
      hard_limit_exceeded: false,
    },
    requests_by_day: [],
    usage_by_model: [],
    usage_by_flow: [],
    usage_by_session: [],
    ...overrides,
  } as AccountGatewayUsageSummaryResponse;
}

function policyFixture(overrides: Partial<BudgetPolicy> = {}): BudgetPolicy {
  return {
    id: 'policy-1',
    subject_type: 'global',
    subject_id: null,
    model_alias: null,
    period: 'monthly',
    hard_limit_usd: 300,
    soft_limit_usd: 200,
    notify_on_soft: true,
    notify_on_hard: true,
    notification_user_ids: null,
    notification_team_ids: null,
    notification_emails: null,
    current_spend_usd: 33.57,
    ...overrides,
  };
}

function daysFixture(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    date: `2026-08-${String(index + 1).padStart(2, '0')}`,
    request_count: 100 + index,
    estimated_cost: 1 + index,
    total_tokens: 1000 + index * 10,
  }));
}

async function renderCard(props: Partial<UsageCard> = {}): Promise<UsageCard> {
  const element = await fixture<UsageCard>(html`
    <usage-card
      .summary=${'summary' in props ? props.summary : summaryFixture()}
      .policies=${props.policies ?? []}
      .timeRange=${props.timeRange ?? 'month'}
      .toolCallsCount=${props.toolCallsCount ?? 0}
      .loading=${props.loading ?? false}
      .error=${props.error ?? null}
    ></usage-card>
  `);
  await element.updateComplete;
  return element;
}

function text(element: UsageCard, selector: string): string {
  return (
    element
      .shadowRoot!.querySelector(selector)
      ?.textContent?.replace(/\s+/g, ' ') || ''
  ).trim();
}

describe('usage-card', () => {
  beforeEach(() => {
    localStorage.removeItem(USAGE_UNIT_STORAGE_KEY);
  });

  afterEach(() => {
    localStorage.removeItem(USAGE_UNIT_STORAGE_KEY);
  });

  it('leads with tokens for the selected range', async () => {
    const element = await renderCard();
    expect(text(element, '.primary-value')).to.equal('584.3M');
    expect(text(element, '.primary-label')).to.contain('tokens · 30d');
    expect(text(element, '.secondary-line')).to.equal(
      '412.1M prompt · 172.2M completion · 19.5M requests'
    );
  });

  it('appends tool calls to the secondary line when present', async () => {
    const element = await renderCard({ toolCallsCount: 4200 });
    expect(text(element, '.secondary-line')).to.contain('4.2K tool calls');
  });

  it('switches to estimated dollars and persists the choice', async () => {
    const element = await renderCard();
    const group = element.shadowRoot!.querySelector('sl-radio-group')!;
    (group as unknown as { value: string }).value = 'dollars';
    group.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;

    expect(text(element, '.primary-value')).to.equal('$33.57');
    expect(text(element, '.primary-label')).to.contain('est. spend · 30d');
    expect(text(element, '.secondary-line')).to.equal(
      '584.3M tokens · 19.5M requests'
    );
    expect(localStorage.getItem(USAGE_UNIT_STORAGE_KEY)).to.equal('dollars');

    const restored = await renderCard();
    expect(text(restored, '.primary-value')).to.equal('$33.57');
  });

  it('hides the sparkline under three points and shows it above', async () => {
    const sparse = await renderCard({
      summary: summaryFixture({ requests_by_day: daysFixture(2) }),
    });
    expect(sparse.shadowRoot!.querySelector('.sparkline')).to.be.null;

    const dense = await renderCard({
      summary: summaryFixture({ requests_by_day: daysFixture(7) }),
    });
    const sparkline = dense.shadowRoot!.querySelector('.sparkline');
    expect(sparkline).to.exist;
    expect(
      sparkline!.querySelector('polyline')!.getAttribute('points')!.split(' ')
    ).to.have.lengthOf(7);
  });

  it('shows a budget row per global policy with the spend meter', async () => {
    const element = await renderCard({
      policies: [
        policyFixture({ id: 'p-month', period: 'monthly' }),
        policyFixture({
          id: 'p-day',
          period: 'daily',
          hard_limit_usd: 20,
          soft_limit_usd: 10,
          current_spend_usd: 12,
        }),
      ],
    });
    const rows = element.shadowRoot!.querySelectorAll('.budget-row');
    expect(rows.length).to.equal(2);
    // Each row says which window it resets in, so the number is not read as
    // a running total.
    expect(rows[0].textContent).to.contain('Daily budget · today');
    const month = new Date().toLocaleDateString(undefined, { month: 'short' });
    expect(rows[1].textContent).to.contain(`Monthly budget · ${month}`);
    expect(rows[1].textContent!.replace(/\s+/g, ' ')).to.contain(
      '$33.57 / $300.00'
    );
    expect(
      element.shadowRoot!.querySelectorAll('.budget-track').length
    ).to.equal(2);
  });

  it('renders the legacy account budget as a monthly row', async () => {
    const element = await renderCard({
      summary: summaryFixture({
        budget: {
          monthly_limit_usd: 500,
          soft_limit_usd: 400,
          current_spend_usd: 33.57,
          soft_limit_exceeded: false,
          hard_limit_exceeded: false,
        },
      }),
    });
    const row = element.shadowRoot!.querySelector('.budget-row')!;
    expect(row.textContent).to.contain('Monthly budget');
    expect(row.textContent!.replace(/\s+/g, ' ')).to.contain(
      '$33.57 / $500.00'
    );
  });

  it('puts the unit toggle in the header, left of the range select', async () => {
    const element = await renderCard({});
    const header = element.shadowRoot!.querySelector('.header')!;
    const controls = header.querySelector('.header-controls')!;
    const children = Array.from(controls.children).map((child) =>
      child.tagName.toLowerCase()
    );
    expect(children[0]).to.equal('div');
    expect(controls.querySelector('.unit-toggle')).to.exist;
    expect(children[children.length - 1]).to.equal('time-range-select');

    // The label stays for screen readers but takes no room on screen.
    const group = controls.querySelector('sl-radio-group')!;
    expect(group.getAttribute('label')).to.equal('Usage unit');
    const label = group.shadowRoot?.querySelector(
      '[part~="form-control-label"]'
    ) as HTMLElement | null;
    if (label) {
      expect(label.getBoundingClientRect().height).to.be.at.most(1);
    }
  });

  it('summarises non global policies on one line', async () => {
    const element = await renderCard({
      policies: [
        policyFixture(),
        policyFixture({
          id: 'p-agent',
          subject_type: 'managed_agent',
          subject_id: 'agent-1',
        }),
        policyFixture({
          id: 'p-model',
          subject_type: 'ai_model',
          subject_id: 'model-1',
        }),
      ],
    });
    const more = element.shadowRoot!.querySelector('.more-limits')!;
    expect(more.textContent!.replace(/\s+/g, ' ')).to.contain(
      '+ 2 more limits (agents, models)'
    );
  });

  it('tells the operator when there is no budget at all', async () => {
    const element = await renderCard();
    expect(text(element, '.muted')).to.equal('No budget set.');
    expect(element.shadowRoot!.querySelector('.budget-row')).to.be.null;
  });

  it('emits configure-limits from the footer button', async () => {
    const element = await renderCard();
    const button = element.shadowRoot!.querySelector(
      '.footer sl-button'
    ) as HTMLElement;
    setTimeout(() => button.click());
    await oneEvent(element, 'configure-limits');
  });

  it('forwards the range change from the header select', async () => {
    const element = await renderCard();
    const select = element.shadowRoot!.querySelector('time-range-select')!;
    setTimeout(() =>
      select.dispatchEvent(
        new CustomEvent('range-change', {
          detail: { value: 'week' },
          bubbles: true,
          composed: true,
        })
      )
    );
    const event = (await oneEvent(element, 'range-change')) as CustomEvent<{
      value: string;
    }>;
    expect(event.detail.value).to.equal('week');
  });

  it('shows skeletons while loading and an inline alert on error', async () => {
    const loading = await renderCard({ loading: true, summary: null });
    expect(
      loading.shadowRoot!.querySelectorAll('sl-skeleton').length
    ).to.be.at.least(2);

    const failed = await renderCard({ error: 'Failed to load usage' });
    const alert = failed.shadowRoot!.querySelector('sl-alert')!;
    expect(alert.getAttribute('variant')).to.equal('danger');
    expect(alert.textContent).to.contain('Failed to load usage');
  });
});
