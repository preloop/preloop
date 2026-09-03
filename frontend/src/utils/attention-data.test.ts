import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import { ATTENTION_QUERY, loadAttentionInputs } from './attention-data';

describe('loadAttentionInputs', () => {
  let fetchStub: sinon.SinonStub;
  let failing: string[] = [];
  let dismissalsStatus = 200;

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  const requestedUrls = () =>
    fetchStub.getCalls().map((call) => String(call.args[0]));

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    failing = [];
    dismissalsStatus = 200;
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (failing.some((fragment) => url.includes(fragment))) {
          return new Response('{"detail":"forbidden"}', { status: 403 });
        }
        if (url.startsWith('/api/v1/attention/dismissals')) {
          if (dismissalsStatus !== 200) {
            return new Response('{"detail":"Not Found"}', {
              status: dismissalsStatus,
            });
          }
          return json({
            items: [
              {
                id: 'dismissal-1',
                item_id: 'flow:flow-1',
                fingerprint: 'run:run-9',
                reason: 'expected',
                snooze_until: null,
                dismissed_by_user_id: null,
                dismissed_by_username: 'tester',
                created_at: new Date().toISOString(),
              },
            ],
          });
        }
        if (url.startsWith('/api/v1/approval-requests')) {
          return json([
            { id: 'approval-1', status: 'pending' },
            {
              id: 'approval-expired',
              status: 'pending',
              expires_at: new Date(Date.now() - 3600_000).toISOString(),
            },
          ]);
        }
        if (url.startsWith('/api/v1/agents')) {
          return json({ items: [{ id: 'agent-1' }], total: 1 });
        }
        if (url.startsWith('/api/v1/runtime-sessions')) {
          return json({ items: [{ id: 'session-1' }], total: 1 });
        }
        if (url.startsWith('/api/v1/flows/executions')) {
          return json([{ id: 'execution-1', status: 'FAILED' }]);
        }
        if (url.startsWith('/api/v1/account/gateway-usage/search')) {
          return json({
            items: [
              { id: 'ok', outcome: 'success' },
              { id: 'bad', outcome: 'error' },
            ],
          });
        }
        if (url.startsWith('/api/v1/budget/policies')) {
          return json([{ id: 'policy-1' }]);
        }
        if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
          return json({ total_requests: 3 });
        }
        if (url.startsWith('/api/v1/billing/cost/pricing-overrides')) {
          return json([
            { id: 'override-1', model_alias: 'ox-alpha', is_active: true },
          ]);
        }
        return json({ detail: `Unhandled ${url}` });
      });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('requests every input with the parameters both views share', async () => {
    await loadAttentionInputs();
    const urls = requestedUrls();

    expect(
      urls.some((url) =>
        url.includes(
          `/api/v1/approval-requests?status=pending&limit=${ATTENTION_QUERY.approvalsLimit}`
        )
      ),
      'pending approvals'
    ).to.be.true;
    // 100 is the maximum the agents endpoint accepts; 200 is a 422 that used
    // to empty the Agents section of the Attention page.
    expect(
      urls.some(
        (url) =>
          url.startsWith('/api/v1/agents') &&
          url.includes(`limit=${ATTENTION_QUERY.agentsLimit}`)
      ),
      'agents limit'
    ).to.be.true;
    expect(ATTENTION_QUERY.agentsLimit).to.be.at.most(100);
    expect(
      urls.some(
        (url) =>
          url.startsWith('/api/v1/flows/executions') &&
          url.includes('status=FAILED') &&
          url.includes(`limit=${ATTENTION_QUERY.executionsLimit}`)
      ),
      'failed executions'
    ).to.be.true;
    expect(
      urls.some(
        (url) =>
          url.startsWith('/api/v1/runtime-sessions') &&
          url.includes(`limit=${ATTENTION_QUERY.sessionsLimit}`) &&
          url.includes('start_date')
      ),
      'runtime sessions'
    ).to.be.true;
    expect(
      urls.some((url) =>
        url.startsWith('/api/v1/account/gateway-usage/search')
      ),
      'gateway failures'
    ).to.be.true;
    expect(
      urls.some((url) => url.startsWith('/api/v1/budget/policies')),
      'budget policies'
    ).to.be.true;
    expect(
      urls.some((url) =>
        url.startsWith('/api/v1/account/gateway-usage/summary')
      ),
      'usage summary'
    ).to.be.true;
  });

  it('returns every input and keeps only failed gateway interactions', async () => {
    const inputs = await loadAttentionInputs();

    expect(inputs.approvals).to.have.length(1);
    expect(inputs.agents).to.have.length(1);
    expect(inputs.sessions).to.have.length(1);
    expect(inputs.executions).to.have.length(1);
    expect(inputs.gatewayFailures).to.have.length(1);
    expect(inputs.gatewayFailures?.[0].id).to.equal('bad');
    expect(inputs.budgetPolicies).to.have.length(1);
    expect(inputs.usageSummary?.total_requests).to.equal(3);
  });

  it('hides approvals whose expiry has passed, whatever the API calls them', async () => {
    const inputs = await loadAttentionInputs();

    // The API keeps reporting expired requests as pending (backend issue
    // #335). Nobody can act on one, so neither view may count it.
    expect(inputs.approvals?.map((approval) => approval.id)).to.eql([
      'approval-1',
    ]);
  });

  it('drops only the input whose request fails', async () => {
    failing = ['/api/v1/budget/policies'];
    const inputs = await loadAttentionInputs();

    expect(inputs.budgetPolicies).to.eql([]);
    expect(inputs.approvals).to.have.length(1);
    expect(inputs.agents).to.have.length(1);
  });

  it('asks the summary for the per-model breakdown', async () => {
    await loadAttentionInputs();
    // Without the breakdown the pricing item can only say "336 requests
    // unpriced" and never which models have no price.
    expect(
      requestedUrls().some(
        (url) =>
          url.startsWith('/api/v1/account/gateway-usage/summary') &&
          url.includes('include_breakdown=true')
      )
    ).to.be.true;
  });

  it('loads the active dismissals alongside the rest', async () => {
    const inputs = await loadAttentionInputs();

    expect(inputs.dismissalsSupported).to.be.true;
    expect(inputs.dismissals?.map((dismissal) => dismissal.item_id)).to.eql([
      'flow:flow-1',
    ]);
  });

  // An older server has no such endpoint. Nothing is hidden and the page hides
  // its dismiss controls instead of showing buttons that cannot work.
  it('degrades quietly when the dismissals endpoint 404s', async () => {
    dismissalsStatus = 404;
    const inputs = await loadAttentionInputs();

    expect(inputs.dismissalsSupported).to.be.false;
    expect(inputs.dismissals).to.eql([]);
    expect(inputs.approvals).to.have.length(1);
  });

  it('reads the price overrides, so an override counts as a price', async () => {
    const inputs = await loadAttentionInputs();

    expect(
      requestedUrls().some((url) =>
        url.startsWith('/api/v1/billing/cost/pricing-overrides')
      ),
      'overrides requested'
    ).to.be.true;
    expect(
      inputs.priceOverrides?.map((override) => override.model_alias)
    ).to.eql(['ox-alpha']);
  });

  it('carries on without overrides when the account cannot read them', async () => {
    // A 403 on an account without the feature, or a 404 on an older server.
    failing = ['/api/v1/billing/cost/pricing-overrides'];
    const inputs = await loadAttentionInputs();

    expect(inputs.priceOverrides).to.eql([]);
    expect(inputs.usageSummary?.total_requests).to.equal(3);
  });

  it('skips the budget call when billing is off', async () => {
    await loadAttentionInputs({ includeBudgetPolicies: false });
    expect(
      requestedUrls().some((url) => url.startsWith('/api/v1/budget/policies'))
    ).to.be.false;
  });

  it('skips the usage-summary breakdown when the Overview already fetches it', async () => {
    const inputs = await loadAttentionInputs({ skipUsageSummary: true });
    expect(
      requestedUrls().some((url) =>
        url.startsWith('/api/v1/account/gateway-usage/summary')
      )
    ).to.be.false;
    expect(inputs.usageSummary).to.equal(null);
    expect(inputs.approvals).to.have.length(1);
  });
});
