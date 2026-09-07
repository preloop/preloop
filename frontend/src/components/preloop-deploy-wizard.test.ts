import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './preloop-deploy-wizard';
import type { PreloopDeployWizard } from './preloop-deploy-wizard';

describe('PreloopDeployWizard custom agent path', () => {
  let fetchStub: sinon.SinonStub;
  let confirmStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    // Default: AI models call (made in connectedCallback) and anything else.
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
    confirmStub = sinon.stub(window, 'confirm');
  });

  afterEach(() => {
    // Cancel any in-flight first-data polling so timers from one test cannot
    // fire fetches during the next (which, with cleared tokens, would trigger
    // an auth redirect and reload the page).
    document
      .querySelectorAll('preloop-deploy-wizard')
      .forEach((el) => (el as any).cancelFirstDataPolling?.());
    fetchStub.restore();
    confirmStub.restore();
    localStorage.clear();
  });

  // Tracks how many times GET /agents/{id} has been polled, so a stub can
  // return total_requests=0 for the first N polls then flip to a positive count.
  let pollCount = 0;

  // Configures register (POST /agents), model-bindings (PUT
  // /agents/{id}/model-bindings), credential mint (POST
  // /agents/{id}/credentials), and the GET /agents/{id} first-data poll.
  function stubApi(opts?: {
    agentId?: string;
    token?: string;
    failRegister?: boolean;
    failMint?: boolean;
    failBindings?: boolean;
    failTags?: boolean;
    // Number of GET /agents/{id} polls that return total_requests=0 before the
    // agent "connects". Default: never connects (stays at 0).
    pollsUntilActive?: number;
    activeRequestCount?: number;
  }) {
    const agentId = opts?.agentId || 'agent-123';
    const token = opts?.token || 'pl_gw_secret_token_value';
    pollCount = 0;
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        // Model bindings replace.
        if (
          /\/api\/v1\/agents\/[^/]+\/model-bindings$/.test(url) &&
          method === 'PUT'
        ) {
          if (opts?.failBindings) {
            return new Response(JSON.stringify({ detail: 'Bindings failed' }), {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        // Tags update: PATCH /agents/{id}. Checked before the GET poll matcher
        // and the bare /agents POST since those gate on different methods.
        if (/\/api\/v1\/agents\/[^/?]+$/.test(url) && method === 'PATCH') {
          if (opts?.failTags) {
            return new Response(JSON.stringify({ detail: 'Tags failed' }), {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(
            JSON.stringify({
              id: agentId,
              display_name: 'My Agent',
              tags: {},
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        // Credential mint must be checked before the bare /agents POST.
        if (
          /\/api\/v1\/agents\/[^/]+\/credentials$/.test(url) &&
          method === 'POST'
        ) {
          if (opts?.failMint) {
            return new Response(JSON.stringify({ detail: 'Mint failed' }), {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(
            JSON.stringify({
              credential: {
                id: 'cred-1',
                name: 'c',
                scopes: ['mcp:read', 'mcp:write'],
              },
              token,
            }),
            { status: 201, headers: { 'Content-Type': 'application/json' } }
          );
        }

        // First-data poll: GET /agents/{id}.
        if (/\/api\/v1\/agents\/[^/?]+(\?|$)/.test(url) && method === 'GET') {
          pollCount += 1;
          const threshold = opts?.pollsUntilActive ?? Infinity;
          const active = pollCount >= threshold;
          const totalRequests = active ? (opts?.activeRequestCount ?? 3) : 0;
          return new Response(
            JSON.stringify({
              agent: {
                id: agentId,
                display_name: 'My Agent',
                total_requests: totalRequests,
                last_seen_at: '2026-01-01T00:00:00Z',
              },
              aggregate: {},
              usage_by_model: [],
              activity_by_server: [],
              activity_by_tool: [],
              sessions: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (/\/api\/v1\/agents$/.test(url) && method === 'POST') {
          if (opts?.failRegister) {
            return new Response(JSON.stringify({ detail: 'Register failed' }), {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(
            JSON.stringify({
              id: agentId,
              display_name: 'My Agent',
              session_source_type: 'managed_agent',
              lifecycle_state: 'active',
            }),
            { status: 201, headers: { 'Content-Type': 'application/json' } }
          );
        }

        // AI models / fallback.
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );
  }

  // A gateway-enabled AI model fixture matching what getAIModels() returns.
  function gatewayModel(
    id: string,
    name: string,
    alias?: string
  ): Record<string, unknown> {
    return {
      id,
      name,
      provider_name: 'openai',
      model_kind: 'llm',
      model_identifier: name,
      meta_data: { gateway: { enabled: true, model_alias: alias } },
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };
  }

  async function createWizard(): Promise<PreloopDeployWizard> {
    const el = (await fixture(
      html`<preloop-deploy-wizard></preloop-deploy-wizard>`
    )) as PreloopDeployWizard;
    await el.updateComplete;
    return el;
  }

  it('does not fetch /ai-models when the host already owns that list', async () => {
    await fixture(
      html`<preloop-deploy-wizard
        ?modelsFromHost=${true}
      ></preloop-deploy-wizard>`
    );
    const modelCalls = fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('/api/v1/ai-models'));
    expect(modelCalls).to.eql([]);
  });

  function findCardByText(
    el: PreloopDeployWizard,
    text: string
  ): HTMLElement | undefined {
    const buttons = Array.from(
      el.shadowRoot?.querySelectorAll('.wizard-option-button') || []
    ) as HTMLElement[];
    return buttons.find((b) => b.textContent?.includes(text));
  }

  // The custom-agent path now lives behind the "Govern Existing Agents"
  // sub-choice. Click into govern, then the "Connect a custom agent" card.
  async function goToGovern(el: PreloopDeployWizard): Promise<void> {
    const governCard = findCardByText(el, 'Govern Existing Agents');
    expect(governCard, 'govern card should exist').to.exist;
    governCard!.click();
    await el.updateComplete;
  }

  async function findCustomCard(
    el: PreloopDeployWizard
  ): Promise<HTMLElement | undefined> {
    await goToGovern(el);
    return findCardByText(el, 'Connect a custom agent');
  }

  it('choose screen shows two cards: govern existing and deploy new', async () => {
    const el = await createWizard();
    expect((el as any).onboardingPath).to.equal('choose');
    const cards = Array.from(
      el.shadowRoot?.querySelectorAll('.wizard-option-button') || []
    ) as HTMLElement[];
    expect(cards.length).to.equal(2);
    expect(findCardByText(el, 'Govern Existing Agents')).to.exist;
    expect(findCardByText(el, 'Deploy New Agents')).to.exist;
    // The custom-agent card is no longer a top-level choice.
    expect(findCardByText(el, 'Connect a custom agent')).to.not.exist;
  });

  it('govern sub-choice offers CLI autodiscovery and connect-custom, routing correctly', async () => {
    const el = await createWizard();
    await goToGovern(el);
    expect((el as any).onboardingPath).to.equal('govern');

    const cliCard = findCardByText(el, 'Autodiscover via CLI');
    const customCard = findCardByText(el, 'Connect a custom agent');
    expect(cliCard, 'cli card').to.exist;
    expect(customCard, 'custom card').to.exist;

    cliCard!.click();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('cli');
    expect(el.shadowRoot?.textContent).to.contain('preloop agents discover');
  });

  it('routes from the govern sub-choice into the custom path', async () => {
    const el = await createWizard();
    const card = await findCustomCard(el);
    expect(card, 'custom card should exist').to.exist;
    expect(card!.textContent).to.contain(
      'Onboard an existing agent (LangGraph, custom SDK) the CLI'
    );

    card!.click();
    await el.updateComplete;

    expect((el as any).onboardingPath).to.equal('custom');
    expect((el as any).customSubStep).to.equal('name');
    const nameInput = el.shadowRoot?.querySelector(
      'sl-input[name="display_name"]'
    );
    expect(nameInput).to.exist;
  });

  it('initial-path deep-links land directly on cli, custom, and govern', async () => {
    const cliEl = (await fixture(
      html`<preloop-deploy-wizard initial-path="cli"></preloop-deploy-wizard>`
    )) as PreloopDeployWizard;
    await cliEl.updateComplete;
    expect((cliEl as any).onboardingPath).to.equal('cli');
    expect(cliEl.shadowRoot?.textContent).to.contain('preloop agents discover');

    const customEl = (await fixture(
      html`<preloop-deploy-wizard
        initial-path="custom"
      ></preloop-deploy-wizard>`
    )) as PreloopDeployWizard;
    await customEl.updateComplete;
    expect((customEl as any).onboardingPath).to.equal('custom');
    expect((customEl as any).customSubStep).to.equal('name');

    const governEl = (await fixture(
      html`<preloop-deploy-wizard
        initial-path="govern"
      ></preloop-deploy-wizard>`
    )) as PreloopDeployWizard;
    await governEl.updateComplete;
    expect((governEl as any).onboardingPath).to.equal('govern');
    expect(findCardByText(governEl, 'Autodiscover via CLI')).to.exist;
    expect(findCardByText(governEl, 'Connect a custom agent')).to.exist;
  });

  it('back from cli/custom returns to the govern sub-choice', async () => {
    const el = await createWizard();
    await goToGovern(el);
    findCardByText(el, 'Autodiscover via CLI')!.click();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('cli');
    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('govern');

    findCardByText(el, 'Connect a custom agent')!.click();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('custom');
    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('govern');
  });

  it('back from the govern sub-choice returns to the choose screen', async () => {
    const el = await createWizard();
    await goToGovern(el);
    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('choose');
    expect(findCardByText(el, 'Govern Existing Agents')).to.exist;
    expect(findCardByText(el, 'Deploy New Agents')).to.exist;
  });

  it('happy path: registers, mints credential, and shows snippet with token', async () => {
    stubApi({ agentId: 'agent-xyz', token: 'pl_gw_TESTTOKEN' });
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;

    (el as any).customDisplayName = 'Support agent';
    await (el as any).handleCustomRegister();
    await el.updateComplete;

    // Verify POST /agents body.
    const registerCall = fetchStub
      .getCalls()
      .find(
        (c) =>
          /\/api\/v1\/agents$/.test(String(c.args[0])) &&
          (c.args[1] as RequestInit)?.method === 'POST'
      );
    expect(registerCall, 'register call').to.exist;
    const registerBody = JSON.parse(
      (registerCall!.args[1] as RequestInit).body as string
    );
    expect(registerBody.display_name).to.equal('Support agent');

    // Verify POST /agents/{id}/credentials body and path.
    const mintCall = fetchStub
      .getCalls()
      .find(
        (c) =>
          /\/api\/v1\/agents\/agent-xyz\/credentials$/.test(
            String(c.args[0])
          ) && (c.args[1] as RequestInit)?.method === 'POST'
      );
    expect(mintCall, 'mint call').to.exist;
    const mintBody = JSON.parse(
      (mintCall!.args[1] as RequestInit).body as string
    );
    expect(mintBody.name).to.be.a('string').and.not.be.empty;
    expect(mintBody.scopes).to.be.an('array');

    expect((el as any).customSubStep).to.equal('result');

    await waitUntil(() =>
      el.shadowRoot?.textContent?.includes('Your agent is connected')
    );

    // Token is rendered in a copy button.
    const copyButtons = Array.from(
      el.shadowRoot?.querySelectorAll('sl-copy-button') || []
    ) as Array<HTMLElement & { value: string }>;
    const tokenButton = copyButtons.find((b) => b.value === 'pl_gw_TESTTOKEN');
    expect(tokenButton, 'token copy button').to.exist;
  });

  it('base_url is built from window.location.origin and carried on a copy button', async () => {
    stubApi();
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;
    (el as any).customDisplayName = 'A';
    await (el as any).handleCustomRegister();
    await el.updateComplete;

    const expectedBase =
      window.location.hostname === 'preloop.ai'
        ? 'https://preloop.ai/openai/v1'
        : `${window.location.origin}/openai/v1`;

    expect((el as any).buildGatewayBaseUrl()).to.equal(expectedBase);

    const copyButtons = Array.from(
      el.shadowRoot?.querySelectorAll('sl-copy-button') || []
    ) as Array<HTMLElement & { value: string }>;
    const baseButton = copyButtons.find((b) => b.value === expectedBase);
    expect(baseButton, 'base url copy button').to.exist;

    // Snippet copy button carries the per-run session header.
    const snippetButton = copyButtons.find((b) =>
      b.value.includes('X-Preloop-Session-Id')
    );
    expect(snippetButton, 'snippet copy button with session header').to.exist;
    expect(snippetButton!.value).to.contain(expectedBase);
  });

  it('register failure shows an inline error and allows retry', async () => {
    stubApi({ failRegister: true });
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;

    (el as any).customDisplayName = 'A';
    await (el as any).handleCustomRegister();
    await el.updateComplete;

    expect((el as any).customError).to.be.a('string').and.not.be.empty;
    expect((el as any).customSubStep).to.equal('name');
    expect((el as any).customBusy).to.equal(false);

    // Retry succeeds after the API recovers.
    stubApi();
    await (el as any).handleCustomRegister();
    await el.updateComplete;
    expect((el as any).customSubStep).to.equal('result');
    expect((el as any).customError).to.equal('');
  });

  it('mint failure shows an inline error', async () => {
    stubApi({ failMint: true });
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;

    (el as any).customDisplayName = 'A';
    await (el as any).handleCustomRegister();
    await el.updateComplete;

    expect((el as any).customError).to.be.a('string').and.not.be.empty;
    expect((el as any).customSubStep).to.equal('name');
  });

  it('blocks register when the display name is empty', async () => {
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;

    (el as any).customDisplayName = '   ';
    await (el as any).handleCustomRegister();
    await el.updateComplete;

    expect((el as any).customError).to.contain('required');
    const postCalls = fetchStub
      .getCalls()
      .filter((c) => (c.args[1] as RequestInit)?.method === 'POST');
    expect(postCalls.length).to.equal(0);
  });

  it('back from result confirms before discarding the token; cancel keeps it', async () => {
    stubApi({ token: 'pl_gw_KEEP' });
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;
    (el as any).customDisplayName = 'A';
    await (el as any).handleCustomRegister();
    await el.updateComplete;
    expect((el as any).customSubStep).to.equal('result');

    // User cancels the confirm -> stays on the result screen, token retained.
    confirmStub.returns(false);
    (el as any).handleBack();
    await el.updateComplete;
    expect(confirmStub).to.have.been.calledOnce;
    expect((el as any).customSubStep).to.equal('result');
    expect((el as any).customCredentialToken).to.equal('pl_gw_KEEP');

    // User confirms -> token discarded, returns to name form.
    confirmStub.returns(true);
    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).customSubStep).to.equal('name');
    expect((el as any).customCredentialToken).to.equal(null);
  });

  it('back from the name substep returns to the govern sub-choice', async () => {
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('custom');

    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('govern');
    // No confirm needed when leaving the name substep.
    expect(confirmStub).not.to.have.been.called;
  });

  // Drive the custom path to the model-selection step with the given models
  // seeded, and a name entered.
  async function goToModelStep(
    models: Record<string, unknown>[]
  ): Promise<PreloopDeployWizard> {
    const el = await createWizard();
    (el as any).aiModels = models;
    (await findCustomCard(el))!.click();
    await el.updateComplete;
    (el as any).customDisplayName = 'Support agent';
    (el as any).handleCustomContinueToModels();
    await el.updateComplete;
    return el;
  }

  it('shows the model step with gateway-enabled models and requires a selection', async () => {
    stubApi();
    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
      gatewayModel('m2', 'gpt-4o-mini'),
    ]);

    expect((el as any).customSubStep).to.equal('models');
    const select = el.shadowRoot?.querySelector(
      'sl-select[name="allowed_models"]'
    );
    expect(select, 'model multi-select renders').to.exist;
    const options = el.shadowRoot?.querySelectorAll('sl-option') || [];
    expect(options.length).to.equal(2);

    // With models available but none selected, register is blocked.
    await (el as any).handleCustomRegister();
    await el.updateComplete;
    expect((el as any).customError).to.contain('at least one');
    expect((el as any).customSubStep).to.equal('models');
    const bindingCalls = fetchStub
      .getCalls()
      .filter((c) => /\/model-bindings$/.test(String(c.args[0])));
    expect(bindingCalls.length).to.equal(0);
  });

  it('filters out non-gateway and non-llm models', async () => {
    stubApi();
    const directModel = gatewayModel('d1', 'direct');
    (directModel.meta_data as any) = { gateway: { enabled: false } };
    const sttModel = gatewayModel('s1', 'whisper');
    (sttModel as any).model_kind = 'stt';

    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
      directModel,
      sttModel,
    ]);

    expect(
      (el as any).gatewayEnabledModels().map((m: any) => m.id)
    ).to.deep.equal(['m1']);
  });

  it('PUTs model bindings after register and before mint, and uses the alias in the snippet', async () => {
    stubApi({ agentId: 'agent-xyz', token: 'pl_gw_TOK' });
    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
      gatewayModel('m2', 'gpt-4o-mini'),
    ]);
    (el as any).customSelectedModelIds = ['m1', 'm2'];

    await (el as any).handleCustomRegister();
    await el.updateComplete;

    // Ordering: register POST, then bindings PUT, then mint POST.
    const calls = fetchStub.getCalls();
    const indexOf = (re: RegExp, method: string) =>
      calls.findIndex(
        (c) =>
          re.test(String(c.args[0])) &&
          (c.args[1] as RequestInit)?.method === method
      );
    const registerIdx = indexOf(/\/api\/v1\/agents$/, 'POST');
    const bindingsIdx = indexOf(
      /\/api\/v1\/agents\/agent-xyz\/model-bindings$/,
      'PUT'
    );
    const mintIdx = indexOf(
      /\/api\/v1\/agents\/agent-xyz\/credentials$/,
      'POST'
    );
    expect(registerIdx).to.be.greaterThan(-1);
    expect(bindingsIdx).to.be.greaterThan(registerIdx);
    expect(mintIdx).to.be.greaterThan(bindingsIdx);

    // Bindings request shape.
    const bindingsBody = JSON.parse(
      (calls[bindingsIdx].args[1] as RequestInit).body as string
    );
    expect(bindingsBody.bindings).to.be.an('array').with.length(2);
    expect(bindingsBody.bindings[0].ai_model_id).to.equal('m1');
    expect(bindingsBody.bindings[0].gateway_alias).to.equal('openai/gpt-4o');
    expect(bindingsBody.bindings[0].is_primary).to.equal(true);
    // Second model has no explicit alias -> default provider/identifier.
    expect(bindingsBody.bindings[1].gateway_alias).to.equal(
      'openai/gpt-4o-mini'
    );

    // Snippet uses the primary alias.
    expect((el as any).customSubStep).to.equal('result');
    expect((el as any).customModelAlias).to.equal('openai/gpt-4o');
    const snippet = (el as any).buildCustomSnippet(
      'http://x/openai/v1',
      'pl_gw_TOK'
    );
    expect(snippet).to.contain('model="openai/gpt-4o"');

    (el as any).cancelFirstDataPolling();
  });

  it('renders the optional tags input on the name substep', async () => {
    const el = await createWizard();
    (await findCustomCard(el))!.click();
    await el.updateComplete;
    expect((el as any).customSubStep).to.equal('name');

    const tagsInput = el.shadowRoot?.querySelector('sl-input[name="tags"]');
    expect(tagsInput, 'tags input should render on the name substep').to.exist;
  });

  it('PATCHes parsed tags after register/bindings and before mint', async () => {
    stubApi({ agentId: 'agent-xyz', token: 'pl_gw_TOK' });
    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
    ]);
    (el as any).customSelectedModelIds = ['m1'];
    // key=value, a second pair, and a bare boolean label.
    (el as any).customTagsInput = 'env=prod team=support beta';

    await (el as any).handleCustomRegister();
    await el.updateComplete;

    const calls = fetchStub.getCalls();
    const indexOf = (re: RegExp, method: string) =>
      calls.findIndex(
        (c) =>
          re.test(String(c.args[0])) &&
          (c.args[1] as RequestInit)?.method === method
      );
    const bindingsIdx = indexOf(
      /\/api\/v1\/agents\/agent-xyz\/model-bindings$/,
      'PUT'
    );
    const tagsIdx = indexOf(/\/api\/v1\/agents\/agent-xyz$/, 'PATCH');
    const mintIdx = indexOf(
      /\/api\/v1\/agents\/agent-xyz\/credentials$/,
      'POST'
    );
    expect(tagsIdx, 'tags PATCH should be issued').to.be.greaterThan(-1);
    // Ordering: bindings PUT -> tags PATCH -> mint POST.
    expect(tagsIdx).to.be.greaterThan(bindingsIdx);
    expect(mintIdx).to.be.greaterThan(tagsIdx);

    // Body shape: a flat Record<string,string>, bare token -> "true".
    const tagsBody = JSON.parse(
      (calls[tagsIdx].args[1] as RequestInit).body as string
    );
    expect(tagsBody.tags).to.deep.equal({
      env: 'prod',
      team: 'support',
      beta: 'true',
    });

    expect((el as any).customSubStep).to.equal('result');
    (el as any).cancelFirstDataPolling();
  });

  it('skips the tags PATCH entirely when no tags are entered', async () => {
    stubApi({ agentId: 'agent-xyz' });
    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
    ]);
    (el as any).customSelectedModelIds = ['m1'];
    (el as any).customTagsInput = '   ';

    await (el as any).handleCustomRegister();
    await el.updateComplete;

    const patchCalls = fetchStub
      .getCalls()
      .filter(
        (c) =>
          /\/api\/v1\/agents\/agent-xyz$/.test(String(c.args[0])) &&
          (c.args[1] as RequestInit)?.method === 'PATCH'
      );
    expect(patchCalls.length).to.equal(0);
    // The flow still completes through to the result screen.
    expect((el as any).customSubStep).to.equal('result');
    (el as any).cancelFirstDataPolling();
  });

  it('waits for first data, then on detected activity shows success and dispatches done', async () => {
    stubApi({
      agentId: 'agent-xyz',
      pollsUntilActive: 1,
      activeRequestCount: 2,
    });
    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
    ]);
    (el as any).customSelectedModelIds = ['m1'];

    let doneFired = false;
    el.addEventListener('deploy-wizard-done', () => {
      doneFired = true;
    });

    await (el as any).handleCustomRegister();
    await el.updateComplete;
    expect((el as any).customSubStep).to.equal('result');
    expect((el as any).customConnState).to.equal('waiting');

    // Waiting status line renders a spinner.
    expect(el.shadowRoot?.querySelector('.conn-waiting sl-spinner')).to.exist;

    // First poll (after the interval) detects total_requests > 0.
    await waitUntil(
      () => (el as any).customConnState === 'connected',
      'agent should connect after first poll',
      { timeout: 6000 }
    );
    await el.updateComplete;
    expect((el as any).customConnRequestCount).to.equal(2);
    expect(el.shadowRoot?.textContent).to.contain('Agent connected');

    // After the success delay it auto-dismisses via deploy-wizard-done.
    await waitUntil(() => doneFired, 'should auto-dismiss', { timeout: 6000 });
    expect(doneFired).to.equal(true);
    expect((el as any).customCredentialToken).to.equal(null);
  });

  it('polling cap leaves a manual Done fallback that dispatches done', async () => {
    // Never connects (pollsUntilActive default Infinity).
    stubApi({ agentId: 'agent-xyz' });
    const el = await goToModelStep([
      gatewayModel('m1', 'gpt-4o', 'openai/gpt-4o'),
    ]);
    (el as any).customSelectedModelIds = ['m1'];

    let doneFired = false;
    el.addEventListener('deploy-wizard-done', () => {
      doneFired = true;
    });

    await (el as any).handleCustomRegister();
    await el.updateComplete;
    expect((el as any).customSubStep).to.equal('result');

    // Simulate the polling cap being reached.
    (el as any).customPollDeadline = Date.now() - 1;
    await waitUntil(
      () => (el as any).customPolling === false,
      'polling should stop at the cap',
      { timeout: 8000 }
    );
    expect((el as any).customConnState).to.equal('waiting');

    // Manual Done still works as a fallback.
    const doneButton = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') || []
    ).find((b) => b.textContent?.trim() === 'Done') as HTMLElement | undefined;
    expect(doneButton, 'manual Done button').to.exist;
    doneButton!.click();
    await el.updateComplete;
    expect(doneFired).to.equal(true);
  });
});

describe('PreloopDeployWizard CLI path polling', () => {
  let fetchStub: sinon.SinonStub;
  // Counts GET /api/v1/agents list polls so agents can "arrive" after the
  // baseline snapshot.
  let listCallCount = 0;

  function stubAgentsList(opts?: {
    // Agents present from the very first (baseline) fetch.
    baseline?: Array<Record<string, unknown>>;
    // Agents that appear from the Nth list call on (1-based).
    arrivals?: Array<Record<string, unknown>>;
    arriveOnCall?: number;
  }) {
    listCallCount = 0;
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (/\/api\/v1\/agents(\?|$)/.test(url) && method === 'GET') {
          listCallCount += 1;
          const arrived =
            listCallCount >= (opts?.arriveOnCall ?? 2)
              ? opts?.arrivals || []
              : [];
          const items = [...(opts?.baseline || []), ...arrived];
          return new Response(JSON.stringify({ total: items.length, items }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );
  }

  function agentSummary(id: string, name: string): Record<string, unknown> {
    return {
      id,
      display_name: name,
      session_source_type: 'claude_code',
      session_source_id: `host-${id}`,
      enrolled_via: 'cli',
      lifecycle_state: 'active',
      activity_status: 'idle',
      is_active_now: false,
      last_seen_at: '2026-07-18T00:00:00Z',
      total_requests: 0,
      estimated_cost: 0,
      managed_mcp_servers: [],
      tags: {},
    };
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    stubAgentsList();
  });

  afterEach(() => {
    document.querySelectorAll('preloop-deploy-wizard').forEach((el) => {
      (el as any).cancelFirstDataPolling?.();
      (el as any).cancelCliAgentPolling?.();
    });
    fetchStub.restore();
    localStorage.clear();
  });

  async function createCliWizard(): Promise<PreloopDeployWizard> {
    const el = (await fixture(
      html`<preloop-deploy-wizard initial-path="cli"></preloop-deploy-wizard>`
    )) as PreloopDeployWizard;
    await el.updateComplete;
    return el;
  }

  it('shows the waiting line and polls the agents list while the CLI screen is open', async () => {
    const el = await createCliWizard();
    expect((el as any).cliPollingActive).to.equal(true);
    const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain(
      'Waiting for the CLI. Onboarded agents appear here automatically.'
    );
    // The baseline snapshot fetch fires immediately.
    await waitUntil(() => listCallCount >= 1, 'baseline fetch', {
      timeout: 3000,
    });
  });

  it('flips to "✓ <agent> connected" as CLI-onboarded agents land', async () => {
    stubAgentsList({
      arrivals: [agentSummary('a1', 'Claude Code')],
      arriveOnCall: 2,
    });
    const el = await createCliWizard();

    await waitUntil(
      () => (el as any).cliConnectedAgents.length === 1,
      'agent should be detected after the baseline',
      { timeout: 8000 }
    );
    await el.updateComplete;

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('✓ Claude Code connected');
    expect(text).to.contain('View it on the Agents page');
    const link = el.shadowRoot?.querySelector('a.cli-connected-link');
    expect(link?.getAttribute('href')).to.equal('/console/agents');
    // Polling continues so further agents can land.
    expect((el as any).cliPollingActive).to.equal(true);
  });

  it('does not count agents that existed before the CLI screen opened', async () => {
    stubAgentsList({
      baseline: [agentSummary('old', 'Existing Agent')],
      arrivals: [agentSummary('new', 'Codex CLI')],
      arriveOnCall: 2,
    });
    const el = await createCliWizard();

    await waitUntil(
      () => (el as any).cliConnectedAgents.length === 1,
      'only the new arrival should be detected',
      { timeout: 8000 }
    );
    await el.updateComplete;

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('✓ Codex CLI connected');
    expect(text).to.not.contain('✓ Existing Agent connected');
  });

  it('stops polling when navigating away from the CLI screen', async () => {
    const el = await createCliWizard();
    expect((el as any).cliPollingActive).to.equal(true);

    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('govern');
    expect((el as any).cliPollingActive).to.equal(false);
    expect((el as any).cliPollTimer).to.equal(null);
  });

  it('stops polling quietly at the cap but keeps connected lines', async () => {
    stubAgentsList({
      arrivals: [agentSummary('a1', 'Claude Code')],
      arriveOnCall: 2,
    });
    const el = await createCliWizard();
    await waitUntil(() => (el as any).cliConnectedAgents.length === 1, '', {
      timeout: 8000,
    });

    // Simulate the deadline being hit before the next poll.
    (el as any).cliPollDeadline = Date.now() - 1;
    await waitUntil(
      () => (el as any).cliPollingActive === false,
      'polling should stop at the cap',
      { timeout: 8000 }
    );
    await el.updateComplete;

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('✓ Claude Code connected');
    expect(text).to.not.contain('Waiting for the CLI');
  });

  it('sets an absolute hard stop beyond the initial soft deadline', async () => {
    const before = Date.now();
    const el = await createCliWizard();
    expect((el as any).cliPollHardStop).to.be.greaterThan(
      (el as any).cliPollDeadline
    );
    // Soft window ~3m; hard cap ~10m from start.
    expect((el as any).cliPollHardStop - before).to.be.at.least(9 * 60 * 1000);
  });
});

/**
 * Step progress, the single-primary action bar, and the phone layout.
 *
 * The wizard used to render the same static dialog label on every screen with
 * no sense of position, and clipped long commands with `white-space: nowrap`
 * inside an `overflow-x: auto` box, which is invisible to a page-level "does
 * it scroll sideways" check. These tests pin both.
 */
describe('PreloopDeployWizard step progress and phone layout', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
  });

  afterEach(() => {
    document.querySelectorAll('preloop-deploy-wizard').forEach((el) => {
      (el as any).cancelFirstDataPolling?.();
      (el as any).cancelCliAgentPolling?.();
    });
    fetchStub.restore();
    localStorage.clear();
  });

  const PHONE_WIDTH = 390;

  /** Mount the wizard in a phone-width column, the way a 390px viewport does. */
  async function mountNarrow(
    initialPath?: string
  ): Promise<PreloopDeployWizard> {
    const host = (await fixture(html`
      <div style="width: ${PHONE_WIDTH}px;">
        <preloop-deploy-wizard
          initial-path=${initialPath || 'choose'}
        ></preloop-deploy-wizard>
      </div>
    `)) as HTMLElement;
    const el = host.querySelector(
      'preloop-deploy-wizard'
    ) as PreloopDeployWizard;
    await el.updateComplete;
    return el;
  }

  function stepText(el: PreloopDeployWizard): string {
    return (
      el.shadowRoot?.querySelector('.wizard-step-count')?.textContent || ''
    )
      .replace(/\s+/g, ' ')
      .trim();
  }

  function rail(el: PreloopDeployWizard): HTMLElement | null {
    return el.shadowRoot?.querySelector('.wizard-step-rail') || null;
  }

  function cardByText(
    el: PreloopDeployWizard,
    text: string
  ): HTMLElement | undefined {
    return (
      Array.from(
        el.shadowRoot?.querySelectorAll('.wizard-option-button') || []
      ) as HTMLElement[]
    ).find((b) => b.textContent?.includes(text));
  }

  /**
   * Nothing inside the component may stick out past the column it was given.
   * Measured on the rendered boxes rather than on the document, because the
   * clipping this guards against happens inside the component.
   */
  function expectNoHorizontalOverflow(el: PreloopDeployWizard, step: string) {
    const shell = el.shadowRoot?.querySelector('.wizard-shell') as HTMLElement;
    expect(shell, `${step}: shell renders`).to.exist;
    expect(shell.scrollWidth, `${step}: shell scrollWidth`).to.be.at.most(
      PHONE_WIDTH + 1
    );
    const right = el.getBoundingClientRect().right;
    Array.from(el.shadowRoot?.querySelectorAll('*') || []).forEach((node) => {
      const rect = (node as HTMLElement).getBoundingClientRect();
      if (rect.width === 0) {
        return;
      }
      expect(
        Math.round(rect.right),
        `${step}: ${(node as HTMLElement).className || node.tagName} right edge`
      ).to.be.at.most(Math.round(right) + 1);
    });
    // A command must wrap, not scroll: a clipped command cannot be read.
    Array.from(el.shadowRoot?.querySelectorAll('.command-code') || []).forEach(
      (code) => {
        const box = code as HTMLElement;
        expect(box.scrollWidth, `${step}: command block`).to.be.at.most(
          box.clientWidth + 1
        );
      }
    );
  }

  it('numbers the choose path and draws a rail only once the total is known', async () => {
    const el = await mountNarrow('choose');
    expect(stepText(el)).to.equal('Step 1');
    expect(rail(el), 'no rail on a branch screen').to.equal(null);

    cardByText(el, 'Govern Existing Agents')!.click();
    await el.updateComplete;
    expect(stepText(el)).to.equal('Step 2');
    expect(rail(el)).to.equal(null);

    cardByText(el, 'Autodiscover via CLI')!.click();
    await el.updateComplete;
    expect(stepText(el)).to.contain('Step 3 of 3');
    expect(rail(el)!.children.length).to.equal(3);
    expect(rail(el)!.querySelectorAll('.done').length).to.equal(3);
  });

  it('counts the custom path from the step the wizard was opened on', async () => {
    // Deep-linked at govern (the agents-view dialog): govern is step 1, so the
    // custom path runs 2, 3, 4 rather than repeating a phantom step 1.
    const el = await mountNarrow('govern');
    expect(stepText(el)).to.equal('Step 1');

    cardByText(el, 'Connect a custom agent')!.click();
    await el.updateComplete;
    expect(stepText(el)).to.contain('Step 2 of 4');
    expect(rail(el)!.querySelectorAll('.done').length).to.equal(2);

    (el as any).customDisplayName = 'Support triage agent';
    (el as any).handleCustomContinueToModels();
    await el.updateComplete;
    expect(stepText(el)).to.contain('Step 3 of 4');
    expect(rail(el)!.querySelectorAll('.done').length).to.equal(3);
  });

  it('gives each step one primary action with Back as text beside it', async () => {
    const el = await mountNarrow('govern');
    cardByText(el, 'Connect a custom agent')!.click();
    await el.updateComplete;

    const actions = el.shadowRoot?.querySelector(
      '.wizard-actions'
    ) as HTMLElement;
    expect(actions, 'action bar').to.exist;
    const primaries = actions.querySelectorAll('sl-button[variant="primary"]');
    expect(primaries.length, 'exactly one primary').to.equal(1);
    expect(primaries[0].textContent?.trim()).to.equal('Continue');
    const back = actions.querySelector('.wizard-back') as HTMLElement;
    expect(back, 'back button').to.exist;
    expect(back.getAttribute('variant')).to.equal('text');
  });

  it('states what is about to be registered before minting a credential', async () => {
    const el = await mountNarrow('govern');
    cardByText(el, 'Connect a custom agent')!.click();
    await el.updateComplete;
    (el as any).customDisplayName = 'Support triage agent';
    (el as any).customTagsInput = 'env=prod';
    (el as any).handleCustomContinueToModels();
    await el.updateComplete;

    const summary = el.shadowRoot?.querySelector('.wizard-summary');
    expect(summary, 'summary block').to.exist;
    const text = (summary?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('Support triage agent');
    expect(text).to.contain('env=prod');
    expect(text).to.contain('shown once');
  });

  it('does not overflow horizontally at 390px on any step', async () => {
    const choose = await mountNarrow('choose');
    expectNoHorizontalOverflow(choose, 'choose');

    const govern = await mountNarrow('govern');
    expectNoHorizontalOverflow(govern, 'govern');

    const cli = await mountNarrow('cli');
    await cli.updateComplete;
    expectNoHorizontalOverflow(cli, 'cli');

    const custom = await mountNarrow('custom');
    expectNoHorizontalOverflow(custom, 'custom-name');

    (custom as any).customDisplayName = 'Support triage agent';
    (custom as any).handleCustomContinueToModels();
    await custom.updateComplete;
    expectNoHorizontalOverflow(custom, 'custom-models');

    // The result screen carries the longest strings in the wizard: a minted
    // token and a multi-line snippet.
    (custom as any).customCredentialToken =
      'pl_gw_9f3c2a7e5b1d4c8f6a0e2b7d9c4f1a3e0b8d6c4a2f1e9b7d5c3a1f0e8d6c4b2';
    (custom as any).customModelAlias = 'openai/gpt-4o';
    (custom as any).customSubStep = 'result';
    await custom.updateComplete;
    expectNoHorizontalOverflow(custom, 'custom-result');
  });
});
