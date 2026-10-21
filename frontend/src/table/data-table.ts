import { LitElement, html, nothing } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import type { RowData } from '@tanstack/table-core';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/skeleton/skeleton.js';

import '../components/list-selection';
import '../components/resource-actions';
import type { ListSelectionController } from '../components/list-selection';
import type { ResourceAction } from '../components/resource-actions';
import type { ListTable } from './list-table';
import {
  renderHeaderColumnPicker,
  renderListCells,
  renderListHeaders,
} from './list-table-render';
import './table-pagination';
import './table-state';

/** What `table-state` says when a list has nothing, or nothing matches. */
export interface DataTableEmpty {
  heading: string;
  message?: string;
  icon?: string;
  /** A call to action under the message, e.g. "Create an API key". */
  action?: { label: string; onClick: () => void };
}

/**
 * One consistent console list: header, rows, selection, row actions,
 * loading, empty, error and pagination, from a `ListTable` model.
 *
 * It renders into **light DOM**. The element lives inside the view's shadow
 * root, so the view's own styles (`consoleStyles`, `listTableStyles`,
 * `dataTableStyles` and whatever its cell templates need) reach every cell.
 * A shadow root here would cut the cells off from the styles that draw them,
 * which is what would make every migration a re-skin.
 *
 * The model's host is the view, not this element: a sort or a filter repaints
 * the view, the view passes `.rows=${table.rows}` (a fresh array each time),
 * and that repaints the table. Selection works the same way through the
 * view's `ListSelectionController`.
 *
 * Search, filters and the bulk bar sit in the shared
 * `list-toolbar` above (see `renderDataTableToolbar`), not here: the toolbar
 * is shared with lists that render cards or a timeline instead of a table.
 *
 * @fires row-activate - `{ detail: { row, event } }` on click or Enter on a
 *   row, when `activatable` is set. Clicks on links, buttons, checkboxes and
 *   the actions menu do not activate the row.
 */
@customElement('data-table')
export class DataTable<TRow extends RowData = RowData> extends LitElement {
  @property({ attribute: false }) table!: ListTable<TRow>;
  /** The rows to draw; pass `table.rows`. */
  @property({ attribute: false }) rows: TRow[] = [];
  @property({ attribute: false })
  selection?: ListSelectionController<TRow>;
  /** Per-row kebab menu. */
  @property({ attribute: false })
  rowActions?: (row: TRow) => ResourceAction[];
  /** The name a row is read by, for the checkbox label. */
  @property({ attribute: false }) rowLabel?: (row: TRow) => string;
  /** Extra classes on a `<tr>`. */
  @property({ attribute: false }) rowClass?: (row: TRow) => string;
  /** Selectable rows; others get no checkbox. */
  @property({ attribute: false }) rowSelectable?: (row: TRow) => boolean;
  /**
   * A full-width row drawn under a row (an expanded drill-down). Return
   * `nothing` (or null/undefined) for a row that is collapsed. The detail
   * row is not a data row: keyboard navigation and selection skip it.
   */
  @property({ attribute: false }) rowDetail?: (row: TRow) => unknown;
  /** Extra classes on the `<table>`, for a view's column widths. */
  @property({ type: String, attribute: 'table-class' }) tableClass = '';
  /** Accessible name of the table, e.g. "API keys". */
  @property({ type: String }) label = '';
  /** Plural noun for the pagination range and default empty copy. */
  @property({ type: String }) noun = 'rows';
  @property({ type: Boolean }) loading = false;
  /** A load error; replaces the rows with an error state and a retry. */
  @property({ attribute: false }) error: string | null = null;
  @property({ attribute: false }) empty?: DataTableEmpty;
  @property({ type: Boolean }) activatable = false;
  /** Keeps the header in view while the body scrolls. */
  @property({ type: Boolean, attribute: 'sticky-header' }) stickyHeader = true;
  /** Placeholder rows while the first page loads. */
  @property({ type: Number, attribute: 'skeleton-rows' }) skeletonRows = 5;

  createRenderRoot() {
    return this;
  }

  private get hasActions(): boolean {
    return Boolean(this.rowActions);
  }

  private get columnCount(): number {
    return (
      this.table.visibleColumns.length +
      (this.selection ? 1 : 0) +
      (this.hasActions ? 1 : 0)
    );
  }

  private retry() {
    this.dispatchEvent(
      new CustomEvent('retry', { bubbles: true, composed: true })
    );
  }

  private activate(row: TRow, event: Event) {
    this.dispatchEvent(
      new CustomEvent('row-activate', {
        detail: { row, event },
        bubbles: true,
        composed: true,
      })
    );
  }

  private onRowClick(row: TRow, event: MouseEvent) {
    if (!this.activatable) return;
    const path = event.composedPath();
    const rowEl = event.currentTarget as HTMLElement;
    for (const node of path) {
      if (node === rowEl) break;
      if (
        node instanceof HTMLElement &&
        node.matches(
          'a, button, input, select, textarea, sl-button, sl-checkbox, list-select-checkbox, resource-actions, sl-dropdown, [data-no-activate]'
        )
      ) {
        return;
      }
    }
    // A drag to select text is not a click on the row.
    if (window.getSelection()?.toString()) return;
    this.activate(row, event);
  }

  /**
   * Arrow keys move between rows, Enter opens one, Home and End jump. The
   * selection controller on the view already owns Escape and Ctrl+A.
   */
  private onRowKeydown(row: TRow, event: KeyboardEvent) {
    if (event.target !== event.currentTarget) return;
    const current = event.currentTarget as HTMLElement;
    const rows = [
      ...(current.parentElement?.querySelectorAll<HTMLElement>(
        'tr[data-row]'
      ) ?? []),
    ];
    const index = rows.indexOf(current);
    let next: HTMLElement | undefined;
    switch (event.key) {
      case 'ArrowDown':
        next = rows[index + 1];
        break;
      case 'ArrowUp':
        next = rows[index - 1];
        break;
      case 'Home':
        next = rows[0];
        break;
      case 'End':
        next = rows[rows.length - 1];
        break;
      case 'Enter':
        // Dispatch first: consumers that honour `defaultPrevented` (the
        // views' click handlers do, for links) must see an untouched event.
        if (this.activatable) {
          this.activate(row, event);
          event.preventDefault();
        }
        return;
      case ' ':
        if (this.selection && this.isSelectable(row)) {
          event.preventDefault();
          this.selection.toggle(this.table.idOf(row), event.shiftKey);
        }
        return;
      default:
        return;
    }
    if (next) {
      event.preventDefault();
      next.focus();
    }
  }

  private isSelectable(row: TRow): boolean {
    return this.rowSelectable ? this.rowSelectable(row) : true;
  }

  private renderHeaderRow(): TemplateResult {
    const selection = this.selection;
    return html`<tr>
      ${
        selection
          ? html`<th class="select-cell" scope="col">
              <list-select-checkbox
                label=${`Select all ${this.noun}`}
                ?checked=${selection.allSelected}
                ?indeterminate=${selection.someSelected}
                ?disabled=${selection.busy || !selection.order.length}
                @selection-toggle=${selection.handleToggleEvent}
              ></list-select-checkbox>
            </th>`
          : nothing
      }
      ${renderListHeaders(this.table, !this.hasActions)}
      ${
        this.hasActions
          ? html`<th class="actions-cell" scope="col">
              <div class="table-header-content">
                <span class="visually-hidden">Actions</span>
                ${renderHeaderColumnPicker(this.table)}
              </div>
            </th>`
          : nothing
      }
    </tr>`;
  }

  private renderRow(row: TRow): TemplateResult {
    const id = this.table.idOf(row);
    const selection = this.selection;
    const selected = selection?.isSelected(id) ?? false;
    const classes = [
      'data-row',
      this.activatable ? 'activatable' : '',
      selected ? 'selected' : '',
      this.rowClass?.(row) ?? '',
    ]
      .filter(Boolean)
      .join(' ');
    const label = this.rowLabel?.(row) ?? id;
    return html`<tr
      class=${classes}
      data-row
      data-selection-id=${id}
      tabindex="0"
      aria-selected=${selection ? String(selected) : nothing}
      @click=${(event: MouseEvent) => this.onRowClick(row, event)}
      @keydown=${(event: KeyboardEvent) => this.onRowKeydown(row, event)}
    >
      ${
        selection
          ? html`<td class="select-cell">
              ${
                this.isSelectable(row)
                  ? html`<list-select-checkbox
                      item-id=${id}
                      label=${`Select ${label}`}
                      ?checked=${selected}
                      ?disabled=${selection.busy}
                      @selection-toggle=${selection.handleToggleEvent}
                    ></list-select-checkbox>`
                  : nothing
              }
            </td>`
          : nothing
      }
      ${renderListCells(this.table, row)}
      ${
        this.rowActions
          ? html`<td class="actions-cell">
              <resource-actions
                .actions=${this.rowActions(row)}
                menu-only
                size="small"
              ></resource-actions>
            </td>`
          : nothing
      }
    </tr>`;
  }

  private renderRowWithDetail(row: TRow): TemplateResult {
    const detail = this.rowDetail?.(row);
    if (detail === undefined || detail === null || detail === nothing) {
      return this.renderRow(row);
    }
    return html`${this.renderRow(row)}
      <tr class="row-detail" data-detail-for=${this.table.idOf(row)}>
        <td colspan=${this.columnCount}>${detail}</td>
      </tr>`;
  }

  private renderSkeleton(): TemplateResult {
    const cells = Array.from({ length: this.columnCount });
    return html`${Array.from(
      { length: this.skeletonRows },
      () =>
        html`<tr class="skeleton-row" aria-hidden="true">
          ${cells.map(() => html`<td><sl-skeleton effect="sheen"></sl-skeleton></td>`)}
        </tr>`
    )}`;
  }

  private renderState(): TemplateResult | typeof nothing {
    if (this.error) {
      return html`<table-state
        kind="error"
        heading=${`Could not load ${this.noun}`}
        message=${this.error}
      >
        <sl-button size="small" @click=${this.retry}>
          <sl-icon slot="prefix" name="arrow-clockwise"></sl-icon>
          Try again
        </sl-button>
      </table-state>`;
    }
    if (this.loading || this.rows.length) return nothing;
    if (this.table.filtersActive) {
      return html`<table-state
        kind="no-results"
        heading=${`No ${this.noun} match`}
        message="Try a different search, or clear the filters."
      >
        <sl-button size="small" @click=${() => this.table.clearFilters()}>
          Clear filters
        </sl-button>
      </table-state>`;
    }
    const empty = this.empty;
    return html`<table-state
      kind="empty"
      heading=${empty?.heading ?? `No ${this.noun} yet`}
      message=${empty?.message ?? ''}
      icon=${empty?.icon ?? ''}
    >
      ${
        empty?.action
          ? html`<sl-button
              size="small"
              variant="primary"
              @click=${empty.action.onClick}
            >
              ${empty.action.label}
            </sl-button>`
          : nothing
      }
    </table-state>`;
  }

  private renderPagination(): TemplateResult | typeof nothing {
    const table = this.table;
    if (!table.paginated || this.error) return nothing;
    return html`<table-pagination
      page-index=${table.pageIndex}
      page-size=${table.pageSize}
      .pageSizes=${table.pageSizes}
      .total=${table.totalRows}
      page-rows=${this.rows.length}
      ?has-next=${table.canNextPage}
      ?loading=${this.loading}
      noun=${this.noun}
      @page-change=${(event: CustomEvent) =>
        table.setPageIndex(event.detail.pageIndex)}
      @page-size-change=${(event: CustomEvent) =>
        table.setPageSize(event.detail.pageSize)}
    ></table-pagination>`;
  }

  render() {
    if (!this.table) return nothing;
    const showSkeleton = this.loading && !this.rows.length && !this.error;
    const state = this.renderState();
    const hideTable = state !== nothing;
    return html`
      <div
        class="data-table ${this.table.density} ${
          this.stickyHeader ? 'sticky' : ''
        } ${this.loading && this.rows.length ? 'refreshing' : ''}"
      >
        ${
          hideTable
            ? state
            : html`<div class="table-scroll data-table-scroll">
                <table
                  class="styled-table ${this.tableClass}"
                  role=${this.selection ? 'grid' : nothing}
                  aria-multiselectable=${this.selection ? 'true' : nothing}
                  aria-label=${this.label || nothing}
                  aria-busy=${this.loading ? 'true' : 'false'}
                  aria-rowcount=${this.table.totalRows ?? nothing}
                >
                  <thead>
                    ${this.renderHeaderRow()}
                  </thead>
                  <tbody>
                    ${
                      showSkeleton
                        ? this.renderSkeleton()
                        : repeat(
                            this.rows,
                            (row) => this.table.idOf(row),
                            (row) => this.renderRowWithDetail(row)
                          )
                    }
                  </tbody>
                </table>
              </div>`
        }
        ${this.renderPagination()}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'data-table': DataTable;
  }
}
