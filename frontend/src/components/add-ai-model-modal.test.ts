import { html, fixture, expect } from '@open-wc/testing';
import sinon, { SinonSandbox, SinonStub } from 'sinon';
import './add-ai-model-modal.ts';
import { AddAIModelModal } from './add-ai-model-modal';

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
