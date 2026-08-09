import { expect } from '@open-wc/testing';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';
import {
  buildConversation,
  collectRawToolResultPrefixes,
  isModelGatewayEventType,
  matchInjectedSegment,
} from './transcript';

function gatewayEvent(
  id: string,
  timestamp: string,
  messages: Array<{
    role: string;
    text: string;
    source?: string;
    redacted?: boolean;
    truncated?: boolean;
  }>,
  overrides: Record<string, unknown> = {}
): FlowGatewayEvent {
  return {
    id,
    execution_id: 'exec-1',
    timestamp,
    type: 'model_gateway_call',
    payload: {
      outcome: 'success',
      conversation_preview: {
        messages: messages.map((message) => ({
          source: message.source || 'request',
          role: message.role,
          text: message.text,
          redacted: Boolean(message.redacted),
          truncated: Boolean(message.truncated),
        })),
      },
      ...overrides,
    },
  } as FlowGatewayEvent;
}

describe('matchInjectedSegment', () => {
  it('matches known harness injection conventions', () => {
    expect(
      matchInjectedSegment('[Preloop] Question for the human operator:')?.id
    ).to.equal('preloop_notice');
    expect(
      matchInjectedSegment('before <system-reminder>x</system-reminder>')?.id
    ).to.equal('system_reminder');
    expect(
      matchInjectedSegment('Caveat: The messages below were generated')?.id
    ).to.equal('caveat');
    expect(
      matchInjectedSegment('<command-name>/clear</command-name>')?.id
    ).to.equal('local_command');
    expect(
      matchInjectedSegment(
        'This session is being continued from a previous conversation.'
      )?.id
    ).to.equal('compaction');
    expect(matchInjectedSegment('[Request interrupted by user]')?.id).to.equal(
      'interrupted'
    );
  });

  it('never matches ordinary prompts (when in doubt, keep the prompt)', () => {
    expect(matchInjectedSegment('Fix the login bug please')).to.equal(null);
    expect(matchInjectedSegment('The caveat here is we need tests')).to.equal(
      null
    );
  });
});

describe('collectRawToolResultPrefixes', () => {
  it('returns null without a captured raw body (honest degradation)', () => {
    const event = gatewayEvent('e1', '2026-08-06T10:00:00Z', [
      { role: 'user', text: 'hello' },
    ]);
    expect(collectRawToolResultPrefixes(event)).to.equal(null);
  });

  it('detects Anthropic tool_result blocks inside user messages', () => {
    const event = gatewayEvent(
      'e1',
      '2026-08-06T10:00:00Z',
      [{ role: 'user', text: 'file contents here' }],
      {
        request: {
          messages: [
            {
              role: 'user',
              content: [
                {
                  type: 'tool_result',
                  tool_use_id: 't1',
                  content: [{ type: 'text', text: 'file contents here' }],
                },
              ],
            },
          ],
        },
      }
    );
    const prefixes = collectRawToolResultPrefixes(event);
    expect(prefixes).to.not.equal(null);
    expect(prefixes!.has('file contents here')).to.equal(true);
  });

  it('detects OpenAI tool-role messages and Responses function_call_output', () => {
    const event = gatewayEvent('e1', '2026-08-06T10:00:00Z', [], {
      request: {
        messages: [{ role: 'tool', content: 'exit code 0' }],
        input: [{ type: 'function_call_output', output: 'ls output' }],
      },
    });
    const prefixes = collectRawToolResultPrefixes(event)!;
    expect(prefixes.has('exit code 0')).to.equal(true);
    expect(prefixes.has('ls output')).to.equal(true);
  });

  it('returns null when tool results exist but yield no matchable text', () => {
    // Unrecognized structure: exact detection would silently misclassify
    // every tool result as a prompt, so this must degrade to "no raw body".
    const event = gatewayEvent('e1', '2026-08-06T10:00:00Z', [], {
      request: {
        messages: [
          {
            role: 'user',
            content: [{ type: 'tool_result', content: [{ weird: true }] }],
          },
        ],
      },
    });
    expect(collectRawToolResultPrefixes(event)).to.equal(null);
  });

  it('returns an empty set when the raw body truly has no tool results', () => {
    const event = gatewayEvent('e1', '2026-08-06T10:00:00Z', [], {
      request: { messages: [{ role: 'user', content: 'plain prompt' }] },
    });
    const prefixes = collectRawToolResultPrefixes(event);
    expect(prefixes).to.not.equal(null);
    expect(prefixes!.size).to.equal(0);
  });
});

describe('isModelGatewayEventType', () => {
  it('prefix-matches gateway event types, not substrings', () => {
    expect(isModelGatewayEventType('model_gateway_call')).to.equal(true);
    expect(isModelGatewayEventType('model_gateway')).to.equal(true);
    expect(isModelGatewayEventType('audit_model_gateway_call')).to.equal(false);
    expect(isModelGatewayEventType('tool_call')).to.equal(false);
  });
});

describe('buildConversation', () => {
  it('expands prompts and final responses; collapses tool results exactly', () => {
    const events = [
      gatewayEvent(
        'e1',
        '2026-08-06T10:00:00Z',
        [
          { role: 'user', text: 'Fix the login bug' },
          { role: 'assistant', text: 'Reading auth.ts', source: 'response' },
        ],
        {}
      ),
      gatewayEvent(
        'e2',
        '2026-08-06T10:01:00Z',
        [
          { role: 'user', text: 'Fix the login bug' },
          { role: 'user', text: 'contents of auth.ts: export const x = 1' },
          {
            role: 'assistant',
            text: 'Fixed it in auth.ts',
            source: 'response',
          },
        ],
        {
          request: {
            messages: [
              { role: 'user', content: 'Fix the login bug' },
              {
                role: 'user',
                content: [
                  {
                    type: 'tool_result',
                    content: 'contents of auth.ts: export const x = 1',
                  },
                ],
              },
            ],
          },
        }
      ),
    ];

    const { items, stats } = buildConversation(events);
    const messages = items.filter((item) => item.type === 'message');
    expect(messages).to.have.length(2);
    expect(messages[0]).to.include({ kind: 'user_prompt' });
    expect(messages[1]).to.include({
      kind: 'agent_response',
      text: 'Fixed it in auth.ts',
    });

    // The tool result (Anthropic user-role shape) and the intermediate
    // response are collapsed steps between prompt and final response.
    const stepGroups = items.filter((item) => item.type === 'steps');
    expect(stepGroups).to.have.length(1);
    const kinds = (
      stepGroups[0] as { steps: Array<{ kind: string }> }
    ).steps.map((step) => step.kind);
    expect(kinds).to.include('tool_result');
    expect(kinds).to.include('intermediate');
    expect(stats.promptCount).to.equal(1);
    expect(stats.responseCount).to.equal(1);
    expect(stats.toolResultCount).to.equal(1);
  });

  it('deduplicates the growing prefix across requests', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'prompt one' },
        { role: 'assistant', text: 'answer one', source: 'response' },
      ]),
      gatewayEvent('e2', '2026-08-06T10:02:00Z', [
        { role: 'user', text: 'prompt one' },
        { role: 'assistant', text: 'answer one' },
        { role: 'user', text: 'prompt two' },
        { role: 'assistant', text: 'answer two', source: 'response' },
      ]),
    ];
    const { items, stats } = buildConversation(events);
    const prompts = items.filter(
      (item) => item.type === 'message' && item.kind === 'user_prompt'
    );
    expect(prompts).to.have.length(2);
    expect(stats.promptCount).to.equal(2);
    // "answer one" replayed as request history collapses to a step, and the
    // final response of each exchange stays expanded.
    const responses = items.filter(
      (item) => item.type === 'message' && item.kind === 'agent_response'
    );
    expect(responses).to.have.length(2);
  });

  it('collapses injected segments and counts them', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: '<system-reminder>plan mode</system-reminder>' },
        { role: 'user', text: 'Real prompt' },
        { role: 'user', text: '[Preloop] Question for the human operator:' },
        { role: 'assistant', text: 'ok', source: 'response' },
      ]),
    ];
    const { items, stats } = buildConversation(events);
    expect(stats.injectedCount).to.equal(2);
    const prompts = items.filter(
      (item) => item.type === 'message' && item.kind === 'user_prompt'
    );
    expect(prompts).to.have.length(1);
    expect((prompts[0] as { text: string }).text).to.equal('Real prompt');
  });

  it('reports missing raw bodies instead of guessing', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'could be a prompt or a tool result' },
        { role: 'assistant', text: 'ok', source: 'response' },
      ]),
    ];
    const { stats } = buildConversation(events);
    expect(stats.eventsWithoutRawBody).to.equal(1);
    expect(stats.totalEvents).to.equal(1);
  });

  it('counts raw bodies with unusable tool-result structure as unavailable', () => {
    // Raw body present but its tool_result blocks yield no matchable text:
    // exact detection is impossible, so the disclosure stat must include it.
    const events = [
      gatewayEvent(
        'e1',
        '2026-08-06T10:00:00Z',
        [
          { role: 'user', text: 'possibly a tool result' },
          { role: 'assistant', text: 'ok', source: 'response' },
        ],
        {
          request: {
            messages: [
              {
                role: 'user',
                content: [{ type: 'tool_result', content: [{ weird: 1 }] }],
              },
            ],
          },
        }
      ),
    ];
    const { stats } = buildConversation(events);
    expect(stats.eventsWithoutRawBody).to.equal(1);
  });

  it('keeps identical texts at different conversation positions distinct', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'continue' },
        { role: 'assistant', text: 'step one done', source: 'response' },
      ]),
      gatewayEvent('e2', '2026-08-06T10:01:00Z', [
        { role: 'user', text: 'continue' },
        { role: 'assistant', text: 'step one done' },
        { role: 'user', text: 'continue' },
        { role: 'assistant', text: 'step two done', source: 'response' },
      ]),
    ];
    const { stats } = buildConversation(events);
    // The user saying "continue" twice is two prompts, not one.
    expect(stats.promptCount).to.equal(2);
  });

  it('classifies system messages as collapsed steps', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'system', text: 'You are a helpful agent' },
        { role: 'user', text: 'hi' },
        { role: 'assistant', text: 'hello', source: 'response' },
      ]),
    ];
    const { items } = buildConversation(events);
    const stepGroups = items.filter((item) => item.type === 'steps');
    expect(stepGroups).to.have.length(1);
    expect(
      (stepGroups[0] as { steps: Array<{ kind: string }> }).steps[0].kind
    ).to.equal('system');
  });

  it('interleaves tool_call activity, operator messages and lifecycle dividers', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:01:00Z', [
        { role: 'user', text: 'run the tests' },
        { role: 'assistant', text: 'done', source: 'response' },
      ]),
    ];
    const activity: RuntimeSessionActivityItem[] = [
      {
        activity_type: 'session_started',
        timestamp: '2026-08-06T10:00:00Z',
        title: 'Session started',
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
      },
      {
        activity_type: 'tool_call',
        timestamp: '2026-08-06T10:01:30Z',
        title: 'Tool call',
        summary: 'pytest -q',
        status: 'completed',
        api_usage_id: null,
        tool_name: 'run_tests',
        server_name: 'ci',
        auth_subject_type: null,
        api_key_id: null,
        api_key_name: null,
        estimated_cost: null,
        total_tokens: null,
      },
      {
        activity_type: 'agent_control_message',
        timestamp: '2026-08-06T10:02:00Z',
        title: 'Operator message',
        summary: 'please also run lint',
        status: 'delivered',
        api_usage_id: null,
        tool_name: null,
        server_name: null,
        auth_subject_type: null,
        api_key_id: null,
        api_key_name: null,
        estimated_cost: null,
        total_tokens: null,
      },
    ];
    const { items, stats } = buildConversation(events, activity);
    expect(items[0]).to.include({ type: 'divider', label: 'Session started' });
    expect(stats.toolCallCount).to.equal(1);
    const operator = items.find(
      (item) => item.type === 'message' && item.kind === 'operator'
    );
    expect(operator).to.not.equal(undefined);
    expect((operator as { text: string }).text).to.equal(
      'please also run lint'
    );
  });

  it('keeps redacted messages visible with their redaction flag', () => {
    const events = [
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: '', redacted: true },
        { role: 'assistant', text: '', source: 'response', redacted: true },
      ]),
    ];
    const { items } = buildConversation(events);
    const messages = items.filter((item) => item.type === 'message');
    expect(messages.length).to.be.greaterThan(0);
    expect((messages[0] as { redacted?: boolean }).redacted).to.equal(true);
  });

  it('accepts events in newest-first order (observer storage order)', () => {
    const events = [
      gatewayEvent('e2', '2026-08-06T10:05:00Z', [
        { role: 'user', text: 'first prompt' },
        { role: 'user', text: 'second prompt' },
        { role: 'assistant', text: 'second answer', source: 'response' },
      ]),
      gatewayEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'first prompt' },
        { role: 'assistant', text: 'first answer', source: 'response' },
      ]),
    ];
    const { items } = buildConversation(events);
    const messageTexts = items
      .filter((item) => item.type === 'message')
      .map((item) => (item as { text: string }).text);
    expect(messageTexts).to.deep.equal([
      'first prompt',
      'first answer',
      'second prompt',
      'second answer',
    ]);
  });
});
