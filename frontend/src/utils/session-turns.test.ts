import { expect } from '@open-wc/testing';
import { formatElapsedCompact } from './duration';
import {
  buildSessionTurns,
  parseExitCode,
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
});
