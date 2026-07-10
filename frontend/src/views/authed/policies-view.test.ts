import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './policies-view';
import type { PoliciesView } from './policies-view';

describe('PoliciesView', () => {
  let fetchStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(
    opts: {
      tools?: unknown[];
      workflows?: unknown[];
      toolsFail?: boolean;
      emptyVersions?: boolean;
    } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          if (opts.toolsFail) {
            return json({ detail: 'boom' }, 500);
          }
          return json(opts.tools ?? []);
        }

        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return json(opts.workflows ?? []);
        }

        if (url.includes('/api/v1/features')) {
          return json({ plugins: [], features: {} });
        }

        if (url.includes('/api/v1/policies/versions')) {
          if (opts.emptyVersions) {
            return json([]);
          }
          return json([
            {
              id: 'ver-1',
              version_number: 1,
              tag: null,
              description: 'Initial snapshot',
              created_at: '2026-06-01T10:00:00Z',
              created_by_username: 'alice',
              is_active: true,
              snapshot_summary: {
                mcp_servers_count: 0,
                tools_count: 1,
                policies_count: 0,
              },
            },
          ]);
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  const sampleTool = {
    name: 'search_issues',
    source: 'builtin',
    source_id: null,
    source_name: 'Built-in',
    is_enabled: true,
    approval_workflow_id: null,
    has_approval_condition: false,
    config_id: null,
  };

  const sampleWorkflow = {
    id: 'wf-1',
    name: 'Standard Approval',
    description: 'Requires one human approval',
    approval_type: 'standard',
    is_default: true,
    approvals_required: 1,
    timeout_seconds: 300,
  };

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('renders the governance header and tabs after load', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(() => !(element as any)._loading, 'still loading');
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Governance');
    expect(element.shadowRoot?.querySelector('sl-tab-group')).to.exist;
  });

  it('shows the empty access state when no tools exist', async () => {
    fetchStub = createFetchStub({ tools: [] });
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(() => !(element as any)._loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('No tools configured');
  });

  it('does not loop fetching versions when the list is empty', async () => {
    // Regression: renderPolicyFilesTab() used to call loadVersions() whenever
    // _versions was empty, so an empty response retriggered the fetch on every
    // render (infinite loop). It must now fetch versions at most once.
    fetchStub = createFetchStub({ emptyVersions: true });
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(() => !(element as any)._loading, 'still loading');
    // Force a few extra render cycles; an unfixed component would refetch here.
    for (let i = 0; i < 3; i++) {
      (element as any).requestUpdate();
      await element.updateComplete;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));

    const versionCalls = fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('/api/v1/policies/versions'));
    expect(versionCalls.length).to.be.lessThan(2);
    expect((element as any)._versionsLoaded).to.be.true;
  });

  it('builds access rules from loaded tools', async () => {
    fetchStub = createFetchStub({ tools: [sampleTool] });
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(
      () => (element as any)._toolAccessRules?.length === 1,
      'tool access rules did not build'
    );
    await element.updateComplete;

    expect((element as any)._toolAccessRules[0].toolName).to.equal(
      'search_issues'
    );
    expect((element as any)._toolAccessRules[0].action).to.equal('allow');
    expect(element.shadowRoot?.textContent).to.contain('search_issues');
  });

  it('loads approval workflows into state', async () => {
    fetchStub = createFetchStub({ workflows: [sampleWorkflow] });
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(
      () => (element as any)._approvalPolicies?.length === 1,
      'workflows did not load'
    );
    await element.updateComplete;

    expect((element as any)._approvalPolicies[0].name).to.equal(
      'Standard Approval'
    );
  });

  it('renders an error alert when loading fails', async () => {
    fetchStub = createFetchStub({ toolsFail: true });
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(
      () => (element as any)._error !== null,
      'error did not appear'
    );
    await element.updateComplete;

    expect((element as any)._error).to.be.a('string');
    expect(element.shadowRoot?.querySelector('sl-alert[variant="danger"]')).to
      .exist;
  });
});
