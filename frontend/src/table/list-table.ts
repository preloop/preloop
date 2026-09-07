import type { ReactiveController, ReactiveControllerHost } from 'lit';
import {
  columnOrderingFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  constructTable,
  createCoreRowModel,
  createSortedRowModel,
  getInitialTableState,
  rowSortingFeature,
  tableFeatures,
} from '@tanstack/table-core';
import type { RowData, Table, TableState, Updater } from '@tanstack/table-core';
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
 * library: sorting, visibility, ordering, sizing and resizing. Selection
 * stays with `list-selection.ts`, which already does it, and row expansion is
 * one more feature import on the day a list grows a tree.
 */
const listTableFeatures = tableFeatures({
  coreReactivityFeature: storeReactivityBindings(),
  columnOrderingFeature,
  columnResizingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  rowSortingFeature,
  coreRowModel: createCoreRowModel(),
  sortedRowModel: createSortedRowModel(),
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
}

/** Which state slices a list remembers between visits. */
export type PersistedSlice =
  'columnVisibility' | 'columnOrder' | 'columnSizing' | 'sorting';

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
}

/** Nothing readable is narrower than this, drag or keyboard. */
const MIN_COLUMN_WIDTH = 48;

/** What a column with no declared width starts from when it is nudged. */
const DEFAULT_COLUMN_WIDTH = 150;

const DEFAULT_PERSISTED: PersistedSlice[] = [
  'columnVisibility',
  'columnOrder',
  'columnSizing',
];

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

const textOf = (value: unknown): string =>
  value === null || value === undefined ? '' : String(value).trim();

const numberOf = (value: unknown): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

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

  constructor(host: ReactiveControllerHost, options: ListTableOptions<TRow>) {
    this.host = host;
    this.listId = options.listId;
    this.columns = options.columns;
    this.persisted = options.persist ?? DEFAULT_PERSISTED;
    for (const column of options.columns) this.byId.set(column.id, column);

    this.tableState = getInitialTableState(
      listTableFeatures,
      this.initialState()
    );
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
      getRowId: (row: TRow) => options.getRowId(row),
      columnResizeMode: 'onChange',
      enableSortingRemoval: false,
      sortDescFirst: true,
      renderFallbackValue: null,
    });
    host.addController(this);
  }

  hostConnected(): void {
    // Nothing to subscribe to: every state change comes back through
    // onStateChange, which already asks the host to repaint.
  }

  /** The rows to render, sorted by whatever the headers say. */
  get rows(): TRow[] {
    return this.table.getRowModel().rows.map((row) => row.original);
  }

  set data(rows: TRow[]) {
    if (rows === this.rowsData) return;
    this.rowsData = rows;
    this.table.setOptions((options) => ({ ...options, data: rows }));
  }

  get data(): TRow[] {
    return this.rowsData;
  }

  /** The visible columns, in display order. */
  get visibleColumns(): Array<ListColumn<TRow>> {
    const visible: Array<ListColumn<TRow>> = [];
    for (const column of this.table.getVisibleLeafColumns()) {
      const declared = this.byId.get(column.id);
      if (declared) visible.push(declared);
    }
    return visible;
  }

  /** One entry per visible column, with everything a `<th>` needs. */
  get headers(): Array<ListHeader<TRow>> {
    const sorting = this.tableState.sorting ?? [];
    return this.table.getLeafHeaders().flatMap((header) => {
      const declared = this.byId.get(header.column.id);
      if (!declared) return [];
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
    return this.columns.map((column) => ({
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
    const next = Math.max(MIN_COLUMN_WIDTH, Math.round(current + delta));
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
  private initialState(): Partial<ListTableState> {
    const stored = readTablePreferences(this.listId) ?? {};
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
    const sorting =
      this.persists('sorting') && stored.sorting
        ? stored.sorting.filter((entry) => this.byId.has(entry.id))
        : [];
    return {
      columnVisibility: visibility,
      columnOrder: order,
      columnSizing: sizing,
      sorting,
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
    const state = this.tableState;
    this.table.setOptions((options) => ({ ...options, state }));
    // The drag itself is transient: only what it settles on is remembered.
    if (slice !== 'columnResizing') this.save();
    this.host.requestUpdate();
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
      sortDescFirst: true,
      sortFn: (
        rowA: { getValue: (id: string) => unknown },
        rowB: { getValue: (id: string) => unknown },
        columnId: string
      ) => compare(rowA.getValue(columnId), rowB.getValue(columnId)),
    };
  }
}
