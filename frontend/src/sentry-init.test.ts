import { expect } from '@open-wc/testing';
import {
  SENTRY_TRACES_SAMPLE_RATE,
  initSentry,
  resolveSentryDsn,
} from './sentry-init';

describe('sentry-init', () => {
  describe('resolveSentryDsn', () => {
    it('treats a missing or blank value as disabled', () => {
      expect(resolveSentryDsn(undefined)).to.equal(null);
      expect(resolveSentryDsn(null)).to.equal(null);
      expect(resolveSentryDsn('')).to.equal(null);
      expect(resolveSentryDsn('   ')).to.equal(null);
      expect(resolveSentryDsn(42)).to.equal(null);
    });

    it('trims a configured DSN', () => {
      expect(resolveSentryDsn('  https://key@errors.example.com/1 ')).to.equal(
        'https://key@errors.example.com/1'
      );
    });
  });

  describe('initSentry', () => {
    it('does not initialise Sentry without a DSN', () => {
      const calls: unknown[] = [];
      const started = initSentry(undefined, 'production', (options) =>
        calls.push(options)
      );
      expect(started).to.equal(false);
      expect(calls).to.have.length(0);
    });

    it('initialises Sentry with the build-time DSN and environment', () => {
      const calls: Array<Record<string, unknown>> = [];
      const started = initSentry(
        'https://key@errors.example.com/1',
        'staging',
        (options) => calls.push(options as Record<string, unknown>)
      );
      expect(started).to.equal(true);
      expect(calls).to.deep.equal([
        {
          dsn: 'https://key@errors.example.com/1',
          tracesSampleRate: SENTRY_TRACES_SAMPLE_RATE,
          environment: 'staging',
        },
      ]);
    });
  });
});
