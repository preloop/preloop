import { expect } from '@open-wc/testing';
import type { GatewayUsageBySession } from '../types';
import {
  agentForSourceId,
  agentForUsageSession,
  usageByAgent,
  usageForOwner,
  type UsageAgent,
} from './user-usage';

const agents: UsageAgent[] = [
  {
    id: 'agent-1',
    session_source_id: 'custom_abc',
    runtime_session_id: null,
    owner_user_id: 'user-1',
  },
  {
    id: 'agent-2',
    session_source_id: 'custom_abc_long',
    runtime_session_id: 'rs-2',
    owner_user_id: 'user-2',
  },
  {
    id: 'agent-3',
    session_source_id: 'hermes-01',
    runtime_session_id: null,
    owner_user_id: 'user-1',
  },
];

function session(
  overrides: Partial<GatewayUsageBySession>
): GatewayUsageBySession {
  return {
    flow_execution_id: null,
    flow_id: null,
    flow_name: null,
    session_reference: null,
    model_alias: null,
    provider_name: null,
    request_count: 1,
    token_usage: {
      prompt_tokens: 60,
      completion_tokens: 40,
      total_tokens: 100,
    },
    estimated_cost: 0.5,
    ...overrides,
  } as GatewayUsageBySession;
}

describe('user-usage', () => {
  it('matches exact ids before per-run prefixes, longest base first', () => {
    expect(agentForSourceId('agent-2', agents)?.id).to.equal('agent-2');
    expect(agentForSourceId('rs-2', agents)?.id).to.equal('agent-2');
    expect(agentForSourceId('custom_abc:run-1', agents)?.id).to.equal(
      'agent-1'
    );
    expect(agentForSourceId('custom_abc_long:run-1', agents)?.id).to.equal(
      'agent-2'
    );
    expect(agentForSourceId('hermes-01-123', agents)?.id).to.equal('agent-3');
    expect(agentForSourceId('unknown', agents)).to.equal(undefined);
    expect(agentForSourceId(null, agents)).to.equal(undefined);
  });

  it('reads a session by agent, runtime session, source and principal', () => {
    expect(
      agentForUsageSession(
        session({ runtime_principal_id: 'hermes-01-x' }),
        agents
      )?.id
    ).to.equal('agent-3');
    expect(agentForUsageSession(session({}), agents)).to.equal(undefined);
  });

  it('sums tokens and spend per agent', () => {
    const totals = usageByAgent(
      [
        session({ agent_id: 'agent-1' }),
        session({
          session_source_id: 'custom_abc:run-2',
          estimated_cost: 0.25,
        }),
        session({ agent_id: 'nobody' }),
      ],
      agents
    );
    expect(totals.get('agent-1')?.tokens).to.equal(200);
    expect(totals.get('agent-1')?.cost).to.equal(0.75);
    expect(totals.get('agent-1')?.tokenUsage?.total_tokens).to.equal(200);
    expect(totals.has('agent-2')).to.equal(false);
  });

  it('attributes spend to a person only through the agents they own', () => {
    const sessions = [
      session({ agent_id: 'agent-1' }),
      session({ agent_id: 'agent-3', estimated_cost: 1 }),
      session({ agent_id: 'agent-2', estimated_cost: 9 }),
    ];
    const mine = usageForOwner('user-1', sessions, agents);
    expect(mine.agents).to.equal(2);
    expect(mine.tokens).to.equal(200);
    expect(mine.cost).to.equal(1.5);
    const nobody = usageForOwner('user-9', sessions, agents);
    expect(nobody).to.deep.equal({
      agents: 0,
      tokens: 0,
      tokenUsage: null,
      cost: 0,
    });
  });
});
