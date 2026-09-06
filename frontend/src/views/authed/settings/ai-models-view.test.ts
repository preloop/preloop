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
    expect(
      bar.shadowRoot!.querySelector('[data-testid="bulk-count"]')!.textContent
    ).to.contain('2 selected');

    const deleteButton = bar.shadowRoot!.querySelector<HTMLElement>(
      'sl-button[data-action="delete"]'
    )!;
    await (deleteButton as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
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
