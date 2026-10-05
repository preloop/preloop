import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './ci-identities-view';
import type { CiIdentitiesView } from './ci-identities-view';
import { CI_ACTIONS, type CiIdentity } from '../../../ci-administration-api';
import {
  mockApi,
  signInForTest,
  type MockApi,
  type MockRoute,
} from '../../../test-helpers/capability-api';

const BASE = '/api/v1/ci-identities';
const grant = {
  version: 1 as const,
  project_id: 'project-a',
  flow_id: 'flow-a',
  actions: [...CI_ACTIONS],
};
const identity: CiIdentity = {
  id: 'principal-a',
  name: 'Review identity',
  is_active: true,
  credential_version: 1,
  grant,
  repository_identifier: 'repo-a',
  repository_slug: 'example/repository',
  tracker_type: 'github',
  keys: [
    {
      id: 'key-a',
      is_active: true,
      actions: [...CI_ACTIONS],
      expires_at: null,
      created_at: null,
      last_used_at: null,
    },
  ],
};
const capabilities = {
  available: true,
  can_view: true,
  can_manage: true,
  supported_actions: [...CI_ACTIONS],
};
const flow = {
  id: 'flow-a',
  name: 'Hosted review',
  account_id: 'account-a',
  is_enabled: true,
  runner_pool: 'server',
  trigger_project_ids: ['project-a'],
  git_clone_config: {
    enabled: true,
    repositories: [{ project_id: 'project-a', tracker_id: 'tracker-a' }],
  },
};

async function ready(element: CiIdentitiesView): Promise<void> {
  await waitUntil(() => !element.querySelector('[data-loading]'));
  await element.updateComplete;
}
async function value(
  element: CiIdentitiesView,
  selector: string,
  input: string
): Promise<void> {
  const target = element.querySelector<HTMLInputElement | HTMLSelectElement>(
    selector
  )!;
  target.value = input;
  target.dispatchEvent(
    new Event(target.tagName === 'SELECT' ? 'change' : 'input', {
      bubbles: true,
    })
  );
  await element.updateComplete;
}
function button(element: Element, label: string): HTMLButtonElement {
  return [...element.querySelectorAll('button')].find(
    (button) => button.textContent?.trim() === label
  )!;
}
async function selectGrant(element: CiIdentitiesView): Promise<void> {
  await value(element, '#ci-name', 'Review');
  await value(element, '#ci-project', 'project-a');
  await value(element, '#ci-flow', 'flow-a');
}

describe('restricted CI settings view', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
    sessionStorage.clear();
  });
  function routes(
    extra: MockRoute[] = [],
    rows: CiIdentity[] = [identity]
  ): MockRoute[] {
    return [
      ...extra,
      { path: BASE + '/capabilities', body: capabilities },
      { path: BASE, body: rows },
      {
        path: '/api/v1/projects',
        body: [
          { id: 'project-a', name: 'Repository', organization_id: 'org-a' },
        ],
      },
      { path: '/api/v1/flows', body: [flow] },
      {
        method: 'POST',
        path: BASE + '/preview',
        body: (call) => ({
          grant: (call.body as { grant: unknown }).grant,
          repository_identifier: 'repo-a',
          repository_slug: 'example/repository',
          tracker_type: 'github',
          runner_pool: 'server',
        }),
      },
    ];
  }
  async function render(): Promise<CiIdentitiesView> {
    const element = await fixture<CiIdentitiesView>(
      html`<ci-identities-view></ci-identities-view>`
    );
    await ready(element);
    return element;
  }

  for (const state of [
    null,
    { ...capabilities, available: false },
    { ...capabilities, can_view: false },
    { ...capabilities, supported_actions: CI_ACTIONS.slice(1) },
  ]) {
    it(`fails closed for unavailable/view-denied/incomplete capabilities ${JSON.stringify(state)}`, async () => {
      api = mockApi({
        routes: [
          {
            path: BASE + '/capabilities',
            status: state ? 200 : 404,
            body: state,
          },
        ],
      });
      const element = await render();
      expect(element.shadowRoot).to.equal(null);
      expect(element.textContent).to.contain('unavailable');
      expect(element.querySelector('#ci-create')).to.equal(null);
      expect(api.callsTo(BASE)).to.have.length(0);
      expect(api.callsTo('/api/v1/projects')).to.have.length(0);
    });
  }
  it('shows safe metadata in light DOM without loading management resources for view-only users', async () => {
    api = mockApi({
      routes: routes([
        {
          path: BASE + '/capabilities',
          body: { ...capabilities, can_manage: false },
        },
      ]),
    });
    const element = await render();
    expect(element.shadowRoot).to.equal(null);
    expect(element.textContent).to.contain('Review identity');
    expect(element.textContent).to.contain('View only');
    expect(element.querySelector('#ci-create')).to.equal(null);
    expect(element.textContent).not.to.contain('Rotate key');
    expect(api.callsTo('/api/v1/projects')).to.have.length(0);
  });
  it('selects real dedicated flows and requires preview; changes invalidate it', async () => {
    api = mockApi({
      routes: routes([
        {
          path: '/api/v1/flows',
          body: [
            flow,
            { ...flow, id: 'local', runner_pool: 'local' },
            { ...flow, id: 'shared', account_id: null },
            { ...flow, id: 'disabled', is_enabled: false },
            { ...flow, id: 'other', trigger_project_ids: ['project-b'] },
          ],
        },
      ]),
    });
    const element = await render();
    await selectGrant(element);
    expect(
      [...element.querySelectorAll('#ci-flow option')].map(
        (option) => (option as HTMLOptionElement).value
      )
    ).to.eql(['', 'flow-a']);
    expect(
      (element.querySelector('#ci-create') as HTMLButtonElement).disabled
    ).to.equal(true);
    button(element, 'Preview grant').click();
    await waitUntil(() => !!element.querySelector('#ci-preview-summary'));
    expect(api.callsTo(BASE + '/preview', 'POST')[0].body).to.eql({ grant });
    expect(
      (element.querySelector('#ci-create') as HTMLButtonElement).disabled
    ).to.equal(false);
    const action = element.querySelector<HTMLInputElement>(
      'input[type=checkbox]'
    )!;
    action.checked = false;
    action.dispatchEvent(new Event('change', { bubbles: true }));
    await element.updateComplete;
    expect(element.querySelector('#ci-preview-summary')).to.equal(null);
    expect(
      (element.querySelector('#ci-create') as HTMLButtonElement).disabled
    ).to.equal(true);
  });
  it('discloses a new token once and removes it on close/disconnect without storage', async () => {
    api = mockApi({
      routes: routes(
        [
          {
            method: 'POST',
            path: BASE,
            status: 201,
            body: {
              identity,
              key_id: 'key-a',
              token: 'synthetic-once-token',
              secret_note: 'once',
            },
          },
        ],
        []
      ),
    });
    const element = await render();
    await selectGrant(element);
    button(element, 'Preview grant').click();
    await waitUntil(() => !!element.querySelector('#ci-preview-summary'));
    button(element, 'Create identity and key').click();
    await waitUntil(() => !!element.querySelector('dialog'));
    expect(element.querySelector('dialog')!.textContent).to.contain(
      'synthetic-once-token'
    );
    expect(JSON.stringify(localStorage)).not.to.contain('synthetic-once-token');
    expect(JSON.stringify(sessionStorage)).not.to.contain(
      'synthetic-once-token'
    );
    button(element, 'Close and forget secret').click();
    await element.updateComplete;
    expect(element.querySelector('dialog')).to.equal(null);
    expect(element.textContent).not.to.contain('synthetic-once-token');
    button(element, 'Preview grant').click();
    await waitUntil(() => !!element.querySelector('#ci-preview-summary'));
    button(element, 'Create identity and key').click();
    await waitUntil(() => !!element.querySelector('dialog'));
    element.remove();
    await element.updateComplete;
    expect(element.textContent).not.to.contain('synthetic-once-token');
  });
  it('denies an operation when current management authority is revoked', async () => {
    let reads = 0;
    api = mockApi({
      routes: routes([
        {
          path: BASE + '/capabilities',
          body: () => ({ ...capabilities, can_manage: ++reads === 1 }),
        },
      ]),
    });
    const element = await render();
    button(element, 'Issue replacement key').click();
    await waitUntil(() => reads > 1);
    await waitUntil(() => element.textContent?.includes('View only') === true);
    expect(api.callsTo(BASE + '/principal-a/keys', 'POST')).to.have.length(0);
    expect(element.textContent).to.contain('View only');
  });
  it('supports replacement after expiry/revocation and preserves safe disable/reenable paths', async () => {
    const expired = {
      ...identity,
      keys: [
        { ...identity.keys[0], expires_at: '2000-01-01T00:00:00' },
        { ...identity.keys[0], id: 'revoked', is_active: false },
      ],
    };
    api = mockApi({
      routes: routes(
        [
          {
            method: 'POST',
            path: BASE + '/principal-a/keys',
            status: 201,
            body: {
              principal_id: 'principal-a',
              key_id: 'key-b',
              token: 'replacement-once',
              secret_note: 'once',
            },
          },
        ],
        [expired]
      ),
    });
    const element = await render();
    expect(button(element, 'Rotate key').disabled).to.equal(true);
    expect(button(element, 'Issue replacement key').disabled).to.equal(false);
    button(element, 'Issue replacement key').click();
    await waitUntil(() => !!element.querySelector('dialog'));
    expect(api.callsTo(BASE + '/principal-a/keys', 'POST')[0].body).to.eql({});
  });
  it('narrows only existing actions and uses real key anchors for fixed completion callbacks', async () => {
    api = mockApi({
      routes: routes([
        { method: 'PATCH', path: BASE + '/principal-a', body: identity },
        {
          method: 'POST',
          path: BASE + '/principal-a/subscriptions',
          status: 201,
          body: {
            id: 'endpoint-a',
            url: 'https://example.com/completed',
            event_types: ['flow.execution.finished'],
            secret: 'synthetic-signing-secret',
          },
        },
      ]),
    });
    const element = await render();
    button(element, 'Narrow grant').click();
    await element.updateComplete;
    const scope = element.querySelector('[data-principal]')!;
    const action = scope.querySelector<HTMLInputElement>(
      'input[type=checkbox]'
    )!;
    action.checked = false;
    action.dispatchEvent(new Event('change', { bubbles: true }));
    await element.updateComplete;
    button(element, 'Save narrower grant').click();
    await waitUntil(
      () => api.callsTo(BASE + '/principal-a', 'PATCH').length === 1
    );
    await waitUntil(
      () => !button(element, 'Set up completion callback').disabled
    );
    button(element, 'Set up completion callback').click();
    await element.updateComplete;
    expect(
      (element.querySelector('#ci-anchor') as HTMLSelectElement).value
    ).to.equal('key-a');
    await value(element, '#ci-callback-url', 'https://example.com/completed');
    button(element, 'Create completion callback').click();
    await waitUntil(() => !!element.querySelector('dialog'));
    expect(
      api.callsTo(BASE + '/principal-a/subscriptions', 'POST')[0].body
    ).to.eql({
      key_id: 'key-a',
      url: 'https://example.com/completed',
      description: '',
      event_types: ['flow.execution.finished'],
    });
    expect(element.querySelector('dialog')!.textContent).to.contain(
      'synthetic-signing-secret'
    );
    button(element, 'Close and forget secret').click();
    await element.updateComplete;
    expect(element.textContent).not.to.contain('synthetic-signing-secret');
  });
});

// Response races must never disclose a token in a reused view or skip grant preview.
describe('restricted CI setup response races', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });
  async function configured(extra: MockRoute[]): Promise<CiIdentitiesView> {
    api = mockApi({
      routes: [
        ...extra,
        { path: BASE + '/capabilities', body: capabilities },
        { path: BASE, body: [] },
        {
          path: '/api/v1/projects',
          body: [
            { id: 'project-a', name: 'Repository', organization_id: 'org-a' },
          ],
        },
        { path: '/api/v1/flows', body: [flow] },
        {
          method: 'POST',
          path: BASE + '/preview',
          body: {
            grant,
            repository_identifier: 'repo-a',
            repository_slug: 'example/repository',
            tracker_type: 'github',
            runner_pool: 'server',
          },
        },
      ],
    });
    const element = await fixture<CiIdentitiesView>(
      html`<ci-identities-view></ci-identities-view>`
    );
    await ready(element);
    await selectGrant(element);
    button(element, 'Preview grant').click();
    await waitUntil(() => !!element.querySelector('#ci-preview-summary'));
    return element;
  }
  it('issues exactly the previewed grant even if input changes during fresh permission check', async () => {
    const element = await configured([
      {
        method: 'POST',
        path: BASE,
        status: 201,
        body: { token: 'race-fixture', key_id: 'key-a', secret_note: 'once' },
      },
    ]);
    // Delay only the next capability response: actual click still uses the real client.
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const firstCapability = api.callsTo(BASE + '/capabilities').length;
    let held = false;
    // Replace fetch with a separate delegate to the established route fixture.
    const fixtureFetch = window.fetch;
    window.fetch = async (
      input: RequestInfo | URL,
      init?: RequestInit
    ): Promise<Response> => {
      const response = await fixtureFetch(input, init);
      if (String(input).endsWith('/capabilities') && !held) {
        held = true;
        await gate;
      }
      return response;
    };
    try {
      button(element, 'Create identity and key').click();
      await waitUntil(
        () => api.callsTo(BASE + '/capabilities').length > firstCapability
      );
      const checkbox = element.querySelector<HTMLInputElement>(
        'input[type=checkbox]'
      )!;
      checkbox.checked = false;
      checkbox.dispatchEvent(new Event('change', { bubbles: true }));
      await element.updateComplete;
      release();
      await waitUntil(() => api.callsTo(BASE, 'POST').length === 1);
      expect(
        (api.callsTo(BASE, 'POST')[0].body as { grant: unknown }).grant
      ).to.eql(grant);
    } finally {
      release();
      window.fetch = fixtureFetch;
    }
  });
});

describe('restricted CI key and identity lifecycle controls', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });
  async function render(
    extra: MockRoute[],
    row = identity
  ): Promise<CiIdentitiesView> {
    api = mockApi({
      routes: [
        ...extra,
        { path: BASE + '/capabilities', body: capabilities },
        { path: BASE, body: [row] },
        { path: '/api/v1/projects', body: [] },
        { path: '/api/v1/flows', body: [] },
      ],
    });
    const element = await fixture<CiIdentitiesView>(
      html`<ci-identities-view></ci-identities-view>`
    );
    await ready(element);
    return element;
  }
  it('rotates the actual key and forgets its token on Escape', async () => {
    const element = await render([
      {
        method: 'POST',
        path: BASE + '/principal-a/keys/key-a/rotate',
        body: {
          key_id: 'key-b',
          principal_id: 'principal-a',
          token: 'rotated-ui-secret',
        },
      },
    ]);
    button(element, 'Rotate key').click();
    await waitUntil(() => !!element.querySelector('dialog'));
    expect(
      api.callsTo(BASE + '/principal-a/keys/key-a/rotate', 'POST')[0].body
    ).to.eql({});
    expect(element.querySelector('dialog')!.open).to.equal(true);
    expect(document.activeElement?.closest('dialog')).to.equal(
      element.querySelector('dialog')
    );
    element
      .querySelector('dialog')!
      .dispatchEvent(new Event('cancel', { cancelable: true }));
    await element.updateComplete;
    expect(element.textContent).not.to.contain('rotated-ui-secret');
    expect(element.querySelector('dialog')).to.equal(null);
  });
  it('shows a copy failure inside the open secret dialog', async () => {
    const element = await render([
      {
        method: 'POST',
        path: BASE + '/principal-a/keys/key-a/rotate',
        body: {
          key_id: 'key-b',
          principal_id: 'principal-a',
          token: 'rotated-ui-secret',
        },
      },
    ]);
    button(element, 'Rotate key').click();
    await waitUntil(() => !!element.querySelector('dialog'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error('denied')) },
    });
    button(element, 'Copy secret').click();
    const dialog = element.querySelector('dialog')!;
    await waitUntil(() => !!dialog.querySelector('[role="alert"]'));
    await element.updateComplete;
    expect(dialog.open).to.equal(true);
    expect(dialog.querySelector('[role="alert"]')?.textContent).to.contain(
      'Copy failed. Select the secret and copy it manually.'
    );
    expect(dialog.textContent).to.contain('rotated-ui-secret');
    const outside = [...element.querySelectorAll('[role="alert"]')].filter(
      (node) => !dialog.contains(node)
    );
    expect(outside).to.eql([]);
    button(dialog, 'Close and forget secret').click();
    await element.updateComplete;
    expect(element.textContent).not.to.contain('Copy failed');
    expect(element.textContent).not.to.contain('rotated-ui-secret');
    expect(element.querySelector('dialog')).to.equal(null);
  });
  it('revokes the real audit key without opening a token dialog', async () => {
    const element = await render([
      { method: 'DELETE', path: BASE + '/principal-a/keys/key-a', status: 204 },
    ]);
    button(element, 'Revoke key').click();
    await waitUntil(
      () => api.callsTo(BASE + '/principal-a/keys/key-a', 'DELETE').length === 1
    );
    await waitUntil(() => api.callsTo(BASE).length === 2);
    await element.updateComplete;
    expect(element.querySelector('dialog')).to.equal(null);
  });
  for (const enabled of [true, false]) {
    it(`sends explicit human ${enabled ? 'disable' : 'reenable'} and blocks disabled issuance`, async () => {
      const element = await render(
        [
          {
            method: 'PATCH',
            path: BASE + '/principal-a',
            body: { ...identity, is_active: !enabled },
          },
        ],
        { ...identity, is_active: enabled }
      );
      expect(button(element, 'Issue replacement key').disabled).to.equal(
        !enabled
      );
      button(
        element,
        enabled ? 'Disable identity' : 'Reenable identity'
      ).click();
      await waitUntil(
        () => api.callsTo(BASE + '/principal-a', 'PATCH').length === 1
      );
      expect(api.callsTo(BASE + '/principal-a', 'PATCH')[0].body).to.eql({
        enabled: !enabled,
      });
    });
  }
  it('never echoes a server failure containing synthetic secret material', async () => {
    const element = await render([
      {
        method: 'POST',
        path: BASE + '/principal-a/keys',
        status: 400,
        body: { detail: 'private-ui-error-secret' },
      },
    ]);
    button(element, 'Issue replacement key').click();
    await waitUntil(() => !!element.querySelector('[role=alert]'));
    expect(element.textContent).not.to.contain('private-ui-error-secret');
    expect(element.querySelector('dialog')).to.equal(null);
  });
});
