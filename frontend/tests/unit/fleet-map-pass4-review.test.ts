import { afterEach, describe, expect, it } from 'vitest';
import { page } from 'vitest/browser';
import '@shoelace-style/shoelace/dist/themes/light.css';
import '../../src/components/fleet-map';
import { sampleFleetMap } from '../../src/test-helpers/fleet-map-graph';
import type { FleetCodeActivity, FleetWorkItem } from '../../src/fleet-map-api';

const mounted: HTMLElement[] = [];
async function settle(el: any) {
  await el.updateComplete;
  await new Promise(requestAnimationFrame);
  await el.updateComplete;
  await new Promise((resolve) => setTimeout(resolve, 50));
  await el.updateComplete;
}
async function mount(extra: Record<string, unknown> = {}) {
  const el: any = document.createElement('fleet-map');
  Object.assign(el, {
    graph: sampleFleetMap(),
    instanceAdmin: true,
    reducedMotion: true,
    userId: 'pass4-review',
    positionsScope: 'pass4-review',
    defaultArrangement: 'spatial',
    ...extra,
  });
  el.style.cssText = 'height:700px;width:1400px;min-width:0';
  document.body.style.margin = '0';
  document.body.append(el);
  mounted.push(el);
  await settle(el);
  return el;
}
const root = (el: HTMLElement) => el.shadowRoot!;
const nodeIds = (el: HTMLElement) =>
  [...root(el).querySelectorAll('[data-node]')].map((n) =>
    n.getAttribute('data-node')
  );
afterEach(() => {
  mounted.splice(0).forEach((el) => el.remove());
  localStorage.clear();
});

const at = new Date(Date.now() - 5 * 60_000).toISOString();
function item(key: string, nodeIds: string[]): FleetWorkItem {
  return {
    id: `work:issue:${key}`,
    key,
    kind: 'issue',
    reference: key.slice(key.indexOf('#')),
    repo: 'example/widgets',
    title: `Issue ${key}`,
    url: `https://github.com/${key.replace('#', '/issues/')}`,
    state: 'open',
    last_action: 'updated',
    last_at: at,
    review_state: null,
    labels: [],
    related_issue: null,
    node_ids: nodeIds,
  };
}
function activity(work: FleetWorkItem[], users?: string[]): FleetCodeActivity {
  return {
    active_within_minutes: 60,
    since: at,
    truncated: false,
    counts: {},
    flows: [],
    events: [],
    work,
    relations: [],
    ...(users ? { users } : {}),
  };
}

describe('Fleet Map pass 4 review fixes', () => {
  it('drops work seeded before a Users scope was chosen', async () => {
    await page.viewport(1440, 900);
    const el = await mount({
      activityWindow: '1h',
      codeActivity: activity([
        item('example/widgets#1', ['agent:claude']),
        item('example/widgets#2', ['agent:codex']),
      ]),
    });
    expect(nodeIds(el).filter((id) => id!.startsWith('work:'))).toHaveLength(2);
    // The server answers the scoped request: only Sam's work, and a map it
    // already scoped. The older, unscoped history must not linger.
    el.users = ['user:sam'];
    await settle(el);
    el.graph = { ...sampleFleetMap(), users: ['user:sam'] };
    el.codeActivity = activity(
      [item('example/widgets#2', ['agent:codex'])],
      ['user:sam']
    );
    await settle(el);
    const work = nodeIds(el).filter((id) => id!.startsWith('work:'));
    expect(work.some((id) => id!.includes('#1'))).toBe(false);
    expect(work.some((id) => id!.includes('#2'))).toBe(true);
  });

  it('names models and accounts inside a provider box by their kind', async () => {
    await page.viewport(1440, 900);
    const el = await mount();
    const subtitle = (id: string) =>
      root(el)
        .querySelector(`[data-node="${id}"]`)!
        .textContent!.replace(/\s+/g, ' ')
        .trim();
    // Accounts are hidden by default: OpenAI's box holds one model only.
    expect(subtitle('provider:openai')).toContain('1 model');
    expect(subtitle('provider:openai')).not.toContain('account');
    el.types = new Set([...el.types, 'provider_account']);
    await settle(el);
    expect(subtitle('provider:openai')).toContain('1 model · 2 accounts');
  });

  it('gives the icon-only Lanes and Canvas buttons accessible names', async () => {
    await page.viewport(1440, 900);
    const el = await mount();
    const names = [
      ...root(el).querySelectorAll(
        '[data-testid="fleet-map-arrangement"] sl-button'
      ),
    ].map(
      (button) =>
        button.shadowRoot!.querySelector('button')!.textContent!.trim() ||
        [...button.querySelectorAll('.sr-only')]
          .map((n) => n.textContent!.trim())
          .join('')
    );
    expect(names).toEqual(['Lanes', 'Canvas']);
  });

  it('announces each filter trigger as its name and value', async () => {
    await page.viewport(1440, 900);
    const el = await mount({ activityWindow: '1h' });
    const name = (id: string) => {
      const host = root(el).querySelector(`[data-testid="${id}"]`)!;
      // sl-button renders its slot inside the inner button: the light DOM
      // text is what assistive tech reads; aria-label on the host is lost.
      return host.textContent!.replace(/\s+/g, ' ').trim();
    };
    expect(name('fleet-map-window')).toBe('Window: Last hour');
    expect(name('fleet-map-users')).toBe('Users: All users');
    expect(name('fleet-map-show')).toBe('Show: All resources');
    const shown = root(el).querySelector(
      '[data-testid="fleet-map-window"] .filter-value'
    )!;
    expect(shown.textContent!.trim()).toBe('Last hour');
  });
});
