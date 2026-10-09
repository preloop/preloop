import { fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import type { LitElement } from 'lit';
import { invalidateApiCaches } from '../../api';
import { signInForTest } from '../../test-helpers/capability-api';
import '../../components/budget-policy-editor';
import '../../components/team-budgets-panel';
import './settings/ai-models-view';
import './settings/ai-model-detail-view';
import './settings/user-management-view';
import './settings/team-management-view';
import './settings/invitation-management-view';
import './cost-view';

const model = {
  id: 'model-example',
  name: 'Example model',
  provider_name: 'openai',
  model_identifier: 'example',
  model_kind: 'llm',
  is_default: false,
};
const surfaces = [
  ['budget-policy-editor', 'Add limit'],
  ['team-budgets-panel', 'Add team budget'],
  ['ai-models-view', 'Add model'],
  ['ai-model-detail-view', 'model actions'],
  ['user-management-view', 'Add user'],
  ['team-management-view', 'Create team'],
  ['invitation-management-view', 'Send invitation'],
  ['cost-view', 'Configure limits'],
] as const;

describe('Edit controls permission states', () => {
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });
  for (const [tag, label] of surfaces) {
    for (const state of [
      'rbac off',
      'readonly',
      'unknown',
      ...(tag === 'budget-policy-editor' || tag === 'team-budgets-panel'
        ? ['explicit readonly']
        : []),
    ] as const) {
      it(`${tag}: ${state} gates ${label}`, async () => {
        signInForTest();
        invalidateApiCaches();
        let profileSettled = false;
        sinon.stub(window, 'fetch').callsFake(async (input) => {
          const url = String(input);
          const profile = url.includes('/auth/users/me');
          if (profile) profileSettled = true;
          const body = profile
            ? {
                permissions:
                  state === 'rbac off' || state === 'explicit readonly'
                    ? null
                    : [],
              }
            : url.includes('/features')
              ? {
                  features: {
                    user_management: true,
                    billing: true,
                    model_price_overrides: true,
                  },
                }
              : [];
          return new Response(JSON.stringify(body), {
            status: profile && state === 'unknown' ? 503 : 200,
            headers: { 'Content-Type': 'application/json' },
          });
        });
        const prototype = customElements.get(tag)!.prototype;
        for (const method of [
          'load',
          'loadData',
          'loadPolicies',
          'loadSubjects',
          'fetchModels',
          'fetchUsers',
          'fetchTeams',
          'fetchInvitations',
          'fetchRoles',
          'refresh',
        ]) {
          if (typeof prototype[method] === 'function')
            sinon.stub(prototype, method).resolves();
        }
        const element = await fixture<LitElement>(document.createElement(tag));
        const view = element as any;
        await waitUntil(() => profileSettled);
        await new Promise((resolve) => setTimeout(resolve, 30));
        Object.assign(view, {
          readOnly: state === 'explicit readonly',
          loading: false,
          isLoading: false,
          featureEnabled: true,
          loadingFeatures: false,
          loadingPolicies: false,
          error: null,
          models: [model],
          model,
          policies: [
            {
              id: 'policy-example',
              subject_type: 'global',
              period: 'monthly',
              hard_limit_usd: 10,
              current_spend_usd: 0,
            },
          ],
          roles: [],
          teamMembers: [],
          availableRoles: [],
          usage: [
            { team_id: 'team-example', team_name: 'Example team', cost_usd: 0 },
          ],
          budgets: [
            {
              id: 'budget-example',
              team_id: 'team-example',
              team_name: 'Example team',
              period: 'monthly',
              hard_limit_usd: 10,
            },
          ],
          teams: [{ id: 'team-example', name: 'Example team', roles: [] }],
          users: [
            {
              id: 'user-example',
              username: 'example',
              email: 'user@example.com',
              is_active: true,
              roles: [],
            },
          ],
          invitations: [],
          summary: {
            total_cost: 0,
            token_usage: {
              total_tokens: 0,
              prompt_tokens: 0,
              completion_tokens: 0,
            },
            total_requests: 0,
            requests_by_day: [],
          },
          priceOverridesEnabled: true,
        });
        view.requestUpdate();
        await element.updateComplete;
        if (tag === 'ai-model-detail-view') {
          const actions = element.shadowRoot!.querySelector(
            'resource-actions'
          ) as any;
          expect(actions?.actions?.length ?? 0).to.equal(
            state === 'rbac off' ? 2 : 0
          );
          expect(view.canEditPrice).to.equal(state === 'rbac off');
        } else if (tag === 'cost-view') {
          const container = document.createElement('div');
          const { render } = await import('lit');
          render(view.renderBudgets(), container);
          const card = container.querySelector('budget-health-card') as any;
          expect(card?.configurable).to.equal(state === 'rbac off');
        } else {
          const button = [
            ...element.shadowRoot!.querySelectorAll('sl-button'),
          ].find((item) => item.textContent?.includes(label)) as any;
          expect(!!button && !button.disabled).to.equal(state === 'rbac off');
        }
        const mutationLabels =
          tag === 'user-management-view'
            ? ['Manage roles', 'Edit user', 'Deactivate user']
            : tag === 'team-management-view'
              ? ['Manage roles', 'Edit team', 'Delete team']
              : [];
        for (const title of mutationLabels) {
          const button = element.shadowRoot!.querySelector(
            `sl-button[title="${title}"]`
          ) as any;
          expect(button, title).to.exist;
          expect(button.disabled, title).to.equal(state !== 'rbac off');
        }
        if (tag === 'budget-policy-editor') {
          expect(!!element.shadowRoot!.querySelector('sl-dropdown')).to.equal(
            state === 'rbac off'
          );
        }
        if (tag === 'team-budgets-panel') {
          const remove = [
            ...element.shadowRoot!.querySelectorAll('sl-button'),
          ].find((item) => item.textContent?.includes('Remove'));
          expect(!!remove).to.equal(state === 'rbac off');
        }
        if (tag === 'ai-models-view') {
          expect(
            view.modelActions(model).map((action: any) => action.id)
          ).to.deep.equal(
            state === 'rbac off'
              ? ['view', 'edit', 'set-default', 'delete']
              : ['view']
          );
        }
      });
    }
  }
});

describe('Model mutation permission split', () => {
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });
  for (const [permission, actionIds] of [
    ['create_ai_models', ['view']],
    ['edit_ai_models', ['view', 'edit', 'set-default']],
    ['delete_ai_models', ['view', 'delete']],
  ] as const) {
    it(`grants only ${permission}`, async () => {
      signInForTest();
      invalidateApiCaches();
      sinon
        .stub(window, 'fetch')
        .callsFake(
          async (input) =>
            new Response(
              JSON.stringify(
                String(input).includes('/auth/users/me')
                  ? { permissions: [permission] }
                  : { features: {} }
              ),
              { headers: { 'Content-Type': 'application/json' } }
            )
        );
      const prototype = customElements.get('ai-models-view')!.prototype;
      sinon.stub(prototype, 'fetchModels').resolves();
      const element = await fixture<LitElement>(
        document.createElement('ai-models-view')
      );
      const view = element as any;
      await waitUntil(() => view.editPermissions.loaded);
      Object.assign(view, {
        models: [model],
        isLoading: false,
        loading: false,
        error: null,
      });
      view.requestUpdate();
      await element.updateComplete;
      expect(
        view.modelActions(model).map((action: any) => action.id)
      ).to.deep.equal(actionIds);
      const add = [...element.shadowRoot!.querySelectorAll('sl-button')].find(
        (button) => button.textContent?.includes('Add model')
      ) as any;
      expect(add.disabled).to.equal(permission !== 'create_ai_models');
      expect(view.renderBulkBar() !== null).to.equal(
        permission === 'delete_ai_models'
      );
    });
  }
});

describe('Endpoint permission regression guards', () => {
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
  });
  for (const permissions of [
    [],
    ['manage_budgets'],
    ['edit_ai_models'],
    ['assign_roles'],
  ]) {
    it(`uses endpoint permissions for ${JSON.stringify(permissions)}`, async () => {
      signInForTest();
      invalidateApiCaches();
      const mutations: string[] = [];
      sinon.stub(window, 'fetch').callsFake(async (input, init) => {
        const url = String(input);
        if (init?.method && init.method !== 'GET') mutations.push(url);
        const body = url.includes('/auth/users/me')
          ? { permissions }
          : url.includes('/features')
            ? {
                features: {
                  model_price_overrides: true,
                  user_management: true,
                  billing: true,
                },
              }
            : [];
        return new Response(JSON.stringify(body), {
          headers: { 'Content-Type': 'application/json' },
        });
      });
      for (const [tag, methods] of [
        ['ai-model-detail-view', ['loadData']],
        ['cost-view', ['loadData', 'refresh']],
        ['user-management-view', ['fetchUsers', 'fetchRoles']],
      ] as const) {
        const proto = customElements.get(tag)!.prototype;
        for (const method of methods)
          if (typeof proto[method] === 'function')
            sinon.stub(proto, method).resolves();
      }
      const detail = await fixture<LitElement>(
        '<ai-model-detail-view></ai-model-detail-view>'
      );
      const cost = await fixture<LitElement>('<cost-view></cost-view>');
      const users = await fixture<LitElement>(
        '<user-management-view></user-management-view>'
      );
      await waitUntil(
        () =>
          (detail as any).editPermissions.loaded &&
          (cost as any).editPermissions.loaded &&
          (users as any).editPermissions.loaded
      );
      const d = detail as any,
        c = cost as any,
        u = users as any;
      d.priceOverridesEnabled = true;
      d.modelId = 'model-example';
      expect(d.canEditPrice).to.equal(permissions.includes('edit_ai_models'));
      c.summary = {
        price_catalog: {
          fetched_at: '2020-01-01T00:00:00Z',
          source: 'Example',
          model_count: 1,
        },
      };
      const catalog = document.createElement('div');
      const { render } = await import('lit');
      render(c.renderCatalogInfo(), catalog);
      expect(!!catalog.querySelector('.catalog-action')).to.equal(
        permissions.includes('edit_ai_models')
      );
      c.openPriceOverrideEditor(null);
      expect(c.priceDialogOpen).to.equal(
        permissions.includes('edit_ai_models')
      );
      if (!permissions.includes('edit_ai_models')) {
        await d.fetchProviderPrice();
        await d.savePrice();
        await c.savePriceOverride();
        await c.removeOverride();
        expect(mutations).to.deep.equal([]);
      }
      await u.openRoleModal({ id: 'user-example', roles: [] });
      expect(u.isRoleModalOpen).to.equal(permissions.includes('assign_roles'));
      if (!permissions.includes('assign_roles')) {
        await u.handleToggleRole('role-example', true);
        expect(mutations).to.deep.equal([]);
      }
    });
  }
});
