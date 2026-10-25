import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import './session-optimization-panel';
import type { SessionOptimizationPanel } from './session-optimization-panel';

// A role that may edit models but holds none of the action permissions.
const READER = ['view_flows', 'edit_ai_models'];
// The seeded editor: tools and policies, never budgets.
const EDITOR = [
  'view_flows',
  'edit_ai_models',
  'manage_tools',
  'manage_policies',
];
// A custom role with the action permissions but without the apply baseline.
const ACTIONS_ONLY = ['view_flows', 'manage_tools', 'manage_budgets'];
const OWNER = [
  'view_flows',
  'edit_ai_models',
  'manage_account',
  'manage_budgets',
  'manage_tools',
  'manage_policies',
];

const session = {
  id: 'sess-1',
  session_source_type: 'claude_code',
  session_source_id: 'workspace-1',
  session_reference: 'claude-session-1',
  runtime_principal_name: 'Claude Workspace',
  started_at: '2026-03-09T18:00:00Z',
  last_activity_at: '2026-03-09T20:00:00Z',
  ended_at: null,
  is_active_now: true,
  activity_status: 'active_now',
  total_requests: 1,
  successful_requests: 1,
  failed_requests: 0,
  token_usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  estimated_cost: 0.1,
  last_request_at: '2026-03-09T20:00:00Z',
};

function suggestion(id: string, type: string, params = {}) {
  return {
    id,
    title: `Suggestion ${id}`,
    description: 'Example.',
    expectedSavingsTokens: 10,
    expectedSavingsUsd: 0.01,
    confidence: 'high' as const,
    actionLabel: 'Apply',
    evidence: [],
    action: { type, params },
  };
}

describe('SessionOptimizationPanel apply permissions', () => {
  let fetchStub: sinon.SinonStub;

  function stubProfile(permissions: string[] | null) {
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      const body = url.includes('/api/v1/auth/users/me')
        ? { id: 'user-1', permissions }
        : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    invalidateApiCaches();
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  async function render(permissions: string[] | null) {
    stubProfile(permissions);
    const el = (await fixture(
      html`<session-optimization-panel
        .session=${session}
        .suggestions=${[
          suggestion('budget', 'set_budget', { limit_usd: 5 }),
          suggestion('tools', 'scope_tools', { tool_names: ['search'] }),
        ]}
      ></session-optimization-panel>`
    )) as SessionOptimizationPanel;
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/auth/users/me'))
    );
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
    return el;
  }

  function applyButtons(el: SessionOptimizationPanel) {
    return Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).filter((b) => b.textContent?.trim() === 'Apply') as (HTMLElement & {
      disabled: boolean;
    })[];
  }

  it('disables Apply and names the missing permission for a reader', async () => {
    const el = await render(READER);
    await waitUntil(
      () =>
        el.shadowRoot?.querySelectorAll(
          '[data-testid="apply-missing-permission"]'
        ).length === 2,
      'missing-permission notes did not render'
    );
    const buttons = applyButtons(el);
    expect(buttons).to.have.length(2);
    expect(buttons.every((b) => b.disabled)).to.equal(true);
    const notes = Array.from(
      el.shadowRoot?.querySelectorAll(
        '[data-testid="apply-missing-permission"]'
      ) || []
    ).map((n) => (n.textContent || '').replace(/\s+/g, ' ').trim());
    expect(notes).to.deep.equal([
      'Needs the manage budgets permission',
      'Needs the manage tools permission',
    ]);
  });

  it('disables only the budget action for the seeded editor', async () => {
    const el = await render(EDITOR);
    await waitUntil(
      () =>
        el.shadowRoot?.querySelectorAll(
          '[data-testid="apply-missing-permission"]'
        ).length === 1,
      'missing-permission note did not render'
    );
    const buttons = applyButtons(el);
    expect(buttons).to.have.length(2);
    expect(buttons.map((b) => b.disabled)).to.deep.equal([true, false]);
    const notes = Array.from(
      el.shadowRoot?.querySelectorAll(
        '[data-testid="apply-missing-permission"]'
      ) || []
    ).map((n) => (n.textContent || '').replace(/\s+/g, ' ').trim());
    expect(notes).to.deep.equal(['Needs the manage budgets permission']);
  });

  it('reports the apply baseline when the action permission alone is held', async () => {
    const el = await render(ACTIONS_ONLY);
    await waitUntil(
      () =>
        el.shadowRoot?.querySelectorAll(
          '[data-testid="apply-missing-permission"]'
        ).length === 2,
      'missing-permission notes did not render'
    );
    const buttons = applyButtons(el);
    expect(buttons.every((b) => b.disabled)).to.equal(true);
    const notes = Array.from(
      el.shadowRoot?.querySelectorAll(
        '[data-testid="apply-missing-permission"]'
      ) || []
    ).map((n) => (n.textContent || '').replace(/\s+/g, ' ').trim());
    expect(notes).to.deep.equal([
      'Needs the edit AI models permission',
      'Needs the edit AI models permission',
    ]);
  });

  it('keeps Apply enabled for an owner', async () => {
    const el = await render(OWNER);
    const buttons = applyButtons(el);
    expect(buttons).to.have.length(2);
    expect(buttons.some((b) => b.disabled)).to.equal(false);
    expect(
      el.shadowRoot?.querySelector('[data-testid="apply-missing-permission"]')
    ).to.not.exist;
  });

  it('keeps Apply enabled when RBAC is off', async () => {
    const el = await render(null);
    expect(applyButtons(el).some((b) => b.disabled)).to.equal(false);
  });
});
