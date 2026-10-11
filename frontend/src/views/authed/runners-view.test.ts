import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import type SlSelect from '@shoelace-style/shoelace/dist/components/select/select.js';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './runners-view';
import type { RunnersView } from './runners-view';
import { invalidateApiCaches } from '../../api';
import { resetConfirmDialogForTests } from '../../components/confirm-dialog';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

describe('RunnersView', () => {
  let fetchStub: sinon.SinonStub;
  let onRunnerMessage: ((message: unknown) => void) | undefined;

  function createFetchStub(
    runners: unknown[] = [],
    account: { default_runner_pool?: string | null } = {},
    options: { failPatch?: boolean; deleteConflict?: boolean } = {}
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
        if (url.includes('/concurrency')) {
          const body = JSON.parse(String(init?.body || '{}')) as {
            concurrency?: number;
          };
          const runner = (runners[0] || {}) as Record<string, unknown>;
          return json({
            ...runner,
            concurrency: body.concurrency,
            capacity: body.concurrency,
          });
        }
        const method = String(init?.method || 'GET').toUpperCase();
        if (url.includes('/api/v1/runners/') && url.endsWith('/token')) {
          const runner = (runners[0] || {}) as Record<string, unknown>;
          return json({ ...runner, token: 'prl_runner_example' });
        }
        if (url.includes('/api/v1/runners/') && method === 'DELETE') {
          if (options.deleteConflict && !url.includes('force=true')) {
            return json(
              {
                detail:
                  'Runner holds 1 active execution(s). Stop them first, or retry with force=true to halt them and delete the runner.',
              },
              409
            );
          }
          return json({
            id: (runners[0] as { id?: string })?.id,
            deleted: true,
            halted_execution_ids: url.includes('force=true')
              ? ['22222222-2222-4222-8222-222222222222']
              : [],
          });
        }
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
    resetConfirmDialogForTests();
  });

  /**
   * Answers the console confirm dialog: clicks the button labelled `label`
   * (the confirm label, or "Cancel"). Returns the dialog's message text.
   */
  async function answerConfirm(label: string): Promise<string> {
    let button: HTMLElement | undefined;
    let dialog: HTMLElement | null = null;
    await waitUntil(() => {
      dialog = document.body.querySelector('confirm-dialog');
      button = [...(dialog?.shadowRoot?.querySelectorAll('sl-button') || [])]
        .map((candidate) => candidate as HTMLElement)
        .find((candidate) => candidate.textContent?.trim() === label);
      return Boolean(button);
    }, `confirm dialog with "${label}" did not open`);
    const text = (dialog as HTMLElement | null)?.shadowRoot?.textContent || '';
    button!.click();
    return text;
  }

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

  it('opens the new session dialog for an online runner', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        status: 'online',
        labels: [],
      },
      {
        id: '33333333-3333-4333-8333-333333333333',
        name: 'ci-once',
        status: 'online',
        ephemeral: true,
        labels: [],
      },
      {
        id: '44444444-4444-4444-8444-444444444444',
        name: 'old-box',
        status: 'offline',
        labels: [],
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

    const buttons = [
      ...element.shadowRoot!.querySelectorAll('sl-button.new-session'),
    ];
    // Ephemeral CI runners never host sessions.
    expect(buttons).to.have.length(2);
    expect(buttons[1].hasAttribute('disabled')).to.equal(true);
    const dialog = element.shadowRoot!.querySelector(
      'new-runner-session-dialog'
    )!;
    expect(dialog.hasAttribute('open')).to.equal(false);
    (buttons[0] as HTMLElement).click();
    await element.updateComplete;
    expect(dialog.hasAttribute('open')).to.equal(true);
    expect(dialog.getAttribute('runner-id')).to.equal(
      '11111111-1111-4111-8111-111111111111'
    );
  });

  it('badges an ephemeral runner only while it is connected', async () => {
    fetchStub = createFetchStub([
      {
        id: '33333333-3333-4333-8333-333333333333',
        name: 'ci-gha-4242',
        labels: ['ci-gha-4242'],
        ephemeral: true,
        status: 'online',
        last_heartbeat: '2026-09-16T10:00:00Z',
      },
      {
        id: '44444444-4444-4444-8444-444444444444',
        name: 'gone-ci',
        labels: ['ci-old'],
        ephemeral: true,
        status: 'offline',
        last_heartbeat: '2026-09-16T09:00:00Z',
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

    const rows = Array.from(
      element.shadowRoot?.querySelectorAll('tbody tr') || []
    );
    expect(rows.length).to.equal(2);
    expect(rows[0].textContent).to.contain('ephemeral');
    // The offline row is a leftover the sweeper has not reaped yet; calling
    // it ephemeral there would advertise a runner that cannot take a job.
    expect(rows[1].textContent).to.not.contain('ephemeral');
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

  it('shows running count against the runner slot ceiling', async () => {
    fetchStub = createFetchStub([
      {
        id: '11111111-1111-4111-8111-111111111111',
        name: 'office-mac',
        hostname: 'mac.local',
        os: 'darwin',
        arch: 'arm64',
        labels: ['local'],
        status: 'busy',
        last_heartbeat: '2026-08-17T10:00:00Z',
        concurrency: 2,
        reported_concurrency: 2,
        capacity: 2,
        running_count: 2,
        running_execution_ids: [
          '22222222-2222-4222-8222-222222222222',
          '33333333-3333-4333-8333-333333333333',
        ],
        current_execution_id: '22222222-2222-4222-8222-222222222222',
      },
    ]);
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;

    const slots = element.shadowRoot?.querySelector('.slot-count');
    expect(slots?.textContent?.replace(/\s+/g, ' ').trim()).to.equal('2 / 2');
    const links = Array.from(
      element.shadowRoot?.querySelectorAll('.executions a') || []
    ).map((link) => link.getAttribute('href'));
    expect(links).to.deep.equal([
      '/console/flows/executions/22222222-2222-4222-8222-222222222222',
      '/console/flows/executions/33333333-3333-4333-8333-333333333333',
    ]);
  });

  it('edits the runner slot ceiling', async () => {
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
        concurrency: 2,
        capacity: 2,
        running_count: 0,
        running_execution_ids: [],
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

    const edit = element.shadowRoot?.querySelector(
      '.slots sl-button'
    ) as HTMLElement;
    edit.click();
    await element.updateComplete;

    const input = element.shadowRoot?.querySelector(
      '.slot-edit sl-input'
    ) as HTMLInputElement;
    input.value = '4';
    const save = element.shadowRoot?.querySelector(
      '.slot-edit sl-button'
    ) as HTMLElement;
    save.click();

    await waitUntil(() =>
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/concurrency'))
    );
    const patch = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/concurrency'));
    expect(String(patch?.args[0])).to.contain(
      '/api/v1/runners/11111111-1111-4111-8111-111111111111/concurrency'
    );
    expect(
      String((patch?.args[1] as RequestInit).method).toUpperCase()
    ).to.equal('PATCH');
    expect(
      JSON.parse(String((patch?.args[1] as RequestInit).body))
    ).to.deep.equal({ concurrency: 4 });
    await waitUntil(() =>
      Boolean(
        element.shadowRoot
          ?.querySelector('.slot-count')
          ?.textContent?.includes('/ 4')
      )
    );
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
  const actionRunner = {
    id: '11111111-1111-4111-8111-111111111111',
    name: 'office-mac',
    hostname: 'mac.local',
    os: 'darwin',
    arch: 'arm64',
    labels: ['local'],
    status: 'online',
    last_heartbeat: '2026-09-27T10:00:00Z',
    running_execution_ids: [],
    current_execution_id: null,
  };

  async function loadedView(): Promise<RunnersView> {
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading
    );
    await element.updateComplete;
    return element;
  }

  function requestsMatching(
    predicate: (url: string, method: string) => boolean
  ) {
    return fetchStub
      .getCalls()
      .filter((call) =>
        predicate(
          String(call.args[0]),
          String(
            (call.args[1] as RequestInit | undefined)?.method || 'GET'
          ).toUpperCase()
        )
      );
  }

  it('deletes a runner after confirmation and drops its row', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const nativeConfirm = sinon.stub(window, 'confirm').returns(true);
    const element = await loadedView();

    (
      element.shadowRoot?.querySelector('.delete-runner') as HTMLElement
    ).click();
    const asked = await answerConfirm('Delete runner');
    expect(asked).to.contain('Delete runner office-mac?');
    expect(nativeConfirm.called, 'no native confirm').to.equal(false);
    await waitUntil(
      () => !element.shadowRoot?.textContent?.includes('office-mac'),
      'Deleted runner row did not disappear'
    );
    const deletes = requestsMatching((_url, method) => method === 'DELETE');
    expect(deletes).to.have.length(1);
    expect(String(deletes[0].args[0])).to.contain(
      '/api/v1/runners/11111111-1111-4111-8111-111111111111'
    );
    expect(String(deletes[0].args[0])).not.to.contain('force=true');
  });

  it('does nothing when the delete is not confirmed', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();

    (
      element.shadowRoot?.querySelector('.delete-runner') as HTMLElement
    ).click();
    await answerConfirm('Cancel');
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    expect(
      requestsMatching((_url, method) => method === 'DELETE')
    ).to.have.length(0);
    expect(element.shadowRoot?.textContent).to.contain('office-mac');
  });

  it('surfaces the active lease refusal and offers a force delete', async () => {
    fetchStub = createFetchStub([actionRunner], {}, { deleteConflict: true });
    const element = await loadedView();

    (
      element.shadowRoot?.querySelector('.delete-runner') as HTMLElement
    ).click();
    await answerConfirm('Delete runner');
    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.force-delete')),
      'Force delete was not offered'
    );
    expect(
      element.shadowRoot?.querySelector('.action-notice-text')?.textContent
    ).to.contain('Runner holds 1 active execution(s)');
    expect(element.shadowRoot?.textContent).to.contain('office-mac');

    (element.shadowRoot?.querySelector('.force-delete') as HTMLElement).click();
    const forceQuestion = await answerConfirm('Halt and delete');
    expect(forceQuestion).to.contain('Halt the executions office-mac');
    await waitUntil(
      () => !element.shadowRoot?.textContent?.includes('office-mac'),
      'Force deleted runner row did not disappear'
    );
    const forced = requestsMatching(
      (url, method) => method === 'DELETE' && url.includes('force=true')
    );
    expect(forced).to.have.length(1);
  });

  function spyOnConfirmAsks() {
    const ctor = customElements.get('confirm-dialog') as unknown as {
      prototype: { ask: (...args: unknown[]) => Promise<boolean> };
    };
    return sinon.spy(ctor.prototype, 'ask');
  }

  function actionButton(element: RunnersView, selector: string) {
    return element.shadowRoot?.querySelector(selector) as HTMLElement & {
      disabled: boolean;
    };
  }

  it('asks once and deletes once on a rapid double click', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();
    const asks = spyOnConfirmAsks();

    const remove = actionButton(element, '.delete-runner');
    remove.click();
    remove.click();
    await element.updateComplete;
    // The row is busy while its question is open: neither action can start
    // a second ask that would cancel the first.
    expect(actionButton(element, '.delete-runner').disabled).to.equal(true);
    expect(actionButton(element, '.rotate-token').disabled).to.equal(true);
    actionButton(element, '.rotate-token').click();

    await answerConfirm('Delete runner');
    await waitUntil(
      () => !element.shadowRoot?.textContent?.includes('office-mac'),
      'Deleted runner row did not disappear'
    );
    expect(asks.callCount).to.equal(1);
    expect(
      requestsMatching((_url, method) => method === 'DELETE')
    ).to.have.length(1);
    expect(
      requestsMatching(
        (url, method) => method === 'POST' && url.endsWith('/token')
      )
    ).to.have.length(0);
  });

  it('asks once and rotates once on a rapid double click', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();
    const asks = spyOnConfirmAsks();

    const rotate = actionButton(element, '.rotate-token');
    rotate.click();
    rotate.click();
    await answerConfirm('Rotate token');
    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.action-notice-text')),
      'Rotation notice did not appear'
    );
    expect(asks.callCount).to.equal(1);
    expect(
      requestsMatching(
        (url, method) => method === 'POST' && url.endsWith('/token')
      )
    ).to.have.length(1);
  });

  it('frees the row again when the question is cancelled', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();

    actionButton(element, '.delete-runner').click();
    await answerConfirm('Cancel');
    await waitUntil(
      () => !actionButton(element, '.delete-runner').disabled,
      'the row stayed busy after Cancel'
    );
    expect(actionButton(element, '.rotate-token').disabled).to.equal(false);
  });

  it('rotates the token without showing it', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();

    (element.shadowRoot?.querySelector('.rotate-token') as HTMLElement).click();
    await answerConfirm('Rotate token');
    await waitUntil(
      () => Boolean(element.shadowRoot?.querySelector('.action-notice-text')),
      'Rotation notice did not appear'
    );
    const rotations = requestsMatching(
      (url, method) => method === 'POST' && url.endsWith('/token')
    );
    expect(rotations).to.have.length(1);
    expect(String(rotations[0].args[0])).to.contain(
      '/api/v1/runners/11111111-1111-4111-8111-111111111111/token'
    );
    expect(element.shadowRoot?.textContent).to.contain('Token rotated');
    expect(element.shadowRoot?.textContent).not.to.contain(
      'prl_runner_example'
    );
  });

  it('removes a runner deleted elsewhere from a websocket event', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();
    expect(element.shadowRoot?.textContent).to.contain('office-mac');

    onRunnerMessage?.({
      type: 'runner_deleted',
      payload: { id: '11111111-1111-4111-8111-111111111111' },
    });
    await element.updateComplete;
    expect(element.shadowRoot?.textContent).not.to.contain('office-mac');
  });

  it('wraps the runners table in a horizontal scroll box', async () => {
    fetchStub = createFetchStub([actionRunner]);
    const element = await loadedView();
    const table = element.shadowRoot?.querySelector('table');
    expect(table?.parentElement?.classList.contains('table-scroll')).to.equal(
      true
    );
    expect(getComputedStyle(table!.parentElement!).overflowX).to.equal('auto');
  });

  it('shows a load failure as a danger alert with Try again', async () => {
    let fail = true;
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input.toString();
        const json = (data: unknown, status = 200) =>
          new Response(JSON.stringify(data), {
            status,
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.includes('/api/v1/runners')) {
          return fail
            ? json({ detail: 'Service unavailable' }, 503)
            : json([actionRunner]);
        }
        return json({ id: 'acct-1', default_runner_pool: null });
      });
    const element = (await fixture(
      html`<runners-view></runners-view>`
    )) as RunnersView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading,
      'Runners view did not finish loading'
    );
    await element.updateComplete;

    const alert = element.shadowRoot?.querySelector('sl-alert.load-error');
    expect(alert?.getAttribute('variant')).to.equal('danger');
    expect(alert?.textContent).to.contain('Could not load runners');

    fail = false;
    const retry = alert!.querySelector('sl-button') as HTMLElement;
    expect(retry.textContent?.trim()).to.equal('Try again');
    retry.click();
    await waitUntil(
      () => element.shadowRoot?.textContent?.includes('office-mac'),
      'Runners did not load after Try again'
    );
  });
});
