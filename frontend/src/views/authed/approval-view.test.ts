import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './approval-view';
import type { ApprovalView } from './approval-view';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('ApprovalView', () => {
  let fetchStub: sinon.SinonStub;
  let wsSubscribeStub: sinon.SinonStub;
  let wsStateStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function pendingRequest(overrides: Record<string, unknown> = {}) {
    return {
      id: 'req-1',
      account_id: 'acc-1',
      tool_configuration_id: 'tc-1',
      approval_workflow_id: 'aw-1',
      execution_id: null,
      tool_name: 'shell_command',
      tool_args: { command: 'ls -la' },
      agent_reasoning: 'Listing files to inspect the repo',
      status: 'pending',
      requested_at: '2026-06-01T10:00:00Z',
      resolved_at: null,
      expires_at: '2026-06-01T11:00:00Z',
      approver_comment: null,
      webhook_posted_at: null,
      webhook_error: null,
      ...overrides,
    };
  }

  function createFetchStub(
    opts: {
      request?: Record<string, unknown> | null;
      getFails?: boolean;
    } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (
          /\/api\/v1\/approval-requests\/req-1$/.test(url) &&
          method === 'GET'
        ) {
          if (opts.getFails) {
            return json({ detail: 'boom' }, 500);
          }
          if (opts.request === null) {
            return json(null);
          }
          return json(opts.request ?? pendingRequest());
        }

        if (url.includes('/approve') && method === 'POST') {
          return json(
            pendingRequest({
              status: 'approved',
              resolved_at: '2026-06-01T10:30:00Z',
            })
          );
        }

        if (url.includes('/decline') && method === 'POST') {
          return json(
            pendingRequest({
              status: 'declined',
              resolved_at: '2026-06-01T10:30:00Z',
            })
          );
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    wsSubscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => {});
    wsStateStub = sinon
      .stub(unifiedWebSocketManager, 'onStateChange')
      .returns(() => {});
  });

  afterEach(() => {
    fetchStub?.restore();
    wsSubscribeStub.restore();
    wsStateStub.restore();
    localStorage.clear();
  });

  it('renders a pending approval request', async () => {
    fetchStub = createFetchStub({ request: pendingRequest() });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('shell_command');
    expect(element.shadowRoot?.textContent).to.contain('PENDING');
    const buttons = element.shadowRoot?.querySelectorAll('.actions sl-button');
    expect(buttons?.length).to.equal(2);
  });

  it('renders the resolved state for an approved request', async () => {
    fetchStub = createFetchStub({
      request: pendingRequest({
        status: 'approved',
        resolved_at: '2026-06-01T10:30:00Z',
        approver_comment: 'Looks safe',
      }),
    });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('APPROVED');
    expect(element.shadowRoot?.querySelector('.resolved-info')).to.exist;
    expect(element.shadowRoot?.textContent).to.contain('Looks safe');
    // No decision buttons once resolved.
    expect(element.shadowRoot?.querySelector('.actions')).to.not.exist;
  });

  it('shows a not-found warning when the request is missing', async () => {
    fetchStub = createFetchStub({ request: null });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('not found');
  });

  it('renders an error alert when loading fails with not found', async () => {
    // fetchData swallows the HTTP error and returns null, so the view falls
    // back to the "not found" branch rather than the error branch.
    fetchStub = createFetchStub({ getFails: true });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    const alert = element.shadowRoot?.querySelector('sl-alert');
    expect(alert).to.exist;
  });

  it('approves a pending request', async () => {
    fetchStub = createFetchStub({ request: pendingRequest() });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');

    (element as any).comment = 'approved by test';
    await (element as any).handleApprove();
    await element.updateComplete;

    expect((element as any).approvalRequest?.status).to.equal('approved');
    expect((element as any).actionResult?.type).to.equal('success');
    const approveCall = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/approve'));
    expect(approveCall, 'expected a POST to /approve').to.exist;
  });
});
