import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';
import './agent-talk-view';
import type { AgentTalkView } from './agent-talk-view';
import {
  conversationEvents,
  conversationLabel,
  conversationNotice,
  conversationRequest,
  formatCountdown,
  secondsUntilIdleStop,
  type FlowConversation,
} from '../../utils/flow-conversations';

import { buildConversation } from '../../utils/transcript';

const thread: FlowConversation = {
  id: 'conversation-example',
  flow_id: 'flow-example',
  owner_user_id: 'owner-example',
  flow_name: 'Example flow',
  execution_id: 'execution-example',
  can_write: true,
  state: 'waiting',
  notice: 'Waiting for you. The container will stop after the idle timeout.',
  turns: 1,
  max_turns: 30,
  idle_deadline: new Date(Date.now() + 600000).toISOString(),
  messages: [
    {
      id: 'message-example',
      turn: 1,
      text: 'Remember amber.',
      answer: 'I remember amber.',
      status: 'completed',
      execution_id: 'execution-example',
    },
  ],
};

describe('Hosted conversation on Talk', () => {
  let fetch: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetch = sinon.stub(window, 'fetch').callsFake(
      async () =>
        new Response(JSON.stringify(thread), {
          headers: { 'Content-Type': 'application/json' },
        })
    );
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });

  async function mount(): Promise<AgentTalkView> {
    const el = await fixture<AgentTalkView>(
      html`<agent-talk-view .conversationId=${thread.id}></agent-talk-view>`
    );
    await waitUntil(() =>
      Boolean(el.shadowRoot?.querySelector('talk-composer'))
    );
    return el;
  }

  it('uses the shared transcript and composer and restores the same thread in another tab', async () => {
    for (let index = 0; index < 2; index++) {
      const el = await mount();
      const transcript = el.shadowRoot!.querySelector(
        'session-chat-view'
      ) as any;
      expect(
        transcript.events[0].payload.conversation_preview.messages[0].text
      ).to.equal('Remember amber.');
      expect(
        transcript.events[0].payload.conversation_preview.messages[1].text
      ).to.equal('I remember amber.');
      expect(el.shadowRoot!.textContent).to.contain('Waiting for you');
      expect(el.shadowRoot!.textContent).to.contain('The container stops in');
    }
  });

  async function show(
    el: AgentTalkView,
    patch: Partial<FlowConversation>
  ): Promise<ShadowRoot> {
    // Hold the poll and the clock still so the state under test stays put;
    // a poll already in flight settles first and schedules nothing real.
    (el as any).loadConversation = async () => {};
    window.clearTimeout((el as any).conversationTimer);
    window.clearInterval((el as any).conversationClock);
    await new Promise((resolve) => setTimeout(resolve, 20));
    (el as any).setConversation({ ...thread, ...patch });
    (el as any).conversationNow = Date.now();
    await el.updateComplete;
    const view = el.shadowRoot!.querySelector('session-chat-view') as any;
    await view.updateComplete;
    return el.shadowRoot!;
  }

  function composer(root: ShadowRoot): any {
    return root.querySelector('talk-composer');
  }

  async function placeholder(root: ShadowRoot): Promise<string> {
    const element = composer(root);
    await element.updateComplete;
    return element.shadowRoot
      .querySelector('[data-testid="composer-input"]')
      .getAttribute('placeholder');
  }

  function notices(root: ShadowRoot): HTMLElement[] {
    return [
      ...root.querySelectorAll<HTMLElement>(
        '[data-testid="conversation-notice"], .talk-error'
      ),
    ];
  }

  function indicator(root: ShadowRoot): HTMLElement | null {
    return (
      root.querySelector('session-chat-view') as any
    ).shadowRoot.querySelector('[data-testid="working-indicator"]');
  }

  it('sends a stopped conversation through the same API and displays its cold start', async () => {
    const el = await mount();
    await show(el, { state: 'stopped', idle_deadline: null });
    expect(composer(el.shadowRoot!).transportDisabled).to.equal(false);
    fetch.resolves(
      new Response(
        JSON.stringify({
          ...thread,
          state: 'starting',
          notice: 'Starting your conversation.',
        })
      )
    );
    await composer(el.shadowRoot!).sendMessage('What word did I give you?');
    await el.updateComplete;
    const call = fetch.lastCall;
    expect(String(call.args[0])).to.contain(
      `/flow-conversations/${thread.id}/messages`
    );
    expect(JSON.parse(call.args[1].body).message).to.equal(
      'What word did I give you?'
    );
    expect(el.shadowRoot!.textContent).to.contain('Starting (cold start)');
  });

  it('shows Starting as one calm in-thread indicator with elapsed time', async () => {
    const el = await mount();
    const root = await show(el, {
      state: 'starting',
      notice: 'Starting your conversation.',
      idle_deadline: null,
      messages: [
        { ...thread.messages[0], text: '', answer: '', status: 'pending' },
      ],
    });
    const line = indicator(root);
    expect(line, 'working indicator').to.exist;
    expect(line!.textContent).to.contain('Starting a container');
    expect(line!.textContent).to.contain('a minute or more');
    expect(line!.textContent).to.match(/\(\d+s\)/);
    // The server's "Starting your conversation." is not repeated anywhere.
    expect(notices(root)).to.have.length(0);
    expect(root.textContent).not.to.contain('Starting your conversation.');
    expect(await placeholder(root)).to.contain('Wait for the first answer');
    expect(composer(root).transportDisabled).to.equal(true);
  });

  it('advances the elapsed time without a poll', async () => {
    const el = await mount();
    const root = await show(el, { state: 'working', idle_deadline: null });
    (el as any).conversationBusySince = Date.now() - 75_000;
    (el as any).conversationNow = Date.now();
    await el.updateComplete;
    await (root.querySelector('session-chat-view') as any).updateComplete;
    expect(indicator(root)!.textContent).to.contain('Working (1m 15s)');
  });

  it('shows Working with partial answer text as it streams and refuses sends', async () => {
    const el = await mount();
    const root = await show(el, {
      state: 'working',
      idle_deadline: null,
      messages: [
        { ...thread.messages[0], answer: 'I remem', status: 'working' },
      ],
    });
    const view = root.querySelector('session-chat-view') as any;
    expect(view.liveReply).to.equal('I remem');
    const live = view.shadowRoot.querySelector('[data-testid="live-reply"]');
    expect(live.textContent).to.contain('I remem');
    expect(live.textContent).to.contain('Working');
    expect(
      view.events[0].payload.conversation_preview.messages.map(
        (message: any) => message.role
      )
    ).to.deep.equal(['user']);
    expect(notices(root)).to.have.length(0);
    expect(await placeholder(root)).to.contain('Wait for the answer');
    expect(await placeholder(root)).to.contain('not queued');
    expect(composer(root).transportDisabled).to.equal(true);
  });

  it('shows Waiting for you once, neutral, with a countdown that ticks', async () => {
    const el = await mount();
    const root = await show(el, {
      idle_deadline: new Date(Date.now() + 125_000).toISOString(),
    });
    const shown = notices(root);
    expect(shown).to.have.length(1);
    expect(shown[0].classList.contains('failed')).to.equal(false);
    expect(shown[0].getAttribute('role')).to.equal('status');
    expect(shown[0].textContent).to.contain('Waiting for you');
    expect(shown[0].textContent).to.match(/stops in 2:0[45]/);
    expect(root.textContent).not.to.contain('idle timeout');
    (el as any).conversationNow = Date.now() + 65_000;
    await el.updateComplete;
    expect(notices(root)[0].textContent).to.match(/stops in 1:0[0-1]/);
    expect(indicator(root)).to.equal(null);
    expect(composer(root).transportDisabled).to.equal(false);
    expect(await placeholder(root)).to.equal('Message Example flow');
  });

  it('focuses the composer when the answer arrives', async () => {
    const el = await mount();
    await show(el, { state: 'working', idle_deadline: null });
    const focus = sinon.spy(composer(el.shadowRoot!), 'focusInput');
    await show(el, { state: 'waiting' });
    await el.updateComplete;
    expect(focus.calledOnce).to.equal(true);
  });

  for (const [state, text, tone] of [
    ['stopped', 'Send a message to resume this conversation', 'status'],
    ['ended', 'You ended this conversation.', 'status'],
    ['failed', 'The checkpoint is missing or expired.', 'alert'],
  ] as const) {
    it(`shows ${state} once with ${tone === 'alert' ? 'error' : 'neutral'} styling`, async () => {
      const el = await mount();
      const root = await show(el, {
        state,
        idle_deadline: null,
        notice:
          state === 'stopped'
            ? 'The container stopped after the idle timeout. Send a message to resume this conversation.'
            : state === 'ended'
              ? 'You ended this conversation. Start a new conversation to continue.'
              : 'The checkpoint is missing or expired. Start a new conversation.',
      });
      const shown = notices(root);
      expect(shown).to.have.length(1);
      expect(shown[0].textContent).to.contain(text);
      expect(shown[0].getAttribute('role')).to.equal(tone);
      expect(shown[0].classList.contains('failed')).to.equal(
        state === 'failed'
      );
      expect(
        (root.textContent || '').split(text).length - 1,
        'notice is shown once'
      ).to.equal(1);
      expect(composer(root).transportDisabled).to.equal(state !== 'stopped');
    });
  }

  for (const [state, words] of [
    ['starting', 'Wait for the first answer'],
    ['working', 'Wait for the answer'],
    ['waiting', 'Message Example flow'],
    ['stopped', 'resume this conversation'],
    ['ended', 'has ended'],
    ['failed', 'cannot continue'],
  ] as const) {
    it(`writes the ${state} placeholder for hosted conversations`, async () => {
      const el = await mount();
      const value = await placeholder(await show(el, { state }));
      expect(value).to.contain(words);
      expect(value).not.to.contain('Agent Control');
      expect(value).not.to.contain('Unknown');
    });
  }

  it('tells a viewer who is not the owner why they cannot send', async () => {
    const el = await mount();
    const root = await show(el, { can_write: false });
    expect(await placeholder(root)).to.contain(
      'Only the person who started this conversation'
    );
    expect(composer(root).transportDisabled).to.equal(true);
  });

  it('keeps the Agent Control heartbeat off the hosted status line', async () => {
    const el = await mount();
    const root = await show(el, { state: 'starting', idle_deadline: null });
    const view = root.querySelector('session-chat-view') as any;
    expect(view.liveEnabled).to.equal(false);
    expect(root.textContent).not.to.contain('Reconnecting');
    expect(view.shadowRoot.textContent).not.to.contain('Idle');
    expect(
      root.querySelector('[data-testid="conversation-state"]')!.textContent
    ).to.contain('Starting (cold start)');
  });

  it('says Reconnecting only while polling fails', async () => {
    const el = await mount();
    fetch.rejects(new TypeError('Failed to fetch'));
    await (el as any).loadConversation();
    await el.updateComplete;
    const state = el.shadowRoot!.querySelector(
      '[data-testid="conversation-state"]'
    )!;
    expect(state.textContent).to.contain('Reconnecting');
    expect(el.shadowRoot!.querySelector('.talk-error')).to.equal(null);
    fetch.resolves(
      new Response(JSON.stringify(thread), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
    await (el as any).loadConversation();
    await el.updateComplete;
    expect(state.textContent).not.to.contain('Reconnecting');
  });

  it('presents a turn started without a message as a system line', async () => {
    const el = await mount();
    const root = await show(el, {
      messages: [{ ...thread.messages[0], text: '' }],
    });
    const view = root.querySelector('session-chat-view') as any;
    await view.updateComplete;
    const intro = view.shadowRoot.querySelector('[data-testid="thread-intro"]');
    expect(intro.textContent).to.contain(
      'Conversation started with the flow’s initial prompt'
    );
    expect(view.shadowRoot.textContent).not.to.contain(
      'Start the flow’s initial prompt.'
    );
    expect(view.shadowRoot.querySelector('.bubble.user')).to.equal(null);
  });

  it('keeps a typed first message as the user bubble', async () => {
    const el = await mount();
    const view = (await show(el, {})).querySelector('session-chat-view') as any;
    await view.updateComplete;
    expect(
      view.shadowRoot.querySelector('[data-testid="thread-intro"]')
    ).to.equal(null);
    expect(view.shadowRoot.textContent).to.contain('Remember amber.');
  });

  it('does not warn about missing message structure for durable turns', async () => {
    const el = await mount();
    const view = (await show(el, {})).querySelector('session-chat-view') as any;
    await view.updateComplete;
    expect(view.shadowRoot.textContent).not.to.contain(
      'Message structure was unavailable'
    );
    expect(
      buildConversation(conversationEvents(thread)).stats.eventsWithoutRawBody
    ).to.equal(0);
  });

  it('starts from the client-neutral flow endpoint with an optional first message', async () => {
    await conversationRequest('flows/flow-example/conversations', {
      message: 'Inspect the README.',
      request_id: 'request-example',
    });
    expect(String(fetch.lastCall.args[0])).to.contain(
      '/flows/flow-example/conversations'
    );
    expect(JSON.parse(fetch.lastCall.args[1].body).message).to.equal(
      'Inspect the README.'
    );
  });

  it('names all lifecycle states and counts down to zero', () => {
    for (const [state, label] of Object.entries({
      starting: 'Starting (cold start)',
      working: 'Working',
      waiting: 'Waiting for you',
      stopped: 'Stopped (resumable)',
      ended: 'Ended',
      failed: 'Failed',
    })) {
      expect(
        conversationLabel({
          ...thread,
          state: state as FlowConversation['state'],
        })
      ).to.equal(label);
    }
    expect(secondsUntilIdleStop(thread, Date.now() + 700000)).to.equal(0);
    expect(formatCountdown(545)).to.equal('9:05');
    expect(conversationNotice(thread, Date.now() + 700000)!.text).to.contain(
      'Saving the conversation'
    );
  });
});

describe('Durable conversation transcript', () => {
  it('preserves repeated user prompts and identical assistant replies across turns', () => {
    const repeated = {
      ...thread,
      messages: [1, 2].map((turn) => ({
        ...thread.messages[0],
        id: `message-${turn}`,
        turn,
        text: 'continue',
        answer: 'Done.',
      })),
    };
    const result = buildConversation(conversationEvents(repeated));
    expect(
      result.items.map((item: any) => [item.role, item.text])
    ).to.deep.equal([
      ['user', 'continue'],
      ['assistant', 'Done.'],
      ['user', 'continue'],
      ['assistant', 'Done.'],
    ]);
  });
});
