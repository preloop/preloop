/**
 * #1125 E2E: the Sensitive data tab on the Policies page.
 *
 * Synthetic data only. Logs in, opens Policies > Sensitive data, sets
 * Email addresses to Redact in logs, tests "mail a@example.com" against the
 * real detector endpoint, saves through the policy diff and import path,
 * reloads and checks that the choice persisted (read back from the export).
 *
 * HOW TO RUN: a local backend seeded with INIT_TEST_DATA=true (admin/admin)
 * and the dev frontend, then
 *        cd frontend
 *        PRELOOP_E2E_BASE_URL=http://localhost:5173 \
 *          npx playwright test --project=e2e-ci sensitive-data-tab
 * Set PRELOOP_E2E_SCREENSHOTS=<dir> to also write the PR screenshots.
 */
import { test, expect, type Page } from '@playwright/test';
import * as path from 'path';
import { dismissPlanChoiceIfShown } from './login';

const SHOTS = process.env.PRELOOP_E2E_SCREENSHOTS;
const USERNAME = process.env.PRELOOP_E2E_USERNAME || 'admin';
const PASSWORD = process.env.PRELOOP_E2E_PASSWORD || 'admin';

async function shot(page: Page, name: string): Promise<void> {
  if (SHOTS) {
    await page.waitForTimeout(400);
    await page.screenshot({
      path: path.join(SHOTS, `${name}.png`),
      fullPage: true,
    });
  }
}

async function login(page: Page): Promise<void> {
  await page.goto('/login');
  const username = page.locator('sl-input[name="username"]');
  await username.waitFor({ state: 'visible' });
  await username.click();
  await page.keyboard.type(USERNAME);
  await page.locator('sl-input[name="password"]').click();
  await page.keyboard.type(PASSWORD);
  await page.locator('sl-button[type="submit"]').click();
  await page.waitForURL(/\/console/, { timeout: 30_000 });
  await dismissPlanChoiceIfShown(page);
}

async function openTab(page: Page) {
  await page.goto('/console/policies');
  await page.locator('sl-tab[panel="sensitive-data"]').click();
  const panel = page.locator('sensitive-data-panel');
  await expect(panel.locator('#type-email')).toBeVisible({ timeout: 30_000 });
  return panel;
}

test('sensitive data tab: redact email, test, save, reload', async ({
  page,
}) => {
  await login(page);
  let panel = await openTab(page);

  const email = panel.locator('#type-email');
  // Run on a fresh account: the email rule must not exist yet.
  await expect(email).not.toBeChecked();
  await email.check();
  await panel.getByRole('radio', { name: 'Redact in logs' }).first().check();
  await expect(panel.getByTestId('sensitive-summary')).toContainText(
    '[REDACTED:email]'
  );

  await panel.getByLabel(/Sample text/).fill('mail a@example.com');
  await panel.getByTestId('sensitive-test').click();
  await expect(panel.getByTestId('sensitive-stored')).toHaveText(
    'mail [REDACTED:email]'
  );
  await expect(panel.locator('mark[data-type="email"]')).toContainText(
    'a@example.com'
  );
  await expect(panel.getByTestId('sensitive-yaml')).toContainText(
    'id: console-redact'
  );
  await shot(page, 'sensitive-data-tab-test');

  await panel.getByTestId('sensitive-save').click();
  const apply = page.locator('sl-button', { hasText: 'Apply changes' });
  await expect(apply).toBeVisible({ timeout: 30_000 });
  await shot(page, 'sensitive-data-tab-diff');
  await apply.click();
  await expect(apply).toBeHidden({ timeout: 30_000 });

  await page.reload();
  panel = await openTab(page);
  await expect(panel.locator('#type-email')).toBeChecked();
  await expect(
    panel.locator('input[name="action-email"][value="redact"]')
  ).toBeChecked();
  await expect(panel.getByTestId('sensitive-summary')).toContainText(
    'matches are stored as [REDACTED:email].'
  );
  await shot(page, 'sensitive-data-tab-persisted');
});
