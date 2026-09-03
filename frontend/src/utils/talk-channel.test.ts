import { expect } from '@open-wc/testing';

import {
  TALK_STALE_MS,
  TalkWindowRegistry,
  type TalkChannelMessage,
} from './talk-channel';

function message(
  overrides: Partial<TalkChannelMessage> &
    Pick<TalkChannelMessage, 'type' | 'agentId'>
): TalkChannelMessage {
  return {
    agentName: 'Hermes',
    sessionId: 'sess-1',
    at: 1_000,
    ...overrides,
  };
}

describe('TalkWindowRegistry', () => {
  it('adds a window on open and removes it on close', () => {
    const registry = new TalkWindowRegistry();
    expect(registry.apply(message({ type: 'open', agentId: 'a1' }))).to.be.true;
    expect(registry.entries).to.have.length(1);
    expect(registry.entries[0].agentName).to.equal('Hermes');
    expect(registry.entries[0].unread).to.be.false;
    expect(registry.apply(message({ type: 'close', agentId: 'a1' }))).to.be
      .true;
    expect(registry.entries).to.be.empty;
  });

  it('marks a window unread on a message and clears it on focus', () => {
    const registry = new TalkWindowRegistry();
    registry.apply(message({ type: 'open', agentId: 'a1' }));
    registry.apply(message({ type: 'message', agentId: 'a1', at: 2_000 }));
    expect(registry.entries[0].unread).to.be.true;
    expect(registry.clearUnread('a1')).to.be.true;
    expect(registry.entries[0].unread).to.be.false;
    expect(registry.clearUnread('a1'), 'already read').to.be.false;
  });

  it('keeps a heartbeat from re-marking a read window unread', () => {
    const registry = new TalkWindowRegistry();
    registry.apply(message({ type: 'message', agentId: 'a1' }));
    registry.clearUnread('a1');
    registry.apply(message({ type: 'open', agentId: 'a1', at: 5_000 }));
    expect(registry.entries[0].unread).to.be.false;
  });

  it('drops windows that go quiet for longer than the stale window', () => {
    const registry = new TalkWindowRegistry();
    registry.apply(message({ type: 'open', agentId: 'a1', at: 1_000 }));
    registry.apply(message({ type: 'open', agentId: 'a2', at: 60_000 }));

    expect(registry.prune(1_000 + TALK_STALE_MS)).to.be.false;
    expect(registry.entries).to.have.length(2);

    expect(registry.prune(1_001 + TALK_STALE_MS)).to.be.true;
    expect(registry.entries.map((entry) => entry.agentId)).to.deep.equal([
      'a2',
    ]);
  });

  it('sorts chips by agent name so they do not jump around', () => {
    const registry = new TalkWindowRegistry();
    registry.apply(message({ type: 'open', agentId: 'a1', agentName: 'Zeus' }));
    registry.apply(
      message({ type: 'open', agentId: 'a2', agentName: 'Apollo' })
    );
    expect(registry.entries.map((entry) => entry.agentName)).to.deep.equal([
      'Apollo',
      'Zeus',
    ]);
  });

  it('ignores a message with no agent', () => {
    const registry = new TalkWindowRegistry();
    expect(
      registry.apply({ type: 'open', agentId: '', sessionId: null, at: 1 })
    ).to.be.false;
  });
});
