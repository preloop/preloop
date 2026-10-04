import { expect, waitUntil } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import sinon from 'sinon';

import './audit-view';
import type { AuditView } from './audit-view';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('AuditView', () => {
  let fetchStub: sinon.SinonStub;
  let wsSubscribeStub: sinon.SinonStub;
  let wsConnectStub: sinon.SinonStub;
  let wsCallback: ((message: any) => void) | null = null;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    wsCallback = null;
    wsSubscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake((_topic: string, cb: (message: any) => void) => {
        wsCallback = cb;
        return () => {
          wsCallback = null;
        };
      });
    wsConnectStub = sinon
      .stub(unifiedWebSocketManager, 'connect')
      .resolves(undefined as any);

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url === '/api/v1/users') {
        return new Response(
          JSON.stringify([
            {
              id: 'user-1',
              username: 'alice',
              email: 'alice@example.com',
              full_name: 'Alice Example',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      if (url.startsWith('/api/v1/audit-logs/grouped?')) {
        return new Response(
          JSON.stringify({
            groups: [
              {
                correlation_id: null,
                outcome: 'created',
                primary_event: {
                  id: 'audit-1',
                  account_id: 'account-1',
                  user_id: 'user-1',
                  action: 'runtime_session_created',
                  resource_type: 'runtime_session',
                  resource_id: 'runtime-session-1',
                  status: 'created',
                  ip_address: null,
                  user_agent: null,
                  timestamp: '2026-03-10T10:00:00Z',
                  details: {
                    runtime_session_id: 'runtime-session-1',
                    session_reference: 'claude-session-42',
                    session_source_type: 'claude_code',
                    session_source_id: 'workspace-42',
                    runtime_principal_name: 'Claude Workspace',
                    api_key_name: 'Claude Workspace Token',
                  },
                },
                sub_events: [],
              },
              {
                correlation_id: 'corr-1',
                outcome: 'approved',
                primary_event: {
                  id: 'audit-2',
                  account_id: 'account-1',
                  user_id: 'user-1',
                  action: 'tool_call',
                  resource_type: 'tool',
                  resource_id: 'search',
                  status: 'executed',
                  ip_address: null,
                  user_agent: null,
                  timestamp: '2026-03-10T10:02:00Z',
                  details: {
                    tool_name: 'search',
                    tool_args: { query: 'deployment risk' },
                    duration_ms: 125,
                    correlation_id: 'corr-1',
                    runtime_session_id: 'runtime-session-1',
                    api_key_name: 'Claude Workspace Token',
                  },
                },
                sub_events: [
                  {
                    id: 'audit-3',
                    action: 'policy_allow',
                    status: 'allow',
                    timestamp: '2026-03-10T10:01:59Z',
                    details: {
                      decision: 'allow',
                      correlation_id: 'corr-1',
                      api_key_name: 'Claude Workspace Token',
                    },
                  },
                ],
              },
              {
                correlation_id: 'corr-pay',
                outcome: 'executed',
                primary_event: {
                  id: 'audit-pay-1',
                  account_id: 'account-1',
                  user_id: 'user-1',
                  action: 'tool_call',
                  resource_type: 'tool',
                  resource_id: 'pay',
                  status: 'executed',
                  ip_address: null,
                  user_agent: null,
                  timestamp: '2026-03-10T10:05:00Z',
                  details: {
                    tool_name: 'pay',
                    tool_args: { amount: 50, to: 'Jill' },
                    correlation_id: 'corr-pay',
                    api_key_name: 'Hermes Token',
                  },
                },
                sub_events: [
                  {
                    id: 'audit-pay-2',
                    action: 'policy_require_approval',
                    status: 'require_approval',
                    timestamp: '2026-03-10T10:05:01Z',
                    details: {
                      decision: 'require_approval',
                      correlation_id: 'corr-pay',
                      rule_description: 'Default Rule',
                    },
                  },
                  {
                    id: 'audit-pay-3',
                    action: 'approval_created',
                    status: 'created',
                    timestamp: '2026-03-10T10:05:02Z',
                    details: {
                      approval_id: 'apr-1',
                      correlation_id: 'corr-pay',
                      tool_name: 'pay',
                      timeout_seconds: 300,
                    },
                  },
                  {
                    id: 'audit-pay-4',
                    action: 'approval_notification_sent',
                    status: 'sent',
                    timestamp: '2026-03-10T10:05:03Z',
                    details: {
                      approval_id: 'apr-1',
                      correlation_id: 'corr-pay',
                      channel: 'email',
                      tool_name: 'pay',
                      sent_count: 1,
                      failed_count: 0,
                      skipped_count: 0,
                      recipient_user_ids: ['user-1'],
                      recipient_count: 1,
                    },
                  },
                  {
                    id: 'audit-pay-5',
                    action: 'approval_notification_sent',
                    status: 'no_devices',
                    timestamp: '2026-03-10T10:05:03Z',
                    details: {
                      approval_id: 'apr-1',
                      correlation_id: 'corr-pay',
                      channel: 'mobile_push',
                      tool_name: 'pay',
                      sent_count: 0,
                      failed_count: 0,
                      recipient_user_ids: ['user-1'],
                      recipient_count: 1,
                    },
                  },
                  {
                    id: 'audit-pay-6',
                    action: 'approval_approved',
                    status: 'approved',
                    timestamp: '2026-03-10T10:05:30Z',
                    details: {
                      approval_id: 'apr-1',
                      correlation_id: 'corr-pay',
                      approver_id: 'user-1',
                      reason: 'Looks fine',
                      tool_name: 'pay',
                    },
                  },
                  {
                    id: 'audit-pay-7',
                    action: 'approval_tool_executed',
                    status: 'executed',
                    timestamp: '2026-03-10T10:05:31Z',
                    details: {
                      approval_id: 'apr-1',
                      correlation_id: 'corr-pay',
                      tool_name: 'pay',
                      duration_ms: 234,
                      result_preview: 'Paid $50 to Jill',
                    },
                  },
                ],
              },
              {
                correlation_id: null,
                outcome: 'budget_denied',
                primary_event: {
                  id: 'audit-4',
                  account_id: 'account-1',
                  user_id: 'user-1',
                  action: 'model_gateway_request',
                  resource_type: 'model_gateway',
                  resource_id: 'openai/gpt-5',
                  status: 'budget_denied',
                  ip_address: null,
                  user_agent: null,
                  timestamp: '2026-03-10T10:03:00Z',
                  details: {
                    endpoint: '/openai/v1/responses',
                    endpoint_kind: 'responses',
                    status_code: 403,
                    requested_model: 'openai/gpt-5',
                    model_alias: 'openai/gpt-5',
                    provider_name: 'openai',
                    gateway_provider: 'preloop',
                    runtime_session_id: 'runtime-session-1',
                    api_key_name: 'Claude Workspace Token',
                    error_detail:
                      'Model gateway budget exceeded: account monthly limit reached',
                    error_type: 'budget_limit_exceeded',
                    budget: { hard_limit_exceeded: true, account_limit_usd: 5 },
                  },
                },
                sub_events: [],
              },
            ],
            total: 4,
            skip: 0,
            limit: 50,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }

      return new Response(
        JSON.stringify({ detail: `Unhandled request: ${url}` }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    });
  });

  afterEach(async () => {
    // Close toasts the Shoelace way. Pulling one out of the DOM leaves its
    // auto-hide timer running, and it throws later inside another test.
    await new Promise((resolve) => requestAnimationFrame(resolve));
    await Promise.all(
      Array.from(document.querySelectorAll('sl-alert')).map((alert) =>
        (alert as any).hide()
      )
    );
    fetchStub.restore();
    wsSubscribeStub.restore();
    wsConnectStub.restore();
    localStorage.clear();
  });

  it('shows a first-run empty state when no audit events exist', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.startsWith('/api/v1/audit-logs/grouped?')) {
        return new Response(
          JSON.stringify({ groups: [], total: 0, skip: 0, limit: 50 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response('[]', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);

    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;

    const content = (element.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(content).to.contain('No audit events yet.');
    expect(content).to.contain(
      'Governed tool calls, approvals, and policy decisions are recorded here'
    );
    expect(content).to.not.contain('matching your filters');

    element.remove();
  });

  it('renders expandable runtime session events and API token attribution', async () => {
    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);

    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;

    const content = element.shadowRoot?.textContent || '';
    expect(
      element.shadowRoot
        ?.querySelector('view-header')
        ?.getAttribute('headerText')
    ).to.equal('Audit Timeline');
    expect(content).to.contain('Runtime session started');
    expect(content).to.contain('search');
    expect(content).to.contain('Alice Example via Claude Workspace Token');

    const rows = Array.from(
      element.shadowRoot?.querySelectorAll('.primary-row') || []
    ) as HTMLElement[];
    rows[0].click();
    await element.updateComplete;

    const expandedContent = element.shadowRoot?.textContent || '';
    expect(expandedContent).to.contain('claude-session-42');
    expect(expandedContent).to.contain('Claude Workspace');
    expect(expandedContent).to.contain('Runtime Session Id');

    document.body.removeChild(element);
  });

  it('shortens the ids in an expanded event and links the ones with a page', async () => {
    const sessionId = '11111111-2222-4333-8444-555555555555';
    const executionId = '99999999-8888-4777-8666-555555555555';
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/users') {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.startsWith('/api/v1/audit-logs/grouped?')) {
        return new Response(
          JSON.stringify({
            groups: [
              {
                correlation_id: null,
                outcome: 'executed',
                primary_event: {
                  id: 'audit-id-1',
                  account_id: 'account-1',
                  user_id: null,
                  action: 'tool_call',
                  resource_type: 'tool',
                  resource_id: 'search',
                  status: 'executed',
                  ip_address: null,
                  user_agent: null,
                  timestamp: '2026-03-10T10:00:00Z',
                  details: {
                    tool_name: 'search',
                    runtime_session_id: sessionId,
                    flow_execution_id: executionId,
                  },
                },
                sub_events: [],
              },
            ],
            total: 1,
            skip: 0,
            limit: 50,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ detail: url }), { status: 500 });
    });

    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);
    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;

    const row = element.shadowRoot?.querySelector(
      '.primary-row'
    ) as HTMLElement;
    row.click();
    await element.updateComplete;

    const ids = Array.from(
      element.shadowRoot?.querySelectorAll('.id-value') || []
    );
    const texts = ids.map((node) => (node.textContent || '').trim());
    // Eight characters is enough to tell two ids apart in one event; the
    // whole thing is in the title and one click from the clipboard.
    expect(texts).to.deep.equal(['11111111', '99999999']);
    expect(
      ids.map((node) =>
        node.querySelector('sl-icon-button')?.getAttribute('label')
      )
    ).to.deep.equal(['Copy runtime session id', 'Copy flow execution id']);

    const links = ids.map((node) => node.querySelector('a'));
    expect(links[0]?.getAttribute('href')).to.equal(
      `/console/runtime-sessions?sessionId=${sessionId}`
    );
    expect(links[0]?.getAttribute('title')).to.equal(sessionId);
    expect(links[1]?.getAttribute('href')).to.equal(
      `/console/flows/executions/${executionId}`
    );

    document.body.removeChild(element);
  });

  describe('filter bar and rows (B-D1)', () => {
    async function mountLoaded() {
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      await element.updateComplete;
      return element;
    }

    it('gives every filter the full width of the bar at 390px', async () => {
      await setViewport({ width: 390, height: 844 });
      const element = await mountLoaded();

      const bar = element.shadowRoot?.querySelector(
        '.filter-bar'
      ) as HTMLElement;
      const controls = Array.from(
        bar.querySelectorAll('sl-input, sl-select')
      ) as HTMLElement[];
      // Search, event type, outcomes, from, to, min $, max $.
      expect(controls.length).to.equal(7);

      const barWidth = bar.getBoundingClientRect().width;
      for (const control of controls) {
        const rect = control.getBoundingClientRect();
        expect(
          Math.abs(rect.width - barWidth),
          `${control.tagName} ${control.getAttribute('type') || ''} is not full width`
        ).to.be.lessThan(2);
        expect(
          Math.abs(rect.left - bar.getBoundingClientRect().left)
        ).to.be.lessThan(2);
      }

      element.remove();
      await setViewport({ width: 1280, height: 800 });
    });

    it('keeps the seven filters on one grid row on desktop', async () => {
      await setViewport({ width: 1280, height: 800 });
      const element = await mountLoaded();

      const bar = element.shadowRoot?.querySelector(
        '.filter-bar'
      ) as HTMLElement;
      const columns = getComputedStyle(bar)
        .gridTemplateColumns.split(' ')
        .filter(Boolean);
      expect(columns.length).to.equal(7);

      // One row: the seven controls share a vertical centre (they are
      // centre-aligned in the grid and a date input is a little taller).
      const centres = (
        Array.from(bar.querySelectorAll('sl-input, sl-select')) as HTMLElement[]
      ).map((control) => {
        const rect = control.getBoundingClientRect();
        return rect.top + rect.height / 2;
      });
      for (const centre of centres) {
        expect(Math.abs(centre - centres[0])).to.be.lessThan(2);
      }

      element.remove();
    });

    it('carries the outcome in a tint chip and leaves the row unruled', async () => {
      const element = await mountLoaded();

      const row = element.shadowRoot?.querySelector(
        '.timeline-group'
      ) as HTMLElement;
      const style = getComputedStyle(row);
      expect(style.borderLeftStyle).to.equal('none');
      expect(parseFloat(style.borderLeftWidth)).to.equal(0);

      const badges = Array.from(
        element.shadowRoot?.querySelectorAll('.timeline-group sl-badge') || []
      );
      expect(badges.length).to.be.greaterThan(0);
      for (const badge of badges) {
        expect(badge.classList.contains('status-chip')).to.equal(true);
        expect(badge.classList.contains('solid')).to.equal(false);
      }

      element.remove();
    });
  });

  describe('the page header and the approval filter (B-D2, B-D3)', () => {
    it('puts the title first and "56.3K events · LIVE" in the header meta', async () => {
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/audit-logs/grouped?')) {
          return new Response(
            JSON.stringify({ groups: [], total: 56316, skip: 0, limit: 50 }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      await element.updateComplete;

      const header = element.shadowRoot?.querySelector('view-header');
      expect(header?.getAttribute('headerText')).to.equal('Audit Timeline');
      // Nothing precedes the title any more: the count and LIVE are meta.
      expect(header?.querySelector('[slot="title-prefix"]')).to.equal(null);

      const meta = header?.querySelector('[slot="meta"]') as HTMLElement;
      expect(meta).to.exist;
      expect((meta.textContent || '').replace(/\s+/g, ' ').trim()).to.equal(
        '56.3K events · LIVE'
      );

      element.remove();
    });

    it('offers an "Approval decisions" event type and asks for the three approval actions', async () => {
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      await element.updateComplete;

      const labels = Array.from(
        element.shadowRoot?.querySelectorAll('sl-select sl-option') || []
      ).map((option) => (option.textContent || '').trim());
      expect(labels).to.contain('Approval decisions');

      const params = (element as any)._timelineParams(0, 50) as URLSearchParams;
      expect(params.getAll('event_type')).to.deep.equal([]);

      (element as any)._eventTypeFilters = ['approval_decision', 'tool_call'];
      const filtered = (element as any)._timelineParams(
        0,
        50
      ) as URLSearchParams;
      expect(filtered.getAll('event_type')).to.deep.equal([
        'approval_approved',
        'approval_denied',
        'approval_expired',
        'tool_call',
      ]);

      element.remove();
    });
  });

  it('renders gateway request failures with readable labels and details', async () => {
    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);

    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;

    const rows = Array.from(
      element.shadowRoot?.querySelectorAll('.primary-row') || []
    ) as HTMLElement[];
    const gatewayRow = rows.find((row) =>
      row.textContent?.includes('budget denied: openai/gpt-5')
    );

    expect(gatewayRow).to.exist;
    expect(element.shadowRoot?.textContent || '').to.contain('Budget Denied');

    gatewayRow?.click();
    await element.updateComplete;

    const expandedContent = element.shadowRoot?.textContent || '';
    expect(expandedContent).to.contain('Status Code');
    expect(expandedContent).to.contain('403');
    expect(expandedContent).to.contain('budget_limit_exceeded');
    expect(expandedContent).to.contain('Claude Workspace Token');

    document.body.removeChild(element);
  });

  it('renders the full approval lifecycle: notifications, decision, execution', async () => {
    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);

    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;

    const rows = Array.from(
      element.shadowRoot?.querySelectorAll('.primary-row') || []
    ) as HTMLElement[];
    const payRow = rows.find((row) => row.textContent?.includes('pay'));
    expect(payRow, 'pay row should be present').to.exist;

    payRow?.click();
    await element.updateComplete;

    const expanded = element.shadowRoot?.textContent || '';
    expect(expanded).to.contain('Policy: Require Approval');
    expect(expanded).to.contain('Default Rule');
    expect(expanded).to.contain('Approval requested for pay');
    expect(expanded).to.contain('Notified via Email');
    expect(expanded).to.contain('1 sent');
    expect(expanded).to.contain('Notified via Mobile push');
    expect(expanded).to.contain('no registered devices');
    expect(expanded).to.contain('Approved by Alice Example');
    expect(expanded).to.contain('Looks fine');
    expect(expanded).to.contain('Tool pay executed successfully');
    expect(expanded).to.contain('Paid $50 to Jill');

    document.body.removeChild(element);
  });

  describe('the ?event= deep link', () => {
    let restoreUrl: () => void;

    function openWith(query: string): AuditView {
      const before = window.location.pathname + window.location.search;
      window.history.replaceState({}, '', `/console/audit${query}`);
      restoreUrl = () => window.history.replaceState({}, '', before);
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      return element;
    }

    afterEach(async () => {
      restoreUrl?.();
    });

    it('expands, marks and holds the event the link asked for', async () => {
      // The id is a sub-event: the row that answers for it is its group.
      const element = openWith('?event=audit-3');
      await waitUntil(() => !(element as any)._loading, 'did not load');
      await element.updateComplete;

      const group = element.shadowRoot?.querySelector(
        '[data-group-key="corr-1"]'
      ) as HTMLElement;
      expect(group, 'the linked group renders').to.exist;
      expect(group.classList.contains('linked')).to.equal(true);
      expect((group.textContent || '').replace(/\s+/g, ' ')).to.contain(
        'Policy: Allow'
      );
      // Nothing else is opened on the operator's behalf.
      expect((element as any)._expandedGroups.size).to.equal(1);
      element.remove();
    });

    it('finds an older event and narrows the range to the day it happened', async () => {
      const older = {
        correlation_id: null,
        outcome: 'executed',
        primary_event: {
          id: 'audit-old',
          account_id: 'account-1',
          user_id: 'user-1',
          action: 'tool_call',
          resource_type: 'tool',
          resource_id: 'deploy',
          status: 'executed',
          ip_address: null,
          user_agent: null,
          timestamp: '2026-03-04T09:30:00Z',
          details: { tool_name: 'deploy', duration_ms: 12 },
        },
        sub_events: [],
      };
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url === '/api/v1/users') {
          return new Response('[]', { status: 200 });
        }
        const body = (groups: unknown[]) =>
          new Response(
            JSON.stringify({
              groups,
              total: groups.length,
              skip: 0,
              limit: 50,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        // The walk back through the timeline finds when it happened.
        if (url.includes('limit=200')) return body([older]);
        // The reload, once the day is known, puts it on the first page.
        if (url.includes('start_date=')) return body([older]);
        return body([]);
      });

      const element = openWith('?event=audit-old');
      await waitUntil(
        () =>
          !!element.shadowRoot?.querySelector('[data-group-key="audit-old"]'),
        'the older event was never reached'
      );
      await element.updateComplete;
      expect((element as any)._startDate).to.equal('2026-03-04');
      expect((element as any)._endDate).to.equal('2026-03-05');
      const group = element.shadowRoot?.querySelector(
        '[data-group-key="audit-old"]'
      ) as HTMLElement;
      expect(group.classList.contains('linked')).to.equal(true);
      element.remove();
    });

    it('says so, once, when the event is nowhere in range', async () => {
      const element = openWith('?event=audit-missing');
      await waitUntil(
        () => !!document.querySelector('sl-alert'),
        'no toast for a missing event'
      );
      expect(document.querySelector('sl-alert')?.textContent).to.contain(
        'Event not in the current range'
      );
      // The page is still a working audit page, not an error page.
      expect(
        element.shadowRoot?.querySelectorAll('.timeline-group').length
      ).to.be.greaterThan(0);
      element.remove();
    });

    it('copies a link that opens the row it was copied from', async () => {
      const written: string[] = [];
      const original = navigator.clipboard;
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
          writeText: async (text: string) => {
            written.push(text);
          },
        },
      });

      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(() => !(element as any)._loading, 'did not load');
      await element.updateComplete;

      const buttons = Array.from(
        element.shadowRoot?.querySelectorAll('.copy-link') || []
      ) as HTMLButtonElement[];
      expect(buttons.length).to.be.greaterThan(0);
      buttons[1].click();
      await element.updateComplete;
      expect(written[0]).to.equal(
        `${window.location.origin}/console/audit?event=audit-2`
      );
      // Copying a link is not opening a row.
      expect((element as any)._expandedGroups.size).to.equal(0);

      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: original,
      });
      element.remove();
    });
  });

  describe('DORA exports (#561)', () => {
    let clickedAnchors: HTMLAnchorElement[];
    let anchorClickStub: sinon.SinonStub;
    let createObjectURLStub: sinon.SinonStub;
    let revokeObjectURLStub: sinon.SinonStub;

    beforeEach(() => {
      clickedAnchors = [];
      // Stop the browser actually navigating on the synthetic download.
      anchorClickStub = sinon
        .stub(HTMLAnchorElement.prototype, 'click')
        .callsFake(function (this: HTMLAnchorElement) {
          clickedAnchors.push(this);
        });
      createObjectURLStub = sinon
        .stub(window.URL, 'createObjectURL')
        .returns('blob:export');
      revokeObjectURLStub = sinon.stub(window.URL, 'revokeObjectURL');
    });

    afterEach(() => {
      anchorClickStub.restore();
      createObjectURLStub.restore();
      revokeObjectURLStub.restore();
    });

    const mountView = async () => {
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      await element.updateComplete;
      return element;
    };

    const exportButtons = (element: AuditView) =>
      Array.from(
        element.shadowRoot?.querySelectorAll(
          '[slot="main-column"] sl-button'
        ) || []
      ) as HTMLElement[];

    it('offers both exports in the header', async () => {
      const element = await mountView();
      const labels = exportButtons(element).map((button) =>
        (button.textContent || '').trim()
      );
      expect(labels).to.deep.equal(['Asset register', 'Incident candidates']);
      element.remove();
    });

    it('downloads the asset register with the filename the server chose', async () => {
      fetchStub
        .withArgs(sinon.match(/^\/api\/v1\/exports\/asset-register/))
        .resolves(
          new Response('record_type,asset_id\r\nagent,a-1\r\n', {
            status: 200,
            headers: {
              'Content-Type': 'text/csv',
              'Content-Disposition':
                'attachment; filename="preloop-asset-register-2026-03-15.csv"',
              'X-Preloop-Export-Sha256': 'abc123def4567890',
            },
          })
        );

      const element = await mountView();
      exportButtons(element)[0].click();

      await waitUntil(
        () => clickedAnchors.length === 1,
        'the register should have been downloaded'
      );
      const requested = fetchStub
        .getCalls()
        .map((call) => call.args[0] as string)
        .find((url) => url.startsWith('/api/v1/exports/asset-register'));
      expect(requested).to.contain('format=csv');
      expect(clickedAnchors[0].download).to.equal(
        'preloop-asset-register-2026-03-15.csv'
      );
      element.remove();
    });

    it('sends the timeline date filter as the incident period', async () => {
      fetchStub
        .withArgs(sinon.match(/^\/api\/v1\/exports\/incident-candidates/))
        .resolves(
          new Response('record_type,occurred_at\r\n', {
            status: 200,
            headers: { 'Content-Type': 'text/csv' },
          })
        );

      const element = await mountView();
      (element as any)._startDate = '2026-01-01';
      (element as any)._endDate = '2026-02-01';
      await element.updateComplete;

      exportButtons(element)[1].click();
      await waitUntil(
        () => clickedAnchors.length === 1,
        'the candidates file should have been downloaded'
      );

      const requested = fetchStub
        .getCalls()
        .map((call) => call.args[0] as string)
        .find((url) => url.startsWith('/api/v1/exports/incident-candidates'));
      expect(requested).to.contain('from=2026-01-01');
      expect(requested).to.contain('to=2026-02-01');
      element.remove();
    });

    it('reports the server error instead of saving an error page', async () => {
      fetchStub
        .withArgs(sinon.match(/^\/api\/v1\/exports\/asset-register/))
        .resolves(
          new Response(
            JSON.stringify({ detail: 'period is longer than 400 days' }),
            {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            }
          )
        );

      const element = await mountView();
      exportButtons(element)[0].click();

      await waitUntil(
        () => !!document.querySelector('sl-alert[variant="danger"]'),
        'a failure toast should appear'
      );
      expect(clickedAnchors.length, 'nothing should be saved').to.equal(0);
      element.remove();
    });
  });

  it('labels policy configuration changes with the policy name', async () => {
    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);
    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    const label = (element as any)._getPrimaryLabel({
      action: 'configuration_change',
      resource_id: 'policy',
      details: {
        config_type: 'policy',
        action: 'version_deleted',
        old_value: { name: 'v2' },
      },
    });
    expect(label).to.equal('Policy version deleted: v2');
    element.remove();
  });

  describe('tool-name search and policy rows (#1136)', () => {
    const groupedCalls = () =>
      fetchStub
        .getCalls()
        .map((c) => String(c.args[0]))
        .filter((u) => u.startsWith('/api/v1/audit-logs/grouped?'));

    it('filters as the user types, after a short pause', async () => {
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      await element.updateComplete;
      const before = groupedCalls().length;

      const input = element.shadowRoot?.querySelector(
        'sl-input[placeholder="Search tool name…"]'
      ) as HTMLInputElement;
      expect(input).to.exist;
      for (const value of ['d', 'de', 'deploy']) {
        input.value = value;
        input.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
      }

      await waitUntil(
        () => groupedCalls().length > before,
        'typing should refetch the timeline without pressing Enter',
        { timeout: 2000 }
      );
      // Wait past the debounce window to make sure the burst was coalesced.
      await new Promise((r) => setTimeout(r, 400));
      const after = groupedCalls().slice(before);
      expect(after).to.have.length(1);
      expect(
        new URL(after[0], 'http://x').searchParams.get('tool_name')
      ).to.equal('deploy');

      element.remove();
    });

    it('drops a pending search when the filters are cleared', async () => {
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      (element as any)._onToolSearchInput('dep');
      (element as any)._clearFilters();
      (element as any)._page = 2;
      await new Promise((r) => setTimeout(r, 400));
      expect((element as any)._page).to.equal(2);
      element.remove();
    });

    it('names the tool on policy deny and approval-required rows', async () => {
      const policyEvent = (id: string, action: string, status: string) => ({
        correlation_id: `corr-${id}`,
        outcome: status,
        primary_event: {
          id,
          account_id: 'account-1',
          user_id: 'user-1',
          action,
          resource_type: 'policy',
          resource_id: 'delete_repo',
          status,
          ip_address: null,
          user_agent: null,
          timestamp: '2026-03-10T10:00:00Z',
          details: {
            tool_name: 'delete_repo',
            decision: status,
            correlation_id: `corr-${id}`,
          },
        },
        sub_events: [],
      });
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/audit-logs/grouped?')) {
          return new Response(
            JSON.stringify({
              groups: [
                policyEvent('deny-1', 'policy_deny', 'deny'),
                policyEvent(
                  'approval-1',
                  'policy_require_approval',
                  'require_approval'
                ),
              ],
              total: 2,
              skip: 0,
              limit: 50,
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(
        () => !(element as any)._loading,
        'Audit view did not finish loading'
      );
      await element.updateComplete;

      const labels = Array.from(
        element.shadowRoot?.querySelectorAll('.primary-label') || []
      ).map((el) => (el.textContent || '').trim());
      expect(labels).to.deep.equal([
        'Denied by policy: delete_repo',
        'Approval required: delete_repo',
      ]);

      element.remove();
    });
  });

  it('subscribes to the audit websocket topic and refreshes on live events', async () => {
    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);

    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;

    expect(wsSubscribeStub.calledOnce, 'subscribe should be called once').to.be
      .true;
    expect(wsSubscribeStub.firstCall.args[0]).to.equal('audit');
    expect(wsConnectStub.calledOnce, 'connect should be called once').to.be
      .true;
    expect(wsCallback, 'callback should have been registered').to.not.be.null;

    const fetchCallsBefore = fetchStub
      .getCalls()
      .filter((c) =>
        (c.args[0] as string).startsWith('/api/v1/audit-logs/grouped?')
      ).length;

    wsCallback?.({
      type: 'audit_event',
      action: 'tool_call',
      status: 'executed',
    });
    await element.updateComplete;

    const liveIndicator = element.shadowRoot?.querySelector('.live-indicator');
    expect(liveIndicator, 'live indicator should render').to.exist;
    expect(
      liveIndicator?.classList.contains('pulsing'),
      'pulse class should be applied immediately on event'
    ).to.be.true;

    await waitUntil(
      () =>
        fetchStub
          .getCalls()
          .filter((c) =>
            (c.args[0] as string).startsWith('/api/v1/audit-logs/grouped?')
          ).length > fetchCallsBefore,
      'audit list should be refetched after live event',
      { timeout: 1500 }
    );

    document.body.removeChild(element);
  });

  describe('live refresh under steady traffic', () => {
    const sleep = (ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, ms));

    const groupsFor = (id: string) => ({
      groups: [
        {
          correlation_id: null,
          outcome: 'created',
          primary_event: {
            id,
            action: 'runtime_session_created',
            status: 'created',
            timestamp: '2026-03-10T10:00:00Z',
            details: {},
          },
          sub_events: [],
        },
      ],
      total: 1,
      skip: 0,
      limit: 50,
    });

    /** A slow grouped endpoint that counts concurrency and numbers answers. */
    const slowGrouped = (
      latencyMs: number,
      labelFor?: (url: string) => string
    ) => {
      const stats = { started: 0, inFlight: 0, maxInFlight: 0 };
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/audit-logs/grouped?')) {
          stats.started += 1;
          const n = stats.started;
          stats.inFlight += 1;
          stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
          await sleep(latencyMs);
          stats.inFlight -= 1;
          const label = labelFor ? labelFor(url) : `answer-${n}`;
          return new Response(JSON.stringify(groupsFor(label)), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      return stats;
    };

    const spinnerShown = (element: AuditView) =>
      !!element.shadowRoot?.querySelector('.loading sl-spinner');
    const rowIds = (element: AuditView) =>
      (element as any)._groups.map((g: any) => g.primary_event.id);

    it('keeps rows on screen and one request in flight while events stream in', async function () {
      this.timeout(20000);
      // The reported loop: a slow endpoint and an event every 600 ms.
      const stats = slowGrouped(1500);
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(() => !(element as any)._loading, 'first load', {
        timeout: 3000,
      });
      await element.updateComplete;
      expect(rowIds(element)).to.deep.equal(['answer-1']);

      let spinnerSamples = 0;
      for (let i = 0; i < 8; i += 1) {
        wsCallback?.({ type: 'audit_event', action: 'model_gateway_request' });
        await sleep(600);
        await element.updateComplete;
        if (spinnerShown(element)) spinnerSamples += 1;
      }

      expect(
        spinnerSamples,
        'live refresh must not replace rows with a spinner'
      ).to.equal(0);
      expect(
        stats.maxInFlight,
        'at most one grouped request in flight'
      ).to.equal(1);
      // 4.8 s of events: a couple of coalesced refreshes, not one per event.
      expect(stats.started).to.be.within(2, 4);

      // The trailing refresh lands and its answer is rendered.
      await waitUntil(
        () =>
          stats.inFlight === 0 &&
          (element as any)._refreshTimer === null &&
          !(element as any)._liveRefreshInFlight,
        'settle',
        { timeout: 8000 }
      );
      await element.updateComplete;
      expect(rowIds(element)).to.deep.equal([`answer-${stats.started}`]);
      expect(spinnerShown(element)).to.equal(false);
      element.remove();
    });

    it('clears a permission error when a live refresh succeeds', async function () {
      this.timeout(10000);
      let calls = 0;
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/audit-logs/grouped?')) {
          calls += 1;
          if (calls === 1) {
            return new Response(JSON.stringify({ detail: 'Forbidden' }), {
              status: 403,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(JSON.stringify(groupsFor('granted')), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(() => (element as any)._permissionError, 'first 403');
      wsCallback?.({ type: 'audit_event', action: 'tool_call' });
      await waitUntil(
        () => !(element as any)._permissionError,
        'live refresh should bring the list back',
        { timeout: 4000 }
      );
      expect(rowIds(element)).to.deep.equal(['granted']);
      element.remove();
    });

    it('does not starve the refresh when events arrive faster than the debounce', async function () {
      this.timeout(10000);
      const stats = slowGrouped(50);
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(() => !(element as any)._loading, 'first load');
      const before = stats.started;
      // Check while the stream is still going, not after it stops.
      for (let i = 0; i < 15; i += 1) {
        wsCallback?.({ type: 'audit_event', action: 'model_gateway_request' });
        await sleep(200);
      }
      expect(
        stats.started,
        'a steady stream still refreshes'
      ).to.be.greaterThan(before);
      element.remove();
    });

    it('drops a live answer for filters the reader has already left', async function () {
      this.timeout(10000);
      // The stale unfiltered answer must land AFTER the filtered one.
      // Equal delays finish in start order, so the filtered row would win
      // even without the generation guard.
      fetchStub.callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/audit-logs/grouped?')) {
          const filtered = url.includes('tool_name=');
          await sleep(filtered ? 40 : 900);
          return new Response(
            JSON.stringify(groupsFor(filtered ? 'filtered' : 'unfiltered')),
            {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      const element = document.createElement('audit-view') as AuditView;
      document.body.appendChild(element);
      await waitUntil(() => !(element as any)._loading, 'first load', {
        timeout: 3000,
      });

      wsCallback?.({ type: 'audit_event', action: 'tool_call' });
      // Wait for the live refresh to start, then change the filter.
      await waitUntil(
        () => (element as any)._liveRefreshInFlight,
        'live start',
        {
          timeout: 3000,
        }
      );
      (element as any)._toolNameFilter = 'deploy';
      (element as any)._page = 0;
      void (element as any)._loadTimeline();

      await waitUntil(
        () =>
          !(element as any)._loading && !(element as any)._liveRefreshInFlight,
        'both settle',
        { timeout: 3000 }
      );
      await sleep(1100);
      expect(rowIds(element)).to.deep.equal(['filtered']);
      element.remove();
    });
  });

  it('marks a row sealed only when the payload already carries chain_seq', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.startsWith('/api/v1/audit-logs/grouped?')) {
        return new Response(
          JSON.stringify({
            groups: [
              {
                correlation_id: null,
                outcome: 'created',
                primary_event: {
                  id: 'sealed-1',
                  action: 'runtime_session_created',
                  status: 'created',
                  timestamp: '2026-03-10T10:00:00Z',
                  details: {},
                  chain_seq: 12,
                },
                sub_events: [],
              },
              {
                correlation_id: null,
                outcome: 'created',
                primary_event: {
                  id: 'open-1',
                  action: 'runtime_session_created',
                  status: 'created',
                  timestamp: '2026-03-10T10:01:00Z',
                  details: {},
                  chain_seq: null,
                },
                sub_events: [],
              },
              {
                correlation_id: null,
                outcome: 'created',
                primary_event: {
                  id: 'plain-1',
                  action: 'runtime_session_created',
                  status: 'created',
                  timestamp: '2026-03-10T10:02:00Z',
                  details: {},
                },
                sub_events: [],
              },
            ],
            total: 3,
            skip: 0,
            limit: 50,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ detail: 'no' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = document.createElement('audit-view') as AuditView;
    document.body.appendChild(element);
    await waitUntil(
      () => !(element as any)._loading,
      'Audit view did not finish loading'
    );
    await element.updateComplete;
    const marks = [
      ...(element.shadowRoot?.querySelectorAll('[data-testid="seal-mark"]') ??
        []),
    ].map((node) => node.textContent?.trim());
    expect(marks).to.deep.equal(['Sealed 12', 'Unsealed']);
    element.remove();
  });
});
