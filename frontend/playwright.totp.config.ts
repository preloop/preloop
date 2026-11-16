import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

const port = Number(process.env.PRELOOP_E2E_FRONTEND_PORT || 5173);
const baseURL = `http://localhost:${port}`;

/** The existing e2e-ci browser harness, restricted to the isolated MFA suite. */
export default defineConfig({
  ...base,
  testDir: './tests/e2e',
  testMatch: 'totp-sign-in.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL,
    // Proofs must not enter traces, videos or automatic failure screenshots.
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  projects: [
    { name: 'e2e-ci', use: { ...devices['Desktop Chrome'], baseURL } },
  ],
  webServer: {
    command:
      `npm run dev -- --host localhost --port ${port} --strictPort ` +
      '--config tests/e2e/vite.totp.config.ts',
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
