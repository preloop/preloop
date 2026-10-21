import { defineConfig } from 'vitest/config';
import { playwright } from '@vitest/browser-playwright';

export default defineConfig({
  define: { 'import.meta.env.DEV': 'false' },
  optimizeDeps: {
    include: [
      '@shoelace-style/shoelace/dist/components/**/*.js',
      'lit',
      'lit/directives/*.js',
    ],
    noDiscovery: true,
  },
  test: {
    include: ['tests/unit/**/*.test.ts'],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: 'chromium' }],
    },
  },
});
