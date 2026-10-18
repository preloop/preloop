import { LitElement, css, html, nothing, svg, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import {
  FLEET_MAP_COLUMNS,
  FLEET_MAP_COLUMN_LABEL,
  FLEET_MAP_EDGE_LABEL,
  FLEET_MAP_NODE_HEIGHT,
  FLEET_MAP_NODE_WIDTH,
  FLEET_MAP_TYPE_LABEL,
  blastRadius,
  filterGraph,
  layoutGraph,
  nodeColumn,
  nodeGaps,
  type FleetMapGapCode,
  type FleetMapGraph,
  type FleetMapNode,
  type FleetMapNodeType,
} from '../fleet-map-api';
import consoleStyles from '../styles/console-styles.css?inline';

const ICON: Record<FleetMapNodeType, string> = {
  user: 'person',
  device: 'laptop',
  runner: 'hdd-rack',
  agent: 'robot',
  model: 'stars',
  provider_account: 'key',
  provider: 'cloud',
  cluster: 'collection',
};

/**
 * The fleet as one picture: people, their machines, the agents on them and
 * the models, provider accounts and providers those agents reach. Selecting
 * a node lights its blast radius and lists it beside the map; everything
 * else dims. Gaps (ungoverned agents, offline runners, accounts that bypass
 * the gateway, open findings) are marked on the node itself.
 */
@customElement('fleet-map')
export class FleetMap extends LitElement {
  @property({ attribute: false }) graph: FleetMapGraph | null = null;
  @state() private selected = '';
  @state() private types = new Set<FleetMapNodeType>(FLEET_MAP_COLUMNS);
  @state() private owner = '';
  @state() private provider = '';
  @state() private focusGap: FleetMapGapCode | '' = '';

  /** Whether a node or a gap is selected, which Esc would clear. */
  hasSelection(): boolean {
    return Boolean(this.selected || this.focusGap);
  }

  /** Clear the node and gap selection, as Esc inside the canvas does. */
  clearSelection(): void {
    this.selected = '';
    this.focusGap = '';
  }

  private scale = 1;
  private translateX = 24;
  private translateY = 24;
  private pan: { id: number; x: number; y: number } | null = null;

  private viewCache?: {
    graph: FleetMapGraph | null;
    types: Set<FleetMapNodeType>;
    owner: string;
    provider: string;
    view: FleetMapGraph;
    layout: ReturnType<typeof layoutGraph>;
  };

  /** Gestures change only the transform and zoom label, never graph templates. */
  private applyTransform(): void {
    const plane = this.shadowRoot?.querySelector<HTMLElement>('.plane');
    if (plane)
      plane.style.transform = `translate(${this.translateX}px,${this.translateY}px) scale(${this.scale})`;
    const zoom = this.shadowRoot?.querySelector('.zoom');
    if (zoom) zoom.textContent = `${Math.round(this.scale * 100)}%`;
  }

  protected updated(): void {
    this.applyTransform();
  }

  // The Agents canvas uses the same transformed Lit nodes, SVG links and
  // pointer-centred zoom. Keep the map keyboard-scrollable via its controls.
  private zoom(factor: number, clientX?: number, clientY?: number): void {
    const bounds = this.shadowRoot
      ?.querySelector('.stage')
      ?.getBoundingClientRect();
    if (!bounds) return;
    const x = clientX === undefined ? bounds.width / 2 : clientX - bounds.left;
    const y = clientY === undefined ? bounds.height / 2 : clientY - bounds.top;
    const next = Math.max(0.15, Math.min(2, this.scale * factor));
    this.translateX = x - ((x - this.translateX) * next) / this.scale;
    this.translateY = y - ((y - this.translateY) * next) / this.scale;
    this.scale = next;
    this.applyTransform();
  }

  private fit(): void {
    const bounds = this.shadowRoot
      ?.querySelector('.stage')
      ?.getBoundingClientRect();
    const layout = this.layout();
    if (!bounds || !layout.width) return;
    this.scale = Math.max(
      0.15,
      Math.min(
        1,
        (bounds.width - 48) / layout.width,
        (bounds.height - 96) / layout.height
      )
    );
    this.translateX = (bounds.width - layout.width * this.scale) / 2;
    this.translateY = Math.max(
      56,
      (bounds.height - layout.height * this.scale) / 2
    );
    this.applyTransform();
  }

  private pointerDown(event: PointerEvent): void {
    if (event.button !== 0 || (event.target as Element).closest('button'))
      return;
    this.pan = {
      id: event.pointerId,
      x: event.clientX - this.translateX,
      y: event.clientY - this.translateY,
    };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  private pointerMove(event: PointerEvent): void {
    if (this.pan?.id !== event.pointerId) return;
    this.translateX = event.clientX - this.pan.x;
    this.translateY = event.clientY - this.pan.y;
    this.applyTransform();
  }

  private pointerUp(event: PointerEvent): void {
    if (this.pan?.id !== event.pointerId) return;
    this.pan = null;
    (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
  }

  protected firstUpdated(): void {
    this.fit();
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small) var(--sl-spacing-large);
        align-items: center;
        margin-bottom: var(--sl-spacing-small);
      }
      .filters label {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .filters select {
        font: inherit;
        font-size: var(--console-text-body);
        color: var(--console-body-color);
        background: var(--console-button-face);
        border: 1px solid var(--console-button-border);
        border-radius: var(--sl-border-radius-medium);
        padding: 4px 8px;
        min-height: 32px;
      }
      .type-toggles {
        display: inline-flex;
        flex-wrap: wrap;
        gap: 4px;
        border: 0;
        margin: 0;
        padding: 0;
      }
      .type-toggles button,
      .gap-chip {
        font: inherit;
        font-size: var(--console-text-meta);
        font-weight: 500;
        border: 1px solid var(--console-hairline);
        background: transparent;
        color: var(--console-meta-color);
        border-radius: 999px;
        padding: 3px 12px;
        min-height: 28px;
        cursor: pointer;
      }
      .type-toggles button[aria-pressed='true'] {
        color: var(--console-body-color);
        border-color: var(--sl-color-neutral-400);
      }
      .type-toggles button:hover,
      .gap-chip:hover {
        background: var(--console-hover-tint);
      }
      .count {
        margin-left: auto;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
      }
      .gaps {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px 8px;
        margin-bottom: var(--sl-spacing-small);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }
      .gaps .title {
        font-weight: 600;
        color: var(--console-body-color);
        margin-right: 4px;
      }
      .gaps .title sl-icon {
        color: var(--sl-color-warning-600);
        vertical-align: -2px;
        margin-right: 4px;
      }
      .gap-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--console-body-color);
        border-color: color-mix(
          in srgb,
          var(--sl-color-warning-500) 40%,
          transparent
        );
      }
      .gap-chip strong {
        font-variant-numeric: tabular-nums;
        color: var(--sl-color-warning-700);
      }
      .gap-chip[aria-pressed='true'] {
        background: color-mix(
          in srgb,
          var(--sl-color-warning-500) 16%,
          transparent
        );
        border-color: var(--sl-color-warning-600);
      }
      .stage {
        position: relative;
        overflow: hidden;
        touch-action: none;
        cursor: grab;
        height: var(--fleet-map-stage-height, max(480px, calc(100vh - 340px)));
        background: var(--console-page);
        border: 1px solid var(--console-hairline);
        border-radius: var(--console-card-radius);
        background-image: radial-gradient(
          color-mix(in srgb, var(--sl-color-neutral-500) 22%, transparent) 1px,
          transparent 1px
        );
        background-size: 24px 24px;
      }
      .stage:active {
        cursor: grabbing;
      }
      .stage:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }
      .plane {
        position: relative;
        transform-origin: 0 0;
      }
      .plane svg {
        position: absolute;
        inset: 0;
        overflow: visible;
        pointer-events: none;
      }
      .edge {
        fill: none;
        stroke: color-mix(
          in srgb,
          var(--sl-color-neutral-500) 70%,
          transparent
        );
        stroke-width: 1.25;
        transition:
          opacity 150ms,
          stroke 150ms;
      }
      .edge.gap {
        stroke: var(--sl-color-warning-600);
      }
      .edge.observed {
        stroke-dasharray: 4 4;
      }
      .edge.lit {
        stroke: var(--sl-color-primary-600);
        stroke-width: 2;
      }
      .edge.dim {
        opacity: 0.12;
      }
      .column-title {
        position: absolute;
        top: 0;
        width: ${FLEET_MAP_NODE_WIDTH}px;
        font-size: var(--console-text-eyebrow);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--console-meta-color);
      }
      .column-title .n {
        font-weight: 500;
        margin-left: 4px;
        font-variant-numeric: tabular-nums;
      }
      .node {
        position: absolute;
        box-sizing: border-box;
        display: grid;
        grid-template-columns: 30px minmax(0, 1fr) auto;
        align-items: center;
        gap: 8px;
        padding: 6px 10px 6px 8px;
        border-radius: var(--sl-border-radius-large);
        border: 1px solid var(--console-hairline);
        background: var(--console-surface);
        cursor: pointer;
        text-align: left;
        font: inherit;
        color: inherit;
        transition:
          opacity 150ms,
          border-color 150ms;
      }
      .node:hover {
        border-color: var(--sl-color-neutral-400);
      }
      .node:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }
      .node.has-gap {
        border-color: color-mix(
          in srgb,
          var(--sl-color-warning-500) 45%,
          transparent
        );
      }
      .node.selected {
        border-color: var(--sl-color-primary-600);
        box-shadow: 0 0 0 1px var(--sl-color-primary-600);
      }
      .node.focus {
        border-color: var(--sl-color-warning-600);
        box-shadow: 0 0 0 1px var(--sl-color-warning-600);
      }
      .node.dim {
        opacity: 0.28;
      }
      .node.cluster {
        border-style: dashed;
      }
      .node .glyph {
        width: 30px;
        height: 30px;
        border-radius: 50%;
        display: grid;
        place-items: center;
        background: color-mix(
          in srgb,
          var(--sl-color-neutral-500) 14%,
          transparent
        );
        color: var(--console-body-color);
        font-size: 15px;
      }
      .node.selected .glyph,
      .node.lit .glyph {
        background: color-mix(
          in srgb,
          var(--sl-color-primary-500) 16%,
          transparent
        );
        color: var(--sl-color-primary-700);
      }
      .node .text {
        display: flex;
        flex-direction: column;
        min-width: 0;
        line-height: 1.3;
      }
      .node .label {
        font-size: var(--console-text-body);
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .node .sub {
        font-size: 12px;
        color: var(--console-meta-color);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .node .sub.warn {
        color: var(--sl-color-warning-700);
      }
      .gap-dot {
        display: grid;
        place-items: center;
        font-size: 14px;
        color: var(--sl-color-warning-600);
      }
      .overlay {
        position: absolute;
        display: flex;
        gap: 4px;
        align-items: center;
        z-index: 2;
      }
      .canvas-tools {
        top: 12px;
        right: 12px;
      }
      .stage.has-selection .canvas-tools {
        right: 344px;
      }
      .canvas-tools button {
        font: inherit;
        font-size: var(--console-text-meta);
        color: var(--console-body-color);
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-medium);
        min-width: 32px;
        min-height: 32px;
        padding: 0 10px;
        cursor: pointer;
      }
      .canvas-tools .zoom {
        min-width: 44px;
        text-align: center;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        font-variant-numeric: tabular-nums;
      }
      .legend {
        left: 12px;
        bottom: 10px;
        gap: var(--sl-spacing-medium);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        pointer-events: none;
      }
      .legend span {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .legend svg {
        width: 24px;
        height: 8px;
      }
      .panel {
        position: absolute;
        top: 12px;
        right: 12px;
        bottom: 12px;
        width: 320px;
        z-index: 3;
        box-sizing: border-box;
        background: var(--console-surface-raised, var(--console-surface));
        border: 1px solid var(--console-hairline);
        box-shadow: var(--sl-shadow-large);
        border-radius: var(--console-card-radius);
        padding: var(--sl-spacing-medium) var(--sl-spacing-large);
        overflow: auto;
        cursor: auto;
      }
      .panel h3 {
        margin: 2px 0 0;
        font-size: var(--console-text-card-title);
        padding-right: 24px;
      }
      .panel .eyebrow {
        font-size: var(--console-text-eyebrow);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--console-meta-color);
      }
      .panel h4 {
        margin: var(--sl-spacing-large) 0 2px;
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }
      .panel .group {
        margin: var(--sl-spacing-small) 0 0;
        font-size: var(--console-text-eyebrow);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--console-meta-color);
      }
      .panel ul {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .panel li {
        padding: 6px 0;
        border-top: 1px solid var(--console-hairline);
        font-size: var(--console-text-body);
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 4px 8px;
      }
      .panel li:first-child {
        border-top: 0;
      }
      .panel li .act {
        font-size: var(--console-text-meta);
      }
      .panel a {
        color: var(--console-link-color);
      }
      .panel .meta {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        margin: 4px 0 0;
      }
      .panel .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: var(--sl-spacing-small);
      }
      .panel .actions {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-top: var(--sl-spacing-small);
        font-size: var(--console-text-body);
      }
      .panel .reach {
        font-size: var(--console-text-body);
        margin: 2px 0 0;
      }
      .close {
        position: absolute;
        top: 8px;
        right: 8px;
        background: none;
        border: 0;
        cursor: pointer;
        color: var(--console-meta-color);
        font-size: 18px;
        width: 32px;
        height: 32px;
      }
      .notice {
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        margin: 0 0 var(--sl-spacing-small);
      }
      .empty-state {
        min-height: 72px;
        height: 100%;
        display: grid;
        place-items: center;
        text-align: center;
        color: var(--console-meta-color);
      }
      @media (max-width: 900px) {
        .stage.has-selection .canvas-tools {
          right: 12px;
        }
        .panel {
          left: 12px;
          width: auto;
          top: auto;
          max-height: 60%;
        }
        .legend {
          display: none;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .edge,
        .node {
          transition: none;
        }
      }
    `,
  ];

  /** Filters cannot leave a hidden selection or gap focus dimming the canvas. */
  willUpdate(): void {
    const visible = this.visible().nodes;
    if (this.selected && !visible.some((node) => node.id === this.selected))
      this.selected = '';
    if (
      this.focusGap &&
      !visible.some((node) =>
        node.gaps.includes(this.focusGap as FleetMapGapCode)
      )
    )
      this.focusGap = '';
  }

  private visible(): FleetMapGraph {
    const cached = this.viewCache;
    if (
      cached &&
      cached.graph === this.graph &&
      cached.types === this.types &&
      cached.owner === this.owner &&
      cached.provider === this.provider
    )
      return cached.view;
    const graph = this.graph ?? {
      schema_version: 1,
      limit: 500,
      clusters: [],
      unavailable: [],
      truncated: false,
      nodes: [],
      edges: [],
    };
    const view = filterGraph(graph, {
      types: this.types,
      owner: this.owner,
      provider: this.provider,
    });
    this.viewCache = {
      graph: this.graph,
      types: this.types,
      owner: this.owner,
      provider: this.provider,
      view,
      layout: layoutGraph(view),
    };
    return view;
  }

  private layout(): ReturnType<typeof layoutGraph> {
    this.visible();
    return this.viewCache!.layout;
  }

  private select(id: string): void {
    this.selected = this.selected === id ? '' : id;
    if (!this.selected) return;
    this.focusGap = '';
    // The panel covers the right of the stage; keep the selection in view.
    const stage = this.shadowRoot?.querySelector('.stage');
    const position = this.layout().positions.get(id);
    if (!stage || !position) return;
    const right =
      this.translateX + (position.x + FLEET_MAP_NODE_WIDTH) * this.scale;
    // On narrow screens the panel sits below the selection, not to its right.
    const narrow = window.matchMedia('(max-width: 900px)').matches;
    const bounds = stage.getBoundingClientRect();
    const limit = bounds.width - (narrow ? 24 : 356);
    if (right > limit) this.translateX -= right - limit;
    const left = this.translateX + position.x * this.scale;
    if (left < 24) this.translateX += 24 - left;
    if (narrow) this.translateY = 64 - position.y * this.scale;
    this.applyTransform();
  }

  private toggleType(type: FleetMapNodeType): void {
    const next = new Set(this.types);
    if (next.has(type)) next.delete(type);
    else next.add(type);
    this.types = next;
  }

  private onKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape') {
      this.selected = '';
      this.focusGap = '';
    }
    if (event.key === '+' || event.key === '=') this.zoom(1.25);
    if (event.key === '-') this.zoom(0.8);
    const step = 48;
    if (event.key === 'ArrowLeft') this.translateX += step;
    if (event.key === 'ArrowRight') this.translateX -= step;
    if (event.key === 'ArrowUp') this.translateY += step;
    if (event.key === 'ArrowDown') this.translateY -= step;
    if (event.key.startsWith('Arrow')) {
      event.preventDefault();
      this.applyTransform();
    }
  };

  private renderFilters(graph: FleetMapGraph, shown: number) {
    const total = graph.nodes.filter((node) => node.type !== 'cluster').length;
    const groups = graph.nodes.filter((node) => node.type === 'cluster').length;
    const people = graph.nodes.filter((node) => node.type === 'user');
    const providers = graph.nodes.filter((node) => node.type === 'provider');
    const filtered =
      this.owner || this.provider || this.types.size < FLEET_MAP_COLUMNS.length;
    return html`<div class="filters">
      <fieldset class="type-toggles" aria-label="Show">
        ${FLEET_MAP_COLUMNS.map(
          (type) =>
            html`<button
              type="button"
              aria-pressed=${this.types.has(type) ? 'true' : 'false'}
              @click=${() => this.toggleType(type)}
            >
              ${FLEET_MAP_COLUMN_LABEL[type]}
            </button>`
        )}
      </fieldset>
      <label
        >Owner
        <select
          .value=${this.owner}
          @change=${(e: Event) =>
            (this.owner = (e.target as HTMLSelectElement).value)}
        >
          <option value="">Anyone</option>
          ${people.map(
            (node) => html`<option value=${node.id}>${node.label}</option>`
          )}
        </select></label
      >
      <label
        >Provider
        <select
          .value=${this.provider}
          @change=${(e: Event) =>
            (this.provider = (e.target as HTMLSelectElement).value)}
        >
          <option value="">Any provider</option>
          ${providers.map(
            (node) => html`<option value=${node.id}>${node.label}</option>`
          )}
        </select></label
      >
      <span class="count" aria-live="polite"
        >${filtered ? `${shown} of ${total}` : total}
        node${total === 1 ? '' : 's'}${groups ? ` · ${groups} summary group${groups === 1 ? '' : 's'}` : ''}</span
      >
    </div>`;
  }

  /**
   * One line naming every kind of gap in the loaded map with its count.
   * A chip lights the nodes that carry that gap, so "1 runner offline" is
   * a way to find it, not a number to take on trust.
   */
  private renderGaps(graph: FleetMapGraph) {
    const counts = new Map<FleetMapGapCode, { label: string; n: number }>();
    for (const node of graph.nodes)
      for (const gap of nodeGaps(node)) {
        const entry = counts.get(gap.code);
        if (entry) entry.n += 1;
        else counts.set(gap.code, { label: gap.label, n: 1 });
      }
    if (!counts.size)
      return html`<div class="gaps" data-testid="fleet-map-gaps">
        <span>No governance gaps in this map.</span>
      </div>`;
    return html`<div
      class="gaps"
      role="group"
      aria-label="Governance gaps"
      data-testid="fleet-map-gaps"
    >
      <span class="title"
        ><sl-icon name="exclamation-triangle-fill"></sl-icon>Gaps</span
      >
      ${[...counts].map(
        ([code, { label, n }]) =>
          html`<button
            type="button"
            class="gap-chip"
            data-gap=${code}
            aria-pressed=${this.focusGap === code ? 'true' : 'false'}
            @click=${() => {
              this.focusGap = this.focusGap === code ? '' : code;
              if (this.focusGap) this.selected = '';
            }}
          >
            <strong>${n}</strong>${GAP_CHIP_LABEL[code] ?? label}
          </button>`
      )}
    </div>`;
  }

  private renderNode(
    node: FleetMapNode,
    x: number,
    y: number,
    lit: Set<string> | null
  ) {
    const gaps = nodeGaps(node);
    const focused = !!this.focusGap && node.gaps.includes(this.focusGap);
    const classes = [
      'node',
      node.type === 'cluster' ? 'cluster' : '',
      gaps.length ? 'has-gap' : '',
      node.id === this.selected ? 'selected' : '',
      focused ? 'focus' : '',
      lit && lit.has(node.id) && node.id !== this.selected ? 'lit' : '',
      (lit && !lit.has(node.id)) || (this.focusGap && !focused) ? 'dim' : '',
    ].join(' ');
    const sub =
      node.type === 'cluster'
        ? `${node.count ?? ''} grouped`
        : gaps.length
          ? gaps.map((gap) => gap.label).join(' · ')
          : node.detail || FLEET_MAP_TYPE_LABEL[node.type];
    return html`<button
      type="button"
      class=${classes}
      data-node=${node.id}
      style="left:${x}px;top:${y}px;width:${FLEET_MAP_NODE_WIDTH}px;height:${FLEET_MAP_NODE_HEIGHT}px"
      aria-pressed=${node.id === this.selected ? 'true' : 'false'}
      aria-label="${FLEET_MAP_TYPE_LABEL[node.type]} ${node.label}${
        gaps.length ? `, ${gaps.map((gap) => gap.label).join(', ')}` : ''
      }"
      title="${node.label}${node.detail ? `\n${node.detail}` : ''}${
        gaps.length ? `\n${gaps.map((gap) => gap.label).join('\n')}` : ''
      }"
      @click=${() => this.select(node.id)}
    >
      <span class="glyph"><sl-icon name=${ICON[node.type]}></sl-icon></span>
      <span class="text"
        ><span class="label">${node.label}</span
        ><span class="sub ${gaps.length ? 'warn' : ''}">${sub}</span></span
      >
      ${
        gaps.length
          ? html`<sl-icon
              class="gap-dot"
              name="exclamation-triangle-fill"
              aria-hidden="true"
            ></sl-icon>`
          : nothing
      }
    </button>`;
  }

  private renderPanel(graph: FleetMapGraph, reach: Set<string>) {
    const node = graph.nodes.find((item) => item.id === this.selected);
    if (!node) return nothing;
    const gaps = nodeGaps(node);
    const byId = new Map(graph.nodes.map((item) => [item.id, item]));
    const dependents = [...reach]
      .filter((id) => id !== node.id)
      .map((id) => byId.get(id) as FleetMapNode);
    const groups = FLEET_MAP_COLUMNS.map((type) => ({
      type,
      items: dependents.filter((item) => nodeColumn(item) === type),
    })).filter((group) => group.items.length);
    const people = groups.find((group) => group.type === 'user')?.items.length;
    const agents = groups.find((group) => group.type === 'agent')?.items.length;
    return html`<aside
      class="panel"
      aria-label="Blast radius"
      data-testid="fleet-map-panel"
      @pointerdown=${(event: Event) => event.stopPropagation()}
      @wheel=${(event: Event) => event.stopPropagation()}
    >
      <button
        class="close"
        type="button"
        aria-label="Clear selection"
        @click=${() => (this.selected = '')}
      >
        ×
      </button>
      <div class="eyebrow">${FLEET_MAP_TYPE_LABEL[node.type]}</div>
      <h3>
        ${node.href ? html`<a href=${node.href}>${node.label}</a>` : node.label}
      </h3>
      ${node.detail ? html`<p class="meta">${node.detail}</p>` : nothing}
      ${
        gaps.length
          ? html`<div class="chips">
              ${gaps.map(
                (gap) =>
                  html`<sl-badge class="chip" pill variant=${gap.severity}
                    >${gap.label}</sl-badge
                  >`
              )}
            </div>`
          : nothing
      }
      ${
        node.actions?.length
          ? html`<div class="actions">
              ${node.actions.map(
                (action) => html`<a href=${action.href}>${action.label}</a>`
              )}
            </div>`
          : nothing
      }
      <h4>Blast radius</h4>
      <p class="reach">
        ${
          dependents.length
            ? html`Reaches ${dependents.length} connected
              resource${dependents.length === 1 ? '' : 's'}${
                agents || people
                  ? html`, including
                    ${[
                      agents ? `${agents} agent${agents === 1 ? '' : 's'}` : '',
                      people
                        ? `${people} ${people === 1 ? 'person' : 'people'}`
                        : '',
                    ]
                      .filter(Boolean)
                      .join(' and ')}`
                  : nothing
              }.`
            : 'No downstream resources in this map.'
        }
      </p>
      <p class="meta">
        What depends on this in the loaded map, not an outage report.
        ${
          graph.truncated || graph.unavailable.length
            ? html`<span role="status"
                >Partial map: this impact list may be incomplete.</span
              >`
            : nothing
        }
      </p>
      ${groups.map(
        ({ type, items }) =>
          html`<div class="group">
              ${FLEET_MAP_COLUMN_LABEL[type]} · ${items.length}
            </div>
            <ul>
              ${items.map((item) => {
                const itemGaps = nodeGaps(item);
                return html`<li>
                  ${
                    item.href
                      ? html`<a href=${item.href}>${item.label}</a>`
                      : html`<span>${item.label}</span>`
                  }
                  ${
                    itemGaps.length
                      ? html`<sl-badge class="chip" pill variant="warning"
                          >${itemGaps[0].label}</sl-badge
                        >`
                      : nothing
                  }
                  ${(item.actions || []).map(
                    (action) =>
                      html`<a class="act" href=${action.href}
                        >${action.label}</a
                      >`
                  )}
                </li>`;
              })}
            </ul>`
      )}
    </aside>`;
  }

  render() {
    const graph = this.graph;
    if (!graph) return nothing;
    if (!graph.nodes.length)
      return html`<div class="empty-state" data-testid="fleet-map-empty">
        ${graph.unavailable.length ? html`<p>Device evidence is unavailable. Check fleet collection and refresh.</p>` : nothing}
        Nothing to map yet. Register a runner with
        <code>preloop runner fg</code> or onboard an agent, then configure its
        model to connect the map.
      </div>`;
    const view = this.visible();
    const layout = this.layout();
    const radius =
      this.selected && graph.nodes.some((node) => node.id === this.selected)
        ? blastRadius(graph, this.selected)
        : null;
    const lit = radius ? radius.nodes : null;
    const byId = new Map(view.nodes.map((node) => [node.id, node]));
    const perColumn = new Map<string, number>();
    for (const node of view.nodes)
      if (node.type !== 'cluster')
        perColumn.set(node.type, (perColumn.get(node.type) || 0) + 1);
    const half = FLEET_MAP_NODE_HEIGHT / 2;
    return html`${this.renderFilters(graph, view.nodes.filter((node) => node.type !== 'cluster').length)}
      ${this.renderGaps(view)}
      ${
        graph.unavailable.length
          ? html`<p class="notice" role="status">
              Device evidence is unavailable. Refresh when fleet collection is
              available; existing agents and model configuration remain visible.
              Blast radius is incomplete.
            </p>`
          : nothing
      }
      ${
        graph.truncated
          ? html`<p class="notice" role="status">
              This map is capped at ${graph.limit} resources. Summary groups
              show omitted resources; filters only narrow the loaded map. Blast
              radius is incomplete. Use the table or resource pages to inspect
              the rest.
            </p>`
          : nothing
      }
      <div
        class="stage ${radius ? 'has-selection' : ''}"
        tabindex="0"
        role="region"
        aria-label="Fleet dependency map"
        @keydown=${this.onKeydown}
        @wheel=${(event: WheelEvent) => {
          event.preventDefault();
          this.zoom(
            Math.exp(-event.deltaY * 0.001),
            event.clientX,
            event.clientY
          );
        }}
        @pointerdown=${this.pointerDown}
        @pointermove=${this.pointerMove}
        @pointerup=${this.pointerUp}
        @pointercancel=${this.pointerUp}
        data-testid="fleet-map"
      >
        ${
          view.nodes.length
            ? html`<div
                class="plane"
                style="width:${layout.width}px;height:${layout.height}px;transform:translate(${this.translateX}px,${this.translateY}px) scale(${this.scale})"
              >
                ${layout.columns.map(
                  (column) =>
                    html`<div class="column-title" style="left:${column.x}px">
                      ${FLEET_MAP_COLUMN_LABEL[column.type]}<span class="n"
                        >${perColumn.get(column.type) ?? ''}</span
                      >
                    </div>`
                )}
                <svg
                  width=${layout.width}
                  height=${layout.height}
                  aria-hidden="true"
                >
                  ${view.edges.map((edge) => {
                    const a = layout.positions.get(edge.source);
                    const b = layout.positions.get(edge.target);
                    const na = byId.get(edge.source);
                    const nb = byId.get(edge.target);
                    if (!a || !b || !na || !nb) return nothing;
                    const [left, right] = a.x <= b.x ? [a, b] : [b, a];
                    const x1 = left.x + FLEET_MAP_NODE_WIDTH;
                    const y1 = left.y + half;
                    const x2 = right.x;
                    const y2 = right.y + half;
                    const mid = (x1 + x2) / 2;
                    const inRadius =
                      radius?.nodes.has(edge.impact_source) &&
                      radius.nodes.has(edge.impact_target);
                    const focused =
                      !!this.focusGap &&
                      (na.gaps.includes(this.focusGap) ||
                        nb.gaps.includes(this.focusGap));
                    const cls = [
                      'edge',
                      edge.provenance,
                      edge.gaps.length ? 'gap' : '',
                      inRadius ? 'lit' : '',
                      (radius && !inRadius) || (this.focusGap && !focused)
                        ? 'dim'
                        : '',
                    ].join(' ');
                    return svg`<path class=${cls} d="M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}"><title>${na.label} ${FLEET_MAP_EDGE_LABEL[edge.kind]} ${nb.label}</title></path>`;
                  })}
                </svg>
                ${view.nodes.map((node) => {
                  const position = layout.positions.get(node.id);
                  return position
                    ? this.renderNode(node, position.x, position.y, lit)
                    : nothing;
                })}
              </div>`
            : html`<div class="empty-state">
                No nodes match these filters. Clear a filter to see more.
              </div>`
        }
        <div
          class="overlay canvas-tools"
          role="group"
          aria-label="Map navigation"
        >
          <button
            type="button"
            @click=${() => this.zoom(0.8)}
            aria-label="Zoom out"
          >
            −
          </button>
          <span class="zoom"></span>
          <button
            type="button"
            @click=${() => this.zoom(1.25)}
            aria-label="Zoom in"
          >
            +
          </button>
          <button type="button" @click=${this.fit}>Fit</button>
        </div>
        <div class="overlay legend">
          <span
            ><svg viewBox="0 0 24 8" aria-hidden="true">
              <line x1="0" y1="4" x2="24" y2="4" class="edge" /></svg
            >Configured</span
          >
          <span
            ><svg viewBox="0 0 24 8" aria-hidden="true">
              <line x1="0" y1="4" x2="24" y2="4" class="edge observed" /></svg
            >Observed</span
          >
          <span
            ><sl-icon class="gap-dot" name="exclamation-triangle-fill"></sl-icon
            >Gap</span
          >
          <span>Select a node to see what depends on it. Esc clears.</span>
        </div>
        ${radius ? this.renderPanel(graph, radius.nodes) : nothing}
      </div>`;
  }
}

/** Short chip wording; the node and the side panel keep the full label. */
const GAP_CHIP_LABEL: Partial<Record<FleetMapGapCode, string>> = {
  discovered_agent: 'not onboarded',
  unknown_device: 'device unknown',
  offline_runner: 'runner offline',
  not_via_gateway: 'not via the gateway',
  open_finding: 'open finding',
};

declare global {
  interface HTMLElementTagNameMap {
    'fleet-map': FleetMap;
  }
}
