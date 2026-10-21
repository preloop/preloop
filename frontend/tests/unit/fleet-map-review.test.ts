import { afterEach, describe, expect, it, vi } from 'vitest';
import { page } from 'vitest/browser';
import '@shoelace-style/shoelace/dist/themes/light.css';
import '../../src/components/fleet-map';
import { sampleFleetMap } from '../../src/test-helpers/fleet-map-graph';

const mounted: HTMLElement[] = [];
async function mount(extra: Record<string, unknown> = {}, width = '100%') {
  const el: any = document.createElement('fleet-map');
  Object.assign(el, {
    graph: sampleFleetMap(),
    instanceAdmin: true,
    reducedMotion: true,
    userId: 'review-user',
    positionsScope: 'review',
    ...extra,
  });
  el.style.cssText = `height:600px;width:${width};min-width:0`;
  document.body.style.margin = '0';
  document.body.append(el);
  mounted.push(el);
  await el.updateComplete;
  await new Promise(requestAnimationFrame);
  await el.updateComplete;
  // The stage size arrives through a ResizeObserver.
  await new Promise((resolve) => setTimeout(resolve, 50));
  await el.updateComplete;
  return el;
}
const root = (el: HTMLElement) => el.shadowRoot!;
afterEach(() => {
  mounted.splice(0).forEach((el) => el.remove());
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('Fleet Map review fixes', () => {
  it('shows an Arrange button in the Spatial toolbar that arranges with Undo', async () => {
    await page.viewport(1440, 900);
    const el = await mount({ defaultArrangement: 'spatial' });
    el.points = { 'user:maya': { x: 1200, y: 900 } };
    await el.updateComplete;
    const button = root(el).querySelector<HTMLElement>(
      '.map-actions [data-testid="fleet-map-arrange-button"]'
    )!;
    expect(button).not.toBeNull();
    expect(button.getBoundingClientRect().width).toBeGreaterThan(0);
    expect(button.textContent).toContain('Arrange');
    button.click();
    await el.updateComplete;
    expect(el.points).toEqual({});
    expect(root(el).querySelector('.reset-notice')!.textContent).toContain(
      'Arranged'
    );
  });

  it('keeps the Arrange button out of Columns and drops its label in a narrow stage', async () => {
    await page.viewport(1440, 900);
    const columns = await mount({ defaultArrangement: 'columns' });
    expect(
      root(columns).querySelector('[data-testid="fleet-map-arrange-button"]')
    ).toBeNull();
    const narrow = await mount({ defaultArrangement: 'spatial' }, '1000px');
    const icon = root(narrow).querySelector(
      'sl-icon-button[data-testid="fleet-map-arrange-button"]'
    );
    expect(icon).not.toBeNull();
    expect(icon!.getAttribute('label')).toBe('Arrange');
  });
});
