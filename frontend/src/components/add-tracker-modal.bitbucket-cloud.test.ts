/**
 * Managed Bitbucket Cloud onboarding in the tracker modal (issue #1065).
 *
 * Covers the `features.bitbucket_cloud_oauth` gate (flag off hides managed
 * UI, pasted-token onboarding unchanged), the Connect button, completion
 * from an opaque callback handle (actor, workspace/repository selection,
 * capabilities shown as granted/missing/unknown), the reuse of the existing
 * scope preview through the tracker id, managed-edit behaviour (no token or
 * manual expiry input, reconnect/disconnect) and the rule that editing a
 * pasted token never converts it.
 */
import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon, { SinonSandbox, SinonStub } from 'sinon';
import './add-tracker-modal.ts';
import { AddTrackerModal } from './add-tracker-modal';
import * as api from '../api';

interface Stubs {
  getFeatures: SinonStub;
  getTrackerAuthMethods: SinonStub;
  getGitHubInstallations: SinonStub;
  validateTrackerToken: SinonStub;
  listProjectsForOrg: SinonStub;
  addTracker: SinonStub;
  updateTracker: SinonStub;
  startBitbucketConnect: SinonStub;
  completeBitbucketConnect: SinonStub;
  getBitbucketConnectionStatus: SinonStub;
  getBitbucketDiscovery: SinonStub;
  bindBitbucketRepository: SinonStub;
  startBitbucketReconnect: SinonStub;
  completeBitbucketReconnect: SinonStub;
  disconnectBitbucket: SinonStub;
}

const connectedStatus: api.BitbucketConnectionStatus = {
  tracker_id: 'managed-1',
  name: 'Bitbucket Cloud',
  provider: 'bitbucket',
  managed: true,
  state: 'workspace_required',
  consumer_configured: true,
  workspace: null,
  repository: null,
  actor: { uuid: '{u}', display_name: 'Jane Doe', nickname: 'jane' },
  expires_at: '2026-10-04T13:00:00+00:00',
  rotation_version: 1,
  grant_status: 'active',
  granted_scopes: ['pullrequest:write', 'account'],
  capabilities: {
    read_repositories: true,
    push: true,
    write_pull_requests: true,
    register_webhooks: false,
    identify_actor: true,
  },
  capabilities_verified: false,
  reconnect_reason: null,
};

function buildApi(
  sandbox: SinonSandbox,
  features: Record<string, boolean | string[]> = {}
): { stubs: Stubs; api: typeof api } {
  const stubs: Stubs = {
    getFeatures: sandbox.stub().resolves({ plugins: [], features }),
    getTrackerAuthMethods: sandbox
      .stub()
      .resolves({ methods: ['api_token'], github_app_configured: false }),
    getGitHubInstallations: sandbox.stub().resolves([]),
    validateTrackerToken: sandbox.stub().resolves({
      success: true,
      message: 'ok',
      orgs: [{ id: 'ws', name: 'Workspace' }],
    }),
    listProjectsForOrg: sandbox.stub().resolves([]),
    addTracker: sandbox.stub().resolves({ id: 'new-1' }),
    updateTracker: sandbox.stub().resolves({ id: 'managed-1' }),
    startBitbucketConnect: sandbox.stub().resolves({
      authorization_url: 'https://bitbucket.org/site/oauth2/authorize?x=1',
      transaction_id: 't-1',
      expires_at: '2026-10-04T12:10:00+00:00',
    }),
    completeBitbucketConnect: sandbox.stub().resolves(connectedStatus),
    getBitbucketConnectionStatus: sandbox.stub().resolves({
      ...connectedStatus,
      state: 'connected',
      workspace: 'ws',
      repository: 'repo',
    }),
    getBitbucketDiscovery: sandbox
      .stub()
      .callsFake(async (_id: string, workspace?: string) => ({
        actor: connectedStatus.actor,
        workspaces: [
          { slug: 'ws', name: 'Workspace' },
          { slug: 'other', name: 'Other' },
        ],
        repositories: workspace
          ? [{ slug: 'repo', full_name: `${workspace}/repo` }]
          : null,
      })),
    bindBitbucketRepository: sandbox.stub().resolves({
      ...connectedStatus,
      state: 'connected',
      workspace: 'ws',
      repository: 'repo',
    }),
    startBitbucketReconnect: sandbox.stub().resolves({
      authorization_url: 'https://bitbucket.org/site/oauth2/authorize?r=1',
      transaction_id: 't-2',
      expires_at: '2026-10-04T12:10:00+00:00',
      tracker_id: 'managed-1',
    }),
    completeBitbucketReconnect: sandbox.stub().resolves({
      ...connectedStatus,
      state: 'connected',
      workspace: 'ws',
    }),
    disconnectBitbucket: sandbox.stub().resolves(),
  };
  return { stubs, api: { ...api, ...stubs } as typeof api };
}

const managedTracker = {
  id: 'managed-1',
  name: 'Bitbucket Cloud',
  tracker_type: 'bitbucket',
  url: 'https://bitbucket.org',
  auth_type: 'managed_oauth',
  connection_details: {
    workspace: 'ws',
    repository: 'repo',
    managed_oauth: true,
    token_kind: 'access_token',
    token_expires_at: '2020-01-01',
  },
  scope_rules: [
    { rule_type: 'INCLUDE', scope_type: 'ORGANIZATION', identifier: 'ws' },
  ],
};

const pastedTracker = {
  id: 'pasted-1',
  name: 'Bitbucket (pasted)',
  tracker_type: 'bitbucket',
  url: 'https://bitbucket.org',
  auth_type: 'oauth_token',
  connection_details: { workspace: 'ws', token_expires_at: '2026-12-01' },
  scope_rules: [
    { rule_type: 'INCLUDE', scope_type: 'ORGANIZATION', identifier: 'ws' },
  ],
};

describe('AddTrackerModal managed Bitbucket Cloud', () => {
  let element: AddTrackerModal;
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').resolves(new Response(JSON.stringify([])));
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const featureLoaded = (el: AddTrackerModal) =>
    waitUntil(
      () => (el as any).bitbucketDcFeatureLoaded === true,
      'features lookup did not settle'
    );

  const selectType = async (el: AddTrackerModal, value: string) => {
    const typeSelect = el.shadowRoot?.querySelector<any>(
      'sl-select[name="type"]'
    );
    typeSelect!.value = value;
    typeSelect?.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
  };

  describe('feature gate', () => {
    it('hides managed UI and keeps pasted-token onboarding when the flag is off', async () => {
      const { api: stubbedApi } = buildApi(sandbox, {});
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await selectType(element, 'bitbucket');
      const root = element.shadowRoot!;
      expect(root.querySelector('sl-button[name="bitbucket-connect"]')).to.not
        .exist;
      expect(root.querySelector('details.bitbucket-manual-fallback')).to.not
        .exist;
      expect(root.querySelector('sl-input[name="bitbucket_workspace"]')).to
        .exist;
      expect(root.querySelector('sl-select[name="bitbucket_auth_type"]')).to
        .exist;
      expect(root.querySelector('sl-input[name="token_expires_at"]')).to.exist;
      expect(root.querySelector('sl-input[name="api_key"]')).to.exist;
    });

    it('offers Connect Bitbucket with the pasted token as an explicit fallback', async () => {
      const { api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await selectType(element, 'bitbucket');
      const root = element.shadowRoot!;
      const connect = root.querySelector('sl-button[name="bitbucket-connect"]');
      expect(connect).to.exist;
      expect(connect?.textContent).to.contain('Connect Bitbucket');
      const fallback = root.querySelector('details.bitbucket-manual-fallback');
      expect(fallback).to.exist;
      expect(fallback?.querySelector('sl-input[name="bitbucket_workspace"]')).to
        .exist;
      expect(fallback?.querySelector('sl-input[name="api_key"]')).to.exist;
      expect(fallback?.querySelector('sl-input[name="token_expires_at"]')).to
        .exist;
      // Exactly one token input: inside the fallback, not a second required one.
      expect(root.querySelectorAll('sl-input[name="api_key"]').length).to.equal(
        1
      );
    });

    it('does not offer managed connect for other tracker types', async () => {
      const { api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await selectType(element, 'gitlab');
      expect(
        element.shadowRoot?.querySelector('sl-button[name="bitbucket-connect"]')
      ).to.not.exist;
    });
  });

  describe('connect', () => {
    it('starts consent and redirects to the provider URL only', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await selectType(element, 'bitbucket');
      (element as any).trackerName = 'Team Bitbucket';
      const navigate = sandbox.stub(element, '_navigate');
      const button = element.shadowRoot?.querySelector<HTMLElement>(
        'sl-button[name="bitbucket-connect"]'
      );
      button?.click();
      await waitUntil(() => navigate.calledOnce, 'no redirect happened');
      expect(stubs.startBitbucketConnect).to.have.been.calledOnceWith(
        '/console/trackers'
      );
      expect(navigate).to.have.been.calledOnceWith(
        'https://bitbucket.org/site/oauth2/authorize?x=1'
      );
      expect(sessionStorage.getItem('bitbucket_connect_tracker_name')).to.equal(
        'Team Bitbucket'
      );
      // Nothing secret is parked in browser storage.
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i += 1) {
          const key = store.key(i)!;
          expect(store.getItem(key)).to.not.contain('authorize');
        }
      }
    });

    it('refuses Next without a token or a managed connection', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await selectType(element, 'bitbucket');
      await element.testConnection();
      expect((element as any).errorMessage).to.contain('Connect Bitbucket');
      expect(stubs.validateTrackerToken).to.not.have.been.called;
    });
  });

  describe('completion from the callback handle', () => {
    it('exchanges the handle once, shows actor and selection, never a token', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      sessionStorage.setItem(
        'bitbucket_connect_tracker_name',
        'Team Bitbucket'
      );
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .bitbucketConnectHandle=${'opaque-handle'}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      await waitUntil(
        () => stubs.completeBitbucketConnect.calledOnce,
        'completion was not requested'
      );
      await waitUntil(
        () => (element as any).managedDiscovery !== null,
        'discovery did not load'
      );
      await element.updateComplete;
      expect(stubs.completeBitbucketConnect).to.have.been.calledOnceWith({
        handle: 'opaque-handle',
        name: 'Team Bitbucket',
      });
      // The handle is consumed: a re-render cannot replay it.
      expect(element.bitbucketConnectHandle).to.equal(null);
      expect(sessionStorage.getItem('bitbucket_connect_tracker_name')).to.equal(
        null
      );
      const root = element.shadowRoot!;
      const status = root.querySelector('.bitbucket-managed-status');
      expect(status).to.exist;
      expect(status?.getAttribute('data-state')).to.equal('workspace_required');
      expect(root.querySelector('.managed-actor')?.textContent).to.contain(
        'Jane Doe'
      );
      expect(root.querySelector('.managed-expiry')?.textContent).to.contain(
        new Date('2026-10-04T13:00:00+00:00').toLocaleString()
      );
      // Capabilities: granted / missing distinguished; nothing claimed tested.
      expect(
        root
          .querySelector('[data-capability="push"]')
          ?.getAttribute('data-value')
      ).to.equal('granted');
      expect(
        root
          .querySelector('[data-capability="register_webhooks"]')
          ?.getAttribute('data-value')
      ).to.equal('missing');
      expect(
        root
          .querySelector('.managed-capability-note')
          ?.textContent?.replace(/\s+/g, ' ')
      ).to.contain('not tested');
      // No token fields, no manual expiry field, no pasted-token input.
      expect(root.querySelector('sl-input[name="api_key"]')).to.not.exist;
      expect(root.querySelector('sl-input[name="token_expires_at"]')).to.not
        .exist;
      expect(root.querySelector('sl-select[name="bitbucket_auth_type"]')).to.not
        .exist;
      // Workspaces come from discovery.
      const workspaceSelect = root.querySelector(
        'sl-select[name="bitbucket_managed_workspace"]'
      );
      expect(workspaceSelect).to.exist;
      expect(workspaceSelect?.querySelectorAll('sl-option').length).to.equal(2);
      expect(root.textContent).to.not.contain('opaque-handle');
    });

    it('renders unknown capabilities when the provider reports none', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      stubs.completeBitbucketConnect.resolves({
        ...connectedStatus,
        capabilities: { push: null, register_webhooks: null },
      });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .bitbucketConnectHandle=${'opaque-handle'}
        ></add-tracker-modal>`
      );
      await waitUntil(() => (element as any).managedDiscovery !== null);
      await element.updateComplete;
      expect(
        element.shadowRoot
          ?.querySelector('[data-capability="push"]')
          ?.getAttribute('data-value')
      ).to.equal('unknown');
    });

    it('binds the selection, reuses the scope preview and saves through update', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .bitbucketConnectHandle=${'opaque-handle'}
        ></add-tracker-modal>`
      );
      await waitUntil(() => (element as any).managedDiscovery !== null);
      await element.updateComplete;
      (element as any).managedWorkspace = 'ws';
      await (element as any).loadManagedRepositories('ws');
      (element as any).managedRepository = 'repo';
      await element.testConnection();
      expect(stubs.bindBitbucketRepository).to.have.been.calledOnceWith(
        'managed-1',
        { workspace: 'ws', repository: 'repo' }
      );
      const [type, token, , , id, options] =
        stubs.validateTrackerToken.firstCall.args;
      expect(type).to.equal('bitbucket');
      expect(token).to.equal('unchanged');
      expect(id).to.equal('managed-1');
      expect(options.connectionDetails).to.deep.equal({
        workspace: 'ws',
        repository: 'repo',
      });
      expect((element as any).step).to.equal(2);

      await element.handleSave();
      expect(stubs.addTracker).to.not.have.been.called;
      expect(stubs.updateTracker).to.have.been.calledOnce;
      const [savedId, payload] = stubs.updateTracker.firstCall.args;
      expect(savedId).to.equal('managed-1');
      expect(payload.api_key).to.equal('unchanged');
      expect(payload.auth_type).to.equal('managed_oauth');
      expect(payload.connection_details).to.deep.equal({
        workspace: 'ws',
        repository: 'repo',
      });
      expect(payload.scope_rules).to.deep.include({
        rule_type: 'INCLUDE',
        scope_type: 'ORGANIZATION',
        identifier: 'ws',
      });
    });

    it('completes a reconnect for the named tracker and closes', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      const closed = sinon.spy();
      const host = await fixture(
        html`<div @close-modal=${closed}>
          <add-tracker-modal
            ._api=${stubbedApi}
            .bitbucketConnectHandle=${'opaque-handle'}
            .bitbucketReconnectTrackerId=${'managed-1'}
          ></add-tracker-modal>
        </div>`
      );
      element = host.querySelector('add-tracker-modal') as AddTrackerModal;
      await waitUntil(() => stubs.completeBitbucketReconnect.calledOnce);
      await waitUntil(() => closed.calledOnce, 'modal did not close');
      expect(stubs.completeBitbucketReconnect).to.have.been.calledOnceWith(
        'managed-1',
        'opaque-handle'
      );
      expect(stubs.completeBitbucketConnect).to.not.have.been.called;
      expect(closed.firstCall.args[0].detail.success).to.equal(true);
      expect(element.opened).to.equal(false);
    });

    it('surfaces a sanitized completion failure', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      stubs.completeBitbucketConnect.rejects(new Error('invalid_state'));
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .bitbucketConnectHandle=${'opaque-handle'}
        ></add-tracker-modal>`
      );
      await waitUntil(() => (element as any).errorMessage !== '');
      expect((element as any).errorMessage).to.equal('invalid_state');
      expect(stubs.getBitbucketDiscovery).to.not.have.been.called;
    });
  });

  describe('editing', () => {
    it('shows managed status with reconnect/disconnect and no token fields', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${managedTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      await waitUntil(
        () => (element as any).managedStatus !== null,
        'status did not load'
      );
      await element.updateComplete;
      expect(stubs.getBitbucketConnectionStatus).to.have.been.calledWith(
        'managed-1'
      );
      const root = element.shadowRoot!;
      expect(
        root
          .querySelector('.bitbucket-managed-status')
          ?.getAttribute('data-state')
      ).to.equal('connected');
      expect(root.querySelector('sl-button[name="bitbucket-reconnect"]')).to
        .exist;
      expect(root.querySelector('sl-button[name="bitbucket-disconnect"]')).to
        .exist;
      expect(root.querySelector('sl-input[name="api_key"]')).to.not.exist;
      expect(root.querySelector('sl-input[name="token_expires_at"]')).to.not
        .exist;
      expect(root.querySelector('sl-select[name="bitbucket_token_kind"]')).to
        .not.exist;
      // The stale manual expiry on the row is ignored for managed grants.
      expect((element as any).tokenExpiresAt).to.equal('');

      root
        .querySelector<HTMLElement>('sl-button[name="bitbucket-disconnect"]')
        ?.click();
      await waitUntil(() => stubs.disconnectBitbucket.calledOnce);
      await waitUntil(() => stubs.getBitbucketConnectionStatus.callCount === 2);
      expect(stubs.disconnectBitbucket).to.have.been.calledOnceWith(
        'managed-1'
      );

      const navigate = sandbox.stub(element, '_navigate');
      root
        .querySelector<HTMLElement>('sl-button[name="bitbucket-reconnect"]')
        ?.click();
      await waitUntil(() => navigate.calledOnce, 'reconnect did not redirect');
      expect(stubs.startBitbucketReconnect).to.have.been.calledOnceWith(
        'managed-1',
        '/console/trackers'
      );
      expect(navigate).to.have.been.calledOnceWith(
        'https://bitbucket.org/site/oauth2/authorize?r=1'
      );
    });

    it('keeps managed scope editing available when the flag is off', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {});
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${managedTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;
      const root = element.shadowRoot!;
      expect(root.querySelector('.bitbucket-managed-disabled')).to.exist;
      expect(root.querySelector('sl-button[name="bitbucket-reconnect"]')).to.not
        .exist;
      expect(root.querySelector('sl-input[name="api_key"]')).to.not.exist;
      expect(stubs.getBitbucketConnectionStatus).to.not.have.been.called;
      await element.testConnection();
      expect(stubs.bindBitbucketRepository).to.have.been.calledOnceWith(
        'managed-1',
        { workspace: 'ws', repository: 'repo' }
      );
      expect((element as any).step).to.equal(2);
    });

    it('edits a pasted OAuth token without converting it', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_cloud_oauth: true,
      });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${pastedTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;
      const root = element.shadowRoot!;
      expect(root.querySelector('sl-button[name="bitbucket-connect"]')).to.not
        .exist;
      expect(root.querySelector('.bitbucket-managed-status')).to.not.exist;
      expect(root.querySelector('sl-input[name="api_key"]')).to.exist;
      expect(root.querySelector('sl-input[name="token_expires_at"]')).to.exist;
      expect(stubs.getBitbucketConnectionStatus).to.not.have.been.called;
      (element as any).trackerToken = 'new-pasted-token';
      await element.testConnection();
      expect((element as any).step).to.equal(2);
      await element.handleSave();
      const [, payload] = stubs.updateTracker.firstCall.args;
      expect(payload.auth_type).to.equal('oauth_token');
      expect(payload.api_key).to.equal('new-pasted-token');
      expect(payload.connection_details.token_expires_at).to.equal(
        '2026-12-01'
      );
      expect(stubs.bindBitbucketRepository).to.not.have.been.called;
    });
  });
});
