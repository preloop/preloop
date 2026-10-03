import { playwrightLauncher } from '@web/test-runner-playwright';
import { testConcurrency } from './scripts/test-concurrency.mjs';

const concurrency = testConcurrency();

export default {
  ...(concurrency === undefined ? {} : { concurrency }),
  files: ['src/**/*.test.ts'],
  nodeResolve: true,
  testsFinishTimeout: 180000, // 3 min for CI; some tests (e.g. mcp-server-form) can be slow
  browsers: [
    playwrightLauncher({ product: 'chromium' }),
    playwrightLauncher({ product: 'firefox' }),
    playwrightLauncher({ product: 'webkit' }),
  ],
  coverage: true,
  coverageConfig: {
    reporters: ['text', 'lcov'],
    threshold: {
      statements: 80,
      branches: 80,
      functions: 80,
      lines: 80,
    },
  },
};
