import { css } from 'lit';

/**
 * The header recipe every list driven by `ListTable` shares.
 *
 * It is the uppercase sortable eyebrow the Flows and Executions tables
 * already carried, lifted out of one view so the next list gets the same
 * header, the same caret and the same hit area without copying them. Cell and
 * row rules stay with each view: what a row looks like is the view's
 * business, what a header does is the table layer's.
 */
export const listTableStyles = css`
  .table-header-content {
    display: flex;
    align-items: center;
    gap: var(--sl-spacing-small, 8px);
  }
  th.numeric .table-header-content {
    justify-content: flex-end;
  }
  .table-header-content > :not(column-picker) {
    min-width: 0;
  }
  .table-header-content > column-picker {
    flex: 0 0 auto;
    margin-left: auto;
    text-transform: none;
    letter-spacing: normal;
  }

  /* The button is the whole cell, so the hit area is the label, not the six
     pixels of the caret. */
  th.sortable {
    padding: 0;
  }
  /* Anchors the resize handle to the cell's right edge. */
  th {
    position: relative;
  }
  .sort-button {
    display: flex;
    align-items: center;
    gap: 4px;
    width: 100%;
    background: none;
    border: none;
    cursor: pointer;
    font: inherit;
    font-weight: var(--sl-font-weight-semibold);
    /* Sentence case at the meta size, as on Models and Gateway traffic:
       the uppercase header was one of two header styles in the console. */
    font-size: var(--console-text-meta, 13px);
    color: var(--sl-color-neutral-600);
    padding: 8px;
  }
  th.numeric .sort-button {
    justify-content: flex-end;
  }
  .sort-button:hover,
  .sort-button:focus-visible {
    color: var(--sl-color-neutral-900);
  }
  th.active .sort-button {
    color: var(--sl-color-neutral-900);
  }
  .sort-caret {
    font-size: 0.75em;
    opacity: 0.55;
  }
  th.active .sort-caret {
    opacity: 1;
  }
  /* Which column a multi-column sort applied first. Only drawn when there is
     more than one, because "1" beside a single sort says nothing. */
  .sort-rank {
    font-size: 0.75em;
    font-variant-numeric: tabular-nums;
    opacity: 0.7;
  }
  /* A 9px grab strip on the cell's right edge, invisible until it is wanted:
     a visible rule on every header would draw the gray grid the console
     spent wave 4 removing. */
  /* Inside the header cell: a handle that hangs past the edge makes every
     resizable header overflow its cell by a few pixels. */
  .col-resize {
    position: absolute;
    top: 0;
    right: 0;
    width: 9px;
    height: 100%;
    cursor: col-resize;
    touch-action: none;
    user-select: none;
    z-index: 1;
  }
  .col-resize::after {
    content: '';
    position: absolute;
    top: 25%;
    right: 0;
    width: 1px;
    height: 50%;
    background: var(--sl-color-neutral-400);
    opacity: 0;
    transition: opacity 150ms ease-out;
  }
  .col-resize:hover::after,
  .col-resize:focus-visible::after,
  .col-resize.resizing::after {
    opacity: 1;
  }
  .col-resize:focus-visible {
    outline: none;
  }
  th:last-child .col-resize {
    display: none;
  }
  .cell-truncate {
    display: block;
    max-width: var(--cell-truncate-max, 32rem);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
`;
