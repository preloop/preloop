import { aTimeout, expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../api';
import {
  PHONE_WIDTH,
  createPhoneFrame,
  type EmptyPhoneFrame,
} from '../../test-helpers/phone-frame';
import { resetConfirmDialogForTests } from '../../components/confirm-dialog';
import '../../components/view-header.ts';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import './attention-view';
import type { AttentionView } from './attention-view';

describe('AttentionView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let approvalsResponse: any[];
  let executionsResponse: any[];
  let policiesResponse: any[];
  let dismissalsResponse: any[];
  let dismissalsSupported = true;
  let rejectPolicies = false;
  let permissions: string[] | null = null;
  let agentsResponse: any[];
  let usageByModel: any[];
  let agentDeletes: string[];
  let dismissalWrites: { url: string; method: string; body: any }[];

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    invalidateApiCaches();
    rejectPolicies = false;
    dismissalsSupported = true;
    permissions = null;
    dismissalsResponse = [];
    dismissalWrites = [];
    agentsResponse = [];
    usageByModel = [];
    agentDeletes = [];
    window.location.hash = '';

    approvalsResponse = [
      {
        id: 'approval-1',
        tool_name: 'refund_order',
        status: 'pending',
        requested_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ];
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        end_time: new Date(Date.now() - 3_500_000).toISOString(),
      },
    ];
    policiesResponse = [
      {
        id: 'budget-global',
        subject_type: 'global',
        subject_id: null,
        model_alias: null,
        period: 'monthly',
        hard_limit_usd: 50,
        soft_limit_usd: 40,
        current_spend_usd: 55,
      },
    ];

    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (url.startsWith('/api/v1/attention/dismissals')) {
          if (!dismissalsSupported) {
            return new Response('{"detail":"Not Found"}', { status: 404 });
          }
          const method = (init?.method || 'GET').toUpperCase();
          if (method === 'GET') {
            return json({ items: dismissalsResponse });
          }
          dismissalWrites.push({
            url,
            method,
            body: init?.body ? JSON.parse(String(init.body)) : null,
          });
          if (method === 'DELETE') {
            dismissalsResponse = [];
            return new Response(null, { status: 204 });
          }
          const body = JSON.parse(String(init!.body));
          const record = {
            id: 'dismissal-1',
            item_id: decodeURIComponent(url.split('/').pop()!),
            fingerprint: body.fingerprint,
            reason: body.reason,
            snooze_until: null,
            dismissed_by_user_id: 'user-1',
            dismissed_by_username: 'tester',
            created_at: new Date().toISOString(),
          };
          dismissalsResponse = [record];
          return json(record);
        }
        if (url.startsWith('/api/v1/approval-requests')) {
          return json(approvalsResponse);
        }
        if (url.startsWith('/api/v1/flows/executions')) {
          return json(executionsResponse);
        }
        if (url.startsWith('/api/v1/budget/policies')) {
          return rejectPolicies
            ? new Response('{"detail":"forbidden"}', { status: 403 })
            : json(policiesResponse);
        }
        if (url.startsWith('/api/v1/agents')) {
          if ((init?.method || 'GET').toUpperCase() === 'DELETE') {
            agentDeletes.push(url);
            return json({ message: 'removed' });
          }
          return json({ items: agentsResponse, total: agentsResponse.length });
        }
        if (url.startsWith('/api/v1/runtime-sessions')) {
          return json({ items: [], total: 0 });
        }
        if (url.startsWith('/api/v1/account/gateway-usage/search')) {
          return json({ items: [] });
        }
        if (url.startsWith('/api/v1/account/gateway-usage/summary')) {
          return json({
            total_requests: 0,
            successful_requests: 0,
            failed_requests: 0,
            token_usage: {
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
            },
            estimated_cost: 0,
            requests_by_day: [],
            price_catalog: {
              fetched_at: new Date().toISOString(),
              model_count: 120,
            },
            usage_by_model: usageByModel,
            usage_by_flow: [],
            usage_by_session: [],
          });
        }
        if (url === '/api/v1/features') {
          return json({ features: { billing: true } });
        }
        if (url === '/api/v1/auth/users/me') {
          return json({
            id: 'user-1',
            username: 'tester',
            email: 'tester@example.com',
            permissions,
          });
        }
        return json({ detail: `Unhandled ${url}` });
      });

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
    invalidateApiCaches();
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  const mount = async (): Promise<AttentionView> => {
    const el = (await fixture(
      html`<attention-view></attention-view>`
    )) as AttentionView;
    await waitUntil(
      () => !el.shadowRoot!.querySelector('sl-spinner'),
      'attention view finished loading'
    );
    await el.updateComplete;
    return el;
  };

  const text = (el: AttentionView) =>
    el.shadowRoot!.textContent!.replace(/\s+/g, ' ');

  // A media query answers to the viewport, so the phone layout is only real
  // inside a 390px frame. The frame borrows the stubbed fetch from this
  // window and never opens a socket.
  let phoneFrame: EmptyPhoneFrame | undefined;
  const mountOnPhone = async (): Promise<AttentionView> => {
    phoneFrame = await createPhoneFrame({
      moduleUrl: new URL('./attention-view.ts', import.meta.url).href,
      tagName: 'attention-view',
    });
    const frameWindow = phoneFrame.frameWindow as unknown as {
      fetch: typeof window.fetch;
      WebSocket: unknown;
    };
    frameWindow.fetch = (...args: Parameters<typeof window.fetch>) =>
      window.fetch(...args);
    frameWindow.WebSocket = class {
      close() {}
      send() {}
      addEventListener() {}
      removeEventListener() {}
    };
    const el = phoneFrame.frameDocument.createElement(
      'attention-view'
    ) as AttentionView;
    phoneFrame.frameDocument.body.appendChild(el);
    await waitUntil(
      () => !el.shadowRoot!.querySelector('sl-spinner'),
      'attention view finished loading on the phone'
    );
    await phoneFrame.settle(el);
    await phoneFrame.settle(el);
    return el;
  };

  afterEach(() => {
    phoneFrame?.cleanup();
    phoneFrame = undefined;
  });

  it('groups items into sections with counts and actions', async () => {
    const el = await mount();

    expect(
      el.shadowRoot!.querySelector('view-header')!.getAttribute('headerText')
    ).to.equal('Needs attention');
    const sections = Array.from(
      el.shadowRoot!.querySelectorAll('sl-card[id]')
    ).map((card) => card.id);
    expect(sections).to.eql(['approvals', 'flows', 'budgets']);

    const approvals = el.shadowRoot!.querySelector('#approvals')!;
    expect(approvals.textContent).to.contain('refund_order');
    expect(approvals.querySelector('sl-badge')!.textContent!.trim()).to.equal(
      '1'
    );
    expect(
      approvals.querySelector('.row-action')!.getAttribute('href')
    ).to.equal('/console/approval/approval-1');

    const flows = el.shadowRoot!.querySelector('#flows')!;
    expect(flows.textContent).to.contain('Refund Assistant');

    const budgets = el.shadowRoot!.querySelector('#budgets')!;
    expect(budgets.textContent).to.contain('Hard limit reached');
    expect(
      budgets.querySelector('.severity-dot')!.classList.contains('critical')
    ).to.be.true;
  });

  it('states how long an approval has been waiting, with the date on hover', async () => {
    approvalsResponse = [
      {
        id: 'approval-1',
        tool_name: 'Bash',
        status: 'pending',
        requested_at: new Date(Date.now() - 49 * 24 * 3600_000).toISOString(),
        managed_agent_name: 'Claude Code',
      },
    ];
    const el = await mount();

    const detail = el.shadowRoot!.querySelector('#approvals .row-detail')!;
    expect(detail.textContent!.trim()).to.equal('Claude Code · pending 7w');
    expect(detail.getAttribute('title')).to.not.be.null;
  });

  it('approves an approval from its row', async () => {
    const el = await mount();

    const approve = el.shadowRoot!.querySelector(
      '#approvals .row-approve'
    ) as HTMLElement;
    expect(approve, 'expected an Approve on the row').to.exist;
    approvalsResponse = [];
    approve.click();

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .some((call) =>
            String(call.args[0]).includes(
              '/api/v1/approval-requests/approval-1/approve'
            )
          ),
      'no approve call'
    );
    await waitUntil(
      () => !el.shadowRoot!.querySelector('#approvals'),
      'the decided row stayed on the page'
    );
  });

  it('confirms before it denies from the row', async () => {
    const el = await mount();

    const deny = el.shadowRoot!.querySelector(
      '#approvals .row-deny'
    ) as HTMLElement;
    expect(deny.getAttribute('variant')).to.equal('danger');
    expect(deny.hasAttribute('outline'), 'Deny must be outline').to.be.true;
    deny.click();

    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'no confirm dialog'
    );
    const declined = () =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/decline'));
    expect(declined(), 'denied without confirming').to.be.false;

    (
      document
        .querySelector('confirm-dialog')!
        .shadowRoot!.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
    ).click();
    await waitUntil(declined, 'no decline call');
    resetConfirmDialogForTests();
  });

  it('sends a question to its detail page instead of a yes or no', async () => {
    approvalsResponse = [
      {
        id: 'approval-q',
        tool_name: 'ask_user',
        summary: 'Which branch?',
        is_question: true,
        status: 'pending',
        requested_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ];
    const el = await mount();

    expect(el.shadowRoot!.querySelector('#approvals .row-approve')).to.not
      .exist;
    expect(
      el
        .shadowRoot!.querySelector('#approvals .row-action')!
        .getAttribute('href')
    ).to.equal('/console/approval/approval-q');
  });

  it('shows how long is left to decide, soonest deadline first', async () => {
    const inMinutes = (minutes: number) =>
      new Date(Date.now() + minutes * 60_000).toISOString();
    approvalsResponse = [
      {
        id: 'later',
        tool_name: 'later_tool',
        status: 'pending',
        requested_at: new Date(Date.now() - 60_000).toISOString(),
        expires_at: inMinutes(120),
      },
      {
        id: 'soonest',
        tool_name: 'soonest_tool',
        status: 'pending',
        requested_at: new Date(Date.now() - 600_000).toISOString(),
        expires_at: inMinutes(9),
      },
    ];
    const el = await mount();

    const rows = Array.from(
      el.shadowRoot!.querySelectorAll('#approvals .attention-row')
    );
    expect(rows.map((row) => row.getAttribute('data-item-id'))).to.eql([
      'approval:soonest',
      'approval:later',
    ]);
    const chip = rows[0].querySelector('.expiry-chip')!;
    expect(chip.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
      'expires in 9m'
    );
    expect(chip.getAttribute('variant')).to.equal('warning');
    expect(
      rows[1].querySelector('.expiry-chip')!.getAttribute('variant')
    ).to.equal('neutral');
  });

  it('reads "expired" once the deadline passes under an open page', async () => {
    // The loader drops approvals that are already past their deadline, so
    // this state is reached by a page left open across the deadline: the row
    // must then say "expired", never "expires expired".
    approvalsResponse = [
      {
        id: 'stale',
        tool_name: 'stale_tool',
        status: 'pending',
        requested_at: new Date(Date.now() - 3600_000).toISOString(),
        expires_at: new Date(Date.now() + 300).toISOString(),
      },
    ];
    const el = await mount();
    expect(
      el
        .shadowRoot!.querySelector('#approvals .expiry-chip')!
        .textContent!.replace(/\s+/g, ' ')
        .trim()
    ).to.contain('expires');

    await new Promise((resolve) => setTimeout(resolve, 400));
    el.requestUpdate();
    await el.updateComplete;

    expect(
      el
        .shadowRoot!.querySelector('#approvals .expiry-chip')!
        .textContent!.replace(/\s+/g, ' ')
        .trim()
    ).to.equal('expired');
  });

  it('shows one row per flow with the failure count', async () => {
    const start = (daysAgo: number) =>
      new Date(Date.now() - daysAgo * 24 * 3600_000).toISOString();
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'Pull Request Reviewer',
        status: 'FAILED',
        start_time: start(2),
        end_time: new Date(
          Date.now() - 2 * 24 * 3600_000 + 31_000
        ).toISOString(),
      },
      {
        id: 'execution-2',
        flow_id: 'flow-1',
        flow_name: 'Pull Request Reviewer',
        status: 'FAILED',
        start_time: start(3),
      },
    ];
    const el = await mount();

    const rows = el.shadowRoot!.querySelectorAll('#flows .attention-row');
    expect(rows).to.have.length(1);
    expect(rows[0].textContent).to.contain('2 failed runs in 3d');
    expect(
      el.shadowRoot!.querySelector('#flows sl-button')!.getAttribute('href')
    ).to.equal('/console/flows/executions?flow_id=flow-1&status=FAILED');
    const flowsChip = Array.from(el.shadowRoot!.querySelectorAll('.chip')).find(
      (chip) => chip.textContent!.includes('flow')
    )!;
    expect(flowsChip.textContent!.trim()).to.equal('1 flow');
  });

  it('shows a chip per kind, muted when the kind is empty', async () => {
    const el = await mount();
    const chips = Array.from(el.shadowRoot!.querySelectorAll('.chip')).map(
      (chip) => chip.textContent!.trim()
    );
    expect(chips).to.eql([
      '1 approval',
      '0 agents',
      '1 flow',
      '0 models',
      '1 budget',
      '0 pricing',
    ]);
    const agentsChip = el.shadowRoot!.querySelectorAll('.chip')[1];
    expect(agentsChip.classList.contains('empty')).to.be.true;
  });

  it('opens the limits dialog from a budget row', async () => {
    const el = await mount();
    const configure = Array.from(
      el.shadowRoot!.querySelectorAll('#budgets sl-button')
    ).find((button) => button.textContent!.includes('Configure limits'))!;
    expect(configure).to.exist;

    (configure as HTMLElement).click();
    await el.updateComplete;

    expect(
      el.shadowRoot!.querySelector('budget-limits-dialog')!.hasAttribute('open')
    ).to.be.true;
  });

  it('drops a section when its request fails and keeps the rest', async () => {
    rejectPolicies = true;
    const el = await mount();

    expect(el.shadowRoot!.querySelector('#budgets')).to.not.exist;
    expect(el.shadowRoot!.querySelector('#approvals')).to.exist;
  });

  it('expands a flow row onto the runs behind the count', async () => {
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        end_time: new Date(Date.now() - 3_500_000).toISOString(),
        error_message: 'Failed to start agent Job: (409) Conflict\n  at x.py',
      },
      {
        id: 'execution-2',
        flow_id: 'flow-1',
        flow_name: 'Refund Assistant',
        status: 'FAILED',
        start_time: new Date(Date.now() - 7_200_000).toISOString(),
        error_message: 'Failed to start agent Job: (409) Conflict',
      },
    ];
    const el = await mount();

    // One row in the section: open on arrival, no click needed.
    const row = el.shadowRoot!.querySelector('#flows .attention-row')!;
    expect(
      row.querySelector('.row-toggle')!.getAttribute('aria-expanded')
    ).to.equal('true');
    const evidence = row.querySelector('.row-evidence')!;
    expect(evidence.textContent).to.contain('Most common:');
    expect(evidence.textContent).to.contain('(2 of 2)');
    const runLinks = Array.from(evidence.querySelectorAll('a')).map((link) =>
      link.getAttribute('href')
    );
    expect(runLinks).to.contain('/console/flows/executions/execution-1');
    expect(runLinks).to.contain('/console/flows/executions/execution-2');

    (row.querySelector('.row-toggle') as HTMLElement).click();
    await el.updateComplete;
    expect(row.querySelector('.row-evidence')).to.not.exist;
  });

  it('says what each failed run was about, linked to it (wave 4)', async () => {
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        end_time: new Date(Date.now() - 3_500_000).toISOString(),
        error_message: 'Failed to start agent Job: (409) Conflict',
        trigger_subject: 'spacecode/preloop-ios !17 · Merge Request Updated',
        trigger_subject_url:
          'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17',
      },
      {
        id: 'execution-2',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 7_200_000).toISOString(),
        error_message: 'Failed to start agent Job: (409) Conflict',
      },
    ];
    const el = await mount();

    const evidence = el.shadowRoot!.querySelector('#flows .row-evidence')!;
    const headers = Array.from(evidence.querySelectorAll('th')).map((th) =>
      (th.textContent || '').trim()
    );
    // Subject leads: two failures of one flow differ by what they ran on.
    expect(headers.slice(0, 3)).to.eql(['Subject', 'Started', 'Duration']);

    const rows = Array.from(evidence.querySelectorAll('tbody tr'));
    const first = rows[0].querySelector('.subject-cell')!;
    const link = first.querySelector('a')!;
    expect(link.textContent).to.contain('spacecode/preloop-ios !17');
    expect(link.getAttribute('href')).to.equal(
      'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17'
    );
    expect(link.getAttribute('target')).to.equal('_blank');

    // A run with no derivable subject still gets a handle, in meta register.
    const second = rows[1].querySelector('.subject-cell')!;
    expect(second.textContent!.trim()).to.equal('executio');
    expect(
      second
        .querySelector('.execution-subject')!
        .classList.contains('is-fallback')
    ).to.be.true;
  });

  it('shows a Category column when the server categorises failures', async () => {
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        end_time: new Date(Date.now() - 3_500_000).toISOString(),
        error_message: 'Read timed out',
        failure_category: 'model_transient',
      },
      {
        id: 'execution-2',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 7_200_000).toISOString(),
        failure_category: 'no_confirmation',
      },
    ];
    const el = await mount();

    const row = el.shadowRoot!.querySelector('#flows .attention-row')!;
    // The count says what it is made of before the table is read at all.
    expect(row.querySelector('.row-detail')!.textContent).to.contain(
      '2 failed: 1 model transient, 1 no confirmation'
    );

    const evidence = row.querySelector('.row-evidence')!;
    const headers = Array.from(evidence.querySelectorAll('th')).map((th) =>
      (th.textContent || '').trim()
    );
    expect(headers).to.contain('Category');

    const chips = Array.from(
      evidence.querySelectorAll('.category-cell sl-badge')
    );
    expect(chips.map((chip) => chip.textContent!.trim())).to.eql([
      'Model transient',
      'No confirmation',
    ]);
    // The soft chip recipe, never a second red object beside the pill.
    expect(chips[0].getAttribute('variant')).to.equal('neutral');
    expect(chips[0].classList.contains('solid')).to.be.false;
    expect(
      chips[0].closest('sl-tooltip')!.getAttribute('content')
    ).to.have.length.greaterThan(0);
  });

  it('drops the Category column on a server that does not categorise', async () => {
    const el = await mount();

    const evidence = el.shadowRoot!.querySelector('#flows .row-evidence')!;
    const headers = Array.from(evidence.querySelectorAll('th')).map((th) =>
      (th.textContent || '').trim()
    );
    expect(headers).to.not.contain('Category');
    expect(evidence.querySelector('.category-cell')).to.not.exist;
  });

  it('dismisses a row with a reason and lists it under Dismissed', async () => {
    const el = await mount();

    const dropdown = el.shadowRoot!.querySelector(
      '#flows .dismiss-dropdown'
    ) as HTMLElement;
    expect(dropdown).to.exist;
    const menu = dropdown.querySelector('sl-menu')!;
    menu.dispatchEvent(
      new CustomEvent('sl-select', {
        detail: { item: { value: 'expected' } },
      })
    );
    await waitUntil(
      () => dismissalWrites.length > 0,
      'the dismissal was never written'
    );
    await waitUntil(
      () => !el.shadowRoot!.querySelector('#flows'),
      'the dismissed flow row stayed on the page'
    );

    expect(dismissalWrites[0].method).to.equal('PUT');
    expect(dismissalWrites[0].url).to.contain('flow%3Aflow-1');
    expect(dismissalWrites[0].body.reason).to.equal('expected');
    expect(dismissalWrites[0].body.fingerprint).to.equal('run:execution-1');

    const dismissed = el.shadowRoot!.querySelector('#dismissed')!;
    expect(dismissed.textContent).to.contain('Dismissed (1)');

    // Collapsed by default; the detail only appears once it is opened.
    expect(dismissed.querySelector('.dismissed-row')).to.not.exist;
    (dismissed.querySelector('.dismissed-toggle') as HTMLElement).click();
    await el.updateComplete;
    const row = dismissed.querySelector('.dismissed-row')!;
    expect(row.textContent).to.contain('Expected');
    expect(row.textContent).to.contain('tester');

    const restore = Array.from(row.querySelectorAll('sl-button')).find(
      (button) => button.textContent!.includes('Restore')
    )!;
    (restore as HTMLElement).click();
    await waitUntil(
      () => Boolean(el.shadowRoot!.querySelector('#flows')),
      'the restored flow row never came back'
    );
    expect(dismissalWrites[1].method).to.equal('DELETE');
  });

  it('sends a snooze with the number of days the menu promises', async () => {
    const el = await mount();
    const menu = el.shadowRoot!.querySelector(
      '#flows .dismiss-dropdown sl-menu'
    )!;
    menu.dispatchEvent(
      new CustomEvent('sl-select', { detail: { item: { value: 'snoozed' } } })
    );
    await waitUntil(() => dismissalWrites.length > 0, 'nothing was written');

    expect(dismissalWrites[0].body).to.deep.equal({
      fingerprint: 'run:execution-1',
      reason: 'snoozed',
      snooze_days: 7,
    });
  });

  // Staging runs a build without the endpoint: the inbox still works, it just
  // cannot hide anything.
  it('hides every dismiss control when the endpoint is missing', async () => {
    dismissalsSupported = false;
    const el = await mount();

    expect(el.shadowRoot!.querySelector('.dismiss-dropdown')).to.not.exist;
    expect(el.shadowRoot!.querySelector('#dismissed')).to.not.exist;
    expect(el.shadowRoot!.querySelector('#flows')).to.exist;
  });

  // Every member can read the inbox, but only `manage_agents` may write a
  // dismissal, so a member without it gets no button rather than a 403.
  it('hides the dismiss controls from a member without manage_agents', async () => {
    permissions = ['view_agents', 'view_flows'];
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'flow:flow-1',
        fingerprint: 'run:execution-1',
        reason: 'expected',
        snooze_until: null,
        dismissed_by_user_id: 'user-2',
        dismissed_by_username: 'owner',
        created_at: new Date().toISOString(),
      },
    ];
    const el = await mount();

    expect(el.shadowRoot!.querySelector('.dismiss-dropdown')).to.not.exist;
    // The dismissed list is still readable, it just cannot be undone here.
    const dismissed = el.shadowRoot!.querySelector('#dismissed')!;
    expect(dismissed).to.exist;
    (dismissed.querySelector('.dismissed-toggle') as HTMLElement).click();
    await el.updateComplete;
    const restore = Array.from(dismissed.querySelectorAll('sl-button')).find(
      (button) => button.textContent!.includes('Restore')
    );
    expect(restore).to.be.undefined;
  });

  it('shows the dismiss controls to a member with manage_agents', async () => {
    permissions = ['view_flows', 'manage_agents'];
    const el = await mount();

    expect(el.shadowRoot!.querySelector('#flows .dismiss-dropdown')).to.exist;
  });

  it('never offers to dismiss an approval', async () => {
    const el = await mount();
    expect(el.shadowRoot!.querySelector('#approvals .dismiss-dropdown')).to.not
      .exist;
    expect(el.shadowRoot!.querySelector('#flows .dismiss-dropdown')).to.exist;
  });

  it('opens and highlights the row named in the hash', async () => {
    window.location.hash = '#item-flow%3Aflow-1';
    const el = await mount();
    await el.updateComplete;

    const row = el.shadowRoot!.querySelector(
      '[data-item-id="flow:flow-1"]'
    ) as HTMLElement;
    expect(row).to.exist;
    expect(row.classList.contains('highlighted')).to.be.true;
    expect(
      row.querySelector('.row-toggle')!.getAttribute('aria-expanded')
    ).to.equal('true');
    window.location.hash = '';
  });

  // A custom agent is not something the CLI can find on a machine, so the
  // row offers the two things its owner can actually do.
  it('offers Remove instead of an onboard command for a custom agent', async () => {
    permissions = ['view_flows', 'manage_agents'];
    agentsResponse = [
      {
        id: 'agent-7',
        display_name: 'Researcher',
        agent_kind: 'custom',
        lifecycle_state: 'active',
        onboarding_state: 'incomplete',
        live_validation_status: 'passed',
        total_requests: 0,
        last_seen_at: new Date(Date.now() - 3_600_000).toISOString(),
      },
    ];
    const el = await mount();

    const agents = el.shadowRoot!.querySelector('#agents')!;
    expect(agents.textContent).to.contain('Custom agents are started by you.');
    expect(agents.querySelector('.evidence-command')).to.not.exist;

    const remove = agents.querySelector(
      '[data-testid="remove-agent"]'
    ) as HTMLElement;
    expect(remove).to.exist;
    remove.click();
    await aTimeout(0);

    const dialog = document.body.querySelector('confirm-dialog')!;
    await (dialog as any).updateComplete;
    const confirm = Array.from(
      dialog.shadowRoot!.querySelectorAll('sl-button')
    ).find((button) => button.textContent!.trim() === 'Remove agent')!;
    (confirm as HTMLElement).click();
    await waitUntil(() => agentDeletes.length > 0, 'agent removal requested');

    expect(agentDeletes[0]).to.equal('/api/v1/agents/agent-7');
    resetConfirmDialogForTests();
  });

  it('keeps the onboard command for a CLI agent', async () => {
    permissions = ['view_flows', 'manage_agents'];
    agentsResponse = [
      {
        id: 'agent-8',
        display_name: 'Claude Code',
        agent_kind: 'claude_code',
        lifecycle_state: 'active',
        onboarding_state: 'incomplete',
        live_validation_status: 'passed',
        total_requests: 0,
        last_seen_at: new Date(Date.now() - 3_600_000).toISOString(),
      },
    ];
    const el = await mount();

    const agents = el.shadowRoot!.querySelector('#agents')!;
    expect(agents.textContent).to.contain('preloop agents onboard');
    expect(agents.querySelector('[data-testid="remove-agent"]')).to.not.exist;
  });

  it('shows an all-clear card when nothing needs attention', async () => {
    approvalsResponse = [];
    executionsResponse = [];
    policiesResponse = [];

    const el = await mount();
    expect(text(el)).to.contain('Nothing needs you right now.');
    expect(el.shadowRoot!.querySelector('sl-card[id]')).to.not.exist;
  });

  // Wave 8: a $0 price is a question with one sensible answer, so the row
  // offers that answer directly instead of a menu.
  it('dismisses a zero-priced model in one click', async () => {
    permissions = ['manage_agents'];
    usageByModel = [
      {
        ai_model_id: 'model-2',
        model_alias: 'local/qwen-3-coder',
        provider_name: 'ollama',
        request_count: 12,
        token_usage: {
          prompt_tokens: 4,
          completion_tokens: 4,
          total_tokens: 40,
        },
        estimated_cost: 0,
        unpriced_request_count: 0,
        zero_priced_request_count: 12,
        last_request_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ];
    const el = await mount();

    const row = el.shadowRoot!.querySelector(
      '[data-item-id="pricing:zero-priced"]'
    ) as HTMLElement;
    expect(row, 'zero-priced row').to.exist;
    expect(row.textContent!.replace(/\s+/g, ' ')).to.contain(
      '1 model priced at $0'
    );
    expect(row.querySelector('.severity-dot')!.classList.contains('low')).to.be
      .true;
    expect(row.querySelector('sl-dropdown'), 'no dismiss menu').to.not.exist;

    const quick = row.querySelector(
      '[data-testid="quick-dismiss"]'
    ) as HTMLElement;
    expect(quick.textContent!.trim()).to.equal('Expected');
    quick.click();
    await waitUntil(() => dismissalWrites.length > 0, 'dismissal written');

    expect(dismissalWrites[0].method).to.equal('PUT');
    expect(dismissalWrites[0].url).to.contain('pricing%3Azero-priced');
    expect(dismissalWrites[0].body.reason).to.equal('expected');
  });

  it('drops the log prefix from the error it shows and groups by', async () => {
    // Runners hand the executor a logfmt line: nine of twelve runs in the
    // round-4 review showed nothing but "timestamp=2026-09-03T19:32:45.726Z".
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        error_message:
          'timestamp=2026-09-03T19:32:45.726Z level=error Failed to start agent Job: (409) Conflict',
      },
      {
        id: 'execution-2',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 7_200_000).toISOString(),
        error_message:
          'timestamp=2026-09-03T18:02:11.100Z level=ERROR\nFailed to start agent Job: (409) Conflict\n  at x.py',
      },
    ];
    const el = await mount();

    const evidence = el.shadowRoot!.querySelector('#flows .row-evidence')!;
    const errorCells = Array.from(
      evidence.querySelectorAll('td.error-cell')
    ).map((cell) => cell.textContent!.trim());
    expect(errorCells).to.eql([
      'Failed to start agent Job: (409) Conflict',
      'Failed to start agent Job: (409) Conflict',
    ]);
    // The raw line stays in the title: the console is still the record.
    expect(
      evidence.querySelector('td.error-cell')!.getAttribute('title')
    ).to.contain('timestamp=');

    // Both runs now group as one cause instead of two timestamps.
    const common = evidence.querySelector('.evidence-line')!.textContent!;
    expect(common.replace(/\s+/g, ' ')).to.contain(
      'Most common: Failed to start agent Job: (409) Conflict (2 of 2)'
    );
    expect(common).to.not.contain('timestamp=');
    expect(common).to.not.contain('level=');
  });

  it('keeps the category chip clear of Open run at 390px', async () => {
    executionsResponse = [
      {
        id: 'execution-1',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 3_600_000).toISOString(),
        end_time: new Date(Date.now() - 3_500_000).toISOString(),
        error_message: 'Read timed out',
        failure_category: 'model_transient',
      },
      {
        id: 'execution-2',
        flow_id: 'flow-1',
        flow_name: 'PR Reviewer',
        status: 'FAILED',
        start_time: new Date(Date.now() - 7_200_000).toISOString(),
        error_message: 'Read timed out',
        failure_category: 'model_transient',
      },
    ];
    const el = await mountOnPhone();
    const view = phoneFrame!.frameWindow;

    await waitUntil(
      () => el.shadowRoot!.querySelector('#flows .row-evidence'),
      'flow evidence rendered'
    );
    const evidence = el.shadowRoot!.querySelector('#flows .row-evidence')!;
    const started = evidence.querySelector('td.started-cell') as HTMLElement;
    expect(view.getComputedStyle(started).display).to.equal('none');

    const row = evidence.querySelector('tbody tr') as HTMLElement;
    const chip = row.querySelector('.category-cell sl-badge') as HTMLElement;
    const open = row.querySelector('.open-cell a') as HTMLElement;
    const chipBox = chip.getBoundingClientRect();
    const openBox = open.getBoundingClientRect();
    expect(chipBox.width, 'chip is rendered').to.be.greaterThan(0);
    expect(openBox.width, 'Open run is rendered').to.be.greaterThan(0);
    expect(Math.round(chipBox.right), 'chip clears Open run').to.be.at.most(
      Math.round(openBox.left)
    );
    expect(Math.round(openBox.right)).to.be.at.most(PHONE_WIDTH);
  });

  it('keeps the pricing evidence headers in their own columns at 390px', async () => {
    usageByModel = [
      {
        ai_model_id: 'model-2',
        model_alias: 'openrouter/z-ai/glm-4.6',
        provider_name: 'openrouter',
        request_count: 2100,
        token_usage: {
          prompt_tokens: 40,
          completion_tokens: 40,
          total_tokens: 95_592_073,
        },
        estimated_cost: 0,
        unpriced_request_count: 0,
        zero_priced_request_count: 2100,
        last_request_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ];
    const el = await mountOnPhone();
    const view = phoneFrame!.frameWindow;

    await waitUntil(
      () => el.shadowRoot!.querySelector('.pricing-table'),
      'pricing evidence rendered'
    );
    const table = el.shadowRoot!.querySelector(
      '.pricing-table'
    ) as HTMLTableElement;
    expect(table, 'pricing evidence table').to.exist;

    // Tokens and Last request are on the model page; the phone keeps Model,
    // Requests and the action.
    expect(
      view.getComputedStyle(
        table.querySelector('th.tokens-cell') as HTMLElement
      ).display
    ).to.equal('none');
    expect(
      view.getComputedStyle(
        table.querySelector('th.last-request-cell') as HTMLElement
      ).display
    ).to.equal('none');

    // The provider rides under the alias instead of in a column of its own.
    expect(
      view.getComputedStyle(
        table.querySelector('th.provider-cell') as HTMLElement
      ).display
    ).to.equal('none');
    const stacked = table.querySelector('.stacked-provider') as HTMLElement;
    expect(view.getComputedStyle(stacked).display).to.equal('block');
    expect(stacked.textContent!.trim()).to.equal('openrouter');

    const headers = Array.from(table.querySelectorAll('th')).filter(
      (th) => view.getComputedStyle(th).display !== 'none'
    );
    for (let index = 1; index < headers.length; index += 1) {
      const previous = headers[index - 1].getBoundingClientRect();
      const current = headers[index].getBoundingClientRect();
      expect(
        Math.round(previous.right),
        `${headers[index - 1].textContent!.trim()} overprints ${headers[
          index
        ].textContent!.trim()}`
      ).to.be.at.most(Math.round(current.left));
    }
    const last = headers[headers.length - 1].getBoundingClientRect();
    expect(Math.round(last.right)).to.be.at.most(PHONE_WIDTH);
  });

  it('links a zero-priced model to its pricing editor', async () => {
    usageByModel = [
      {
        ai_model_id: 'model-2',
        model_alias: 'local/qwen-3-coder',
        provider_name: 'ollama',
        request_count: 12,
        token_usage: {
          prompt_tokens: 4,
          completion_tokens: 4,
          total_tokens: 40,
        },
        estimated_cost: 0,
        unpriced_request_count: 0,
        zero_priced_request_count: 12,
        last_request_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ];
    const el = await mount();

    const row = el.shadowRoot!.querySelector(
      '[data-item-id="pricing:zero-priced"]'
    ) as HTMLElement;
    const link = row.querySelector(
      'a[href="/console/ai-models/model-2?pricing=edit"]'
    ) as HTMLAnchorElement;
    expect(link, 'edit price link').to.exist;
    expect(link.textContent!.trim()).to.equal('Edit price');
  });
});
