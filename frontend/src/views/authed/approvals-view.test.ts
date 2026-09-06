import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import { Router } from '@vaadin/router';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './approvals-view';
import { resetConfirmDialogForTests } from '../../components/confirm-dialog';
import type { ApprovalsView } from './approvals-view';

describe('ApprovalsView', () => {
  let fetchStub: sinon.SinonStub;
  /** Per id results the stubbed batch endpoint should return, if any. */
  let batchOutcomes: Record<string, unknown> = {};

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

        if (url.includes('/decide-batch') && method === 'POST') {
          const ids = JSON.parse(String(init?.body)).ids as string[];
          return json({
            results: ids.map((id) => batchOutcomes[id] ?? { id, ok: true }),
            succeeded: ids.length,
            failed: 0,
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
    batchOutcomes = {};
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    resetConfirmDialogForTests();
    document.querySelectorAll('sl-alert[open]').forEach((a) => a.remove());
    localStorage.clear();
  });

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

  function inMinutes(minutes: number) {
    return new Date(Date.now() + minutes * 60_000).toISOString();
  }

  describe('waiting for you', () => {
    it('puts the waiting group before history and sorts it by expiry', async () => {
      const element = await renderList([
        baseRequest({ id: 'later', expires_at: inMinutes(40) }),
        baseRequest({
          id: 'done',
          status: 'approved',
          resolved_at: new Date().toISOString(),
        }),
        baseRequest({ id: 'soonest', expires_at: inMinutes(3) }),
      ]);

      const headings = Array.from(
        element.shadowRoot?.querySelectorAll('.group-header h2') ?? []
      ).map((h) => h.textContent?.trim());
      expect(headings).to.deep.equal(['Waiting for you', 'History']);

      expect(
        (element as any).waitingRequests.map((r: any) => r.id)
      ).to.deep.equal(['soonest', 'later']);
      expect(
        (element as any).historyRequests.map((r: any) => r.id)
      ).to.deep.equal(['done']);
    });

    it('approves from the row without leaving the list', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      const approve = element.shadowRoot?.querySelector(
        '.row-approve'
      ) as HTMLElement;
      expect(approve, 'expected a row-level Approve').to.exist;
      approve.click();

      await waitUntil(() => !!decisionCall('approve'), 'no approve call');
      const body = JSON.parse(
        String((decisionCall('approve')!.args[1] as RequestInit).body)
      );
      expect(body.approved).to.be.true;
      await waitUntil(
        () => (element as any).approvalRequests[0].status === 'approved',
        'row was not marked approved'
      );
    });

    it('confirms before it denies from the row', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      const deny = element.shadowRoot?.querySelector(
        '.row-deny'
      ) as HTMLElement;
      expect(deny, 'expected a row-level Deny').to.exist;
      expect(deny.getAttribute('variant')).to.equal('danger');
      expect(deny.hasAttribute('outline'), 'Deny must be outline').to.be.true;
      deny.click();

      await waitUntil(
        () => !!document.querySelector('confirm-dialog'),
        'no confirm dialog'
      );
      expect(decisionCall('decline'), 'denied without confirming').to.be
        .undefined;

      const dialog = document.querySelector('confirm-dialog') as HTMLElement;
      (
        dialog.shadowRoot?.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
      ).click();

      await waitUntil(() => !!decisionCall('decline'), 'no decline call');
      await waitUntil(
        () => (element as any).approvalRequests[0].status === 'declined',
        'row was not marked denied'
      );
    });

    it('keeps an expired pending request in history with no decision', async () => {
      const element = await renderList([
        baseRequest({
          id: 'stale',
          requested_at: '2026-07-13T14:59:10Z',
          expires_at: '2026-07-13T15:04:10Z',
        }),
      ]);

      expect((element as any).waitingRequests.length).to.equal(0);
      expect(element.shadowRoot?.querySelector('.row-approve')).to.not.exist;
      expect(element.shadowRoot?.textContent).to.contain('Timed out');
    });

    it('moves a pending row to history when it expires while the list is open', async () => {
      const element = await renderList([
        baseRequest({
          id: 'about-to-expire',
          expires_at: new Date(Date.now() + 400).toISOString(),
        }),
      ]);

      expect(
        (element as any).waitingRequests.map((r: { id: string }) => r.id)
      ).to.deep.equal(['about-to-expire']);
      expect(element.shadowRoot?.querySelector('.row-approve')).to.exist;

      await waitUntil(
        () => (element as any).waitingRequests.length === 0,
        'expired row stayed in Waiting for you',
        { timeout: 3500 }
      );
      await element.updateComplete;

      expect(element.shadowRoot?.querySelector('.row-approve')).to.not.exist;
      expect(element.shadowRoot?.textContent).to.contain('Timed out');
      expect(
        (element as any).historyRequests.map((r: { id: string }) => r.id)
      ).to.deep.equal(['about-to-expire']);
    });

    it('does not post approve once the row has timed out', async () => {
      const element = await renderList([
        baseRequest({
          id: 'ar-1',
          expires_at: new Date(Date.now() + 60_000).toISOString(),
        }),
      ]);
      const request = (element as any).waitingRequests[0];
      request.expires_at = new Date(Date.now() - 1000).toISOString();

      await (element as any).handleRowApprove(request);

      expect(decisionCall('approve'), 'approved after expiry').to.be.undefined;
      expect((element as any).waitingRequests.length).to.equal(0);
      expect(element.shadowRoot?.querySelector('.row-approve')).to.not.exist;
    });
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
    expect(header?.getAttribute('headerText')).to.equal('Approval requests');
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

  describe('attribution', () => {
    /** The attribution line renders into its own shadow root. */
    function lineOf(element: ApprovalsView, requestId: string) {
      const row = element.shadowRoot!.querySelector(
        `[data-request-id="${requestId}"] attribution-line`
      )!;
      return {
        text: row.shadowRoot!.textContent!.replace(/\s+/g, ' ').trim(),
        hrefs: Array.from(row.shadowRoot!.querySelectorAll('a')).map((a) =>
          a.getAttribute('href')
        ),
      };
    }

    it('names and links the agent, key, session and run on the row', async () => {
      const element = await renderList([
        baseRequest({
          id: 'attributed',
          managed_agent_name: null,
          agent: {
            id: 'agent-1',
            name: 'Claude Code (laptop)',
            kind: 'claude_code',
          },
          api_key: { id: 'key-1', name: 'claude-code-laptop' },
          session: { id: 'session-1', subject: 'feature/attribution' },
          flow_execution: {
            id: 'exec-1',
            flow_id: 'flow-1',
            flow_name: 'Nightly audit',
          },
        }),
      ]);

      const { text, hrefs } = lineOf(element, 'attributed');
      expect(text).to.contain('Claude Code (laptop)');
      expect(text).to.contain('claude-code-laptop');
      expect(text).to.contain('feature/attribution');
      expect(text).to.contain('Nightly audit');
      expect(hrefs).to.deep.equal([
        '/console/agents/agent-1',
        '/console/settings/api-keys/key-1',
        '/console/runtime-sessions?sessionId=session-1',
        '/console/flows/executions/exec-1',
      ]);
      // The requester chip stops saying "AI agent" for a named agent.
      const row = element.shadowRoot!.querySelector(
        '[data-request-id="attributed"]'
      )!;
      expect(row.textContent).to.contain('Claude Code (laptop)');
      expect(row.textContent).to.not.contain('AI agent');
    });

    it('shows just the key when the caller is only a key', async () => {
      const element = await renderList([
        baseRequest({
          id: 'key-only',
          api_key: { id: 'k-2', name: 'ci-deploy' },
        }),
      ]);

      const { text, hrefs } = lineOf(element, 'key-only');
      expect(text).to.equal('Key ci-deploy');
      expect(hrefs).to.deep.equal(['/console/settings/api-keys/k-2']);
    });
  });

  describe('keyboard', () => {
    function press(
      element: ApprovalsView,
      key: string,
      options: { shiftKey?: boolean } = {}
    ) {
      // Keys go to the row the keyboard is on, exactly as they do in the
      // browser: J and K put the focus on a row, so that row is the target.
      const focused =
        element.shadowRoot?.querySelector<HTMLElement>(
          '.approval-item[tabindex="0"]'
        ) ?? element;
      focused.dispatchEvent(
        new KeyboardEvent('keydown', {
          key,
          bubbles: true,
          composed: true,
          ...options,
        })
      );
      return element.updateComplete;
    }

    function rows(element: ApprovalsView) {
      return Array.from(
        element.shadowRoot?.querySelectorAll('.approval-item') ?? []
      ) as HTMLElement[];
    }

    it('moves the focused row with j and k', async () => {
      const element = await renderList([
        baseRequest({ id: 'first', expires_at: inMinutes(3) }),
        baseRequest({ id: 'second', expires_at: inMinutes(30) }),
      ]);

      await press(element, 'j');
      expect((element as any).focusedIndex).to.equal(0);
      await press(element, 'j');
      expect((element as any).focusedIndex).to.equal(1);
      expect(rows(element)[1].getAttribute('tabindex')).to.equal('0');

      await press(element, 'k');
      expect((element as any).focusedIndex).to.equal(0);

      // k on the first row stays put rather than wrapping to the bottom.
      await press(element, 'k');
      expect((element as any).focusedIndex).to.equal(0);
    });

    it('approves the focused row with a', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      await press(element, 'j');
      await press(element, 'a');

      await waitUntil(() => !!decisionCall('approve'), 'no approve call');
    });

    it('ignores a second a while the first decision is in flight', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      await press(element, 'j');
      // The Approve button is disabled while the POST is out; the key has to
      // behave the same, or an impatient double tap decides twice.
      const first = press(element, 'a');
      const second = press(element, 'a');
      await Promise.all([first, second]);
      await waitUntil(() => !!decisionCall('approve'), 'no approve call');

      const approveCalls = fetchStub
        .getCalls()
        .filter(
          (c) =>
            String(c.args[0]).includes('/approve') &&
            String((c.args[1] as RequestInit)?.method || '').toUpperCase() ===
              'POST'
        );
      expect(approveCalls).to.have.length(1);
    });

    it('confirms before d denies the focused row', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      await press(element, 'j');
      await press(element, 'd');

      await waitUntil(
        () => !!document.querySelector('confirm-dialog'),
        'no confirm dialog'
      );
      expect(decisionCall('decline'), 'denied without confirming').to.be
        .undefined;
      (
        document
          .querySelector('confirm-dialog')!
          .shadowRoot?.querySelector(
            '[data-testid="confirm-dialog-confirm"]'
          ) as HTMLElement
      ).click();
      await waitUntil(() => !!decisionCall('decline'), 'no decline call');
    });

    it('selects the focused row with x and says so to a screen reader', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      await press(element, 'j');
      expect(rows(element)[0].getAttribute('aria-selected')).to.equal('false');

      await press(element, 'x');
      expect((element as any).selectedIds).to.deep.equal(['ar-1']);
      expect(rows(element)[0].getAttribute('aria-selected')).to.equal('true');

      await press(element, 'x');
      expect((element as any).selectedIds).to.deep.equal([]);
    });

    it('opens the focused row with Enter', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);
      const go = sinon.stub(Router, 'go');
      try {
        await press(element, 'j');
        await press(element, 'Enter');
        expect(go.calledWith('/console/approval/ar-1')).to.be.true;
      } finally {
        go.restore();
      }
    });

    it('does not open the row when Enter is pressed on Approve', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);
      await press(element, 'j');
      const go = sinon.stub(Router, 'go');
      try {
        const approve = rows(element)[0].querySelector(
          'sl-button'
        ) as HTMLElement;
        approve.dispatchEvent(
          new KeyboardEvent('keydown', {
            key: 'Enter',
            bubbles: true,
            composed: true,
          })
        );
        await element.updateComplete;
        expect(go.called).to.equal(false);
      } finally {
        go.restore();
      }
    });

    it('leaves keys alone while a filter is being typed in', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      const search = element.shadowRoot?.querySelector(
        'sl-input'
      ) as HTMLElement;
      search.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'a',
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;

      expect(decisionCall('approve'), 'typing decided a request').to.be
        .undefined;
    });

    it('offers the key legend on the waiting group', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      const legend = element.shadowRoot?.querySelector('.key-legend');
      expect(legend, 'expected a key legend').to.exist;
      expect(legend?.textContent).to.contain('A approve');
      expect(legend?.textContent).to.contain('D deny');
    });
  });

  describe('the counts strip (B-L3)', () => {
    function resolved(status: string, id: string) {
      const requestedAt = new Date(Date.now() - 10 * 60_000).toISOString();
      return baseRequest({
        id,
        status,
        requested_at: requestedAt,
        resolved_at: new Date(Date.now() - 8 * 60_000).toISOString(),
      });
    }

    it('states the counts on one hairline strip, not in cards', async () => {
      const element = await renderList([
        baseRequest({ id: 'waiting', expires_at: inMinutes(30) }),
        resolved('approved', 'yes'),
        resolved('declined', 'no'),
      ]);

      expect(
        element.shadowRoot?.querySelectorAll('.stat-card').length
      ).to.equal(0);
      const strip = element.shadowRoot?.querySelector(
        '.stat-strip'
      ) as HTMLElement;
      expect(strip).to.exist;
      // The separators are their own spans, spaced by the flex gap, so the
      // text node reading is the facts joined by the middle dot.
      expect((strip.textContent || '').replace(/\s+/g, ' ').trim()).to.equal(
        '3 requests·1 waiting·1 approved·1 denied·0 timed out·50% approved by a person·avg response 2m'
      );
    });

    it('says "last 100" instead of a total when the page came back full', async () => {
      const requests = Array.from({ length: 100 }, (_unused, index) =>
        resolved('approved', `ar-${index}`)
      );
      const element = await renderList(requests);

      const strip = element.shadowRoot?.querySelector(
        '.stat-strip'
      ) as HTMLElement;
      const text = (strip.textContent || '').replace(/\s+/g, ' ').trim();
      // "Last 100", never a bare "100 requests": the page limit is not a
      // count of everything the account ever asked for.
      expect(text).to.match(/^Last 100 requests·/);
    });
  });

  describe('new since last visit', () => {
    it('dots a waiting request the first time it is seen and not after', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);
      expect(element.shadowRoot?.querySelector('.new-dot'), 'no new dot').to
        .exist;
      fetchStub.restore();

      const second = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);
      expect(second.shadowRoot?.querySelector('.new-dot'), 'dot came back').to
        .not.exist;
    });
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
      expect(element.shadowRoot?.textContent).to.contain('Details');
    });
  });
  describe('bulk decisions', () => {
    function rows(element: ApprovalsView) {
      return Array.from(
        element.shadowRoot?.querySelectorAll('.approval-item') ?? []
      ) as HTMLElement[];
    }

    function checkboxes(element: ApprovalsView) {
      return Array.from(
        element.shadowRoot?.querySelectorAll('list-select-checkbox') ?? []
      ) as HTMLElement[];
    }

    function bulkBar(element: ApprovalsView) {
      return element.shadowRoot?.querySelector('list-bulk-bar') as HTMLElement;
    }

    function barButton(element: ApprovalsView, action: string) {
      return bulkBar(element)?.shadowRoot?.querySelector(
        `sl-button[data-action="${action}"]`
      ) as HTMLElement;
    }

    async function select(element: ApprovalsView, ids: string[]) {
      for (const id of ids) {
        (element as any).selection.toggle(id);
      }
      await element.updateComplete;
    }

    function batchCall() {
      return fetchStub
        .getCalls()
        .find((c) => String(c.args[0]).includes('/decide-batch'));
    }

    function confirmDialogElement() {
      return document.querySelector('confirm-dialog');
    }

    async function agree() {
      await waitUntil(() => !!confirmDialogElement(), 'no confirm dialog');
      (
        confirmDialogElement()!.shadowRoot?.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
      ).click();
    }

    it('offers a checkbox on waiting rows only', async () => {
      const element = await renderList([
        baseRequest({ id: 'waiting', expires_at: inMinutes(10) }),
        baseRequest({
          id: 'done',
          status: 'approved',
          resolved_at: new Date().toISOString(),
        }),
        questionRequest({ id: 'question', expires_at: inMinutes(10) }),
      ]);

      const boxes = checkboxes(element);
      expect(boxes.length, 'one checkbox, on the decidable row').to.equal(1);
      expect(boxes[0].getAttribute('item-id')).to.equal('waiting');
      expect(
        rows(element).filter((row) => row.dataset.selectionId).length
      ).to.equal(1);
    });

    it('shows the bar with a count only once something is selected', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
        baseRequest({ id: 'ar-2', expires_at: inMinutes(20) }),
      ]);

      expect(bulkBar(element)?.shadowRoot?.querySelector('.bulk-bar')).to.not
        .exist;

      await select(element, ['ar-1', 'ar-2']);
      expect(
        bulkBar(element)
          ?.shadowRoot?.querySelector('[data-testid="bulk-count"]')
          ?.textContent?.trim()
      ).to.equal('2 selected');
    });

    it('approves the selection with one call after naming the requests', async () => {
      const element = await renderList([
        baseRequest({
          id: 'ar-1',
          tool_name: 'write_file',
          expires_at: inMinutes(10),
        }),
        baseRequest({
          id: 'ar-2',
          tool_name: 'run_tests',
          expires_at: inMinutes(20),
        }),
      ]);

      await select(element, ['ar-1', 'ar-2']);
      barButton(element, 'approve').click();

      await waitUntil(() => !!confirmDialogElement(), 'no confirm dialog');
      const dialog = confirmDialogElement()!.shadowRoot!;
      expect(dialog.querySelector('sl-dialog')?.getAttribute('label')).to.equal(
        'Approve 2 requests?'
      );
      const dialogText = dialog.textContent ?? '';
      expect(dialogText).to.contain('write_file');
      expect(dialogText).to.contain('run_tests');
      expect(batchCall(), 'decided before the operator agreed').to.be.undefined;

      await agree();
      await waitUntil(() => !!batchCall(), 'no batch call');

      const body = bodyOf(batchCall()!);
      expect(body.ids).to.deep.equal(['ar-1', 'ar-2']);
      expect(body.approved).to.be.true;
      expect(
        fetchStub
          .getCalls()
          .filter((c) => String(c.args[0]).includes('/approve')).length,
        'a batch must not also send per row calls'
      ).to.equal(0);

      await waitUntil(
        () =>
          (element as any).approvalRequests.every(
            (r: any) => r.status === 'approved'
          ),
        'rows were not marked approved'
      );
      expect((element as any).selectedIds).to.deep.equal([]);
    });

    it('denies the selection with one call, confirming first', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
      ]);

      await select(element, ['ar-1']);
      barButton(element, 'deny').click();

      await waitUntil(() => !!confirmDialogElement(), 'no confirm dialog');
      expect(
        confirmDialogElement()!
          .shadowRoot!.querySelector('sl-dialog')
          ?.getAttribute('label')
      ).to.equal('Deny 1 request?');
      expect(batchCall(), 'denied without confirming').to.be.undefined;

      await agree();
      await waitUntil(() => !!batchCall(), 'no batch call');
      expect(bodyOf(batchCall()!).approved).to.be.false;
    });

    it('keeps only the requests the server refused selected', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(10) }),
        baseRequest({ id: 'ar-2', expires_at: inMinutes(20) }),
      ]);
      batchOutcomes = {
        'ar-2': { id: 'ar-2', ok: false, error: 'Request already expired' },
      };

      await select(element, ['ar-1', 'ar-2']);
      barButton(element, 'approve').click();
      await agree();
      await waitUntil(() => !!batchCall(), 'no batch call');
      await waitUntil(
        () => (element as any).selectedIds.length === 1,
        'the failure was not kept for a retry'
      );

      expect((element as any).selectedIds).to.deep.equal(['ar-2']);
      const toast = document.querySelector('sl-alert[open]');
      expect(toast?.textContent ?? '').to.contain('1 request approved');
      expect(toast?.textContent ?? '').to.contain('Request already expired');
    });

    it('drops a row from the selection when it leaves the page', async () => {
      const element = await renderList([
        baseRequest({
          id: 'ar-1',
          tool_name: 'write_file',
          expires_at: inMinutes(10),
        }),
        baseRequest({
          id: 'ar-2',
          tool_name: 'run_tests',
          expires_at: inMinutes(20),
        }),
      ]);

      await select(element, ['ar-1', 'ar-2']);
      expect((element as any).selectedIds.length).to.equal(2);

      (element as any).searchQuery = 'write_file';
      (element as any).applyFilters();
      await element.updateComplete;

      expect((element as any).selectedIds).to.deep.equal(['ar-1']);
    });

    it('extends the selection with shift and X and clears it with Escape', async () => {
      const element = await renderList([
        baseRequest({ id: 'ar-1', expires_at: inMinutes(5) }),
        baseRequest({ id: 'ar-2', expires_at: inMinutes(10) }),
        baseRequest({ id: 'ar-3', expires_at: inMinutes(15) }),
      ]);

      const press = (key: string, shiftKey = false) => {
        const focused = element.shadowRoot?.querySelector<HTMLElement>(
          '.approval-item[tabindex="0"]'
        ) as HTMLElement;
        focused.dispatchEvent(
          new KeyboardEvent('keydown', {
            key,
            shiftKey,
            bubbles: true,
            composed: true,
          })
        );
        return element.updateComplete;
      };

      await press('j');
      await press('x');
      await press('j');
      await press('j');
      await press('X', true);
      expect((element as any).selectedIds).to.deep.equal([
        'ar-1',
        'ar-2',
        'ar-3',
      ]);

      await press('Escape');
      expect((element as any).selectedIds).to.deep.equal([]);
    });
  });

  describe('configure approvals menu', () => {
    it('sends each item to the tools tab it names', async () => {
      const element = await renderList([baseRequest({ id: 'ar-1' })]);

      const items = Array.from(
        element.shadowRoot!.querySelectorAll('sl-menu-item')
      ).map((item) => ({
        text: (item.textContent || '').replace(/\s+/g, ' ').trim(),
        href: item.getAttribute('data-href'),
      }));

      // Both tools items used to open /console/tools and land on whichever
      // tab was open last, so one of them always looked broken.
      expect(
        items.find((item) => item.text.includes('MCP tool access'))?.href
      ).to.equal('/console/tools?tab=mcp');
      expect(
        items.find((item) => item.text.includes('Native tool approvals'))?.href
      ).to.equal('/console/tools?tab=native');
    });
  });
});
