import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { FlowExecutionView } from '../views/authed/flow-execution-view';

describe('execution recovery request bound', () => {
  it('coalesces continuing gaps to at most one request group per two seconds', async () => {
    const clock = sinon.useFakeTimers();
    const recover = sinon.stub().resolves();
    const controller: any = {
      isConnected: true,
      executionGeneration: 0,
      nextRealtimeRecoveryAt: 0,
      recoverRealtime: recover,
      scheduleRealtimeRecovery: (FlowExecutionView.prototype as any)
        .scheduleRealtimeRecovery,
    };
    try {
      for (let i = 0; i < 10; i++) controller.scheduleRealtimeRecovery();
      await clock.tickAsync(100);
      expect(recover.callCount).to.equal(1);
      for (let i = 0; i < 10; i++) controller.scheduleRealtimeRecovery();
      await clock.tickAsync(1999);
      expect(recover.callCount).to.equal(1);
      await clock.tickAsync(1);
      expect(recover.callCount).to.equal(2);
    } finally {
      clock.restore();
    }
  });
});
