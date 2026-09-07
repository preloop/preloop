import { html, fixture, expect } from '@open-wc/testing';
import { LitElement } from 'lit';
import { customElement } from 'lit/decorators.js';
import { ListTable } from './list-table';
import { renderListCells, renderListHeaders } from './list-table-render';
import {
  readTablePreferences,
  tablePreferencesKey,
  writeTablePreferences,
} from './table-preferences';
import './column-picker';
import type { ColumnPicker } from './column-picker';

interface Run extends Record<string, unknown> {
  id: string;
  name: string;
  tokens: number;
}

const ROWS: Run[] = [
  { id: 'a', name: 'Cedar', tokens: 30 },
  { id: 'b', name: 'Alder', tokens: 10 },
  { id: 'c', name: 'Birch', tokens: 20 },
];

@customElement('test-list-table')
class TestListTable extends LitElement {
  table = new ListTable<Run>(this, {
    listId: 'test-list',
    getRowId: (row) => row.id,
    columns: [
      {
        id: 'name',
        header: 'Name',
        width: 120,
        hideable: false,
        value: (row) => row.name,
        cell: (row) => row.name,
      },
      {
        id: 'tokens',
        header: 'Tokens',
        numeric: true,
        sort: 'number',
        group: 'Tokens',
        value: (row) => row.tokens,
        cell: (row) => String(row.tokens),
      },
      {
        id: 'cached',
        header: 'Cached',
        numeric: true,
        sort: 'number',
        group: 'Tokens',
        visible: false,
        value: (row) => row.tokens,
        cell: (row) => String(row.tokens),
      },
      {
        id: 'note',
        header: 'Note',
        resizable: false,
        cell: () => 'n/a',
      },
    ],
  });

  connectedCallback() {
    super.connectedCallback();
    this.table.data = ROWS;
  }

  render() {
    return html`<table>
      <thead>
        <tr>
          ${renderListHeaders(this.table)}
        </tr>
      </thead>
      <tbody>
        ${this.table.rows.map(
          (row) =>
            html`<tr data-row=${row.id}>
              ${renderListCells(this.table, row)}
            </tr>`
        )}
      </tbody>
    </table>`;
  }
}

const rowIds = (el: TestListTable) =>
  Array.from(el.shadowRoot!.querySelectorAll('tbody tr')).map((row) =>
    row.getAttribute('data-row')
  );

const headerLabels = (el: TestListTable) =>
  Array.from(el.shadowRoot!.querySelectorAll('thead th')).map((th) =>
    (th.textContent || '').trim()
  );

const sortButton = (el: TestListTable, id: string) =>
  el.shadowRoot!.querySelector(
    `.sort-button[data-sort-key="${id}"]`
  ) as HTMLButtonElement;

describe('ListTable', () => {
  afterEach(() => localStorage.clear());

  async function render() {
    const el = (await fixture(
      html`<test-list-table></test-list-table>`
    )) as TestListTable;
    await el.updateComplete;
    return el;
  }

  it('renders the declared columns in order, hiding the ones that start off', async () => {
    const el = await render();
    expect(headerLabels(el)).to.eql(['Name', 'Tokens', 'Note']);
    expect(rowIds(el)).to.eql(['a', 'b', 'c']);
  });

  it('leaves the rows in the order they arrived until a header is clicked', async () => {
    const el = await render();
    const header = el.shadowRoot!.querySelector('thead th')!;
    expect(header.getAttribute('aria-sort')).to.equal('none');
  });

  it('sorts descending on the first click and flips on the second', async () => {
    const el = await render();
    sortButton(el, 'name').click();
    await el.updateComplete;
    expect(rowIds(el)).to.eql(['a', 'c', 'b']);
    expect(
      el.shadowRoot!.querySelector('th.col-name')!.getAttribute('aria-sort')
    ).to.equal('descending');

    sortButton(el, 'name').click();
    await el.updateComplete;
    expect(rowIds(el)).to.eql(['b', 'c', 'a']);
    // The cycle never returns to the arrival order: someone asked for a sort.
    sortButton(el, 'name').click();
    await el.updateComplete;
    expect(rowIds(el)).to.eql(['a', 'c', 'b']);
  });

  it('sorts a numeric column by its number, not its text', async () => {
    const el = await render();
    sortButton(el, 'tokens').click();
    await el.updateComplete;
    expect(rowIds(el)).to.eql(['a', 'c', 'b']);
  });

  it('adds a second column to the sort on shift-click and ranks them', async () => {
    const el = await render();
    sortButton(el, 'name').click();
    await el.updateComplete;
    sortButton(el, 'tokens').dispatchEvent(
      new MouseEvent('click', { shiftKey: true, bubbles: true })
    );
    await el.updateComplete;

    expect(el.table.sorting.map((entry) => entry.id)).to.eql([
      'name',
      'tokens',
    ]);
    const ranks = Array.from(el.shadowRoot!.querySelectorAll('.sort-rank')).map(
      (span) => (span.textContent || '').trim()
    );
    expect(ranks).to.eql(['1', '2']);
  });

  it('replaces the sort on a plain click after a multi-sort', async () => {
    const el = await render();
    sortButton(el, 'name').click();
    sortButton(el, 'tokens').dispatchEvent(
      new MouseEvent('click', { shiftKey: true, bubbles: true })
    );
    await el.updateComplete;
    sortButton(el, 'tokens').click();
    await el.updateComplete;

    expect(el.table.sorting.map((entry) => entry.id)).to.eql(['tokens']);
    expect(el.shadowRoot!.querySelector('.sort-rank')).to.not.exist;
  });

  it('does not offer a sort on a column with no value', async () => {
    const el = await render();
    expect(sortButton(el, 'note')).to.not.exist;
    const note = el.shadowRoot!.querySelector('th.col-note')!;
    expect((note.textContent || '').trim()).to.equal('Note');
  });

  it('puts the declared width on the cell and leaves the rest to the table', async () => {
    const el = await render();
    const name = el.shadowRoot!.querySelector('th.col-name') as HTMLElement;
    const note = el.shadowRoot!.querySelector('th.col-note') as HTMLElement;
    expect(name.style.width).to.equal('120px');
    expect(note.style.width).to.equal('');
  });

  it('turns a column on and off and remembers it', async () => {
    const el = await render();
    el.table.setVisible('cached', true);
    await el.updateComplete;
    expect(headerLabels(el)).to.eql(['Name', 'Tokens', 'Cached', 'Note']);

    const stored = readTablePreferences('test-list');
    expect(stored?.columnVisibility?.cached).to.be.true;

    el.table.setVisible('tokens', false);
    await el.updateComplete;
    expect(headerLabels(el)).to.eql(['Name', 'Cached', 'Note']);
  });

  it('does not remember a sort, because a sort orders one page', async () => {
    const el = await render();
    sortButton(el, 'name').click();
    await el.updateComplete;
    expect(readTablePreferences('test-list')?.sorting).to.be.undefined;
  });

  it('starts from what was remembered for this list', async () => {
    writeTablePreferences('test-list', {
      columnVisibility: { cached: true, tokens: false },
    });
    const el = await render();
    expect(headerLabels(el)).to.eql(['Name', 'Cached', 'Note']);
  });

  it('ignores a record written by another version of the list', async () => {
    localStorage.setItem(
      tablePreferencesKey('test-list'),
      JSON.stringify({ v: 99, columnVisibility: { tokens: false } })
    );
    const el = await render();
    expect(headerLabels(el)).to.eql(['Name', 'Tokens', 'Note']);
  });

  it('ignores an unparseable record rather than failing the list', async () => {
    localStorage.setItem(tablePreferencesKey('test-list'), 'not json');
    const el = await render();
    expect(headerLabels(el)).to.eql(['Name', 'Tokens', 'Note']);
  });

  it('gives the columns back on reset', async () => {
    const el = await render();
    el.table.setVisible('cached', true);
    el.table.resizeBy('tokens', 40);
    await el.updateComplete;
    expect(el.table.hasColumnChanges).to.be.true;

    el.table.resetColumns();
    await el.updateComplete;
    expect(headerLabels(el)).to.eql(['Name', 'Tokens', 'Note']);
    expect(el.table.hasColumnChanges).to.be.false;
    expect(readTablePreferences('test-list')?.columnVisibility?.cached).to.not
      .be.true;
  });

  it('resizes a column from the keyboard and keeps the width', async () => {
    const el = await render();
    const handle = el.shadowRoot!.querySelector(
      'th.col-name .col-resize'
    ) as HTMLElement;
    expect(handle.getAttribute('role')).to.equal('separator');
    expect(handle.getAttribute('aria-valuemin')).to.equal('48');
    expect(handle.getAttribute('aria-valuemax')).to.equal('2000');
    expect(handle.getAttribute('aria-valuenow')).to.equal('120');
    handle.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })
    );
    await el.updateComplete;

    const name = el.shadowRoot!.querySelector('th.col-name') as HTMLElement;
    expect(name.style.width).to.equal('136px');
    expect(readTablePreferences('test-list')?.columnSizing?.name).to.equal(136);
    expect(
      el
        .shadowRoot!.querySelector('th.col-name .col-resize')!
        .getAttribute('aria-valuenow')
    ).to.equal('136');
  });

  it('does not write column sizing until a pointer drag ends', async () => {
    const el = await render();
    el.table.startResize(
      'name',
      new MouseEvent('mousedown', { clientX: 120, bubbles: true })
    );
    await el.updateComplete;
    expect(el.table.resizingColumn).to.equal('name');

    document.dispatchEvent(
      new MouseEvent('mousemove', { clientX: 180, bubbles: true })
    );
    await el.updateComplete;
    expect(el.table.widthOfColumn('name')).to.equal(180);
    expect(readTablePreferences('test-list')?.columnSizing?.name).to.be
      .undefined;

    document.dispatchEvent(
      new MouseEvent('mouseup', { clientX: 180, bubbles: true })
    );
    await el.updateComplete;

    expect(el.table.resizingColumn).to.be.null;
    expect(readTablePreferences('test-list')?.columnSizing?.name).to.equal(180);
  });

  it('offers no resize handle on a column that absorbs the leftover width', async () => {
    const el = await render();
    expect(el.shadowRoot!.querySelector('th.col-note .col-resize')).to.not
      .exist;
  });

  it('does not shrink a column below a readable width', async () => {
    const el = await render();
    el.table.resizeBy('name', -1000);
    await el.updateComplete;
    expect(el.table.widthOfColumn('name')).to.equal(48);
  });
});

describe('column-picker', () => {
  it('groups the composite columns and locks the identifying one', async () => {
    const el = (await fixture(html`
      <column-picker
        .columns=${[
          { id: 'name', label: 'Name', visible: true, hideable: false },
          {
            id: 'tokens',
            label: 'Total',
            group: 'Tokens',
            visible: true,
            hideable: true,
          },
          {
            id: 'cached',
            label: 'Cached',
            group: 'Tokens',
            visible: false,
            hideable: true,
          },
        ]}
      ></column-picker>
    `)) as ColumnPicker;
    await el.updateComplete;

    const labels = Array.from(
      el.shadowRoot!.querySelectorAll('sl-menu-label')
    ).map((label) => (label.textContent || '').trim());
    expect(labels).to.eql(['Tokens']);

    const items = Array.from(el.shadowRoot!.querySelectorAll('sl-menu-item'));
    expect(items.map((item) => item.getAttribute('data-column'))).to.eql([
      'name',
      'tokens',
      'cached',
    ]);
    expect(items[0].hasAttribute('disabled'), 'the name column is locked').to.be
      .true;
    expect(items[2].hasAttribute('checked')).to.be.false;
  });

  it('reports the column that was toggled', async () => {
    const el = (await fixture(html`
      <column-picker
        .columns=${[
          { id: 'cached', label: 'Cached', visible: false, hideable: true },
        ]}
      ></column-picker>
    `)) as ColumnPicker;
    await el.updateComplete;

    const toggles: Array<{ id: string; visible: boolean }> = [];
    el.addEventListener('column-toggle', (event) =>
      toggles.push((event as CustomEvent).detail)
    );
    const item = el.shadowRoot!.querySelector('sl-menu-item') as HTMLElement & {
      checked: boolean;
    };
    item.checked = true;
    el.shadowRoot!.querySelector('sl-menu')!.dispatchEvent(
      new CustomEvent('sl-select', { detail: { item } })
    );

    expect(toggles).to.eql([{ id: 'cached', visible: true }]);
  });

  it('asks for a reset only when it is offered', async () => {
    const el = (await fixture(html`
      <column-picker
        can-reset
        .columns=${[
          { id: 'cached', label: 'Cached', visible: false, hideable: true },
        ]}
      ></column-picker>
    `)) as ColumnPicker;
    await el.updateComplete;

    let reset = 0;
    el.addEventListener('columns-reset', () => (reset += 1));
    const item = el.shadowRoot!.querySelector('.reset-columns') as HTMLElement;
    el.shadowRoot!.querySelector('sl-menu')!.dispatchEvent(
      new CustomEvent('sl-select', { detail: { item } })
    );

    expect(reset).to.equal(1);
  });
});
