import { aTimeout, expect, fixture, html } from '@open-wc/testing';
import { LitElement } from 'lit';
import { customElement } from 'lit/decorators.js';

import { ListSelectionController } from '../components/list-selection';
import { ListTable } from './list-table';
import type { ListColumn, ListQuery } from './list-table';
import { dataTableStyles } from './data-table-styles';
import { listTableStyles } from './list-table-styles';
import { renderDataTableToolbar } from './data-table-toolbar';
import { readTableUrlState, writeTableUrlState } from './table-url-state';
import { tablePreferencesKey } from './table-preferences';
import './data-table';
import './table-pagination';
import type { TablePagination } from './table-pagination';

interface Key {
  id: string;
  name: string;
  scope: string;
  uses: number;
}

const KEYS: Key[] = Array.from({ length: 30 }, (_, index) => ({
  id: `k${index}`,
  name: `key-${String(index).padStart(2, '0')}`,
  scope: index % 3 === 0 ? 'admin' : 'read',
  uses: index * 7,
}));

/** Drives the `available()` column below; off unless a test turns it on. */
let notesAvailable = false;

const COLUMNS: Array<ListColumn<Key>> = [
  {
    id: 'name',
    header: 'Name',
    hideable: false,
    value: (row) => row.name,
    cell: (row) => html`<a href=${`/keys/${row.id}`}>${row.name}</a>`,
  },
  {
    id: 'scope',
    header: 'Scope',
    filter: 'select',
    filterOptions: [
      { value: 'admin', label: 'Admin' },
      { value: 'read', label: 'Read' },
    ],
    value: (row) => row.scope,
    cell: (row) => row.scope,
  },
  {
    id: 'uses',
    header: 'Uses',
    numeric: true,
    sort: 'number',
    value: (row) => row.uses,
    searchText: () => '',
    cell: (row) => String(row.uses),
  },
  {
    id: 'notes',
    header: 'Notes',
    available: () => notesAvailable,
    cell: (row) => `note ${row.id}`,
  },
];

let counter = 0;

@customElement('test-data-table-host')
class Host extends LitElement {
  static styles = [dataTableStyles, listTableStyles];
  listId = `dt-${++counter}`;
  mode: 'client' | 'server' = 'client';
  url = false;
  queries: ListQuery[] = [];
  activated: Key[] = [];
  activatedPrevented: boolean[] = [];
  loading = false;
  error: string | null = null;
  table!: ListTable<Key>;
  selection = new ListSelectionController<Key>(this, { idOf: (k) => k.id });
  expanded = new Set<string>();

  init() {
    this.table = new ListTable<Key>(this, {
      listId: this.listId,
      columns: COLUMNS,
      getRowId: (row) => row.id,
      pageSize: 10,
      mode: this.mode,
      url: this.url,
      onQueryChange: (query) => this.queries.push(query),
    });
    this.table.data = KEYS;
    this.selection.setItems(this.table.rows);
  }

  render() {
    return html`
      ${renderDataTableToolbar({
        table: this.table,
        selection: this.selection,
        bulkActions: [{ id: 'revoke', label: 'Revoke' }],
        count: `${this.table.totalRows} keys`,
      })}
      <data-table
        .table=${this.table}
        .rows=${this.table.rows}
        .selection=${this.selection}
        .rowActions=${(row: Key) => [{ id: 'del', label: `Delete ${row.name}` }]}
        .rowLabel=${(row: Key) => row.name}
        .rowDetail=${(row: Key) =>
          this.expanded.has(row.id)
            ? html`<p class="detail">Detail of ${row.name}</p>`
            : null}
        .loading=${this.loading}
        .error=${this.error}
        .empty=${{ heading: 'No API keys yet' }}
        label="API keys"
        noun="keys"
        activatable
        @row-activate=${(event: CustomEvent) => {
          this.activated.push(event.detail.row);
          this.activatedPrevented.push(event.detail.event.defaultPrevented);
        }}
      ></data-table>
    `;
  }
}

async function mount(setup: (host: Host) => void = () => {}): Promise<Host> {
  const host = document.createElement('test-data-table-host') as Host;
  setup(host);
  host.init();
  const el = await fixture<Host>(host);
  await el.updateComplete;
  return el;
}

const q = (host: Host, selector: string) =>
  host.shadowRoot!.querySelector(selector) as HTMLElement;
const qa = (host: Host, selector: string) => [
  ...host.shadowRoot!.querySelectorAll<HTMLElement>(selector),
];

async function settle(host: Host) {
  await host.updateComplete;
  const table = q(host, 'data-table') as LitElement;
  await table.updateComplete;
  const pagination = host.shadowRoot!.querySelector(
    'table-pagination'
  ) as TablePagination | null;
  await pagination?.updateComplete;
}

describe('ListTable: filtering, pagination, query', () => {
  afterEach(() => {
    window.history.replaceState(null, '', window.location.pathname);
  });

  it('pages client rows and reports the filtered total', async () => {
    const host = await mount();
    expect(host.table.rows).to.have.length(10);
    expect(host.table.totalRows).to.equal(30);
    expect(host.table.canNextPage).to.be.true;
    host.table.setPageIndex(2);
    expect(host.table.rows.map((r) => r.id)[0]).to.equal('k20');
    expect(host.table.canNextPage).to.be.false;
  });

  it('global search matches searchable columns and skips opted-out ones', async () => {
    const host = await mount();
    host.table.setSearch('key-1');
    expect(host.table.totalRows).to.equal(10);
    // "uses" opted out of search: 70 is a value of k10 but must not match.
    host.table.setSearch('70');
    expect(host.table.totalRows).to.equal(0);
  });

  it('a select filter keeps only matching rows, and clearing restores', async () => {
    const host = await mount();
    host.table.setFilter('scope', ['admin']);
    expect(host.table.totalRows).to.equal(10);
    expect(host.table.filtersActive).to.be.true;
    host.table.clearFilters();
    expect(host.table.totalRows).to.equal(30);
  });

  it('a new sort or filter returns to the first page', async () => {
    const host = await mount();
    host.table.setPageIndex(2);
    host.table.toggleSort('uses');
    expect(host.table.pageIndex).to.equal(0);
    expect(host.table.rows[0].id).to.equal('k29');
  });

  it('clamps the page when new data is shorter', async () => {
    const host = await mount();
    host.table.setPageIndex(2);
    host.table.data = KEYS.slice(0, 12);
    expect(host.table.pageIndex).to.equal(1);
  });

  it('server mode leaves rows alone and reports every query change', async () => {
    const host = await mount((h) => (h.mode = 'server'));
    host.table.totalRows = 300;
    expect(host.table.rows).to.have.length(30);
    host.table.toggleSort('name');
    host.table.setSearch('abc');
    host.table.setPageIndex(3);
    const last = host.queries[host.queries.length - 1];
    expect(last).to.deep.include({
      search: 'abc',
      pageIndex: 3,
      pageSize: 10,
      offset: 30,
    });
    expect(last.sorting).to.deep.equal([{ id: 'name', desc: true }]);
    expect(host.table.canNextPage).to.be.true;
  });

  it('server mode without a total pages on hasMore (cursor APIs)', async () => {
    const host = await mount((h) => (h.mode = 'server'));
    host.table.totalRows = null;
    host.table.hasMore = true;
    expect(host.table.canNextPage).to.be.true;
    host.table.hasMore = false;
    expect(host.table.canNextPage).to.be.false;
  });

  it('remembers page size and density per list', async () => {
    const host = await mount();
    host.table.setPageSize(50);
    host.table.setDensity('compact');
    const stored = JSON.parse(
      localStorage.getItem(tablePreferencesKey(host.listId))!
    );
    expect(stored.pageSize).to.equal(50);
    expect(stored.density).to.equal('compact');
    const again = await mount((h) => (h.listId = host.listId));
    expect(again.table.pageSize).to.equal(50);
    expect(again.table.density).to.equal('compact');
    localStorage.removeItem(tablePreferencesKey(host.listId));
  });

  it('writes sort, search, filters and page into the URL and reads them back', async () => {
    const host = await mount((h) => (h.url = true));
    host.table.toggleSort('uses');
    host.table.setFilter('scope', ['read']);
    host.table.setSearch('key');
    host.table.setPageIndex(1);
    const params = new URLSearchParams(window.location.search);
    expect(params.get('sort')).to.equal('-uses');
    expect(params.getAll('f.scope')).to.deep.equal(['read']);
    expect(params.get('q')).to.equal('key');
    expect(params.get('page')).to.equal('2');

    const again = await mount((h) => (h.url = true));
    expect(again.table.sorting).to.deep.equal([{ id: 'uses', desc: true }]);
    expect(again.table.search).to.equal('key');
    expect(again.table.filterValue('scope')).to.equal('read');
    expect(again.table.pageIndex).to.equal(1);
  });

  it('ignores URL filters for columns that do not filter', () => {
    window.history.replaceState(
      null,
      '',
      '?f.name=x&f.scope=admin&size=9999&page=-1'
    );
    const state = readTableUrlState({});
    expect(state.filters).to.deep.equal({ name: 'x', scope: 'admin' });
    expect(state.pageSize).to.be.undefined;
    expect(state.pageIndex).to.be.undefined;
  });

  it('keeps unrelated parameters and drops cleared filters', () => {
    window.history.replaceState(null, '', '?tab=keys&f.scope=admin#top');
    writeTableUrlState(
      { prefix: 'keys' },
      { sorting: [], search: '', filters: {}, pageIndex: 0, pageSize: 0 }
    );
    expect(window.location.search).to.equal('?tab=keys&f.scope=admin');
    writeTableUrlState(
      {},
      { sorting: [], search: '', filters: {}, pageIndex: 0, pageSize: 0 }
    );
    expect(window.location.search).to.equal('?tab=keys');
    expect(window.location.hash).to.equal('#top');
  });
});

describe('<data-table>', () => {
  it('fits the header picker inside its header cell', async () => {
    const host = await mount();
    await settle(host);
    const within = (selector: string) => {
      const cell = q(host, selector).getBoundingClientRect();
      const picker = q(host, `${selector} column-picker`);
      const trigger = picker.shadowRoot!.querySelector(
        'sl-button[slot="trigger"]'
      ) as HTMLElement;
      const box = trigger.getBoundingClientRect();
      expect(box.width).to.be.greaterThan(0);
      expect(box.left).to.be.at.least(cell.left - 0.5);
      expect(box.right).to.be.at.most(cell.right + 0.5);
      expect(trigger.getAttribute('aria-label')).to.equal('Columns');
      expect(trigger.textContent!.trim()).to.equal('');
    };
    within('thead th.actions-cell');

    const table = q(
      host,
      'data-table'
    ) as import('./data-table').DataTable<Key>;
    table.rowActions = undefined;
    await settle(host);
    within('thead th:last-child');
  });

  it('places the picker in the last header cell without adding a column or a toolbar control', async () => {
    const host = await mount();
    await settle(host);
    expect(q(host, 'list-toolbar').querySelector('column-picker')).to.be.null;
    expect(qa(host, 'thead tr column-picker')).to.have.length(1);
    expect(q(host, 'thead th:last-child column-picker')).to.exist;
    expect(qa(host, 'thead th')).to.have.length(5);
    expect(qa(host, 'tbody tr:first-child td')).to.have.length(5);

    const table = q(
      host,
      'data-table'
    ) as import('./data-table').DataTable<Key>;
    table.rowActions = undefined;
    await table.updateComplete;
    expect(qa(host, 'thead th')).to.have.length(4);
    expect(q(host, 'thead th:last-child .sort-button').textContent).to.contain(
      'Uses'
    );
    const picker = q(host, 'thead th:last-child column-picker');
    const rowBounds = q(host, 'thead tr').getBoundingClientRect();
    const pickerBounds = picker.getBoundingClientRect();
    expect(
      Math.abs(
        (pickerBounds.top + pickerBounds.bottom) / 2 -
          (rowBounds.top + rowBounds.bottom) / 2
      )
    ).to.be.lessThan(2);
    picker.dispatchEvent(
      new CustomEvent('column-toggle', {
        detail: { id: 'uses', visible: false },
      })
    );
    await settle(host);
    expect(q(host, 'thead th:last-child column-picker')).to.exist;
    expect(qa(host, 'thead th')).to.have.length(4);
    q(host, 'thead column-picker').dispatchEvent(
      new CustomEvent('columns-reset')
    );
    await settle(host);
    expect(qa(host, 'thead th')).to.have.length(5);
  });

  it('leaves the picker out of a table that does not offer one', async () => {
    const host = await mount();
    await settle(host);
    const table = q(
      host,
      'data-table'
    ) as import('./data-table').DataTable<Key>;
    table.columnPicker = false;
    await settle(host);
    expect(qa(host, 'column-picker')).to.have.length(0);
    expect(qa(host, 'thead th')).to.have.length(5);
    table.rowActions = undefined;
    await settle(host);
    expect(qa(host, 'column-picker')).to.have.length(0);
    expect(qa(host, 'thead th')).to.have.length(4);
  });

  it('renders headers, a select column, rows and an actions column', async () => {
    const host = await mount();
    await settle(host);
    const ths = qa(host, 'thead th');
    expect(ths[0].classList.contains('select-cell')).to.be.true;
    expect(ths.map((th) => th.textContent!.trim()).slice(1, 4)).to.deep.equal([
      'Name',
      'Scope',
      'Uses',
    ]);
    expect(qa(host, 'tbody tr[data-row]')).to.have.length(10);
    expect(qa(host, 'tbody resource-actions')).to.have.length(10);
    expect(q(host, 'table').getAttribute('aria-label')).to.equal('API keys');
  });

  it('shows skeleton rows while the first page loads', async () => {
    const host = await mount((h) => {
      h.loading = true;
    });
    host.table.data = [];
    host.requestUpdate();
    await settle(host);
    expect(qa(host, 'tr.skeleton-row')).to.have.length(5);
    expect(q(host, 'table').getAttribute('aria-busy')).to.equal('true');
  });

  it('says empty, no results and error differently', async () => {
    const host = await mount();
    host.table.data = [];
    host.requestUpdate();
    await settle(host);
    expect(q(host, 'table-state').getAttribute('kind')).to.equal('empty');
    expect(q(host, 'table-state').getAttribute('heading')).to.equal(
      'No API keys yet'
    );

    host.table.data = KEYS;
    host.table.setSearch('zzz');
    await settle(host);
    expect(q(host, 'table-state').getAttribute('kind')).to.equal('no-results');
    (q(host, 'table-state sl-button') as HTMLElement).click();
    await settle(host);
    expect(host.table.search).to.equal('');
    expect(q(host, 'table-state')).to.not.exist;

    host.error = 'Network down';
    host.requestUpdate();
    await settle(host);
    expect(q(host, 'table-state').getAttribute('kind')).to.equal('error');
    expect(q(host, 'table-pagination')).to.not.exist;
  });

  it('activates a row on click and Enter, but not from a control inside it', async () => {
    const host = await mount();
    await settle(host);
    const row = qa(host, 'tbody tr[data-row]')[0];
    row
      .querySelector('td:nth-child(3)')!
      .dispatchEvent(
        new MouseEvent('click', { bubbles: true, composed: true })
      );
    expect(host.activated).to.have.length(1);
    const link = row.querySelector('a')!;
    link.addEventListener('click', (event) => event.preventDefault());
    link.click();
    expect(host.activated).to.have.length(1);
    row.focus();
    row.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    );
    expect(host.activated).to.have.length(2);
    // Consumers that skip prevented events (link handling) still act on it.
    expect(host.activatedPrevented).to.deep.equal([false, false]);
  });

  it('announces a multi-select grid only when rows can be selected', async () => {
    const host = await mount();
    await settle(host);
    const table = q(host, 'table');
    expect(table.getAttribute('role')).to.equal('grid');
    expect(table.getAttribute('aria-multiselectable')).to.equal('true');
    const plain = await fixture<DerivedRowsHost>(
      html`<test-derived-rows-host></test-derived-rows-host>`
    );
    await aTimeout(20);
    const bare = plain.shadowRoot!.querySelector('table')!;
    expect(bare.hasAttribute('role')).to.equal(false);
    expect(bare.hasAttribute('aria-multiselectable')).to.equal(false);
  });

  it('arrow keys move focus between rows and Space selects', async () => {
    const host = await mount();
    await settle(host);
    const rows = qa(host, 'tbody tr[data-row]');
    rows[0].focus();
    rows[0].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })
    );
    expect(host.shadowRoot!.activeElement).to.equal(rows[1]);
    rows[1].dispatchEvent(
      new KeyboardEvent('keydown', { key: ' ', bubbles: true })
    );
    await settle(host);
    expect(host.selection.isSelected('k1')).to.be.true;
    expect(rows[1].getAttribute('aria-selected')).to.equal('true');
    expect(
      host.hasAttribute('selecting') ||
        q(host, 'list-toolbar').hasAttribute('selecting')
    ).to.be.true;
  });

  it('pagination footer drives the model', async () => {
    const host = await mount();
    await settle(host);
    const pagination = q(host, 'table-pagination') as TablePagination;
    const range = pagination.shadowRoot!.querySelector('.range')!;
    expect(range.textContent!.trim()).to.equal('1-10 of 30 keys');
    (
      pagination.shadowRoot!.querySelector('sl-button.next') as HTMLElement
    ).click();
    await settle(host);
    expect(host.table.pageIndex).to.equal(1);
    expect(range.textContent!.trim()).to.equal('11-20 of 30 keys');
    const select = pagination.shadowRoot!.querySelector('select')!;
    select.value = '25';
    select.dispatchEvent(new Event('change'));
    await settle(host);
    expect(host.table.pageSize).to.equal(25);
    expect(host.table.pageIndex).to.equal(0);
    localStorage.removeItem(tablePreferencesKey(host.listId));
  });

  it('toolbar search filters the table', async () => {
    const host = await mount();
    await settle(host);
    q(host, 'list-toolbar').dispatchEvent(
      new CustomEvent('search-change', { detail: { value: 'key-2' } })
    );
    await settle(host);
    expect(qa(host, 'tbody tr[data-row]')).to.have.length(10);
    expect(host.table.totalRows).to.equal(10);
  });

  it('density comes from the model', async () => {
    const host = await mount();
    host.table.setDensity('compact');
    await settle(host);
    expect(q(host, '.data-table').classList.contains('compact')).to.be.true;
    localStorage.removeItem(tablePreferencesKey(host.listId));
  });
});

@customElement('test-derived-rows-host')
class DerivedRowsHost extends LitElement {
  static styles = dataTableStyles;
  renders = 0;
  table = new ListTable<Key>(this, {
    listId: `dt-derived-${++counter}`,
    columns: COLUMNS,
    getRowId: (row) => row.id,
    pageSize: 10,
  });
  protected willUpdate() {
    // A fresh array every pass, the way views derive their visible rows.
    this.table.data = KEYS.filter(() => true);
  }
  render() {
    this.renders++;
    return html`<data-table
      .table=${this.table}
      .rows=${this.table.rows}
    ></data-table>`;
  }
}

describe('<data-table> with rows derived per update', () => {
  it('settles instead of repainting forever', async () => {
    const el = await fixture<DerivedRowsHost>(
      html`<test-derived-rows-host></test-derived-rows-host>`
    );
    await aTimeout(50);
    expect(el.renders).to.be.lessThan(4);
    el.table.setPageIndex(1);
    await aTimeout(50);
    expect(el.renders).to.be.lessThan(6);
    expect(el.table.rows[0].id).to.equal('k10');
  });
});

describe('<table-pagination>', () => {
  it('cursor mode shows a range without a total', async () => {
    const el = await fixture<TablePagination>(
      html`<table-pagination
        page-index="1"
        page-size="25"
        page-rows="25"
        has-next
        noun="events"
        .total=${null}
      ></table-pagination>`
    );
    expect(
      el.shadowRoot!.querySelector('.range')!.textContent!.trim()
    ).to.equal('26-50 events');
    const next = el.shadowRoot!.querySelector('sl-button.next')!;
    expect(next.hasAttribute('disabled')).to.be.false;
  });

  it('renders nothing for a list that fits on its smallest page', async () => {
    const el = await fixture<TablePagination>(
      html`<table-pagination
        page-size="25"
        page-rows="7"
        .total=${7}
      ></table-pagination>`
    );
    expect(el.shadowRoot!.querySelector('nav')).to.not.exist;
    await aTimeout(0);
  });
});

describe('data-table: rowDetail and available()', () => {
  afterEach(() => {
    notesAvailable = false;
  });

  it('draws a full-width detail row under an expanded row only', async () => {
    const host = await mount();
    expect(qa(host, 'tr.row-detail')).to.have.length(0);
    host.expanded.add('k1');
    host.requestUpdate();
    await settle(host);
    const details = qa(host, 'tr.row-detail');
    expect(details).to.have.length(1);
    expect(details[0].dataset.detailFor).to.equal('k1');
    // Directly under its data row, spanning select + 3 columns + actions.
    const previous = details[0].previousElementSibling as HTMLElement;
    expect(previous.dataset.selectionId).to.equal('k1');
    const cell = details[0].querySelector('td')!;
    expect(cell.getAttribute('colspan')).to.equal('5');
    expect(cell.textContent).to.contain('Detail of key-01');
    // Keyboard navigation and row counts see data rows only.
    expect(qa(host, 'tr[data-row]')).to.have.length(10);
  });

  it('hides an unavailable column from headers, cells and the picker', async () => {
    const host = await mount();
    const headerText = () =>
      qa(host, 'thead th').map((th) => th.textContent!.trim());
    expect(headerText().join('|')).to.not.contain('Notes');
    expect(host.table.pickerColumns.map((c) => c.id)).to.not.include('notes');
    notesAvailable = true;
    host.requestUpdate();
    await settle(host);
    expect(headerText().join('|')).to.contain('Notes');
    expect(host.table.pickerColumns.map((c) => c.id)).to.include('notes');
    expect(q(host, 'tbody tr[data-row]').textContent).to.contain('note k0');
  });
});

describe('data-table: truncate and minWidth', () => {
  function twoColumnTable() {
    const table = new ListTable<Key>(
      { addController() {}, requestUpdate() {} } as never,
      {
        listId: `trunc-${++counter}`,
        getRowId: (row) => row.id,
        columns: [
          {
            id: 'name',
            header: 'Name',
            truncate: true,
            minWidth: 120,
            width: 200,
            value: (row) => row.name,
            cell: (row) => row.name,
          },
          // A second column, so the first header is not :last-child (where
          // the resize handle is hidden) and the handle is really measured.
          {
            id: 'uses',
            header: 'Uses',
            value: (row) => row.uses,
            cell: (row) => String(row.uses),
          },
        ],
      }
    );
    table.data = [{ id: 'a', name: 'x'.repeat(400), scope: 'read', uses: 1 }];
    return table;
  }

  it('cuts a long cell on an inner block with the full text on hover', async () => {
    const table = twoColumnTable();
    const el = await fixture<HTMLElement>(
      html`<div style="width: 600px">
        <data-table .table=${table} .rows=${table.rows}></data-table>
      </div>`
    );
    // The view's styles, as a view would adopt them.
    const style = document.createElement('style');
    style.textContent = listTableStyles.cssText;
    el.prepend(style);
    const dataTable = el.querySelector('data-table') as LitElement;
    await dataTable.updateComplete;
    const td = el.querySelector('td')!;
    expect(td.title).to.equal('x'.repeat(400));
    expect(td.querySelector('.cell-truncate')).to.exist;
    const th = el.querySelector('th')!;
    expect(th.getAttribute('style')).to.contain('min-width: 120px');
    const handle = th.querySelector('.col-resize') as HTMLElement;
    expect(getComputedStyle(handle).display).to.not.equal('none');
    expect(handle.getAttribute('aria-valuemin')).to.equal('120');
    // The handle sits inside the header, so the header does not overflow,
    // and the cut text does not widen its cell either.
    expect(th.scrollWidth).to.be.at.most(th.clientWidth + 2);
    expect(td.scrollWidth).to.be.at.most(td.clientWidth + 2);
  });

  it('stops a resize at the column minWidth', () => {
    const table = twoColumnTable();
    table.resizeBy('name', -500);
    expect(table.widthOfColumn('name')).to.equal(120);
  });
});
