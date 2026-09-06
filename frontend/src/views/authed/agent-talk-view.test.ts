import { aTimeout, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-talk-view.ts';
import type { AgentTalkView } from './agent-talk-view';
import { TALK_CHANNEL_NAME } from '../../utils/talk-channel';
import type { TalkChannelMessage } from '../../utils/talk-channel';
import { TALK_MESSAGE_SENT_EVENT } from '../../components/talk-composer';
import type { TalkComposer } from '../../components/talk-composer';

const AGENT = {
  id: 'agent-1',
  display_name: 'Hermes',
  agent_kind: 'hermes',
  session_source_type: 'hermes',
  session_source_id: 'hermes-1',
  session_reference: null,
  lifecycle_state: 'active',
  activity_status: 'active_now',
  is_active_now: true,
  onboarding_state: 'fully_onboarded',
  live_validation_status: 'passed',
  control_state: 'plugin_connected',
  control_enabled: true,
  control_online: true,
  control_capabilities: ['send_text_prompt'],
  control_session_mode: 'remote',
};

const SESSION = {
  id: 'sess-1',
  title: 'Refactor the billing job',
  session_source_type: 'hermes',
  session_source_id: 'hermes-1',
  started_at: '2026-09-03T10:00:00Z',
  last_activity_at: '2026-09-03T10:30:00Z',
  is_active_now: true,
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Everything this agent's window says on the talk channel, in order. */
function listenOnTalkChannel(): {
  messages: TalkChannelMessage[];
  close: () => void;
} {
  const messages: TalkChannelMessage[] = [];
  const channel = new BroadcastChannel(TALK_CHANNEL_NAME);
  channel.onmessage = (event: MessageEvent<TalkChannelMessage>) => {
    if (event.data?.agentId === 'agent-1') messages.push(event.data);
  };
  return { messages, close: () => channel.close() };
}

/** Is the operator looking at this window, or at something else? */
function setAttention(state: { hidden: boolean; focused: boolean }): void {
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => state.hidden,
  });
  sinon.stub(document, 'hasFocus').returns(state.focused);
}

describe('agent-talk-view', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/gateway-events')) {
        return jsonResponse({ logs: [], pagination: { has_more: false } });
      }
      if (url.includes('/activity')) return jsonResponse({ items: [] });
      if (url.includes('/api/v1/agents/agent-1')) {
        return jsonResponse({ agent: AGENT, sessions: [SESSION] });
      }
      return new Response('{}', { status: 200 });
    });
  });

  afterEach(() => {
    sinon.restore();
    delete (document as unknown as { hidden?: boolean }).hidden;
  });

  async function mount(search = ''): Promise<AgentTalkView> {
    const el = document.createElement('agent-talk-view') as AgentTalkView;
    el.onBeforeEnter({ params: { agentId: 'agent-1' }, search });
    document.body.append(el);
    await el.updateComplete;
    await waitUntil(() =>
      Boolean(el.shadowRoot!.querySelector('[data-testid="talk-agent-name"]'))
    );
    return el;
  }

  it('fills the viewport and never scrolls the page', async () => {
    const el = await mount();
    const styles = getComputedStyle(el);
    expect(styles.display).to.equal('flex');
    expect(styles.flexDirection).to.equal('column');
    expect(styles.overflow).to.equal('hidden');
    // The thread owns the scroll so the composer stays pinned.
    const chat = el.shadowRoot!.querySelector('session-chat-view')!;
    expect(chat.hasAttribute('scrollable')).to.be.true;
    expect(el.shadowRoot!.querySelector('talk-composer')).to.exist;
    el.remove();
  });

  it('names the agent, the session and the way back to the console', async () => {
    const el = await mount('?session=sess-1');
    expect(
      el.shadowRoot!.querySelector('[data-testid="talk-agent-name"]')!
        .textContent
    ).to.contain('Hermes');
    expect(
      el.shadowRoot!.querySelector('[data-testid="talk-session-subject"]')!
        .textContent
    ).to.contain('Refactor the billing job');
    const link = el.shadowRoot!.querySelector(
      '[data-testid="open-in-console"]'
    ) as HTMLAnchorElement;
    expect(link.getAttribute('href')).to.equal(
      '/console/agents/agent-1?session=sess-1'
    );
    el.remove();
  });

  it('titles the window with the agent and the session subject', async () => {
    const before = document.title;
    const el = await mount('?window=1&session=sess-1');
    expect(document.title).to.equal('Hermes · Refactor the billing job');
    el.remove();
    await el.updateComplete;
    expect(document.title).to.equal(before);
  });

  it('asks the shell for the whole viewport', async () => {
    const el = document.createElement('agent-talk-view') as AgentTalkView;
    el.onBeforeEnter({ params: { agentId: 'agent-1' }, search: '' });
    const seen: unknown[] = [];
    document.body.addEventListener(
      'request-full-bleed',
      (event) => seen.push((event as CustomEvent).detail),
      { once: true }
    );
    document.body.append(el);
    await el.updateComplete;
    expect(seen).to.deep.equal([true]);
    el.remove();
  });

  it('picks the live session when the url does not name one', async () => {
    const el = await mount();
    expect(
      el.shadowRoot!.querySelector('[data-testid="talk-session-subject"]')!
        .textContent
    ).to.contain('Refactor the billing job');
    el.remove();
  });

  it('flags an agent turn that arrives while the window is not focused', async () => {
    setAttention({ hidden: true, focused: false });
    const listener = listenOnTalkChannel();
    const el = await mount('?window=1&session=sess-1');

    el.receiveActivity({
      payload: { managed_agent_id: 'agent-1', runtime_session_id: 'sess-1' },
    });

    await waitUntil(
      () => listener.messages.some((message) => message.type === 'message'),
      'the window never announced the unread turn'
    );
    listener.close();
    el.remove();
  });

  it('does not flag the operator’s own send', async () => {
    // Hidden, so a stray post would be visible: the send itself is what must
    // not raise the dot, not the focus state.
    setAttention({ hidden: true, focused: false });
    const listener = listenOnTalkChannel();
    const el = await mount('?window=1&session=sess-1');

    el.dispatchEvent(
      new CustomEvent(TALK_MESSAGE_SENT_EVENT, { bubbles: true })
    );

    await aTimeout(50);
    expect(listener.messages.map((message) => message.type)).to.not.contain(
      'message'
    );
    listener.close();
    el.remove();
  });

  it('does not flag a turn the operator is already watching', async () => {
    setAttention({ hidden: false, focused: true });
    const listener = listenOnTalkChannel();
    const el = await mount('?window=1&session=sess-1');

    el.receiveActivity({
      payload: { managed_agent_id: 'agent-1', runtime_session_id: 'sess-1' },
    });

    await aTimeout(50);
    expect(listener.messages.map((message) => message.type)).to.not.contain(
      'message'
    );
    listener.close();
    el.remove();
  });

  it('announces the window on the talk channel in window mode', async () => {
    const listener = listenOnTalkChannel();
    const el = await mount('?window=1&session=sess-1');

    await waitUntil(
      () => listener.messages.some((message) => message.type === 'open'),
      'the window never announced itself'
    );
    listener.close();
    el.remove();
  });

  it('posts no heartbeat from the in-page form', async () => {
    // DESIGN: one chip per open talk *window*. On the page (and on a phone,
    // where Talk navigates instead of opening a window) the chip would point
    // at the tab the operator is already reading.
    const listener = listenOnTalkChannel();
    const el = await mount('?session=sess-1');

    await aTimeout(50);
    expect(listener.messages.map((message) => message.type)).to.not.contain(
      'open'
    );
    expect(listener.messages).to.have.length(0);

    // Closing the page says nothing either.
    el.remove();
    await aTimeout(50);
    expect(listener.messages).to.have.length(0);
    listener.close();
  });

  it('raises no unread dot from the in-page form', async () => {
    setAttention({ hidden: true, focused: false });
    const listener = listenOnTalkChannel();
    const el = await mount('?session=sess-1');

    el.receiveActivity({
      payload: { managed_agent_id: 'agent-1', runtime_session_id: 'sess-1' },
    });

    await aTimeout(50);
    expect(listener.messages).to.have.length(0);
    listener.close();
    el.remove();
  });

  it('reports the entry point that opened it as the composer source', async () => {
    const el = await mount('?window=1&session=sess-1&source=agent-detail-view');
    const composer = el.shadowRoot!.querySelector(
      'talk-composer'
    ) as TalkComposer;
    expect(composer.sourceContext).to.equal('agent-detail-view');
    el.remove();
  });

  it('falls back to the page shape when no entry point is named', async () => {
    const el = await mount('?window=1&session=sess-1');
    const composer = el.shadowRoot!.querySelector(
      'talk-composer'
    ) as TalkComposer;
    expect(composer.sourceContext).to.equal('talk-window');
    el.remove();
  });
});
