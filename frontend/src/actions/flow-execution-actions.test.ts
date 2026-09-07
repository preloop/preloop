import { expect } from '@open-wc/testing';
import { flowExecutionActions } from './flow-execution-actions';
import { actionIds, intersectActions } from './registry';

const ctx = {
  includeOpen: true,
  onCancel: () => {},
  onRetry: () => {},
};

describe('flowExecutionActions', () => {
  it('offers Cancel run while the run is going, and no retry', () => {
    const ids = actionIds(
      flowExecutionActions({ id: 'e1', status: 'RUNNING' }, ctx)
    );
    expect(ids).to.deep.equal(['open', 'cancel']);
  });

  it('offers Retry run on a failed run, and no cancel', () => {
    for (const status of ['FAILED', 'STOPPED', 'TIMEOUT', 'CANCELLED']) {
      const ids = actionIds(flowExecutionActions({ id: 'e1', status }, ctx));
      expect(ids, status).to.deep.equal(['open', 'retry']);
    }
  });

  it('offers neither on a run that succeeded', () => {
    expect(
      actionIds(flowExecutionActions({ id: 'e1', status: 'SUCCEEDED' }, ctx))
    ).to.deep.equal(['open']);
  });

  it('offers Open session only for a run that opened one', () => {
    const withSession = flowExecutionActions(
      { id: 'e1', status: 'SUCCEEDED', agent_session_reference: 'sess-1' },
      ctx
    );
    expect(actionIds(withSession)).to.contain('open-session');
    expect(
      withSession.find((action) => action.id === 'open-session')!.href
    ).to.equal('/console/runtime-sessions?query=e1');
    expect(
      actionIds(flowExecutionActions({ id: 'e1', status: 'SUCCEEDED' }, ctx))
    ).to.not.contain('open-session');
  });

  it('offers View flow on the execution page only, and only with a flow', () => {
    expect(
      actionIds(
        flowExecutionActions(
          { id: 'e1', status: 'SUCCEEDED', flow_id: 'flow-1' },
          { ...ctx, includeOpen: false, includeViewFlow: true }
        )
      )
    ).to.deep.equal(['view-flow']);
    expect(
      actionIds(
        flowExecutionActions(
          { id: 'e1', status: 'SUCCEEDED' },
          { ...ctx, includeOpen: false, includeViewFlow: true }
        )
      )
    ).to.deep.equal([]);
  });

  it('offers a running and a failed run only their common actions', () => {
    const running = flowExecutionActions({ id: 'a', status: 'RUNNING' }, ctx);
    const failed = flowExecutionActions({ id: 'b', status: 'FAILED' }, ctx);
    expect(actionIds(intersectActions([running, failed]))).to.deep.equal([
      'open',
    ]);
  });
});
