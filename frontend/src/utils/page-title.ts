import { getBrandConfig } from '../brand-config';

/**
 * `<page> · <brand>`, or just the page where no brand config is loaded.
 *
 * Kept apart from view-header so a page in the public bundle (the 404) can
 * title the tab without pulling in the console header and its styles.
 */
export function pageTitle(headerText: string): string {
  let brand = '';
  try {
    brand = getBrandConfig().name;
  } catch {
    // Unit tests and other hosts without the Vite brand plugin.
  }
  return brand ? `${headerText} · ${brand}` : headerText;
}
