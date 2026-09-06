import { expect } from '@open-wc/testing';

import {
  approvalRequesterName,
  formatApprovalRequester,
  getApprovalSource,
  withoutApprovalMetadata,
} from './approval-identity';

describe('approval identity', () => {
  it('labels a managed agent with its known adapter', () => {
    expect(
      formatApprovalRequester('Release Bot', {
        _preloop_source: 'claude_code',
      })
    ).to.equal('Release Bot via Claude Code');
  });

  it('uses the adapter when no managed name is available', () => {
    expect(
      formatApprovalRequester(null, { _preloop_source: 'cursor' })
    ).to.equal('Cursor');
  });

  it('labels the OpenCode plugin adapter', () => {
    expect(
      formatApprovalRequester('Laptop OpenCode', {
        _preloop_source: 'opencode',
      })
    ).to.equal('Laptop OpenCode via OpenCode');
    expect(
      formatApprovalRequester(null, { _preloop_source: 'opencode' })
    ).to.equal('OpenCode');
  });

  it('prefers the server-resolved agent name over the stored one', () => {
    expect(
      approvalRequesterName({
        agent: { name: 'Claude Code (laptop)' },
        managed_agent_name: null,
        tool_args: {},
      })
    ).to.equal('Claude Code (laptop)');
  });

  it('still says "AI agent" only when nothing at all names the caller', () => {
    expect(approvalRequesterName({})).to.equal('AI agent');
    expect(
      approvalRequesterName({ tool_args: { _preloop_source: 'cursor' } })
    ).to.equal('Cursor');
  });

  it('keeps adapter metadata out of tool arguments', () => {
    const toolArgs = { command: 'git status', _preloop_source: 'cursor' };
    expect(getApprovalSource(toolArgs)).to.equal('cursor');
    expect(withoutApprovalMetadata(toolArgs)).to.deep.equal({
      command: 'git status',
    });
  });
});
