import { playwrightLauncher } from '@web/test-runner-playwright';
import { esbuildPlugin } from '@web/dev-server-esbuild';
import fs from 'fs/promises';
import path from 'path';

const headed = process.env.HEADED === 'true';

const cssInlinePlugin = {
  name: 'css-inline-plugin',
  async serve(context) {
    if (!context.url.endsWith('.css?inline')) return undefined;

    const cssUrl = context.url.replace('?inline', '');
    const filePath = path.resolve(process.cwd(), `.${cssUrl}`);
    const cssText = await fs.readFile(filePath, 'utf-8');

    return {
      body: `export default ${JSON.stringify(cssText)};`,
      type: 'js',
    };
  },
};

export default {
  plugins: [
    cssInlinePlugin,
    esbuildPlugin({
      ts: true,
      tsconfig: './tsconfig.json',
      target: 'es2020',
      // Vite substitutes this in `npm run build`; the test runner serves the
      // same dependency sources untouched, so a library guarded by
      // `process.env.NODE_ENV` (table-core's debug logging) threw
      // "process is not defined" in the browser. Substituting the same value
      // the build uses keeps the two environments on one code path.
      define: { 'process.env.NODE_ENV': '"production"' },
    }),
  ],
  browsers: [playwrightLauncher({
    product: 'chromium',
    launchOptions: { headless: !headed }
  })],
  testFramework: {
    config: {
      timeout: '240000',
    },
  },
  // Invalidate short-TTL API caches between tests (features/profile caches
  // otherwise leak stubbed edition flags across cases).
  testRunnerHtml: (testFramework) => `
<!DOCTYPE html>
<html>
  <body>
    <script type="module" src="${testFramework}"></script>
    <script type="module" src="/src/test-setup.ts"></script>
  </body>
</html>
`,
  filterBrowserLogs: () => true,
  nodeResolve: true,
};
