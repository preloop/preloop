import { expect } from '@open-wc/testing';

import {
  agentStateChip,
  CLAUDE_INTERRUPTED,
  deriveAgentStatus,
  fmtElapsedCompact,
  fmtTokens,
  PRELOOP_CLAUDE_DONE_VERBS,
  PRELOOP_CLAUDE_VERBS,
  runtimeForSource,
} from './agent-status';

const T0 = Date.UTC(2026, 9, 21, 20, 0, 0);

describe('agent status vocabulary', () => {
  it('codex working line formats 2m 38s', () => {
    const status = deriveAgentStatus({
      runtime: 'codex',
      turnId: 'turn-1',
      turnStartedAt: T0,
      modelBusy: true,
      now: T0 + 158_000,
    });
    expect(status.line).to.equal('Working (2m 38s)');
    expect(status.glyph).to.equal('codex');
    expect(status.chip.label).to.equal('Working');
    expect(fmtElapsedCompact(39)).to.equal('39s');
    expect(fmtElapsedCompact(3600 + 125)).to.equal('1h 02m 05s');
    expect(
      deriveAgentStatus({
        runtime: 'codex',
        turnStartedAt: T0,
        modelBusy: true,
        canInterrupt: true,
        now: T0 + 39_000,
      }).line
    ).to.equal('Working (39s • esc to interrupt)');
    expect(
      deriveAgentStatus({
        runtime: 'codex',
        turnStartedAt: T0,
        hookRunning: true,
        now: T0 + 40_000,
      }).line
    ).to.equal('Working (40s) · Running hook');
  });

  it('claude line shows verb, elapsed, tokens and thinking label', () => {
    const status = deriveAgentStatus({
      runtime: 'claude',
      turnId: 'turn-7',
      turnStartedAt: T0,
      modelBusy: true,
      outputTokens: 4100,
      now: T0 + 12_000,
    });
    const match = /^(\S+)… \(12s · ↓ 4\.1k tokens · thinking\)$/.exec(
      status.line || ''
    );
    expect(match, status.line || '').to.not.equal(null);
    expect(PRELOOP_CLAUDE_VERBS as readonly string[]).to.include(match![1]);
    expect(status.glyph).to.equal('claude');
    expect(fmtTokens(512)).to.equal('512');
    expect(fmtTokens(12_400)).to.equal('12k');
    const tool = deriveAgentStatus({
      runtime: 'claude',
      turnId: 'turn-7',
      turnStartedAt: T0,
      runningTool: 'Bash',
      outputTokens: 1200,
      now: T0 + 40_000,
    });
    expect(tool.line).to.match(/… \(40s · ↓ 1\.2k tokens · running Bash\)$/);
  });

  it('keeps our own word list free of the distinctive Claude Code words', () => {
    expect(PRELOOP_CLAUDE_VERBS.length).to.be.within(55, 65);
    for (const word of ['Thinking', 'Working', 'Processing'])
      expect(PRELOOP_CLAUDE_VERBS as readonly string[]).to.include(word);
    for (const word of [
      'Clauding',
      'Gitifying',
      'Flibbertigibbeting',
      'Whatchamacalliting',
    ])
      expect(PRELOOP_CLAUDE_VERBS as readonly string[]).to.not.include(word);
  });

  it('verb stays stable across re-renders of one turn', () => {
    const lines = new Set<string>();
    for (let second = 1; second < 50; second += 7) {
      const line = deriveAgentStatus({
        runtime: 'claude',
        turnId: 'turn-stable',
        turnStartedAt: T0,
        modelBusy: second % 2 === 0,
        runningTool: second % 2 ? 'Read' : null,
        now: T0 + second * 1000,
      }).line!;
      lines.add(line.split('…')[0]);
    }
    expect(lines.size).to.equal(1);
  });

  it('turn elapsed does not reset on a new request', () => {
    // The turn began at T0; a second model request opened at T0 + 90s.
    const status = deriveAgentStatus({
      runtime: 'codex',
      turnId: 'turn-2',
      turnStartedAt: T0,
      modelBusy: true,
      now: T0 + 95_000,
    });
    expect(status.line).to.equal('Working (1m 35s)');
  });

  it('waiting for approval wins over tool running', () => {
    const status = deriveAgentStatus({
      runtime: 'codex',
      turnStartedAt: T0,
      runningTool: 'Bash',
      modelBusy: true,
      pendingApprovals: 1,
      pendingSince: T0 + 10_000,
      now: T0 + 72_000,
    });
    expect(status.phase).to.equal('waiting_for_approval');
    expect(status.line).to.equal('Waiting for approval (1m 02s)');
    expect(status.tone).to.equal('warning');
    expect(status.chip).to.deep.equal({
      label: 'Needs approval',
      variant: 'warning',
    });
    expect(status.glyph).to.equal(null);
  });

  it('turn_end yields Worked for footer and Your turn chip', () => {
    const codex = deriveAgentStatus({
      runtime: 'codex',
      turnStartedAt: T0,
      turnEndedAt: T0 + 204_000,
      turnState: 'your_turn',
      now: T0 + 300_000,
    });
    expect(codex.line).to.equal(null);
    expect(codex.chip.label).to.equal('Your turn');
    expect(codex.footer).to.match(/^Worked for 3m 24s • \d{1,2}:\d{2} [AP]M$/);
    const claude = deriveAgentStatus({
      runtime: 'claude',
      turnId: 'turn-9',
      turnStartedAt: T0,
      turnEndedAt: T0 + 912_000,
      turnState: 'your_turn',
      now: T0 + 999_000,
    });
    const word = claude.footer!.split(' ')[0];
    expect(PRELOOP_CLAUDE_DONE_VERBS as readonly string[]).to.include(word);
    expect(claude.footer).to.match(/ for 15m 12s · done \d{1,2}:\d{2} [AP]M$/);
  });

  it("interrupted turn shows the runtime's interrupted marker", () => {
    const base = {
      turnStartedAt: T0,
      turnEndedAt: T0 + 5000,
      interrupted: true,
      now: T0 + 6000,
    };
    expect(deriveAgentStatus({ runtime: 'codex', ...base }).marker).to.equal(
      'Conversation interrupted'
    );
    expect(deriveAgentStatus({ runtime: 'claude', ...base }).marker).to.equal(
      CLAUDE_INTERRUPTED
    );
    expect(deriveAgentStatus({ runtime: 'other', ...base }).marker).to.equal(
      'Interrupted'
    );
  });

  it('a working turn with nothing observed for 10 minutes is not working', () => {
    const status = deriveAgentStatus({
      runtime: 'codex',
      turnStartedAt: T0,
      turnState: 'working',
      lastEventAt: T0 + 60_000,
      now: T0 + 60_000 + 11 * 60_000,
    });
    expect(status.line).to.equal(null);
    expect(status.chip.label).to.equal('Idle');
    expect(
      deriveAgentStatus({
        runtime: 'codex',
        turnStartedAt: T0,
        turnState: 'working',
        lastEventAt: T0 + 60_000,
        now: T0 + 120_000,
      }).line
    ).to.equal('Working (2m 00s)');
  });

  it('maps source types and list rows to one chip vocabulary', () => {
    expect(runtimeForSource('codex_cli')).to.equal('codex');
    expect(runtimeForSource('claude_code')).to.equal('claude');
    expect(runtimeForSource('openclaw')).to.equal('other');
    const now = Date.UTC(2026, 9, 21, 20, 0, 0);
    expect(
      agentStateChip(
        { turn_state: 'working', last_activity_at: '2026-10-21T19:59:00' },
        now
      ).label
    ).to.equal('Working');
    // An offset-less timestamp is UTC; an hour of silence is not working.
    expect(
      agentStateChip(
        { turn_state: 'working', last_activity_at: '2026-10-21T19:00:00' },
        now
      ).label
    ).to.equal('Idle');
    expect(agentStateChip({ is_active_now: true }, now).label).to.equal('Idle');
    expect(agentStateChip({ ended_at: '2026-10-21T19:00:00' }).label).to.equal(
      'Ended'
    );
  });
});
