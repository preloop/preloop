import { expect } from '@open-wc/testing';

import type { ManagedAgentSummary } from '../types';
import {
  getAgentLifecycleLabel,
  getAgentLifecycleVariant,
  getAgentStatusChip,
  getSystemAgentTags,
  getVisibleAgentTags,
  isSystemAgentTag,
  sessionBelongsToAgent,
} from './agent-display';

const baseAgent = {
  id: 'agent-1',
  runtime_session_id: null,
  owner_user_id: null,
  owner_username: null,
  owner_email: null,
  display_name: 'OpenClaw',
  session_source_type: 'openclaw',
  session_source_id: 'openclaw-1',
  session_reference: null,
  enrolled_via: 'cli',
  managed_mcp_servers: [],
  lifecycle_state: 'active',
  lifecycle_reason: null,
  lifecycle_updated_at: null,
  is_active_now: false,
  activity_status: 'idle',
  last_seen_at: new Date().toISOString(),
  started_at: null,
  last_activity_at: null,
  ended_at: null,
  total_requests: 0,
  estimated_cost: 0,
  configured_model_alias: null,
  latest_model_alias: null,
  latest_provider_name: null,
  last_request_at: null,
  mcp_proxy_configured: false,
  model_gateway_configured: false,
  onboarding_state: 'incomplete',
  live_validation_supported: false,
  live_validation_passed: null,
  live_validation_status: 'unsupported',
  last_validated_at: null,
} satisfies ManagedAgentSummary;

describe('agent lifecycle presentation', () => {
  it('labels a suspended agent as Paused, not Halted', () => {
    const agent = { ...baseAgent, lifecycle_state: 'suspended' };

    expect(getAgentLifecycleLabel(agent)).to.equal('Paused');
  });

  it('renders pause in warning tones so danger stays for offboarding', () => {
    expect(
      getAgentLifecycleVariant({ ...baseAgent, lifecycle_state: 'suspended' })
    ).to.equal('warning');
    expect(
      getAgentLifecycleVariant({
        ...baseAgent,
        lifecycle_state: 'decommissioned',
      })
    ).to.equal('danger');
  });
});

describe('getAgentStatusChip', () => {
  const onboarded = {
    ...baseAgent,
    onboarding_state: 'fully_onboarded',
  } satisfies ManagedAgentSummary;

  it('puts lifecycle ahead of everything else', () => {
    expect(
      getAgentStatusChip({
        ...onboarded,
        lifecycle_state: 'decommissioned',
        is_active_now: true,
      })
    ).to.deep.equal({ label: 'Decommissioned', variant: 'neutral' });
    expect(
      getAgentStatusChip({
        ...onboarded,
        lifecycle_state: 'suspended',
        live_validation_status: 'failed',
      })
    ).to.deep.equal({ label: 'Paused', variant: 'neutral' });
  });

  it('reports a failed live check ahead of onboarding and activity', () => {
    expect(
      getAgentStatusChip({
        ...baseAgent,
        live_validation_status: 'failed',
        is_active_now: true,
      })
    ).to.deep.equal({ label: 'Live check failed', variant: 'warning' });
  });

  it('flags an agent that was never connected, and only that one', () => {
    expect(
      getAgentStatusChip({
        ...baseAgent,
        onboarding_state: 'incomplete',
        is_active_now: true,
      })
    ).to.deep.equal({ label: 'Not connected', variant: 'warning' });
  });

  it('reads partial onboarding as a configuration, not a fault', () => {
    expect(
      getAgentStatusChip({
        ...baseAgent,
        onboarding_state: 'mcp_proxy_only',
        is_active_now: true,
      })
    ).to.deep.equal({
      label: 'MCP only',
      variant: 'neutral',
      tooltip: 'Tool firewall configured, model gateway not used',
    });
    expect(
      getAgentStatusChip({
        ...baseAgent,
        onboarding_state: 'gateway_only',
        is_active_now: true,
      })
    ).to.deep.equal({
      label: 'Gateway only',
      variant: 'neutral',
      tooltip: 'Model gateway configured, tool firewall not used',
    });
  });

  it('never turns a long idle agent amber', () => {
    expect(
      getAgentStatusChip({
        ...onboarded,
        last_seen_at: '2026-01-01T00:00:00Z',
        activity_status: 'inactive',
      })
    ).to.deep.equal({ label: 'Idle', variant: 'neutral' });
  });

  it('shows Active now for a live agent and an outlined chip for a recent one', () => {
    expect(
      getAgentStatusChip({ ...onboarded, is_active_now: true })
    ).to.deep.equal({ label: 'Active now', variant: 'success' });
    expect(
      getAgentStatusChip({ ...onboarded, activity_status: 'recently_active' })
    ).to.deep.equal({
      label: 'Recently active',
      variant: 'success',
      outline: true,
    });
  });

  it('falls back to Idle', () => {
    expect(getAgentStatusChip(onboarded)).to.deep.equal({
      label: 'Idle',
      variant: 'neutral',
    });
  });

  it('does not treat an unsupported or passing live check as a failure', () => {
    expect(getAgentStatusChip({ ...onboarded }).label).to.equal('Idle');
    expect(
      getAgentStatusChip({ ...onboarded, live_validation_status: 'passed' })
        .label
    ).to.equal('Idle');
    expect(
      getAgentStatusChip({ ...onboarded, live_validation_status: 'not_run' })
        .label
    ).to.equal('Idle');
  });
});

describe('agent tag visibility', () => {
  const tags = {
    team: 'platform',
    'identity.previous_ids': 'openclaw-0,openclaw-00',
  };

  it('treats the identity namespace as server bookkeeping', () => {
    expect(isSystemAgentTag('identity.previous_ids')).to.equal(true);
    expect(isSystemAgentTag('team')).to.equal(false);
  });

  it('hides identity tags from the default chip row', () => {
    expect(getVisibleAgentTags(tags)).to.deep.equal([['team', 'platform']]);
  });

  it('keeps identity tags so a tag edit cannot drop them', () => {
    expect(getSystemAgentTags(tags)).to.deep.equal({
      'identity.previous_ids': 'openclaw-0,openclaw-00',
    });
  });

  it('tolerates agents without tags', () => {
    expect(getVisibleAgentTags(null)).to.deep.equal([]);
    expect(getSystemAgentTags(undefined)).to.deep.equal({});
  });
});

describe('sessionBelongsToAgent', () => {
  const agent = { id: 'agent-1', session_source_id: 'openclaw-1' };

  it('matches the agent source id on either session identifier', () => {
    expect(
      sessionBelongsToAgent(
        { runtime_principal_id: 'openclaw-1', session_source_id: null },
        agent
      )
    ).to.equal(true);
    expect(
      sessionBelongsToAgent(
        { runtime_principal_id: null, session_source_id: 'openclaw-1' },
        agent
      )
    ).to.equal(true);
  });

  it('matches the agent id itself', () => {
    expect(
      sessionBelongsToAgent(
        { runtime_principal_id: 'agent-1', session_source_id: null },
        agent
      )
    ).to.equal(true);
  });

  it('matches per-run suffixes of the source id', () => {
    expect(
      sessionBelongsToAgent(
        { runtime_principal_id: 'openclaw-1:2', session_source_id: null },
        agent
      )
    ).to.equal(true);
    expect(
      sessionBelongsToAgent(
        { runtime_principal_id: 'openclaw-1-cli', session_source_id: null },
        agent
      )
    ).to.equal(true);
  });

  it('rejects an unrelated session', () => {
    expect(
      sessionBelongsToAgent(
        { runtime_principal_id: 'other-agent', session_source_id: 'other' },
        agent
      )
    ).to.equal(false);
  });
});
