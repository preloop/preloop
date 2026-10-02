import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateUserProfileCache } from '../api';
import {
  unifiedWebSocketManager,
  ConnectionState,
} from '../services/unified-websocket-manager';
import type { ApprovalRequest } from '../types';
import './session-chat-view';
import type { SessionChatView } from './session-chat-view';
import './session-live-activity';
import type { SessionLiveActivity } from './session-live-activity';

export function syntheticApproval(
  id = 'approval-example',
  session = 'session-a'
): ApprovalRequest {
  return {
    id,
    account_id: 'account-example',
    tool_configuration_id: 'tool-example',
    approval_workflow_id: 'workflow-example',
    execution_id: null,
    runtime_session_id: session,
    tool_name: 'terminal',
    summary: 'Run a synthetic workspace command',
    tool_args: { command: 'pwd' },
    agent_reasoning: 'Inspect the example workspace',
    status: 'pending',
    requested_at: '2026-10-02T10:00:00Z',
    resolved_at: null,
    expires_at: null,
    approver_comment: null,
  };
}
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('shared session approval controller', () => {
  let fetchStub: sinon.SinonStub;
  let callbacks: Map<string, (message: any) => void>;
  let stateCallback: (state: ConnectionState) => void;
  let rows: ApprovalRequest[];
  let permissions: string[] | null;
  beforeEach(() => {
    rows = [syntheticApproval()];
    permissions = null;
    callbacks = new Map();
    invalidateUserProfileCache();
    localStorage.setItem('accessToken', 'synthetic-token');
    sinon
      .stub(unifiedWebSocketManager, 'getState')
      .returns(ConnectionState.CONNECTED);
    sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake((topic, callback) => {
        callbacks.set(topic, callback);
        return () => {
          if (callbacks.get(topic) === callback) callbacks.delete(topic);
        };
      });
    sinon
      .stub(unifiedWebSocketManager, 'onStateChange')
      .callsFake((callback) => {
        stateCallback = callback;
        return () => undefined;
      });
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      if (url.includes('/users/me'))
        return json({
          id: 'user-example',
          email: 'jane@example.com',
          permissions,
        });
      if (url.includes('/approval-requests?')) {
        const query = new URL(url, location.origin).searchParams;
        return json(
          rows
            .filter(
              (r) =>
                r.runtime_session_id === query.get('runtime_session_id') &&
                (!query.get('status') || r.status === query.get('status'))
            )
            .slice(
              Number(query.get('skip') || 0),
              Number(query.get('skip') || 0) + 100
            )
        );
      }
      if (url.includes('/approval-requests/'))
        return json(rows.find((r) => url.endsWith(r.id)));
      return json({});
    });
  });
  afterEach(() => {
    sinon.restore();
    invalidateUserProfileCache();
    localStorage.removeItem('accessToken');
  });
  async function chat() {
    const element = await fixture<SessionChatView>(
      html`<session-chat-view .sessionId=${'session-a'}></session-chat-view>`
    );
    await waitUntil(() =>
      Boolean(element.shadowRoot?.querySelector('session-approval-card'))
    );
    return element;
  }
  it('renders a pending-only session with one persistent controller and bounded reads', async () => {
    const element = await chat();
    const controller = element.shadowRoot!.querySelector(
      'session-live-activity'
    );
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(
      fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/approval-requests?'))
    ).to.have.length(2);
    expect(
      element.shadowRoot!.querySelectorAll('session-approval-card')
    ).to.have.length(1);
    element.events = [
      {
        id: 'event-example',
        execution_id: '',
        timestamp: '2026-10-02T10:01:00Z',
        type: 'model_gateway_call',
        payload: {
          tools: [
            {
              kind: 'call',
              call_id: 'tool-example',
              name: 'terminal',
              text: '{"command":"pwd"}',
            },
          ],
        },
      },
    ];
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('session-live-activity')).to.equal(
      controller
    );
    expect(
      fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/approval-requests?'))
    ).to.have.length(2);
  });
  it('loads pending requests beyond the first historical page within the selected session', async () => {
    rows = Array.from({ length: 105 }, (_, i) => ({
      ...syntheticApproval(`approval-${i}`),
      status: i < 100 ? ('approved' as const) : ('pending' as const),
    }));
    rows.push(syntheticApproval('other', 'session-b'));
    const element = await chat();
    expect(
      element.shadowRoot!.querySelectorAll('session-approval-card')
    ).to.have.length(105);
    expect(element.shadowRoot!.textContent).not.to.include('other');
    for (const call of fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('/approval-requests?')))
      expect(String(call.args[0])).to.include('runtime_session_id=session-a');
  });
  it('refreshes an approval event without a gateway request and keeps terminal history', async () => {
    rows = [];
    const element = await fixture<SessionChatView>(
      html`<session-chat-view .sessionId=${'session-a'}></session-chat-view>`
    );
    await waitUntil(() => callbacks.has('approvals'));
    rows = [syntheticApproval()];
    callbacks.get('approvals')!({
      type: 'approval_created',
      approval_request_id: rows[0].id,
      runtime_session_id: 'session-a',
    });
    await waitUntil(() =>
      Boolean(element.shadowRoot!.querySelector('session-approval-card'))
    );
    rows[0] = {
      ...rows[0],
      status: 'approved',
      resolved_at: '2026-10-02T10:02:00Z',
      decided_by_human: true,
    };
    callbacks.get('approvals')!({
      type: 'approval_approved',
      approval_request_id: rows[0].id,
      runtime_session_id: 'session-a',
    });
    await waitUntil(() =>
      element
        .shadowRoot!.querySelector('session-approval-card')!
        .shadowRoot!.textContent!.includes('Approved')
    );
    expect(
      element.shadowRoot!.querySelector('session-live-activity')!.shadowRoot!
        .textContent
    ).not.to.include('Waiting for approval');
  });
  it('ignores another session and does not regress a resolved request with stale pending detail', async () => {
    const element = await chat();
    const controller = element.shadowRoot!.querySelector<SessionLiveActivity>(
      'session-live-activity'
    )!;
    controller.updateRequest({ ...syntheticApproval(), status: 'approved' });
    controller.updateRequest(syntheticApproval());
    controller.updateRequest(syntheticApproval('other', 'session-b'));
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelectorAll('session-approval-card')
    ).to.have.length(1);
    expect(
      element.shadowRoot!.querySelector('session-approval-card')!.shadowRoot!
        .textContent
    ).to.include('Approved');
  });
  it('keeps viewers read-only and avoids approval subscription for users without visibility', async () => {
    permissions = ['view_approvals'];
    const element = await chat();
    expect(
      element
        .shadowRoot!.querySelector('session-approval-card')!
        .shadowRoot!.querySelector('.actions')
    ).to.equal(null);
    element.remove();
    invalidateUserProfileCache();
    permissions = [];
    fetchStub.resetHistory();
    const hidden = await fixture<SessionLiveActivity>(
      html`<session-live-activity
        .sessionId=${'session-a'}
      ></session-live-activity>`
    );
    await hidden.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(callbacks.has('approvals')).to.equal(false);
    expect(
      fetchStub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/approval-requests'))
    ).to.have.length(0);
  });
  it('reconciles reconnect and suspends observed processing after transport loss', async () => {
    rows = [];
    const element = await fixture<SessionLiveActivity>(
      html`<session-live-activity
        .sessionId=${'session-a'}
      ></session-live-activity>`
    );
    await waitUntil(() => callbacks.has('gateway_activity'));
    callbacks.get('gateway_activity')!({
      type: 'model_gateway_request_started',
      payload: {
        runtime_session_id: 'session-a',
        request_id: 'request-example',
        timestamp: new Date().toISOString(),
        model_alias: 'example-model',
      },
    });
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.include('Model processing');
    stateCallback(ConnectionState.DISCONNECTED);
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.include('Activity unavailable');
    expect(element.shadowRoot!.textContent).to.include('Reconnecting');
    let reconciled = false;
    element.addEventListener(
      'session-live-reconcile',
      () => (reconciled = true)
    );
    stateCallback(ConnectionState.CONNECTED);
    await element.updateComplete;
    expect(reconciled).to.equal(true);
  });
  it('rejects late session A history after switching to session B', async () => {
    let finishA!: (response: Response) => void;
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      if (url.includes('/users/me'))
        return json({ id: 'user-example', permissions: null });
      if (url.includes('/approval-requests?')) {
        const params = new URL(url, location.origin).searchParams;
        if (params.get('runtime_session_id') === 'session-a')
          return new Promise((resolve) => (finishA = resolve));
        return json([syntheticApproval('approval-b', 'session-b')]);
      }
      return json({});
    });
    const element = await fixture<SessionChatView>(
      html`<session-chat-view .sessionId=${'session-a'}></session-chat-view>`
    );
    await waitUntil(() => Boolean(finishA));
    element.sessionId = 'session-b';
    await element.updateComplete;
    await waitUntil(() =>
      Boolean(
        element.shadowRoot!.querySelector('[data-approval-id="approval-b"]')
      )
    );
    finishA(json([syntheticApproval('approval-a', 'session-a')]));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(
      element.shadowRoot!.querySelector('[data-approval-id="approval-a"]')
    ).to.equal(null);
    expect(
      element.shadowRoot!.querySelectorAll('session-approval-card')
    ).to.have.length(1);
  });
  it('adds approvals while scrolled up without resuming follow-live', async () => {
    const events = Array.from({ length: 20 }, (_, i) => ({
      id: `event-${i}`,
      execution_id: 'execution-example',
      timestamp: `2026-10-02T10:${String(i).padStart(2, '0')}:00Z`,
      type: 'model_gateway_call',
      payload: {
        conversation_preview: {
          messages: [
            { role: 'user', source: 'request', text: `Example prompt ${i}` },
            {
              role: 'assistant',
              source: 'response',
              text: `Example reply ${i}`,
            },
          ],
        },
      },
    }));
    const element = await fixture<SessionChatView>(
      html`<session-chat-view
        style="height:250px"
        scrollable
        followLive
        .events=${events}
        .sessionId=${'session-a'}
      ></session-chat-view>`
    );
    await waitUntil(() =>
      Boolean(element.shadowRoot!.querySelector('session-approval-card'))
    );
    const thread = element.shadowRoot!.querySelector<HTMLElement>('.thread')!;
    thread.dispatchEvent(new WheelEvent('wheel', { bubbles: true }));
    thread.scrollTop = 0;
    thread.dispatchEvent(new Event('scroll'));
    const before = thread.scrollTop;
    element
      .shadowRoot!.querySelector<SessionLiveActivity>('session-live-activity')!
      .updateRequest({
        ...syntheticApproval('new-example'),
        requested_at: '2026-10-02T11:00:00Z',
      });
    await element.updateComplete;
    await new Promise((resolve) => requestAnimationFrame(resolve));
    expect(thread.scrollTop).to.equal(before);
    expect(
      element.shadowRoot!.querySelector('[data-testid="jump-latest"]')
    ).not.to.equal(null);
  });
  it('ages unmatched native running evidence to unavailable without inventing completion', async () => {
    rows = [];
    const element = await fixture<SessionLiveActivity>(
      html`<session-live-activity
        .sessionId=${'session-a'}
        .activity=${[
          {
            activity_type: 'tool_call',
            timestamp: new Date(Date.now() - 180000).toISOString(),
            title: 'terminal',
            summary: 'pwd',
            status: 'running',
            tool_name: 'terminal',
          },
        ]}
      ></session-live-activity>`
    );
    await waitUntil(() => fetchStub.callCount >= 3);
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.include('Activity unavailable');
    expect(element.shadowRoot!.textContent).not.to.include('Running terminal');
  });
  it('renders one card for a provably identical gateway/native call and removes its legacy step', async () => {
    const event = {
      id: 'gateway-example',
      execution_id: 'execution-example',
      timestamp: '2026-10-02T10:01:00Z',
      type: 'model_gateway_call',
      payload: {
        tools: [
          {
            kind: 'call',
            call_id: 'call-example',
            name: 'terminal',
            text: '{"command":"pwd"}',
          },
          { kind: 'result', call_id: 'call-example', text: 'Example result' },
        ],
        conversation_preview: {
          messages: [
            {
              role: 'tool',
              source: 'request',
              text: 'Example result',
              tool_call_ids: ['call-example'],
            },
          ],
        },
      },
    };
    const native = {
      activity_type: 'tool_call',
      activity_id: 'activity-example',
      timestamp: '2026-10-02T10:00:00Z',
      title: 'terminal',
      summary: 'pwd',
      status: 'running',
      tool_name: 'terminal',
      metadata: { tool_call_id: 'call-example' },
    } as import('../types').RuntimeSessionActivityItem;
    const element = await fixture<SessionChatView>(
      html`<session-chat-view
        .events=${[event]}
        .activity=${[native]}
      ></session-chat-view>`
    );
    expect(
      element.shadowRoot!.querySelectorAll('session-tool-card')
    ).to.have.length(1);
    expect(element.shadowRoot!.querySelector('.steps')).to.equal(null);
    const card = element.shadowRoot!.querySelector('session-tool-card')!;
    expect(card.shadowRoot!.textContent).to.include('completed');
    expect(element.shadowRoot!.textContent).not.to.include(
      'Message structure was unavailable'
    );
  });
  it('refetches approval-only state after a reconnect races an older pending snapshot', async () => {
    let releasePending!: (response: Response) => void;
    let pendingReads = 0;
    rows = [];
    fetchStub.callsFake(async (input) => {
      const url = String(input);
      if (url.includes('/users/me'))
        return json({ id: 'user-example', permissions: null });
      if (url.includes('/approval-requests?')) {
        const params = new URL(url, location.origin).searchParams;
        if (params.get('status') === 'pending' && ++pendingReads === 1)
          return new Promise((resolve) => (releasePending = resolve));
        return json(rows);
      }
      return json({});
    });
    const element = await fixture<SessionChatView>(
      html`<session-chat-view .sessionId=${'session-a'}></session-chat-view>`
    );
    await waitUntil(() => Boolean(releasePending));
    stateCallback(ConnectionState.DISCONNECTED);
    rows = [syntheticApproval('during-disconnect')];
    stateCallback(ConnectionState.CONNECTED);
    releasePending(json([]));
    await waitUntil(() =>
      Boolean(
        element.shadowRoot!.querySelector(
          '[data-approval-id="during-disconnect"]'
        )
      )
    );
    expect(
      fetchStub
        .getCalls()
        .filter((call) => String(call.args[0]).includes('/approval-requests?'))
    ).to.have.length(4);
  });
  it('retains schema form input across repeated pending snapshots and view switches', async () => {
    rows = [
      {
        ...syntheticApproval(),
        question_schema: {
          type: 'object',
          properties: { note: { type: 'string' } },
          required: ['note'],
        },
      },
    ];
    const element = await chat();
    const card = element.shadowRoot!.querySelector('session-approval-card')!;
    const form =
      card.shadowRoot!.querySelector<import('./answer-form').AnswerForm>(
        'answer-form'
      )!;
    await form.updateComplete;
    const input = form.shadowRoot!.querySelector('sl-input') as HTMLElement & {
      value: string;
    };
    input.value = 'Example retained input';
    input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await form.updateComplete;
    element
      .shadowRoot!.querySelector<SessionLiveActivity>('session-live-activity')!
      .updateRequest(JSON.parse(JSON.stringify(rows[0])));
    await element.updateComplete;
    await form.updateComplete;
    expect(form.answer.note).to.equal('Example retained input');
    element.liveEnabled = false;
    await element.updateComplete;
    element.liveEnabled = true;
    await element.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(card.shadowRoot!.querySelector('answer-form')).to.equal(form);
    expect(form.answer.note).to.equal('Example retained input');
  });
});
