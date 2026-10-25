import { expect } from '@open-wc/testing';
import { FleetEventLog } from './fleet-map-event-log';
import {
  messagesForRealtime,
  approvalCommand,
  FleetLights,
} from './fleet-map-motion';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';

describe('Fleet Map event metadata', () => {
  it('keeps independent concurrent requests and rejects late starts and duplicate completions', () => {
    const log = new FleetEventLog(),
      graph = sampleFleetMap(),
      now = Date.now();
    const wire = (type: string, id: string, usage?: string) => ({
      type,
      payload: {
        managed_agent_id: 'codex',
        model_alias: 'gpt-6.1',
        gateway_request_id: id,
        api_usage_id: usage,
        prompt_tokens: 1200,
        completion_tokens: 300,
      },
    });
    const record = (e: ReturnType<typeof wire>) =>
      log.record(messagesForRealtime(graph, 'gateway_activity', e), e, now);
    expect(record(wire('model_gateway_call', 'r1', 'usage-1'))).to.equal(true);
    expect(record(wire('model_gateway_request_started', 'r1'))).to.equal(false);
    expect(record(wire('model_gateway_call', 'r1', 'usage-1'))).to.equal(false);
    expect(record(wire('model_gateway_call', 'r2', 'usage-2'))).to.equal(true);
    expect(
      log.forNode('agent:codex', null, now).filter((e) => e.kind === 'response')
    ).to.have.length(2);
  });
  it('clears only the matching thinking note when a completion arrives', () => {
    const lights = new FleetLights(4000, 4);
    lights.light(
      {
        from: 'agent:a',
        to: 'model:m',
        kind: 'request',
        label: 'thinking',
        requestId: 'r2',
      },
      0
    );
    lights.light(
      {
        from: 'model:m',
        to: 'agent:a',
        actor: 'agent:a',
        kind: 'response',
        label: 'done',
        requestId: 'r1',
      },
      1
    );
    expect(lights.at('agent:a', 1)?.requestId).to.equal('r2');
    lights.light(
      {
        from: 'model:m',
        to: 'agent:a',
        actor: 'agent:a',
        kind: 'response',
        label: 'done',
        requestId: 'r2',
      },
      2
    );
    expect(lights.at('agent:a', 2)).to.equal(undefined);
  });
  it('folds adjacent identical tool notes and bounds event retention', () => {
    const log = new FleetEventLog(),
      now = Date.now();
    for (let i = 0; i < 2001; i++)
      log.record(
        [
          {
            from: 'agent:a',
            to: 'device:d',
            kind: 'tool',
            label: 'Read',
            icon: 'wrench',
          },
        ],
        { payload: { activity_id: `a${i}` } },
        now
      );
    expect(log.truncated).to.equal(true);
    expect(log.forNode('agent:a', null, now)).to.have.length(2000);
    expect(log.recent('agent:a', null)[0].count).to.equal(2000);
  });
  it('scrubs credential values before bounding existing approval commands', () => {
    const text = approvalCommand(
      'curl -H "Authorization: Bearer synthetic-secret" https://user:password@example.com/x?token=private'
    );
    expect(text).not.to.contain('synthetic-secret');
    expect(text).not.to.contain('password');
    expect(text).not.to.contain('private');
    expect(approvalCommand('pytest -q tests/api')).to.equal(
      'pytest -q tests/api'
    );
  });
});
