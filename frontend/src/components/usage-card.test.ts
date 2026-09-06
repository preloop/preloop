import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import sinon from 'sinon';
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
      input_tokens: 412100000,
      output_tokens: 172200000,
      cache_read_tokens: 300000000,
      cache_write_tokens: 2000000,
      uncached_input_tokens: 110100000,
      cache_hit_ratio: 0.7315,
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
      ?updating=${props.updating ?? false}
      .error=${props.error ?? null}
      .priorSummary=${props.priorSummary ?? null}
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
    // The split lives in <token-figures>, so the line around it carries the
    // requests and the figures carry in, out and the cache.
    expect(text(element, '.secondary-line')).to.contain('19.5M requests');

    const figures = element.shadowRoot!.querySelector('token-figures')!;
    await (figures as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const split = (figures.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(split).to.contain('412.1M in');
    expect(split).to.contain('172.2M out');
    expect(split).to.contain('300M hit');
    expect(split).to.contain('110.1M miss');
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

  it('projects where the period lands and colours it by the limit crossed', async () => {
    // Half the window gone with $60 spent lands on $120: under the $200 soft
    // limit, so the line is quiet.
    const halfWay = {
      period_start: new Date(Date.now() - 6 * 86400000).toISOString(),
      period_end: new Date(Date.now() + 6 * 86400000).toISOString(),
      current_spend_usd: 60,
    };
    const element = await renderCard({
      policies: [policyFixture(halfWay)],
    });
    const forecast = element.shadowRoot!.querySelector('.budget-forecast')!;
    expect(forecast.textContent!.replace(/\s+/g, ' ').trim()).to.contain(
      'On track for $120.00 by'
    );
    expect(forecast.classList.contains('warning')).to.be.false;
    expect(forecast.classList.contains('danger')).to.be.false;
    // The operator can check the arithmetic without leaving the card.
    expect(forecast.getAttribute('title')).to.contain(
      '$60.00 spent in the first 50%'
    );

    const amber = await renderCard({
      policies: [
        policyFixture({ ...halfWay, soft_limit_usd: 100, hard_limit_usd: 300 }),
      ],
    });
    expect(
      amber
        .shadowRoot!.querySelector('.budget-forecast')!
        .classList.contains('warning')
    ).to.be.true;

    const red = await renderCard({
      policies: [
        policyFixture({ ...halfWay, soft_limit_usd: 100, hard_limit_usd: 110 }),
      ],
    });
    expect(
      red
        .shadowRoot!.querySelector('.budget-forecast')!
        .classList.contains('danger')
    ).to.be.true;
  });

  it('stays quiet until a tenth of the period has passed', async () => {
    const element = await renderCard({
      policies: [
        policyFixture({
          period_start: new Date(Date.now() - 3600000).toISOString(),
          period_end: new Date(Date.now() + 99 * 3600000).toISOString(),
          current_spend_usd: 60,
        }),
      ],
    });
    expect(element.shadowRoot!.querySelector('.budget-forecast')).to.be.null;
  });

  it("names the end of a daily budget on the reader's clock", async () => {
    // The server cuts days in UTC, so the window ends at a UTC midnight: that
    // is midnight for a reader in UTC and some other hour of their day
    // everywhere else. Either way the sentence never names tomorrow's date.
    const end = new Date();
    end.setUTCHours(24, 0, 0, 0);
    const expected =
      end.getHours() === 0 && end.getMinutes() === 0 && end.getSeconds() === 0
        ? 'midnight'
        : end.toLocaleTimeString(undefined, {
            hour: 'numeric',
            minute: '2-digit',
          });
    const element = await renderCard({
      policies: [
        policyFixture({
          period: 'daily',
          period_start: new Date(Date.now() - 24 * 3600000).toISOString(),
          period_end: end.toISOString(),
          current_spend_usd: 6,
        }),
      ],
    });
    expect(
      element
        .shadowRoot!.querySelector('.budget-forecast')!
        .textContent!.replace(/\s+/g, ' ')
    ).to.contain(`by ${expected}`);
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
  describe('while a new range loads', () => {
    it('keeps the numbers it has and marks them as being replaced', async () => {
      const element = await renderCard({ updating: true });

      // The old range is still readable: a blank card would be a worse
      // answer than a slightly stale one.
      expect(text(element, '.primary-value')).to.equal('584.3M');
      expect(element.shadowRoot!.querySelector('sl-skeleton')).to.not.exist;
      expect(
        element.shadowRoot!.querySelector('[data-testid="usage-updating"]')
      ).to.exist;

      const body = element.shadowRoot!.querySelector('.body') as HTMLElement;
      expect(body.classList.contains('is-updating')).to.equal(true);
      expect(
        getComputedStyle(body.querySelector('.primary-value')!).opacity
      ).to.equal('0.6');

      element.updating = false;
      await element.updateComplete;
      expect(
        element.shadowRoot!.querySelector('[data-testid="usage-updating"]')
      ).to.not.exist;
      expect(
        getComputedStyle(element.shadowRoot!.querySelector('.primary-value')!)
          .opacity
      ).to.equal('1');
    });

    it('says the total came off a rollup only when the server says so', async () => {
      const plain = await renderCard();
      expect(
        plain.shadowRoot!.querySelector('[data-testid="usage-provenance"]')
      ).to.not.exist;

      const rolled = await renderCard({
        summary: summaryFixture({ from_rollup: true }),
      });
      expect(text(rolled, '[data-testid="usage-provenance"]')).to.equal(
        '· from rollup'
      );

      // Some servers name the source instead of flagging it.
      const named = await renderCard({
        summary: summaryFixture({ source: 'rollup' }),
      });
      expect(
        named.shadowRoot!.querySelector('[data-testid="usage-provenance"]')
      ).to.exist;
    });

    it('warns about a slow year only after it has been slow', async () => {
      const clock = sinon.useFakeTimers();
      try {
        const element = await renderCard({ timeRange: 'year', updating: true });
        const hint = () =>
          element.shadowRoot!.querySelector('[data-testid="long-range-hint"]');
        expect(hint(), 'immediately').to.not.exist;

        clock.tick(1999);
        await element.updateComplete;
        expect(hint(), 'just under two seconds').to.not.exist;

        clock.tick(2);
        await element.updateComplete;
        expect((hint()!.textContent || '').trim()).to.equal(
          'Long ranges take longer'
        );

        // Gone the moment the year lands, and never shown for a short range.
        element.updating = false;
        await element.updateComplete;
        expect(hint()).to.not.exist;

        const month = await renderCard({ timeRange: 'month', updating: true });
        clock.tick(5000);
        await month.updateComplete;
        expect(
          month.shadowRoot!.querySelector('[data-testid="long-range-hint"]')
        ).to.not.exist;
      } finally {
        clock.restore();
      }
    });
  });

  describe('prior-period delta', () => {
    it('states the change against the previous window of the same length', async () => {
      const element = await renderCard({
        priorSummary: summaryFixture({
          token_usage: {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 500000000,
          },
        }),
      });

      // 584.3M against 500M is up 17%.
      expect(text(element, '.delta')).to.equal('▲ 17% vs prior 30d');
    });

    it('points down when usage falls, and stays colour-free either way', async () => {
      const element = await renderCard({
        priorSummary: summaryFixture({
          token_usage: {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 1000000000,
          },
        }),
      });

      expect(text(element, '.delta')).to.contain('▼');

      // Colour-free: the delta is meta text, not a verdict. The sentinel
      // stands in for the theme sheet, which the test page does not load.
      document.documentElement.style.setProperty(
        '--sl-color-neutral-500',
        'rgb(7, 8, 9)'
      );
      try {
        const delta = element.shadowRoot!.querySelector(
          '.delta'
        ) as HTMLElement;
        expect(getComputedStyle(delta).color).to.equal('rgb(7, 8, 9)');
      } finally {
        document.documentElement.style.removeProperty('--sl-color-neutral-500');
      }

      // The unit toggle switches what the delta compares.
      const dollars = await renderCard({
        priorSummary: summaryFixture({ estimated_cost: 16.78 }),
      });
      dollars['unit'] = 'dollars';
      await dollars.updateComplete;
      expect(text(dollars, '.delta')).to.equal('▲ 100% vs prior 30d');
    });

    it('says nothing when there is no prior period to compare against', async () => {
      const none = await renderCard();
      expect(none.shadowRoot!.querySelector('.delta')).to.not.exist;

      const zero = await renderCard({
        priorSummary: summaryFixture({
          token_usage: {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0,
          },
        }),
      });
      expect(zero.shadowRoot!.querySelector('.delta')).to.not.exist;
    });
  });
});
