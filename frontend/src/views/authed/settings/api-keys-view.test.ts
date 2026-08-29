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
