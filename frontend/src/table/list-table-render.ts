import { html, nothing } from 'lit';
import type { TemplateResult } from 'lit';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import './column-picker';
import type { RowData } from '@tanstack/table-core';
import {
  DEFAULT_COLUMN_WIDTH,
  MAX_COLUMN_WIDTH,
  MIN_COLUMN_WIDTH,
  type ListHeader,
  type ListTable,
} from './list-table';

function textOf(value: unknown): string {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : String(value);
}

/** How far one arrow key moves a resize handle. */
const KEYBOARD_RESIZE_STEP = 16;

/**
 * The `<th>` row for a list, from the model.
 *
 * The markup is what the executions and flows tables already rendered by
 * hand: a `col-<id>` class the view's CSS keys on, `aria-sort`, and a button
 * that fills the cell. Views append their own trailing header (the kebab
 * column) after these.
 */
export function renderListHeaders<TRow extends RowData>(
  table: ListTable<TRow>,
  picker = false
): TemplateResult[] {
  return table.headers.map((header, index) =>
    renderListHeader(
      table,
      header,
      picker && index === table.headers.length - 1
    )
  );
}

function renderListHeader<TRow extends RowData>(
  table: ListTable<TRow>,
  header: ListHeader<TRow>,
  picker: boolean
): TemplateResult {
  const column = header.column;
  const classes = [
    column.headerClass ?? `col-${column.id}`,
    header.sortable ? 'sortable' : '',
    column.numeric ? 'numeric' : '',
    header.sorted ? 'active' : '',
  ]
    .filter(Boolean)
    .join(' ');
  return html`
    <th
      class=${classes}
      style=${
        header.width || column.minWidth
          ? [
              header.width ? `width: ${header.width}px` : '',
              column.minWidth ? `min-width: ${column.minWidth}px` : '',
            ]
              .filter(Boolean)
              .join('; ')
          : nothing
      }
      aria-sort=${header.ariaSort}
      scope="col"
    >
      <div class="table-header-content">
        ${
          header.sortable
            ? html`
                <button
                  type="button"
                  class="sort-button"
                  data-sort-key=${column.id}
                  title=${column.headerTitle || 'Sorts the rows on this page'}
                  aria-label=${column.headerLabel || nothing}
                  @click=${(event: MouseEvent) =>
                    table.toggleSort(column.id, event.shiftKey)}
                >
                  <span>${column.header}</span>
                  <sl-icon
                    class="sort-caret"
                    name=${
                      header.sorted
                        ? header.sorted === 'asc'
                          ? 'caret-up-fill'
                          : 'caret-down-fill'
                        : 'chevron-expand'
                    }
                  ></sl-icon>
                  ${
                    header.sortRank
                      ? html`<span class="sort-rank">${header.sortRank}</span>`
                      : nothing
                  }
                </button>
              `
            : html`<span class="header-label">${column.header}</span>`
        }
        ${picker ? renderHeaderColumnPicker(table) : nothing}
      </div>
      ${
        header.resizable
          ? html`<span
              class="col-resize ${
                table.resizingColumn === column.id ? 'resizing' : ''
              }"
              role="separator"
              tabindex="0"
              aria-orientation="vertical"
              aria-valuemin=${column.minWidth ?? MIN_COLUMN_WIDTH}
              aria-valuemax=${MAX_COLUMN_WIDTH}
              aria-valuenow=${
                header.width ??
                table.widthOfColumn(column.id) ??
                DEFAULT_COLUMN_WIDTH
              }
              aria-label=${`Resize the ${column.header} column`}
              @mousedown=${(event: MouseEvent) =>
                table.startResize(column.id, event)}
              @touchstart=${(event: TouchEvent) =>
                table.startResize(column.id, event)}
              @click=${(event: Event) => event.stopPropagation()}
              @keydown=${(event: KeyboardEvent) =>
                handleResizeKey(table, column.id, event)}
            ></span>`
          : nothing
      }
    </th>
  `;
}

function handleResizeKey<TRow extends RowData>(
  table: ListTable<TRow>,
  columnId: string,
  event: KeyboardEvent
): void {
  const step =
    event.key === 'ArrowLeft'
      ? -KEYBOARD_RESIZE_STEP
      : event.key === 'ArrowRight'
        ? KEYBOARD_RESIZE_STEP
        : 0;
  if (!step) return;
  event.preventDefault();
  table.resizeBy(columnId, step);
}

/**
 * The `<td>` cells of one row, in the order and visibility the model says.
 *
 * The cell bodies are the view's own templates: the table layer decides which
 * cells there are, never what is in them.
 */
export function renderListCells<TRow extends RowData>(
  table: ListTable<TRow>,
  row: TRow
): TemplateResult[] {
  return table.visibleColumns.map((column) => {
    const classes = [column.cellClass ?? '', column.numeric ? 'numeric' : '']
      .filter(Boolean)
      .join(' ');
    if (column.truncate) {
      const title =
        column.cellTitle?.(row) ??
        column.searchText?.(row) ??
        textOf(column.value?.(row));
      // The cut happens on an inner block, so the cell itself never
      // overflows and the row stays one line.
      return html`<td
        class=${classes || nothing}
        title=${title || nothing}
        data-label=${column.cellLabel ?? nothing}
      >
        <span class="cell-truncate">${column.cell(row)}</span>
      </td>`;
    }
    const title = column.cellTitle?.(row);
    return html`<td
      class=${classes || nothing}
      title=${title || nothing}
      data-label=${column.cellLabel ?? nothing}
    >
      ${column.cell(row)}
    </td>`;
  });
}

/** Column controls share the final header cell, without adding a column. */
export function renderHeaderColumnPicker<TRow extends RowData>(
  table: ListTable<TRow>,
  density = true
): TemplateResult {
  return html`<column-picker
    compact
    .columns=${table.pickerColumns}
    ?can-reset=${table.hasColumnChanges}
    density=${density ? table.density : ''}
    @column-toggle=${(event: CustomEvent) =>
      table.setVisible(event.detail.id, event.detail.visible)}
    @columns-reset=${() => table.resetColumns()}
    @density-change=${(event: CustomEvent) =>
      table.setDensity(event.detail.density)}
  ></column-picker>`;
}
