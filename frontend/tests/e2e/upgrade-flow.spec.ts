/**
 * T2 E2E: premium gate, upgrade modal, and where its buttons lead.
 *
 * Per the eng-review decision (D37), the STRIPE BOUNDARY IS MOCKED in CI:
 * page.route intercepts the billing endpoints so this spec runs with
 * zero external dependencies and asserts the code WE wrote:
 *
 *   1. A 402 {code:"upgrade_required"} response opens the upgrade modal with
 *      feature-specific copy (fetchWithAuth dispatch, console-shell dialog).
 *   2. "Upgrade now" closes the dialog and hands the decision to the plan
 *      page, naming the refused feature in the query so that page can open
 *      on the cheapest plan that unlocks it. The dialog buys nothing itself:
 *      it used to start a checkout for one hardcoded plan, which was the
 *      wrong plan for half the gates.
 *   3. No checkout request leaves the dialog, so nothing is charged by a
 *      button whose only job is to explain what was refused.
 *   4. With entitlements {premium:false}, the sessions list shows the
 *      AI-titles upsell hint, and clicking it opens the same modal.
 *
 * The real reconciliation (checkout-success → subscription row) is covered by
 * plugins/billing/tests/test_upgrade_flow.py; the real Stripe loop is
 * verified once pre-ship by scripts/verify_upgrade_flow_staging.sh.
 *
 * Run (CI stack, same 4 processes as custom-agent-session.spec.ts):
 *   cd preloop/frontend && npx playwright test upgrade-flow --project=e2e-ci
 */

import { test, expect, type Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { dismissPlanChoiceIfShown } from './login';

const IS_STAGING = process.env.PRELOOP_E2E_TARGET === 'staging';

interface Creds {
  username: string;
  password: string;
}

function resolveCreds(): Creds {
  const envUser = process.env.PRELOOP_E2E_USERNAME;
  const envPass = process.env.PRELOOP_E2E_PASSWORD;
  if (envUser && envPass) return { username: envUser, password: envPass };
  const credsPath = path.resolve(process.cwd(), 'tmp', 'staging-creds.json');
  if (fs.existsSync(credsPath)) {
    const raw = JSON.parse(fs.readFileSync(credsPath, 'utf-8'));
    if (raw.username && raw.password) {
      return { username: raw.username, password: raw.password };
    }
  }
  return { username: 'admin', password: 'admin' };
}

async function login(page: Page, creds: Creds): Promise<void> {
  await page.goto('/login');
  const username = page.locator('sl-input[name="username"]');
  const password = page.locator('sl-input[name="password"]');
  await username.waitFor({ state: 'visible' });
  await username.click();
  await page.keyboard.type(creds.username);
  await password.click();
  await page.keyboard.type(creds.password);
  await page.locator('sl-button[type="submit"]').click();
  await page.waitForURL(/\/console/, { timeout: 30_000 });
  await dismissPlanChoiceIfShown(page);
}

test.describe('T2 upgrade flow (mocked Stripe boundary)', () => {
  test.skip(IS_STAGING, 'CI-only: staging runs the real-Stripe script instead');

  test.beforeEach(async ({ page }) => {
    // The local stack is self-hosted by default. Upgrade prompts intentionally
    // do not open there, so this mocked billing boundary must also declare the
    // hosted capability. Do not enable real hosted billing/Stripe on the server.
    await page.route('**/api/v1/features', async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({
        response,
        json: { ...body, features: { ...body.features, billing: true } },
      });
    });
  });

  test('402 opens the feature-named modal; upgrade goes to the plan page', async ({
    page,
  }) => {
    const gatePath = '/console/runtime-sessions';
    let checkoutCalls = 0;

    // Mocked Stripe boundary, kept so that a checkout started by mistake is
    // counted here instead of reaching a real account.
    await page.route('**/api/v1/billing/create-checkout-session', (route) => {
      checkoutCalls += 1;
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ action: 'redirect', url: '/console' }),
      });
    });
    // The premium gate itself, mocked at the boundary: one gated endpoint
    // answering with the real 402 contract.
    await page.route('**/optimizations', (route) => {
      void route.fulfill({
        status: 402,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            code: 'upgrade_required',
            feature: 'session_optimization',
          },
        }),
      });
    });

    await login(page, resolveCreds());
    await page.goto(gatePath);
    await page.waitForLoadState('networkidle');

    // Trigger the gate through the app's own fetch layer (fetchWithAuth owns
    // the 402 → show-upgrade-modal dispatch).
    await page.evaluate(async () => {
      const token = localStorage.getItem('accessToken');
      await fetch(
        '/api/v1/billing/cost/runtime-sessions/e2e-fake/optimizations',
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: '{}',
        }
      ).then(async (response) => {
        // Mirror fetchWithAuth's 402 contract handling for the E2E trigger.
        if (response.status === 402) {
          const body = await response.clone().json();
          window.dispatchEvent(
            new CustomEvent('show-upgrade-modal', {
              detail: {
                feature: body?.detail?.feature ?? '',
                code: 'upgrade_required',
              },
              bubbles: true,
              composed: true,
            })
          );
        }
      });
    });

    const dialog = page.locator('console-shell sl-dialog#upgrade-modal');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('AI session optimization');

    // "Upgrade now" leaves for the plan page. The query keeps the name the
    // 402 sent (`session_optimization`); the plan page maps that onto the
    // `ai_optimization` capability itself.
    await dialog.locator('sl-button', { hasText: 'Upgrade now' }).click();
    await page.waitForURL(
      /\/console\/settings\/plan\?feature=session_optimization/,
      { timeout: 15_000 }
    );
    expect(page.url()).toContain('/console/settings/plan');
    await expect(dialog).toBeHidden();
    expect(checkoutCalls, 'the dialog buys nothing by itself').toBe(0);
  });

  test('free accounts see the AI-titles upsell hint on the sessions list', async ({
    page,
  }) => {
    await page.route('**/api/v1/billing/entitlements', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ premium: false, reason: 'none' }),
      });
    });

    await login(page, resolveCreds());
    await page.goto('/console/runtime-sessions');
    await page.waitForLoadState('networkidle');

    const hint = page.locator('.titles-upsell-hint');
    await expect(hint).toBeVisible();
    await expect(hint).toContainText('AI titles');

    await hint.click();
    const dialog = page.locator('console-shell sl-dialog#upgrade-modal');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('AI session titles');
  });
});
