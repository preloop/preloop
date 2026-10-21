# Console lists: `src/table`

One way to build a list in the console. A headless model (`ListTable`, on
`@tanstack/table-core` v9) holds the state; a light-DOM element
(`<data-table>`) draws it with the console's own `.styled-table` markup; a
handful of shared primitives make lists that are not tables (timelines, card
grids) look and behave the same.

## Pieces

| Piece | What it is |
|---|---|
| `ListTable` (`list-table.ts`) | Reactive controller. Columns, sort, global search, per-column filters, pagination, visibility, order, widths, density. Client or server mode. Optional URL state. |
| `<data-table>` (`data-table.ts`) | Renders a `ListTable`: select column, sortable and resizable headers with a column picker in the final header cell, cells, row actions menu, skeleton, empty / no-results / error states, pagination footer, sticky header, row keyboard navigation. Light DOM, so the view's styles reach the cells. |
| `renderDataTableToolbar()` (`data-table-toolbar.ts`) | The shared `list-toolbar` wired to the model: search, the page's filters, count, and the bulk bar docked in the toolbar while rows are selected. |
| `<table-pagination>` | The one pagination footer: "26-50 of 1,412 runs", rows per page, Previous / Next. Offset (`total`) or cursor (`total = null` plus `has-next`). |
| `<table-state>` | The one empty / no-results / error panel, with a slot for the action. |
| `<column-picker>` | Column visibility (grouped), reset, and density. |
| `table-preferences.ts` | Per-browser preferences per list in `localStorage` (`preloop.table.<listId>`), versioned. |
| `table-url-state.ts` | `?sort=-created,name&q=..&f.status=failed&page=2&size=50`, written with `replaceState`, unrelated parameters kept. |

Selection stays in `components/list-selection.ts` (`ListSelectionController`,
`list-bulk-bar`); `<data-table>` takes the controller as a property.

## A list in five steps

```ts
static styles = [tableScrollStyles, unsafeCSS(consoleStyles), listTableStyles, dataTableStyles];

readonly selection = new ListSelectionController<Key>(this, { idOf: (k) => k.id });
readonly table = new ListTable<Key>(this, {
  listId: 'api-keys',            // localStorage key part; never rename
  getRowId: (k) => k.id,
  pageSize: 50,                   // 0 = no pagination
  url: true,                      // sort, search, filters, page in the URL
  columns: [
    { id: 'name', header: 'Name', hideable: false, firstSort: 'asc',
      value: (k) => k.name, cell: (k) => html`<a href=...>${k.name}</a>` },
    { id: 'created', header: 'Created', sort: 'number',
      value: (k) => Date.parse(k.created_at), searchText: () => '',
      cell: (k) => formatDate(k.created_at) },
    { id: 'scope', header: 'Scope', filter: 'select',
      filterOptions: [{ value: 'admin', label: 'Admin' }], value: (k) => k.scope,
      cell: (k) => k.scope },
  ],
});

protected willUpdate() {
  this.table.data = this.keys;                 // same rows => no rebuild
  this.selection.setItems(this.table.rows);    // select what is on the page
}

render() {
  const emptyState = {
    heading: 'No API keys yet',
    action: { label: 'Add an API key', onClick },
  };
  return html`
    ${renderDataTableToolbar({ table: this.table, selection: this.selection,
      bulkActions: this.bulkActions, onBulkAction: (id) => this.bulk(id),
      searchPlaceholder: 'Search keys', count: `${this.table.totalRows} keys` })}
    <data-table .table=${this.table} .rows=${this.table.rows}
      .selection=${this.selection} .rowActions=${(k) => this.actionsFor(k)}
      .loading=${this.loading} .error=${this.error} @retry=${this.load}
      .empty=${emptyState}
      label="API keys" noun="keys" activatable @row-activate=${this.open}>
    </data-table>`;
}
```

Pass `.rows=${this.table.rows}`: the getter returns a fresh array, which is
what repaints the light-DOM table when the view repaints.

## Server mode

```ts
new ListTable<Run>(this, {
  listId: 'runs', mode: 'server', pageSize: 50, url: true, columns,
  getRowId: (r) => r.id,
  onQueryChange: (q) => this.load(q),   // { sorting, search, filters, pageIndex, pageSize, offset }
});
// after a fetch:
this.table.data = page.items;
this.table.totalRows = page.total;      // or null, then:
this.table.hasMore = Boolean(page.next_cursor);
```

Rows are drawn as given. A sort or filter change resets to page 1 before
`onQueryChange` fires. Debounce search in the view if the API is slow.

## Rules

- Column ids are a storage and URL contract. Renaming one means bumping
  `TABLE_PREFERENCES_VERSION` or accepting that people lose that column's
  setting.
- First click sorts descending (recent, expensive first); set
  `firstSort: 'asc'` on names and labels. `defaultSorting` is the order a
  list opens in and stays out of the URL.
- Columns that should not match the global search return `''` from
  `searchText` (dates, counts).
- Lists that are not tables (the audit timeline, card grids) still use
  `list-toolbar`, `<table-pagination>` and `<table-state>`, so every list
  pages, filters and says "nothing here" the same way.
- Sticky headers need a bounded scroll box; set `--data-table-max-height`
  on the host if the default (`100dvh - 260px`) does not suit the page, or
  `sticky-header="false"` (property `.stickyHeader=${false}`) for short lists.
