import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import {
  ssoApi,
  type SsoConnection,
  type SsoPolicy,
} from '../services/sso-api';
import { resetConfirmDialogForTests } from './confirm-dialog';
import { answerConfirmDialog } from '../utils/test-confirm-dialog';
import './sso-settings';
import type { SsoSettings } from './sso-settings';

const connection = (extra: Partial<SsoConnection> = {}): SsoConnection => ({
  id: 'connection-a',
  account_id: 'account-a',
  protocol: 'oidc',
  name: 'Example IdP',
  enabled: true,
  oidc_issuer: 'https://idp.example.com',
  oidc_client_id: 'client-a',
  oidc_client_secret_set: true,
  oidc_client_secret_last4: '9f3a',
  oidc_scopes: ['openid', 'email', 'profile'],
  groups_claim: null,
  saml_idp_entity_id: null,
  saml_idp_sso_url: null,
  saml_idp_metadata_url: null,
  saml_idp_certs: [],
  saml_sp_signing_key_set: false,
  saml_want_assertions_signed: true,
  default_role: 'member',
  jit_enabled: true,
  attribute_mapping: { email: 'email' },
  test_sign_in_method: 'POST',
  current_admin_test_valid: false,
  ...extra,
});
const policy = (extra: Partial<SsoPolicy> = {}): SsoPolicy => ({
  enforce: false,
  break_glass_owner_ids: null,
  session_max_age_seconds: null,
  ...extra,
});
const domain = {
  id: 'domain-a',
  domain: 'example.com',
  verified: false,
  verified_at: null,
  txt_record_name: 'example.com',
  txt_record_value: 'preloop-verification=synthetic-token',
};
const token = {
  id: 'token-a',
  name: 'Example SCIM',
  prefix: 'scim_example',
  connection_id: 'connection-a' as string | null,
  created_at: '2026-10-28T12:00:00Z',
  revoked_at: null,
};

describe('SSO account settings', () => {
  let element: SsoSettings;
  let enabled: boolean;
  let permissions: string[];
  let requests: Record<keyof typeof ssoApi, sinon.SinonStub>;
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    enabled = true;
    permissions = ['manage_account'];
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'synthetic-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const path = String(input);
      const body = path.includes('/features')
        ? { features: { enterprise_sso: enabled }, plugins: [] }
        : path.includes('/roles')
          ? { roles: [{ id: 'viewer', name: 'viewer' }] }
          : path.includes('/teams')
            ? { teams: [{ id: 'team-a', name: 'Engineering' }] }
            : { id: 'user-a', account_id: 'account-a', permissions };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    requests = Object.fromEntries(
      Object.keys(ssoApi).map((key) => [
        key,
        sinon.stub(ssoApi, key as keyof typeof ssoApi),
      ])
    ) as typeof requests;
    requests.connections.resolves([connection()]);
    requests.domains.resolves([domain]);
    requests.policy.resolves(policy());
    requests.tokens.resolves([token]);
    requests.mappings.resolves([]);
    requests.saveConnection.resolves(connection());
    requests.addDomain.resolves(domain);
    requests.verifyDomain.resolves({ ...domain, verified: true });
    requests.createToken.resolves({
      ...token,
      token: 'scim_one_time_synthetic',
    });
    requests.savePolicy.resolves(policy({ enforce: true }));
  });

  afterEach(() => {
    sinon.restore();
    invalidateApiCaches();
    resetConfirmDialogForTests();
    localStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  async function mount() {
    element = await fixture<SsoSettings>(html`<sso-settings></sso-settings>`);
    await waitUntil(
      () =>
        !element.shadowRoot!.textContent!.includes(
          'Loading single sign-on settings'
        )
    );
    await element.updateComplete;
    return element.shadowRoot!;
  }
  function click(text: string) {
    const button = [
      ...element.shadowRoot!.querySelectorAll<HTMLElement>('sl-button'),
    ].find((button) => button.textContent!.trim() === text);
    expect(button, text).to.exist;
    button!.click();
  }
  function fill(name: string, value: string) {
    const input = element.shadowRoot!.querySelector<HTMLInputElement>(
      `[name="${name}"]`
    )!;
    input.value = value;
  }
  function submit(id: string) {
    element
      .shadowRoot!.querySelector(id)!
      .dispatchEvent(
        new SubmitEvent('submit', { bubbles: true, cancelable: true })
      );
  }
  async function editor(protocol = 'OIDC') {
    click(`Add ${protocol} connection`);
    await element.updateComplete;
    fill('name', 'Example provider');
    if (protocol === 'OIDC') {
      fill('oidc_issuer', 'https://idp.example.com');
      fill('oidc_client_id', 'client-a');
    } else {
      fill('saml_idp_entity_id', 'https://idp.example.com/entity');
      fill('saml_idp_sso_url', 'https://idp.example.com/sso');
    }
  }

  it('gives copy controls an accessible button name', async () => {
    await mount();
    await expect(element).to.be.accessible();
  });

  it('offers the existing upgrade action without calling admin APIs when the feature is off', async () => {
    enabled = false;
    const listener = sinon.spy();
    window.addEventListener('show-upgrade-modal', listener);
    try {
      const root = await mount();
      expect(root.textContent).to.contain('Preloop Enterprise');
      expect(requests.connections).not.to.have.been.called;
      expect(fetchStub.callCount).to.equal(1);
      expect(listener).not.to.have.been.called;
      click('See plans');
      expect(listener).to.have.been.calledOnce;
      expect(listener.firstCall.args[0].detail.feature).to.equal(
        'enterprise_sso'
      );
    } finally {
      window.removeEventListener('show-upgrade-modal', listener);
    }
  });

  it('denies member controls without making admin API requests', async () => {
    permissions = [];
    const root = await mount();
    expect(root.textContent).to.contain('Only account owners and admins');
    expect(root.querySelector('form')).to.be.null;
    expect(requests.connections).not.to.have.been.called;
  });

  it('keeps submitted OIDC secrets out of the form and subsequent reads', async () => {
    await mount();
    await editor();
    fill('oidc_client_secret', 'synthetic-client-secret');
    submit('#connection-form');
    await waitUntil(() => requests.saveConnection.calledOnce);
    expect(requests.saveConnection.firstCall.args).to.deep.equal([
      'account-a',
      null,
      {
        protocol: 'oidc',
        name: 'Example provider',
        enabled: false,
        default_role: 'member',
        jit_enabled: true,
        groups_claim: null,
        attribute_mapping: {},
        oidc_issuer: 'https://idp.example.com',
        oidc_client_id: 'client-a',
        oidc_scopes: ['openid', 'email', 'profile'],
        oidc_client_secret: 'synthetic-client-secret',
      },
    ]);
    await waitUntil(
      () => !element.shadowRoot!.querySelector('#connection-form')
    );
    expect(element.shadowRoot!.innerHTML).not.to.contain(
      'synthetic-client-secret'
    );
    await waitUntil(
      () =>
        element
          .shadowRoot!.querySelector('[aria-busy]')
          ?.getAttribute('aria-busy') === 'false'
    );
    click('Edit connection');
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector<HTMLInputElement>(
        '[name=oidc_client_secret]'
      )!.value
    ).to.equal('');
  });

  it('clears a submitted secret even if the server rejects the update', async () => {
    requests.saveConnection.rejects(new Error('Configuration rejected.'));
    await mount();
    await editor();
    fill('oidc_client_secret', 'synthetic-rejected-secret');
    submit('#connection-form');
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('Configuration rejected.')
    );
    expect(
      element.shadowRoot!.querySelector<HTMLInputElement>(
        '[name=oidc_client_secret]'
      )!.value
    ).to.equal('');
    expect(element.shadowRoot!.innerHTML).not.to.contain(
      'synthetic-rejected-secret'
    );
  });

  it('saves SAML certificates and a write-only SP signing key', async () => {
    await mount();
    await editor('SAML');
    fill(
      'saml_idp_certs',
      '-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----'
    );
    fill('saml_sp_signing_key', 'synthetic-private-key');
    submit('#connection-form');
    await waitUntil(() => requests.saveConnection.calledOnce);
    expect(requests.saveConnection.firstCall.args[2]).to.include({
      protocol: 'saml',
      saml_sp_signing_key: 'synthetic-private-key',
      saml_want_assertions_signed: true,
    });
    expect(
      requests.saveConnection.firstCall.args[2].saml_idp_certs
    ).to.have.length(1);
    expect(element.shadowRoot!.innerHTML).not.to.contain(
      'synthetic-private-key'
    );
  });

  it('preserves a saved secret when editing without a replacement', async () => {
    await mount();
    click('Edit connection');
    await element.updateComplete;
    submit('#connection-form');
    await waitUntil(() => requests.saveConnection.calledOnce);
    expect(requests.saveConnection.firstCall.args[1]).to.equal('connection-a');
    expect(requests.saveConnection.firstCall.args[2]).not.to.have.property(
      'oidc_client_secret'
    );
    expect(requests.saveConnection.firstCall.args[2]).not.to.have.property(
      'protocol'
    );
  });

  it('shows actual DNS instructions and refreshes verification from the server', async () => {
    const root = await mount();
    expect(root.textContent).to.contain(domain.txt_record_value);
    requests.domains.resolves([{ ...domain, verified: true }]);
    click('Verify DNS');
    await waitUntil(
      () => !element.shadowRoot!.textContent!.includes(domain.txt_record_value)
    );
    expect(requests.verifyDomain).to.have.been.calledWith(
      'account-a',
      'domain-a'
    );
    expect(element.shadowRoot!.textContent).not.to.contain(
      domain.txt_record_value
    );
  });

  it('adds domain claims', async () => {
    await mount();
    fill('domain', 'example.org');
    submit('#domain-form');
    await waitUntil(() => requests.addDomain.calledOnce);
    expect(requests.addDomain).to.have.been.calledWith(
      'account-a',
      'example.org'
    );
  });

  it('creates mappings scoped to the selected connection and rejects owner grants', async () => {
    await mount();
    const select = element.shadowRoot!.querySelector('select')!;
    select.value = 'connection-a';
    select.dispatchEvent(new Event('change'));
    await waitUntil(() => !!element.shadowRoot!.querySelector('#mapping-form'));
    fill('idp_group', 'engineers');
    fill('role', 'owner');
    submit('#mapping-form');
    await element.updateComplete;
    expect(requests.addMapping).not.to.have.been.called;
    fill('role', 'viewer');
    submit('#mapping-form');
    await waitUntil(() => requests.addMapping.calledOnce);
    expect(requests.addMapping).to.have.been.calledWith(
      'account-a',
      'connection-a',
      { idp_group: 'engineers', team_id: null, role: 'viewer' }
    );
  });

  it('discloses a newly created SCIM token once and clears it on dismissal', async () => {
    await mount();
    expect(element.shadowRoot!.textContent).not.to.contain(
      'scim_one_time_synthetic'
    );
    fill('name', 'Directory sync');
    submit('#token-form');
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('scim_one_time_synthetic')
    );
    expect(requests.createToken).to.have.been.calledWith(
      'account-a',
      'Directory sync',
      'connection-a'
    );
    click('I have saved the token');
    await element.updateComplete;
    expect(element.shadowRoot!.innerHTML).not.to.contain(
      'scim_one_time_synthetic'
    );
  });

  describe('SCIM token connection', () => {
    const tokenSelect = () =>
      element.shadowRoot!.querySelector<HTMLSelectElement>(
        '#token-form select[name="connection_id"]'
      )!;
    const text = () => element.shadowRoot!.textContent!.replace(/\s+/g, ' ');

    it('preselects the only connection and binds the token to it', async () => {
      await mount();
      expect(tokenSelect().value).to.equal('connection-a');
      expect(tokenSelect().required).to.equal(true);
      fill('name', 'Directory sync');
      submit('#token-form');
      await waitUntil(() => requests.createToken.calledOnce);
      expect(requests.createToken).to.have.been.calledWith(
        'account-a',
        'Directory sync',
        'connection-a'
      );
    });

    it('requires a choice when more than one connection exists', async () => {
      requests.connections.resolves([
        connection(),
        connection({
          id: 'connection-b',
          name: 'Second IdP',
          protocol: 'saml',
        }),
      ]);
      await mount();
      const select = tokenSelect();
      expect(select.value).to.equal('');
      expect(select.required).to.equal(true);
      fill('name', 'Directory sync');
      submit('#token-form');
      await element.updateComplete;
      expect(requests.createToken).not.to.have.been.called;
      select.value = 'connection-b';
      select.dispatchEvent(new Event('change'));
      submit('#token-form');
      await waitUntil(() => requests.createToken.calledOnce);
      expect(requests.createToken).to.have.been.calledWith(
        'account-a',
        'Directory sync',
        'connection-b'
      );
    });

    it('marks disabled connections in the selector', async () => {
      requests.connections.resolves([
        connection(),
        connection({ id: 'connection-b', name: 'Paused IdP', enabled: false }),
      ]);
      await mount();
      const labels = [...tokenSelect().options].map((o) =>
        o.textContent!.trim()
      );
      expect(labels).to.include('Paused IdP (OIDC, disabled)');
      expect(labels).to.include('Example IdP (OIDC)');
    });

    it('explains that a connection is needed before a token can be created', async () => {
      requests.connections.resolves([]);
      await mount();
      expect(text()).to.contain(
        'Add an OIDC or SAML connection before creating a SCIM token'
      );
      const submitButton = element.shadowRoot!.querySelector<HTMLElement>(
        '#token-form sl-button[type="submit"]'
      )!;
      expect(submitButton.hasAttribute('disabled')).to.equal(true);
      fill('name', 'Directory sync');
      submit('#token-form');
      await element.updateComplete;
      expect(requests.createToken).not.to.have.been.called;
    });

    it('shows each token binding and the bound connection of a new token', async () => {
      requests.tokens.resolves([
        token,
        { ...token, id: 'token-gone', name: 'Old', connection_id: 'deleted' },
      ]);
      await mount();
      expect(text()).to.contain('Example SCIM · scim_example… · Example IdP');
      expect(text()).to.contain('Old · scim_example… · Unavailable connection');
      fill('name', 'Directory sync');
      submit('#token-form');
      await waitUntil(() => text().includes('scim_one_time_synthetic'));
      expect(text()).to.contain(
        'This token provisions members for Example IdP.'
      );
    });

    it('explains that unbound legacy tokens must be replaced', async () => {
      requests.tokens.resolves([
        { ...token, id: 'legacy', name: 'Legacy', connection_id: null },
        {
          ...token,
          id: 'legacy-revoked',
          name: 'Revoked legacy',
          connection_id: null,
          revoked_at: '2026-10-29T12:00:00Z',
        },
      ]);
      await mount();
      expect(text()).to.contain('Legacy · scim_example… · Not bound');
      const chips = [
        ...element.shadowRoot!.querySelectorAll('sl-badge'),
      ].filter((chip) => chip.textContent!.trim() === 'Replace');
      expect(chips).to.have.length(1);
      expect(chips[0].getAttribute('variant')).to.equal('warning');
      expect(text()).to.contain(
        'Tokens that are not bound to a connection cannot provision. Create a replacement bound to a connection, update your identity provider, then revoke the old token.'
      );
    });

    it('omits the legacy explanation when every active token is bound', async () => {
      await mount();
      expect(text()).not.to.contain('not bound to a connection cannot');
    });

    it('labels the selector and keeps it keyboard reachable', async () => {
      requests.connections.resolves([
        connection(),
        connection({ id: 'connection-b', name: 'Second IdP' }),
      ]);
      await mount();
      const select = tokenSelect();
      expect(select.labels![0].textContent!.trim()).to.match(/^Connection/);
      expect(select.tabIndex).to.equal(0);
      select.focus();
      expect(element.shadowRoot!.activeElement).to.equal(select);
      const describedBy = select.getAttribute('aria-describedby')!;
      expect(
        element.shadowRoot!.getElementById(describedBy)!.textContent
      ).to.contain('Each token provisions members for one connection');
      await expect(element).to.be.accessible();
    });
  });

  it('keeps a one-time token visible when revocation is cancelled', async () => {
    await mount();
    fill('name', 'Directory sync');
    submit('#token-form');
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('scim_one_time_synthetic')
    );
    click('Revoke');
    await answerConfirmDialog(false);
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.contain(
      'scim_one_time_synthetic'
    );
    expect(requests.revokeToken).not.to.have.been.called;
  });

  it('preserves an unrelated one-time token after an existing token is revoked', async () => {
    requests.createToken.resolves({
      ...token,
      id: 'new-token',
      token: 'scim_one_time_synthetic',
    });
    await mount();
    fill('name', 'Directory sync');
    submit('#token-form');
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('scim_one_time_synthetic')
    );
    click('Revoke');
    await answerConfirmDialog(true);
    await waitUntil(() => requests.revokeToken.calledOnce);
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.contain(
      'scim_one_time_synthetic'
    );
  });

  for (const role of ['Owner', ' OWNER ']) {
    it(`rejects the reserved default role ${role} before requesting a save`, async () => {
      await mount();
      await editor();
      fill('default_role', role);
      submit('#connection-form');
      await element.updateComplete;
      expect(element.shadowRoot!.textContent).to.contain(
        'cannot grant the owner role'
      );
      expect(requests.saveConnection).not.to.have.been.called;
    });
  }

  it('hides a SAML signing key by default and supports explicit temporary disclosure', async () => {
    await mount();
    await editor('SAML');
    const input = element.shadowRoot!.querySelector<HTMLTextAreaElement>(
      '[name=saml_sp_signing_key]'
    )!;
    input.value = 'synthetic-private-key';
    expect(
      getComputedStyle(input).getPropertyValue('-webkit-text-security')
    ).to.equal('disc');
    click('Show signing key');
    await element.updateComplete;
    expect(
      getComputedStyle(input).getPropertyValue('-webkit-text-security')
    ).to.equal('none');
    click('Hide signing key');
    await element.updateComplete;
    expect(
      getComputedStyle(input).getPropertyValue('-webkit-text-security')
    ).to.equal('disc');
    submit('#connection-form');
    await waitUntil(() => requests.saveConnection.calledOnce);
    expect(input.value).to.equal('');
  });

  for (const owners of [[], ['legacy-owner']]) {
    it(`keeps the all-owner emergency-access guarantee for informational legacy list ${JSON.stringify(owners)}`, async () => {
      requests.policy.resolves(policy({ break_glass_owner_ids: owners }));
      const root = await mount();
      expect(root.textContent!.replace(/\s+/g, ' ')).to.contain(
        'All account owners retain emergency password or passkey sign-in'
      );
    });
  }

  it('clears the displayed token only after that token is successfully revoked', async () => {
    await mount();
    fill('name', 'Directory sync');
    submit('#token-form');
    await waitUntil(() =>
      element.shadowRoot!.textContent!.includes('scim_one_time_synthetic')
    );
    click('Revoke');
    await answerConfirmDialog(true);
    await waitUntil(
      () =>
        !element.shadowRoot!.textContent!.includes('scim_one_time_synthetic')
    );
    expect(requests.revokeToken).to.have.been.calledWith(
      'account-a',
      'token-a'
    );
  });

  it('confirms token revocation and refreshes tokens', async () => {
    await mount();
    click('Revoke');
    await answerConfirmDialog(true);
    await waitUntil(() => requests.revokeToken.calledOnce);
    expect(requests.revokeToken).to.have.been.calledWith(
      'account-a',
      'token-a'
    );
  });

  it('explains unavailable enforcement and test sign-in without calling missing endpoints', async () => {
    const root = await mount();
    expect(
      root.querySelector('#enforcement-toggle')!.hasAttribute('disabled')
    ).to.equal(true);
    expect(root.textContent).to.contain('complete a successful test sign-in');
    expect(root.textContent!.replace(/\s+/g, ' ')).to.contain(
      'successfully tested by you within the last 24 hours'
    );
    expect(root.textContent).to.contain('Test sign-in is unavailable');
    expect(root.querySelector('sl-button[href]')).to.be.null;
  });

  it('requires explicit server readiness, verified domain and enabled connection before enabling', async () => {
    requests.connections.resolves([
      connection({ current_admin_test_valid: true }),
    ]);
    const root = await mount();
    expect(
      root.querySelector('#enforcement-toggle')!.hasAttribute('disabled')
    ).to.equal(true);
    expect(requests.savePolicy).not.to.have.been.called;
  });

  it('requires confirmation and preserves policy recovery settings on enforcement changes', async () => {
    requests.domains.resolves([{ ...domain, verified: true }]);
    requests.connections.resolves([
      connection({ current_admin_test_valid: true }),
    ]);
    requests.policy.resolves(
      policy({
        break_glass_owner_ids: ['owner-a'],
        session_max_age_seconds: 3600,
      })
    );
    await mount();
    click('Require single sign-on');
    const text = await answerConfirmDialog(true);
    expect(text).to.contain('All account owners retain emergency');
    await waitUntil(() => requests.savePolicy.calledOnce);
    expect(requests.savePolicy).to.have.been.calledWith('account-a', {
      enforce: true,
      break_glass_owner_ids: ['owner-a'],
      session_max_age_seconds: 3600,
    });
  });

  it('allows existing enforcement to be turned off even without readiness fields', async () => {
    requests.policy.resolves(policy({ enforce: true }));
    await mount();
    click('Turn off enforcement');
    await waitUntil(() => requests.savePolicy.calledOnce);
    expect(requests.savePolicy.firstCall.args[1].enforce).to.equal(false);
  });

  it('starts administrator proof using POST and never links to its admin URL', async () => {
    requests.connections.resolves([
      connection({
        test_sign_in_url:
          '/api/v1/accounts/account-a/sso/connections/connection-a/test-sign-in',
      }),
    ]);
    requests.testSignIn.rejects(
      new Error('Test sign-in could not be started.')
    );
    const root = await mount();
    expect(root.querySelector('sl-button[href]')).to.be.null;
    click('Test sign-in');
    await waitUntil(() => requests.testSignIn.calledOnce);
    expect(requests.testSignIn).to.have.been.calledWith(
      'account-a',
      'connection-a'
    );
  });

  it('shows SAML metadata and public SP certificate from the published fields', async () => {
    requests.connections.resolves([
      connection({
        protocol: 'saml',
        saml_metadata_url:
          'https://console.example.com/api/v1/auth/sso/a/metadata',
        saml_sp_certificate: 'PUBLIC CERTIFICATE',
      }),
    ]);
    const root = await mount();
    expect(root.textContent).to.contain('/api/v1/auth/sso/a/metadata');
    expect(root.textContent).to.contain('PUBLIC CERTIFICATE');
  });

  it('previews imported SAML metadata before applying it to an unsaved editor', async () => {
    requests.importSamlMetadata.resolves({
      saml_idp_entity_id: 'https://idp.example.com/imported',
      saml_idp_sso_url: 'https://idp.example.com/sso',
      saml_idp_certs: ['PUBLIC IDP CERTIFICATE'],
    });
    await mount();
    await editor('SAML');
    fill('metadata_url', 'https://idp.example.com/metadata');
    click('Preview metadata');
    await waitUntil(() => requests.importSamlMetadata.calledOnce);
    await element.updateComplete;
    expect(requests.saveConnection).not.to.have.been.called;
    expect(element.shadowRoot!.textContent).to.contain(
      'PUBLIC IDP CERTIFICATE'
    );
    click('Apply metadata to editor');
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelector<HTMLInputElement>(
        '[name=saml_idp_entity_id]'
      )!.value
    ).to.equal('https://idp.example.com/imported');
  });

  it('does not disclose a late-created token after the component is removed', async () => {
    let resolve!: (value: unknown) => void;
    requests.createToken.returns(new Promise((done) => (resolve = done)));
    await mount();
    fill('name', 'Directory sync');
    submit('#token-form');
    await waitUntil(() => requests.createToken.calledOnce);
    element.remove();
    resolve({ ...token, token: 'scim_late_synthetic' });
    await new Promise((done) => setTimeout(done, 0));
    expect(element.shadowRoot!.innerHTML).not.to.contain('scim_late_synthetic');
  });
});
