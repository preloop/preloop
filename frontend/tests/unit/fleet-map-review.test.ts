import { afterEach, describe, expect, it, vi } from 'vitest';
import { page } from 'vitest/browser';
import '@shoelace-style/shoelace/dist/themes/light.css';
import '../../src/components/fleet-map';
import { sampleFleetMap } from '../../src/test-helpers/fleet-map-graph';
import {
  workActionWords,
  workRelationLabel,
} from '../../src/utils/fleet-map-details';

const mounted: HTMLElement[] = [];
async function mount(
  extra: Record<string, unknown> = {},
  width = '100%',
  height = '600px'
) {
  const el: any = document.createElement('fleet-map');
  Object.assign(el, {
    graph: sampleFleetMap(),
    instanceAdmin: true,
    reducedMotion: true,
    userId: 'review-user',
    positionsScope: 'review',
    ...extra,
  });
  el.style.cssText = `height:${height};width:${width};min-width:0`;
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
  it('puts an icon-only Arrange beside Fit that arranges with Undo (pass 4)', async () => {
    await page.viewport(1440, 900);
    const el = await mount({ defaultArrangement: 'spatial' });
    el.points = { 'user:maya': { x: 1200, y: 900 } };
    await el.updateComplete;
    expect(
      root(el).querySelector(
        '.map-actions [data-testid="fleet-map-arrange-button"]'
      )
    ).toBeNull();
    const button = root(el).querySelector<HTMLElement>(
      '.canvas-tools [data-testid="fleet-map-arrange-button"]'
    )!;
    expect(button).not.toBeNull();
    expect(button.getBoundingClientRect().width).toBeGreaterThan(0);
    expect(button.getAttribute('aria-label')).toBe('Arrange');
    expect(button.textContent!.trim()).toBe('');
    button.click();
    await el.updateComplete;
    expect(el.points).toEqual({});
    expect(root(el).querySelector('.reset-notice')!.textContent).toContain(
      'Arranged'
    );
  });

  it('keeps Arrange out of Columns and icon-only on a narrow stage', async () => {
    await page.viewport(1440, 900);
    const columns = await mount({ defaultArrangement: 'columns' });
    expect(
      root(columns).querySelector('[data-testid="fleet-map-arrange-button"]')
    ).toBeNull();
    const narrow = await mount({ defaultArrangement: 'spatial' }, '1000px');
    const icon = root(narrow).querySelector(
      '.canvas-tools [data-testid="fleet-map-arrange-button"]'
    );
    expect(icon).not.toBeNull();
    expect(icon!.getAttribute('aria-label')).toBe('Arrange');
  });

  it('folds the legend behind its button where it would run under the zoom controls', async () => {
    await page.viewport(1024, 900);
    const el = await mount({ defaultArrangement: 'spatial' }, '752px', '800px');
    const legend = root(el).querySelector<HTMLElement>('.legend')!;
    const tools = root(el)
      .querySelector('.canvas-tools')!
      .getBoundingClientRect();
    const shown = !legend.hidden && legend.getBoundingClientRect().width > 0;
    const box = legend.getBoundingClientRect();
    const overlaps =
      shown &&
      box.left < tools.right &&
      tools.left < box.right &&
      box.top < tools.bottom &&
      tools.top < box.bottom;
    expect(overlaps).toBe(false);
    const toggle = root(el).querySelector<HTMLElement>('.legend-toggle')!;
    expect(toggle).not.toBeNull();
    toggle.click();
    await el.updateComplete;
    const open = legend.getBoundingClientRect();
    expect(legend.hidden).toBe(false);
    expect(open.bottom).toBeLessThanOrEqual(tools.top);
  });

  it('keeps the legend inline on a wide stage', async () => {
    await page.viewport(1440, 900);
    const el = await mount(
      { defaultArrangement: 'spatial' },
      '1168px',
      '800px'
    );
    expect(root(el).querySelector<HTMLElement>('.legend')!.hidden).toBe(false);
    expect(root(el).querySelector('.legend-toggle')).toBeNull();
  });

  it('words server action keys and counts one action in the singular', () => {
    expect(
      workActionWords(['issue_updated', 'commented', 'labeled', 'commented'])
    ).toEqual(['updated', 'commented', 'labelled']);
    expect(workActionWords(['pull_request_updated', 'issue_opened'])).toEqual([
      'updated',
      'opened',
    ]);
  });

  it('shows an issue only the window numbers that apply to it', async () => {
    await page.viewport(1440, 900);
    const at = new Date().toISOString();
    const issue = {
      id: 'work:issue:example/widgets#8',
      key: 'example/widgets#8',
      kind: 'issue',
      reference: '#8',
      title: 'Document export limits',
      repo: 'example/widgets',
      url: 'https://example.com/example/widgets/issues/8',
      state: 'open',
      last_action: 'commented',
      last_at: at,
      review_state: null,
      labels: [],
      related_issue: null,
      node_ids: ['agent:codex'],
    };
    const codeActivity = {
      active_within_minutes: 60,
      since: at,
      truncated: false,
      counts: {},
      events: [],
      flows: [],
      work: [issue],
      relations: [
        {
          id: `touched:agent:codex:${issue.id}`,
          kind: 'touched',
          source: 'agent:codex',
          target: issue.id,
          actions: ['issue_updated'],
          count: 1,
          at,
          provenance: 'tool_call',
        },
      ],
    };
    const usage = {
      active_within_minutes: 60,
      since: at,
      until: at,
      partial: false,
      truncated: false,
      models: {},
      providers: {},
      work: {
        [issue.id]: {
          request_count: 0,
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
          cache_write_tokens: null,
          estimated_cost: null,
          cost_is_estimate: true,
          currency: null,
          partial: false,
          tool_calls: 2,
          approvals_requested: 0,
          approvals_approved: 0,
          approvals_declined: 0,
          prs_opened: 0,
          prs_reviewed: 0,
          prs_merged: 0,
          review_comments: 0,
          issues_touched: 0,
          prs_touched: 0,
        },
      },
    };
    const el = await mount({ codeActivity, usage });
    const chip = root(el).querySelector(`[data-node="${issue.id}"]`)!;
    chip.dispatchEvent(new MouseEvent('click'));
    await el.updateComplete;
    const block = root(el).querySelector('[data-testid="fleet-map-usage"]')!;
    const labels = [...block.querySelectorAll('dt')].map((dt) =>
      dt.textContent?.trim()
    );
    expect(labels).toEqual(['Requests', 'Tool calls']);
    const who = root(el)
      .querySelector('[data-testid="fleet-map-work-details"]')!
      .textContent!.replace(/\s+/g, ' ');
    expect(who).toContain('1 action · updated');
    expect(who).not.toContain('issue_updated');
    const panel = root(el).querySelector('.panel')!.textContent!;
    expect(panel.split('Document export limits').length - 1).toBe(1);
  });

  it('stacks spatial shelves so a narrow card shows every node at a legible scale', async () => {
    await page.viewport(1024, 900);
    const graph = sampleFleetMap();
    const base = graph.nodes.find((n) => n.id === 'user:sam')!;
    for (let i = 0; i < 4; i++) {
      graph.nodes.push(
        {
          ...base,
          id: `user:extra${i}`,
          resource_id: `extra${i}`,
          label: `Person ${i}`,
        },
        {
          ...base,
          id: `device:extra${i}`,
          type: 'device',
          resource_id: `extra${i}`,
          label: `laptop-${i}`,
          owner_ids: [`user:extra${i}`],
        }
      );
      graph.edges.push({
        id: `owns:user:extra${i}:device:extra${i}:observed`,
        source: `user:extra${i}`,
        target: `device:extra${i}`,
        kind: 'owns',
        provenance: 'observed',
        impact_source: `device:extra${i}`,
        impact_target: `user:extra${i}`,
        gaps: [],
      });
    }
    const el = await mount(
      { graph, defaultArrangement: 'spatial' },
      '752px',
      '660px'
    );
    el.fit();
    await el.updateComplete;
    const stage = root(el).querySelector('.stage')!.getBoundingClientRect();
    const outside = [...root(el).querySelectorAll('[data-node]')]
      .filter((node) => {
        const box = node.getBoundingClientRect();
        return (
          box.width > 0 &&
          (box.left < stage.left - 1 ||
            box.right > stage.right + 1 ||
            box.top < stage.top - 1 ||
            box.bottom > stage.bottom + 1)
        );
      })
      .map((node) => node.getAttribute('data-node'));
    expect(el.scale).toBeGreaterThanOrEqual(0.85);
    expect(outside).toEqual([]);
  });

  it('labels label and unlabel actions on a touched relation', () => {
    const edge: any = {
      id: 'touched:agent:a:work:issue:x#7',
      source: 'agent:a',
      target: 'work:issue:x#7',
      kind: 'touched',
      provenance: 'observed',
      impact_source: 'work:issue:x#7',
      impact_target: 'agent:a',
      gaps: [],
      actions: ['issue_labeled', 'issue_unlabeled'],
    };
    expect(workRelationLabel(edge, { work_reference: '#7' } as any)).toBe(
      'labelled, unlabelled #7'
    );
  });

  it('opens the folded legend on a phone', async () => {
    await page.viewport(390, 844);
    const el = await mount({ defaultArrangement: 'spatial' }, '366px', '600px');
    const toggle = root(el).querySelector<HTMLElement>('.legend-toggle')!;
    expect(toggle).not.toBeNull();
    toggle.click();
    await el.updateComplete;
    const box = root(el).querySelector('.legend')!.getBoundingClientRect();
    expect(box.width).toBeGreaterThan(0);
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(390);
  });
});
