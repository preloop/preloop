import { expect } from '@open-wc/testing';
import {
  MAX_INFLIGHT_REQUEST_MS,
  deriveLiveSessionState,
  formatDuration,
  formatElapsed,
  inFlightGatewayRequests,
  normalizeToolCalls,
  summarizeToolArguments,
  toolStateFromActivityStatus,
} from './live-session';
import type { LiveToolCall } from './live-session';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';

/**
 * Synthetic Hermes-through-gateway fixtures.
 *
 * The reproduced case had 14 model interactions, six gateway calls, a
 * session-start row and ZERO native tool activity rows. Everything below is
 * invented: placeholder tool names, `example.com` targets, made-up call ids.
 */

function gatewayEvent(
  id: string,
  timestamp: string,
  extra: Partial<FlowGatewayEvent['payload']> = {}
): FlowGatewayEvent {
  return {
    id,
    execution_id: 'exec-1',
    timestamp,
    type: 'model_gateway_call',
    payload: { ...extra },
  };
}

function toolEntry(
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    id: 'call_1',
    stable_id: true,
    direction: 'call',
    name: 'terminal',
    dialect: 'openai_chat',
    arguments: '{"command": "pytest -q"}',
    result: null,
    is_error: null,
    redacted: false,
    truncated: false,
    ...overrides,
  };
}

function activity(
  overrides: Partial<RuntimeSessionActivityItem> = {}
): RuntimeSessionActivityItem {
  return {
    activity_type: 'tool_call',
    timestamp: '2026-10-02T09:00:00Z',
    title: 'Tool call',
    summary: null,
    status: null,
    api_usage_id: null,
    tool_name: null,
    server_name: null,
    auth_subject_type: null,
    api_key_id: null,
    api_key_name: null,
    estimated_cost: null,
    total_tokens: null,
    ...overrides,
  };
}

describe('normalizeToolCalls', () => {
  it('names a tool from gateway-only history with no native activity rows', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
    ]);

    expect(calls).to.have.length(1);
    expect(calls[0].name).to.equal('terminal');
    expect(calls[0].summary).to.equal('pytest -q');
  });

  it('reports a call with no result as requested, not completed', () => {
    const [call] = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
    ]);

    expect(call.state).to.equal('requested');
  });

  it('joins a call and its result by the provider id', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
      gatewayEvent('e2', '2026-10-02T09:00:12Z', {
        tool_activity: {
          entries: [
            toolEntry({
              direction: 'result',
              arguments: null,
              result: '42 passed',
            }),
          ],
        },
      }),
    ]);

    expect(calls).to.have.length(1);
    expect(calls[0].state).to.equal('completed');
    expect(calls[0].resultText).to.equal('42 passed');
  });

  it('keeps parallel calls with identical arguments apart', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [toolEntry({ id: 'call_a' }), toolEntry({ id: 'call_b' })],
        },
      }),
    ]);

    expect(calls).to.have.length(2);
    expect(calls.map((call) => call.id)).to.deep.equal(['call_a', 'call_b']);
  });

  it('keeps a history replay of the same call from becoming two rows', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [
            toolEntry({ id: 'call_a' }),
            toolEntry({
              id: 'call_a',
              direction: 'result',
              arguments: null,
              result: 'x',
            }),
          ],
        },
      }),
      gatewayEvent('e2', '2026-10-02T09:01:05Z', {
        tool_activity: {
          entries: [
            toolEntry({ id: 'call_a' }),
            toolEntry({
              id: 'call_a',
              direction: 'result',
              arguments: null,
              result: 'x',
            }),
          ],
        },
      }),
    ]);

    // Every request replays the whole conversation, so the second event
    // carries the first turn's call again under the same provider id. That is
    // one call observed twice, not two calls.
    expect(calls).to.have.length(1);
    expect(calls[0].state).to.equal('completed');
  });

  it('keeps a genuinely repeated call under a new id as two rows', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [
            toolEntry({ id: 'call_a' }),
            toolEntry({
              id: 'call_a',
              direction: 'result',
              arguments: null,
              result: 'x',
            }),
          ],
        },
      }),
      gatewayEvent('e2', '2026-10-02T09:01:05Z', {
        tool_activity: {
          entries: [
            toolEntry({ id: 'call_b' }),
            toolEntry({
              id: 'call_b',
              direction: 'result',
              arguments: null,
              result: 'x',
            }),
          ],
        },
      }),
    ]);

    expect(calls).to.have.length(2);
  });

  it('does not dedupe id-less calls that happen to look identical', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [
            toolEntry({ id: null, stable_id: false }),
            toolEntry({ id: null, stable_id: false }),
          ],
        },
      }),
    ]);

    expect(calls).to.have.length(2);
    expect(calls.every((call) => call.stableId === false)).to.equal(true);
  });

  it('marks an errored result as failed', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [
            toolEntry(),
            toolEntry({
              direction: 'result',
              arguments: null,
              result: 'command not found',
              is_error: true,
            }),
          ],
        },
      }),
    ]);

    expect(calls[0].state).to.equal('failed');
  });

  it('reports no duration when only one end of the call was observed', () => {
    const [call] = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
    ]);

    expect(call.durationMs).to.equal(null);
    expect(formatDuration(call.durationMs)).to.equal(null);
  });

  it('survives out-of-order arrival', () => {
    const calls = normalizeToolCalls([
      gatewayEvent('e2', '2026-10-02T09:00:12Z', {
        tool_activity: {
          entries: [
            toolEntry({ direction: 'result', arguments: null, result: 'ok' }),
          ],
        },
      }),
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
    ]);

    expect(calls[0].timestamp).to.equal('2026-10-02T09:00:05Z');
    expect(calls[0].state).to.equal('completed');
  });

  it('honours the capture policy instead of showing withheld arguments', () => {
    const [call] = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [toolEntry({ arguments: null, redacted: true })],
        },
      }),
    ]);

    expect(call.name).to.equal('terminal');
    expect(call.argumentsText).to.equal(null);
    expect(call.redacted).to.equal(true);
  });

  it('builds a row from a native tool_call activity with a duration', () => {
    const calls = normalizeToolCalls(
      [],
      [
        activity({
          tool_name: 'run_tests',
          server_name: 'ci',
          status: 'success',
          summary: 'pytest -q',
          metadata: {
            correlation_id: 'corr-1',
            arguments_summary: 'pytest -q',
            started_at: '2026-10-02T08:59:59Z',
          },
        }),
      ]
    );

    expect(calls).to.have.length(1);
    expect(calls[0].name).to.equal('run_tests');
    expect(calls[0].state).to.equal('completed');
    expect(calls[0].durationMs).to.equal(1000);
    expect(formatDuration(calls[0].durationMs)).to.equal('1.0s');
  });

  it('reports no duration for a native row with no start marker', () => {
    const [call] = normalizeToolCalls(
      [],
      [activity({ tool_name: 'run_tests', status: 'success' })]
    );

    expect(call.durationMs).to.equal(null);
  });

  it('never merges a native row into a gateway entry on a name match', () => {
    const calls = normalizeToolCalls(
      [
        gatewayEvent('e1', '2026-10-02T09:00:05Z', {
          tool_activity: { entries: [toolEntry({ name: 'terminal' })] },
        }),
      ],
      [activity({ tool_name: 'terminal', status: 'success' })]
    );

    expect(calls).to.have.length(2);
  });

  it('ignores activity rows that are not tool calls', () => {
    const calls = normalizeToolCalls(
      [],
      [
        activity({ activity_type: 'session_started', tool_name: null }),
        activity({ activity_type: 'model_interaction' }),
      ]
    );

    expect(calls).to.have.length(0);
  });

  it('renders malformed argument payloads as text instead of throwing', () => {
    const [call] = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: {
          entries: [toolEntry({ arguments: '{"path": "a.txt", "cont' })],
        },
      }),
    ]);

    expect(call.summary).to.equal('{"path": "a.txt", "cont');
  });
});

describe('summarizeToolArguments', () => {
  it('prefers the deciding argument', () => {
    expect(
      summarizeToolArguments('{"path": "src/index.ts", "mode": "r"}')
    ).to.equal('src/index.ts');
    expect(summarizeToolArguments('{"url": "https://example.com/x"}')).to.equal(
      'https://example.com/x'
    );
  });

  it('falls back to the first key when nothing looks decisive', () => {
    expect(summarizeToolArguments('{"alpha": 1, "beta": 2}')).to.equal(
      'alpha: 1'
    );
  });

  it('returns nothing for empty arguments', () => {
    expect(summarizeToolArguments(null)).to.equal('');
    expect(summarizeToolArguments('   ')).to.equal('');
  });
});

describe('toolStateFromActivityStatus', () => {
  it('maps free-text statuses onto evidence-backed states', () => {
    expect(toolStateFromActivityStatus('error')).to.equal('failed');
    expect(toolStateFromActivityStatus('completed')).to.equal('completed');
    expect(toolStateFromActivityStatus('running')).to.equal('running');
    expect(toolStateFromActivityStatus('')).to.equal('unknown');
    expect(toolStateFromActivityStatus(null)).to.equal('unknown');
  });
});

describe('inFlightGatewayRequests', () => {
  const started = (id: string, timestamp: string, model = 'openai/gpt-5') =>
    ({
      type: 'model_gateway_request_started',
      timestamp,
      payload: { gateway_request_id: id, model_alias: model },
    }) as FlowGatewayEvent;
  const completed = (id: string, timestamp: string) =>
    ({
      type: 'model_gateway_call',
      timestamp,
      payload: { gateway_request_id: id },
    }) as FlowGatewayEvent;

  it('pairs a start with its own completion, not the next one', () => {
    const now = Date.parse('2026-10-02T09:00:30Z');
    const inFlight = inFlightGatewayRequests(
      [
        started('a', '2026-10-02T09:00:00Z'),
        completed('a', '2026-10-02T09:00:05Z'),
        started('b', '2026-10-02T09:00:10Z'),
      ],
      now
    );

    expect(inFlight.map((request) => request.id)).to.deep.equal(['b']);
  });

  it('ignores a start with no correlation id rather than guessing', () => {
    const now = Date.parse('2026-10-02T09:00:30Z');
    const inFlight = inFlightGatewayRequests(
      [
        {
          type: 'model_gateway_request_started',
          timestamp: '2026-10-02T09:00:00Z',
          payload: {},
        } as FlowGatewayEvent,
      ],
      now
    );

    expect(inFlight).to.have.length(0);
  });

  it('stops claiming processing for a start that never arrived', () => {
    const startedAt = new Date(Date.parse('2026-10-02T09:00:00Z')).getTime();
    const inFlight = inFlightGatewayRequests(
      [started('a', '2026-10-02T09:00:00Z')],
      startedAt + MAX_INFLIGHT_REQUEST_MS + 1
    );

    expect(inFlight).to.have.length(0);
  });
});

describe('deriveLiveSessionState', () => {
  const now = Date.parse('2026-10-02T09:00:30Z');

  it('says nothing rather than guessing when nothing was observed', () => {
    expect(deriveLiveSessionState({ events: [], now }).status).to.equal(
      'unavailable'
    );
  });

  it('reports model processing for a tracked in-flight request', () => {
    const state = deriveLiveSessionState({
      events: [
        {
          type: 'model_gateway_request_started',
          timestamp: '2026-10-02T09:00:00Z',
          payload: { gateway_request_id: 'a', model_alias: 'openai/gpt-5' },
        } as FlowGatewayEvent,
      ],
      now,
    });

    expect(state.status).to.equal('model_processing');
    expect(state.detail).to.equal('openai/gpt-5');
    expect(state.since).to.equal(Date.parse('2026-10-02T09:00:00Z'));
  });

  it('lets a pending approval outrank an in-flight request', () => {
    const state = deriveLiveSessionState({
      events: [
        {
          type: 'model_gateway_request_started',
          timestamp: '2026-10-02T09:00:00Z',
          payload: { gateway_request_id: 'a', model_alias: 'openai/gpt-5' },
        } as FlowGatewayEvent,
      ],
      pendingApprovals: [
        { status: 'pending', requested_at: '2026-10-02T09:00:10Z' } as never,
      ],
      now,
    });

    expect(state.status).to.equal('waiting_for_approval');
    expect(state.pendingApprovalCount).to.equal(1);
    expect(state.since).to.equal(Date.parse('2026-10-02T09:00:10Z'));
  });

  it('ignores an already-resolved approval', () => {
    const state = deriveLiveSessionState({
      events: [activity({ activity_type: 'session_started' }) as never],
      pendingApprovals: [{ status: 'approved' } as never],
      now,
    });

    expect(state.status).to.equal('idle');
    expect(state.pendingApprovalCount).to.equal(0);
  });

  it('reports a tool observed running', () => {
    const state = deriveLiveSessionState({
      events: [],
      activity: [activity({ status: 'running' })],
      now,
    });

    expect(state.status).to.equal('running_tool');
  });

  it('reports waiting for agent on a queued operator turn', () => {
    const state = deriveLiveSessionState({
      events: [],
      activity: [
        activity({ activity_type: 'agent_control_message', status: 'queued' }),
      ],
      now,
    });

    expect(state.status).to.equal('waiting_for_agent');
  });

  it('reports ended once the session says so', () => {
    const state = deriveLiveSessionState({
      events: [
        {
          type: 'model_gateway_request_started',
          timestamp: '2026-10-02T09:00:00Z',
          payload: { gateway_request_id: 'a' },
        } as FlowGatewayEvent,
      ],
      ended: true,
      now,
    });

    expect(state.status).to.equal('ended');
  });

  it('keeps transport health out of the work status', () => {
    const state = deriveLiveSessionState({
      events: [activity({ activity_type: 'session_started' }) as never],
      connected: false,
      now,
    });

    expect(state.status).to.equal('idle');
    expect(state.transport).to.equal('reconnecting');
    expect(state.lastUpdateAt).to.equal(Date.parse('2026-10-02T09:00:00Z'));
  });

  it('walks the whole synthetic lifecycle in order', () => {
    const start = {
      type: 'model_gateway_request_started',
      timestamp: '2026-10-02T09:00:00Z',
      payload: { gateway_request_id: 'a', model_alias: 'openai/gpt-5' },
    } as FlowGatewayEvent;
    // The model request that asks for the tool, and the same request completed.
    const asked: FlowGatewayEvent[] = [
      start,
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
    ];
    const afterCompletion: FlowGatewayEvent[] = [
      start,
      ...asked.slice(1),
      gatewayEvent('e2', '2026-10-02T09:00:06Z', { gateway_request_id: 'a' }),
    ];

    expect(
      deriveLiveSessionState({
        events: [
          start,
          gatewayEvent('e1', '2026-10-02T09:00:00Z', {
            tool_activity: { entries: [] },
          }),
        ],
        now,
      }).status
    ).to.equal('model_processing');

    // A tool was requested; the model request is still open, so it leads.
    expect(deriveLiveSessionState({ events: asked, now }).status).to.equal(
      'model_processing'
    );

    // Approval needed: it outranks the request that is still open.
    expect(
      deriveLiveSessionState({
        events: asked,
        pendingApprovals: [{ status: 'pending' } as never],
        now,
      }).status
    ).to.equal('waiting_for_approval');

    // Request completed, approval resolved: the asked-for tool is not running,
    // so the session is idle — the card says `requested`, not `running`.
    expect(
      deriveLiveSessionState({ events: afterCompletion, now }).status
    ).to.equal('idle');

    // A tool observed executing by the gateway does read as running.
    expect(
      deriveLiveSessionState({
        events: afterCompletion,
        activity: [activity({ status: 'running' })],
        now,
      }).status
    ).to.equal('running_tool');

    expect(
      deriveLiveSessionState({
        events: [
          ...afterCompletion,
          activity({ activity_type: 'session_ended' }) as never,
        ],
        ended: true,
        now,
      }).status
    ).to.equal('ended');
  });
});

describe('formatElapsed', () => {
  it('is anchored to the start, not to the last render', () => {
    const since = Date.parse('2026-10-02T09:00:00Z');
    expect(formatElapsed(since, since + 12_000)).to.equal('12s');
    expect(formatElapsed(since, since + 90_000)).to.equal('1m 30s');
    expect(formatElapsed(null, since)).to.equal(null);
  });
});

describe('LiveToolCall shape', () => {
  it('always carries the fields a card needs to render without guessing', () => {
    const calls: LiveToolCall[] = normalizeToolCalls([
      gatewayEvent('e1', '2026-10-02T09:00:05Z', {
        tool_activity: { entries: [toolEntry()] },
      }),
    ]);
    expect(calls[0]).to.include.keys(
      'key',
      'id',
      'stableId',
      'name',
      'state',
      'summary',
      'argumentsText',
      'resultText',
      'durationMs',
      'timestamp',
      'redacted',
      'truncated'
    );
  });
});
