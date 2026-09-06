import {
  LitElement,
  css,
  html,
  nothing,
  type ReactiveController,
  type ReactiveControllerHost,
} from 'lit';
import { customElement, property } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';

import { confirmDialog, showToast } from './confirm-dialog';
import {
  DEFAULT_FETCH_CONCURRENCY,
  mapWithConcurrency,
} from '../utils/concurrency';

/**
 * Shared multi-select for console collections.
 *
 * Every collection page (agents, API keys, models, flows) had the same row
 * actions one row at a time, so pausing six agents was six kebabs and six
 * confirmations. This module is the one way a list offers "pick several, do
 * one thing": an immutable selection model, a row checkbox that fits a table
 * cell and a card corner, a bulk action bar, and a runner that fans the single
 * endpoints out with a small bound and reports per item.
 *
 * The pieces are deliberately separate so a page can adopt them one at a time,
 * and so the approvals list (which already tracks `selectedIds` and toggles
 * with X) can hand its selection to `ListSelection` without changing shape.
 */

/** An id list plus the anchor a shift-range extends from. Immutable. */
export class ListSelection {
  /** Selected ids, in the order they were selected. */
  readonly ids: readonly string[];
  /** The last id toggled on its own; where a shift-range starts. */
  readonly anchorId: string | null;

  private constructor(ids: readonly string[], anchorId: string | null) {
    this.ids = ids;
    this.anchorId = anchorId;
  }

  /** The empty selection. Every page starts here and returns here on Escape. */
  static empty(): ListSelection {
    return new ListSelection([], null);
  }

  /** Rebuilds a selection from ids, for a page restoring its own state. */
  static of(ids: readonly string[], anchorId: string | null = null) {
    const unique = Array.from(new Set(ids));
    return new ListSelection(
      unique,
      anchorId && unique.includes(anchorId)
        ? anchorId
        : (unique[unique.length - 1] ?? null)
    );
  }

  get size(): number {
    return this.ids.length;
  }

  get isEmpty(): boolean {
    return this.ids.length === 0;
  }

  has(id: string): boolean {
    return this.ids.includes(id);
  }

  /**
   * Adds or removes one id. Adding moves the anchor to it; removing clears
   * the anchor if it left the set, the same way `deselectAll` does.
   */
  toggle(id: string): ListSelection {
    if (this.has(id)) {
      const remaining = this.ids.filter((selected) => selected !== id);
      return new ListSelection(
        remaining,
        this.anchorId && remaining.includes(this.anchorId)
          ? this.anchorId
          : null
      );
    }
    return new ListSelection([...this.ids, id], id);
  }

  /**
   * Selects everything between the anchor and `id` inclusive, in page order.
   *
   * Shift-click adds a range rather than replacing the selection, which is
   * what a file manager does and what an operator picking two clusters of
   * rows expects. Without an anchor (shift-click as the first click) this is
   * a plain toggle.
   */
  extendTo(id: string, order: readonly string[]): ListSelection {
    const anchor = this.anchorId;
    if (anchor === null || anchor === id) {
      return this.toggle(id);
    }
    const from = order.indexOf(anchor);
    const to = order.indexOf(id);
    if (from === -1 || to === -1) {
      return this.toggle(id);
    }
    const [start, end] = from <= to ? [from, to] : [to, from];
    const range = order.slice(start, end + 1);
    const added = range.filter((candidate) => !this.has(candidate));
    // The anchor stays put so a second shift-click grows the same range
    // additively (nothing is removed) instead of walking away from where
    // the operator started.
    return new ListSelection([...this.ids, ...added], anchor);
  }

  /** Selects every id on the page, keeping ids selected elsewhere. */
  selectAll(order: readonly string[]): ListSelection {
    const added = order.filter((id) => !this.has(id));
    return new ListSelection([...this.ids, ...added], this.anchorId);
  }

  /** Deselects every id on the page, keeping ids selected elsewhere. */
  deselectAll(order: readonly string[]): ListSelection {
    const remaining = this.ids.filter((id) => !order.includes(id));
    return new ListSelection(
      remaining,
      this.anchorId && remaining.includes(this.anchorId) ? this.anchorId : null
    );
  }

  clear(): ListSelection {
    return ListSelection.empty();
  }

  /**
   * Drops ids that are no longer on the page. A filter change or a reload
   * must not leave a bar offering to pause rows nobody can see.
   */
  retain(order: readonly string[]): ListSelection {
    const kept = this.ids.filter((id) => order.includes(id));
    if (kept.length === this.ids.length) {
      return this;
    }
    return new ListSelection(
      kept,
      this.anchorId && kept.includes(this.anchorId) ? this.anchorId : null
    );
  }

  /** True when every id on the page is selected (and the page is not empty). */
  allSelected(order: readonly string[]): boolean {
    return order.length > 0 && order.every((id) => this.has(id));
  }

  /** True when some but not all of the page is selected: the header dash. */
  someSelected(order: readonly string[]): boolean {
    return (
      order.some((id) => this.has(id)) && !order.every((id) => this.has(id))
    );
  }

  /** The selected items, in page order rather than in click order. */
  pick<T>(items: readonly T[], idOf: (item: T) => string): T[] {
    return items.filter((item) => this.has(idOf(item)));
  }
}

/**
 * A row or card checkbox.
 *
 * Emits `selection-toggle` with `{ id, checked, range }`; `range` is true when
 * the operator held shift, which the page turns into `ListSelection.extendTo`.
 * A header checkbox passes no `item-id` and reports `{ id: null }`.
 */
@customElement('list-select-checkbox')
export class ListSelectCheckbox extends LitElement {
  /** The id this checkbox selects. Empty means "select all on this page". */
  @property({ type: String, attribute: 'item-id' }) itemId = '';
  /** Accessible name, e.g. "Select Payments agent". Never rely on the row. */
  @property({ type: String }) label = 'Select item';
  @property({ type: Boolean }) checked = false;
  @property({ type: Boolean }) indeterminate = false;
  @property({ type: Boolean }) disabled = false;

  /** Shift state captured on the way in, since sl-change carries no keys. */
  private rangeIntent = false;

  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
    }
    sl-checkbox::part(base) {
      /* The label is for assistive tech only: the row beside it is the label
         a sighted operator reads. */
      gap: 0;
    }
    sl-checkbox::part(label) {
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
  `;

  private handlePointerDown(event: MouseEvent) {
    this.rangeIntent = event.shiftKey;
  }

  private handleKeyDown(event: KeyboardEvent) {
    if (event.key === ' ' || event.key === 'Enter') {
      this.rangeIntent = event.shiftKey;
    }
  }

  private handleChange(event: Event) {
    event.stopPropagation();
    const range = this.rangeIntent;
    this.rangeIntent = false;
    const checkbox = event.target as HTMLInputElement;
    this.dispatchEvent(
      new CustomEvent('selection-toggle', {
        detail: {
          id: this.itemId || null,
          checked: checkbox.checked,
          range,
        },
        bubbles: true,
        composed: true,
      })
    );
    // The page owns the truth. Re-assert it after the widget flipped itself,
    // so a rejected or reordered toggle cannot leave a lying checkbox.
    this.syncChecked();
  }

  protected updated() {
    this.syncChecked();
  }

  private syncChecked() {
    const checkbox = this.renderRoot.querySelector<
      HTMLElement & { checked: boolean; indeterminate: boolean }
    >('sl-checkbox');
    if (!checkbox) return;
    checkbox.checked = this.checked;
    checkbox.indeterminate = this.indeterminate && !this.checked;
  }

  render() {
    return html`
      <sl-checkbox
        ?checked=${this.checked}
        ?indeterminate=${this.indeterminate && !this.checked}
        ?disabled=${this.disabled}
        @pointerdown=${this.handlePointerDown}
        @keydown=${this.handleKeyDown}
        @sl-change=${this.handleChange}
        @click=${(event: Event) => event.stopPropagation()}
        >${this.label}</sl-checkbox
      >
    `;
  }
}

/**
 * One button in the bulk bar. Ids match the row kebab's action ids.
 *
 * There is no per action `disabled` or `outline`: danger is outlined and
 * pushed away by rule below, and the only thing that disables a button is a
 * run in flight, which the bar already knows about. A field no caller sets is
 * a field nobody maintains.
 */
export interface BulkAction {
  id: string;
  label: string;
  icon?: string;
  variant?:
    'default' | 'primary' | 'success' | 'neutral' | 'warning' | 'danger';
}

/**
 * The strip that appears once something is selected: "3 selected · Pause ·
 * Decommission · Clear". One hairline row, not a floating box: depth stays at
 * two and the strip reads as part of the table it sits above.
 *
 * @fires bulk-action - `{ detail: { id } }` when an action button is pressed
 * @fires selection-clear - when Clear is pressed
 */
@customElement('list-bulk-bar')
export class ListBulkBar extends LitElement {
  @property({ type: Number }) count = 0;
  @property({ type: Array }) actions: BulkAction[] = [];
  /** Id of the action currently running, if any. */
  @property({ type: String }) running: string | null = null;
  /** Items finished so far in the running action. */
  @property({ type: Number, attribute: 'progress-done' }) progressDone = 0;
  /** Items in the running action. */
  @property({ type: Number, attribute: 'progress-total' }) progressTotal = 0;
  /** Names the bar for assistive tech: "Agent bulk actions". */
  @property({ type: String }) label = 'Bulk actions';

  static styles = css`
    :host {
      display: block;
      width: 100%;
    }
    .bulk-bar {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
      flex-wrap: wrap;
      padding: var(--sl-spacing-x-small) 0;
      border-top: 1px solid var(--console-hairline);
      border-bottom: 1px solid var(--console-hairline);
    }
    .count {
      font-size: var(--console-text-meta);
      font-variant-numeric: tabular-nums;
      font-weight: 600;
      color: var(--console-body-color);
      white-space: nowrap;
    }
    .progress {
      font-size: var(--console-text-meta);
      font-variant-numeric: tabular-nums;
      color: var(--console-meta-color);
      white-space: nowrap;
    }
    .separator {
      color: var(--console-meta-color);
    }
    .spacer {
      flex: 1 1 auto;
    }
    /* Destructive actions never sit next to the everyday ones. */
    sl-button.destructive {
      margin-left: var(--sl-spacing-large);
    }
    @media (max-width: 640px) {
      sl-button.destructive {
        margin-left: 0;
      }
    }
  `;

  private emit(name: string, detail?: unknown) {
    this.dispatchEvent(
      new CustomEvent(name, { detail, bubbles: true, composed: true })
    );
  }

  render() {
    if (this.count <= 0) {
      return nothing;
    }
    const busy = this.running !== null;
    return html`
      <div class="bulk-bar" role="toolbar" aria-label=${this.label}>
        <!-- Static text on purpose: the count changes because the operator
             just ticked a box, and a second live region would make a run
             announce the count over every progress tick. -->
        <span class="count" data-testid="bulk-count"
          >${this.count} selected</span
        >
        <span class="separator" aria-hidden="true">·</span>
        ${this.actions.map((action) => {
          const destructive = action.variant === 'danger';
          return html`
            <sl-button
              size="small"
              class=${destructive ? 'destructive' : ''}
              data-action=${action.id}
              variant=${action.variant || 'default'}
              ?outline=${destructive}
              ?disabled=${busy && this.running !== action.id}
              ?loading=${this.running === action.id}
              @click=${() => this.emit('bulk-action', { id: action.id })}
            >
              ${
                action.icon
                  ? html`<sl-icon slot="prefix" name=${action.icon}></sl-icon>`
                  : nothing
              }
              ${action.label}
            </sl-button>
          `;
        })}
        <span class="spacer"></span>
        ${
          busy && this.progressTotal > 0
            ? html`<span
                class="progress"
                role="status"
                aria-live="polite"
                data-testid="bulk-progress"
                >${this.progressDone} of ${this.progressTotal}</span
              >`
            : nothing
        }
        <sl-button
          size="small"
          variant="text"
          data-action="clear"
          ?disabled=${busy}
          @click=${() => this.emit('selection-clear')}
        >
          Clear
        </sl-button>
      </div>
    `;
  }
}

/** The minimum a bulk run needs to know about an item: what it is called. */
export interface BulkItem {
  id: string;
  name: string;
}

export interface BulkFailure<T extends BulkItem> {
  item: T;
  message: string;
}

export interface BulkResult<T extends BulkItem> {
  succeeded: T[];
  failed: BulkFailure<T>[];
}

export interface RunBulkOptions {
  /** Requests in flight at once. Defaults to the console's bound of 4. */
  concurrency?: number;
  /** Called after each item settles, for the "3 of 7" in the bar. */
  onProgress?: (done: number, total: number) => void;
}

/**
 * Runs one action over the selected items with a bounded fan-out.
 *
 * A failure never stops the run: the operator asked for seven, and six that
 * worked plus a named one that did not is more useful than a run that stopped
 * at the first 403 leaving the rest in an unknown state.
 */
export async function runBulkAction<T extends BulkItem>(
  items: readonly T[],
  run: (item: T) => Promise<unknown>,
  options: RunBulkOptions = {}
): Promise<BulkResult<T>> {
  const total = items.length;
  let done = 0;
  options.onProgress?.(0, total);
  const outcomes = await mapWithConcurrency(
    items,
    options.concurrency ?? DEFAULT_FETCH_CONCURRENCY,
    async (item) => {
      try {
        await run(item);
        return { item, message: null as string | null };
      } catch (error) {
        return {
          item,
          message:
            error instanceof Error && error.message
              ? error.message
              : 'Request failed',
        };
      } finally {
        done += 1;
        options.onProgress?.(done, total);
      }
    }
  );
  return {
    succeeded: outcomes.filter((o) => o.message === null).map((o) => o.item),
    failed: outcomes
      .filter((o) => o.message !== null)
      .map((o) => ({ item: o.item, message: o.message as string })),
  };
}

/** How many names a confirmation spells out before it counts the rest. */
const MAX_NAMED = 10;

/** "Alpha, Beta and 3 more" - a confirmation names what it will touch. */
export function formatItemNames(
  names: readonly string[],
  max = MAX_NAMED
): string {
  if (names.length === 0) return '';
  if (names.length <= max) {
    return names.join(', ');
  }
  const shown = names.slice(0, max).join(', ');
  return `${shown} and ${names.length - max} more`;
}

export interface ConfirmBulkOptions {
  title: string;
  /** Sentence the names complete, e.g. "Pause 3 agents?" */
  message: string;
  names: readonly string[];
  detail?: string;
  confirmLabel: string;
  variant?: 'danger' | 'primary';
}

/**
 * The shared confirmation for a bulk action, with the names listed.
 *
 * A dialog that says "delete 7 flows?" asks the operator to trust a count they
 * cannot check. The names are the check.
 */
export function confirmBulkAction(
  options: ConfirmBulkOptions
): Promise<boolean> {
  const names = formatItemNames(options.names);
  return confirmDialog({
    title: options.title,
    message: `${options.message}\n\n${names}`,
    detail: options.detail,
    confirmLabel: options.confirmLabel,
    variant: options.variant,
  });
}

/** How a finished run describes itself: "pause", "paused", "agent". */
export interface BulkReport {
  /** Infinitive, for the failure sentence ("Could not pause Gamma"). */
  verb: string;
  /** Past tense, for the success sentence ("3 agents paused"). */
  verbPast: string;
  /** Singular subject; the plural is the noun plus "s". */
  noun: string;
}

/** Compact description of who failed and why, grouping shared messages. */
function formatFailedItems<T extends BulkItem>(
  failed: readonly BulkFailure<T>[]
): string {
  if (failed.length === 0) return '';
  const uniqueMessages = [...new Set(failed.map((failure) => failure.message))];
  if (uniqueMessages.length === 1) {
    const names = formatItemNames(
      failed.map((failure) => failure.item.name),
      3
    );
    const message = uniqueMessages[0];
    return message ? `${names} (${message})` : names;
  }
  if (failed.length <= 3) {
    return failed
      .map((failure) =>
        failure.message
          ? `${failure.item.name} (${failure.message})`
          : failure.item.name
      )
      .join(', ');
  }
  const groups = new Map<string, string[]>();
  for (const failure of failed) {
    const key = failure.message || 'Request failed';
    const names = groups.get(key) ?? [];
    names.push(failure.item.name);
    groups.set(key, names);
  }
  return Array.from(groups, ([message, names]) => {
    return `${formatItemNames(names, 3)} (${message})`;
  }).join('; ');
}

/**
 * One toast for the whole run: what worked and, by name, what did not.
 */
export function bulkResultMessage<T extends BulkItem>(
  result: BulkResult<T>,
  report: BulkReport
): string {
  const okCount = result.succeeded.length;
  const okNoun = okCount === 1 ? report.noun : `${report.noun}s`;
  if (result.failed.length === 0) {
    return `${okCount} ${okNoun} ${report.verbPast}`;
  }
  const failedDetail = formatFailedItems(result.failed);
  if (okCount === 0) {
    return `Could not ${report.verb} ${failedDetail}`;
  }
  return `${okCount} ${okNoun} ${report.verbPast}, ${result.failed.length} failed: ${failedDetail}`;
}

/** Shows the end-of-run toast. Success only when nothing failed. */
export function reportBulkResult<T extends BulkItem>(
  result: BulkResult<T>,
  report: BulkReport
): void {
  const message = bulkResultMessage(result, report);
  const variant =
    result.failed.length === 0
      ? 'success'
      : result.succeeded.length === 0
        ? 'danger'
        : 'warning';
  showToast(message, variant);
}

export interface SelectionControllerOptions<T> {
  idOf: (item: T) => string;
  /** True when a row can be selected at all (a revoked key cannot). */
  selectable?: (item: T) => boolean;
}

/**
 * Wires the selection model to a Lit view: page order, keyboard, progress.
 *
 * Keyboard, on any list that installs this: `x` toggles the row the focus is
 * in, `shift+X` extends the range from the anchor, `Escape` clears. The row
 * is found from the event path, so the keys work from the checkbox, the name
 * link or the kebab without every table growing a roving tabindex.
 */
export class ListSelectionController<T> implements ReactiveController {
  private host: ReactiveControllerHost & HTMLElement;
  private options: SelectionControllerOptions<T>;
  private items: readonly T[] = [];

  selection = ListSelection.empty();
  /** Id of the running bulk action, or null. Drives the bar's spinner. */
  running: string | null = null;
  progressDone = 0;
  progressTotal = 0;

  constructor(
    host: ReactiveControllerHost & HTMLElement,
    options: SelectionControllerOptions<T>
  ) {
    this.host = host;
    this.options = options;
    host.addController(this);
  }

  hostConnected(): void {
    this.host.addEventListener('keydown', this.handleKeyDown);
  }

  hostDisconnected(): void {
    this.host.removeEventListener('keydown', this.handleKeyDown);
  }

  /**
   * Tells the controller which rows the page is showing, in display order.
   * Selections for rows that went away are dropped.
   *
   * Call it from `willUpdate`, never from inside `render`. Lit evaluates
   * template expressions in source order, so a bar rendered above the branch
   * that prunes reads the count from one pass ago and can paint "3 selected"
   * over a page that has nothing selected. `willUpdate` runs before every
   * expression in the pass, which is the only place the count is true for the
   * whole template.
   *
   * Pruning deliberately does not request another update: it runs inside the
   * pass that is already rendering the new rows, and asking for a second one
   * is how a list ends up in an update loop.
   */
  setItems(items: readonly T[]): void {
    this.items = items.filter(
      (item) => this.options.selectable?.(item) ?? true
    );
    this.selection = this.selection.retain(this.order);
  }

  get order(): string[] {
    return this.items.map((item) => this.options.idOf(item));
  }

  get count(): number {
    return this.selection.size;
  }

  /**
   * True while a bulk run is in flight.
   *
   * The bar locks its own buttons, and pages pass this to the row and header
   * checkboxes too: a run ends by leaving only the failures selected, so a
   * row ticked while "2 of 7" was counting would be silently dropped.
   */
  get busy(): boolean {
    return this.running !== null;
  }

  get selectedIds(): readonly string[] {
    return this.selection.ids;
  }

  get selectedItems(): T[] {
    return this.selection.pick(this.items, this.options.idOf);
  }

  isSelected(id: string): boolean {
    return this.selection.has(id);
  }

  get allSelected(): boolean {
    return this.selection.allSelected(this.order);
  }

  get someSelected(): boolean {
    return this.selection.someSelected(this.order);
  }

  /** Handles the `selection-toggle` event from a row or header checkbox. */
  handleToggleEvent = (event: Event): void => {
    const detail = (event as CustomEvent).detail as {
      id: string | null;
      checked: boolean;
      range: boolean;
    };
    if (detail.id === null) {
      this.toggleAll(detail.checked);
      return;
    }
    this.toggle(detail.id, detail.range);
  };

  toggle(id: string, range = false): void {
    this.selection = range
      ? this.selection.extendTo(id, this.order)
      : this.selection.toggle(id);
    this.host.requestUpdate();
  }

  toggleAll(checked: boolean): void {
    this.selection = checked
      ? this.selection.selectAll(this.order)
      : this.selection.deselectAll(this.order);
    this.host.requestUpdate();
  }

  /**
   * Empties the selection and always asks for a repaint.
   *
   * The repaint is unconditional on purpose: if a bar ever renders from a
   * count that a later `setItems` pruned away, Clear is the operator's way out
   * and it has to redraw even when the model is already empty.
   */
  clear(): void {
    this.selection = this.selection.clear();
    this.host.requestUpdate();
  }

  /**
   * Runs `action` over the selected items and reports once.
   *
   * The caller supplies the confirmation (destructive actions get one with the
   * names) and the reload; this owns the progress counter and the toast so
   * every page counts and reports the same way.
   */
  async run<I extends BulkItem>(
    actionId: string,
    items: readonly I[],
    action: (item: I) => Promise<unknown>,
    report: BulkReport
  ): Promise<BulkResult<I>> {
    if (this.running !== null) {
      return { succeeded: [], failed: [] };
    }
    this.running = actionId;
    this.progressDone = 0;
    this.progressTotal = items.length;
    this.host.requestUpdate();
    try {
      const result = await runBulkAction(items, action, {
        onProgress: (done, total) => {
          this.progressDone = done;
          this.progressTotal = total;
          this.host.requestUpdate();
        },
      });
      this.settle(result, report);
      return result;
    } finally {
      this.running = null;
      this.progressDone = 0;
      this.progressTotal = 0;
      this.host.requestUpdate();
    }
  }

  /**
   * Runs one call that carries the whole selection, and reports the same way.
   *
   * Some collections have a real batch endpoint (approvals decide the picked
   * ids in one POST). Those cannot report "3 of 7" because there is one
   * request, but everything after it is identical: one toast, and only the
   * failures stay selected. `action` returns the per item outcome the server
   * sent back.
   */
  async runBatch<I extends BulkItem>(
    actionId: string,
    items: readonly I[],
    action: (items: readonly I[]) => Promise<BulkResult<I>>,
    report: BulkReport
  ): Promise<BulkResult<I>> {
    if (this.running !== null) {
      return { succeeded: [], failed: [] };
    }
    this.running = actionId;
    // No per item progress: one request settles all of them at once.
    this.progressDone = 0;
    this.progressTotal = 0;
    this.host.requestUpdate();
    try {
      const result = await action(items);
      this.settle(result, report);
      return result;
    } catch (error) {
      const message =
        error instanceof Error && error.message
          ? error.message
          : 'Request failed';
      const failed = items.map((item) => ({ item, message }));
      const result: BulkResult<I> = { succeeded: [], failed };
      this.settle(result, report);
      return result;
    } finally {
      this.running = null;
      this.host.requestUpdate();
    }
  }

  /**
   * The end of every run: one toast, and only the failures stay selected so a
   * retry is one click and a finished run leaves a quiet page.
   */
  private settle<I extends BulkItem>(
    result: BulkResult<I>,
    report: BulkReport
  ): void {
    reportBulkResult(result, report);
    this.selection = ListSelection.of(
      result.failed.map((failure) => failure.item.id)
    );
  }

  private handleKeyDown = (event: KeyboardEvent): void => {
    if (
      event.defaultPrevented ||
      event.metaKey ||
      event.ctrlKey ||
      event.altKey
    ) {
      return;
    }
    if (isTypingTarget(event)) return;

    if (event.key === 'Escape') {
      if (this.selection.isEmpty) return;
      event.preventDefault();
      this.clear();
      return;
    }
    if (event.key !== 'x' && event.key !== 'X') return;
    const id = selectionIdFromEvent(event);
    if (!id || !this.order.includes(id)) return;
    event.preventDefault();
    this.toggle(id, event.shiftKey);
  };
}

/** The nearest `data-selection-id` in the event path, if any. */
export function selectionIdFromEvent(event: Event): string | null {
  for (const node of event.composedPath()) {
    const element = node as HTMLElement;
    const id = element?.dataset?.selectionId;
    if (id) return id;
  }
  return null;
}

/**
 * True when the key belongs to a field the operator is typing in.
 *
 * A checkbox is not one: X has to keep working while the focus sits on the
 * row's own checkbox, which is a real `<input>` inside the Shoelace shadow
 * root and would otherwise swallow the key.
 */
function isTypingTarget(event: Event): boolean {
  return event.composedPath().some((node) => {
    const element = node as HTMLElement;
    const tag = element?.tagName?.toLowerCase();
    if (tag === 'input') {
      const type = (element as HTMLInputElement).type;
      return type !== 'checkbox' && type !== 'radio' && type !== 'button';
    }
    return (
      tag === 'textarea' ||
      tag === 'select' ||
      tag === 'sl-input' ||
      tag === 'sl-textarea' ||
      tag === 'sl-select' ||
      element?.isContentEditable === true
    );
  });
}

declare global {
  interface HTMLElementTagNameMap {
    'list-select-checkbox': ListSelectCheckbox;
    'list-bulk-bar': ListBulkBar;
  }
}
