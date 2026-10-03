import { expect } from '@open-wc/testing';
import { sessionTools, sessionWork, readablePayload } from './session-live';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';

function event(
  id: string,
  payload: Record<string, unknown>,
  type = 'model_gateway_call'
): FlowGatewayEvent {
  return {
    id,
    type,
    execution_id: 'execution-example',
    timestamp: '2026-10-02T10:00:00Z',
    payload,
  };
}
const call = (id: string) => ({
  id,
  function: { name: 'terminal', arguments: '{"command":"pwd"}' },
});
describe('session live evidence', () => {
  it('reconstructs gateway-only Hermes parallel and repeated calls by identity', () => {
    const response = {
      choices: [
        {
          message: {
            role: 'assistant',
            content: null,
            tool_calls: [call('one'), call('two')],
          },
        },
      ],
    };
    const result = {
      messages: [
        { role: 'tool', tool_call_id: 'one', content: 'example output' },
      ],
    };
    const tools = sessionTools([
      event('a', { response }),
      event('b', { request: result, response }),
    ]);
    expect(tools).to.have.length(2);
    expect(tools[0]).to.include({
      name: 'terminal',
      phase: 'completed',
      result: 'example output',
    });
    expect(tools[1].phase).to.equal('requested');
  });
  it('reconciles out-of-order result and call without inferring execution', () => {
    const tools = sessionTools([
      event('result', {
        tools: [{ kind: 'result', call_id: 'one', text: 'done' }],
      }),
      event('call', {
        tools: [{ kind: 'call', call_id: 'one', name: 'terminal', text: '{}' }],
      }),
    ]);
    expect(tools).to.have.length(1);
    expect(tools[0].phase).to.equal('completed');
  });
  it('normalizes Responses and Anthropic structures, failure and missing result', () => {
    const tools = sessionTools([
      event('response', {
        response: {
          output: [
            {
              type: 'function_call',
              call_id: 'one',
              name: 'terminal',
              arguments: '{}',
            },
          ],
        },
      }),
      event('anthropic', {
        request: {
          messages: [
            {
              role: 'user',
              content: [
                {
                  type: 'tool_result',
                  tool_use_id: 'one',
                  content: 'failed',
                  is_error: true,
                },
              ],
            },
          ],
        },
        response: {
          content: [
            {
              type: 'tool_use',
              id: 'two',
              name: 'read_file',
              input: { path: 'example.txt' },
            },
          ],
        },
      }),
    ]);
    expect(tools[0].phase).to.equal('failed');
    expect(tools[1].phase).to.equal('requested');
  });
  it('retains redaction and never reads omitted raw capture content', () => {
    expect(
      sessionTools([
        event('redacted', {
          capture_policy: { content_capture_enabled: false },
          response: {
            choices: [{ message: { tool_calls: [call('secret')] } }],
          },
        }),
      ])
    ).to.have.length(0);
    const tools = sessionTools([
      event('redacted', {
        tools: [
          {
            kind: 'call',
            call_id: 'one',
            name: 'terminal',
            redacted: true,
            truncated: true,
          },
        ],
      }),
    ]);
    expect(tools[0]).to.include({ redacted: true, truncated: true });
    expect(readablePayload('{malformed')).to.equal('{malformed');
    expect(readablePayload('{"command":"pwd"}')).to.include('\n');
  });
  it('does not regress terminal results with an older native running record', () => {
    const native = {
      activity_type: 'tool_call',
      timestamp: '2026-10-02T09:59:00Z',
      title: 'terminal',
      tool_name: 'terminal',
      status: 'running',
      metadata: { tool_call_id: 'one' },
    } as RuntimeSessionActivityItem;
    const tools = sessionTools(
      [
        event('result', {
          tools: [{ kind: 'result', call_id: 'one', text: 'done' }],
        }),
      ],
      [native]
    );
    expect(tools[0].phase).to.equal('completed');
  });
  it('correlates concurrent starts and completions and bounds stale requests', () => {
    const start1 = event(
      'a',
      { request_id: 'request-one', model_alias: 'example-model' },
      'model_gateway_request_started'
    );
    const start2 = event(
      'b',
      { request_id: 'request-two' },
      'model_gateway_request_started'
    );
    const finish = event('c', { request_id: 'request-one' });
    const now = Date.parse(start1.timestamp!) + 12000;
    expect(sessionWork([finish, start1, start2], false, now)).to.include({
      label: 'Model processing',
      count: 1,
    });
    expect(
      sessionWork([start1, event('unrelated', {})], false, now).label
    ).to.equal('Model processing');
    expect(sessionWork([start1], false, now + 120000).label).to.equal(
      'Activity unavailable'
    );
    expect(sessionWork([start1], true, now).label).to.equal('Ended');
    expect(sessionWork([event('legacy', {})], false, now).label).to.equal(
      'Waiting for agent'
    );
  });
});
