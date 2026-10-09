import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './api-key-view';
import type { ApiKeyView } from './api-key-view';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';

describe('ApiKeyView', () => {
  let fetchStub: sinon.SinonStub;
  let wsSendStub: sinon.SinonStub;
  let wsSubscribeStub: sinon.SinonStub;
  let wsStateStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const MODELS = [
    {
      id: 'model-a',
      name: 'Model A',
      provider_name: 'openai',
      model_identifier: 'model-a',
    },
    {
      id: 'model-b',
      name: 'Model B',
      provider_name: 'anthropic',
      model_identifier: 'model-b',
    },
  ];

  function createFetchStub(
    opts: {
      keyFails?: boolean;
      key?: Record<string, unknown>;
      allowedModels?: string[];
      models?: unknown[];
    } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (/\/api\/v1\/auth\/api-keys\/key-1$/.test(url) && method === 'GET') {
          if (opts.keyFails) {
            return json({ detail: 'boom' }, 500);
          }
          return json({
            id: 'key-1',
            name: 'Production Key',
            created_at: '2026-03-01T00:00:00Z',
            expires_at: null,
            last_used_at: '2026-03-02T00:00:00Z',
            ...opts.key,
          });
        }

        if (
          url.includes('/api/v1/auth/api-keys/key-1/governance') &&
          method === 'PUT'
        ) {
          return json({
            subject_type: 'api_keys',
            subject_id: 'key-1',
            config: JSON.parse(String(init?.body || '{}')),
          });
        }

        if (url.includes('/api/v1/auth/api-keys/key-1/governance')) {
          return json({
            subject_type: 'api_keys',
            subject_id: 'key-1',
            config: {
              allowed_models: opts.allowedModels ?? [],
              tool_rules: {},
            },
          });
        }

        if (url.includes('/api/v1/auth/api-keys/key-1/gateway-usage/summary')) {
          return json({
            estimated_cost: 1.23,
            total_requests: 4,
            usage_by_model: [],
            usage_by_session: [],
          });
        }

        if (url.endsWith('/api/v1/tools')) return json([]);
        if (url.endsWith('/api/v1/mcp-servers')) return json([]);
        if (url.endsWith('/api/v1/approval-workflows')) return json([]);
        if (url.endsWith('/api/v1/ai-models')) return json(opts.models ?? []);
        if (url.includes('/api/v1/features')) return json({ features: {} });

        return json({ detail: `Unhandled: ${method} ${url}` }, 200);
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    wsSendStub = sinon.stub(unifiedWebSocketManager, 'send').returns(true);
    wsSubscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .returns(() => {});
    wsStateStub = sinon
      .stub(unifiedWebSocketManager, 'onStateChange')
      .returns(() => {});
  });

  afterEach(() => {
    fetchStub?.restore();
    wsSendStub.restore();
    wsSubscribeStub.restore();
    wsStateStub.restore();
    localStorage.clear();
    resetConfirmDialogForTests();
  });

  async function mount(): Promise<ApiKeyView> {
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;
    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;
    return element;
  }

  function governancePuts(): Array<{ allowed_models: string[] }> {
    return fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]).includes('/governance') &&
          call.args[1]?.method === 'PUT'
      )
      .map((call) => JSON.parse(String(call.args[1].body)));
  }

  function checkbox(element: ApiKeyView, name: string): any {
    return [...element.shadowRoot!.querySelectorAll('sl-checkbox')].find(
      (box) => box.textContent?.trim() === name
    );
  }

  async function toggle(target: any, checked: boolean) {
    target.checked = checked;
    target.dispatchEvent(new Event('sl-change', { bubbles: true }));
  }

  async function answerConfirm(confirm: boolean) {
    await waitUntil(() =>
      document
        .querySelector('confirm-dialog')
        ?.shadowRoot?.querySelector('sl-dialog[open]')
    );
    const dialog = document.querySelector('confirm-dialog')!.shadowRoot!;
    const buttons = dialog.querySelectorAll('sl-button[slot="footer"]');
    (
      (confirm
        ? dialog.querySelector('[data-testid="confirm-dialog-confirm"]')
        : buttons[0]) as HTMLElement
    ).click();
  }

  it('shows the loading spinner when no key id is provided', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<api-key-view></api-key-view>`
    )) as ApiKeyView;
    await element.updateComplete;

    // Without a keyId, loadData is never called, so loading stays true.
    expect((element as any).loading).to.be.true;
    expect(element.shadowRoot?.querySelector('sl-spinner')).to.exist;
  });

  it('loads and renders API key details', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;

    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;

    expect((element as any).apiKey?.name).to.equal('Production Key');
    const header = element.shadowRoot?.querySelector('view-header');
    expect((header as any)?.headerText).to.equal('Production Key');
    expect(header?.shadowRoot?.querySelector('h1')?.textContent).to.contain(
      'Production Key'
    );
    expect(element.shadowRoot?.textContent).to.contain('Active');
  });

  it('renders the back link and a Revoke button in slots view-header has', async () => {
    fetchStub = createFetchStub();
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;

    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;

    const back = element.shadowRoot?.querySelector(
      'view-header [slot="top"] sl-button'
    );
    expect(back, 'back link is rendered').to.exist;
    expect(back?.getAttribute('href')).to.equal('/console/settings/api-keys');
    expect(back?.textContent?.trim()).to.contain('Back to API keys');

    const revoke = element.shadowRoot?.querySelector(
      'view-header [slot="main-column"] sl-button[variant="danger"]'
    );
    expect(revoke, 'Revoke button is rendered').to.exist;
    expect(revoke?.hasAttribute('outline')).to.be.true;
    expect(revoke?.textContent?.trim()).to.contain('Revoke key');
  });

  it('renders an error state when the key fails to load', async () => {
    fetchStub = createFetchStub({ keyFails: true });
    const element = (await fixture(
      html`<api-key-view
        .location=${{ params: { keyId: 'key-1' } }}
      ></api-key-view>`
    )) as ApiKeyView;

    await waitUntil(
      () => !(element as any).loading,
      'API key view did not finish loading'
    );
    await element.updateComplete;

    expect((element as any).error).to.be.a('string');
    expect(element.shadowRoot?.textContent).to.contain('Error loading API key');
  });

  it('shows a revoked key as revoked and offers no Revoke action', async () => {
    fetchStub = createFetchStub({ key: { activity_status: 'revoked' } });
    const element = await mount();
    const status = element.shadowRoot!.querySelector('.details-grid sl-badge');
    expect(status?.textContent?.trim()).to.equal('Revoked');
    expect(status?.getAttribute('variant')).to.equal('danger');
    expect(
      element.shadowRoot!.querySelector(
        'view-header [slot="main-column"] sl-button[variant="danger"]'
      )
    ).to.not.exist;
    expect(element.shadowRoot!.textContent).to.contain('This key was revoked');
  });

  it('labels the spend range in words and names the range picker', async () => {
    fetchStub = createFetchStub();
    const element = await mount();
    expect(element.shadowRoot!.textContent).to.contain('Spend (all time)');
    const select = element.shadowRoot!.querySelector(
      'select.range-select'
    ) as HTMLSelectElement;
    expect(select.getAttribute('aria-label')).to.equal('Spend time range');
  });

  describe('model allowlist', () => {
    it('shows an unrestricted key honestly, with the checkboxes locked', async () => {
      fetchStub = createFetchStub({ models: MODELS });
      const element = await mount();
      const restrict = element.shadowRoot!.querySelector(
        'sl-switch.restrict-switch'
      ) as any;
      expect(restrict.checked).to.equal(false);
      expect(element.shadowRoot!.textContent).to.contain(
        'This key can call any model on the account.'
      );
      const box = checkbox(element, 'Model A');
      expect(box.checked).to.equal(true);
      expect(box.disabled).to.equal(true);
    });

    it('restricts to the first model picked after switching restriction on', async () => {
      fetchStub = createFetchStub({ models: MODELS });
      const element = await mount();
      const restrict = element.shadowRoot!.querySelector(
        'sl-switch.restrict-switch'
      ) as any;
      await toggle(restrict, true);
      await element.updateComplete;
      expect(governancePuts()).to.have.length(0);
      const box = checkbox(element, 'Model B');
      expect(box.checked).to.equal(false);
      expect(box.disabled).to.equal(false);
      await toggle(box, true);
      await waitUntil(() => governancePuts().length === 1);
      expect(governancePuts()[0].allowed_models).to.deep.equal([
        'anthropic/model-b',
      ]);
    });

    it('stores the configured gateway alias after a display-name rename', async () => {
      const renamed = {
        ...MODELS[1],
        name: 'Renamed display label',
        meta_data: { gateway: { model_alias: 'team/stable-model' } },
      };
      fetchStub = createFetchStub({
        models: [MODELS[0], renamed],
        allowedModels: ['openai/model-a'],
      });
      const element = await mount();
      await toggle(checkbox(element, 'Renamed display label'), true);
      await waitUntil(() => governancePuts().length === 1);
      expect(governancePuts()[0].allowed_models).to.deep.equal([
        'openai/model-a',
        'team/stable-model',
      ]);
    });

    it('refuses to empty the list by unchecking the last model', async () => {
      fetchStub = createFetchStub({
        models: MODELS,
        allowedModels: ['Model A'],
      });
      const element = await mount();
      const box = checkbox(element, 'Model A');
      expect(box.checked).to.equal(true);
      await toggle(box, false);
      await element.updateComplete;
      expect(box.checked).to.equal(true);
      expect(governancePuts()).to.have.length(0);
      expect(
        element.shadowRoot!.querySelector('.allowlist-warning')?.textContent
      ).to.contain('Keep at least one model selected');
    });

    it('ticks entries stored as gateway aliases', async () => {
      fetchStub = createFetchStub({
        models: MODELS,
        allowedModels: ['anthropic/model-b'],
      });
      const element = await mount();
      expect(checkbox(element, 'Model B').checked).to.equal(true);
      expect(checkbox(element, 'Model A').checked).to.equal(false);
    });

    const ALIASED_MODELS = [
      ...MODELS,
      {
        id: 'c3d2e1f0-0000-4000-8000-00000000000c',
        name: 'Model C',
        provider_name: 'openai',
        model_identifier: 'model-c-upstream',
        meta_data: { gateway: { enabled: true, model_alias: 'team/model-c' } },
      },
    ];

    it('ticks an entry stored as a bare model_identifier, and unchecking removes it', async () => {
      fetchStub = createFetchStub({
        models: ALIASED_MODELS,
        allowedModels: ['model-c-upstream', 'Model A'],
      });
      const element = await mount();
      const box = checkbox(element, 'Model C');
      expect(box.checked).to.equal(true);
      await toggle(box, false);
      await waitUntil(() => governancePuts().length === 1);
      expect(governancePuts()[0].allowed_models).to.deep.equal(['Model A']);
    });

    it('ticks an entry stored as the configured gateway alias, and unchecking removes it', async () => {
      fetchStub = createFetchStub({
        models: ALIASED_MODELS,
        allowedModels: ['Model A', 'team/model-c'],
      });
      const element = await mount();
      const box = checkbox(element, 'Model C');
      expect(box.checked).to.equal(true);
      expect(checkbox(element, 'Model B').checked).to.equal(false);
      await toggle(box, false);
      await waitUntil(() => governancePuts().length === 1);
      expect(governancePuts()[0].allowed_models).to.deep.equal(['Model A']);
    });

    it('keeps another provider allowed when unchecking a shared bare identifier', async () => {
      fetchStub = createFetchStub({
        models: [
          ...MODELS,
          {
            id: 'id-vendor-b',
            name: 'Vendor B',
            provider_name: 'vendor',
            model_identifier: 'model-b',
          },
        ],
        allowedModels: ['model-b'],
      });
      const element = await mount();
      expect(checkbox(element, 'Model B').checked).to.equal(true);
      expect(checkbox(element, 'Vendor B').checked).to.equal(true);
      await toggle(checkbox(element, 'Model B'), false);
      await waitUntil(() => governancePuts().length === 1);
      expect(governancePuts()[0].allowed_models).to.deep.equal([
        'vendor/model-b',
      ]);
    });

    it('ticks an entry stored as the bare tail of the configured alias', async () => {
      fetchStub = createFetchStub({
        models: ALIASED_MODELS,
        allowedModels: ['model-c'],
      });
      const element = await mount();
      expect(checkbox(element, 'Model C').checked).to.equal(true);
      expect(checkbox(element, 'Model A').checked).to.equal(false);
    });

    it('asks before turning restriction off, and keeps the list on cancel', async () => {
      fetchStub = createFetchStub({
        models: MODELS,
        allowedModels: ['Model A'],
      });
      const element = await mount();
      const restrict = element.shadowRoot!.querySelector(
        'sl-switch.restrict-switch'
      ) as any;
      await toggle(restrict, false);
      await answerConfirm(false);
      await waitUntil(() => restrict.checked === true);
      expect(governancePuts()).to.have.length(0);

      await toggle(restrict, false);
      await answerConfirm(true);
      await waitUntil(() => governancePuts().length === 1);
      expect(governancePuts()[0].allowed_models).to.deep.equal([]);
    });
  });

  it('shows spend in dollars and cents, with the exact amount on hover', async () => {
    fetchStub = createFetchStub();
    const element = await mount();
    const total = element.shadowRoot!.querySelector('.spend-total')!;
    expect(total.textContent?.trim()).to.equal('$1.23');
    expect(total.getAttribute('title')).to.equal('$1.23');
  });
});
