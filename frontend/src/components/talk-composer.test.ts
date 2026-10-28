import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './talk-composer';
import type { TalkComposer } from './talk-composer';
import type { ManagedAgentSummary } from '../types';

function agent(overrides: Partial<ManagedAgentSummary> = {}) {
  return {
    id: 'agent-1',
    display_name: 'Hermes',
    agent_kind: 'hermes',
    session_source_type: 'hermes',
    control_state: 'plugin_connected',
    control_enabled: true,
    control_online: true,
    control_capabilities: ['send_text_prompt'],
    control_session_mode: 'remote',
    ...overrides,
  } as ManagedAgentSummary;
}

async function mount(value: ManagedAgentSummary | null): Promise<TalkComposer> {
  const el = await fixture<TalkComposer>(
    html`<talk-composer .agent=${value} sessionId="sess-1"></talk-composer>`
  );
  await el.updateComplete;
  return el;
}

function textarea(el: TalkComposer): HTMLElement & {
  value: string;
  disabled: boolean;
} {
  return el.shadowRoot!.querySelector(
    '[data-testid="composer-input"]'
  ) as never;
}

/** Shoelace fetches its icon SVGs, so only control calls count as "sent". */
function controlCalls(fetchStub: sinon.SinonStub): sinon.SinonSpyCall[] {
  return fetchStub
    .getCalls()
    .filter((call) => String(call.args[0]).includes('/control/'));
}

describe('talk-composer', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    sinon.restore();
  });

  it('enables the box when Agent Control is connected', async () => {
    const el = await mount(agent());
    expect(textarea(el).disabled).to.be.false;
    expect(el.shadowRoot!.textContent).to.contain('Enter sends');
    expect(el.shadowRoot!.querySelector('.install-command')).to.not.exist;
  });

  it('offers the install command when the plugin is not connected', async () => {
    const el = await mount(
      agent({
        control_state: 'install_pending',
        control_enabled: false,
        control_online: false,
      })
    );
    expect(textarea(el).disabled).to.be.true;
    expect(textarea(el).getAttribute('placeholder')).to.equal(
      'Install Agent Control to talk to Hermes'
    );
    const command = el.shadowRoot!.querySelector('.install-command code');
    expect(command!.textContent).to.equal(
      "preloop agents install-plugin 'Hermes'"
    );
    const link = el.shadowRoot!.querySelector('a[href]') as HTMLAnchorElement;
    expect(link.href).to.contain('docs.preloop.ai');
  });

  it('says nothing to install for an unsupported agent kind', async () => {
    const el = await mount(
      agent({
        agent_kind: 'claude_desktop',
        session_source_type: 'claude_desktop',
        control_state: 'unsupported',
        control_enabled: false,
        control_online: false,
        control_capabilities: [],
      })
    );
    expect(textarea(el).disabled).to.be.true;
    expect(textarea(el).getAttribute('placeholder')).to.contain(
      'Agent Control is not available for'
    );
    expect(el.shadowRoot!.querySelector('.install-command')).to.not.exist;
  });

  it('sends on Enter and adds a newline on Shift+Enter', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ status: 'delivered', published: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const el = await mount(agent());
    const input = textarea(el);
    input.value = 'ship it';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;

    input.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        shiftKey: true,
        bubbles: true,
      })
    );
    await el.updateComplete;
    expect(controlCalls(fetchStub), 'Shift+Enter must not send').to.be.empty;

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
    await waitUntil(() => controlCalls(fetchStub).length > 0);
    const [url, init] = controlCalls(fetchStub)[0].args;
    expect(url).to.contain('/agents/agent-1/control/command');
    expect(JSON.parse(init.body).message).to.equal('ship it');
  });

  it('attributes the turn to the entry point it was given', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ status: 'delivered', published: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const el = await fixture<TalkComposer>(
      html`<talk-composer
        .agent=${agent()}
        sessionId="sess-1"
        sourceContext="dashboard-active-agents"
      ></talk-composer>`
    );
    await el.updateComplete;
    const input = textarea(el);
    input.value = 'status?';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );

    await waitUntil(() => controlCalls(fetchStub).length > 0);
    const [, init] = controlCalls(fetchStub)[0].args;
    expect(JSON.parse(init.body).metadata).to.deep.equal({
      source: 'preloop_console',
      requested_from: 'dashboard-active-agents',
    });
  });

  it('shows the turn optimistically and offers Retry when it fails', async () => {
    fetchStub.resolves(new Response('nope', { status: 500 }));
    const el = await mount(agent());
    const pendingEvents: number[] = [];
    el.addEventListener('talk-pending-changed', (event) => {
      pendingEvents.push(
        (event as CustomEvent<{ pending: unknown[] }>).detail.pending.length
      );
    });

    const input = textarea(el);
    input.value = 'deploy';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector(
        '[data-testid="composer-send"]'
      ) as HTMLElement
    ).click();

    await waitUntil(() => el.pendingMessages[0]?.state === 'failed');
    expect(pendingEvents[0]).to.equal(1);
    expect(el.pendingMessages[0].text).to.equal('deploy');
    expect(textarea(el).value, 'the box empties as the turn leaves').to.equal(
      ''
    );

    fetchStub.resolves(
      new Response(JSON.stringify({ status: 'delivered', published: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    await el.retry(el.pendingMessages[0].id);
    expect(el.pendingMessages[0].state).to.equal('sent');
    expect(controlCalls(fetchStub)).to.have.length(2);
  });

  it('takes the session over before sending when it is running locally', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ status: 'delivered', published: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const el = await mount(agent({ control_session_mode: 'local' }));
    const input = textarea(el);
    input.value = 'stop';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );

    await waitUntil(() => controlCalls(fetchStub).length === 2);
    expect(controlCalls(fetchStub)[0].args[0]).to.contain('/control/takeover');
    expect(controlCalls(fetchStub)[1].args[0]).to.contain('/control/command');
  });

  it('uses the active harness session without requesting a new process', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ status: 'delivered' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const el = await mount(
      agent({
        agent_kind: 'pi',
        supports_new_session: false,
        supports_voice: false,
        supports_interrupt: true,
      })
    );
    el.sessionId = null;
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain('Interrupt');
    expect(el.shadowRoot!.textContent).not.to.contain('Release');
    const input = textarea(el);
    input.value = 'continue';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
    await waitUntil(() => controlCalls(fetchStub).length === 1);
    const body = JSON.parse(controlCalls(fetchStub)[0].args[1].body);
    expect(body.start_new_session).to.equal(false);
  });

  it('never releases the session on its own', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify({ status: 'delivered', published: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const el = await mount(agent({ control_session_mode: 'local' }));
    const input = textarea(el);
    input.value = 'keep going';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );

    await waitUntil(() => controlCalls(fetchStub).length === 2);
    const calls = controlCalls(fetchStub).map((call) => String(call.args[0]));
    expect(calls.some((url) => url.includes('/control/release'))).to.be.false;
  });
});

describe('talk-composer session scoping', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    sinon.restore();
  });

  function ok(): Response {
    return new Response(
      JSON.stringify({ status: 'delivered', command_id: 'cmd-1' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
  }

  async function type(el: TalkComposer, text: string): Promise<void> {
    const input = textarea(el);
    input.value = text;
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
  }

  function commandBodies(): Array<Record<string, unknown>> {
    return controlCalls(fetchStub)
      .filter((call) => String(call.args[0]).includes('/control/command'))
      .map((call) => JSON.parse(call.args[1].body));
  }

  it('delivers to the session the message was typed in even if sessionId changes during takeover', async () => {
    let releaseTakeover: (response: Response) => void = () => undefined;
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      if (String(input).includes('/control/takeover')) {
        return new Promise<Response>((resolve) => (releaseTakeover = resolve));
      }
      return ok();
    });
    const el = await mount(agent({ control_session_mode: 'local' }));
    await type(el, 'for sess-1');
    await waitUntil(() => controlCalls(fetchStub).length === 1);
    el.sessionId = 'sess-2';
    await el.updateComplete;
    releaseTakeover(ok());
    await waitUntil(() => commandBodies().length === 1);
    expect(commandBodies()[0].target_session_id).to.equal('sess-1');
    const takeover = JSON.parse(controlCalls(fetchStub)[0].args[1].body);
    expect(takeover.target_session_id).to.equal('sess-1');
  });

  it('retry targets the original session', async () => {
    fetchStub.resolves(new Response('nope', { status: 500 }));
    const el = await mount(agent());
    await type(el, 'retry me');
    await waitUntil(() => el.pendingMessages[0]?.state === 'failed');
    el.sessionId = 'sess-2';
    await el.updateComplete;
    fetchStub.callsFake(async () => ok());
    await el.retry(el.pendingMessages[0].id);
    const bodies = commandBodies();
    expect(bodies.at(-1)!.target_session_id).to.equal('sess-1');
    expect(bodies.at(-1)!.start_new_session).to.equal(false);
  });

  it("only the current session's pending items are emitted", async () => {
    fetchStub.resolves(new Response('nope', { status: 500 }));
    const el = await mount(agent());
    const emitted: Array<Array<{ id: string }>> = [];
    el.addEventListener('talk-pending-changed', (event) => {
      emitted.push(
        (event as CustomEvent<{ pending: Array<{ id: string }> }>).detail
          .pending
      );
    });
    await type(el, 'only in sess-1');
    await waitUntil(() => el.pendingMessages[0]?.state === 'failed');
    expect(emitted.at(-1)).to.have.length(1);

    el.sessionId = 'sess-2';
    await el.updateComplete;
    expect(emitted.at(-1), 'sess-2 shows nothing from sess-1').to.have.length(
      0
    );

    el.sessionId = 'sess-1';
    await el.updateComplete;
    expect(emitted.at(-1)).to.have.length(1);
  });

  it('keeps a sent turn until the server echoes its command id', async () => {
    fetchStub.callsFake(async () => ok());
    const el = await mount(agent());
    await type(el, 'echo me');
    await waitUntil(() => el.pendingMessages[0]?.state === 'sent');
    el.clearSentPending(new Set(['cmd-other']));
    expect(el.pendingMessages, 'not echoed yet').to.have.length(1);
    el.clearSentPending(new Set(['cmd-1']));
    expect(el.pendingMessages).to.have.length(0);
  });
});

describe('talk-composer per-session routing', () => {
  let fetchStub: sinon.SinonStub;

  function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function terminalControl(): Record<string, unknown> {
    return {
      runtime_session_id: 'sess-1',
      mode: 'note',
      reason_code: 'terminal_session',
      reason: 'this session runs in a terminal that Preloop cannot type into',
      delivery: 'note',
      send_path: null,
      managed_agent_id: 'agent-1',
      agent_name: 'Hermes',
      agent_kind: 'claude_code',
    };
  }

  /** Route by URL: the control read, the note POST, and commands. */
  function route(control: Record<string, unknown> | null): void {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/runtime-sessions/') && url.endsWith('/control')) {
        return control ? json(control) : json({ detail: 'boom' }, 500);
      }
      if (url.endsWith('/api/v1/operator-notes')) {
        return json({ note_id: 'note-1', state: 'pending' }, 201);
      }
      if (url.includes('/control/')) {
        return json({ status: 'delivered', published: true });
      }
      return new Response('', { status: 200 });
    });
  }

  async function mountRouted(
    options: { idle?: boolean; loadFailed?: boolean } = {}
  ): Promise<TalkComposer> {
    const el = await fixture<TalkComposer>(
      html`<talk-composer
        .agent=${agent({
          control_capabilities: ['send_text_prompt', 'release'],
          supports_interrupt: true,
        } as Partial<ManagedAgentSummary>)}
        sessionId="sess-1"
        .sessionIdle=${Boolean(options.idle)}
        .sessionLoadFailed=${Boolean(options.loadFailed)}
      ></talk-composer>`
    );
    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some((call) => String(call.args[0]).endsWith('/sess-1/control')),
      'control mode not read'
    );
    if (options.loadFailed) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    } else {
      await waitUntil(
        () => el.shadowRoot!.querySelector('[data-testid="composer-mode"]'),
        'mode line never rendered'
      );
    }
    await el.updateComplete;
    return el;
  }

  async function type(el: TalkComposer, text: string): Promise<void> {
    const input = textarea(el);
    input.value = text;
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
  }

  function buttons(el: TalkComposer): string[] {
    return Array.from(el.shadowRoot!.querySelectorAll('sl-button')).map(
      (button) => (button.textContent || '').trim()
    );
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    sinon.restore();
  });

  it('terminal session sends an operator note, not a command', async () => {
    route(terminalControl());
    const el = await mountRouted();

    await type(el, 'use the staging bucket');
    await waitUntil(
      () => el.pendingMessages[0]?.state === 'waiting',
      'note never sent'
    );

    const noteCall = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).endsWith('/api/v1/operator-notes'));
    expect(noteCall, 'POST /operator-notes').to.exist;
    const body = JSON.parse(String((noteCall!.args[1] as RequestInit).body));
    expect(body).to.deep.equal({
      text: 'use the staging bucket',
      runtime_session_id: 'sess-1',
    });
    expect(controlCalls(fetchStub)).to.have.length(0);
    expect(el.pendingMessages[0].noteId).to.equal('note-1');
  });

  it('terminal session mode line states when the note is read', async () => {
    route(terminalControl());
    const el = await mountRouted();
    const mode = () =>
      el
        .shadowRoot!.querySelector('[data-testid="composer-mode"]')!
        .textContent!.trim();

    expect(mode()).to.equal(
      'Terminal session. Your message is read at its next tool call or when the turn ends.'
    );
    expect(el.shadowRoot!.textContent).to.not.contain('Queued for delivery');

    el.sessionIdle = true;
    await el.updateComplete;
    expect(mode()).to.contain(
      'It is idle: it reads this with your next prompt in the terminal.'
    );
  });

  it('note bubble shows Read at tool call on operator_note_delivered', async () => {
    route(terminalControl());
    const el = await mountRouted();
    await type(el, 'pause before deploy');
    await waitUntil(() => el.pendingMessages[0]?.state === 'waiting');

    el.handleRealtime({
      event_type: 'runtime_session_updated',
      payload: {
        runtime_session_id: 'sess-1',
        activity_type: 'agent_control_message',
        metadata: {
          kind: 'operator_note',
          note_id: 'note-1',
          hook_event: 'pre_tool_use',
          delivered_at: '2026-10-21T10:12:40Z',
        },
      },
    });

    const item = el.pendingMessages[0];
    expect(item.state).to.equal('read');
    expect(item.readPoint).to.equal('tool call');
    expect(item.readAt).to.equal('2026-10-21T10:12:40Z');

    // The thread renders "Read at tool call <time>" for that state.
    await import('./session-chat-view');
    const view = await fixture(
      html`<session-chat-view .pending=${[item]}></session-chat-view>`
    );
    await (view as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const label = view.shadowRoot!.querySelector(
      '[data-testid="pending-note-state"]'
    )!;
    expect(label.textContent!.replace(/\s+/g, ' ').trim()).to.match(
      /^Read at tool call \S/
    );
  });

  it('a delivered note leaves exactly one bubble', async () => {
    route(terminalControl());
    const el = await mountRouted();
    await type(el, 'rebase first');
    await waitUntil(() => el.pendingMessages[0]?.state === 'waiting');

    // Not echoed yet: the pending bubble stays.
    el.clearSentPending(new Set());
    expect(el.pendingMessages).to.have.length(1);

    el.handleRealtime({
      payload: {
        runtime_session_id: 'sess-1',
        metadata: {
          kind: 'operator_note',
          note_id: 'note-1',
          hook_event: 'stop',
          delivered_at: '2026-10-21T10:12:40Z',
        },
      },
    });
    // The reload shows the note on the timeline: the pending copy goes.
    el.clearSentPending(new Set(['note-1']));
    expect(el.pendingMessages).to.have.length(0);

    // The timeline copy carries the read point itself.
    await import('./session-chat-view');
    const view = await fixture(
      html`<session-chat-view
        .activity=${[
          {
            id: 'act-1',
            activity_type: 'agent_control_message',
            timestamp: '2026-10-21T10:12:40Z',
            title: 'Operator message',
            summary: 'rebase first',
            status: 'delivered',
            metadata: {
              kind: 'operator_note',
              note_id: 'note-1',
              delivery_event: 'stop',
              author_display: 'Ada',
              author_auth_method: 'jwt',
            },
          },
        ]}
      ></session-chat-view>`
    );
    await (view as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const bubbles = view.shadowRoot!.querySelectorAll(
      '.bubble[data-kind="operator"]'
    );
    expect(bubbles).to.have.length(1);
    expect(
      bubbles[0].querySelector('[data-testid="note-read-at"]')!.textContent
    ).to.contain('Read at turn end');
  });

  it('a line typed before the mode is known still goes as a note', async () => {
    let releaseControl: () => void = () => {};
    const controlReady = new Promise<void>((resolve) => {
      releaseControl = resolve;
    });
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/control') && url.includes('/runtime-sessions/')) {
        await controlReady;
        return json(terminalControl());
      }
      if (url.endsWith('/api/v1/operator-notes')) {
        return json({ note_id: 'note-early', state: 'pending' }, 201);
      }
      if (url.includes('/control/')) {
        return json({ status: 'delivered', published: true });
      }
      return new Response('', { status: 200 });
    });
    const el = await fixture<TalkComposer>(
      html`<talk-composer .agent=${agent()} sessionId="sess-1"></talk-composer>`
    );
    await el.updateComplete;
    await type(el, 'early line');
    releaseControl();
    await waitUntil(() => el.pendingMessages[0]?.state === 'waiting');

    expect(el.pendingMessages[0].noteId).to.equal('note-early');
    expect(controlCalls(fetchStub)).to.have.length(0);
  });

  it('a command refused as terminal_session is resent as a note', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/control') && url.includes('/runtime-sessions/')) {
        // An older answer said command; the server has since learned better.
        return json({
          ...terminalControl(),
          mode: 'command',
          reason_code: null,
        });
      }
      if (url.endsWith('/api/v1/operator-notes')) {
        return json({ note_id: 'note-409', state: 'pending' }, 201);
      }
      if (url.includes('/control/')) {
        return json(
          { detail: { code: 'terminal_session', message: 'terminal' } },
          409
        );
      }
      return new Response('', { status: 200 });
    });
    const el = await mountRouted();
    await type(el, 'refused line');
    await waitUntil(() => el.pendingMessages[0]?.state === 'waiting');

    expect(el.pendingMessages).to.have.length(1);
    expect(el.pendingMessages[0].noteId).to.equal('note-409');
  });

  it('two Enters before the mode is known send one note to the typed session', async () => {
    let releaseControl: () => void = () => {};
    const controlReady = new Promise<void>((resolve) => {
      releaseControl = resolve;
    });
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/control') && url.includes('/runtime-sessions/')) {
        await controlReady;
        return json(terminalControl());
      }
      if (url.endsWith('/api/v1/operator-notes')) {
        return json({ note_id: 'note-once', state: 'pending' }, 201);
      }
      if (url.includes('/control/')) {
        return json({ status: 'delivered', published: true });
      }
      return new Response('', { status: 200 });
    });
    const el = await fixture<TalkComposer>(
      html`<talk-composer .agent=${agent()} sessionId="sess-1"></talk-composer>`
    );
    await el.updateComplete;
    await type(el, 'only once');
    textarea(el).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
    // The operator switches sessions while the read is in flight.
    el.sessionId = 'sess-2';
    await el.updateComplete;
    releaseControl();
    await waitUntil(() => el.pendingMessages[0]?.state === 'waiting');

    expect(el.pendingMessages).to.have.length(1);
    expect(el.pendingMessages[0].targetSessionId).to.equal('sess-1');
    const notes = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).endsWith('/api/v1/operator-notes')
      );
    expect(notes).to.have.length(1);
    expect(
      JSON.parse(String((notes[0].args[1] as RequestInit).body))
        .runtime_session_id
    ).to.equal('sess-1');
  });

  it('retry re-routes a failed command as a note once the mode is note', async () => {
    let commandStatus = 500;
    let mode = 'command';
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/control') && url.includes('/runtime-sessions/')) {
        return json({ ...terminalControl(), mode });
      }
      if (url.endsWith('/api/v1/operator-notes')) {
        return json({ note_id: 'note-retry', state: 'pending' }, 201);
      }
      if (url.includes('/control/')) {
        return json({ detail: 'boom' }, commandStatus);
      }
      return new Response('', { status: 200 });
    });
    const el = await mountRouted();
    await type(el, 'retry me');
    await waitUntil(() => el.pendingMessages[0]?.state === 'failed');
    const commandsBefore = controlCalls(fetchStub).length;

    mode = 'note';
    el.handleRealtime({ payload: { runtime_session_id: 'sess-1' } });
    await waitUntil(
      () =>
        el
          .shadowRoot!.querySelector('[data-testid="composer-mode"]')
          ?.textContent?.includes('Terminal session'),
      'mode never became note'
    );
    await el.retry(el.pendingMessages[0].id);

    expect(el.pendingMessages[0].state).to.equal('waiting');
    expect(el.pendingMessages[0].noteId).to.equal('note-retry');
    expect(controlCalls(fetchStub).length).to.equal(commandsBefore);
    commandStatus = 200;
  });

  it('retry stays a command when the session still takes commands', async () => {
    let commandStatus = 500;
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/control') && url.includes('/runtime-sessions/')) {
        return json({
          ...terminalControl(),
          mode: 'command',
          reason_code: null,
        });
      }
      if (url.endsWith('/api/v1/operator-notes')) {
        return json({ note_id: 'never', state: 'pending' }, 201);
      }
      if (url.includes('/control/')) {
        return commandStatus === 200
          ? json({ status: 'delivered', published: true })
          : json({ detail: 'boom' }, commandStatus);
      }
      return new Response('', { status: 200 });
    });
    const el = await mountRouted();
    await type(el, 'retry as command');
    await waitUntil(() => el.pendingMessages[0]?.state === 'failed');

    commandStatus = 200;
    await el.retry(el.pendingMessages[0].id);

    expect(el.pendingMessages[0].state).to.equal('sent');
    expect(
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).endsWith('/api/v1/operator-notes'))
    ).to.equal(false);
  });

  it('no Interrupt or Release for a note-mode session', async () => {
    route(terminalControl());
    const el = await mountRouted();

    expect(buttons(el)).to.not.include('Interrupt');
    expect(buttons(el)).to.not.include('Release');
    expect(el.shadowRoot!.textContent).to.not.contain('You have this session');
  });

  it('no control row when the session failed to load', async () => {
    route(null);
    const el = await mountRouted({ loadFailed: true });

    expect(el.shadowRoot!.querySelector('.control-row')).to.not.exist;
    expect(buttons(el)).to.not.include('Interrupt');
    expect(buttons(el)).to.not.include('Release');
  });
});

describe('talk-composer keyboard and slash commands', () => {
  let fetchStub: sinon.SinonStub;

  function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function commandControl(sessionId = 'sess-1'): Record<string, unknown> {
    return {
      runtime_session_id: sessionId,
      mode: 'command',
      reason_code: 'harness_session',
      reason: 'Preloop drives this session',
      delivery: 'command',
      send_path: null,
      managed_agent_id: 'agent-1',
      agent_name: 'Hermes',
      agent_kind: 'hermes',
    };
  }

  function route(control: Record<string, unknown>): void {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/runtime-sessions/') && url.endsWith('/control')) {
        const id = url.split('/runtime-sessions/')[1].split('/')[0];
        return json({ ...control, runtime_session_id: id });
      }
      if (url.includes('/control/'))
        return json({ status: 'delivered', published: true });
      return new Response('', { status: 200 });
    });
  }

  async function mount(
    overrides: Partial<ManagedAgentSummary> = {},
    sessionId = 'sess-1'
  ): Promise<TalkComposer> {
    const el = await fixture<TalkComposer>(
      html`<talk-composer
        .agent=${agent({
          control_capabilities: ['send_text_prompt', 'release'],
          supports_interrupt: true,
          ...overrides,
        } as Partial<ManagedAgentSummary>)}
        sessionId=${sessionId}
      ></talk-composer>`
    );
    await waitUntil(
      () => el.shadowRoot!.querySelector('[data-testid="composer-mode"]'),
      'mode line never rendered'
    );
    await el.updateComplete;
    return el;
  }

  async function setText(el: TalkComposer, text: string): Promise<void> {
    const input = textarea(el);
    input.value = text;
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;
  }

  async function press(el: TalkComposer, key: string): Promise<KeyboardEvent> {
    const event = new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
    });
    textarea(el).dispatchEvent(event);
    await el.updateComplete;
    return event;
  }

  function commandBodies(): Array<Record<string, unknown>> {
    return fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('/control/'))
      .map((call) => JSON.parse(call.args[1].body));
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    sessionStorage.clear();
    fetchStub = sinon.stub(window, 'fetch');
    route(commandControl());
  });

  afterEach(() => {
    sinon.restore();
    sessionStorage.clear();
  });

  it('Esc sends interrupt only when supported', async () => {
    const el = await mount();
    expect(el.shadowRoot!.textContent).to.contain('Esc interrupts');
    const event = await press(el, 'Escape');
    expect(event.defaultPrevented).to.equal(true);
    await waitUntil(() => commandBodies().length === 1);
    expect(commandBodies()[0]).to.include({
      interrupt: true,
      target_session_id: 'sess-1',
    });

    fetchStub.resetHistory();
    const unsupported = await mount({ supports_interrupt: false });
    expect(unsupported.shadowRoot!.textContent).to.not.contain(
      'Esc interrupts'
    );
    const ignored = await press(unsupported, 'Escape');
    expect(ignored.defaultPrevented).to.equal(false);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(commandBodies()).to.have.length(0);
  });

  it('Esc does not interrupt a session Preloop cannot drive', async () => {
    const el = await mount({ control_session_mode: 'local' });
    const event = await press(el, 'Escape');
    expect(event.defaultPrevented).to.equal(false);
    expect(commandBodies()).to.have.length(0);
  });

  it("Up recalls this session's last prompt only", async () => {
    const el = await mount();
    await setText(el, 'first in one');
    await press(el, 'Enter');
    await setText(el, 'second in one');
    await press(el, 'Enter');
    await waitUntil(() => commandBodies().length === 2);

    await press(el, 'ArrowUp');
    expect(textarea(el).value).to.equal('second in one');
    await press(el, 'ArrowUp');
    expect(textarea(el).value).to.equal('first in one');
    await press(el, 'ArrowDown');
    expect(textarea(el).value).to.equal('second in one');
    await press(el, 'ArrowDown');
    expect(textarea(el).value).to.equal('');

    // Another session has its own history, and none yet.
    el.sessionId = 'sess-2';
    await el.updateComplete;
    const up = await press(el, 'ArrowUp');
    expect(textarea(el).value).to.equal('');
    expect(up.defaultPrevented).to.equal(false);

    // Back on the first session, its prompts are still there.
    el.sessionId = 'sess-1';
    await el.updateComplete;
    await press(el, 'ArrowUp');
    expect(textarea(el).value).to.equal('second in one');
  });

  it('Up leaves a draft alone', async () => {
    const el = await mount();
    await setText(el, 'sent');
    await press(el, 'Enter');
    await setText(el, 'a draft');
    const event = await press(el, 'ArrowUp');
    expect(event.defaultPrevented).to.equal(false);
    expect(textarea(el).value).to.equal('a draft');
  });

  it('/ opens the command menu with what this session can do', async () => {
    const el = await mount();
    await setText(el, '/');
    const menu = el.shadowRoot!.querySelector('talk-command-menu');
    expect(menu).to.exist;
    // Light DOM inside the composer: the rows are the menu's own children.
    expect(menu!.shadowRoot).to.equal(null);
    const names = Array.from(menu!.querySelectorAll('[data-command]')).map(
      (row) => row.getAttribute('data-command')
    );
    expect(names).to.deep.equal([
      'new',
      'resume',
      'interrupt',
      'release',
      'status',
    ]);

    await setText(el, '/re');
    const filtered = Array.from(
      el.shadowRoot!.querySelectorAll('talk-command-menu [data-command]')
    ).map((row) => row.getAttribute('data-command'));
    expect(filtered).to.deep.equal(['resume', 'release']);
    await press(el, 'ArrowDown');
    expect(
      el
        .shadowRoot!.querySelector('[aria-selected="true"]')
        ?.getAttribute('data-command')
    ).to.equal('release');

    const unsupported = await mount({
      supports_interrupt: false,
      control_capabilities: ['send_text_prompt'],
    });
    await setText(unsupported, '/');
    const rows = Array.from(
      unsupported.shadowRoot!.querySelectorAll(
        'talk-command-menu [data-command]'
      )
    ).map((row) => row.getAttribute('data-command'));
    expect(rows).to.deep.equal(['new', 'resume', 'status']);
  });

  it('/new starts a new session', async () => {
    const el = await mount();
    const asked = sinon.spy();
    el.addEventListener('talk-new-session', asked);
    await setText(el, '/new');
    await press(el, 'Enter');
    expect(asked.calledOnce).to.equal(true);
    expect(textarea(el).value).to.equal('');
    expect(commandBodies()).to.have.length(0);
  });

  it('/resume opens the picker', async () => {
    const el = await mount();
    const asked = sinon.spy();
    el.addEventListener('talk-open-session-picker', asked);
    await setText(el, '/res');
    await press(el, 'Enter');
    expect(asked.calledOnce).to.equal(true);
    expect(commandBodies()).to.have.length(0);
  });

  it('/interrupt and /release drive the session', async () => {
    const el = await mount();
    await setText(el, '/interrupt');
    await press(el, 'Enter');
    await waitUntil(() => commandBodies().length === 1);
    expect(commandBodies()[0]).to.include({ interrupt: true });
    await setText(el, '/release');
    await press(el, 'Enter');
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/control/release'))
    );
  });

  it('/status says where the message would go', async () => {
    const el = await mount();
    await setText(el, '/status');
    await press(el, 'Enter');
    expect(el.shadowRoot!.querySelector('.hint')!.textContent).to.contain(
      'You have this session'
    );
    expect(commandBodies()).to.have.length(0);
  });

  it('Esc closes the menu without interrupting', async () => {
    const el = await mount();
    await setText(el, '/');
    await press(el, 'Escape');
    expect(el.shadowRoot!.querySelector('talk-command-menu')).to.not.exist;
    expect(commandBodies()).to.have.length(0);
  });

  it('unknown slash text is sent as a normal message', async () => {
    const el = await mount();
    await setText(el, '/deploy');
    expect(el.shadowRoot!.querySelector('talk-command-menu')).to.not.exist;
    await press(el, 'Enter');
    await waitUntil(() => commandBodies().length === 1);
    expect(commandBodies()[0].message).to.equal('/deploy');

    await setText(el, '/new please');
    await press(el, 'Enter');
    await waitUntil(() => commandBodies().length === 2);
    expect(commandBodies()[1].message).to.equal('/new please');
  });
  it('an unavailable command is not sent as a message', async () => {
    const el = await mount({ supports_interrupt: false });
    await setText(el, '/interrupt');
    expect(el.shadowRoot!.querySelector('talk-command-menu')).to.not.exist;
    await press(el, 'Enter');
    expect(commandBodies()).to.have.length(0);
    expect(el.shadowRoot!.querySelector('.hint')!.textContent).to.contain(
      '/interrupt is not available'
    );
  });

  it('/resume leaves focus to the picker', async () => {
    const el = await mount();
    const focus = sinon.spy(el, 'focusInput');
    await setText(el, '/resume');
    await press(el, 'Enter');
    expect(focus.called).to.equal(false);
  });

  it("a new session's first prompt is recalled there and nowhere else", async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/runtime-sessions/') && url.endsWith('/control'))
        return json(commandControl());
      if (url.includes('/control/'))
        return json({
          status: 'delivered',
          published: true,
          target_session_id: 'sess-new',
        });
      return new Response('', { status: 200 });
    });
    const el = await mount();
    el.sessionId = null;
    await el.updateComplete;
    await setText(el, 'start fresh');
    await press(el, 'Enter');
    await waitUntil(() => commandBodies().length === 1);
    await waitUntil(() => el.pendingMessages[0]?.state === 'sent');

    // Still starting a session: nothing to recall, no shared bucket.
    const up = await press(el, 'ArrowUp');
    expect(up.defaultPrevented).to.equal(false);
    expect(textarea(el).value).to.equal('');

    el.sessionId = 'sess-new';
    await el.updateComplete;
    await press(el, 'ArrowUp');
    expect(textarea(el).value).to.equal('start fresh');

    el.sessionId = 'sess-1';
    await el.updateComplete;
    await setText(el, '');
    await press(el, 'ArrowUp');
    expect(textarea(el).value).to.equal('');
  });
});
