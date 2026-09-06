import { aTimeout, html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { setViewport } from '@web/test-runner-commands';

import './approval-view';
import { resetConfirmDialogForTests } from '../../components/confirm-dialog';
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
      // Relative to now: a pending request whose expiry has passed reads as
      // timed out, so a fixed past date would test the wrong branch.
      requested_at: new Date(Date.now() - 60_000).toISOString(),
      resolved_at: null,
      expires_at: new Date(Date.now() + 60 * 60_000).toISOString(),
      approver_comment: null,
      webhook_posted_at: null,
      webhook_error: null,
      ...overrides,
    };
  }

  function questionRequest(overrides: Record<string, unknown> = {}) {
    return pendingRequest({
      tool_name: 'ask_user',
      tool_args: { question: 'Which colour?' },
      summary: 'Which colour?',
      is_question: true,
      question: 'Which colour?',
      question_options: ['blue', 'green'],
      allow_free_text: true,
      ...overrides,
    });
  }

  function createFetchStub(
    opts: {
      request?: Record<string, unknown> | null;
      getFails?: boolean;
      history?: Array<Record<string, unknown>> | null;
      publicData?: Record<string, unknown> | null;
      decideHistory?: Array<Record<string, unknown>>;
      pendingQueue?: Record<string, unknown>[];
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

        if (/\/history$/.test(url) && method === 'GET') {
          if (opts.history === null) {
            return json({ detail: 'none' }, 404);
          }
          return json(
            opts.history ?? [
              {
                id: 'ev-1',
                event_type: 'approval_requested',
                detail: "Approval requested for tool 'shell_command'",
                comment: null,
                actor_id: null,
                actor_email: null,
                timestamp: '2026-06-01T10:00:00Z',
              },
              {
                id: 'ev-2',
                event_type: 'notification_sent',
                detail: 'Notification via email to jane@example.com (sent)',
                comment: null,
                actor_id: null,
                actor_email: null,
                timestamp: '2026-06-01T10:00:01Z',
              },
            ]
          );
        }

        if (
          /\/api\/v1\/approval-requests\?status=pending/.test(url) &&
          method === 'GET'
        ) {
          return json(
            opts.pendingQueue ?? [
              pendingRequest(),
              pendingRequest({
                id: 'req-2',
                expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
              }),
            ]
          );
        }

        if (/\/approval\/req-1\/data/.test(url) && method === 'GET') {
          if (opts.publicData === null) {
            return json({ detail: 'invalid token' }, 404);
          }
          return json(
            opts.publicData ?? {
              id: 'req-1',
              tool_name: 'shell_command',
              tool_args: { command: 'ls -la' },
              agent_reasoning: null,
              status: 'pending',
              requested_at: '2026-06-01T10:00:00Z',
              expires_at: '2026-06-01T11:00:00Z',
              resolved_at: null,
              history: [],
            }
          );
        }

        if (/\/approval\/req-1\/decide/.test(url) && method === 'POST') {
          return json({
            id: 'req-1',
            tool_name: 'shell_command',
            tool_args: { command: 'ls -la' },
            agent_reasoning: null,
            status: 'approved',
            requested_at: '2026-06-01T10:00:00Z',
            expires_at: '2026-06-01T11:00:00Z',
            resolved_at: '2026-06-01T10:30:00Z',
            history: opts.decideHistory ?? [],
          });
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
    resetConfirmDialogForTests();
    document.querySelectorAll('sl-alert[open]').forEach((a) => a.remove());
    localStorage.clear();
  });

  async function renderRequest(request: Record<string, unknown>) {
    fetchStub = createFetchStub({ request });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;
    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;
    return element;
  }

  function decisionCall(action: 'approve' | 'decline') {
    return fetchStub
      .getCalls()
      .find(
        (c) =>
          String(c.args[0]).includes(`/${action}`) &&
          String((c.args[1] as RequestInit)?.method || '').toUpperCase() ===
            'POST'
      );
  }

  describe('an expired pending request', () => {
    it('renders as timed out with no decision bar', async () => {
      const element = await renderRequest(
        pendingRequest({
          requested_at: '2026-07-13T14:59:10Z',
          expires_at: '2026-07-13T15:04:10Z',
        })
      );

      expect(element.shadowRoot?.textContent).to.contain('Timed out');
      expect(element.shadowRoot?.textContent).to.not.contain('Pending');
      expect(element.shadowRoot?.querySelector('.decision-bar')).to.not.exist;
    });

    it('ignores the decision keys', async () => {
      await renderRequest(
        pendingRequest({
          requested_at: '2026-07-13T14:59:10Z',
          expires_at: '2026-07-13T15:04:10Z',
        })
      );

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
      await aTimeout(0);
      expect(decisionCall('approve'), 'a key decided an expired request').to.be
        .undefined;
    });
  });

  describe('the decision bar', () => {
    it('states what is paused and counts down to the expiry', async () => {
      const element = await renderRequest(pendingRequest());

      const bar = element.shadowRoot?.querySelector('.decision-bar');
      expect(bar, 'expected a sticky decision bar').to.exist;
      expect(bar?.textContent).to.contain('is paused until you decide');
      expect(bar?.textContent).to.contain('expires in');
      expect(bar?.querySelector('.row-approve, .approve')).to.exist;
      const deny = bar?.querySelector('.deny') as HTMLElement;
      expect(deny.getAttribute('variant')).to.equal('danger');
      expect(deny.hasAttribute('outline'), 'Deny must be outline').to.be.true;
    });

    it('names the comment field and hides the shortcut from screen readers', async () => {
      const element = await renderRequest(pendingRequest());
      const bar = element.shadowRoot?.querySelector(
        '.decision-bar'
      ) as HTMLElement;

      // Shoelace wires the label to the inner input, so a label attribute is
      // what gives the field an accessible name; a placeholder alone does not.
      const comment = bar.querySelector('.decision-comment') as HTMLElement;
      expect(
        comment.getAttribute('label'),
        'the comment field needs an accessible name'
      ).to.contain('Comment');

      bar.querySelectorAll('kbd').forEach((key) => {
        expect(
          key.getAttribute('aria-hidden'),
          'the shortcut hint is read out as part of the button label'
        ).to.equal('true');
      });
      expect(
        (bar.querySelector('.approve') as HTMLElement).getAttribute('title')
      ).to.equal('Approve (A)');
      expect(
        (bar.querySelector('.deny') as HTMLElement).getAttribute('title')
      ).to.equal('Deny (D)');
    });

    it('approves on the A key', async () => {
      const element = await renderRequest(pendingRequest());

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
      await waitUntil(() => !!decisionCall('approve'), 'no approve call');
      await waitUntil(
        () => (element as any).approvalRequest?.status === 'approved',
        'the request was not approved'
      );
    });

    it('opens the confirm on the D key and only then denies', async () => {
      const element = await renderRequest(pendingRequest());

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'd' }));
      await waitUntil(
        () => !!document.querySelector('confirm-dialog'),
        'no confirm dialog'
      );
      const dialog = document.querySelector('confirm-dialog') as HTMLElement;
      expect(decisionCall('decline'), 'denied without confirming').to.be
        .undefined;

      const confirm = dialog.shadowRoot?.querySelector(
        '[data-testid="confirm-dialog-confirm"]'
      ) as HTMLElement;
      confirm.click();
      await waitUntil(() => !!decisionCall('decline'), 'no decline call');
      await waitUntil(
        () => (element as any).approvalRequest?.status === 'declined',
        'the request was not denied'
      );
    });

    it('ignores the A key while the deny confirm is open', async () => {
      await renderRequest(pendingRequest());

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'd' }));
      await waitUntil(
        () => !!document.querySelector('confirm-dialog'),
        'no confirm dialog'
      );

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
      await aTimeout(0);
      expect(
        decisionCall('approve'),
        'A approved behind the open deny confirmation'
      ).to.be.undefined;
      expect(
        document.querySelectorAll('confirm-dialog').length,
        'the confirmation should still be the only thing asking'
      ).to.equal(1);
    });

    it('ignores a held-down D key', async () => {
      await renderRequest(pendingRequest());

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'd' }));
      await waitUntil(
        () => !!document.querySelector('confirm-dialog'),
        'no confirm dialog'
      );
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'd', repeat: true })
      );
      await aTimeout(0);

      const dialog = document.querySelector('confirm-dialog') as HTMLElement;
      const confirm = dialog.shadowRoot?.querySelector(
        '[data-testid="confirm-dialog-confirm"]'
      ) as HTMLElement;
      confirm.click();
      await waitUntil(() => !!decisionCall('decline'), 'no decline call');
    });

    it('leaves the keys alone while a comment is being typed', async () => {
      const element = await renderRequest(pendingRequest());

      const comment = element.shadowRoot?.querySelector(
        '.decision-comment'
      ) as HTMLElement;
      comment.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'a',
          bubbles: true,
          composed: true,
        })
      );
      await aTimeout(0);

      expect(decisionCall('approve'), 'a typed "a" approved the request').to.be
        .undefined;
    });

    it('does not toast twice when the websocket echoes this page own decision', async () => {
      const element = await renderRequest(pendingRequest());

      await (element as any).handleApprove();
      await element.updateComplete;
      const afterDecision = document.querySelectorAll('sl-alert').length;

      (element as any).handleWebSocketMessage({
        type: 'approval_approved',
        approval_request_id: 'req-1',
        status: 'approved',
        resolved_at: new Date().toISOString(),
      });
      await element.updateComplete;

      expect(
        document.querySelectorAll('sl-alert').length,
        'the echoed websocket update toasted the same decision again'
      ).to.equal(afterDecision);
    });

    it('points at the next waiting request once a decision is taken', async () => {
      const element = await renderRequest(pendingRequest());

      await (element as any).handleApprove();
      await element.updateComplete;

      const line = element.shadowRoot?.querySelector('.post-decision');
      expect(line, 'expected the post-decision line').to.exist;
      expect(line?.textContent).to.contain('Back to approvals');
      expect(line?.textContent).to.contain('Next waiting (1)');
      const next = line?.querySelector('a[href*="/console/approval/"]');
      expect(next?.getAttribute('href')).to.equal('/console/approval/req-2');
    });

    it('says 100+ when the pending page is full, not a count that looks total', async () => {
      const pendingQueue = Array.from({ length: 100 }, (_, i) =>
        pendingRequest({
          id: `queued-${i}`,
          expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
        })
      );
      fetchStub = createFetchStub({
        request: pendingRequest(),
        pendingQueue,
      });
      const element = (await fixture(
        html`<approval-view .requestId=${'req-1'}></approval-view>`
      )) as ApprovalView;
      await waitUntil(() => !(element as any).loading, 'still loading');
      await element.updateComplete;

      await (element as any).handleApprove();
      await element.updateComplete;

      const line = element.shadowRoot?.querySelector('.post-decision');
      expect(line?.textContent).to.contain('Next waiting (100+)');
      expect(line?.textContent).to.not.contain('Next waiting (99)');
    });
  });

  describe('on a phone viewport', () => {
    // The viewport is per browser page, so hand it back to the other tests.
    afterEach(async () => {
      await setViewport({ width: 1280, height: 800 });
    });

    it('keeps the header and the decision buttons inside the page', async () => {
      // A narrow wrapper div would not do: media queries read the window, so the
      // 640px rules (host padding, the bar's margins, the stacked full-width
      // buttons) only run when the viewport itself is a phone.
      await setViewport({ width: 375, height: 800 });
      fetchStub = createFetchStub({ request: pendingRequest() });
      const element = (await fixture(
        html`<approval-view .requestId=${'req-1'}></approval-view>`
      )) as ApprovalView;

      await waitUntil(() => !(element as any).loading, 'still loading');
      await element.updateComplete;

      const hostRight = element.getBoundingClientRect().right;
      const badge = element.shadowRoot?.querySelector(
        '.status-badge'
      ) as HTMLElement;
      expect(
        badge.getBoundingClientRect().right,
        'the status chip overflows the page'
      ).to.be.at.most(hostRight + 1);

      const buttons = element.shadowRoot?.querySelector(
        '.decision-buttons'
      ) as HTMLElement;
      expect(
        buttons.getBoundingClientRect().right,
        'the decision buttons overflow the page'
      ).to.be.at.most(hostRight + 1);
      expect(
        buttons.getBoundingClientRect().width,
        'the buttons should take the full row on a phone'
      ).to.be.greaterThan(200);
    });
  });

  it('renders a pending approval request', async () => {
    fetchStub = createFetchStub({ request: pendingRequest() });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('shell_command');
    expect(element.shadowRoot?.textContent).to.contain('Pending');
    const buttons = element.shadowRoot?.querySelectorAll(
      '.decision-bar sl-button'
    );
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

    expect(element.shadowRoot?.textContent).to.contain('Approved');
    expect(element.shadowRoot?.querySelector('.resolved-info')).to.exist;
    expect(element.shadowRoot?.textContent).to.contain('Looks safe');
    // No decision bar once resolved.
    expect(element.shadowRoot?.querySelector('.decision-bar')).to.not.exist;
  });

  it('renders an explicit expired state, not an error', async () => {
    fetchStub = createFetchStub({
      request: pendingRequest({
        status: 'expired',
        resolved_at: '2026-06-01T11:00:00Z',
      }),
    });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('Timed out');
    expect(element.shadowRoot?.textContent).to.contain(
      'no response within the window'
    );
    expect(element.shadowRoot?.querySelector('.expired-banner')).to.exist;
    expect(element.shadowRoot?.querySelector('.actions')).to.not.exist;
  });

  it('renders the workflow-history timeline', async () => {
    fetchStub = createFetchStub({ request: pendingRequest() });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await waitUntil(
      () => (element as any).history?.length === 2,
      'history did not load'
    );
    await element.updateComplete;

    const timeline = element.shadowRoot?.querySelector('.timeline');
    expect(timeline).to.exist;
    expect(element.shadowRoot?.textContent).to.contain('Workflow History');
    expect(element.shadowRoot?.textContent).to.contain(
      "Approval requested for tool 'shell_command'"
    );
    expect(element.shadowRoot?.textContent).to.contain(
      'Notification via email'
    );
    // Timeline items carry their timestamps.
    expect(
      element.shadowRoot?.querySelectorAll('.timeline li').length
    ).to.equal(2);
  });

  it('falls back to the public token payload when the account read fails', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/approval/req-1?token=tok-123'
    );
    fetchStub = createFetchStub({
      request: null,
      publicData: {
        id: 'req-1',
        tool_name: 'shell_command',
        tool_args: { command: 'ls -la' },
        agent_reasoning: null,
        status: 'expired',
        requested_at: '2026-06-01T10:00:00Z',
        expires_at: '2026-06-01T11:00:00Z',
        resolved_at: '2026-06-01T11:00:00Z',
        history: [
          {
            event_type: 'expired',
            detail: 'Expired: no response within the approval window',
            comment: null,
            timestamp: '2026-06-01T11:00:00Z',
          },
        ],
      },
    });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    expect((element as any).publicOnly).to.be.true;
    expect(element.shadowRoot?.textContent).to.contain('Timed out');
    expect(element.shadowRoot?.textContent).to.contain('Workflow History');
    expect(element.shadowRoot?.textContent).to.contain(
      'Expired: no response within the approval window'
    );
    window.history.replaceState({}, '', '/');
  });

  it('submits public decisions through the token endpoint', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/approval/req-1?token=tok-123'
    );
    fetchStub = createFetchStub({
      request: null,
      publicData: {
        id: 'req-1',
        tool_name: 'shell_command',
        tool_args: { command: 'ls -la' },
        agent_reasoning: null,
        status: 'pending',
        requested_at: '2026-06-01T10:00:00Z',
        expires_at: '2026-06-01T11:00:00Z',
        resolved_at: null,
        history: [],
      },
    });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await element.updateComplete;

    (element as any).comment = 'approved from the link';
    await (element as any).handleApprove();
    await element.updateComplete;

    const decideCall = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/decide'));
    expect(decideCall, 'expected a POST to the token decide endpoint').to.exist;
    const body = JSON.parse(String((decideCall!.args[1] as RequestInit).body));
    expect(body.action).to.equal('approve');
    expect(body.comment).to.equal('approved from the link');
    window.history.replaceState({}, '', '/');
  });

  it('keeps workflow history after a token-path decision with empty history', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/approval/req-1?token=tok-123'
    );
    const publicHistory = [
      {
        event_type: 'approval_requested',
        detail: "Approval requested for tool 'shell_command'",
        comment: null,
        timestamp: '2026-06-01T10:00:00Z',
      },
      {
        event_type: 'notification_sent',
        detail: 'Notification via email to 1 recipient (sent)',
        comment: null,
        timestamp: '2026-06-01T10:00:01Z',
      },
    ];
    fetchStub = createFetchStub({
      request: null,
      publicData: {
        id: 'req-1',
        tool_name: 'shell_command',
        tool_args: { command: 'ls -la' },
        agent_reasoning: null,
        status: 'pending',
        requested_at: '2026-06-01T10:00:00Z',
        expires_at: '2026-06-01T11:00:00Z',
        resolved_at: null,
        history: publicHistory,
      },
      decideHistory: [],
    });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await waitUntil(
      () => (element as any).history?.length === 2,
      'history did not load'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('Workflow History');
    await (element as any).handleApprove();
    await element.updateComplete;

    expect((element as any).history.length).to.equal(2);
    expect(element.shadowRoot?.textContent).to.contain('Workflow History');
    expect(element.shadowRoot?.textContent).to.contain(
      "Approval requested for tool 'shell_command'"
    );
    window.history.replaceState({}, '', '/');
  });

  it('replaces workflow history when the decide payload includes events', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/approval/req-1?token=tok-123'
    );
    fetchStub = createFetchStub({
      request: null,
      publicData: {
        id: 'req-1',
        tool_name: 'shell_command',
        tool_args: { command: 'ls -la' },
        agent_reasoning: null,
        status: 'pending',
        requested_at: '2026-06-01T10:00:00Z',
        expires_at: '2026-06-01T11:00:00Z',
        resolved_at: null,
        history: [
          {
            event_type: 'approval_requested',
            detail: "Approval requested for tool 'shell_command'",
            comment: null,
            timestamp: '2026-06-01T10:00:00Z',
          },
        ],
      },
      decideHistory: [
        {
          event_type: 'approval_requested',
          detail: "Approval requested for tool 'shell_command'",
          comment: null,
          timestamp: '2026-06-01T10:00:00Z',
        },
        {
          event_type: 'vote_received',
          detail: 'Approval vote received (token-based) via token link',
          comment: null,
          timestamp: '2026-06-01T10:30:00Z',
        },
      ],
    });
    const element = (await fixture(
      html`<approval-view .requestId=${'req-1'}></approval-view>`
    )) as ApprovalView;

    await waitUntil(() => !(element as any).loading, 'still loading');
    await (element as any).handleApprove();
    await element.updateComplete;

    expect((element as any).history.length).to.equal(2);
    expect(element.shadowRoot?.textContent).to.contain(
      'Approval vote received'
    );
    window.history.replaceState({}, '', '/');
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
    expect((element as any).decisionTaken).to.be.true;
    const approveCall = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('/approve'));
    expect(approveCall, 'expected a POST to /approve').to.exist;
  });

  describe('agent questions', () => {
    async function renderQuestion(overrides: Record<string, unknown> = {}) {
      fetchStub = createFetchStub({ request: questionRequest(overrides) });
      const element = (await fixture(
        html`<approval-view .requestId=${'req-1'}></approval-view>`
      )) as ApprovalView;

      await waitUntil(() => !(element as any).loading, 'still loading');
      await element.updateComplete;

      const panel = element.shadowRoot?.querySelector(
        'question-answer-panel'
      ) as any;
      expect(panel, 'expected a question-answer-panel').to.exist;
      await panel.updateComplete;
      return { element, panel };
    }

    function bodyOf(call: sinon.SinonSpyCall) {
      return JSON.parse(String((call.args[1] as RequestInit).body));
    }

    it('renders option buttons and the answer field for a question', async () => {
      const { element, panel } = await renderQuestion();

      expect(element.shadowRoot?.textContent).to.contain('Agent question');
      expect(panel.shadowRoot.textContent).to.contain('Which colour?');

      const options = panel.shadowRoot.querySelectorAll('.question-option');
      expect(options.length).to.equal(2);
      expect(options[0].textContent.trim()).to.equal('blue');
      expect(options[1].textContent.trim()).to.equal('green');
      expect(panel.shadowRoot.querySelector('.answer-input')).to.exist;

      // The decision bar is hidden for questions: the panel carries them.
      expect(element.shadowRoot?.querySelector('.decision-bar')).to.not.exist;
    });

    it('submits selected_option when an option is clicked', async () => {
      const { element, panel } = await renderQuestion();

      const options = panel.shadowRoot.querySelectorAll('.question-option');
      options[1].click();
      await waitUntil(
        () =>
          fetchStub
            .getCalls()
            .some((c) => String(c.args[0]).includes('/approve')),
        'no approve call'
      );
      await element.updateComplete;

      const approveCall = fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/approve'))!;
      const body = bodyOf(approveCall);
      expect(body.approved).to.be.true;
      expect(body.selected_option).to.equal('green');
      expect(body.answer_text).to.be.undefined;
    });

    it('submits answer_text when free text is sent', async () => {
      const { element, panel } = await renderQuestion();

      const textarea = panel.shadowRoot.querySelector('.answer-input') as any;
      textarea.value = 'teal';
      textarea.dispatchEvent(
        new CustomEvent('sl-input', { bubbles: true, composed: true })
      );
      await panel.updateComplete;

      const send = panel.shadowRoot.querySelector(
        '.send-answer'
      ) as HTMLElement;
      send.click();
      await waitUntil(
        () =>
          fetchStub
            .getCalls()
            .some((c) => String(c.args[0]).includes('/approve')),
        'no approve call'
      );
      await element.updateComplete;

      const body = bodyOf(
        fetchStub
          .getCalls()
          .find((c) => String(c.args[0]).includes('/approve'))!
      );
      expect(body.approved).to.be.true;
      expect(body.answer_text).to.equal('teal');
      expect(body.selected_option).to.be.undefined;
    });

    it('hides the answer field when free text is not allowed', async () => {
      const { panel } = await renderQuestion({ allow_free_text: false });

      expect(panel.shadowRoot.querySelector('.answer-input')).to.not.exist;
      expect(
        panel.shadowRoot.querySelectorAll('.question-option').length
      ).to.equal(2);
    });

    it('declines the request when the question is dismissed', async () => {
      const { element, panel } = await renderQuestion();

      const dismiss = panel.shadowRoot.querySelector(
        '.dismiss-question'
      ) as HTMLElement;
      dismiss.click();
      await waitUntil(
        () =>
          fetchStub
            .getCalls()
            .some((c) => String(c.args[0]).includes('/decline')),
        'no decline call'
      );
      await element.updateComplete;

      const body = bodyOf(
        fetchStub
          .getCalls()
          .find((c) => String(c.args[0]).includes('/decline'))!
      );
      expect(body.approved).to.be.false;
      await waitUntil(
        () => (element as any).approvalRequest?.status === 'declined',
        'request was not marked declined'
      );
    });

    it('renders the normal approve/decline UI when question fields are absent', async () => {
      fetchStub = createFetchStub({ request: pendingRequest() });
      const element = (await fixture(
        html`<approval-view .requestId=${'req-1'}></approval-view>`
      )) as ApprovalView;

      await waitUntil(() => !(element as any).loading, 'still loading');
      await element.updateComplete;

      expect(element.shadowRoot?.querySelector('question-answer-panel')).to.not
        .exist;
      expect(element.shadowRoot?.querySelector('.decision-comment')).to.exist;
      expect(
        element.shadowRoot?.querySelectorAll('.decision-bar sl-button').length
      ).to.equal(2);
    });
  });
});
