import { expect } from '@open-wc/testing';

import {
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

  it('keeps adapter metadata out of tool arguments', () => {
    const toolArgs = { command: 'git status', _preloop_source: 'cursor' };
    expect(getApprovalSource(toolArgs)).to.equal('cursor');
    expect(withoutApprovalMetadata(toolArgs)).to.deep.equal({
      command: 'git status',
    });
  });
});
