import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches, type GovernanceThresholdSource } from '../api';
import './governance-policy-panel';
import {
  markRuleHeadings,
  PRIVACY_SENTENCE,
  type GovernancePolicyPanel,
} from './governance-policy-panel';

const BODY_V1 = '# Governance policy\n\n### must.approved_models\n\nApproved.';

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface Version {
  id: string;
  version: number;
  body_markdown: string;
  is_active: boolean;
  source: string;
  created_by_id: string | null;
  created_at: string;
  watchlist_fingerprint: string;
}

/** An in-memory governance API, enough to round-trip the panel. */
function thresholdGroups(
  saved: Record<string, Record<string, unknown>> | null,
  legacyCount?: number
) {
  const value = (kind: string, key: string, fallback: number | string) =>
    saved?.[kind]?.[key] ?? fallback;
  return [
    {
      kind: 'retry_limit',
      label: 'Repeated errors',
      help: 'Flag the same error repeated in one session.',
      fields: [
        {
          key: 'count',
          label: 'Repeats in one session',
          default: 5,
          minimum: 2,
          maximum: 1000,
          integer: true,
          unit: 'repeats',
          choices: [],
          value: value('retry_limit', 'count', legacyCount ?? 5),
          source: (saved?.retry_limit?.count !== undefined
            ? 'settings'
            : legacyCount !== undefined
              ? 'policy_document'
              : 'default') as GovernanceThresholdSource,
        },
      ],
    },
    {
      kind: 'model_per_task',
      label: 'Model per task',
      help: 'Flag a top-tier model used for work of this kind.',
      fields: [
        {
          key: 'triage',
          label: 'Triage',
          default: 'small',
          integer: false,
          choices: ['small', 'large', 'any'],
          value: value('model_per_task', 'triage', 'small'),
          source:
            saved?.model_per_task?.triage !== undefined
              ? 'settings'
              : 'default',
        },
      ],
    },
  ];
}

function fakeServer(
  mode = 'on_platform',
  extraVersions = 0,
  options: {
    legacyBlocks?: number;
    thresholds?: boolean;
    legacyCount?: number;
    watchlistCount?: boolean;
    policyGate?: Promise<void>;
  } = {}
) {
  let legacyBlocks = options.legacyBlocks ?? 0;
  let savedThresholds: Record<string, Record<string, unknown>> | null = null;
  const versions: Version[] = [
    {
      id: 'v1',
      version: 1,
      body_markdown: BODY_V1,
      is_active: true,
      source: 'default_template',
      created_by_id: null,
      created_at: '2026-10-01T09:00:00Z',
      watchlist_fingerprint: 'f1',
    },
  ];
  for (let i = 0; i < extraVersions; i++) {
    versions.forEach((v) => (v.is_active = false));
    versions.unshift({
      ...versions[0],
      id: `seed-${i}`,
      version: versions.length + 1,
      body_markdown: `Edit ${i}`,
      is_active: true,
      source: 'user',
    });
  }
  const settings = {
    mode,
    analysis_model_id: (mode === 'on_own_model' ? 'm-own' : null) as
      string | null,
    analysis_model_alias: mode === 'on_own_model' ? 'Own small model' : null,
    own_model_exempt: false,
    effective_reason: undefined as string | undefined,
    privacy_line: 'server line',
  };
  const email = { account_enabled: true, user_enabled: true };
  const calls: { method: string; url: string; body: unknown }[] = [];
  const active = () => versions.find((v) => v.is_active)!;
  const document = () => ({
    ...active(),
    watchlist: {
      policy_document_version: active().version,
      fingerprint: 'f',
      ...(options.watchlistCount === false
        ? {}
        : { legacy_rule_blocks: legacyBlocks }),
      rules: [
        {
          rule_ref: 'prefer.retry_limit',
          class: 'prefer',
          kind: 'retry_limit',
          check: 'Retries stay under the limit.',
          signals: [],
          thresholds: {},
        },
        {
          rule_ref: 'must.approved_models',
          class: 'must',
          kind: 'approved_models',
          check: 'Models used are allowed.',
          signals: [],
          thresholds: {},
        },
      ],
    },
  });
  const addVersion = (body: string) => {
    versions.forEach((v) => (v.is_active = false));
    versions.unshift({
      id: `v${versions.length + 1}`,
      version: versions.length + 1,
      body_markdown: body,
      is_active: true,
      source: 'user',
      created_by_id: 'user-1',
      created_at: new Date().toISOString(),
      watchlist_fingerprint: 'f',
    });
  };

  const stub = sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = (init?.method || 'GET').toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, url, body });
      const path = new URL(url, window.location.origin).pathname;
      if (path === '/api/v1/governance-review/policy') {
        if (method === 'GET') await options.policyGate;
        if (method === 'PUT') addVersion(body.body_markdown);
        return json(document());
      }
      if (path === '/api/v1/governance-review/policy/versions') {
        return json(versions);
      }
      const restore = path.match(/versions\/(\d+)\/restore$/);
      if (restore && method === 'POST') {
        const source = versions.find((v) => v.version === Number(restore[1]))!;
        addVersion(source.body_markdown);
        return json(document());
      }
      if (path === '/api/v1/governance-review/review-settings') {
        if (options.thresholds === false) return json({ detail: 'x' }, 404);
        if (method === 'PUT') savedThresholds = body.detector_thresholds;
        return json({
          groups: thresholdGroups(savedThresholds, options.legacyCount),
          legacy_rule_blocks: legacyBlocks,
        });
      }
      if (path === '/api/v1/governance-review/policy/convert-legacy-rules') {
        legacyBlocks = 0;
        addVersion('# Converted');
        return json(document());
      }
      if (path === '/api/v1/governance-review/settings') {
        if (method === 'PUT') Object.assign(settings, body);
        return json(settings);
      }
      if (path === '/api/v1/governance-review/keep-on') {
        settings.mode = 'on_own_model';
        return json(settings);
      }
      if (path === '/api/v1/weekly-report/preferences') {
        if (method === 'PUT') Object.assign(email, body);
        return json(email);
      }
      if (path === '/api/v1/users') {
        return json({
          users: [{ id: 'user-1', full_name: 'Jane Doe' }],
          total: 1,
          skip: 0,
          limit: 100,
        });
      }
      if (path === '/api/v1/ai-models') {
        return json([
          { id: 'm-own', name: 'Own small model', provider_name: 'openai' },
          {
            id: 'm-shared',
            name: 'Shared model',
            provider_name: 'openai',
            is_shared: true,
          },
        ]);
      }
      return json({ detail: 'not found' }, 404);
    });
  return { stub, calls, versions, settings, email };
}

async function mount(
  canManage: boolean,
  canManageAccount = canManage
): Promise<GovernancePolicyPanel> {
  const element = await fixture<GovernancePolicyPanel>(
    html`<governance-policy-panel
      .canManage=${canManage}
      .canManageAccount=${canManageAccount}
    ></governance-policy-panel>`
  );
  await waitUntil(
    () => element.shadowRoot?.querySelector('table.versions'),
    'panel did not load'
  );
  return element;
}

function versionRows(element: GovernancePolicyPanel): string[] {
  return [
    ...element.shadowRoot!.querySelectorAll('table.versions tbody tr'),
  ].map((row) => row.getAttribute('data-version')!);
}

describe('governance-policy-panel', () => {
  let server: ReturnType<typeof fakeServer>;

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });
  afterEach(() => {
    sinon.restore();
    localStorage.removeItem('accessToken');
    localStorage.removeItem('refreshToken');
  });

  it('explains legacy implicit analysis is off until explicit opt-in', async () => {
    server = fakeServer('off');
    server.settings.effective_reason = 'explicit_analysis_opt_in_required';
    const element = await mount(true);
    expect(element.shadowRoot!.textContent).to.contain(
      'Automatic analysis now requires an explicit opt-in'
    );
    expect(element.shadowRoot!.textContent).to.contain(
      'factual exception emails work without it'
    );
    expect(server.calls.filter((call) => call.method === 'PUT')).to.have.length(
      0
    );
  });

  it('saving creates a version and shows it in the history', async () => {
    server = fakeServer();
    const element = await mount(true);
    expect(versionRows(element)).to.deep.equal(['1']);

    const textarea = element.shadowRoot!.querySelector(
      'textarea.policy-source'
    ) as HTMLTextAreaElement;
    textarea.value = `${BODY_V1}\n\nEdited.`;
    textarea.dispatchEvent(new Event('input'));
    await element.updateComplete;
    (
      element.shadowRoot!.querySelector('sl-button.save-policy') as HTMLElement
    ).click();

    await waitUntil(() => versionRows(element).length === 2);
    expect(versionRows(element)).to.deep.equal(['2', '1']);
    const put = server.calls.find((c) => c.method === 'PUT');
    expect(put?.body).to.deep.equal({ body_markdown: `${BODY_V1}\n\nEdited.` });
    const newest = element.shadowRoot!.querySelector(
      'tr[data-version="2"]'
    )!.textContent!;
    expect(newest).to.contain('Active');
    await waitUntil(() =>
      element
        .shadowRoot!.querySelector('tr[data-version="2"]')!
        .textContent!.includes('Jane Doe')
    );
  });

  it('restore asks first, then reloads the policy and history', async () => {
    server = fakeServer('on_platform', 1);
    const panel = await mount(true);
    expect(versionRows(panel)).to.deep.equal(['2', '1']);

    const confirm = sinon.stub().resolves(false);
    panel.confirm = confirm;
    const restoreButton = () =>
      panel.shadowRoot!.querySelector(
        'tr[data-version="1"] sl-button.restore'
      ) as HTMLElement;
    restoreButton().click();
    await waitUntil(() => confirm.called);
    expect(server.calls.some((c) => c.url.includes('/restore'))).to.equal(
      false
    );

    confirm.resolves(true);
    restoreButton().click();
    await waitUntil(() => versionRows(panel).length === 3);
    expect(versionRows(panel)).to.deep.equal(['3', '2', '1']);
    const textarea = panel.shadowRoot!.querySelector(
      'textarea.policy-source'
    ) as HTMLTextAreaElement;
    expect(textarea.value).to.equal(BODY_V1);
  });

  it('gives view_policies members a read-only editor', async () => {
    server = fakeServer();
    const element = await mount(false);
    const textarea = element.shadowRoot!.querySelector(
      'textarea.policy-source'
    ) as HTMLTextAreaElement;
    expect(textarea.readOnly).to.equal(true);
    expect(element.shadowRoot!.querySelector('sl-button.save-policy')).to.equal(
      null
    );
    expect(element.shadowRoot!.querySelector('sl-button.restore')).to.equal(
      null
    );
    expect(
      element.shadowRoot!.querySelector('sl-button.save-settings')
    ).to.equal(null);
    expect(element.shadowRoot!.querySelector('.read-only-note')).to.exist;
  });

  it('names the seeded version as the default template', async () => {
    server = fakeServer();
    const element = await mount(true);
    expect(
      element.shadowRoot!.querySelector('tr[data-version="1"]')!.textContent
    ).to.contain('Default template');
  });

  it('shows read-only members the configured analysis model', async () => {
    server = fakeServer('on_own_model');
    const element = await mount(false);
    expect(element.shadowRoot!.querySelector('sl-select.model')).to.equal(null);
    expect(
      element.shadowRoot!.querySelector('.analysis-model')?.textContent
    ).to.contain('Own small model');
  });

  it('does not list compiled rule refs', async () => {
    server = fakeServer();
    const element = await mount(true);
    const text = element.shadowRoot!.textContent!;
    expect(text).not.to.contain('prefer.retry_limit');
    expect(text).not.to.contain('governance-rule');
    expect(element.shadowRoot!.querySelector('.rules')).to.equal(null);
  });

  it('explains that the policy is plain language', async () => {
    server = fakeServer();
    const element = await mount(true);
    const intro = element.shadowRoot!.querySelector('.policy-intro')!;
    expect(intro.textContent).to.contain('plain language');
    const textarea = element.shadowRoot!.querySelector(
      'textarea.policy-source'
    ) as HTMLTextAreaElement;
    expect(textarea.getAttribute('aria-label')).to.equal('AI usage policy');
  });

  it('shows what the review measures and saves a changed number', async () => {
    server = fakeServer();
    const element = await mount(true);
    await waitUntil(() => element.shadowRoot!.querySelector('form.thresholds'));
    const input = element.shadowRoot!.querySelector(
      'sl-input[data-field="retry_limit.count"]'
    ) as HTMLInputElement;
    expect(String(input.value)).to.equal('5');
    expect(input.getAttribute('label')).to.equal('Repeats in one session');
    input.value = '8';
    input.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    (
      element.shadowRoot!.querySelector(
        'sl-button.save-thresholds'
      ) as HTMLElement
    ).click();
    await waitUntil(() =>
      server.calls.some(
        (c) => c.method === 'PUT' && c.url.endsWith('/review-settings')
      )
    );
    const put = server.calls.find(
      (c) => c.method === 'PUT' && c.url.endsWith('/review-settings')
    );
    expect(put?.body).to.deep.equal({
      detector_thresholds: { retry_limit: { count: 8 } },
    });
  });

  it('confirms before resetting measured checks, allowing cancellation', async () => {
    server = fakeServer();
    const element = await mount(true);
    await waitUntil(() =>
      element.shadowRoot!.querySelector('sl-button.reset-thresholds')
    );
    const confirm = sinon.stub().resolves(false);
    element.confirm = confirm;
    (
      element.shadowRoot!.querySelector(
        'sl-button.reset-thresholds'
      ) as HTMLElement
    ).click();
    await waitUntil(() => confirm.calledOnce);
    await element.updateComplete;
    expect(server.calls.some((c) => c.method === 'PUT')).to.equal(false);
    confirm.resolves(true);
    (
      element.shadowRoot!.querySelector(
        'sl-button.reset-thresholds'
      ) as HTMLElement
    ).click();
    await waitUntil(() =>
      server.calls.some(
        (c) => c.method === 'PUT' && c.url.endsWith('/review-settings')
      )
    );
    const put = server.calls.find(
      (c) => c.method === 'PUT' && c.url.endsWith('/review-settings')
    );
    expect(put?.body).to.deep.equal({ detector_thresholds: null });
  });

  for (const value of ['', '1', '1001', '2.5', 'not a number', 'Infinity']) {
    it(`rejects invalid measured-check value ${JSON.stringify(value)} beside its field`, async () => {
      server = fakeServer();
      const element = await mount(true);
      const input = element.shadowRoot!.querySelector(
        'sl-input[data-field="retry_limit.count"]'
      ) as HTMLInputElement;
      input.value = value;
      input.dispatchEvent(new Event('sl-input'));
      await element.updateComplete;
      expect(input.getAttribute('aria-invalid')).to.equal('true');
      expect(input.getAttribute('help-text')).not.to.equal('');
      const button = element.shadowRoot!.querySelector(
        'sl-button.save-thresholds'
      ) as HTMLButtonElement;
      expect(button.disabled).to.equal(true);
      element
        .shadowRoot!.querySelector('form.thresholds')!
        .dispatchEvent(new Event('submit', { cancelable: true }));
      await element.updateComplete;
      expect(server.calls.some((c) => c.method === 'PUT')).to.equal(false);
      input.value = '8';
      input.dispatchEvent(new Event('sl-input'));
      await element.updateComplete;
      expect(input.getAttribute('aria-invalid')).to.equal('false');
      expect(button.disabled).to.equal(false);
    });
  }

  it('leaves unchanged legacy values in the policy when saving another field', async () => {
    server = fakeServer('on_platform', 0, { legacyCount: 7 });
    const element = await mount(true);
    const input = element.shadowRoot!.querySelector(
      'sl-select[data-field="model_per_task.triage"]'
    ) as HTMLSelectElement;
    input.value = 'large';
    input.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    (
      element.shadowRoot!.querySelector(
        'sl-button.save-thresholds'
      ) as HTMLElement
    ).click();
    await waitUntil(() => server.calls.some((c) => c.method === 'PUT'));
    expect(server.calls.find((c) => c.method === 'PUT')?.body).to.deep.equal({
      detector_thresholds: { model_per_task: { triage: 'large' } },
    });
  });

  it('saves an edited legacy value even when it equals the built-in default', async () => {
    server = fakeServer('on_platform', 0, { legacyCount: 7 });
    const element = await mount(true);
    const input = element.shadowRoot!.querySelector(
      'sl-input[data-field="retry_limit.count"]'
    ) as HTMLInputElement;
    input.value = '5';
    input.dispatchEvent(new Event('sl-input'));
    await element.updateComplete;
    (
      element.shadowRoot!.querySelector(
        'sl-button.save-thresholds'
      ) as HTMLElement
    ).click();
    await waitUntil(() => server.calls.some((c) => c.method === 'PUT'));
    expect(server.calls.find((c) => c.method === 'PUT')?.body).to.deep.equal({
      detector_thresholds: { retry_limit: { count: 5 } },
    });
  });

  it('uses the review-settings legacy count when the watchlist omits it', async () => {
    server = fakeServer('on_platform', 0, {
      legacyBlocks: 3,
      watchlistCount: false,
    });
    const element = await mount(true);
    expect(
      element.shadowRoot!.querySelector('.legacy-blocks')?.textContent
    ).to.contain('3 hidden rule');
  });

  it('requests thresholds before the initial policy request resolves', async () => {
    let release!: () => void;
    const policyGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server = fakeServer('on_platform', 0, { policyGate });
    const element = await fixture<GovernancePolicyPanel>(
      html`<governance-policy-panel></governance-policy-panel>`
    );
    try {
      await waitUntil(() =>
        server.calls.some((c) => c.url.endsWith('/review-settings'))
      );
    } finally {
      release();
    }
    await waitUntil(() => element.shadowRoot?.querySelector('form.thresholds'));
  });

  it('gives read-only members disabled measured checks', async () => {
    server = fakeServer();
    const element = await mount(false);
    await waitUntil(() => element.shadowRoot!.querySelector('form.thresholds'));
    const input = element.shadowRoot!.querySelector(
      'sl-input[data-field="retry_limit.count"]'
    ) as HTMLInputElement;
    expect(input.hasAttribute('disabled')).to.equal(true);
    expect(
      element.shadowRoot!.querySelector('sl-button.save-thresholds')
    ).to.equal(null);
  });

  it('hides the measured checks on a server without them', async () => {
    server = fakeServer('on_platform', 0, { thresholds: false });
    const element = await mount(true);
    expect(element.shadowRoot!.querySelector('form.thresholds')).to.equal(null);
    expect(element.shadowRoot!.querySelector('textarea.policy-source')).to
      .exist;
  });

  it('offers to move legacy rule blocks into the settings', async () => {
    server = fakeServer('on_platform', 0, { legacyBlocks: 3 });
    const element = await mount(true);
    const notice = element.shadowRoot!.querySelector('.legacy-blocks')!;
    expect(notice.textContent!.replace(/\s+/g, ' ')).to.contain(
      '3 hidden rule blocks'
    );
    (
      element.shadowRoot!.querySelector(
        'sl-button.convert-legacy'
      ) as HTMLElement
    ).click();
    await waitUntil(() => !element.shadowRoot!.querySelector('.legacy-blocks'));
    expect(
      server.calls.some(
        (c) => c.method === 'POST' && c.url.endsWith('/convert-legacy-rules')
      )
    ).to.equal(true);
    expect(versionRows(element)[0]).to.equal('2');
  });

  it('labels rule headings in the preview with their class', () => {
    const marked = markRuleHeadings(
      '<h3>must.approved_models</h3><h3>prefer.retry_limit</h3><h3>Other</h3>'
    );
    expect(marked).to.contain(
      '<span class="rule-class rule-class-must">Must</span> must.approved_models'
    );
    expect(marked).to.contain('rule-class-prefer">Prefer</span>');
    expect(marked).to.contain('<h3>Other</h3>');
  });

  it('shows the privacy sentence and only own gateway models', async () => {
    server = fakeServer('on_own_model');
    const element = await mount(true);
    expect(element.shadowRoot!.querySelector('.privacy')!.textContent).to.equal(
      PRIVACY_SENTENCE
    );
    await waitUntil(
      () =>
        element.shadowRoot!.querySelectorAll('sl-select.model sl-option')
          .length > 0
    );
    const options = [
      ...element.shadowRoot!.querySelectorAll('sl-select.model sl-option'),
    ].map((option) => option.getAttribute('value'));
    expect(options).to.deep.equal(['m-own']);
  });

  it('keep-on calls the endpoint and the banner disappears', async () => {
    server = fakeServer('off_over_budget');
    const element = await mount(true);
    expect(element.shadowRoot!.querySelector('.over-budget-banner')).to.exist;

    (
      element.shadowRoot!.querySelector('sl-button.keep-on') as HTMLElement
    ).click();
    await waitUntil(
      () => !element.shadowRoot!.querySelector('.over-budget-banner')
    );
    expect(
      server.calls.some(
        (c) => c.method === 'POST' && c.url.endsWith('/keep-on')
      )
    ).to.equal(true);
  });

  it('turns the review off and back on with one switch, restoring the mode', async () => {
    server = fakeServer('on_platform');
    const element = await mount(true);
    const review = () =>
      element.shadowRoot!.querySelector('.review-switch') as HTMLInputElement;
    expect(review().checked).to.equal(true);
    review().click();
    await waitUntil(() => server.settings.mode === 'off');
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('No sessions are read')
    );
    review().click();
    await waitUntil(() => server.settings.mode === 'on_platform');
  });

  it('switches the weekly email for owners/admins and for me', async () => {
    server = fakeServer('on_platform');
    const element = await mount(true);
    await waitUntil(() =>
      element.shadowRoot!.querySelector('.email-user-switch')
    );
    (
      element.shadowRoot!.querySelector('.email-user-switch') as HTMLElement
    ).click();
    await waitUntil(() => server.email.user_enabled === false);
    await waitUntil(
      () =>
        !element
          .shadowRoot!.querySelector('.email-account-switch')!
          .hasAttribute('disabled')
    );
    (
      element.shadowRoot!.querySelector('.email-account-switch') as HTMLElement
    ).click();
    await waitUntil(() => server.email.account_enabled === false);
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes(
        'Owners and admins do not receive'
      )
    );
    const text = element.shadowRoot!.textContent!;
    expect(text).to.contain('Weekly report for owners and admins');
    expect(text).not.to.contain('Nobody in the account');
    expect(
      element
        .shadowRoot!.querySelector('.email-user-switch')!
        .hasAttribute('disabled')
    ).to.equal(false);
    expect(text).not.to.contain('Sent each Monday to members');
    expect(
      element.shadowRoot!.querySelector('a[href="/console/settings/account"]')
        ?.textContent
    ).to.contain('Account settings');
    const puts = server.calls.filter(
      (call) =>
        call.method === 'PUT' && call.url.includes('weekly-report/preferences')
    );
    expect(puts.map((call) => call.body)).to.eql([
      { user_enabled: false },
      { account_enabled: false },
    ]);
  });

  it('keeps read-only members from flipping account switches', async () => {
    server = fakeServer('on_platform');
    const element = await mount(false);
    await waitUntil(() =>
      element.shadowRoot!.querySelector('.email-account-switch')
    );
    expect(
      element
        .shadowRoot!.querySelector('.review-switch')!
        .hasAttribute('disabled')
    ).to.equal(true);
    expect(
      element
        .shadowRoot!.querySelector('.email-account-switch')!
        .hasAttribute('disabled')
    ).to.equal(true);
    expect(
      element
        .shadowRoot!.querySelector('.email-user-switch')!
        .hasAttribute('disabled')
    ).to.equal(false);
  });

  it('gates the account email switch on manage_account, not manage_policies', async () => {
    server = fakeServer('on_platform');
    const element = await mount(true, false);
    await waitUntil(() =>
      element.shadowRoot!.querySelector('.email-account-switch')
    );
    expect(
      element
        .shadowRoot!.querySelector('.email-account-switch')!
        .hasAttribute('disabled')
    ).to.equal(true);
    expect(
      element
        .shadowRoot!.querySelector('.review-switch')!
        .hasAttribute('disabled')
    ).to.equal(false);
    const accountOnly = await mount(false, true);
    await waitUntil(() =>
      accountOnly.shadowRoot!.querySelector('.email-account-switch')
    );
    expect(
      accountOnly
        .shadowRoot!.querySelector('.email-account-switch')!
        .hasAttribute('disabled')
    ).to.equal(false);
  });
});
