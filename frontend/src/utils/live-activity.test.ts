import { expect } from '@open-wc/testing';
import {
  ACTIVITY_WINDOW_KEY,
  AnnounceThrottle,
  agentChipVariant,
  agentStateLabel,
  FLIP_TTL_MS,
  formatAgentRate,
  formatDuration,
  formatLiveRelative,
  formatUsd,
  isStale,
  latencyCell,
  liveFlips,
  loadActivityWindow,
  personStateDurationLabel,
  saveActivityWindow,
  sortAgents,
  sortPeople,
  sparklineSummary,
  sparklineY,
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

  it('sorts by state precedence, then a stable key', () => {
    const agents = sortAgents([
      {
        managed_agent_id: 'a',
        display_name: 'Codex',
        state: 'idle',
        last_activity_at: '2026-10-18T10:05:00Z',
      },
      {
        managed_agent_id: 'b',
        display_name: 'Cursor',
        state: 'working',
        last_activity_at: '2026-10-18T10:04:00Z',
      },
      {
        managed_agent_id: 'c',
        display_name: 'OpenCode',
        state: 'waiting_approval',
        last_activity_at: null,
      },
      {
        managed_agent_id: 'd',
        display_name: 'Claude Code',
        state: 'working',
        last_activity_at: '2026-10-18T10:00:00Z',
      },
    ] as ActivityAgent[]);
    expect(agents.map((a) => a.managed_agent_id)).to.eql(['c', 'd', 'b', 'a']);
    const people = sortPeople([
      { user_id: 'x', state: 'away', last_signal_at: null },
      { user_id: 'y', state: 'active', last_signal_at: null },
    ] as ActivityPerson[]);
    expect(people[0].user_id).to.equal('y');
  });

  it('keeps two people in place while their signals alternate', () => {
    const at = (s: number) =>
      new Date(Date.UTC(2026, 9, 18, 10, 0, s)).toISOString();
    const person = (
      id: string,
      name: string,
      signal: string,
      state = 'active'
    ) =>
      ({
        user_id: id,
        display_name: name,
        state,
        last_signal_at: signal,
      }) as ActivityPerson;
    const order = (people: ActivityPerson[]) =>
      sortPeople(people).map((p) => p.user_id);
    // Bea prompts last, then Al does: the order must not change.
    expect(
      order([person('al', 'Al', at(1)), person('bea', 'Bea', at(2))])
    ).to.eql(['al', 'bea']);
    expect(
      order([person('al', 'Al', at(3)), person('bea', 'Bea', at(2))])
    ).to.eql(['al', 'bea']);
    expect(
      order([person('bea', 'Bea', at(4)), person('al', 'Al', at(3))])
    ).to.eql(['al', 'bea']);
    // A state change still moves the row.
    expect(
      order([person('al', 'Al', at(5), 'idle'), person('bea', 'Bea', at(4))])
    ).to.eql(['bea', 'al']);
  });

  it('keeps identical agent names apart by owner then host, not recency', () => {
    const agent = (id: string, owner: string, last: string) =>
      ({
        managed_agent_id: id,
        display_name: 'Claude Code',
        owner_display_name: owner,
        state: 'working',
        last_activity_at: last,
      }) as ActivityAgent;
    const ids = (list: ActivityAgent[]) =>
      sortAgents(list).map((a) => a.managed_agent_id);
    expect(
      ids([
        agent('2', 'Sana', '2026-10-18T10:00:09Z'),
        agent('1', 'Maya', '2026-10-18T10:00:01Z'),
      ])
    ).to.eql(['1', '2']);
    expect(
      ids([
        agent('1', 'Maya', '2026-10-18T10:00:10Z'),
        agent('2', 'Sana', '2026-10-18T10:00:11Z'),
      ])
    ).to.eql(['1', '2']);
  });

  it('formats agent rates with one decimal up to 100 and blanks idle zeros', () => {
    expect(formatAgentRate(0, false)).to.equal('');
    expect(formatAgentRate(undefined, true)).to.equal('');
    expect(formatAgentRate(0, true)).to.equal('0.0');
    expect(formatAgentRate(9.3, true)).to.equal('9.3');
    expect(formatAgentRate(10, true)).to.equal('10.0');
    expect(formatAgentRate(99.94, true)).to.equal('99.9');
    expect(formatAgentRate(140.4, true)).to.equal('140');
  });

  it('ticks relative times in seconds under a minute', () => {
    const now = Date.parse('2026-10-18T10:10:00Z');
    expect(formatLiveRelative('2026-10-18T10:09:58Z', now)).to.equal(
      'just now'
    );
    expect(formatLiveRelative('2026-10-18T10:09:48Z', now)).to.equal('12s ago');
    expect(formatLiveRelative('2026-10-18T10:09:47Z', now)).to.equal('13s ago');
    expect(formatLiveRelative('2026-10-18T10:06:00Z', now)).to.equal('4m ago');
    expect(formatLiveRelative('2026-10-18T07:10:00Z', now)).to.equal('3h ago');
  });

  it('gives idle and away people a duration', () => {
    const now = Date.parse('2026-10-18T10:12:00Z');
    expect(
      personStateDurationLabel(
        { state: 'idle', since: '2026-10-18T10:00:00Z' },
        now
      )
    ).to.equal('Idle · 12m');
    expect(
      personStateDurationLabel(
        { state: 'away', since: '2026-10-18T08:00:00Z' },
        now
      )
    ).to.equal('Away · 2h');
    expect(
      personStateDurationLabel(
        { state: 'active', since: '2026-10-18T10:00:00Z' },
        now
      )
    ).to.equal('Active');
    expect(
      personStateDurationLabel({ state: 'idle', since: null }, now)
    ).to.equal('Idle');
  });

  it('keeps client flips across a poll until they expire or a sweep supersedes them', () => {
    const flippedAt = Date.parse('2026-10-18T10:00:00Z');
    const flips = new Map([
      ['a1', flippedAt],
      ['a2', flippedAt],
    ]);
    const agents = [
      { managed_agent_id: 'a1', since: '2026-10-18T09:50:00Z' },
      { managed_agent_id: 'a2', since: '2026-10-18T10:00:05Z' },
    ];
    // A poll 10s later: a1's sweep is older than the flip, a2's is newer.
    expect([...liveFlips(flips, agents, flippedAt + 10_000).keys()]).to.eql([
      'a1',
    ]);
    // Past the TTL nothing survives.
    expect(liveFlips(flips, agents, flippedAt + FLIP_TTL_MS).size).to.equal(0);
  });

  it('summarises the sparkline and places the reference line on its scale', () => {
    expect(sparklineSummary([])).to.eql({ latest: null, peak: 0, avg: 0 });
    expect(sparklineSummary([2, 8, 5])).to.eql({ latest: 5, peak: 8, avg: 5 });
    expect(sparklineY(8, 8, 56)).to.equal(2);
    expect(sparklineY(0, 8, 56)).to.equal(54);
    expect(sparklineY(4, 8, 56)).to.equal(28);
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
