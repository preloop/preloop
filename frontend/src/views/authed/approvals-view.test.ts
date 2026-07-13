import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './approvals-view';
import type { ApprovalsView } from './approvals-view';

describe('ApprovalsView', () => {
  let fetchStub: sinon.SinonStub;

  function createFetchStub(approvalRequests: unknown[] = []) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });

        if (url.includes('/approve') && method === 'POST') {
          return json({
            status: 'approved',
            resolved_at: new Date().toISOString(),
          });
        }

        if (url.includes('/decline') && method === 'POST') {
          return json({
            status: 'declined',
            resolved_at: new Date().toISOString(),
          });
        }

        if (url.includes('/api/v1/approval-requests') && method === 'GET') {
          return json(approvalRequests);
        }

        return json({ detail: `Unhandled: ${method} ${url}` });
      });
  }

  function baseRequest(overrides: Record<string, unknown> = {}) {
    return {
      id: 'ar-1',
      account_id: 'acc-1',
      tool_configuration_id: 'tc-1',
      approval_workflow_id: 'aw-1',
      execution_id: null,
      tool_name: 'example_tool',
      summary: null,
      tool_args: {},
      agent_reasoning: null,
      status: 'pending',
      requested_at: new Date().toISOString(),
      resolved_at: null,
      expires_at: null,
      approver_comment: null,
      ...overrides,
    };
  }

  function questionRequest(overrides: Record<string, unknown> = {}) {
    return baseRequest({
      tool_name: 'ask_user',
      summary: 'Which colour?',
      is_question: true,
      question: 'Which colour?',
      question_options: ['blue', 'green'],
      allow_free_text: true,
      ...overrides,
    });
  }

  async function renderList(requests: unknown[]) {
    fetchStub = createFetchStub(requests);
    const element = (await fixture(
      html`<approvals-view></approvals-view>`
    )) as ApprovalsView;

    await waitUntil(
      () => !(element as any).loading,
      'Approvals view did not finish loading'
    );
    await element.updateComplete;
    return element;
  }

  function bodyOf(call: sinon.SinonSpyCall) {
    return JSON.parse(String((call.args[1] as RequestInit).body));
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('renders the approval list view', async () => {
    fetchStub = createFetchStub([]);
    const element = (await fixture(
      html`<approvals-view></approvals-view>`
    )) as ApprovalsView;

    await waitUntil(
      () => !(element as any).loading,
      'Approvals view did not finish loading'
    );
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    expect(header?.getAttribute('headerText')).to.equal('Approval Requests');
  });

  it('shows empty state when no approval requests', async () => {
    fetchStub = createFetchStub([]);
    const element = (await fixture(
      html`<approvals-view></approvals-view>`
    )) as ApprovalsView;

    await waitUntil(
      () => !(element as any).loading,
      'Approvals view did not finish loading'
    );
    await element.updateComplete;

    const emptyState = element.shadowRoot?.querySelector('.empty-state');
    expect(emptyState).to.exist;
    expect(emptyState?.textContent).to.include('No approval requests yet');
  });

  it('shows approval list when requests exist', async () => {
    const mockRequests = [
      {
        id: 'ar-1',
        account_id: 'acc-1',
        tool_configuration_id: 'tc-1',
        approval_workflow_id: 'aw-1',
        execution_id: null,
        tool_name: 'example_tool',
        tool_args: {},
        agent_reasoning: null,
        status: 'pending',
        requested_at: new Date().toISOString(),
        resolved_at: null,
        expires_at: null,
        approver_comment: null,
      },
    ];
    fetchStub = createFetchStub(mockRequests);
    const element = (await fixture(
      html`<approvals-view></approvals-view>`
    )) as ApprovalsView;

    await waitUntil(
      () => (element as any).approvalRequests?.length === 1,
      'Approval requests did not load'
    );
    await element.updateComplete;

    const approvalList = element.shadowRoot?.querySelector('.approval-list');
    expect(approvalList).to.exist;
    const approvalItems =
      element.shadowRoot?.querySelectorAll('.approval-item');
    expect(approvalItems?.length).to.equal(1);
  });

  it('stubs fetch for approval-requests API', async () => {
    fetchStub = createFetchStub([]);
    const element = (await fixture(
      html`<approvals-view></approvals-view>`
    )) as ApprovalsView;

    await waitUntil(
      () => !(element as any).loading,
      'Approvals view did not finish loading'
    );

    expect(fetchStub).to.have.been.called;
    const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
    expect(urls.some((u) => u.includes('/api/v1/approval-requests'))).to.be
      .true;
  });

  describe('agent questions', () => {
    async function questionPanel(overrides: Record<string, unknown> = {}) {
      const element = await renderList([questionRequest(overrides)]);
      const panel = element.shadowRoot?.querySelector(
        'question-answer-panel'
      ) as any;
      expect(panel, 'expected an inline question-answer-panel').to.exist;
      await panel.updateComplete;
      return { element, panel };
    }

    it('renders options and the answer field inline on the question card', async () => {
      const { element, panel } = await questionPanel();

      expect(element.shadowRoot?.querySelector('.approval-item.question')).to
        .exist;
      expect(panel.shadowRoot.textContent).to.contain('Which colour?');
      const options = panel.shadowRoot.querySelectorAll('.question-option');
      expect(options.length).to.equal(2);
      expect(panel.shadowRoot.querySelector('.answer-input')).to.exist;
    });

    it('approves with selected_option when an option is clicked', async () => {
      const { element, panel } = await questionPanel();

      panel.shadowRoot.querySelectorAll('.question-option')[0].click();
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
      expect(body.selected_option).to.equal('blue');
      await waitUntil(
        () => (element as any).approvalRequests[0].status === 'approved',
        'question was not marked approved'
      );
    });

    it('approves with answer_text when free text is sent', async () => {
      const { element, panel } = await questionPanel();

      const textarea = panel.shadowRoot.querySelector('.answer-input') as any;
      textarea.value = 'teal';
      textarea.dispatchEvent(
        new CustomEvent('sl-input', { bubbles: true, composed: true })
      );
      await panel.updateComplete;
      (panel.shadowRoot.querySelector('.send-answer') as HTMLElement).click();

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
      expect(body.answer_text).to.equal('teal');
      expect(body.selected_option).to.be.undefined;
    });

    it('declines the request when the question is dismissed', async () => {
      const { element, panel } = await questionPanel();

      (
        panel.shadowRoot.querySelector('.dismiss-question') as HTMLElement
      ).click();
      await waitUntil(
        () =>
          fetchStub
            .getCalls()
            .some((c) => String(c.args[0]).includes('/decline')),
        'no decline call'
      );
      await waitUntil(
        () => (element as any).approvalRequests[0].status === 'declined',
        'question was not marked declined'
      );
    });

    it('hides the answer field when free text is not allowed', async () => {
      const { panel } = await questionPanel({ allow_free_text: false });

      expect(panel.shadowRoot.querySelector('.answer-input')).to.not.exist;
      expect(
        panel.shadowRoot.querySelectorAll('.question-option').length
      ).to.equal(2);
    });

    it('leaves non-question requests unchanged (no inline answer UI)', async () => {
      const element = await renderList([baseRequest()]);

      expect(element.shadowRoot?.querySelector('question-answer-panel')).to.not
        .exist;
      expect(element.shadowRoot?.querySelector('.approval-item')).to.exist;
      expect(element.shadowRoot?.textContent).to.contain('Review');
    });
  });
});
