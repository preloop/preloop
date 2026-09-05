import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';

import type { ListViewMode } from '../utils/view-mode';

const VIEW_OPTIONS: Array<{
  value: ListViewMode;
  label: string;
  icon: string;
}> = [
  { value: 'list', label: 'List', icon: 'list-ul' },
  { value: 'cards', label: 'Cards', icon: 'grid-3x3-gap' },
];

/**
 * Shared collection toolbar: search, a slot for page filters, and the
 * list/cards switcher. Spacing and the 900px / 640px collapse match the
 * Flows filter bar pixel-for-pixel so Trackers and Models feel like the
 * same product.
 *
 * @fires search-change - `{ detail: { value } }` when the search input changes
 * @fires view-change - `{ detail: { value } }` when a view button is pressed
 */
@customElement('list-toolbar')
export class ListToolbar extends LitElement {
  @property({ type: String }) search = '';
  @property({ type: String }) searchPlaceholder = 'Search';
  @property({ type: String }) view: ListViewMode = 'list';
  @property({ type: Array }) views: ListViewMode[] = ['list', 'cards'];
  @property({ type: String }) toggleLabel = 'View';

  static styles = css`
    :host {
      display: block;
      width: 100%;
    }

    /* Names the search field for assistive tech without a visible label. */
    sl-input::part(form-control-label) {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
      border: 0;
    }

    /* --- Filter bar (copied from flows-view) --- */
    .list-toolbar {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: var(--sl-spacing-medium);
      flex-wrap: wrap;
      width: 100%;
    }
    .filters {
      display: flex;
      gap: var(--sl-spacing-medium);
      flex-wrap: wrap;
      align-items: end;
      flex: 1 1 520px;
      min-width: 0;
    }
    .filters sl-input,
    .filters ::slotted(sl-select),
    .filters ::slotted(sl-input) {
      min-width: 180px;
    }
    .filters sl-input {
      flex: 1 1 260px;
    }
    .view-switcher-group {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-medium);
      margin-left: auto;
    }
    /* Says how many rows the filters matched, right where the eye already
       goes to switch views. */
    .results-count {
      color: var(--console-meta-color);
      font-size: var(--console-text-meta);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .toolbar-divider {
      width: 1px;
      height: 32px;
      background: var(--console-hairline);
    }
    @media (max-width: 900px) {
      .view-switcher-group {
        margin-left: 0;
        width: 100%;
        justify-content: flex-end;
      }
      .toolbar-divider {
        display: none;
      }
    }
    /* Phones: the filters take the full width one after another, and the
       switcher goes away. Below this width a multi-column table cannot be
       read, so cards are the only view on offer and a switcher that could
       not honour a click would be a lie. Matches LIST_TO_CARDS_BREAKPOINT. */
    @media (max-width: 640px) {
      .filters sl-input,
      .filters ::slotted(sl-select),
      .filters ::slotted(sl-input) {
        flex: 1 1 100%;
      }
      .view-switcher-group sl-button-group {
        display: none;
      }
    }
  `;

  private get visibleViews(): ListViewMode[] {
    const allowed = new Set(this.views);
    return VIEW_OPTIONS.map((option) => option.value).filter((value) =>
      allowed.has(value)
    );
  }

  private get showToggle(): boolean {
    return this.visibleViews.length > 1;
  }

  private handleSearchInput(event: Event) {
    const input = event.target as HTMLInputElement;
    this.emitSearch(input.value);
  }

  private handleSearchClear() {
    this.emitSearch('');
  }

  private emitSearch(value: string) {
    this.search = value;
    this.dispatchEvent(
      new CustomEvent('search-change', {
        detail: { value },
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleViewClick(view: ListViewMode) {
    if (view === this.view) {
      return;
    }
    this.view = view;
    this.dispatchEvent(
      new CustomEvent('view-change', {
        detail: { value: view },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    return html`
      <div class="list-toolbar">
        <form
          class="filters"
          @submit=${(event: Event) => event.preventDefault()}
        >
          <sl-input
            class="search-input"
            label=${this.searchPlaceholder}
            placeholder=${this.searchPlaceholder}
            clearable
            .value=${this.search}
            @sl-input=${this.handleSearchInput}
            @sl-clear=${this.handleSearchClear}
          >
            <sl-icon name="search" slot="prefix"></sl-icon>
          </sl-input>
          <slot></slot>
        </form>

        <div class="view-switcher-group">
          <span class="results-count" aria-live="polite">
            <slot name="count"></slot>
          </span>
          ${
            this.showToggle
              ? html`
                  <span class="toolbar-divider" aria-hidden="true"></span>
                  <sl-button-group label=${this.toggleLabel}>
                    ${VIEW_OPTIONS.filter((option) =>
                      this.visibleViews.includes(option.value)
                    ).map(
                      (option) => html`
                        <sl-button
                          size="small"
                          data-view=${option.value}
                          variant=${
                            this.view === option.value ? 'primary' : 'default'
                          }
                          aria-pressed=${this.view === option.value}
                          @click=${() => this.handleViewClick(option.value)}
                        >
                          <sl-icon slot="prefix" name=${option.icon}></sl-icon>
                          ${option.label}
                        </sl-button>
                      `
                    )}
                  </sl-button-group>
                `
              : nothing
          }
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'list-toolbar': ListToolbar;
  }
}
