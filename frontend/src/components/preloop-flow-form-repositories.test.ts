import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import { resetConfirmDialogForTests } from './confirm-dialog';

const trackers = [
  {
    id: 'github',
    name: 'GitHub',
    tracker_type: 'github',
    hosts_repositories: true,
  },
  {
    id: 'gitlab',
    name: 'GitLab',
    tracker_type: 'gitlab',
    hosts_repositories: true,
  },
  { id: 'jira', name: 'Jira', tracker_type: 'jira' },
];
const projects = [
  { id: 'api', name: 'API', organization_id: 'github-org' },
  { id: 'web', name: 'Web', organization_id: 'gitlab-org' },
];

describe('PreloopFlowForm repository checkouts', () => {
  let sandbox: SinonSandbox;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const target = String(url);
      const data = target.includes('/api/v1/trackers')
        ? trackers
        : target.includes('/api/v1/projects')
          ? projects
          : target.includes('/api/v1/organizations')
            ? {
                items: trackers.map((tracker) => ({
                  id: `${tracker.id}-org`,
                  tracker_id: tracker.id,
                })),
              }
            : [];
      return new Response(JSON.stringify(data));
    });
  });
  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
    resetConfirmDialogForTests();
  });
  async function mount(git: Record<string, unknown> = { enabled: true }) {
    const element = await fixture<PreloopFlowForm>(
      html` <preloop-flow-form
        .flow=${{
          name: 'Multi-repository flow',
          agent_type: 'codex',
          trigger_event_source: 'webhook',
          trigger_event_types: ['webhook'],
          git_clone_config: git,
        }}
      ></preloop-flow-form>`
    );
    await waitUntil(() => !(element as any)._loadingReferenceData);
    await element.updateComplete;
    return element;
  }
  async function click(element: PreloopFlowForm, selector: string) {
    const button = element.shadowRoot!.querySelector<HTMLElement>(selector);
    expect(button, selector).to.exist;
    button!.click();
    await element.updateComplete;
  }
  async function change(
    element: PreloopFlowForm,
    selector: string,
    value: string
  ) {
    const input = element.shadowRoot!.querySelector(selector) as any;
    expect(input, selector).to.exist;
    input.value = value;
    input.dispatchEvent(
      new Event(input.tagName === 'SL-INPUT' ? 'sl-input' : 'sl-change')
    );
    await element.updateComplete;
  }
  async function saved(element: PreloopFlowForm) {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener, { once: true });
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return JSON.parse(
      JSON.stringify(listener.firstCall.args[0].detail.flow.git_clone_config)
    );
  }

  for (const trackerType of ['azure_devops', 'bitbucket_dc', 'future_forge']) {
    it(`offers repository-capable ${trackerType} trackers`, async () => {
      const element = await mount();
      (element as any).trackers = [
        {
          id: 'repo-host',
          name: 'Repository host',
          tracker_type: trackerType,
          hosts_repositories: true,
        },
        {
          id: 'issues',
          name: 'Issues',
          tracker_type: 'github',
          hosts_repositories: false,
        },
      ];
      await element.updateComplete;
      await click(element, '[data-add-repository]');
      expect(
        element.flow.git_clone_config?.repositories?.[0].tracker_id
      ).to.equal('repo-host');
      const choices = element.shadowRoot!.querySelectorAll(
        '[data-repository-field="tracker_id"] sl-option'
      );
      expect(choices.length).to.equal(1);
      expect(choices[0].getAttribute('value')).to.equal('repo-host');
    });
  }

  it('creates, saves, reloads and edits two independent checkouts', async () => {
    const element = await mount();
    await click(element, '[data-add-repository]');
    await click(element, '[data-add-repository]');
    for (const [index, project] of projects.entries()) {
      const row = `[data-repository="${index}"]`;
      await change(
        element,
        `${row} [data-repository-field="tracker_id"]`,
        trackers[index].id
      );
      await change(
        element,
        `${row} [data-repository-field="project_id"]`,
        project.id
      );
      await change(
        element,
        `${row} [data-repository-field="repository_url"]`,
        `https://example.com/team/${project.id}.git`
      );
      await change(
        element,
        `${row} [data-repository-field="clone_path"]`,
        `/workspace/${project.id}`
      );
      await change(element, `${row} [data-repository-field="branch"]`, 'main');
    }
    const config = await saved(element);
    expect(config.repositories).to.deep.equal(
      projects.map((project, index) => ({
        tracker_id: trackers[index].id,
        project_id: project.id,
        repository_url: `https://example.com/team/${project.id}.git`,
        clone_path: `/workspace/${project.id}`,
        branch: 'main',
      }))
    );
    const reloaded = await mount(config);
    expect(
      reloaded.shadowRoot!.querySelectorAll('[data-repository]')
    ).to.have.length(2);
    await change(
      reloaded,
      '[data-repository="1"] [data-repository-field="branch"]',
      'release'
    );
    const edited = await saved(reloaded);
    expect(edited.repositories[0]).to.deep.equal(config.repositories[0]);
    expect(edited.repositories[1].branch).to.equal('release');
  });

  it('filters projects by tracker and clears the previous project when tracker changes', async () => {
    const element = await mount({
      enabled: true,
      repositories: [
        {
          tracker_id: 'github',
          project_id: 'api',
          clone_path: '/workspace',
        },
      ],
    });
    const selector =
      '[data-repository="0"] [data-repository-field="project_id"]';
    const values = () =>
      Array.from(
        element.shadowRoot!.querySelectorAll(`${selector} sl-option`)
      ).map((option) => option.getAttribute('value'));
    await waitUntil(() => (element as any).repositoryProjects.length === 2);
    await element.updateComplete;
    expect(values()).to.include('api');
    expect(values()).not.to.include('web');
    const trackerOptions = element.shadowRoot!.querySelectorAll(
      '[data-repository-field="tracker_id"] sl-option'
    );
    expect(
      Array.from(trackerOptions).map((option) => option.getAttribute('value'))
    ).not.to.include('jira');
    await change(
      element,
      '[data-repository="0"] [data-repository-field="tracker_id"]',
      'gitlab'
    );
    expect(element.flow.git_clone_config.repositories[0].tracker_id).to.equal(
      'gitlab'
    );
    await waitUntil(() => values().includes('web'));
    expect(values()).not.to.include('api');
    expect((await saved(element)).repositories[0].project_id).to.be.undefined;
  });

  it('marks the first checkout as default and can promote another without losing fields', async () => {
    const repositories = [
      { tracker_id: 'github', clone_path: '/workspace/api', branch: 'main' },
      { tracker_id: 'gitlab', clone_path: '/workspace/web', branch: 'release' },
    ];
    const element = await mount({ enabled: true, repositories });
    expect(
      element.shadowRoot!.querySelector(
        '[data-repository="0"] [data-default-repository]'
      )?.textContent
    ).to.include('Default');
    await click(
      element,
      '[data-repository="1"] [data-make-default-repository]'
    );
    expect((await saved(element)).repositories).to.deep.equal([
      repositories[1],
      repositories[0],
    ]);
  });

  it('removes a checkout and adds a distinct path after a removal', async () => {
    const element = await mount({
      enabled: true,
      repositories: [
        { tracker_id: 'github', clone_path: '/workspace' },
        { tracker_id: 'github', clone_path: '/workspace-2' },
        { tracker_id: 'github', clone_path: '/workspace-3' },
      ],
    });
    await click(element, '[data-repository="1"] [data-remove-repository]');
    const dialog = document.querySelector('confirm-dialog') as any;
    await dialog.updateComplete;
    dialog.shadowRoot.querySelector('sl-button[variant="danger"]').click();
    await waitUntil(
      () => element.flow.git_clone_config.repositories.length === 2
    );
    await element.updateComplete;
    expect(
      (await saved(element)).repositories.map((repo: any) => repo.clone_path)
    ).to.deep.equal(['/workspace', '/workspace-3']);
    await click(element, '[data-add-repository]');
    expect(
      (await saved(element)).repositories.map((repo: any) => repo.clone_path)
    ).to.deep.equal(['/workspace', '/workspace-3', '/workspace-2']);
  });

  it('preserves saved repository selections when reference choices are unavailable', async () => {
    const repositories = [
      {
        tracker_id: 'unavailable-tracker',
        project_id: 'unavailable-project',
        repository_url: 'https://example.com/team/api.git',
        clone_path: '/workspace/api',
      },
    ];
    const element = await mount({ enabled: true, repositories });
    await waitUntil(() => (element as any).repositoryProjects.length === 2);
    await element.updateComplete;
    const tracker = element.shadowRoot!.querySelector(
      '[data-repository-field="tracker_id"]'
    ) as any;
    const project = element.shadowRoot!.querySelector(
      '[data-repository-field="project_id"]'
    ) as any;
    expect(tracker.value).to.equal('unavailable-tracker');
    expect(project.value).to.equal('unavailable-project');
    expect((await saved(element)).repositories).to.deep.equal(repositories);
  });

  it('says a disabled Git workspace means no checkout', async () => {
    const element = await mount({ enabled: false, repositories: [] });
    const help = element.shadowRoot!.querySelector(
      '[data-git-clone-off-help]'
    ) as HTMLElement;
    expect(help.textContent!.trim()).to.equal(
      'Off: the agent runs without a checkout of the trigger repository.'
    );
    const toggle = element.shadowRoot!.querySelector(
      '[data-git-clone-enabled]'
    ) as any;
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('[data-git-clone-off-help]')).to.be
      .null;
  });

  it('loads repository choices only after enabling cloning', async () => {
    const element = await mount({
      enabled: false,
      repositories: [
        {
          tracker_id: 'github',
          clone_path: '/workspace',
        },
      ],
    });
    expect(
      (window.fetch as sinon.SinonStub)
        .getCalls()
        .filter((call) => String(call.args[0]).includes('/api/v1/projects'))
    ).to.have.length(0);
    const toggle = element.shadowRoot!.querySelector(
      '[data-git-clone-enabled]'
    ) as any;
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await waitUntil(() => (element as any).repositoryProjects.length === 2);
    await element.updateComplete;
    expect(
      element.shadowRoot!.querySelectorAll('[data-repository]')
    ).to.have.length(1);
  });

  it('preserves Jira bindings and other Git options through editing and disabling cloning', async () => {
    const config = {
      enabled: true,
      repositories: [],
      repository_bindings: [
        { tracker_id: 'gitlab', repository: 'team/api', default: true },
      ],
      publication_mode: 'isolated',
      source_branch: 'main',
    };
    const element = await mount(config);
    expect(await saved(element)).to.deep.equal(config);
    await click(element, '[data-add-repository]');
    const toggle = element.shadowRoot!.querySelector(
      '[data-git-clone-enabled]'
    ) as any;
    toggle.checked = false;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    const disabled = await saved(element);
    expect(disabled.enabled).to.equal(false);
    expect(disabled.repositories).to.deep.equal([]);
    expect(element.flow.git_clone_config.repositories).to.have.length(1);
    expect(disabled.repository_bindings).to.deep.equal(
      config.repository_bindings
    );
    expect(disabled.publication_mode).to.equal('isolated');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await element.updateComplete;
    expect((await saved(element)).repositories).to.have.length(1);
  });
});
