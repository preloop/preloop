import type { ReactiveController, ReactiveControllerHost } from 'lit';
import {
  columnFilteringFeature,
  columnOrderingFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  constructTable,
  createCoreRowModel,
  createFilteredRowModel,
  createPaginatedRowModel,
  createSortedRowModel,
  getInitialTableState,
  globalFilteringFeature,
  rowPaginationFeature,
  rowSortingFeature,
  tableFeatures,
} from '@tanstack/table-core';
import type {
  ColumnFiltersState,
  RowData,
  Table,
  TableState,
  Updater,
} from '@tanstack/table-core';
import {
  readTableUrlState,
  writeTableUrlState,
  type TableUrlOptions,
} from './table-url-state';
import { storeReactivityBindings } from '@tanstack/table-core/store-reactivity-bindings';
import {
  readTablePreferences,
  writeTablePreferences,
  clearTablePreferences,
} from './table-preferences';
import type { StoredTablePreferences } from './table-preferences';

/**
 * The console's headless table layer.
 *
 * `@tanstack/table-core` holds the model (which columns exist, which are on,
 * in what order, how wide, how the rows are sorted) and this module holds the
 * console's contract with it. Nothing here renders: the views keep their own
 * `.styled-table` markup, their CSS and their cell templates, so adopting the
 * model layer costs no visual change and no re-skinning inside somebody
 * else's shadow DOM.
 *
 * Only the features a console list actually uses are registered, which is
 * what keeps the cost near the measured +17 kB gz rather than the whole
 * library: sorting, filtering (global search and per column), pagination,
 * visibility, ordering, sizing and resizing. Selection stays with
 * `list-selection.ts`, which already does it, and row expansion is one more
 * feature import on the day a list grows a tree.
 *
 * A list runs in one of two modes. `client` (the default) sorts, filters and
 * pages the rows it was handed. `server` hands every query change to the view
 * through `onQueryChange` and renders the rows it is given as they are: the
 * model still owns the state, so the header carets, the search box, the page
 * size and the URL agree whichever side does the work.
 */

function textOfFn(value: unknown): string {
  return value === null || value === undefined ? '' : String(value).trim();
}

type FilterRow = { getValue: (columnId: string) => unknown };

/** "Contains, ignoring case": the console's one text filter. */
const consoleText = Object.assign(
  (row: FilterRow, columnId: string, filterValue: unknown): boolean =>
    textOfFn(row.getValue(columnId))
      .toLowerCase()
      .includes(textOfFn(filterValue).toLowerCase()),
  { autoRemove: (value: unknown) => !textOfFn(value) }
);

/** "Is one of": the console's one select filter. */
const consoleOneOf = Object.assign(
  (row: FilterRow, columnId: string, filterValue: unknown): boolean => {
    const wanted = Array.isArray(filterValue)
      ? filterValue.map(textOfFn)
      : [textOfFn(filterValue)];
    return wanted.includes(textOfFn(row.getValue(columnId)));
  },
  {
    autoRemove: (value: unknown) =>
      Array.isArray(value) ? value.length === 0 : !textOfFn(value),
  }
);

const listTableFeatures = tableFeatures({
  coreReactivityFeature: storeReactivityBindings(),
  columnOrderingFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  rowSortingFeature,
  columnFilteringFeature,
  globalFilteringFeature,
  rowPaginationFeature,
  coreRowModel: createCoreRowModel(),
  sortedRowModel: createSortedRowModel(),
  filteredRowModel: createFilteredRowModel(),
  paginatedRowModel: createPaginatedRowModel(),
  // One filter recipe instead of the stock registry: every console filter is
  // "contains, ignoring case" on text, or "is one of" on a select.
  filterFns: { consoleText, consoleOneOf },
});

type ListFeatures = typeof listTableFeatures;
type ListTableState = TableState<ListFeatures>;

/** How a column's values are compared when its header is clicked. */
export type ListSortKind = 'text' | 'number';

/**
 * One column of one list, as the list declares it.
 *
 * This is the type the views write against; the tanstack column def is built
 * from it. Keeping our own shape means a column carries the things our tables
 * need (a cell template, the `col-*` class the CSS keys on, whether it is
 * numeric) next to the things the model needs, and that a list's columns can
 * be read as a table of contents for the list.
 */
export interface ListColumn<TRow extends RowData> {
  /** Stable id. It is stored in `localStorage`, so renaming one is a version bump. */
  id: string;
  /** The header label. */
  header: string;
  /** The cell body, a lit template or any renderable. */
  cell: (row: TRow) => unknown;
  /** The value the sort compares. Omit on a column that cannot be sorted. */
  value?: (row: TRow) => unknown;
  /** How `value` is compared. Defaults to text. */
  sort?: ListSortKind;
  /** Right-aligned, tabular. Adds the `numeric` class to header and cell. */
  numeric?: boolean;
  /** Extra class on the `<td>`. */
  cellClass?: string;
  /**
   * `data-label` on the `<td>`: the word a stacked phone row prints before
   * the value once the header row is hidden.
   */
  cellLabel?: string;
  /**
   * `title` for the whole cell, for the pattern the console uses on
   * timestamps: the relative time is read, the absolute one is hovered.
   */
  cellTitle?: (row: TRow) => string | undefined;
  /** Extra class on the `<th>`; defaults to `col-<id>`. */
  headerClass?: string;
  /** Declared pixel width. A column without one takes what is left. */
  width?: number;
  /** False for the column that absorbs the leftover width. */
  resizable?: boolean;
  /**
   * Narrowest the column may get, in pixels: the header is never squeezed
   * under it and a resize stops there. Defaults to `MIN_COLUMN_WIDTH`.
   */
  minWidth?: number;
  /**
   * One line, cut with an ellipsis, the whole text on hover (`cellTitle`,
   * else `searchText`, else `value`). For long names and ids that would
   * otherwise wrap a row to three lines or spill out of the cell.
   */
  truncate?: boolean;
  /** False for a column the operator may not switch off. */
  hideable?: boolean;
  /** False for a column that is off until the operator turns it on. */
  visible?: boolean;
  /** Files the column under a heading in the column picker. */
  group?: string;
  /** The name in the picker, when the header label needs context there. */
  pickerLabel?: string;
  /** `title` on the header button. */
  headerTitle?: string;
  /**
   * Accessible name of the header button, when the visible label is an
   * abbreviation ("$ est." is read as "Estimated spend").
   */
  headerLabel?: string;
  /**
   * Offers a per-column filter. `text` is "contains"; `select` is "is one
   * of" the `filterOptions`. Client mode filters on `value`; server mode
   * passes the filter through `onQueryChange` untouched.
   */
  filter?: 'text' | 'select';
  /**
   * Which way the first click sorts. Defaults to descending (recent and
   * expensive first); names and labels read better A to Z.
   */
  firstSort?: 'asc' | 'desc';
  /** The choices of a `select` filter. */
  filterOptions?: Array<{ value: string; label: string }>;
  /**
   * The text the global search matches, when it is not `value`. Return an
   * empty string to keep a column out of the search. Columns without a
   * `value` or `searchText` are never searched.
   */
  searchText?: (row: TRow) => string;
  /**
   * False while the column does not apply (a feature flag is off, a setting
   * is missing). An unavailable column is neither drawn nor offered in the
   * picker, and the operator's stored visibility for it is kept for when it
   * comes back. Read on every paint, so it may depend on view state.
   */
  available?: () => boolean;
}

function isAvailable<TRow extends RowData>(column: ListColumn<TRow>): boolean {
  return column.available ? column.available() : true;
}

/** Which state slices a list remembers between visits. */
export type PersistedSlice =
  | 'columnVisibility'
  | 'columnOrder'
  | 'columnSizing'
  | 'sorting'
  | 'pageSize'
  | 'density';

/** Row height. Compact is for operators scanning long lists. */
export type ListDensity = 'comfortable' | 'compact';

/** Everything the server needs to produce a page, in one object. */
export interface ListQuery {
  sorting: Array<{ id: string; desc: boolean }>;
  /** The global search text, trimmed. */
  search: string;
  /** Per-column filters, by column id. */
  filters: Record<string, string | string[]>;
  /** 0-based. */
  pageIndex: number;
  pageSize: number;
  /** `pageIndex * pageSize`, for offset APIs. */
  offset: number;
}

export interface ListTableOptions<TRow extends RowData> {
  /** The `localStorage` key's list part, e.g. `flow-executions`. */
  listId: string;
  columns: Array<ListColumn<TRow>>;
  getRowId: (row: TRow) => string;
  /**
   * What survives a reload. Column layout does by default; sorting does not,
   * because a client-side sort orders the page in view, and silently
   * reordering one window of a larger set on arrival is exactly what the list
   * avoids doing until someone asks.
   */
  persist?: PersistedSlice[];
  /**
   * The order the list opens in when nothing is remembered or linked. It is
   * left out of the URL, so a list in its own order has a clean address.
   */
  defaultSorting?: Array<{ id: string; desc: boolean }>;
  /** `client` (default) or `server`; see the class comment. */
  mode?: 'client' | 'server';
  /**
   * Rows per page. 0 (the default) turns pagination off, which is what a
   * list that already shows everything it fetched wants.
   */
  pageSize?: number;
  /** What the page size picker offers. */
  pageSizes?: number[];
  /** Server mode: called after any sort, filter or page change. */
  onQueryChange?: (query: ListQuery) => void;
  /**
   * Mirrors sort, search, filters, page and page size into the query string
   * so a filtered, sorted page can be linked, and reads them back on load.
   * Pass a `prefix` when two lists share a page.
   */
  url?: TableUrlOptions | boolean;
}

/** Nothing readable is narrower than this, drag or keyboard. */
export const MIN_COLUMN_WIDTH = 48;

/**
 * High bound for the resize handle's ARIA slider. The table does not cap
 * how far a drag can go; this is only so the separator reports a range.
 */
export const MAX_COLUMN_WIDTH = 2000;

/** What a column with no declared width starts from when it is nudged. */
export const DEFAULT_COLUMN_WIDTH = 150;

const DEFAULT_PERSISTED: PersistedSlice[] = [
  'columnVisibility',
  'columnOrder',
  'columnSizing',
  'pageSize',
  'density',
];

export const DEFAULT_PAGE_SIZES = [25, 50, 100];

/** Filter slices that send a list back to its first page. */
const QUERY_SLICES = new Set(['sorting', 'globalFilter', 'columnFilters']);

/** What the header row needs to draw one cell. */
export interface ListHeader<TRow extends RowData> {
  column: ListColumn<TRow>;
  /** `false`, or the direction this column is sorted in. */
  sorted: false | 'asc' | 'desc';
  /** 1-based position in a multi-column sort, or 0 when it is the only one. */
  sortRank: number;
  sortable: boolean;
  resizable: boolean;
  /** The width to put on the cell, or undefined to let the table decide. */
  width?: number;
  /** `aria-sort` for the cell. */
  ariaSort: 'ascending' | 'descending' | 'none';
}

const textOf = textOfFn;

const numberOf = (value: unknown): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

/** True while tanstack's resize state names an active pointer-drag column. */
const isDraggingColumn = (value: unknown): boolean =>
  typeof value === 'object' &&
  value !== null &&
  typeof (value as { isResizingColumn?: unknown }).isResizingColumn ===
    'string';

/**
 * Drives one list's table model and keeps its host painting.
 *
 * The controller owns the state and hands tanstack a controlled copy, so
 * every change goes through one place that can persist it and ask the host
 * for a repaint. The table instance itself is built once and re-optioned,
 * not rebuilt per render.
 */
export class ListTable<TRow extends RowData> implements ReactiveController {
  readonly listId: string;
  readonly columns: Array<ListColumn<TRow>>;

  private readonly host: ReactiveControllerHost;
  private readonly persisted: PersistedSlice[];
  private readonly byId = new Map<string, ListColumn<TRow>>();
  private readonly table: Table<ListFeatures, TRow>;
  private tableState: ListTableState;
  private rowsData: TRow[] = [];
  private densityValue: ListDensity = 'comfortable';
  private serverRowCount: number | null = null;
  private serverHasMore = false;
  private readonly getRowId: (row: TRow) => string;
  private readonly defaultSorting: Array<{ id: string; desc: boolean }>;
  private readonly onQueryChange?: (query: ListQuery) => void;
  private readonly url: TableUrlOptions | null;
  readonly mode: 'client' | 'server';
  readonly pageSizes: number[];
  /** True when the list pages at all. */
  readonly paginated: boolean;

  constructor(host: ReactiveControllerHost, options: ListTableOptions<TRow>) {
    this.host = host;
    this.listId = options.listId;
    this.columns = options.columns;
    this.persisted = options.persist ?? DEFAULT_PERSISTED;
    this.mode = options.mode ?? 'client';
    this.getRowId = options.getRowId;
    this.defaultSorting = options.defaultSorting ?? [];
    this.onQueryChange = options.onQueryChange;
    this.url = options.url === true ? {} : options.url ? options.url : null;
    this.paginated = (options.pageSize ?? 0) > 0;
    this.pageSizes = options.pageSizes ?? DEFAULT_PAGE_SIZES;
    for (const column of options.columns) this.byId.set(column.id, column);
    const declaredPageSize = options.pageSize ?? 0;

    this.tableState = getInitialTableState(
      listTableFeatures,
      this.initialState(declaredPageSize)
    );
    const server = this.mode === 'server';
    this.table = constructTable<ListFeatures, TRow>({
      features: listTableFeatures,
      data: this.rowsData,
      columns: options.columns.map((column) => this.toColumnDef(column)),
      state: this.tableState,
      // v9 has no single onStateChange: each slice reports its own change,
      // and every one of them lands in the same place so that persisting and
      // repainting cannot be forgotten for one of them.
      onSortingChange: (updater) => this.applySlice('sorting', updater),
      onColumnVisibilityChange: (updater) =>
        this.applySlice('columnVisibility', updater),
      onColumnOrderChange: (updater) => this.applySlice('columnOrder', updater),
      onColumnSizingChange: (updater) =>
        this.applySlice('columnSizing', updater),
      onColumnResizingChange: (updater) =>
        this.applySlice('columnResizing', updater),
      onGlobalFilterChange: (updater) =>
        this.applySlice('globalFilter', updater),
      onColumnFiltersChange: (updater) =>
        this.applySlice('columnFilters', updater),
      onPaginationChange: (updater) => this.applySlice('pagination', updater),
      manualSorting: server,
      manualFiltering: server,
      manualPagination: server || !this.paginated,
      autoResetPageIndex: false,
      globalFilterFn: Object.assign(
        (
          row: { original: TRow; getValue: (id: string) => unknown },
          columnId: string,
          filterValue: unknown
        ) => {
          const declared = this.byId.get(columnId);
          const text = declared?.searchText
            ? declared.searchText(row.original)
            : textOf(row.getValue(columnId));
          return text.toLowerCase().includes(textOf(filterValue).toLowerCase());
        },
        { autoRemove: (value: unknown) => !textOf(value) }
      ),
      getColumnCanGlobalFilter: (column) => {
        const declared = this.byId.get(column.id);
        return Boolean(declared && (declared.searchText || declared.value));
      },
      getRowId: (row: TRow) => options.getRowId(row),
      columnResizeMode: 'onChange',
      enableSortingRemoval: false,
      sortDescFirst: true,
      renderFallbackValue: null,
    });
    host.addController(this);
  }

  /**
   * How test failure messages print the model. Chai walks an object graph to
   * describe it, and tanstack's table is large enough that an assertion
   * failing on a view element holding one hangs the browser instead of
   * failing.
   */
  [Symbol.for('chai/inspect')](): string {
    return `ListTable(${this.listId})`;
  }

  hostConnected(): void {
    // Nothing to subscribe to: every state change comes back through
    // onStateChange, which already asks the host to repaint.
  }

  /**
   * The rows to render: filtered, sorted and paged in client mode, and
   * exactly the rows handed in, in that order, in server mode.
   */
  get rows(): TRow[] {
    return this.table.getRowModel().rows.map((row) => row.original);
  }

  /** The id a row is keyed, selected and stored by. */
  idOf(row: TRow): string {
    return this.getRowId(row);
  }

  /** The rows that pass the filters, before paging (client mode). */
  get filteredRows(): TRow[] {
    return this.table.getPrePaginatedRowModel().rows.map((row) => row.original);
  }

  /** Rows that pass the filters, before paging (client mode). */
  get filteredRowCount(): number {
    return this.table.getPrePaginatedRowModel().rows.length;
  }

  /**
   * Views commonly derive the rows in `willUpdate` (`keys.filter(...)`), so
   * every pass hands over a new array with the same rows. Treating that as
   * new data would rebuild the row model on every paint, and a repaint per
   * rebuild is a loop; the same rows in the same order are not a change.
   */
  set data(rows: TRow[]) {
    if (rows === this.rowsData) return;
    if (
      rows.length === this.rowsData.length &&
      rows.every((row, index) => row === this.rowsData[index])
    ) {
      return;
    }
    this.rowsData = rows;
    this.table.setOptions((options) => ({ ...options, data: rows }));
    // A refetch that shrank the list must not strand the operator on a page
    // past its end.
    if (this.mode === 'client' && this.paginated && this.pageIndex > 0) {
      const lastPage = Math.max(
        0,
        Math.ceil(this.filteredRowCount / this.pageSize) - 1
      );
      if (this.pageIndex > lastPage) this.setPageIndex(lastPage);
    }
  }

  get data(): TRow[] {
    return this.rowsData;
  }

  /** The visible columns, in display order. */
  get visibleColumns(): Array<ListColumn<TRow>> {
    const visible: Array<ListColumn<TRow>> = [];
    for (const column of this.table.getVisibleLeafColumns()) {
      const declared = this.byId.get(column.id);
      if (declared && isAvailable(declared)) visible.push(declared);
    }
    return visible;
  }

  /** One entry per visible column, with everything a `<th>` needs. */
  get headers(): Array<ListHeader<TRow>> {
    const sorting = this.tableState.sorting ?? [];
    return this.table.getLeafHeaders().flatMap((header) => {
      const declared = this.byId.get(header.column.id);
      if (!declared || !isAvailable(declared)) return [];
      const sorted = header.column.getIsSorted();
      const rank = sorting.findIndex((entry) => entry.id === header.column.id);
      return [
        {
          column: declared,
          sorted,
          sortRank: sorting.length > 1 && rank >= 0 ? rank + 1 : 0,
          sortable: header.column.getCanSort(),
          resizable: header.column.getCanResize(),
          width: this.widthOf(declared),
          ariaSort:
            sorted === 'asc'
              ? 'ascending'
              : sorted === 'desc'
                ? 'descending'
                : 'none',
        },
      ];
    });
  }

  /**
   * First click sorts descending, because recent and expensive lead, and the
   * cycle never returns to the server's order: an operator who sorted a
   * column wants it sorted. Shift adds a column to the sort rather than
   * replacing it.
   */
  toggleSort(columnId: string, multi = false): void {
    this.table.getColumn(columnId)?.toggleSorting(undefined, multi);
  }

  /** The sorts in force, outermost first. */
  get sorting(): Array<{ id: string; desc: boolean }> {
    return (this.tableState.sorting ?? []).map((entry) => ({ ...entry }));
  }

  setSorting(sorting: Array<{ id: string; desc: boolean }>): void {
    this.table.setSorting(sorting.map((entry) => ({ ...entry })));
  }

  // ----- Search, filters, pagination --------------------------------------

  get search(): string {
    return textOf(this.tableState.globalFilter);
  }

  setSearch(value: string): void {
    this.table.setGlobalFilter(value);
  }

  filterValue(columnId: string): string | string[] {
    const entry = (this.tableState.columnFilters ?? []).find(
      (filter) => filter.id === columnId
    );
    return (entry?.value as string | string[] | undefined) ?? '';
  }

  setFilter(columnId: string, value: string | string[] | null): void {
    this.table.setColumnFilters((filters: ColumnFiltersState) => {
      const rest = filters.filter((filter) => filter.id !== columnId);
      const empty =
        value === null || (Array.isArray(value) ? !value.length : !value);
      return empty ? rest : [...rest, { id: columnId, value }];
    });
  }

  /** Drops the search and every column filter. */
  clearFilters(): void {
    this.table.setGlobalFilter('');
    this.table.setColumnFilters([]);
  }

  get filtersActive(): boolean {
    return (
      Boolean(this.search) || (this.tableState.columnFilters ?? []).length > 0
    );
  }

  get pageIndex(): number {
    return this.tableState.pagination?.pageIndex ?? 0;
  }

  get pageSize(): number {
    return this.tableState.pagination?.pageSize ?? 0;
  }

  setPageIndex(index: number): void {
    this.table.setPageIndex(Math.max(0, index));
  }

  setPageSize(size: number): void {
    if (!(size > 0)) return;
    this.table.setPagination({ pageIndex: 0, pageSize: size });
  }

  /**
   * The total the server reported, for server mode. Pass null when the API
   * does not count (cursor APIs) and use `hasMore` instead.
   */
  set totalRows(total: number | null) {
    this.serverRowCount = total;
    this.table.setOptions((options) => ({
      ...options,
      rowCount: total ?? undefined,
    }));
  }

  /** How many rows the whole list has, or null when nobody counted. */
  get totalRows(): number | null {
    if (this.mode === 'server') return this.serverRowCount;
    return this.filteredRowCount;
  }

  /** Server mode without a total: whether there is a page after this one. */
  set hasMore(more: boolean) {
    this.serverHasMore = more;
  }

  get canPreviousPage(): boolean {
    return this.pageIndex > 0;
  }

  get canNextPage(): boolean {
    if (!this.paginated) return false;
    const total = this.totalRows;
    if (total === null) return this.serverHasMore;
    return (this.pageIndex + 1) * this.pageSize < total;
  }

  /** The query a server-mode list fetches with. */
  get query(): ListQuery {
    const filters: Record<string, string | string[]> = {};
    for (const filter of this.tableState.columnFilters ?? []) {
      filters[filter.id] = filter.value as string | string[];
    }
    const pageSize = this.pageSize;
    return {
      sorting: this.sorting,
      search: this.search.trim(),
      filters,
      pageIndex: this.pageIndex,
      pageSize,
      offset: this.pageIndex * pageSize,
    };
  }

  get density(): ListDensity {
    return this.densityValue;
  }

  setDensity(density: ListDensity): void {
    if (density === this.densityValue) return;
    this.densityValue = density;
    this.save();
    this.host.requestUpdate();
  }

  isVisible(columnId: string): boolean {
    return this.table.getColumn(columnId)?.getIsVisible() ?? false;
  }

  setVisible(columnId: string, visible: boolean): void {
    this.table.getColumn(columnId)?.toggleVisibility(visible);
  }

  /** Back to the list's declared columns, widths and order. */
  resetColumns(): void {
    this.table.setColumnVisibility(this.declaredVisibility());
    this.table.setColumnOrder([]);
    this.table.setColumnSizing({});
    clearTablePreferences(this.listId);
  }

  /** True when anything about the columns differs from the declared layout. */
  get hasColumnChanges(): boolean {
    const declared = this.declaredVisibility();
    const current = this.tableState.columnVisibility ?? {};
    for (const column of this.columns) {
      if ((current[column.id] ?? true) !== (declared[column.id] ?? true)) {
        return true;
      }
    }
    if (Object.keys(this.tableState.columnSizing ?? {}).length) return true;
    return (this.tableState.columnOrder ?? []).length > 0;
  }

  /** The picker's model: label, group and current state per column. */
  get pickerColumns(): Array<{
    id: string;
    label: string;
    group?: string;
    visible: boolean;
    hideable: boolean;
  }> {
    return this.columns.filter(isAvailable).map((column) => ({
      id: column.id,
      label: column.pickerLabel || column.header,
      group: column.group,
      visible: this.isVisible(column.id),
      hideable: column.hideable !== false,
    }));
  }

  /**
   * Starts a resize drag from a header handle.
   *
   * tanstack installs and removes the move/release listeners itself, on the
   * document, because a drag that leaves the header must keep tracking.
   */
  startResize(columnId: string, event: MouseEvent | TouchEvent): void {
    const header = this.table
      .getLeafHeaders()
      .find((candidate) => candidate.column.id === columnId);
    if (!header) return;
    event.preventDefault();
    event.stopPropagation();
    header.getResizeHandler(document)(event);
  }

  /**
   * Nudges a column's width, for the keyboard path on the resize handle.
   *
   * A control that only answers a mouse drag is not a control for everyone,
   * and the ARIA window-splitter pattern this handle follows expects the
   * arrow keys to move it.
   */
  resizeBy(columnId: string, delta: number): void {
    const column = this.byId.get(columnId);
    if (!column || column.resizable === false) return;
    const current = this.widthOf(column) ?? DEFAULT_COLUMN_WIDTH;
    const next = Math.max(
      this.byId.get(columnId)?.minWidth ?? MIN_COLUMN_WIDTH,
      Math.round(current + delta)
    );
    this.table.setColumnSizing((sizing) => ({ ...sizing, [columnId]: next }));
  }

  /** The width a cell is drawn at, dragged or declared. */
  widthOfColumn(columnId: string): number | undefined {
    const column = this.byId.get(columnId);
    return column ? this.widthOf(column) : undefined;
  }

  /** True while a handle is being dragged, so the view can show it. */
  get resizingColumn(): string | null {
    const active = this.tableState.columnResizing?.isResizingColumn;
    return typeof active === 'string' ? active : null;
  }

  /**
   * The width to put on a cell: what the operator dragged it to, else what
   * the list declared, else nothing at all. A column with no width is the one
   * that takes whatever the fixed-width columns leave over, and forcing a
   * number on it would break that.
   */
  private widthOf(column: ListColumn<TRow>): number | undefined {
    const sized = this.tableState.columnSizing?.[column.id];
    if (typeof sized === 'number' && sized > 0) return sized;
    return column.width;
  }

  private declaredVisibility(): Record<string, boolean> {
    const visibility: Record<string, boolean> = {};
    for (const column of this.columns) {
      visibility[column.id] = column.visible !== false;
    }
    return visibility;
  }

  /** Declared defaults, with anything remembered for this list on top. */
  private initialState(declaredPageSize: number): Partial<ListTableState> {
    const stored = readTablePreferences(this.listId) ?? {};
    if (this.persists('density') && stored.density) {
      this.densityValue = stored.density;
    }
    const visibility = this.declaredVisibility();
    if (this.persists('columnVisibility') && stored.columnVisibility) {
      for (const [id, value] of Object.entries(stored.columnVisibility)) {
        // A remembered id the list no longer has is dropped rather than
        // carried: it would hide nothing and confuse the reset check.
        if (this.byId.has(id)) visibility[id] = value;
      }
    }
    const order =
      this.persists('columnOrder') && stored.columnOrder
        ? stored.columnOrder.filter((id) => this.byId.has(id))
        : [];
    const sizing: Record<string, number> = {};
    if (this.persists('columnSizing') && stored.columnSizing) {
      for (const [id, value] of Object.entries(stored.columnSizing)) {
        if (this.byId.has(id)) sizing[id] = value;
      }
    }
    let sorting =
      this.persists('sorting') && stored.sorting
        ? stored.sorting.filter((entry) => this.byId.has(entry.id))
        : [];
    if (!sorting.length) sorting = this.defaultSorting.map((e) => ({ ...e }));
    let pageSize =
      this.persists('pageSize') && stored.pageSize && declaredPageSize > 0
        ? stored.pageSize
        : declaredPageSize;
    let pageIndex = 0;
    let globalFilter = '';
    let columnFilters: ColumnFiltersState = [];
    // A link beats a remembered preference: it is what someone meant to show.
    if (this.url) {
      const fromUrl = readTableUrlState(this.url);
      if (fromUrl.sorting) {
        sorting = fromUrl.sorting.filter((entry) => this.byId.has(entry.id));
      }
      if (fromUrl.search) globalFilter = fromUrl.search;
      columnFilters = Object.entries(fromUrl.filters)
        .filter(([id]) => this.byId.get(id)?.filter)
        .map(([id, value]) => ({ id, value }));
      if (declaredPageSize > 0) {
        if (fromUrl.pageSize) pageSize = fromUrl.pageSize;
        if (fromUrl.pageIndex) pageIndex = fromUrl.pageIndex;
      }
    }
    return {
      columnVisibility: visibility,
      columnOrder: order,
      columnSizing: sizing,
      sorting,
      globalFilter,
      columnFilters,
      pagination: { pageIndex, pageSize: pageSize || 0 },
    };
  }

  private persists(slice: PersistedSlice): boolean {
    return this.persisted.includes(slice);
  }

  private applySlice<TSlice extends keyof ListTableState>(
    slice: TSlice,
    updater: Updater<ListTableState[TSlice]>
  ): void {
    const previous = this.tableState[slice];
    const next =
      typeof updater === 'function'
        ? (
            updater as (value: ListTableState[TSlice]) => ListTableState[TSlice]
          )(previous)
        : updater;
    if (next === previous) return;
    this.tableState = { ...this.tableState, [slice]: next };
    // A new sort or filter makes "page 4" meaningless: back to the start.
    if (QUERY_SLICES.has(slice) && this.pageIndex !== 0) {
      this.tableState = {
        ...this.tableState,
        pagination: { ...this.tableState.pagination, pageIndex: 0 },
      };
    }
    const state = this.tableState;
    this.table.setOptions((options) => ({ ...options, state }));
    // columnResizeMode is 'onChange', so a pointer drag fires sizing on
    // every move. Preview that live, but write localStorage only when the
    // drag ends (columnResizing clears) or when there is no drag at all —
    // keyboard resize never sets resizingColumn, so it still persists.
    if (slice === 'columnResizing') {
      const wasDragging = isDraggingColumn(previous);
      const stillDragging = isDraggingColumn(next);
      if (wasDragging && !stillDragging) this.save();
    } else if (slice !== 'columnSizing' || this.resizingColumn === null) {
      this.save();
    }
    if (QUERY_SLICES.has(slice) || slice === 'pagination') {
      if (this.url) writeTableUrlState(this.url, this.urlSnapshot());
      if (this.mode === 'server') this.onQueryChange?.(this.query);
    }
    this.host.requestUpdate();
  }

  private urlSnapshot() {
    const query = this.query;
    const isDefault =
      query.sorting.length === this.defaultSorting.length &&
      query.sorting.every(
        (entry, index) =>
          entry.id === this.defaultSorting[index].id &&
          entry.desc === this.defaultSorting[index].desc
      );
    return {
      sorting: isDefault ? [] : query.sorting,
      search: query.search,
      filters: query.filters,
      pageIndex: this.paginated ? query.pageIndex : 0,
      pageSize: this.paginated ? query.pageSize : 0,
    };
  }

  private save(): void {
    const preferences: StoredTablePreferences = {};
    if (this.persists('columnVisibility')) {
      preferences.columnVisibility = this.tableState.columnVisibility;
    }
    if (this.persists('columnOrder')) {
      preferences.columnOrder = this.tableState.columnOrder;
    }
    if (this.persists('columnSizing')) {
      preferences.columnSizing = this.tableState.columnSizing;
    }
    if (this.persists('sorting')) {
      preferences.sorting = this.tableState.sorting;
    }
    if (this.persists('pageSize') && this.paginated) {
      preferences.pageSize = this.pageSize;
    }
    if (this.persists('density') && this.densityValue !== 'comfortable') {
      preferences.density = this.densityValue;
    }
    writeTablePreferences(this.listId, preferences);
  }

  private toColumnDef(column: ListColumn<TRow>) {
    const value = column.value;
    const compare =
      column.sort === 'number'
        ? (a: unknown, b: unknown) => numberOf(a) - numberOf(b)
        : (a: unknown, b: unknown) => textOf(a).localeCompare(textOf(b));
    return {
      id: column.id,
      accessorFn: (row: TRow) => (value ? value(row) : undefined),
      header: column.header,
      enableSorting: Boolean(value),
      enableHiding: column.hideable !== false,
      enableResizing: column.resizable !== false,
      size: column.width,
      minSize: column.minWidth ?? MIN_COLUMN_WIDTH,
      sortDescFirst: column.firstSort !== 'asc',
      enableColumnFilter: Boolean(column.filter),
      enableGlobalFilter: Boolean(column.searchText || column.value),
      filterFn: (column.filter === 'select'
        ? 'consoleOneOf'
        : 'consoleText') as 'consoleOneOf' | 'consoleText',
      sortFn: (
        rowA: { getValue: (id: string) => unknown },
        rowB: { getValue: (id: string) => unknown },
        columnId: string
      ) => compare(rowA.getValue(columnId), rowB.getValue(columnId)),
    };
  }
}
