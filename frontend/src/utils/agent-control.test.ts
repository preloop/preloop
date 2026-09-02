import { expect } from '@open-wc/testing';

import type { ManagedAgentSummary } from '../types';
import {
  AGENT_CONTROL_DOCS_URL,
  getAgentControlInstallHint,
  getAgentControlState,
} from './agent-control';

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

describe('getAgentControlState', () => {
  it('shows install_pending without enabling prompts', () => {
    const state = getAgentControlState({
      ...baseAgent,
      control_state: 'install_pending',
      control_enabled: false,
      control_online: false,
      control_capabilities: [],
    });

    expect(state.visible).to.equal(true);
    expect(state.enabled).to.equal(false);
    expect(state.label).to.equal('Install pending');
  });

  it('labels verified offline plugins as configured', () => {
    const state = getAgentControlState({
      ...baseAgent,
      control_state: 'plugin_configured',
      control_enabled: true,
      control_online: false,
      control_capabilities: ['send_text_prompt'],
    });

    expect(state.enabled).to.equal(true);
    expect(state.online).to.equal(false);
    expect(state.label).to.equal('Agent Control configured');
  });

  describe('install hint', () => {
    it('offers the install command for a runtime that supports the plugin', () => {
      const hint = getAgentControlInstallHint({
        ...baseAgent,
        display_name: 'Hermes',
        agent_kind: 'hermes',
        control_state: 'plugin_configured',
        control_enabled: false,
        control_online: false,
      });

      expect(hint.supported).to.equal(true);
      expect(hint.command).to.equal('preloop agents install-plugin "Hermes"');
      expect(hint.placeholder).to.equal(
        'Install Agent Control to talk to Hermes'
      );
      expect(hint.docsUrl).to.equal(AGENT_CONTROL_DOCS_URL);
    });

    it('says the config is written when the plugin has not connected', () => {
      const hint = getAgentControlInstallHint({
        ...baseAgent,
        control_state: 'install_pending',
        control_enabled: false,
      });

      expect(hint.helptext).to.contain('has not connected yet');
      expect(hint.command).to.not.equal(null);
    });

    it('offers no command for a runtime that cannot run the plugin', () => {
      const hint = getAgentControlInstallHint({
        ...baseAgent,
        display_name: 'Claude Desktop',
        agent_kind: 'claude_desktop',
        session_source_type: 'claude_desktop',
        control_state: 'unsupported',
        control_enabled: false,
        control_capabilities: [],
      });

      expect(hint.supported).to.equal(false);
      expect(hint.command).to.equal(null);
      expect(hint.placeholder).to.contain('Agent Control is not available for');
      expect(hint.helptext).to.contain('cannot talk to it');
    });

    it('has something to say with no agent at all', () => {
      const hint = getAgentControlInstallHint(null);
      expect(hint.supported).to.equal(false);
      expect(hint.command).to.equal(null);
    });
  });
});
