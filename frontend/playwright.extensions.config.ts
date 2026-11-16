import { defineConfig, devices } from '@playwright/test';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const python =
  process.env.PRELOOP_E2E_PYTHON || resolve(root, '.venv/bin/python');
const apiPort = Number(process.env.PRELOOP_EXTENSION_API_PORT || 58147);
const port = Number(process.env.PRELOOP_EXTENSION_FRONTEND_PORT || 58148);

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: 'extension-inventory.spec.ts',
  workers: 1,
  retries: 0,
  reporter: [['list']],
  timeout: 120_000,
  use: {
    ...devices['Desktop Chrome'],
    baseURL: `http://localhost:${port}`,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  webServer: [
    {
      command: `${python} -m uvicorn tests.e2e_support.extension_server:create_fixture_app --factory --host 127.0.0.1 --port ${apiPort} --no-access-log`,
      cwd: root,
      url: `http://127.0.0.1:${apiPort}/health`,
      reuseExistingServer: false,
      env: {
        PYTHONPATH: resolve(root, 'backend'),
        PRELOOP_DISABLE_TELEMETRY: 'true',
        TESTING: 'true',
      },
    },
    {
      command: `npm run dev -- --host localhost --port ${port} --strictPort`,
      url: `http://localhost:${port}`,
      reuseExistingServer: false,
      env: {
        VITE_API_PROXY_TARGET: `http://127.0.0.1:${apiPort}`,
        PRELOOP_DISABLE_TELEMETRY: 'true',
      },
    },
  ],
});
