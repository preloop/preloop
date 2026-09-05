/**
 * Loads the real Shoelace design tokens into the test document.
 *
 * The test runner's page carries no theme stylesheet, so `--sl-spacing-medium`
 * and friends resolve to nothing and every Shoelace control renders at a
 * fraction of its real size. That is fine for behaviour tests and fatal for
 * layout ones: a 48px kebab button measures 22px, and a column too narrow to
 * hold it looks like it fits.
 *
 * Any test that asserts a rendered geometry awaits this first. It goes in as a
 * `<link>` rather than through `fetch`, because the suites that need it are
 * exactly the suites that have stubbed `window.fetch`.
 */
let loading: Promise<void> | null = null;

const THEME_URL =
  '/node_modules/@shoelace-style/shoelace/dist/themes/light.css';

export async function loadShoelaceTokens(): Promise<void> {
  if (loading) return loading;
  loading = new Promise<void>((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = THEME_URL;
    link.dataset.shoelaceTestTheme = 'true';
    link.addEventListener('load', () => resolve());
    link.addEventListener('error', () =>
      reject(new Error(`Could not load Shoelace tokens from ${THEME_URL}`))
    );
    document.head.appendChild(link);
  });
  return loading;
}
