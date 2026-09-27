import * as Sentry from '@sentry/browser';

/** Sample rate for browser performance traces when error reporting is on. */
export const SENTRY_TRACES_SAMPLE_RATE = 0.01;

/** Signature of `Sentry.init`, injectable so tests never report anything. */
export type SentryInitFn = (options: Sentry.BrowserOptions) => unknown;

/**
 * Normalise the build-time DSN. Empty or whitespace-only values mean
 * "no browser error reporting", which is the default for every build that
 * does not set `VITE_SENTRY_DSN`.
 */
export function resolveSentryDsn(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const dsn = raw.trim();
  return dsn ? dsn : null;
}

/**
 * Start browser error reporting when the build was given a DSN.
 *
 * The DSN comes from the `VITE_SENTRY_DSN` environment variable at build
 * time, so the source carries no reporting endpoint and a self-hosted build
 * reports nowhere unless its operator opts in.
 *
 * @returns true when Sentry was initialised.
 */
export function initSentry(
  rawDsn: unknown,
  environment: string,
  init: SentryInitFn = Sentry.init
): boolean {
  const dsn = resolveSentryDsn(rawDsn);
  if (!dsn) return false;
  init({
    dsn,
    tracesSampleRate: SENTRY_TRACES_SAMPLE_RATE,
    environment,
  });
  return true;
}
