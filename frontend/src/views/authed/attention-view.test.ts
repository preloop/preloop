import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import './attention-view';
import type { AttentionView } from './attention-view';

describe('AttentionView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let approvalsResponse: any[];
  let executionsResponse: any[];
  let policiesResponse: any[];
  let rejectPolicies = false;

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    rejectPolicies = false;

    approvalsResponse = [
      {
        id: 'approval-1',
        tool_name: 'refund_order',
        status: 'pending',
        requested_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ];
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        end_time: new Date(Date.now() - 3_500_000).toISOString(),
      },
    ];
    policiesResponse = [
      {
        id: 'budget-global',
        subject_type: 'global',
        subject_id: null,
        model_alias: null,
        period: 'monthly',
        hard_limit_usd: 50,
        soft_limit_usd: 40,
        current_spend_usd: 55,
      },
    ];

    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (url.startsWith('/api/v1/approval-requests')) {
          return json(approvalsResponse);
        }
        if (url.startsWith('/api/v1/flows/executions')) {
          return json(executionsResponse);
        }
        if (url.startsWith('/api/v1/budget/policies')) {
          return rejectPolicies
            ? new Response('{"detail":"forbidden"}', { status: 403 })
            : json(policiesResponse);
        }
        if (url.startsWith('/api/v1/agents')) {
          return json({ items: [], total: 0 });
        }
        if (url.startsWith('/api/v1/runtime-sessions')) {
          return json({ items: [], total: 0 });
        }
        if (url.startsWith('/api/v1/account/gateway-usage/search')) {
          return json({ items: [] });
        }
        if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
          return json({
            total_requests: 0,
            successful_requests: 0,
            failed_requests: 0,
            token_usage: {
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
            },
            estimated_cost: 0,
            requests_by_day: [],
            usage_by_model: [],
            usage_by_flow: [],
            usage_by_session: [],
          });
        }
        if (url === '/api/v1/features') {
          return json({ features: { billing: true } });
        }
        return json({ detail: `Unhandled ${url}` });
      });

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  const mount = async (): Promise<AttentionView> => {
    const el = (await fixture(
      html`<attention-view></attention-view>`
    )) as AttentionView;
    await waitUntil(
      () => !el.shadowRoot!.querySelector('sl-spinner'),
      'attention view finished loading'
    );
    await el.updateComplete;
    return el;
  };

  const text = (el: AttentionView) =>
    el.shadowRoot!.textContent!.replace(/\s+/g, ' ');

  it('groups items into sections with counts and actions', async () => {
    const el = await mount();

    expect(
      el.shadowRoot!.querySelector('view-header')!.getAttribute('headerText')
    ).to.equal('Needs attention');
    const sections = Array.from(
      el.shadowRoot!.querySelectorAll('sl-card[id]')
    ).map((card) => card.id);
    expect(sections).to.eql(['approvals', 'flows', 'budgets']);

    const approvals = el.shadowRoot!.querySelector('#approvals')!;
    expect(approvals.textContent).to.contain('refund_order');
    expect(approvals.querySelector('sl-badge')!.textContent!.trim()).to.equal(
      '1'
    );
    expect(approvals.querySelector('sl-button')!.getAttribute('href')).to.equal(
      '/console/approval/approval-1'
    );

    const flows = el.shadowRoot!.querySelector('#flows')!;
    expect(flows.textContent).to.contain('Refund Assistant');

    const budgets = el.shadowRoot!.querySelector('#budgets')!;
    expect(budgets.textContent).to.contain('Hard limit reached');
    expect(
      budgets.querySelector('.severity-dot')!.classList.contains('critical')
    ).to.be.true;
  });

  it('shows a chip per kind, muted when the kind is empty', async () => {
    const el = await mount();
    const chips = Array.from(el.shadowRoot!.querySelectorAll('.chip')).map(
      (chip) => chip.textContent!.trim()
    );
    expect(chips).to.eql([
      '1 approval',
      '0 agents',
      '1 flow',
      '0 models',
      '1 budget',
      '0 pricing',
    ]);
    const agentsChip = el.shadowRoot!.querySelectorAll('.chip')[1];
    expect(agentsChip.classList.contains('empty')).to.be.true;
  });

  it('opens the limits dialog from a budget row', async () => {
    const el = await mount();
    const configure = Array.from(
      el.shadowRoot!.querySelectorAll('#budgets sl-button')
    ).find((button) => button.textContent!.includes('Configure limits'))!;
    expect(configure).to.exist;

    (configure as HTMLElement).click();
    await el.updateComplete;

    expect(
      el.shadowRoot!.querySelector('budget-limits-dialog')!.hasAttribute('open')
    ).to.be.true;
  });

  it('drops a section when its request fails and keeps the rest', async () => {
    rejectPolicies = true;
    const el = await mount();

    expect(el.shadowRoot!.querySelector('#budgets')).to.not.exist;
    expect(el.shadowRoot!.querySelector('#approvals')).to.exist;
  });

  it('shows an all-clear card when nothing needs attention', async () => {
    approvalsResponse = [];
    executionsResponse = [];
    policiesResponse = [];

    const el = await mount();
    expect(text(el)).to.contain('Nothing needs you right now.');
    expect(el.shadowRoot!.querySelector('sl-card[id]')).to.not.exist;
  });
});
