import { defineConfig } from 'vite';
import { resolve } from 'path';
import { fileURLToPath } from 'url';
import cssInjectedByJsPlugin from 'vite-plugin-css-injected-by-js';
import { brandPlugin } from './vite-plugin-brand';
import { resolveAllowedHosts } from './src/vite-allowed-hosts';

const __dirname = fileURLToPath(new URL('.', import.meta.url));

// Get brand from environment variable, default to 'preloop'
const brand = process.env.VITE_BRAND || 'preloop';
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000';
const adminProxyTarget = process.env.VITE_ADMIN_PROXY_TARGET || 'http://127.0.0.1:5175';
const gatewayProxyTarget = process.env.VITE_GATEWAY_PROXY_TARGET || apiProxyTarget;
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || 5173);
const hmrHost = process.env.VITE_HMR_HOST;
const hmrProtocol = process.env.VITE_HMR_PROTOCOL;
const allowedHosts = resolveAllowedHosts({
  allowedHosts: process.env.VITE_ALLOWED_HOSTS,
  additionalHosts: process.env.__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS,
  hmrHost,
  apiUrl: process.env.VITE_API_URL,
  apiProxyTarget,
});

/**
 * Default Vite configuration for Preloop Open Source / Self-Hosted edition
 *
 * This build:
 * - Redirects landing page to login (no marketing content)
 * - Removes pricing page
 * - Uses minimal branding configuration
 *
 * For SaaS builds, set VITE_BRAND environment variable
 */
export default defineConfig({
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: 'index.html',
      },
    },
  },
  plugins: [
    cssInjectedByJsPlugin({
      jsAssetsFilterFunction: (chunk) => {
        return /main/.test(chunk.fileName);
      },
    }),
    brandPlugin(brand),
  ],
  resolve: {
    alias: [
      { find: 'events', replacement: 'events' },
      // Ensure packages resolve from frontend's node_modules for plugins outside this package
      { find: /^lit($|\/)/, replacement: resolve(__dirname, 'node_modules/lit$1') },
      {
        find: /^@shoelace-style\/shoelace(\/.*)?$/,
        replacement: resolve(__dirname, 'node_modules/@shoelace-style/shoelace$1'),
      },
      {
        find: /^@lit\/reactive-element(\/.*)?$/,
        replacement: resolve(__dirname, 'node_modules/@lit/reactive-element$1'),
      },
    ],
    // Dedupe ensures only one copy of these packages is used
    dedupe: ['lit', '@lit/reactive-element', 'lit-element', 'lit-html', '@shoelace-style/shoelace'],
  },
  optimizeDeps: {
    include: ['lit', 'lit/decorators.js'],
  },
  server: {
    ...(allowedHosts ? { allowedHosts } : {}),
    hmr: {
      clientPort:
        Number.isFinite(hmrClientPort) && hmrClientPort > 0 ? hmrClientPort : 5173,
      ...(hmrHost ? { host: hmrHost } : {}),
      ...(hmrProtocol === 'ws' || hmrProtocol === 'wss'
        ? { protocol: hmrProtocol }
        : {}),
    },
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      '/mcp': {
        target: apiProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      '/admin': {
        target: adminProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      '/docs': {
        target: apiProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      '/openai': {
        target: gatewayProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      '/anthropic': {
        target: gatewayProxyTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
