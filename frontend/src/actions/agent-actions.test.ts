import { expect } from '@open-wc/testing';
import type { ManagedAgentSummary } from '../types';
import { agentActions } from './agent-actions';
import { actionIds, intersectActions } from './registry';

function makeAgent(
  overrides: Partial<ManagedAgentSummary> = {}
): ManagedAgentSummary {
  return {
    id: 'agent-1',
    runtime_session_id: 'session-1',
    owner_user_id: null,
    owner_username: null,
    owner_email: null,
    display_name: 'Alpha runner',
    session_source_type: 'claude_code',
    session_source_id: 'workspace-1',
    session_reference: 'ref-1',
    enrolled_via: 'runtime_session_token',
    managed_mcp_servers: [],
    lifecycle_state: 'active',
    lifecycle_reason: null,
    lifecycle_updated_at: null,
    is_active_now: true,
    activity_status: 'active_now',
    last_seen_at: '2026-09-01T10:00:00Z',
    started_at: null,
    last_activity_at: null,
    ended_at: null,
    total_requests: 0,
    estimated_cost: 0,
    configured_model_alias: null,
    latest_model_alias: null,
    latest_provider_name: null,
    last_request_at: null,
    mcp_proxy_configured: true,
    model_gateway_configured: true,
    onboarding_state: 'fully_onboarded',
    ...overrides,
  } as ManagedAgentSummary;
}

/** Every handler a list row wires. */
const rowCtx = {
  onTalk: () => {},
  onRename: () => {},
  onEditTags: () => {},
  onChangeOwner: () => {},
  onLifecycle: () => {},
  onRemove: () => {},
};

describe('agentActions', () => {
  it('offers pause and decommission for a running agent', () => {
    expect(actionIds(agentActions(makeAgent(), rowCtx))).to.deep.equal([
      'rename',
      'edit-tags',
      'pause',
      'decommission',
      'remove',
    ]);
  });

  it('offers resume instead of pause for a suspended agent', () => {
    const ids = actionIds(
      agentActions(makeAgent({ lifecycle_state: 'suspended' }), rowCtx)
    );
    expect(ids).to.contain('resume');
    expect(ids).to.not.contain('pause');
    expect(ids).to.contain('decommission');
  });

  it('never offers decommission twice for a decommissioned agent', () => {
    const ids = actionIds(
      agentActions(makeAgent({ lifecycle_state: 'decommissioned' }), rowCtx)
    );
    expect(ids).to.deep.equal(['rename', 'edit-tags', 'resume', 'remove']);
  });

  it('adds Change owner only where an owner can be chosen', () => {
    expect(
      actionIds(agentActions(makeAgent(), { ...rowCtx, canChangeOwner: true }))
    ).to.contain('change-owner');
    expect(actionIds(agentActions(makeAgent(), rowCtx))).to.not.contain(
      'change-owner'
    );
  });

  it('leads with Talk for an agent with Agent Control, disabled while offline', () => {
    const connected = agentActions(
      makeAgent({
        control_state: 'plugin_connected',
        control_enabled: true,
        control_online: true,
        control_capabilities: ['send_text_prompt'],
      } as Partial<ManagedAgentSummary>),
      rowCtx
    );
    expect(connected[0].id).to.equal('talk');
    expect(connected[0].disabled).to.equal(false);

    const pending = agentActions(
      makeAgent({
        control_state: 'install_pending',
      } as Partial<ManagedAgentSummary>),
      rowCtx
    );
    const talk = pending.find((action) => action.id === 'talk');
    expect(talk, 'a pending install still says Talk, disabled').to.exist;
    expect(talk!.disabled).to.equal(true);

    expect(
      actionIds(agentActions(makeAgent(), rowCtx)),
      'a runtime without Agent Control gets no Talk'
    ).to.not.contain('talk');
  });

  it('keeps Remove last, outlined and set apart (DESIGN.md)', () => {
    const actions = agentActions(makeAgent(), rowCtx);
    const decommission = actions.find(
      (action) => action.id === 'decommission'
    )!;
    expect(decommission.separated).to.equal(true);
    expect(decommission.variant).to.equal('danger');
    expect(decommission.outline).to.equal(true);
    const remove = actions[actions.length - 1];
    expect(remove.id).to.equal('remove');
    expect(remove.variant).to.equal('danger');
    expect(remove.outline).to.equal(true);
    expect(remove.separated).to.equal(true);
  });

  it('offers only what a surface wired handlers for', () => {
    expect(
      actionIds(agentActions(makeAgent(), { onLifecycle: () => {} }))
    ).to.deep.equal(['pause', 'decommission']);
  });

  it('offers a running and a stopped agent only their common actions', () => {
    const running = agentActions(makeAgent({ id: 'a' }), rowCtx);
    const stopped = agentActions(
      makeAgent({ id: 'b', lifecycle_state: 'suspended' }),
      rowCtx
    );
    expect(actionIds(intersectActions([running, stopped]))).to.deep.equal([
      'rename',
      'edit-tags',
      'decommission',
      'remove',
    ]);
  });

  it('leaves two running agents every lifecycle move they share', () => {
    const one = agentActions(makeAgent({ id: 'a' }), rowCtx);
    const two = agentActions(makeAgent({ id: 'b' }), rowCtx);
    expect(actionIds(intersectActions([one, two]))).to.contain('pause');
  });
});
