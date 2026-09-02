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
