import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './tool-cost-flags-panel.ts';
import type { ToolCostFlagsPanel } from './tool-cost-flags-panel';
import type { ToolCostFlag } from '../api';

const makeFlag = (overrides: Partial<ToolCostFlag> = {}): ToolCostFlag => ({
  id: 'flag-1',
  tool_name: 'search_web',
  tool_source: 'mcp',
  flag_kind: 'oversized_definition',
  evidence: { claim: 'This tool adds 4,200 tokens to every request.' },
  estimated_weekly_cost: 12.5,
  status: 'open',
  disable_eligible: true,
  window_start: '2026-06-01T00:00:00Z',
  window_end: '2026-06-08T00:00:00Z',
  ...overrides,
});

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('ToolCostFlagsPanel', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  const mount = async (): Promise<ToolCostFlagsPanel> => {
    const element = (await fixture(
      html`<tool-cost-flags-panel></tool-cost-flags-panel>`
    )) as ToolCostFlagsPanel;
    await element.updateComplete;
    return element;
  };

  it('renders a list from a bare-array response shape', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse([
        makeFlag(),
        makeFlag({ id: 'flag-2', tool_name: 'read_file' }),
      ])
    );
    const element = await mount();
    await waitUntil(
      () => element.shadowRoot?.querySelectorAll('.flag-row').length === 2,
      'flags should render'
    );
    const rows = element.shadowRoot?.querySelectorAll('.flag-row');
    expect(rows?.length).to.equal(2);
    expect(element.shadowRoot?.textContent).to.contain('search_web');
    expect(element.shadowRoot?.textContent).to.contain('read_file');
    // Source badge and claim are rendered.
    expect(element.shadowRoot?.textContent).to.contain('mcp');
    expect(element.shadowRoot?.textContent).to.contain('adds 4,200 tokens');
  });

  it('renders a list from a { flags: [...] } envelope response shape', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse({ flags: [makeFlag({ tool_name: 'envelope_tool' })] })
    );
    const element = await mount();
    await waitUntil(
      () => element.shadowRoot?.querySelectorAll('.flag-row').length === 1,
      'envelope flags should render'
    );
    expect(element.shadowRoot?.textContent).to.contain('envelope_tool');
  });

  it('shows the empty state when there are no flags', async () => {
    fetchStub.callsFake(async () => jsonResponse([]));
    const element = await mount();
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('.empty'),
      'empty state should render'
    );
    expect(element.shadowRoot?.querySelector('.empty')?.textContent).to.contain(
      'look efficient'
    );
  });

  it('shows a loading state before the fetch resolves', async () => {
    let resolveFetch: (value: Response) => void = () => {};
    fetchStub.callsFake(
      async () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        })
    );
    const element = await mount();
    // The fetch is still pending, so the loading state must be visible.
    expect(element.shadowRoot?.querySelector('.loading-state')).to.exist;
    resolveFetch(jsonResponse([]));
  });

  it('shows an error alert with a retry control on fetch failure', async () => {
    fetchStub.callsFake(async () => new Response('boom', { status: 500 }));
    const element = await mount();
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('.error-state'),
      'error state should render'
    );
    const retry = element.shadowRoot?.querySelector<HTMLElement>(
      '.error-state sl-button'
    );
    expect(retry).to.exist;
    expect(retry?.textContent).to.contain('Retry');
  });

  it('dismiss calls the endpoint and removes the row', async () => {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/dismiss') && method === 'POST') {
          return new Response(null, { status: 204 });
        }
        return jsonResponse([makeFlag()]);
      }
    );
    const element = await mount();
    await waitUntil(
      () => element.shadowRoot?.querySelectorAll('.flag-row').length === 1,
      'flag should render'
    );

    element.shadowRoot
      ?.querySelector<HTMLElement>('.flag-row sl-button[aria-label^="Dismiss"]')
      ?.click();

    await waitUntil(
      () => element.shadowRoot?.querySelectorAll('.flag-row').length === 0,
      'row should be removed after dismiss'
    );

    const dismissCall = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/flag-1/dismiss'));
    expect(dismissCall, 'dismiss endpoint should be called').to.exist;
    expect((dismissCall?.args[1]?.method || '').toUpperCase()).to.equal('POST');
  });

  it('restores the row and shows an error when dismiss fails', async () => {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/dismiss') && method === 'POST') {
          return jsonResponse({ detail: 'Dismiss failed' }, 500);
        }
        return jsonResponse([makeFlag()]);
      }
    );
    const element = await mount();
    await waitUntil(
      () => element.shadowRoot?.querySelectorAll('.flag-row').length === 1,
      'flag should render'
    );

    element.shadowRoot
      ?.querySelector<HTMLElement>('.flag-row sl-button[aria-label^="Dismiss"]')
      ?.click();

    await waitUntil(
      () => !!element.shadowRoot?.querySelector('sl-alert[variant="danger"]'),
      'error alert should appear'
    );
    // Row restored after the failure.
    expect(element.shadowRoot?.querySelectorAll('.flag-row').length).to.equal(
      1
    );
    expect(
      element.shadowRoot?.querySelector('sl-alert[variant="danger"]')
        ?.textContent
    ).to.contain('Dismiss failed');
  });

  it('hides any disable affordance and shows a muted note when disable_eligible is false', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse([makeFlag({ disable_eligible: false })])
    );
    const element = await mount();
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('.muted-note'),
      'muted note should render'
    );
    expect(
      element.shadowRoot?.querySelector('.muted-note')?.textContent
    ).to.contain('One-click disable unavailable');
    // No disable button is ever rendered — only the Dismiss action exists.
    const buttons = Array.from(
      element.shadowRoot?.querySelectorAll('.flag-row sl-button') || []
    );
    const labels = buttons.map((b) => b.textContent?.trim() || '');
    expect(labels.some((l) => /disable/i.test(l))).to.be.false;
    expect(labels.some((l) => /dismiss/i.test(l))).to.be.true;
  });

  it('falls back to a constructed claim when evidence.claim is absent', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse([makeFlag({ evidence: {}, estimated_weekly_cost: 8 })])
    );
    const element = await mount();
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('.flag-claim'),
      'claim should render'
    );
    expect(
      element.shadowRoot?.querySelector('.flag-claim')?.textContent
    ).to.contain('$8.00/week');
  });
});
