import { expect } from '@open-wc/testing';

import { MessageRouter } from './message-router';

describe('MessageRouter', () => {
  let router: MessageRouter;

  beforeEach(() => {
    router = new MessageRouter();
  });

  describe('Subscribe', () => {
    it('adds subscription and returns unsubscribe function', () => {
      const callback = () => {};
      const unsubscribe = router.subscribe('flow_executions', callback);

      expect(router.getSubscriptionCount('flow_executions')).to.equal(1);
      expect(typeof unsubscribe).to.equal('function');
    });

    it('supports multiple subscribers for same topic', () => {
      router.subscribe('approvals', () => {});
      router.subscribe('approvals', () => {});

      expect(router.getSubscriptionCount('approvals')).to.equal(2);
    });

    it('supports subscription with filter', () => {
      const callback = () => {};
      const filter = (msg: { type: string }) => msg.type === 'approval_created';
      router.subscribe('approvals', callback, filter);

      expect(router.getSubscriptionCount('approvals')).to.equal(1);
    });
  });

  describe('Unsubscribe', () => {
    it('removes subscriber when unsubscribe is called', () => {
      const callback = () => {};
      const unsubscribe = router.subscribe('flow_executions', callback);

      expect(router.getSubscriptionCount('flow_executions')).to.equal(1);

      unsubscribe();
      expect(router.getSubscriptionCount('flow_executions')).to.equal(0);
    });

    it('only removes the specific subscriber', () => {
      const cb1 = () => {};
      const cb2 = () => {};
      const unsub1 = router.subscribe('approvals', cb1);
      router.subscribe('approvals', cb2);

      unsub1();
      expect(router.getSubscriptionCount('approvals')).to.equal(1);
    });
  });

  describe('Message handling', () => {
    it('routes approval_created to approvals topic', () => {
      const received: unknown[] = [];
      router.subscribe('approvals', (msg) => received.push(msg));

      router.route({ type: 'approval_created', id: 'a1' });

      expect(received).to.have.lengthOf(1);
      expect(received[0]).to.deep.equal({ type: 'approval_created', id: 'a1' });
    });

    it('routes execution_started to flow_executions topic', () => {
      const received: unknown[] = [];
      router.subscribe('flow_executions', (msg) => received.push(msg));

      router.route({ type: 'execution_started', execution_id: 'e1' });

      expect(received).to.have.lengthOf(1);
      expect(received[0]).to.deep.include({
        type: 'execution_started',
        execution_id: 'e1',
      });
    });

    it('routes runner_updated to runners topic', () => {
      const received: unknown[] = [];
      router.subscribe('runners', (msg) => received.push(msg));

      router.route({
        type: 'runner_updated',
        payload: { id: 'r1', status: 'online' },
      });

      expect(received).to.have.lengthOf(1);
      expect(received[0]).to.deep.include({ type: 'runner_updated' });
    });

    it('routes to wildcard subscribers', () => {
      const received: unknown[] = [];
      router.subscribe('*', (msg) => received.push(msg));

      router.route({ type: 'approval_created', id: 'a1' });

      expect(received).to.have.lengthOf(1);
      expect(received[0]).to.deep.include({ type: 'approval_created' });
    });

    it('respects filter when provided', () => {
      const received: unknown[] = [];
      router.subscribe(
        'approvals',
        (msg) => received.push(msg),
        (m: { status?: string }) => m.status === 'pending'
      );

      router.route({ type: 'approval_created', status: 'approved' });
      expect(received).to.have.lengthOf(0);

      router.route({ type: 'approval_created', status: 'pending' });
      expect(received).to.have.lengthOf(1);
    });

    it('does not route messages without type', () => {
      const received: unknown[] = [];
      router.subscribe('approvals', (msg) => received.push(msg));

      router.route({ data: 'invalid' });

      expect(received).to.have.lengthOf(0);
    });

    it('notifies all topic subscribers', () => {
      const received1: unknown[] = [];
      const received2: unknown[] = [];
      router.subscribe('flow_executions', (msg) => received1.push(msg));
      router.subscribe('flow_executions', (msg) => received2.push(msg));

      router.route({ type: 'execution_started', id: 'e1' });

      expect(received1).to.have.lengthOf(1);
      expect(received2).to.have.lengthOf(1);
    });
  });

  describe('clearTopic', () => {
    it('removes all subscribers for a topic', () => {
      router.subscribe('approvals', () => {});
      router.subscribe('approvals', () => {});

      router.clearTopic('approvals');

      expect(router.getSubscriptionCount('approvals')).to.equal(0);
    });
  });

  describe('clearAll', () => {
    it('removes all subscriptions', () => {
      router.subscribe('approvals', () => {});
      router.subscribe('flow_executions', () => {});

      router.clearAll();

      expect(router.getSubscriptionCount('approvals')).to.equal(0);
      expect(router.getSubscriptionCount('flow_executions')).to.equal(0);
    });
  });
});
