import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './chat-connections-view';
import type { ChatConnectionsView } from './chat-connections-view';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
const row = {
  id: 'c-1',
  provider: 'slack',
  workspace_id: 'team-1',
  name: 'Team chat',
  enabled: true,
  linked: false,
  external_user_id: null,
  ingress_url: '/api/v1/chat/ingress/c-1',
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
describe('Chat connections', () => {
  let stub: sinon.SinonStub;
  let manage: boolean;
  let linked: boolean;
  let enabled: boolean;
  let denied: boolean;
  let expiry: string;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    manage = false;
    linked = false;
    enabled = true;
    denied = false;
    expiry = new Date(Date.now() + 60000).toISOString();
    stub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        const method = init?.method ?? 'GET';
        const connection = {
          ...row,
          linked,
          enabled,
          external_user_id: linked ? 'user-1' : null,
        };
        if (path.endsWith('/link-code'))
          return json(
            denied
              ? { detail: 'Link denied' }
              : {
                  code: 'private-code',
                  expires_at: expiry,
                  instruction: 'Link privately',
                },
            denied ? 403 : 200
          );
        if (path.endsWith('/identity')) {
          linked = false;
          return new Response(null, { status: 204 });
        }
        if (path.endsWith('/deliveries'))
          return json({
            deliveries: [
              {
                id: 'd-1',
                status: 'failed',
                created_at: new Date().toISOString(),
                last_error: 'Provider unavailable',
              },
            ],
          });
        if (method === 'PATCH') {
          enabled = JSON.parse(init!.body as string).enabled;
          return json({ ...connection, enabled });
        }
        if (method === 'POST') return json(connection);
        return json({ connections: [connection], can_manage: manage });
      });
  });
  afterEach(() => {
    sinon.restore();
    localStorage.removeItem('accessToken');
  });
  async function view() {
    const el = await fixture<ChatConnectionsView>(
      html`<chat-connections-view></chat-connections-view>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('article'));
    await new Promise((resolve) => setTimeout(resolve, 30));
    return el;
  }
  async function click(el: ChatConnectionsView, action: string) {
    (
      el.shadowRoot!.querySelector(`[data-action="${action}"]`) as HTMLElement
    ).click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    await el.updateComplete;
  }
  it('discards old identity responses after disconnect and reconnect', async () => {
    let finish!: (response: Response) => void;
    stub.onFirstCall().returns(
      new Promise<Response>((resolve) => {
        finish = resolve;
      })
    );
    const el = await fixture<ChatConnectionsView>(
      html`<chat-connections-view></chat-connections-view>`
    );
    el.remove();
    document.body.append(el);
    await waitUntil(() => !!el.shadowRoot!.querySelector('article'));
    finish(
      json({
        connections: [{ ...row, name: 'Old account', linked: true }],
        can_manage: true,
      })
    );
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(el.shadowRoot!.textContent).not.to.include('Old account');
    expect(el.shadowRoot!.querySelector('[data-action="create"]')).to.equal(
      null
    );
  });

  it('lets members link privately without management controls', async () => {
    const el = await view();
    for (const action of ['create', 'toggle'])
      expect(
        el.shadowRoot!.querySelector(`[data-action="${action}"]`)
      ).to.equal(null);
    expect(el.shadowRoot!.querySelector('.endpoint')).to.equal(null);
    await click(el, 'link');
    expect(el.shadowRoot!.textContent).to.include('/link private-code');
    expect(
      stub
        .getCalls()
        .find((call) => String(call.args[0]).endsWith('/link-code'))!.args[1]
        .cache
    ).to.equal('no-store');
  });
  it('reports denied and expired linking without exposing proof', async () => {
    const el = await view();
    denied = true;
    await click(el, 'link');
    expect(el.shadowRoot!.textContent).to.include('Link denied');
    denied = false;
    expiry = new Date(Date.now() - 1000).toISOString();
    await click(el, 'link');
    expect(el.shadowRoot!.textContent).to.include('expired');
    expect(el.shadowRoot!.querySelector('.link-code')).to.equal(null);
  });
  it('removes proof at expiry', async () => {
    const el = await view();
    expiry = new Date(Date.now() + 300).toISOString();
    await click(el, 'link');
    expect(el.shadowRoot!.querySelector('.link-code')).to.exist;
    await waitUntil(() => !el.shadowRoot!.querySelector('.link-code'));
  });
  it('unlinks the current identity and refreshes', async () => {
    linked = true;
    const el = await view();
    await click(el, 'unlink');
    expect(
      stub
        .getCalls()
        .some(
          (call) =>
            String(call.args[0]).endsWith('/c-1/identity') &&
            call.args[1].method === 'DELETE'
        )
    ).to.equal(true);
    expect(el.shadowRoot!.querySelector('[data-action="link"]')).to.exist;
  });
  it('shows absolute admin endpoint and saves enabled state', async () => {
    manage = true;
    const el = await view();
    expect(
      el.shadowRoot!.querySelector('.endpoint code')!.textContent
    ).to.equal(`${window.location.origin}${row.ingress_url}`);
    await click(el, 'toggle');
    expect(enabled).to.equal(false);
    expect(
      el
        .shadowRoot!.querySelector('[data-action="link"]')!
        .hasAttribute('disabled')
    ).to.equal(true);
  });
  it('shows delivery failure', async () => {
    const el = await view();
    await click(el, 'deliveries');
    expect(el.shadowRoot!.textContent).to.include('failed');
    expect(el.shadowRoot!.textContent).to.include('Provider unavailable');
  });
  async function fill(el: ChatConnectionsView) {
    for (const input of el.shadowRoot!.querySelectorAll<SlInput>('sl-input')) {
      input.value = 'secret-value';
      input.dispatchEvent(new Event('sl-input', { bubbles: true }));
    }
    await el.updateComplete;
  }
  const inputsEmpty = (el: ChatConnectionsView) =>
    [...el.shadowRoot!.querySelectorAll<SlInput>('sl-input')].every(
      (input) => input.value === ''
    );
  it('validates creation and clears credentials on success', async () => {
    manage = true;
    const el = await view();
    await click(el, 'create');
    const form = el.shadowRoot!.querySelector('form')!;
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await el.updateComplete;
    expect(
      stub.getCalls().some((call) => call.args[1]?.method === 'POST')
    ).to.equal(false);
    await fill(el);
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await waitUntil(() => !el.shadowRoot!.querySelector('form'));
    const post = stub
      .getCalls()
      .find((call) => call.args[1]?.method === 'POST')!;
    expect(JSON.parse(post.args[1].body).bot_token).to.equal('secret-value');
    await click(el, 'create');
    expect(inputsEmpty(el)).to.equal(true);
  });
  it('clears credentials on cancel and disconnect', async () => {
    manage = true;
    const el = await view();
    await click(el, 'create');
    await fill(el);
    [...el.shadowRoot!.querySelectorAll('sl-button')]
      .find((button) => button.textContent!.trim() === 'Cancel')!
      .click();
    await el.updateComplete;
    await click(el, 'create');
    expect(inputsEmpty(el)).to.equal(true);
    await fill(el);
    el.remove();
    document.body.append(el);
    await new Promise((resolve) => setTimeout(resolve, 30));
    await waitUntil(
      () => !!el.shadowRoot!.querySelector('[data-action="create"]')
    );
    await click(el, 'create');
    expect(inputsEmpty(el)).to.equal(true);
  });
});
