import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './session-replay-panel';
import type { SessionReplayPanel } from './session-replay-panel';
import type { FlowGatewayEvent } from '../types';

const SESSION = {
  id: 'session-1',
  sourceId: 'hermes-1',
  sourceType: 'hermes',
  title: 'Hermes',
  subtitle: null,
  sessionReference: null,
  runtimePrincipalName: 'Hermes',
  flowName: null,
  flowExecutionId: null,
  status: 'active_now',
  startedAt: '2026-06-07T12:00:00Z',
  lastActivityAt: '2026-06-07T12:05:00Z',
  endedAt: null,
  totalRequests: 0,
  successfulRequests: 0,
  failedRequests: 0,
  tokenUsage: {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
  },
  estimatedCost: 0,
  latestModelAlias: null,
  latestProviderName: null,
  canLoadEvents: true,
  raw: {},
};

function previewEvent(
  id: string,
  timestamp: string,
  messages: Array<{ role: string; text: string }>,
  overrides: Record<string, unknown> = {}
): FlowGatewayEvent {
  return {
    id,
    execution_id: 'exec-1',
    timestamp,
    type: 'model_gateway_call',
    payload: {
      model_alias: 'gpt-test',
      outcome: 'success',
      total_tokens: 100,
      prompt_tokens: 80,
      completion_tokens: 20,
      estimated_cost: 0.01,
      conversation_preview: { messages },
      ...overrides,
    },
  };
}

describe('SessionReplayPanel', () => {
  it('renders agent control messages in chat mode', async () => {
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${[]}
        .activity=${[
          {
            activity_type: 'agent_control_message',
            timestamp: '2026-06-07T12:05:00Z',
            title: 'Operator message',
            summary: 'Please inspect the failing test.',
            status: 'delivered',
            api_usage_id: null,
            tool_name: null,
            server_name: null,
            auth_subject_type: null,
            api_key_id: null,
            api_key_name: null,
            estimated_cost: null,
            total_tokens: null,
            metadata: { source_metadata: { source: 'web' } },
          },
        ]}
      ></session-replay-panel>
    `);

    const text = element.shadowRoot?.textContent || '';
    expect(text).to.include('Please inspect the failing test.');
    expect(text).to.not.include('No conversation preview captured.');
  });

  it('deduplicates re-sent messages across turns (delta model)', async () => {
    // Request 2 re-sends request 1's message plus a new tail. The shared
    // message must appear exactly once across the whole thread.
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'FIRST_USER_MESSAGE' },
        { role: 'assistant', text: 'FIRST_ASSISTANT_REPLY' },
      ]),
      previewEvent('e2', '2026-06-07T12:01:00Z', [
        { role: 'user', text: 'FIRST_USER_MESSAGE' },
        { role: 'assistant', text: 'FIRST_ASSISTANT_REPLY' },
        { role: 'user', text: 'SECOND_USER_MESSAGE' },
        { role: 'assistant', text: 'SECOND_ASSISTANT_REPLY' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const text = element.shadowRoot?.textContent || '';
    const occurrences = (text.match(/FIRST_USER_MESSAGE/g) || []).length;
    expect(occurrences, 'shared message rendered once').to.equal(1);
    expect(text).to.include('SECOND_USER_MESSAGE');
  });

  it('renders a per-turn cost/token/tool header for every turn', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'Hello' },
        { role: 'tool', text: 'tool call payload' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const text = (element.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.include('tok');
    expect(text).to.include('$');
    expect(text).to.match(/1 tool/);
  });

  it('reorders turns when sort changes (newest first)', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'OLDEST_TURN' },
      ]),
      previewEvent('e2', '2026-06-07T12:05:00Z', [
        { role: 'user', text: 'NEWEST_TURN' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    // Newest-first is the default: the latest turn renders before older ones.
    const oldestIndex =
      element.shadowRoot?.textContent?.indexOf('OLDEST_TURN') ?? -1;
    const newestIndex =
      element.shadowRoot?.textContent?.indexOf('NEWEST_TURN') ?? -1;
    expect(newestIndex).to.be.lessThan(oldestIndex);

    const select = element.shadowRoot?.querySelector(
      'select[aria-label="Sort turns"]'
    ) as HTMLSelectElement;
    select.value = 'oldest';
    select.dispatchEvent(new Event('change'));
    await element.updateComplete;

    const text = element.shadowRoot?.textContent || '';
    expect(text.indexOf('OLDEST_TURN')).to.be.lessThan(
      text.indexOf('NEWEST_TURN')
    );
  });

  it('orders messages inside a turn to match the turn sort', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'TURN_PROMPT' },
        { role: 'assistant', text: 'TURN_REPLY' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    // Newest-first (default): the latest message of the turn renders first.
    let text = element.shadowRoot?.textContent || '';
    expect(text.indexOf('TURN_REPLY')).to.be.lessThan(
      text.indexOf('TURN_PROMPT')
    );

    // Oldest-first restores natural conversation order.
    const select = element.shadowRoot?.querySelector(
      'select[aria-label="Sort turns"]'
    ) as HTMLSelectElement;
    select.value = 'oldest';
    select.dispatchEvent(new Event('change'));
    await element.updateComplete;
    text = element.shadowRoot?.textContent || '';
    expect(text.indexOf('TURN_PROMPT')).to.be.lessThan(
      text.indexOf('TURN_REPLY')
    );
  });

  it('collapses the re-sent (cached) prefix inside full request context', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'EARLIER_TURN_MESSAGE' },
      ]),
      previewEvent(
        'e2',
        '2026-06-07T12:01:00Z',
        [
          { role: 'user', text: 'EARLIER_TURN_MESSAGE' },
          { role: 'user', text: 'FRESH_TAIL_MESSAGE' },
        ],
        { usage_details: { prompt_tokens_details: { cached_tokens: 640 } } }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    // Expand the SECOND turn's full context (its request re-sends turn 1).
    element.eventDetails = {
      e2: {
        ...events[1],
        payload: {
          ...events[1].payload,
          request: {
            messages: [
              { role: 'system', content: 'CACHED_SYSTEM_PROMPT' },
              { role: 'user', content: 'EARLIER_TURN_MESSAGE' },
              { role: 'user', content: 'FRESH_TAIL_MESSAGE' },
            ],
          },
        },
      },
    };
    const expandButtons = Array.from(
      element.shadowRoot?.querySelectorAll('sl-button') || []
    ).filter((button) =>
      (button.textContent || '').includes('Expand full context')
    ) as HTMLElement[];
    // Newest-first: the first expand button belongs to the newest turn (e2).
    expandButtons[0].click();
    await element.updateComplete;
    await waitUntil(() =>
      element.shadowRoot?.querySelector('.cached-prefix-details')
    );

    const prefix = element.shadowRoot?.querySelector(
      '.cached-prefix-details'
    ) as HTMLElement & { open?: boolean };
    expect(prefix, 'cached prefix container present').to.exist;
    // Collapsed by default, labelled with the count and cache annotation.
    expect(Boolean(prefix.open)).to.equal(false);
    const summaryText = (
      prefix.querySelector('[slot="summary"]')?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(summaryText).to.contain('re-sent from previous turns');
    expect(summaryText).to.contain('640');
    // The prefix holds ONLY the re-sent context; the fresh tail stays outside.
    expect(prefix.textContent).to.contain('CACHED_SYSTEM_PROMPT');
    expect(prefix.textContent).to.contain('EARLIER_TURN_MESSAGE');
    expect(prefix.textContent).to.not.contain('FRESH_TAIL_MESSAGE');
  });

  it('shows the full context unsplit when no delta message matches', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'PREVIEW_ONLY_TEXT' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    // The raw request text differs from the preview (e.g. truncation), so no
    // signature matches: nothing must be hidden as "cached".
    element.eventDetails = {
      e1: {
        ...events[0],
        payload: {
          ...events[0].payload,
          request: {
            messages: [
              { role: 'system', content: 'RAW_SYSTEM_PROMPT' },
              { role: 'user', content: 'RAW_DIVERGENT_TEXT' },
            ],
          },
        },
      },
    };
    const expandButton = Array.from(
      element.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) =>
      (button.textContent || '').includes('Expand full context')
    ) as HTMLElement;
    expandButton.click();
    await element.updateComplete;
    await waitUntil(() =>
      (element.shadowRoot?.textContent || '').includes('RAW_SYSTEM_PROMPT')
    );
    expect(element.shadowRoot?.querySelector('.cached-prefix-details')).to.not
      .exist;
  });

  it('hides turns below the token threshold', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'CHEAP_TURN' }],
        { total_tokens: 10, estimated_cost: 0.001 }
      ),
      previewEvent(
        'e2',
        '2026-06-07T12:05:00Z',
        [{ role: 'user', text: 'EXPENSIVE_TURN' }],
        { total_tokens: 5000, estimated_cost: 1.5 }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    expect(element.shadowRoot?.textContent || '').to.include('CHEAP_TURN');

    const input = element.shadowRoot?.querySelector(
      'input[aria-label="Threshold"]'
    ) as HTMLInputElement;
    input.value = '1000';
    input.dispatchEvent(new Event('input'));
    await element.updateComplete;

    const text = element.shadowRoot?.textContent || '';
    expect(text).to.include('EXPENSIVE_TURN');
    expect(text).to.not.include('CHEAP_TURN');
  });

  it('filters by event type (tool calls only)', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'PLAIN_MESSAGE_TURN' },
      ]),
      previewEvent('e2', '2026-06-07T12:05:00Z', [
        { role: 'tool', text: 'TOOL_CALL_TURN' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const select = element.shadowRoot?.querySelector(
      'select[aria-label="Filter by type"]'
    ) as HTMLSelectElement;
    select.value = 'tools';
    select.dispatchEvent(new Event('change'));
    await element.updateComplete;

    const text = element.shadowRoot?.textContent || '';
    expect(text).to.include('TOOL_CALL_TURN');
    expect(text).to.not.include('PLAIN_MESSAGE_TURN');
  });

  it('expands a turn to show full request context, requesting detail', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'EXPAND_ME' },
      ]),
    ];
    let requestedEventId: string | null = null;
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="timeline"
        .session=${SESSION}
        .events=${events}
        @session-event-detail-requested=${(event: CustomEvent) => {
          requestedEventId = event.detail.eventId;
        }}
      ></session-replay-panel>
    `);
    const expandButton = Array.from(
      element.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((button) =>
      (button.textContent || '').includes('Expand full context')
    ) as HTMLElement;
    expect(expandButton, 'expand button present').to.exist;
    expandButton.click();
    await element.updateComplete;
    expect(requestedEventId).to.equal('e1');

    // Provide the lazily-loaded detail; the full context message list renders.
    element.eventDetails = {
      e1: {
        ...events[0],
        payload: {
          ...events[0].payload,
          request: {
            messages: [
              { role: 'system', content: 'FULL_CONTEXT_SYSTEM_PROMPT' },
              { role: 'user', content: 'EXPAND_ME' },
            ],
          },
        },
      },
    };
    await element.updateComplete;
    await waitUntil(() =>
      (element.shadowRoot?.textContent || '').includes(
        'FULL_CONTEXT_SYSTEM_PROMPT'
      )
    );
    expect(element.shadowRoot?.textContent || '').to.include(
      'FULL_CONTEXT_SYSTEM_PROMPT'
    );
  });

  // --- Fix 1: per-message "show full message" toggle ------------------------

  it('expands and collapses a single long message per-message', async () => {
    const longA = 'A'.repeat(2500);
    const longB = 'B'.repeat(2500);
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'system', text: longA },
        { role: 'user', text: longB },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const lengths = () =>
      Array.from(element.shadowRoot!.querySelectorAll('.message-text')).map(
        (node) => (node.textContent || '').length
      );
    const toggleButtons = () =>
      Array.from(element.shadowRoot!.querySelectorAll('sl-button')).filter(
        (button) => (button.textContent || '').includes('Show full message')
      ) as HTMLElement[];

    const before = lengths();
    expect(before.length, 'two truncated bubbles').to.equal(2);
    expect(before[0]).to.be.lessThan(2000);
    expect(before[1]).to.be.lessThan(2000);
    expect(toggleButtons().length).to.equal(2);

    // Expand ONLY the second message.
    toggleButtons()[1].click();
    await element.updateComplete;
    const afterExpand = lengths();
    expect(afterExpand[0], 'first stays collapsed').to.equal(before[0]);
    expect(afterExpand[1], 'second expands').to.equal(2500);

    // The expanded message now offers a Collapse control; collapse it again.
    const collapseButton = Array.from(
      element.shadowRoot!.querySelectorAll('sl-button')
    ).find((button) =>
      (button.textContent || '').includes('Collapse message')
    ) as HTMLElement;
    expect(collapseButton, 'collapse button present').to.exist;
    collapseButton.click();
    await element.updateComplete;
    const afterCollapse = lengths();
    expect(afterCollapse[1], 'second collapses again').to.equal(before[1]);
    expect(afterCollapse[0], 'first untouched').to.equal(before[0]);
  });

  // --- Fix 2: activity (operator) turns labelled correctly, no stat zeros ---

  it('labels operator activity turns and suppresses 0 stats', async () => {
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${[]}
        .activity=${[
          {
            activity_type: 'agent_control_message',
            timestamp: '2026-06-07T12:05:00Z',
            title: 'Operator message',
            summary: 'Please inspect the failing test.',
            status: 'delivered',
            api_usage_id: null,
            tool_name: null,
            server_name: null,
            auth_subject_type: null,
            api_key_id: null,
            api_key_name: null,
            estimated_cost: null,
            total_tokens: null,
            metadata: { role: 'user' },
          },
        ]}
      ></session-replay-panel>
    `);
    const title = element.shadowRoot?.querySelector('.chat-turn-title');
    expect(title?.textContent || '').to.contain('Operator message');
    expect(title?.textContent || '').to.not.contain('Developer message');

    // The activity turn must NOT show the meaningless 0-stat header pills.
    const turn = element.shadowRoot?.querySelector('.chat-turn');
    const header = turn?.querySelector('.chat-turn-header');
    const headerText = (header?.textContent || '').replace(/\s+/g, ' ');
    expect(headerText).to.not.contain('0 tok');
    expect(headerText).to.not.contain('$0.00');
    expect(headerText).to.not.contain('0 tools');
    expect(turn?.querySelectorAll('.chat-turn-stat').length).to.equal(0);
  });

  // --- Fix 3: cached tokens surfaced when present, omitted when absent ------

  it('shows cached tokens in the turn header when present', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'Hello cached' }],
        {
          total_tokens: 15463,
          usage_details: { prompt_tokens_details: { cached_tokens: 14210 } },
        }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const text = (
      element.shadowRoot?.querySelector('.chat-turn-header')?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(text).to.contain('cached');
    expect(text).to.contain('14.2k cached');
  });

  it('reads Anthropic cache_read_input_tokens too', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'Hello anthropic' }],
        { total_tokens: 9000, cache_read_input_tokens: 512 }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const text = (
      element.shadowRoot?.querySelector('.chat-turn-header')?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(text).to.contain('512 cached');
  });

  it('omits the cached annotation when no cache fields are present', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'No cache here' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const text =
      element.shadowRoot?.querySelector('.chat-turn-header')?.textContent || '';
    expect(text).to.not.contain('cached');
  });

  // --- Fix 4: slider <-> number input stay in sync --------------------------

  it('keeps the threshold slider and number input in sync', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'CHEAP_TURN' }],
        { total_tokens: 100, estimated_cost: 0.001 }
      ),
      previewEvent(
        'e2',
        '2026-06-07T12:05:00Z',
        [{ role: 'user', text: 'EXPENSIVE_TURN' }],
        { total_tokens: 8000, estimated_cost: 2 }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const slider = element.shadowRoot?.querySelector(
      'sl-range[aria-label="Threshold slider"]'
    ) as HTMLInputElement & { value: number };
    const input = element.shadowRoot?.querySelector(
      'input[aria-label="Threshold"]'
    ) as HTMLInputElement;
    expect(slider, 'slider present').to.exist;

    // Move the slider -> number input + filtering update.
    slider.value = 5000;
    slider.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    expect(Number(input.value)).to.equal(5000);
    let text = element.shadowRoot?.textContent || '';
    expect(text).to.include('EXPENSIVE_TURN');
    expect(text).to.not.include('CHEAP_TURN');

    // Type into the number input -> slider follows.
    input.value = '50';
    input.dispatchEvent(new Event('input'));
    await element.updateComplete;
    expect(Number(slider.value)).to.equal(50);
    text = element.shadowRoot?.textContent || '';
    expect(text).to.include('CHEAP_TURN');
    expect(text).to.include('EXPENSIVE_TURN');
  });

  // --- Win 1: whole-session cost-summary bar --------------------------------

  it('renders an aggregated session cost-summary bar', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [
          { role: 'user', text: 'First request' },
          { role: 'tool', text: 'tool call one' },
        ],
        {
          total_tokens: 1000,
          prompt_tokens: 800,
          completion_tokens: 200,
          estimated_cost: 0.25,
          usage_details: { prompt_tokens_details: { cached_tokens: 400 } },
        }
      ),
      previewEvent(
        'e2',
        '2026-06-07T12:01:00Z',
        [
          { role: 'user', text: 'Second request' },
          { role: 'tool', text: 'tool call two' },
        ],
        {
          total_tokens: 2000,
          prompt_tokens: 1200,
          completion_tokens: 800,
          estimated_cost: 0.75,
          outcome: 'error',
          status_code: 500,
          usage_details: { prompt_tokens_details: { cached_tokens: 200 } },
        }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const bar = element.shadowRoot?.querySelector('.chat-summary-bar');
    expect(bar, 'summary bar present').to.exist;
    const text = (bar?.textContent || '').replace(/\s+/g, ' ');
    // Total cost = 0.25 + 0.75 = 1.00.
    expect(text).to.contain('$1.00');
    // Total tokens = 1000 + 2000 = 3000.
    expect(text).to.contain('3,000');
    // Cached tokens = 400 + 200 = 600; prompt tokens = 2000 -> 30%.
    expect(text).to.contain('600 cached');
    expect(text).to.contain('30%');
    // Tool calls = 1 + 1 = 2; requests = 2; outcome 1 ok / 1 failed.
    expect(text).to.contain('1 ok / 1 failed');
    expect(text).to.match(/Requests\s*2/i);
  });

  it('omits the summary bar when there are no request turns', async () => {
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${[]}
      ></session-replay-panel>
    `);
    expect(element.shadowRoot?.querySelector('.chat-summary-bar')).to.not.exist;
  });

  // --- Win 2: highlight the single most-expensive turn ----------------------

  it('marks only the single most-expensive request turn', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'CHEAP_TURN' }],
        { estimated_cost: 0.01 }
      ),
      previewEvent(
        'e2',
        '2026-06-07T12:01:00Z',
        [{ role: 'user', text: 'PRICEY_TURN' }],
        { estimated_cost: 2.5 }
      ),
      previewEvent(
        'e3',
        '2026-06-07T12:02:00Z',
        [{ role: 'user', text: 'MID_TURN' }],
        { estimated_cost: 0.5 }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const marked = element.shadowRoot?.querySelectorAll(
      '.chat-turn.most-expensive'
    );
    expect(marked?.length, 'exactly one turn marked').to.equal(1);
    expect(marked?.[0].textContent || '').to.contain('PRICEY_TURN');
    expect(marked?.[0].textContent || '').to.contain('Most expensive');
  });

  it('does not mark a most-expensive turn with a single request', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'ONLY_TURN' }],
        { estimated_cost: 2.5 }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    expect(
      element.shadowRoot?.querySelectorAll('.chat-turn.most-expensive').length
    ).to.equal(0);
  });

  it('does not mark a most-expensive turn when all costs are zero', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent(
        'e1',
        '2026-06-07T12:00:00Z',
        [{ role: 'user', text: 'FREE_ONE' }],
        { estimated_cost: 0 }
      ),
      previewEvent(
        'e2',
        '2026-06-07T12:01:00Z',
        [{ role: 'user', text: 'FREE_TWO' }],
        { estimated_cost: 0 }
      ),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    expect(
      element.shadowRoot?.querySelectorAll('.chat-turn.most-expensive').length
    ).to.equal(0);
  });

  // --- UX: relative timestamps with absolute on hover -----------------------

  it('shows a relative turn timestamp with the absolute time on hover', async () => {
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', fiveMinutesAgo, [
        { role: 'user', text: 'RECENT_TURN' },
      ]),
    ];
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
      ></session-replay-panel>
    `);
    const time = element.shadowRoot?.querySelector(
      '.chat-turn-time'
    ) as HTMLElement;
    expect(time, 'turn time present').to.exist;
    expect(time.textContent || '').to.match(/5m ago/);
    // The precise timestamp stays one hover away.
    expect(time.getAttribute('title') || '').to.not.equal('');
  });

  // --- UX: clickable summary-bar stats jump to turns -------------------------

  it('jumps to the most-expensive turn from the Cost summary stat', async () => {
    // jsdom-less browsers in the test runner may lack scrollIntoView on some
    // elements; stub it so the click handler can run end to end.
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = () => {};
    try {
      const events: FlowGatewayEvent[] = [
        previewEvent(
          'e1',
          '2026-06-07T12:00:00Z',
          [{ role: 'user', text: 'CHEAP_TURN' }],
          { estimated_cost: 0.01 }
        ),
        previewEvent(
          'e2',
          '2026-06-07T12:01:00Z',
          [{ role: 'user', text: 'PRICEY_TURN' }],
          { estimated_cost: 2.5 }
        ),
      ];
      const element = await fixture<SessionReplayPanel>(html`
        <session-replay-panel
          replayMode="chat"
          .session=${SESSION}
          .events=${events}
        ></session-replay-panel>
      `);
      const costLink = Array.from(
        element.shadowRoot?.querySelectorAll('button.chat-summary-link') || []
      ).find((button) =>
        (button.textContent || '').includes('Cost')
      ) as HTMLElement;
      expect(costLink, 'clickable Cost stat present').to.exist;
      costLink.click();
      await element.updateComplete;
      const highlighted = element.shadowRoot?.querySelector(
        '.chat-turn.jump-highlight'
      );
      expect(highlighted, 'a turn is highlighted').to.exist;
      expect(highlighted?.getAttribute('data-event-id')).to.equal('e2');
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it('jumps to the first failed turn from the Outcome summary stat', async () => {
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = () => {};
    try {
      const events: FlowGatewayEvent[] = [
        previewEvent('e1', '2026-06-07T12:00:00Z', [
          { role: 'user', text: 'OK_TURN' },
        ]),
        previewEvent(
          'e2',
          '2026-06-07T12:01:00Z',
          [{ role: 'user', text: 'FAILED_TURN' }],
          { outcome: 'error', status_code: 500 }
        ),
      ];
      const element = await fixture<SessionReplayPanel>(html`
        <session-replay-panel
          replayMode="chat"
          .session=${SESSION}
          .events=${events}
        ></session-replay-panel>
      `);
      const outcomeLink = Array.from(
        element.shadowRoot?.querySelectorAll('button.chat-summary-link') || []
      ).find((button) =>
        (button.textContent || '').includes('Outcome')
      ) as HTMLElement;
      expect(outcomeLink, 'clickable Outcome stat present').to.exist;
      outcomeLink.click();
      await element.updateComplete;
      const highlighted = element.shadowRoot?.querySelector(
        '.chat-turn.jump-highlight'
      );
      expect(highlighted, 'a turn is highlighted').to.exist;
      expect(highlighted?.getAttribute('data-event-id')).to.equal('e2');
      expect(highlighted?.textContent || '').to.contain('FAILED_TURN');
    } finally {
      Element.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  // --- UX: keyboard navigation over the chat thread --------------------------

  it('navigates turns with j/k and expands with Enter', async () => {
    const events: FlowGatewayEvent[] = [
      previewEvent('e1', '2026-06-07T12:00:00Z', [
        { role: 'user', text: 'FIRST_TURN' },
      ]),
      previewEvent('e2', '2026-06-07T12:01:00Z', [
        { role: 'user', text: 'SECOND_TURN' },
      ]),
    ];
    let requestedEventId: string | null = null;
    const element = await fixture<SessionReplayPanel>(html`
      <session-replay-panel
        replayMode="chat"
        .session=${SESSION}
        .events=${events}
        @session-event-detail-requested=${(event: CustomEvent) => {
          requestedEventId = event.detail.eventId;
        }}
      ></session-replay-panel>
    `);
    const turns = Array.from(
      element.shadowRoot?.querySelectorAll('.chat-turn') || []
    ) as HTMLElement[];
    expect(turns.length).to.equal(2);
    turns[0].focus();
    expect(element.shadowRoot?.activeElement).to.equal(turns[0]);

    // j moves focus to the next turn in DOM order.
    turns[0].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'j', bubbles: true, composed: true })
    );
    await element.updateComplete;
    expect(element.shadowRoot?.activeElement).to.equal(turns[1]);

    // Enter toggles full-context expansion of the focused turn, which lazily
    // requests the event detail (same contract as clicking the expand button).
    turns[1].dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;
    expect(requestedEventId).to.equal(turns[1].getAttribute('data-event-id'));

    // k moves focus back to the previous turn.
    turns[1].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'k', bubbles: true, composed: true })
    );
    await element.updateComplete;
    expect(element.shadowRoot?.activeElement).to.equal(turns[0]);
  });
});
