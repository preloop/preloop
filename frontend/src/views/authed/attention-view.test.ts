import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

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
  let dismissalWrites: { url: string; method: string; body: any }[];

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    rejectPolicies = false;
    dismissalsSupported = true;
    dismissalsResponse = [];
    dismissalWrites = [];
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
          return json({ items: [], total: 0 });
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
            usage_by_model: [],
            usage_by_flow: [],
            usage_by_session: [],
          });
        }
        if (url === '/api/v1/features') {
          return json({ features: { billing: true } });
        }
        return json({ detail: `Unhandled ${url}` });
      });

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
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
    expect(approvals.querySelector('sl-button')!.getAttribute('href')).to.equal(
      '/console/approval/approval-1'
    );

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

  it('shows an all-clear card when nothing needs attention', async () => {
    approvalsResponse = [];
    executionsResponse = [];
    policiesResponse = [];

    const el = await mount();
    expect(text(el)).to.contain('Nothing needs you right now.');
    expect(el.shadowRoot!.querySelector('sl-card[id]')).to.not.exist;
  });
});
