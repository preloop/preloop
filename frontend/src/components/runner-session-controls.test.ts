import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { runnerTurnSessionId } from '../api';
import './runner-session-controls';
import type { RunnerSessionControls } from './runner-session-controls';
import { RUNNER_TURN_SENT_EVENT } from './operator-note-composer';

const SESSION = '0b8f6c1e-2d4a-4f5b-9c3d-1a2b3c4d5e6f';
const RUNNER = '7d9e1f20-3b4c-4d5e-8f60-718293a4b5c6';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function control(overrides: Record<string, unknown> = {}) {
  return {
    runtime_session_id: SESSION,
    mode: 'command',
    reason_code: null,
    reason: null,
    send_path: `/api/v1/runner-sessions/${RUNNER}/turns`,
    managed_agent_id: null,
    agent_name: null,
    agent_kind: 'runner_session',
    ...overrides,
  };
}

describe('runner-session-controls', () => {
  let fetchStub: sinon.SinonStub;
  let controlBody: unknown;
  let turnResponse: () => Response;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    controlBody = control();
    turnResponse = () => json({ turn_id: 'turn-1', state: 'queued' }, 202);
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith('/control')) return json(controlBody);
        if (url.includes('/runner-sessions/') && url.endsWith('/turns'))
          return turnResponse();
        if (url.includes('/operator-notes') && init?.method === 'POST')
          return json({ note_id: 'note-1', state: 'pending', text: 'x' }, 201);
        if (url.includes('/operator-notes')) return json({ notes: [] });
        return json({});
      });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  async function mount(): Promise<RunnerSessionControls> {
    const el = await fixture<RunnerSessionControls>(
      html`<runner-session-controls
        session-id=${SESSION}
      ></runner-session-controls>`
    );
    await waitUntil(
      () =>
        !el
          .shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
          .textContent!.includes('Checking'),
      'control mode loaded'
    );
    return el;
  }

  function composer(el: RunnerSessionControls): any {
    return el.shadowRoot!.querySelector('operator-note-composer');
  }

  async function type(el: RunnerSessionControls, text: string): Promise<any> {
    const box = composer(el);
    await box.updateComplete;
    box.text = text;
    await box.updateComplete;
    return box;
  }

  function posts(fragment: string) {
    return fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]).includes(fragment) &&
          call.args[1]?.method === 'POST'
      );
  }

  it('accepts only the runner turn endpoint as a send path', () => {
    expect(
      runnerTurnSessionId(`/api/v1/runner-sessions/${RUNNER}/turns`)
    ).to.equal(RUNNER);
    for (const bad of [
      `/api/v1/agents/${RUNNER}/control/prompts`,
      `https://evil.example/api/v1/runner-sessions/${RUNNER}/turns`,
      `/api/v1/runner-sessions/${RUNNER}/turns/../stop`,
      '/api/v1/runner-sessions/not-a-uuid/turns',
      null,
    ]) {
      expect(runnerTurnSessionId(bad), String(bad)).to.equal(null);
    }
  });

  it('sends the typed text as a runner turn in command mode', async () => {
    const el = await mount();
    expect(
      el.shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
        .textContent
    ).to.contain('Runner online');
    const sent = sinon.spy();
    el.addEventListener(RUNNER_TURN_SENT_EVENT, sent);
    const box = await type(el, 'Run the tests again');
    box.shadowRoot.querySelector('[data-testid="note-send"]').click();
    await waitUntil(() => sent.called, 'turn sent');

    const calls = posts(`/runner-sessions/${RUNNER}/turns`);
    expect(calls).to.have.length(1);
    expect(JSON.parse(String(calls[0].args[1].body))).to.deep.equal({
      text: 'Run the tests again',
    });
    expect(posts('/operator-notes')).to.have.length(0);
    expect(box.text).to.equal('');
  });

  it('says a turn is in progress and keeps the text on 409', async () => {
    turnResponse = () =>
      json(
        { detail: { code: 'turn_in_progress', message: 'A turn is running.' } },
        409
      );
    const el = await mount();
    const box = await type(el, 'And then deploy');
    box.shadowRoot.querySelector('[data-testid="note-send"]').click();
    await waitUntil(
      () => box.shadowRoot.querySelector('.error'),
      'error shown'
    );
    expect(box.shadowRoot.querySelector('.error').textContent).to.contain(
      'still working on the last message'
    );
    expect(box.text).to.equal('And then deploy');
  });

  it('falls back to a note when the runner is offline', async () => {
    controlBody = control({
      mode: 'note',
      reason_code: 'control_offline',
      send_path: null,
    });
    const el = await mount();
    expect(
      el.shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
        .textContent
    ).to.contain('Runner offline: your message is queued as a note.');
    const box = await type(el, 'Pick this up later');
    box.shadowRoot.querySelector('[data-testid="note-send"]').click();
    await waitUntil(() => posts('/operator-notes').length === 1, 'note sent');
    expect(
      JSON.parse(String(posts('/operator-notes')[0].args[1].body))
    ).to.deep.equal({
      text: 'Pick this up later',
      runtime_session_id: SESSION,
    });
    expect(posts('/turns')).to.have.length(0);
  });

  it('holds the turn for a sleeping laptop instead of an error', async () => {
    controlBody = control({
      reason_code: 'runner_away',
      reason: 'the runner hosting this session is asleep',
    });
    const el = await mount();
    expect(
      el.shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
        .textContent
    ).to.contain('Your laptop is asleep or offline: your message is held');
    const sent = sinon.spy();
    el.addEventListener(RUNNER_TURN_SENT_EVENT, sent);
    const box = await type(el, 'Continue when you wake');
    box.shadowRoot.querySelector('[data-testid="note-send"]').click();
    await waitUntil(() => sent.called, 'turn sent');
    expect(posts(`/runner-sessions/${RUNNER}/turns`)).to.have.length(1);
    expect(posts('/operator-notes')).to.have.length(0);
  });

  it('ignores a command send path outside the runner turn endpoint', async () => {
    controlBody = control({
      send_path: `/api/v1/agents/${RUNNER}/control/prompts`,
    });
    const el = await mount();
    expect(composer(el).runnerSessionId).to.equal(null);
  });

  it('re-reads the mode once per burst of revisions', async () => {
    const el = await mount();
    const hint = () =>
      el.shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
        .textContent!;
    expect(hint()).to.contain('Runner online');
    const controlCalls = () =>
      fetchStub.getCalls().filter((c) => String(c.args[0]).endsWith('/control'))
        .length;
    const before = controlCalls();
    controlBody = control({
      mode: 'note',
      reason_code: 'control_offline',
      send_path: null,
    });
    for (const revision of ['a', 'b', 'c']) {
      el.revision = revision;
      await el.updateComplete;
    }
    await waitUntil(() => hint().includes('Runner offline'), 'offline hint');
    expect(controlCalls() - before).to.equal(1);
    expect(composer(el).runnerSessionId).to.equal(null);
  });

  it('re-reads the mode after a failed command send', async () => {
    turnResponse = () => json({ detail: { code: 'runner_offline' } }, 409);
    const el = await mount();
    const before = fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).endsWith('/control')).length;
    controlBody = control({
      mode: 'note',
      reason_code: 'control_offline',
      send_path: null,
    });
    const box = await type(el, 'Hello');
    box.shadowRoot.querySelector('[data-testid="note-send"]').click();
    await waitUntil(
      () =>
        el
          .shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
          .textContent!.includes('Runner offline'),
      'refreshed after failure'
    );
    expect(
      fetchStub.getCalls().filter((c) => String(c.args[0]).endsWith('/control'))
        .length - before
    ).to.equal(1);
    expect(box.text).to.equal('Hello');
  });

  it('falls back to notes when a refresh fails after a good read', async () => {
    const el = await mount();
    const hint = () =>
      el.shadowRoot!.querySelector('[data-testid="control-mode-hint"]')!
        .textContent!;
    expect(hint()).to.contain('Runner online');
    controlBody = undefined;
    fetchStub
      .withArgs(sinon.match((u: unknown) => String(u).endsWith('/control')))
      .resolves(json({ detail: 'boom' }, 500));
    el.revision = 'changed';
    await waitUntil(
      () => hint().includes('Could not read the session controls'),
      'error line'
    );
    expect(hint()).to.contain('your message is sent as a note');
    const box = await type(el, 'Still there?');
    expect(box.runnerSessionId).to.equal(null);
    box.shadowRoot.querySelector('[data-testid="note-send"]').click();
    await waitUntil(() => posts('/operator-notes').length === 1, 'note sent');
    expect(posts('/turns')).to.have.length(0);
  });

  it('shows the attach command as copyable text', async () => {
    const el = await mount();
    const hint = el.shadowRoot!.querySelector('[data-testid="attach-hint"]')!;
    expect(hint.querySelector('code')!.textContent).to.equal(
      `preloop sessions attach ${SESSION}`
    );
    expect((hint.querySelector('sl-copy-button') as any).value).to.equal(
      `preloop sessions attach ${SESSION}`
    );
  });
});
