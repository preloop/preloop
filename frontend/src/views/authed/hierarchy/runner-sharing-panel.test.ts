import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './runner-sharing-panel';
import type { RunnerSharingPanel } from './runner-sharing-panel';
import { CAPABILITY_EXTENSIONS } from '../../../components/capability-extension';
import {
  mockApi,
  signInForTest,
  TEST_PROFILE,
  type MockApi,
} from '../../../test-helpers/capability-api';

const RUNNER = 'runner-1';

function sharing(overrides: Record<string, unknown> = {}) {
  return {
    runner_id: RUNNER,
    owner_user_id: TEST_PROFILE.id,
    policy: {
      owner_consent: 'required',
      source: 'default',
      runner_setting: 'inherit',
      account_setting: 'required',
    },
    can_share: true,
    can_set_policy: false,
    grants: [
      {
        id: 'grant-1',
        runner_id: RUNNER,
        owner_user_id: TEST_PROFILE.id,
        grantee_type: 'team',
        grantee_id: 'team-1',
        scopes: ['sessions'],
        harnesses: ['claude_code'],
        directory_ids: ['dir_d'],
        consent_state: 'pending_owner_consent',
        consent_source: null,
        created_by_user_id: 'admin-1',
        created_at: null,
        revoked_at: null,
      },
    ],
    ...overrides,
  };
}

async function mount(): Promise<RunnerSharingPanel> {
  const el = await fixture<RunnerSharingPanel>(
    html`<runner-sharing-panel
      .context=${{ runners: [{ id: RUNNER, name: 'laptop' }] }}
    ></runner-sharing-panel>`
  );
  const select = el.shadowRoot!.querySelector(
    '[data-testid="sharing-runner"]'
  ) as HTMLSelectElement;
  select.value = RUNNER;
  select.dispatchEvent(new CustomEvent('sl-change'));
  await waitUntil(() =>
    el.shadowRoot!.querySelector('[data-testid="effective-policy"]')
  );
  return el;
}

describe('runner sharing panel', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('is registered behind the runner_sharing capability', () => {
    expect(CAPABILITY_EXTENSIONS['runner-sharing'].capabilities).to.deep.equal([
      'runner_sharing',
    ]);
  });

  it('shows the effective policy and lets the owner accept a waiting share', async () => {
    api = mockApi({
      routes: [
        { path: `/api/v1/runners/${RUNNER}/shares`, body: sharing() },
        {
          path: '/api/v1/teams',
          body: {
            teams: [{ id: 'team-1', name: 'platform' }],
            total: 1,
            skip: 0,
            limit: 100,
          },
        },
        {
          method: 'POST',
          path: `/api/v1/runners/${RUNNER}/shares/grant-1/accept`,
          body: {},
        },
      ],
    });
    const el = await mount();
    const text = el.shadowRoot!.textContent ?? '';
    expect(text).to.contain('Owner consent required');
    expect(text).to.contain('default');
    expect(text).to.contain('Waiting for owner');
    expect(text).to.contain('claude_code');
    // Members cannot change the policy.
    expect(el.shadowRoot!.querySelector('[data-testid="runner-policy"]')).to.be
      .null;
    await waitUntil(() =>
      (el.shadowRoot!.textContent ?? '').includes('Team platform')
    );
    (
      el.shadowRoot!.querySelector(
        '[data-testid="accept-share"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        api.callsTo(`/api/v1/runners/${RUNNER}/shares/grant-1/accept`, 'POST')
          .length === 1
    );
  });

  it('lets an admin override the policy for one runner', async () => {
    api = mockApi({
      routes: [
        {
          path: `/api/v1/runners/${RUNNER}/shares`,
          body: sharing({
            can_set_policy: true,
            owner_user_id: 'someone-else',
          }),
        },
        {
          method: 'PUT',
          path: `/api/v1/runners/${RUNNER}/sharing-policy`,
          body: {},
        },
      ],
    });
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-testid="accept-share"]')).to.be
      .null;
    const select = el.shadowRoot!.querySelector(
      '[data-testid="runner-policy"]'
    ) as HTMLSelectElement;
    select.value = 'not_required';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(
      () =>
        api.callsTo(`/api/v1/runners/${RUNNER}/sharing-policy`, 'PUT')
          .length === 1
    );
    expect(
      api.callsTo(`/api/v1/runners/${RUNNER}/sharing-policy`, 'PUT')[0].body
    ).to.deep.equal({ owner_consent: 'not_required' });
  });

  it('creates a scoped share', async () => {
    api = mockApi({
      routes: [
        {
          path: `/api/v1/runners/${RUNNER}/shares`,
          body: sharing({ grants: [] }),
        },
        {
          path: '/api/v1/users',
          body: {
            users: [{ id: 'user-2', username: 'bo', email: 'bo@example.com' }],
            total: 1,
            skip: 0,
            limit: 100,
          },
        },
        { method: 'POST', path: `/api/v1/runners/${RUNNER}/shares`, body: {} },
      ],
    });
    const el = await mount();
    expect(el.shadowRoot!.textContent).to.contain('Not shared.');
    const root = el.shadowRoot!;
    const grantee = root.querySelector(
      '[data-testid="grantee"]'
    ) as HTMLSelectElement;
    grantee.value = 'user:user-2';
    grantee.dispatchEvent(new CustomEvent('sl-change'));
    const harnesses = root.querySelector(
      '[data-testid="harnesses"]'
    ) as HTMLInputElement;
    harnesses.value = 'claude_code';
    harnesses.dispatchEvent(new CustomEvent('sl-input'));
    const dirs = root.querySelector(
      '[data-testid="directories"]'
    ) as HTMLInputElement;
    dirs.value = 'dir_d';
    dirs.dispatchEvent(new CustomEvent('sl-input'));
    await el.updateComplete;
    await waitUntil(
      () => (el as unknown as { grantees: unknown[] }).grantees.length > 0
    );
    (root.querySelector('[data-testid="share-submit"]') as HTMLElement).click();
    await waitUntil(
      () => api.callsTo(`/api/v1/runners/${RUNNER}/shares`, 'POST').length === 1
    );
    expect(
      api.callsTo(`/api/v1/runners/${RUNNER}/shares`, 'POST')[0].body
    ).to.deep.equal({
      grantee_type: 'user',
      grantee_id: 'user-2',
      scopes: ['sessions'],
      harnesses: ['claude_code'],
      directory_ids: ['dir_d'],
    });
  });
});

describe('runner sharing panel edge cases', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('renders nothing when the server has no runner sharing', async () => {
    api = mockApi({ routes: [] });
    const el = await fixture<RunnerSharingPanel>(
      html`<runner-sharing-panel
        .context=${{ runners: [{ id: RUNNER, name: 'laptop' }] }}
      ></runner-sharing-panel>`
    );
    const select = el.shadowRoot!.querySelector(
      '[data-testid="sharing-runner"]'
    ) as HTMLSelectElement;
    select.value = RUNNER;
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() => !el.shadowRoot!.querySelector('h3'));
    expect(el.shadowRoot!.querySelector('[role="alert"]')).to.be.null;
  });

  it('explains when the owner cannot list people', async () => {
    api = mockApi({
      routes: [
        {
          path: `/api/v1/runners/${RUNNER}/shares`,
          body: sharing({ grants: [] }),
        },
        { path: '/api/v1/teams', status: 403, body: {} },
        { path: '/api/v1/users', status: 403, body: {} },
      ],
    });
    const el = await mount();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="share-form-unavailable"]')
    );
    expect(el.shadowRoot!.querySelector('[data-testid="share-form"]')).to.be
      .null;
  });

  it('drops a response for a runner that is no longer selected', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    const reply = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    const stub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input instanceof Request ? input.url : input);
        if (url.includes(`/runners/${RUNNER}/shares`)) {
          await gate;
          return reply(sharing());
        }
        if (url.includes('/runners/runner-2/shares')) {
          return reply(sharing({ runner_id: 'runner-2', grants: [] }));
        }
        if (url.includes('/auth/users/me')) return reply(TEST_PROFILE);
        return new Response('{}', { status: 404 });
      });
    try {
      const el = await fixture<RunnerSharingPanel>(
        html`<runner-sharing-panel
          .context=${{
            runners: [
              { id: RUNNER, name: 'laptop' },
              { id: 'runner-2', name: 'desktop' },
            ],
          }}
        ></runner-sharing-panel>`
      );
      const select = el.shadowRoot!.querySelector(
        '[data-testid="sharing-runner"]'
      ) as HTMLSelectElement;
      select.value = RUNNER;
      select.dispatchEvent(new CustomEvent('sl-change'));
      await el.updateComplete;
      select.value = 'runner-2';
      select.dispatchEvent(new CustomEvent('sl-change'));
      await waitUntil(() =>
        (el.shadowRoot!.textContent ?? '').includes('Not shared.')
      );
      release();
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(el.shadowRoot!.textContent).to.contain('Not shared.');
      expect(el.shadowRoot!.querySelector('[data-testid="share-row"]')).to.be
        .null;
    } finally {
      stub.restore();
    }
  });
});
