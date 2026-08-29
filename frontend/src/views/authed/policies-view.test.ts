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
      modelIORules?: unknown[];
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

        if (
          url.includes('/api/v1/policies/model-io-rules') &&
          method === 'GET'
        ) {
          return json({ rules: opts.modelIORules ?? [] });
        }

        if (
          url.endsWith('/api/v1/policies/model-io-rules') &&
          method === 'POST'
        ) {
          const body = init?.body ? JSON.parse(String(init.body)) : {};
          return json(body);
        }

        if (
          url.includes('/api/v1/policies/model-io-rules/') &&
          method === 'PUT'
        ) {
          const body = init?.body ? JSON.parse(String(init.body)) : {};
          return json(body);
        }

        if (url.endsWith('/api/v1/policies/upload') && method === 'POST') {
          return json({
            success: true,
            policy_name: 'imported',
            model_io_rules_applied: 1,
          });
        }

        if (url.includes('/api/v1/policies/export')) {
          return new Response('version: "1.0"\n', {
            status: 200,
            headers: { 'Content-Type': 'application/x-yaml' },
          });
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

  it('renders the Policies header and tabs after load', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(() => !(element as any)._loading, 'still loading');
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Policies');
    expect(element.shadowRoot?.textContent).to.contain('Describe a change');
    expect(element.shadowRoot?.querySelector('sl-tab-group')).to.exist;
  });

  it('shows the empty rules state when nothing is configured', async () => {
    fetchStub = createFetchStub({ tools: [] });
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;

    await waitUntil(() => !(element as any)._loading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('No rules yet');
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

  it('lists an explicit tool rule without rebuilding the tool catalog', async () => {
    fetchStub = createFetchStub({
      tools: [
        {
          ...sampleTool,
          config_id: 'cfg-1',
          approval_workflow_id: 'wf-1',
        },
      ],
    });
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
    expect(element.shadowRoot?.textContent).to.contain('search_issues');
    expect(element.shadowRoot?.textContent).to.not.contain(
      'No tools configured'
    );
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

  it('saves a model.request deny rule from the form and reloads it', async () => {
    const stored: unknown[] = [];
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return json([]);
        }
        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return json([sampleWorkflow]);
        }
        if (url.includes('/api/v1/features')) {
          return json({ plugins: [], features: {} });
        }
        if (url.includes('/api/v1/policies/versions')) {
          return json([]);
        }
        if (url.includes('/api/v1/policies/export')) {
          return new Response('version: "1.0"\n', { status: 200 });
        }
        if (
          url.includes('/api/v1/policies/model-io-rules') &&
          method === 'GET'
        ) {
          return json({ rules: stored });
        }
        if (
          url.endsWith('/api/v1/policies/model-io-rules') &&
          method === 'POST'
        ) {
          const body = init?.body ? JSON.parse(String(init.body)) : {};
          stored.splice(0, stored.length, body);
          return json(body);
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });

    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(() => !(element as any)._loading, 'still loading');

    (element as any).openModelIODialog();
    (element as any)._modelIOForm = {
      ...(element as any)._modelIOForm,
      id: 'deny-pii',
      kind: 'model.request',
      target: 'model.request',
      action: 'deny',
      expression: 'pii.found == true',
      detectPii: true,
    };
    await (element as any).saveModelIORule();
    await waitUntil(
      () => (element as any)._modelIORules?.length === 1,
      'rule did not reload'
    );
    await element.updateComplete;

    expect((element as any)._modelIORules[0].id).to.equal('deny-pii');
    expect((element as any)._modelIORules[0].target).to.equal('model.request');
    expect((element as any)._modelIORules[0].conditions[0].action).to.equal(
      'deny'
    );
    expect(element.shadowRoot?.textContent).to.contain('deny-pii');
  });

  it('shows a YAML-imported model.response require_approval rule in the list', async () => {
    const importedRule = {
      id: 'approve-flagged',
      target: 'model.response',
      enabled: true,
      approval_workflow: 'Standard Approval',
      detectors: { moderation: true },
      conditions: [
        {
          expression: 'moderation.flagged == true',
          action: 'require_approval',
        },
      ],
    };
    const stored: unknown[] = [];
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return json([]);
        }
        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return json([sampleWorkflow]);
        }
        if (url.includes('/api/v1/features')) {
          return json({ plugins: [], features: {} });
        }
        if (url.includes('/api/v1/policies/versions')) {
          return json([]);
        }
        if (url.includes('/api/v1/policies/export')) {
          return new Response('version: "1.0"\n', { status: 200 });
        }
        if (
          url.includes('/api/v1/policies/model-io-rules') &&
          method === 'GET'
        ) {
          return json({ rules: stored });
        }
        if (url.endsWith('/api/v1/policies/upload') && method === 'POST') {
          stored.splice(0, stored.length, importedRule);
          return json({
            success: true,
            policy_name: 'imported',
            model_io_rules_applied: 1,
          });
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });

    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(() => !(element as any)._loading, 'still loading');
    expect((element as any)._modelIORules).to.have.length(0);

    (element as any)._pendingFile = new File(
      ['model_io:\n  - id: approve-flagged\n'],
      'policies.yaml',
      { type: 'application/x-yaml' }
    );
    await (element as any).applyPolicyFile();
    await waitUntil(
      () => (element as any)._modelIORules?.length === 1,
      'imported rule did not appear'
    );
    await element.updateComplete;

    expect((element as any)._modelIORules[0].id).to.equal('approve-flagged');
    expect((element as any)._modelIORules[0].target).to.equal('model.response');
    expect((element as any)._modelIORules[0].conditions[0].action).to.equal(
      'require_approval'
    );
    expect(element.shadowRoot?.textContent).to.contain('approve-flagged');
    expect(element.shadowRoot?.textContent).to.contain('require_approval');
  });

  it('shows a unified YAML diff from Describe a change and Save applies', async () => {
    const current = 'version: "1.0"\nmetadata:\n  name: current';
    const generated = 'version: "1.0"\nmetadata:\n  name: edited';
    let uploaded = false;
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return json([]);
        }
        if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
          return json([]);
        }
        if (url.includes('/api/v1/features')) {
          return json({ plugins: [], features: {} });
        }
        if (url.includes('/api/v1/policies/versions')) {
          return json([]);
        }
        if (url.includes('/api/v1/policies/model-io-rules')) {
          return json({ rules: [] });
        }
        if (url.includes('/api/v1/policies/export')) {
          return new Response(current, { status: 200 });
        }
        if (url.endsWith('/api/v1/policies/generate') && method === 'POST') {
          return json({ yaml: generated, warnings: [] });
        }
        if (url.endsWith('/api/v1/policies/diff') && method === 'POST') {
          return json({ summary: '1 modified', has_changes: true });
        }
        if (url.endsWith('/api/v1/policies/upload') && method === 'POST') {
          uploaded = true;
          return json({ success: true, policy_name: 'edited' });
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });

    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(() => !(element as any)._loading, 'still loading');
    (element as any)._showGenerateDialog = true;
    await element.updateComplete;

    const dialog = element.shadowRoot?.querySelector(
      'policy-generate-dialog'
    ) as any;
    expect(dialog).to.exist;
    dialog._prompt = 'rename the policy';
    dialog.requestUpdate();
    await dialog.updateComplete;
    await dialog._generate();
    await dialog.updateComplete;

    expect(dialog._unifiedDiff).to.contain('--- a/policies.yaml');
    expect(uploaded).to.be.false;

    dialog._discard();
    await dialog.updateComplete;
    expect(uploaded).to.be.false;
    expect(dialog._generatedYaml).to.equal('');

    dialog._prompt = 'rename the policy';
    await dialog._generate();
    await dialog.updateComplete;
    dialog._applyPolicy();
    await waitUntil(() => uploaded, 'Save did not apply');
    expect(uploaded).to.be.true;
  });
});
