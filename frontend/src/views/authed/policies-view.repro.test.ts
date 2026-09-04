/**
 * Repro suite for the Policies page bug hunt (2026-09-04). Every case drives
 * the real controls (clicks, Shoelace events) instead of private methods so
 * a pass or fail says something about what a user sees.
 */
import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './policies-view';
import type { PoliciesView } from './policies-view';
import '../../components/policy-generate-dialog';
import type { PolicyGenerateDialog } from '../../components/policy-generate-dialog';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface StubOpts {
  tools?: unknown[];
  workflows?: unknown[];
  modelIORules?: unknown[];
  createStatus?: number;
  calls: Array<{ url: string; method: string; body: any }>;
}

function stubFetch(opts: StubOpts) {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method || 'GET').toUpperCase();
      let body: any = null;
      if (typeof init?.body === 'string') {
        try {
          body = JSON.parse(init.body);
        } catch {
          body = init.body;
        }
      }
      opts.calls.push({ url, method, body });

      if (url.endsWith('/api/v1/tools') && method === 'GET') {
        return json(opts.tools ?? []);
      }
      if (url.endsWith('/api/v1/approval-workflows') && method === 'GET') {
        return json(opts.workflows ?? []);
      }
      if (url.includes('/api/v1/features')) {
        return json({ plugins: [], features: {} });
      }
      if (url.includes('/api/v1/policies/model-io-rules') && method === 'GET') {
        return json({ rules: opts.modelIORules ?? [] });
      }
      if (
        url.endsWith('/api/v1/policies/model-io-rules') &&
        method === 'POST'
      ) {
        if (opts.createStatus && opts.createStatus >= 400) {
          return json({ detail: 'Permission denied' }, opts.createStatus);
        }
        return json(body);
      }
      if (url.endsWith('/api/v1/tool-configurations') && method === 'POST') {
        return json({ id: 'cfg-new', ...body });
      }
      if (url.includes('/access-rules') && method === 'POST') {
        return json({ id: 'rule-new', ...body }, 201);
      }
      if (url.includes('/api/v1/access-rules/') && method === 'PUT') {
        return json({ id: url.split('/').pop(), ...body });
      }
      if (url.includes('/api/v1/access-rules/') && method === 'DELETE') {
        return new Response(null, { status: 204 });
      }
      if (url.includes('/api/v1/policies/export')) {
        return new Response('version: "1.0"\n', { status: 200 });
      }
      if (url.includes('/api/v1/policies/versions')) {
        return json([]);
      }
      if (url.includes('/api/v1/policies/generate') && method === 'POST') {
        return json({ yaml: 'version: "1.0"\ntools: []\n', warnings: [] });
      }
      if (url.endsWith('/api/v1/policies/diff') && method === 'POST') {
        return json({
          summary: '1 change',
          has_changes: true,
          changes: { added: [], removed: [], modified: [] },
        });
      }
      return json({ detail: `Unhandled: ${method} ${url}` }, 500);
    });
}

const toolWithDenyRule = {
  name: 'shell',
  description: 'Run a command',
  source: 'builtin',
  source_id: null,
  source_name: 'Built-in',
  schema: {},
  is_enabled: true,
  approval_workflow_id: null,
  has_approval_condition: false,
  config_id: 'cfg-1',
  access_rules: [
    {
      id: 'ar-1',
      action: 'deny',
      condition_expression: 'args.command.contains("rm")',
      condition_type: 'cel',
      priority: 0,
      description: null,
      is_enabled: true,
      approval_workflow_id: null,
    },
  ],
};

function headerButton(element: PoliciesView, label: string) {
  const buttons = Array.from(
    element.shadowRoot!.querySelectorAll('view-header sl-button')
  );
  return buttons.find((b) => b.textContent?.includes(label)) as HTMLElement;
}

function ruleDialog(element: PoliciesView) {
  return element.shadowRoot!.querySelector(
    '[data-testid="rule-dialog"]'
  ) as any;
}

function dialogSave(element: PoliciesView) {
  return ruleDialog(element).querySelector(
    'sl-button[slot="footer"][variant="primary"]'
  ) as HTMLElement;
}

async function mount(opts: Partial<StubOpts> = {}) {
  const calls: StubOpts['calls'] = [];
  const stub = stubFetch({ ...opts, calls });
  const element = (await fixture(
    html`<policies-view></policies-view>`
  )) as PoliciesView;
  await waitUntil(() => !(element as any)._loading, 'still loading');
  await element.updateComplete;
  return { element, calls, stub };
}

describe('Policies page repro', () => {
  let stub: sinon.SinonStub | undefined;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    stub?.restore();
    (window.fetch as any).restore?.();
    localStorage.clear();
  });

  it('R1 header Add rule button opens the rule dialog', async () => {
    const m = await mount();
    stub = m.stub;
    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;
    expect(ruleDialog(m.element).open).to.be.true;
  });

  it('R2 Save with the default form shows the error inside the dialog', async () => {
    const m = await mount();
    stub = m.stub;
    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;
    // Default form: preset selected, id empty. Save is disabled; calling
    // save still writes the message into the dialog, not the page overlay.
    const save = dialogSave(m.element) as HTMLButtonElement;
    expect(save.disabled, 'Save stays off until the id is filled').to.be.true;
    await (m.element as any).saveModelIORule();
    await m.element.updateComplete;

    const posted = m.calls.filter(
      (c) => c.url.endsWith('/policies/model-io-rules') && c.method === 'POST'
    );
    expect(posted).to.have.length(0);
    expect((m.element as any)._ruleDialogError).to.contain(
      'Rule id is required'
    );
    expect(ruleDialog(m.element).open, 'dialog stays open').to.be.true;
    expect(ruleDialog(m.element).textContent).to.contain('Rule id is required');
  });

  it('R3 API 403 on Save shows the error inside the dialog', async () => {
    const m = await mount({ createStatus: 403 });
    stub = m.stub;
    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;
    const idInput = ruleDialog(m.element).querySelector('sl-input') as any;
    idInput.value = 'deny-pii';
    idInput.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await m.element.updateComplete;

    dialogSave(m.element).click();
    await waitUntil(
      () => Boolean((m.element as any)._ruleDialogError),
      'no error set'
    );
    await m.element.updateComplete;

    expect((m.element as any)._ruleDialogError).to.contain('Permission denied');
    expect(ruleDialog(m.element).open).to.be.true;
    expect(ruleDialog(m.element).textContent).to.contain('Permission denied');
  });

  it('R4 picking a second preset updates the id to match', async () => {
    const m = await mount();
    stub = m.stub;
    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;
    const card = (id: string) =>
      ruleDialog(m.element).querySelector(
        `[data-preset="${id}"]`
      ) as HTMLElement;
    card('block-pii-prompts').click();
    await m.element.updateComplete;
    card('flag-injection').click();
    await m.element.updateComplete;
    const form = (m.element as any)._modelIOForm;
    expect(form.presetId).to.equal('flag-injection');
    expect(form.id).to.equal('flag-injection');
  });

  it('R5 a saved tool access rule is shown as deny with its condition', async () => {
    const m = await mount({ tools: [toolWithDenyRule] });
    stub = m.stub;
    const card = m.element.shadowRoot!.querySelector(
      '[data-rule-id="shell"]'
    ) as HTMLElement;
    expect(card).to.exist;
    const badge = card.querySelector('sl-badge') as HTMLElement;
    expect(badge.textContent?.trim()).to.equal('deny');
    expect(card.querySelector('code')?.textContent).to.contain(
      'contains("rm")'
    );
  });

  it('R6 clicking a tool rule opens it for edit with card actions', async () => {
    const m = await mount({ tools: [toolWithDenyRule] });
    stub = m.stub;
    const card = m.element.shadowRoot!.querySelector(
      '[data-rule-id="shell"] .access-rule-header'
    ) as HTMLElement;
    card.click();
    await m.element.updateComplete;
    expect(ruleDialog(m.element).open).to.be.true;
    expect(ruleDialog(m.element).getAttribute('label')).to.equal('Edit rule');
    const form = (m.element as any)._modelIOForm;
    expect(form.ruleType).to.equal('tool');
    expect(form.toolName).to.equal('shell');
    expect((m.element as any)._editingAccessRuleId).to.equal('ar-1');
    const cardEl = m.element.shadowRoot!.querySelector(
      '[data-rule-id="shell"]'
    ) as HTMLElement;
    expect(cardEl.querySelectorAll('sl-button')).to.have.length(2);
  });

  it('R7 tool rule Save posts condition_type simple with a CEL expression', async () => {
    const m = await mount({
      tools: [{ ...toolWithDenyRule, config_id: null, access_rules: [] }],
    });
    stub = m.stub;
    headerButton(m.element, 'Add rule').click();
    await m.element.updateComplete;
    (m.element as any)._patchModelIOForm({
      ruleType: 'tool',
      toolName: 'shell',
      action: 'deny',
      expression: 'args.command.contains("rm")',
    });
    await m.element.updateComplete;
    dialogSave(m.element).click();
    await waitUntil(
      () => m.calls.some((c) => c.url.includes('/access-rules')),
      'no access rule POST'
    );
    const post = m.calls.find((c) => c.url.includes('/access-rules'))!;
    expect(post.url).to.contain(
      '/api/v1/tool-configurations/cfg-new/access-rules'
    );
    expect(post.body.condition_type).to.equal('simple');
    expect(post.body.condition_expression).to.contain('contains(');
  });

  it('R8 editing a rule then choosing Start from a preset selects nothing', async () => {
    const rule = {
      id: 'r1',
      target: 'model.request',
      enabled: true,
      detectors: { pii: true },
      conditions: [{ expression: 'pii.found == true', action: 'deny' }],
    };
    const m = await mount({ modelIORules: [rule] });
    stub = m.stub;
    const card = m.element.shadowRoot!.querySelector(
      '[data-rule-id="r1"] .access-rule-header'
    ) as HTMLElement;
    card.click();
    await m.element.updateComplete;
    expect(ruleDialog(m.element).getAttribute('label')).to.equal('Edit rule');
    (m.element as any)._setConditionMode('preset');
    await m.element.updateComplete;
    expect((m.element as any)._modelIOForm.conditionMode).to.equal('preset');
    expect(ruleDialog(m.element).querySelector('.preset-card.selected')).to.not
      .exist;
  });

  it('R9 header Describe a change opens the generate dialog', async () => {
    const m = await mount();
    stub = m.stub;
    headerButton(m.element, 'Describe a change').click();
    await waitUntil(
      () => (m.element as any)._showGenerateDialog === true,
      'generate dialog never opened'
    );
    await m.element.updateComplete;
    const gen = m.element.shadowRoot!.querySelector(
      'policy-generate-dialog'
    ) as PolicyGenerateDialog;
    expect(gen.open).to.be.true;
  });

  it('R10 collapsing Full generated YAML keeps the generate dialog and yaml', async () => {
    const calls: StubOpts['calls'] = [];
    stub = stubFetch({ calls });
    const gen = (await fixture(
      html`<policy-generate-dialog
        open
        currentYaml=""
      ></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    (gen as any)._prompt = 'deny everything';
    await gen.updateComplete;
    const generate = Array.from(
      gen.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.includes('Generate')) as HTMLElement;
    generate.click();
    await waitUntil(() => Boolean((gen as any)._generatedYaml), 'no yaml');
    await gen.updateComplete;

    const details = gen.shadowRoot!.querySelector('sl-details') as any;
    expect(details).to.exist;
    await details.show();
    await details.hide();
    await gen.updateComplete;

    expect(gen.open, 'inner sl-details must not close the dialog').to.be.true;
    expect((gen as any)._generatedYaml).to.contain('version:');
  });
});
