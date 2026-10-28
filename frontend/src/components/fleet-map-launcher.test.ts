import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import { mockApi, type MockApi } from '../test-helpers/capability-api';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';
import './fleet-map-launcher';
import {
  fleetMapWindowForRange,
  type FleetMapLauncher,
} from './fleet-map-launcher';
import type { FleetMap } from './fleet-map';

describe('fleet-map-launcher', () => {
  let api: MockApi | undefined;
  beforeEach(() => localStorage.setItem('accessToken', 'test-token'));
  afterEach(() => {
    api?.restore();
    api = undefined;
    localStorage.removeItem('accessToken');
    for (const key of Object.keys(localStorage))
      if (key.startsWith('preloop:fleet-map:')) localStorage.removeItem(key);
    document.body.style.overflow = '';
  });

  const mount = (range = 'last-30') =>
    fixture<FleetMapLauncher>(
      html`<fleet-map-launcher
        time-range=${range}
        user-id="l1"
      ></fleet-map-launcher>`
    );
  const button = (el: FleetMapLauncher) =>
    el.shadowRoot!.querySelector<HTMLElement>(
      '[data-testid="fleet-map-open"]'
    )!;
  const map = (el: FleetMapLauncher) =>
    el.shadowRoot!.querySelector<FleetMap>('fleet-map');

  it('maps each page range onto the window the map opens on', () => {
    expect(fleetMapWindowForRange('24h')).to.equal('24h');
    expect(fleetMapWindowForRange('7d')).to.equal('7d');
    expect(fleetMapWindowForRange('30d')).to.equal('30d');
    expect(fleetMapWindowForRange('1y')).to.equal('90d');
    expect(fleetMapWindowForRange('last-24h')).to.equal('24h');
    expect(fleetMapWindowForRange('this-month')).to.equal('30d');
    expect(fleetMapWindowForRange('this-week')).to.equal('7d');
    expect(fleetMapWindowForRange('last-365')).to.equal('90d');
  });

  it('fetches nothing until opened, then opens the map full screen on the page period', async () => {
    api = mockApi({
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    // A window remembered from the Device fleet page does not win.
    localStorage.setItem('preloop:fleet-map:window', '5m');
    const el = await mount('7d');
    expect(button(el).textContent!.trim()).to.equal('Fleet map');
    expect(api.callsTo('/api/v1/fleet/map').length).to.equal(0);
    expect(map(el)).to.equal(null);
    button(el).click();
    await waitUntil(() => map(el)?.graph, 'the map opens');
    const opened = map(el)!;
    expect(opened.fullscreen).to.equal(true);
    expect(opened.activityWindow).to.equal('7d');
    expect(opened.includeIdle).to.equal(false);
    expect(opened.defaultOnboardedOnly).to.equal(true);
    expect(opened.positionsScope).to.equal('inventory');
    expect(opened.defaultArrangement).to.equal('spatial');
    expect(opened.live).to.equal(true);
    expect(api.callsTo('/api/v1/fleet/map')[0].search).to.contain(
      `active_within_minutes=${7 * 24 * 60}`
    );
    expect(document.body.style.overflow).to.equal('hidden');
  });

  for (const [range, minutes] of [
    ['24h', 24 * 60],
    ['7d', 7 * 24 * 60],
    ['30d', 30 * 24 * 60],
    ['1y', 90 * 24 * 60],
  ] as const)
    it(`asks for the ${range} page range as ${minutes} minutes`, async () => {
      api = mockApi({
        routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
      });
      const el = await mount(range);
      button(el).click();
      await waitUntil(() => api!.callsTo('/api/v1/fleet/map').length === 1);
      expect(api.callsTo('/api/v1/fleet/map')[0].search).to.contain(
        `active_within_minutes=${minutes}`
      );
    });

  it('unmounts on Esc and puts focus back on the button', async () => {
    api = mockApi({
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    const el = await mount();
    button(el).click();
    await waitUntil(() => map(el)?.graph, 'the map opens');
    await map(el)!.updateComplete;
    window.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    );
    await waitUntil(() => !map(el), 'the map unmounts');
    await el.updateComplete;
    expect(el.shadowRoot!.activeElement).to.equal(button(el));
    expect(document.body.style.overflow).to.equal('');
    // A change inside the map stayed in the map: the page range is unchanged.
    expect(el.timeRange).to.equal('last-30');
  });

  it('closes from the exit button too', async () => {
    api = mockApi({
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    const el = await mount();
    button(el).click();
    await waitUntil(() => map(el)?.graph, 'the map opens');
    await map(el)!.updateComplete;
    map(el)!
      .shadowRoot!.querySelector<HTMLElement>(
        '[data-testid="fleet-map-fullscreen"]'
      )!
      .click();
    await waitUntil(() => !map(el), 'the map unmounts');
  });

  it('hides the button on a phone but keeps an open map open', async () => {
    api = mockApi({
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    const el = await mount();
    button(el).click();
    await waitUntil(() => map(el)?.graph, 'the map opens');
    await setViewport({ width: 600, height: 800 });
    try {
      expect(getComputedStyle(button(el)).display).to.equal('none');
      expect(map(el)!.getBoundingClientRect().width).to.equal(600);
      el.close();
      await waitUntil(() => !map(el), 'the map unmounts');
      await el.updateComplete;
      expect(document.body.style.overflow).to.equal('');
    } finally {
      await setViewport({ width: 800, height: 600 });
    }
  });

  it('keeps a window chosen inside the map out of the shared default', async () => {
    api = mockApi({
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    localStorage.setItem('preloop:fleet-map:window', '7d');
    const el = await mount();
    button(el).click();
    await waitUntil(() => map(el)?.graph, 'the map opens');
    await map(el)!.updateComplete;
    map(el)!
      .shadowRoot!.querySelector<HTMLElement>('[data-window="5m"]')!
      .click();
    await waitUntil(() => api!.callsTo('/api/v1/fleet/map').length === 2);
    expect(map(el)!.activityWindow).to.equal('5m');
    expect(localStorage.getItem('preloop:fleet-map:window')).to.equal('7d');
  });
});
