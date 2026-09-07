import { expect, waitUntil, aTimeout } from '@open-wc/testing';
import { emulateMedia } from '@web/test-runner-commands';
import sinon from 'sinon';
import {
  routeLoadingRenderer,
  PENDING_DELAY_MS,
  type RouteLoadError,
} from './route-loading';

describe('route loading states', () => {
  let parent: HTMLElement;

  beforeEach(() => {
    parent = document.createElement('div');
    parent.innerHTML = '<span class="previous">the page you were on</span>';
    document.body.append(parent);
  });

  afterEach(async () => {
    parent.remove();
    await emulateMedia({ reducedMotion: 'no-preference' });
  });

  it('draws nothing while a fast chunk is in flight', async () => {
    const stop = routeLoadingRenderer.pending({ parent, atOutlet: true });
    await aTimeout(PENDING_DELAY_MS - 60);
    stop();
    await aTimeout(PENDING_DELAY_MS);
    // The previous view stays put: a wait too short to read is not a state.
    expect(parent.querySelector('route-loading')).to.equal(null);
    expect(parent.querySelector('.previous')).to.exist;
  });

  it('says it is loading once the wait is long enough to notice', async () => {
    const stop = routeLoadingRenderer.pending({ parent, atOutlet: false });
    await waitUntil(() => !!parent.querySelector('route-loading'), 'no state', {
      timeout: 2000,
    });
    const element = parent.querySelector('route-loading')!;
    await element.updateComplete;
    const status = element.shadowRoot!.querySelector('[role="status"]')!;
    expect(status.getAttribute('aria-live')).to.equal('polite');
    expect(status.textContent!.trim()).to.equal('Loading…');
    // Inside the shell the page padding is already paid for by .main-content.
    expect(element.hasAttribute('standalone')).to.equal(false);
    stop();
    expect(parent.querySelector('route-loading')).to.equal(null);
  });

  it('fades in, and does not move at all under reduced motion (D19)', async () => {
    const stop = routeLoadingRenderer.pending({ parent, atOutlet: false });
    await waitUntil(() => !!parent.querySelector('route-loading'));
    expect(
      getComputedStyle(parent.querySelector('route-loading')!).animationName
    ).to.not.equal('none');
    stop();

    await emulateMedia({ reducedMotion: 'reduce' });
    const stopAgain = routeLoadingRenderer.pending({ parent, atOutlet: false });
    await waitUntil(() => !!parent.querySelector('route-loading'));
    const reduced = getComputedStyle(parent.querySelector('route-loading')!);
    expect(reduced.animationName).to.equal('none');
    expect(reduced.opacity).to.equal('1');
    stopAgain();
  });

  it('explains the failure and offers the reload that can fix it', async () => {
    routeLoadingRenderer.failed(
      { parent, atOutlet: true },
      new Error('chunk unavailable')
    );
    const element = parent.querySelector<RouteLoadError>('route-load-error')!;
    const recover = sinon.spy();
    element.recover = recover;
    await element.updateComplete;
    expect(parent.querySelector('.previous')).to.equal(null);
    const panel = element.shadowRoot!.querySelector('[role="alert"]')!;
    expect(panel.querySelector('h2')!.textContent).to.contain('did not load');
    // One action, and it is the one that works: the browser's module map keeps
    // rejecting a specifier that failed once, so retrying the import in place
    // would only redraw this same panel.
    const buttons = [...panel.querySelectorAll('button')];
    expect(buttons.map((b) => b.textContent!.trim())).to.deep.equal([
      'Reload the page',
    ]);
    buttons[0].click();
    expect(recover.calledOnce).to.equal(true);
    // No shell around it here, so it pays its own page inset.
    expect(element.hasAttribute('standalone')).to.equal(true);
  });
});
