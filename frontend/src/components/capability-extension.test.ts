import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './capability-extension';
import type { CapabilityExtension } from './capability-extension';
import type { Capability } from '../capabilities';
import {
  mockApi,
  signInForTest,
  toastCount,
  type MockApi,
} from '../test-helpers/capability-api';

const none = new Set<Capability>();

describe('capability-extension', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('mounts nothing, loads no module and calls no endpoint without the capability', async () => {
    api = mockApi();
    const el = await fixture<CapabilityExtension>(
      html`<capability-extension
        name="access-rules"
        .capabilities=${none}
      ></capability-extension>`
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(el.children).to.have.length(0);
    expect(customElements.get('access-rules-panel')).to.equal(undefined);
    expect(api.calls).to.have.length(0);
  });

  it('reads /features when no set is given and stays empty when all are off', async () => {
    api = mockApi();
    const el = await fixture<CapabilityExtension>(
      html`<capability-extension name="access-rules"></capability-extension>`
    );
    await waitUntil(() => api.callsTo('/api/v1/features').length === 1);
    await new Promise((r) => setTimeout(r, 50));
    expect(el.children).to.have.length(0);
    expect(customElements.get('access-rules-panel')).to.equal(undefined);
    expect(api.callsTo(/^\/api\/v1\/access/)).to.have.length(0);
  });

  it('mounts the extension with its context when the capability is on', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        { path: '/api/v1/accounts/acc-root/usage/rollup', body: { rows: [] } },
        { path: '/api/v1/accounts/acc-root/attention/rollup', body: [] },
      ],
    });
    const el = await fixture<CapabilityExtension>(
      html`<capability-extension
        name="usage-rollup"
        .context=${{ range: '' }}
      ></capability-extension>`
    );
    // The module is fetched lazily; its registration is the browser's real
    // readiness signal, regardless of how many other CI pages are loading.
    await customElements.whenDefined('usage-rollup-panel');
    await waitUntil(
      () => el.querySelector('usage-rollup-panel'),
      'registered usage extension did not mount',
      { timeout: 10000 }
    );
    const panel = el.querySelector('usage-rollup-panel') as HTMLElement & {
      context: Record<string, unknown>;
    };
    expect(panel.context).to.eql({ range: '' });
    // An equal context literal from a re-render is not a change.
    el.context = { range: '' };
    await el.updateComplete;
    expect(el.querySelector('usage-rollup-panel')).to.equal(panel);
  });

  it('removes the extension without a toast when its endpoint is missing', async () => {
    api = mockApi({ capabilities: ['account_hierarchy'] });
    const before = toastCount();
    const el = await fixture<CapabilityExtension>(
      html`<capability-extension
        name="usage-rollup"
        .context=${{}}
      ></capability-extension>`
    );
    await waitUntil(
      () => api.callsTo('/api/v1/accounts/acc-root/usage/rollup').length === 1
    );
    await waitUntil(() => el.children.length === 0, 'extension not removed');
    expect(toastCount()).to.equal(before);
  });
});
