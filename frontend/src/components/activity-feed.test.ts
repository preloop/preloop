import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './activity-feed.ts';
import {
  AUDIT_PAGE_SIZE,
  FEED_CAP,
  feedEventFromAuditGroup,
  feedEventFromRealtime,
} from './activity-feed';
import type { ActivityFeed, AuditGroupLike } from './activity-feed';

const NOW = new Date().toISOString();

function auditGroup(
  action: string,
  overrides: Partial<AuditGroupLike['primary_event']> = {},
  outcome = 'executed'
): AuditGroupLike {
  return {
    correlation_id: null,
    outcome,
    primary_event: {
      id: `audit-${action}-${overrides.id || '1'}`,
      user_id: 'user-1',
      action,
      resource_id: null,
      status: outcome,
      details: {},
      timestamp: NOW,
      ...overrides,
    },
  };
}

/**
 * Answer the feed's startup requests with a fixed timeline.
 *
 * `pages` is the audit timeline in fetch order: one array per page the feed
 * asks for. `users` decides how `/api/v1/users` behaves, since the actor
 * lookup is the other startup request and its failure must not cost rows.
 */
function stubFetch(
  pages: AuditGroupLike[][],
  users: 'ok' | 'reject' | 'forbidden' | 'odd-shape' = 'ok'
): { restore: () => void; urls: string[] } {
  const original = window.fetch;
  const urls: string[] = [];
  let page = 0;
  window.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    urls.push(url);
    if (url.includes('/audit-logs/grouped')) {
      const groups = pages[Math.min(page, pages.length - 1)] || [];
      page += 1;
      return new Response(
        JSON.stringify({
          groups,
          total: groups.length,
          skip: 0,
          limit: 50,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    }
    if (users === 'reject') throw new Error('network down');
    if (users === 'forbidden') {
      return new Response(JSON.stringify({ detail: 'nope' }), { status: 403 });
    }
    const body =
      users === 'odd-shape'
        ? { items: [{ id: 'user-1', username: 'dimo' }] }
        : { users: [{ id: 'user-1', username: 'dimo', full_name: 'Dimo' }] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof window.fetch;
  return {
    restore: () => {
      window.fetch = original;
    },
    urls,
  };
}

/** A successful gateway call: traffic the feed drops, whole pages of it. */
function gatewayNoise(count: number): AuditGroupLike[] {
  return Array.from({ length: count }, (_, index) =>
    auditGroup(
      'model_gateway_request',
      {
        id: `noise-${index}`,
        resource_id: 'deepseek/deepseek-v4-pro',
        status: 'success',
        details: { model_alias: 'deepseek/deepseek-v4-pro', status_code: 200 },
      },
      'success'
    )
  );
}

async function feed(autoload = false): Promise<ActivityFeed> {
  const el = await fixture<ActivityFeed>(html`
    <activity-feed
      .autoload=${autoload}
      .flows=${[{ id: 'flow-1', name: 'Merge Request Reviewer' }]}
      .executions=${[
        {
          id: 'exec-1',
          flow_id: 'flow-1',
          flow_name: 'Merge Request Reviewer',
          status: 'FAILED',
          start_time: NOW,
          trigger_subject: 'spacecode/preloop-ios !17',
          trigger_subject_url:
            'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17',
        },
      ]}
      .budgetPolicies=${[{ period: 'daily', soft_limit_usd: 80, hard_limit_usd: 100 }]}
    ></activity-feed>
  `);
  await el.updateComplete;
  return el;
}

function rowText(el: ActivityFeed): string[] {
  return Array.from(el.shadowRoot!.querySelectorAll('.row')).map((row) =>
    (row.textContent || '').replace(/\s+/g, ' ').trim()
  );
}

describe('activity-feed', () => {
  describe('reading events', () => {
    it('names a failed run and links its subject out', () => {
      const event = feedEventFromRealtime(
        'flow_executions',
        {
          execution_id: 'exec-1',
          flow_id: 'flow-1',
          type: 'status_update',
          timestamp: NOW,
          payload: { status: 'FAILED' },
        },
        {
          flows: [{ id: 'flow-1', name: 'Merge Request Reviewer' }],
          executions: [
            {
              id: 'exec-1',
              trigger_subject: 'spacecode/preloop-ios !17',
              trigger_subject_url: 'https://example.test/mr/17',
            },
          ],
        }
      );
      expect(event?.text).to.equal('Merge Request Reviewer failed');
      expect(event?.tone).to.equal('danger');
      expect(event?.href).to.equal('/console/flows/executions/exec-1');
      expect(event?.subject?.trigger_subject).to.equal(
        'spacecode/preloop-ios !17'
      );
    });

    it('carries the duration of a run that succeeded', () => {
      const end = new Date(Date.now() - 1000);
      const start = new Date(end.getTime() - 187000);
      const event = feedEventFromRealtime('flow_executions', {
        execution_id: 'exec-2',
        type: 'status_update',
        timestamp: end.toISOString(),
        payload: {
          status: 'SUCCEEDED',
          flow_name: 'Pull Request Reviewer',
          start_time: start.toISOString(),
          end_time: end.toISOString(),
        },
      });
      expect(event?.text).to.equal('Pull Request Reviewer succeeded');
      expect(event?.tone).to.equal('success');
      expect(event?.trail).to.equal('3m 7s');
    });

    it('reads sessions, approvals, budgets, gateway failures and agents', () => {
      const session = feedEventFromRealtime('runtime_sessions', {
        type: 'runtime_session_created',
        timestamp: NOW,
        payload: {
          runtime_session_id: 'sess-1',
          runtime_principal_name: 'Hermes',
        },
      });
      expect(session?.text).to.equal('Hermes started a session');
      expect(session?.tone).to.equal('neutral');
      expect(session?.href).to.equal(
        '/console/runtime-sessions?sessionId=sess-1'
      );

      const requested = feedEventFromRealtime('approvals', {
        type: 'approval_created',
        approval_request_id: 'req-1',
        tool_name: 'Bash',
        managed_agent_name: 'Claude Code',
        timestamp: NOW,
      });
      expect(requested?.text).to.equal(
        'Approval requested: Bash (Claude Code)'
      );
      expect(requested?.tone).to.equal('warning');
      expect(requested?.href).to.equal('/console/approval/req-1');

      const approved = feedEventFromRealtime(
        'approvals',
        {
          type: 'approval_approved',
          approval_request_id: 'req-1',
          tool_name: 'Bash',
          approver_id: 'user-1',
          timestamp: NOW,
        },
        { users: [{ id: 'user-1', username: 'dimo' }] }
      );
      expect(approved?.text).to.equal('Approval approved by dimo: Bash');
      expect(approved?.tone).to.equal('success');

      const budget = feedEventFromRealtime(
        'budget_health',
        {
          type: 'budget_health_updated',
          timestamp: NOW,
          payload: {
            budget: { soft_limit_exceeded: true, account_soft_limit_usd: 80 },
          },
        },
        { budgetPolicies: [{ period: 'daily', soft_limit_usd: 80 }] }
      );
      expect(budget?.text).to.equal('Daily budget soft limit reached · $80.00');
      expect(budget?.tone).to.equal('warning');
      expect(budget?.budget).to.equal(true);

      const gateway = feedEventFromRealtime('gateway_activity', {
        type: 'model_gateway_call',
        timestamp: NOW,
        payload: {
          api_usage_id: 'usage-1',
          status_code: 502,
          requested_model: 'deepseek/deepseek-v4-pro',
        },
      });
      expect(gateway?.text).to.equal(
        'Gateway request failed · deepseek/deepseek-v4-pro · 502'
      );
      expect(gateway?.tone).to.equal('danger');
      expect(gateway?.href).to.equal('/console/api-usage');

      const agent = feedEventFromRealtime('managed_agents', {
        type: 'managed_agent_created',
        timestamp: NOW,
        payload: { agent_id: 'agent-1', display_name: 'Hermes' },
      });
      expect(agent?.text).to.equal('Hermes connected');
      expect(agent?.href).to.equal('/console/agents/agent-1');
    });

    it('drops traffic that is not news', () => {
      expect(
        feedEventFromRealtime('gateway_activity', {
          type: 'model_gateway_call',
          payload: { status_code: 200, model_alias: 'gpt-5' },
        })
      ).to.equal(null);
      expect(
        feedEventFromRealtime('runtime_sessions', {
          type: 'runtime_session_updated',
          payload: { runtime_session_id: 'sess-1' },
        })
      ).to.equal(null);
      expect(
        feedEventFromRealtime('managed_agents', {
          type: 'managed_agent_updated',
          payload: { agent_id: 'agent-1' },
        })
      ).to.equal(null);
    });

    it('leads a tool line with the caller and links to that session', () => {
      const event = feedEventFromAuditGroup(
        auditGroup(
          'tool_call',
          {
            id: 't1',
            resource_id: 'get_pull_request',
            details: {
              runtime_principal_name: 'Pull Request Reviewer',
              runtime_session_id: 'sess-7',
              mcp_server_name: 'gitlab',
            },
          },
          'allow'
        )
      );
      expect(event?.text).to.equal(
        'Pull Request Reviewer ran get_pull_request'
      );
      expect(event?.tone).to.equal('neutral');
      expect(event?.href).to.equal(
        '/console/runtime-sessions?sessionId=sess-7'
      );
      expect(event?.kind).to.equal('tool');
      const labels = (event?.fields || []).map((item) => item.label);
      expect(labels).to.include('Tool');
      expect(labels).to.include('Server');
    });

    it('says what happened to a tool call, caller first', () => {
      const shape = (outcome: string) =>
        feedEventFromAuditGroup(
          auditGroup(
            'tool_call',
            {
              id: `t-${outcome}`,
              resource_id: 'Bash',
              details: { managed_agent_name: 'Claude Code' },
            },
            outcome
          )
        );
      expect(shape('failed')?.text).to.equal('Claude Code failed to run Bash');
      expect(shape('deny')?.text).to.equal('Claude Code was blocked from Bash');
      expect(shape('require_approval')?.text).to.equal(
        'Claude Code needs approval for Bash'
      );
    });

    it('never drops an audit event type it has no recipe for', () => {
      const event = feedEventFromAuditGroup(
        auditGroup('api_key_created', { id: 'k1' }, 'success'),
        { users: [{ id: 'user-1', username: 'dimo' }] }
      );
      expect(event?.text).to.equal('API key created by dimo');
      expect(event?.href).to.equal('/console/audit?event_type=api_key_created');

      const odd = feedEventFromAuditGroup(
        auditGroup('widget_frobnicated', { id: 'w1' }, 'success'),
        { users: [{ id: 'user-1', username: 'dimo' }] }
      );
      expect(odd?.text).to.equal('Widget frobnicated · dimo');
    });
  });

  describe('the card', () => {
    it('fills from the audit timeline, newest first', async () => {
      localStorage.setItem('accessToken', 'test-token');
      const { restore } = stubFetch([
        [
          auditGroup('runtime_session_created', {
            id: 'a',
            resource_id: 'sess-9',
            details: { runtime_principal_name: 'Hermes' },
            timestamp: new Date(Date.now() - 600000).toISOString(),
          }),
          auditGroup(
            'tool_call',
            { id: 'b', resource_id: 'Bash', timestamp: NOW },
            'failed'
          ),
        ],
      ]);
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 2, 'timeline rows appear');
      expect(rowText(el)[0]).to.contain('failed to run Bash');
      expect(rowText(el)[1]).to.contain('Hermes started a session');
      restore();
      localStorage.removeItem('accessToken');
    });

    it('pages past a wall of gateway traffic to find the news', async () => {
      // The staging 03:00 bug: the newest audit page was 50 successful
      // gateway calls, which are not news, so the feed read "Nothing yet"
      // while the audit page listed events from a minute before.
      localStorage.setItem('accessToken', 'test-token');
      const { restore, urls } = stubFetch([
        gatewayNoise(AUDIT_PAGE_SIZE),
        [
          auditGroup(
            'tool_call',
            {
              id: 'deep',
              resource_id: 'get_pull_request',
              details: { runtime_principal_name: 'Pull Request Reviewer' },
              timestamp: NOW,
            },
            'allow'
          ),
        ],
      ]);
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 1, 'the news is found');
      expect(rowText(el)[0]).to.contain('get_pull_request');
      const audit = urls.filter((url) => url.includes('/audit-logs/grouped'));
      expect(audit.length).to.be.greaterThan(1);
      expect(audit[1]).to.contain(`skip=${AUDIT_PAGE_SIZE}`);
      restore();
      localStorage.removeItem('accessToken');
    });

    it('keeps paging when a page folds down to one row', async () => {
      // Staging at 03:00: twelve events, all of them the same agent calling
      // the same two tools, so the fill hit its twelve and the rail showed
      // three lines in a 240px box. Rows are what has to be counted.
      localStorage.setItem('accessToken', 'test-token');
      const sameCall = (index: number) =>
        auditGroup(
          'tool_call',
          {
            id: `same-${index}`,
            resource_id: 'update_pull_request',
            details: { runtime_principal_name: 'Pull Request Reviewer' },
            timestamp: new Date(Date.now() - index * 1000).toISOString(),
          },
          'allow'
        );
      const { restore, urls } = stubFetch([
        Array.from({ length: 20 }, (_, index) => sameCall(index)).concat(
          gatewayNoise(AUDIT_PAGE_SIZE - 20)
        ),
        [
          auditGroup('runtime_session_created', {
            id: 'older',
            resource_id: 'sess-9',
            details: { runtime_principal_name: 'Hermes' },
            timestamp: new Date(Date.now() - 90000).toISOString(),
          }),
        ],
      ]);
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 2, 'the older row arrives');
      expect(rowText(el)[0]).to.contain('×20');
      expect(rowText(el)[1]).to.contain('Hermes started a session');
      expect(
        urls.filter((url) => url.includes('/audit-logs/grouped')).length
      ).to.be.greaterThan(1);
      restore();
      localStorage.removeItem('accessToken');
    });

    it('renders rows even when the user lookup fails', async () => {
      localStorage.setItem('accessToken', 'test-token');
      const { restore } = stubFetch(
        [
          [
            auditGroup(
              'tool_call',
              { id: 'x', resource_id: 'Bash', timestamp: NOW },
              'failed'
            ),
            auditGroup('api_key_created', { id: 'k' }, 'success'),
          ],
        ],
        'reject'
      );
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 2, 'rows survive');
      expect(rowText(el).join(' ')).to.contain('failed to run Bash');
      restore();
      localStorage.removeItem('accessToken');
    });

    it('survives a user list in a shape it did not expect', async () => {
      // `{ items: [...] }` used to be stored as-is, and the first `.find`
      // on it threw through the whole fill.
      localStorage.setItem('accessToken', 'test-token');
      const { restore } = stubFetch(
        [[auditGroup('api_key_created', { id: 'k2' }, 'success')]],
        'odd-shape'
      );
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 1, 'the row is there');
      expect(rowText(el)[0]).to.contain('API key created');
      restore();
      localStorage.removeItem('accessToken');
    });

    it('falls back to the newest events when the last day is empty', async () => {
      localStorage.setItem('accessToken', 'test-token');
      const old = new Date(Date.now() - 40 * 3600 * 1000).toISOString();
      const { restore, urls } = stubFetch([
        [],
        [
          auditGroup(
            'runtime_session_created',
            {
              id: 'old',
              resource_id: 'sess-old',
              details: { runtime_principal_name: 'Hermes' },
              timestamp: old,
            },
            'created'
          ),
        ],
      ]);
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 1, 'yesterday still shows');
      expect(rowText(el)[0]).to.contain('Hermes started a session');
      const audit = urls.filter((url) => url.includes('/audit-logs/grouped'));
      expect(audit[0]).to.contain('start_date=');
      expect(audit[1]).to.not.contain('start_date=');
      restore();
      localStorage.removeItem('accessToken');
    });

    it('prepends live rows and ignores the same event twice', async () => {
      const el = await feed();
      const message = {
        execution_id: 'exec-1',
        flow_id: 'flow-1',
        type: 'status_update',
        timestamp: NOW,
        payload: { status: 'FAILED' },
      };
      el.ingest('flow_executions', message);
      el.ingest('flow_executions', message);
      await el.updateComplete;
      expect(rowText(el).length).to.equal(1);
      expect(rowText(el)[0]).to.contain('Merge Request Reviewer failed');
      expect(rowText(el)[0]).to.contain('spacecode/preloop-ios !17');
      // The line opens the row; the run is one click further in, in the body.
      el.shadowRoot!.querySelector<HTMLButtonElement>(
        'button.row-text'
      )!.click();
      await el.updateComplete;
      const open = Array.from(
        el.shadowRoot!.querySelectorAll<HTMLAnchorElement>('.row-actions a')
      );
      expect(open[0].textContent!.trim()).to.equal('Open run');
      expect(open[0].getAttribute('href')).to.equal(
        '/console/flows/executions/exec-1'
      );
    });

    it('keeps the newest 30 rows and no more', async () => {
      const el = await feed();
      for (let index = 0; index < FEED_CAP + 5; index += 1) {
        el.ingest('gateway_activity', {
          type: 'model_gateway_call',
          timestamp: new Date(Date.now() - index * 1000).toISOString(),
          payload: {
            api_usage_id: `usage-${index}`,
            status_code: 500,
            model_alias: `model-${index}`,
          },
        });
      }
      await el.updateComplete;
      const rows = rowText(el);
      expect(rows.length).to.equal(FEED_CAP);
      expect(rows[0]).to.contain('model-0');
      expect(rows.join(' ')).to.not.contain('model-34');
    });

    it('opens the budget dialog instead of navigating', async () => {
      const el = await feed();
      el.ingest('budget_health', {
        type: 'budget_health_updated',
        timestamp: NOW,
        payload: {
          budget: { hard_limit_exceeded: true, account_limit_usd: 100 },
        },
      });
      await el.updateComplete;
      let opened = false;
      el.addEventListener('open-budget-limits', () => (opened = true));
      el.shadowRoot!.querySelector<HTMLButtonElement>(
        'button.row-text'
      )!.click();
      await el.updateComplete;
      const action = el.shadowRoot!.querySelector<HTMLButtonElement>(
        '.row-actions button'
      )!;
      expect(action.textContent!.trim()).to.equal('Change limits');
      action.click();
      expect(opened).to.equal(true);
      expect(rowText(el)[0]).to.contain('Daily budget hard limit reached');
      // A budget line has no audit event of its own to open.
      expect(el.shadowRoot!.querySelector('.row-actions a')).to.equal(null);
    });

    it('expands one row at a time, with the fields for its kind', async () => {
      localStorage.setItem('accessToken', 'test-token');
      const { restore } = stubFetch([
        [
          auditGroup(
            'tool_call',
            {
              id: 'expand-1',
              resource_id: 'Bash',
              details: {
                managed_agent_name: 'Claude Code',
                mcp_server_name: 'shell',
                runtime_session_id: 'sess-12',
                duration_ms: 1400,
              },
              timestamp: NOW,
            },
            'failed'
          ),
          auditGroup(
            'approval_approved',
            {
              id: 'expand-2',
              resource_id: 'req-3',
              details: { tool_name: 'Bash', comment: 'fine by me' },
              timestamp: new Date(Date.now() - 60000).toISOString(),
            },
            'approved'
          ),
        ],
      ]);
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 2, 'two rows');
      const toggles = () =>
        Array.from(
          el.shadowRoot!.querySelectorAll<HTMLButtonElement>('button.row-text')
        );
      expect(
        toggles().every((btn) => btn.getAttribute('aria-expanded') === 'false')
      ).to.equal(true);
      expect(el.shadowRoot!.querySelector('.row-body')).to.equal(null);

      toggles()[0].click();
      await el.updateComplete;
      expect(toggles()[0].getAttribute('aria-expanded')).to.equal('true');
      const body = el.shadowRoot!.querySelector('.row-body')!;
      expect(body.id).to.equal(toggles()[0].getAttribute('aria-controls'));
      const labels = Array.from(body.querySelectorAll('dt')).map((dt) =>
        dt.textContent!.trim()
      );
      expect(labels).to.include('When');
      expect(labels).to.include('Tool');
      expect(labels).to.include('Server');
      expect(labels).to.include('Session');
      const links = Array.from(
        body.querySelectorAll<HTMLAnchorElement>('.row-actions a')
      );
      expect(links.map((link) => link.textContent!.trim())).to.deep.equal([
        'Open session',
        'Open in audit',
      ]);
      expect(links[1].getAttribute('href')).to.equal(
        '/console/audit?event=expand-1'
      );

      // One at a time: opening the approval closes the tool call.
      toggles()[1].click();
      await el.updateComplete;
      expect(el.shadowRoot!.querySelectorAll('.row-body').length).to.equal(1);
      expect(toggles()[0].getAttribute('aria-expanded')).to.equal('false');
      const approval = el.shadowRoot!.querySelector('.row-body')!;
      const approvalLabels = Array.from(approval.querySelectorAll('dt')).map(
        (dt) => dt.textContent!.trim()
      );
      expect(approvalLabels).to.include('Decision');
      expect(approvalLabels).to.include('Comment');
      expect(approval.textContent).to.contain('fine by me');

      // Clicking the open row again closes it.
      toggles()[1].click();
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('.row-body')).to.equal(null);
      restore();
      localStorage.removeItem('accessToken');
    });

    it('folds a run of identical tool lines into one counted row', async () => {
      localStorage.setItem('accessToken', 'test-token');
      const repeated = Array.from({ length: 4 }, (_, index) =>
        auditGroup(
          'tool_call',
          {
            id: `fold-${index}`,
            resource_id: 'update_pull_request',
            details: { runtime_principal_name: 'Pull Request Reviewer' },
            timestamp: new Date(Date.now() - index * 1000).toISOString(),
          },
          'allow'
        )
      );
      const { restore } = stubFetch([
        [
          ...repeated,
          auditGroup(
            'tool_call',
            {
              id: 'other',
              resource_id: 'get_pull_request',
              details: { runtime_principal_name: 'Pull Request Reviewer' },
              timestamp: new Date(Date.now() - 9000).toISOString(),
            },
            'allow'
          ),
        ],
      ]);
      const el = await fixture<ActivityFeed>(
        html`<activity-feed></activity-feed>`
      );
      await waitUntil(() => rowText(el).length === 2, 'four lines become one');
      expect(rowText(el)[0]).to.contain(
        'Pull Request Reviewer ran update_pull_request'
      );
      expect(rowText(el)[0]).to.contain('×4');
      expect(rowText(el)[1]).to.contain('get_pull_request');
      // Expanding a folded row lists every occurrence.
      el.shadowRoot!.querySelector<HTMLButtonElement>(
        'button.row-text'
      )!.click();
      await el.updateComplete;
      expect(el.shadowRoot!.querySelectorAll('.occurrence').length).to.equal(4);
      restore();
      localStorage.removeItem('accessToken');
    });

    it('says so when there is nothing, and always offers the audit page', async () => {
      const el = await feed();
      expect(
        (el.shadowRoot?.querySelector('.empty')?.textContent || '').trim()
      ).to.equal('Nothing yet. Events appear here as agents work.');
      const footer = el.shadowRoot?.querySelector('.footer a');
      expect((footer?.textContent || '').trim()).to.equal('View audit →');
      expect(footer?.getAttribute('href')).to.equal('/console/audit');
    });

    it('scrolls its list instead of growing the card', async () => {
      const el = await feed();
      for (let index = 0; index < FEED_CAP; index += 1) {
        el.ingest('gateway_activity', {
          type: 'model_gateway_call',
          timestamp: new Date(Date.now() - index * 1000).toISOString(),
          payload: {
            api_usage_id: `scroll-${index}`,
            status_code: 500,
            model_alias: `model-${index}`,
          },
        });
      }
      await el.updateComplete;
      const list = el.shadowRoot!.querySelector<HTMLElement>('.rows')!;
      const styles = getComputedStyle(list);
      expect(styles.overflowY).to.equal('auto');
      // The stop is the same 360px floor the rail gives the card, and it
      // does not depend on how wide the window is: a card nobody has
      // bounded stops there whatever the viewport.
      expect(styles.maxHeight).to.equal('360px');
      expect(list.clientHeight).to.be.at.most(360);
      expect(list.scrollHeight).to.be.greaterThan(list.clientHeight);
    });

    it('lets a column that bounds it lift the stop', async () => {
      const el = await feed();
      // What the Overview rail does: it is sticky, stretched and capped at
      // one viewport, so it owns the height and the card's own stop would
      // only leave part of the rail empty.
      el.style.setProperty('--activity-feed-list-max-height', 'none');
      el.style.height = '640px';
      for (let index = 0; index < FEED_CAP; index += 1) {
        el.ingest('gateway_activity', {
          type: 'model_gateway_call',
          timestamp: new Date(Date.now() - index * 1000).toISOString(),
          payload: {
            api_usage_id: `lift-${index}`,
            status_code: 500,
            model_alias: `model-${index}`,
          },
        });
      }
      await el.updateComplete;
      const list = el.shadowRoot!.querySelector<HTMLElement>('.rows')!;
      expect(getComputedStyle(list).maxHeight).to.equal('none');
      // The list takes the height the column gave it, not its own stop, and
      // it is still the one thing that scrolls when the rows outgrow it.
      expect(list.clientHeight).to.be.greaterThan(360);
      expect(getComputedStyle(list).overflowY).to.equal('auto');
    });

    it('holds the read position when a row arrives above it', async () => {
      const el = await feed();
      // The rail hands the card a height; here the test does.
      el.style.height = '200px';
      for (let index = 0; index < 20; index += 1) {
        el.ingest('gateway_activity', {
          type: 'model_gateway_call',
          timestamp: new Date(Date.now() - (index + 10) * 60000).toISOString(),
          payload: {
            api_usage_id: `hold-${index}`,
            status_code: 500,
            model_alias: `model-${index}`,
          },
        });
      }
      await el.updateComplete;
      const list = el.shadowRoot!.querySelector<HTMLElement>('.rows')!;
      list.scrollTop = 60;
      const before = list.scrollTop;
      expect(before).to.equal(60);
      el.ingest('flow_executions', {
        execution_id: 'exec-1',
        flow_id: 'flow-1',
        type: 'status_update',
        timestamp: new Date().toISOString(),
        payload: { status: 'FAILED' },
      });
      await el.updateComplete;
      expect(list.scrollTop).to.be.greaterThan(before);
    });

    it('gives each tone its own dot', async () => {
      const el = await feed();
      el.ingest('flow_executions', {
        execution_id: 'exec-9',
        type: 'status_update',
        timestamp: NOW,
        payload: { status: 'SUCCEEDED', flow_name: 'Nightly sweep' },
      });
      el.ingest('approvals', {
        type: 'approval_created',
        approval_request_id: 'req-2',
        tool_name: 'Bash',
        timestamp: new Date(Date.now() - 1000).toISOString(),
      });
      el.ingest('gateway_activity', {
        type: 'model_gateway_call',
        timestamp: new Date(Date.now() - 2000).toISOString(),
        payload: {
          api_usage_id: 'u-9',
          status_code: 502,
          model_alias: 'gpt-5',
        },
      });
      el.ingest('runtime_sessions', {
        type: 'runtime_session_created',
        timestamp: new Date(Date.now() - 3000).toISOString(),
        payload: {
          runtime_session_id: 's-9',
          runtime_principal_name: 'Hermes',
        },
      });
      await el.updateComplete;
      const tones = Array.from(el.shadowRoot!.querySelectorAll('.dot')).map(
        (dot) => dot.className.replace('dot ', '')
      );
      expect(tones).to.eql(['success', 'warning', 'danger', 'neutral']);
    });
  });
});
