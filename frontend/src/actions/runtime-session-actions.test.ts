import { expect } from '@open-wc/testing';
import { runtimeSessionActions } from './runtime-session-actions';
import { actionIds, intersectActions } from './registry';

const ctx = { includeOpen: true, onOpen: () => {}, onEnd: () => {} };

describe('runtimeSessionActions', () => {
  it('offers End session while the session is live', () => {
    expect(actionIds(runtimeSessionActions({ id: 's1' }, ctx))).to.deep.equal([
      'open',
      'end-session',
    ]);
  });

  it('drops End session once the session ended', () => {
    expect(
      actionIds(
        runtimeSessionActions(
          { id: 's1', ended_at: '2026-09-01T10:00:00Z' },
          ctx
        )
      )
    ).to.deep.equal(['open']);
  });

  it('drops End session once the status says ended, with no end time', () => {
    expect(
      actionIds(runtimeSessionActions({ id: 's1', status: 'ended' }, ctx))
    ).to.deep.equal(['open']);
  });

  it('keeps End session on a quiet session that has not ended', () => {
    expect(
      actionIds(runtimeSessionActions({ id: 's1', status: 'idle' }, ctx))
    ).to.deep.equal(['open', 'end-session']);
  });

  it('links to the flow run only when the session came from one', () => {
    const actions = runtimeSessionActions(
      { id: 's1', flow_execution_id: 'exec-1' },
      ctx
    );
    expect(actionIds(actions)).to.contain('view-flow-execution');
    expect(
      actions.find((action) => action.id === 'view-flow-execution')!.href
    ).to.equal('/console/flows/executions/exec-1');
  });

  it('keeps End session outlined and set apart', () => {
    const actions = runtimeSessionActions({ id: 's1' }, ctx);
    const end = actions[actions.length - 1];
    expect(end.id).to.equal('end-session');
    expect(end.variant).to.equal('danger');
    expect(end.outline).to.equal(true);
    expect(end.separated).to.equal(true);
  });

  it('offers a live and an ended session only their common actions', () => {
    const live = runtimeSessionActions({ id: 'a' }, ctx);
    const ended = runtimeSessionActions(
      { id: 'b', ended_at: '2026-09-01T10:00:00Z' },
      ctx
    );
    expect(actionIds(intersectActions([live, ended]))).to.deep.equal(['open']);
  });
});
