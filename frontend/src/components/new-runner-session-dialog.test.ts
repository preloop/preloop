import { html, fixture, expect, waitUntil, oneEvent } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import './new-runner-session-dialog';
import type { NewRunnerSessionDialog } from './new-runner-session-dialog';

const OPTIONS = {
  runner_id: 'runner-1',
  online: true,
  sessions_available: true,
  harnesses: [
    {
      harness: 'copilot_cli',
      display_name: 'GitHub Copilot CLI',
      session_mode: 'resume',
      models: [
        { id: 'auto', source: 'static' },
        { id: 'gpt-5.2', source: 'static' },
      ],
      available: true,
    },
  ],
  authorized_directories: [
    { id: 'dir_api', label: 'api', mode: 'write', harnesses: ['copilot_cli'] },
    {
      id: 'dir_x',
      label: 'cursor only',
      mode: 'write',
      harnesses: ['cursor_cli'],
    },
  ],
  checkout_sources: [
    {
      tracker_id: 'trk-1',
      tracker_name: 'GitHub',
      provider: 'github',
      repositories: [{ full_name: 'acme/api', default_branch: 'main' }],
    },
  ],
  limits: { max_concurrent: 2, active: 0, idle_timeout_seconds: 1800 },
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('new-runner-session-dialog', () => {
  let fetchStub: sinon.SinonStub;

  function stubFetch(
    options: unknown = OPTIONS,
    start: { status: number; body: unknown } = {
      status: 202,
      body: {
        session_id: 'rs-1',
        remote_session_id: 'rem-1',
        state: 'requested',
      },
    }
  ) {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/session-options')) return json(options);
        if (url.endsWith('/sessions') && method === 'POST') {
          return json(start.body, start.status);
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  const startBodies = () =>
    fetchStub
      .getCalls()
      .filter((call) => call.args[1]?.method === 'POST')
      .map((call) => JSON.parse(String(call.args[1].body)));

  async function mount(): Promise<NewRunnerSessionDialog> {
    const element = await fixture<NewRunnerSessionDialog>(
      html`<new-runner-session-dialog
        runner-id="runner-1"
        open
      ></new-runner-session-dialog>`
    );
    await waitUntil(() => (element as any).options !== null, 'options load');
    await element.updateComplete;
    return element;
  }

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('preselects the first harness, model and an authorized directory', async () => {
    stubFetch();
    const element = await mount();
    const root = element.shadowRoot!;
    expect(
      root.querySelector('sl-select.harness')!.getAttribute('value')
    ).to.equal('copilot_cli');
    expect(
      root.querySelector('sl-select.model')!.getAttribute('value')
    ).to.equal('auto');
    const dirs = [
      ...root.querySelectorAll('sl-select.directory sl-option'),
    ].map((o) => o.getAttribute('value'));
    expect(dirs).to.deep.equal(['dir_api']);
    expect(root.querySelector('.limits')!.textContent).to.contain('0 of 2');
    expect(element.canSubmit).to.equal(true);
  });

  it('starts the session and fires runner-session-started', async () => {
    stubFetch();
    const element = await mount();
    (element as any).prompt = 'Summarize the open TODOs';
    setTimeout(() => void element.submit());
    const event = (await oneEvent(
      element,
      'runner-session-started'
    )) as CustomEvent;
    expect(event.detail.session_id).to.equal('rs-1');
    expect(startBodies()).to.deep.equal([
      {
        harness: 'copilot_cli',
        model: 'auto',
        workspace: { kind: 'authorized_directory', id: 'dir_api' },
        first_prompt: 'Summarize the open TODOs',
      },
    ]);
    expect(element.open).to.equal(false);
  });

  it('sends a tracker checkout workspace', async () => {
    stubFetch();
    const element = await mount();
    Object.assign(element as any, {
      workspaceKind: 'tracker_checkout',
      trackerId: 'trk-1',
      repository: 'acme/api',
      ref: ' feature/x ',
    });
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('sl-select.repository')).to.exist;
    await element.submit();
    expect(startBodies()[0].workspace).to.deep.equal({
      kind: 'tracker_checkout',
      tracker_id: 'trk-1',
      repository: 'acme/api',
      ref: 'feature/x',
    });
  });

  it('renders refusal codes as sentences and stays open', async () => {
    stubFetch(OPTIONS, {
      status: 409,
      body: {
        detail: { code: 'max_concurrent_reached', message: 'raw server text' },
      },
    });
    const element = await mount();
    await element.submit();
    await element.updateComplete;
    const alert = element.shadowRoot!.querySelector('sl-alert.error')!;
    expect(alert.textContent).to.contain('maximum number of remote sessions');
    expect(element.open).to.equal(true);
  });

  it('falls back to the server message for unknown codes', async () => {
    stubFetch(OPTIONS, {
      status: 409,
      body: { detail: { code: 'something_new', message: 'Server says no.' } },
    });
    const element = await mount();
    await element.submit();
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector('sl-alert.error')!.textContent
    ).to.contain('Server says no.');
  });

  it('blocks start on an offline runner', async () => {
    stubFetch({ ...OPTIONS, online: false });
    const element = await mount();
    expect(element.shadowRoot!.querySelector('sl-alert.offline')).to.exist;
    expect(element.canSubmit).to.equal(false);
  });

  it('explains when the server has no session service yet', async () => {
    stubFetch({ ...OPTIONS, sessions_available: false });
    const element = await mount();
    expect(element.shadowRoot!.querySelector('sl-alert.unavailable')).to.exist;
    expect(element.canSubmit).to.equal(false);
  });

  it('marks harnesses that are not session ready as disabled', async () => {
    stubFetch({
      ...OPTIONS,
      harnesses: [
        {
          ...OPTIONS.harnesses[0],
          available: false,
          unavailable_reason: 'harness_signed_out',
        },
      ],
    });
    const element = await mount();
    const option = element.shadowRoot!.querySelector(
      'sl-select.harness sl-option'
    )!;
    expect(option.hasAttribute('disabled')).to.equal(true);
    expect(option.textContent).to.contain('harness signed out');
    expect(element.canSubmit).to.equal(false);
  });

  it('shows the forbidden refusal when options are refused', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .resolves(
        json(
          { detail: { code: 'not_runner_owner', message: 'Not allowed.' } },
          403
        )
      );
    const element = await fixture<NewRunnerSessionDialog>(
      html`<new-runner-session-dialog
        runner-id="runner-1"
        open
      ></new-runner-session-dialog>`
    );
    await waitUntil(() => (element as any).error !== '', 'error shown');
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector('sl-alert.error')!.textContent
    ).to.contain('Only the runner owner');
  });
});
