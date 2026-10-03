import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './team-budgets-panel.ts';
import type { TeamBudgetsPanel } from './team-budgets-panel';
import { invalidateApiCaches } from '../api';

describe('TeamBudgetsPanel', () => {
  let fetchStub: sinon.SinonStub;
  let budgets: unknown[];
  const writes: { method: string; url: string; body: unknown }[] = [];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    budgets = [
      {
        id: 'b1',
        subject_type: 'team',
        team_id: 't1',
        team_name: 'Platform',
        period: 'monthly',
        hard_limit_usd: 50,
        soft_limit_usd: null,
        notify_on_soft: false,
        notify_on_hard: false,
        model_alias: null,
        current_spend_usd: 12.5,
      },
    ];
    writes.length = 0;
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/auth/users/me') || url.includes('/users/me')) {
          return new Response(JSON.stringify({ account_id: 'acc-1' }));
        }
        if (url.includes('/usage/teams')) {
          return new Response(
            JSON.stringify({
              rows: [
                {
                  team_id: 't1',
                  team_name: 'Platform',
                  member_count: 2,
                  cost_usd: 30,
                },
                {
                  team_id: 't2',
                  team_name: 'Security',
                  member_count: 1,
                  cost_usd: 20,
                },
              ],
            })
          );
        }
        if (url.includes('/team-budgets')) {
          if (method !== 'GET') {
            writes.push({
              method,
              url,
              body: init?.body ? JSON.parse(String(init.body)) : null,
            });
            if (method === 'DELETE') {
              budgets = [];
              return new Response(null, { status: 204 });
            }
            return new Response(JSON.stringify(budgets[0]), { status: 201 });
          }
          return new Response(JSON.stringify({ items: budgets }));
        }
        return new Response('{}');
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('lists spend per team and the team budgets', async () => {
    const el = (await fixture(
      html`<team-budgets-panel
        .startDate=${'2026-10-01T00:00:00Z'}
        .endDate=${'2026-10-03T00:00:00Z'}
      ></team-budgets-panel>`
    )) as TeamBudgetsPanel;
    await waitUntil(() =>
      el.shadowRoot?.querySelector('table[aria-label="Spend per team"]')
    );
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('Platform');
    expect(text).to.contain('Security');
    expect(text).to.contain('counts in each of them');
    const budgetRows = el.shadowRoot!.querySelectorAll(
      'table[aria-label="Team budgets"] tbody tr'
    );
    expect(budgetRows.length).to.equal(1);
    expect(
      fetchStub
        .getCalls()
        .some((call) =>
          String(call.args[0]).includes(
            'usage/teams?start=2026-10-01T00%3A00%3A00Z'
          )
        )
    ).to.equal(true);
  });

  it('adds and removes a team budget', async () => {
    const el = (await fixture(
      html`<team-budgets-panel
        .startDate=${'2026-10-01T00:00:00Z'}
      ></team-budgets-panel>`
    )) as TeamBudgetsPanel;
    await waitUntil(() =>
      el.shadowRoot?.querySelector('table[aria-label="Team budgets"]')
    );
    const changed = sinon.spy();
    el.addEventListener('team-budgets-changed', changed);
    (el as unknown as { formLimit: string }).formLimit = '25';
    await (el as unknown as { addBudget: () => Promise<void> }).addBudget();
    expect(writes[0].method).to.equal('POST');
    expect(writes[0].body).to.deep.equal({
      team_id: 't1',
      period: 'monthly',
      hard_limit_usd: 25,
    });
    await (
      el as unknown as { removeBudget: (b: unknown) => Promise<void> }
    ).removeBudget(budgets[0]);
    expect(writes[1].method).to.equal('DELETE');
    expect(writes[1].url).to.contain('/team-budgets/b1');
    expect(changed.callCount).to.equal(2);
  });

  it('refuses a negative limit without calling the server', async () => {
    const el = (await fixture(
      html`<team-budgets-panel></team-budgets-panel>`
    )) as TeamBudgetsPanel;
    await waitUntil(() =>
      el.shadowRoot?.querySelector('table[aria-label="Team budgets"]')
    );
    (el as unknown as { formLimit: string }).formLimit = '-1';
    await (el as unknown as { addBudget: () => Promise<void> }).addBudget();
    expect(writes).to.deep.equal([]);
  });
});
