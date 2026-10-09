import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './session-embedding-settings';
import type { SessionEmbeddingSettings } from './session-embedding-settings';
import { invalidateApiCaches } from '../api';

const SETTING_URL = '/api/v1/runtime-sessions/settings/embedding';

function setting(overrides: Record<string, unknown> = {}) {
  return {
    enabled: false,
    scope: 'summaries_only',
    scope_help: 'server help text',
    provider: 'openai_compatible',
    model_identifier: null,
    base_url: null,
    dimensions: 1536,
    daily_cap_usd: null,
    deployment_daily_cap_usd: 2,
    deployment_embedding_enabled: true,
    degraded_reason: null,
    degraded_at: null,
    corpus: {
      vectors: 0,
      model_vectors: 0,
      pending: 0,
      embedded_through: null,
    },
    ...overrides,
  };
}

interface Call {
  url: string;
  method: string;
  body: any;
}

describe('SessionEmbeddingSettings', () => {
  let fetchStub: sinon.SinonStub;
  const calls: Call[] = [];

  function install(options: {
    permissions: string[] | null;
    read?: Record<string, unknown>;
    readStatus?: number;
    put?: (body: any) => { status: number; body: unknown };
  }) {
    calls.length = 0;
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || 'GET').toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ url, method, body });
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      if (url.includes('/auth/users/me')) {
        return json({
          username: 'operator',
          email: 'operator@example.com',
          email_verified: true,
          permissions: options.permissions,
        });
      }
      if (url.includes(SETTING_URL) && method === 'PUT') {
        const answer = options.put
          ? options.put(body)
          : { status: 200, body: setting(body) };
        return json(answer.body, answer.status);
      }
      if (url.includes(SETTING_URL)) {
        if (options.readStatus && options.readStatus !== 200) {
          return json(
            { detail: 'Required: view_runtime_sessions' },
            options.readStatus
          );
        }
        return json(setting(options.read));
      }
      return json({});
    });
  }

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  async function render(): Promise<SessionEmbeddingSettings> {
    const el = await fixture<SessionEmbeddingSettings>(
      html`<session-embedding-settings></session-embedding-settings>`
    );
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="embedding-card"]'),
      'the card did not render'
    );
    await el.updateComplete;
    return el;
  }

  function q<T extends Element = HTMLElement>(
    el: SessionEmbeddingSettings,
    id: string
  ): T | null {
    return el.shadowRoot!.querySelector(`[data-testid="${id}"]`) as T | null;
  }

  function setValue(target: HTMLElement, value: unknown, event = 'sl-change') {
    (target as any).value = value;
    target.dispatchEvent(new CustomEvent(event, { bubbles: true }));
  }

  describe('permission split', () => {
    it('lets a manage_budgets holder turn it on, pick a scope, set a cap and save', async () => {
      install({
        permissions: ['view_runtime_sessions', 'manage_budgets'],
      });
      const el = await render();

      const toggle = q(el, 'embedding-enabled')!;
      expect(toggle.hasAttribute('disabled')).to.equal(false);
      (toggle as any).checked = true;
      toggle.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await el.updateComplete;

      setValue(q(el, 'embedding-model')!, 'text-embedding-3-small', 'sl-input');
      setValue(
        q(el, 'embedding-base-url')!,
        'https://embeddings.example.com/v1',
        'sl-input'
      );
      setValue(q(el, 'embedding-scope')!, 'full');
      setValue(q(el, 'embedding-cap')!, '0.75', 'sl-input');
      await el.updateComplete;

      q(el, 'embedding-save')!.click();
      await waitUntil(
        () => calls.some((call) => call.method === 'PUT'),
        'the save did not reach the endpoint'
      );
      const put = calls.find((call) => call.method === 'PUT')!;
      expect(put.url).to.contain(SETTING_URL);
      expect(put.body).to.deep.equal({
        enabled: true,
        scope: 'full',
        daily_cap_usd: 0.75,
        provider: 'openai_compatible',
        model_identifier: 'text-embedding-3-small',
        base_url: 'https://embeddings.example.com/v1',
      });
      await waitUntil(
        () => q(el, 'embedding-saved'),
        'the saved confirmation did not appear'
      );
      expect(q(el, 'embedding-status')!.textContent).to.contain('On');
    });

    it('shows a view_runtime_sessions holder the state and no way to change it', async () => {
      install({
        permissions: ['view_runtime_sessions'],
        read: {
          enabled: true,
          scope: 'full',
          provider: 'local',
          model_identifier: 'all-MiniLM',
          daily_cap_usd: 5,
        },
      });
      const el = await render();

      expect(q(el, 'embedding-status')!.textContent).to.contain('On');
      expect(q(el, 'embedding-enabled')!.hasAttribute('disabled')).to.equal(
        true
      );
      expect(q(el, 'embedding-scope')!.hasAttribute('disabled')).to.equal(true);
      expect(q(el, 'embedding-cap')!.hasAttribute('disabled')).to.equal(true);
      expect(q(el, 'embedding-model')!.hasAttribute('disabled')).to.equal(true);
      expect(q(el, 'embedding-save')).to.equal(null);
      expect(q(el, 'embedding-read-only')!.textContent).to.contain(
        'Manage Budgets'
      );
      expect((q(el, 'embedding-scope') as any).value).to.equal('full');
      expect(calls.some((call) => call.method === 'PUT')).to.equal(false);
    });

    it('renders nothing when the account setting cannot be read', async () => {
      install({ permissions: [], readStatus: 403 });
      const el = await fixture<SessionEmbeddingSettings>(
        html`<session-embedding-settings></session-embedding-settings>`
      );
      await waitUntil(
        () => (el as any).loaded === true,
        'the component did not settle'
      );
      await el.updateComplete;
      expect(q(el, 'embedding-card')).to.equal(null);
      expect(q(el, 'embedding-save')).to.equal(null);
    });
  });

  describe('scope copy', () => {
    it('offers exactly the two scopes and states the storage cost of each', async () => {
      install({ permissions: null });
      const el = await render();

      const options = Array.from(
        q(el, 'embedding-scope')!.querySelectorAll('sl-radio')
      ).map((radio) => radio.getAttribute('value'));
      expect(options).to.deep.equal(['summaries_only', 'full']);
      const summaries = q(el, 'scope-summaries_only')!.textContent!;
      const full = q(el, 'scope-full')!.textContent!;
      expect(summaries).to.contain('60 MB');
      expect(summaries).to.contain('10,000 sessions');
      expect(full).to.contain('2.4 GB');
      expect(full).to.contain('forty times');
    });

    it('says narrowing keeps existing vectors and widening hands back the backlog', async () => {
      install({ permissions: null });
      const el = await render();

      const note = q(el, 'scope-change-note')!.textContent!;
      expect(note).to.contain('does not delete');
      expect(note).to.contain('does not reclaim');
      expect(note).to.contain('backlog');
    });

    it('shows the deployment default when the account has no cap of its own', async () => {
      install({ permissions: null, read: { deployment_daily_cap_usd: 2 } });
      const el = await render();

      const cap = q(el, 'embedding-cap')!;
      expect((cap as any).value).to.equal('');
      expect(cap.getAttribute('placeholder')).to.equal('$2.00');
      const help = q(el, 'embedding-cap-help')!.textContent!.replace(
        /\s+/g,
        ' '
      );
      expect(help).to.contain('deployment default of $2.00');
    });
  });

  describe('read-only state', () => {
    it('shows corpus progress and the degraded reason from the same read', async () => {
      install({
        permissions: ['view_runtime_sessions'],
        read: {
          enabled: true,
          provider: 'local',
          model_identifier: 'all-MiniLM',
          degraded_reason: 'daily_cap_reached',
          degraded_at: '2026-09-27T08:00:00Z',
          corpus: {
            vectors: 1500,
            model_vectors: 1200,
            pending: 300,
            embedded_through: '2026-09-26T18:00:00Z',
          },
        },
      });
      const el = await render();

      const progress = q(el, 'embedding-progress')!.textContent!;
      expect(progress).to.contain('1,200');
      expect(progress).to.contain('300');
      const degraded = q(el, 'embedding-degraded')!.textContent!;
      expect(degraded).to.contain('daily cap');
    });

    it('does not read as a stalled backlog when embedding is off', async () => {
      install({
        permissions: null,
        read: {
          enabled: false,
          provider: 'local',
          model_identifier: 'all-MiniLM',
          corpus: {
            vectors: 500,
            model_vectors: 500,
            pending: 40,
            embedded_through: null,
          },
        },
      });
      const el = await render();

      const progress = q(el, 'embedding-progress')!.textContent!.replace(
        /\s+/g,
        ' '
      );
      expect(progress).to.contain('Embedding is off');
      expect(progress).to.contain('500');
      expect(progress).to.not.contain('waiting');
      expect(progress).to.contain('40 chunks would be embedded');
    });

    it('warns when the deployment kill switch overrides the account', async () => {
      install({
        permissions: null,
        read: { enabled: true, deployment_embedding_enabled: false },
      });
      const el = await render();

      expect(q(el, 'embedding-kill-switch')!.textContent).to.contain(
        'switched off for this deployment'
      );
    });
  });

  describe('422 path', () => {
    it('shows the refusal sentence and keeps the stored state', async () => {
      install({
        permissions: null,
        put: () => ({
          status: 422,
          body: {
            detail: {
              code: 'invalid_base_url',
              message:
                'an OpenAI compatible base url must not target a private, loopback, or link-local host',
            },
          },
        }),
      });
      const el = await render();

      const toggle = q(el, 'embedding-enabled')!;
      (toggle as any).checked = true;
      toggle.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await el.updateComplete;
      setValue(q(el, 'embedding-model')!, 'text-embedding-3-small', 'sl-input');
      setValue(q(el, 'embedding-base-url')!, 'https://10.0.0.1/v1', 'sl-input');
      await el.updateComplete;

      q(el, 'embedding-save')!.click();
      await waitUntil(
        () => q(el, 'embedding-error'),
        'the refusal was not shown'
      );
      expect(q(el, 'embedding-error')!.textContent).to.contain(
        'must not target a private'
      );
      expect(q(el, 'embedding-status')!.textContent).to.contain('Off');
      expect(q(el, 'embedding-saved')).to.equal(null);
    });

    it('shows a validation 422 from the request schema as its message', async () => {
      install({
        permissions: null,
        read: { enabled: true, provider: 'local', model_identifier: 'm' },
        put: () => ({
          status: 422,
          body: {
            detail: [
              {
                loc: ['body', 'daily_cap_usd'],
                msg: 'Input should be greater than or equal to 0',
                type: 'greater_than_equal',
              },
            ],
          },
        }),
      });
      const el = await render();

      setValue(q(el, 'embedding-cap')!, '1', 'sl-input');
      await el.updateComplete;
      q(el, 'embedding-save')!.click();
      await waitUntil(
        () => q(el, 'embedding-error'),
        'the refusal was not shown'
      );
      expect(q(el, 'embedding-error')!.textContent).to.contain(
        'greater than or equal to 0'
      );
    });

    it('sends an empty endpoint as typed so the server names the refusal', async () => {
      install({
        permissions: null,
        put: () => ({
          status: 422,
          body: {
            detail: {
              code: 'base_url_required',
              message: 'an OpenAI compatible provider must name its base url',
            },
          },
        }),
      });
      const el = await render();

      const toggle = q(el, 'embedding-enabled')!;
      (toggle as any).checked = true;
      toggle.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await el.updateComplete;
      setValue(q(el, 'embedding-model')!, 'text-embedding-3-small', 'sl-input');
      await el.updateComplete;

      q(el, 'embedding-save')!.click();
      await waitUntil(() => q(el, 'embedding-error'), 'no error shown');
      const put = calls.find((call) => call.method === 'PUT')!;
      expect(put.body.base_url).to.equal('');
      expect(q(el, 'embedding-error')!.textContent).to.contain(
        'must name its base url'
      );
    });

    it('refuses a negative cap before sending anything', async () => {
      install({ permissions: null });
      const el = await render();

      setValue(q(el, 'embedding-cap')!, '-1', 'sl-input');
      await el.updateComplete;
      q(el, 'embedding-save')!.click();
      await waitUntil(() => q(el, 'embedding-error'), 'no error shown');
      expect(calls.some((call) => call.method === 'PUT')).to.equal(false);
    });
  });

  it('announces a saved change so the search view can re-run', async () => {
    install({ permissions: null });
    const el = await render();
    const changed = new Promise<CustomEvent>((resolve) =>
      el.addEventListener('session-embedding-changed', (event) =>
        resolve(event as CustomEvent)
      )
    );

    setValue(q(el, 'embedding-scope')!, 'full');
    await el.updateComplete;
    q(el, 'embedding-save')!.click();

    const event = await changed;
    expect(event.detail.setting.scope).to.equal('full');
  });
});
