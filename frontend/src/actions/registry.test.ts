import { expect } from '@open-wc/testing';
import {
  actionIds,
  defineActions,
  intersectActions,
  offersAction,
} from './registry';

interface Thing {
  state: 'running' | 'stopped';
}

const noop = () => {};

describe('defineActions', () => {
  it('drops actions the state does not allow', () => {
    const actions = defineActions({ state: 'stopped' } as Thing, [
      {
        id: 'stop',
        label: 'Stop',
        available: (t) => t.state === 'running',
        onClick: noop,
      },
      {
        id: 'start',
        label: 'Start',
        available: (t) => t.state === 'stopped',
        onClick: noop,
      },
      { id: 'rename', label: 'Rename', onClick: noop },
    ]);
    expect(actionIds(actions)).to.deep.equal(['start', 'rename']);
  });

  it('keeps a visibleWhenUnavailable action disabled with its tooltip', () => {
    const [action] = defineActions({ state: 'stopped' } as Thing, [
      {
        id: 'run',
        label: 'Run now',
        available: (t) => t.state === 'running',
        visibleWhenUnavailable: true,
        unavailableTooltip: 'Resume it first',
        onClick: noop,
      },
    ]);
    expect(action.id).to.equal('run');
    expect(action.disabled).to.equal(true);
    expect(action.tooltip).to.equal('Resume it first');
  });

  it('drops an action no surface wired a handler for', () => {
    const actions = defineActions({ state: 'running' } as Thing, [
      { id: 'talk', label: 'Talk' },
      { id: 'open', label: 'Open', href: '/somewhere' },
    ]);
    expect(actionIds(actions)).to.deep.equal(['open']);
  });

  it('moves destructive actions to the end whatever the declared order', () => {
    const actions = defineActions({ state: 'running' } as Thing, [
      { id: 'remove', label: 'Remove', separated: true, onClick: noop },
      { id: 'rename', label: 'Rename', onClick: noop },
    ]);
    expect(actionIds(actions)).to.deep.equal(['rename', 'remove']);
  });

  it('skips falsy candidates so surfaces can opt out inline', () => {
    const actions = defineActions({ state: 'running' } as Thing, [
      null,
      false,
      undefined,
      { id: 'rename', label: 'Rename', onClick: noop },
    ]);
    expect(actionIds(actions)).to.deep.equal(['rename']);
  });

  it('hides actions the operator has no permission for', () => {
    const candidates = [
      {
        id: 'remove',
        label: 'Remove',
        permission: 'manage_agents',
        onClick: noop,
      },
      { id: 'view', label: 'View', href: '/x' },
    ];
    expect(
      actionIds(
        defineActions({ state: 'running' } as Thing, candidates, {
          permissions: ['view_agents'],
        })
      )
    ).to.deep.equal(['view']);
    // RBAC off (null permissions) is unrestricted.
    expect(
      actionIds(
        defineActions({ state: 'running' } as Thing, candidates, {
          permissions: null,
        })
      )
    ).to.deep.equal(['remove', 'view']);
  });

  it('explains a permission it keeps on screen', () => {
    const [action] = defineActions(
      { state: 'running' } as Thing,
      [
        {
          id: 'remove',
          label: 'Remove',
          permission: 'manage_agents',
          visibleWhenUnavailable: true,
          onClick: noop,
        },
      ],
      { permissions: ['view_agents'] }
    );
    expect(action.disabled).to.equal(true);
    expect(action.tooltip).to.equal('You need Manage Agents to do this.');
  });

  it('leaves no registry-only fields on what it returns', () => {
    const [action] = defineActions({ state: 'running' } as Thing, [
      { id: 'rename', label: 'Rename', available: () => true, onClick: noop },
    ]);
    expect(Object.keys(action).sort()).to.deep.equal([
      'id',
      'label',
      'onClick',
    ]);
  });
});

describe('intersectActions', () => {
  it('keeps only what every selected resource offers', () => {
    const running = [
      { id: 'stop', label: 'Stop' },
      { id: 'rename', label: 'Rename' },
    ];
    const stopped = [
      { id: 'start', label: 'Start' },
      { id: 'rename', label: 'Rename' },
    ];
    expect(actionIds(intersectActions([running, stopped]))).to.deep.equal([
      'rename',
    ]);
  });

  it('treats a disabled action as not offered', () => {
    const a = [{ id: 'run', label: 'Run', disabled: true }];
    const b = [{ id: 'run', label: 'Run' }];
    expect(intersectActions([b, a])).to.deep.equal([]);
    expect(intersectActions([a, b])).to.deep.equal([]);
  });

  it('is empty for an empty selection', () => {
    expect(intersectActions([])).to.deep.equal([]);
  });
});

describe('offersAction', () => {
  it('answers for enabled actions only', () => {
    const actions = [
      { id: 'approve', label: 'Approve' },
      { id: 'deny', label: 'Deny', disabled: true },
    ];
    expect(offersAction(actions, 'approve')).to.equal(true);
    expect(offersAction(actions, 'deny')).to.equal(false);
    expect(offersAction(actions, 'nope')).to.equal(false);
  });
});
