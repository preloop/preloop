import { expect, waitUntil } from '@open-wc/testing';
import { Router } from '@vaadin/router';
import sinon from 'sinon';
import { withLazyRoutes } from './lazy-routes';

describe('lazy route modules', () => {
  let outlet: HTMLElement;
  let router: Router;

  beforeEach(() => {
    outlet = document.createElement('div');
    document.body.append(outlet);
    router = new Router(outlet);
  });

  afterEach(() => {
    router.unsubscribe();
    outlet.remove();
  });

  it('loads only matching routes and waits for registration before rendering', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const load = sinon.spy(async () => {
      await pending;
      customElements.define('lazy-test-child', class extends HTMLElement {});
    });
    const unused = sinon.spy(async () => undefined);
    await router.setRoutes(
      withLazyRoutes(
        [
          {
            path: '/parent',
            component: 'lazy-test-parent',
            children: [{ path: '/child', component: 'lazy-test-child' }],
          },
          { path: '/unused', component: 'lazy-test-unused' },
        ],
        { 'lazy-test-child': load, 'lazy-test-unused': unused }
      ),
      true
    );
    expect(load.called).to.equal(false);
    const rendering = router.render('/parent/child');
    await waitUntil(() => load.called);
    expect(outlet.querySelector('lazy-test-child')).to.equal(null);
    release();
    await rendering;
    const child = outlet.querySelector('lazy-test-parent > lazy-test-child');
    expect(child).to.be.instanceOf(customElements.get('lazy-test-child')!);
    expect(unused.called).to.equal(false);
  });

  it('preserves guards and redirects before downloading a protected view', async () => {
    const load = sinon.spy(async () => undefined);
    const guard = sinon.spy(function (
      this: { path: string },
      _context,
      commands
    ) {
      expect(this.path).to.equal('/protected');
      return commands.redirect('/login');
    });
    await router.setRoutes(
      withLazyRoutes(
        [
          {
            path: '/protected',
            component: 'lazy-test-protected',
            action: guard,
          },
          { path: '/login', component: 'lazy-test-login' },
        ],
        { 'lazy-test-protected': load }
      ),
      true
    );
    await router.render('/protected');
    expect(outlet.querySelector('lazy-test-login')).to.exist;
    expect(guard.calledOnce).to.equal(true);
    expect(load.called).to.equal(false);
  });

  it('can retry a failed chunk instead of caching a rejected promise', async () => {
    const load = sinon.stub();
    load.onFirstCall().rejects(new Error('chunk unavailable'));
    load.onSecondCall().resolves();
    await router.setRoutes(
      withLazyRoutes([{ path: '/retry', component: 'lazy-test-retry' }], {
        'lazy-test-retry': load,
      }),
      true
    );
    let failure: unknown;
    try {
      await router.render('/retry');
    } catch (error) {
      failure = error;
    }
    expect(failure).to.be.instanceOf(Error);
    expect(outlet.querySelector('lazy-test-retry')).to.equal(null);
    await router.render('/retry');
    expect(outlet.querySelector('lazy-test-retry')).to.exist;
    expect(load.calledTwice).to.equal(true);
  });

  it('does not replace a newer navigation when an old chunk finishes', async () => {
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const load = sinon.spy(() => pending);
    await router.setRoutes(
      withLazyRoutes(
        [
          { path: '/slow', component: 'lazy-test-slow' },
          { path: '/fast', component: 'lazy-test-fast' },
        ],
        { 'lazy-test-slow': load }
      ),
      true
    );
    const oldNavigation = router.render('/slow');
    await waitUntil(() => load.called);
    await router.render('/fast');
    release();
    await oldNavigation;
    expect(outlet.querySelector('lazy-test-fast')).to.exist;
    expect(outlet.querySelector('lazy-test-slow')).to.equal(null);
  });
});
