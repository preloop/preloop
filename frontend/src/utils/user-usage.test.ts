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
    expect(nobody).to.deep.equal({
      agents: 0,
      tokens: 0,
      tokenUsage: null,
      cost: 0,
    });
  });

  it('counts an owned agent with no row as no usage, not an error', () => {
    const mine = usageForOwner('user-1', [row('agent-1', 0.5)], agents);
    expect(mine.agents).to.equal(2);
    expect(mine.cost).to.equal(0.5);
  });
});
