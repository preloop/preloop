import { expect } from '@open-wc/testing';

import {
  approvalRequesterName,
  formatApprovalRequester,
  formatRepositoryChip,
  getApprovalSource,
  getRepositoryContext,
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

  it('shortens the agent id rather than saying "AI agent"', () => {
    // A deleted agent (or a server that predates the resolved summary) leaves
    // the id as the only fact. The attribution line prints "Agent 3f2a9c14",
    // so the chip beside it must not print a generic label instead.
    expect(
      approvalRequesterName({
        managed_agent_id: '3f2a9c14-6b7d-4e58-9a01-77b1c0d2e3f4',
        managed_agent_name: null,
        tool_args: {},
      })
    ).to.equal('3f2a9c14');
    expect(
      approvalRequesterName({
        agent: { id: '3f2a9c14-6b7d-4e58-9a01-77b1c0d2e3f4' },
        tool_args: { _preloop_source: 'claude_code' },
      })
    ).to.equal('3f2a9c14 via Claude Code');
  });

  it('keeps adapter metadata out of tool arguments', () => {
    const toolArgs = {
      command: 'git status',
      _preloop_source: 'cursor',
      _preloop_repository: {
        remote: 'github.com/example/repo',
        toplevel: '/tmp/example',
        relative_path: '',
        source: 'hook_cwd',
      },
    };
    expect(getApprovalSource(toolArgs)).to.equal('cursor');
    expect(withoutApprovalMetadata(toolArgs)).to.deep.equal({
      command: 'git status',
    });
  });

  it('formats a repository chip from the hook marker', () => {
    expect(
      formatRepositoryChip({
        remote: 'github.com/example/repo',
        toplevel: '/tmp/example',
        relative_path: 'sub/dir',
        source: 'hook_cwd',
      })
    ).to.deep.equal({
      label: 'example/repo · sub/dir',
      title: 'github.com/example/repo\n/tmp/example',
    });
    expect(
      formatRepositoryChip({
        remote: '',
        toplevel: '/tmp/example',
        relative_path: '',
        no_remote: true,
      })
    ).to.deep.equal({
      label: 'no remote',
      title: '/tmp/example',
    });
    expect(formatRepositoryChip(null)).to.equal(null);
    expect(
      getRepositoryContext({
        command: 'ls',
        _preloop_repository: {
          remote: 'github.com/example/repo',
          toplevel: '/tmp/example',
        },
      })?.remote
    ).to.equal('github.com/example/repo');
  });
});
