import { expect } from '@open-wc/testing';
import type { RunnerRecord } from '../api';
import {
  hostExecRunnerChoices,
  hostExecRunnerBlockers,
  runnerToken,
  selectedHostExecChoice,
} from './host-exec-runners';

function runner(
  id: string,
  name: string,
  login = 'signed_in',
  enabled = true
): RunnerRecord {
  return {
    id,
    name,
    status: 'online',
    capabilities: {
      host_exec_profiles: [
        { name: 'cursor', capabilities: ['host_exec', 'cursor_cli'] },
      ],
    },
    harness_inventory: {
      entries: [
        {
          harness: 'cursor_cli',
          login_state: login,
          enabled,
          generated_profile: 'cursor',
        },
      ],
    },
  } as RunnerRecord;
}

describe('Cursor runner selection', () => {
  it('uses IDs even for names that collide with reserved pools or labels', () => {
    for (const name of ['auto', 'server', 'personal', 'Jane Mac']) {
      expect(runnerToken(runner('runner-1', name))).to.equal('runner-1');
    }
  });
  it('does not infer one machine from a duplicate name or reserved pool', () => {
    const choices = hostExecRunnerChoices(
      [
        runner('runner-1', 'same'),
        runner('runner-2', 'same'),
        runner('runner-3', 'auto'),
      ],
      'cursor_cli'
    );
    expect(selectedHostExecChoice(choices, 'same')).to.equal(undefined);
    expect(selectedHostExecChoice(choices, 'auto')).to.equal(undefined);
    expect(selectedHostExecChoice(choices, 'runner-2')?.runner.id).to.equal(
      'runner-2'
    );
  });
  it('rejects stale advertised profiles when Cursor is signed out or disabled', () => {
    const runners = [
      runner('runner-1', 'signed-out', 'signed_out'),
      runner('runner-2', 'disabled', 'signed_in', false),
    ];
    expect(hostExecRunnerChoices(runners, 'cursor_cli')).to.deep.equal([]);
    expect(
      hostExecRunnerBlockers(runners, 'cursor_cli').map((item) => item.reason)
    ).to.deep.equal(['signed_out', 'disabled']);
  });
});
