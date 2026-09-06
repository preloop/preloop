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

  it('names the trigger radios in sentence case', async () => {
    const el = await mount();
    const radios = [...(el.shadowRoot?.querySelectorAll('sl-radio') || [])].map(
      (node) => (node.textContent || '').trim()
    );

    expect(radios).to.include('Tracker event');
    expect(radios).to.not.include('Tracker Event');
  });
});
