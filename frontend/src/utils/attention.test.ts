import { expect } from '@open-wc/testing';

import {
  ATTENTION_KIND_META,
  ATTENTION_KIND_ORDER,
  attentionKindChipLabel,
  deriveAttentionItems,
  groupAttentionItems,
  type AttentionInputs,
} from './attention';

const NOW = new Date('2026-09-02T12:00:00Z');

function minutesAgo(minutes: number): string {
  return new Date(NOW.getTime() - minutes * 60000).toISOString();
}

function daysAgo(days: number): string {
  return new Date(NOW.getTime() - days * 86400000).toISOString();
}

function agentFixture(overrides: Record<string, unknown> = {}): any {
  return {
    id: 'agent-1',
    display_name: 'Hermes',
    session_source_id: 'hermes-principal',
    lifecycle_state: 'active',
    activity_status: 'idle',
    onboarding_state: 'fully_onboarded',
    live_validation_status: 'passed',
    last_seen_at: minutesAgo(30),
    ...overrides,
  };
}

function sessionFixture(overrides: Record<string, unknown> = {}): any {
  return {
    id: 'session-1',
    runtime_principal_id: 'hermes-principal',
    session_source_id: 'hermes-principal',
    started_at: minutesAgo(60),
    last_activity_at: minutesAgo(10),
    failed_requests: 0,
    ...overrides,
  };
}

function failureFixture(overrides: Record<string, unknown> = {}): any {
  return {
    api_usage_id: 'usage-1',
    timestamp: minutesAgo(5),
    status_code: 500,
    outcome: 'error',
    endpoint: '/openai/v1/chat/completions',
    model_alias: 'deepseek/deepseek-v4-pro',
    provider_name: 'deepseek',
    ...overrides,
  };
}

function derive(inputs: AttentionInputs) {
  return deriveAttentionItems({ now: NOW, ...inputs }).items;
}

describe('deriveAttentionItems', () => {
  describe('approvals', () => {
    it('emits a critical item per pending request with agent and relative time', () => {
      const items = derive({
        approvals: [
          {
            id: 'approval-1',
            tool_name: 'shell.run',
            summary: 'Deploy to production',
            status: 'pending',
            requested_at: minutesAgo(5),
            managed_agent_name: 'Hermes',
          },
        ],
      });

      expect(items).to.have.length(1);
      expect(items[0].id).to.equal('approval:approval-1');
      expect(items[0].kind).to.equal('approval');
      expect(items[0].severity).to.equal('critical');
      expect(items[0].title).to.equal('Deploy to production');
      expect(items[0].detail).to.equal('Hermes · pending 5m');
      expect(items[0].href).to.equal('/console/approval/approval-1');
    });

    it('falls back to the tool name and prefixes questions', () => {
      const items = derive({
        approvals: [
          {
            id: 'approval-2',
            tool_name: 'github.merge',
            requested_at: minutesAgo(1),
          },
          {
            id: 'approval-3',
            tool_name: 'ask.user',
            summary: 'Which branch?',
            is_question: true,
            requested_at: minutesAgo(1),
          },
        ],
      });

      const titles = items.map((item) => item.title);
      expect(titles).to.include('github.merge');
      expect(titles).to.include('Question: Which branch?');
    });

    it('ignores requests that are no longer pending', () => {
      const items = derive({
        approvals: [
          {
            id: 'approval-4',
            tool_name: 'shell.run',
            status: 'approved',
            requested_at: minutesAgo(5),
          },
        ],
      });

      expect(items).to.have.length(0);
    });
  });

  describe('agents', () => {
    it('reports a failed live check', () => {
      const items = derive({
        agents: [agentFixture({ live_validation_status: 'failed' })],
      });

      expect(items).to.have.length(1);
      expect(items[0].kind).to.equal('agent');
      expect(items[0].severity).to.equal('warning');
      expect(items[0].title).to.equal('Hermes');
      expect(items[0].detail).to.equal('Live check failed');
      expect(items[0].href).to.equal('/console/agents/agent-1');
    });

    it('joins every reason for one agent with a middle dot', () => {
      const items = derive({
        agents: [
          agentFixture({
            live_validation_status: 'failed',
            onboarding_state: 'incomplete',
          }),
        ],
        sessions: [sessionFixture({ failed_requests: 2 })],
      });

      expect(items).to.have.length(1);
      expect(items[0].detail).to.equal(
        'Live check failed · Not connected · 2 failed requests in last session'
      );
    });

    it('counts failures from the latest session only', () => {
      const items = derive({
        agents: [agentFixture()],
        sessions: [
          sessionFixture({
            id: 'older',
            last_activity_at: minutesAgo(600),
            failed_requests: 9,
          }),
          sessionFixture({
            id: 'newest',
            last_activity_at: minutesAgo(2),
            failed_requests: 1,
          }),
        ],
      });

      expect(items[0].detail).to.equal('1 failed request in last session');
    });

    it('matches sessions whose principal id is suffixed from the agent source id', () => {
      const items = derive({
        agents: [agentFixture()],
        sessions: [
          sessionFixture({
            runtime_principal_id: 'hermes-principal:2',
            session_source_id: 'hermes-principal:2',
            failed_requests: 3,
          }),
        ],
      });

      expect(items[0].detail).to.contain('3 failed requests in last session');
    });

    it('skips suspended and decommissioned agents', () => {
      const items = derive({
        agents: [
          agentFixture({
            id: 'agent-suspended',
            lifecycle_state: 'suspended',
            live_validation_status: 'failed',
          }),
          agentFixture({
            id: 'agent-gone',
            lifecycle_state: 'decommissioned',
            onboarding_state: 'incomplete',
          }),
        ],
      });

      expect(items).to.have.length(0);
    });

    it('stays quiet for a healthy agent', () => {
      const items = derive({
        agents: [agentFixture()],
        sessions: [sessionFixture()],
      });

      expect(items).to.have.length(0);
    });
  });

  describe('flows', () => {
    it('emits one item for a single failed execution inside the 7 day window', () => {
      const items = derive({
        executions: [
          {
            id: 'exec-1',
            flow_name: 'Pull Request Reviewer',
            status: 'FAILED',
            start_time: minutesAgo(90),
            end_time: minutesAgo(88),
          },
        ],
      });

      expect(items).to.have.length(1);
      expect(items[0].kind).to.equal('flow');
      expect(items[0].title).to.equal('Pull Request Reviewer');
      expect(items[0].detail).to.equal('Failed · 1h ago · 2m 0s');
      expect(items[0].href).to.equal('/console/flows/executions/exec-1');
    });

    it('groups repeated failures of one flow into a single counted row', () => {
      const items = derive({
        executions: [
          {
            id: 'exec-1',
            flow_id: 'flow-1',
            flow_name: 'Pull Request Reviewer',
            status: 'FAILED',
            start_time: daysAgo(2),
            end_time: minutesAgo(2 * 24 * 60 - 1),
          },
          {
            id: 'exec-2',
            flow_id: 'flow-1',
            flow_name: 'Pull Request Reviewer',
            status: 'FAILED',
            start_time: daysAgo(3),
          },
          {
            id: 'exec-3',
            flow_id: 'flow-2',
            flow_name: 'Merge Request Reviewer',
            status: 'FAILED',
            start_time: daysAgo(1),
          },
        ],
      });

      expect(items).to.have.length(2);
      const reviewer = items.find(
        (item) => item.title === 'Pull Request Reviewer'
      )!;
      expect(reviewer.id).to.equal('flow:flow-1');
      expect(reviewer.detail).to.equal(
        '2 failed runs in 3d · latest 2d ago · 1m 0s'
      );
      expect(reviewer.href).to.equal(
        '/console/flows/executions?flow_id=flow-1&status=FAILED'
      );

      const merge = items.find(
        (item) => item.title === 'Merge Request Reviewer'
      )!;
      expect(merge.href).to.equal('/console/flows/executions/exec-3');
    });

    it('ignores succeeded runs and anything older than 7 days', () => {
      const items = derive({
        executions: [
          {
            id: 'exec-ok',
            flow_name: 'Nightly',
            status: 'SUCCEEDED',
            start_time: minutesAgo(30),
          },
          {
            id: 'exec-old',
            flow_name: 'Nightly',
            status: 'FAILED',
            start_time: daysAgo(8),
          },
        ],
      });

      expect(items).to.have.length(0);
    });
  });

  describe('models', () => {
    it('groups gateway failures by model alias', () => {
      const items = derive({
        gatewayFailures: [
          failureFixture({ api_usage_id: 'u1', timestamp: minutesAgo(30) }),
          failureFixture({ api_usage_id: 'u2', timestamp: minutesAgo(5) }),
        ],
      });

      expect(items).to.have.length(1);
      expect(items[0].id).to.equal('model:deepseek/deepseek-v4-pro');
      expect(items[0].detail).to.equal('2 failed requests · last 5m ago');
      expect(items[0].href).to.equal('/console/ai-models');
      expect(items[0].at).to.equal(minutesAgo(5));
    });

    it('deep links to the model when the id is known and falls back to the provider name', () => {
      const items = derive({
        gatewayFailures: [
          failureFixture({ ai_model_id: 'model-9' }),
          failureFixture({
            api_usage_id: 'u3',
            model_alias: null,
            provider_name: 'openai',
          }),
        ],
      });

      const byId = new Map(items.map((item) => [item.id, item]));
      expect(byId.get('model:deepseek/deepseek-v4-pro')?.href).to.equal(
        '/console/ai-models/model-9'
      );
      expect(byId.get('model:openai')).to.exist;
    });

    it('ignores successful interactions', () => {
      const items = derive({
        gatewayFailures: [failureFixture({ outcome: 'success' })],
      });

      expect(items).to.have.length(0);
    });
  });

  describe('budgets', () => {
    const policy = (overrides: Record<string, unknown> = {}): any => ({
      id: 'policy-1',
      subject_type: 'global',
      subject_id: null,
      model_alias: null,
      period: 'monthly',
      hard_limit_usd: 300,
      soft_limit_usd: 200,
      notify_on_soft: true,
      notify_on_hard: true,
      notification_user_ids: null,
      notification_team_ids: null,
      notification_emails: null,
      ...overrides,
    });

    it('is critical at or over the hard limit', () => {
      const items = derive({
        budgetPolicies: [policy({ current_spend_usd: 300 })],
      });

      expect(items).to.have.length(1);
      expect(items[0].severity).to.equal('critical');
      expect(items[0].title).to.equal('Global · monthly');
      expect(items[0].detail).to.equal(
        'Hard limit reached ($300.00 / $300.00)'
      );
      expect(items[0].href).to.equal('/console/attention#budgets');
      expect(items[0].action).to.deep.equal({
        label: 'Configure limits',
        event: 'configure-limits',
      });
    });

    it('is a warning between soft and hard', () => {
      const items = derive({
        budgetPolicies: [policy({ current_spend_usd: 220 })],
      });

      expect(items[0].severity).to.equal('warning');
      expect(items[0].detail).to.equal(
        'Soft limit exceeded ($220.00 / $200.00)'
      );
    });

    it('stays quiet under the soft limit and without a spend figure', () => {
      const items = derive({
        budgetPolicies: [
          policy({ current_spend_usd: 12 }),
          policy({ id: 'policy-2', current_spend_usd: null }),
        ],
      });

      expect(items).to.have.length(0);
    });

    it('names the model scope for model policies', () => {
      const items = derive({
        budgetPolicies: [
          policy({
            subject_type: 'ai_model',
            subject_id: 'model-1',
            model_alias: 'gpt-5.4',
            period: 'daily',
            current_spend_usd: 400,
          }),
        ],
      });

      expect(items[0].title).to.equal('gpt-5.4 · daily');
    });
  });

  describe('pricing', () => {
    const summary = (overrides: Record<string, unknown> = {}): any => ({
      period_start: daysAgo(30),
      period_end: NOW.toISOString(),
      total_requests: 10,
      successful_requests: 10,
      failed_requests: 0,
      token_usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      estimated_cost: 1,
      budget: {
        monthly_limit_usd: null,
        soft_limit_usd: null,
        current_spend_usd: 0,
        soft_limit_exceeded: false,
        hard_limit_exceeded: false,
      },
      requests_by_day: [],
      usage_by_model: [],
      usage_by_flow: [],
      usage_by_session: [],
      ...overrides,
    });

    it('flags a catalog older than 14 days', () => {
      const items = derive({
        usageSummary: summary({
          price_catalog: { fetched_at: daysAgo(30), model_count: 120 },
        }),
      });

      expect(items).to.have.length(1);
      expect(items[0].kind).to.equal('pricing');
      expect(items[0].title).to.equal('Price catalog');
      expect(items[0].detail).to.contain('Price catalog stale since');
      expect(items[0].action).to.deep.equal({
        label: 'Update prices',
        href: '/console/cost?panel=pricing',
      });
    });

    // Staging shape: `price_catalog` is null, so nothing can be costed at all.
    it('names the missing catalog rather than counting unpriced requests', () => {
      const items = derive({
        usageSummary: summary({
          price_catalog: { fetched_at: null, model_count: 0 },
          unpriced_requests: 4,
        }),
      });

      expect(items).to.have.length(1);
      expect(items[0].title).to.equal('No price catalog loaded');
      expect(items[0].detail).to.equal(
        'Estimated spend is missing for 4 requests because no provider price list is loaded.'
      );
      expect(items[0].evidence?.catalogMissing).to.equal(true);
    });

    // The count alone ("336 requests unpriced") is not actionable: the row has
    // to say which models have no price.
    it('lists the models a loaded catalog cannot price', () => {
      const items = derive({
        usageSummary: summary({
          price_catalog: { fetched_at: daysAgo(1), model_count: 120 },
          unpriced_requests: 2161,
          usage_by_model: [
            {
              ai_model_id: 'model-1',
              model_alias: 'openrouter/stealth/ox-alpha',
              provider_name: 'openrouter',
              request_count: 2134,
              token_usage: {
                prompt_tokens: 1,
                completion_tokens: 1,
                total_tokens: 900,
              },
              estimated_cost: 0,
              last_request_at: minutesAgo(10),
            },
            {
              ai_model_id: null,
              model_alias: 'zai/glm-5-3-turbo',
              provider_name: 'zai',
              request_count: 27,
              token_usage: {
                prompt_tokens: 1,
                completion_tokens: 1,
                total_tokens: 40,
              },
              estimated_cost: 0,
              last_request_at: minutesAgo(90),
            },
            {
              ai_model_id: 'model-3',
              model_alias: 'anthropic/claude-4-opus',
              provider_name: 'anthropic',
              request_count: 500,
              token_usage: {
                prompt_tokens: 1,
                completion_tokens: 1,
                total_tokens: 80,
              },
              estimated_cost: 12.5,
              last_request_at: minutesAgo(5),
            },
          ],
        }),
      });

      expect(items).to.have.length(1);
      expect(items[0].title).to.equal('2 models without a price');
      const unpriced = items[0].evidence?.unpricedModels || [];
      expect(unpriced.map((model) => model.alias)).to.eql([
        'openrouter/stealth/ox-alpha',
        'zai/glm-5-3-turbo',
      ]);
      expect(unpriced[0].aiModelId).to.equal('model-1');
      expect(unpriced[0].requests).to.equal(2134);
    });

    it('stays quiet for a fresh catalog with everything priced', () => {
      const items = derive({
        usageSummary: summary({
          price_catalog: { fetched_at: daysAgo(1), model_count: 120 },
          unpriced_requests: 0,
        }),
      });

      expect(items).to.have.length(0);
    });
  });

  describe('evidence', () => {
    it('carries the failed runs and the most common error behind a flow count', () => {
      const items = derive({
        executions: [
          {
            id: 'run-3',
            flow_id: 'flow-1',
            flow_name: 'Nightly Sync',
            status: 'FAILED',
            start_time: minutesAgo(30),
            end_time: minutesAgo(29),
            error_message:
              'Failed to start agent Job: (409) Conflict\n  at runner.py:88',
          },
          {
            id: 'run-2',
            flow_id: 'flow-1',
            flow_name: 'Nightly Sync',
            status: 'FAILED',
            start_time: minutesAgo(90),
            error_message: 'Failed to start agent Job: (409) Conflict',
          },
          {
            id: 'run-1',
            flow_id: 'flow-1',
            flow_name: 'Nightly Sync',
            status: 'FAILED',
            start_time: minutesAgo(200),
            error_message: 'Timed out waiting for tool response',
          },
        ],
      });

      const runs = items[0].evidence?.failedRuns || [];
      expect(runs.map((run) => run.id)).to.eql(['run-3', 'run-2', 'run-1']);
      // Only the first line: stack traces belong in the title attribute.
      expect(runs[0].errorMessage).to.contain('(409) Conflict');
      expect(items[0].evidence?.mostCommonError).to.deep.equal({
        message: 'Failed to start agent Job: (409) Conflict',
        count: 2,
        total: 3,
      });
      expect(items[0].fingerprint).to.equal('run:run-3');
    });

    it('gives an unconnected agent a sentence and the command that fixes it', () => {
      const items = derive({
        agents: [
          agentFixture({
            display_name: 'Researcher',
            onboarding_state: 'incomplete',
          }),
        ],
      });

      const reasons = items[0].evidence?.agentReasons || [];
      expect(reasons).to.have.length(1);
      expect(reasons[0].text).to.contain('Onboarding never completed');
      expect(reasons[0].command).to.equal(
        'preloop agents onboard "Researcher"'
      );
      expect(reasons[0].action).to.deep.equal({
        label: 'Open agent',
        href: '/console/agents/agent-1',
      });
    });

    it('keeps the last five gateway failures for a model', () => {
      const items = derive({
        gatewayFailures: Array.from({ length: 7 }, (_, index) =>
          failureFixture({
            api_usage_id: `usage-${index}`,
            timestamp: minutesAgo(index + 1),
            excerpt: `boom ${index}`,
            runtime_session_id: 'session-9',
          })
        ),
      });

      const failures = items[0].evidence?.modelFailures || [];
      expect(failures).to.have.length(5);
      expect(failures[0].excerpt).to.equal('boom 0');
      expect(failures[0].statusCode).to.equal(500);
      expect(failures[0].sessionId).to.equal('session-9');
    });
  });

  describe('dismissals', () => {
    const flowInputs: AttentionInputs = {
      executions: [
        {
          id: 'run-9',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'FAILED',
          start_time: minutesAgo(30),
          error_message: 'boom',
        },
      ],
    };

    it('hides an item whose fingerprint still matches its dismissal', () => {
      const result = deriveAttentionItems({
        now: NOW,
        ...flowInputs,
        dismissals: [
          {
            item_id: 'flow:flow-1',
            fingerprint: 'run:run-9',
            reason: 'expected',
            created_at: minutesAgo(10),
          },
        ],
      });

      expect(result.items).to.have.length(0);
      expect(result.dismissed).to.have.length(1);
      expect(result.dismissed[0].item.id).to.equal('flow:flow-1');
      expect(result.dismissed[0].dismissal.reason).to.equal('expected');
    });

    it('brings the item back when the fingerprint changes', () => {
      const result = deriveAttentionItems({
        now: NOW,
        ...flowInputs,
        dismissals: [
          {
            item_id: 'flow:flow-1',
            fingerprint: 'run:run-8',
            reason: 'expected',
          },
        ],
      });

      expect(result.items.map((item) => item.id)).to.eql(['flow:flow-1']);
      expect(result.dismissed).to.have.length(0);
    });

    it('ignores a snooze that has run out', () => {
      const result = deriveAttentionItems({
        now: NOW,
        ...flowInputs,
        dismissals: [
          {
            item_id: 'flow:flow-1',
            fingerprint: 'run:run-9',
            reason: 'snoozed',
            snooze_until: minutesAgo(1),
          },
        ],
      });

      expect(result.items).to.have.length(1);
    });

    it('honours a snooze that is still running', () => {
      const result = deriveAttentionItems({
        now: NOW,
        ...flowInputs,
        dismissals: [
          {
            item_id: 'flow:flow-1',
            fingerprint: 'run:run-9',
            reason: 'snoozed',
            snooze_until: new Date(NOW.getTime() + 3 * 86400000).toISOString(),
          },
        ],
      });

      expect(result.items).to.have.length(0);
      expect(result.dismissed).to.have.length(1);
    });

    it('never hides an approval, even with a matching dismissal', () => {
      const approvals = [
        {
          id: 'approval-1',
          tool_name: 'refund_order',
          requested_at: minutesAgo(5),
        },
      ];
      const [item] = deriveAttentionItems({ now: NOW, approvals }).items;
      expect(item.dismissable).to.equal(false);

      const result = deriveAttentionItems({
        now: NOW,
        approvals,
        dismissals: [
          {
            item_id: item.id,
            fingerprint: item.fingerprint,
            reason: 'expected',
          },
        ],
      });

      expect(result.items).to.have.length(1);
    });

    it('fingerprints an agent by its onboarding and validation state', () => {
      const [item] = deriveAttentionItems({
        now: NOW,
        agents: [
          agentFixture({
            onboarding_state: 'incomplete',
            live_validation_status: 'failed',
            last_validated_at: '2026-08-01T00:00:00Z',
          }),
        ],
      }).items;

      expect(item.fingerprint).to.equal(
        'incomplete|failed|2026-08-01T00:00:00Z'
      );
    });
  });

  it('sorts critical items first, then most recent, nulls last', () => {
    const items = derive({
      approvals: [
        { id: 'a-old', tool_name: 'old', requested_at: minutesAgo(120) },
        { id: 'a-new', tool_name: 'new', requested_at: minutesAgo(2) },
      ],
      gatewayFailures: [failureFixture({ timestamp: minutesAgo(1) })],
      budgetPolicies: [
        {
          id: 'policy-soft',
          subject_type: 'global',
          subject_id: null,
          model_alias: null,
          period: 'weekly',
          hard_limit_usd: null,
          soft_limit_usd: 10,
          current_spend_usd: 20,
          notify_on_soft: false,
          notify_on_hard: false,
          notification_user_ids: null,
          notification_team_ids: null,
          notification_emails: null,
        } as any,
      ],
    });

    expect(items.map((item) => item.id)).to.deep.equal([
      'approval:a-new',
      'approval:a-old',
      'model:deepseek/deepseek-v4-pro',
      'budget:policy-soft',
    ]);
  });

  it('returns an empty list when nothing is wrong', () => {
    expect(derive({})).to.deep.equal([]);
  });
});

describe('groupAttentionItems', () => {
  it('groups by kind in section order and omits empty kinds', () => {
    const items = derive({
      approvals: [
        { id: 'a1', tool_name: 'shell.run', requested_at: minutesAgo(3) },
      ],
      agents: [agentFixture({ onboarding_state: 'incomplete' })],
      gatewayFailures: [failureFixture()],
    });

    const grouped = groupAttentionItems(items);
    expect(Array.from(grouped.keys())).to.deep.equal([
      'approval',
      'agent',
      'model',
    ]);
    expect(grouped.get('approval')).to.have.length(1);
    expect(grouped.get('flow')).to.equal(undefined);
  });
});

describe('ATTENTION_KIND_META', () => {
  it('describes every kind in the section order', () => {
    for (const kind of ATTENTION_KIND_ORDER) {
      const meta = ATTENTION_KIND_META[kind];
      expect(meta.label, kind).to.be.a('string');
      expect(meta.plural, kind).to.be.a('string');
      expect(meta.icon, kind).to.be.a('string');
      expect(meta.sectionHref, kind).to.contain('/console');
    }
  });

  it('builds singular and plural chip labels', () => {
    expect(attentionKindChipLabel('approval', 2)).to.equal('2 approvals');
    expect(attentionKindChipLabel('agent', 1)).to.equal('1 agent');
    expect(attentionKindChipLabel('flow', 0)).to.equal('0 flows');
  });
});
