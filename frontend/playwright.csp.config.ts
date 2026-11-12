import { defineConfig, devices } from '@playwright/test';

/**
 * Content-Security-Policy violation sweep (#2280). Not part of the visual or
 * e2e projects: run it with `npm run test:csp`, which builds dist-csp-sweep
 * first. The spec starts its own local servers, so there is no webServer.
 */
export default defineConfig({
  testDir: './tests/csp',
  testMatch: /.*\.spec\.ts$/,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list']],
  use: {
    ...devices['Desktop Chrome'],
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'csp-sweep', use: { ...devices['Desktop Chrome'] } }],
});
