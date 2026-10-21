import { html, nothing } from 'lit';
import type { TemplateResult } from 'lit';
import type { RowData } from '@tanstack/table-core';

import '../components/list-toolbar';
import '../components/list-selection';
import type {
  BulkAction,
  ListSelectionController,
} from '../components/list-selection';
import type { ListTable } from './list-table';

export interface DataTableToolbarOptions<TRow extends RowData> {
  table: ListTable<TRow>;
  searchPlaceholder?: string;
  /** Accessible name for the search field, e.g. "Search API keys". */
  searchLabel?: string;
  /** Page filters (selects, range pickers), placed after the search. */
  filters?: TemplateResult | typeof nothing;
  /** The count on the right, e.g. "12 keys". */
  count?: string;
  selection?: ListSelectionController<TRow>;
  bulkActions?: BulkAction[];
  bulkLabel?: string;
  onBulkAction?: (actionId: string) => void;
}

/**
 * The shared toolbar for a `<data-table>`: `list-toolbar` with its search
 * wired to the model's global filter, the page's own filters, the count, and the bulk bar that takes the row
 * over while something is selected.
 *
 * It is a function rather than another element so the filters a page passes
 * stay in the page's shadow root and keep its styles.
 */
export function renderDataTableToolbar<TRow extends RowData>(
  options: DataTableToolbarOptions<TRow>
): TemplateResult {
  const { table, selection } = options;
  const selecting = Boolean(selection && selection.count > 0);
  return html`
    <list-toolbar
      .search=${table.search}
      searchPlaceholder=${options.searchPlaceholder ?? 'Search'}
      searchLabel=${options.searchLabel ?? ''}
      .views=${['list']}
      ?selecting=${selecting}
      @search-change=${(event: CustomEvent) =>
        table.setSearch(event.detail.value)}
    >
      ${options.filters ?? nothing}
      ${
        options.count
          ? html`<span slot="count">${options.count}</span>`
          : nothing
      }
      ${
        selection && options.bulkActions
          ? html`<list-bulk-bar
              slot="bulk"
              docked
              label=${options.bulkLabel ?? 'Bulk actions'}
              .count=${selection.count}
              .total=${selection.order.length}
              .actions=${options.bulkActions}
              .running=${selection.running}
              .progressDone=${selection.progressDone}
              .progressTotal=${selection.progressTotal}
              @bulk-action=${(event: CustomEvent) =>
                options.onBulkAction?.(event.detail.id)}
              @selection-select-all=${() => selection.toggleAll(true)}
              @selection-clear=${() => selection.clear()}
            ></list-bulk-bar>`
          : nothing
      }
    </list-toolbar>
  `;
}
