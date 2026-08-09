import { expect } from '@open-wc/testing';

import type { ManagedAgentSummary } from '../types';
import {
  getAgentLifecycleLabel,
  getAgentLifecycleVariant,
  getSystemAgentTags,
  getVisibleAgentTags,
  isSystemAgentTag,
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
