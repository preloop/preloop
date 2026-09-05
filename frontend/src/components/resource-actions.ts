import { LitElement, html, css, unsafeCSS, type TemplateResult } from 'lit';
import { customElement, property, state, query } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import consoleStyles from '../styles/console-styles.css?inline';
import {
  LIST_TO_CARDS_BREAKPOINT,
  subscribeNarrowViewport,
  type NarrowViewportSubscription,
} from '../utils/view-mode';

export interface ResourceAction {
  id: string;
  label: string;
  icon?: string;
  variant?:
    | 'default'
    | 'primary'
    | 'success'
    | 'neutral'
    | 'warning'
    | 'danger'
    | 'text';
  /**
   * Render the button outlined rather than solid. Used for destructive
   * actions that should stay legible as "danger" without a solid red block
   * competing with the primary action on the page.
   */
  outline?: boolean;
  /**
   * Push this action away from the ones before it. Separates a destructive
   * action from the everyday ones so it is not clicked by muscle memory.
   */
  separated?: boolean;
  disabled?: boolean;
  loading?: boolean;
  tooltip?: string;
  href?: string;
  onClick?: () => void;
  render?: () => TemplateResult;
}

@customElement('resource-actions')
export class ResourceActions extends LitElement {
  /** Provide either a single array of actions (single resource view or pre-computed) */
  @property({ type: Array }) actions: ResourceAction[] = [];

  /** OR provide an array of action lists from multiple selected items. The component will intersect them by ID. */
  @property({ type: Array }) actionsSets?: ResourceAction[][];

  /** Render all actions behind the overflow menu, useful inside clickable cards. */
  @property({ type: Boolean, attribute: 'menu-only' }) menuOnly = false;

  /** When false, always show every action instead of collapsing into overflow. */
  @property({ type: Boolean, attribute: 'collapse-overflow' })
  collapseOverflow = true;

  @state() private containerWidth = 0;
  /** True below 640px, where a separated button has no room beside the rest. */
  @state() private narrowViewport = false;
  private resizeObserver!: ResizeObserver;
  private narrowViewportSubscription?: NarrowViewportSubscription;

  @query('.actions-container') container!: HTMLElement;
  @query('.measure-container') measureContainer!: HTMLElement;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        width: 100%;
      }
      .actions-container {
        display: flex;
        gap: var(--sl-spacing-small);
        justify-content: flex-end;
        align-items: center;
        flex-wrap: nowrap;
        width: 100%;
        overflow: hidden;
      }
      .measure-container {
        position: absolute;
        visibility: hidden;
        pointer-events: none;
        display: flex;
        gap: var(--sl-spacing-small);
        width: max-content;
      }
      /* The host cell can be right-aligned (table action cells are), and the
         menu inherits that through the slot, which parks every label far from
         its icon. The menu sets its own alignment. */
      sl-menu {
        text-align: left;
      }
      sl-menu-item::part(base) {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
      }
      .danger-item::part(base) {
        color: var(--sl-color-danger-600);
      }
      sl-icon[slot='prefix'] {
        font-size: 1rem;
        margin-right: var(--sl-spacing-2x-small);
      }
      .separated {
        margin-left: var(--sl-spacing-large);
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    this.narrowViewportSubscription = subscribeNarrowViewport((narrow) => {
      this.narrowViewport = narrow;
    }, LIST_TO_CARDS_BREAKPOINT);
    this.narrowViewport = this.narrowViewportSubscription.matches;
    this.resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.target === this.container) {
          const newWidth = entry.contentRect.width;
          if (Math.abs(this.containerWidth - newWidth) > 1) {
            this.containerWidth = newWidth;
          }
        }
      }
    });
  }

  protected firstUpdated() {
    if (this.container) {
      this.resizeObserver.observe(this.container);
      this.containerWidth = this.container.getBoundingClientRect().width;
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.resizeObserver.disconnect();
    this.narrowViewportSubscription?.disconnect();
    this.narrowViewportSubscription = undefined;
  }

  private getEffectiveActions(): ResourceAction[] {
    if (this.actionsSets && this.actionsSets.length > 0) {
      // Find intersection of all action sets by ID
      const baseSet = this.actionsSets[0];
      return baseSet.filter((baseAction) => {
        return this.actionsSets!.every((set) =>
          set.some((a) => a.id === baseAction.id)
        );
      });
    }
    return this.actions || [];
  }

  private renderButton(action: ResourceAction) {
    if (action.render) {
      return action.render();
    }

    const button = html`
      <sl-button
        class=${action.separated ? 'separated' : ''}
        variant=${action.variant || 'default'}
        ?outline=${action.outline}
        ?disabled=${action.disabled}
        ?loading=${action.loading}
        href=${action.href || ''}
        @click=${(e: Event) => {
          if (action.href) {
            // Shoelace anchor button handles it
            return;
          }
          if (action.onClick) {
            action.onClick();
          }
        }}
      >
        ${
          action.icon
            ? html`<sl-icon slot="prefix" name=${action.icon}></sl-icon>`
            : ''
        }
        ${action.label}
      </sl-button>
    `;

    if (action.tooltip) {
      return html`
        <sl-tooltip content=${action.tooltip}> ${button} </sl-tooltip>
      `;
    }

    return button;
  }

  private renderMenuItem(action: ResourceAction) {
    const isDanger = action.variant === 'danger';
    const classes = isDanger ? 'danger-item' : '';

    return html`
      <sl-menu-item
        class=${classes}
        ?disabled=${action.disabled}
        @click=${() => {
          if (action.href) {
            window.location.href = action.href;
          } else if (action.onClick) {
            action.onClick();
          }
        }}
      >
        ${
          action.icon
            ? html`<sl-icon
                slot="prefix"
                name=${action.icon}
                style=${isDanger ? 'color: var(--sl-color-danger-600);' : ''}
              ></sl-icon>`
            : ''
        }
        ${action.label}
      </sl-menu-item>
    `;
  }

  render() {
    const effectiveActions = this.getEffectiveActions();
    if (effectiveActions.length === 0) {
      return html`<div class="actions-container"></div>`;
    }

    // A separated action (Remove) is pushed away by `--sl-spacing-large`,
    // which the width heuristic below never counted, so on a phone it was
    // pushed straight out of the hidden overflow. Below 640px it moves into
    // the menu instead; above it, its margin joins the measurement.
    const SEPARATED_MARGIN = 24; // --sl-spacing-large
    const foldSeparated =
      this.narrowViewport &&
      !this.menuOnly &&
      this.collapseOverflow &&
      effectiveActions.length > 1 &&
      effectiveActions.some((action) => action.separated);
    const inlineActions = foldSeparated
      ? effectiveActions.filter((action) => !action.separated)
      : effectiveActions;
    const foldedActions = foldSeparated
      ? effectiveActions.filter((action) => action.separated)
      : [];

    // Heuristics for fitting: a button is approx 120px on average. Gap is 12px (sl-spacing-small).
    // Dropdown toggle is ~40px.
    // If containerWidth is 0 (initial), render all or wait.
    let visibleCount = inlineActions.length;
    if (this.menuOnly) {
      visibleCount = 0;
    }

    const separatedMargins =
      inlineActions.filter((action) => action.separated).length *
      SEPARATED_MARGIN;

    // We can do a rudimentary calculation based on container width.
    if (!this.menuOnly && this.collapseOverflow && this.containerWidth > 0) {
      // Let's assume average button width + gap is 120px
      const estimatedTotalWidth = inlineActions.length * 120 + separatedMargins;

      if (
        estimatedTotalWidth > this.containerWidth &&
        inlineActions.length > 1
      ) {
        // Space reserved for overflow dropdown button (~50px)
        const availableWidthForButtons =
          this.containerWidth - 50 - separatedMargins;
        visibleCount = Math.max(0, Math.floor(availableWidthForButtons / 120));
      }
    }

    const overflowCount = inlineActions.length - visibleCount;
    let visibleActions = inlineActions.slice(overflowCount);
    let overflowActions = inlineActions.slice(0, overflowCount);

    // If there is only 1 overflow action and it would fit in the overflow dropdown,
    // it's sometimes better to just show it if `visibleCount` allows. But we rely on the math.
    // However, if we drop exactly 1, the "..." button takes up space anyway.
    if (
      overflowActions.length === 1 &&
      foldedActions.length === 0 &&
      visibleCount * 120 + 120 + separatedMargins <= this.containerWidth
    ) {
      visibleActions = inlineActions;
      overflowActions = [];
    }
    // Destructive actions stay last in the menu, as they are on the row.
    overflowActions = [...overflowActions, ...foldedActions];

    return html`
      <div class="actions-container" part="container">
        ${
          overflowActions.length > 0
            ? html`
                <sl-dropdown placement="bottom-start">
                  <sl-button
                    slot="trigger"
                    variant="default"
                    ?caret=${!this.menuOnly}
                    aria-label="Resource actions"
                  >
                    <sl-icon
                      name=${this.menuOnly ? 'three-dots-vertical' : 'three-dots'}
                    ></sl-icon>
                  </sl-button>
                  <sl-menu>
                    ${overflowActions.map((action) =>
                      this.renderMenuItem(action)
                    )}
                  </sl-menu>
                </sl-dropdown>
              `
            : ''
        }
        ${visibleActions.map((action) => this.renderButton(action))}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'resource-actions': ResourceActions;
  }
}
