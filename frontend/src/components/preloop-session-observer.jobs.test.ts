/**
 * Async optimization job flow in the session observer: submit-then-poll on
 * Generate, retry after failure, and reload-resume from sessionStorage.
 */
import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';
import './preloop-session-observer';
import type { PreloopSessionObserver } from './preloop-session-observer';

const SESSION = {
  id: 'runtime-session-1',
  session_source_type: 'claude_code',
  session_source_id: 'workspace-42',
  session_reference: 'claude-session-42',
  runtime_principal_name: 'Claude Workspace',
  started_at: '2026-07-19T18:00:00Z',
  last_activity_at: '2026-07-19T20:00:00Z',
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
  last_request_at: '2026-07-19T20:00:00Z',
};

const SUCCEEDED_RESULT = {
  generated_by: 'model',
  fast_model_name: 'fast-model',
  model_id: 'model-1',
  model_name: 'fast-model',
  potential_savings_tokens: 300,
  suggestions: [
    {
      id: 'trim-context',
      title: 'Trim prompt context',
      description: 'Most tokens were prompt-side.',
      expected_savings_tokens: 300,
      expected_savings_usd: 0.08,
      confidence: 'medium',
      action_label: 'Review context segments',
      evidence: ['1200 prompt tokens'],
    },
  ],
};

const STORAGE_KEY = 'preloop_optimize_job_runtime-session-1';

describe('PreloopSessionObserver — async optimization jobs', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let submittedJobs: string[];
  // Status script per job id, consumed one entry per poll (last repeats).
  let jobStatusScript: Record<string, Array<Record<string, unknown>>>;

  function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sessionStorage.clear();
    submittedJobs = [];
    jobStatusScript = {};
    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => undefined);
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const jobPollMatch = url.match(/\/optimizations\/jobs\/([^/?]+)$/);
        if (jobPollMatch) {
          const jobId = jobPollMatch[1];
          const script = jobStatusScript[jobId] || [];
          const next = script.length > 1 ? script.shift()! : script[0];
          if (!next) return jsonResponse({ detail: 'not found' }, 404);
          return jsonResponse(next);
        }
        if (url.includes('/optimizations/jobs')) {
          const jobId = `job-${submittedJobs.length + 1}`;
          submittedJobs.push(jobId);
          return jsonResponse({ job_id: jobId, status: 'pending' }, 202);
        }
        if (url.includes('/optimizations/actions')) {
          return jsonResponse({ items: [] });
        }
        if (url.includes('/optimizations')) {
          // Panel-open cache probe stays on the legacy inline endpoint.
          const body = init?.body ? JSON.parse(String(init.body)) : {};
          if (body.cache_only) {
            return jsonResponse({
              generated_by: 'local',
              fast_model_name: null,
              cache_miss: true,
              suggestions: [],
            });
          }
          return jsonResponse({
            generated_by: 'local',
            fast_model_name: null,
            suggestions: [],
          });
        }
        if (url.includes('/ai-models')) {
          return jsonResponse([
            {
              id: 'model-1',
              name: 'fast-model',
              provider_name: 'anthropic',
              model_kind: 'llm',
              model_identifier: 'claude-sonnet-4',
              is_default: true,
              created_at: '2026-07-19T00:00:00Z',
              updated_at: '2026-07-19T00:00:00Z',
            },
          ]);
        }
        if (url.includes('/gateway-events')) {
          return jsonResponse({
            logs: [
              {
                id: 'event-1',
                timestamp: '2026-07-19T20:00:00Z',
                type: 'model_gateway_call',
                payload: {
                  outcome: 'success',
                  model_alias: 'anthropic/claude-sonnet-4',
                  prompt_tokens: 1200,
                  completion_tokens: 100,
                  total_tokens: 1300,
                  estimated_cost: 0.42,
                  conversation_preview: {
                    messages: [{ role: 'user', text: 'Build a widget' }],
                  },
                },
              },
            ],
          });
        }
        if (url.includes('/activity')) {
          return jsonResponse({ items: [] });
        }
        return jsonResponse({});
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  async function mountObserver(): Promise<PreloopSessionObserver> {
    const el = (await fixture(
      html`<preloop-session-observer
        .sessions=${[SESSION]}
        .features=${{ optimization: true }}
      ></preloop-session-observer>`
    )) as PreloopSessionObserver;
    await waitUntil(
      () => Boolean(el.shadowRoot?.querySelector('session-replay-panel')),
      'replay panel did not mount',
      { timeout: 3000 }
    );
    return el;
  }

  function replayPanel(el: PreloopSessionObserver) {
    return el.shadowRoot?.querySelector('session-replay-panel');
  }

  function optimizationPanel(el: PreloopSessionObserver) {
    return replayPanel(el)?.shadowRoot?.querySelector(
      'session-optimization-panel'
    );
  }

  async function openOptimizeTab(el: PreloopSessionObserver): Promise<void> {
    const optimizeButton = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) => button.textContent?.trim() === 'Optimize');
    expect(optimizeButton, 'Optimize tab button required').to.exist;
    optimizeButton!.click();
    await el.updateComplete;
  }

  async function clickGenerate(el: PreloopSessionObserver): Promise<void> {
    const panel = replayPanel(el);
    await waitUntil(
      () =>
        Boolean(
          Array.from(
            panel?.shadowRoot?.querySelectorAll('sl-button') || []
          ).find((button) =>
            /Generate suggestions/.test(button.textContent || '')
          )
        ),
      'Generate button did not render',
      { timeout: 3000 }
    );
    const generate = Array.from(
      panel?.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) => /Generate suggestions/.test(button.textContent || ''));
    generate!.click();
    await el.updateComplete;
  }

  it('submits a job on Generate, shows analyzing, and renders the polled result', async () => {
    const el = await mountObserver();
    await openOptimizeTab(el);
    jobStatusScript['job-1'] = [
      {
        job_id: 'job-1',
        status: 'succeeded',
        result: SUCCEEDED_RESULT,
        error: null,
      },
    ];

    await clickGenerate(el);

    // The submit hits the async jobs endpoint (not the legacy inline one).
    await waitUntil(() => submittedJobs.length === 1, 'job was not submitted', {
      timeout: 3000,
    });
    // The stored job id enables reload-resume while the job is active.
    // (It is cleared again once the poll below completes.)

    // First poll is immediate; the succeeded result renders as suggestions.
    await waitUntil(
      () => {
        const panel = optimizationPanel(el);
        return Boolean(
          panel?.shadowRoot?.textContent?.includes('Trim prompt context')
        );
      },
      'polled suggestions did not render',
      { timeout: 4000 }
    );
    // Job finished: the persisted id is cleared.
    expect(sessionStorage.getItem(STORAGE_KEY)).to.equal(null);
  });

  it('shows the failed state and retries with a fresh job', async () => {
    const el = await mountObserver();
    await openOptimizeTab(el);
    jobStatusScript['job-1'] = [
      {
        job_id: 'job-1',
        status: 'failed',
        result: null,
        error: "The model request didn't complete. Nothing was changed.",
      },
    ];
    jobStatusScript['job-2'] = [
      {
        job_id: 'job-2',
        status: 'succeeded',
        result: SUCCEEDED_RESULT,
        error: null,
      },
    ];

    await clickGenerate(el);

    await waitUntil(
      () =>
        Boolean(
          optimizationPanel(el)?.shadowRoot?.querySelector('.job-failed')
        ),
      'failed state did not render',
      { timeout: 4000 }
    );

    const retry = optimizationPanel(el)?.shadowRoot?.querySelector(
      '.job-retry-button'
    ) as HTMLButtonElement;
    expect(retry, 'retry button required').to.exist;
    retry.click();

    await waitUntil(
      () => submittedJobs.length === 2,
      'retry did not submit a fresh job',
      { timeout: 4000 }
    );
    await waitUntil(
      () =>
        Boolean(
          optimizationPanel(el)?.shadowRoot?.textContent?.includes(
            'Trim prompt context'
          )
        ),
      'retried job result did not render',
      { timeout: 4000 }
    );
  });

  it('resumes a persisted in-flight job on mount instead of showing the trigger', async () => {
    sessionStorage.setItem(STORAGE_KEY, 'job-resume');
    jobStatusScript['job-resume'] = [
      { job_id: 'job-resume', status: 'running', result: null, error: null },
    ];

    const el = await mountObserver();
    await openOptimizeTab(el);

    // Polling resumes against the stored id without a new submit...
    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some((call) =>
            String(call.args[0]).includes('/optimizations/jobs/job-resume')
          ),
      'stored job was not polled',
      { timeout: 4000 }
    );
    expect(submittedJobs.length, 'no new job submitted on resume').to.equal(0);

    // ...and the analyzing state renders instead of the Generate controls.
    await waitUntil(
      () =>
        Boolean(
          optimizationPanel(el)?.shadowRoot?.querySelector('.job-analyzing')
        ),
      'analyzing state did not render on resume',
      { timeout: 4000 }
    );
    const controls =
      replayPanel(el)?.shadowRoot?.querySelector('.optimize-controls');
    expect(controls, 'trigger controls hidden while resuming').to.not.exist;
  });
});
