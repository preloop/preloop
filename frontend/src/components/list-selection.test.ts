import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import { LitElement } from 'lit';

import './list-selection.ts';
import {
  ListSelection,
  ListSelectionController,
  bulkResultMessage,
  confirmBulkAction,
  formatItemNames,
  runBulkAction,
  selectionIdFromEvent,
  type ListBulkBar,
  type ListSelectCheckbox,
} from './list-selection';
import { resetConfirmDialogForTests } from './confirm-dialog';

const ORDER = ['a', 'b', 'c', 'd', 'e'];

describe('ListSelection model', () => {
  it('starts empty and toggles one id at a time', () => {
    let selection = ListSelection.empty();
    expect(selection.size).to.equal(0);
    expect(selection.isEmpty).to.equal(true);

    selection = selection.toggle('b');
    expect(selection.ids).to.eql(['b']);
    expect(selection.has('b')).to.equal(true);
    expect(selection.anchorId).to.equal('b');

    selection = selection.toggle('b');
    expect(selection.ids).to.eql([]);
    expect(selection.anchorId).to.equal(null);
  });

  it('does not treat a just-deselected id as the shift-range anchor', () => {
    const selection = ListSelection.empty()
      .toggle('a')
      .toggle('b')
      .toggle('b')
      .extendTo('c', ORDER);
    // Deselecting B must forget B as the anchor, so shift-click C is a
    // plain toggle of C and does not re-select B (and the A–C range).
    expect(selection.ids).to.eql(['a', 'c']);
    expect(selection.has('b')).to.equal(false);
    expect(selection.anchorId).to.equal('c');
  });

  it('leaves the previous value untouched when toggling', () => {
    const first = ListSelection.empty().toggle('a');
    const second = first.toggle('b');
    expect(first.ids).to.eql(['a']);
    expect(second.ids).to.eql(['a', 'b']);
  });

  it('extends a range from the anchor in page order', () => {
    const selection = ListSelection.empty().toggle('b').extendTo('d', ORDER);
    expect(selection.ids).to.eql(['b', 'c', 'd']);
    // The anchor stays where the operator started so the range can be grown.
    expect(selection.anchorId).to.equal('b');
  });

  it('extends a range upwards and keeps ids selected outside it', () => {
    const selection = ListSelection.empty()
      .toggle('e')
      .toggle('c')
      .extendTo('a', ORDER);
    expect(selection.ids).to.eql(['e', 'c', 'a', 'b']);
  });

  it('treats a range with no anchor as a plain toggle', () => {
    const selection = ListSelection.empty().extendTo('c', ORDER);
    expect(selection.ids).to.eql(['c']);
  });

  it('selects and deselects every id on the page', () => {
    const all = ListSelection.empty().selectAll(ORDER);
    expect(all.ids).to.eql(ORDER);
    expect(all.allSelected(ORDER)).to.equal(true);
    expect(all.someSelected(ORDER)).to.equal(false);

    const none = all.deselectAll(ORDER);
    expect(none.ids).to.eql([]);
  });

  it('reports a partial page as some, not all', () => {
    const selection = ListSelection.empty().toggle('a');
    expect(selection.allSelected(ORDER)).to.equal(false);
    expect(selection.someSelected(ORDER)).to.equal(true);
  });

  it('drops ids that left the page and forgets a stale anchor', () => {
    const selection = ListSelection.empty().toggle('a').toggle('z');
    const retained = selection.retain(ORDER);
    expect(retained.ids).to.eql(['a']);
    expect(retained.anchorId).to.equal(null);
  });

  it('picks selected items back in page order', () => {
    const items = ORDER.map((id) => ({ id }));
    const selection = ListSelection.empty().toggle('d').toggle('a');
    expect(selection.pick(items, (item) => item.id).map((i) => i.id)).to.eql([
      'a',
      'd',
    ]);
  });
});

describe('list-select-checkbox', () => {
  it('names the checkbox for assistive tech and reports a toggle', async () => {
    const element = await fixture<ListSelectCheckbox>(html`
      <list-select-checkbox
        item-id="agent-1"
        label="Select Payments agent"
      ></list-select-checkbox>
    `);
    const checkbox = element.shadowRoot!.querySelector('sl-checkbox')!;
    expect(checkbox.textContent?.trim()).to.equal('Select Payments agent');

    setTimeout(() => {
      (checkbox as unknown as HTMLInputElement).checked = true;
      checkbox.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    });
    const event = await oneEvent(element, 'selection-toggle');
    expect(event.detail).to.eql({ id: 'agent-1', checked: true, range: false });
  });

  it('reports a range when the operator held shift', async () => {
    const element = await fixture<ListSelectCheckbox>(html`
      <list-select-checkbox
        item-id="agent-2"
        label="Select"
      ></list-select-checkbox>
    `);
    const checkbox = element.shadowRoot!.querySelector('sl-checkbox')!;
    checkbox.dispatchEvent(
      new MouseEvent('pointerdown', { shiftKey: true, bubbles: true })
    );
    setTimeout(() => {
      (checkbox as unknown as HTMLInputElement).checked = true;
      checkbox.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    });
    const event = await oneEvent(element, 'selection-toggle');
    expect(event.detail.range).to.equal(true);
  });

  it('reports a null id for the header checkbox', async () => {
    const element = await fixture<ListSelectCheckbox>(html`
      <list-select-checkbox label="Select all agents"></list-select-checkbox>
    `);
    const checkbox = element.shadowRoot!.querySelector('sl-checkbox')!;
    setTimeout(() => {
      (checkbox as unknown as HTMLInputElement).checked = true;
      checkbox.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    });
    const event = await oneEvent(element, 'selection-toggle');
    expect(event.detail.id).to.equal(null);
  });

  it('keeps the widget on the page state after a toggle', async () => {
    const element = await fixture<ListSelectCheckbox>(html`
      <list-select-checkbox item-id="a" label="Select"></list-select-checkbox>
    `);
    const checkbox = element.shadowRoot!.querySelector(
      'sl-checkbox'
    ) as unknown as HTMLInputElement;
    checkbox.checked = true;
    element
      .shadowRoot!.querySelector('sl-checkbox')!
      .dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;
    // The page never said yes, so the checkbox goes back to unchecked.
    expect(checkbox.checked).to.equal(false);
  });
});

describe('list-bulk-bar', () => {
  it('stays hidden while nothing is selected', async () => {
    const element = await fixture<ListBulkBar>(html`
      <list-bulk-bar
        .count=${0}
        .actions=${[{ id: 'pause', label: 'Pause' }]}
      ></list-bulk-bar>
    `);
    expect(element.shadowRoot!.querySelector('.bulk-bar')).to.equal(null);
  });

  it('counts the selection and offers the actions once something is picked', async () => {
    const element = await fixture<ListBulkBar>(html`
      <list-bulk-bar
        label="Agent bulk actions"
        .count=${3}
        .actions=${[
          { id: 'pause', label: 'Pause', icon: 'pause-fill' },
          { id: 'remove', label: 'Decommission', variant: 'danger' },
        ]}
      ></list-bulk-bar>
    `);
    const bar = element.shadowRoot!.querySelector('.bulk-bar')!;
    expect(bar.getAttribute('role')).to.equal('toolbar');
    expect(bar.getAttribute('aria-label')).to.equal('Agent bulk actions');
    expect(
      element.shadowRoot!.querySelector('[data-testid="bulk-count"]')!
        .textContent
    ).to.contain('3 selected');

    const destructive = element.shadowRoot!.querySelector(
      'sl-button[data-action="remove"]'
    )!;
    // DESIGN.md: destructive is danger outline, pushed away from the rest.
    expect(destructive.getAttribute('variant')).to.equal('danger');
    expect(destructive.hasAttribute('outline')).to.equal(true);
    expect(destructive.classList.contains('destructive')).to.equal(true);
  });

  it('emits the action id and a clear request', async () => {
    const element = await fixture<ListBulkBar>(html`
      <list-bulk-bar
        .count=${2}
        .actions=${[{ id: 'pause', label: 'Pause' }]}
      ></list-bulk-bar>
    `);
    const pause = element.shadowRoot!.querySelector<HTMLElement>(
      'sl-button[data-action="pause"]'
    )!;
    setTimeout(() => pause.click());
    const action = await oneEvent(element, 'bulk-action');
    expect(action.detail).to.eql({ id: 'pause' });

    const clear = element.shadowRoot!.querySelector<HTMLElement>(
      'sl-button[data-action="clear"]'
    )!;
    setTimeout(() => clear.click());
    await oneEvent(element, 'selection-clear');
  });

  it('shows progress and locks the other actions while a run is in flight', async () => {
    const element = await fixture<ListBulkBar>(html`
      <list-bulk-bar
        .count=${7}
        .running=${'pause'}
        .progressDone=${3}
        .progressTotal=${7}
        .actions=${[
          { id: 'pause', label: 'Pause' },
          { id: 'resume', label: 'Resume' },
        ]}
      ></list-bulk-bar>
    `);
    expect(
      element
        .shadowRoot!.querySelector('[data-testid="bulk-progress"]')!
        .textContent!.trim()
    ).to.equal('3 of 7');
    const running = element.shadowRoot!.querySelector(
      'sl-button[data-action="pause"]'
    )!;
    const other = element.shadowRoot!.querySelector(
      'sl-button[data-action="resume"]'
    )!;
    expect(running.hasAttribute('loading')).to.equal(true);
    expect(other.hasAttribute('disabled')).to.equal(true);
  });

  it('announces the run from one live region, not two', async () => {
    const element = await fixture<ListBulkBar>(html`
      <list-bulk-bar
        .count=${7}
        .running=${'pause'}
        .progressDone=${3}
        .progressTotal=${7}
        .actions=${[{ id: 'pause', label: 'Pause' }]}
      ></list-bulk-bar>
    `);
    // The count changes because the operator just ticked a box; only the
    // progress, which changes on its own, is announced.
    const count = element.shadowRoot!.querySelector(
      '[data-testid="bulk-count"]'
    )!;
    expect(count.hasAttribute('aria-live')).to.equal(false);
    const progress = element.shadowRoot!.querySelector(
      '[data-testid="bulk-progress"]'
    )!;
    expect(progress.getAttribute('role')).to.equal('status');
    expect(progress.getAttribute('aria-live')).to.equal('polite');
    expect(element.shadowRoot!.querySelectorAll('[aria-live]').length).to.equal(
      1
    );
  });
});

describe('runBulkAction', () => {
  const items = [1, 2, 3, 4, 5, 6, 7].map((n) => ({
    id: `id-${n}`,
    name: `Item ${n}`,
  }));

  it('reports progress per item and never exceeds the bound', async () => {
    let inFlight = 0;
    let peak = 0;
    const progress: Array<[number, number]> = [];
    const result = await runBulkAction(
      items,
      async () => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((resolve) => setTimeout(resolve, 1));
        inFlight -= 1;
      },
      { onProgress: (done, total) => progress.push([done, total]) }
    );

    expect(peak).to.be.at.most(4);
    expect(result.succeeded).to.have.lengthOf(7);
    expect(result.failed).to.eql([]);
    expect(progress[0]).to.eql([0, 7]);
    expect(progress[progress.length - 1]).to.eql([7, 7]);
  });

  it('keeps going past a failure and names the item that failed', async () => {
    const result = await runBulkAction(items.slice(0, 3), async (item) => {
      if (item.id === 'id-2') {
        throw new Error('Forbidden');
      }
    });
    expect(result.succeeded.map((item) => item.id)).to.eql(['id-1', 'id-3']);
    expect(result.failed).to.have.lengthOf(1);
    expect(result.failed[0].item.name).to.equal('Item 2');
    expect(result.failed[0].message).to.equal('Forbidden');
  });
});

describe('bulk reporting', () => {
  it('lists names and counts the rest beyond the cap', () => {
    expect(formatItemNames(['Alpha', 'Beta'])).to.equal('Alpha, Beta');
    expect(formatItemNames(['Alpha', 'Beta', 'Gamma'], 2)).to.equal(
      'Alpha, Beta and 1 more'
    );
  });

  it('says what worked and what did not', () => {
    const report = { verb: 'pause', verbPast: 'paused', noun: 'agent' };
    expect(
      bulkResultMessage(
        { succeeded: [{ id: 'a', name: 'Alpha' }], failed: [] },
        report
      )
    ).to.equal('1 agent paused');
    expect(
      bulkResultMessage(
        {
          succeeded: [
            { id: 'a', name: 'Alpha' },
            { id: 'b', name: 'Beta' },
          ],
          failed: [{ item: { id: 'c', name: 'Gamma' }, message: 'Forbidden' }],
        },
        report
      )
    ).to.equal('2 agents paused, 1 failed: Gamma (Forbidden)');
    expect(
      bulkResultMessage(
        {
          succeeded: [],
          failed: [{ item: { id: 'c', name: 'Gamma' }, message: 'Forbidden' }],
        },
        report
      )
    ).to.equal('Could not pause Gamma (Forbidden)');
  });

  it('reports distinct failure reasons instead of the first message only', () => {
    const report = { verb: 'pause', verbPast: 'paused', noun: 'agent' };
    expect(
      bulkResultMessage(
        {
          succeeded: [{ id: 'a', name: 'Alpha' }],
          failed: [
            { item: { id: 'b', name: 'Beta' }, message: 'Forbidden' },
            { item: { id: 'c', name: 'Gamma' }, message: 'Conflict' },
          ],
        },
        report
      )
    ).to.equal('1 agent paused, 2 failed: Beta (Forbidden), Gamma (Conflict)');
    expect(
      bulkResultMessage(
        {
          succeeded: [],
          failed: [
            { item: { id: 'a', name: 'Alpha' }, message: 'Forbidden' },
            { item: { id: 'b', name: 'Beta' }, message: 'Forbidden' },
            { item: { id: 'c', name: 'Gamma' }, message: 'Conflict' },
            { item: { id: 'd', name: 'Delta' }, message: 'Conflict' },
            { item: { id: 'e', name: 'Epsilon' }, message: 'Not found' },
          ],
        },
        report
      )
    ).to.equal(
      'Could not pause Alpha, Beta (Forbidden); Gamma, Delta (Conflict); Epsilon (Not found)'
    );
  });
});

describe('confirmBulkAction', () => {
  afterEach(() => {
    resetConfirmDialogForTests();
  });

  it('lists the names it is about to touch', async () => {
    const pending = confirmBulkAction({
      title: 'Delete flows',
      message: 'Delete 2 flows?',
      names: ['Nightly sweep', 'PR reviewer'],
      confirmLabel: 'Delete flows',
      variant: 'danger',
    });
    const dialog = document.querySelector('confirm-dialog')!;
    await (dialog as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const text = dialog.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('Delete 2 flows?');
    expect(text).to.contain('Nightly sweep, PR reviewer');

    dialog
      .shadowRoot!.querySelector<HTMLElement>(
        '[data-testid="confirm-dialog-confirm"]'
      )!
      .click();
    expect(await pending).to.equal(true);
  });
});

describe('selectionIdFromEvent', () => {
  it('finds the row id from anywhere inside the row', async () => {
    const row = await fixture<HTMLElement>(html`
      <div data-selection-id="row-9"><button>Kebab</button></div>
    `);
    const button = row.querySelector('button')!;
    let seen: string | null = 'unset';
    row.addEventListener('click', (event) => {
      seen = selectionIdFromEvent(event);
    });
    button.click();
    expect(seen).to.equal('row-9');
  });
});

/** A minimal host, so the controller is tested without a page around it. */
class SelectionHost extends LitElement {
  static properties = { rows: { type: Array } };
  rows: Array<{ id: string; name: string }> = [];
  selection = new ListSelectionController<{ id: string; name: string }>(this, {
    idOf: (row) => row.id,
  });

  // The rule the views follow: prune before anything in the pass renders.
  willUpdate() {
    this.selection.setItems(this.rows);
  }

  render() {
    return html`
      <table>
        <tbody>
          ${this.rows.map(
            (row) => html`
              <tr
                data-selection-id=${row.id}
                aria-selected=${
                  this.selection.isSelected(row.id) ? 'true' : 'false'
                }
              >
                <td>
                  <list-select-checkbox
                    item-id=${row.id}
                    label=${`Select ${row.name}`}
                    ?checked=${this.selection.isSelected(row.id)}
                    ?disabled=${this.selection.busy}
                    @selection-toggle=${this.selection.handleToggleEvent}
                  ></list-select-checkbox>
                </td>
                <td><a href="#">${row.name}</a></td>
              </tr>
            `
          )}
        </tbody>
      </table>
    `;
  }
}
customElements.define('selection-host-fixture', SelectionHost);

describe('ListSelectionController', () => {
  const rows = ['a', 'b', 'c', 'd'].map((id) => ({
    id,
    name: `Row ${id.toUpperCase()}`,
  }));

  async function host(): Promise<SelectionHost> {
    const element = await fixture<SelectionHost>(
      html`<selection-host-fixture .rows=${rows}></selection-host-fixture>`
    );
    await element.updateComplete;
    return element;
  }

  function press(element: HTMLElement, key: string, shiftKey = false) {
    element.dispatchEvent(
      new KeyboardEvent('keydown', {
        key,
        shiftKey,
        bubbles: true,
        composed: true,
        cancelable: true,
      })
    );
  }

  it('toggles the row the focus is in with x', async () => {
    const element = await host();
    const link = element.shadowRoot!.querySelector<HTMLElement>(
      'tr[data-selection-id="b"] a'
    )!;
    press(link, 'x');
    await element.updateComplete;
    expect(element.selection.selectedIds).to.eql(['b']);
    expect(
      element
        .shadowRoot!.querySelector('tr[data-selection-id="b"]')!
        .getAttribute('aria-selected')
    ).to.equal('true');
  });

  it('extends the range with shift and X', async () => {
    const element = await host();
    press(
      element.shadowRoot!.querySelector<HTMLElement>(
        'tr[data-selection-id="a"] a'
      )!,
      'x'
    );
    await element.updateComplete;
    press(
      element.shadowRoot!.querySelector<HTMLElement>(
        'tr[data-selection-id="c"] a'
      )!,
      'X',
      true
    );
    await element.updateComplete;
    expect(element.selection.selectedIds).to.eql(['a', 'b', 'c']);
  });

  it('keeps working when the focus is on the row checkbox', async () => {
    const element = await host();
    const checkbox = element.shadowRoot!.querySelector<HTMLElement>(
      'tr[data-selection-id="d"] list-select-checkbox'
    )!;
    const input = checkbox
      .shadowRoot!.querySelector('sl-checkbox')!
      .shadowRoot!.querySelector<HTMLElement>('input')!;
    press(input, 'x');
    await element.updateComplete;
    expect(element.selection.selectedIds).to.eql(['d']);
  });

  it('clears with Escape', async () => {
    const element = await host();
    element.selection.toggleAll(true);
    await element.updateComplete;
    expect(element.selection.count).to.equal(4);
    press(element.shadowRoot!.querySelector<HTMLElement>('a')!, 'Escape');
    await element.updateComplete;
    expect(element.selection.count).to.equal(0);
  });

  it('drops selections for rows that left the page', async () => {
    const element = await host();
    element.selection.toggle('a');
    element.selection.toggle('c');
    await element.updateComplete;
    element.rows = rows.filter((row) => row.id !== 'c');
    await element.updateComplete;
    expect(element.selection.selectedIds).to.eql(['a']);
  });

  it('routes the checkbox event through the model', async () => {
    const element = await host();
    const checkbox = element.shadowRoot!.querySelector<HTMLElement>(
      'tr[data-selection-id="b"] list-select-checkbox'
    )!;
    checkbox.dispatchEvent(
      new CustomEvent('selection-toggle', {
        detail: { id: 'b', checked: true, range: false },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;
    expect(element.selection.selectedIds).to.eql(['b']);
  });

  it('counts progress while running and keeps only the failures selected', async () => {
    const element = await host();
    element.selection.toggleAll(true);
    await element.updateComplete;

    const seen: Array<[number, number]> = [];
    const pending = element.selection.run(
      'pause',
      element.selection.selectedItems,
      async (item) => {
        seen.push([
          element.selection.progressDone,
          element.selection.progressTotal,
        ]);
        if (item.id === 'c') throw new Error('Forbidden');
      },
      { verb: 'pause', verbPast: 'paused', noun: 'row' }
    );
    expect(element.selection.running).to.equal('pause');
    const result = await pending;

    expect(result.succeeded.map((row) => row.id)).to.eql(['a', 'b', 'd']);
    expect(result.failed.map((failure) => failure.item.id)).to.eql(['c']);
    expect(seen[0]).to.eql([0, 4]);
    expect(element.selection.running).to.equal(null);
    // A retry of the one that failed is one click away.
    expect(element.selection.selectedIds).to.eql(['c']);
  });

  it('locks the row checkboxes while a run is in flight', async () => {
    const element = await host();
    element.selection.toggle('a');
    await element.updateComplete;

    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const pending = element.selection.run(
      'pause',
      element.selection.selectedItems,
      () => gate,
      { verb: 'pause', verbPast: 'paused', noun: 'row' }
    );
    await element.updateComplete;

    // A row ticked mid-run would be thrown away when the run replaces the
    // selection with its failures, so the boxes are locked until it settles.
    const box = element.shadowRoot!.querySelector<HTMLElement>(
      'tr[data-selection-id="b"] list-select-checkbox'
    )!;
    expect(box.hasAttribute('disabled')).to.equal(true);
    expect(
      box.shadowRoot!.querySelector('sl-checkbox')!.hasAttribute('disabled')
    ).to.equal(true);

    release();
    await pending;
    await element.updateComplete;
    expect(box.hasAttribute('disabled')).to.equal(false);
  });

  it('runs one batch call and keeps only the refused rows selected', async () => {
    const element = await host();
    element.selection.toggleAll(true);
    await element.updateComplete;

    let sent: string[] = [];
    const result = await element.selection.runBatch(
      'approve',
      element.selection.selectedItems,
      async (items) => {
        sent = items.map((item) => item.id);
        return {
          succeeded: items.filter((item) => item.id !== 'c'),
          failed: items
            .filter((item) => item.id === 'c')
            .map((item) => ({ item, message: 'Already expired' })),
        };
      },
      { verb: 'approve', verbPast: 'approved', noun: 'request' }
    );

    // One call, carrying the whole selection.
    expect(sent).to.eql(['a', 'b', 'c', 'd']);
    expect(result.succeeded.map((row) => row.id)).to.eql(['a', 'b', 'd']);
    expect(element.selection.selectedIds).to.eql(['c']);
    expect(element.selection.running).to.equal(null);
  });

  it('treats a batch call that throws as every row failing', async () => {
    const element = await host();
    element.selection.toggleAll(true);
    await element.updateComplete;

    const result = await element.selection.runBatch(
      'approve',
      element.selection.selectedItems,
      async () => {
        throw new Error('Service unavailable');
      },
      { verb: 'approve', verbPast: 'approved', noun: 'request' }
    );

    expect(result.succeeded).to.eql([]);
    expect(result.failed.map((failure) => failure.message)).to.eql([
      'Service unavailable',
      'Service unavailable',
      'Service unavailable',
      'Service unavailable',
    ]);
    // Nothing was lost: the whole selection is still there to retry.
    expect(element.selection.selectedIds).to.eql(['a', 'b', 'c', 'd']);
  });
});
