import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './session-optimization-panel';
import type { SessionOptimizationPanel } from './session-optimization-panel';

describe('SessionOptimizationPanel — Verify savings', () => {
  let fetchStub: sinon.SinonStub;

  const session = {
    id: 'sess-1',
    session_source_type: 'claude_code',
    session_source_id: 'workspace-1',
    session_reference: 'claude-session-1',
    runtime_principal_name: 'Claude Workspace',
    started_at: '2026-03-09T18:00:00Z',
    last_activity_at: '2026-03-09T20:00:00Z',
    ended_at: null,
    latest_model_alias: 'anthropic/claude-sonnet-4',
    latest_provider_name: 'Anthropic',
    is_active_now: true,
    activity_status: 'active_now',
    total_requests: 1,
    successful_requests: 1,
    failed_requests: 0,
    token_usage: {
      prompt_tokens: 1200,
      completion_tokens: 100,
      total_tokens: 1300,
    },
    estimated_cost: 0.42,
    last_request_at: '2026-03-09T20:00:00Z',
  };

  const scopeToolsSuggestion = {
    id: 'scope-search',
    title: 'Disable unused tools',
    description: 'The search tool was never called.',
    expectedSavingsTokens: 400,
    expectedSavingsUsd: 0.09,
    confidence: 'high' as const,
    actionLabel: 'Scope tools',
    evidence: ['search advertised but unused'],
    action: {
      type: 'scope_tools',
      params: { tool_names: ['search'] },
    },
  };

  const openEventsSuggestion = {
    id: 'inspect-failures',
    title: 'Inspect failed requests',
    description: 'Two requests errored out.',
    expectedSavingsTokens: 0,
    expectedSavingsUsd: 0,
    confidence: 'low' as const,
    actionLabel: 'Inspect evidence',
    evidence: ['2 failed requests'],
    action: {
      type: 'open_events',
      params: { event_ids: ['ev-1', 'ev-2'] },
    },
  };

  const replayResponse = {
    id: 'replay-1',
    runtime_session_id: 'sess-1',
    status: 'completed',
    input_delta_tokens: 120,
    input_pct_saved: 0.3,
    end_to_end_delta_median: 40,
    end_to_end_delta_low: 38,
    end_to_end_delta_high: 42,
    inconclusive: false,
    cost_spent: 0.01,
    n_runs: 3,
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/replay')) {
        return new Response(JSON.stringify(replayResponse), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  function verifyButton(el: SessionOptimizationPanel) {
    return Array.from(el.shadowRoot?.querySelectorAll('sl-button') || []).find(
      (button) => button.textContent?.trim() === 'Verify savings'
    );
  }

  function dialogButton(el: SessionOptimizationPanel, label: string) {
    const dialog = el.shadowRoot?.querySelector(
      'sl-dialog[label="Verify savings by replay"]'
    );
    return Array.from(dialog?.querySelectorAll('sl-button') || []).find(
      (button) => button.textContent?.trim() === label
    );
  }

  async function renderPanel(suggestions: unknown[]) {
    const el = (await fixture(
      html`<session-optimization-panel
        .session=${session}
        .suggestions=${suggestions}
      ></session-optimization-panel>`
    )) as SessionOptimizationPanel;
    await el.updateComplete;
    return el;
  }

  it('renders a "Verify savings" button for a scope_tools suggestion', async () => {
    const el = await renderPanel([scopeToolsSuggestion]);
    expect(verifyButton(el), 'expected a Verify savings button').to.exist;
  });

  it('opens the consent dialog when "Verify savings" is clicked', async () => {
    const el = await renderPanel([scopeToolsSuggestion]);
    expect(
      el.shadowRoot?.querySelector(
        'sl-dialog[label="Verify savings by replay"]'
      )
    ).to.not.exist;

    verifyButton(el)!.click();
    await el.updateComplete;

    const dialog = el.shadowRoot?.querySelector(
      'sl-dialog[label="Verify savings by replay"]'
    );
    expect(dialog, 'consent dialog should render').to.exist;
    expect(dialog?.hasAttribute('open')).to.be.true;
    expect(dialogButton(el, 'Cancel')).to.exist;
    expect(dialogButton(el, 'I understand, verify')).to.exist;
  });

  it('replays and renders measured input savings when confirmed', async () => {
    const el = await renderPanel([scopeToolsSuggestion]);
    verifyButton(el)!.click();
    await el.updateComplete;

    dialogButton(el, 'I understand, verify')!.click();

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some(
            (call) =>
              typeof call.args[0] === 'string' &&
              (call.args[0] as string).includes('/replay')
          ),
      'replay was not requested',
      { timeout: 3000 }
    );
    await new Promise((resolve) => setTimeout(resolve, 100));
    await el.updateComplete;

    const replayCall = fetchStub
      .getCalls()
      .find(
        (call) =>
          typeof call.args[0] === 'string' &&
          (call.args[0] as string).includes('/replay')
      );
    expect(replayCall, 'expected a /replay fetch call').to.exist;
    const [reqUrl, init] = replayCall!.args as [string, RequestInit];
    expect(reqUrl).to.include(
      '/api/v1/billing/cost/runtime-sessions/sess-1/replay'
    );
    expect(init.method).to.equal('POST');
    const body = JSON.parse(init.body as string);
    expect(body.consented).to.equal(true);
    expect(body.candidate.removed_tool_names).to.deep.equal(['search']);

    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('.verify-result')),
      'verify result did not render',
      { timeout: 3000 }
    );
    const resultText =
      el.shadowRoot?.querySelector('.verify-result')?.textContent || '';
    expect(resultText).to.include('Measured input savings');
    expect(resultText).to.include('120');
    expect(resultText).to.include('30%');
  });

  it('closes the dialog without replaying when Cancel is clicked', async () => {
    const el = await renderPanel([scopeToolsSuggestion]);
    verifyButton(el)!.click();
    await el.updateComplete;

    dialogButton(el, 'Cancel')!.click();
    await el.updateComplete;

    expect(
      el.shadowRoot?.querySelector(
        'sl-dialog[label="Verify savings by replay"]'
      )
    ).to.not.exist;
    const replayed = fetchStub
      .getCalls()
      .some(
        (call) =>
          typeof call.args[0] === 'string' &&
          (call.args[0] as string).includes('/replay')
      );
    expect(replayed, 'Cancel must not trigger a replay').to.be.false;
  });

  it('does not render a Verify button for an open_events suggestion', async () => {
    const el = await renderPanel([openEventsSuggestion]);
    expect(verifyButton(el)).to.not.exist;
  });

  it('surfaces measured idle cache expiry cost on the optimize tab', async () => {
    const idleSuggestion = {
      id: 'reduce-idle-cache-expiry',
      title: 'Avoid idle prompt-cache expiry',
      description: 'Cache expired after a pause.',
      expectedSavingsTokens: 8000,
      expectedSavingsUsd: 0.0276,
      confidence: 'high' as const,
      actionLabel: 'Inspect idle cache expiries',
      evidence: ['1 idle cache expiry'],
      evidenceEventIds: ['e2'],
    };
    const el = (await fixture(
      html`<session-optimization-panel
        .session=${session}
        .suggestions=${[idleSuggestion]}
        .optimization=${{
          generated_by: 'local',
          suggestions: [],
          potential_savings_tokens: 8000,
          potential_savings_usd: 0.0276,
          analyzed_scope_estimated_cost: 1.25,
          context_profile: {
            session_id: 'sess-1',
            analyzed_event_count: 2,
            total_prompt_tokens: 10000,
            total_completion_tokens: 200,
            cache_profile: {
              idle_expiry_events: [
                {
                  event_id: 'e2',
                  previous_event_id: 'e1',
                  idle_seconds: 660,
                  rewritten_tokens: 8000,
                  measured_extra_cost_usd: 0.0276,
                },
              ],
              measured_idle_expiry_extra_cost_usd: 0.0276,
            },
          },
        }}
      ></session-optimization-panel>`
    )) as SessionOptimizationPanel;
    await el.updateComplete;
    const summary = el.shadowRoot?.querySelector(
      '[data-testid="idle-expiry-summary"]'
    );
    expect(summary, 'idle expiry summary should render').to.exist;
    const text = (summary?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.include('Cache expiries cost');
    expect(text).to.include("this session's");
    expect(text).to.include('1 expir');
    expect(text).to.match(/\$0\.02|\$0\.03|0\.0276/);
  });
});
