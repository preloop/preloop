import { unsafeCSS } from 'lit';
import styles from './table-scroll.css?inline';

/** Shared overflow containment for tables in Lit shadow roots. */
export const tableScrollStyles = unsafeCSS(styles);
