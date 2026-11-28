import { test, expect } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
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
        <style>
        body { font-family: system-ui, sans-serif; margin: 32px; color: #263241;
          --console-text-body: 14px; --console-text-meta: 13px;
          --console-meta-color: var(--sl-color-neutral-600);
          --console-hairline: var(--sl-color-neutral-200); }
        h1 { font-size: 24px; margin-bottom: 24px; }
        </style>
        </head><body><h1>Extension inventory</h1><extension-inventory></extension-inventory></body></html>`,
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
        CGO_ENABLED: '0',
        PRELOOP_EXTENSION_E2E_URL: `http://127.0.0.1:${apiPort}`,
      },
    }
  );
  expect(result.status, result.stderr + result.stdout).toBe(0);
  await page.addInitScript(() =>
    localStorage.setItem('accessToken', 'synthetic-extension-identity')
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/extension-fixture');
  const row = page
    .getByRole('row')
    .filter({
      has: page.getByRole('button', {
        name: 'Details for reviewer@community',
        exact: true,
      }),
    });
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
      .filter({
        has: page.getByRole('button', {
          name: 'Details for reviewer@community',
          exact: true,
        }),
      })
      .locator('sl-select')
  ).toHaveJSProperty('value', 'unapproved');
  await expect(row).toContainText('Review helpers for consistent code checks.');
  const captureDir = process.env.PRELOOP_EXTENSION_SCREENSHOTS;
  if (captureDir) await mkdir(captureDir, { recursive: true });
  for (const [kind, id, detail] of [
    ['skill', 'careful', /Bundled scripts\s*1/],
    ['hook', 'PreToolUse', 'Bash|Edit'],
    ['plugin', 'reviewer@community', /Publisher\s*Jane Doe/],
  ] as const) {
    await page
      .getByRole('button', { name: `Details for ${id}`, exact: true })
      .click();
    const expanded = page.locator('.extension-details');
    await expect(expanded).toContainText(detail);
    await expect(
      expanded.getByRole('list', { name: 'Device locations' })
    ).toBeVisible();
    await expect(expanded).toContainText(
      `preloop agents extensions show ${kind}`
    );
    if (captureDir)
      await page.screenshot({
        path: resolve(captureDir, `${kind}.png`),
        fullPage: true,
      });
  }
  await page.getByRole('searchbox').fill('permissions');
  await expect(
    page.getByRole('button', { name: 'Details for careful', exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Details for PreToolUse', exact: true })
  ).toHaveCount(0);
});
