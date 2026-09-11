import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './operator-note-composer';
import type { OperatorNoteComposer } from './operator-note-composer';
import { OPERATOR_NOTE_SENT_EVENT } from './operator-note-composer';

/** Shoelace fetches its icon SVGs, so only note calls count as traffic. */
function noteCalls(fetchStub: sinon.SinonStub): sinon.SinonSpyCall[] {
  return fetchStub
    .getCalls()
    .filter((call) => String(call.args[0]).includes('/operator-notes'));
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function note(overrides: Record<string, unknown> = {}) {
  return {
    note_id: 'note-1',
    state: 'pending',
    text: 'Stop after the current test.',
    author: { display: 'Ada Lovelace', auth_method: 'jwt' },
    ...overrides,
  };
}

function input(el: OperatorNoteComposer): HTMLElement & { value: string } {
  return el.shadowRoot!.querySelector('[data-testid="note-input"]') as never;
}

function sendButton(el: OperatorNoteComposer): HTMLElement & {
  disabled: boolean;
  click: () => void;
} {
  return el.shadowRoot!.querySelector('[data-testid="note-send"]') as never;
}

async function mount(
  attrs: { agentId?: string; executionId?: string } = {}
): Promise<OperatorNoteComposer> {
  const el = await fixture<OperatorNoteComposer>(
    html`<operator-note-composer
      agent-id=${attrs.agentId ?? 'agent-1'}
      execution-id=${attrs.executionId ?? ''}
    ></operator-note-composer>`
  );
  await el.updateComplete;
  return el;
}

describe('operator-note-composer', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.resolves(jsonResponse({ notes: [] }));
  });

  afterEach(() => {
    sinon.restore();
  });

  it('renders a box and lists what was already sent', async () => {
    fetchStub.resolves(
      jsonResponse({
        notes: [note({ note_id: 'note-9', text: 'Rebase first.' })],
      })
    );

    const el = await mount();
    await waitUntil(() =>
      Boolean(el.shadowRoot!.querySelector('[data-testid="note-list"]'))
    );

    expect(input(el)).to.exist;
    expect(el.shadowRoot!.textContent).to.contain('Rebase first.');
    expect(el.shadowRoot!.textContent).to.contain('Waiting for the next turn');
  });

  it('sends the note to the agent it was given', async () => {
    const el = await mount();
    fetchStub.resolves(jsonResponse(note()));

    input(el).value = 'Stop after the current test.';
    input(el).dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;
    sendButton(el).click();
    await waitUntil(() =>
      noteCalls(fetchStub).some((call) => call.args[1]?.method === 'POST')
    );

    const post = noteCalls(fetchStub).find(
      (call) => call.args[1]?.method === 'POST'
    )!;
    expect(String(post.args[0])).to.equal('/api/v1/operator-notes');
    expect(JSON.parse(String(post.args[1]?.body))).to.deep.equal({
      text: 'Stop after the current test.',
      agent_id: 'agent-1',
    });
  });

  it('sends on Enter and keeps the box for the next line on Shift+Enter', async () => {
    const el = await mount();
    fetchStub.resolves(jsonResponse(note()));

    input(el).value = 'Ship it.';
    input(el).dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;

    input(el).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true })
    );
    await el.updateComplete;
    expect(noteCalls(fetchStub).some((call) => call.args[1]?.method === 'POST'))
      .to.be.false;

    input(el).dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await waitUntil(() =>
      noteCalls(fetchStub).some((call) => call.args[1]?.method === 'POST')
    );
  });

  it('shows the delivery state, which is the whole point of the list', async () => {
    const el = await mount();
    await waitUntil(() => noteCalls(fetchStub).length > 0);
    fetchStub.resolves(
      jsonResponse(
        note({
          note_id: 'note-2',
          state: 'delivered',
          delivery_channel: 'gateway',
        })
      )
    );

    input(el).value = 'Use the smaller diff.';
    input(el).dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;
    sendButton(el).click();
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('Delivered at a turn boundary')
    );

    expect(el.shadowRoot!.querySelector('[data-testid="note-state-note-2"]')).to
      .exist;
  });

  it('reports why a note was refused instead of losing it silently', async () => {
    const el = await mount();
    fetchStub.resolves(
      jsonResponse({ detail: 'Rate limit reached: 20 notes per hour' }, 429)
    );

    input(el).value = 'again';
    input(el).dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;
    sendButton(el).click();
    await waitUntil(() =>
      Boolean(el.shadowRoot!.querySelector('[data-testid="note-error"]'))
    );

    expect(
      el.shadowRoot!.querySelector('[data-testid="note-error"]')!.textContent
    ).to.contain('Rate limit reached');
  });

  it('announces a sent note so the page can refresh its timeline', async () => {
    const el = await mount();
    fetchStub.resolves(jsonResponse(note()));
    let detail: unknown = null;
    el.addEventListener(OPERATOR_NOTE_SENT_EVENT, (event) => {
      detail = (event as CustomEvent).detail;
    });

    input(el).value = 'Pause the deploy.';
    input(el).dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;
    sendButton(el).click();
    await waitUntil(() => detail !== null);

    expect((detail as { note_id: string }).note_id).to.equal('note-1');
  });

  it('addresses an execution when it is given one', async () => {
    const el = await mount({ agentId: '', executionId: 'exec-7' });
    fetchStub.resolves(jsonResponse(note()));

    input(el).value = 'Stop at the next commit.';
    input(el).dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;
    sendButton(el).click();
    await waitUntil(() =>
      noteCalls(fetchStub).some((call) => call.args[1]?.method === 'POST')
    );

    const post = noteCalls(fetchStub).find(
      (call) => call.args[1]?.method === 'POST'
    )!;
    expect(JSON.parse(String(post.args[1]?.body))).to.deep.equal({
      text: 'Stop at the next commit.',
      execution_id: 'exec-7',
    });
  });
});
