import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import {
  sendAgentControlVoiceTranscript,
  sendAgentControlCommand,
  sendAgentControlTakeover,
  sendAgentControlRelease,
  startRunnerSession,
} from './api';

describe('Agent Control API', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.removeItem('accessToken');
  });

  it('routes voice transcripts to the voice transcript endpoint', async () => {
    fetchStub.resolves(
      new Response(
        JSON.stringify({
          command_id: 'cmd-1',
          managed_agent_id: 'agent-1',
          session_mode: 'new',
          published: true,
        }),
        {
          status: 202,
          headers: { 'Content-Type': 'application/json' },
        }
      )
    );

    const response = await sendAgentControlVoiceTranscript('agent-1', {
      transcript: 'Summarize your current status',
      start_new_session: true,
      metadata: { requested_from: 'test' },
      voice: { locale: 'en-US' },
    });

    expect(response.command_id).to.equal('cmd-1');
    expect(fetchStub.calledOnce).to.equal(true);
    const [url, init] = fetchStub.firstCall.args as [string, RequestInit];
    expect(url).to.equal('/api/v1/agents/agent-1/control/voice-transcripts');
    expect(init.method).to.equal('POST');
    expect(new Headers(init.headers).get('Authorization')).to.equal(
      'Bearer test-token'
    );
    const sentBody = JSON.parse(init.body as string);
    expect(sentBody.consent_request_id).to.match(/^[0-9a-f-]{36}$/);
    delete sentBody.consent_request_id;
    expect(sentBody).to.deep.equal({
      transcript: 'Summarize your current status',
      start_new_session: true,
      metadata: { requested_from: 'test' },
      voice: { locale: 'en-US' },
    });
  });

  for (const [name, invoke] of [
    [
      'command',
      () =>
        sendAgentControlCommand('agent-1', {
          message: 'A task',
          start_new_session: true,
        }),
    ],
    [
      'voice',
      () =>
        sendAgentControlVoiceTranscript('agent-1', {
          transcript: 'A task',
          start_new_session: true,
        }),
    ],
    [
      'takeover',
      () => sendAgentControlTakeover('agent-1', { start_new_session: true }),
    ],
    ['release', () => sendAgentControlRelease('agent-1')],
    [
      'runner',
      () =>
        startRunnerSession('runner-1', {
          harness: 'copilot_cli',
          workspace: { kind: 'authorized_directory', id: 'dir_example' },
        }),
    ],
  ] as const) {
    it(`waits for owner approval before retrying ${name} starts`, async () => {
      fetchStub.onCall(0).resolves(
        new Response(
          JSON.stringify({
            detail: {
              status: 'pending',
              approval_id: 'approval-1',
              message: 'Waiting for runner owner',
            },
          }),
          { status: 409 }
        )
      );
      fetchStub
        .onCall(1)
        .resolves(new Response(JSON.stringify({ status: 'approved' })));
      fetchStub
        .onCall(2)
        .resolves(
          new Response(
            JSON.stringify({ command_id: 'cmd-1', session_id: 'session-1' }),
            { status: 202 }
          )
        );
      await invoke();
      expect(fetchStub.callCount).to.equal(3);
      expect(fetchStub.secondCall.args[0]).to.equal(
        '/api/v1/approval-requests/approval-1/consent-status'
      );
      const first = fetchStub.firstCall.args[1] as RequestInit;
      const retried = fetchStub.thirdCall.args[1] as RequestInit;
      expect(retried.body).to.equal(first.body);
      expect(JSON.parse(first.body as string).consent_request_id).to.match(
        /^[0-9a-f-]{36}$/
      );
    });
  }
});
