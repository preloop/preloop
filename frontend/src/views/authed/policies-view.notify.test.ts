import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../api';
import '../../components/view-header.ts';
import './policies-view';
import type { PoliciesView } from './policies-view';

/** The rule dialog's `notify` action (#959): model text rules only. */
describe('PoliciesView notify action', () => {
  let fetchStub: sinon.SinonStub;
  let stored: unknown[];

  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    invalidateApiCaches();
    stored = [];
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.endsWith('/api/v1/tools') && method === 'GET') {
          return json([
            {
              name: 'search_issues',
              source: 'builtin',
              source_id: null,
              source_name: 'Built-in',
              is_enabled: true,
              approval_workflow_id: null,
              has_approval_condition: false,
              config_id: null,
            },
          ]);
        }
        if (url.endsWith('/api/v1/approval-workflows')) {
          return json([]);
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
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  async function mountWithDialog(): Promise<PoliciesView> {
    const element = (await fixture(
      html`<policies-view></policies-view>`
    )) as PoliciesView;
    await waitUntil(() => !(element as any)._loading, 'still loading');
    (element as any).openModelIODialog();
    await element.updateComplete;
    return element;
  }

  const notifyOption = (element: PoliciesView) =>
    element.shadowRoot?.querySelector(
      '[data-testid="rule-dialog"] sl-option[value="notify"]'
    );

  it('offers Notify for model text rules and explains it', async () => {
    const element = await mountWithDialog();
    expect(notifyOption(element)).to.exist;

    (element as any)._patchModelIOForm({ action: 'notify' });
    await element.updateComplete;

    const hint = element.shadowRoot?.querySelector(
      '[data-testid="notify-hint"]'
    );
    expect(hint?.textContent).to.contain('goes through unchanged');
    // No workflow picker: notify needs none.
    expect(element.shadowRoot?.textContent).to.not.contain(
      'Create one before you can'
    );
  });

  it('hides Notify for tool rules and drops it when switching to a tool', async () => {
    const element = await mountWithDialog();
    (element as any)._patchModelIOForm({ action: 'notify' });
    (element as any)._patchModelIOForm({ ruleType: 'tool' });
    await element.updateComplete;

    expect(notifyOption(element)).to.not.exist;
    expect((element as any)._modelIOForm.action).to.equal('deny');
  });

  it('saves a notify rule without an approval workflow', async () => {
    const element = await mountWithDialog();
    (element as any)._modelIOForm = {
      ...(element as any)._modelIOForm,
      id: 'notify-codename',
      ruleType: 'model',
      target: 'model.request',
      action: 'notify',
      expression: "request.text.contains('project-x')",
      detectPii: false,
    };
    await (element as any).saveModelIORule();
    await waitUntil(
      () => (element as any)._modelIORules?.length === 1,
      'rule did not reload'
    );
    await element.updateComplete;

    const saved = (element as any)._modelIORules[0];
    expect(saved.conditions[0].action).to.equal('notify');
    expect(saved.approval_workflow).to.equal(null);
    const badge = element.shadowRoot?.querySelector('sl-badge.action-notify');
    expect(badge?.textContent?.trim()).to.equal('Notify');
  });
});
