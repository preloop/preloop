import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * The flow form is the longest form in the console, so its copy is where
 * Title Case survives longest. These tests pin the labels a reader meets
 * first: the submit button, the fields above the fold, the card headers and
 * the options and radios that a label test cannot see.
 */
describe('PreloopFlowForm copy', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify([])));
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  async function mount(flow: Record<string, unknown> = {}) {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while (
      (element as unknown as { _loadingReferenceData: boolean })
        ._loadingReferenceData
    ) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  }

  it('submits a new flow with the label "Create flow"', async () => {
    const el = await mount({ name: 'Reviewer' });
    const submit = el.shadowRoot?.querySelector(
      'sl-button[type="submit"]'
    ) as HTMLElement;
    expect(submit).to.exist;
    expect(submit.textContent?.trim()).to.equal('Create flow');
  });

  it('says "Save changes" when the flow already exists', async () => {
    const el = await mount({ id: 'flow-1', name: 'Reviewer' });
    const submit = el.shadowRoot?.querySelector(
      'sl-button[type="submit"]'
    ) as HTMLElement;
    expect(submit.textContent?.trim()).to.equal('Save changes');
  });

  it('labels its fields in sentence case', async () => {
    const el = await mount();
    const labels = [...(el.shadowRoot?.querySelectorAll('[label]') || [])].map(
      (node) => node.getAttribute('label') || ''
    );
    expect(labels).to.include('Flow name');
    expect(labels).to.not.include('Flow Name');
    const titleCased = labels.filter((label) =>
      /^\w+ [A-Z]\w*/.test(label.replace(/\(.*\)/, '').trim())
    );
    expect(
      titleCased,
      `Title Case labels: ${titleCased.join(', ')}`
    ).to.deep.equal([]);
  });

  it('titles its cards in sentence case', async () => {
    const el = await mount();
    const headers = [
      ...(el.shadowRoot?.querySelectorAll('.card-header-title') || []),
    ].map((node) => (node.textContent || '').replace(/\s+/g, ' ').trim());

    expect(headers).to.include('Flow information');
    expect(headers).to.include('Trigger configuration');
    // `[A-Z][a-z]` so an acronym ("Allowed MCP tools") is not a false hit.
    const titleCased = headers.filter((header) =>
      /^\w+ .*\b[A-Z][a-z]/.test(header)
    );
    expect(
      titleCased,
      `Title Case card headers: ${titleCased.join(', ')}`
    ).to.deep.equal([]);
  });

  it('shows git defaults as placeholders and still submits nothing for them', async () => {
    const el = await mount({
      name: 'Reviewer',
      git_clone_config: { enabled: true },
    });
    const field = (label: string) =>
      el.shadowRoot?.querySelector(`sl-input[label="${label}"]`) as
        (HTMLElement & { value: string }) | null;

    expect(field('Git author name')?.value).to.equal('');
    expect(field('Git author name')?.getAttribute('placeholder')).to.equal(
      'Preloop'
    );
    expect(field('Git author email')?.getAttribute('placeholder')).to.equal(
      'git@preloop.ai'
    );
    // A blank source branch follows the trigger's branch, so a "main"
    // placeholder would read as the default and invite pinning it.
    expect(
      field('Source branch (optional)')?.hasAttribute('placeholder')
    ).to.equal(false);

    // Untouched, the keys stay absent so the server default applies, as
    // before the defaults moved to placeholders.
    const listener = sinon.spy();
    el.addEventListener('flow-submit', listener);
    await (el as any).handleFormSubmit(new Event('submit'));
    const git = listener.firstCall.args[0].detail.flow.git_clone_config;
    expect(git).to.not.have.property('git_user_name');
    expect(git).to.not.have.property('git_user_email');
    // A blank source branch is sent as null so it follows the trigger's
    // branch; an absent key would take the server's `main` default (#2211).
    expect(git.source_branch).to.equal(null);
  });

  it('offers "Add new tracker" in sentence case', async () => {
    const el = await mount({ name: 'Reviewer' });
    (el as any).handleTriggerTypeChange('tracker');
    await el.updateComplete;
    const labels = [
      ...(el.shadowRoot?.querySelectorAll('sl-button') || []),
    ].map((node) => (node.textContent || '').replace(/\s+/g, ' ').trim());
    expect(labels).to.include('Add new tracker');
  });

  it('explains project scope and disables the picker when there are no choices', async () => {
    const el = await mount({
      name: 'Reviewer',
      trigger_event_source: 'tracker-1',
      trigger_organization_id: 'org-1',
    });
    const picker = () =>
      el.shadowRoot!.querySelector('sl-select[label="Projects (optional)"]')!;
    expect(picker().hasAttribute('disabled')).to.equal(true);
    expect(picker().getAttribute('placeholder')).to.equal(
      'No projects available'
    );
    expect(picker().getAttribute('help-text')).to.include(
      'scope and sync status'
    );

    (el as any).projects = [
      { id: 'project-1', organization_id: 'org-1', name: 'Widgets' },
      { id: 'project-2', organization_id: 'org-2', name: 'Gadgets' },
    ];
    el.requestUpdate();
    await el.updateComplete;

    expect(picker().hasAttribute('disabled')).to.equal(false);
    expect(picker().getAttribute('help-text')).to.include(
      'clear the selection to include all projects in this organization'
    );
    expect(picker().querySelector('sl-option')?.textContent?.trim()).to.equal(
      'Widgets'
    );
    expect(picker().querySelectorAll('sl-option').length).to.equal(1);
  });

  it('names the execution modes by what they do', async () => {
    const el = await mount();
    (el as any).longRunningAgents = [{ id: 'agent-1', display_name: 'Agent' }];
    await el.updateComplete;
    const radios = [...(el.shadowRoot?.querySelectorAll('sl-radio') || [])].map(
      (node) => (node.textContent || '').replace(/\s+/g, ' ').trim()
    );
    expect(radios).to.include('On-demand (new sandbox per run)');
    expect(radios.some((label) => label.startsWith('Existing agent'))).to.equal(
      true
    );
  });

  it('names the trigger radios in sentence case', async () => {
    const el = await mount();
    const radios = [...(el.shadowRoot?.querySelectorAll('sl-radio') || [])].map(
      (node) => (node.textContent || '').trim()
    );

    expect(radios).to.include('Tracker event');
    expect(radios).to.not.include('Tracker Event');
  });
});
