import { defineConfig, mergeConfig } from 'vite';
import base from '../../vite.config.ts';

/** Include the instance OAuth proxy for real CLI browser sign-in. */
export default mergeConfig(
  base,
  defineConfig({
    server: {
      proxy: {
        '/oauth': {
          target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000',
          changeOrigin: true,
        },
      },
    },
  })
);
