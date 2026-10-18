import { expect } from '@open-wc/testing';
import {
  ACTIVITY_WINDOW_KEY,
  AnnounceThrottle,
  agentChipVariant,
  agentStateLabel,
  formatDuration,
  formatUsd,
  isStale,
  latencyCell,
  loadActivityWindow,
  saveActivityWindow,
  sortAgents,
  sortPeople,
  windowMinutes,
  windowRefreshMs,
} from './live-activity';
import type { ActivityAgent, ActivityPerson } from '../live-activity-api';

describe('live activity helpers', () => {
  afterEach(() => localStorage.removeItem(ACTIVITY_WINDOW_KEY));

  it('defaults the window to 1h and persists a choice', () => {
    expect(loadActivityWindow()).to.equal('1h');
    saveActivityWindow('5m');
    expect(localStorage.getItem(ACTIVITY_WINDOW_KEY)).to.equal('5m');
    expect(loadActivityWindow()).to.equal('5m');
    localStorage.setItem(ACTIVITY_WINDOW_KEY, 'bogus');
    expect(loadActivityWindow()).to.equal('1h');
  });

  it('maps windows to minutes and the fleet map refresh cadence', () => {
    expect(windowMinutes('1h')).to.equal(60);
    expect(windowMinutes('any')).to.equal(null);
    expect(windowRefreshMs('5m')).to.equal(30_000);
    expect(windowRefreshMs('1h')).to.equal(60_000);
    expect(windowRefreshMs('24h')).to.equal(300_000);
    expect(windowRefreshMs('any')).to.equal(null);
  });

  it('formats durations and money', () => {
    expect(formatDuration(100)).to.equal('1m 40s');
    expect(formatDuration(48)).to.equal('48s');
    expect(formatDuration(7500)).to.equal('2h 5m');
    expect(formatUsd(3.2)).to.equal('$3.20');
    expect(formatUsd(0.0042)).to.equal('$0.0042');
    expect(formatUsd(0)).to.equal('$0.00');
  });

  it('renders a median only with enough samples', () => {
    expect(latencyCell(100, 2, 'replies')).to.deep.equal({
      value: '—',
      title: 'Fewer than 3 replies in this window',
    });
    expect(latencyCell(null, 9, 'decisions').value).to.equal('—');
    const cell = latencyCell(100, 3, 'replies');
    expect(cell.value).to.equal('1m 40s');
    expect(cell.title).to.contain('Approximate');
  });

  it('labels agent states with the wait inside the chip', () => {
    const now = Date.parse('2026-10-18T10:06:00Z');
    expect(
      agentStateLabel(
        { state: 'waiting_approval', since: '2026-10-18T10:00:00Z' },
        now
      )
    ).to.equal('Waiting for approval · 6m');
    expect(agentStateLabel({ state: 'working', since: null }, now)).to.equal(
      'Working'
    );
    expect(agentChipVariant('working')).to.equal('success');
    expect(agentChipVariant('waiting_approval')).to.equal('warning');
    expect(agentChipVariant('waiting_reply')).to.equal('neutral');
  });

  it('sorts by state precedence then recency', () => {
    const agents = sortAgents([
      {
        managed_agent_id: 'a',
        state: 'idle',
        last_activity_at: '2026-10-18T10:05:00Z',
      },
      {
        managed_agent_id: 'b',
        state: 'working',
        last_activity_at: '2026-10-18T10:00:00Z',
      },
      {
        managed_agent_id: 'c',
        state: 'waiting_approval',
        last_activity_at: null,
      },
      {
        managed_agent_id: 'd',
        state: 'working',
        last_activity_at: '2026-10-18T10:04:00Z',
      },
    ] as ActivityAgent[]);
    expect(agents.map((a) => a.managed_agent_id)).to.eql(['c', 'd', 'b', 'a']);
    const people = sortPeople([
      { user_id: 'x', state: 'away', last_signal_at: null },
      { user_id: 'y', state: 'active', last_signal_at: null },
    ] as ActivityPerson[]);
    expect(people[0].user_id).to.equal('y');
  });

  it('calls a sweep older than 60s stale', () => {
    const now = Date.parse('2026-10-18T10:02:00Z');
    expect(isStale('2026-10-18T10:00:00Z', now)).to.equal(true);
    expect(isStale('2026-10-18T10:01:30Z', now)).to.equal(false);
  });

  it('throttles announcements to one per 30s and skips repeats', () => {
    const t = new AnnounceThrottle();
    expect(t.next('a', 0)).to.equal('a');
    expect(t.next('b', 10_000)).to.equal(null);
    expect(t.next('a', 40_000)).to.equal(null);
    expect(t.next('b', 40_000)).to.equal('b');
  });
});
