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

  const stubBillingFetch = (opts?: {
    failModels?: boolean;
    policies?: unknown[];
    users?: Array<{ id: string; email: string; username?: string }>;
  }) => {
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
            JSON.stringify({
              users: opts?.users || [],
              total: opts?.users?.length || 0,
              skip: 0,
              limit: 100,
            }),
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
        if (url.includes('/api/v1/budget/policies') && method === 'POST') {
          const body = JSON.parse(String(init?.body || '{}'));
          return new Response(
            JSON.stringify({
              id: 'policy-new',
              subject_type: body.subject_type || 'global',
              subject_id: body.subject_id || null,
              model_alias: null,
              period: body.period || 'monthly',
              hard_limit_usd: body.hard_limit_usd,
              soft_limit_usd: body.soft_limit_usd,
              notify_on_soft: body.notify_on_soft,
              notify_on_hard: body.notify_on_hard,
              notification_user_ids: body.notification_user_ids,
              notification_team_ids: body.notification_team_ids,
              notification_emails: body.notification_emails,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.includes('/api/v1/budget/policies') && method === 'GET') {
          return new Response(JSON.stringify(opts?.policies || []), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('{}', { status: 200 });
      }
    );
  };

  const mountEditor = async () => {
    const element = (await fixture(
      html`<budget-policy-editor billingEnabled></budget-policy-editor>`
    )) as BudgetPolicyEditor;
    await waitUntil(
      () => !element.shadowRoot?.textContent?.includes('Loading limits'),
      'editor finished loading its policies'
    );
    await element.updateComplete;
    return element;
  };

  it('lists the limits grouped by scope with periods and amounts', async () => {
    stubBillingFetch({
      policies: [
        {
          id: 'policy-1',
          subject_type: 'global',
          subject_id: null,
          model_alias: null,
          period: 'monthly',
          hard_limit_usd: 300,
          soft_limit_usd: 200,
          current_spend_usd: 220,
          notify_on_soft: true,
          notify_on_hard: false,
          notification_user_ids: ['user-1'],
          notification_team_ids: [],
          notification_emails: [],
        },
        {
          id: 'policy-2',
          subject_type: 'managed_agent',
          subject_id: 'agent-1',
          model_alias: null,
          period: 'daily',
          hard_limit_usd: null,
          soft_limit_usd: 25,
          current_spend_usd: 5,
          notify_on_soft: false,
          notify_on_hard: false,
          notification_user_ids: [],
          notification_team_ids: [],
          notification_emails: [],
        },
      ],
    });

    const element = await mountEditor();
    await waitUntil(() =>
      Boolean(element.shadowRoot?.querySelector('.limit-row'))
    );

    const title = element.shadowRoot?.querySelector(
      '#budget-policy-editor-title'
    );
    expect(title?.textContent?.trim()).to.equal('Limits');

    const groups = Array.from(
      element.shadowRoot!.querySelectorAll('.group-label')
    ).map((label) => label.textContent!.trim());
    expect(groups).to.eql(['Global', 'Agents']);

    const rows = element.shadowRoot!.querySelectorAll('.limit-row');
    expect(rows.length).to.equal(2);
    expect(rows[0].textContent).to.contain('Global');
    expect(rows[0].textContent).to.contain('Monthly');
    expect(rows[0].textContent).to.contain('Soft $200.00 · Hard $300.00');
    expect(rows[0].querySelector('.budget-track')).to.exist;
    expect(rows[0].querySelector('sl-icon[name="bell"]')).to.exist;
    expect(rows[1].textContent).to.contain('Soft $25.00');
    expect(rows[1].textContent).to.not.contain('Hard $');
    expect(rows[1].querySelector('sl-icon[name="bell"]')).to.not.exist;
  });

  it('invites a first limit when none exist', async () => {
    stubBillingFetch();
    const element = await mountEditor();

    expect(element.shadowRoot?.textContent).to.contain(
      'No limits yet. Start with a global monthly hard limit.'
    );
    expect(element.shadowRoot?.querySelector('.limit-row')).to.not.exist;
  });

  it('replaces the list with the form and comes back', async () => {
    stubBillingFetch();
    const element = await mountEditor();

    (
      element.shadowRoot!.querySelector(
        'sl-button[variant="primary"]'
      ) as HTMLElement
    ).click();
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('New limit');
    expect(element.shadowRoot?.textContent).to.not.contain('No limits yet');
    expect(element.shadowRoot?.querySelector('sl-radio-group')).to.exist;
    expect(
      element.shadowRoot?.querySelector('sl-input[label="Soft limit (USD)"]')
    ).to.exist;

    (element.shadowRoot!.querySelector('.back-button') as HTMLElement).click();
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('No limits yet');
  });

  it('refuses to save without a limit and with soft above hard', async () => {
    stubBillingFetch();
    const element = await mountEditor();
    (element as any).startAdd();
    await element.updateComplete;

    const save = () =>
      (
        element.shadowRoot!.querySelectorAll(
          'sl-button[variant="primary"]'
        )[0] as HTMLElement
      ).click();

    save();
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('.field-error')?.textContent
    ).to.contain('Set a soft limit, a hard limit, or both.');

    (element as any).newSoftLimit = '300';
    (element as any).newHardLimit = '100';
    await element.updateComplete;
    save();
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector('.field-error')?.textContent
    ).to.contain('The soft limit must be at or below the hard limit.');

    expect(
      fetchStub
        .getCalls()
        .some((call) => (call.args[1] as any)?.method === 'POST')
    ).to.be.false;
  });

  it('confirms in a dialog before deleting a limit', async () => {
    stubBillingFetch({
      policies: [
        {
          id: 'policy-1',
          subject_type: 'global',
          subject_id: null,
          model_alias: null,
          period: 'monthly',
          hard_limit_usd: 300,
          soft_limit_usd: null,
          current_spend_usd: 10,
          notify_on_soft: false,
          notify_on_hard: false,
          notification_user_ids: [],
          notification_team_ids: [],
          notification_emails: [],
        },
      ],
    });
    const element = await mountEditor();
    await waitUntil(() =>
      Boolean(element.shadowRoot?.querySelector('.limit-row'))
    );

    const dialog = element.shadowRoot!.querySelector('sl-dialog')!;
    expect(dialog.hasAttribute('open')).to.be.false;

    (
      element.shadowRoot!.querySelectorAll('sl-menu-item')[1] as HTMLElement
    ).click();
    await element.updateComplete;
    expect(dialog.hasAttribute('open')).to.be.true;

    (
      dialog.querySelector('sl-button[variant="danger"]') as HTMLElement
    ).click();
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some(
          (call) =>
            String(call.args[0]).includes('/api/v1/budget/policies/policy-1') &&
            (call.args[1] as any)?.method === 'DELETE'
        )
    );
    await waitUntil(
      () => !element.shadowRoot?.querySelector('.limit-row'),
      'row disappears after the delete'
    );
  });

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
    expect(title?.textContent?.trim()).to.equal('Limits');
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
      Boolean(element.shadowRoot?.querySelector('sl-menu-item'))
    );

    (
      element.shadowRoot!.querySelectorAll('sl-menu-item')[0] as HTMLElement
    ).click();
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.include('Edit limit');
    expect(element.shadowRoot?.textContent).to.include('Save limit');

    const hardLimitInput = element.shadowRoot?.querySelector(
      'sl-input[label="Hard limit (USD)"]'
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
      element.shadowRoot?.textContent?.includes('Hard $150.00')
    );
    expect(element.shadowRoot?.textContent).to.include('Hard $150.00');
  });

  async function fillHardLimitAndSave(element: BudgetPolicyEditor) {
    await waitUntil(
      () => Boolean(element.shadowRoot?.textContent?.includes('Save limit')),
      'add-limit form did not open'
    );
    const hardLimitInput = element.shadowRoot?.querySelector(
      'sl-input[label="Hard limit (USD)"]'
    ) as HTMLInputElement | null;
    expect(hardLimitInput).to.exist;
    hardLimitInput!.value = '10';
    hardLimitInput!.dispatchEvent(new Event('sl-input', { bubbles: true }));
    await element.updateComplete;
    await waitUntil(
      () =>
        Boolean(
          (element as unknown as { subjectsLoaded: boolean }).subjectsLoaded
        ),
      'subject lists did not finish loading'
    );
    element.shadowRoot
      ?.querySelector('sl-button[variant="primary"]')
      ?.dispatchEvent(new Event('click', { bubbles: true, composed: true }));
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some(
          (call) =>
            String(call.args[0]).includes('/api/v1/budget/policies') &&
            call.args[1]?.method === 'POST'
        )
    );
    const createCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          String(call.args[0]).includes('/api/v1/budget/policies') &&
          call.args[1]?.method === 'POST'
      );
    return JSON.parse(String(createCall?.args[1]?.body));
  }

  it('does not POST a null notification user id when /me has no id', async () => {
    stubBillingFetch();
    const element = await mountEditor();
    (
      element.shadowRoot!.querySelector(
        'sl-button[variant="primary"]'
      ) as HTMLElement
    ).click();
    const body = await fillHardLimitAndSave(element);
    expect(body.notification_user_ids).to.equal(null);
    expect(body.hard_limit_usd).to.equal(10);
  });

  it('defaults notify recipients to the current user from the users list', async () => {
    stubBillingFetch({
      users: [
        {
          id: 'owner-user-id',
          email: 'owner@example.com',
          username: 'owner',
        },
      ],
    });
    const element = await mountEditor();
    (
      element.shadowRoot!.querySelector(
        'sl-button[variant="primary"]'
      ) as HTMLElement
    ).click();
    const body = await fillHardLimitAndSave(element);
    expect(body.notification_user_ids).to.deep.equal(['owner-user-id']);
  });

  it('matches the current user by email case-insensitively', async () => {
    stubBillingFetch({
      users: [
        {
          id: 'owner-user-id',
          email: 'Owner@Example.com',
          username: 'owner',
        },
      ],
    });
    const element = await mountEditor();
    (
      element.shadowRoot!.querySelector(
        'sl-button[variant="primary"]'
      ) as HTMLElement
    ).click();
    const body = await fillHardLimitAndSave(element);
    expect(body.notification_user_ids).to.deep.equal(['owner-user-id']);
  });
});
