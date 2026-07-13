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

      expect(element.shadowRoot?.textContent).to.contain('Agent Question');
      expect(panel.shadowRoot.textContent).to.contain('Which colour?');

      const options = panel.shadowRoot.querySelectorAll('.question-option');
      expect(options.length).to.equal(2);
      expect(options[0].textContent.trim()).to.equal('blue');
      expect(options[1].textContent.trim()).to.equal('green');
      expect(panel.shadowRoot.querySelector('.answer-input')).to.exist;

      // The plain approve/decline + comment UI is hidden for questions.
      expect(element.shadowRoot?.querySelector('.actions')).to.not.exist;
      expect(element.shadowRoot?.querySelector('.comment-section')).to.not
        .exist;
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
      expect(element.shadowRoot?.querySelector('.comment-section')).to.exist;
      expect(
        element.shadowRoot?.querySelectorAll('.actions sl-button').length
      ).to.equal(2);
    });
  });
});
