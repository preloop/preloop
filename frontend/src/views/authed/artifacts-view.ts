import { html, css, nothing, unsafeCSS, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  AuthedElement,
  getAccountAgents,
  searchAccountArtifacts,
} from '../../api';
import { PermissionError } from '../../permissions';
import { Router } from '../../router';
import type { ArtifactSearchItem, ArtifactSearchResponse } from '../../types';
import {
  ARTIFACTS_DOCS_HREF,
  ARTIFACT_KIND_ICONS,
  TOOLS_PAGE_HREF,
  artifactKindGroup,
  artifactUnavailableReason,
  formatArtifactBytes,
  orderedArtifactLabels,
} from '../../utils/session-artifacts';
import { formatRelativeTime, parseUTCDate } from '../../utils/date';
import '../../components/view-header';
import '../../components/permission-denied';
import '../../components/browser-step-thumbnail';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import consoleStyles from '../../styles/console-styles.css?inline';

/** Every artifact kind the deposit API accepts, in filter order. */
export const ARTIFACT_KINDS = [
  'screenshot',
  'transcript',
  'document',
  'audio',
  'recording',
  'screencast',
  'generated_file',
  'trace',
] as const;

/** Kinds the gallery can show as images. */
export const IMAGE_KINDS = ['screenshot'];

export const KIND_FILTER_LABELS: Record<string, string> = {
  screenshot: 'Screenshots',
  transcript: 'Transcripts',
  document: 'Documents',
  audio: 'Audio',
  recording: 'Recordings',
  screencast: 'Screencasts',
  generated_file: 'Generated files',
  trace: 'Traces',
};

export type DateRange = '' | '24h' | '7d' | '30d' | 'custom';

const RANGE_MS: Record<string, number> = {
  '24h': 24 * 3600 * 1000,
  '7d': 7 * 24 * 3600 * 1000,
  '30d': 30 * 24 * 3600 * 1000,
};

/** Filters of the Artifacts page; this is exactly what the URL carries. */
export interface ArtifactFilters {
  q: string;
  kinds: string[];
  labels: string[];
  agent: string;
  tool: string;
  range: DateRange;
  from: string;
  to: string;
  held: boolean;
  layout: 'list' | 'gallery';
}

export const EMPTY_FILTERS: ArtifactFilters = {
  q: '',
  kinds: [],
  labels: [],
  agent: '',
  tool: '',
  range: '',
  from: '',
  to: '',
  held: false,
  layout: 'list',
};

/** Read the page filters from a location query string. */
export function filtersFromSearch(search: string): ArtifactFilters {
  const params = new URLSearchParams(search);
  const range = params.get('range') ?? '';
  return {
    q: params.get('q') ?? '',
    kinds: params.getAll('kind').filter(Boolean),
    labels: params.getAll('label').filter((v) => v.includes(':')),
    agent: params.get('agent') ?? '',
    tool: params.get('tool') ?? '',
    range: (['24h', '7d', '30d', 'custom'].includes(range)
      ? range
      : '') as DateRange,
    from: params.get('from') ?? '',
    to: params.get('to') ?? '',
    held: params.get('held') === '1',
    layout: params.get('view') === 'gallery' ? 'gallery' : 'list',
  };
}

/** The page URL query for a filter set (empty values are left out). */
export function filtersToSearch(filters: ArtifactFilters): string {
  const params = new URLSearchParams();
  if (filters.q.trim()) params.set('q', filters.q.trim());
  filters.kinds.forEach((kind) => params.append('kind', kind));
  filters.labels.forEach((label) => params.append('label', label));
  if (filters.agent) params.set('agent', filters.agent);
  if (filters.tool) params.set('tool', filters.tool);
  if (filters.range) params.set('range', filters.range);
  if (filters.range === 'custom') {
    if (filters.from) params.set('from', filters.from);
    if (filters.to) params.set('to', filters.to);
  }
  if (filters.held) params.set('held', '1');
  if (filters.layout === 'gallery') params.set('view', 'gallery');
  const query = params.toString();
  return query ? `?${query}` : '';
}

/** True when any filter narrows the result (the layout is not a filter). */
export function hasActiveFilters(filters: ArtifactFilters): boolean {
  return Boolean(
    filters.q.trim() ||
    filters.kinds.length ||
    filters.labels.length ||
    filters.agent ||
    filters.tool ||
    filters.range ||
    filters.held
  );
}

/**
 * Kinds sent to the API. The gallery shows images only, so it narrows the
 * chosen kinds to image kinds (all image kinds when none is chosen).
 */
export function effectiveKinds(filters: ArtifactFilters): string[] {
  if (filters.layout !== 'gallery') return filters.kinds;
  if (!filters.kinds.length) return [...IMAGE_KINDS];
  return filters.kinds.filter((kind) => IMAGE_KINDS.includes(kind));
}

/** Gallery with kinds chosen, none of them images: nothing to show. */
export function galleryHasNoImageKind(filters: ArtifactFilters): boolean {
  return (
    filters.layout === 'gallery' &&
    filters.kinds.length > 0 &&
    !effectiveKinds(filters).length
  );
}

function isoFromDateInput(value: string, endOfDay: boolean): string | null {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return null;
  if (endOfDay) date.setDate(date.getDate() + 1);
  return date.toISOString();
}

/** Query for `GET /api/v1/artifacts` from the page filters. */
export function searchParamsFor(
  filters: ArtifactFilters,
  options: { now?: number; cursor?: string | null; limit?: number } = {}
): URLSearchParams {
  const params = new URLSearchParams();
  params.set('limit', String(options.limit ?? 50));
  if (filters.q.trim()) params.set('q', filters.q.trim());
  effectiveKinds(filters).forEach((kind) => params.append('kind', kind));
  filters.labels.forEach((label) => params.append('label', label));
  if (filters.agent) params.set('agent_id', filters.agent);
  if (filters.tool) params.set('tool_name', filters.tool);
  if (filters.held) params.set('held', 'true');
  const now = options.now ?? Date.now();
  if (filters.range && RANGE_MS[filters.range]) {
    params.set('from', new Date(now - RANGE_MS[filters.range]).toISOString());
  } else if (filters.range === 'custom') {
    const from = isoFromDateInput(filters.from, false);
    const to = isoFromDateInput(filters.to, true);
    if (from) params.set('from', from);
    if (to) params.set('to', to);
  }
  if (options.cursor) params.set('cursor', options.cursor);
  return params;
}

/** Split an excerpt into text and highlighted runs, by the API's offsets. */
export function excerptParts(
  text: string,
  highlights: Array<[number, number]>
): Array<{ text: string; hit: boolean }> {
  const parts: Array<{ text: string; hit: boolean }> = [];
  let position = 0;
  for (const [start, end] of [...highlights].sort((a, b) => a[0] - b[0])) {
    if (start < position || end <= start || end > text.length) continue;
    if (start > position) {
      parts.push({ text: text.slice(position, start), hit: false });
    }
    parts.push({ text: text.slice(start, end), hit: true });
    position = end;
  }
  if (position < text.length) {
    parts.push({ text: text.slice(position), hit: false });
  }
  return parts;
}

/** Session page URL that lands on this artifact's timeline row (#1083). */
export function artifactSessionHref(item: ArtifactSearchItem): string {
  const params = new URLSearchParams({
    sessionId: item.runtime_session_id,
    artifact: item.id,
  });
  return `/console/runtime-sessions?${params.toString()}`;
}

function formatCue(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const rest = String(total % 60).padStart(2, '0');
  return `${minutes}:${rest}`;
}

/**
 * Account-wide Artifacts page (preloop/preloop#1087).
 *
 * Search and filters live in the URL so a view can be shared. The list is
 * the default layout; the gallery shows image kinds as a grid. A row opens
 * the session at `?artifact=<id>`.
 */
@customElement('artifacts-view')
export class ArtifactsView extends AuthedElement {
  @state() filters: ArtifactFilters = { ...EMPTY_FILTERS };
  @state() private result: ArtifactSearchResponse | null = null;
  @state() private items: ArtifactSearchItem[] = [];
  @state() private loading = true;
  @state() private loadingMore = false;
  @state() private error = '';
  /** A failed Load more; the loaded rows stay on screen. */
  @state() private moreError = '';
  @state() private permissionError: PermissionError | null = null;
  @state() private agents: Array<{ id: string; name: string }> = [];
  @state() private labelDraft = '';

  private requestSeq = 0;
  private qTimer: number | undefined;
  private readonly onPopState = () => {
    this.filters = filtersFromSearch(window.location.search);
    void this.load();
  };

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small) var(--sl-spacing-medium);
        align-items: flex-end;
        margin-bottom: var(--sl-spacing-medium);
      }
      .filters .search {
        flex: 1 1 18rem;
      }
      .filters sl-select,
      .filters sl-input.narrow {
        min-width: 11rem;
      }
      fieldset {
        border: 0;
        margin: 0;
        padding: 0;
      }
      legend,
      .filter-label {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
        margin-bottom: var(--sl-spacing-3x-small);
        padding: 0;
      }
      .kind-filters {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small) var(--sl-spacing-medium);
      }
      .facet-count {
        color: var(--sl-color-neutral-500);
        font-variant-numeric: tabular-nums;
        margin-left: 0.25rem;
      }
      .label-suggestions {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        margin-bottom: var(--sl-spacing-small);
      }
      .label-suggestions .filter-label {
        margin: 0 var(--sl-spacing-2x-small) 0 0;
      }
      button.suggestion {
        font: inherit;
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-primary-700);
        background: var(--sl-color-neutral-0);
        border: 1px dashed var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-pill);
        padding: 0.1rem 0.6rem;
        cursor: pointer;
      }
      button.suggestion:hover,
      button.suggestion:focus-visible {
        border-color: var(--sl-color-primary-500);
        outline: none;
      }
      .active-labels {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
        margin-bottom: var(--sl-spacing-small);
      }
      .toolbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }
      ul.results {
        list-style: none;
        margin: 0;
        padding: 0;
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
      }
      ul.results li + li {
        border-top: 1px solid var(--sl-color-neutral-200);
      }
      a.row {
        display: grid;
        grid-template-columns: 1.5rem minmax(0, 1fr) auto;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-small);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        color: inherit;
        text-decoration: none;
      }
      a.row:hover,
      a.row:focus-visible {
        background: var(--sl-color-neutral-50);
      }
      a.row:focus-visible {
        outline: 2px solid var(--sl-color-primary-500);
        outline-offset: -2px;
      }
      .row .kind-icon {
        font-size: 1.1rem;
        color: var(--sl-color-neutral-600);
        padding-top: 0.15rem;
      }
      .row .name {
        font-weight: var(--sl-font-weight-semibold);
        overflow-wrap: anywhere;
      }
      .row .meta,
      .row .side {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }
      .row .side {
        text-align: right;
        white-space: nowrap;
      }
      .row .excerpt {
        margin-top: var(--sl-spacing-3x-small);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
      }
      .row .excerpt mark {
        background: var(--sl-color-warning-200);
        color: inherit;
        border-radius: 2px;
      }
      .row .labels {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-3x-small);
        margin-top: var(--sl-spacing-3x-small);
      }
      .row.unavailable {
        opacity: 0.6;
      }
      .gallery {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr));
        gap: var(--sl-spacing-medium);
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .gallery a {
        display: block;
        color: inherit;
        text-decoration: none;
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-x-small);
      }
      .gallery a:focus-visible {
        outline: 2px solid var(--sl-color-primary-500);
      }
      .gallery .caption {
        font-size: var(--sl-font-size-small);
        margin-top: var(--sl-spacing-2x-small);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .intro {
        max-width: 44rem;
        margin: var(--sl-spacing-large) auto;
        text-align: left;
        color: var(--sl-color-neutral-700);
      }
      .intro h2 {
        font-size: var(--sl-font-size-large);
        margin: 0 0 var(--sl-spacing-small);
      }
      .intro ol {
        padding-left: 1.25rem;
      }
      .intro li {
        margin-bottom: var(--sl-spacing-small);
      }
      .intro code {
        font-size: 0.9em;
      }
      .no-match {
        text-align: center;
        padding: var(--sl-spacing-x-large) var(--sl-spacing-medium);
        color: var(--sl-color-neutral-600);
      }
      .more {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        justify-content: center;
        margin-top: var(--sl-spacing-medium);
      }
      .error {
        color: var(--sl-color-danger-700);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    this.filters = filtersFromSearch(window.location.search);
    window.addEventListener('popstate', this.onPopState);
    void this.load();
    void this.loadAgents();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener('popstate', this.onPopState);
    window.clearTimeout(this.qTimer);
  }

  private async loadAgents(): Promise<void> {
    try {
      const response = await getAccountAgents({ limit: 100 });
      const rows = Array.isArray(response?.items) ? response.items : [];
      this.agents = rows.map((agent) => ({
        id: String(agent.id),
        name: String(agent.display_name || agent.id),
      }));
    } catch {
      // The agent filter stays empty; every other filter still works.
      this.agents = [];
    }
  }

  async load(): Promise<void> {
    const seq = ++this.requestSeq;
    this.error = '';
    this.moreError = '';
    if (galleryHasNoImageKind(this.filters)) {
      // Nothing the gallery could show; do not fetch every kind instead.
      this.result = null;
      this.items = [];
      this.loading = false;
      return;
    }
    this.loading = true;
    try {
      const page = await searchAccountArtifacts(searchParamsFor(this.filters));
      if (seq !== this.requestSeq) return;
      this.result = page;
      this.items = page.items ?? [];
      this.permissionError = null;
    } catch (error) {
      if (seq !== this.requestSeq) return;
      if (error instanceof PermissionError) {
        this.permissionError = error;
      } else {
        this.error =
          error instanceof Error ? error.message : 'Could not load artifacts.';
      }
      this.result = null;
      this.items = [];
    } finally {
      if (seq === this.requestSeq) this.loading = false;
    }
  }

  private async loadMore(): Promise<void> {
    const cursor = this.result?.next_cursor;
    if (!cursor || this.loadingMore) return;
    const seq = this.requestSeq;
    this.loadingMore = true;
    this.moreError = '';
    try {
      const page = await searchAccountArtifacts(
        searchParamsFor(this.filters, { cursor })
      );
      if (seq !== this.requestSeq) return;
      this.items = [...this.items, ...(page.items ?? [])];
      this.result = { ...page, facets: this.result!.facets };
    } catch (error) {
      if (seq !== this.requestSeq) return;
      this.moreError =
        error instanceof Error ? error.message : 'Could not load more.';
    } finally {
      this.loadingMore = false;
    }
  }

  /** Apply a filter change: update the URL, then reload. */
  applyFilters(next: Partial<ArtifactFilters>): void {
    this.filters = { ...this.filters, ...next };
    const url = `${window.location.pathname}${filtersToSearch(this.filters)}`;
    window.history.replaceState(window.history.state, '', url);
    void this.load();
  }

  private clearFilters(): void {
    this.labelDraft = '';
    this.applyFilters({ ...EMPTY_FILTERS, layout: this.filters.layout });
  }

  private onSearchInput(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    window.clearTimeout(this.qTimer);
    this.qTimer = window.setTimeout(() => this.applyFilters({ q: value }), 300);
  }

  private toggleKind(kind: string, checked: boolean): void {
    const kinds = checked
      ? [...new Set([...this.filters.kinds, kind])]
      : this.filters.kinds.filter((value) => value !== kind);
    this.applyFilters({ kinds });
  }

  private addLabel(raw: string): void {
    const value = raw.trim();
    const [key, ...rest] = value.split(':');
    if (!key || !rest.join(':')) return;
    this.labelDraft = '';
    if (this.filters.labels.includes(value)) return;
    this.applyFilters({ labels: [...this.filters.labels, value] });
  }

  private removeLabel(label: string): void {
    this.applyFilters({
      labels: this.filters.labels.filter((value) => value !== label),
    });
  }

  private setSite(site: string): void {
    const labels = this.filters.labels.filter((v) => !v.startsWith('site:'));
    this.applyFilters({ labels: site ? [...labels, `site:${site}`] : labels });
  }

  private openItem(event: MouseEvent, item: ArtifactSearchItem): void {
    if (
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.button !== 0
    ) {
      return;
    }
    event.preventDefault();
    Router.go(artifactSessionHref(item));
  }

  /** Label suggestions: facet sites first, then labels seen on this page. */
  private labelSuggestions(): string[] {
    const out = new Set<string>();
    Object.keys(this.result?.facets.site ?? {}).forEach((site) =>
      out.add(`site:${site}`)
    );
    this.items.forEach((item) =>
      orderedArtifactLabels(item.labels || {}).forEach(([key, value]) =>
        out.add(`${key}:${value}`)
      )
    );
    return [...out].filter((value) => !this.filters.labels.includes(value));
  }

  /**
   * Labels one click away: facet sites first, then labels on this page.
   * Buttons rather than a datalist, which `sl-input` does not forward.
   */
  private renderLabelSuggestions(): TemplateResult | typeof nothing {
    const suggestions = this.labelSuggestions().slice(0, 8);
    if (!suggestions.length) return nothing;
    return html`<div
      class="label-suggestions"
      role="group"
      aria-label="Suggested labels"
      data-testid="label-suggestions"
    >
      <span class="filter-label">Add label:</span>
      ${suggestions.map(
        (label) =>
          html`<button
            type="button"
            class="suggestion"
            data-label=${label}
            @click=${() => this.addLabel(label)}
          >
            ${label}
          </button>`
      )}
    </div>`;
  }

  private renderFilters(): TemplateResult {
    const facets = this.result?.facets ?? { kind: {}, site: {} };
    const site =
      this.filters.labels.find((v) => v.startsWith('site:'))?.slice(5) ?? '';
    const sites = Object.entries(facets.site).sort((a, b) => b[1] - a[1]);
    if (site && !facets.site[site]) sites.unshift([site, 0]);
    return html`
      <form
        class="filters"
        role="search"
        aria-label="Filter artifacts"
        @submit=${(e: Event) => e.preventDefault()}
      >
        <sl-input
          class="search"
          type="search"
          label="Search"
          placeholder="Text in transcripts and documents, or a name"
          clearable
          .value=${this.filters.q}
          @sl-input=${this.onSearchInput}
          @sl-clear=${() => this.applyFilters({ q: '' })}
          data-testid="artifact-search"
        >
          <sl-icon name="search" slot="prefix"></sl-icon>
        </sl-input>
        <sl-select
          label="Site"
          placeholder="Any site"
          clearable
          .value=${site}
          @sl-change=${(e: Event) =>
            this.setSite(String((e.target as HTMLSelectElement).value || ''))}
          data-testid="site-filter"
        >
          ${sites.map(
            ([value, count]) =>
              html`<sl-option value=${value}
                >${value}<span class="facet-count" slot="suffix"
                  >${count}</span
                ></sl-option
              >`
          )}
        </sl-select>
        <sl-select
          label="Agent"
          placeholder="Any agent"
          clearable
          .value=${this.filters.agent}
          @sl-change=${(e: Event) =>
            this.applyFilters({
              agent: String((e.target as HTMLSelectElement).value || ''),
            })}
          data-testid="agent-filter"
        >
          ${this.agents.map(
            (agent) =>
              html`<sl-option value=${agent.id}>${agent.name}</sl-option>`
          )}
        </sl-select>
        <sl-input
          class="narrow"
          label="Tool"
          placeholder="Any tool"
          clearable
          .value=${this.filters.tool}
          @sl-change=${(e: Event) =>
            this.applyFilters({
              tool: (e.target as HTMLInputElement).value.trim(),
            })}
          data-testid="tool-filter"
        ></sl-input>
        <sl-select
          label="Created"
          .value=${this.filters.range || 'any'}
          @sl-change=${(e: Event) => {
            const value = String((e.target as HTMLSelectElement).value);
            this.applyFilters({
              range: (value === 'any' ? '' : value) as DateRange,
            });
          }}
          data-testid="range-filter"
        >
          <sl-option value="any">Any time</sl-option>
          <sl-option value="24h">Last 24 hours</sl-option>
          <sl-option value="7d">Last 7 days</sl-option>
          <sl-option value="30d">Last 30 days</sl-option>
          <sl-option value="custom">Custom range</sl-option>
        </sl-select>
        ${
          this.filters.range === 'custom'
            ? html`
                <sl-input
                  class="narrow"
                  type="date"
                  label="From"
                  .value=${this.filters.from}
                  @sl-change=${(e: Event) =>
                    this.applyFilters({
                      from: (e.target as HTMLInputElement).value,
                    })}
                  data-testid="from-filter"
                ></sl-input>
                <sl-input
                  class="narrow"
                  type="date"
                  label="To"
                  .value=${this.filters.to}
                  @sl-change=${(e: Event) =>
                    this.applyFilters({
                      to: (e.target as HTMLInputElement).value,
                    })}
                  data-testid="to-filter"
                ></sl-input>
              `
            : nothing
        }
        <sl-input
          class="narrow"
          label="Label"
          placeholder="key:value, then Enter"
          .value=${this.labelDraft}
          @sl-input=${(e: Event) =>
            (this.labelDraft = (e.target as HTMLInputElement).value)}
          @sl-change=${(e: Event) =>
            this.addLabel((e.target as HTMLInputElement).value)}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              this.addLabel(this.labelDraft);
            }
          }}
          title="All labels must match"
          data-testid="label-filter"
        ></sl-input>
        <sl-checkbox
          ?checked=${this.filters.held}
          @sl-change=${(e: Event) =>
            this.applyFilters({
              held: (e.target as HTMLInputElement).checked,
            })}
          data-testid="held-filter"
          >Legal hold only</sl-checkbox
        >
        <fieldset data-testid="kind-filter">
          <legend>Kind</legend>
          <div class="kind-filters">
            ${ARTIFACT_KINDS.map(
              (kind) => html`
                <sl-checkbox
                  size="small"
                  value=${kind}
                  data-kind=${kind}
                  ?checked=${this.filters.kinds.includes(kind)}
                  @sl-change=${(e: Event) =>
                    this.toggleKind(
                      kind,
                      (e.target as HTMLInputElement).checked
                    )}
                  >${KIND_FILTER_LABELS[kind]}<span
                    class="facet-count"
                    data-facet=${kind}
                    >${facets.kind[kind] ?? 0}</span
                  ></sl-checkbox
                >
              `
            )}
          </div>
        </fieldset>
      </form>
      ${this.renderLabelSuggestions()}
      ${
        this.filters.labels.length
          ? html`<div class="active-labels" aria-label="Active label filters">
              ${this.filters.labels.map(
                (label) =>
                  html`<sl-tag
                    size="small"
                    removable
                    data-label=${label}
                    @sl-remove=${() => this.removeLabel(label)}
                    >${label}</sl-tag
                  >`
              )}
            </div>`
          : nothing
      }
    `;
  }

  private renderExcerpt(
    item: ArtifactSearchItem
  ): TemplateResult | typeof nothing {
    if (!item.excerpt?.text) return nothing;
    const parts = excerptParts(
      item.excerpt.text,
      item.excerpt.highlights ?? []
    );
    return html`<div class="excerpt" data-testid="artifact-excerpt">
      ${
        typeof item.cue_start === 'number'
          ? html`<span class="cue">[${formatCue(item.cue_start)}]</span> `
          : nothing
      }${parts.map((part) =>
        part.hit ? html`<mark>${part.text}</mark>` : part.text
      )}
    </div>`;
  }

  private renderRow(item: ArtifactSearchItem): TemplateResult {
    const group = artifactKindGroup(item.kind, item.content_type);
    const created = parseUTCDate(item.created_at);
    const unavailable = item.availability !== 'available';
    const name = item.name || item.id;
    return html`
      <li>
        <a
          class="row ${unavailable ? 'unavailable' : ''}"
          href=${artifactSessionHref(item)}
          data-artifact-id=${item.id}
          @click=${(e: MouseEvent) => this.openItem(e, item)}
        >
          <sl-icon
            class="kind-icon"
            name=${ARTIFACT_KIND_ICONS[group]}
            label=${KIND_FILTER_LABELS[item.kind] ?? item.kind}
          ></sl-icon>
          <div>
            <div class="name">${name}</div>
            <div class="meta">
              ${item.session_title || `Session ${item.runtime_session_id.slice(0, 8)}`}${
                item.agent_name ? html` · ${item.agent_name}` : nothing
              }${item.tool_name ? html` · ${item.tool_name}` : nothing}
            </div>
            ${this.renderExcerpt(item)}
            ${
              unavailable
                ? html`<div class="meta">
                    ${artifactUnavailableReason(item.availability)}
                  </div>`
                : nothing
            }
            <div class="labels">
              ${
                item.legal_hold
                  ? html`<sl-tag size="small" variant="warning"
                      >Legal hold</sl-tag
                    >`
                  : nothing
              }
              ${orderedArtifactLabels(item.labels || {}).map(
                ([key, value]) =>
                  html`<sl-tag size="small">${key}: ${value}</sl-tag>`
              )}
            </div>
          </div>
          <div class="side">
            <div title=${created ? created.toLocaleString() : ''}>
              ${formatRelativeTime(item.created_at)}
            </div>
            <div>${formatArtifactBytes(item.size_bytes)}</div>
          </div>
        </a>
      </li>
    `;
  }

  private renderGallery(): TemplateResult {
    const images = this.items.filter((item) =>
      String(item.content_type || '').startsWith('image/')
    );
    return html`<ul class="gallery" data-testid="artifact-gallery">
      ${images.map(
        (item) =>
          html`<li>
            <a
              href=${artifactSessionHref(item)}
              data-artifact-id=${item.id}
              @click=${(e: MouseEvent) => this.openItem(e, item)}
            >
              <browser-step-thumbnail
                compact
                .sessionId=${item.runtime_session_id}
                .artifactId=${item.id}
                .availability=${item.availability}
                .label=${item.name || 'Screenshot'}
              ></browser-step-thumbnail>
              <div class="caption">${item.name || item.id}</div>
              <div class="caption">
                ${item.session_title || `Session ${item.runtime_session_id.slice(0, 8)}`}
              </div>
            </a>
          </li>`
      )}
    </ul>`;
  }

  /** Empty state (a): the account has no artifacts at all. */
  private renderIntro(): TemplateResult {
    return html`<section class="intro" data-testid="artifacts-intro">
      <h2>No artifacts yet</h2>
      <p>
        Artifacts are the files your agents produce while they work:
        screenshots, transcripts, documents, recordings and traces. Each one is
        stored encrypted on its session, appears on the session timeline, and
        can be searched here across every session.
      </p>
      <p>Three ways to store the first one:</p>
      <ol>
        <li data-path="mcp">
          <strong>MCP tool <code>deposit_artifact</code>.</strong> Turn it on on
          the <a href=${TOOLS_PAGE_HREF}>Tools page</a>; an agent with a session
          key can then hand Preloop a file.
        </li>
        <li data-path="cli">
          <strong>CLI or REST.</strong>
          <code>preloop artifacts put &lt;file&gt; --session &lt;id&gt;</code>
          uploads a file, or
          <code>POST /api/v1/runtime-sessions/{id}/artifacts</code> does the
          same over HTTP.
        </li>
        <li data-path="playwright">
          <strong>Playwright MCP through the firewall.</strong> Screenshots a
          browser agent takes are stored as artifacts automatically.
        </li>
      </ol>
      <p>
        <a href=${ARTIFACTS_DOCS_HREF} target="_blank" rel="noopener"
          >Read the artifacts guide</a
        >
      </p>
    </section>`;
  }

  /** Empty state (b): filters match nothing. */
  private renderNoMatch(): TemplateResult {
    const galleryHasNoImageKind =
      this.filters.layout === 'gallery' && !effectiveKinds(this.filters).length;
    return html`<div class="no-match" data-testid="artifacts-no-match">
      <p>
        ${
          galleryHasNoImageKind
            ? 'The gallery shows images. None of the selected kinds are images.'
            : 'No artifacts match.'
        }
      </p>
      <sl-button
        size="small"
        @click=${() => this.clearFilters()}
        data-testid="clear-filters"
        >Clear filters</sl-button
      >
    </div>`;
  }

  private renderResults(): TemplateResult {
    if (this.loading && !this.result) {
      return html`<div class="empty-state"><sl-spinner></sl-spinner></div>`;
    }
    if (this.error) {
      return html`<div class="empty-state error" role="alert">
        ${this.error}
      </div>`;
    }
    const filtered = hasActiveFilters(this.filters);
    if (!this.items.length || galleryHasNoImageKind(this.filters)) {
      if (!filtered && this.filters.layout === 'list')
        return this.renderIntro();
      return this.renderNoMatch();
    }
    return html`
      ${
        this.filters.layout === 'gallery'
          ? this.renderGallery()
          : html`<ul class="results" data-testid="artifact-list">
              ${this.items.map((item) => this.renderRow(item))}
            </ul>`
      }
      ${
        this.result?.next_cursor
          ? html`<div class="more">
              <sl-button
                size="small"
                ?loading=${this.loadingMore}
                @click=${() => this.loadMore()}
                data-testid="load-more"
                >Load more</sl-button
              >
              ${
                this.moreError
                  ? html`<span
                      class="error"
                      role="alert"
                      data-testid="more-error"
                      >${this.moreError}</span
                    >`
                  : nothing
              }
            </div>`
          : nothing
      }
    `;
  }

  render() {
    if (this.permissionError) {
      return html`<view-header
          headerText="Artifacts"
          width="wide"
        ></view-header>
        <div class="column-layout wide">
          <permission-denied
            required-permission=${
              this.permissionError.requiredPermission || 'view_runtime_sessions'
            }
            message=${this.permissionError.message}
          ></permission-denied>
        </div>`;
    }
    const total = Object.values(this.result?.facets.kind ?? {}).reduce(
      (sum, n) => sum + n,
      0
    );
    return html`
      <view-header
        headerText="Artifacts"
        description="Files your agents stored on their sessions, across every session. Open one to see it on its session timeline."
        width="wide"
      ></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          ${this.renderFilters()}
          <div class="toolbar">
            <span data-testid="artifact-count" aria-live="polite"
              >${
                this.result
                  ? `${total.toLocaleString()}${this.result.facets_truncated ? '+' : ''} artifact${total === 1 ? '' : 's'}`
                  : ''
              }</span
            >
            <sl-radio-group
              size="small"
              label="Layout"
              .value=${this.filters.layout}
              @sl-change=${(e: Event) =>
                this.applyFilters({
                  layout: (e.target as HTMLInputElement).value as
                    'list' | 'gallery',
                })}
              data-testid="layout-toggle"
            >
              <sl-radio-button value="list">List</sl-radio-button>
              <sl-radio-button value="gallery">Gallery</sl-radio-button>
            </sl-radio-group>
          </div>
          ${this.renderResults()}
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'artifacts-view': ArtifactsView;
  }
}
