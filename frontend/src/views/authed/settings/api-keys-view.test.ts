import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './api-keys-view.ts';
import type { ApiKeysView } from './api-keys-view';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';

describe('ApiKeysView', () => {
  let fetchStub: sinon.SinonStub;
  let wsStub: sinon.SinonStub;

  beforeEach(() => {
    wsStub = sinon.stub(unifiedWebSocketManager, 'send').returns(true);
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (
          url.includes('/api/v1/auth/api-keys') &&
          !url.includes('/governance') &&
          (!init || !init.method || init.method === 'GET')
        ) {
          return new Response(
            JSON.stringify([
              {
                id: 'key-1',
                name: 'OpenClaw Managed Key',
                created_at: '2026-03-10T09:00:00Z',
                last_used_at: '2026-03-10T09:45:00Z',
                last_activity_at: '2026-03-10T10:00:00Z',
                activity_status: 'recently_active',
                expires_at: null,
                recent_model_calls: 2,
                recent_tool_calls: 1,
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.includes('/api/v1/auth/api-keys/key-1/governance')) {
          return new Response(
            JSON.stringify({
              subject_type: 'api_keys',
              subject_id: 'key-1',
              config: {
                allowed_models: ['openai/gpt-5'],
                model_budgets: {
                  'openai/gpt-5': { monthly_usd_limit: 10 },
                },
                tool_rules: {
                  search_issues: [{ action: 'require_approval' }],
                },
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.endsWith('/api/v1/tools')) {
          return new Response(
            JSON.stringify([
              {
                name: 'search_issues',
                description: 'Search GitHub issues',
                schema: { properties: { query: { type: 'string' } } },
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.endsWith('/api/v1/approval-workflows')) {
          return new Response(
            JSON.stringify([
              {
                id: 'wf-1',
                name: 'Default Approval',
                approval_type: 'standard',
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.endsWith('/api/v1/features')) {
          return new Response(JSON.stringify({ features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${url}` }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }
        );
      }
    );
  });

  afterEach(() => {
    wsStub.restore();
    fetchStub.restore();
    localStorage.clear();
  });

  it('renders activity status', async () => {
    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    let content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('OpenClaw Managed Key');
    expect(content).to.contain('Recently active');
    expect(content).to.contain('2 model');
    expect(content).to.contain('1 tool');
  });

  it('keeps recently active chips primary so they do not look idle', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'recent-key',
              name: 'Recent Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: '2026-03-10T09:45:00Z',
              activity_status: 'recently_active',
              expires_at: null,
            },
            {
              id: 'live-key',
              name: 'Live Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: '2026-03-10T10:00:00Z',
              activity_status: 'active_now',
              expires_at: null,
            },
            {
              id: 'idle-key',
              name: 'Idle Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
            {
              id: 'revoked-key',
              name: 'Revoked Key',
              created_at: '2026-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'revoked',
              expires_at: null,
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    (element as any).showAllKeys = true;
    await element.updateComplete;

    const chipVariants = Object.fromEntries(
      Array.from(
        element.shadowRoot?.querySelectorAll('sl-badge.chip') || []
      ).map((badge) => [
        badge.textContent?.trim(),
        badge.getAttribute('variant'),
      ])
    );
    expect(chipVariants).to.deep.equal({
      'Recently active': 'primary',
      'Active now': 'success',
      Idle: 'neutral',
      Revoked: 'danger',
    });
  });

  it('hides revoked and expired keys behind a footer and reveals them on Show all', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'live-key',
              name: 'Production Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
            {
              id: 'revoked-key',
              name: 'Flow Execution 389da654',
              created_at: '2026-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'revoked',
              expires_at: null,
            },
            {
              id: 'expired-key',
              name: 'Old Laptop Key',
              created_at: '2025-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: '2025-06-01T09:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    let content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Production Key');
    expect(content).to.not.contain('Flow Execution 389da654');
    expect(content).to.not.contain('Old Laptop Key');
    expect(content).to.contain('2 keys are revoked or expired and hidden');

    const showAll = Array.from(
      element.shadowRoot?.querySelectorAll('.link-button') || []
    ).find((button) => button.textContent?.trim() === 'Show all');
    expect(showAll, 'Show all control is rendered').to.exist;

    (showAll as HTMLButtonElement).click();
    await element.updateComplete;

    content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Flow Execution 389da654');
    expect(content).to.contain('Old Laptop Key');
    expect(content).to.contain('Revoked');
    expect(content).to.contain('Expired');
  });

  it('offers Revoke only on keys that can still be used', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'live-key',
              name: 'Production Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
            {
              id: 'revoked-key',
              name: 'Flow Execution 389da654',
              created_at: '2026-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'revoked',
              expires_at: null,
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    (element as any).showAllKeys = true;
    await element.updateComplete;

    // No standalone danger button survives: the action lives in a kebab.
    expect(element.shadowRoot?.querySelector('sl-button[variant="danger"]')).to
      .not.exist;

    const actionLists = Array.from(
      element.shadowRoot?.querySelectorAll('resource-actions') || []
    ).map((element_) => (element_ as any).actions as { id: string }[]);
    expect(actionLists.length).to.equal(2);
    expect(actionLists[0].map((action) => action.id)).to.deep.equal(['revoke']);
    expect(actionLists[1]).to.deep.equal([]);
  });

  it('renders Agent badge when managed_agent_id is present', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'managed-key-1',
              name: 'OpenClaw Managed Key',
              managed_agent_id: 'agent-123',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: '2026-03-10T09:45:00Z',
              activity_status: 'recently_active',
              expires_at: null,
            },
            {
              id: 'user-key-1',
              name: 'Personal Dev Key',
              managed_agent_id: null,
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    const badges = Array.from(
      element.shadowRoot?.querySelectorAll('sl-badge') || []
    );
    const badgeTexts = badges.map((b) => b.textContent?.trim());
    expect(badgeTexts).to.include('Agent');
  });
});
