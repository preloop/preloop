import { expect } from '@open-wc/testing';
import { usageForOwner, type UsageAgent } from './user-usage';
import type { GatewayUsageByAgent } from '../types';

const agents: UsageAgent[] = [
  { id: 'agent-1', owner_user_id: 'user-1' },
  { id: 'agent-2', owner_user_id: 'user-2' },
  { id: 'agent-3', owner_user_id: 'user-1' },
];

function row(agentId: string, cost: number, tokens = 100): GatewayUsageByAgent {
  return {
    agent_id: agentId,
    session_count: 1,
    request_count: 1,
    successful_requests: 1,
    failed_requests: 0,
    token_usage: {
      prompt_tokens: tokens * 0.6,
      completion_tokens: tokens * 0.4,
      total_tokens: tokens,
    },
    estimated_cost: cost,
  } as GatewayUsageByAgent;
}

describe('user-usage', () => {
  it('attributes spend to a person only through the agents they own', () => {
    const usage = [row('agent-1', 0.5), row('agent-3', 1), row('agent-2', 9)];
    const mine = usageForOwner('user-1', usage, agents);
    expect(mine.agents).to.equal(2);
    expect(mine.tokens).to.equal(200);
    expect(mine.cost).to.equal(1.5);
    expect(mine.tokenUsage?.total_tokens).to.equal(200);
    const nobody = usageForOwner('user-9', usage, agents);
    expect(nobody.agents).to.equal(0);
    expect(nobody.tokens).to.equal(0);
    expect(nobody.tokenUsage).to.equal(null);
    expect(nobody.cost).to.equal(0);
    expect(nobody.subscription).to.equal(null);
  });

  // D-05: the person surfaces show the without-subscriptions estimate for
  // their agents together, with the same coverage rules as an agent.
  it('adds up the owned agents subscription blocks into one estimate', () => {
    const block = (requests: number, covered: number, equivalent: number) => ({
      request_count: requests,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      api_equivalent_cost: equivalent,
      api_equivalent_cost_is_estimate: true as const,
      covered_requests: covered,
      coverage: covered / requests,
      billed: null,
      billed_available: false as const,
    });
    const usage = [
      { ...row('agent-1', 0.5), subscription_usage: block(4, 4, 2) },
      { ...row('agent-3', 1), subscription_usage: block(2, 1, 0.5) },
      { ...row('agent-2', 9), subscription_usage: block(9, 9, 90) },
    ];
    const mine = usageForOwner('user-1', usage, agents);
    expect(mine.cost).to.equal(1.5);
    expect(mine.subscription?.request_count).to.equal(6);
    expect(mine.subscription?.covered_requests).to.equal(5);
    expect(mine.withoutSubscriptions.value).to.equal(4);
    expect(mine.withoutSubscriptions.partial).to.equal(true);
    expect(mine.withoutSubscriptions.text).to.equal('at least $4.00');
    expect(mine.withoutSubscriptions.title).to.contain('5 of 6');
    const none = usageForOwner('user-1', [row('agent-1', 0.5)], agents);
    expect(none.subscription).to.equal(null);
    expect(none.withoutSubscriptions.text).to.equal('$0.50');
  });

  it('counts an owned agent with no row as no usage, not an error', () => {
    const mine = usageForOwner('user-1', [row('agent-1', 0.5)], agents);
    expect(mine.agents).to.equal(2);
    expect(mine.cost).to.equal(0.5);
  });
});
