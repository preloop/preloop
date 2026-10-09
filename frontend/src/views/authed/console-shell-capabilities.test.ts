import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './console-shell';
import type { ConsoleShell } from './console-shell';
import { mockApi, type MockApi } from '../../test-helpers/capability-api';
import { CI_ACTIONS } from '../../ci-administration-api';
import type { Capability } from '../../capabilities';

const GATED_LINKS = ['/console/settings/access-grants'];

describe('ConsoleShell capability gating', () => {
  let api: MockApi | undefined;
  let matchMediaStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = {
      name: 'Preloop',
      domain: 'preloop.ai',
      company: { legal_name: 'Preloop', address: '', city: '' },
      branding: {
        logo_light: '/logo.svg',
        logo_dark: '/logo-dark.svg',
        favicon: '/favicon.ico',
        primary_color: '#000',
        gradient_product: '',
        gradient_ai: '',
      },
      social: { twitter: '', linkedin: '', instagram: '' },
    };
    localStorage.setItem('accessToken', 'test-access-token');
    matchMediaStub = sinon.stub(window, 'matchMedia').callsFake(
      () =>
        ({
          matches: false,
          addEventListener: () => {},
          removeEventListener: () => {},
        }) as unknown as MediaQueryList
    );
  });

  afterEach(() => {
    api?.restore();
    api = undefined;
    matchMediaStub.restore();
    localStorage.clear();
    delete (window as any).BRAND_CONFIG;
  });

  async function renderShell(capabilities: Capability[]) {
    api = mockApi({
      capabilities,
      fallback: { status: 200, body: [] },
    });
    const el = (await fixture(
      html`<console-shell></console-shell>`
    )) as ConsoleShell;
    await waitUntil(
      () => el.shadowRoot?.querySelector('console-header') !== null,
      'Header did not render'
    );
    await waitUntil(
      () => api!.callsTo('/api/v1/features').length > 0,
      'Features were not read'
    );
    await el.updateComplete;
    return el;
  }

  it('renders no gated nav item or header slot without capabilities', async () => {
    const el = await renderShell([]);
    const root = el.shadowRoot!;
    for (const href of GATED_LINKS) {
      expect(root.querySelector(`a[href="${href}"]`), href).to.equal(null);
    }
    expect(root.querySelector('capability-extension')).to.equal(null);
    expect(customElements.get('account-switcher')).to.equal(undefined);
    expect(api!.callsTo('/api/v1/me/memberships')).to.have.length(0);
  });

  it('shows the Settings items with account_hierarchy', async () => {
    const el = await renderShell(['account_hierarchy']);
    await waitUntil(
      () =>
        el.shadowRoot!.querySelector(`a[href="${GATED_LINKS[0]}"]`) !== null,
      'Access grants link did not render'
    );
    for (const href of GATED_LINKS) {
      expect(el.shadowRoot!.querySelector(`a[href="${href}"]`), href).to.exist;
    }
    // Subaccounts live on the Account page; the route stays for deep links
    // but nothing in the nav points at it.
    expect(
      el.shadowRoot!.querySelector('a[href="/console/settings/subaccounts"]')
    ).to.equal(null);
    expect(
      el.shadowRoot!.querySelector(
        'capability-extension[name="account-switcher"]'
      )
    ).to.equal(null);
  });

  it('mounts the header account switcher with multi_account', async () => {
    const el = await renderShell(['multi_account']);
    await waitUntil(
      () =>
        el.shadowRoot!.querySelector(
          'capability-extension[name="account-switcher"]'
        ) !== null,
      'Switcher slot did not render'
    );
    const slot = el.shadowRoot!.querySelector(
      'capability-extension[name="account-switcher"]'
    )!;
    expect(slot.getAttribute('slot')).to.equal('account-switcher');
    expect(
      el.shadowRoot!.querySelector(`a[href="${GATED_LINKS[0]}"]`)
    ).to.equal(null);
  });

  for (const allowed of [true, false]) {
    it(`shows restricted CI navigation only with complete capability and view permission: ${allowed}`, async () => {
      api = mockApi({
        routes: [
          {
            path: '/api/v1/ci-identities/capabilities',
            body: {
              available: true,
              can_view: allowed,
              can_manage: false,
              supported_actions: [...CI_ACTIONS],
            },
          },
        ],
        fallback: { status: 200, body: [] },
      });
      const element = await fixture<ConsoleShell>(
        html`<console-shell></console-shell>`
      );
      await waitUntil(
        () => api!.callsTo('/api/v1/ci-identities/capabilities').length > 0
      );
      await waitUntil(
        () => element.shadowRoot?.querySelector('console-header') !== null
      );
      if (allowed)
        await waitUntil(
          () =>
            !!element.shadowRoot?.querySelector(
              'a[href="/console/settings/ci-identities"]'
            )
        );
      await element.updateComplete;
      expect(
        !!element.shadowRoot?.querySelector(
          'a[href="/console/settings/ci-identities"]'
        )
      ).to.equal(allowed);
    });
  }

  it('hides restricted CI navigation when the action rollout is incomplete', async () => {
    api = mockApi({
      routes: [
        {
          path: '/api/v1/ci-identities/capabilities',
          body: {
            available: true,
            can_view: true,
            can_manage: true,
            supported_actions: CI_ACTIONS.slice(1),
          },
        },
      ],
      fallback: { status: 200, body: [] },
    });
    const element = await fixture<ConsoleShell>(
      html`<console-shell></console-shell>`
    );
    await waitUntil(
      () => api!.callsTo('/api/v1/ci-identities/capabilities').length > 0
    );
    await waitUntil(
      () => element.shadowRoot?.querySelector('console-header') !== null
    );
    await element.updateComplete;
    expect(
      element.shadowRoot?.querySelector(
        'a[href="/console/settings/ci-identities"]'
      )
    ).to.equal(null);
  });
});
