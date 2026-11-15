import { expect } from '@open-wc/testing';
import {
  confirmRetryExecution,
  retryConfirmDetail,
  executionSessionUrl,
  flowExecutionActions,
} from './flow-execution-actions';
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

  it('offers Cancel run on a run that has not started yet', () => {
    // The founder-reported case: a queued run, nothing dispatched, and no way
    // to call it off. The stop command accepts all four of these.
    for (const status of ['PENDING', 'INITIALIZING', 'STARTING', 'RUNNING']) {
      const ids = actionIds(flowExecutionActions({ id: 'e1', status }, ctx));
      expect(ids, status).to.deep.equal(['open', 'cancel']);
    }
  });

  it('offers Retry run on a failed run, and no cancel', () => {
    for (const status of ['FAILED', 'STOPPED', 'TIMEOUT', 'CANCELLED']) {
      const ids = actionIds(flowExecutionActions({ id: 'e1', status }, ctx));
      expect(ids, status).to.deep.equal(['open', 'retry']);
    }
  });

  it('does not offer standalone retry for a controller-owned continuation', () => {
    const ids = actionIds(
      flowExecutionActions(
        {
          id: 'repair',
          status: 'FAILED',
          trigger_event_details: { _resume: { thread_id: 'native-thread' } },
        },
        ctx
      )
    );
    expect(ids).to.deep.equal(['open']);
  });

  it('hides retry on lightweight list rows carrying resume lineage', () => {
    expect(
      actionIds(
        flowExecutionActions(
          {
            id: 'repair',
            status: 'FAILED',
            resume_of: 'publisher',
          },
          ctx
        )
      )
    ).to.deep.equal(['open']);
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

  it('lets a surface say where Open session goes', () => {
    const actions = flowExecutionActions(
      { id: 'e1', status: 'SUCCEEDED', agent_session_reference: 'sess-1' },
      { ...ctx, sessionHref: (item) => `/runs/${item.id}?tab=transcript` }
    );
    expect(actions.find((a) => a.id === 'open-session')!.href).to.equal(
      '/runs/e1?tab=transcript'
    );
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

  it('warns that a retry repeats a credential failure', () => {
    const detail = retryConfirmDetail({
      agent_type: 'codex',
      model_name: 'gpt-5',
      failure_category: 'repository_access',
    });
    expect(detail).to.contain('retry repeats');
    expect(detail).to.contain('repository access');
    expect(detail).to.contain('tracker token');
  });

  it('keeps the plain retry wording for a transient failure', () => {
    const detail = retryConfirmDetail({
      agent_type: 'codex',
      model_name: 'gpt-5',
      failure_category: 'model_transient',
    });
    expect(detail).to.equal(
      'The retry will run with codex using gpt-5, re-resolved from the current flow definition.'
    );
  });

  it('exposes confirmRetryExecution function', () => {
    expect(confirmRetryExecution).to.be.a('function');
  });
});

describe('persistent execution session link', () => {
  it('links to the tracking session instead of a control command id', () => {
    expect(
      executionSessionUrl({
        id: 'execution-example',
        status: 'RUNNING',
        agent_session_reference: 'control:agent-example:command-example',
        trigger_event_details: {
          _agent_control: {
            history_session_id: 'session-example',
            runtime_session_id: 'live-example',
          },
        },
      })
    ).to.equal('/console/runtime-sessions?query=session-example');
  });
});
