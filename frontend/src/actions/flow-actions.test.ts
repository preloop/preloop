import { expect } from '@open-wc/testing';
import { flowActions } from './flow-actions';
import { actionIds, intersectActions } from './registry';

const ctx = {
  includeOpen: true,
  onRun: () => {},
  onToggleEnabled: () => {},
  onDelete: () => {},
};

describe('flowActions', () => {
  it('offers Run now on an enabled flow', () => {
    const actions = flowActions({ id: 'f1', is_enabled: true }, ctx);
    expect(actionIds(actions)).to.deep.equal([
      'open',
      'run-now',
      'edit',
      'toggle-enabled',
      'delete',
    ]);
    expect(actions.find((a) => a.id === 'run-now')!.disabled).to.equal(false);
    expect(actions.find((a) => a.id === 'toggle-enabled')!.label).to.equal(
      'Pause'
    );
  });

  it('keeps Run now visible but disabled on a paused flow', () => {
    const actions = flowActions({ id: 'f1', is_enabled: false }, ctx);
    const run = actions.find((action) => action.id === 'run-now')!;
    expect(run.disabled).to.equal(true);
    expect(run.tooltip).to.equal('Resume the flow before running it');
    expect(actions.find((a) => a.id === 'toggle-enabled')!.label).to.equal(
      'Resume'
    );
  });

  it('links Edit to the flow page in edit mode when no handler is wired', () => {
    const [edit] = flowActions({ id: 'f 1', is_enabled: true }, {}).filter(
      (action) => action.id === 'edit'
    );
    expect(edit.href).to.equal('/console/flows/f%201?edit=true');
  });

  it('drops Open on the flow page itself', () => {
    expect(
      actionIds(
        flowActions(
          { id: 'f1', is_enabled: true },
          { ...ctx, includeOpen: false }
        )
      )
    ).to.not.contain('open');
  });

  it('keeps Delete last, outlined and set apart', () => {
    const actions = flowActions({ id: 'f1', is_enabled: true }, ctx);
    const remove = actions[actions.length - 1];
    expect(remove.id).to.equal('delete');
    expect(remove.variant).to.equal('danger');
    expect(remove.outline).to.equal(true);
    expect(remove.separated).to.equal(true);
  });

  it('offers a paused and a running flow only their common actions', () => {
    const running = flowActions({ id: 'a', is_enabled: true }, ctx);
    const paused = flowActions({ id: 'b', is_enabled: false }, ctx);
    // Run now survives nothing: it is disabled on the paused flow.
    expect(actionIds(intersectActions([running, paused]))).to.deep.equal([
      'open',
      'edit',
      'toggle-enabled',
      'delete',
    ]);
  });
});
