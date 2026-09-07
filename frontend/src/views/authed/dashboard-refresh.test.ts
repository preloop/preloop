import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import './dashboard-control-plane-view';
import type { DashboardView } from './dashboard-control-plane-view';

describe('Overview refresh backpressure', () => {
  let clock: sinon.SinonFakeTimers;
  let element: DashboardView;

  beforeEach(() => {
    clock = sinon.useFakeTimers({ now: Date.now() });
    element = document.createElement('dashboard-view') as DashboardView;
    sinon.stub(element, 'isConnected').get(() => true);
  });
  afterEach(() => {
    clock.restore();
    sinon.restore();
  });

  it('delivers queued events even when initial loading finishes within five seconds', async () => {
    element['refreshInFlight'] = true;
    element['lastFetchStartedAt'] = Date.now();
    const run = sinon.stub().resolves();
    element['scheduleTopicRefresh']('fleet', run);
    await clock.tickAsync(1000);
    element['refreshInFlight'] = false;
    element['flushPendingTopicRefreshes']();
    await clock.tickAsync(3999);
    expect(run.called).to.equal(false);
    await clock.tickAsync(1);
    expect(run.calledOnce).to.equal(true);
  });

  it('does not launch realtime requests during a slow initial load', async () => {
    element['refreshInFlight'] = true;
    element['lastFetchStartedAt'] = Date.now();
    const run = sinon.stub().resolves();
    await clock.tickAsync(27000);
    element['scheduleTopicRefresh']('fleet', run);
    await clock.tickAsync(10000);
    expect(run.called).to.equal(false);
    element['refreshInFlight'] = false;
    element['scheduleTopicRefresh']('fleet', run);
    await clock.tickAsync(10000);
    expect(run.calledOnce).to.equal(true);
  });

  it('keeps one request in flight after the realtime interval has elapsed', async () => {
    let release!: () => void;
    const run = sinon.stub().callsFake(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        })
    );
    element['scheduleTopicRefresh']('fleet', run);
    await clock.tickAsync(10000);
    element['scheduleTopicRefresh']('fleet', run);
    await clock.tickAsync(27000);
    expect(run.calledOnce).to.equal(true);
    release();
    await clock.tickAsync(0);
    await clock.tickAsync(10000);
    expect(run.calledTwice).to.equal(true);
    release();
    await clock.tickAsync(0);
  });

  it('rechecks initial loading when an already scheduled event fires', async () => {
    const run = sinon.stub().resolves();
    element['scheduleTopicRefresh']('fleet', run);
    element['refreshInFlight'] = true;
    await clock.tickAsync(10000);
    expect(run.called).to.equal(false);
  });

  it('coalesces background refreshes and releases the guard on failure', async () => {
    let release!: () => void;
    const breakdown = sinon
      .stub(element as any, 'refreshUsageBreakdown')
      .callsFake(
        () =>
          new Promise<void>((resolve) => {
            release = resolve;
          })
      );
    sinon.stub(element as any, 'refreshAttentionInputs').resolves();
    sinon.stub(element as any, 'refreshAuditExceptions').resolves();
    const first = element['refreshBackgroundInputs']();
    await element['refreshBackgroundInputs']();
    expect(breakdown.calledOnce).to.equal(true);
    release();
    await first;
    breakdown.rejects(new Error('temporary failure'));
    try {
      await element['refreshBackgroundInputs']();
    } catch {
      /* expected */
    }
    expect(element['backgroundRefreshInFlight']).to.equal(false);
  });
});
