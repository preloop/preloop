import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/menu-label/menu-label.js';

/** One row of the picker. */
export interface ColumnPickerItem {
  id: string;
  label: string;
  /** Files the row under a heading, for composite columns like Tokens. */
  group?: string;
  visible: boolean;
  /** False for a column the list will not let go of (its identifying one). */
  hideable: boolean;
}

/**
 * Which columns a list shows.
 *
 * A dropdown of checkboxes, in the filter bar beside the list's own filters,
 * because choosing columns is choosing what the table says and belongs in the
 * bar that already decides which rows it says it about. It is a `sl-dropdown`
 * of `sl-menu-item type="checkbox"`, which is the console's one menu recipe,
 * so it inherits the raised surface, the focus ring and the keyboard model
 * from Shoelace rather than inventing a popover.
 *
 * The column a list is identified by cannot be switched off: a table of runs
 * with no Flow column is a table of nothing.
 *
 * @fires column-toggle - `{ detail: { id, visible } }`
 * @fires columns-reset - the operator asked for the list's own layout back
 */
@customElement('column-picker')
export class ColumnPicker extends LitElement {
  @property({ attribute: false })
  columns: ColumnPickerItem[] = [];

  /** Shown as the trigger's label; the icon alone is not a word. */
  @property({ type: String })
  label = 'Columns';

  /** Offers Reset only when something differs from the declared layout. */
  @property({ type: Boolean, attribute: 'can-reset' })
  canReset = false;

  static styles = css`
    :host {
      display: inline-block;
    }
    /* The bar aligns its controls on their bottom edge, and a bare button is
       shorter than a labelled select. */
    sl-dropdown {
      display: block;
    }
    sl-menu {
      max-height: 60vh;
      overflow-y: auto;
    }
  `;

  render() {
    const groups = this.grouped();
    return html`
      <!-- Checking a box is not choosing a command: an operator turning three
           token columns on would otherwise reopen the menu three times,
           because Shoelace closes a dropdown on every selection. Reset is the
           one entry that ends the exchange, so it closes the menu itself. -->
      <sl-dropdown hoist stay-open-on-select placement="bottom-end">
        <sl-button slot="trigger" size="small" caret>
          <sl-icon slot="prefix" name="layout-three-columns"></sl-icon>
          ${this.label}
        </sl-button>
        <sl-menu @sl-select=${this.handleSelect}>
          ${groups.map(
            (group) => html`
              ${
                group.name
                  ? html`<sl-menu-label>${group.name}</sl-menu-label>`
                  : nothing
              }
              ${group.items.map(
                (item) => html`
                  <sl-menu-item
                    type="checkbox"
                    data-column=${item.id}
                    value=${item.id}
                    ?checked=${item.visible}
                    ?disabled=${!item.hideable}
                    >${item.label}</sl-menu-item
                  >
                `
              )}
            `
          )}
          ${
            this.canReset
              ? html`
                  <sl-divider></sl-divider>
                  <sl-menu-item value="__reset__" class="reset-columns"
                    >Reset columns</sl-menu-item
                  >
                `
              : nothing
          }
        </sl-menu>
      </sl-dropdown>
    `;
  }

  /** Shuts the menu, for the entry that is done when it is chosen. */
  private close(): void {
    const dropdown = this.renderRoot.querySelector('sl-dropdown') as
      (HTMLElement & { hide?: () => void }) | null;
    dropdown?.hide?.();
  }

  /** Ungrouped columns first, in declaration order, then each group once. */
  private grouped(): Array<{ name?: string; items: ColumnPickerItem[] }> {
    const ungrouped = this.columns.filter((column) => !column.group);
    const groups: Array<{ name?: string; items: ColumnPickerItem[] }> = [];
    if (ungrouped.length) groups.push({ items: ungrouped });
    const seen = new Set<string>();
    for (const column of this.columns) {
      if (!column.group || seen.has(column.group)) continue;
      seen.add(column.group);
      groups.push({
        name: column.group,
        items: this.columns.filter((entry) => entry.group === column.group),
      });
    }
    return groups;
  }

  private handleSelect(event: CustomEvent) {
    const item = (event.detail as { item?: HTMLElement }).item;
    if (!item) return;
    const value = item.getAttribute('value');
    if (value === '__reset__') {
      this.close();
      this.dispatchEvent(
        new CustomEvent('columns-reset', { bubbles: true, composed: true })
      );
      return;
    }
    const id = item.getAttribute('data-column');
    if (!id) return;
    // Shoelace flips the item's own checked state before it reports the
    // selection; the list is the source of truth, so the new state is read
    // back off the item and the list re-renders it either way.
    const visible = (item as HTMLElement & { checked?: boolean }).checked;
    this.dispatchEvent(
      new CustomEvent('column-toggle', {
        detail: { id, visible: Boolean(visible) },
        bubbles: true,
        composed: true,
      })
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'column-picker': ColumnPicker;
  }
}
