import { fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import type { LitElement } from 'lit';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { invalidateApiCaches } from '../../api';
import {
  replaceListFilters,
  validFilterDate,
} from '../../utils/list-filter-url';
import './runtime-sessions-view';
import './audit-view';
import './approvals-view';
import './tools-view';

const event = (value: string) => ({ target: { value } });

describe('List filter navigation', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access');
    sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => undefined);
    sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ features: {}, permissions: null }), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
  });
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
    window.history.replaceState({}, '', '/');
  });

  it('keeps unrelated parameters, hash and history state while replacing repeated filters', () => {
    window.history.replaceState(
      { example: true },
      '',
      '/console/audit?event=example&event_type=old#details'
    );
    replaceListFilters({ event_type: ['gateway', 'approval'], outcome: null });
    expect(
      new URLSearchParams(location.search).getAll('event_type')
    ).to.deep.equal(['gateway', 'approval']);
    expect(location.search).to.contain('event=example');
    expect(location.hash).to.equal('#details');
    expect(history.state).to.deep.equal({ example: true });
  });

  it('rejects malformed shared date values before API conversion', () => {
    expect(validFilterDate('2026-02-30')).to.equal('');
    expect(validFilterDate('invalid')).to.equal('');
    expect(validFilterDate('2026-10-09')).to.equal('2026-10-09');
  });

  it('applies Sessions selects and dates immediately and restores a shared URL', async () => {
    const proto = customElements.get('runtime-sessions-view')!.prototype;
    const load = sinon.stub(proto, 'loadSessions').resolves();
    sinon.stub(proto, 'loadFeatureFlags').resolves();
    window.history.replaceState(
      {},
      '',
      '/console/runtime-sessions?sessionId=session-example#detail'
    );
    const element = await fixture<LitElement>(
      document.createElement('runtime-sessions-view')
    );
    const view = element as any;
    load.resetHistory();
    view.handleSessionSourceTypeChange(event('codex'));
    view.handleStatusChange(event('ended'));
    view.handleHasArtifactsChange(event('transcript'));
    await Promise.resolve();
    expect(load.callCount).to.equal(3);
    view.handleRangeChange(event('custom'));
    view.handleStartDateChange(event('2026-10-01'));
    view.handleEndDateChange(event('2026-10-09'));
    const params = new URLSearchParams(location.search);
    expect(params.get('source_type')).to.equal('codex');
    expect(params.get('status')).to.equal('ended');
    expect(params.get('has_artifacts')).to.equal('transcript');
    expect(params.get('from')).to.equal('2026-10-01');
    expect(params.get('to')).to.equal('2026-10-09');
    expect(params.get('sessionId')).to.equal('session-example');
    element.remove();
    const restored = (await fixture<LitElement>(
      document.createElement('runtime-sessions-view')
    )) as any;
    expect(restored.sessionSourceType).to.equal('codex');
    expect(restored.status).to.equal('ended');
    expect(restored.hasArtifacts).to.equal('transcript');
    expect(restored.startDate).to.equal('2026-10-01');
    Object.assign(restored, {
      loading: false,
      sessions: { items: [], total: 0 },
    });
    await restored.updateComplete;
    expect(restored.shadowRoot.querySelector('sl-input[label="Start date"]')).to
      .exist;
    restored.handleRangeChange(event('last-7'));
    await restored.updateComplete;
    expect(
      restored.shadowRoot.querySelector('sl-input[label="Start date"]')
    ).to.equal(null);
    expect(restored.shadowRoot.textContent).to.not.match(/\bApply\b/);
  });

  it('round-trips repeated Audit filters and updates them on Back', async () => {
    const proto = customElements.get('audit-view')!.prototype;
    sinon.stub(proto, '_loadTimeline').resolves();
    sinon.stub(proto, '_loadUsers').resolves();
    window.history.replaceState({}, '', '/console/audit?event=event-example');
    const element = (await fixture<LitElement>(
      document.createElement('audit-view')
    )) as any;
    Object.assign(element, {
      _eventTypeFilters: ['gateway', 'approval'],
      _outcomeFilters: ['denied'],
      _toolNameFilter: 'example',
      _minCost: '1',
      _maxCost: '5',
      _startDate: '2026-10-01',
    });
    element._applyFilters();
    expect(
      new URLSearchParams(location.search).getAll('event_type')
    ).to.deep.equal(['gateway', 'approval']);
    element.remove();
    const restored = (await fixture<LitElement>(
      document.createElement('audit-view')
    )) as any;
    expect(restored._toolNameFilter).to.equal('example');
    expect(restored._minCost).to.equal('1');
    expect(restored._startDate).to.equal('2026-10-01');
    window.history.replaceState({}, '', '/console/audit?outcome=allowed');
    window.dispatchEvent(new PopStateEvent('popstate'));
    expect(restored._eventTypeFilters).to.deep.equal([]);
    expect(restored._outcomeFilters).to.deep.equal(['allowed']);
    restored._clearFilters();
    expect(location.search).to.equal('');
  });

  it('restores Approvals filters and debounces only the search update', async () => {
    const proto = customElements.get('approvals-view')!.prototype;
    sinon.stub(proto, 'loadApprovalRequests').resolves();
    window.history.replaceState(
      {},
      '',
      '/console/approvals?status=pending&tool=example&q=review'
    );
    const element = (await fixture<LitElement>(
      document.createElement('approvals-view')
    )) as any;
    expect(element.statusFilter).to.equal('pending');
    expect(element.searchQuery).to.equal('review');
    const apply = sinon.spy(element, 'applyFilters');
    element.handleStatusFilterChange(event('denied'));
    element.handleToolFilterChange(event('new-example'));
    expect(apply.callCount).to.equal(2);
    element.handleSearchInput(event('updated'));
    element.handleSearchInput(event('updated query'));
    expect(apply.callCount).to.equal(2);
    expect(new URLSearchParams(location.search).get('q')).to.equal(
      'updated query'
    );
    await new Promise((resolve) => setTimeout(resolve, 280));
    expect(apply.callCount).to.equal(3);
    element.remove();
    const restored = (await fixture<LitElement>(
      document.createElement('approvals-view')
    )) as any;
    expect(restored.toolFilter).to.equal('new-example');
    expect(restored.searchQuery).to.equal('updated query');
  });
  it('round-trips both Tools tab filters without discarding deep-link state', async () => {
    const proto = customElements.get('tools-view')!.prototype;
    sinon.stub(proto, 'loadData').resolves();
    history.replaceState(
      { example: true },
      '',
      '/console/tools?tab=native&unrelated=keep#anchor'
    );
    const element = (await fixture<LitElement>(
      document.createElement('tools-view')
    )) as any;
    element._setFilterValues({
      query: 'MCP example',
      statuses: ['enabled'],
      servers: ['server-example'],
      rules: ['none'],
      workflows: ['workflow-example'],
    });
    element._setNativeFilterValues({
      query: 'Native example',
      agents: ['agent-example'],
      rules: ['rules'],
    });
    const params = new URLSearchParams(location.search);
    expect(params.get('mcp_q')).to.equal('MCP example');
    expect(params.getAll('native_agent')).to.deep.equal(['agent-example']);
    expect(params.get('unrelated')).to.equal('keep');
    expect(location.hash).to.equal('#anchor');
    element.remove();
    const restored = (await fixture<LitElement>(
      document.createElement('tools-view')
    )) as any;
    expect(restored.activeTab).to.equal('native');
    expect(restored.filters).to.deep.equal({
      query: 'MCP example',
      statuses: ['enabled'],
      servers: ['server-example'],
      rules: ['none'],
      workflows: ['workflow-example'],
    });
    expect(restored.nativeFilters).to.deep.equal({
      query: 'Native example',
      agents: ['agent-example'],
      rules: ['rules'],
    });
    restored._clearFilters();
    expect(new URLSearchParams(location.search).has('mcp_q')).to.equal(false);
    expect(new URLSearchParams(location.search).get('native_q')).to.equal(
      'Native example'
    );
    history.replaceState({}, '', '/console/tools?tab=mcp&mcp_status=disabled');
    window.dispatchEvent(new PopStateEvent('popstate'));
    expect(restored.activeTab).to.equal('mcp');
    expect(restored.filters.statuses).to.deep.equal(['disabled']);
  });

  for (const initialEmpty of [false, true]) {
    it(`keeps the Tools tab mounted during refresh (initially empty: ${initialEmpty})`, async () => {
      const fetch = window.fetch as sinon.SinonStub;
      let release: ((response: Response) => void) | undefined;
      let refresh = false;
      const tool = {
        name: 'example_tool',
        source: 'builtin',
        source_id: null,
        source_name: 'Built-in',
        description: 'Example',
        schema: {},
        is_enabled: true,
        is_supported: true,
        approval_workflow_id: null,
        has_approval_condition: false,
        config_id: 'config-example',
      };
      fetch.callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/tools/summary') && refresh)
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        const body = url.includes('/tools/summary')
          ? initialEmpty
            ? []
            : [tool]
          : url.includes('/features')
            ? { features: {} }
            : url.includes('/auth/users/me')
              ? { permissions: null }
              : [];
        return new Response(JSON.stringify(body), {
          headers: { 'Content-Type': 'application/json' },
        });
      });
      const element = (await fixture<LitElement>(
        document.createElement('tools-view')
      )) as any;
      const { waitUntil } = await import('@open-wc/testing');
      await waitUntil(() => !element.loading && !element.toolsContextLoading);
      const tabs = element.shadowRoot.querySelector('sl-tab-group');
      expect(tabs).to.exist;
      const column = element.shadowRoot.querySelector(
        '.main-column'
      ) as HTMLElement;
      column.style.height = '50px';
      column.style.overflow = 'auto';
      await new Promise((resolve) => requestAnimationFrame(resolve));
      column.scrollTop = 25;
      const scroll = column.scrollTop;
      expect(scroll).to.be.greaterThan(0);
      refresh = true;
      element.toolsSchemasReady = true;
      const settled = sinon.spy();
      const rejected = sinon.spy();
      const pending = initialEmpty
        ? element.loadData()
        : element._handleSaveRule(
            new CustomEvent('save-rule', {
              detail: {
                tool,
                existingRule: null,
                formData: {
                  action: 'allow',
                  condition_expression: '',
                  condition_type: 'cel',
                  description: '',
                  is_enabled: true,
                },
                resolve: settled,
                reject: rejected,
              },
            })
          );
      await waitUntil(() => !!release);
      await element.updateComplete;
      expect(element.loading).to.equal(false);
      expect(element.refreshing).to.equal(true);
      expect(element.shadowRoot.querySelector('sl-tab-group')).to.equal(tabs);
      expect(column.scrollTop).to.equal(scroll);
      expect(element.shadowRoot.textContent).to.contain('Refreshing tools');
      release!(
        new Response(JSON.stringify([tool]), {
          headers: { 'Content-Type': 'application/json' },
        })
      );
      await pending;
      await element.updateComplete;
      expect(element.refreshing).to.equal(false);
      if (!initialEmpty) {
        expect(settled.calledOnce).to.equal(true);
        expect(rejected.called).to.equal(false);
        expect(
          fetch
            .getCalls()
            .some(
              (call) =>
                call.args[1]?.method === 'POST' &&
                String(call.args[0]).includes('access-rules')
            )
        ).to.equal(true);
      }
      expect(element.shadowRoot.querySelector('sl-tab-group')).to.equal(tabs);
    });
  }
});
