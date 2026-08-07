import { expect, fixture, html } from '@open-wc/testing';
import './session-chat-view';
import type { SessionChatView } from './session-chat-view';
import type { FlowGatewayEvent } from '../types';

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
