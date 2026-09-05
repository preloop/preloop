import { expect, fixture, html } from '@open-wc/testing';
import './session-chat-view';
import type { SessionChatView } from './session-chat-view';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';

function activityItem(
  overrides: Partial<RuntimeSessionActivityItem> &
    Pick<RuntimeSessionActivityItem, 'activity_type' | 'timestamp' | 'title'>
): RuntimeSessionActivityItem {
  return {
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

function previewEvent(
  id: string,
  timestamp: string,
  messages: Array<{ role: string; text: string; source?: string }>,
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
        })),
      },
      ...overrides,
    },
  } as FlowGatewayEvent;
}

function manyEvents(count: number): FlowGatewayEvent[] {
  return Array.from({ length: count }, (_, index) =>
    previewEvent(
      `e${index}`,
      `2026-08-06T10:${String(index).padStart(2, '0')}:00Z`,
      [
        { role: 'user', text: `prompt ${index}` },
        { role: 'assistant', text: `answer ${index}`, source: 'response' },
      ]
    )
  );
}

function distanceFromBottom(thread: HTMLElement): number {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight;
}

/** Let the ResizeObserver and the layout it reacts to settle. */
async function waitForStableScroll(thread: HTMLElement): Promise<void> {
  for (let frame = 0; frame < 6; frame += 1) {
    await new Promise((resolve) => requestAnimationFrame(resolve));
    if (distanceFromBottom(thread) < 2) return;
  }
  await new Promise((resolve) => requestAnimationFrame(resolve));
}

describe('session-chat-view', () => {
  it('renders empty state without events', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view></session-chat-view>`
    );
    expect(el.shadowRoot!.querySelector('.empty')).to.exist;
  });

  it('expands prompts and responses, collapses steps by default', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'Fix the login bug' },
        { role: 'assistant', text: 'Looking at auth.ts', source: 'response' },
      ]),
      previewEvent(
        'e2',
        '2026-08-06T10:01:00Z',
        [
          { role: 'user', text: 'Fix the login bug' },
          { role: 'user', text: 'auth.ts contents: const x = 1' },
          { role: 'assistant', text: 'Fixed', source: 'response' },
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
                    content: 'auth.ts contents: const x = 1',
                  },
                ],
              },
            ],
          },
        }
      ),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .events=${events}></session-chat-view>`
    );
    const bubbles = el.shadowRoot!.querySelectorAll('.bubble');
    expect(bubbles).to.have.length(2);
    expect(bubbles[0].getAttribute('data-kind')).to.equal('user_prompt');
    expect(bubbles[1].getAttribute('data-kind')).to.equal('agent_response');
    expect(bubbles[1].textContent).to.contain('Fixed');

    // Tool result + intermediate response live in one collapsed group.
    const details = el.shadowRoot!.querySelectorAll('sl-details.steps');
    expect(details).to.have.length(1);
    expect((details[0] as HTMLElement & { open: boolean }).open).to.equal(
      false
    );
    expect(details[0].textContent).to.contain('tool result');
  });

  it('discloses partial structure coverage instead of guessing', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'maybe a prompt, maybe a tool result' },
        { role: 'assistant', text: 'ok', source: 'response' },
      ]),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .events=${events}></session-chat-view>`
    );
    const note = el.shadowRoot!.querySelector('.coverage-note');
    expect(note).to.exist;
    expect(note!.textContent).to.contain('structure was unavailable');
  });

  it('marks injected segments and keeps the real prompt expanded', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        {
          role: 'user',
          text: '<system-reminder>plan mode is active</system-reminder>',
        },
        { role: 'user', text: 'Real user prompt' },
        { role: 'assistant', text: 'ok', source: 'response' },
      ]),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .events=${events}></session-chat-view>`
    );
    const prompts = el.shadowRoot!.querySelectorAll(
      '.bubble[data-kind="user_prompt"]'
    );
    expect(prompts).to.have.length(1);
    expect(prompts[0].textContent).to.contain('Real user prompt');
    const group = el.shadowRoot!.querySelector('sl-details.steps');
    expect(group).to.exist;
    expect(group!.textContent).to.contain('injected segment');
  });

  it('shows a load-earlier control when more events exist', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'hello' },
        { role: 'assistant', text: 'hi', source: 'response' },
      ]),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        .events=${events}
        .hasMoreEvents=${true}
      ></session-chat-view>`
    );
    let paged = false;
    el.addEventListener('session-events-page-requested', () => {
      paged = true;
    });
    const button = el.shadowRoot!.querySelector('.load-earlier') as HTMLElement;
    expect(button).to.exist;
    button.click();
    expect(paged).to.equal(true);
  });

  it('shows the loading state before any data arrives', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .loading=${true}></session-chat-view>`
    );
    expect(el.shadowRoot!.querySelector('.loading')).to.exist;
    expect(el.shadowRoot!.querySelector('.loading')!.textContent).to.contain(
      'Loading conversation'
    );
  });

  it('renders operator messages as operator bubbles', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'run the tests' },
        { role: 'assistant', text: 'done', source: 'response' },
      ]),
    ];
    const activity = [
      activityItem({
        activity_type: 'agent_control_message',
        timestamp: '2026-08-06T10:02:00Z',
        title: 'Operator message',
        summary: 'please also run lint',
        status: 'delivered',
      }),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        .events=${events}
        .activity=${activity}
      ></session-chat-view>`
    );
    const operator = el.shadowRoot!.querySelector(
      '.bubble[data-kind="operator"]'
    );
    expect(operator).to.exist;
    expect(operator!.textContent).to.contain('please also run lint');
  });

  it('renders a queue chip on pending operator messages', async () => {
    const activity = [
      activityItem({
        activity_type: 'agent_control_message',
        timestamp: '2026-08-06T10:02:00Z',
        title: 'Operator message',
        summary: 'take over from the phone',
        status: 'queued',
      }),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .activity=${activity}></session-chat-view>`
    );
    const chip = el.shadowRoot!.querySelector('.queue-chip');
    expect(chip).to.exist;
    expect(chip!.textContent).to.contain('Queued');
  });

  it('renders session lifecycle markers as dividers', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:01:00Z', [
        { role: 'user', text: 'hello' },
        { role: 'assistant', text: 'hi', source: 'response' },
      ]),
    ];
    const activity = [
      activityItem({
        activity_type: 'session_started',
        timestamp: '2026-08-06T10:00:00Z',
        title: 'Session started',
      }),
      activityItem({
        activity_type: 'session_ended',
        timestamp: '2026-08-06T10:05:00Z',
        title: 'Session ended',
      }),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        .events=${events}
        .activity=${activity}
      ></session-chat-view>`
    );
    const dividers = el.shadowRoot!.querySelectorAll('.divider');
    expect(dividers).to.have.length(2);
    expect(dividers[0].textContent).to.contain('Session started');
    expect(dividers[1].textContent).to.contain('Session ended');
  });

  it('rebuilds the transcript only when events or activity change', async () => {
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: 'hello' },
        { role: 'assistant', text: 'hi', source: 'response' },
      ]),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .events=${events}></session-chat-view>`
    );
    const internals = el as unknown as {
      conversation: { items: unknown[] };
    };
    const built = internals.conversation;
    // A state-only re-render must reuse the cached transcript object.
    el.requestUpdate();
    await el.updateComplete;
    expect(internals.conversation).to.equal(built);
    // New events must trigger a rebuild.
    el.events = [
      ...events,
      previewEvent('e2', '2026-08-06T10:01:00Z', [
        { role: 'user', text: 'more' },
        { role: 'assistant', text: 'ok', source: 'response' },
      ]),
    ];
    await el.updateComplete;
    expect(internals.conversation).to.not.equal(built);
  });

  it('clamps long messages with a manual reveal', async () => {
    const longText = `start ${'x'.repeat(3000)} end`;
    const events = [
      previewEvent('e1', '2026-08-06T10:00:00Z', [
        { role: 'user', text: longText },
        { role: 'assistant', text: 'ok', source: 'response' },
      ]),
    ];
    const el = await fixture<SessionChatView>(
      html`<session-chat-view .events=${events}></session-chat-view>`
    );
    const prompt = el.shadowRoot!.querySelector(
      '.bubble[data-kind="user_prompt"]'
    )!;
    expect(prompt.textContent).to.not.contain(' end');
    const toggle = prompt.querySelector('.inline-toggle') as HTMLElement;
    expect(toggle).to.exist;
    toggle.click();
    await el.updateComplete;
    const expanded = el.shadowRoot!.querySelector(
      '.bubble[data-kind="user_prompt"]'
    )!;
    expect(expanded.textContent).to.contain(' end');
  });
});

describe('session-chat-view in the talk window', () => {
  it('scrolls inside itself and offers a jump pill when scrolled up', async () => {
    const events = Array.from({ length: 12 }, (_, index) =>
      previewEvent(
        `e${index}`,
        `2026-08-06T10:${String(index).padStart(2, '0')}:00Z`,
        [
          { role: 'user', text: `prompt ${index}` },
          { role: 'assistant', text: `answer ${index}`, source: 'response' },
        ]
      )
    );
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        style="height: 200px"
        .events=${events}
      ></session-chat-view>`
    );
    await el.updateComplete;

    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;
    expect(getComputedStyle(thread).overflowY).to.equal('auto');
    expect(thread.scrollHeight).to.be.greaterThan(thread.clientHeight);
    expect(
      el.shadowRoot!.querySelector('[data-testid="jump-latest"]'),
      'at the bottom there is nothing to jump to'
    ).to.not.exist;

    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    await el.updateComplete;
    const pill = el.shadowRoot!.querySelector('[data-testid="jump-latest"]');
    expect(pill).to.exist;

    el.scrollToLatest();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="jump-latest"]')).to.not
      .exist;
  });

  it('renders a pending turn and its retry link', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        .pending=${[
          {
            id: 'p1',
            text: 'ship it',
            at: '2026-09-03T10:00:00Z',
            state: 'failed' as const,
            error: 'Gateway timeout',
          },
        ]}
      ></session-chat-view>`
    );
    await el.updateComplete;

    const bubble = el.shadowRoot!.querySelector('[data-kind="pending"]')!;
    expect(bubble.textContent).to.contain('ship it');
    expect(bubble.textContent).to.contain('Gateway timeout');

    const retried: string[] = [];
    el.addEventListener('talk-retry', (event) =>
      retried.push((event as CustomEvent<{ id: string }>).detail.id)
    );
    (
      el.shadowRoot!.querySelector(
        '[data-testid="pending-retry"]'
      ) as HTMLElement
    ).click();
    expect(retried).to.deep.equal(['p1']);
  });

  it('follows a new turn when the event page size does not change', async () => {
    // The talk view refetches a fixed page of the newest events, so once a
    // session is longer than one page the array length stops growing while
    // its contents keep changing. Counting events missed every one of those.
    const page = (offset: number) =>
      Array.from({ length: 12 }, (_, index) =>
        previewEvent(
          `e${offset + index}`,
          `2026-08-06T10:${String(offset + index).padStart(2, '0')}:00Z`,
          [
            { role: 'user', text: `prompt ${offset + index}` },
            {
              role: 'assistant',
              text: `answer ${offset + index}`,
              source: 'response',
            },
          ]
        )
      );
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        followLive
        style="height: 200px"
        .events=${page(0)}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;

    // Same length, newer contents: one turn scrolled off the top.
    el.events = page(1);
    await el.updateComplete;
    await waitForStableScroll(thread);

    expect(distanceFromBottom(thread)).to.be.lessThan(2);
    expect(thread.textContent).to.contain('answer 12');
  });

  it('stays at the bottom when content grows after the update', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        followLive
        style="height: 200px"
        .events=${[
          previewEvent('e1', '2026-08-06T10:00:00Z', [
            { role: 'user', text: 'ship it' },
            { role: 'assistant', text: 'shipped', source: 'response' },
          ]),
        ]}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;
    const content = el.shadowRoot!.querySelector(
      '.thread-content'
    ) as HTMLElement;

    // Whatever grows after the render that scrolled (a Shoelace element
    // rendering in its own cycle, a font, an image, a reflowing <pre>).
    const grown = document.createElement('div');
    grown.style.height = '800px';
    content.appendChild(grown);
    await waitForStableScroll(thread);

    expect(thread.scrollHeight).to.be.greaterThan(thread.clientHeight);
    expect(
      distanceFromBottom(thread),
      'follow mode re-sticks once the layout settles'
    ).to.be.lessThan(2);
  });

  it('does not treat a layout-driven scroll as the reader scrolling away', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        followLive
        style="height: 200px"
        .events=${manyEvents(12)}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;

    // No wheel, no touch, no key, no pointer: the viewport moved on its own.
    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    await el.updateComplete;

    expect(
      el.shadowRoot!.querySelector('[data-testid="jump-latest"]'),
      'layout must not switch following off'
    ).to.not.exist;
    expect(distanceFromBottom(thread)).to.be.lessThan(2);
  });

  it('never yanks a reader who scrolled up with a gesture', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        followLive
        style="height: 200px"
        .events=${manyEvents(12)}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;

    thread.dispatchEvent(new WheelEvent('wheel', { deltaY: -400 }));
    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="jump-latest"]')).to
      .exist;

    el.events = manyEvents(13);
    await el.updateComplete;
    await waitForStableScroll(thread);

    expect(thread.scrollTop, 'the reader stays where they were').to.equal(0);
    expect(
      el.shadowRoot!.querySelector('[data-testid="jump-latest"]'),
      'the pill is how they come back'
    ).to.exist;
  });

  it('treats a scrollbar-thumb drag as a gesture however long the pause', async () => {
    // A press on the scrollbar itself gets a `mousedown` and no `pointerdown`
    // in Chromium, and a reader can hold the thumb far longer than the 700ms
    // gesture window before they move it. Both halves have to hold: the press
    // keeps the gesture alive, the release lets layout re-stick again.
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        followLive
        style="height: 200px"
        .events=${manyEvents(12)}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;

    thread.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    // Hold past the window, then drag.
    await new Promise((resolve) => setTimeout(resolve, 750));
    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    await el.updateComplete;

    expect(
      el.shadowRoot!.querySelector('[data-testid="jump-latest"]'),
      'the drag detaches the reader'
    ).to.exist;
    expect(thread.scrollTop, 'the reader stays where they dragged to').to.equal(
      0
    );

    // Releasing the thumb ends the gesture: back at the bottom, a later
    // layout-driven scroll must not be mistaken for the reader again.
    window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    (
      el.shadowRoot!.querySelector('[data-testid="jump-latest"]') as HTMLElement
    ).click();
    await el.updateComplete;
    await waitForStableScroll(thread);
    await new Promise((resolve) => setTimeout(resolve, 750));

    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[data-testid="jump-latest"]'),
      'layout must not switch following off once the thumb is released'
    ).to.not.exist;
  });

  it('rebinds the thread after a session switch empties it', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        scrollable
        followLive
        style="height: 200px"
        .events=${manyEvents(12)}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const first = el.shadowRoot!.querySelector('.thread') as HTMLElement;
    first.dispatchEvent(new WheelEvent('wheel', { deltaY: -400 }));
    first.scrollTop = 0;
    first.dispatchEvent(new Event('scroll'));
    await el.updateComplete;

    // Following a new session clears the thread, which removes the .thread
    // node entirely (the empty branch), then builds a new one.
    el.events = [];
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('.empty')).to.exist;
    el.events = manyEvents(12);
    await el.updateComplete;

    const thread = el.shadowRoot!.querySelector('.thread') as HTMLElement;
    await waitForStableScroll(thread);
    expect(
      distanceFromBottom(thread),
      'the new session opens at the latest'
    ).to.be.lessThan(2);

    // The listeners must be on the new node, not the discarded one.
    thread.dispatchEvent(new WheelEvent('wheel', { deltaY: -400 }));
    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="jump-latest"]')).to
      .exist;
  });

  it('announces the agent reply politely', async () => {
    const el = await fixture<SessionChatView>(
      html`<session-chat-view
        .events=${[
          previewEvent('e1', '2026-08-06T10:00:00Z', [
            { role: 'user', text: 'status?' },
            { role: 'assistant', text: 'All green.', source: 'response' },
          ]),
        ]}
      ></session-chat-view>`
    );
    await el.updateComplete;
    const region = el.shadowRoot!.querySelector('.live-region')!;
    expect(region.getAttribute('aria-live')).to.equal('polite');
    expect(region.textContent).to.contain('All green.');
  });
});
