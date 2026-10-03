/**
 * Bitbucket Data Center (manual PAT) configuration in the tracker modal.
 *
 * Covers the `features.bitbucket_dc` gate, the PAT-only payload contract
 * (canonical instance URL, version, optional discovery hints), edit
 * persistence of the stored secret and instance identity, and how backend
 * errors and unsupported version/capability reports are surfaced.
 */
import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon, { SinonSandbox, SinonStub } from 'sinon';
import './add-tracker-modal.ts';
import {
  AddTrackerModal,
  BITBUCKET_DC_VERSION,
  canonicalizeBitbucketDcUrl,
} from './add-tracker-modal';
import * as api from '../api';

const DC_OPTION = 'sl-option[value="bitbucket_dc"]';

interface Stubs {
  getFeatures: SinonStub;
  getTrackerAuthMethods: SinonStub;
  getGitHubInstallations: SinonStub;
  validateTrackerToken: SinonStub;
  listProjectsForOrg: SinonStub;
  addTracker: SinonStub;
  updateTracker: SinonStub;
}

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
    validateTrackerToken: sandbox.stub(),
    listProjectsForOrg: sandbox.stub().resolves([]),
    addTracker: sandbox.stub(),
    updateTracker: sandbox.stub(),
  };
  return { stubs, api: { ...api, ...stubs } as typeof api };
}

const savedDcTracker = {
  id: 'dc-1',
  name: 'Data Center',
  tracker_type: 'bitbucket_dc',
  url: 'https://bitbucket.example.com/stash',
  auth_type: 'api_token',
  connection_details: {
    instance_url: 'https://bitbucket.example.com/stash',
    version: BITBUCKET_DC_VERSION,
    project_key: 'PROJ',
    repository_id: 42,
    repository_slug: 'example-repo',
  },
  scope_rules: [
    { rule_type: 'INCLUDE', scope_type: 'ORGANIZATION', identifier: 'PROJ' },
  ],
};

describe('AddTrackerModal Bitbucket Data Center', () => {
  let element: AddTrackerModal;
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').resolves(new Response(JSON.stringify([])));
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
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

  describe('canonicalizeBitbucketDcUrl', () => {
    it('keeps the https origin and context path and drops trailing slashes', () => {
      expect(
        canonicalizeBitbucketDcUrl(
          ' https://Bitbucket.Example.com:8443/stash/ '
        )
      ).to.deep.equal({
        url: 'https://bitbucket.example.com:8443/stash',
        error: null,
      });
      expect(
        canonicalizeBitbucketDcUrl('https://bitbucket.example.com')
      ).to.deep.equal({ url: 'https://bitbucket.example.com', error: null });
    });

    it('rejects plain http, userinfo, query, fragment and traversal', () => {
      const rejected = [
        'http://bitbucket.example.com',
        'https://user:secret@bitbucket.example.com',
        'https://bitbucket.example.com/stash?x=1',
        'https://bitbucket.example.com/stash#frag',
        'https://bitbucket.example.com/stash/../admin',
        'https://bitbucket.example.com/stash/%2e%2e/admin',
        'https://bitbucket.example.com/stash/%252e%252e/admin',
        'https://bitbucket.example.com/stash%2fadmin',
        'https://bitbucket.example.com/stash\\admin',
        'bitbucket.example.com',
        '',
      ];
      for (const raw of rejected) {
        const result = canonicalizeBitbucketDcUrl(raw);
        expect(result.url, raw).to.equal(null);
        expect(result.error, raw).to.be.a('string').and.not.empty;
      }
    });
  });

  describe('feature gate', () => {
    it('hides the Data Center option when features.bitbucket_dc is absent', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {});
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;
      expect(stubs.getFeatures).to.have.been.calledOnce;
      expect(element.shadowRoot?.querySelector(DC_OPTION)).to.not.exist;
      expect(element.shadowRoot?.querySelector('sl-option[value="bitbucket"]'))
        .to.exist;
    });

    it('hides the Data Center option when features.bitbucket_dc is false', async () => {
      const { api: stubbedApi } = buildApi(sandbox, { bitbucket_dc: false });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;
      expect(element.shadowRoot?.querySelector(DC_OPTION)).to.not.exist;
    });

    it('hides the Data Center option when the features lookup fails', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox);
      stubs.getFeatures.rejects(new Error('offline'));
      const consoleError = sandbox.stub(console, 'error');
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;
      expect(element.shadowRoot?.querySelector(DC_OPTION)).to.not.exist;
      expect(consoleError).to.have.been.called;
    });

    it('offers the Data Center option and PAT form when enabled', async () => {
      const { api: stubbedApi } = buildApi(sandbox, { bitbucket_dc: true });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;
      const option = element.shadowRoot?.querySelector(DC_OPTION);
      expect(option).to.exist;
      expect(option?.textContent?.trim()).to.equal('Bitbucket Data Center');

      await selectType(element, 'bitbucket_dc');

      const root = element.shadowRoot!;
      expect(root.querySelector('sl-input[name="url"]')).to.exist;
      const token = root.querySelector<any>('sl-input[name="api_key"]');
      expect(token).to.exist;
      expect(token.type).to.equal('password');
      expect(token.label).to.equal('Personal access token');
      expect(root.querySelector('sl-input[name="bitbucket_dc_project_key"]')).to
        .exist;
      expect(root.querySelector('sl-input[name="bitbucket_dc_repository_id"]'))
        .to.exist;
      expect(
        root.querySelector('sl-input[name="bitbucket_dc_repository_slug"]')
      ).to.exist;
      // Cloud-only fields stay out of the DC form.
      expect(root.querySelector('sl-input[name="bitbucket_workspace"]')).to.not
        .exist;
      expect(root.querySelector('sl-select[name="bitbucket_auth_type"]')).to.not
        .exist;

      const notice = root.querySelector('.bitbucket-dc-notice');
      expect(notice).to.exist;
      const text = (notice?.textContent ?? '').replace(/\s+/g, ' ');
      expect(text).to.contain('Administrator approval required');
      expect(text).to.contain(`Data Center ${BITBUCKET_DC_VERSION} LTS`);
      expect(text).to.contain('unsupported or unvalidated');
      expect(text).to.contain('never routed through Bitbucket Cloud');
    });
  });

  describe('PAT payload', () => {
    const fillDcForm = (el: AddTrackerModal) => {
      const state = el as any;
      state.trackerName = 'Data Center';
      state.trackerType = 'bitbucket_dc';
      state.bitbucketDcInstanceUrl = 'https://Bitbucket.Example.com/stash/';
      state.trackerToken = 'pat-secret';
      state.bitbucketDcProjectKey = ' PROJ ';
      state.bitbucketDcRepositoryId = '42';
      state.bitbucketDcRepositorySlug = 'example-repo';
    };

    it('tests the connection with the canonical instance URL and api_token auth', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.validateTrackerToken.resolves({
        success: true,
        message: 'ok',
        orgs: [{ id: 'PROJ', name: 'Example project' }],
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      fillDcForm(element);
      await element.updateComplete;

      await element.testConnection();

      expect(stubs.validateTrackerToken).to.have.been.calledOnce;
      const [type, token, url, , trackerId, options] =
        stubs.validateTrackerToken.firstCall.args;
      expect(type).to.equal('bitbucket_dc');
      expect(token).to.equal('pat-secret');
      expect(url).to.equal('https://bitbucket.example.com/stash');
      expect(trackerId).to.equal(undefined);
      expect(options).to.deep.equal({
        authType: 'api_token',
        connectionDetails: {
          instance_url: 'https://bitbucket.example.com/stash',
          version: BITBUCKET_DC_VERSION,
          project_key: 'PROJ',
          repository_id: 42,
          repository_slug: 'example-repo',
        },
      });
      expect((element as any).step).to.equal(2);
      expect((element as any).errorMessage).to.equal('');
    });

    it('creates the tracker with the backend configuration contract', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.addTracker.resolves({ id: 'dc-new', warnings: [] });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      fillDcForm(element);
      const state = element as any;
      state.orgs = [{ id: 'PROJ', name: 'Example project' }];
      state.selectedOrgs = { PROJ: true };
      await element.updateComplete;

      await element.handleSave();

      expect(stubs.addTracker).to.have.been.calledOnce;
      const payload = stubs.addTracker.firstCall.args[0];
      expect(payload.type).to.equal('bitbucket_dc');
      expect(payload.auth_type).to.equal('api_token');
      expect(payload.api_key).to.equal('pat-secret');
      expect(payload.url).to.equal('https://bitbucket.example.com/stash');
      expect(payload.connection_details).to.deep.equal({
        instance_url: 'https://bitbucket.example.com/stash',
        version: BITBUCKET_DC_VERSION,
        project_key: 'PROJ',
        repository_id: 42,
        repository_slug: 'example-repo',
      });
      expect(payload.config).to.deep.equal(payload.connection_details);
      expect(payload).to.not.have.property('github_installation_id');
      expect(payload.scope_rules).to.deep.equal([
        {
          rule_type: 'INCLUDE',
          scope_type: 'ORGANIZATION',
          identifier: 'PROJ',
        },
      ]);
    });

    it('omits project and repository hints when left blank', async () => {
      const { api: stubbedApi } = buildApi(sandbox, { bitbucket_dc: true });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      const state = element as any;
      state.trackerType = 'bitbucket_dc';
      state.bitbucketDcInstanceUrl = 'https://bitbucket.example.com';
      state.bitbucketDcProjectKey = '   ';
      state.bitbucketDcRepositoryId = '';
      state.bitbucketDcRepositorySlug = '';

      expect(element.bitbucketDcConnectionDetails()).to.deep.equal({
        instance_url: 'https://bitbucket.example.com',
        version: BITBUCKET_DC_VERSION,
      });
    });

    it('refuses to send the PAT when the instance URL is invalid', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      fillDcForm(element);
      (element as any).bitbucketDcInstanceUrl =
        'http://bitbucket.example.com/stash';
      await element.updateComplete;

      await element.testConnection();
      await element.updateComplete;

      expect(stubs.validateTrackerToken).to.not.have.been.called;
      expect((element as any).step).to.equal(1);
      expect((element as any).errorMessage).to.contain('https://');
      expect(
        element.shadowRoot?.querySelector('.error')?.textContent
      ).to.contain('https://');
    });

    it('rejects a non-numeric repository id before contacting the backend', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      fillDcForm(element);
      (element as any).bitbucketDcRepositoryId = 'example-repo';

      await element.testConnection();

      expect(stubs.validateTrackerToken).to.not.have.been.called;
      expect((element as any).errorMessage).to.contain('Repository ID');
    });

    it('rejects zero and unsafe numeric repository identities', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      fillDcForm(element);
      for (const repositoryId of ['0', '-1', '9007199254740993']) {
        (element as any).bitbucketDcRepositoryId = repositoryId;
        await element.testConnection();
        expect(stubs.validateTrackerToken).to.not.have.been.called;
        expect((element as any).errorMessage).to.contain('Repository ID');
      }
    });

    it('keeps the reviewer slug separate from Bearer PAT authentication', async () => {
      const { api: stubbedApi } = buildApi(sandbox, { bitbucket_dc: true });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      fillDcForm(element);
      (element as any).trackerUsername = 'jane';
      const details = element.bitbucketDcConnectionDetails();
      expect(details.username).to.equal('jane');
      expect(details).to.not.have.property('api_key');
      expect(details).to.not.have.property('password');
    });

    it('requires a token for a new tracker', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      fillDcForm(element);
      (element as any).trackerToken = '   ';

      await element.testConnection();

      expect(stubs.validateTrackerToken).to.not.have.been.called;
      expect((element as any).errorMessage).to.contain('personal access token');
    });
  });

  describe('edit persistence', () => {
    it('loads the saved instance, identity and keeps the secret unchanged', async () => {
      const { api: stubbedApi } = buildApi(sandbox, { bitbucket_dc: true });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${savedDcTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;

      const state = element as any;
      expect(state.trackerType).to.equal('bitbucket_dc');
      expect(state.bitbucketDcInstanceUrl).to.equal(
        'https://bitbucket.example.com/stash'
      );
      expect(state.bitbucketDcProjectKey).to.equal('PROJ');
      expect(state.bitbucketDcRepositoryId).to.equal('42');
      expect(state.bitbucketDcRepositorySlug).to.equal('example-repo');
      expect(state.trackerToken).to.equal('unchanged');
      expect(state.authMethod).to.equal('api_token');

      const root = element.shadowRoot!;
      expect(root.querySelector(DC_OPTION)).to.exist;
      const urlInput = root.querySelector<any>('sl-input[name="url"]');
      expect(urlInput.value).to.equal('https://bitbucket.example.com/stash');
      expect(urlInput.disabled).to.be.true;
      expect(
        root.querySelector<any>('sl-input[name="api_key"]').value
      ).to.equal('unchanged');
    });

    it('prefers connection_details.instance_url over a drifted url column', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.validateTrackerToken.resolves({
        success: true,
        message: 'ok',
        orgs: [{ id: 'PROJ', name: 'Example project' }],
      });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${{
            ...savedDcTracker,
            url: 'https://bitbucket.example.com/stash/',
          }}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);

      await element.testConnection();

      const [, token, url, , trackerId, options] =
        stubs.validateTrackerToken.firstCall.args;
      expect(token).to.equal('unchanged');
      expect(url).to.equal('https://bitbucket.example.com/stash');
      expect(trackerId).to.equal('dc-1');
      expect(options.connectionDetails.instance_url).to.equal(
        'https://bitbucket.example.com/stash'
      );
    });

    it('saves edits with the unchanged secret and the same instance identity', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.updateTracker.resolves({ id: 'dc-1', warnings: [] });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${savedDcTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      const state = element as any;
      state.orgs = [{ id: 'PROJ', name: 'Example project' }];
      state.selectedOrgs = { PROJ: true };
      // The slug may change on rename; the numeric id stays put.
      state.bitbucketDcRepositorySlug = 'renamed-repo';

      await element.handleSave();

      expect(stubs.updateTracker).to.have.been.calledOnce;
      const [trackerId, payload] = stubs.updateTracker.firstCall.args;
      expect(trackerId).to.equal('dc-1');
      expect(payload.type).to.equal('bitbucket_dc');
      expect(payload.auth_type).to.equal('api_token');
      expect(payload.api_key).to.equal('unchanged');
      expect(payload.url).to.equal('https://bitbucket.example.com/stash');
      expect(payload.connection_details).to.deep.equal({
        instance_url: 'https://bitbucket.example.com/stash',
        version: BITBUCKET_DC_VERSION,
        project_key: 'PROJ',
        repository_id: 42,
        repository_slug: 'renamed-repo',
      });
    });

    it('sends a replacement token when the user enters one', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.updateTracker.resolves({ id: 'dc-1' });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${savedDcTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      (element as any).trackerToken = 'rotated-pat';

      await element.handleSave();

      expect(stubs.updateTracker.firstCall.args[1].api_key).to.equal(
        'rotated-pat'
      );
    });
  });

  describe('disabled feature while editing', () => {
    it('keeps the Data Center type visible, explains the gate and blocks submission', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: false,
      });
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${savedDcTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      await element.updateComplete;

      const root = element.shadowRoot!;
      // The real type stays selected; it is never shown as Bitbucket Cloud.
      expect((element as any).trackerType).to.equal('bitbucket_dc');
      expect(root.querySelector(DC_OPTION)).to.exist;
      expect(root.querySelector<any>('sl-select[name="type"]').value).to.equal(
        'bitbucket_dc'
      );
      expect(root.querySelector<any>('sl-select[name="type"]').disabled).to.be
        .true;
      const alert = root.querySelector('.bitbucket-dc-disabled');
      expect(alert).to.exist;
      const alertText = (alert?.textContent ?? '').replace(/\s+/g, ' ');
      expect(alertText).to.contain('disabled on this deployment');
      expect(alertText).to.contain('not treated as a Bitbucket Cloud tracker');

      const next = root.querySelector<any>('sl-button[variant="primary"]');
      expect(next.disabled).to.be.true;

      await element.testConnection();
      expect(stubs.validateTrackerToken).to.not.have.been.called;
      expect((element as any).errorMessage).to.contain('bitbucket_dc');

      await element.handleSave();
      expect(stubs.updateTracker).to.not.have.been.called;
    });
  });

  describe('backend errors and unsupported capability', () => {
    it('shows the backend message when the instance is not approved', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.validateTrackerToken.rejects(
        new Error(
          'Bitbucket Data Center instance https://bitbucket.example.com is not on the approved list'
        )
      );
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      const state = element as any;
      state.trackerType = 'bitbucket_dc';
      state.bitbucketDcInstanceUrl = 'https://bitbucket.example.com';
      state.trackerToken = 'pat-secret';

      await element.testConnection();
      await element.updateComplete;

      expect(state.step).to.equal(1);
      expect(state.errorMessage).to.contain('not on the approved list');
      expect(
        element.shadowRoot?.querySelector('.error')?.textContent
      ).to.contain('not on the approved list');
    });

    it('shows an unsupported version report from the connection test', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.validateTrackerToken.resolves({
        success: false,
        message:
          'Bitbucket Data Center 8.19 is unsupported: only 10.2 LTS is validated\nREST 1.0 build-status capability unvalidated',
        orgs: [],
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      const state = element as any;
      state.trackerType = 'bitbucket_dc';
      state.bitbucketDcInstanceUrl = 'https://bitbucket.example.com';
      state.trackerToken = 'pat-secret';

      await element.testConnection();
      await element.updateComplete;

      expect(state.step).to.equal(1);
      expect(state.errorMessage).to.equal(
        'Bitbucket Data Center 8.19 is unsupported: only 10.2 LTS is validated'
      );
      expect(
        element.shadowRoot?.querySelector('.error')?.textContent
      ).to.contain('is unsupported');
      expect(stubs.addTracker).to.not.have.been.called;
    });

    it('keeps the modal open and lists capability warnings returned on save', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.addTracker.resolves({
        id: 'dc-new',
        warnings: [
          'Reviewer verdicts are unsupported on this Data Center release',
          'Build status operations are unvalidated',
        ],
      });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      const state = element as any;
      state.trackerType = 'bitbucket_dc';
      state.bitbucketDcInstanceUrl = 'https://bitbucket.example.com';
      state.trackerToken = 'pat-secret';
      state.orgs = [{ id: 'PROJ', name: 'Example project' }];
      state.selectedOrgs = { PROJ: true };

      let closed = false;
      element.addEventListener('close-modal', () => (closed = true));
      let added: CustomEvent | null = null;
      element.addEventListener(
        'tracker-added',
        (e) => (added = e as CustomEvent)
      );

      await element.handleSave();
      await element.updateComplete;

      expect(closed).to.be.false;
      expect(added).to.not.equal(null);
      expect(added!.detail.hasWarnings).to.be.true;
      const items = Array.from(
        element.shadowRoot?.querySelectorAll(
          'sl-alert[variant="warning"] li'
        ) ?? []
      ).map((li) => li.textContent?.trim());
      expect(items).to.deep.equal([
        'Reviewer verdicts are unsupported on this Data Center release',
        'Build status operations are unvalidated',
      ]);
      const done = element.shadowRoot?.querySelector(
        'sl-button[variant="primary"]'
      );
      expect(done?.textContent?.trim()).to.equal('Done');
    });

    it('surfaces an update rejection without changing the stored identity', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.updateTracker.rejects(
        new Error('Create a new tracker to change the Data Center instance')
      );
      element = await fixture(
        html`<add-tracker-modal
          ._api=${stubbedApi}
          .tracker=${savedDcTracker}
        ></add-tracker-modal>`
      );
      await featureLoaded(element);
      const state = element as any;
      state.orgs = [{ id: 'PROJ', name: 'Example project' }];
      state.selectedOrgs = { PROJ: true };

      await element.handleSave();
      await element.updateComplete;

      expect(state.errorMessage).to.contain('Create a new tracker');
      expect(state.bitbucketDcInstanceUrl).to.equal(
        'https://bitbucket.example.com/stash'
      );
      expect(state.trackerToken).to.equal('unchanged');
    });
  });

  describe('Bitbucket Cloud is preserved', () => {
    it('still sends the Cloud contract when Data Center is enabled', async () => {
      const { stubs, api: stubbedApi } = buildApi(sandbox, {
        bitbucket_dc: true,
      });
      stubs.addTracker.resolves({ id: 'bb-new' });
      element = await fixture(
        html`<add-tracker-modal ._api=${stubbedApi}></add-tracker-modal>`
      );
      await featureLoaded(element);
      await selectType(element, 'bitbucket');
      const state = element as any;
      state.bitbucketWorkspace = 'example-workspace';
      state.trackerToken = 'cloud-token';
      state.orgs = [{ id: 'example-workspace', name: 'Example' }];
      state.selectedOrgs = { 'example-workspace': true };

      await element.handleSave();

      const payload = stubs.addTracker.firstCall.args[0];
      expect(payload.type).to.equal('bitbucket');
      expect(payload.url).to.equal('https://bitbucket.org');
      expect(payload.auth_type).to.equal('api_token');
      expect(payload.connection_details).to.deep.equal({
        workspace: 'example-workspace',
        token_kind: 'api_token',
      });
      expect(payload.connection_details).to.not.have.property('instance_url');
    });
  });
});
