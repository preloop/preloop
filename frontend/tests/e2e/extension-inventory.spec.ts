import { test, expect } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const apiPort = Number(process.env.PRELOOP_EXTENSION_API_PORT || 58147);

// protects=fixture home -> CLI report -> PostgreSQL -> console approval;
// fails_when=an installed plugin is missing or its decision does not persist;
// no_claim=synthetic identity/profile enumeration replace OS users and login.
test('reports a plugin and persists its account approval through the console', async ({
  page,
}) => {
  await page.route('**/*', async (route) => {
    const host = new URL(route.request().url()).hostname;
    if (!['localhost', '127.0.0.1', '[::1]'].includes(host))
      return route.abort();
    if (new URL(route.request().url()).pathname === '/extension-fixture') {
      return route.fulfill({
        contentType: 'text/html',
        body: `<!doctype html><html><head>
        <link rel="stylesheet" href="/node_modules/@shoelace-style/shoelace/dist/themes/light.css">
        <script type="module" src="/src/components/extension-inventory.ts"></script>
        </head><body><extension-inventory></extension-inventory></body></html>`,
      });
    }
    return route.continue();
  });
  const result = spawnSync(
    'go',
    [
      'test',
      './internal/cmd',
      '-run',
      '^TestExtensionReportToLocalStack$',
      '-count=1',
    ],
    {
      cwd: resolve(root, 'cli'),
      encoding: 'utf8',
      timeout: 90_000,
      env: {
        ...process.env,
        PRELOOP_DISABLE_TELEMETRY: 'true',
        PRELOOP_EXTENSION_E2E_URL: `http://127.0.0.1:${apiPort}`,
      },
    }
  );
  expect(result.status, result.stderr + result.stdout).toBe(0);
  await page.addInitScript(() =>
    localStorage.setItem('accessToken', 'synthetic-extension-identity')
  );
  await page.goto('/extension-fixture');
  const row = page.getByRole('row').filter({ hasText: 'reviewer@community' });
  await expect(row).toBeVisible();
  await expect(row).toContainText('1.4.0');
  const approval = row.locator('sl-select');
  await expect(approval).toHaveJSProperty('value', 'unknown');
  await approval.click();
  await page.getByRole('option', { name: 'Unapproved', exact: true }).click();
  await expect(row.locator('sl-select')).toHaveJSProperty(
    'value',
    'unapproved'
  );
  await page.reload();
  await expect(
    page
      .getByRole('row')
      .filter({ hasText: 'reviewer@community' })
      .locator('sl-select')
  ).toHaveJSProperty('value', 'unapproved');
});
