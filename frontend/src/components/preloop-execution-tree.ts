import {
  LitElement,
  html,
  css,
  nothing,
  type PropertyValues,
  type TemplateResult,
} from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getExecutionTree,
  type ExecutionTree,
  type ExecutionTreeNode,
} from '../api';
import { formatLocalTime } from '../utils/date';
import { executionDurationText } from '../utils/execution';
import {
  executionStatusLabel,
  executionStatusVariant,
  formatEstimatedCost,
  formatTokenCount,
} from '../utils/execution-presentation';
import { renderFailureCategoryChip } from '../utils/failure-category';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

/**
 * Statuses that mean the run was attempted and ended badly.
 *
 * A refusal is deliberately not here: nothing ran, nothing was charged, and
 * the difference is the first thing an operator needs from this panel.
 */
const FAILED_STATUSES = ['FAILED', 'TIMEOUT', 'TIMED_OUT', 'ABORTED', 'ERROR'];

/** The synthetic status of a delegation the server declined to start. */
const REFUSED_STATUS = 'REFUSED';

/**
 * How deep the rows may nest before rendering stops.
 *
 * The server caps delegation depth well below this, so a tree that reaches
 * here is not a deep tree, it is corrupt lineage pointing at itself. The cap
 * keeps that a missing row rather than a hung tab.
 */
const MAX_RENDER_DEPTH = 32;

/** Group the flat subtree by parent id, keeping the server's order. */
function indexByParent(
  rows: ExecutionTreeNode[]
): Map<string, ExecutionTreeNode[]> {
  const index = new Map<string, ExecutionTreeNode[]>();
  for (const row of rows || []) {
    const parentId = row.parent_execution_id;
    if (!parentId) continue;
    const siblings = index.get(parentId);
    if (siblings) {
      siblings.push(row);
    } else {
      index.set(parentId, [row]);
    }
  }
  return index;
}

/**
 * What one run delegated, as a tree, with the cost of the subtree.
 *
 * A delegating run is otherwise invisible: its own page shows one execution
 * and its own cost while the work and the money sit in the children. This
 * panel lists them, keeps the parent's own cost separate from the subtree
 * total (adding the two would hide exactly the number people come here for),
 * and links every row to its own page.
 *
 * It asks the server once per execution. Most runs delegate nothing, so the
 * usual answer is an empty tree and the usual rendering is one quiet line.
 */
@customElement('preloop-execution-tree')
export class PreloopExecutionTree extends LitElement {
  static styles = css`
    :host {
      display: block;
    }
    .tree {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      margin: var(--sl-spacing-medium) 0;
      overflow: hidden;
    }
    .tree-header {
      align-items: baseline;
      background: var(--sl-color-neutral-50);
      border-bottom: 1px solid var(--sl-color-neutral-200);
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small) var(--sl-spacing-large);
      justify-content: space-between;
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
    }
    .tree-title {
      font-weight: var(--sl-font-weight-semibold);
    }
    .totals {
      color: var(--sl-color-neutral-700);
      display: flex;
      flex-wrap: wrap;
      font-size: var(--sl-font-size-small);
      gap: var(--sl-spacing-medium);
    }
    .totals .figure {
      white-space: nowrap;
    }
    .totals .figure b {
      font-weight: var(--sl-font-weight-semibold);
    }
    .own-cost {
      border-left: 1px solid var(--sl-color-neutral-300);
      padding-left: var(--sl-spacing-medium);
    }
    .row {
      align-items: baseline;
      border-top: 1px solid var(--sl-color-neutral-100);
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small) var(--sl-spacing-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
    }
    .row:first-of-type {
      border-top: none;
    }
    .row.refused {
      background: var(--sl-color-neutral-50);
      color: var(--sl-color-neutral-600);
    }
    .row .flow {
      font-weight: var(--sl-font-weight-semibold);
      overflow-wrap: anywhere;
    }
    .row .label {
      color: var(--sl-color-neutral-700);
      overflow-wrap: anywhere;
    }
    .row .times,
    .row .cost {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      white-space: nowrap;
    }
    .row .cost {
      margin-left: auto;
    }
    a {
      color: var(--sl-color-primary-600);
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .toggle {
      font-size: var(--sl-font-size-small);
    }
    .toggle-spacer {
      display: inline-block;
      width: 1.5rem;
    }
    .error {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      margin: var(--sl-spacing-medium) 0;
    }
    .truncated {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
    }
  `;

  /** Which run's subtree to show. */
  @property({ type: String, attribute: 'execution-id' }) executionId = '';

  @state() private tree: ExecutionTree | null = null;
  @state() private loading = false;
  @state() private error = '';
  @state() private expanded = new Set<string>();

  /** Guards against a slow answer for a run the user has already left. */
  private generation = 0;

  /**
   * Children by parent id, built once per answer.
   *
   * The server hands over the subtree parents-first so the tree can be built
   * in one pass; scanning the flat list per row would pay for that ordering
   * and then ignore it.
   */
  private childIndex = new Map<string, ExecutionTreeNode[]>();

  protected willUpdate(changes: PropertyValues) {
    if (changes.has('executionId')) {
      this.tree = null;
      this.error = '';
      this.expanded = new Set();
      void this.load();
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.generation++;
  }

  /** Read the tree again; the page decides when that is worth doing. */
  async load() {
    const executionId = this.executionId;
    if (!executionId) {
      this.loading = false;
      return;
    }
    const generation = ++this.generation;
    this.loading = true;
    try {
      const tree = await getExecutionTree(executionId);
      if (generation !== this.generation) return;
      this.childIndex = indexByParent(tree.executions);
      this.tree = tree;
      this.error = '';
    } catch (error) {
      if (generation !== this.generation) return;
      this.childIndex = new Map();
      this.tree = null;
      this.error =
        error instanceof Error ? error.message : 'Could not load the tree';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  private childrenOf(parentId: string): ExecutionTreeNode[] {
    return this.childIndex.get(parentId) || [];
  }

  private toggle(id: string) {
    const expanded = new Set(this.expanded);
    if (expanded.has(id)) {
      expanded.delete(id);
    } else {
      expanded.add(id);
    }
    this.expanded = expanded;
  }

  private countOf(status: string): number {
    return this.tree?.rollup?.by_status?.[status] || 0;
  }

  private failedCount(): number {
    return FAILED_STATUSES.reduce(
      (total, status) => total + this.countOf(status),
      0
    );
  }

  private renderRow(
    node: ExecutionTreeNode,
    depth: number
  ): TemplateResult | typeof nothing {
    if (depth > MAX_RENDER_DEPTH) return nothing;
    const children = this.childrenOf(node.id);
    const open = this.expanded.has(node.id);
    const refused = (node.status || '').toUpperCase() === REFUSED_STATUS;
    const duration = executionDurationText(node);
    return html`
      <div
        class="row ${refused ? 'refused' : ''}"
        data-testid="execution-tree-row"
        data-execution-id=${node.id}
        data-status=${node.status}
        data-depth=${depth}
        ?data-refused=${refused}
        style="padding-left: calc(var(--sl-spacing-medium) + ${
          depth * 1.25
        }rem)"
      >
        ${
          children.length > 0
            ? html`<sl-icon-button
                class="toggle"
                data-testid="execution-tree-toggle"
                data-execution-id=${node.id}
                name=${open ? 'chevron-down' : 'chevron-right'}
                label=${
                  open
                    ? `Hide what ${node.flow_name || 'this run'} started`
                    : `Show what ${node.flow_name || 'this run'} started`
                }
                @click=${() => this.toggle(node.id)}
              ></sl-icon-button>`
            : html`<span class="toggle-spacer"></span>`
        }
        <span class="flow">
          <a href="/console/flows/executions/${node.id}"
            >${node.flow_name || 'Flow'}</a
          >
        </span>
        ${
          node.label
            ? html`<span class="label" data-testid="execution-tree-label"
                >${node.label}</span
              >`
            : nothing
        }
        ${
          refused
            ? html`<sl-badge
                class="chip"
                pill
                variant="neutral"
                data-testid="execution-tree-refused-badge"
                >Refused</sl-badge
              >`
            : html`<sl-badge
                class="chip"
                pill
                variant=${executionStatusVariant(node.status)}
                >${executionStatusLabel(node.status)}</sl-badge
              >`
        }
        ${renderFailureCategoryChip(node.failure_category)}
        <span class="times">
          ${formatLocalTime(node.start_time)}${duration ? ` · ${duration}` : ''}
        </span>
        <span class="cost" data-testid="execution-tree-row-cost">
          ${refused ? '—' : formatEstimatedCost(node.estimated_cost)}
        </span>
      </div>
      ${open ? children.map((child) => this.renderRow(child, depth + 1)) : nothing}
    `;
  }

  render() {
    if (!this.executionId) return nothing;
    if (this.error) {
      return html`<div class="error" data-testid="execution-tree-error">
        ${this.error}
      </div>`;
    }
    if (!this.tree) return nothing;

    const rollup = this.tree.rollup;
    const children = this.childrenOf(this.tree.execution_id);
    if (children.length === 0) {
      // The overwhelming majority of runs. A "nothing delegated" line only
      // pushed the error and the tabs further down, so nothing renders.
      return nothing;
    }

    return html`
      <div class="tree" data-testid="execution-tree">
        <div class="tree-header">
          <span class="tree-title">Runs this one started</span>
          <span class="totals">
            <span class="figure" data-testid="execution-tree-launched"
              ><b>${rollup.total}</b> launched</span
            >
            <span class="figure" data-testid="execution-tree-succeeded"
              ><b>${this.countOf('SUCCEEDED')}</b> succeeded</span
            >
            <span class="figure" data-testid="execution-tree-failed"
              ><b>${this.failedCount()}</b> failed</span
            >
            <span class="figure" data-testid="execution-tree-refused"
              ><b>${this.countOf(REFUSED_STATUS)}</b> refused</span
            >
            <span class="figure" data-testid="execution-tree-subtree-cost"
              ><b>${formatEstimatedCost(rollup.total_estimated_cost)}</b>
              subtree cost</span
            >
            <span class="figure" data-testid="execution-tree-subtree-tokens"
              ><b>${formatTokenCount(rollup.total_tokens)}</b> tokens</span
            >
            <span class="figure own-cost" data-testid="execution-tree-own-cost"
              ><b>${formatEstimatedCost(this.tree.execution.estimated_cost)}</b>
              this run</span
            >
          </span>
        </div>
        ${children.map((child) => this.renderRow(child, 0))}
        ${
          this.tree.truncated
            ? html`<div
                class="truncated"
                data-testid="execution-tree-truncated"
              >
                The lineage behind this run is larger than one read; the totals
                cover the runs listed here.
              </div>`
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-execution-tree': PreloopExecutionTree;
  }
}
