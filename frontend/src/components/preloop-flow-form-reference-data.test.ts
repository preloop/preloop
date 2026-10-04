import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * When trackers or models fail to load, the selects that need them would be
 * silently empty. The form says so and offers a retry.
 */
describe('PreloopFlowForm reference data warning', () => {
  let sandbox: SinonSandbox;
  let trackersFail: boolean;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    trackersFail = true;
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/trackers')) {
        return trackersFail
          ? new Response(JSON.stringify({ detail: 'Unavailable' }), {
              status: 500,
            })
          : new Response(
              JSON.stringify([
                { id: 'tracker-1', name: 'Example', tracker_type: 'github' },
              ])
            );
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (): Promise<PreloopFlowForm> => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${{ name: 'Triage' }}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  const warning = (element: PreloopFlowForm) =>
    element.shadowRoot!.querySelector('[data-reference-lists-warning]');

  it('warns that trackers could not be loaded and clears after a retry', async () => {
    const element = await mount();
    expect(warning(element)?.textContent).to.include(
      'Trackers could not be loaded'
    );

    trackersFail = false;
    const retry = warning(element)!.querySelector('sl-button') as HTMLElement;
    expect(retry.textContent?.trim()).to.equal('Try again');
    retry.click();
    while ((element as any).retryingReferenceLists) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;

    expect(warning(element)).to.equal(null);
    expect((element as any).trackers).to.have.length(1);
  });

  it('shows no warning when the lists load', async () => {
    trackersFail = false;
    const element = await mount();
    expect(warning(element)).to.equal(null);
  });
});
