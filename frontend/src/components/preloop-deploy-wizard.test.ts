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
    fetchStub.restore();
    confirmStub.restore();
    localStorage.clear();
  });

  // Configures register (POST /agents) and credential mint
  // (POST /agents/{id}/credentials) responses. Pass failRegister/failMint to
  // exercise error paths.
  function stubApi(opts?: {
    agentId?: string;
    token?: string;
    failRegister?: boolean;
    failMint?: boolean;
  }) {
    const agentId = opts?.agentId || 'agent-123';
    const token = opts?.token || 'pl_gw_secret_token_value';
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

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

  async function createWizard(): Promise<PreloopDeployWizard> {
    const el = (await fixture(
      html`<preloop-deploy-wizard></preloop-deploy-wizard>`
    )) as PreloopDeployWizard;
    await el.updateComplete;
    return el;
  }

  function findCustomCard(el: PreloopDeployWizard): HTMLElement | undefined {
    const buttons = Array.from(
      el.shadowRoot?.querySelectorAll('.wizard-option-button') || []
    ) as HTMLElement[];
    return buttons.find((b) =>
      b.textContent?.includes('Connect a custom agent')
    );
  }

  it('renders the third "Connect a custom agent" card and routes to the custom path', async () => {
    const el = await createWizard();
    const card = findCustomCard(el);
    expect(card, 'third card should exist').to.exist;
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

  it('happy path: registers, mints credential, and shows snippet with token', async () => {
    stubApi({ agentId: 'agent-xyz', token: 'pl_gw_TESTTOKEN' });
    const el = await createWizard();
    findCustomCard(el)!.click();
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
    findCustomCard(el)!.click();
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
    findCustomCard(el)!.click();
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
    findCustomCard(el)!.click();
    await el.updateComplete;

    (el as any).customDisplayName = 'A';
    await (el as any).handleCustomRegister();
    await el.updateComplete;

    expect((el as any).customError).to.be.a('string').and.not.be.empty;
    expect((el as any).customSubStep).to.equal('name');
  });

  it('blocks register when the display name is empty', async () => {
    const el = await createWizard();
    findCustomCard(el)!.click();
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
    findCustomCard(el)!.click();
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

  it('back from the name substep returns to the choose screen', async () => {
    const el = await createWizard();
    findCustomCard(el)!.click();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('custom');

    (el as any).handleBack();
    await el.updateComplete;
    expect((el as any).onboardingPath).to.equal('choose');
    // No confirm needed when leaving the name substep.
    expect(confirmStub).not.to.have.been.called;
  });
});
