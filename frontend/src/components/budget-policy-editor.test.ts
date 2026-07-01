import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './budget-policy-editor.ts';
import type { BudgetPolicyEditor } from './budget-policy-editor';

describe('BudgetPolicyEditor', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  const stubBillingFetch = (opts?: { failModels?: boolean }) => {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/api/v1/features')) {
          return new Response(JSON.stringify({ features: { billing: true } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/auth/users/me')) {
          return new Response(JSON.stringify({ email: 'owner@example.com' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/users?')) {
          return new Response(
            JSON.stringify({ users: [], total: 0, skip: 0, limit: 100 }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/ai-models') && opts?.failModels) {
          return new Response('models unavailable', { status: 500 });
        }
        if (url.includes('/api/v1/ai-models')) {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/agents')) {
          return new Response(
            JSON.stringify({
              query: null,
              agent_kind: null,
              last_seen_after: null,
              status: 'all',
              total: 0,
              limit: 100,
              offset: 0,
              items: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/teams')) {
          return new Response(JSON.stringify({ teams: [], total: 0 }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/budget/policies') && method === 'GET') {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('{}', { status: 200 });
      }
    );
  };

  it('renders budget policy region when billing is enabled', async () => {
    stubBillingFetch();
    const element = (await fixture(
      html`<budget-policy-editor billingEnabled></budget-policy-editor>`
    )) as BudgetPolicyEditor;
    await waitUntil(() =>
      Boolean(element.shadowRoot?.querySelector('#budget-policy-editor-title'))
    );

    const title = element.shadowRoot?.querySelector(
      '#budget-policy-editor-title'
    );
    expect(title?.textContent?.trim()).to.equal('Budget Policies');
    expect(element.shadowRoot?.querySelector('[role="region"]')).to.exist;
  });

  it('surfaces subject load failures instead of failing silently', async () => {
    stubBillingFetch({ failModels: true });
    const element = (await fixture(
      html`<budget-policy-editor billingEnabled></budget-policy-editor>`
    )) as BudgetPolicyEditor;
    await waitUntil(() =>
      Boolean(element.shadowRoot?.querySelector('#budget-policy-editor-title'))
    );

    await element.loadSubjects();
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('sl-alert[role="alert"]')).to
      .exist;
  });

  it('loads an existing policy into the edit form and updates it', async () => {
    const existingPolicy = {
      id: 'policy-1',
      subject_type: 'global',
      subject_id: 'global',
      model_alias: null,
      period: 'monthly',
      hard_limit_usd: 100,
      soft_limit_usd: 80,
      notify_on_soft: true,
      notify_on_hard: false,
      notification_user_ids: ['owner-user-id'],
      notification_team_ids: [],
      notification_emails: ['owner@example.com'],
    };

    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/api/v1/features')) {
          return new Response(JSON.stringify({ features: { billing: true } }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/auth/users/me')) {
          return new Response(JSON.stringify({ email: 'owner@example.com' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/users?')) {
          return new Response(
            JSON.stringify({ users: [], total: 0, skip: 0, limit: 100 }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/ai-models')) {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/agents')) {
          return new Response(
            JSON.stringify({
              query: null,
              agent_kind: null,
              last_seen_after: null,
              status: 'all',
              total: 0,
              limit: 100,
              offset: 0,
              items: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/teams')) {
          return new Response(JSON.stringify({ teams: [], total: 0 }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/v1/budget/policies') && method === 'GET') {
          return new Response(JSON.stringify([existingPolicy]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (
          url.includes('/api/v1/budget/policies/policy-1') &&
          method === 'PUT'
        ) {
          return new Response(
            JSON.stringify({
              ...existingPolicy,
              hard_limit_usd: 150,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response('{}', { status: 200 });
      }
    );

    const element = (await fixture(
      html`<budget-policy-editor billingEnabled></budget-policy-editor>`
    )) as BudgetPolicyEditor;
    await waitUntil(() =>
      Boolean(
        element.shadowRoot?.querySelector('sl-icon-button[name="pencil"]')
      )
    );

    element.shadowRoot
      ?.querySelector('sl-icon-button[name="pencil"]')
      ?.dispatchEvent(new Event('click', { bubbles: true, composed: true }));
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.include('Update Policy');
    expect(element.shadowRoot?.textContent).to.include('Global (Account-wide)');

    const hardLimitInput = element.shadowRoot?.querySelector(
      'sl-input[label="Hard Limit (USD)"]'
    ) as HTMLInputElement | null;
    expect(hardLimitInput?.value).to.equal('100');

    if (hardLimitInput) {
      hardLimitInput.value = '150';
      hardLimitInput.dispatchEvent(new Event('sl-input', { bubbles: true }));
    }
    await element.updateComplete;

    element.shadowRoot
      ?.querySelector('sl-button[variant="primary"]')
      ?.dispatchEvent(new Event('click', { bubbles: true, composed: true }));
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some(
          (call) =>
            String(call.args[0]).includes('/api/v1/budget/policies/policy-1') &&
            call.args[1]?.method === 'PUT'
        )
    );

    const updateCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          String(call.args[0]).includes('/api/v1/budget/policies/policy-1') &&
          call.args[1]?.method === 'PUT'
      );
    expect(updateCall).to.exist;
    expect(JSON.parse(String(updateCall?.args[1]?.body))).to.deep.include({
      hard_limit_usd: 150,
      soft_limit_usd: 80,
      notify_on_soft: true,
      notify_on_hard: false,
      notification_user_ids: ['owner-user-id'],
      notification_emails: ['owner@example.com'],
    });

    await waitUntil(() =>
      element.shadowRoot?.textContent?.includes('Hard: $150.00')
    );
    expect(element.shadowRoot?.textContent).to.include('Hard: $150.00');
  });
});
