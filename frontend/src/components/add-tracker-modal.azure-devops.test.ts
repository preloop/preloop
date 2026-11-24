/**
 * Azure DevOps (Azure Repos) tracker form: fields, validation and the
 * payload sent to the test, project-listing and create endpoints.
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
}

function buildApi(sandbox: SinonSandbox): { stubs: Stubs; api: typeof api } {
  const stubs: Stubs = {
    getFeatures: sandbox.stub().resolves({ plugins: [], features: {} }),
    getTrackerAuthMethods: sandbox
      .stub()
      .resolves({ methods: ['api_token'], github_app_configured: false }),
    getGitHubInstallations: sandbox.stub().resolves([]),
    validateTrackerToken: sandbox.stub().resolves({
      success: true,
      message: 'ok',
      orgs: [{ id: 'example-org', name: 'example-org' }],
    }),
    listProjectsForOrg: sandbox.stub().resolves([
      { id: 'r1', name: 'web', group: 'Platform' },
      { id: 'r2', name: 'api', group: 'Platform' },
      { id: 'r3', name: 'docs', group: 'Docs' },
    ]),
    addTracker: sandbox.stub().resolves({ id: 'new-1' }),
    updateTracker: sandbox.stub().resolves({ id: 'existing-1' }),
  };
  return { stubs, api: { ...api, ...stubs } as typeof api };
}

describe('AddTrackerModal Azure DevOps', () => {
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

  async function open(stubbedApi: typeof api, tracker?: unknown) {
    element = await fixture(
      html`<add-tracker-modal
        ._api=${stubbedApi}
        .tracker=${tracker}
      ></add-tracker-modal>`
    );
    await featureLoaded(element);
    return element;
  }

  it('offers the Azure DevOps option and its fields', async () => {
    const { api: stubbedApi } = buildApi(sandbox);
    await open(stubbedApi);
    const root = element.shadowRoot!;
    expect(
      root.querySelector('sl-option[value="azure_devops"]')?.textContent
    ).to.contain('Azure DevOps');
    await selectType(element, 'azure_devops');
    expect(root.querySelector('sl-input[name="azure_devops_organization"]')).to
      .exist;
    expect(root.querySelector('sl-input[name="azure_devops_project"]')).to
      .exist;
    expect(root.querySelector('sl-input[name="azure_devops_repository"]')).to
      .exist;
    expect(root.querySelector('sl-input[name="url"]')).to.not.exist;
    const tokens = root.querySelectorAll('sl-input[name="api_key"]');
    expect(tokens.length).to.equal(1);
    const hint = tokens[0].getAttribute('help-text') || '';
    expect(hint).to.contain('Code (Read)');
    expect(hint).to.contain('Pull Request Threads (Read & write)');
    expect(hint).to.contain('Service Hooks');
    const guide = root.querySelector<HTMLAnchorElement>(
      '.azure-devops-guide a'
    );
    expect(guide?.href).to.contain('azure-devops-tracker');
  });

  it('defaults voting off and sends the selected vote preference', async () => {
    const { api: stubbedApi } = buildApi(sandbox);
    await open(stubbedApi);
    await selectType(element, 'azure_devops');
    (element as any).azureDevOpsOrganization = 'example-org';
    const toggle = element.shadowRoot!.querySelector<any>(
      'sl-switch[name="azure_devops_cast_token_owner_vote"]'
    )!;
    expect(toggle.checked).to.equal(false);
    expect(
      element.azureDevOpsConnectionDetails().cast_token_owner_vote
    ).to.equal(false);
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    expect(
      element.azureDevOpsConnectionDetails().cast_token_owner_vote
    ).to.equal(true);
  });

  it('requires an organization and a project for a repository', async () => {
    const { stubs, api: stubbedApi } = buildApi(sandbox);
    await open(stubbedApi);
    await selectType(element, 'azure_devops');
    const state = element as any;
    state.trackerToken = 'pat';
    await element.testConnection();
    expect(state.errorMessage).to.contain('organization');
    state.azureDevOpsOrganization = 'https://example-org.visualstudio.com';
    await element.testConnection();
    expect(state.errorMessage).to.contain('not a URL');
    state.azureDevOpsOrganization = 'example-org';
    state.azureDevOpsRepository = 'web';
    await element.testConnection();
    expect(state.errorMessage).to.contain('project');
    expect(stubs.validateTrackerToken).to.not.have.been.called;
  });

  it('sends the contract payload to test, list and create', async () => {
    const { stubs, api: stubbedApi } = buildApi(sandbox);
    await open(stubbedApi);
    await selectType(element, 'azure_devops');
    const state = element as any;
    state.trackerName = 'Azure DevOps';
    state.trackerToken = 'pat-secret';
    state.azureDevOpsOrganization = ' example-org ';
    state.azureDevOpsProject = 'Platform';
    await element.testConnection();

    const [type, token, url, , , options] =
      stubs.validateTrackerToken.firstCall.args;
    expect(type).to.equal('azure_devops');
    expect(token).to.equal('pat-secret');
    expect(url).to.equal('https://dev.azure.com');
    expect(options).to.deep.equal({
      connectionDetails: {
        organization: 'example-org',
        project: 'Platform',
        cast_token_owner_vote: false,
      },
      authType: 'api_token',
    });
    expect(state.step).to.equal(2);

    await element.loadProjects('example-org');
    const projectArgs = stubs.listProjectsForOrg.firstCall.args;
    expect(projectArgs[0]).to.equal('azure_devops');
    expect(projectArgs[6].connectionDetails).to.deep.equal({
      organization: 'example-org',
      project: 'Platform',
      cast_token_owner_vote: false,
    });
    await element.updateComplete;
    const groups = [
      ...element.shadowRoot!.querySelectorAll('.project-group'),
    ].map((node) => node.getAttribute('data-group'));
    expect(groups).to.include.members(['Platform', 'Docs']);

    await element.handleSave();
    const payload = stubs.addTracker.firstCall.args[0];
    expect(payload.type).to.equal('azure_devops');
    expect(payload.url).to.equal('https://dev.azure.com');
    expect(payload.auth_type).to.equal('api_token');
    expect(payload.api_key).to.equal('pat-secret');
    expect(payload.connection_details).to.deep.equal({
      organization: 'example-org',
      project: 'Platform',
      cast_token_owner_vote: false,
    });
  });

  it('keeps the stored token and details when editing', async () => {
    const { stubs, api: stubbedApi } = buildApi(sandbox);
    await open(stubbedApi, {
      id: 'existing-1',
      name: 'Azure DevOps',
      tracker_type: 'azure_devops',
      url: 'https://dev.azure.com',
      auth_type: 'api_token',
      connection_details: {
        organization: 'example-org',
        project: 'Platform',
        repository: 'web',
      },
      scope_rules: [
        {
          rule_type: 'INCLUDE',
          scope_type: 'ORGANIZATION',
          identifier: 'example-org',
        },
      ],
    });
    await element.handleSave();
    const [id, payload] = stubs.updateTracker.firstCall.args;
    expect(id).to.equal('existing-1');
    expect(payload.api_key).to.equal('unchanged');
    expect(payload.connection_details).to.deep.equal({
      organization: 'example-org',
      project: 'Platform',
      cast_token_owner_vote: false,
      repository: 'web',
      cast_token_owner_vote: true,
    });
  });
});
