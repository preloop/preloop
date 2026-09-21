import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import './ai-models-view';
import {
  filterModels,
  isGatewayEnabled,
  type AIModelsView,
} from './ai-models-view';
import type { AIModel } from '../../../types';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { bulkActionButton, bulkCountText } from '../../../utils/test-bulk-bar';

describe('AIModelsView', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.removeItem('preloop.models.view_mode');
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();

      if (url === '/api/v1/ai-models') {
        return new Response(
          JSON.stringify([
            {
              id: 'model-1',
              name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              meta_data: {
                gateway: {
                  enabled: true,
                  model_alias: 'preloop/anthropic/claude-sonnet-4',
                },
                managed_agent_display_name: 'Mini Claw',
              },
              is_default: true,
              created_at: '2026-03-01T10:00:00Z',
              updated_at: '2026-03-09T18:30:00Z',
            },
          ]),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      if (url.startsWith('/api/v1/ai-models/overview')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-09T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            models: [
              {
                ai_model_id: 'model-1',
                model_name: 'Claude Sonnet Primary',
                provider_name: 'Anthropic',
                model_identifier: 'claude-sonnet-4',
                model_alias: 'preloop/anthropic/claude-sonnet-4',
                is_default: true,
                total_requests: 42,
                successful_requests: 40,
                failed_requests: 2,
                token_usage: {
                  prompt_tokens: 1200,
                  completion_tokens: 800,
                  total_tokens: 2000,
                },
                estimated_cost: 12.34,
                unpriced_request_count: 0,
                active_session_count: 3,
                last_request_at: '2026-03-09T18:30:00Z',
                pricing_source: 'override',
              },
            ],
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }

      return new Response(
        JSON.stringify({ detail: `Unhandled request: ${url}` }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      );
    });

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.removeItem('preloop.models.view_mode');
    localStorage.clear();
  });

  it('links each configured model to its observability detail page', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    const content = (element.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(content).to.contain('Claude Sonnet Primary');
    // Honest stat labels: the window is in the label, not in a subtext that
    // claims a configuration count is a 30-day metric.
    expect(content).to.contain('$ est. · 30d');
    expect(content).to.contain('Requests · 30d');
    expect(content).to.contain('Need attention');
    expect(content).to.contain('$12.34');
    expect(content).to.contain('42 requests');
    expect(content).to.contain('3 active sessions');
    expect(content).to.contain('Attention');
    expect(content).to.contain('preloop/anthropic/claude-sonnet-4');
    expect(content).to.contain('Mini Claw');
    expect(content).to.contain('Priced by account override');
    // One batch request for the page, plus the models list itself: the page
    // must never go back to one call per model.
    const modelScopedCalls = fetchStub
      .getCalls()
      .map((call: sinon.SinonSpyCall) => String(call.args[0]))
      .filter((url: string) => url.startsWith('/api/v1/ai-models/model-1'));
    expect(modelScopedCalls).to.eql([]);

    const nameLink = element.shadowRoot?.querySelector(
      'a.model-link[href="/console/ai-models/model-1"]'
    );
    expect(nameLink).to.not.equal(null);
    expect(connectStub).to.have.been.calledOnce;
    expect(subscribeStub.callCount).to.equal(5);

    const providerSelect = element.shadowRoot?.querySelector(
      'sl-select.provider-filter'
    );
    const statusSelect = element.shadowRoot?.querySelector(
      'sl-select.status-filter'
    );
    expect(providerSelect?.getAttribute('label')).to.equal('Provider');
    expect(statusSelect?.getAttribute('label')).to.equal('Status');
  });

  it('states the token split before the cost in the usage cell', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    const figures = element.shadowRoot!.querySelector(
      'token-figures'
    ) as HTMLElement & { updateComplete: Promise<unknown> };
    expect(figures).to.exist;
    await figures.updateComplete;
    const text = (figures.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('1.2K in');
    expect(text).to.contain('800 out');

    // The cell reads tokens first, then what they cost.
    const cell = figures.closest('.cell-secondary')!;
    const cellText = (cell.textContent || '').replace(/\s+/g, ' ').trim();
    expect(cellText).to.contain('$12.34 est.');
    expect(cellText.indexOf('·')).to.be.lessThan(cellText.indexOf('$12.34'));
  });

  it('hides the toolbar when a refresh fails with models still loaded', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;

    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;
    expect(element.shadowRoot?.querySelector('list-toolbar')).to.exist;

    (element as any).error = 'Failed to refresh models';
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('list-toolbar')).to.equal(null);
    expect(element.shadowRoot?.querySelector('sl-alert')).to.exist;
  });

  const secondModel = {
    id: 'model-2',
    name: 'GPT-4o Mini',
    provider_name: 'OpenAI',
    model_identifier: 'gpt-4o-mini',
    meta_data: {
      gateway: { enabled: false, model_alias: 'preloop/openai/gpt-4o-mini' },
    },
    is_default: false,
    created_at: '2026-03-01T10:00:00Z',
    updated_at: '2026-03-09T18:30:00Z',
  };

  it('filters models by name and provider', () => {
    const models = [
      {
        id: 'model-1',
        name: 'Claude Sonnet Primary',
        provider_name: 'Anthropic',
        model_identifier: 'claude-sonnet-4',
        created_at: '2026-03-01T10:00:00Z',
        updated_at: '2026-03-09T18:30:00Z',
      },
      secondModel,
    ] as AIModel[];
    expect(
      filterModels(models, 'claude', '', '').map((m) => m.id)
    ).to.deep.equal(['model-1']);
    expect(
      filterModels(models, '', 'OpenAI', '').map((m) => m.id)
    ).to.deep.equal(['model-2']);
    expect(isGatewayEnabled(models[0])).to.equal(false);
    expect(isGatewayEnabled(secondModel as AIModel)).to.equal(false);
    expect(
      filterModels(models, '', '', 'disabled').map((m) => m.id)
    ).to.deep.equal(['model-1', 'model-2']);
    expect(filterModels(models, '', '', 'enabled')).to.deep.equal([]);
  });

  it('narrows list rows when search matches one model', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/ai-models') {
        return new Response(
          JSON.stringify([
            {
              id: 'model-1',
              name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              meta_data: { gateway: { enabled: true } },
              is_default: true,
              created_at: '2026-03-01T10:00:00Z',
              updated_at: '2026-03-09T18:30:00Z',
            },
            secondModel,
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.startsWith('/api/v1/ai-models/overview')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-09T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            models: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response(JSON.stringify({ detail: url }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelectorAll('.model-row')).to.have.lengthOf(
      2
    );

    const toolbar = element.shadowRoot?.querySelector('list-toolbar');
    expect(toolbar).to.exist;
    toolbar!.dispatchEvent(
      new CustomEvent('search-change', {
        detail: { value: 'openai' },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    const rows = element.shadowRoot?.querySelectorAll('.model-row');
    expect(rows).to.have.lengthOf(1);
    expect(rows?.[0].textContent).to.include('GPT-4o Mini');
  });

  it('deletes the selected models from the bulk bar, naming them first', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/ai-models') {
        return new Response(
          JSON.stringify([
            {
              id: 'model-1',
              name: 'Claude Sonnet Primary',
              provider_name: 'Anthropic',
              model_identifier: 'claude-sonnet-4',
              meta_data: { gateway: { enabled: true } },
              is_default: true,
              created_at: '2026-03-01T10:00:00Z',
              updated_at: '2026-03-09T18:30:00Z',
            },
            secondModel,
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.startsWith('/api/v1/ai-models/overview')) {
        return new Response(
          JSON.stringify({
            period_start: '2026-02-09T00:00:00Z',
            period_end: '2026-03-09T23:59:59Z',
            models: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    const rowCheckbox = element.shadowRoot!.querySelector<HTMLElement>(
      'tr[data-selection-id="model-1"] list-select-checkbox'
    )!;
    expect(rowCheckbox.getAttribute('label')).to.equal(
      'Select Claude Sonnet Primary'
    );
    expect(
      element
        .shadowRoot!.querySelector('table.styled-table')!
        .getAttribute('aria-multiselectable')
    ).to.equal('true');

    element.selection.toggle('model-1');
    element.selection.toggle('model-2');
    await element.updateComplete;

    const bar = element.shadowRoot!.querySelector('list-bulk-bar')!;
    expect(bulkCountText(bar)).to.contain('2 selected');

    const deleteButton = (await bulkActionButton(bar, 'delete'))!;
    deleteButton.click();

    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'no confirm dialog'
    );
    const dialog = document.querySelector('confirm-dialog')!;
    await (dialog as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const dialogText = dialog.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(dialogText).to.contain('Delete 2 models?');
    expect(dialogText).to.contain('Claude Sonnet Primary, GPT-4o Mini');
    // The default model is called out rather than deleted quietly.
    expect(dialogText).to.contain(
      'Claude Sonnet Primary is the account default'
    );

    const deletes = () =>
      fetchStub
        .getCalls()
        .filter((call) => (call.args[1] as RequestInit)?.method === 'DELETE');
    expect(deletes().length, 'deleted before confirming').to.equal(0);

    const confirmButton = dialog.shadowRoot!.querySelector<HTMLElement>(
      '[data-testid="confirm-dialog-confirm"]'
    )!;
    await (confirmButton as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    confirmButton.click();

    await waitUntil(
      () => deletes().length === 2,
      'not every model was deleted'
    );
    expect(
      deletes()
        .map((call) => String(call.args[0]))
        .sort()
    ).to.deep.equal(['/api/v1/ai-models/model-1', '/api/v1/ai-models/model-2']);
    resetConfirmDialogForTests();
  });

  it('switches from list rows to cards', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.model-row')).to.exist;
    expect(element.shadowRoot?.querySelector('.models-grid')).to.equal(null);

    const toolbar = element.shadowRoot?.querySelector('list-toolbar');
    toolbar!.dispatchEvent(
      new CustomEvent('view-change', {
        detail: { value: 'cards' },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.models-grid')).to.exist;
    expect(element.shadowRoot?.querySelector('.model-card')).to.exist;
    expect(element.shadowRoot?.querySelector('.model-row')).to.equal(null);
    expect(element.shadowRoot?.textContent).to.contain('Default');
    // Cards carry the same one kebab as the rows, not a row of buttons.
    expect(
      element.shadowRoot?.querySelectorAll('.model-card resource-actions')
    ).to.have.lengthOf(1);
  });

  it('gives each row one kebab holding View, Edit, Set default and Delete', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    const rows = element.shadowRoot?.querySelectorAll('.model-row') ?? [];
    expect(rows).to.have.lengthOf(1);
    const kebabs = element.shadowRoot?.querySelectorAll(
      '.model-row resource-actions[menu-only]'
    );
    expect(kebabs).to.have.lengthOf(1);
    // No solid danger button and no "Set as default" button in the row: the
    // rare action and the destructive one both live in the kebab.
    expect(
      element.shadowRoot?.querySelector(
        '.model-row sl-button[variant="danger"]'
      )
    ).to.equal(null);
    expect(element.shadowRoot?.textContent).to.not.contain('Set as default');

    // The default model is a chip, not a button; the rest of the column is a
    // dash rather than fourteen invitations to change the default.
    const actions = (
      element as unknown as {
        modelActions: (
          model: AIModel
        ) => { id: string; variant?: string; outline?: boolean }[];
      }
    ).modelActions({ id: 'model-1', is_default: false } as AIModel);
    expect(actions.map((action) => action.id)).to.deep.equal([
      'view',
      'edit',
      'set-default',
      'delete',
    ]);
    expect(actions[actions.length - 1].variant).to.equal('danger');

    // What the row actually renders is the menu: in menu-only mode
    // resource-actions sends every action into the dropdown and ignores the
    // outline and separated flags, so the assertion that can fail is the
    // order, with Delete last and its icon in danger red.
    const kebab = kebabs?.[0] as HTMLElement & {
      updateComplete?: Promise<unknown>;
    };
    await kebab.updateComplete;
    const items = kebab.shadowRoot?.querySelectorAll('sl-menu-item') ?? [];
    // The fixture model is already the default, so its menu holds three
    // actions; Delete is last in either case.
    expect([...items].map((item) => item.textContent?.trim())).to.deep.equal([
      'View',
      'Edit',
      'Delete',
    ]);
    const last = items[items.length - 1];
    expect(last.className).to.contain('danger-item');
    expect(last.querySelector('sl-icon')?.getAttribute('style')).to.contain(
      '--sl-color-danger-600'
    );
  });

  it('does not add a prior-window request to every realtime refresh', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;
    await waitUntil(
      () => (element as any).priorFleetSpend !== null,
      'prior window spend never loaded'
    );

    const overviewCalls = () =>
      fetchStub
        .getCalls()
        .filter((call) =>
          String(call.args[0]).startsWith('/api/v1/ai-models/overview')
        ).length;
    // Mount asks for this window and the one before it, once each.
    expect(overviewCalls()).to.equal(2);

    // A websocket refresh is one request, not two: the prior 30 day window
    // moves once a day, and a burst of extra calls is what emptied the API
    // connection pool on 2026-09-03.
    await element.fetchModels({ preserveLoadingState: true });
    expect(overviewCalls()).to.equal(3);

    // Even a full reload reuses the loaded prior window while it is current.
    await element.fetchModels();
    await waitUntil(() => overviewCalls() >= 4, 'reload did not fetch');
    expect(overviewCalls()).to.equal(4);
    expect((element as any).priorFleetSpend).to.be.a('number');
  });

  it('renders counts compact and sub-cent spend to four decimals', async () => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;

    const format = element as unknown as {
      formatCompactNumber: (value: number) => string;
      formatCurrency: (value: number) => string;
    };
    expect(format.formatCompactNumber(999)).to.equal('999');
    expect(format.formatCompactNumber(18306)).to.equal('18.3K');
    expect(format.formatCompactNumber(572180203)).to.equal('572.2M');
    // A sub-cent estimate is four decimals, never a $0.00 that reads as free.
    expect(format.formatCurrency(0.0042)).to.equal('$0.0042');
    expect(format.formatCurrency(12.3)).to.equal('$12.30');
    expect(format.formatCurrency(0)).to.equal('$0.00');
  });
});

/**
 * The Models page used to count and flag every model with a failure in the
 * window, ignoring the dismissals the Overview and the attention inbox honour.
 * A model marked fixed kept its red badge for as long as the window remembered
 * the failure, which is what taught people to ignore the badge.
 */
describe('AIModelsView attention dismissals', () => {
  let fetchStub: sinon.SinonStub;
  let connectStub: sinon.SinonStub;
  let subscribeStub: sinon.SinonStub;
  let dismissalsResponse: any[];
  let dismissalsSupported: boolean;
  let dismissalWrites: { url: string; method: string; body: any }[];
  let overviewRequests: string[];
  let lastFailureAt: string;
  let failedRequestsSince: number;
  /** #848: what the window says this model served with no price at all. */
  let unpricedRequestCount: number;
  /** Off for the rows that are only unpriced, not failing. */
  let failuresEnabled: boolean;
  let extraAliasFailures: {
    alias: string;
    last_failure_at: string;
    failed_requests: number;
    failed_requests_since: number | null;
  }[];

  const json = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  const overviewRow = (failedSinceAsked: boolean) => ({
    ai_model_id: 'model-1',
    model_name: 'Reviewer model',
    provider_name: 'example-provider',
    model_identifier: 'example-model-1',
    model_alias: 'example/reviewer',
    is_default: false,
    total_requests: 40,
    successful_requests: failuresEnabled ? 31 : 40,
    failed_requests: failuresEnabled ? 9 : 0,
    token_usage: {
      prompt_tokens: 100,
      completion_tokens: 100,
      total_tokens: 200,
    },
    estimated_cost: 1.5,
    unpriced_request_count: unpricedRequestCount,
    active_session_count: 0,
    last_request_at: '2026-09-14T10:00:00Z',
    last_failure_at: failuresEnabled ? lastFailureAt : null,
    last_failure_alias: failuresEnabled ? 'example/reviewer' : null,
    failed_requests_since: failedSinceAsked ? failedRequestsSince : null,
    alias_failures: failuresEnabled
      ? [
          {
            alias: 'example/reviewer',
            last_failure_at: lastFailureAt,
            failed_requests: 9,
            failed_requests_since: failedSinceAsked
              ? failedRequestsSince
              : null,
          },
          ...extraAliasFailures,
        ]
      : [],
    pricing_source: failuresEnabled ? 'catalog' : 'none',
  });

  beforeEach(() => {
    localStorage.removeItem('preloop.models.view_mode');
    localStorage.setItem('accessToken', 'test-access-token');
    dismissalsResponse = [];
    dismissalsSupported = true;
    dismissalWrites = [];
    overviewRequests = [];
    lastFailureAt = '2026-09-14T09:00:00Z';
    failedRequestsSince = 2;
    unpricedRequestCount = 0;
    failuresEnabled = true;
    extraAliasFailures = [];

    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (url.startsWith('/api/v1/attention/dismissals')) {
          if (!dismissalsSupported) {
            return new Response('{"detail":"Not Found"}', { status: 404 });
          }
          const method = (init?.method || 'GET').toUpperCase();
          if (method === 'GET') {
            return json({ items: dismissalsResponse });
          }
          if (method === 'DELETE') {
            const itemId = decodeURIComponent(url.split('/').pop()!);
            dismissalWrites.push({ url, method, body: null });
            dismissalsResponse = dismissalsResponse.filter(
              (record) => record.item_id !== itemId
            );
            return new Response(null, { status: 204 });
          }
          const body = JSON.parse(String(init!.body));
          dismissalWrites.push({ url, method, body });
          const record = {
            id: 'dismissal-1',
            item_id: decodeURIComponent(url.split('/').pop()!),
            fingerprint: body.fingerprint,
            reason: body.reason,
            snooze_until: null,
            dismissed_by_user_id: 'user-1',
            dismissed_by_username: 'Jane Doe',
            created_at: '2026-09-14T09:30:00Z',
          };
          // Upsert, as the API does: a failure marker and an unpriced marker
          // for the same model are two rows, not one.
          dismissalsResponse = [
            ...dismissalsResponse.filter(
              (existing) => existing.item_id !== record.item_id
            ),
            record,
          ];
          return json(record);
        }

        if (url === '/api/v1/ai-models') {
          return json([
            {
              id: 'model-1',
              name: 'Reviewer model',
              provider_name: 'example-provider',
              model_identifier: 'example-model-1',
              meta_data: {
                gateway: { enabled: true, model_alias: 'example/reviewer' },
              },
              is_default: false,
              created_at: '2026-09-01T10:00:00Z',
              updated_at: '2026-09-14T10:00:00Z',
            },
          ]);
        }

        if (url.startsWith('/api/v1/ai-models/overview')) {
          overviewRequests.push(url);
          return json({
            period_start: '2026-08-15T00:00:00Z',
            period_end: '2026-09-14T23:59:59Z',
            models: [overviewRow(url.includes('failed_since'))],
          });
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${url}` }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }
        );
      });

    connectStub = sinon.stub(unifiedWebSocketManager, 'connect').resolves();
    subscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
  });

  afterEach(() => {
    fetchStub.restore();
    connectStub.restore();
    subscribeStub.restore();
    localStorage.clear();
  });

  const mount = async (): Promise<AIModelsView> => {
    const element = (await fixture(
      html`<ai-models-view></ai-models-view>`
    )) as AIModelsView;
    await waitUntil(
      () => !(element as any).isLoading,
      'AI models view did not finish loading'
    );
    await element.updateComplete;
    return element;
  };

  const healthBadge = (element: AIModelsView) =>
    element.shadowRoot!.querySelector(
      'tr[data-model-id="model-1"] sl-badge.status-chip'
    ) as HTMLElement;

  it('flags a failing model nobody has acknowledged', async () => {
    const element = await mount();

    expect((element as any).modelsNeedingAttentionCount).to.equal(1);
    expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
    // Nothing is acknowledged, so there is no "since" count to ask for.
    expect(overviewRequests.some((url) => url.includes('failed_since'))).to.be
      .false;
  });

  it('does not flag a model whose failures were marked fixed', async () => {
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'model:example/reviewer',
        fingerprint: `last:${lastFailureAt}`,
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-14T09:30:00Z',
      },
    ];

    const element = await mount();

    expect((element as any).modelsNeedingAttentionCount).to.equal(0);
    const badge = healthBadge(element);
    expect(badge.textContent!.trim()).to.equal('Healthy');
    // The claim stays checkable: the badge says when it was made.
    expect(badge.getAttribute('title')).to.contain('Marked fixed');
    // An acknowledged model offers no second dismissal.
    expect(
      element.shadowRoot!.querySelector('[data-testid="dismiss-model-1"]')
    ).to.equal(null);
  });

  it('still counts an acknowledged model that has unpriced requests', async () => {
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'model:example/reviewer',
        fingerprint: `last:${lastFailureAt}`,
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-14T09:30:00Z',
      },
    ];
    const element = await mount();
    const overview = new Map((element as any).modelOverview);
    overview.set('model-1', {
      ...(overview.get('model-1') as any),
      unpriced_request_count: 4,
    });
    (element as any).modelOverview = overview;
    await element.updateComplete;

    // Dismissing a failure says nothing about a missing price.
    expect((element as any).modelsNeedingAttentionCount).to.equal(1);
  });

  it('flags the model again after a newer failure and counts only the new ones', async () => {
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'model:example/reviewer',
        // Acknowledged an older failure than the one the window now reports.
        fingerprint: 'last:2026-09-13T08:00:00Z',
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-13T08:30:00Z',
      },
    ];

    const element = await mount();
    await waitUntil(
      () =>
        Boolean(
          element.shadowRoot!.querySelector(
            '[data-testid="since-marker-model-1"]'
          )
        ),
      'the failures-since line never rendered'
    );

    expect((element as any).modelsNeedingAttentionCount).to.equal(1);
    expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
    const since = element.shadowRoot!.querySelector(
      '[data-testid="since-marker-model-1"]'
    )!;
    // The news is what arrived after the fix, not the window's whole tally.
    expect(since.textContent!.replace(/\s+/g, ' ')).to.contain(
      '2 failed since fix'
    );
    const splitRequest = overviewRequests.find((url) =>
      url.includes('failed_since')
    )!;
    expect(splitRequest).to.exist;
    expect(decodeURIComponent(splitRequest)).to.contain(
      'failed_since=model-1:2026-09-13T08:00:00Z'
    );
  });

  it('keeps a two-alias row flagged after only the newest alias is dismissed', async () => {
    extraAliasFailures = [
      {
        alias: 'example/reviewer-old',
        last_failure_at: '2026-09-13T08:00:00Z',
        failed_requests: 4,
        failed_requests_since: null,
      },
    ];
    dismissalsResponse = [
      {
        id: 'dismissal-1',
        item_id: 'model:example/reviewer',
        fingerprint: `last:${lastFailureAt}`,
        reason: 'fixed',
        snooze_until: null,
        dismissed_by_user_id: 'user-1',
        dismissed_by_username: 'Jane Doe',
        created_at: '2026-09-14T09:30:00Z',
      },
    ];

    const element = await mount();

    expect((element as any).modelsNeedingAttentionCount).to.equal(1);
    expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
  });

  it('dismisses a row with the item id and fingerprint the inbox uses', async () => {
    const element = await mount();

    const menu = element.shadowRoot!.querySelector(
      'tr[data-model-id="model-1"] .dismiss-dropdown sl-menu'
    )!;
    menu.dispatchEvent(
      new CustomEvent('sl-select', { detail: { item: { value: 'snoozed' } } })
    );
    await waitUntil(
      () => dismissalWrites.length > 0,
      'the dismissal was never written'
    );

    expect(dismissalWrites[0].method).to.equal('PUT');
    // The id and fingerprint the inbox derives for the same failures, so a
    // dismissal made here is honoured there.
    expect(decodeURIComponent(dismissalWrites[0].url)).to.contain(
      'model:example/reviewer'
    );
    expect(dismissalWrites[0].body).to.deep.equal({
      fingerprint: `last:${lastFailureAt}`,
      reason: 'snoozed',
      snooze_days: 7,
    });

    await waitUntil(
      () => healthBadge(element).textContent!.trim() === 'Healthy',
      'the row stayed flagged after being snoozed'
    );
  });

  it('offers no dismiss control against a server without the endpoint', async () => {
    dismissalsSupported = false;

    const element = await mount();

    expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
    expect(
      element.shadowRoot!.querySelector('[data-testid="dismiss-model-1"]')
    ).to.equal(null);
  });

  /**
   * #848. A model can be unpriced on purpose: a local model, a flat-rate
   * subscription, a bill settled outside Preloop. Until now the only way to
   * quiet one was to price it at $0, which records a false cost.
   */
  describe('unpriced requests marked expected', () => {
    const unpricedMarker = (overrides: Record<string, unknown> = {}) => ({
      id: 'dismissal-unpriced',
      item_id: 'model-unpriced:example/reviewer',
      fingerprint: 'unpriced:example/reviewer',
      reason: 'expected',
      snooze_until: null,
      dismissed_by_user_id: 'user-1',
      dismissed_by_username: 'Jane Doe',
      created_at: '2026-09-14T09:30:00Z',
      ...overrides,
    });

    const failureMarker = () => ({
      id: 'dismissal-1',
      item_id: 'model:example/reviewer',
      fingerprint: `last:${lastFailureAt}`,
      reason: 'fixed',
      snooze_until: null,
      dismissed_by_user_id: 'user-1',
      dismissed_by_username: 'Jane Doe',
      created_at: '2026-09-14T09:30:00Z',
    });

    const selectMenuItem = async (element: AIModelsView, value: string) => {
      const menu = element.shadowRoot!.querySelector(
        'tr[data-model-id="model-1"] .dismiss-dropdown sl-menu'
      )!;
      menu.dispatchEvent(
        new CustomEvent('sl-select', { detail: { item: { value } } })
      );
      await waitUntil(
        () => dismissalWrites.length > 0,
        'the dismissal was never written'
      );
      await element.updateComplete;
    };

    beforeEach(() => {
      failuresEnabled = false;
      unpricedRequestCount = 12;
    });

    it('counts and badges a model nobody has marked', async () => {
      const element = await mount();

      expect((element as any).modelsNeedingAttentionCount).to.equal(1);
      expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
      // The row says what ends it, not only that something is wrong.
      const line = element.shadowRoot!.querySelector(
        '[data-testid="unpriced-model-1"]'
      )!;
      expect(line.textContent!.replace(/\s+/g, ' ')).to.contain(
        '12 requests unpriced'
      );
      expect(line.getAttribute('title')).to.contain('Apply to past usage');
    });

    it('writes the item id and fingerprint the inbox reads', async () => {
      const element = await mount();

      await selectMenuItem(element, 'unpriced-expected');

      expect(dismissalWrites[0].method).to.equal('PUT');
      expect(decodeURIComponent(dismissalWrites[0].url)).to.contain(
        'model-unpriced:example/reviewer'
      );
      expect(dismissalWrites[0].body).to.deep.equal({
        fingerprint: 'unpriced:example/reviewer',
        reason: 'expected',
      });
    });

    it('snoozes the price question for seven days', async () => {
      const element = await mount();

      await selectMenuItem(element, 'unpriced-snoozed');

      expect(dismissalWrites[0].body).to.deep.equal({
        fingerprint: 'unpriced:example/reviewer',
        reason: 'snoozed',
        snooze_days: 7,
      });
    });

    it('drops the model out of the count and badges it Healthy', async () => {
      dismissalsResponse = [unpricedMarker()];

      const element = await mount();

      expect((element as any).modelsNeedingAttentionCount).to.equal(0);
      const badge = healthBadge(element);
      expect(badge.textContent!.trim()).to.equal('Healthy');
      // The claim stays checkable: the badge says when it was made.
      expect(badge.getAttribute('title')).to.contain('marked expected');
      // Nothing left to acknowledge, so no menu.
      expect(
        element.shadowRoot!.querySelector('[data-testid="dismiss-model-1"]')
      ).to.equal(null);
    });

    // The opposite of a failure marker: the fingerprint has no timestamp in
    // it, so more unpriced traffic is not news.
    it('stays quiet when newer unpriced requests arrive', async () => {
      dismissalsResponse = [unpricedMarker()];
      unpricedRequestCount = 9000;

      const element = await mount();

      expect((element as any).modelsNeedingAttentionCount).to.equal(0);
      expect(healthBadge(element).textContent!.trim()).to.equal('Healthy');
    });

    it('flags the model again once the snooze has run out', async () => {
      dismissalsResponse = [
        unpricedMarker({
          reason: 'snoozed',
          snooze_until: '2020-01-01T00:00:00Z',
        }),
      ];

      const element = await mount();

      expect((element as any).modelsNeedingAttentionCount).to.equal(1);
      expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
    });

    it('restores a marked model from its row', async () => {
      dismissalsResponse = [unpricedMarker()];

      const element = await mount();
      const restore = element.shadowRoot!.querySelector(
        '[data-testid="restore-unpriced-model-1"]'
      ) as HTMLElement;
      expect(restore).to.exist;
      restore.click();
      await waitUntil(
        () => dismissalWrites.length > 0,
        'the restore was never written'
      );

      expect(dismissalWrites[0].method).to.equal('DELETE');
      expect(decodeURIComponent(dismissalWrites[0].url)).to.contain(
        'model-unpriced:example/reviewer'
      );
      await waitUntil(
        () => healthBadge(element).textContent!.trim() === 'Attention',
        'the row stayed healthy after being restored'
      );
      expect((element as any).modelsNeedingAttentionCount).to.equal(1);
    });

    // Two independent facts, two independent markers.
    it('keeps a failing and unpriced row flagged until both are marked', async () => {
      failuresEnabled = true;
      dismissalsResponse = [failureMarker()];

      const element = await mount();

      expect((element as any).modelsNeedingAttentionCount).to.equal(1);
      expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
      // The menu offers only the claim that is still open.
      const values = [
        ...element.shadowRoot!.querySelectorAll(
          'tr[data-model-id="model-1"] .dismiss-dropdown sl-menu sl-menu-item'
        ),
      ].map((item) => item.getAttribute('value'));
      expect(values).to.eql(['unpriced-expected', 'unpriced-snoozed']);

      await selectMenuItem(element, 'unpriced-expected');
      await waitUntil(
        () => healthBadge(element).textContent!.trim() === 'Healthy',
        'the row stayed flagged after both markers were taken'
      );
      expect((element as any).modelsNeedingAttentionCount).to.equal(0);
      expect(healthBadge(element).getAttribute('title')).to.contain(
        'Marked fixed'
      );
      expect(healthBadge(element).getAttribute('title')).to.contain(
        'marked expected'
      );
    });

    it('offers both sets of answers on a row that is failing and unpriced', async () => {
      failuresEnabled = true;

      const element = await mount();

      const values = [
        ...element.shadowRoot!.querySelectorAll(
          'tr[data-model-id="model-1"] .dismiss-dropdown sl-menu sl-menu-item'
        ),
      ].map((item) => item.getAttribute('value'));
      expect(values).to.eql([
        'expected',
        'snoozed',
        'fixed',
        'unpriced-expected',
        'unpriced-snoozed',
      ]);
    });

    it('offers no unpriced control against a server without the endpoint', async () => {
      dismissalsSupported = false;

      const element = await mount();

      expect(healthBadge(element).textContent!.trim()).to.equal('Attention');
      expect(
        element.shadowRoot!.querySelector('[data-testid="dismiss-model-1"]')
      ).to.equal(null);
    });
  });
});
