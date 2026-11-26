import { expect } from '@open-wc/testing';
import { sessionTree } from './session-tree';

describe('session tree', () => {
  const sessions = [
    { id: 'child', parentSessionId: 'parent', status: 'active_now' },
    { id: 'finished', parentSessionId: 'parent', status: 'idle' },
    { id: 'parent', status: 'idle' },
  ];
  it('collapses subagents and counts running children', () => {
    const rows = sessionTree(sessions, new Set());
    expect(rows.map((row) => row.session.id)).to.deep.equal(['parent']);
    expect(rows[0].children).to.equal(2);
    expect(rows[0].runningChildren).to.equal(1);
  });
  it('reveals a matching child under its parent', () => {
    const rows = sessionTree(sessions, new Set(), new Set(['child']));
    expect(rows.map((row) => [row.session.id, row.depth])).to.deep.equal([
      ['parent', 0],
      ['child', 1],
      ['finished', 1],
    ]);
  });
  it('keeps cyclic and missing-parent records reachable', () => {
    const rows = sessionTree(
      [
        { id: 'a', parentSessionId: 'b', status: 'idle' },
        { id: 'b', parentSessionId: 'a', status: 'idle' },
        { id: 'orphan', parentSessionId: 'absent', status: 'idle' },
      ],
      new Set(['a', 'b'])
    );
    expect(new Set(rows.map((row) => row.session.id)).size).to.equal(3);
  });
});
