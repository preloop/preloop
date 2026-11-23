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
  conversationRequest,
  type FlowConversation,
} from '../../utils/flow-conversations';

import { buildConversation } from '../../utils/transcript';

const thread: FlowConversation = {
  id: 'conversation-example',
  flow_id: 'flow-example',
  owner_user_id: 'owner-example',
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
      expect(el.shadowRoot!.textContent).to.contain('container stops in');
    }
  });

  it('sends a stopped conversation through the same API and displays its cold start', async () => {
    const el = await mount();
    (el as any).conversation = {
      ...thread,
      state: 'stopped',
      idle_deadline: null,
    };
    await el.updateComplete;
    const composer = el.shadowRoot!.querySelector('talk-composer') as any;
    expect(composer.transportDisabled).to.equal(false);
    fetch.resolves(
      new Response(
        JSON.stringify({
          ...thread,
          state: 'starting',
          notice: 'Starting your conversation.',
        })
      )
    );
    await composer.sendMessage('What word did I give you?');
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

  for (const state of ['starting', 'working', 'ended', 'failed'] as const) {
    it(`disables sending while ${state} and preserves the server explanation`, async () => {
      const el = await mount();
      (el as any).conversation = {
        ...thread,
        state,
        notice:
          'The checkpoint is missing or expired. Start a new conversation.',
      };
      await el.updateComplete;
      expect(
        (el.shadowRoot!.querySelector('talk-composer') as any).transportDisabled
      ).to.equal(true);
      expect(el.shadowRoot!.textContent).to.contain(
        'The checkpoint is missing or expired.'
      );
    });
  }

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
        conversationLabel(
          { ...thread, state: state as FlowConversation['state'] },
          Date.now() + 700000
        )
      ).to.contain(label);
    }
    expect(conversationLabel(thread, Date.now() + 700000)).to.contain('0s');
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
