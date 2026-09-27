interface ImportMetaEnv {
  /**
   * Browser error-reporting DSN, read at build time. Unset (the default)
   * disables Sentry in the browser.
   */
  readonly VITE_SENTRY_DSN?: string;
}
