import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-notes-tab';
import type { AgentNotesTab } from './agent-notes-tab';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function note(i: number) {
  return {
    note_id: `note-${i}`,
    state: 'delivered',
    text: `Conductor note ${i}: ${'a long brief line. '.repeat(40)}`,
    author: { display: 'dimitris', auth_method: 'jwt' },
    created_at: '2026-10-11T21:22:18Z',
    delivered_at: '2026-10-11T23:22:18Z',
  };
}

function noteCalls(fetchStub: sinon.SinonStub): string[] {
  return fetchStub
    .getCalls()
    .map((call) => String(call.args[0]))
    .filter(
      (url) =>
        url.includes('/operator-notes') && !url.includes('/session-targets')
    );
}

describe('agent-notes-tab', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('offset=3'))
        return jsonResponse({ notes: [note(4), note(5)], total: 5 });
      if (url.includes('/operator-notes'))
        return jsonResponse({ notes: [note(1), note(2), note(3)], total: 5 });
      return jsonResponse({});
    });
  });

  afterEach(() => sinon.restore());

  it('shows three one-line previews and loads older ones on demand', async () => {
    const el = await fixture<AgentNotesTab>(
      html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
    );
    await waitUntil(
      () => el.shadowRoot!.querySelectorAll('li').length === 3,
      'first page'
    );
    expect(noteCalls(fetchStub)[0]).to.contain('limit=3');
    // Collapsed: no full text until a row is opened.
    expect(el.shadowRoot!.querySelector('[data-testid="agent-note-full"]')).to
      .be.null;
    const preview = el.shadowRoot!.querySelector('.preview') as HTMLElement;
    expect(getComputedStyle(preview).whiteSpace).to.equal('nowrap');

    const older = el.shadowRoot!.querySelector(
      '[data-testid="notes-older"]'
    ) as HTMLElement;
    expect(older.textContent).to.contain('(2)');
    older.click();
    await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 5);
    expect(noteCalls(fetchStub)[1]).to.contain('offset=3');
    expect(el.shadowRoot!.querySelector('[data-testid="notes-older"]')).to.be
      .null;
  });

  it('expands one note to its full text on click', async () => {
    const el = await fixture<AgentNotesTab>(
      html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
    );
    await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 3);
    const row = el.shadowRoot!.querySelector('button.row') as HTMLElement;
    row.click();
    await el.updateComplete;
    const full = el.shadowRoot!.querySelector(
      '[data-testid="agent-note-full"]'
    );
    expect(full?.textContent).to.contain('Conductor note 1');
    expect(row.getAttribute('aria-expanded')).to.equal('true');
  });

  it('reuses the page the host already read instead of fetching again', async () => {
    const el = await fixture<AgentNotesTab>(
      html`<agent-notes-tab
        agent-id="agent-1"
        can-send
        .initial=${{ notes: [note(1)], total: 1 }}
      ></agent-notes-tab>`
    );
    await el.updateComplete;
    expect(el.shadowRoot!.querySelectorAll('li').length).to.equal(1);
    expect(noteCalls(fetchStub)).to.have.length(0);
  });

  it('has no box for an agent no note can reach', async () => {
    const el = await fixture<AgentNotesTab>(
      html`<agent-notes-tab agent-id="agent-1"></agent-notes-tab>`
    );
    await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 3);
    expect(
      Boolean(el.shadowRoot!.querySelector('operator-note-composer'))
    ).to.equal(false);
    expect(el.shadowRoot!.querySelector('[data-testid="notes-unsupported"]')).to
      .exist;
  });

  it('keeps the box free of a second notes list', async () => {
    const el = await fixture<AgentNotesTab>(
      html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
    );
    await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 3);
    const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
    await (composer as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    expect(composer.shadowRoot!.querySelector('[data-testid="note-list"]')).to
      .be.null;
    // Only the tab read notes; the box did not fetch its own history.
    expect(noteCalls(fetchStub)).to.have.length(1);
  });

  describe('sessions', () => {
    const live = (id: string, alias: string) => ({
      id,
      alias,
      session_source_type: 'claude_code',
      started_at: '2026-10-18T08:00:00Z',
      ended_at: null,
    });

    function stubSessions(liveRows: unknown[], noteRows: unknown[]) {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/operator-notes/session-targets'))
          return jsonResponse({ sessions: liveRows, total: liveRows.length });
        if (url.includes('/runtime-sessions'))
          return jsonResponse({ items: liveRows });
        if (url.includes('/operator-notes')) {
          const sessionId = new URL(url, location.origin).searchParams.get(
            'runtime_session_id'
          );
          const rows = sessionId
            ? noteRows.filter(
                (row) =>
                  (row as { runtime_session_id?: string })
                    .runtime_session_id === sessionId
              )
            : noteRows;
          return jsonResponse({ notes: rows, total: rows.length });
        }
        return jsonResponse({});
      });
    }

    async function settle(el: AgentNotesTab) {
      await waitUntil(() =>
        fetchStub
          .getCalls()
          .some((call) => String(call.args[0]).includes('/session-targets'))
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      await el.updateComplete;
    }

    function target(el: AgentNotesTab): HTMLSelectElement {
      return el.shadowRoot!.querySelector(
        '[data-testid="note-target"]'
      ) as HTMLSelectElement;
    }

    it('aims the box at the only live session', async () => {
      stubSessions([live('s-1', 'fixer')], []);
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await settle(el);
      expect(String(fetchStub.getCalls().map((c) => c.args[0]))).to.contain(
        'session-targets?agent_id=agent-1'
      );
      expect(target(el).value).to.equal('s-1');
      const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
      expect(composer.sessionId).to.equal('s-1');
      expect(composer.nextSession).to.equal(false);
    });

    it('reads every page of live sessions', async () => {
      const rows = Array.from({ length: 101 }, (_, i) =>
        live(`s-${i}`, `run-${i}`)
      );
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/session-targets')) {
          const offset = Number(
            new URL(url, location.origin).searchParams.get('offset')
          );
          return jsonResponse({
            sessions: rows.slice(offset, offset + 100),
            total: rows.length,
          });
        }
        if (url.includes('/operator-notes'))
          return jsonResponse({ notes: [], total: 0 });
        return jsonResponse({ items: [] });
      });
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await waitUntil(
        () => target(el)?.querySelectorAll('sl-option').length === 102,
        'all 101 live sessions and Next session'
      );
      const pages = fetchStub
        .getCalls()
        .map((call) => String(call.args[0]))
        .filter((url) => url.includes('/session-targets'));
      expect(pages).to.have.length(2);
      expect(pages[1]).to.contain('offset=100');
    });

    it('blocks an untargeted note while live sessions load', async () => {
      let release: (rows: unknown[]) => void = () => {};
      const pending = new Promise<unknown[]>((resolve) => {
        release = resolve;
      });
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/session-targets')) {
          const rows = await pending;
          return jsonResponse({ sessions: rows, total: rows.length });
        }
        if (url.includes('/operator-notes'))
          return jsonResponse({ notes: [], total: 0 });
        return jsonResponse({ items: [] });
      });
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await el.updateComplete;
      const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
      expect(composer.agentId).to.equal('');
      expect(
        el.shadowRoot!.querySelector('[data-testid="note-target-hint"]')!
          .textContent
      ).to.contain('Loading live sessions');
      const options = [...target(el).querySelectorAll('sl-option')].map(
        (option) => option.getAttribute('value')
      );
      expect(options).to.deep.equal(['next']);

      release([live('s-1', 'fixer'), live('s-2', 'reviewer')]);
      await waitUntil(
        () => target(el).querySelectorAll('sl-option').length === 3
      );
      // Two live: still the operator's pick, never current-or-next.
      expect(composer.agentId).to.equal('');
    });

    it('says the live sessions failed to load and keeps Next session', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/session-targets'))
          return jsonResponse({ detail: 'boom' }, 500);
        if (url.includes('/operator-notes'))
          return jsonResponse({ notes: [], total: 0 });
        return jsonResponse({ items: [] });
      });
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await waitUntil(() =>
        el.shadowRoot!.querySelector('[data-testid="note-targets-retry"]')
      );
      const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
      expect(composer.agentId).to.equal('');
      expect(
        el.shadowRoot!.querySelector('[data-testid="note-target-hint"]')!
          .textContent
      ).to.contain('Could not load the live sessions');
      expect(
        [...target(el).querySelectorAll('sl-option')].map((o) =>
          o.getAttribute('value')
        )
      ).to.deep.equal(['next']);

      target(el).value = 'next';
      target(el).dispatchEvent(new CustomEvent('sl-change'));
      await el.updateComplete;
      expect(composer.agentId).to.equal('agent-1');
      expect(composer.nextSession).to.equal(true);
    });

    it('reads live sessions when the host supplied the notes page', async () => {
      stubSessions([live('s-1', 'fixer')], []);
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab
          agent-id="agent-1"
          can-send
          .initial=${{ notes: [note(1)], total: 1 }}
        ></agent-notes-tab>`
      );
      await settle(el);
      expect(target(el).value).to.equal('s-1');
      // The notes page was reused, not read again.
      expect(noteCalls(fetchStub)).to.have.length(0);
    });

    it('reports the agent-wide count after a send while filtered', async () => {
      stubSessions(
        [live('s-1', 'fixer'), live('s-2', 'reviewer')],
        [
          { ...note(1), runtime_session_id: 's-1' },
          { ...note(2), runtime_session_id: 's-2' },
          { ...note(3), runtime_session_id: 's-2' },
        ]
      );
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await settle(el);
      await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 3);
      const filter = el.shadowRoot!.querySelector(
        '[data-testid="notes-session-filter"]'
      ) as HTMLSelectElement;
      filter.value = 's-1';
      filter.dispatchEvent(new CustomEvent('sl-change'));
      await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 1);

      const counts: number[] = [];
      el.addEventListener('notes-count-changed', (event) =>
        counts.push((event as CustomEvent<{ total: number }>).detail.total)
      );
      const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
      for (const sessionId of ['s-1', 's-2']) {
        composer.dispatchEvent(
          new CustomEvent('operator-note-sent', {
            detail: {
              ...note(9),
              note_id: `sent-${sessionId}`,
              runtime_session_id: sessionId,
            },
            bubbles: true,
            composed: true,
          })
        );
      }
      expect(counts).to.deep.equal([4, 5]);
      await el.updateComplete;
      // Only the note on the listed session joins the list.
      expect(el.shadowRoot!.querySelectorAll('li').length).to.equal(2);
    });

    it('makes the operator choose between several live sessions', async () => {
      stubSessions([live('s-1', 'fixer'), live('s-2', 'reviewer')], []);
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await settle(el);
      expect(target(el).value).to.equal('');
      expect(
        el.shadowRoot!.querySelector('[data-testid="note-target-hint"]')!
          .textContent
      ).to.contain('2 sessions are live');
      const options = [...target(el).querySelectorAll('sl-option')].map(
        (option) => option.textContent!.trim()
      );
      expect(options[0]).to.contain('fixer · claude code · started');
      expect(options).to.include('Next session');
      // No session picked: the box cannot send yet.
      const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
      expect(composer.agentId).to.equal('');
    });

    it('offers the next session as a target of its own', async () => {
      stubSessions([live('s-1', 'fixer')], []);
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await settle(el);
      target(el).value = 'next';
      target(el).dispatchEvent(new CustomEvent('sl-change'));
      await el.updateComplete;
      const composer = el.shadowRoot!.querySelector('operator-note-composer')!;
      expect(composer.nextSession).to.equal(true);
      expect(composer.sessionId).to.equal('');
      expect(composer.agentId).to.equal('agent-1');
    });

    it('names and links the session each note went to', async () => {
      stubSessions(
        [live('s-1', 'fixer')],
        [
          { ...note(1), runtime_session_id: 's-1', delivered_turn_index: 3 },
          {
            ...note(2),
            state: 'pending',
            delivered_at: null,
            runtime_session_id: null,
            next_session: true,
          },
          {
            ...note(3),
            runtime_session_id: 'abcdef12-0000-0000-0000-000000000000',
          },
        ]
      );
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await settle(el);
      const sessions = [
        ...el.shadowRoot!.querySelectorAll('[data-testid="note-session"]'),
      ];
      expect(sessions.map((node) => node.textContent!.trim())).to.deep.equal([
        'fixer',
        'Next session',
        'Session abcdef12',
      ]);
      expect(sessions[0].getAttribute('href')).to.equal(
        '/console/runtime-sessions?sessionId=s-1&turn=note-1'
      );
      expect(sessions[1].tagName).to.equal('SPAN');
      (el.shadowRoot!.querySelector('button.row') as HTMLElement).click();
      await el.updateComplete;
      expect(
        el.shadowRoot!.querySelector('[data-testid="agent-note-full"]')!
          .textContent
      ).to.contain('at turn 3');
    });

    it('narrows the list to one session', async () => {
      stubSessions(
        [live('s-1', 'fixer'), live('s-2', 'reviewer')],
        [
          { ...note(1), runtime_session_id: 's-1' },
          { ...note(2), runtime_session_id: 's-2' },
        ]
      );
      const el = await fixture<AgentNotesTab>(
        html`<agent-notes-tab agent-id="agent-1" can-send></agent-notes-tab>`
      );
      await settle(el);
      await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 2);
      const filter = el.shadowRoot!.querySelector(
        '[data-testid="notes-session-filter"]'
      ) as HTMLSelectElement;
      filter.value = 's-2';
      filter.dispatchEvent(new CustomEvent('sl-change'));
      await waitUntil(() => el.shadowRoot!.querySelectorAll('li').length === 1);
      expect(String(noteCalls(fetchStub).at(-1))).to.contain(
        'runtime_session_id=s-2'
      );
    });
  });
});
