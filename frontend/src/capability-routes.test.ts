import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { Router } from './router';
import {
  CAPABILITY_ROUTES,
  CapabilityRouteGate,
  capabilityRoutesFor,
  isCapabilityPath,
  withLazyRoutes,
} from './lazy-routes';
import {
  capabilitiesOf,
  hasCapability,
  loadCapabilities,
  NO_CAPABILITIES,
  type Capability,
} from './capabilities';
import { mockApi, type MockApi } from './test-helpers/capability-api';

const caps = (...names: Capability[]) => new Set<Capability>(names);

describe('capabilities', () => {
  let api: MockApi | undefined;
  afterEach(() => {
    api?.restore();
    api = undefined;
  });

  it('counts only a literal true', () => {
    const set = capabilitiesOf({
      multi_account: true,
      account_hierarchy: 'true' as unknown as boolean,
      abac_rules: 1 as unknown as boolean,
    });
    expect([...set]).to.eql(['multi_account']);
    expect(hasCapability({ abac_rules: true }, 'abac_rules')).to.equal(true);
    expect(hasCapability(undefined, 'abac_rules')).to.equal(false);
  });

  it('reads the fixture from /features', async () => {
    api = mockApi({ capabilities: ['account_hierarchy'] });
    expect([...(await loadCapabilities())]).to.eql(['account_hierarchy']);
  });

  it('treats an unreadable /features as no capabilities', async () => {
    api = mockApi({
      routes: [{ path: '/api/v1/features', status: 500, body: {} }],
    });
    expect((await loadCapabilities()).size).to.equal(0);
  });
});

describe('capability routes', () => {
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
    window.history.replaceState({}, '', '/');
  });

  it('registers nothing without capabilities', () => {
    expect(capabilityRoutesFor(NO_CAPABILITIES)).to.eql([]);
  });

  it('registers only the routes of the reported capabilities', () => {
    const paths = capabilityRoutesFor(caps('account_hierarchy')).map(
      (r) => r.path
    );
    expect(paths).to.have.members([
      'settings/subaccounts',
      'settings/access-grants',
      'shared/:kind/:resourceId',
    ]);
    expect(capabilityRoutesFor(caps('multi_account', 'abac_rules'))).to.eql([]);
  });

  it('gates chat separately from hierarchy routes', () => {
    expect(
      capabilityRoutesFor(caps('chat_connections')).map((r) => r.path)
    ).to.eql(['settings/chat']);
    expect(
      capabilityRoutesFor(caps('account_hierarchy')).some(
        (r) => r.path === 'settings/chat'
      )
    ).to.equal(false);
    expect(isCapabilityPath('/console/settings/chat')).to.equal(true);
  });

  it('recognises gated paths for deep links', () => {
    expect(isCapabilityPath('/console/settings/subaccounts')).to.equal(true);
    expect(isCapabilityPath('/console/shared/ai_model/m-1')).to.equal(true);
    expect(isCapabilityPath('/console/settings/profile')).to.equal(false);
    expect(isCapabilityPath('/settings/subaccounts')).to.equal(false);
  });

  function consoleTable() {
    return withLazyRoutes(
      [
        {
          path: '/console',
          component: 'cap-test-shell',
          children: [
            { path: '', component: 'cap-test-home' },
            { path: '(.*)', component: 'cap-test-missing' },
          ],
        },
      ],
      {}
    );
  }

  const table = [
    {
      capability: 'account_hierarchy' as const,
      path: 'settings/subaccounts',
      component: 'cap-test-subaccounts',
    },
  ];

  it('adds nothing and fetches no module when the capability is off', async () => {
    const routes = consoleTable();
    await router.setRoutes(routes, true);
    const loader = sinon.spy(async () => undefined);
    const gate = new CapabilityRouteGate(
      router,
      routes[0],
      async () => NO_CAPABILITIES,
      table,
      { 'cap-test-subaccounts': loader }
    );
    expect(await gate.sync()).to.eql([]);
    expect(routes[0].children!.map((r) => r.path)).to.eql(['', '(.*)']);
    expect(
      router.match('/console/settings/subaccounts')?.chain.at(-1)?.component
    ).to.equal('cap-test-missing');
    expect(loader.called).to.equal(false);
  });

  it('adds the route once, keeps the shell, and redraws a page that was not found', async () => {
    window.history.replaceState({}, '', '/console/settings/subaccounts');
    const routes = consoleTable();
    await router.setRoutes(routes);
    const shell = outlet.querySelector('cap-test-shell');
    expect(shell?.querySelector('cap-test-missing')).to.exist;

    const loader = sinon.spy(async () => undefined);
    const gate = new CapabilityRouteGate(
      router,
      routes[0],
      async () => caps('account_hierarchy'),
      table,
      { 'cap-test-subaccounts': loader }
    );
    expect((await gate.sync()).map((r) => r.path)).to.eql([
      'settings/subaccounts',
    ]);
    expect(await gate.sync()).to.eql([]);
    expect(
      routes[0].children!.filter((r) => r.path === 'settings/subaccounts')
    ).to.have.length(1);
    expect(loader.calledOnce).to.equal(true);
    expect(outlet.querySelector('cap-test-shell')).to.equal(shell);
    expect(shell?.querySelector('cap-test-subaccounts')).to.exist;
    expect(shell?.querySelector('cap-test-missing')).to.equal(null);
  });

  it('lists every gated route with a loader', () => {
    for (const route of CAPABILITY_ROUTES) {
      expect(
        capabilityRoutesFor(caps(route.capability)).some(
          (r) => r.path === route.path && typeof r.load === 'function'
        ),
        route.path
      ).to.equal(true);
    }
  });
});
