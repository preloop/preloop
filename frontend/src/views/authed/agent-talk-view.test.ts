import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-talk-view.ts';
import type { AgentTalkView } from './agent-talk-view';

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
});
