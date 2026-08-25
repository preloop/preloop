import { html, fixture, expect } from '@open-wc/testing';
import sinon, { SinonSandbox, SinonStub } from 'sinon';
import './add-ai-model-modal.ts';
import { AddAIModelModal } from './add-ai-model-modal';
import type { AIModel } from '../types';

const SECRET_ID = '11111111-1111-1111-1111-111111111111';

/** Payloads POSTed to the model-create endpoint, in call order. */
function createdModelPayloads(fetchStub: SinonStub): any[] {
  return fetchStub
    .getCalls()
    .filter(
      (call) =>
        String(call.args[0]).includes('/api/v1/ai-models') &&
        (call.args[1]?.method ?? 'GET') === 'POST'
    )
    .map((call) => JSON.parse(String(call.args[1].body)));
}

describe('AddAIModelModal multi-model per key', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();

    fetchStub = sandbox.stub(window, 'fetch');
    // Every create returns the same secret id, mimicking the server minting one
    // secret for the primary model and reusing it for the rest.
    fetchStub.callsFake(async (url: any, init: any) => {
      const target = String(url);
      if (target.includes('available-models')) {
        return new Response(
          JSON.stringify(['claude-sonnet-4-5', 'claude-haiku-4-5'])
        );
      }
      if (target.includes('/api/v1/ai-models') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        return new Response(
          JSON.stringify({
            id: `id-${body.model_identifier}`,
            ...body,
            credentials_secret_id: SECRET_ID,
          }),
          { status: 201 }
        );
      }
      return new Response(JSON.stringify([]));
    });

    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  /** Put the form into a submittable create-mode state. */
  const primeForm = async (additional: string[]) => {
    element.open = true;
    await element.updateComplete;
    (element as any)._currentModel = {
      name: 'Claude Sonnet',
      provider_name: 'anthropic',
      model_identifier: 'claude-sonnet-4-5',
      model_kind: 'llm',
      api_endpoint: 'https://api.anthropic.com',
      api_key: 'one-anthropic-key',
    };
    (element as any)._modelSuggestions = [
      'claude-sonnet-4-5',
      'claude-haiku-4-5',
    ];
    (element as any)._additionalModelIds = additional;
    (element as any)._syncFormFromDom = () => {};
    await element.updateComplete;
  };

  it('creates only the primary model when no extras are selected', async () => {
    await primeForm([]);

    await (element as any)._handleFormSubmit(new Event('submit'));

    const payloads = createdModelPayloads(fetchStub);
    expect(payloads).to.have.lengthOf(1);
    expect(payloads[0].model_identifier).to.equal('claude-sonnet-4-5');
  });

  it('creates extra models that reuse the primary key secret', async () => {
    await primeForm(['claude-haiku-4-5']);

    await (element as any)._handleFormSubmit(new Event('submit'));

    const payloads = createdModelPayloads(fetchStub);
    expect(payloads).to.have.lengthOf(2);

    // The primary mints the secret: it sends the raw key, not a secret id.
    expect(payloads[0].model_identifier).to.equal('claude-sonnet-4-5');
    expect(payloads[0].api_key).to.equal('one-anthropic-key');
    expect(payloads[0].credentials_secret_id).to.be.undefined;

    // The extra reuses that secret and never re-sends the key.
    expect(payloads[1].model_identifier).to.equal('claude-haiku-4-5');
    expect(payloads[1].credentials_secret_id).to.equal(SECRET_ID);
    expect(payloads[1].api_key).to.be.undefined;
    expect(payloads[1].is_default).to.equal(false);
  });

  it('gives each extra model its own gateway alias', async () => {
    await primeForm(['claude-haiku-4-5']);

    await (element as any)._handleFormSubmit(new Event('submit'));

    const payloads = createdModelPayloads(fetchStub);
    // A shared alias would make the models indistinguishable to the resolver.
    expect(payloads[0].meta_data.gateway.model_alias).to.equal(
      'anthropic/claude-sonnet-4-5'
    );
    expect(payloads[1].meta_data.gateway.model_alias).to.equal(
      'anthropic/claude-haiku-4-5'
    );
  });

  it('never offers the primary model as an extra', async () => {
    await primeForm([]);

    expect((element as any)._additionalModelChoices).to.deep.equal([
      'claude-haiku-4-5',
    ]);
  });

  it('drops the newly chosen primary from an existing extras selection', async () => {
    await primeForm(['claude-haiku-4-5']);

    (element as any)._handleModelNameChange({
      target: { value: 'claude-haiku-4-5' },
    });

    expect((element as any)._additionalModelIds).to.deep.equal([]);
  });

  it('reports partial success when an extra model fails to create', async () => {
    await primeForm(['claude-haiku-4-5']);
    fetchStub.callsFake(async (url: any, init: any) => {
      const target = String(url);
      if (target.includes('/api/v1/ai-models') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        if (body.model_identifier === 'claude-haiku-4-5') {
          return new Response(JSON.stringify({ detail: 'nope' }), {
            status: 400,
          });
        }
        return new Response(
          JSON.stringify({
            id: 'id-primary',
            ...body,
            credentials_secret_id: SECRET_ID,
          }),
          { status: 201 }
        );
      }
      return new Response(JSON.stringify([]));
    });

    await (element as any)._handleFormSubmit(new Event('submit'));

    // The primary succeeded, so this must not read as a total failure.
    expect((element as any)._formError).to.contain('claude-haiku-4-5');
    expect((element as any)._formError).to.contain('claude-sonnet-4-5');
  });
});

/**
 * Issue #171 (empty model list for OpenRouter) and the key-in-URL leak found
 * alongside it. The picker sends the endpoint so openai-compatible providers
 * can be listed at all, and the key must never reach the query string.
 */
describe('AddAIModelModal model discovery', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;

  const discoveryCalls = () =>
    fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('available-models'));

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    fetchStub = sandbox.stub(window, 'fetch');
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(
          JSON.stringify([
            'deepseek/deepseek-v4-flash-0731',
            'openrouter/auto-beta',
          ])
        );
      }
      return new Response(JSON.stringify([]));
    });
    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('lists OpenRouter models for an openai-compatible endpoint', async () => {
    (element as any)._currentModel = {
      provider_name: 'openai-compatible',
      api_endpoint: 'https://openrouter.ai/api/v1',
      api_key: 'sk-or-v1-secret-value',
    };

    await (element as any)._fetchModelsForCurrentProvider();

    expect((element as any)._modelSuggestions).to.deep.equal([
      'deepseek/deepseek-v4-flash-0731',
      'openrouter/auto-beta',
    ]);
    expect((element as any)._modelsFetchError).to.equal(null);

    const [url, init] = discoveryCalls()[0].args;
    const body = JSON.parse(String(init.body));
    expect(init.method).to.equal('POST');
    expect(body.api_endpoint).to.equal('https://openrouter.ai/api/v1');
    expect(body.api_key).to.equal('sk-or-v1-secret-value');
    // The leak that put live keys in access logs.
    expect(String(url)).to.not.contain('api_key');
    expect(String(url)).to.not.contain('sk-or-v1-secret-value');
  });

  it('asks for an endpoint before fetching openai-compatible models', async () => {
    (element as any)._currentModel = {
      provider_name: 'openai-compatible',
      api_key: 'sk-or-v1-secret-value',
    };

    await (element as any)._fetchModelsForCurrentProvider();

    expect((element as any)._modelsFetchError).to.contain('API endpoint');
    expect(discoveryCalls()).to.have.lengthOf(0);
  });

  it('still fetches catalog providers without an endpoint', async () => {
    (element as any)._currentModel = {
      provider_name: 'anthropic',
      api_key: 'sk-ant-secret',
    };

    await (element as any)._fetchModelsForCurrentProvider();

    expect(discoveryCalls()).to.have.lengthOf(1);
    expect(String(discoveryCalls()[0].args[0])).to.not.contain('sk-ant-secret');
  });
});

/**
 * OpenRouter as a first-class provider. The backend has supported it since
 * #176; the picker just never offered it, so users had to know to pick
 * "OpenAI-compatible" and type the base URL by hand.
 */
describe('AddAIModelModal OpenRouter provider', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;

  const discoveryCalls = () =>
    fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('available-models'));

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    fetchStub = sandbox.stub(window, 'fetch');
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(
          JSON.stringify(['openrouter/auto-beta', 'z-ai/glm-4.6'])
        );
      }
      return new Response(JSON.stringify([]));
    });
    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('offers OpenRouter as an LLM provider', () => {
    const values = (element as any)._availableProviders.map(
      (p: { value: string }) => p.value
    );
    expect(values).to.contain('openrouter');
  });

  it('does not offer OpenRouter for stt or tts', async () => {
    (element as any)._currentModel = { model_kind: 'stt' };
    const values = (element as any)._availableProviders.map(
      (p: { value: string }) => p.value
    );
    expect(values).to.not.contain('openrouter');
  });

  it('prefills the OpenRouter base URL when the provider is chosen', async () => {
    await (element as any)._handleProviderChange({
      target: { value: 'openrouter' },
    } as unknown as Event);

    expect((element as any)._currentModel.api_endpoint).to.equal(
      'https://openrouter.ai/api/v1'
    );
  });

  it('fetches models without the user typing an endpoint', async () => {
    await (element as any)._handleProviderChange({
      target: { value: 'openrouter' },
    } as unknown as Event);
    (element as any)._currentModel.api_key = 'sk-or-v1-secret-value';

    await (element as any)._fetchModelsForCurrentProvider();

    expect((element as any)._modelsFetchError).to.equal(null);
    expect((element as any)._modelSuggestions).to.deep.equal([
      'openrouter/auto-beta',
      'z-ai/glm-4.6',
    ]);
    // The provider travels in the URL, the endpoint in the body.
    expect(String(discoveryCalls()[0].args[0])).to.contain(
      '/providers/openrouter/available-models'
    );
    const body = JSON.parse(String(discoveryCalls()[0].args[1].body));
    expect(body.api_endpoint).to.equal('https://openrouter.ai/api/v1');
  });

  it('falls back to the default endpoint if the field was cleared', async () => {
    (element as any)._currentModel = {
      provider_name: 'openrouter',
      api_endpoint: '   ',
      api_key: 'sk-or-v1-secret-value',
    };

    await (element as any)._fetchModelsForCurrentProvider();

    expect((element as any)._modelsFetchError).to.equal(null);
    const body = JSON.parse(String(discoveryCalls()[0].args[1].body));
    expect(body.api_endpoint).to.equal('https://openrouter.ai/api/v1');
  });

  it('keeps the Other... escape hatch for Auto Router ids', async () => {
    (element as any)._modelSuggestions = ['z-ai/glm-4.6'];
    (element as any)._handleModelNameChange({
      target: { value: 'other' },
    } as unknown as Event);
    expect((element as any)._isOtherModel).to.be.true;

    (element as any)._handleCustomModelInput({
      target: { value: 'openrouter/auto-beta' },
    } as unknown as Event);

    expect((element as any)._currentModel.model_identifier).to.equal(
      'openrouter/auto-beta'
    );
  });

  it('renders an existing openrouter model in edit mode', async () => {
    const model = {
      id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      name: 'OpenRouter Auto',
      provider_name: 'openrouter',
      model_identifier: 'openrouter/auto-beta',
      api_endpoint: 'https://openrouter.ai/api/v1',
      model_kind: 'llm',
      has_api_key: true,
    };
    const el: AddAIModelModal = await fixture(
      html`<add-ai-model-modal
        .model=${model as any}
        ?open=${true}
      ></add-ai-model-modal>`
    );
    await el.updateComplete;

    const providerSelect = el.shadowRoot?.querySelector(
      'sl-select[label="Provider"]'
    ) as any;
    expect(providerSelect?.value).to.equal('openrouter');
    const urlInput = Array.from(
      el.shadowRoot?.querySelectorAll('sl-input') ?? []
    ).find((i) => i.getAttribute('label') === 'API URL') as any;
    expect(urlInput?.value).to.equal('https://openrouter.ai/api/v1');
    // Editing must not wipe the stored id just because it is not in the
    // (unfetched) suggestion list.
    expect((el as any)._currentModel.model_identifier).to.equal(
      'openrouter/auto-beta'
    );
  });

  it('renders a full OpenRouter-sized catalog without dropping entries', async () => {
    const many = Array.from({ length: 338 }, (_, i) => `vendor/model-${i}`);
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(JSON.stringify(many));
      }
      return new Response(JSON.stringify([]));
    });
    // Open first: opening repopulates the form and would clear these fields.
    element.open = true;
    await element.updateComplete;
    (element as any)._currentModel = {
      provider_name: 'openrouter',
      api_endpoint: 'https://openrouter.ai/api/v1',
      api_key: 'sk-or-v1-secret-value',
    };

    await (element as any)._fetchModelsForCurrentProvider();
    await element.updateComplete;

    expect((element as any)._modelSuggestions).to.have.lengthOf(338);
    const modelSelect = element.shadowRoot?.querySelector(
      'sl-select[label="Model Name / ID"]'
    );
    const options = modelSelect?.querySelectorAll('sl-option') ?? [];
    // Every model plus the "Other..." entry.
    expect(options.length).to.equal(339);
  });
});

/**
 * Provenance of the model list. The server now says whether the list came
 * from the provider's live catalog or from a static fallback, with a short
 * safe reason code. The picker must tell the user when the list may be
 * incomplete, and must never render raw upstream text.
 */
describe('AddAIModelModal model list provenance', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;

  const noticeText = () => {
    const notice = element.shadowRoot?.querySelector(
      '.models-provenance-notice'
    );
    return notice?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  };

  const fetchAsProvider = async (provider: string) => {
    element.open = true;
    await element.updateComplete;
    (element as any)._currentModel = {
      provider_name: provider,
      api_key: 'sk-secret-key-material',
    };
    await (element as any)._fetchModelsForCurrentProvider();
    await element.updateComplete;
  };

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    fetchStub = sandbox.stub(window, 'fetch');
    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('shows a non-blocking notice when the list is a fallback', async () => {
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(
          JSON.stringify({
            models: ['kimi-k3', 'kimi-k2'],
            source: 'fallback',
            error: 'timeout',
          })
        );
      }
      return new Response(JSON.stringify([]));
    });

    await fetchAsProvider('moonshot');

    // Non-blocking: the suggestions are still usable.
    expect((element as any)._modelSuggestions).to.deep.equal([
      'kimi-k3',
      'kimi-k2',
    ]);
    const text = noticeText();
    expect(text).to.contain('Could not fetch the live model list');
    expect(text).to.contain('timed out');
    expect(text).to.contain('Other...');
    // No endpoint URLs or key material may appear in the notice.
    expect(text).to.not.contain('http://');
    expect(text).to.not.contain('https://');
    expect(text).to.not.contain('sk-secret-key-material');
  });

  /**
   * A keyless fetch is a clean fallback, not a failure: the server sends
   * source=fallback with error=null because no live attempt was possible.
   * Blaming the provider ("could not fetch") for a key the user has not typed
   * yet is a false accusation, and it trains people to ignore the notice.
   */
  it('does not blame the provider when no key was supplied', async () => {
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(
          JSON.stringify({
            models: ['kimi-k3', 'kimi-k2.6'],
            source: 'fallback',
            error: null,
          })
        );
      }
      return new Response(JSON.stringify([]));
    });

    await fetchAsProvider('moonshot');

    const text = noticeText();
    expect(text).to.not.contain('Could not fetch');
    expect(text).to.not.contain('provider unavailable');
    expect(text).to.contain('Showing known models');
    expect(text).to.contain('API key');
    // The list stays usable either way.
    expect((element as any)._modelSuggestions).to.deep.equal([
      'kimi-k3',
      'kimi-k2.6',
    ]);
  });

  it('shows no fallback notice for a live listing', async () => {
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(
          JSON.stringify({
            models: ['kimi-k3', 'kimi-k2', 'kimi-k1.5'],
            source: 'live',
          })
        );
      }
      return new Response(JSON.stringify([]));
    });

    await fetchAsProvider('moonshot');

    const text = noticeText();
    expect(text).to.not.contain('Could not fetch');
    // A subtle count is allowed for live listings.
    expect(text).to.contain('Fetched 3 models');
    expect(text).to.not.contain('https://');
    expect(text).to.not.contain('sk-secret-key-material');
  });

  it('tolerates a bare string[] response from an old server', async () => {
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(JSON.stringify(['kimi-k3']));
      }
      return new Response(JSON.stringify([]));
    });

    await fetchAsProvider('moonshot');

    expect((element as any)._modelSuggestions).to.deep.equal(['kimi-k3']);
    expect(noticeText()).to.not.contain('Could not fetch');
  });

  /**
   * Contract guard. The server emits a fixed reason vocabulary defined in
   * preloop/services/ai_model_provider.py; every code must have human wording
   * here. This test caught a real drift: the picker had a label for "auth",
   * which the server never sends (bad keys raise a 401 instead of returning a
   * fallback list), and had NO label for missing_endpoint / sdk_missing /
   * unknown, which it does send.
   */
  it('renders human wording for every server fallback reason', async () => {
    const serverReasons = [
      'timeout',
      'network',
      'empty_response',
      'unsupported',
      'missing_endpoint',
      'sdk_missing',
      'unknown',
    ];

    for (const reason of serverReasons) {
      fetchStub.callsFake(async (url: any) => {
        if (String(url).includes('available-models')) {
          return new Response(
            JSON.stringify({
              models: ['kimi-k3'],
              source: 'fallback',
              error: reason,
            })
          );
        }
        return new Response(JSON.stringify([]));
      });

      await fetchAsProvider('moonshot');

      const text = noticeText();
      expect(text, `reason ${reason}`).to.contain(
        'Could not fetch the live model list'
      );
      // The machine code must be translated, never echoed. Checking for the
      // snake_case form catches the "no label, render the code" failure mode
      // without false-positives on words that legitimately appear in prose
      // (the label for "network" is "network error").
      if (reason.includes('_')) {
        expect(text, `reason ${reason} leaked its raw code`).to.not.contain(
          reason
        );
      }
      expect(text, `reason ${reason} produced empty wording`).to.match(
        /list \([A-Za-z][A-Za-z ]+\)\./
      );
      // Every code the server can send must have its OWN wording. Landing on
      // the generic catch-all means a label is missing, which is the silent
      // half of the drift this test exists to prevent. Only "unknown" is
      // allowed to be generic, because that is what it means.
      if (reason !== 'unknown') {
        expect(
          text,
          `reason ${reason} has no label and fell through to the generic phrase`
        ).to.not.contain('provider unavailable');
      }
    }
  });
});

/**
 * Three new first-class BYOK providers: Moonshot (Kimi), Z.ai (GLM), Mistral.
 * Fixed catalogs and known base URLs, so fetching works with just a key.
 */
describe('AddAIModelModal new BYOK providers', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').resolves(new Response(JSON.stringify([])));
    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('offers moonshot, zai, and mistral as LLM providers', () => {
    const values = (element as any)._availableProviders.map(
      (p: { value: string }) => p.value
    );
    expect(values).to.contain('moonshot');
    expect(values).to.contain('zai');
    expect(values).to.contain('mistral');
  });

  it('does not offer them for stt or tts', () => {
    (element as any)._currentModel = { model_kind: 'stt' };
    const values = (element as any)._availableProviders.map(
      (p: { value: string }) => p.value
    );
    expect(values).to.not.contain('moonshot');
    expect(values).to.not.contain('zai');
    expect(values).to.not.contain('mistral');
  });

  it('prefills the default base URL for each new provider', async () => {
    const expected: Record<string, string> = {
      moonshot: 'https://api.moonshot.ai/v1',
      zai: 'https://api.z.ai/api/paas/v4',
      mistral: 'https://api.mistral.ai/v1',
    };
    for (const [provider, endpoint] of Object.entries(expected)) {
      await (element as any)._handleProviderChange({
        target: { value: provider },
      } as unknown as Event);
      expect((element as any)._currentModel.api_endpoint).to.equal(endpoint);
    }
  });

  it('links to each provider key console', () => {
    expect((element as any)._getProviderKeyUrl('moonshot')).to.equal(
      'https://platform.moonshot.ai/console/api-keys'
    );
    expect((element as any)._getProviderKeyUrl('zai')).to.equal(
      'https://z.ai/manage-apikey/apikey-list'
    );
    expect((element as any)._getProviderKeyUrl('mistral')).to.equal(
      'https://console.mistral.ai/api-keys'
    );
  });
});

describe('AddAIModelModal Qwen regional endpoints', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').resolves(new Response(JSON.stringify([])));
    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('prefills the China DashScope URL and does not rename it', async () => {
    await (element as any)._handleProviderChange({
      target: { value: 'qwen' },
    } as unknown as Event);
    expect((element as any)._currentModel.api_endpoint).to.equal(
      'https://dashscope.aliyuncs.com/compatible-mode/v1'
    );
  });

  it('mentions the international and US hosts in the API URL help', async () => {
    element.open = true;
    await element.updateComplete;
    await (element as any)._handleProviderChange({
      target: { value: 'qwen' },
    } as unknown as Event);
    await element.updateComplete;
    const help =
      element.shadowRoot?.querySelector(
        'sl-input[label="API URL"] [slot="help-text"]'
      )?.textContent || '';
    expect(help).to.contain('dashscope.aliyuncs.com');
    expect(help).to.contain('dashscope-intl.aliyuncs.com');
    expect(help).to.contain('dashscope-us.aliyuncs.com');
  });
});

/**
 * Editing a model must never echo stored credential bookkeeping back to the
 * server. The API returns credential_type / credentials_* fields on reads;
 * putting them into AIModelUpdate trips its inline-vs-external validator and
 * makes every edit fail even when the user changed nothing about credentials.
 */
describe('AddAIModelModal edit payload', () => {
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;

  const storedModel = {
    id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    name: 'Kimi K3',
    provider_name: 'moonshot',
    model_identifier: 'kimi-k3',
    model_kind: 'llm',
    api_endpoint: 'https://api.moonshot.ai/v1',
    has_api_key: true,
    credential_type: 'api_key',
    credentials_secret_id: SECRET_ID,
    credentials_backend_type: 'kubernetes',
    credentials_external_ref: 'secret/ai-model-key',
    credentials_meta_data: { namespace: 'preloop' },
    is_default: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
  };

  const updatePayloads = () =>
    fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]).includes('/api/v1/ai-models/') &&
          call.args[1]?.method === 'PUT'
      )
      .map((call) => JSON.parse(String(call.args[1].body)));

  const editFixture = async (): Promise<AddAIModelModal> => {
    const el: AddAIModelModal = await fixture(
      html`<add-ai-model-modal
        .model=${storedModel as any}
        ?open=${true}
      ></add-ai-model-modal>`
    );
    await el.updateComplete;
    (el as any)._syncFormFromDom = () => {};
    return el;
  };

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    fetchStub = sandbox.stub(window, 'fetch');
    fetchStub.callsFake(async (url: any, init: any) => {
      if (
        String(url).includes('/api/v1/ai-models/') &&
        init?.method === 'PUT'
      ) {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ ...storedModel, ...body }), {
          status: 200,
        });
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('sends no credential fields when the key is left blank', async () => {
    const el = await editFixture();
    (el as any)._currentModel.name = 'Kimi K3 renamed';

    await (el as any)._handleFormSubmit(new Event('submit'));

    const payloads = updatePayloads();
    expect(payloads).to.have.lengthOf(1);
    const payload = payloads[0];
    expect(payload.name).to.equal('Kimi K3 renamed');
    expect(payload).to.not.have.property('api_key');
    expect(payload).to.not.have.property('credential_type');
    expect(payload).to.not.have.property('credentials_secret_id');
    expect(payload).to.not.have.property('credentials_backend_type');
    expect(payload).to.not.have.property('credentials_external_ref');
    expect(payload).to.not.have.property('credentials_meta_data');
    // The edit succeeded: no form error left behind.
    expect((el as any)._formError).to.equal(null);
  });

  it('sends only api_key when the user typed a new key', async () => {
    const el = await editFixture();
    (el as any)._currentModel.api_key = 'sk-new-moonshot-key';

    await (el as any)._handleFormSubmit(new Event('submit'));

    const payload = updatePayloads()[0];
    expect(payload.api_key).to.equal('sk-new-moonshot-key');
    expect(payload).to.not.have.property('credential_type');
    expect(payload).to.not.have.property('credentials_secret_id');
    expect(payload).to.not.have.property('credentials_backend_type');
    expect(payload).to.not.have.property('credentials_external_ref');
    expect(payload).to.not.have.property('credentials_meta_data');
  });
});

/**
 * AWS Bedrock as a first-class provider: IAM credentials instead of an API
 * key/endpoint pair, live model listing through the Bedrock control plane,
 * and a stored credential JSON blob the gateway already understands.
 */
describe('AddAIModelModal Bedrock provider', () => {
  let element: AddAIModelModal;
  let sandbox: SinonSandbox;
  let fetchStub: SinonStub;

  const discoveryCalls = () =>
    fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('available-models'));

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    fetchStub = sandbox.stub(window, 'fetch');
    fetchStub.callsFake(async (url: any) => {
      if (String(url).includes('available-models')) {
        return new Response(
          JSON.stringify({
            models: ['anthropic.claude-sonnet-4-5', 'amazon.nova-lite-v1:0'],
            source: 'live',
          })
        );
      }
      return new Response(JSON.stringify([]));
    });
    element = await fixture(html`<add-ai-model-modal></add-ai-model-modal>`);
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  it('offers Bedrock as an LLM provider', () => {
    const values = (element as any)._availableProviders.map(
      (p: { value: string }) => p.value
    );
    expect(values).to.contain('bedrock');
  });

  it('does not offer Bedrock for stt or tts', async () => {
    (element as any)._currentModel = { model_kind: 'stt' };
    const values = (element as any)._availableProviders.map(
      (p: { value: string }) => p.value
    );
    expect(values).to.not.contain('bedrock');
  });

  it('resets AWS fields and clears endpoint/key when Bedrock is chosen', async () => {
    await (element as any)._handleProviderChange({
      target: { value: 'bedrock' },
    } as unknown as Event);

    expect((element as any)._bedrockRegion).to.equal('us-east-1');
    expect((element as any)._currentModel.api_endpoint).to.equal('');
    expect((element as any)._currentModel.api_key).to.be.undefined;
  });

  it('refuses to fetch before the AWS credentials are complete', async () => {
    (element as any)._currentModel = { provider_name: 'bedrock' };
    (element as any)._bedrockAccessKeyId = 'AKIAIOSFODNN7EXAMPLE';
    // Secret key and region still missing.

    await (element as any)._fetchModelsForCurrentProvider();

    expect(discoveryCalls()).to.have.lengthOf(0);
    expect((element as any)._modelsFetchError).to.contain('AWS');
  });

  it('sends AWS credential fields in the discovery POST body', async () => {
    (element as any)._currentModel = { provider_name: 'bedrock' };
    (element as any)._bedrockAccessKeyId = 'AKIAIOSFODNN7EXAMPLE';
    (element as any)._bedrockSecretAccessKey = 'shhh';
    (element as any)._bedrockSessionToken = 'tok';
    (element as any)._bedrockRegion = 'eu-west-1';

    await (element as any)._fetchModelsForCurrentProvider();

    expect((element as any)._modelSuggestions).to.deep.equal([
      'anthropic.claude-sonnet-4-5',
      'amazon.nova-lite-v1:0',
    ]);
    const [, init] = discoveryCalls()[0].args;
    const body = JSON.parse(String(init.body));
    expect(body.aws_access_key_id).to.equal('AKIAIOSFODNN7EXAMPLE');
    expect(body.aws_secret_access_key).to.equal('shhh');
    expect(body.aws_session_token).to.equal('tok');
    expect(body.aws_region_name).to.equal('eu-west-1');
    expect(body.model_kind).to.equal('llm');
  });

  it('submits the gateway credential blob and routing region', async () => {
    (element as any)._currentModel = {
      name: 'Claude on Bedrock',
      provider_name: 'bedrock',
      model_identifier: 'anthropic.claude-sonnet-4-5',
      model_kind: 'llm',
    };
    (element as any)._bedrockAccessKeyId = 'AKIAIOSFODNN7EXAMPLE';
    (element as any)._bedrockSecretAccessKey = 'shhh';
    (element as any)._bedrockSessionToken = 'tok';
    (element as any)._bedrockRegion = 'eu-west-1';
    (element as any)._syncBedrockApiKey();
    (element as any)._modelSuggestions = ['anthropic.claude-sonnet-4-5'];
    (element as any)._syncFormFromDom = () => {};
    await element.updateComplete;

    await (element as any)._handleFormSubmit(new Event('submit'));

    const payloads = createdModelPayloads(fetchStub);
    expect(payloads).to.have.lengthOf(1);
    const payload = payloads[0];

    // The stored secret IS this JSON blob; the gateway parses it back into
    // litellm's aws_* kwargs.
    const blob = JSON.parse(payload.api_key);
    expect(blob).to.deep.equal({
      aws_access_key_id: 'AKIAIOSFODNN7EXAMPLE',
      aws_secret_access_key: 'shhh',
      aws_session_token: 'tok',
    });
    // No HTTP endpoint exists for Bedrock; region travels in metadata.
    expect(payload.api_endpoint).to.be.null;
    expect(payload.meta_data.provider_runtime.region).to.equal('eu-west-1');
    expect(payload.meta_data.gateway.model_alias).to.equal(
      'bedrock/anthropic.claude-sonnet-4-5'
    );
  });

  it('blocks submit until the AWS credentials are complete', async () => {
    (element as any)._currentModel = {
      name: 'Claude on Bedrock',
      provider_name: 'bedrock',
      model_identifier: 'anthropic.claude-sonnet-4-5',
      model_kind: 'llm',
    };
    (element as any)._bedrockAccessKeyId = 'AKIAIOSFODNN7EXAMPLE';
    // Secret key missing.
    (element as any)._syncFormFromDom = () => {};

    await (element as any)._handleFormSubmit(new Event('submit'));

    expect((element as any)._formError).to.contain('AWS');
    expect(createdModelPayloads(fetchStub)).to.have.lengthOf(0);
  });

  it('keeps the existing key when editing without retyping credentials', async () => {
    element.model = {
      id: 'existing-id',
      name: 'Claude on Bedrock',
      provider_name: 'bedrock',
      model_identifier: 'anthropic.claude-sonnet-4-5',
      model_kind: 'llm',
      has_api_key: true,
      meta_data: {},
    } as unknown as AIModel;
    element.open = true;
    await element.updateComplete;
    (element as any)._syncFormFromDom = () => {};

    await (element as any)._handleFormSubmit(new Event('submit'));

    expect((element as any)._formError).to.equal(null);
    const putCalls = fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]).includes('/api/v1/ai-models/existing-id') &&
          call.args[1]?.method === 'PUT'
      );
    expect(putCalls).to.have.lengthOf(1);
    const body = JSON.parse(String(putCalls[0].args[1].body));
    expect(body.api_key).to.be.undefined;
  });
});
