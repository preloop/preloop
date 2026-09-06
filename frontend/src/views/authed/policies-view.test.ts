import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../api';
import '../../components/view-header.ts';
import './policies-view';
import { conditionTypeFor } from './policies-view';
import type { PoliciesView } from './policies-view';

describe('conditionTypeFor', () => {
  it('classifies simple comparisons as simple', () => {
    expect(conditionTypeFor('moderation.flagged == true')).to.equal('simple');
    expect(conditionTypeFor('injection.score > 0.7')).to.equal('simple');
    expect(conditionTypeFor('pii.found != true')).to.equal('simple');
  });

  it('classifies CEL operators and functions as cel', () => {
    expect(conditionTypeFor('!args.enabled')).to.equal('cel');
    expect(conditionTypeFor("args.priority in ['critical','high']")).to.equal(
      'cel'
    );
    expect(conditionTypeFor('args.ok ? true : false')).to.equal('cel');
    expect(conditionTypeFor('args.name.contains("x")')).to.equal('cel');
    expect(conditionTypeFor('a && b')).to.equal('cel');
  });
});

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

        if (
          url.includes('/api/v1/policies/versions/') &&
          url.includes('/rollback') &&
          method === 'POST'
        ) {
          return json({
            success: true,
            message: 'preview',
            preview_only: true,
            changes: {
              summary: '1 change',
              has_changes: true,
              changes: { added: [], removed: [], modified: [] },
            },
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
    // The view now reads the cached profile before it fetches; a permission
    // set must not leak from one case into the next.
    invalidateApiCaches();
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('fetches nothing when the viewer lacks view_policies', async () => {
    // B-P1: the gated view stayed connected behind the shell's
    // permission-denied and still fetched tools, workflows and rules.
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.endsWith('/api/v1/auth/users/me')) {
          return json({
            username: 'viewer',
            email: 'viewer@example.com',
            email_verified: true,
            permissions: ['view_agents'],
          });
        }
        return json({ detail: `Unexpected: ${url}` }, 500);
      });

    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(
      () => (element as any)._permissionDenied === true,
      'permission check did not resolve'
    );
    await element.updateComplete;

    const dataCalls = fetchStub
      .getCalls()
      .map((call) => String(call.args[0]))
      .filter(
        (url) => url.includes('/api/') && !url.endsWith('/api/v1/auth/users/me')
      );
    expect(dataCalls).to.deep.equal([]);
    expect(element.shadowRoot?.querySelector('permission-denied')).to.exist;
    expect(element.shadowRoot?.querySelector('sl-tab-group')).to.equal(null);
  });

  it('loads when the viewer has view_policies', async () => {
    fetchStub = createFetchStub();
    fetchStub
      .withArgs(
        sinon.match((url: unknown) =>
          String(url).endsWith('/api/v1/auth/users/me')
        )
      )
      .callsFake(async () =>
        json({
          username: 'admin',
          email: 'admin@example.com',
          email_verified: true,
          permissions: ['view_policies'],
        })
      );

    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(() => !(element as any)._loading, 'still loading');
    await element.updateComplete;

    expect((element as any)._permissionDenied).to.equal(false);
    expect(element.shadowRoot?.querySelector('sl-tab-group')).to.exist;
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
      ruleType: 'model',
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
    let previewed = false;
    let uploadBeforeDiff = false;
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
          previewed = true;
          return json({
            summary: '1 modified',
            has_changes: true,
            changes: { added: [], removed: [], modified: [] },
          });
        }
        if (url.endsWith('/api/v1/policies/upload') && method === 'POST') {
          if (!previewed) {
            uploadBeforeDiff = true;
          }
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
    await waitUntil(
      () => (element as any)._showDiffDialog === true,
      'Save did not open the diff dialog'
    );
    expect(previewed).to.be.true;
    expect(uploaded).to.be.false;
    expect((element as any)._showGenerateDialog).to.be.true;

    await (element as any).applyPolicyFile();
    await waitUntil(() => uploaded, 'Confirm did not apply');
    expect(uploaded).to.be.true;
    expect(uploadBeforeDiff).to.be.false;
    expect((element as any)._showGenerateDialog).to.be.false;
  });

  describe('YAML tab', () => {
    /**
     * Minimal stub focused on the YAML editor: the export is the seed, and
     * /validate decides whether /upload is ever reached.
     */
    function createYamlStub(
      opts: { valid?: boolean; exportOk?: boolean } = {}
    ) {
      const calls = { uploaded: false, validated: 0, previewed: 0 };
      const stub = sinon
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
          if (url.includes('/api/v1/policies/model-io-rules')) {
            return json({ rules: [] });
          }
          if (url.includes('/api/v1/policies/versions')) {
            return json([]);
          }
          if (url.includes('/api/v1/policies/export')) {
            if (opts.exportOk === false) {
              return json({ detail: 'export unavailable' }, 500);
            }
            return new Response('version: "1.0"\nmetadata:\n  name: live\n', {
              status: 200,
            });
          }
          if (url.endsWith('/api/v1/policies/validate') && method === 'POST') {
            calls.validated += 1;
            if (opts.valid === false) {
              return json({
                is_valid: false,
                errors: [
                  { path: '$.tools[0]', message: 'Unknown action "banish"' },
                ],
                warnings: [],
              });
            }
            return json({ is_valid: true, errors: [], warnings: [] });
          }
          if (url.endsWith('/api/v1/policies/diff') && method === 'POST') {
            calls.previewed += 1;
            return json({
              summary: '1 change',
              has_changes: true,
              changes: {
                added: [],
                removed: [],
                modified: [{ type: 'modified', category: 'tools', name: 'x' }],
              },
            });
          }
          if (url.endsWith('/api/v1/policies/upload') && method === 'POST') {
            calls.uploaded = true;
            return json({ success: true, policy_name: 'live' });
          }
          return json({ detail: `Unhandled: ${method} ${url}` }, 500);
        });
      return { stub, calls };
    }

    it('seeds the editor with the exported policy YAML', async () => {
      const { stub } = createYamlStub();
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      (element as any)._activeTab = 'files';
      await element.updateComplete;

      expect((element as any)._yamlDraft).to.contain('metadata:');
      const editor = element.shadowRoot?.querySelector(
        '[data-testid="policy-yaml-editor"]'
      );
      expect(editor).to.exist;
      expect((editor as any).value).to.contain('name: live');
    });

    it('blocks the save when validation fails and never uploads', async () => {
      const { stub, calls } = createYamlStub({ valid: false });
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      (element as any)._activeTab = 'files';
      (element as any)._onYamlDraftInput('version: "1.0"\ntools: banish\n');
      await (element as any)._saveYamlDraft();
      await element.updateComplete;

      expect(calls.validated).to.equal(1);
      expect(calls.previewed).to.equal(0);
      expect(calls.uploaded).to.be.false;
      expect((element as any)._yamlErrors).to.have.length(1);
      expect((element as any)._yamlErrors[0].message).to.contain('banish');
      expect(element.shadowRoot?.textContent).to.contain('was not applied');
    });

    it('validates then previews a valid draft instead of applying immediately', async () => {
      const { stub, calls } = createYamlStub();
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      (element as any)._activeTab = 'files';
      (element as any)._onYamlDraftInput(
        'version: "1.0"\nmetadata:\n  name: x\n'
      );
      await (element as any)._saveYamlDraft();
      await element.updateComplete;

      expect(calls.validated).to.equal(1);
      expect(calls.previewed).to.equal(1);
      expect(calls.uploaded).to.be.false;
      expect((element as any)._yamlErrors).to.have.length(0);
      expect((element as any)._showDiffDialog).to.be.true;
      expect((element as any)._yamlDirty).to.be.true;

      await (element as any).applyPolicyFile();
      await element.updateComplete;

      expect(calls.uploaded).to.be.true;
      expect((element as any)._showDiffDialog).to.be.false;
      expect((element as any)._yamlDirty).to.be.false;
      expect((element as any)._yamlNotice).to.contain('saved and applied');
    });

    it('refuses to save an empty draft without calling the API', async () => {
      const { stub, calls } = createYamlStub();
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      (element as any)._onYamlDraftInput('   ');
      await (element as any)._saveYamlDraft();

      expect(calls.validated).to.equal(0);
      expect(calls.previewed).to.equal(0);
      expect(calls.uploaded).to.be.false;
      expect((element as any)._yamlErrors[0].message).to.contain('empty');
    });

    it('does not upload when the save diff is cancelled', async () => {
      const { stub, calls } = createYamlStub();
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      (element as any)._onYamlDraftInput(
        'version: "1.0"\nmetadata:\n  name: x\n'
      );
      await (element as any)._saveYamlDraft();
      await element.updateComplete;

      expect((element as any)._showDiffDialog).to.be.true;
      (element as any)._cancelDiffPreview();
      await element.updateComplete;

      expect(calls.previewed).to.equal(1);
      expect(calls.uploaded).to.be.false;
      expect((element as any)._showDiffDialog).to.be.false;
      expect((element as any)._yamlDirty).to.be.true;
    });

    it('clears the yaml-save flag when the diff dialog is dismissed', async () => {
      const { stub, calls } = createYamlStub();
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      (element as any)._onYamlDraftInput(
        'version: "1.0"\nmetadata:\n  name: x\n'
      );
      await (element as any)._saveYamlDraft();
      await element.updateComplete;

      expect((element as any)._pendingYamlSave).to.equal(true);
      expect((element as any)._showDiffDialog).to.be.true;

      const dialog = element.shadowRoot?.querySelector(
        'sl-dialog[label="Preview Policy Changes"]'
      ) as HTMLElement;
      dialog.dispatchEvent(
        new CustomEvent('sl-request-close', {
          bubbles: true,
          composed: true,
        })
      );
      await element.updateComplete;

      expect((element as any)._pendingYamlSave).to.equal(false);
      expect((element as any)._showDiffDialog).to.be.false;
      expect(calls.uploaded).to.be.false;
    });

    it('opens Describe a change with a freshly refetched export', async () => {
      const { stub } = createYamlStub();
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      const before = stub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/policies/export')).length;

      await (element as any)._openGenerateDialog();
      await element.updateComplete;

      const after = stub
        .getCalls()
        .filter((c) => String(c.args[0]).includes('/policies/export')).length;
      expect(after).to.be.greaterThan(before);
      expect((element as any)._showGenerateDialog).to.be.true;

      const dialog = element.shadowRoot?.querySelector(
        'policy-generate-dialog'
      ) as any;
      expect(dialog.currentYaml).to.contain('name: live');
    });

    it('surfaces an error instead of opening the dialog when the export fails', async () => {
      const { stub } = createYamlStub({ exportOk: false });
      fetchStub = stub;
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');

      await (element as any)._openGenerateDialog();
      await element.updateComplete;

      expect((element as any)._showGenerateDialog).to.be.false;
      expect((element as any)._error).to.contain('Could not load');
    });
  });

  describe('Add rule dialog', () => {
    async function mountWithDialog() {
      fetchStub = createFetchStub({ tools: [sampleTool] });
      const element = (await fixture(
        html`<policies-view></policies-view>`
      )) as PoliciesView;
      await waitUntil(() => !(element as any)._loading, 'still loading');
      (element as any).openModelIODialog();
      await element.updateComplete;
      return element;
    }

    it('encodes tool option values so names with spaces stay intact', async () => {
      const element = await mountWithDialog();
      (element as any)._patchModelIOForm({ ruleType: 'tool' });
      await element.updateComplete;
      const option = element.shadowRoot?.querySelector(
        '[data-testid="rule-dialog"] sl-option'
      );
      expect(option?.getAttribute('value')).to.equal(
        encodeURIComponent('search_issues')
      );
    });

    it('shows Manage workflows when the action is require_approval', async () => {
      const element = await mountWithDialog();
      (element as any)._patchModelIOForm({ action: 'require_approval' });
      await element.updateComplete;

      expect(element.shadowRoot?.textContent).to.contain('Manage workflows');
    });

    it('stays open when an inner select hides', async () => {
      const element = await mountWithDialog();

      const dialog = element.shadowRoot?.querySelector(
        '[data-testid="rule-dialog"]'
      ) as HTMLElement;
      expect(dialog).to.exist;
      expect((element as any)._showModelIODialog).to.be.true;

      // Regression: the dialog used to listen for sl-hide, which every inner
      // sl-select emits when its dropdown closes.
      const select = element.shadowRoot?.querySelector('sl-select');
      expect(select).to.exist;
      select?.dispatchEvent(
        new CustomEvent('sl-hide', { bubbles: true, composed: true })
      );
      await element.updateComplete;

      expect((element as any)._showModelIODialog).to.be.true;
    });

    it('ignores an overlay dismissal but honours Cancel', async () => {
      const element = await mountWithDialog();

      const overlayClose = new CustomEvent('sl-request-close', {
        detail: { source: 'overlay' },
        cancelable: true,
      });
      (element as any)._handleModelIORequestClose(overlayClose);
      expect(overlayClose.defaultPrevented).to.be.true;
      expect((element as any)._showModelIODialog).to.be.true;

      (element as any)._handleModelIORequestClose(
        new CustomEvent('sl-request-close', {
          detail: { source: 'close-button' },
          cancelable: true,
        })
      );
      expect((element as any)._showModelIODialog).to.be.false;
    });

    it('offers tool and model rule types with request and response sides', async () => {
      const element = await mountWithDialog();

      const ruleType = element.shadowRoot?.querySelector(
        '[data-testid="rule-type"]'
      );
      expect(ruleType?.textContent).to.contain('A tool call');
      expect(ruleType?.textContent).to.contain('Model text');

      const target = element.shadowRoot?.querySelector(
        '[data-testid="rule-target"]'
      );
      expect(target?.textContent).to.contain('the prompt, before it reaches');
      expect(target?.textContent).to.contain('the completion, after the');

      (element as any)._patchModelIOForm({ ruleType: 'tool' });
      await element.updateComplete;
      expect(element.shadowRoot?.querySelector('[data-testid="rule-target"]'))
        .to.not.exist;
    });

    it('explains what each detector produces', async () => {
      const element = await mountWithDialog();

      const text = element.shadowRoot?.textContent ?? '';
      expect(text).to.contain('Detectors scan the text and produce facts');
      expect(text).to.contain('pii.types_found');
      expect(text).to.contain('injection.matched_patterns');
      expect(text).to.contain('moderation.categories');
    });

    it('warns when the condition reads a detector that is switched off', async () => {
      const element = await mountWithDialog();

      (element as any)._patchModelIOForm({
        conditionMode: 'custom',
        expression: 'injection.score > 0.7',
        detectInjection: false,
      });
      await element.updateComplete;

      const warnings = element.shadowRoot?.querySelector(
        '[data-testid="rule-warnings"]'
      );
      expect(warnings?.textContent).to.contain('injection.*');
    });

    it('maps a preset to its detector, condition, and action', async () => {
      const element = await mountWithDialog();

      (element as any)._applyPreset('flag-injection');
      await element.updateComplete;

      const rule = (element as any).buildModelIORuleFromForm();
      expect(rule.id).to.equal('flag-injection');
      expect(rule.target).to.equal('model.request');
      expect(rule.detectors.injection).to.equal(true);
      expect(rule.detectors.pii).to.be.undefined;
      expect(rule.conditions[0].expression).to.equal('injection.score > 0.7');
      expect(rule.conditions[0].action).to.equal('require_approval');

      (element as any)._applyPreset('block-flagged-completions');
      await element.updateComplete;
      const responseRule = (element as any).buildModelIORuleFromForm();
      expect(responseRule.target).to.equal('model.response');
      expect(responseRule.detectors.moderation).to.equal(true);
      expect(responseRule.conditions[0].expression).to.equal(
        'moderation.flagged == true'
      );
      expect(responseRule.conditions[0].action).to.equal('deny');
    });

    it('re-applies the selected preset when switching back from a custom expression', async () => {
      const element = await mountWithDialog();

      (element as any)._applyPreset('flag-injection');
      await element.updateComplete;
      (element as any)._patchModelIOForm({
        conditionMode: 'custom',
        expression: 'injection.score > 0.99',
        detectPii: true,
        detectInjection: false,
      });
      await element.updateComplete;

      expect((element as any)._modelIOForm.presetId).to.equal('flag-injection');
      expect((element as any)._modelIOForm.expression).to.equal(
        'injection.score > 0.99'
      );

      (element as any)._setConditionMode('preset');
      await element.updateComplete;

      const form = (element as any)._modelIOForm;
      expect(form.conditionMode).to.equal('preset');
      expect(form.presetId).to.equal('flag-injection');
      expect(form.expression).to.equal('injection.score > 0.7');
      expect(form.action).to.equal('require_approval');
      expect(form.target).to.equal('model.request');
      expect(form.detectInjection).to.be.true;
      expect(form.detectPii).to.be.false;
      expect(form.detectModeration).to.be.false;
    });

    it('refuses to save a deny rule with no condition', async () => {
      const element = await mountWithDialog();

      (element as any)._patchModelIOForm({
        id: 'deny-everything',
        action: 'deny',
        expression: '   ',
      });
      await (element as any).saveModelIORule();

      expect((element as any)._ruleDialogError).to.contain('needs a condition');
      expect((element as any)._showModelIODialog).to.be.true;
      const posted = fetchStub
        .getCalls()
        .filter(
          (c) =>
            String(c.args[0]).endsWith('/api/v1/policies/model-io-rules') &&
            (c.args[1] as RequestInit | undefined)?.method === 'POST'
        );
      expect(posted).to.have.length(0);
    });
  });

  it('View Diff opens the preview without a Confirm Rollback button', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(() => !(element as any)._loading, 'still loading');
    (element as any)._activeTab = 'files';
    await (element as any).loadVersions();
    await element.updateComplete;

    const version = {
      id: 'ver-2',
      version_number: 2,
      tag: null,
      description: 'Older',
      created_at: '2026-05-01T10:00:00Z',
      created_by_username: 'alice',
      is_active: false,
      snapshot_summary: {
        mcp_servers_count: 0,
        tools_count: 1,
        policies_count: 0,
      },
    };
    await (element as any).openRollbackPreview(version, false);
    await waitUntil(
      () => (element as any)._showRollbackDialog === true,
      'diff dialog did not open'
    );
    await element.updateComplete;

    const dialog = element.shadowRoot?.querySelector(
      'sl-dialog[label="Version Diff"]'
    );
    expect(dialog).to.exist;
    expect(dialog?.textContent).to.not.contain('Confirm Rollback');
    expect((element as any)._rollbackConfirmVisible).to.be.false;
  });
});
