import { expect, waitUntil, oneEvent, aTimeout } from '@open-wc/testing';
import sinon from 'sinon';
import {
  PENDING_DELAY_MS,
  routeLoadingRenderer,
} from '../components/route-loading';
import {
  Router,
  LOCATION_CHANGED,
  LEGACY_LOCATION_CHANGED,
  flattenRoutes,
  normalizePath,
  type RouterLocation,
} from './index';

/** Custom elements cannot be undefined, so every fixture gets a fresh tag. */
let tagSeq = 0;
function defineTag(prefix: string): string {
  const name = `${prefix}-${++tagSeq}`;
  customElements.define(name, class extends HTMLElement {});
  return name;
}

describe('router', () => {
  let outlet: HTMLElement;
  let router: Router;
  const startUrl = window.location.pathname + window.location.search;

  beforeEach(() => {
    outlet = document.createElement('div');
    document.body.append(outlet);
    router = new Router(outlet);
  });

  afterEach(() => {
    router.unsubscribe();
    outlet.remove();
    window.history.replaceState(null, '', startUrl);
  });

  describe('path compilation', () => {
    it('normalizes leading, trailing and doubled slashes', () => {
      expect(normalizePath('console/agents')).to.equal('/console/agents');
      expect(normalizePath('/console/agents/')).to.equal('/console/agents');
      expect(normalizePath('//console//agents')).to.equal('/console/agents');
      expect(normalizePath('')).to.equal('/');
      expect(normalizePath('/')).to.equal('/');
    });

    it('treats a child path as relative even when it starts with a slash', () => {
      // The console's own table mixes both spellings inside /console.
      const flat = flattenRoutes([
        {
          path: '/console',
          component: 'x-shell',
          children: [
            { path: 'cost', component: 'x-cost' },
            { path: '/agents', component: 'x-agents' },
          ],
        },
      ]);
      const paths = flat.map((entry) => entry.pattern.source);
      expect(paths).to.include('^\\/console\\/cost$');
      expect(paths).to.include('^\\/console\\/agents$');
    });

    it('emits children before their parent so /console is the index child', () => {
      const flat = flattenRoutes([
        {
          path: '/console',
          component: 'x-shell',
          children: [{ path: '', component: 'x-overview' }],
        },
      ]);
      const leafOf = (index: number) =>
        flat[index].chain[flat[index].chain.length - 1].component;
      expect(leafOf(0)).to.equal('x-overview');
      expect(leafOf(1)).to.equal('x-shell');
    });
  });

  describe('matching', () => {
    it('renders the matched component into the outlet', async () => {
      const tag = defineTag('rt-plain');
      await router.setRoutes([{ path: '/plain', component: tag }], true);
      await router.render('/plain');
      expect(outlet.querySelector(tag)).to.exist;
    });

    it('captures and decodes :params, and exposes search', async () => {
      const tag = defineTag('rt-params');
      let seen: RouterLocation | undefined;
      await router.setRoutes(
        [
          {
            path: '/things/:id/parts/:partId',
            action: (context) => {
              seen = context;
            },
            component: tag,
          },
        ],
        true
      );
      await router.render('/things/a%20b/parts/7?q=x');
      expect(seen?.params).to.deep.equal({ id: 'a b', partId: '7' });
      expect(seen?.search).to.equal('?q=x');
      expect(seen?.searchParams.get('q')).to.equal('x');
    });

    it('treats malformed percent-encoding as unmatched, not a throw', async () => {
      const tagged = defineTag('rt-bad-enc');
      const missing = defineTag('rt-bad-enc-missing');
      await router.setRoutes(
        [
          { path: '/agents/:agentId', component: tagged },
          { path: '(.*)', component: missing },
        ],
        true
      );
      await router.render('/agents/%zz');
      expect(outlet.querySelector(missing)).to.exist;
      expect(outlet.querySelector(tagged)).to.equal(null);
    });

    it('matches routes in declaration order so (.*) stays a fallback', async () => {
      const real = defineTag('rt-real');
      const missing = defineTag('rt-missing');
      await router.setRoutes(
        [
          { path: '/real', component: real },
          { path: '(.*)', component: missing },
        ],
        true
      );
      await router.render('/real');
      expect(outlet.querySelector(real)).to.exist;
      await router.render('/nothing/here');
      expect(outlet.querySelector(missing)).to.exist;
    });

    it('sets location on the rendered element', async () => {
      const tag = defineTag('rt-loc');
      await router.setRoutes([{ path: '/loc/:id', component: tag }], true);
      await router.render('/loc/9');
      const element = outlet.querySelector(tag) as HTMLElement & {
        location?: RouterLocation;
      };
      expect(element.location?.params.id).to.equal('9');
      expect(element.location?.pathname).to.equal('/loc/9');
    });
  });

  describe('actions and commands', () => {
    it('renders the element an action returns', async () => {
      const tag = defineTag('rt-action');
      await router.setRoutes(
        [
          {
            path: '/action',
            action: (_context, commands) => {
              const element = commands.component(tag) as HTMLElement & {
                src?: string;
              };
              element.src = '/content/x.md';
              return element;
            },
          },
        ],
        true
      );
      await router.render('/action');
      const element = outlet.querySelector(tag) as HTMLElement & {
        src?: string;
      };
      expect(element.src).to.equal('/content/x.md');
    });

    it('follows commands.redirect from an action', async () => {
      const target = defineTag('rt-redirect-target');
      await router.setRoutes(
        [
          {
            path: '/from',
            action: (_context, commands) => commands.redirect('/to'),
          },
          { path: '/to', component: target },
        ],
        true
      );
      await router.render('/from');
      expect(outlet.querySelector(target)).to.exist;
    });

    it('follows a declarative redirect', async () => {
      const target = defineTag('rt-decl-target');
      await router.setRoutes(
        [
          { path: '/settings', redirect: '/settings/profile' },
          { path: '/settings/profile', component: target },
        ],
        true
      );
      await router.render('/settings');
      expect(outlet.querySelector(target)).to.exist;
    });

    it('connects a nested shell once when a child route redirects', async () => {
      const shell = `rt-nested-redir-shell-${++tagSeq}`;
      let connects = 0;
      customElements.define(
        shell,
        class extends HTMLElement {
          connectedCallback() {
            connects += 1;
          }
        }
      );
      const profile = defineTag('rt-nested-redir-profile');
      await router.setRoutes(
        [
          {
            path: '/console',
            component: shell,
            children: [
              { path: 'settings', redirect: '/console/settings/profile' },
              { path: 'settings/profile', component: profile },
            ],
          },
        ],
        true
      );
      await router.render('/console/settings');
      expect(outlet.querySelector(`${shell} > ${profile}`)).to.exist;
      expect(connects).to.equal(1);
    });

    it('does not mount ancestors when a nested action redirects', async () => {
      const shell = `rt-action-redir-shell-${++tagSeq}`;
      let connects = 0;
      customElements.define(
        shell,
        class extends HTMLElement {
          connectedCallback() {
            connects += 1;
          }
        }
      );
      const target = defineTag('rt-action-redir-target');
      await router.setRoutes(
        [
          {
            path: '/console',
            component: shell,
            children: [
              {
                path: 'runners',
                action: (
                  _context: RouterLocation,
                  commands: { redirect(path: string): unknown }
                ) => commands.redirect('/console/settings/runners'),
              },
              { path: 'settings/runners', component: target },
            ],
          },
        ],
        true
      );
      await router.render('/console/runners');
      expect(outlet.querySelector(`${shell} > ${target}`)).to.exist;
      expect(connects).to.equal(1);
    });

    it('gives a redirected click one stop, and Back is where it was clicked', async () => {
      const origin = defineTag('rt-history-origin');
      const target = defineTag('rt-history-target');
      await router.setRoutes(
        [
          { path: '/hop/from', component: origin },
          { path: '/hop', redirect: '/hop/landed' },
          { path: '/hop/landed', component: target },
        ],
        true
      );
      await router.render('/hop/from', { history: 'push' });
      await router.render('/hop', { history: 'push' });
      expect(window.location.pathname).to.equal('/hop/landed');

      // The URL the redirect passed through never gets a stop of its own, so
      // one Back is the page the link was clicked on: no bounce forward, and
      // no page swallowed either.
      window.history.back();
      await waitUntil(() => !!outlet.querySelector(origin), 'back re-renders', {
        timeout: 2000,
      });
      expect(window.location.pathname).to.equal('/hop/from');
    });
  });

  describe('guards', () => {
    it('runs onBeforeEnter before the element is connected', async () => {
      const tag = `rt-guard-${++tagSeq}`;
      let connectedWhenGuarded: boolean | undefined;
      customElements.define(
        tag,
        class extends HTMLElement {
          onBeforeEnter(location: RouterLocation) {
            connectedWhenGuarded = this.isConnected;
            this.setAttribute('data-id', location.params.id);
          }
        }
      );
      await router.setRoutes([{ path: '/guard/:id', component: tag }], true);
      await router.render('/guard/42');
      expect(connectedWhenGuarded).to.equal(false);
      expect(outlet.querySelector(tag)?.getAttribute('data-id')).to.equal('42');
    });

    it('honours a redirect from onBeforeEnter', async () => {
      const tag = `rt-guard-redirect-${++tagSeq}`;
      const target = defineTag('rt-guard-login');
      customElements.define(
        tag,
        class extends HTMLElement {
          onBeforeEnter(
            _location: RouterLocation,
            commands: { redirect(path: string): unknown }
          ) {
            return commands.redirect('/guard-login');
          }
        }
      );
      await router.setRoutes(
        [
          { path: '/guarded', component: tag },
          { path: '/guard-login', component: target },
        ],
        true
      );
      await router.render('/guarded');
      expect(outlet.querySelector(target)).to.exist;
      expect(outlet.querySelector(tag)).to.equal(null);
    });

    it('runs onBeforeLeave and lets it cancel the navigation', async () => {
      const leaving = `rt-leave-${++tagSeq}`;
      const next = defineTag('rt-leave-next');
      let allow = false;
      customElements.define(
        leaving,
        class extends HTMLElement {
          onBeforeLeave(
            _location: RouterLocation,
            commands: { prevent(): unknown }
          ) {
            return allow ? undefined : commands.prevent();
          }
        }
      );
      await router.setRoutes(
        [
          { path: '/stay', component: leaving },
          { path: '/leave', component: next },
        ],
        true
      );
      await router.render('/stay');
      await router.render('/leave');
      expect(outlet.querySelector(leaving)).to.exist;
      allow = true;
      await router.render('/leave');
      expect(outlet.querySelector(next)).to.exist;
    });

    it('calls onAfterEnter once the view is in the outlet', async () => {
      const tag = `rt-after-${++tagSeq}`;
      let connectedWhenCalled: boolean | undefined;
      customElements.define(
        tag,
        class extends HTMLElement {
          onAfterEnter() {
            connectedWhenCalled = this.isConnected;
          }
        }
      );
      await router.setRoutes([{ path: '/after', component: tag }], true);
      await router.render('/after');
      expect(connectedWhenCalled).to.equal(true);
    });
  });

  describe('nested outlets', () => {
    it('renders the child inside the parent element', async () => {
      const shell = defineTag('rt-shell');
      const child = defineTag('rt-child');
      await router.setRoutes(
        [
          {
            path: '/console',
            component: shell,
            children: [{ path: 'child', component: child }],
          },
        ],
        true
      );
      await router.render('/console/child');
      expect(outlet.querySelector(`${shell} > ${child}`)).to.exist;
    });

    it('keeps the shell alive across sibling navigation', async () => {
      const shell = defineTag('rt-keep-shell');
      const first = defineTag('rt-keep-a');
      const second = defineTag('rt-keep-b');
      await router.setRoutes(
        [
          {
            path: '/console',
            component: shell,
            children: [
              { path: 'a', component: first },
              { path: 'b', component: second },
            ],
          },
        ],
        true
      );
      await router.render('/console/a');
      const shellElement = outlet.querySelector(shell);
      await router.render('/console/b');
      expect(outlet.querySelector(shell)).to.equal(shellElement);
      expect(outlet.querySelector(`${shell} > ${second}`)).to.exist;
      expect(outlet.querySelector(first)).to.equal(null);
    });

    it('resolves the parent path to its index child', async () => {
      const shell = defineTag('rt-index-shell');
      const overview = defineTag('rt-index-overview');
      await router.setRoutes(
        [
          {
            path: '/console',
            component: shell,
            children: [{ path: '', component: overview }],
          },
        ],
        true
      );
      await router.render('/console');
      expect(outlet.querySelector(`${shell} > ${overview}`)).to.exist;
    });

    it('re-enters the same view with new params without rebuilding it', async () => {
      const tag = `rt-same-${++tagSeq}`;
      const seen: string[] = [];
      customElements.define(
        tag,
        class extends HTMLElement {
          onBeforeEnter(location: RouterLocation) {
            seen.push(location.params.id);
          }
        }
      );
      await router.setRoutes([{ path: '/same/:id', component: tag }], true);
      await router.render('/same/1');
      const element = outlet.querySelector(tag);
      await router.render('/same/2');
      expect(outlet.querySelector(tag)).to.equal(element);
      expect(seen).to.deep.equal(['1', '2']);
    });
  });

  // The console groups its flows (and trackers, and issues) under a parent
  // route that has children but no component of its own. That group occupies a
  // level in the chain while owning no element, which is what #499 got wrong.
  describe('component-less group routes', () => {
    /** `/console/flows` and its children, as the console declares them. */
    const groupRoutes = (tags: {
      shell: string;
      list: string;
      create: string;
      detail: string;
      execution: string;
    }) => [
      {
        path: '/console',
        component: tags.shell,
        children: [
          {
            path: 'flows',
            children: [
              { path: '', component: tags.list },
              { path: 'new', component: tags.create },
              {
                path: 'executions/:executionId',
                component: tags.execution,
              },
              { path: ':flowId', component: tags.detail },
            ],
          },
        ],
      },
    ];

    it('does not reuse a leaf element as the group it sits under', async () => {
      const tags = {
        shell: defineTag('rt-group-shell'),
        list: defineTag('rt-group-list'),
        create: defineTag('rt-group-create'),
        detail: defineTag('rt-group-detail'),
        execution: defineTag('rt-group-execution'),
      };
      await router.setRoutes(groupRoutes(tags), true);

      await router.render('/console/flows/executions/e1');
      expect(outlet.querySelector(`${tags.shell} > ${tags.execution}`)).to
        .exist;

      // Back to the list: the execution view must go, not become the group.
      await router.render('/console/flows');
      expect(outlet.querySelector(`${tags.shell} > ${tags.list}`)).to.exist;
      expect(outlet.querySelector(tags.execution)).to.equal(null);

      // And down again: the list must go, not host the execution view.
      await router.render('/console/flows/executions/e1');
      expect(outlet.querySelector(`${tags.shell} > ${tags.execution}`)).to
        .exist;
      expect(outlet.querySelector(tags.list)).to.equal(null);
    });

    it('does not hand a sibling route location to the view it left', async () => {
      const detail = `rt-group-params-detail-${++tagSeq}`;
      const seen: (string | undefined)[] = [];
      customElements.define(
        detail,
        class extends HTMLElement {
          onBeforeEnter(location: RouterLocation) {
            seen.push(location.params.flowId);
          }
        }
      );
      const tags = {
        shell: defineTag('rt-group-params-shell'),
        list: defineTag('rt-group-params-list'),
        create: defineTag('rt-group-params-create'),
        detail,
        execution: defineTag('rt-group-params-execution'),
      };
      await router.setRoutes(groupRoutes(tags), true);

      await router.render('/console/flows/f1');
      await router.render('/console/flows/executions/e1');

      // The flow page must not be re-entered with the execution's params:
      // that is how <flow-view> lost its flowId and drew the create form.
      expect(seen).to.deep.equal(['f1']);
      expect(outlet.querySelector(detail)).to.equal(null);
      expect(outlet.querySelector(`${tags.shell} > ${tags.execution}`)).to
        .exist;
    });

    it('keeps two routes that share a component apart inside a group', async () => {
      const tags = {
        shell: defineTag('rt-group-share-shell'),
        list: defineTag('rt-group-share-list'),
        create: defineTag('rt-group-share-view'),
        detail: defineTag('rt-group-share-view-detail'),
        execution: defineTag('rt-group-share-execution'),
      };
      // `new` and `:flowId` are one component in the console. Reuse across
      // them is fine; hosting one inside the other is not.
      const routes = groupRoutes({ ...tags, detail: tags.create });
      await router.setRoutes(routes, true);

      await router.render('/console/flows/f1');
      await router.render('/console/flows/new');
      expect(outlet.querySelector(`${tags.shell} > ${tags.create}`)).to.exist;
      expect(outlet.querySelectorAll(tags.create).length).to.equal(1);
    });

    it('keeps the shell alive while the group changes underneath it', async () => {
      const tags = {
        shell: defineTag('rt-group-keep-shell'),
        list: defineTag('rt-group-keep-list'),
        create: defineTag('rt-group-keep-create'),
        detail: defineTag('rt-group-keep-detail'),
        execution: defineTag('rt-group-keep-execution'),
      };
      await router.setRoutes(groupRoutes(tags), true);
      await router.render('/console/flows');
      const shellElement = outlet.querySelector(tags.shell);
      await router.render('/console/flows/executions/e1');
      expect(outlet.querySelector(tags.shell)).to.equal(shellElement);
    });
  });

  describe('component loading', () => {
    it('awaits a route load() before creating the element', async () => {
      const tag = `rt-lazy-${++tagSeq}`;
      let release!: () => void;
      const pending = new Promise<void>((resolve) => {
        release = resolve;
      });
      const load = sinon.spy(async () => {
        await pending;
        customElements.define(tag, class extends HTMLElement {});
      });
      await router.setRoutes([{ path: '/lazy', component: tag, load }], true);
      const navigation = router.render('/lazy');
      await waitUntil(() => load.called);
      expect(outlet.querySelector(tag)).to.equal(null);
      release();
      await navigation;
      expect(outlet.querySelector(tag)).to.be.instanceOf(
        customElements.get(tag)!
      );
    });

    it('shows the pending state and then hands the failure to the renderer', async () => {
      const tag = defineTag('rt-fail');
      const load = sinon.stub();
      load.onFirstCall().rejects(new Error('chunk unavailable'));
      load.onSecondCall().resolves();
      const slots: boolean[] = [];
      router.setLoadingRenderer({
        pending: ({ parent, atOutlet }) => {
          slots.push(atOutlet);
          const node = document.createElement('span');
          node.className = 'pending';
          parent.replaceChildren(node);
          return () => node.remove();
        },
        failed: ({ parent }) => {
          const node = document.createElement('span');
          node.className = 'failed';
          parent.replaceChildren(node);
        },
      });
      await router.setRoutes([{ path: '/fail', component: tag, load }], true);
      const errorStub = sinon.stub(console, 'error');
      try {
        await router.render('/fail');
        expect(
          errorStub.calledWith('Failed to load route module', '/fail')
        ).to.equal(true);
      } finally {
        errorStub.restore();
      }
      // The outlet says something rather than going blank, and the view that
      // could not be built is not left half-created.
      expect(outlet.querySelector('.failed')).to.exist;
      expect(outlet.querySelector(tag)).to.equal(null);
      // The rejection is not cached: coming back to the route asks again.
      await router.render('/fail');
      expect(load.calledTwice).to.equal(true);
      expect(outlet.querySelector(tag)).to.exist;
      expect(slots).to.deep.equal([true, true]);
    });

    it('puts the URL on the route that failed, so a reload retries it', async () => {
      const from = defineTag('rt-fail-url-from');
      const tag = defineTag('rt-fail-url');
      const load = sinon.stub().rejects(new Error('chunk unavailable'));
      router.setLoadingRenderer({
        pending: () => () => undefined,
        failed: ({ parent }) => {
          const node = document.createElement('span');
          node.className = 'failed';
          parent.replaceChildren(node);
        },
      });
      await router.setRoutes(
        [
          { path: '/fail-url/from', component: from },
          { path: '/fail-url/to', component: tag, load },
        ],
        true
      );
      await router.render('/fail-url/from', { history: 'push' });
      const errorStub = sinon.stub(console, 'error');
      try {
        await router.render('/fail-url/to', { history: 'push' });
      } finally {
        errorStub.restore();
      }
      // The panel offers a reload, and the panel is about /fail-url/to. A URL
      // left on the previous route would reload the page the operator had
      // already left.
      expect(outlet.querySelector('.failed')).to.exist;
      expect(window.location.pathname).to.equal('/fail-url/to');
    });

    it('loads a module once and reuses it on the next visit', async () => {
      const lazy = defineTag('rt-once');
      const other = defineTag('rt-once-other');
      const load = sinon.spy(async () => undefined);
      await router.setRoutes(
        [
          { path: '/once', component: lazy, load },
          { path: '/other', component: other },
        ],
        true
      );
      await router.render('/once');
      await router.render('/other');
      await router.render('/once');
      expect(load.calledOnce).to.equal(true);
    });

    it('does not let a slow chunk overwrite a newer navigation', async () => {
      const slow = defineTag('rt-slow');
      const fast = defineTag('rt-fast');
      let release!: () => void;
      const pending = new Promise<void>((resolve) => {
        release = resolve;
      });
      const load = sinon.spy(() => pending);
      await router.setRoutes(
        [
          { path: '/slow', component: slow, load },
          { path: '/fast', component: fast },
        ],
        true
      );
      const stale = router.render('/slow');
      await waitUntil(() => load.called);
      await router.render('/fast');
      release();
      await stale;
      expect(outlet.querySelector(fast)).to.exist;
      expect(outlet.querySelector(slow)).to.equal(null);
    });

    it('does not paint a stale load failure over a newer view', async () => {
      const slow = defineTag('rt-stale-fail');
      const fast = defineTag('rt-stale-fail-fast');
      let rejectSlow!: (error: Error) => void;
      const pending = new Promise<void>((_resolve, reject) => {
        rejectSlow = reject;
      });
      const load = sinon.spy(() => pending);
      router.setLoadingRenderer({
        pending: () => () => undefined,
        failed: ({ parent }) => {
          const node = document.createElement('span');
          node.className = 'failed';
          parent.replaceChildren(node);
        },
      });
      await router.setRoutes(
        [
          { path: '/stale-fail', component: slow, load },
          { path: '/stale-ok', component: fast },
        ],
        true
      );
      const first = router.render('/stale-fail');
      await waitUntil(() => load.called);
      await router.render('/stale-ok');
      expect(outlet.querySelector(fast)).to.exist;
      const errorStub = sinon.stub(console, 'error');
      try {
        rejectSlow(new Error('chunk unavailable'));
        await first;
      } finally {
        errorStub.restore();
      }
      expect(outlet.querySelector(fast)).to.exist;
      expect(outlet.querySelector('.failed')).to.equal(null);
      expect(outlet.querySelector(slow)).to.equal(null);
    });

    it('does not let a stale pending timer clobber a newer view', async () => {
      const slow = defineTag('rt-stale-pending');
      const fast = defineTag('rt-stale-pending-fast');
      const load = sinon.spy(() => new Promise<void>(() => undefined));
      router.setLoadingRenderer(routeLoadingRenderer);
      await router.setRoutes(
        [
          { path: '/stale-pending', component: slow, load },
          { path: '/stale-ok', component: fast },
        ],
        true
      );
      void router.render('/stale-pending');
      await waitUntil(() => load.called);
      await router.render('/stale-ok');
      expect(outlet.querySelector(fast)).to.exist;
      await aTimeout(PENDING_DELAY_MS + 50);
      expect(outlet.querySelector(fast)).to.exist;
      expect(outlet.querySelector('route-loading')).to.equal(null);
      expect(outlet.querySelector(slow)).to.equal(null);
    });
  });

  describe('anchor interception', () => {
    /**
     * Click a link nested in a shadow root and report whether the router
     * claimed it. A late listener stops the test runner from actually
     * following whatever the router left alone.
     */
    async function clickThroughShadowRoot(href: string, attrs = '') {
      const host = document.createElement('div');
      const shadow = host.attachShadow({ mode: 'open' });
      shadow.innerHTML = `<a href="${href}" ${attrs}><span>go</span></a>`;
      document.body.append(host);
      let routed = false;
      const stopTheBrowser = (event: Event) => {
        routed = event.defaultPrevented;
        event.preventDefault();
      };
      document.addEventListener('click', stopTheBrowser);
      shadow.querySelector('span')!.dispatchEvent(
        new MouseEvent('click', {
          bubbles: true,
          composed: true,
          cancelable: true,
          button: 0,
        })
      );
      document.removeEventListener('click', stopTheBrowser);
      await new Promise((resolve) => setTimeout(resolve, 0));
      host.remove();
      return routed;
    }

    it('routes an in-app link clicked inside a shadow root', async () => {
      const tag = defineTag('rt-anchor');
      await router.setRoutes([{ path: '/anchor', component: tag }], true);
      expect(await clickThroughShadowRoot('/anchor')).to.equal(true);
      await waitUntil(() => !!outlet.querySelector(tag));
      expect(window.location.pathname).to.equal('/anchor');
    });

    it('leaves external, download, targeted and opted-out links alone', async () => {
      await router.setRoutes([{ path: '(.*)', component: 'div' }], true);
      for (const attrs of ['download', 'target="_blank"', 'router-ignore']) {
        expect(
          await clickThroughShadowRoot('/elsewhere', attrs),
          attrs
        ).to.equal(false);
      }
      expect(await clickThroughShadowRoot('https://example.com/x')).to.equal(
        false
      );
    });

    it('leaves a same-page fragment link to the browser', async () => {
      await router.setRoutes([{ path: '(.*)', component: 'div' }], true);
      expect(
        await clickThroughShadowRoot(`${window.location.pathname}#section`)
      ).to.equal(false);
    });

    it('scrolls a followed link back to the top of the page', async () => {
      const tag = defineTag('rt-scroll');
      const scrollTo = sinon.stub(window, 'scrollTo');
      try {
        await router.setRoutes([{ path: '/scrolled', component: tag }], true);
        await clickThroughShadowRoot('/scrolled');
        expect(scrollTo.calledWith(0, 0)).to.equal(true);
      } finally {
        scrollTo.restore();
      }
    });
  });

  describe('history and events', () => {
    it('Router.go pushes an entry and popstate resolves it back', async () => {
      const first = defineTag('rt-go-a');
      const second = defineTag('rt-go-b');
      await router.setRoutes(
        [
          { path: '/go-a', component: first },
          { path: '/go-b', component: second },
        ],
        true
      );
      await router.render('/go-a', { history: 'push' });
      expect(Router.go('/go-b')).to.equal(true);
      await waitUntil(() => !!outlet.querySelector(second));
      expect(window.location.pathname).to.equal('/go-b');
      window.history.back();
      await waitUntil(() => !!outlet.querySelector(first), 'back re-renders', {
        timeout: 2000,
      });
      expect(window.location.pathname).to.equal('/go-a');
    });

    it('fires the location-changed event with the resolved location', async () => {
      const tag = defineTag('rt-event');
      await router.setRoutes([{ path: '/evented/:id', component: tag }], true);
      const fired = oneEvent(window, LOCATION_CHANGED);
      void router.render('/evented/7');
      const event = (await fired) as CustomEvent<{ location: RouterLocation }>;
      expect(event.detail.location.pathname).to.equal('/evented/7');
      expect(event.detail.location.params.id).to.equal('7');
    });

    it('still fires the legacy vaadin event name for outside listeners', async () => {
      const tag = defineTag('rt-legacy-event');
      await router.setRoutes([{ path: '/legacy', component: tag }], true);
      const fired = oneEvent(window, LEGACY_LOCATION_CHANGED);
      void router.render('/legacy');
      const event = (await fired) as CustomEvent<{ location: RouterLocation }>;
      expect(event.detail.location.pathname).to.equal('/legacy');
    });

    it('Router.go does not fire a window popstate', async () => {
      const first = defineTag('rt-nopop-a');
      const second = defineTag('rt-nopop-b');
      await router.setRoutes(
        [
          { path: '/nopop-a', component: first },
          { path: '/nopop-b', component: second },
        ],
        true
      );
      await router.render('/nopop-a', { history: 'push' });
      let pops = 0;
      const onPop = () => {
        pops += 1;
      };
      window.addEventListener('popstate', onPop);
      try {
        expect(Router.go('/nopop-b')).to.equal(true);
        await waitUntil(() => !!outlet.querySelector(second));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(pops).to.equal(0);
      } finally {
        window.removeEventListener('popstate', onPop);
      }
    });

    it('urlForPath substitutes params and returns an in-app pathname', () => {
      expect(router.urlForPath('/console/flows/executions')).to.equal(
        '/console/flows/executions'
      );
      expect(
        router.urlForPath('/console/agents/:agentId', { agentId: 'a 1' })
      ).to.equal('/console/agents/a%201');
    });

    it('Router.go reports false when no router is listening', async () => {
      router.unsubscribe();
      expect(Router.go('/anywhere')).to.equal(false);
    });
  });
});
