import { expect } from '@open-wc/testing';

import {
  approvalRequesterName,
  formatApprovalRepository,
  formatApprovalRequester,
  getApprovalRepository,
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
    const toolArgs = { command: 'git status', _preloop_source: 'cursor' };
    expect(getApprovalSource(toolArgs)).to.equal('cursor');
    expect(withoutApprovalMetadata(toolArgs)).to.deep.equal({
      command: 'git status',
    });
  });

  it('reads the repository marker and shortens it to owner/repo', () => {
    const repository = getApprovalRepository({
      _preloop_repository: {
        remote: 'github.com/example/repo',
        toplevel: '/home/dev/repo',
        relative_path: 'pkg/sub',
        source: 'hook_cwd',
      },
    });
    expect(repository).to.not.equal(null);
    expect(formatApprovalRepository(repository)).to.equal('example/repo');
    expect(repository!.relative_path).to.equal('pkg/sub');
  });

  it('keeps nested groups and drops the host', () => {
    const repository = getApprovalRepository({
      _preloop_repository: { remote: 'gitlab.com/group/sub/repo' },
    });
    expect(formatApprovalRepository(repository)).to.equal('group/sub/repo');
  });

  it('flags a work tree with no remote', () => {
    const repository = getApprovalRepository({
      _preloop_repository: { remote: '', no_remote: true },
    });
    expect(repository?.no_remote).to.equal(true);
    expect(formatApprovalRepository(repository)).to.equal(null);
  });

  it('returns null for a missing or malformed repository marker', () => {
    expect(getApprovalRepository({})).to.equal(null);
    expect(getApprovalRepository({ _preloop_repository: null })).to.equal(null);
    expect(
      getApprovalRepository({ _preloop_repository: ['not', 'an', 'object'] })
    ).to.equal(null);
    expect(getApprovalRepository({ _preloop_repository: {} })).to.equal(null);
  });

  it('keeps repository metadata out of tool arguments too', () => {
    const toolArgs = {
      command: 'git status',
      _preloop_source: 'cursor',
      _preloop_repository: { remote: 'github.com/example/repo' },
    };
    expect(withoutApprovalMetadata(toolArgs)).to.deep.equal({
      command: 'git status',
    });
  });
});
