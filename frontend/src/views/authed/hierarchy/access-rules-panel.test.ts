import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './access-rules-panel';
import type { AccessRulesPanel } from './access-rules-panel';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
} from '../../../test-helpers/capability-api';

const RULES = '/api/v1/access/rules';
const PREVIEW = '/api/v1/access/modes/preview';
const MODE = '/api/v1/access/modes/tool%3Acall';

const ruleSet = {
  rules: [
    {
      name: 'no-prod-tools',
      effect: 'forbid',
      actions: ['tool:call'],
      scope: 'self',
    },
  ],
  inherited: [
    {
      name: 'parent-guard',
      effect: 'forbid',
      actions: ['model:invoke'],
      scope: 'subaccounts',
      account_name: 'Parent Co',
    },
  ],
  modes: {},
};

const baseRoutes = [
  { path: RULES, body: ruleSet },
  { path: `${RULES}/export`, body: { yaml: 'rules: []\n' } },
];

async function mount() {
  const el = await fixture<AccessRulesPanel>(
    html`<access-rules-panel></access-rules-panel>`
  );
  await waitUntil(() => el.shadowRoot!.querySelector('[data-testid="rules"]'));
  await el.updateComplete;
  return el;
}

const row = (el: AccessRulesPanel, action: string) =>
  el.shadowRoot!.querySelector(`tr[data-action="${action}"]`)!;

async function choose(el: AccessRulesPanel, action: string, mode: string) {
  const select = row(el, action).querySelector(
    'sl-select'
  ) as HTMLSelectElement;
  select.value = mode;
  select.dispatchEvent(new CustomEvent('sl-change'));
  await el.updateComplete;
}

const saveButton = (el: AccessRulesPanel, action: string) =>
  row(el, action).querySelector(
    '[data-testid="mode-save"]'
  ) as HTMLButtonElement;

describe('access-rules-panel', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('lists own rules editable and inherited rules read-only', async () => {
    api = mockApi({ capabilities: ['abac_rules'], routes: baseRoutes });
    const el = await mount();
    const own = el.shadowRoot!.querySelector('tr[data-inherited="false"]')!;
    const inherited = el.shadowRoot!.querySelector(
      'tr[data-inherited="true"]'
    )!;
    expect(own.textContent).to.contain('no-prod-tools');
    expect(own.querySelector('sl-button')).to.exist;
    expect(inherited.textContent).to.contain('parent-guard');
    expect(inherited.textContent).to.contain('Inherited from Parent Co');
    expect(inherited.querySelector('sl-button')).to.be.null;
  });

  it('cannot save require_permit until the preview has been shown', async () => {
    api = mockApi({
      capabilities: ['abac_rules'],
      routes: [
        ...baseRoutes,
        {
          method: 'POST',
          path: PREVIEW,
          body: {
            action: 'tool:call',
            mode: 'require_permit',
            losing_access: [{ kind: 'agent', id: 'ag-1', name: 'Reviewer' }],
            preview_token: 'pv-123',
          },
        },
        { method: 'PUT', path: MODE, status: 204 },
      ],
    });
    const el = await mount();
    expect(saveButton(el, 'tool:call').disabled).to.equal(true);

    await choose(el, 'tool:call', 'require_permit');
    expect(saveButton(el, 'tool:call').disabled).to.equal(true);
    expect(el.canSaveMode('tool:call')).to.equal(false);
    // Calling save directly does nothing either.
    await el.saveMode('tool:call');
    expect(api.callsTo(MODE, 'PUT')).to.have.length(0);

    (
      row(el, 'tool:call').querySelector(
        '[data-testid="mode-preview"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() =>
      row(el, 'tool:call').querySelector('[data-testid="mode-preview-result"]')
    );
    expect(row(el, 'tool:call').textContent).to.contain('Reviewer');
    expect(api.callsTo(PREVIEW, 'POST')[0].body).to.eql({
      action: 'tool:call',
      mode: 'require_permit',
    });
    expect(saveButton(el, 'tool:call').disabled).to.equal(false);

    await el.saveMode('tool:call');
    expect(api.callsTo(MODE, 'PUT')[0].body).to.eql({
      mode: 'require_permit',
      preview_token: 'pv-123',
    });
  });

  it('drops the preview when the choice changes', async () => {
    api = mockApi({
      capabilities: ['abac_rules'],
      routes: [
        ...baseRoutes,
        {
          method: 'POST',
          path: PREVIEW,
          body: {
            action: 'tool:call',
            mode: 'require_permit',
            losing_access: [],
            preview_token: 'pv-1',
          },
        },
      ],
    });
    const el = await mount();
    await choose(el, 'tool:call', 'require_permit');
    (
      row(el, 'tool:call').querySelector(
        '[data-testid="mode-preview"]'
      ) as HTMLElement
    ).click();
    await waitUntil(() => el.canSaveMode('tool:call'));
    await choose(el, 'tool:call', 'additive');
    await choose(el, 'tool:call', 'require_permit');
    expect(el.canSaveMode('tool:call')).to.equal(false);
  });

  it('explains a decision', async () => {
    api = mockApi({
      capabilities: ['abac_rules'],
      routes: [
        ...baseRoutes,
        {
          method: 'POST',
          path: '/api/v1/access/explain',
          body: { effect: 'deny', reason: 'forbid rule no-prod-tools matched' },
        },
      ],
    });
    const el = await mount();
    const set = (id: string, v: string) =>
      (el.shadowRoot!.querySelector<HTMLInputElement>(`#${id}`)!.value = v);
    set('explain-subject', 'agent:ag-1');
    set('explain-action', 'tool:call');
    set('explain-resource', 'tool:t-1');
    const button = [
      ...el.shadowRoot!.querySelectorAll('[data-testid="explain"] sl-button'),
    ][0] as HTMLElement;
    button.click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="explain-result"]')
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="explain-result"]')!
        .textContent
    ).to.contain('Denied');
    expect(api.callsTo('/api/v1/access/explain', 'POST')[0].body).to.eql({
      subject: 'agent:ag-1',
      action: 'tool:call',
      resource: 'tool:t-1',
    });
  });

  it('reports capability-off without a toast when the endpoint is missing', async () => {
    api = mockApi();
    const before = toastCount();
    let off = 0;
    const el = await fixture<AccessRulesPanel>(
      html`<access-rules-panel
        @capability-off=${() => off++}
      ></access-rules-panel>`
    );
    await waitUntil(() => off === 1);
    expect(el.shadowRoot!.textContent!.trim()).to.equal('');
    expect(toastCount()).to.equal(before);
  });
});
