import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { Router } from '../../router';
import './artifacts-view';
import {
  artifactSessionHref,
  excerptParts,
  filtersFromSearch,
  filtersToSearch,
  searchParamsFor,
  EMPTY_FILTERS,
  type ArtifactsView,
} from './artifacts-view';
import type { ArtifactSearchItem } from '../../types';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function item(
  id: string,
  kind: string,
  extra: Partial<ArtifactSearchItem> = {}
): ArtifactSearchItem {
  const contentType =
    kind === 'screenshot'
      ? 'image/png'
      : kind === 'transcript'
        ? 'text/vtt'
        : 'text/markdown';
  return {
    id,
    runtime_session_id: `session-${id}`,
    kind,
    name: `${id}.${kind === 'screenshot' ? 'png' : 'txt'}`,
    content_type: contentType,
    size_bytes: 2048,
    sha256: 'x',
    labels: { site: 'nord' },
    availability: 'available',
    legal_hold: false,
    created_at: '2026-10-03T10:00:00Z',
    session_title: 'Nord late shift',
    agent_name: 'Warehouse agent',
    content_block: {
      type: 'resource_link',
      uri: `/api/v1/runtime-sessions/session-${id}/artifacts/${id}`,
      name: id,
      mimeType: contentType,
      size: 2048,
    },
    ...extra,
  };
}

const ITEMS = [
  item('a1', 'transcript', {
    excerpt: {
      text: 'Damaged pallet at door 5',
      highlights: [
        [0, 7],
        [8, 14],
      ],
    },
    cue_start: 75,
  }),
  item('a2', 'document'),
  item('a3', 'screenshot', { labels: { site: 'sued' } }),
];

describe('artifacts-view', () => {
  let fetchStub: sinon.SinonStub;
  let calls: URL[];
  let respond: (url: URL) => Response;

  function apiCalls(): URL[] {
    return calls.filter((url) => url.pathname === '/api/v1/artifacts');
  }

  function lastQuery(): URLSearchParams {
    return apiCalls().at(-1)!.searchParams;
  }

  function page(items: ArtifactSearchItem[], kind: Record<string, number>) {
    return json({
      items,
      next_cursor: null,
      facets: { kind, site: { nord: 2, sued: 1 } },
      facets_truncated: false,
    });
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    calls = [];
    window.history.replaceState({}, '', '/console/artifacts');
    respond = (url) => {
      const kinds = url.searchParams.getAll('kind');
      const items = kinds.length
        ? ITEMS.filter((i) => kinds.includes(i.kind))
        : ITEMS;
      return page(items, { transcript: 1, document: 1, screenshot: 1 });
    };
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = new URL(
          typeof input === 'string' ? input : input.toString(),
          window.location.origin
        );
        calls.push(url);
        if (url.pathname === '/api/v1/artifacts') return respond(url);
        if (url.pathname === '/api/v1/agents') {
          return json({
            items: [{ id: 'agent-1', display_name: 'Warehouse agent' }],
            total: 1,
          });
        }
        if (url.pathname.includes('/artifacts/')) {
          return new Response(new Blob(['x'], { type: 'image/png' }));
        }
        return json({});
      });
  });

  afterEach(() => {
    fetchStub.restore();
    sinon.restore();
    window.history.replaceState({}, '', '/');
  });

  async function mount(search = ''): Promise<ArtifactsView> {
    window.history.replaceState({}, '', `/console/artifacts${search}`);
    const el = await fixture<ArtifactsView>(
      html`<artifacts-view></artifacts-view>`
    );
    await waitUntil(() => !(el as any).loading, 'artifacts did not load');
    await el.updateComplete;
    return el;
  }

  function $(el: ArtifactsView, selector: string) {
    return el.shadowRoot!.querySelector(selector);
  }

  it('lists rows with kind icon, name, excerpt, labels, session, agent, size', async () => {
    const el = await mount();

    const rows = el.shadowRoot!.querySelectorAll('a.row');
    expect(rows.length).to.equal(3);
    const first = rows[0] as HTMLAnchorElement;
    expect(
      first.querySelector('sl-icon.kind-icon')?.getAttribute('name')
    ).to.equal('chat-square-text');
    const text = first.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('a1.txt');
    expect(text).to.contain('Nord late shift');
    expect(text).to.contain('Warehouse agent');
    expect(text).to.contain('site: nord');
    expect(text).to.contain('2.0 KB');
    expect(text).to.contain('[1:15]');
    const marks = Array.from(first.querySelectorAll('mark')).map(
      (m) => m.textContent
    );
    expect(marks).to.deep.equal(['Damaged', 'pallet']);
  });

  it('shows facet counts next to kind and site filters', async () => {
    const el = await mount();

    const count = (kind: string) =>
      $(el, `[data-facet="${kind}"]`)?.textContent?.trim();
    expect(count('transcript')).to.equal('1');
    expect(count('screenshot')).to.equal('1');
    expect(count('audio')).to.equal('0');
    const sites = Array.from(
      el.shadowRoot!.querySelectorAll('[data-testid="site-filter"] sl-option')
    ).map((o) => [
      o.getAttribute('value'),
      o.querySelector('.facet-count')?.textContent,
    ]);
    expect(sites).to.deep.equal([
      ['nord', '2'],
      ['sued', '1'],
    ]);
    expect($(el, '[data-testid="artifact-count"]')?.textContent).to.contain(
      '3 artifacts'
    );
  });

  it('updates the URL and the results when filters change', async () => {
    const el = await mount();

    el.applyFilters({ kinds: ['document'] });
    await waitUntil(
      () => el.shadowRoot!.querySelectorAll('a.row').length === 1
    );
    expect(window.location.search).to.equal('?kind=document');
    expect(lastQuery().getAll('kind')).to.deep.equal(['document']);

    el.applyFilters({ labels: ['site:nord'], range: '7d', held: true });
    await waitUntil(() => apiCalls().length >= 3);
    const params = new URLSearchParams(window.location.search);
    expect(params.getAll('label')).to.deep.equal(['site:nord']);
    expect(params.get('range')).to.equal('7d');
    expect(params.get('held')).to.equal('1');
    expect(lastQuery().get('held')).to.equal('true');
    const from = Date.parse(lastQuery().get('from')!);
    expect(Date.now() - from).to.be.closeTo(7 * 24 * 3600 * 1000, 60_000);
  });

  it('reads filters from the URL, so the storage card kind link opens filtered', async () => {
    const el = await mount('?kind=screenshot');

    expect(apiCalls()[0].searchParams.getAll('kind')).to.deep.equal([
      'screenshot',
    ]);
    const box = $(el, 'sl-checkbox[data-kind="screenshot"]') as any;
    expect(box.checked).to.equal(true);
    expect(el.shadowRoot!.querySelectorAll('a.row').length).to.equal(1);
  });

  it('gallery shows only image kinds', async () => {
    const el = await mount('?view=gallery');

    expect(lastQuery().getAll('kind')).to.deep.equal(['screenshot']);
    const tiles = el.shadowRoot!.querySelectorAll(
      '[data-testid="artifact-gallery"] a'
    );
    expect(tiles.length).to.equal(1);
    expect(tiles[0].getAttribute('data-artifact-id')).to.equal('a3');
    expect(tiles[0].querySelector('browser-step-thumbnail')).to.exist;
    expect($(el, '[data-testid="artifact-list"]')).to.not.exist;
  });

  it('gallery explains itself when only non-image kinds are selected', async () => {
    const el = await mount('?view=gallery&kind=transcript');

    expect(apiCalls()).to.have.length(1);
    expect($(el, '[data-testid="artifacts-no-match"]')?.textContent).to.contain(
      'The gallery shows images'
    );
  });

  it('opens the session at ?artifact=<id> when a row is clicked', async () => {
    const go = sinon.stub(Router, 'go').returns(true);
    const el = await mount();

    (el.shadowRoot!.querySelector('a.row') as HTMLAnchorElement).click();

    expect(
      go.calledOnceWith(
        '/console/runtime-sessions?sessionId=session-a1&artifact=a1'
      )
    ).to.equal(true);
  });

  it('empty state (a): an account without artifacts sees three paths and the docs', async () => {
    respond = () => page([], {});
    const el = await mount();

    const intro = $(el, '[data-testid="artifacts-intro"]')!;
    expect(intro).to.exist;
    const paths = Array.from(intro.querySelectorAll('li[data-path]')).map(
      (li) => li.getAttribute('data-path')
    );
    expect(paths).to.deep.equal(['mcp', 'cli', 'playwright']);
    expect(intro.textContent).to.contain('deposit_artifact');
    expect(intro.textContent).to.contain('preloop artifacts put');
    expect(intro.textContent).to.contain('Playwright MCP');
    const docs = Array.from(intro.querySelectorAll('a')).map((a) =>
      a.getAttribute('href')
    );
    expect(docs).to.include('https://docs.preloop.ai/guide/artifacts');
    expect($(el, '[data-testid="artifacts-no-match"]')).to.not.exist;
  });

  it('empty state (b): filters match nothing and Clear filters resets them', async () => {
    respond = (url) =>
      url.searchParams.has('q')
        ? page([], {})
        : page(ITEMS, { transcript: 1, document: 1, screenshot: 1 });
    const el = await mount('?q=nothing-here&label=site:mars');

    expect($(el, '[data-testid="artifacts-no-match"]')?.textContent).to.contain(
      'No artifacts match'
    );
    expect($(el, '[data-testid="artifacts-intro"]')).to.not.exist;

    ($(el, '[data-testid="clear-filters"]') as HTMLElement).click();
    await waitUntil(
      () => el.shadowRoot!.querySelectorAll('a.row').length === 3
    );
    expect(window.location.search).to.equal('');
    expect([...lastQuery().keys()]).to.deep.equal(['limit']);
  });

  it('labels every filter control and keeps them keyboard reachable', async () => {
    const el = await mount();

    for (const id of [
      'artifact-search',
      'site-filter',
      'agent-filter',
      'tool-filter',
      'range-filter',
      'label-filter',
    ]) {
      const control = $(el, `[data-testid="${id}"]`) as any;
      expect(control, id).to.exist;
      expect(control.label, `${id} label`).to.be.a('string').and.not.empty;
      expect(control.disabled, `${id} enabled`).to.not.equal(true);
    }
    expect($(el, '[data-testid="kind-filter"] legend')?.textContent).to.equal(
      'Kind'
    );
    expect($(el, 'form[role="search"]')?.getAttribute('aria-label')).to.equal(
      'Filter artifacts'
    );
    const row = el.shadowRoot!.querySelector('a.row') as HTMLAnchorElement;
    expect(row.getAttribute('href')).to.contain('artifact=a1');
    row.focus();
    expect(el.shadowRoot!.activeElement).to.equal(row);
  });
});

describe('artifacts-view helpers', () => {
  it('round-trips filters through the URL', () => {
    const filters = {
      ...EMPTY_FILTERS,
      q: 'damaged pallet',
      kinds: ['transcript', 'document'],
      labels: ['site:nord', 'shift:late'],
      agent: 'agent-1',
      tool: 'transcribe',
      range: 'custom' as const,
      from: '2026-09-27',
      to: '2026-10-03',
      held: true,
      layout: 'gallery' as const,
    };
    expect(filtersFromSearch(filtersToSearch(filters))).to.deep.equal(filters);
    expect(filtersToSearch(EMPTY_FILTERS)).to.equal('');
  });

  it('maps filters to API params, custom range end inclusive', () => {
    const params = searchParamsFor({
      ...EMPTY_FILTERS,
      agent: 'agent-1',
      tool: 'transcribe',
      range: 'custom',
      from: '2026-09-27',
      to: '2026-10-03',
    });
    expect(params.get('agent_id')).to.equal('agent-1');
    expect(params.get('tool_name')).to.equal('transcribe');
    expect(
      Date.parse(params.get('to')!) - Date.parse(params.get('from')!)
    ).to.be.closeTo(7 * 24 * 3600 * 1000, 2 * 3600 * 1000);
  });

  it('splits excerpts by offsets and ignores bad ones', () => {
    expect(
      excerptParts('ab cd', [
        [3, 5],
        [0, 2],
        [1, 9],
      ])
    ).to.deep.equal([
      { text: 'ab', hit: true },
      { text: ' ', hit: false },
      { text: 'cd', hit: true },
    ]);
  });

  it('builds the session deep link', () => {
    expect(artifactSessionHref(ITEMS[0])).to.equal(
      '/console/runtime-sessions?sessionId=session-a1&artifact=a1'
    );
  });
});
