import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for visual regression testing
 *
 * This config is used to ensure pixel-perfect rendering during the
 * slot-based refactoring for SEO optimization.
 */
export default defineConfig({
  testDir: './tests/visual',

  // Run tests in parallel
  fullyParallel: true,

  // Fail the build on CI if you accidentally left test.only in the source code
  forbidOnly: !!process.env.CI,

  // Retry on CI only
  retries: process.env.CI ? 2 : 0,

  // Opt out of parallel tests on CI
  workers: process.env.CI ? 1 : undefined,

  // Reporter to use
  reporter: [
    ['html', { outputFolder: 'playwright-report' }],
    ['list'],
  ],

  use: {
    // Base URL to use in actions like `await page.goto('/')`
    baseURL: 'http://localhost:5173',

    // Collect trace when retrying the failed test
    trace: 'on-first-retry',

    // Screenshot on failure
    screenshot: 'only-on-failure',

    // Video on failure
    video: 'retain-on-failure',
  },

  // Configure projects for major browsers
  projects: [
    {
      name: 'chromium',
      testDir: './tests/visual',
      use: {
        ...devices['Desktop Chrome'],
        // Wait for fonts to load
        launchOptions: {
          args: ['--font-render-hinting=none'],
        },
      },
    },

    {
      name: 'firefox',
      testDir: './tests/visual',
      use: { ...devices['Desktop Firefox'] },
    },

    {
      name: 'webkit',
      testDir: './tests/visual',
      use: { ...devices['Desktop Safari'] },
    },

    // Mobile viewports
    {
      name: 'mobile-chrome',
      testDir: './tests/visual',
      use: { ...devices['Pixel 5'] },
    },
    {
      name: 'mobile-safari',
      testDir: './tests/visual',
      use: { ...devices['iPhone 12'] },
    },

    // ------------------------------------------------------------------
    // E2E: custom-agent onboarding + per-run session correctness (T10)
    //
    // Two dedicated projects share the same spec (./tests/e2e) but target
    // different backends. They are NOT part of the default `npm run test:visual`
    // run; invoke them explicitly with `--project`.
    //
    //   CI project ('e2e-ci'):
    //     - Drives the local dev frontend (baseURL http://localhost:5173,
    //       proxied to a local backend on :8000).
    //     - Requires a local backend + Postgres + a fake upstream model server.
    //     - See tests/e2e/custom-agent-session.spec.ts header for the full
    //       harness bring-up. Run:
    //         npx playwright test --project=e2e-ci
    //
    //   Staging project ('e2e-staging'):
    //     - Runs the UI-only smoke variant against https://staging.preloop.ai
    //       (real backend, no fake upstream).
    //     - Reads credentials from ./tmp/staging-creds.json or env vars
    //       (PRELOOP_E2E_USERNAME / PRELOOP_E2E_PASSWORD). No secrets are
    //       committed. Run:
    //         npx playwright test --project=e2e-staging
    // ------------------------------------------------------------------
    {
      name: 'e2e-ci',
      testDir: './tests/e2e',
      use: {
        ...devices['Desktop Chrome'],
        baseURL: process.env.PRELOOP_E2E_BASE_URL || 'http://localhost:5173',
      },
    },
    {
      name: 'e2e-staging',
      testDir: './tests/e2e',
      use: {
        ...devices['Desktop Chrome'],
        baseURL: process.env.PRELOOP_E2E_BASE_URL || 'https://staging.preloop.ai',
      },
    },
  ],

  // Run your local dev server before starting the tests.
  //
  // Only the local-frontend projects (visual + e2e-ci) need this dev server.
  // The staging project hits a remote URL, so we skip the local webServer when
  // PRELOOP_E2E_TARGET=staging (set by the `test:e2e:staging` script).
  webServer:
    process.env.PRELOOP_E2E_TARGET === 'staging'
      ? undefined
      : {
          command: 'npm run dev',
          url: 'http://localhost:5173',
          reuseExistingServer: !process.env.CI,
          timeout: 120 * 1000,
        },
});
