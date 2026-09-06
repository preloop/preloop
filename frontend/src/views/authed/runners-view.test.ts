import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import type SlSelect from '@shoelace-style/shoelace/dist/components/select/select.js';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './runners-view';
import type { RunnersView } from './runners-view';
import { invalidateApiCaches } from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('RunnersView', () => {
  let fetchStub: sinon.SinonStub;
  let onRunnerMessage: ((message: unknown) => void) | undefined;

  function createFetchStub(
    runners: unknown[] = [],
    account: { default_runner_pool?: string | null } = {},
    options: { failPatch?: boolean } = {}
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (data: unknown, status = 200) =>
          new Response(JSON.stringify(data), {
            status,
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.includes('/api/v1/runners')) {
          return json(runners);
        }
        if (url.includes('/api/v1/account/details')) {
          const method = String(init?.method || 'GET').toUpperCase();
          if (method === 'PATCH') {
            if (options.failPatch) {
              return json(
                { detail: 'Failed to update account organization' },
                400
              );
            }
            const body = JSON.parse(String(init?.body || '{}')) as {
              default_runner_pool?: string | null;
            };
            return json({
              id: 'acct-1',
              organization_name: 'Example Org',
              default_runner_pool: body.default_runner_pool ?? null,
              created_at: '2026-09-04T00:00:00Z',
              updated_at: '2026-09-04T00:00:00Z',
            });
          }
          return json({
            id: 'acct-1',
            organization_name: 'Example Org',
            default_runner_pool: account.default_runner_pool ?? null,
            created_at: '2026-09-04T00:00:00Z',
            updated_at: '2026-09-04T00:00:00Z',
          });
        }
        return json({ detail: `Unhandled: ${url}` });
      });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    onRunnerMessage = undefined;
    sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake((_topic: string, callback: (message: unknown) => void) => {
        onRunnerMessage = callback;
        return () => undefined;
      });
  });

  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('renders the runners list', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'online',
        last_heartbeat: '2026-08-17T10:00:00Z',
        current_execution_id: '22222222-2222-4222-8222-222222222222',
        registered_by_email: 'ops@example.com',
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;

    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading,
      'Runners view did not finish loading'
    );
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    expect(header?.getAttribute('headerText')).to.equal('Runners');
    expect(element.shadowRoot?.textContent).to.contain('office-mac');
    expect(element.shadowRoot?.textContent).to.contain('ops@example.com');
    expect(element.shadowRoot?.textContent).to.contain('local');
    const control = element.shadowRoot?.querySelector(
      'preloop-runner-pool-select'
    );
    expect(control).to.exist;
    expect(control?.shadowRoot?.textContent).to.contain(
      'Auto (default): private first, then hosted'
    );
    expect(control?.shadowRoot?.textContent).to.contain('Preloop hosted only');
  });

  it('empty state is one line with one command and a docs link', async () => {
    fetchStub = createFetchStub([]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('No runners registered');

    const empty = element.shadowRoot?.querySelector(
      '.empty-state'
    ) as HTMLElement;
    expect(empty).to.exist;
    // The console recipe: one centred line in a 72px box, not a hero card.
    const box = empty.getBoundingClientRect();
    expect(box.height).to.be.at.least(72);
    expect(box.height).to.be.lessThan(80);
    const style = getComputedStyle(empty);
    expect(style.justifyContent).to.equal('center');
    expect(style.flexDirection).to.equal('row');
    expect(style.boxSizing).to.equal('border-box');
    expect(style.marginTop).to.equal('0px');

    const commands = empty.querySelectorAll('.empty-command');
    expect(commands.length).to.equal(1);
    expect(commands[0].textContent).to.contain(
      'preloop runner fg --labels local'
    );
    expect(empty.querySelectorAll('sl-copy-button').length).to.equal(1);

    const docs = empty.querySelector('a.empty-docs');
    expect(docs).to.exist;
    expect(docs?.getAttribute('rel')).to.equal('noopener noreferrer');
    expect(docs?.getAttribute('target')).to.equal('_blank');
    expect(element.shadowRoot?.querySelector('sl-card')).to.not.exist;
  });

  it('updates status from a runners websocket event without a refetch', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'offline',
        last_heartbeat: '2026-08-17T10:00:00Z',
        current_execution_id: null,
        registered_by_email: 'ops@example.com',
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('Offline');

    onRunnerMessage?.({
      type: 'runner_updated',
      payload: {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        status: 'online',
        last_heartbeat: '2026-09-03T10:00:00Z',
      },
    });
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('Online');
    expect(
      fetchStub.getCalls().filter((call) => {
        const url = String(call.args[0]);
        return url.includes('/api/v1/runners');
      })
    ).to.have.lengthOf(1);
  });

  it('saves the account default runner pool', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'online',
        last_heartbeat: '2026-08-17T10:00:00Z',
        current_execution_id: null,
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;

    const control = element.shadowRoot?.querySelector(
      'preloop-runner-pool-select'
    ) as HTMLElement;
    const select = control.shadowRoot?.querySelector('sl-select') as SlSelect;
    select.value = 'server';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some(
          (call) => String(call.args[1]?.method || '').toUpperCase() === 'PATCH'
        )
    );
    const patch = fetchStub.getCalls().find((call) => {
      const init = call.args[1] as RequestInit | undefined;
      return String(init?.method || '').toUpperCase() === 'PATCH';
    });
    expect(patch).to.exist;
    expect(String(patch?.args[0])).to.contain('/api/v1/account/details');
    expect(
      JSON.parse(String((patch?.args[1] as RequestInit).body))
    ).to.deep.equal({ default_runner_pool: 'server' });
  });

  it('keeps an offline saved default visible in the select', async () => {
    fetchStub = createFetchStub([], { default_runner_pool: 'office-mac' });
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;

    const control = element.shadowRoot?.querySelector(
      'preloop-runner-pool-select'
    ) as HTMLElement;
    const select = control.shadowRoot?.querySelector('sl-select') as SlSelect;
    const values = Array.from(select.querySelectorAll('sl-option')).map(
      (option) => option.getAttribute('value')
    );
    expect(values).to.include('office-mac');
    expect(select.value).to.equal('office-mac');
  });

  it('saves Auto as a null account default runner pool', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'online',
        last_heartbeat: '2026-08-17T10:00:00Z',
        current_execution_id: null,
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;

    const control = element.shadowRoot?.querySelector(
      'preloop-runner-pool-select'
    ) as HTMLElement;
    const select = control.shadowRoot?.querySelector('sl-select') as SlSelect;
    select.value = 'auto';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some(
          (call) => String(call.args[1]?.method || '').toUpperCase() === 'PATCH'
        )
    );
    const patch = fetchStub.getCalls().find((call) => {
      const init = call.args[1] as RequestInit | undefined;
      return String(init?.method || '').toUpperCase() === 'PATCH';
    });
    expect(
      JSON.parse(String((patch?.args[1] as RequestInit).body))
    ).to.deep.equal({ default_runner_pool: null });
  });

  it('restores the saved default when the PATCH fails', async () => {
    fetchStub = createFetchStub(
      [],
      { default_runner_pool: 'office-mac' },
      {
        failPatch: true,
      }
    );
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;

    const control = element.shadowRoot?.querySelector(
      'preloop-runner-pool-select'
    ) as HTMLElement;
    const select = control.shadowRoot?.querySelector('sl-select') as SlSelect;
    expect(select.value).to.equal('office-mac');
    select.value = 'server';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() =>
      Boolean(
        element.shadowRoot?.textContent?.includes(
          'Failed to update account organization'
        )
      )
    );
    await element.updateComplete;
    const restored = control.shadowRoot?.querySelector('sl-select') as SlSelect;
    expect(restored.value).to.equal('office-mac');
  });

  it('shows registered-by email for a runner that arrives over websocket', async () => {
    fetchStub = createFetchStub([]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('No runners registered');

    onRunnerMessage?.({
      type: 'runner_updated',
      payload: {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'online',
        last_heartbeat: '2026-09-03T10:00:00Z',
        registered_by_email: 'ops@example.com',
      },
    });
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).to.contain('office-mac');
    expect(element.shadowRoot?.textContent).to.contain('ops@example.com');
  });
});
