import { expect } from '@open-wc/testing';
import { formatElapsedCompact } from './duration';
import {
  buildSessionTurns,
  parseExitCode,
  runtimeFromSourceType,
  stripWrapperTags,
  toolDiff,
} from './session-turns';
import type { TranscriptStepGroupItem } from './transcript';

describe('session-turns', () => {
  it('formats elapsed time like the CLI', () => {
    expect(formatElapsedCompact(9_400)).to.equal('9s');
    expect(formatElapsedCompact(204_000)).to.equal('3m 24s');
    expect(formatElapsedCompact(3_725_000)).to.equal('1h 02m 05s');
  });

  it('reads exit codes from captured results', () => {
    expect(parseExitCode('Process exited with code 2\nboom')).to.equal(2);
    expect(parseExitCode('{"exit_code": 1}')).to.equal(1);
    expect(parseExitCode('all good')).to.equal(null);
  });

  it('renders apply_patch arguments as a diff', () => {
    const diff = toolDiff(
      'apply_patch',
      JSON.stringify({
        input:
          '*** Begin Patch\n*** Update File: a.ts\n@@\n-old\n+new\n keep\n*** End Patch',
      })
    );
    expect(diff?.map((line) => line.kind)).to.deep.equal([
      'hunk',
      'hunk',
      'del',
      'add',
      'ctx',
    ]);
  });

  it('maps session source types to a runtime', () => {
    expect(runtimeFromSourceType('claude_code')).to.equal('claude');
    expect(runtimeFromSourceType('codex_cli')).to.equal('codex');
    expect(runtimeFromSourceType('hermes')).to.equal('');
    expect(runtimeFromSourceType(null)).to.equal('');
  });

  it('strips session wrapper tags only', () => {
    expect(stripWrapperTags('<session id="x">\nhi\n</session>')).to.equal('hi');
  });

  it('folds system segment groups of one turn into one group', () => {
    const group = (key: string, ts: string): TranscriptStepGroupItem => ({
      type: 'steps',
      key,
      steps: [
        {
          key: `${key}:s`,
          kind: 'system',
          label: 'System prompt',
          text: 'x',
          timestamp: ts,
          detectionExact: true,
        },
      ],
    });
    const result = buildSessionTurns([
      {
        type: 'message',
        key: 'p',
        kind: 'user_prompt',
        role: 'user',
        text: 'go',
        timestamp: '2026-08-06T10:00:00Z',
        event: null,
      },
      group('g1', '2026-08-06T10:00:01Z'),
      group('g2', '2026-08-06T10:00:02Z'),
    ]);
    expect(result.turns).to.have.length(1);
    const groups = result.turns[0].items.filter((i) => i.type === 'steps');
    expect(groups).to.have.length(1);
    expect((groups[0] as TranscriptStepGroupItem).steps).to.have.length(2);
  });
  it('interrupt marks the running turn interrupted', () => {
    const prompt = {
      type: 'message' as const,
      key: 'p',
      kind: 'user_prompt',
      role: 'user',
      text: 'refactor billing',
      timestamp: '2026-08-06T10:00:00Z',
      event: null,
    };
    const turnEnd = (interrupted: boolean) => ({
      activity_id: 'end-1',
      activity_type: 'turn_end',
      timestamp: '2026-08-06T10:00:39Z',
      metadata: { interrupted },
    });
    for (const [runtime, marker] of [
      ['codex', 'Conversation interrupted'],
      ['claude', 'Interrupted'],
    ] as const) {
      const { turns } = buildSessionTurns([prompt as never], {
        runtime,
        activity: [turnEnd(true) as never],
      });
      expect(turns[0].footer?.interrupted).to.equal(true);
      expect(turns[0].footer?.text).to.contain(marker);
    }
    const done = buildSessionTurns([prompt as never], {
      runtime: 'codex',
      activity: [turnEnd(false) as never],
    });
    expect(done.turns[0].footer?.interrupted).to.equal(false);
    expect(done.turns[0].footer?.text).to.not.contain('nterrupted');
  });
});
