import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import {
  REPOSITORY_BRANCH_HELP,
  SOURCE_BRANCH_HELP,
  pinnedCheckoutBranches,
} from './preloop-flow-form';

const reviewerPreset = {
  id: 'preset-reviewer',
  name: 'Pull Request Reviewer',
  slug: 'pull-request-reviewer',
  prompt_template: 'Review the change.',
  agent_type: 'codex',
  trigger_event_types: ['pull_request_opened'],
};

function reviewerFlow(gitCloneConfig: Record<string, unknown>) {
  return {
    name: 'Reviewer copy',
    source_preset_id: reviewerPreset.id,
    prompt_template: 'Review the change.',
    git_clone_config: { enabled: true, ...gitCloneConfig },
  };
}

describe('PreloopFlowForm checkout branch fields', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const path = String(url);
      const body = path.includes('/api/v1/flows/presets')
        ? [reviewerPreset]
        : [];
      return new Response(JSON.stringify(body));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  async function mount(
    flow: Record<string, unknown>
  ): Promise<PreloopFlowForm> {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  }

  function sourceBranch(element: PreloopFlowForm): SlInput {
    const control = element.shadowRoot!.querySelector<SlInput>(
      '[data-git="source_branch"]'
    );
    expect(control, 'Source branch control').to.exist;
    return control!;
  }

  function warning(element: PreloopFlowForm): Element | null {
    return element.shadowRoot!.querySelector('[data-pinned-branch-warning]');
  }

  it('has no placeholder that reads as a default and explains blank', async () => {
    const element = await mount(reviewerFlow({}));
    const control = sourceBranch(element);
    expect(control.placeholder || '').to.equal('');
    expect(control.value).to.equal('');
    expect(control.helpText).to.equal(SOURCE_BRANCH_HELP);
    expect(SOURCE_BRANCH_HELP).to.include('Leave blank');
    expect(SOURCE_BRANCH_HELP).to.include('source branch');
    expect(SOURCE_BRANCH_HELP).to.include('pins every run');
  });

  it('does not warn a reviewer flow with blank branches', async () => {
    const element = await mount(
      reviewerFlow({ source_branch: '  ', repositories: [] })
    );
    expect(warning(element)).to.equal(null);
  });

  it('warns a reviewer flow that pins the source branch', async () => {
    const element = await mount(reviewerFlow({ source_branch: 'master' }));
    const alert = warning(element);
    expect(alert).to.exist;
    expect(alert!.getAttribute('variant')).to.equal('warning');
    const text = (alert!.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.include('pins master');
    expect(text).to.include('When the trigger carries no head commit');
    expect(text).to.include("pull or merge request's source branch");
  });

  it('warns as soon as a branch is typed and clears when it is removed', async () => {
    const element = await mount(reviewerFlow({}));
    const control = sourceBranch(element);
    control.value = 'main';
    control.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;
    expect(warning(element)).to.exist;
    control.value = '';
    control.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;
    expect(warning(element)).to.equal(null);
  });

  it('warns a gated review flow that pins a repository branch', async () => {
    const element = await mount({
      name: 'Custom review',
      prompt_template: 'Review.',
      git_clone_config: {
        enabled: true,
        review_publication: { required: true },
        repositories: [
          { tracker_id: 't1', clone_path: 'app', branch: 'develop' },
        ],
      },
    });
    expect(warning(element)?.textContent).to.include('develop');
    const repoBranch = element.shadowRoot!.querySelector<SlInput>(
      '[data-repository-field="branch"]'
    );
    expect(repoBranch?.helpText).to.equal(REPOSITORY_BRANCH_HELP);
  });

  it('does not warn a flow that is not a reviewer', async () => {
    const element = await mount({
      name: 'Implementation',
      slug: 'automated-issue-implementation',
      prompt_template: 'Implement.',
      git_clone_config: { enabled: true, source_branch: 'develop' },
    });
    expect(sourceBranch(element).value).to.equal('develop');
    expect(warning(element)).to.equal(null);
  });

  it('lists each pinned branch once', () => {
    expect(pinnedCheckoutBranches(undefined)).to.deep.equal([]);
    expect(
      pinnedCheckoutBranches({
        source_branch: ' main ',
        repositories: [{ branch: 'main' }, { branch: '' }, { branch: 'dev' }],
      })
    ).to.deep.equal(['main', 'dev']);
  });

  async function submittedGit(
    element: PreloopFlowForm
  ): Promise<Record<string, unknown>> {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow.git_clone_config;
  }

  it('saves a blank or cleared source branch as null, not the server default', async () => {
    const blank = await mount(reviewerFlow({}));
    expect((await submittedGit(blank)).source_branch).to.equal(null);
    fixtureCleanup();

    const cleared = await mount(reviewerFlow({ source_branch: 'master' }));
    const control = sourceBranch(cleared);
    control.value = '  ';
    control.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await cleared.updateComplete;
    expect((await submittedGit(cleared)).source_branch).to.equal(null);
  });

  it('saves a pinned source branch as typed', async () => {
    const element = await mount(reviewerFlow({ source_branch: 'release/1.0' }));
    expect((await submittedGit(element)).source_branch).to.equal('release/1.0');
  });
});
