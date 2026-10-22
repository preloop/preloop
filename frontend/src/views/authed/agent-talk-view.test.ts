import { aTimeout, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-talk-view.ts';
import type { AgentTalkView } from './agent-talk-view';
import { TALK_CHANNEL_NAME } from '../../utils/talk-channel';
import type { TalkChannelMessage } from '../../utils/talk-channel';
import { TALK_MESSAGE_SENT_EVENT } from '../../components/talk-composer';
import type { TalkComposer } from '../../components/talk-composer';
import {
  ConnectionState,
  unifiedWebSocketManager,
} from '../../services/unified-websocket-manager';

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
      if (url.includes('/approval-requests')) {
        return jsonResponse(
          url.includes('runtime_session_id') ? [] : [{ id: 'req-other' }]
        );
      }
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

  it('provides a page heading with the current conversation as its accessible description', async () => {
    const el = await mount();
    const heading = el.shadowRoot!.querySelector('h1');
    expect(heading?.textContent?.trim()).to.equal('Hermes');
    const subject = el.shadowRoot!.getElementById(
      heading!.getAttribute('aria-describedby')!
    );
    expect(subject?.textContent?.trim()).to.equal(SESSION.title);
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="open-in-console"]')
        ?.getAttribute('href')
    ).to.equal('/console/agents/agent-1?session=sess-1');
    el.remove();
  });

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

describe('agent-talk-view approvals', () => {
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
      if (url.includes('/approval-requests')) {
        return jsonResponse([
          {
            id: 'req-1',
            status: 'pending',
            requested_at: '2026-10-02T09:00:00Z',
          },
        ]);
      }
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

  function approvalCalls(): string[] {
    return fetchStub
      .getCalls()
      .map((call) => String(call.args[0]))
      .filter((url) => url.includes('/approval-requests'));
  }

  it('scopes the approval read to the followed session, never the account', async () => {
    const el = document.createElement('agent-talk-view') as AgentTalkView;
    el.onBeforeEnter({ params: { agentId: 'agent-1' }, search: '' });
    document.body.append(el);
    await waitUntil(() => approvalCalls().length > 0);

    const calls = approvalCalls();
    expect(calls[0]).to.contain('runtime_session_id=sess-1');
    expect(calls[0]).to.contain('status=pending');
    el.remove();
  });

  it('tells the conversation it is waiting on a decision', async () => {
    const el = document.createElement('agent-talk-view') as AgentTalkView;
    el.onBeforeEnter({ params: { agentId: 'agent-1' }, search: '' });
    document.body.append(el);
    await waitUntil(() => approvalCalls().length > 0);
    const chat = el.shadowRoot!.querySelector('session-chat-view') as
      | (HTMLElement & {
          pendingApprovals?: Array<{ id: string; status: string }>;
        })
      | null;
    expect(chat).to.not.equal(null);
    // Wait for the state, not for the request: the read is async, so asserting
    // that a fetch happened can pass a microtask before the render.
    await waitUntil(() => (chat!.pendingApprovals?.length ?? 0) > 0, '', {
      timeout: 3000,
    });
    expect(chat!.pendingApprovals?.map((row) => row.id)).to.deep.equal([
      'req-1',
    ]);
    el.remove();
  });

  it('reloads approvals when a flat approval_created event arrives', async () => {
    const callbacks = new Map<
      string,
      (message: Record<string, unknown>) => void
    >();
    sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake((topic: string, callback: (message: unknown) => void) => {
        callbacks.set(
          topic,
          callback as (message: Record<string, unknown>) => void
        );
        return () => {
          callbacks.delete(topic);
        };
      });
    sinon
      .stub(unifiedWebSocketManager, 'getState')
      .returns(ConnectionState.CONNECTED);
    sinon
      .stub(unifiedWebSocketManager, 'onStateChange')
      .returns(() => undefined);
    sinon.stub(unifiedWebSocketManager, 'connect').resolves();

    const el = document.createElement('agent-talk-view') as AgentTalkView;
    el.onBeforeEnter({ params: { agentId: 'agent-1' }, search: '' });
    document.body.append(el);
    await waitUntil(() => approvalCalls().length > 0);
    const before = approvalCalls().length;
    const onApproval = callbacks.get('approvals');
    expect(onApproval, 'approvals subscription').to.not.equal(undefined);
    onApproval!({
      type: 'approval_created',
      runtime_session_id: 'sess-1',
    });
    await waitUntil(() => approvalCalls().length > before);
    expect(approvalCalls().at(-1)).to.contain('runtime_session_id=sess-1');
    el.remove();
  });
});

describe('agent-talk-view session integrity', () => {
  const OTHER = {
    ...SESSION,
    id: 'sess-2',
    title: 'Fix the flaky login test',
    last_activity_at: '2026-09-03T10:20:00Z',
  };
  let fetchStub: sinon.SinonStub;
  let sessions: Array<Record<string, unknown>>;
  let agentOverrides: Record<string, unknown>;
  let agentFailure: boolean;
  let eventPages: Map<string, () => Promise<Response>>;

  function eventsFor(sessionId: string, ids: string[], hasMore = false) {
    return jsonResponse({
      logs: ids.map((id, index) => ({
        id,
        execution_id: 'exec-1',
        timestamp: `2026-09-03T10:0${index}:00Z`,
        type: 'model_gateway_call',
        runtime_session_id: sessionId,
        payload: {
          outcome: 'success',
          conversation_preview: {
            messages: [{ source: 'request', role: 'user', text: id }],
          },
        },
      })),
      pagination: { has_more: hasMore, next_offset: ids.length },
    });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    sessions = [SESSION, OTHER];
    agentOverrides = {};
    agentFailure = false;
    eventPages = new Map();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      const match = url.match(/runtime-sessions\/([^/]+)\/gateway-events/);
      if (match || url.includes('/gateway-events')) {
        const sessionId = match?.[1] ?? 'unknown';
        const offset = new URL(url, 'http://x').searchParams.get('offset');
        const custom = eventPages.get(`${sessionId}:${offset}`);
        if (custom) return custom();
        return eventsFor(sessionId, [`${sessionId}-e1`]);
      }
      if (url.includes('/activity')) return jsonResponse({ items: [] });
      if (url.includes('/approval-requests')) return jsonResponse([]);
      if (url.includes('/control/')) {
        return jsonResponse({ command_id: 'cmd-1', status: 'delivered' });
      }
      if (url.includes('/api/v1/agents/agent-1')) {
        if (agentFailure) return new Response('down', { status: 500 });
        return jsonResponse({
          agent: { ...AGENT, ...agentOverrides },
          sessions,
        });
      }
      return new Response('{}', { status: 200 });
    });
  });

  afterEach(() => {
    sinon.restore();
  });

  async function mount(search = ''): Promise<AgentTalkView> {
    const el = document.createElement('agent-talk-view') as AgentTalkView;
    el.onBeforeEnter({ params: { agentId: 'agent-1' }, search });
    document.body.append(el);
    await waitUntil(() =>
      Boolean(el.shadowRoot!.querySelector('[data-testid="talk-agent-name"]'))
    );
    await waitUntil(() => chat(el).events.length > 0);
    return el;
  }

  function chat(el: AgentTalkView) {
    return el.shadowRoot!.querySelector('session-chat-view') as HTMLElement & {
      sessionId: string;
      events: Array<{ id: string }>;
      pending: Array<{ id: string }>;
      pendingApprovals: unknown[];
    };
  }

  function composer(el: AgentTalkView): TalkComposer {
    return el.shadowRoot!.querySelector('talk-composer') as TalkComposer;
  }

  function subject(el: AgentTalkView): string {
    return (
      el.shadowRoot!.querySelector('[data-testid="talk-session-subject"]')!
        .textContent || ''
    );
  }

  function controlBodies(): Array<Record<string, unknown>> {
    return fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('/control/command'))
      .map((call) => JSON.parse(call.args[1].body));
  }

  async function typeAndSend(el: AgentTalkView, text: string): Promise<void> {
    const box = composer(el).shadowRoot!.querySelector(
      '[data-testid="composer-input"]'
    ) as HTMLElement & { value: string };
    box.value = text;
    box.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await composer(el).updateComplete;
    box.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
  }

  it('stays on the open session when another live session of the same agent reports activity', async () => {
    const el = await mount();
    expect(chat(el).sessionId).to.equal('sess-1');
    el.receiveActivity({
      payload: { managed_agent_id: 'agent-1', runtime_session_id: 'sess-2' },
    });
    await aTimeout(50);
    await el.updateComplete;
    expect(chat(el).sessionId).to.equal('sess-1');
    expect(subject(el)).to.contain('Refactor the billing job');
    expect(chat(el).events.map((event) => event.id)).to.deep.equal([
      'sess-1-e1',
    ]);
    el.remove();
  });

  it('shows a switch notice for a new session without switching', async () => {
    const el = await mount();
    el.receiveActivity({
      payload: { managed_agent_id: 'agent-1', runtime_session_id: 'sess-2' },
    });
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="session-notice"]')
    );
    const notice = el.shadowRoot!.querySelector(
      '[data-testid="session-notice"]'
    ) as HTMLElement;
    expect(notice.getAttribute('aria-live')).to.equal('polite');
    expect(notice.textContent).to.contain(
      'New session: Fix the flaky login test.'
    );
    expect(chat(el).sessionId).to.equal('sess-1');

    (
      notice.querySelector(
        '[data-testid="session-notice-switch"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => chat(el).sessionId === 'sess-2');
    await waitUntil(() => chat(el).events[0]?.id === 'sess-2-e1');
    el.remove();
  });

  it('switching from the picker loads that session and clears pending, events and approvals', async () => {
    fetchStub
      .withArgs(sinon.match(/control\/command/))
      .resolves(new Response('nope', { status: 500 }));
    const el = await mount();
    await typeAndSend(el, 'for session one');
    await waitUntil(() => chat(el).pending.length === 1);

    const picker = el.shadowRoot!.querySelector('talk-session-picker')!;
    expect(picker.shadowRoot, 'light DOM, content stays visible').to.equal(
      null
    );
    const option = picker.querySelector(
      '[data-session-id="sess-2"]'
    ) as HTMLElement;
    expect(option.textContent).to.contain('Fix the flaky login test');
    option.click();

    await waitUntil(() => chat(el).sessionId === 'sess-2');
    expect(chat(el).pending).to.have.length(0);
    expect(chat(el).pendingApprovals).to.have.length(0);
    await waitUntil(() => chat(el).events[0]?.id === 'sess-2-e1');
    expect(chat(el).events).to.have.length(1);
    el.remove();
  });

  it('New session sends start_new_session with no target_session_id', async () => {
    const el = await mount();
    const picker = el.shadowRoot!.querySelector('talk-session-picker')!;
    (
      picker.querySelector('[data-testid="talk-new-session"]') as HTMLElement
    ).click();
    await waitUntil(() => composer(el).sessionId === null);
    expect(chat(el).events).to.have.length(0);
    await typeAndSend(el, 'start fresh');
    await waitUntil(() => controlBodies().length === 1);
    const body = controlBodies()[0];
    expect(body.start_new_session).to.equal(true);
    expect(body.target_session_id ?? null).to.equal(null);
    el.remove();
  });

  it('New session opens the session the send created and clears its echoed bubble', async () => {
    let activity: unknown[] = [];
    fetchStub.withArgs(sinon.match(/control\/command/)).callsFake(async () =>
      jsonResponse({
        command_id: 'cmd-new',
        status: 'delivered',
        // The agent's existing binding, not the session this send created.
        runtime_session_id: 'sess-1',
        target_session_id: 'sess-new',
      })
    );
    fetchStub
      .withArgs(sinon.match(/sess-new\/activity/))
      .callsFake(async () => jsonResponse({ items: activity }));
    const el = await mount();
    (
      el
        .shadowRoot!.querySelector('talk-session-picker')!
        .querySelector('[data-testid="talk-new-session"]') as HTMLElement
    ).click();
    await waitUntil(() => composer(el).sessionId === null);

    // Before the send, another session's traffic is offered, not opened.
    el.receiveActivity({
      payload: { managed_agent_id: 'agent-1', runtime_session_id: 'sess-2' },
    });
    await aTimeout(50);
    expect(chat(el).sessionId).to.equal('');
    expect(el.shadowRoot!.querySelector('[data-testid="session-notice"]')).to
      .exist;

    activity = [
      {
        activity_type: 'agent_control_message',
        timestamp: '2026-09-03T11:00:00Z',
        title: 'start fresh',
        summary: 'start fresh',
        metadata: { command_id: 'cmd-new' },
      },
    ];
    await typeAndSend(el, 'start fresh');
    await waitUntil(() => chat(el).sessionId === 'sess-new');
    await waitUntil(() => composer(el).pendingMessages.length === 0);
    el.remove();
  });

  it('a load-more page resolving after a switch is discarded', async () => {
    let releasePage: (response: Response) => void = () => undefined;
    eventPages.set('sess-1:0', async () =>
      eventsFor('sess-1', ['sess-1-e1'], true)
    );
    eventPages.set(
      'sess-1:1',
      () => new Promise<Response>((resolve) => (releasePage = resolve))
    );
    const el = await mount();
    chat(el).dispatchEvent(
      new CustomEvent('session-events-page-requested', { bubbles: true })
    );
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('offset=1'))
    );
    (
      el
        .shadowRoot!.querySelector('talk-session-picker')!
        .querySelector('[data-session-id="sess-2"]') as HTMLElement
    ).click();
    await waitUntil(() => chat(el).events[0]?.id === 'sess-2-e1');
    releasePage(eventsFor('sess-1', ['sess-1-old']));
    await aTimeout(50);
    expect(chat(el).events.map((event) => event.id)).to.deep.equal([
      'sess-2-e1',
    ]);
    el.remove();
  });

  it('a failed load-more page does not mark the session as failed to load', async () => {
    eventPages.set('sess-1:0', async () =>
      eventsFor('sess-1', ['sess-1-e1'], true)
    );
    eventPages.set(
      'sess-1:1',
      async () => new Response('down', { status: 500 })
    );
    const el = await mount();
    chat(el).dispatchEvent(
      new CustomEvent('session-events-page-requested', { bubbles: true })
    );
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('offset=1'))
    );
    await aTimeout(50);
    await el.updateComplete;
    expect(composer(el).sessionLoadFailed).to.equal(false);
    el.remove();
  });

  it('refetches the open session on socket reconnect', async () => {
    // Other parts of the page listen too; tell every listener.
    const listeners: Array<(state: ConnectionState) => void> = [];
    const onState = (state: ConnectionState) =>
      listeners.forEach((listener) => listener(state));
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => undefined);
    sinon
      .stub(unifiedWebSocketManager, 'getState')
      .returns(ConnectionState.CONNECTED);
    sinon.stub(unifiedWebSocketManager, 'onStateChange').callsFake((cb) => {
      listeners.push(cb as (state: ConnectionState) => void);
      return () => undefined;
    });
    sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    const el = await mount();
    const eventCalls = () =>
      fetchStub
        .getCalls()
        .filter((call) =>
          String(call.args[0]).includes('sess-1/gateway-events')
        ).length;
    const before = eventCalls();
    onState(ConnectionState.DISCONNECTED);
    onState(ConnectionState.CONNECTED);
    await waitUntil(() => eventCalls() > before);
    expect(chat(el).sessionId).to.equal('sess-1');
    el.remove();
  });

  it('shows a reload error and keeps the thread', async () => {
    const el = await mount();
    agentFailure = true;
    (el as unknown as { load: () => Promise<void> }).load();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="talk-error"]')
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="talk-error"]')!.textContent
    ).to.contain('Failed to fetch managed agent');
    expect(chat(el).events.map((event) => event.id)).to.deep.equal([
      'sess-1-e1',
    ]);
    el.remove();
  });

  it('refocuses the input after a session switch and after retry', async () => {
    const el = await mount();
    const focus = sinon.spy(composer(el), 'focusInput');
    (
      el
        .shadowRoot!.querySelector('talk-session-picker')!
        .querySelector('[data-session-id="sess-2"]') as HTMLElement
    ).click();
    await waitUntil(() => focus.callCount === 1);
    el.dispatchEvent(
      new CustomEvent('talk-retry', {
        detail: { id: 'missing' },
        bubbles: true,
      })
    );
    await waitUntil(() => focus.callCount === 2);
    el.remove();
  });

  it('drops a reload error that resolves after a switch', async () => {
    const el = await mount();
    let failAgent: (response: Response) => void = () => undefined;
    fetchStub
      .withArgs(sinon.match(/\/api\/v1\/agents\/agent-1/))
      .callsFake(
        () => new Promise<Response>((resolve) => (failAgent = resolve))
      );
    void (el as unknown as { load: () => Promise<void> }).load();
    (
      el
        .shadowRoot!.querySelector('talk-session-picker')!
        .querySelector('[data-session-id="sess-2"]') as HTMLElement
    ).click();
    await waitUntil(() => chat(el).events[0]?.id === 'sess-2-e1');
    failAgent(new Response('down', { status: 500 }));
    await aTimeout(50);
    expect(el.shadowRoot!.querySelector('[data-testid="talk-error"]')).to.not
      .exist;
    el.remove();
  });

  it('only fetches the sessions section of the agent detail', async () => {
    const el = await mount();
    const agentCall = fetchStub
      .getCalls()
      .map((call) => String(call.args[0]))
      .find((url) => url.includes('/api/v1/agents/agent-1'));
    expect(agentCall).to.contain('include=sessions');
    el.remove();
  });

  it('sending into an ended session is blocked and offers switch or new', async () => {
    sessions = [
      { ...SESSION, ended_at: '2026-09-03T10:40:00Z', is_active_now: false },
      OTHER,
    ];
    const el = await mount('?session=sess-1');
    await typeAndSend(el, 'are you there?');
    const guard = await waitUntil(
      () =>
        composer(el).shadowRoot!.querySelector(
          '[data-testid="ended-session-guard"]'
        ),
      'no ended-session guard'
    ).then(() =>
      composer(el).shadowRoot!.querySelector(
        '[data-testid="ended-session-guard"]'
      )
    );
    expect(controlBodies()).to.have.length(0);
    expect(guard!.textContent).to.contain('This session has ended');
    const switchButton = guard!.querySelector(
      '[data-testid="ended-switch"]'
    ) as HTMLElement;
    expect(switchButton.textContent).to.contain('Fix the flaky login test');
    expect(guard!.querySelector('[data-testid="ended-new"]')).to.exist;
    switchButton.click();
    await waitUntil(() => chat(el).sessionId === 'sess-2');
    el.remove();
  });
});
