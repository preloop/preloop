import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './agent-credentials-card';
import type { AgentCredentialsCard } from './agent-credentials-card';
import { invalidateApiCaches } from '../api';

const credential = {
  id: 'credential-1',
  name: 'Gateway key',
  key_prefix: 'agt_example',
  scopes: ['mcp:read', 'mcp:write'],
  status: 'active',
  created_at: '2026-11-20T10:00:00Z',
  expires_at: null,
  last_used_at: null,
};

describe('Agent credentials card', () => {
  let fetchStub: sinon.SinonStub;
  let permissions: string[] | null;
  const json = (body: unknown, status = 200): Response =>
    new Response(JSON.stringify(body), { status });
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-token');
    permissions = ['view_agents', 'manage_agents'];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/auth/users/me'))
        return json({ id: 'user-1', permissions });
      if (init?.method === 'POST')
        return json({ credential, token: 'agt_synthetic_once_only' }, 201);
      if (init?.method === 'DELETE')
        return json({ ...credential, status: 'revoked' });
      return json([credential]);
    });
  });
  afterEach(() => {
    sinon.restore();
    invalidateApiCaches();
    localStorage.clear();
    document.querySelector('confirm-dialog')?.remove();
  });
  async function mount(lifecycle = 'active'): Promise<AgentCredentialsCard> {
    const el = await fixture<AgentCredentialsCard>(
      html`<agent-credentials-card
        agentId="agent-1"
        lifecycleState=${lifecycle}
      ></agent-credentials-card>`
    );
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Gateway key'));
    return el;
  }
  it('lists metadata, distinguishes expired keys, and never retrieves the full key', async () => {
    fetchStub.callsFake(async (input) =>
      String(input).endsWith('/auth/users/me')
        ? json({ permissions })
        : json([
            credential,
            {
              ...credential,
              id: 'expired-1',
              name: 'Old key',
              expires_at: '2020-01-01T00:00:00Z',
            },
          ])
    );
    const el = await mount();
    expect(el.shadowRoot!.textContent).to.contain('agt_example');
    expect(el.shadowRoot!.textContent).to.contain('Never used');
    expect(el.shadowRoot!.textContent).to.contain('Expired');
    expect(el.shadowRoot!.textContent).not.to.contain(
      'agt_synthetic_once_only'
    );
  });
  it('exposes creation, sends name and expiry, and clears the one-time key on close', async () => {
    const el = await mount();
    const result = el.openCreate();
    await el.updateComplete;
    const name = el.shadowRoot!.querySelector('sl-input') as any;
    name.value = 'Test key';
    name.dispatchEvent(new Event('sl-input'));
    const expiry = el.shadowRoot!.querySelector('sl-select') as any;
    expiry.value = '30';
    expiry.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('[data-create-submit]') as HTMLElement
    ).click();
    expect((await result)?.token).to.equal('agt_synthetic_once_only');
    await el.updateComplete;
    const post = fetchStub
      .getCalls()
      .find((c) => c.args[1]?.method === 'POST')!;
    expect(JSON.parse(post.args[1].body)).to.deep.equal({
      name: 'Test key',
      expires_in_days: 30,
    });
    expect(el.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.contain(
      'You will not be able to see it again'
    );
    expect(
      (el.shadowRoot!.querySelector('sl-copy-button') as any).value
    ).to.equal('agt_synthetic_once_only');
    (el.shadowRoot!.querySelector('[data-key-done]') as HTMLElement).click();
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).not.to.contain(
      'agt_synthetic_once_only'
    );
    expect(el.shadowRoot!.querySelector('sl-copy-button')).to.equal(null);
  });
  it('requires confirmation before revocation and updates status', async () => {
    const el = await mount();
    (el.shadowRoot!.querySelector('[data-revoke]') as HTMLElement).click();
    await waitUntil(
      () =>
        !!document
          .querySelector('confirm-dialog')
          ?.shadowRoot?.querySelector('[data-testid="confirm-dialog-confirm"]')
    );
    expect(
      fetchStub.getCalls().some((c) => c.args[1]?.method === 'DELETE')
    ).to.equal(false);
    (
      document
        .querySelector('confirm-dialog')!
        .shadowRoot!.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
    ).click();
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Revoked'));
    expect(
      fetchStub.getCalls().filter((c) => c.args[1]?.method === 'DELETE')
    ).to.have.length(1);
  });
  it('keeps view-only users from creating or revoking', async () => {
    permissions = ['view_agents'];
    const el = await mount();
    expect(el.shadowRoot!.querySelector('[data-create-open]')).to.equal(null);
    expect(el.shadowRoot!.querySelector('[data-revoke]')).to.equal(null);
    expect(await el.openCreate()).to.equal(null);
  });
  it('does not load credentials without view permission', async () => {
    permissions = [];
    const el = await fixture<AgentCredentialsCard>(
      html`<agent-credentials-card agentId="agent-1"></agent-credentials-card>`
    );
    await waitUntil(() => fetchStub.called);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/credentials'))
    ).to.equal(false);
    expect(await el.openCreate()).to.equal(null);
  });
  it('allows OSS permissions and blocks creation on inactive agents', async () => {
    permissions = null;
    const el = await mount('suspended');
    expect(await el.openCreate()).to.equal(null);
    expect(
      (el.shadowRoot!.querySelector('[data-create-open]') as any).disabled
    ).to.equal(true);
    expect(el.shadowRoot!.querySelector('[data-revoke]')).not.to.equal(null);
  });
  async function fillCreate(
    el: AgentCredentialsCard,
    expiryValue = '365'
  ): Promise<void> {
    await el.updateComplete;
    const name = el.shadowRoot!.querySelector('sl-input') as any;
    name.value = 'New key';
    name.dispatchEvent(new Event('sl-input'));
    const expiry = el.shadowRoot!.querySelector('sl-select') as any;
    expiry.value = expiryValue;
    expiry.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('[data-create-submit]') as HTMLElement
    ).click();
  }

  it('sends explicit null for a non-expiring key', async () => {
    const el = await mount();
    const pending = el.openCreate();
    await fillCreate(el, 'never');
    await pending;
    const post = fetchStub
      .getCalls()
      .find((call) => call.args[1]?.method === 'POST')!;
    expect(JSON.parse(post.args[1].body).expires_in_days).to.equal(null);
  });

  it('clears a new key on Escape or backdrop dismissal', async () => {
    const el = await mount();
    const pending = el.openCreate();
    await fillCreate(el);
    await pending;
    await el.updateComplete;
    el.shadowRoot!.querySelector('sl-dialog')!.dispatchEvent(
      new CustomEvent('sl-hide')
    );
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).not.to.contain(
      'agt_synthetic_once_only'
    );
    expect(el.shadowRoot!.querySelector('sl-copy-button')).to.equal(null);
  });

  it('returns null when creation is dismissed without issuing a key', async () => {
    const el = await mount();
    const pending = el.openCreate();
    await el.updateComplete;
    el.shadowRoot!.querySelector('sl-dialog')!.dispatchEvent(
      new CustomEvent('sl-hide')
    );
    expect(await pending).to.equal(null);
    expect(
      fetchStub.getCalls().some((call) => call.args[1]?.method === 'POST')
    ).to.equal(false);
  });

  it('does not dismiss or issue duplicate keys while a create is in flight', async () => {
    const el = await mount();
    let finish!: (response: Response) => void;
    fetchStub.callsFake(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        })
    );
    const pending = el.openCreate();
    await fillCreate(el);
    await el.updateComplete;
    (
      el.shadowRoot!.querySelector('[data-create-submit]') as HTMLElement
    ).click();
    const close = new CustomEvent('sl-request-close', { cancelable: true });
    el.shadowRoot!.querySelector('sl-dialog')!.dispatchEvent(close);
    expect(close.defaultPrevented).to.equal(true);
    expect(
      fetchStub.getCalls().filter((call) => call.args[1]?.method === 'POST')
    ).to.have.length(1);
    finish(json({ credential, token: 'agt_synthetic_once_only' }));
    expect((await pending)?.token).to.equal('agt_synthetic_once_only');
  });

  it('discards a late creation result after navigation', async () => {
    const el = await mount();
    let finish!: (response: Response) => void;
    fetchStub.callsFake(async (_input, init) =>
      init?.method === 'POST'
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : json([])
    );
    const pending = el.openCreate();
    await fillCreate(el);
    el.agentId = 'agent-2';
    await el.updateComplete;
    expect(await pending).to.equal(null);
    finish(json({ credential, token: 'agt_synthetic_once_only' }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(el.shadowRoot!.textContent).not.to.contain(
      'agt_synthetic_once_only'
    );
    expect(el.shadowRoot!.textContent).not.to.contain('Gateway key');
  });

  it('shows creation errors and allows retry without closing the form', async () => {
    const el = await mount();
    fetchStub.callsFake(async () =>
      json({ detail: 'A credential with this name already exists' }, 400)
    );
    const pending = el.openCreate();
    await fillCreate(el);
    await waitUntil(() =>
      el.shadowRoot?.textContent?.includes('already exists')
    );
    expect(el.shadowRoot!.querySelector('sl-copy-button')).to.equal(null);
    fetchStub.callsFake(async () =>
      json({ credential, token: 'agt_synthetic_once_only' })
    );
    (
      el.shadowRoot!.querySelector('[data-create-submit]') as HTMLElement
    ).click();
    expect((await pending)?.token).to.equal('agt_synthetic_once_only');
  });

  it('shows load failures with a retry and no misleading empty state', async () => {
    fetchStub.callsFake(async (input) =>
      String(input).endsWith('/auth/users/me')
        ? json({ permissions })
        : json({ detail: 'Credentials unavailable' }, 500)
    );
    const el = await fixture<AgentCredentialsCard>(
      html`<agent-credentials-card agentId="agent-1"></agent-credentials-card>`
    );
    await waitUntil(() =>
      el.shadowRoot?.textContent?.includes('Credentials unavailable')
    );
    expect(el.shadowRoot!.textContent).not.to.contain('No credentials yet');
    fetchStub.callsFake(async () => json([credential]));
    (el.shadowRoot!.querySelector('sl-alert sl-button') as HTMLElement).click();
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Gateway key'));
  });

  it('does not revoke when confirmation is cancelled', async () => {
    const el = await mount();
    (el.shadowRoot!.querySelector('[data-revoke]') as HTMLElement).click();
    await waitUntil(
      () =>
        !!document
          .querySelector('confirm-dialog')
          ?.shadowRoot?.querySelector('sl-dialog')
    );
    document
      .querySelector('confirm-dialog')!
      .shadowRoot!.querySelector('sl-dialog')!
      .dispatchEvent(new CustomEvent('sl-after-hide'));
    await el.updateComplete;
    expect(
      fetchStub.getCalls().some((call) => call.args[1]?.method === 'DELETE')
    ).to.equal(false);
  });
  it('waits for the initial list before allowing creation', async () => {
    let finish!: (response: Response) => void;
    fetchStub.callsFake(async (input) =>
      String(input).endsWith('/auth/users/me')
        ? json({ permissions })
        : new Promise<Response>((resolve) => {
            finish = resolve;
          })
    );
    const el = await fixture<AgentCredentialsCard>(
      html`<agent-credentials-card agentId="agent-1"></agent-credentials-card>`
    );
    await waitUntil(() => !!finish);
    expect(await el.openCreate()).to.equal(null);
    expect(
      (el.shadowRoot!.querySelector('[data-create-open]') as any).disabled
    ).to.equal(true);
    finish(json([credential]));
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Gateway key'));
    expect(
      (el.shadowRoot!.querySelector('[data-create-open]') as any).disabled
    ).to.equal(false);
  });
  it('clears pending revocation when navigating to another agent', async () => {
    const el = await mount();
    let finish!: (response: Response) => void;
    fetchStub.callsFake(async (_input, init) =>
      init?.method === 'DELETE'
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : json([credential])
    );
    (el.shadowRoot!.querySelector('[data-revoke]') as HTMLElement).click();
    await waitUntil(
      () =>
        !!document
          .querySelector('confirm-dialog')
          ?.shadowRoot?.querySelector('[data-testid="confirm-dialog-confirm"]')
    );
    (
      document
        .querySelector('confirm-dialog')!
        .shadowRoot!.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
    ).click();
    await waitUntil(() => !!finish);
    el.agentId = 'agent-2';
    await el.updateComplete;
    await waitUntil(() => el.shadowRoot?.textContent?.includes('Gateway key'));
    finish(json({ ...credential, status: 'revoked' }));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(
      (el.shadowRoot!.querySelector('[data-revoke]') as any).disabled
    ).to.equal(false);
    expect(el.shadowRoot!.textContent).not.to.contain('Revoked');
  });
  for (const reason of ['inactive', 'permission'] as const) {
    it(`explains and blocks creation when ${reason} changes while the dialog is open`, async () => {
      const el = await mount();
      void el.openCreate();
      await el.updateComplete;
      const name = el.shadowRoot!.querySelector('sl-input') as any;
      name.value = 'New key';
      name.dispatchEvent(new Event('sl-input'));
      if (reason === 'inactive') el.lifecycleState = 'suspended';
      else (el as any).canManage = false;
      await el.updateComplete;
      const submit = el.shadowRoot!.querySelector(
        '[data-create-submit]'
      ) as any;
      expect(submit.disabled).to.equal(true);
      expect(
        el.shadowRoot!.querySelector('sl-dialog sl-alert')!.textContent
      ).to.contain(reason === 'inactive' ? 'Only active agents' : 'permission');
      submit.click();
      expect(
        fetchStub.getCalls().some((call) => call.args[1]?.method === 'POST')
      ).to.equal(false);
      if (reason === 'inactive') el.lifecycleState = 'active';
      else (el as any).canManage = true;
      await el.updateComplete;
      expect(submit.disabled).to.equal(false);
      expect(el.shadowRoot!.querySelector('sl-dialog sl-alert')).to.equal(null);
    });
  }

  it('labels a list refresh after a failed revocation as Reload', async () => {
    const el = await mount();
    fetchStub.callsFake(async (_input, init) =>
      init?.method === 'DELETE'
        ? json({ detail: 'Could not revoke this key' }, 500)
        : json([credential])
    );
    (el.shadowRoot!.querySelector('[data-revoke]') as HTMLElement).click();
    await waitUntil(
      () =>
        !!document
          .querySelector('confirm-dialog')
          ?.shadowRoot?.querySelector('[data-testid="confirm-dialog-confirm"]')
    );
    (
      document
        .querySelector('confirm-dialog')!
        .shadowRoot!.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
    ).click();
    await waitUntil(() =>
      el.shadowRoot?.textContent?.includes('Could not revoke this key')
    );
    const refresh = el.shadowRoot!.querySelector(
      'sl-alert sl-button'
    ) as HTMLElement;
    expect(refresh.textContent).to.equal('Reload');
    refresh.click();
    await waitUntil(
      () => !el.shadowRoot?.textContent?.includes('Could not revoke this key')
    );
    expect(
      fetchStub.getCalls().filter((call) => call.args[1]?.method === 'DELETE')
    ).to.have.length(1);
  });
});
