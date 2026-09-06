import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * The flow form is the longest form in the console, so its copy is where
 * Title Case survives longest. These tests pin the labels a reader meets
 * first: the submit button and the fields above the fold.
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
});
