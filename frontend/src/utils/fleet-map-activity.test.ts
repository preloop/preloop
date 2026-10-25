import { expect } from '@open-wc/testing';
import { useFakeTimers } from 'sinon';
import {
  FleetActivityTracker,
  activityFromRealtime,
  activityLabel,
  modelKey,
  windowMinutes,
  windowRefreshMs,
} from './fleet-map-activity';

describe('fleet map activity', () => {
  it('offers short windows and polls faster for them', () => {
    expect(windowMinutes('5m')).to.equal(5);
    expect(windowMinutes('1h')).to.equal(60);
    expect(windowMinutes('30d')).to.equal(60 * 24 * 30);
    expect(windowMinutes('any')).to.equal(null);
    expect(windowRefreshMs('5m')).to.equal(30_000);
    expect(windowRefreshMs('1h')).to.equal(60_000);
    expect(windowRefreshMs('7d')).to.equal(300_000);
    expect(windowRefreshMs('any')).to.equal(null);
  });

  it('repaints at most once per interval and drops the stalest lines', () => {
    const clock = useFakeTimers({ now: 0 });
    try {
      let paints = 0;
      const tracker = new FleetActivityTracker(() => (paints += 1), {
        flushMs: 1000,
        holdMs: 3000,
        maxLit: 2,
        now: () => clock.now,
      });
      for (let i = 0; i < 50; i++) tracker.add(['a'], 'tool call');
      tracker.add(['b'], 'approval');
      tracker.add(['c'], 'model request');
      expect(paints).to.equal(0);
      clock.tick(1000);
      expect(paints).to.equal(1);
      expect([...tracker.marks.keys()]).to.deep.equal(['b', 'c']);
      clock.tick(3000);
      expect(tracker.marks.size).to.equal(0);
      tracker.stop();
    } finally {
      clock.restore();
    }
  });

  it('reads only traffic that names an agent', () => {
    expect(
      activityFromRealtime('gateway_activity', {
        type: 'model_gateway_call',
        payload: {},
      })
    ).to.equal(null);
    expect(
      activityFromRealtime('approvals', {
        type: 'approval_created',
        managed_agent_id: 'a1',
      })
    ).to.deep.equal({
      kind: 'approval',
      agent: 'agent:a1',
      label: 'approval requested',
    });
    expect(modelKey('anthropic/claude-opus-5-5-20261001')).to.equal(
      'claude-opus-5-5'
    );
  });

  it('counts a start and its completion as one call in the label', () => {
    const clock = useFakeTimers({ now: 0 });
    try {
      const tracker = new FleetActivityTracker(() => undefined, {
        flushMs: 1000,
        now: () => clock.now,
      });
      for (let i = 0; i < 2; i++) {
        tracker.add(['m'], 'model request');
        tracker.add(['m'], 'model request', 'Sonnet · 3.6k in / 210 out');
      }
      clock.tick(1000);
      expect(activityLabel(tracker.marks.get('m')!)).to.equal(
        'Sonnet · 3.6k in / 210 out ×2'
      );
      tracker.stop();
    } finally {
      clock.restore();
    }
  });
});
